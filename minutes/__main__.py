"""Desktop and automation entry points."""

import argparse
import hashlib
import json
import sys
from pathlib import Path

from .core import load_profile, render
from .engine import REFERENCE_BUDGET, generate, read_input, transcribe
from .asr_job import is_complete_recording_folder, transcribe_to_text


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="会議の文字起こしから、本文と未決事項を分けて作成します。")
    actions = parser.add_subparsers(dest="action")
    gui_action = actions.add_parser("gui", help="操作画面を開く")
    gui_action.add_argument("--settings", type=Path, help="この起動で使う設定ファイル")
    gui_action.add_argument("--check-launch", action="store_true",
                            help="隔離した設定先で通常画面の起動・初期設定を確認して終了")
    setup = actions.add_parser("setup", help="環境を安全に確認し、初期設定を作成する")
    setup.add_argument("--check", action="store_true", help="設定を書かずにJSONで確認結果を表示")
    setup.add_argument("--settings", type=Path, help="設定ファイル（省略時は標準の設定先）")
    setup.add_argument("--whisper", type=Path, help="既に導入済みのwhisper-cli")
    setup.add_argument("--whisper-model", type=Path, help="既に存在する許可済みモデル")
    setup.add_argument("--provider", choices=("off", "ollama"), default="off",
                       help="明示的に選択したローカルAI。外部API課金・キー読取・自動接続はしません")
    setup.add_argument("--model", default="", help="選択したプロバイダーのモデル名")
    for name, help_text in (("render", "議事録JSONを書式に整える"), ("generate", "ローカルAIで議事録を生成する"),
                            ("workflow", "初期設定に従って校正・議事録生成・検証を行う")):
        action = actions.add_parser(name, help=help_text)
        action.add_argument("input", type=Path)
        action.add_argument("--title", required=True)
        action.add_argument("--out", type=Path, required=True, help="出力する新規フォルダ")
        action.add_argument("--profile", type=Path)
        if name != "render":
            action.add_argument("--reference", type=Path, action="append", default=[])
        if name == "generate":
            action.add_argument("--model", required=True)
        if name == "workflow":
            action.add_argument("--settings", type=Path, help="処理の設定JSON")
            action.add_argument("--confirmed", type=Path, help="本人が確認した事項だけを含むUTF-8テキスト")
            action.add_argument("--wav", action="store_true", help="入力をWAVとしてローカルで文字起こし")
            action.add_argument("--allow-openai", action="store_true",
                                help="今回の実行で設定済みのOpenAI APIへの送信とAPI利用を明示許可する")
            action.add_argument("--allow-anthropic", action="store_true",
                                help="今回の実行で設定済みのAnthropic APIへの送信とAPI利用を明示許可する")
    audio = actions.add_parser("transcribe", help="選択したWAVをローカルで文字起こしする")
    audio.add_argument("input", type=Path)
    audio.add_argument("--whisper", type=Path, required=True)
    audio.add_argument("--model", type=Path, required=True)
    audio.add_argument("--language", default="ja")
    audio.add_argument("--out", type=Path, required=True, help="新規テキストファイル")
    audio.add_argument("--allow-partial-recording", action="store_true",
                       help="完了マーカーがない分割録音フォルダを、録音途中の可能性を承知して処理する")
    args = parser.parse_args(argv)
    try:
        if args.action == "setup":
            from .setup import run_setup

            report = run_setup(check_only=args.check, settings_path=args.settings,
                               whisper=args.whisper, whisper_model=args.whisper_model,
                               provider=args.provider, model=args.model)
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 1 if report["status"] == "existing_invalid" else 0
        if args.action in (None, "gui"):
            try:
                from .gui import main as gui
                from tkinter import TclError
            except ImportError:
                raise RuntimeError("操作画面にはTkが必要です。python -m tkinter で確認するか、--help を参照してください。") from None
            try:
                if args.action == "gui" and args.check_launch and args.settings is None:
                    raise ValueError("--check-launch には隔離用の --settings PATH が必要です。")
                return gui(settings_path=args.settings if args.action == "gui" else None,
                           check_launch=args.check_launch if args.action == "gui" else False)
            except TclError:
                raise RuntimeError("操作画面を開けません。画面のある環境で起動するか、--help を参照してください。") from None
        if args.out.exists() or args.out.is_symlink():
            raise ValueError("出力先は既に存在します。新しい名前を指定してください。")
        if args.action == "transcribe":
            transcript = transcribe_to_text(args.input, args.whisper.absolute(), args.model, args.language,
                                            args.out, allow_partial_recording=args.allow_partial_recording)
        else:
            profile = load_profile(args.profile)
            diagnostics = None
            corrected = None
            if args.action in ("generate", "workflow"):
                if len(args.reference) > 3:
                    raise ValueError("参考資料は3件までです。")
                references = tuple(read_input(path, max_bytes=REFERENCE_BUDGET) for path in args.reference)
                if args.action == "generate":
                    data = generate(read_input(args.input), args.title, args.model, profile, references)
                else:
                    from time import perf_counter

                    from .routes import load_settings
                    from .workflow import run_workflow

                    if args.settings is not None and not args.settings.is_file():
                        raise ValueError("指定した処理の設定ファイルが見つかりません。")
                    settings = load_settings(args.settings)
                    if settings.generation.provider == "off":
                        raise ValueError("処理の設定で議事録生成を有効にしてください。")
                    if not args.allow_openai and any(
                        stage.provider == "openai"
                        for stage in (settings.correction, settings.generation, settings.verification)
                    ):
                        raise ValueError("OpenAIに送信するには今回の実行で --allow-openai が必要です。")
                    if not args.allow_anthropic and any(
                        stage.provider == "anthropic"
                        for stage in (settings.correction, settings.generation, settings.verification)
                    ):
                        raise ValueError("Anthropicに送信するには今回の実行で --allow-anthropic が必要です。")
                    confirmed = read_input(args.confirmed, 4_000) if args.confirmed else ""
                    started = perf_counter()
                    if args.wav:
                        if settings.transcription != "whisper":
                            raise ValueError("処理の設定でローカル文字起こしを有効にしてください。")
                        if args.input.is_dir():
                            raise ValueError("workflow --wav は単一WAVのみです。分割録音は transcribe で先に文字起こしし、保存したTXTを workflow に渡してください。")
                        transcript = transcribe(args.input, Path(settings.whisper), Path(settings.whisper_model),
                                                settings.language)
                    else:
                        transcript = read_input(args.input)
                    input_seconds = perf_counter() - started
                    result = run_workflow(transcript, args.title, settings, profile, references, confirmed,
                                          authorized=args.allow_openai, allow_anthropic=args.allow_anthropic)
                    data = result.minutes
                    corrected = result.corrected if result.corrected != transcript else None
                    diagnostics = {
                        "source_sha256": hashlib.sha256(transcript.encode("utf-8")).hexdigest(),
                        "generation_provider": settings.generation.provider,
                        "generation_model": settings.generation.model,
                        "chunks": result.chunks, "corrections_rejected": result.corrections_rejected,
                        "verification_rejected": result.verification_rejected,
                        "seconds": {**result.seconds, "input": input_seconds},
                    }
            else:
                try:
                    data = json.loads(read_input(args.input))
                except (ValueError, RecursionError):
                    raise ValueError("議事録JSONを読み込めませんでした。形式・文字コードを確認してください。") from None
            body, pending = render(data, args.title, profile)
            args.out.mkdir()
            outputs = {"minutes.txt": body, "open-issues.txt": pending}
            if corrected is not None:
                outputs["transcript-corrected.txt"] = corrected
            for filename, content in outputs.items():
                payload = (content.rstrip() + "\n").encode("utf-8")
                destination = args.out / filename
                with destination.open("xb") as handle:
                    handle.write(payload)
                if destination.read_bytes() != payload:
                    raise RuntimeError("出力ファイルの読み戻しに失敗しました。")
            if diagnostics is not None:
                diagnostics["seconds"]["workflow"] = perf_counter() - started
                with (args.out / "timing.json").open("x", encoding="utf-8", newline="\n") as handle:
                    json.dump(diagnostics, handle, ensure_ascii=False, indent=2)
                    handle.write("\n")
        if args.action == "transcribe" and args.input.is_dir() and not is_complete_recording_folder(args.input):
            print(f"保存パートのみ（録音全体とは未確認）を保存しました: {args.out.absolute()}")
        else:
            print("保存しました。")
        return 0
    except OSError:
        print("ファイルを開くか保存できませんでした。アクセス権・空き容量・既存ファイルを確認してください。", file=sys.stderr)
    except (RuntimeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
