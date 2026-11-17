import io
import json
import os
import subprocess
import tempfile
import threading
import time
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

from minutes.desktop_capture import DesktopCapture
from minutes.live_transcript import WINDOW_BYTES


class _Process:
    def __init__(self, pcm: bytes, stop_code: int = 0, natural_exit: bool = False):
        self.stdout = io.BytesIO(pcm)
        self.stderr = io.BytesIO(b"WORKBENCH_CAPTURE_READY\n")
        self._returncode = 0 if natural_exit else None
        self._stopped = threading.Event()
        if natural_exit:
            self._stopped.set()
        self.stop_code = stop_code

    def poll(self):
        return self._returncode

    def wait(self, timeout=None):
        if not self._stopped.wait(timeout):
            raise subprocess.TimeoutExpired("fixture", timeout)
        return self._returncode

    def send_signal(self, _signal):
        self._returncode = self.stop_code
        self._stopped.set()


class _Recognizer:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    def transcribe(self, pcm):
        self.calls.append(pcm)
        if self.fail:
            raise RuntimeError("synthetic ASR failure")
        return f"fixture {len(self.calls)}"


class DesktopCaptureTest(unittest.TestCase):
    def _run(self, pcm, *, mode="microphone-only", include_microphone=False,
             save_wav_parts=False, recognizer=None):
        temp = tempfile.TemporaryDirectory()
        root = Path(temp.name)
        output = root / "transcript.txt"
        args_seen = []
        events = []
        finished = threading.Event()
        started = threading.Event()

        def factory(args, **_kwargs):
            args_seen.extend(args)
            return _Process(pcm)

        capture = DesktopCapture(capture_helper="/fixture/workbench-system-audio",
                                 recognizer=recognizer or _Recognizer(), capture_factory=factory)
        def on_event(state, payload):
            events.append((state, payload))
            if state == "started":
                started.set()
            if state == "stopped":
                finished.set()
        capture.start(output, mode=mode, include_microphone=include_microphone,
                      save_wav_parts=save_wav_parts, on_event=on_event)
        if not (recognizer and recognizer.fail):
            self.assertTrue(started.wait(5), "capture helper did not report ready")
            capture.stop()
        self.assertTrue(finished.wait(5), "capture worker did not finish")
        return temp, root, output, args_seen, events, capture

    def test_splits_pcm_flushes_text_and_optionally_saves_wav_parts(self):
        recognizer = _Recognizer()
        temp, root, output, args, events, capture = self._run(
            b"a" * WINDOW_BYTES + b"b" * 12, save_wav_parts=True, recognizer=recognizer)
        try:
            self.assertEqual(output.read_text(encoding="utf-8"), "fixture 1\nfixture 2\n")
            self.assertEqual([len(part) for part in recognizer.calls], [WINDOW_BYTES, 12])
            self.assertIn("--microphone-only", args)
            self.assertNotIn("--include-microphone", args)
            with wave.open(str(root / "transcript" / "part-000001.wav"), "rb") as wav:
                self.assertEqual((wav.getnchannels(), wav.getsampwidth(), wav.getframerate()), (1, 2, 16000))
                self.assertEqual(wav.getnframes(), WINDOW_BYTES // 2)
            self.assertEqual([state for state, _ in events][-2:], ["complete", "stopped"])
            marker = json.loads((root / "transcript" / "recording-complete.json").read_text(encoding="utf-8"))
            self.assertEqual(marker, {"version": 1, "parts": 2})
            from minutes.asr_job import is_complete_recording_folder
            self.assertTrue(is_complete_recording_folder(root / "transcript"))
            self.assertFalse(capture.running)
        finally:
            temp.cleanup()

    def test_online_includes_mic_only_when_independently_selected(self):
        temp, _root, output, args, events, _capture = self._run(
            b"x" * 12, mode="whole-mac", include_microphone=True)
        try:
            self.assertIn("--whole-mac", args)
            self.assertIn("--include-microphone", args)
            self.assertEqual(output.read_text(encoding="utf-8"), "fixture 1\n")
        finally:
            temp.cleanup()

    def test_asr_failure_keeps_txt_partial_without_falling_back_to_raw_audio(self):
        temp, _root, output, _args, events, _capture = self._run(
            b"x" * 12, recognizer=_Recognizer(fail=True))
        try:
            self.assertTrue(output.exists())
            self.assertEqual(output.read_text(encoding="utf-8"), "")
            self.assertEqual([state for state, _ in events], ["started", "error", "stopped"])
            self.assertIn("文字起こしに失敗", events[1][1][0])
        finally:
            temp.cleanup()

    def test_rejects_existing_destination_and_duplicate_start(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            output = root / "transcript.txt"
            output.write_text("keep", encoding="utf-8")
            capture = DesktopCapture(capture_helper="/fixture/workbench-system-audio", recognizer=_Recognizer())
            with self.assertRaises(ValueError):
                capture.start(output, mode="microphone-only")

    def test_workspace_capture_lock_rejects_parallel_capture(self):
        import threading

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            lock = threading.BoundedSemaphore(1)
            lock.acquire()
            capture = DesktopCapture(capture_helper="/fixture/workbench-system-audio",
                                     recognizer=_Recognizer(), capture_lock=lock)
            with self.assertRaisesRegex(RuntimeError, "別の会議で録音中"):
                capture.start(root / "parallel.txt", mode="microphone-only")
            lock.release()

    def test_native_nonzero_after_stop_keeps_partial_and_never_publishes_marker(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            output = root / "transcript.txt"
            events = []
            started = threading.Event()
            stopped = threading.Event()
            process = _Process(b"x" * 12, stop_code=137)
            capture = DesktopCapture(capture_helper="/fixture/workbench-system-audio",
                                     recognizer=_Recognizer(), capture_factory=lambda *_args, **_kwargs: process)
            def on_event(state, payload):
                events.append((state, payload))
                if state == "started":
                    started.set()
                if state == "stopped":
                    stopped.set()
            capture.start(output, mode="whole-mac", save_wav_parts=True, on_event=on_event)
            self.assertTrue(started.wait(5))
            capture.stop()
            self.assertTrue(stopped.wait(5))
            self.assertEqual(output.read_text(encoding="utf-8"), "fixture 1\n")
            self.assertFalse((root / "transcript" / "recording-complete.json").exists())
            self.assertEqual([state for state, _ in events], ["started", "progress", "error", "stopped"])

    def test_helper_outage_reports_error_and_releases_workspace_lock(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            events = []
            stopped = threading.Event()
            lock = threading.BoundedSemaphore(1)
            def unavailable(*_args, **_kwargs):
                raise OSError("synthetic helper outage")
            capture = DesktopCapture(capture_helper="/fixture/workbench-system-audio",
                                     recognizer=_Recognizer(), capture_factory=unavailable,
                                     capture_lock=lock)
            capture.start(root / "outage.txt", mode="microphone-only",
                          on_event=lambda state, payload: (events.append((state, payload)),
                              stopped.set() if state == "stopped" else None))
            self.assertTrue(stopped.wait(5))
            self.assertEqual([state for state, _ in events], ["error", "stopped"])
            self.assertFalse((root / "outage.txt").exists())
            self.assertTrue(lock.acquire(blocking=False))
            lock.release()

    def test_unrequested_clean_helper_exit_is_partial_not_complete(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            output = root / "unexpected.txt"
            events = []
            stopped = threading.Event()
            process = _Process(b"x" * 12, natural_exit=True)
            capture = DesktopCapture(capture_helper="/fixture/workbench-system-audio",
                                     recognizer=_Recognizer(), capture_factory=lambda *_args, **_kwargs: process)
            capture.start(output, mode="whole-mac", save_wav_parts=True,
                          on_event=lambda state, payload: (events.append((state, payload)),
                              stopped.set() if state == "stopped" else None))
            self.assertTrue(stopped.wait(5))
            self.assertEqual(output.read_text(encoding="utf-8"), "fixture 1\n")
            self.assertFalse((root / "unexpected" / "recording-complete.json").exists())
            self.assertEqual([state for state, _ in events], ["started", "progress", "error", "stopped"])

    @unittest.skipUnless(os.environ.get("MINUTES_GUI_TEST") == "1", "needs a display")
    def test_packaged_mac_ui_exposes_direct_recording_without_browser_page(self):
        from minutes import gui

        with tempfile.TemporaryDirectory() as temp:
            root_path = Path(temp)
            helper = root_path / "workbench-system-audio"
            transcriber = root_path / "workbench-apple-transcribe"
            helper.touch()
            transcriber.touch()
            helper.chmod(0o700)
            transcriber.chmod(0o700)
            root = gui.tk.Tk()
            root.withdraw()
            try:
                with patch.dict(os.environ, {"MINUTES_SYSTEM_AUDIO_CAPTURE": str(helper),
                                             "MINUTES_APPLE_TRANSCRIBER": str(transcriber)}), \
                        patch.object(gui.sys, "platform", "darwin"):
                    workspace = gui.Workspace(root, config_path=root_path / "settings.json", show_setup=False)
                    app = next(iter(workspace.apps.values()))
                    self.assertEqual(root.title(), "Meeting Minutes Workbench")
                    self.assertEqual(app.record_button.cget("text"), "録音を開始")
                    self.assertEqual(str(app.capture_read_button.cget("state")), "disabled")
                    toolbar = root.winfo_children()[0]
                    self.assertFalse(any("録音ページ" in child.cget("text")
                                         for child in toolbar.winfo_children()
                                         if isinstance(child, gui.ttk.Button)))
                    self.assertTrue(app._desktop_capture_destination().parent.is_dir())
            finally:
                root.destroy()


if __name__ == "__main__":
    unittest.main()
