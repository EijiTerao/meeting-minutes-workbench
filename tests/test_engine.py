import json
import shutil
import subprocess
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

from minutes import engine
from minutes.asr_job import transcribe_to_text
from minutes.core import load_profile


class EngineTest(unittest.TestCase):
    @staticmethod
    def _wav(path: Path, seconds: int = 1):
        with wave.open(str(path), "wb") as handle:
            handle.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
            handle.writeframes(bytes(32000 * seconds))

    def test_long_wav_is_segmented_sequentially_with_progress(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            wav, model, exe = root / "long.wav", root / "model.bin", root / "whisper-cli"
            self._wav(wav, 5)
            model.touch(); exe.touch()
            seen = []
            cli_calls = 0

            def cli(args, **_):
                nonlocal cli_calls
                cli_calls += 1
                with wave.open(args[args.index("-f") + 1], "rb") as segment:
                    seen.append(segment.getnframes())
                Path(args[-1] + ".txt").write_text(f"chunk{cli_calls}", encoding="utf-8")

            with patch.object(engine, "ASR_SEGMENT_SECONDS", 2), patch.object(engine.subprocess, "run", side_effect=cli):
                result = engine.transcribe(wav, exe, model, on_part=lambda n, text: seen.append((n, text)))
            self.assertEqual(result, "chunk1\nchunk2\nchunk3")
            self.assertEqual(seen, [32000, (1, "chunk1"), 32000, (2, "chunk2"), 16000, (3, "chunk3")])

    def test_failure_keeps_only_completed_progress_and_names_part(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            wav, model, exe = root / "long.wav", root / "model.bin", root / "whisper-cli"
            self._wav(wav, 5); model.touch(); exe.touch()
            progress = []
            calls = 0

            def cli(args, **_):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise subprocess.CalledProcessError(1, args)
                Path(args[-1] + ".txt").write_text("first", encoding="utf-8")

            with patch.object(engine, "ASR_SEGMENT_SECONDS", 2), patch.object(engine.subprocess, "run", side_effect=cli):
                with self.assertRaisesRegex(RuntimeError, "区間 2"):
                    engine.transcribe(wav, exe, model, on_part=lambda n, text: progress.append((n, text)))
            self.assertEqual(progress, [(1, "first")])

    def test_durable_asr_restart_skips_committed_units_and_marks_state(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            wav, model, exe, output = root / "long.wav", root / "model.bin", root / "whisper-cli", root / "lecture.txt"
            self._wav(wav, 5); model.touch(); exe.touch()
            calls = []

            def first_attempt(args, **_):
                calls.append(Path(args[args.index("-f") + 1]).stat().st_size)
                if len(calls) == 2:
                    raise subprocess.CalledProcessError(1, args)
                Path(args[-1] + ".txt").write_text("one", encoding="utf-8")

            with patch.object(engine, "ASR_SEGMENT_SECONDS", 2), patch.object(engine.subprocess, "run", side_effect=first_attempt):
                with self.assertRaisesRegex(RuntimeError, "途中保存"):
                    transcribe_to_text(wav, exe, model, "ja", output)
            manifest_path = root / "lecture.asr-checkpoint" / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["state"], "partial")
            self.assertEqual([part["index"] for part in manifest["parts"]], [1])
            self.assertEqual((root / "lecture.partial.txt").read_text(encoding="utf-8"), "one")

            calls.clear()
            def resumed_attempt(args, **_):
                calls.append(Path(args[args.index("-f") + 1]).stat().st_size)
                Path(args[-1] + ".txt").write_text(f"unit{len(calls) + 1}", encoding="utf-8")

            with patch.object(engine, "ASR_SEGMENT_SECONDS", 2), patch.object(engine.subprocess, "run", side_effect=resumed_attempt):
                result = transcribe_to_text(wav, exe, model, "ja", output)
            self.assertEqual(result, "one\nunit2\nunit3")
            self.assertEqual(calls, [64_044, 32_044])
            self.assertEqual(json.loads(manifest_path.read_text(encoding="utf-8"))["state"], "complete")
            self.assertEqual(output.read_text(encoding="utf-8"), result)

    def test_empty_first_asr_unit_allows_first_text_unit_to_write_partial(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            wav, model, exe, output = root / "audio.wav", root / "model.bin", root / "whisper-cli", root / "lecture.txt"
            self._wav(wav, 3); model.touch(); exe.touch()
            calls = 0

            def cli(args, **_):
                nonlocal calls
                calls += 1
                text = "" if calls == 1 else "発話あり"
                Path(args[-1] + ".txt").write_text(text, encoding="utf-8")

            with patch.object(engine, "ASR_SEGMENT_SECONDS", 2), patch.object(engine.subprocess, "run", side_effect=cli):
                result = transcribe_to_text(wav, exe, model, "ja", output)
            self.assertEqual(result, "発話あり")
            self.assertEqual((root / "lecture.partial.txt").read_text(encoding="utf-8"), "発話あり")
            manifest = json.loads((root / "lecture.asr-checkpoint" / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual([part["index"] for part in manifest["parts"]], [1, 2])
            self.assertEqual(manifest["state"], "complete")

    def test_asr_resume_refuses_changed_audio_without_mutating_checkpoint(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            wav, model, exe, output = root / "audio.wav", root / "model.bin", root / "whisper-cli", root / "lecture.txt"
            self._wav(wav, 3); model.touch(); exe.touch()
            calls = 0
            def fail_second(args, **_):
                nonlocal calls
                calls += 1
                if calls == 1:
                    Path(args[-1] + ".txt").write_text("saved", encoding="utf-8")
                else:
                    raise subprocess.CalledProcessError(1, args)
            with patch.object(engine, "ASR_SEGMENT_SECONDS", 2), patch.object(engine.subprocess, "run", side_effect=fail_second):
                with self.assertRaises(RuntimeError):
                    transcribe_to_text(wav, exe, model, "ja", output)
            checkpoint = root / "lecture.asr-checkpoint"
            before = {path.name: path.read_bytes() for path in checkpoint.iterdir() if path.is_file()}
            partial = root / "lecture.partial.txt"
            partial_before = partial.read_bytes()
            self._wav(wav, 4)
            with patch.object(engine, "ASR_SEGMENT_SECONDS", 2), patch.object(engine.subprocess, "run") as run:
                with self.assertRaisesRegex(ValueError, "入力・認識器・モデル・言語"):
                    transcribe_to_text(wav, exe, model, "ja", output)
                run.assert_not_called()
            self.assertEqual({path.name: path.read_bytes() for path in checkpoint.iterdir() if path.is_file()}, before)
            self.assertEqual(partial.read_bytes(), partial_before)

    def test_truncated_pcm_after_completed_segment_never_reports_completion(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            wav, model, exe = root / "truncated.wav", root / "model.bin", root / "whisper-cli"
            self._wav(wav, 4)
            # Keep the valid WAV header but remove one second of PCM payload.
            wav.write_bytes(wav.read_bytes()[:-32000])
            model.touch(); exe.touch()
            progress = []

            def cli(args, **_):
                Path(args[-1] + ".txt").write_text("first", encoding="utf-8")

            with patch.object(engine, "ASR_SEGMENT_SECONDS", 2), patch.object(engine.subprocess, "run", side_effect=cli):
                with self.assertRaisesRegex(RuntimeError, "途中で終わっています.*区間 2"):
                    engine.transcribe(wav, exe, model, on_part=lambda n, text: progress.append((n, text)))
            self.assertEqual(progress, [(1, "first")])

    def test_ordered_recorder_folder_and_invalid_or_corrupt_inputs(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); folder = root / "recording"; folder.mkdir()
            self._wav(folder / "part-000002.wav"); self._wav(folder / "part-000001.wav")
            (folder / "notes.txt").write_text("ignored", encoding="utf-8")
            model, exe = root / "model.bin", root / "whisper-cli"
            model.touch(); exe.touch()
            order = []

            def cli(args, **_):
                order.append(Path(args[args.index("-f") + 1]).stat().st_size)
                Path(args[-1] + ".txt").write_text(f"part{len(order)}", encoding="utf-8")

            with patch.object(engine.subprocess, "run", side_effect=cli):
                self.assertEqual(engine.transcribe(folder, exe, model), "part1")
            self.assertEqual(len(order), 1)
            (folder / "part-000003.wav").write_bytes(b"not wave")
            with patch.object(engine.subprocess, "run") as run:
                with self.assertRaises(ValueError):
                    engine.transcribe(folder, exe, model)
                run.assert_not_called()
            broken = root / "broken.wav"; broken.write_bytes(b"broken")
            with self.assertRaisesRegex(ValueError, "WAVを読み込めません"):
                engine.transcribe(broken, exe, model)

    def test_five_short_parts_are_assembled_in_order_into_fewer_asr_calls(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); folder = root / "recording"; folder.mkdir()
            expected_pcm = bytearray()
            for index in range(1, 6):
                path = folder / f"part-{index:06d}.wav"
                sample = index.to_bytes(2, "little", signed=True)
                pcm = sample * 16000
                expected_pcm.extend(pcm)
                with wave.open(str(path), "wb") as handle:
                    handle.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
                    handle.writeframes(pcm)
            model, exe = root / "model.bin", root / "whisper-cli"
            model.touch(); exe.touch()
            observed_pcm = []

            def cli(args, **_):
                with wave.open(args[args.index("-f") + 1], "rb") as segment:
                    frames = segment.getnframes()
                    observed_pcm.append(segment.readframes(frames))
                Path(args[-1] + ".txt").write_text(f"segment{len(observed_pcm)}", encoding="utf-8")

            with patch.object(engine, "ASR_SEGMENT_SECONDS", 2), patch.object(engine.subprocess, "run", side_effect=cli):
                result = engine.transcribe(folder, exe, model)
            self.assertEqual(result, "segment1\nsegment2\nsegment3")
            self.assertEqual(len(observed_pcm), 3)
            self.assertEqual(b"".join(observed_pcm), bytes(expected_pcm))
            self.assertEqual([len(pcm) for pcm in observed_pcm], [64000, 64000, 32000])

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
                with wave.open(args[args.index("-f") + 1], "rb") as segment:
                    self.assertEqual(segment.getnframes(), 160)
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
