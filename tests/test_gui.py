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
