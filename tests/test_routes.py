import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from minutes.routes import Settings, Stage, default_path, load_settings, openai_json, save_settings


class FakeResponse:
    status = 200

    def __init__(self, value):
        self.value = json.dumps(value).encode()

    def read(self, size=-1):
        return self.value[:size]


class FakeConnection:
    response = None
    instances = []

    def __init__(self, host, **kwargs):
        self.host, self.kwargs, self.calls = host, kwargs, []
        self.instances.append(self)

    def request(self, *args, **kwargs):
        self.calls.append((args, kwargs))

    def getresponse(self):
        return self.response

    def close(self):
        pass


class RoutesTests(unittest.TestCase):
    def test_defaults_and_missing_config_do_not_write(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "missing.json"
            self.assertEqual(load_settings(path), Settings())
            self.assertFalse(path.exists())
        self.assertEqual(default_path().name, "settings.json")

    def test_roundtrip_and_restricted_permissions(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "sub" / "settings.json"
            settings = Settings(correction=Stage("openai", "gpt-test"))
            save_settings(settings, path)
            self.assertEqual(load_settings(path), settings)
            if os.name != "nt":
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
            self.assertNotIn("OPENAI_API_KEY", path.read_text())

    def test_invalid_and_corrupt_config_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "settings.json"
            for raw in ('{', '{"unknown": 1}', '{"generation":{"provider":"openai"}}'):
                path.write_text(raw)
                with self.subTest(raw=raw), self.assertRaises(ValueError):
                    load_settings(path)
        for kwargs in ({"generation": Stage("openai")},
                       {"generation": Stage("cloud", "model")},
                       {"generation": Stage("openai", "https://example.com")},
                       {"generation": Stage("off", "model")}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                Settings(**kwargs)

    def test_whisper_requires_existing_file(self):
        with self.assertRaises(ValueError):
            Settings(transcription="whisper", whisper="/missing", whisper_model="base")
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "日本語 tools" / ("whisper-cli.exe" if os.name == "nt" else "whisper-cli")
            path.parent.mkdir()
            path.touch()
            model = path.parent / "モデル file.bin"
            model.touch()
            self.assertEqual(Settings(transcription="whisper", whisper=str(path), whisper_model=str(model)).transcription, "whisper")
        if os.name != "nt":
            windows = Settings(transcription="whisper", whisper=r"C:\Tools\whisper-cli.exe",
                               whisper_model=r"C:\Models\model.bin", language="auto")
            self.assertEqual(windows.language, "auto")
        for bad in ("model.bin", "/tools/whisper", "https://example.test/whisper-cli"):
            with self.subTest(path=bad), self.assertRaises(ValueError):
                Settings(transcription="whisper", whisper=bad, whisper_model="/models/model.bin")

    def test_config_bounds_duplicates_and_special_files(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "settings.json"
            for raw in ('{"language":"ja","language":"en"}', "{" + '"a":' * 2000 + '0' + '}' * 2000):
                path.write_text(raw)
                with self.subTest(raw=raw[:40]), self.assertRaises(ValueError):
                    load_settings(path)
            path.write_bytes(b" " * 16_385)
            with self.assertRaises(ValueError):
                load_settings(path)

    def test_config_symlink_not_followed_or_overwritten(self):
        with tempfile.TemporaryDirectory() as d:
            target, link = Path(d) / "real.json", Path(d) / "link.json"
            target.write_text("{}")
            try:
                link.symlink_to(target)
            except (OSError, NotImplementedError):
                self.skipTest("OS does not allow this user to create symlinks")
            with self.assertRaises(ValueError):
                load_settings(link)
            with self.assertRaises(ValueError):
                save_settings(Settings(), link)
            self.assertEqual(target.read_text(), "{}")

    def test_openai_call_shape_and_exact_result(self):
        FakeConnection.instances.clear()
        FakeConnection.response = FakeResponse({"status": "completed", "output": [
            {"type": "message", "content": [{"type": "output_text", "text": '{"ok":true}'}]}]})
        with patch("minutes.routes.http.client.HTTPSConnection", FakeConnection), \
             patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test-secret"}):
            with self.assertRaises(PermissionError):
                openai_json("model", "system", {}, {}, authorized=False)
            self.assertEqual(openai_json("model", "指示\n複数行のプロンプト", {"x": 1}, {"type": "object"}, authorized=True),
                             {"ok": True})
        conn = FakeConnection.instances[-1]
        self.assertEqual(conn.host, "api.openai.com")
        args, kwargs = conn.calls[0]
        self.assertEqual(args[:2], ("POST", "/v1/responses"))
        request = json.loads(kwargs["body"])
        self.assertFalse(request["store"])
        self.assertEqual(request["truncation"], "disabled")
        self.assertEqual(request["text"]["format"]["strict"], True)
        self.assertEqual(request["input"][0]["content"], "指示\n複数行のプロンプト")
        self.assertGreaterEqual(conn.kwargs["timeout"], 120)
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer sk-test-secret")

    def test_openai_rejects_incomplete_refusal_tools_and_multiple_texts(self):
        cases = [
            {"status": "incomplete", "output": []},
            {"status": "completed", "output": [{"type": "message", "content": [{"type": "refusal"}]}]},
            {"status": "completed", "output": [{"type": "function_call"}]},
            {"status": "completed", "output": [{"type": "message", "content": [
                {"type": "output_text", "text": "{}"}, {"type": "output_text", "text": "{}"}]}]},
        ]
        with patch("minutes.routes.http.client.HTTPSConnection", FakeConnection), \
             patch.dict(os.environ, {"OPENAI_API_KEY": "secret-test-value"}):
            for value in cases:
                FakeConnection.response = FakeResponse(value)
                with self.subTest(value=value), self.assertRaises(ValueError):
                    openai_json("model", "system", {}, {}, authorized=True)


if __name__ == "__main__":
    unittest.main()
