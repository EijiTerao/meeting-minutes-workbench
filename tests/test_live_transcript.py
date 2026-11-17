import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from unittest.mock import patch

from minutes import live_transcript


@unittest.skipUnless(os.name == "posix", "fake executable pipes require POSIX")
class LiveTranscriptTest(unittest.TestCase):
    def setUp(self):
        self.window_seconds = patch.object(live_transcript, "WINDOW_SECONDS", 1)
        self.window_bytes = patch.object(live_transcript, "WINDOW_BYTES", 32_000)
        self.window_seconds.start()
        self.window_bytes.start()
        self.addCleanup(self.window_seconds.stop)
        self.addCleanup(self.window_bytes.stop)

    @staticmethod
    def _script(path: Path, body: str) -> Path:
        path.write_text(f"#!{sys.executable}\n" + textwrap.dedent(body), encoding="utf-8")
        path.chmod(0o700)
        return path

    def _tools(self, root: Path, *, chunks=None,
               capture_exit=0, odd_tail=False, whisper_delay=0, fail_short=False, text="recognized"):
        model = root / "local-model.bin"
        model.write_bytes(b"synthetic-model-placeholder")
        chunks = chunks if chunks is not None else (live_transcript.WINDOW_BYTES + live_transcript.SAMPLE_RATE,)
        capture = self._script(root / "workbench-system-audio", f"""
            import os, signal, sys, time
            stopped = False
            def stop(_sig, _frame):
                global stopped
                stopped = True
            signal.signal(signal.SIGINT, stop)
            args = sys.argv[1:]
            if "--pcm-stdout" not in args or "--confirm" not in args:
                sys.exit(31)
            if not ("--whole-mac" in args or "--app-bundle-id" in args):
                sys.exit(32)
            if "OPENAI_API_KEY" in os.environ or "HTTPS_PROXY" in os.environ:
                sys.exit(33)
            chunks = {list(chunks)!r}
            for size in chunks:
                remaining = size
                while remaining and not stopped:
                    block = bytes(min(65536, remaining))
                    wrote = os.write(1, block)
                    remaining -= wrote
                if stopped:
                    break
            if {odd_tail!r} and not stopped:
                os.write(1, b"\\x01")
            sys.exit({capture_exit})
        """)
        whisper = self._script(root / "whisper-cli", f"""
            import io, os, sys, time, wave
            args = sys.argv[1:]
            if args[args.index("-f") + 1] != "-":
                sys.exit(41)
            if "OPENAI_API_KEY" in os.environ or "HTTPS_PROXY" in os.environ:
                sys.exit(42)
            data = sys.stdin.buffer.read()
            if not data.startswith(b"RIFF"):
                sys.exit(43)
            with wave.open(io.BytesIO(data), "rb") as stream:
                frames = stream.getnframes()
                if (stream.getframerate(), stream.getnchannels(), stream.getsampwidth()) != (16000, 1, 2):
                    sys.exit(44)
            time.sleep({whisper_delay})
            if {fail_short!r} and frames < {live_transcript.SAMPLE_RATE * live_transcript.WINDOW_SECONDS}:
                sys.exit(45)
            output = args[args.index("-of") + 1] + ".txt"
            with open(output, "w", encoding="utf-8") as handle:
                value = {text!r}
                handle.write((value + "-" + str(frames)) if value else "")
        """)
        return capture, whisper, model

    def _run(self, capture: Path, whisper: Path, model: Path, output: Path, *, mode="app"):
        kwargs = {"whole_mac": mode == "whole", "app_bundle_id": "com.example.Meeting" if mode == "app" else None,
                  "confirm": True}
        with patch.object(live_transcript.sys, "platform", "darwin"):
            return live_transcript.transcribe_live(capture, whisper, model, output, **kwargs)

    def test_synthetic_stream_writes_text_only_and_publishes_complete_marker(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            capture, whisper, model = self._tools(root)
            output = root / "minutes.txt"
            with patch.dict(os.environ, {"OPENAI_API_KEY": "synthetic-secret", "HTTPS_PROXY": "https://invalid"}):
                self._run(capture, whisper, model, output, mode="whole")
            self.assertEqual(output.read_text(encoding="utf-8"), "recognized-16000\nrecognized-8000")
            marker = json.loads((root / "minutes.txt.complete.json").read_text(encoding="utf-8"))
            self.assertEqual((marker["state"], marker["units"], marker["scope"]), ("complete", 2, "whole-mac"))
            self.assertFalse(list(root.rglob("*.wav")))
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE((root / "minutes.txt.complete.json").stat().st_mode), 0o600)

    def test_recognizer_failure_preserves_prior_units_without_complete_marker(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            capture, whisper, model = self._tools(root, fail_short=True)
            output = root / "minutes.txt"
            with self.assertRaisesRegex(RuntimeError, "認識済みのテキスト"):
                self._run(capture, whisper, model, output)
            self.assertEqual(output.read_text(encoding="utf-8"), "recognized-16000")
            self.assertFalse((root / "minutes.txt.complete.json").exists())
            self.assertFalse(list(root.rglob("*.wav")))

    def test_malformed_pcm_does_not_mark_completion(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            capture, whisper, model = self._tools(root, chunks=(), odd_tail=True)
            output = root / "minutes.txt"
            with self.assertRaisesRegex(RuntimeError, "PCM"):
                self._run(capture, whisper, model, output)
            self.assertEqual(output.read_bytes(), b"")
            self.assertFalse((root / "minutes.txt.complete.json").exists())
            self.assertFalse(list(root.rglob("*.wav")))

    def test_capture_failure_and_no_speech_have_distinct_incomplete_outcomes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            capture, whisper, model = self._tools(root, capture_exit=5)
            output = root / "capture-failed.txt"
            with self.assertRaisesRegex(RuntimeError, "正常終了しませんでした"):
                self._run(capture, whisper, model, output)
            self.assertFalse((root / "capture-failed.txt.complete.json").exists())

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            capture, whisper, model = self._tools(root, text="")
            output = root / "silent.txt"
            with self.assertRaisesRegex(RuntimeError, "認識できた発話がありません"):
                self._run(capture, whisper, model, output)
            self.assertFalse((root / "silent.txt.complete.json").exists())

    def test_backlog_stops_capture_and_never_claims_completion(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            capture, whisper, model = self._tools(
                root, chunks=(live_transcript.WINDOW_BYTES,) * 6, whisper_delay=0.1)
            output = root / "backlogged.txt"
            with self.assertRaisesRegex(RuntimeError, "追いつかず停止"):
                self._run(capture, whisper, model, output)
            self.assertFalse((root / "backlogged.txt.complete.json").exists())
            self.assertTrue(output.is_file())
            self.assertFalse(list(root.rglob("*.wav")))

    def test_sigint_requests_graceful_stop_and_hung_helper_has_deadline(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            model = root / "local-model.bin"
            model.write_bytes(b"synthetic-model-placeholder")
            ready = root / "helper-ready"
            capture = self._script(root / "workbench-system-audio", """
                import signal, sys, time
                signal.signal(signal.SIGINT, signal.SIG_IGN)
                open("__READY_PATH__", "w").close()
                while True:
                    time.sleep(0.1)
            """.replace("__READY_PATH__", str(ready)))
            whisper = self._script(root / "whisper-cli", """
                import sys
                sys.exit(1)
            """)
            output = root / "hung.txt"
            def interrupt_after_ready():
                deadline = time.monotonic() + 5
                while not ready.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                if ready.exists():
                    os.kill(os.getpid(), live_transcript.signal.SIGINT)

            timer = threading.Thread(target=interrupt_after_ready)
            with patch.object(live_transcript, "STOP_TIMEOUT_SECONDS", 0.5):
                timer.start()
                try:
                    with self.assertRaisesRegex(RuntimeError, "停止期限"):
                        self._run(capture, whisper, model, output)
                finally:
                    timer.join(timeout=5)
            self.assertFalse((root / "hung.txt.complete.json").exists())

    def test_ctrl_c_stops_helper_and_recognizes_its_final_tail(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            model = root / "local-model.bin"
            model.write_bytes(b"synthetic-model-placeholder")
            ready = root / "helper-ready"
            capture = self._script(root / "workbench-system-audio", """
                import os, signal, sys, time
                stopped = False
                def stop(_sig, _frame):
                    global stopped
                    stopped = True
                signal.signal(signal.SIGINT, stop)
                args = sys.argv[1:]
                if "--pcm-stdout" not in args or "--app-bundle-id" not in args:
                    sys.exit(31)
                os.write(1, bytes(32000))
                open("__READY_PATH__", "w").close()
                while not stopped:
                    time.sleep(0.01)
                os.write(1, bytes(8000))
                sys.exit(0)
            """.replace("__READY_PATH__", str(ready)))
            whisper = self._script(root / "whisper-cli", """
                import io, sys, wave
                args = sys.argv[1:]
                if args[args.index("-f") + 1] != "-":
                    sys.exit(41)
                with wave.open(io.BytesIO(sys.stdin.buffer.read()), "rb") as stream:
                    frames = stream.getnframes()
                with open(args[args.index("-of") + 1] + ".txt", "w") as out:
                    out.write(str(frames))
            """)
            output = root / "stopped.txt"

            def interrupt_after_ready():
                deadline = time.monotonic() + 5
                while not ready.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                if ready.exists():
                    os.kill(os.getpid(), live_transcript.signal.SIGINT)

            interrupter = threading.Thread(target=interrupt_after_ready)
            interrupter.start()
            try:
                self._run(capture, whisper, model, output)
            finally:
                interrupter.join(timeout=5)
            self.assertEqual(output.read_text(encoding="utf-8"), "16000\n4000")
            marker = json.loads((root / "stopped.txt.complete.json").read_text(encoding="utf-8"))
            self.assertTrue(marker["stop_requested"])
            self.assertEqual(marker["units"], 2)

    def test_preexisting_output_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            capture, whisper, model = self._tools(root)
            output = root / "existing.txt"
            output.write_text("keep", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "既にあります"):
                self._run(capture, whisper, model, output)
            self.assertEqual(output.read_text(encoding="utf-8"), "keep")


if __name__ == "__main__":
    unittest.main()
