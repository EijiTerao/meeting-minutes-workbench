import json
import shutil
import subprocess
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

from minutes import engine
from minutes.core import load_profile


class EngineTest(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node.js required for recorder interoperability test")
    def test_browser_wav_format_is_accepted_by_transcription_input(self):
        recorder = Path(__file__).resolve().parents[1] / "capture" / "recorder.js"
        wav_bytes = subprocess.check_output([
            "node", "-e",
            "const m=require(process.argv[1]);"
            "m.encodeWav([Int16Array.from([100, -100, 0])]).arrayBuffer()"
            ".then(bytes=>process.stdout.write(Buffer.from(bytes)));",
            str(recorder),
        ], timeout=10)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            original = root / "synthetic.wav"
            original.write_bytes(wav_bytes)
            model = root / "test-model.bin"
            model.touch()
            executable = root / "whisper-cli.exe"
            executable.touch()

            def fake_asr(arguments, **_):
                Path(arguments[-1] + ".txt").write_text("合成の出力", encoding="utf-8")

            with patch.object(engine.subprocess, "run", side_effect=fake_asr) as asr:
                self.assertEqual(engine.transcribe(original, executable.resolve(), model), "合成の出力")
            asr.assert_called_once()
            self.assertEqual(original.read_bytes(), wav_bytes)

    def test_generation_uses_only_selected_input(self):
        result = dict.fromkeys(("decisions", "owners", "deadlines", "next", "open"), [])
        result["decisions"] = [{"topic": "掲示", "text": "案内を1枚掲示する。"}]
        installed = {"models": [{"name": "fixture:latest", "size": 9_000_000}]}
        reply = {"done": True, "done_reason": "stop", "message": {"content": json.dumps(result)}}
        with patch.object(engine, "_request", side_effect=[installed, reply]) as request:
            self.assertEqual(
                engine.generate("案内は1枚で決定です。", "掲示の相談", "fixture", load_profile(None), ("参考は色のみ。",)),
                result,
            )
        self.assertEqual(request.call_args_list[0].args, ("GET", "/api/tags"))
        method, route, payload = request.call_args_list[1].args
        self.assertEqual((method, route), ("POST", "/api/chat"))
        contents = json.loads(payload["messages"][1]["content"])
        self.assertEqual(contents["references"], ["参考は色のみ。"])
        self.assertEqual(contents["transcript"], "案内は1枚で決定です。")
        self.assertNotIn("tools", payload)

    def test_local_model_validation_precedes_data_transfer(self):
        profile = load_profile(None)
        with patch.object(engine, "_request") as request:
            with self.assertRaises(ValueError):
                engine.generate("発言の内容", "掲示の相談", "fixture:cloud", profile)
            with self.assertRaises(ValueError):
                engine.generate("長" * engine.TEXT_BUDGET, "掲示の相談", "fixture", profile)
            request.assert_not_called()
        with patch.object(engine, "_request", return_value={"models": [
            {"name": "fixture:latest", "size": 9_000_000, "remote_host": "fixture.invalid"}
        ]}) as request:
            with self.assertRaises(ValueError):
                engine.generate("発言の内容", "掲示の相談", "fixture", profile)
            request.assert_called_once_with("GET", "/api/tags")

    def test_redirect_is_not_followed_and_response_body_is_not_read(self):
        with patch.object(engine.http.client, "HTTPConnection") as http:
            client = http.return_value
            response = client.getresponse.return_value.__enter__.return_value
            response.status = 302
            with self.assertRaises(RuntimeError):
                engine._request("GET", "/api/tags")
            http.assert_called_once_with("127.0.0.1", 11434, timeout=120)
            client.request.assert_called_once()
            response.read.assert_not_called()
            client.close.assert_called_once()

    def test_incomplete_or_invalid_result_is_not_used(self):
        for reply in ({"done": True, "done_reason": "length"},
                      {"done": True, "done_reason": "stop", "message": {"content": "{}"}}):
            with self.subTest(reply=reply), patch.object(engine, "_request", side_effect=[
                {"models": [{"name": "fixture:latest", "size": 9_000_000}]}, reply,
            ]):
                with self.assertRaises(RuntimeError):
                    engine.generate("発言の内容", "掲示の相談", "fixture", load_profile(None))

    def test_wav_only_and_no_shell_and_no_persistent_temp(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            wav = root / "架空 text.wav"
            with wave.open(str(wav), "wb") as handle:
                handle.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
                handle.writeframes(bytes(320))
            source = wav.read_bytes()
            model = root / "local model.bin"
            model.touch()
            exe = root / "whisper-cli.exe"
            exe.touch()
            out_paths = []

            def cli(args, **kwargs):
                self.assertFalse(kwargs["shell"])
                self.assertEqual(args[args.index("-f") + 1], str(wav.resolve()))
                out = Path(args[-1] + ".txt")
                out.write_text("架空の文字起こし", encoding="utf-8")
                out_paths.append(out)

            with patch.object(engine.subprocess, "run", side_effect=cli):
                self.assertEqual(engine.transcribe(wav, exe, model), "架空の文字起こし")
            self.assertEqual(wav.read_bytes(), source)
            self.assertFalse(out_paths[0].exists())
            with patch.object(engine.subprocess, "run") as run:
                with self.assertRaises(ValueError):
                    engine.transcribe(wav, root / "whisper.cmd", model)
                run.assert_not_called()
