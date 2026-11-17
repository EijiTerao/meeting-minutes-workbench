"""Tk smoke test; enable explicitly on a machine with a display."""

import os
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from minutes.__main__ import main
from minutes.routes import Settings, Stage, load_settings, save_settings
from tools.benchmark_minutes import score

ROOT = Path(__file__).resolve().parents[1]


class EntryTest(unittest.TestCase):
    def test_long_asr_checkpoints_durable_progress_and_preserves_partial_on_failure(self):
        from minutes.asr_job import transcribe_to_text

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "recording"
            source.mkdir()
            (source / "part-000001.wav").write_bytes(b"fixture")
            output = root / "lecture.txt"
            exe, model = root / "whisper-cli", root / "model.bin"
            progress = []

            def failing(_source, _exe, _model, _language, *, on_part):
                on_part(1, "最初の区間")
                progress.append((1, output.with_name("lecture.partial.txt").read_text(encoding="utf-8")))
                on_part(2, "")
                raise RuntimeError("区間2の失敗")

            with patch("minutes.asr_job.transcribe", side_effect=failing):
                with self.assertRaisesRegex(RuntimeError, "区間2"):
                    transcribe_to_text(source, exe, model, "ja", output, allow_partial_recording=True)
            partial = root / "lecture.partial.txt"
            self.assertEqual(progress, [(1, "最初の区間")])
            self.assertEqual(partial.read_text(encoding="utf-8"), "最初の区間")
            self.assertFalse(output.exists())

    def test_long_asr_writes_final_only_after_success_and_reads_it_back(self):
        from minutes.asr_job import transcribe_to_text

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "recording"
            source.mkdir()
            output = root / "lecture.txt"
            partial = root / "lecture.partial.txt"

            def complete(_source, _exe, _model, _language, *, on_part):
                on_part(1, "最初の区間")
                on_part(2, "次の区間")
                return "最初の区間\n次の区間"

            with patch("minutes.asr_job.transcribe", side_effect=complete):
                result = transcribe_to_text(source, root / "whisper-cli", root / "model.bin", "ja", output,
                                            allow_partial_recording=True)
            self.assertEqual(result, output.read_text(encoding="utf-8"))
            self.assertEqual(output.read_text(encoding="utf-8"), "最初の区間\n次の区間")
            self.assertEqual(partial.read_text(encoding="utf-8"), "最初の区間\n次の区間")

    def test_long_asr_refuses_existing_final_or_partial_and_protects_symlinks(self):
        from minutes.asr_job import transcribe_to_text

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            exe, model = root / "whisper-cli", root / "model.bin"
            for filename in ("lecture.txt", "lecture.partial.txt"):
                target = root / filename
                target.write_text("keep", encoding="utf-8")
                with patch("minutes.asr_job.transcribe") as run:
                    with self.assertRaises(ValueError):
                        transcribe_to_text(root / "lecture.wav", exe, model, "ja", root / "lecture.txt")
                    run.assert_not_called()
                target.unlink()
            outside = root / "outside.txt"
            outside.write_text("keep", encoding="utf-8")
            partial = root / "lecture.partial.txt"
            try:
                partial.symlink_to(outside)
            except OSError:
                self.skipTest("symlinks unavailable")
            with patch("minutes.asr_job.transcribe") as run:
                with self.assertRaises(ValueError):
                    transcribe_to_text(root / "lecture.wav", exe, model, "ja", root / "lecture.txt")
                run.assert_not_called()
            self.assertEqual(outside.read_text(encoding="utf-8"), "keep")

    def test_cli_unmarked_recording_requires_flag_and_workflow_wav_rejects_folder(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            folder = root / "recording"
            folder.mkdir()
            (folder / "part-000001.wav").write_bytes(b"fixture")
            exe, model = root / "whisper-cli", root / "model.bin"
            output = root / "transcript.txt"
            command = ["transcribe", str(folder), "--whisper", str(exe), "--model", str(model), "--out", str(output)]
            with patch("builtins.print"), patch("minutes.asr_job.transcribe") as run:
                self.assertEqual(main(command), 1)
                run.assert_not_called()
            with patch("builtins.print"), patch("minutes.asr_job.transcribe", return_value="保存された区間"):
                self.assertEqual(main(command + ["--allow-partial-recording"]), 0)
            self.assertEqual(output.read_text(encoding="utf-8"), "保存された区間")

            settings = root / "settings.json"
            exe.touch()
            model.touch()
            save_settings(Settings(transcription="whisper", whisper=str(exe), whisper_model=str(model),
                                   generation=Stage("ollama", "fixture")), settings)
            with patch("builtins.print"):
                self.assertEqual(main(["workflow", str(folder), "--title", "会議", "--settings", str(settings),
                                       "--wav", "--out", str(root / "candidate")]), 1)
            self.assertFalse((root / "candidate").exists())

    def test_workflow_entry_routes_one_provider_and_measures_saveable_result(self):
        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp) / "public-fixture-output"
            config = Path(temp) / "settings.json"
            save_settings(Settings(generation=Stage("ollama", "fixture")), config)
            # Mock the transport, not the processing or the CLI. This is not a model-quality result.
            text = ROOT / "examples/benchmark-transcript.txt"
            expected = json.loads((ROOT / "examples/benchmark-minutes.json").read_text(encoding="utf-8"))

            def local(_model, _system, data, _schema, **_kwargs):
                sources = [r["id"] for r in data["sources"]]
                return {section: [{**item, "evidence": sources} for item in items]
                        for section, items in expected.items()}

            with patch("minutes.workflow.ollama_json", side_effect=local) as provider, patch("builtins.print"):
                self.assertEqual(main(["workflow", str(text), "--title", "展示会の準備", "--settings",
                                       str(config), "--out", str(out)]), 0)
            provider.assert_called_once()
            self.assertTrue(all(score(out).values()))
            timing = json.loads((out / "timing.json").read_text(encoding="utf-8"))
            self.assertGreater(timing["seconds"]["workflow"], 0)
            self.assertEqual((timing["generation_provider"], timing["generation_model"]), ("ollama", "fixture"))
            self.assertFalse((out / "transcript-corrected.txt").exists())

    def test_render_entry_saves_two_files_then_refuses_overwrite(self):
        with tempfile.TemporaryDirectory() as temp:
            destination = Path(temp) / "架空の議事録"
            command = [
                "render", str(ROOT / "examples/minutes.json"), "--title", "図書室の案内改善",
                "--out", str(destination),
            ]
            with patch("builtins.print"):
                self.assertEqual(main(command), 0)
                self.assertEqual(main(command), 1)
            body = (destination / "minutes.txt").read_text(encoding="utf-8")
            pending = (destination / "open-issues.txt").read_text(encoding="utf-8")
            self.assertIn("【次回】\n＜初稿レビュー＞\n・", body)
            self.assertEqual(body.count("11月20日"), 1)
            self.assertNotIn("多言語化", body)
            self.assertIn("【未決事項】\n＜掲示の多言語化＞\n・", pending)

    @unittest.skipUnless(os.environ.get("MINUTES_GUI_TEST") == "1", "needs a display")
    def test_tk_reads_fixture_and_saves_body_separately(self):
        from minutes import gui

        root = gui.tk.Tk()
        root.withdraw()
        try:
            app = gui.MinutesApp(root, show_setup=False, config_path=ROOT / "examples/pipeline-config.json")
            root.update()
            with patch.object(gui.filedialog, "askopenfilename", return_value=str(ROOT / "examples/minutes.json")):
                app._open_json()
            self.assertIn("【担当】", app.body.get("1.0", "end"))
            self.assertNotIn("【未決事項】", app.body.get("1.0", "end"))
            self.assertIn("【未決事項】", app.open_items.get("1.0", "end"))
            with tempfile.TemporaryDirectory() as temp:
                target = Path(temp) / "body.txt"
                with patch.object(gui.filedialog, "asksaveasfilename", return_value=str(target)):
                    app._save(app.body)
                before = target.read_bytes()
                with patch.object(gui.filedialog, "asksaveasfilename", return_value=str(target)), \
                        patch.object(gui.messagebox, "showinfo") as info:
                    app._save(app.open_items)
                    info.assert_called_once()
                self.assertEqual(target.read_bytes(), before)
                self.assertIn("【担当】", before.decode("utf-8"))
        finally:
            root.destroy()

    @unittest.skipUnless(os.environ.get("MINUTES_GUI_TEST") == "1", "needs a display")
    def test_single_wav_gui_route_uses_shared_durable_helper(self):
        from minutes import gui

        with tempfile.TemporaryDirectory() as temp:
            root = gui.tk.Tk()
            root.withdraw()
            source = Path(temp) / "one-hour.wav"
            source.touch()
            output = Path(temp) / "transcript.txt"
            executable, model = Path(temp) / "whisper-cli", Path(temp) / "model.bin"
            executable.touch()
            model.touch()
            try:
                app = gui.MinutesApp(root, show_setup=False, config_path=ROOT / "examples/pipeline-config.json")
                app.settings = Settings(transcription="whisper", whisper=str(executable),
                                        whisper_model=str(model))
                with patch.object(gui.filedialog, "askopenfilename", return_value=str(source)), \
                        patch.object(gui.filedialog, "asksaveasfilename", return_value=str(output)), \
                        patch.object(gui, "transcribe_to_text", return_value="fixture transcript") as durable:
                    app._transcribe_wav()
                    event = app.events.get(timeout=3)
                durable.assert_called_once()
                call = durable.call_args
                self.assertEqual(call.args[:5], (source, executable, model, "ja", output))
                self.assertFalse(call.kwargs["allow_partial_recording"])
                self.assertEqual(event[0], "folder_transcribed")
            finally:
                root.destroy()

    @unittest.skipUnless(os.environ.get("MINUTES_GUI_TEST") == "1", "needs a display")
    def test_tk_old_meeting_cannot_export_after_input_changes_and_generation_fails(self):
        from minutes import gui

        root = gui.tk.Tk()
        root.withdraw()
        try:
            app = gui.MinutesApp(root, show_setup=False, config_path=ROOT / "examples/pipeline-config.json")
            with patch.object(gui.filedialog, "askopenfilename", return_value=str(ROOT / "examples/minutes.json")):
                app._open_json()
            self.assertTrue(app.result_valid)
            app.title_value.set("別の架空会議")
            self.assertFalse(app.result_valid)
            self.assertTrue(app.body_copy_button.instate(["disabled"]))
            app.transcript.insert("1.0", "別の架空会議の発言")
            app.settings = Settings(generation=Stage("ollama", "fixture"))
            root.update()
            with patch.object(gui, "run_workflow", side_effect=RuntimeError("模型の故障")), \
                    patch.object(gui.messagebox, "showerror") as error:
                app._generate()
                thread_event = app.events.get(timeout=2)
                app.events.put(thread_event)
                app._poll()
                error.assert_called_once()
            self.assertIn("別の架空会議の発言", app.transcript.get("1.0", "end"))
            self.assertFalse(app.result_valid)
            self.assertTrue(app.body_save_button.instate(["disabled"]))
            with patch.object(gui.filedialog, "asksaveasfilename") as dialog:
                app._save(app.body)
                dialog.assert_not_called()
        finally:
            root.destroy()

    @unittest.skipUnless(os.environ.get("MINUTES_GUI_TEST") == "1", "needs a display")
    def test_first_setup_persists_independent_routes_without_network(self):
        from minutes import gui

        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp) / "settings.json"
            root = gui.tk.Tk()
            root.withdraw()
            try:
                app = gui.MinutesApp(root, config_path=config, show_setup=False)
                self.assertEqual(app.settings, Settings())
                self.assertFalse(config.exists())
                self.assertTrue(app.generate_button.instate(["disabled"]))
                with patch.object(gui, "run_workflow") as run:
                    app._open_settings()
                    self.assertTrue(app.settings_window.winfo_exists())
                    for stage, provider, model in (("correction", "ollama", "compact"),
                                                   ("generation", "openai", "accurate"),
                                                   ("verification", "ollama", "checks")):
                        label, choices, model_text = app._stage_vars[stage]
                        label.set(next(name for name, code in choices.items() if code == provider))
                        model_text.set(model)
                    app._save_settings_dialog(app.settings_window)
                    self.assertEqual(load_settings(config), Settings(correction=Stage("ollama", "compact"),
                        generation=Stage("openai", "accurate"), verification=Stage("ollama", "checks")))
                    app.title_value.set("架空会議")
                    app.transcript.insert("1.0", "架空の発言")
                    with patch.object(gui.messagebox, "askyesno", return_value=False) as permission:
                        app._generate()
                        permission.assert_called_once()
                    run.assert_not_called()
            finally:
                root.destroy()

    @unittest.skipUnless(os.environ.get("MINUTES_GUI_TEST") == "1", "needs a display")
    def test_windows_reference_layout_fits_at_minimum_and_wide_geometry(self):
        from minutes import gui

        with tempfile.TemporaryDirectory() as temp:
            root = gui.tk.Tk()
            root.geometry("980x680")
            try:
                with patch.object(gui.sys, "platform", "win32"):
                    workspace = gui.Workspace(root, config_path=Path(temp) / "settings.json", show_setup=False)
                    app = next(iter(workspace.apps.values()))
                    root.deiconify()
                    root.update()
                    for geometry in ("980x680", "1080x720"):
                        root.geometry(geometry)
                        root.update()
                        app._initialize_split()
                        root.update_idletasks()
                        self.assertGreaterEqual(app.split.winfo_width(), 828)
                        self.assertGreaterEqual(app.split.winfo_children()[0].winfo_width(), 390)
                        self.assertGreaterEqual(app.split.winfo_children()[1].winfo_width(), 430)
                        self.assertGreaterEqual(app.body.winfo_height(), 100)
                        self.assertEqual(app.capture_mode_buttons, [])
                        self.assertIn("WASAPI CLI", app.record_mode_note.cget("text"))
                        self.assertEqual(app.record_button.cget("text"), "ブラウザで録音")
                        self.assertNotIn("disabled", app.record_button.state())
                        for widget in (app.settings_button, app.more_button, app.record_button,
                                       app.generate_button, app.json_button, app.open_transcript_button,
                                       app.copy_transcript_button, app.follow_button):
                            self.assertTrue(widget.winfo_ismapped(), widget.cget("text"))
                            self.assertGreaterEqual(widget.winfo_width() + 1, widget.winfo_reqwidth(),
                                                    f"clipped action: {widget.cget('text')} at {geometry}")
            finally:
                root.destroy()
