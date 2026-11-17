"""Loopback-only host for the public text-first recorder page."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import shutil
import stat
import subprocess
import tempfile
import threading
import time
from urllib.parse import urlsplit

from .live_transcript import SAMPLE_RATE, SAMPLE_WIDTH, WINDOW_BYTES, _pcm_window_as_wav
from .routes import Settings, default_path, load_settings

MAX_CHUNK = WINDOW_BYTES
MAX_JSON = 1024
MAX_TEXT_BYTES = 1_000_000
REQUEST_TIMEOUT = 20
RECOGNITION_TIMEOUT = 180
MAX_REQUESTS = 8
SESSION_IDLE_TIMEOUT = 180


def _plain_path(info, expected_type) -> bool:
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return expected_type(info.st_mode) and not (getattr(info, "st_file_attributes", 0) & reparse_flag)


def _file_identity(info) -> tuple[int, int]:
    return info.st_dev, info.st_ino


class _Recognizer:
    def __init__(self, settings: Settings, data_dir: Path, apple_helper: str | None = None):
        self.settings = settings
        self.data_dir = data_dir
        self.apple_helper = apple_helper or os.environ.get("MINUTES_APPLE_TRANSCRIBER", "")
        self.ready = False
        self.message = "文字起こし設定がありません。設定でローカルWhisperを指定してください。"
        if settings.transcription == "whisper":
            exe, model = Path(settings.whisper), Path(settings.whisper_model)
            self.ready = (exe.is_absolute() and exe.name in ("whisper-cli", "whisper-cli.exe")
                          and exe.is_file() and os.access(exe, os.X_OK)
                          and model.is_absolute() and model.is_file())
            self.message = "端末内のWhisperを使用します。" if self.ready else "Whisper実行ファイルまたはモデルを確認してください。"
        elif self.apple_helper:
            helper = Path(self.apple_helper)
            self.ready = (helper.is_absolute() and helper.name == "workbench-apple-transcribe"
                          and helper.is_file() and os.access(helper, os.X_OK)
                          and settings.language in ("ja", "ko"))
            self.message = "端末内の音声認識を使用します。" if self.ready else "設定済み音声認識ヘルパーを確認してください。"

    def transcribe(self, pcm: bytes) -> str:
        if not self.ready:
            raise RuntimeError(self.message)
        private = Path(tempfile.mkdtemp(prefix="minutes-recorder-"))
        try:
            os.chmod(private, 0o700)
            environment = {"PATH": os.defpath, "LANG": "C", "LC_ALL": "C",
                           "TMPDIR": str(private), "TEMP": str(private), "TMP": str(private)}
            if os.name == "nt" and "SystemRoot" in os.environ:
                environment["SystemRoot"] = os.environ["SystemRoot"]
            if self.settings.transcription == "whisper":
                output_base = private / "unit"
                command = [self.settings.whisper, "-m", self.settings.whisper_model,
                           "-f", "-", "-l", self.settings.language, "-otxt", "-of", str(output_base)]
                subprocess.run(command, input=_pcm_window_as_wav(pcm), stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, timeout=RECOGNITION_TIMEOUT,
                               check=True, shell=False, cwd=private, env=environment)
                text_path = output_base.with_suffix(".txt")
                if text_path.is_symlink() or not text_path.is_file() or text_path.stat().st_size > MAX_TEXT_BYTES:
                    raise RuntimeError("文字起こし結果を確認できません。")
                return text_path.read_text(encoding="utf-8-sig").rstrip("\r\n")
            language = self.settings.language if self.settings.language in ("ja", "ko") else "ja"
            result = subprocess.run([self.apple_helper, "--pcm-stdin", "--language", language],
                                    input=pcm, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                    timeout=RECOGNITION_TIMEOUT, check=True, shell=False, cwd=private,
                                    env=environment)
            if len(result.stdout) > MAX_TEXT_BYTES:
                raise RuntimeError("文字起こし結果が上限を超えています。")
            return result.stdout.decode("utf-8").rstrip("\r\n")
        finally:
            shutil.rmtree(private, ignore_errors=True)


class RecorderHost:
    """Owns one ephemeral loopback listener and its private text-only sessions."""

    def __init__(self, *, config_path: Path | None = None, data_dir: Path | None = None,
                 recognizer=None, static_dir: Path | None = None,
                 session_idle_timeout: float = SESSION_IDLE_TIMEOUT):
        config = Path(config_path) if config_path is not None else default_path()
        self._config_path = config
        self.token = secrets.token_urlsafe(32)
        self.data_dir = Path(data_dir) if data_dir is not None else config.parent / "live-transcripts"
        try:
            existing = self.data_dir.lstat()
        except FileNotFoundError:
            existing = None
        if existing is not None and not _plain_path(existing, stat.S_ISDIR):
            raise ValueError("live transcript path must be a regular directory")
        self.data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory = self.data_dir.lstat()
        if not _plain_path(directory, stat.S_ISDIR):
            raise ValueError("live transcript path must be a regular directory")
        self._data_dir_identity = _file_identity(directory)
        self._data_dir_fd = None
        if os.open in os.supports_dir_fd and hasattr(os, "fchmod"):
            self._data_dir_fd = os.open(self.data_dir, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                                        | getattr(os, "O_NOFOLLOW", 0))
            try:
                if _file_identity(os.fstat(self._data_dir_fd)) != self._data_dir_identity:
                    raise OSError("transcript directory changed")
                os.fchmod(self._data_dir_fd, 0o700)
            except OSError:
                os.close(self._data_dir_fd)
                raise
        try:
            settings = load_settings(config)
        except ValueError:
            settings = Settings()
        self._injected_recognizer = recognizer is not None
        self.recognizer = recognizer if recognizer is not None else _Recognizer(settings, self.data_dir)
        self.static_dir = Path(static_dir) if static_dir else Path(__file__).resolve().parents[1] / "capture"
        self.active_id: str | None = None
        self.active_file: Path | None = None
        self.active_file_identity: tuple[int, int] | None = None
        self.received_pcm_bytes = 0
        self.has_text = False
        self.pending_chunks = 0
        self.closing_session = False
        self.last_audio_activity = 0.0
        self.session_idle_timeout = session_idle_timeout
        self.closing = False
        self._closed = False
        self._close_lock = threading.Lock()
        self.lock = threading.RLock()
        self.recognition_lock = threading.Lock()
        self._server = _Server(("127.0.0.1", 0), self._handler())
        self._server.host_owner = self
        self._thread = threading.Thread(target=self._server.serve_forever, name="minutes-recorder-host", daemon=True)
        self._thread.start()
        self.base_url = f"http://127.0.0.1:{self._server.server_port}/r/{self.token}/"
        self._reaper_stop = threading.Event()
        self._reaper = threading.Thread(target=self._reaper_loop, name="minutes-recorder-session-reaper", daemon=True)
        self._reaper.start()

    def _open_transcript(self, name: str, *, create: bool) -> int:
        flags = os.O_WRONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        flags |= (os.O_CREAT | os.O_EXCL) if create else os.O_APPEND
        mode = stat.S_IRUSR | stat.S_IWUSR
        if self._data_dir_fd is not None:
            fd = os.open(name, flags, mode, dir_fd=self._data_dir_fd)
        else:
            # Windows does not support dir_fd. Use only server-generated names and
            # refuse directory replacement, symlinks/junctions and swapped files.
            folder = self.data_dir.lstat()
            if not _plain_path(folder, stat.S_ISDIR) or _file_identity(folder) != self._data_dir_identity:
                raise OSError("transcript directory changed")
            path = self.data_dir / name
            if not create:
                existing = path.lstat()
                if not _plain_path(existing, stat.S_ISREG) or _file_identity(existing) != self.active_file_identity:
                    raise OSError("transcript file changed")
            fd = os.open(path, flags, mode)
        try:
            opened = os.fstat(fd)
            if not _plain_path(opened, stat.S_ISREG) or (not create and _file_identity(opened) != self.active_file_identity):
                raise OSError("transcript file changed")
            return fd
        except OSError:
            os.close(fd)
            raise

    def _refresh_recognizer_if_idle(self):
        if self._injected_recognizer:
            return
        with self.lock:
            if self.active_id is not None or self.closing:
                return
            try:
                settings = load_settings(self._config_path)
            except ValueError:
                settings = Settings()
            self.recognizer = _Recognizer(settings, self.data_dir)

    def _reaper_loop(self):
        while not self._reaper_stop.wait(min(30.0, max(0.1, self.session_idle_timeout / 6))):
            self._expire_abandoned_session()

    def _expire_abandoned_session(self, *, now: float | None = None) -> bool:
        if not self.recognition_lock.acquire(blocking=False):
            return False
        try:
            with self.lock:
                current = time.monotonic() if now is None else now
                if (self.active_id is None or self.closing_session or self.pending_chunks
                        or current - self.last_audio_activity <= self.session_idle_timeout):
                    return False
                # Keep the partial TXT, but revoke the capability without claiming completion.
                self.active_id, self.active_file = None, None
                self.received_pcm_bytes, self.has_text = 0, False
                self.pending_chunks, self.closing_session = 0, False
                self.last_audio_activity = 0.0
                return True
        finally:
            self.recognition_lock.release()

    @property
    def has_active_session(self) -> bool:
        with self.lock:
            return self.active_id is not None

    def _handler(self):
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def setup(self):
                super().setup()
                self.connection.settimeout(REQUEST_TIMEOUT)

            def log_message(self, *_args):
                return

            def _headers(self, status=200, length=0, content_type="application/json; charset=utf-8"):
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(length))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("X-Frame-Options", "DENY")
                self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'self' blob:; worker-src blob:; connect-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none'")
                self.end_headers()

            def _json(self, code, data):
                payload = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                self._headers(code, len(payload))
                self.wfile.write(payload)

            def _valid_common(self, post=False):
                host = self.headers.get("Host", "")
                if host != f"127.0.0.1:{owner._server.server_port}":
                    return False
                if post and self.headers.get("Origin") != f"http://127.0.0.1:{owner._server.server_port}":
                    return False
                return True

            def _route(self):
                path = urlsplit(self.path)
                if path.query or path.fragment or not path.path.startswith(f"/r/{owner.token}/"):
                    return None
                return path.path[len(f"/r/{owner.token}/"):]

            def do_GET(self):
                if not self._valid_common():
                    return self._json(403, {"error": "request rejected"})
                route = self._route()
                if route == "api/status":
                    owner._refresh_recognizer_if_idle()
                    return self._json(200, {"ready": bool(owner.recognizer.ready), "message": str(owner.recognizer.message)})
                names = {"": ("recorder.html", "text/html; charset=utf-8"),
                         "recorder.js": ("recorder.js", "text/javascript; charset=utf-8")}
                item = names.get(route)
                if item is None:
                    return self._json(404, {"error": "not found"})
                try:
                    file = owner.static_dir / item[0]
                    if file.is_symlink() or not file.is_file():
                        return self._json(404, {"error": "not found"})
                    body = file.read_bytes()
                except OSError:
                    return self._json(404, {"error": "not found"})
                self._headers(200, len(body), item[1])
                self.wfile.write(body)

            def _post_gate(self):
                if not self._valid_common(post=True):
                    self._json(403, {"error": "request rejected"})
                    return None
                route = self._route()
                if route not in ("api/start", "api/chunk", "api/stop"):
                    self._json(404, {"error": "not found"})
                    return None
                return route

            def _read_json(self, allow_empty=False):
                raw_length = self.headers.get("Content-Length", "")
                if not raw_length.isdigit() or int(raw_length) > MAX_JSON:
                    raise ValueError("invalid request body")
                raw = self.rfile.read(int(raw_length))
                if not raw and allow_empty:
                    return {}
                value = json.loads(raw.decode("utf-8"))
                if not isinstance(value, dict):
                    raise ValueError("invalid request body")
                return value

            def do_POST(self):
                route = self._post_gate()
                if route is None:
                    return
                try:
                    if route == "api/start":
                        if self.headers.get("Content-Type", "").split(";", 1)[0] not in ("", "application/json"):
                            return self._json(415, {"error": "unsupported content type"})
                        body = self._read_json(allow_empty=True)
                        if body:
                            return self._json(400, {"error": "invalid request body"})
                        owner._refresh_recognizer_if_idle()
                        with owner.lock:
                            if owner.closing:
                                return self._json(503, {"error": "recorder host is closing"})
                            if not owner.recognizer.ready:
                                return self._json(503, {"error": owner.recognizer.message})
                            if owner.active_id is not None:
                                return self._json(409, {"error": "recording already active"})
                            ident = secrets.token_urlsafe(24)
                            stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
                            target = owner.data_dir / f"live-{stamp}-{secrets.token_hex(5)}.txt"
                            fd = owner._open_transcript(target.name, create=True)
                            with os.fdopen(fd, "wb") as handle:
                                handle.flush()
                                os.fsync(handle.fileno())
                                owner.active_file_identity = _file_identity(os.fstat(handle.fileno()))
                            owner.active_id, owner.active_file = ident, target
                            owner.received_pcm_bytes, owner.has_text = 0, False
                            owner.pending_chunks, owner.closing_session = 0, False
                            owner.last_audio_activity = time.monotonic()
                        return self._json(200, {"id": ident, "filename": target.name})
                    ident = self.headers.get("X-Recording-Session", "")
                    with owner.lock:
                        if not ident or ident != owner.active_id or owner.active_file is None:
                            return self._json(409, {"error": "stale or unknown session"})
                    if route == "api/chunk":
                        length = self.headers.get("Content-Length", "")
                        if not length.isdigit() or not 2 <= int(length) <= MAX_CHUNK or int(length) % SAMPLE_WIDTH:
                            return self._json(413, {"error": "invalid chunk size"})
                        if self.headers.get("Content-Type", "").split(";", 1)[0] != "application/octet-stream":
                            return self._json(415, {"error": "unsupported content type"})
                        pcm = self.rfile.read(int(length))
                        if len(pcm) != int(length):
                            return self._json(400, {"error": "incomplete chunk"})
                        with owner.lock:
                            if (ident != owner.active_id or owner.active_file is None
                                    or owner.closing_session):
                                return self._json(409, {"error": "stale or closing session"})
                            owner.pending_chunks += 1
                            owner.last_audio_activity = time.monotonic()
                        with owner.recognition_lock:
                            try:
                                with owner.lock:
                                    if ident != owner.active_id or owner.active_file is None:
                                        return self._json(409, {"error": "stale or unknown session"})
                                    recognizer = owner.recognizer
                                text = recognizer.transcribe(pcm)
                                encoded = text.encode("utf-8")
                                if len(encoded) > MAX_TEXT_BYTES:
                                    return self._json(502, {"error": "transcription result exceeds limit"})
                                with owner.lock:
                                    if ident != owner.active_id or owner.active_file is None:
                                        return self._json(409, {"error": "stale or unknown session"})
                                    fd = owner._open_transcript(owner.active_file.name, create=False)
                                    with os.fdopen(fd, "ab") as output:
                                        if text.strip():
                                            output.write((b"\n" if owner.has_text else b"") + encoded)
                                        output.flush()
                                        os.fsync(output.fileno())
                                    owner.has_text = owner.has_text or bool(text.strip())
                                    owner.received_pcm_bytes += len(pcm)
                                    owner.last_audio_activity = time.monotonic()
                                return self._json(200, {"text": text})
                            finally:
                                with owner.lock:
                                    owner.pending_chunks = max(0, owner.pending_chunks - 1)
                    body = self._read_json()
                    if set(body) != {"complete"} or not isinstance(body["complete"], bool):
                        return self._json(400, {"error": "invalid request body"})
                    if body["complete"]:
                        with owner.lock:
                            if (ident != owner.active_id or owner.active_file is None
                                    or owner.pending_chunks or owner.closing_session):
                                return self._json(409, {"error": "recognition pending or session unavailable"})
                            owner.closing_session = True
                        with owner.recognition_lock:
                            with owner.lock:
                                if ident != owner.active_id or owner.active_file is None:
                                    return self._json(409, {"error": "stale or unknown session"})
                                target = owner.active_file
                                has_text = owner.has_text
                                owner.active_id, owner.active_file = None, None
                                owner.received_pcm_bytes, owner.has_text = 0, False
                                owner.pending_chunks, owner.closing_session = 0, False
                                owner.last_audio_activity = 0.0
                    else:
                        with owner.lock:
                            if ident != owner.active_id or owner.active_file is None:
                                return self._json(409, {"error": "stale or unknown session"})
                            target, has_text = owner.active_file, owner.has_text
                            owner.active_id, owner.active_file = None, None
                            owner.received_pcm_bytes, owner.has_text = 0, False
                            owner.pending_chunks, owner.closing_session = 0, False
                            owner.last_audio_activity = 0.0
                    return self._json(200, {"filename": target.name,
                                            "status": "complete" if body["complete"] else "incomplete",
                                            "has_text": has_text})
                except (ValueError, UnicodeError, json.JSONDecodeError):
                    return self._json(400, {"error": "invalid request"})
                except (OSError, subprocess.SubprocessError, RuntimeError):
                    return self._json(502, {"error": "local transcription or storage failed"})

            def log_request(self, *_args):
                return

        return Handler

    def close(self):
        with self._close_lock:
            if self._closed:
                return True
            with self.lock:
                if self.active_id is not None:
                    return False
                self.closing = True
            self._reaper_stop.set()
            self._reaper.join(timeout=2)
            self._server.shutdown()
            self._server.server_close()
            self._thread.join(timeout=3)
            if self._data_dir_fd is not None:
                os.close(self._data_dir_fd)
            self._closed = True
            return True


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, *args, **kwargs):
        self._slots = threading.BoundedSemaphore(MAX_REQUESTS)
        super().__init__(*args, **kwargs)

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            request.close()
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()
