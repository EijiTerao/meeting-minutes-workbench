"""Live rawless transcription through the explicitly selected native capture helper."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
import io
import json
import os
from pathlib import Path
import queue
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
import wave

SAMPLE_RATE = 16_000
SAMPLE_WIDTH = 2
WINDOW_SECONDS = 30
WINDOW_BYTES = SAMPLE_RATE * SAMPLE_WIDTH * WINDOW_SECONDS
QUEUE_WINDOWS = 2
MAX_TEXT_BYTES = 1_000_000
ASR_TIMEOUT_SECONDS = 1_800
STOP_TIMEOUT_SECONDS = 10
_SENTINEL = object()


def _completion_path(output: Path) -> Path:
    return output.with_name(output.name + ".complete.json")


def _minimal_child_env(private_temp: Path) -> dict[str, str]:
    # Do not forward credentials, proxies, or arbitrary dynamic-loader settings.
    return {"PATH": os.defpath, "LANG": "C", "LC_ALL": "C", "TMPDIR": str(private_temp)}


def _create_private_text(path: Path):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    return os.fdopen(fd, "w", encoding="utf-8", newline="\n")


def _publish_completion(path: Path, payload: dict) -> None:
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    fd, name = tempfile.mkstemp(prefix=".live-complete-", dir=path.parent)
    temp_path = Path(name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        # Exclusive publication: never overwrite a marker created by another run.
        os.link(temp_path, path)
    finally:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            # If the hard link succeeded, the authoritative marker is already durable.
            pass


def _pcm_window_as_wav(pcm: bytes) -> bytes:
    if not pcm or len(pcm) % SAMPLE_WIDTH:
        raise ValueError("PCM区間の長さが不正です。")
    stream = io.BytesIO()
    with wave.open(stream, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(SAMPLE_WIDTH)
        wav.setframerate(SAMPLE_RATE)
        wav.writeframes(pcm)
    return stream.getvalue()


def _checked_paths(capture: Path, whisper: Path, model: Path, output: Path) -> None:
    if not capture.is_absolute() or not capture.is_file() or not os.access(capture, os.X_OK):
        raise ValueError("明示したキャプチャ補助ツールの通常ファイルを指定してください。")
    if not whisper.is_absolute() or whisper.name.lower() not in {"whisper-cli", "whisper-cli.exe"}:
        raise ValueError("信頼できる配布元の whisper-cli 実行ファイルを絶対パスで指定してください。")
    if (not whisper.is_file() or not os.access(whisper, os.X_OK)
            or not model.is_absolute() or not model.is_file()):
        raise ValueError("whisper-cli またはローカルモデルが見つかりません。")
    if not output.is_absolute() or not output.parent.is_dir():
        raise ValueError("出力先は存在するフォルダ内の絶対パスにしてください。")
    marker = _completion_path(output)
    if output.exists() or output.is_symlink() or marker.exists() or marker.is_symlink():
        raise ValueError("出力先または完了マーカーが既にあります。別の新しい名前を指定してください。")


def _read_pcm_windows(stream, emit) -> None:
    pending = bytearray()
    while True:
        # Read in bounded blocks; this thread never accumulates the whole session.
        block = stream.read(min(64 * 1024, WINDOW_BYTES - len(pending)))
        if not block:
            break
        pending.extend(block)
        if len(pending) == WINDOW_BYTES:
            emit(bytes(pending))
            pending.clear()
    if pending:
        if len(pending) % SAMPLE_WIDTH:
            raise ValueError("キャプチャPCMが16bitサンプルの途中で終了しました。")
        emit(bytes(pending))


def transcribe_live(capture: Path, whisper: Path, model: Path, output: Path,
                    *, language: str = "ja", app_bundle_id: str | None = None,
                    whole_mac: bool = False, confirm: bool = False) -> Path:
    """Capture an explicit system-audio scope and persist transcript text only."""
    capture, whisper, model, output = map(Path, (capture, whisper, model, output))
    if sys.platform != "darwin":
        raise ValueError("ライブ収音はmacOS上でのみ実行できます。")
    _checked_paths(capture, whisper, model, output)
    if not confirm:
        raise ValueError("ライブ収音には --confirm が必要です。")
    if (whole_mac and app_bundle_id) or (not whole_mac and not app_bundle_id):
        raise ValueError("対象は --app-bundle-id または --whole-mac のどちらか1つにしてください。")
    if app_bundle_id and (len(app_bundle_id) > 255 or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_-]*(?:\.[A-Za-z0-9][A-Za-z0-9_-]*)+", app_bundle_id)):
        raise ValueError("アプリのBundle IDを確認してください。")
    if not re.fullmatch(r"[a-z]{2,3}|auto", language):
        raise ValueError("文字起こしの言語指定を確認してください。")

    marker = _completion_path(output)
    capture_args = [str(capture), "--pcm-stdout", "--confirm"]
    capture_args += ["--whole-mac"] if whole_mac else ["--app-bundle-id", app_bundle_id]
    work_queue: queue.Queue[bytes | object] = queue.Queue(maxsize=QUEUE_WINDOWS)
    reader_errors: list[BaseException] = []
    backlog = threading.Event()
    stopping = threading.Event()
    stop_started: list[float] = []
    forced_stop = threading.Event()
    process_holder: list[subprocess.Popen] = []

    def stop_capture() -> None:
        if stopping.is_set():
            return
        stopping.set()
        stop_started.append(time.monotonic())
        process = process_holder[0] if process_holder else None
        if process is not None:
            try:
                os.killpg(process.pid, signal.SIGINT)
            except OSError:
                if process.poll() is None:
                    try:
                        process.send_signal(signal.SIGINT)
                    except OSError:
                        pass

    def publish(window: bytes) -> None:
        try:
            work_queue.put_nowait(window)
        except queue.Full:
            # Backpressure is never hidden: stop capture, flag the run incomplete,
            # then retain this one bounded window while the consumer catches up.
            backlog.set()
            stop_capture()
            work_queue.put(window)

    try:
        transcript_handle = _create_private_text(output)
    except OSError as exc:
        raise ValueError(f"出力先を新規作成できません: {output}") from exc

    old_sigint = None
    installed_sigint = False
    live_workspace = tempfile.TemporaryDirectory(prefix="minutes-live-asr-")
    child_env = _minimal_child_env(Path(live_workspace.name))
    try:
        try:
            process = subprocess.Popen(capture_args, stdin=subprocess.DEVNULL,
                                       stdout=subprocess.PIPE, stderr=None, shell=False, bufsize=0,
                                       start_new_session=True, env=child_env)
        except OSError as exc:
            raise RuntimeError("キャプチャ補助ツールを起動できませんでした。") from exc
        process_holder.append(process)
        assert process.stdout is not None

        def reader() -> None:
            try:
                _read_pcm_windows(process.stdout, publish)
            except BaseException as exc:
                reader_errors.append(exc)
                stop_capture()
            finally:
                work_queue.put(_SENTINEL)

        reader_thread = threading.Thread(target=reader, name="live-pcm-reader", daemon=True)
        reader_thread.start()
        unit_count = 0
        has_text = False
        recognition_error: BaseException | None = None
        user_stopped = threading.Event()
        if threading.current_thread() is threading.main_thread():
            old_sigint = signal.getsignal(signal.SIGINT)

            def request_stop(_signum, _frame) -> None:
                user_stopped.set()
                stop_capture()

            signal.signal(signal.SIGINT, request_stop)
            installed_sigint = True

        with tempfile.TemporaryDirectory(prefix="minutes-live-asr-", dir=live_workspace.name) as temp_name:
            private_output = Path(temp_name) / "unit"
            while True:
                try:
                    item = work_queue.get(timeout=0.2)
                except queue.Empty:
                    if (stopping.is_set() and stop_started
                            and time.monotonic() - stop_started[0] > STOP_TIMEOUT_SECONDS):
                        forced_stop.set()
                        try:
                            os.killpg(process.pid, signal.SIGKILL)
                        except OSError:
                            if process.poll() is None:
                                process.kill()
                    continue
                if item is _SENTINEL:
                    break
                assert isinstance(item, bytes)
                if recognition_error is not None:
                    continue
                unit_count += 1
                wav_bytes = _pcm_window_as_wav(item)
                text_path = Path(str(private_output) + ".txt")
                try:
                    text_path.unlink(missing_ok=True)
                    subprocess.run(
                        [str(whisper), "-m", str(model), "-f", "-", "-l", language,
                         "-otxt", "-of", str(private_output)],
                        cwd=temp_name, input=wav_bytes, stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL, shell=False, check=True,
                        timeout=ASR_TIMEOUT_SECONDS, start_new_session=True, env=child_env,
                    )
                    if (text_path.is_symlink() or not text_path.is_file()
                            or text_path.stat().st_size > MAX_TEXT_BYTES):
                        raise RuntimeError("whisper-cliのテキスト出力を確認できません。")
                    text = text_path.read_text(encoding="utf-8-sig")
                except (OSError, UnicodeError, subprocess.SubprocessError, ValueError, RuntimeError) as exc:
                    recognition_error = exc
                    stop_capture()
                    continue
                text = text.rstrip("\r\n")
                if text.strip():
                    try:
                        if has_text:
                            transcript_handle.write("\n")
                        transcript_handle.write(text)
                        transcript_handle.flush()
                        os.fsync(transcript_handle.fileno())
                    except OSError as exc:
                        recognition_error = exc
                        stop_capture()
                        continue
                    has_text = True

        reader_thread.join(timeout=10)
        if reader_thread.is_alive():
            stop_capture()
            process.kill()
            reader_thread.join(timeout=5)
            raise RuntimeError("キャプチャ補助ツールが停止せず、安全に収音を完了できませんでした。")
        process.stdout.close()
        try:
            return_code = process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            forced_stop.set()
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except OSError:
                process.kill()
            return_code = process.wait(timeout=5)
        if reader_errors:
            raise RuntimeError(f"キャプチャPCMを読み込めませんでした: {reader_errors[0]}") from reader_errors[0]
        if recognition_error is not None:
            raise RuntimeError("文字起こしに失敗しました。認識済みのテキストは途中保存に残しています。") from recognition_error
        if backlog.is_set():
            raise RuntimeError("文字起こしが収音に追いつかず停止しました。保存済みテキストは残しています。")
        if forced_stop.is_set():
            raise RuntimeError("キャプチャ補助ツールが停止期限を超えたため強制終了しました。")
        if return_code != 0:
            raise RuntimeError(f"キャプチャが正常終了しませんでした（終了コード {return_code}）。")
        if unit_count == 0:
            raise RuntimeError("キャプチャ音声がありません。完了扱いにはしませんでした。")
        if not has_text:
            raise RuntimeError("認識できた発話がありません。完了扱いにはしませんでした。")

        transcript_handle.flush()
        os.fsync(transcript_handle.fileno())
        transcript_handle.close()
        _publish_completion(marker, {"version": 1, "state": "complete", "units": unit_count,
                                     "language": language,
                                     "scope": "whole-mac" if whole_mac else "app-bundle-id",
                                     "stop_requested": user_stopped.is_set(),
                                     "app_bundle_id": app_bundle_id if app_bundle_id else None})
        return output
    finally:
        if installed_sigint:
            signal.signal(signal.SIGINT, old_sigint)
        if not transcript_handle.closed:
            transcript_handle.close()
        if process_holder:
            process = process_holder[0]
            if process.poll() is None:
                stop_capture()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except OSError:
                        process.kill()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        pass
            if process.stdout is not None:
                process.stdout.close()
        live_workspace.cleanup()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Live, rawless system-audio transcription")
    parser.add_argument("--capture", type=Path, required=True)
    parser.add_argument("--whisper", type=Path, required=True)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--language", default="ja")
    scope = parser.add_mutually_exclusive_group(required=True)
    scope.add_argument("--app-bundle-id")
    scope.add_argument("--whole-mac", action="store_true")
    parser.add_argument("--confirm", action="store_true", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = transcribe_live(args.capture, args.whisper, args.model, args.output,
                                 language=args.language, app_bundle_id=args.app_bundle_id,
                                 whole_mac=args.whole_mac, confirm=args.confirm)
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"ライブ文字起こしは未完了です: {exc}", file=sys.stderr)
        return 1
    print(f"文字起こしを保存しました: {result}")
    print(f"完了マーカー: {_completion_path(result)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
