import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from minutes.history import History, Meeting


class HistoryTest(unittest.TestCase):
    def test_exact_topic_background_only_and_revision_checks(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "history.sqlite3"
            history = History(path)
            self.assertEqual(history.list(), [])
            self.assertFalse(path.exists())
            first = history.save(Meeting("", 0, "Previous", "Project A", "RAW ONLY", "Past decision", "OPEN ONLY"))
            history.save(Meeting("", 0, "Other", "Project AA", "Elsewhere", "OTHER ONLY"))
            current = history.save(Meeting("", 0, "Now", "project A", "Current", "Current decision"))
            self.assertEqual(len(history.list("Ｐｒｏｊｅｃｔ Ａ")), 2)
            refs = history.background(" Ｐｒｏｊｅｃｔ  Ａ ", current.id)
            self.assertEqual(len(refs), 1)
            self.assertIn("Past decision", refs[0])
            for secret in ("RAW ONLY", "OPEN ONLY", "OTHER ONLY", "Current decision"):
                self.assertNotIn(secret, refs[0])
            updated = history.save(Meeting(first.id, first.revision, "Edited", "Project A", "RAW ONLY", "Past decision"))
            with self.assertRaises(ValueError):
                history.save(first)
            with self.assertRaises(ValueError):
                history.delete(first.id, first.revision)
            self.assertEqual(history.get(first.id), updated)
            history.delete(updated.id, updated.revision)
            self.assertIsNone(history.get(first.id))
            if os.name != "nt":
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)


