"""Bounded native desktop capture and incremental text persistence."""

from __future__ import annotations

import os
from pathlib import Path
import queue
import signal
import subprocess
import threading
import time
import tempfile
import wave
import json
import stat

from .live_transcript import SAMPLE_RATE, SAMPLE_WIDTH, WINDOW_BYTES

QUEUE_WINDOWS = 2
_END = object()


class DesktopCapture:
    """One asynchronous desktop capture; callbacks receive (state, payload)."""

    def __init__(self, *, capture_helper: str | None = None, recognizer=None,
                 capture_factory=None, capture_lock: threading.BoundedSemaphore | None = None) -> None:
        self.capture_helper = capture_helper or os.environ.get("MINUTES_SYSTEM_AUDIO_CAPTURE", "")
        self.recognizer = recognizer
        self.capture_factory = capture_factory or subprocess.Popen
        self.capture_lock = capture_lock or threading.BoundedSemaphore(1)
        self._thread: threading.Thread | None = None
        self._process = None
        self._process_lock = threading.Lock()
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self.running = False

    def start(self, output_txt: Path, *, mode: str, include_microphone: bool = False,
              save_wav_parts: bool = False, on_event=None) -> None:
        with self._lock:
            if self.running:
                raise RuntimeError("録音中です。")
            if mode not in ("microphone-only", "whole-mac"):
                raise ValueError("収音方式を確認してください。")
            if not self.capture_helper or not Path(self.capture_helper).is_absolute():
                raise ValueError("配布版の収音補助ツールがありません。")
            output_txt = Path(output_txt)
            if not output_txt.is_absolute() or not output_txt.parent.is_dir():
                raise ValueError("保存先は既存フォルダ内の絶対パスにしてください。")
            parent_info = output_txt.parent.lstat()
            if not stat.S_ISDIR(parent_info.st_mode):
                raise ValueError("保存先フォルダは通常のフォルダを指定してください。")
            if output_txt.exists() or output_txt.is_symlink():
                raise ValueError("保存先TXTは既にあります。別の名前を指定してください。")
            if save_wav_parts and output_txt.with_suffix("").exists():
                raise ValueError("分割WAVの保存フォルダが既にあります。")
            if output_txt.with_suffix("").is_symlink():
                raise ValueError("分割WAVの保存先を確認してください。")
            if not self.capture_lock.acquire(blocking=False):
                raise RuntimeError("別の会議で録音中です。停止後に再試行してください。")
            self.running = True
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run, args=(output_txt, mode, include_microphone, save_wav_parts, on_event),
                name="desktop-capture", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        already_stopping = self._stop.is_set()
        self._stop.set()
        with self._process_lock:
            process = self._process
        if not already_stopping and process is not None and process.poll() is None:
            self._signal_stop(process)

    def _signal_stop(self, process) -> None:
        try:
            process.send_signal(signal.SIGINT)
        except OSError:
            pass
        threading.Thread(target=self._force_stop_after_timeout, args=(process,),
                         name="desktop-capture-stop-watchdog", daemon=True).start()

    @staticmethod
    def _force_stop_after_timeout(process) -> None:
        try:
            process.wait(timeout=12)
            return
        except (subprocess.TimeoutExpired, TypeError):
            pass
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (AttributeError, OSError):
            try:
                process.kill()
            except (AttributeError, OSError):
                pass

    def _emit(self, callback, state: str, payload=None) -> None:
        if callback is not None:
            callback(state, payload)

    def _run(self, output: Path, mode: str, include_mic: bool, save_wav: bool, callback) -> None:
        errors: list[BaseException] = []
        pcm_queue: queue.Queue[bytes | object] = queue.Queue(maxsize=QUEUE_WINDOWS)
        cancel_reader = threading.Event()
        ready = threading.Event()
        output_handle = None
        reader = None
        private_temp = None
        try:
            private_temp = tempfile.TemporaryDirectory(prefix="minutes-desktop-capture-")
            if self.recognizer is None:
                raise RuntimeError("端末内の音声認識が利用できません。")
            args = [self.capture_helper, "--pcm-stdout", "--confirm", "--" + mode]
            if include_mic and mode == "whole-mac":
                args.append("--include-microphone")
            child_env = {"PATH": os.defpath, "LANG": "C", "LC_ALL": "C",
                         "TMPDIR": private_temp.name}
            process = self.capture_factory(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                           stderr=subprocess.PIPE, shell=False, bufsize=0,
                                           start_new_session=True, env=child_env)
            with self._process_lock:
                self._process = process
                # A stop may race with process creation. Publish and signal under
                # one lock so a newly-created child cannot escape the stop request.
                if self._stop.is_set() and process.poll() is None:
                    self._signal_stop(process)

            def read_stderr() -> None:
                try:
                    while True:
                        line = process.stderr.readline()
                        if not line:
                            return
                        if line.strip() == b"WORKBENCH_CAPTURE_READY":
                            ready.set()
                except BaseException as exc:
                    errors.append(exc)

            err_thread = threading.Thread(target=read_stderr, name="desktop-capture-status", daemon=True)
            err_thread.start()

            def read_pcm() -> None:
                pending = bytearray()
                def enqueue(item) -> bool:
                    while not cancel_reader.is_set():
                        try:
                            pcm_queue.put(item, timeout=0.2)
                            return True
                        except queue.Full:
                            continue
                    return False
                try:
                    while True:
                        block = process.stdout.read(min(64 * 1024, WINDOW_BYTES - len(pending)))
                        if not block:
                            break
                        pending.extend(block)
                        if len(pending) == WINDOW_BYTES:
                            if not enqueue(bytes(pending)):
                                return
                            pending.clear()
                    if pending:
                        if len(pending) % SAMPLE_WIDTH:
                            raise ValueError("PCMが16bitサンプルの途中で終了しました。")
                        enqueue(bytes(pending))
                except BaseException as exc:
                    errors.append(exc)
                finally:
                    enqueue(_END)

            reader = threading.Thread(target=read_pcm, name="desktop-capture-pcm", daemon=True)
            reader.start()
            deadline = time.monotonic() + 30
            while not ready.wait(0.05):
                if process.poll() is not None:
                    raise RuntimeError("収音を開始できませんでした。マイク等の許可を確認してください。")
                if time.monotonic() >= deadline:
                    self.stop()
                    raise RuntimeError("収音の初期化が時間内に完了しませんでした。")
            fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                         stat.S_IRUSR | stat.S_IWUSR)
            output_handle = os.fdopen(fd, "w", encoding="utf-8", newline="\n")
            self._emit(callback, "started", output)
            wav_dir = output.with_suffix("")
            if save_wav:
                wav_dir.mkdir(mode=0o700)
            index = 0
            has_text = False
            recognition_error: BaseException | None = None
            while True:
                item = pcm_queue.get()
                if item is _END:
                    break
                index += 1
                assert isinstance(item, bytes)
                if save_wav:
                    wav_path = wav_dir / f"part-{index:06d}.wav"
                    wav_fd = os.open(wav_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                                     stat.S_IRUSR | stat.S_IWUSR)
                    with os.fdopen(wav_fd, "wb") as wav_file:
                        with wave.open(wav_file, "wb") as wav:
                            wav.setnchannels(1)
                            wav.setsampwidth(SAMPLE_WIDTH)
                            wav.setframerate(SAMPLE_RATE)
                            wav.writeframes(item)
                        wav_file.flush()
                        os.fsync(wav_file.fileno())
                if recognition_error is not None:
                    continue
                try:
                    text = self.recognizer.transcribe(item).strip()
                    if text:
                        output_handle.write(text + "\n")
                        output_handle.flush()
                        os.fsync(output_handle.fileno())
                        has_text = True
                        self._emit(callback, "progress", (index, text))
                except BaseException as exc:
                    recognition_error = exc
                    self.stop()
            reader.join()
            return_code = process.wait(timeout=30)
            err_thread.join(timeout=1)
            if errors:
                raise RuntimeError(f"PCM収音に失敗しました: {errors[0]}")
            if recognition_error is not None:
                raise RuntimeError(f"文字起こしに失敗しました: {recognition_error}")
            if return_code != 0:
                raise RuntimeError(f"収音補助ツールが正常終了しませんでした（終了値 {return_code}）。")
            if not self._stop.is_set():
                raise RuntimeError("収音補助ツールが停止操作前に終了しました。途中保存を残しています。")
            if not has_text:
                raise RuntimeError("文字起こし結果が空です。空のTXTは成功扱いにしません。")
            if save_wav and self._stop.is_set():
                marker_path = wav_dir / "recording-complete.json"
                marker_fd = os.open(marker_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                                    stat.S_IRUSR | stat.S_IWUSR)
                with os.fdopen(marker_fd, "w", encoding="utf-8", newline="\n") as marker:
                    json.dump({"version": 1, "parts": index}, marker, separators=(",", ":"))
                    marker.write("\n")
                    marker.flush()
                    os.fsync(marker.fileno())
            self._emit(callback, "complete", output)
        except BaseException as exc:
            cancel_reader.set()
            self.stop()
            process = self._process
            if process is not None:
                try:
                    process.wait(timeout=12)
                except Exception:
                    pass
            self._emit(callback, "error", (str(exc), output if output.exists() else None))
        finally:
            if output_handle is not None:
                output_handle.close()
            if reader is not None and reader.is_alive():
                reader.join(timeout=2)
            if private_temp is not None:
                private_temp.cleanup()
            self._process = None
            with self._lock:
                self.running = False
            self.capture_lock.release()
            self._emit(callback, "stopped", output)
