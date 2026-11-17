from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch

from minutes.gui import Workspace
from minutes.recorder_host import RecorderHost
from minutes.routes import Settings, save_settings


class FakeRecognizer:
    ready = True
    message = "synthetic test recognizer"

    def __init__(self):
        self.calls = []

    def transcribe(self, pcm):
        self.calls.append(pcm)
        return f"text-{len(self.calls)}"


class RecorderHostTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.recognizer = FakeRecognizer()
        self.host = RecorderHost(data_dir=self.root / "private", recognizer=self.recognizer,
                                 static_dir=Path(__file__).parents[1] / "capture")
        self.base = self.host.base_url
        self.origin = f"http://127.0.0.1:{self.host._server.server_port}"

    def tearDown(self):
        self.host.close()
        self.temp.cleanup()

    def request(self, path, *, method="GET", body=None, headers=None):
        url = self.base + path
        req = Request(url, data=body, method=method, headers=headers or {})
        try:
            with urlopen(req, timeout=3) as response:
                return response.status, response.headers, response.read()
        except HTTPError as exc:
            try:
                return exc.code, exc.headers, exc.read()
            finally:
                exc.close()

    def post_json(self, path, data, *, ident=None, origin=None):
        headers = {"Content-Type": "application/json", "Origin": origin or self.origin}
        if ident:
            headers["X-Recording-Session"] = ident
        body = json.dumps(data).encode("utf-8") if data is not None else b""
        return self.request(path, method="POST", body=body, headers=headers)

    def test_only_allowlisted_resources_and_no_cache(self):
        status, headers, payload = self.request("")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertIn("Content-Security-Policy", headers)
        self.assertIn(b"recorder.js", payload)
        self.assertEqual(self.request("recorder.js")[0], 200)
        self.assertEqual(self.request("../minutes/routes.py")[0], 404)
        self.assertEqual(self.request("api/status")[0], 200)
        self.assertEqual(self.request("api/status?x=1")[0], 404)

    def test_text_is_durable_before_chunk_ack_and_stop_returns_status(self):
        status, _, raw = self.post_json("api/start", {})
        self.assertEqual(status, 200)
        started = json.loads(raw)
        target = self.root / "private" / started["filename"]
        self.assertTrue(target.exists())
        pcm = b"\x01\x00" * 16000
        status, _, raw = self.request("api/chunk", method="POST", body=pcm, headers={
            "Content-Type": "application/octet-stream", "Origin": self.origin,
            "X-Recording-Session": started["id"],
        })
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw), {"text": "text-1"})
        self.assertEqual(target.read_text(encoding="utf-8"), "text-1")
        if os.name != "nt":
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)
        status, _, raw = self.post_json("api/stop", {"complete": True}, ident=started["id"])
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw), {"filename": started["filename"], "status": "complete", "has_text": True})
        self.assertEqual(list((self.root / "private").glob("*.wav")), [])
        self.assertEqual(list((self.root / "private").glob("*.pcm")), [])

    def test_path_mode_without_directory_fd_saves_and_refuses_replaced_directory(self):
        folder = self.root / "portable"
        with patch("minutes.recorder_host.os.supports_dir_fd", set()):
            other = RecorderHost(data_dir=folder, recognizer=self.recognizer)
        try:
            self.assertIsNone(other._data_dir_fd)
            status, _, raw = self.post_json_for(other, "api/start", {})
            self.assertEqual(status, 200)
            started = json.loads(raw)
            headers = {"Content-Type": "application/octet-stream",
                       "Origin": f"http://127.0.0.1:{other._server.server_port}",
                       "X-Recording-Session": started["id"]}
            self.assertEqual(self.request_for(other, "api/chunk", method="POST", body=b"\x01\x00",
                                              headers=headers)[0], 200)
            self.assertEqual((folder / started["filename"]).read_text(encoding="utf-8"), "text-1")
            original = self.root / "original"
            folder.rename(original)
            folder.mkdir()
            self.assertEqual(self.request_for(other, "api/chunk", method="POST", body=b"\x02\x00",
                                              headers=headers)[0], 502)
            self.assertFalse((folder / started["filename"]).exists())
            self.assertEqual((original / started["filename"]).read_text(encoding="utf-8"), "text-1")
            status, _, _ = self.post_json_for(other, "api/stop", {"complete": False}, ident=started["id"])
            self.assertEqual(status, 200)
        finally:
            other.close()

    def test_origin_capability_and_stale_id_fail_closed(self):
        status, _, _ = self.post_json("api/start", {}, origin="http://localhost")
        self.assertEqual(status, 403)
        status, _, raw = self.post_json("api/chunk", {"anything": True}, ident="stale-recording-session-id")
        self.assertEqual(status, 409)
        self.assertEqual(json.loads(raw), {"error": "stale or unknown session"})
        status, _, _ = self.post_json("api/start", {})
        self.assertEqual(status, 200)
        status, _, _ = self.request("api/start", method="POST", body=b"{}", headers={
            "Content-Type": "application/json", "Origin": self.origin, "Host": "localhost",
        })
        self.assertEqual(status, 403)

    def test_invalid_chunk_and_unconfigured_recognizer_preserve_no_audio(self):
        status, _, raw = self.post_json("api/start", {})
        ident = json.loads(raw)["id"]
        status, _, _ = self.request("api/chunk", method="POST", body=b"\x00", headers={
            "Content-Type": "application/octet-stream", "Origin": self.origin,
            "X-Recording-Session": ident,
        })
        self.assertEqual(status, 413)
        self.assertEqual(self.recognizer.calls, [])

    def test_unavailable_status_and_start_are_explicit(self):
        class NotReady:
            ready = False
            message = "not configured"

        other = RecorderHost(data_dir=self.root / "other", recognizer=NotReady())
        try:
            req = Request(other.base_url + "api/status")
            with urlopen(req, timeout=3) as response:
                self.assertEqual(json.loads(response.read()), {"ready": False, "message": "not configured"})
            origin = f"http://127.0.0.1:{other._server.server_port}"
            req = Request(other.base_url + "api/start", data=b"{}", method="POST",
                          headers={"Content-Type": "application/json", "Origin": origin})
            with self.assertRaises(HTTPError) as caught:
                urlopen(req, timeout=3)
            self.assertEqual(caught.exception.code, 503)
            caught.exception.close()
        finally:
            other.close()

    def test_saved_settings_refresh_only_while_no_session_is_active(self):
        config = self.root / "settings.json"
        local_host = RecorderHost(config_path=config, data_dir=self.root / "configured")
        try:
            status, _, raw = self.request_for(local_host, "api/status")
            self.assertEqual(status, 200)
            self.assertFalse(json.loads(raw)["ready"])

            executable, model = self.root / "whisper-cli", self.root / "model.bin"
            executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            executable.chmod(0o700)
            model.write_bytes(b"synthetic")
            save_settings(Settings(transcription="whisper", whisper=str(executable),
                                   whisper_model=str(model)), config)
            status, _, raw = self.request_for(local_host, "api/status")
            self.assertTrue(json.loads(raw)["ready"])

            _, _, raw = self.post_json_for(local_host, "api/start", {})
            ident = json.loads(raw)["id"]
            whisper_recognizer = local_host.recognizer
            save_settings(Settings(), config)
            status, _, raw = self.request_for(local_host, "api/status")
            self.assertTrue(json.loads(raw)["ready"])
            self.assertIs(local_host.recognizer, whisper_recognizer)
            status, _, raw = self.post_json_for(local_host, "api/stop", {"complete": False}, ident=ident)
            self.assertEqual(status, 200)
            self.assertFalse(json.loads(raw)["has_text"])
            self.assertFalse(json.loads(self.request_for(local_host, "api/status")[2])["ready"])
        finally:
            local_host.close()

    def test_idle_session_keeps_partial_text_and_revokes_session(self):
        host = RecorderHost(data_dir=self.root / "idle", recognizer=self.recognizer,
                            static_dir=Path(__file__).parents[1] / "capture", session_idle_timeout=10)
        try:
            _, _, raw = self.post_json_for(host, "api/start", {})
            started = json.loads(raw)
            pcm = b"\x01\x00" * 16000
            status, _, _ = self.request_for(host, "api/chunk", method="POST", body=pcm, headers={
                "Content-Type": "application/octet-stream",
                "Origin": f"http://127.0.0.1:{host._server.server_port}",
                "X-Recording-Session": started["id"],
            })
            self.assertEqual(status, 200)
            transcript = self.root / "idle" / started["filename"]
            old = host.last_audio_activity
            host.recognition_lock.acquire()
            try:
                self.assertFalse(host._expire_abandoned_session(now=old + 11))
            finally:
                host.recognition_lock.release()
            self.assertTrue(host._expire_abandoned_session(now=old + 11))
            self.assertEqual(transcript.read_text(encoding="utf-8"), "text-1")
            status, _, raw = self.post_json_for(host, "api/stop", {"complete": True}, ident=started["id"])
            self.assertEqual(status, 409)
            _, _, raw = self.post_json_for(host, "api/start", {})
            self.assertNotEqual(json.loads(raw)["filename"], started["filename"])
            _, _, raw = self.post_json_for(host, "api/stop", {"complete": False},
                                           ident=host.active_id)
            self.assertEqual(json.loads(raw)["status"], "incomplete")
        finally:
            host.close()

    def test_complete_stop_rejects_pending_chunk_and_empty_transcript_is_explicit(self):
        _, _, raw = self.post_json("api/start", {})
        started = json.loads(raw)
        with self.host.lock:
            self.host.pending_chunks = 1
        status, _, _ = self.post_json("api/stop", {"complete": True}, ident=started["id"])
        self.assertEqual(status, 409)
        self.assertTrue(self.host.has_active_session)
        with self.host.lock:
            self.host.pending_chunks = 0
        status, _, raw = self.post_json("api/stop", {"complete": True}, ident=started["id"])
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)["has_text"], False)

    def test_workspace_exit_waits_until_active_text_session_is_stopped(self):
        class Root:
            destroyed = False

            def destroy(self):
                self.destroyed = True

        root = Root()
        workspace = Workspace.__new__(Workspace)
        workspace.root = root
        workspace.apps = {}
        workspace.recorder_host = self.host
        _, _, raw = self.post_json("api/start", {})
        ident = json.loads(raw)["id"]
        with patch("minutes.gui.messagebox.showinfo") as showinfo:
            workspace._close_workspace()
        self.assertFalse(root.destroyed)
        self.assertIs(workspace.recorder_host, self.host)
        self.assertIn("録音を停止", showinfo.call_args.args[1])
        self.post_json("api/stop", {"complete": False}, ident=ident)
        workspace._close_workspace()
        self.assertTrue(root.destroyed)
        self.assertIsNone(workspace.recorder_host)

    def test_refuses_symlink_data_directory_and_never_follows_transcript_symlink(self):
        actual = self.root / "actual"
        actual.mkdir()
        linked = self.root / "linked"
        try:
            linked.symlink_to(actual, target_is_directory=True)
        except OSError as error:
            if os.name == "nt" and error.winerror == 1314:
                self.skipTest("Windows symlink permission is not available")
            raise
        with self.assertRaises(ValueError):
            RecorderHost(data_dir=linked, recognizer=self.recognizer)

        _, _, raw = self.post_json("api/start", {})
        started = json.loads(raw)
        transcript = self.root / "private" / started["filename"]
        external = self.root / "external.txt"
        external.write_text("preserve", encoding="utf-8")
        transcript.unlink()
        transcript.symlink_to(external)
        pcm = b"\x01\x00" * 16000
        status, _, _ = self.request("api/chunk", method="POST", body=pcm, headers={
            "Content-Type": "application/octet-stream", "Origin": self.origin,
            "X-Recording-Session": started["id"],
        })
        self.assertEqual(status, 502)
        self.assertEqual(external.read_text(encoding="utf-8"), "preserve")
        self.post_json("api/stop", {"complete": False}, ident=started["id"])

    @staticmethod
    def request_for(host, path, *, method="GET", body=None, headers=None):
        req = Request(host.base_url + path, data=body, method=method, headers=headers or {})
        try:
            with urlopen(req, timeout=3) as response:
                return response.status, response.headers, response.read()
        except HTTPError as exc:
            try:
                return exc.code, exc.headers, exc.read()
            finally:
                exc.close()

    @classmethod
    def post_json_for(cls, host, path, data, *, ident=None):
        headers = {"Content-Type": "application/json",
                   "Origin": f"http://127.0.0.1:{host._server.server_port}"}
        if ident:
            headers["X-Recording-Session"] = ident
        return cls.request_for(host, path, method="POST", body=json.dumps(data).encode("utf-8"), headers=headers)


if __name__ == "__main__":
    unittest.main()