@unittest.skipUnless(os.environ.get("MINUTES_GUI_TEST") == "1", "needs a display")
class WorkspaceTest(unittest.TestCase):
    def test_answered_draft_cannot_be_adopted_if_regeneration_not_authorized(self):
        from minutes import gui
        from minutes.routes import Settings, Stage

        with tempfile.TemporaryDirectory() as temp:
            root = gui.tk.Tk()
            root.withdraw()
            try:
                workspace = gui.Workspace(root, config_path=Path(temp) / "settings.json", show_setup=False)
                app = workspace.apps[workspace.tabs.select()]
                app.title_value.set("架空の会議")
                app.transcript.insert("1.0", "会場を確認しました。")
                app.settings = Settings(generation=Stage("anthropic", "synthetic"))
                root.update()
                app.questions = ("次回の会場は図書室で合っていますか？",)
                app.body.insert("1.0", "回答前の下書き")
                app._edit_confirmed()
                window = app.confirmed_window
                inputs = [widget for widget in window.winfo_children() if isinstance(widget, gui.tk.Text)]
                inputs[-1].insert("1.0", "1. はい")
                action = next(button for frame in window.winfo_children() if isinstance(frame, gui.ttk.Frame)
                              for button in frame.winfo_children() if button.cget("text") == "回答を使って生成")
                with patch.object(gui.messagebox, "askyesno", return_value=False) as request, \
                       patch.object(gui, "run_workflow") as workflow:
                    action.invoke()
                    request.assert_called_once()
                    workflow.assert_not_called()
                self.assertIn("次回の会場は図書室", app.confirmed_text)
                self.assertFalse(app.result_valid)
                self.assertFalse(app.questions)
                app._accept_draft()
                self.assertFalse(app.result_valid)
                self.assertIn("回答前の下書き", app.body.get("1.0", "end-1c"))
            finally:
                root.destroy()

    def test_long_checkpoint_copied_and_external_revision_reloaded_only_if_clean(self):
        from minutes import gui

        with tempfile.TemporaryDirectory() as temp:
            root = gui.tk.Tk()
            root.withdraw()
            try:
                folder = Path(temp)
                workspace = gui.Workspace(root, config_path=folder / "settings.json", show_setup=False)
                saved = workspace.history.save(Meeting("", 0, "前の版", "話題", "原文", "本文"))
                app = workspace.new_tab(saved)
                long_text = "合成入力" * 25_000
                app.active_checkpoint = folder / "test.partial.txt"
                app.active_checkpoint.write_text(long_text, encoding="utf-8")
                with patch.object(root, "clipboard_clear"), patch.object(root, "clipboard_append") as paste:
                    app._copy_transcript()
                    paste.assert_called_once_with(long_text)
                newer = workspace.history.save(Meeting(saved.id, saved.revision, "次の版", "話題", "原文", "新しい本文"))
                self.assertIs(workspace.new_tab(newer), app)
                self.assertEqual(app.meeting.revision, newer.revision)
                app.body.insert("end-1c", "下書き")
                third = workspace.history.save(Meeting(newer.id, newer.revision, "外の更新", "話題", "原文", "別の本文"))
                self.assertIs(workspace.new_tab(third), app)
                self.assertIn("下書き", app.body.get("1.0", "end-1c"))
                self.assertIn("新しい保存版", app.status.cget("text"))
            finally:
                root.destroy()

    def test_follow_does_not_replace_unsaved_body_without_confirmation(self):
        from minutes import gui

        with tempfile.TemporaryDirectory() as temp:
            root = gui.tk.Tk()
            root.withdraw()
            try:
                folder = Path(temp)
                workspace = gui.Workspace(root, config_path=folder / "settings.json", show_setup=False)
                app = workspace.apps[workspace.tabs.select()]
                output = folder / "fixture.txt"
                output.write_text("新しい合成文字起こし", encoding="utf-8")
                app.body.insert("1.0", "まだ保存していない修正")
                with patch.object(gui.filedialog, "askopenfilename", return_value=str(output)), \
                       patch.object(gui.messagebox, "askyesno", return_value=False) as confirm:
                    app.follow_button.invoke()
                    confirm.assert_called_once()
                self.assertIsNone(app.follow_path)
                self.assertEqual(app.body.get("1.0", "end-1c"), "まだ保存していない修正")
            finally:
                root.destroy()

    def test_following_written_text_appends_and_can_be_copied_without_generation(self):
        from minutes import gui

        with tempfile.TemporaryDirectory() as temp:
            root = gui.tk.Tk()
            root.withdraw()
            try:
                folder = Path(temp)
                workspace = gui.Workspace(root, config_path=folder / "settings.json", show_setup=False)
                first = workspace.apps[workspace.tabs.select()]
                output = folder / "synthetic.txt"
                output.write_text("公開テストの一文\n", encoding="utf-8")
                first._start_follow(output)
                actual = first.follow_job
                root.after_cancel(actual)
                with output.open("a", encoding="utf-8") as stream:
                    stream.write("追従した二文目\n")
                second = workspace.new_tab()
                first._follow_tick()
                self.assertEqual(workspace.tabs.select(), str(second.container))
                with patch.object(root, "clipboard_clear"), patch.object(root, "clipboard_append") as paste:
                    first._copy_transcript()
                    paste.assert_called_once_with("公開テストの一文\n追従した二文目\n")
                first._stop_follow()
                self.assertEqual(first.transcript.cget("state"), "normal")
            finally:
                root.destroy()

    def test_cancelled_result_is_not_saved_and_same_meeting_can_retry(self):
        from minutes import gui
        from minutes.routes import Settings, Stage
        from minutes.workflow import RunResult

        with tempfile.TemporaryDirectory() as temp:
            root = gui.tk.Tk()
            root.withdraw()
            try:
                workspace = gui.Workspace(root, config_path=Path(temp) / "settings.json", show_setup=False)
                app = workspace.apps[workspace.tabs.select()]
                app.title_value.set("架空会議")
                app.topic.set("連絡")
                app.transcript.insert("1.0", "会場を確認した。")
                app.settings = Settings(generation=Stage("ollama", "test"))
                self.assertTrue(app._save_history())
                original = app.meeting
                sections = {k: [] for k in ("decisions", "owners", "deadlines", "next", "open")}
                sections["next"] = [{"topic": "集合", "text": "図書室に集まる。"}]
                started, release = threading.Event(), threading.Event()

                def late_result(*args, **kwargs):
                    started.set()
                    if not release.wait(4):
                        raise RuntimeError("fixture was not released")
                    self.assertTrue(kwargs["is_cancelled"]())
                    return RunResult(sections, "原文", 1, {"generation": 0.1})

                def receive():
                    event = app.events.get(timeout=3)
                    app.events.put(event)
                    app._poll()

                with patch.object(gui, "run_workflow", side_effect=late_result):
                    app._generate()
                    self.assertTrue(started.wait(2))
                    app.cancel_button.invoke()
                    other = workspace.new_tab()
                    release.set()
                    receive()
                    self.assertEqual(workspace.history.get(original.id), original)
                    self.assertIs(workspace.new_tab(original), app)
                    self.assertIsNot(app, other)
                with patch.object(gui, "run_workflow", return_value=RunResult(sections, "原文", 1, {"generation": 0.1})):
                    app._generate()
                    receive()
                self.assertIn("【次回】", workspace.history.get(original.id).body)
                self.assertIn("元の会議の履歴に保存", app.status.cget("text"))
            finally:
                root.destroy()

    def test_background_job_returns_to_original_history_and_copy_is_independent(self):
        from minutes import gui
        from minutes.routes import Settings, Stage
        from minutes.workflow import RunResult

        with tempfile.TemporaryDirectory() as temp:
            root = gui.tk.Tk()
            root.withdraw()
            try:
                workspace = gui.Workspace(root, config_path=Path(temp) / "settings.json", show_setup=False)
                first = workspace.apps[workspace.tabs.select()]
                first.title_value.set("架空会議一")
                first.topic.set("展示会")
                first.transcript.insert("1.0", "原文一")
                first.settings = Settings(generation=Stage("ollama", "test"))
                self.assertTrue(first._save_history())
                original_id = first.meeting.id
                started, release = threading.Event(), threading.Event()

                def generate(*args, **kwargs):
                    started.set()
                    if not release.wait(4):
                        raise RuntimeError("fixture was not released")
                    sections = {k: [] for k in ("decisions", "owners", "deadlines", "next", "open")}
                    sections["decisions"] = [{"topic": "会場", "text": "入口を決めた。"}]
                    sections["open"] = [{"topic": "受付", "text": "受付の配置を選ぶ。"}]
                    return RunResult(sections, "補正一", 1, {"generation": 0.15})

                with patch.object(gui, "run_workflow", side_effect=generate):
                    first.generate_button.configure(state="normal")
                    first.generate_button.invoke()
                    self.assertTrue(started.wait(2))
                    second = workspace.new_tab()
                    second.title_value.set("作業タブ")
                    second.transcript.insert("1.0", "原文二")
                    with patch.object(root, "clipboard_clear"), patch.object(root, "clipboard_append") as paste:
                        second._copy_transcript()
                        paste.assert_called_once_with("原文二")
                    self.assertEqual(workspace.tabs.select(), str(second.container))
                    release.set()
                    result = first.events.get(timeout=3)
                    first.events.put(result)
                    first._poll()
                    self.assertEqual(workspace.tabs.select(), str(second.container))
                saved = workspace.history.get(original_id)
                self.assertIn("【決定事項】\n＜会場＞\n・ 入口を決めた。", saved.body)
                self.assertNotIn("【未決事項】", saved.body)
                self.assertIn("【未決事項】", saved.open_items)
                self.assertEqual(saved.transcript, "原文一")
                self.assertIsNone(second.meeting)
                self.assertEqual(second.body.get("1.0", "end-1c"), "")
                workspace.tabs.select(first.container)
                with patch.object(root, "clipboard_clear"), patch.object(root, "clipboard_append") as paste:
                    first.body_copy_button.invoke()
                    paste.assert_called_once_with(saved.body)
                    paste.reset_mock()
                    first.open_copy_button.invoke()
                    paste.assert_called_once_with(saved.open_items)
            finally:
                root.destroy()
