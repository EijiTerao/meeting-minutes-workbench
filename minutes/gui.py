"""Small, local-first desktop interface for preparing meeting minutes."""

from __future__ import annotations

import json
import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any

from .core import load_profile, render
from .engine import REFERENCE_BUDGET, read_input, transcribe
from .routes import Settings, Stage, default_path, load_settings, save_settings
from .workflow import run_workflow


class MinutesApp:
    def __init__(self, root: tk.Tk, *, config_path: Path | None = None, show_setup: bool = True) -> None:
        self.root = root
        self.config_path = config_path
        self.settings_load_error = ""
        try:
            self.settings = load_settings(config_path)
        except ValueError as exc:
            self.settings = Settings()
            self.settings_load_error = str(exc)
        self.root.title("議事録")
        self.root.minsize(760, 680)
        self.events: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.busy = False
        self.references: list[Path] = []
        self.reference_texts: tuple[str, ...] = ()
        self.profile = load_profile(None)
        self.confirmed_text = ""
        self.settings_window: tk.Toplevel | None = None
        self.confirmed_window: tk.Toplevel | None = None
        self.request_id = 0
        self.result_valid = False
        self.input_controls: list[Any] = []
        self._build()
        self._sync_settings_controls()
        if self.settings_load_error:
            self.status.configure(text="設定を読み込めませんでした。処理は無効です。「処理の設定」で修正してください。")
        self.root.after(100, self._poll)
        setup_path = Path(config_path) if config_path is not None else default_path()
        if show_setup and (self.settings_load_error or not setup_path.exists()):
            self.root.after(150, self._open_settings)

    def _build(self) -> None:
        outer = ttk.Frame(self.root, padding=14)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(2, weight=1)
        ttk.Label(outer, text="1. 文字起こし", font=("TkDefaultFont", 13, "bold")).grid(row=0, column=0, sticky="w")
        top = ttk.Frame(outer)
        top.grid(row=1, column=0, sticky="ew", pady=(8, 10))
        top.columnconfigure(1, weight=1)
        ttk.Label(top, text="タイトル").grid(row=0, column=0, sticky="w", padx=(0, 8))
        self.title_value = tk.StringVar()
        self.title = ttk.Entry(top, textvariable=self.title_value)
        self.title.grid(row=0, column=1, sticky="ew")
        self.input_controls.append(self.title)
        self.open_transcript_button = ttk.Button(top, text="テキストを開く", command=self._open_transcript)
        self.open_transcript_button.grid(row=0, column=2, padx=(8, 0))
        self.input_controls.append(self.open_transcript_button)
        self.transcript = tk.Text(outer, height=10, wrap="word", undo=True)
        self.transcript.grid(row=2, column=0, sticky="nsew")
        self.transcript.bind("<<Modified>>", self._transcript_changed)
        self.input_controls.append(self.transcript)
        scroll = ttk.Scrollbar(outer, orient="vertical", command=self.transcript.yview)
        scroll.grid(row=2, column=1, sticky="ns")
        self.transcript.configure(yscrollcommand=scroll.set)
        refs = ttk.Frame(outer)
        refs.grid(row=3, column=0, sticky="ew", pady=(7, 12))
        self.ref_label = ttk.Label(refs, text="参考資料：なし（任意・最大3件）")
        self.ref_label.pack(side="left", fill="x", expand=True)
        self.reference_button = ttk.Button(refs, text="参考資料を選ぶ", command=self._open_references)
        self.reference_button.pack(side="right")
        self.confirmed_button = ttk.Button(refs, text="確認済み事実を追加", command=self._edit_confirmed)
        self.confirmed_button.pack(side="right", padx=(0, 6))
        self.remove_reference_button = ttk.Button(refs, text="参考資料を外す", command=self._remove_references)
        self.remove_reference_button.pack(side="right", padx=(0, 6))
        self.input_controls.extend((self.reference_button, self.remove_reference_button, self.confirmed_button))
        settings = ttk.Frame(outer)
        settings.grid(row=4, column=0, sticky="ew", pady=(0, 12))
        self.settings_button = ttk.Button(settings, text="処理の設定", command=self._open_settings)
        self.settings_button.pack(side="left")
        self.settings_summary = ttk.Label(settings, text="設定：未設定（読み込み・手動入力で利用できます）")
        self.settings_summary.pack(side="left", padx=(8, 0))
        self.profile_label = ttk.Label(settings, text="書式設定：なし")
        self.profile_label.pack(side="right")
        self.profile_button = ttk.Button(settings, text="profile JSONを選ぶ", command=self._open_profile)
        self.profile_button.pack(side="right", padx=(0, 8))
        self.input_controls.extend((self.profile_button, self.settings_button))
        actions = ttk.Frame(outer)
        actions.grid(row=5, column=0, sticky="ew", pady=(0, 10))
        ttk.Label(actions, text="2. 生成して確認", font=("TkDefaultFont", 13, "bold")).pack(side="left")
        self.json_button = ttk.Button(actions, text="構造化JSONを開く", command=self._open_json)
        self.json_button.pack(side="right")
        self.generate_button = ttk.Button(actions, text="生成", command=self._generate)
        self.generate_button.pack(side="right", padx=(0, 8))
        self.transcribe_button = ttk.Button(actions, text="WAVを文字起こし", command=self._transcribe_wav)
        self.transcribe_button.pack(side="right", padx=(0, 8))
        self.input_controls.append(self.transcribe_button)
        self.status = ttk.Label(outer, text="入力を確認して生成してください。", foreground="#555")
        self.status.grid(row=6, column=0, sticky="w", pady=(0, 6))
        self.tabs = ttk.Notebook(outer)
        self.tabs.grid(row=7, column=0, sticky="nsew")
        outer.rowconfigure(7, weight=1)
        self.body = self._output_tab("本文")
        self.open_items = self._output_tab("未決事項")
        self.corrected_transcript = self._output_tab("補正後の文字起こし")
        footer = ttk.Frame(outer)
        footer.grid(row=8, column=0, sticky="ew", pady=(8, 0))
        ttk.Label(footer, text="3. 必要な内容を編集して取り出す").pack(side="left")
        self.body_copy_button = ttk.Button(footer, text="本文をコピー", command=lambda: self._copy(self.body))
        self.body_copy_button.pack(side="right", padx=(6, 0))
        self.open_copy_button = ttk.Button(footer, text="未決事項をコピー", command=lambda: self._copy(self.open_items))
        self.open_copy_button.pack(side="right", padx=(6, 0))
        self.open_save_button = ttk.Button(footer, text="未決事項を保存", command=lambda: self._save(self.open_items))
        self.open_save_button.pack(side="right", padx=(6, 0))
        self.body_save_button = ttk.Button(footer, text="本文を保存", command=lambda: self._save(self.body))
        self.body_save_button.pack(side="right")
        self.output_actions = (self.body_copy_button, self.open_copy_button, self.open_save_button, self.body_save_button)
        self._set_result_actions(False)
        self.title_value.trace_add("write", self._input_changed)

    def _sync_settings_controls(self) -> None:
        s = self.settings
        self.transcribe_button.configure(state="normal" if s.transcription == "whisper" else "disabled")
        enabled = [label for name, label in (("correction", "文字起こし補正"), ("generation", "議事録生成"), ("verification", "根拠確認")) if getattr(s, name).provider != "off"]
        self.generate_button.configure(state="normal" if self.settings.generation.provider != "off" else "disabled")
        self.settings_summary.configure(text="設定：" + ("、".join(enabled) if enabled else "処理なし（読み込み・手動入力のみ）"))

    def _open_settings(self) -> None:
        if self.busy:
            return
        if self.settings_window is not None and self.settings_window.winfo_exists():
            self.settings_window.lift()
            return
        window = tk.Toplevel(self.root)
        self.settings_window = window
        window.title("処理の設定")
        window.transient(self.root)
        window.resizable(True, False)
        frame = ttk.Frame(window, padding=14)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(1, weight=1)
        s = self.settings
        self._settings_vars = {}
        ttk.Label(frame, text="音声入力").grid(row=0, column=0, sticky="w", padx=(0, 10), pady=3)
        transcription = tk.StringVar(value=s.transcription)
        input_values = {"読み込み・手入力": "import", "WAVを端末内で文字起こし": "whisper"}
        input_display = tk.StringVar(value=next(k for k, v in input_values.items() if v == s.transcription))
        ttk.Combobox(frame, textvariable=input_display, values=tuple(input_values), state="readonly", width=27).grid(row=0, column=1, sticky="w")
        self._settings_vars["transcription"] = (transcription, input_display, input_values)
        self._path_var = tk.StringVar(value=s.whisper)
        ttk.Label(frame, text="Whisper実行ファイル").grid(row=1, column=0, sticky="w", padx=(0, 10), pady=3)
        path_row = ttk.Frame(frame)
        path_row.grid(row=1, column=1, sticky="ew")
        path_row.columnconfigure(0, weight=1)
        ttk.Entry(path_row, textvariable=self._path_var).grid(row=0, column=0, sticky="ew")
        ttk.Button(path_row, text="選ぶ", command=self._choose_whisper).grid(row=0, column=1, padx=(5, 0))
        ttk.Label(frame, text="Whisperモデル").grid(row=2, column=0, sticky="w", padx=(0, 10), pady=3)
        self._whisper_model = tk.StringVar(value=s.whisper_model)
        model_row = ttk.Frame(frame)
        model_row.grid(row=2, column=1, sticky="ew")
        model_row.columnconfigure(0, weight=1)
        ttk.Entry(model_row, textvariable=self._whisper_model).grid(row=0, column=0, sticky="ew")
        ttk.Button(model_row, text="選ぶ", command=self._choose_whisper_model).grid(row=0, column=1, padx=(5, 0))
        language_values = {"日本語 (ja)": "ja", "英語 (en)": "en", "自動判定 (auto)": "auto"}
        self._language = tk.StringVar(value=next((k for k, v in language_values.items() if v == s.language), "日本語 (ja)"))
        ttk.Label(frame, text="文字起こしの言語").grid(row=3, column=0, sticky="w", padx=(0, 10), pady=3)
        ttk.Combobox(frame, textvariable=self._language, values=tuple(language_values), state="readonly", width=27).grid(row=3, column=1, sticky="w")
        ttk.Label(frame, text="Whisperは選択したWAVをこの端末内で処理します。設定保存時に実行しません。", wraplength=430).grid(row=4, column=0, columnspan=2, sticky="w", pady=(2, 8))
        self._stage_vars = {}
        labels = (("correction", "文字起こし補正"), ("generation", "議事録生成"), ("verification", "根拠確認"))
        row = 5
        for name, label in labels:
            stage = getattr(s, name)
            ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w", padx=(0, 10), pady=3)
            line = ttk.Frame(frame)
            line.grid(row=row, column=1, sticky="ew", pady=3)
            provider_values = {"使わない": "off", "Ollama（ローカル）": "ollama", "OpenAI（オンライン）": "openai"}
            provider_display = tk.StringVar(value=next(k for k, v in provider_values.items() if v == stage.provider))
            model = tk.StringVar(value=stage.model)
            ttk.Combobox(line, textvariable=provider_display, values=tuple(provider_values), state="readonly", width=24).pack(side="left")
            ttk.Entry(line, textvariable=model).pack(side="left", fill="x", expand=True, padx=(6, 0))
            self._stage_vars[name] = (provider_display, provider_values, model)
            row += 1
        ttk.Label(frame, text="Ollamaは端末内で処理します。OpenAIは実行時に確認後、api.openai.comへ送信し、料金が発生する場合があります。補正には文字起こし、生成には補正後の文字起こし・確認済み事実・参考資料、根拠確認には生成結果と根拠（該当する確認済み事項を含む）が送信対象です。音声ファイルは送信しません。", wraplength=470, justify="left").grid(row=row, column=0, columnspan=2, sticky="w", pady=(8, 4))
        row += 1
        ttk.Label(frame, text="offの段階は実行されません。各モデル名を個別に指定してください。設定保存だけでは処理を開始しません。", wraplength=470, justify="left").grid(row=row, column=0, columnspan=2, sticky="w", pady=(0, 8))
        row += 1
        actions = ttk.Frame(frame)
        actions.grid(row=row, column=0, columnspan=2, sticky="e")
        ttk.Button(actions, text="後で設定", command=window.destroy).pack(side="right")
        ttk.Button(actions, text="設定を保存", command=lambda: self._save_settings_dialog(window)).pack(side="right", padx=(0, 8))

    def _choose_whisper(self) -> None:
        path = filedialog.askopenfilename(title="Whisper実行ファイルを選ぶ")
        if path:
            self._path_var.set(path)

    def _choose_whisper_model(self) -> None:
        path = filedialog.askopenfilename(title="Whisperモデルファイルを選ぶ")
        if path:
            self._whisper_model.set(path)

    def _save_settings_dialog(self, window: tk.Toplevel) -> None:
        if self.busy:
            messagebox.showinfo("処理中", "処理中は設定を保存できません。", parent=window)
            return
        try:
            stage_values = {}
            for name, (display, choices, model) in self._stage_vars.items():
                selected = choices[display.get()]
                stage_values[name] = Stage(selected, model.get().strip() if selected != "off" else "")
            _, input_display, input_values = self._settings_vars["transcription"]
            transcription = input_values[input_display.get()]
            language_values = {"日本語 (ja)": "ja", "英語 (en)": "en", "自動判定 (auto)": "auto"}
            settings = Settings(transcription, self._path_var.get().strip() if transcription == "whisper" else "", self._whisper_model.get().strip() if transcription == "whisper" else "", language_values[self._language.get()], **stage_values)
            save_settings(settings, self.config_path)
        except (ValueError, OSError) as exc:
            messagebox.showerror("設定エラー", f"設定を保存できませんでした。処理は開始していません。\n{exc}", parent=window)
            return
        self.settings = settings
        self._sync_settings_controls()
        window.destroy()
        self._invalidate_result("処理の設定を変更しました。")

    def _transcribe_wav(self) -> None:
        if self.busy or self.settings.transcription != "whisper":
            return
        path = filedialog.askopenfilename(title="文字起こしするWAVを選ぶ", filetypes=[("WAV音声", "*.wav"), ("すべて", "*.*")])
        if not path:
            return
        has_input = bool(self.transcript.get("1.0", "end-1c").strip() or self.title.get().strip() or self.references)
        if has_input and not messagebox.askyesno("入力を置き換える", "現在の入力を選択したWAVの結果で置き換えますか？", parent=self.root):
            return
        self._invalidate_result("WAVを文字起こししています。")
        self.busy = True
        self._set_busy(True)
        request_id = self.request_id = self.request_id + 1
        settings = self.settings
        def work() -> None:
            try:
                text = transcribe(Path(path), Path(settings.whisper), Path(settings.whisper_model), settings.language)
                self.events.put(("transcribed", (request_id, text, Path(path).stem)))
            except Exception as exc:
                self.events.put(("transcription_error", (request_id, str(exc))))
        threading.Thread(target=work, daemon=True).start()

    def _edit_confirmed(self) -> None:
        if self.busy:
            return
        if self.confirmed_window is not None and self.confirmed_window.winfo_exists():
            self.confirmed_window.lift()
            return
        window = tk.Toplevel(self.root)
        self.confirmed_window = window
        window.title("確認済み事実")
        window.transient(self.root)
        window.grab_set()
        ttk.Label(window, text="今回の生成にだけ使う事実を入力します。自動保存しません。", padding=10).pack(anchor="w")
        text = tk.Text(window, width=72, height=8, wrap="word")
        text.pack(fill="both", expand=True, padx=10)
        text.insert("1.0", self.confirmed_text)
        def keep() -> None:
            self.confirmed_text = text.get("1.0", "end-1c").strip()
            self.confirmed_button.configure(text="確認済み事実を編集" if self.confirmed_text else "確認済み事実を追加")
            self._invalidate_result("確認済み事実を変更しました。")
            window.destroy()
        actions = ttk.Frame(window)
        actions.pack(anchor="e", padx=10, pady=10)
        ttk.Button(actions, text="閉じる", command=window.destroy).pack(side="right")
        ttk.Button(actions, text="この会話だけに使う", command=keep).pack(side="right", padx=(0, 8))
        window.protocol("WM_DELETE_WINDOW", window.destroy)

    def _clear_confirmed(self) -> None:
        self.confirmed_text = ""
        self.confirmed_button.configure(text="確認済み事実を追加")

    def _output_tab(self, name: str) -> tk.Text:
        frame = ttk.Frame(self.tabs)
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        text = tk.Text(frame, wrap="word", undo=True)
        text.grid(row=0, column=0, sticky="nsew")
        bar = ttk.Scrollbar(frame, command=text.yview)
        bar.grid(row=0, column=1, sticky="ns")
        text.configure(yscrollcommand=bar.set)
        self.tabs.add(frame, text=name)
        return text

    @staticmethod
    def _set_text(widget: tk.Text, value: str) -> None:
        widget.delete("1.0", "end")
        widget.insert("1.0", value)

    def _open_transcript(self) -> None:
        path = filedialog.askopenfilename(title="文字起こしを選ぶ", filetypes=[("テキスト", "*.txt *.srt *.vtt"), ("すべて", "*.*")])
        if not path:
            return
        try:
            content = read_input(Path(path))
        except (OSError, UnicodeError, ValueError, RuntimeError) as exc:
            messagebox.showerror("読込エラー", f"文字起こしを読み込めませんでした。\n{exc}")
            return
        self._set_text(self.transcript, content)
        self.transcript.edit_reset()
        self.transcript.edit_modified(False)
        self._clear_confirmed()
        self.title.delete(0, "end")
        self.title.insert(0, Path(path).stem)
        self._remove_references(silent=True)
        self._invalidate_result("別の文字起こしを開きました。参考資料は解除しました。")

    def _open_references(self) -> None:
        paths = filedialog.askopenfilenames(title="参考資料を選ぶ（最大3件、合計6KBまで）", filetypes=[("テキスト", "*.txt"), ("すべて", "*.*")])
        if not paths:
            return
        selected = [Path(p) for p in paths]
        if len(selected) > 3:
            messagebox.showerror("件数超過", "参考資料は最大3件です。選び直してください。")
            return
        try:
            snapshots = tuple(read_input(path, max_bytes=REFERENCE_BUDGET) for path in selected)
        except (OSError, UnicodeError, ValueError, RuntimeError) as exc:
            messagebox.showerror("参考資料の読込エラー", f"選択した参考資料を読み込めませんでした。\n{exc}")
            return
        total_bytes = sum(len(content.encode("utf-8")) for content in snapshots)
        if total_bytes > REFERENCE_BUDGET:
            messagebox.showerror("容量超過", "参考資料の合計は6KBまでです。選び直してください。")
            return
        self.references = selected
        self.reference_texts = snapshots
        self.ref_label.configure(text="参考資料：" + ("、".join(p.name for p in selected) if selected else "なし"))
        self._invalidate_result("参考資料を変更しました。")

    def _remove_references(self, silent: bool = False) -> None:
        self.references = []
        self.reference_texts = ()
        self.ref_label.configure(text="参考資料：なし（任意・最大3件）")
        if not silent:
            self._invalidate_result("参考資料を外しました。")

    def _open_profile(self) -> None:
        path = filedialog.askopenfilename(title="profile JSONを選ぶ", filetypes=[("JSON", "*.json")])
        if not path:
            return
        try:
            profile = load_profile(Path(path))
        except (OSError, UnicodeError, ValueError, RuntimeError) as exc:
            messagebox.showerror("設定の読込エラー", f"profile JSONを読み込めませんでした。\n{exc}")
            return
        self.profile = profile
        self.profile_label.configure(text=f"書式設定：{Path(path).name}")
        self._invalidate_result("書式設定を変更しました。")

    def _open_json(self) -> None:
        path = filedialog.askopenfilename(title="構造化JSONを選ぶ", filetypes=[("JSON", "*.json")])
        if not path:
            return
        self._invalidate_result("選んだ議事録JSONを確認しています。")
        try:
            payload = json.loads(read_input(Path(path)))
            title = self.title.get().strip() or "会議メモ"
            body, open_text = render(payload, title, self.profile)
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError, RuntimeError, RecursionError) as exc:
            messagebox.showerror("JSONの確認エラー", f"構造化JSONを確認できませんでした。\n{exc}")
            return
        self._set_text(self.body, body)
        self._set_text(self.open_items, open_text)
        self.result_valid = True
        self._set_result_actions(True)
        self.status.configure(text="JSONを確認し、本文と未決事項に整形しました。必要に応じて編集してください。")
        self.tabs.select(0)

    def _generate(self) -> None:
        if self.busy:
            return
        if ((self.settings_window is not None and self.settings_window.winfo_exists())
                or (self.confirmed_window is not None and self.confirmed_window.winfo_exists())):
            messagebox.showinfo("入力編集中", "設定または確認済み事実の編集を閉じてから生成してください。")
            return
        transcript = self.transcript.get("1.0", "end-1c")
        title = self.title.get().strip()
        if not title:
            messagebox.showinfo("タイトル", "タイトルを入力してください。")
            return
        if not transcript.strip():
            messagebox.showinfo("文字起こし", "文字起こしを貼り付けるか、テキストファイルを開いてください。")
            return
        if self.settings.generation.provider == "off":
            messagebox.showinfo("処理の設定", "「処理の設定」で議事録生成の実行先とモデルを設定してください。")
            return
        openai_stages = [name for name in ("correction", "generation", "verification") if getattr(self.settings, name).provider == "openai"]
        authorized = False
        if openai_stages:
            stages = {"correction": "文字起こし補正", "generation": "議事録生成", "verification": "確認"}
            details = "、".join(stages[name] for name in openai_stages)
            if not messagebox.askyesno("OpenAIへの送信を確認", f"今回、{details}の入力を api.openai.com へ送信します。料金が発生する場合があります。\n\n校正には文字起こし、生成には会議情報・確認済み事実・参考資料、根拠確認には主張と該当する原文・確認済み事実を使います。音声ファイルは送信しません。\n\n送信して実行しますか？", parent=self.root):
                return
            authorized = True
        profile = self.profile
        refs = self.reference_texts
        settings = self.settings
        confirmed = self.confirmed_text
        self.busy = True
        self._set_busy(True)
        self._invalidate_result("生成中です。以前の結果はコピー・保存できません。")
        self.status.configure(text="設定した段階を実行中です。入力は保持されています…")
        self.request_id += 1
        request_id = self.request_id

        def work() -> None:
            try:
                result = run_workflow(transcript, title, settings, profile, refs, confirmed, authorized=authorized)
                body, open_text = render(result.minutes, title, profile)
                self.events.put(("ok", (request_id, body, open_text, result)))
            except (OSError, UnicodeError, ValueError, RuntimeError) as exc:
                self.events.put(("error", (request_id, str(exc))))
            except Exception:
                self.events.put(("error", (request_id, "予期しないエラーが発生しました。入力は保持されています。")))

        threading.Thread(target=work, daemon=True).start()

    def _set_busy(self, busy: bool) -> None:
        state = "disabled" if busy else "normal"
        self.generate_button.configure(state=state)
        self.json_button.configure(state=state)
        for control in self.input_controls:
            control.configure(state=state)
        for output in (self.body, self.open_items, self.corrected_transcript):
            output.configure(state=state)
        if not busy:
            self._sync_settings_controls()
        if not busy:
            self._set_result_actions(self.result_valid)
        else:
            self._set_result_actions(False)

    def _set_result_actions(self, enabled: bool) -> None:
        state = "normal" if enabled and not self.busy else "disabled"
        for button in self.output_actions:
            button.configure(state=state)

    def _invalidate_result(self, message: str) -> None:
        self.result_valid = False
        self._set_result_actions(False)
        self.status.configure(text=message + " 前回の結果は画面に残していますが、コピー・保存できません。")

    def _input_changed(self, *_args: object) -> None:
        if self.result_valid:
            self._invalidate_result("入力を変更しました。")

    def _transcript_changed(self, _event: Any) -> None:
        if self.transcript.edit_modified():
            self.transcript.edit_modified(False)
            self._input_changed()

    @staticmethod
    def _show_ollama_help() -> None:
        messagebox.showinfo(
            "Ollamaのクラウド機能を停止",
            "この画面は外部サービスへ送信せず、起動済みのローカルOllamaへ接続します。"
            "\nOllama自体のクラウド機能も止める設定は、公式FAQの「How do I disable Ollama's cloud features?」を確認してください。"
            "\nhttps://docs.ollama.com/faq#how-do-i-disable-ollamas-cloud-features",
        )

    def _poll(self) -> None:
        try:
            while True:
                kind, data = self.events.get_nowait()
                request_id = data[0]
                if request_id != self.request_id:
                    continue
                self.busy = False
                self._set_busy(False)
                if kind == "ok":
                    _, body, open_text, result = data
                    self._set_text(self.body, body)
                    self._set_text(self.open_items, open_text)
                    self._set_text(self.corrected_transcript, result.corrected)
                    self.result_valid = True
                    self._set_result_actions(True)
                    elapsed = sum(result.seconds.values())
                    uncorrected = f" {result.corrections_rejected}箇所は原文を維持。" if result.corrections_rejected else ""
                    excluded = f" 根拠確認で{result.verification_rejected}件を除外。" if result.verification_rejected else ""
                    self.status.configure(text=f"生成しました（AI処理 {elapsed:.1f}秒）。{uncorrected}{excluded}本文・未決事項を確認してください。")
                    self.tabs.select(0)
                elif kind == "transcribed":
                    _, text, title = data
                    self._set_text(self.transcript, text)
                    self.transcript.edit_reset()
                    self.transcript.edit_modified(False)
                    self._clear_confirmed()
                    self.title.delete(0, "end")
                    self.title.insert(0, title)
                    self._remove_references(silent=True)
                    self._invalidate_result("WAVの文字起こしを完了しました。参考資料は解除しました。")
                elif kind == "transcription_error":
                    self.status.configure(text="WAVの文字起こしに失敗しました。入力は保持されています。")
                    messagebox.showerror("文字起こしエラー", f"WAVを文字起こしできませんでした。入力は保持されています。\n{data[1]}")
                else:
                    self.status.configure(text="生成できませんでした。入力は保持されています。")
                    messagebox.showerror("処理エラー", f"処理を完了できませんでした。入力は保持されています。\n{data[1]}")
        except queue.Empty:
            pass
        self.root.after(100, self._poll)

    def _copy(self, widget: tk.Text) -> None:
        if not self.result_valid or self.busy:
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(widget.get("1.0", "end-1c"))
        self.status.configure(text="クリップボードにコピーしました。")

    def _save(self, widget: tk.Text) -> None:
        if not self.result_valid or self.busy:
            return
        path = filedialog.asksaveasfilename(title="保存先を選ぶ", defaultextension=".md", filetypes=[("Markdown", "*.md"), ("テキスト", "*.txt")])
        if not path:
            return
        destination = Path(path)
        content = widget.get("1.0", "end-1c")
        if destination.suffix.lower() == ".md":
            # Indented code blocks keep generated or edited text literal in
            # Markdown; do not interpret HTML, embeds, or external images.
            content = "\n".join(("    " + line) if line else "    " for line in content.splitlines())
        try:
            # Exclusive creation avoids replacing an existing file, even if a
            # platform's save dialog does not expose overwrite confirmation.
            with destination.open("x", encoding="utf-8", newline="") as stream:
                stream.write(content)
        except FileExistsError:
            messagebox.showinfo("既存ファイル", "既存ファイルには保存しません。別の保存先を選んでください。")
            return
        except (OSError, UnicodeError, ValueError) as exc:
            messagebox.showerror("保存エラー", f"保存できませんでした。編集内容は画面に残っています。\n{exc}")
            return
        self.status.configure(text=f"保存しました：{Path(path).name}")


def main() -> int:
    root = tk.Tk()
    MinutesApp(root)
    root.mainloop()
    return 0
