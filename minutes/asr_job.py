"""Durable, user-directed persistence for local long audio transcription."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Callable

from . import engine
from .engine import transcribe


def is_complete_recording_folder(source: Path) -> bool:
    """Return whether a directory carries the recorder's valid completion marker."""
    marker = source / "recording-complete.json"
    if not marker.is_file() or marker.is_symlink():
        return False
    try:
        if marker.stat().st_size > 1_024:
            return False
        with marker.open("rb") as handle:
            raw = handle.read(1_025)
        if len(raw) > 1_024:
            return False
        data = json.loads(raw.decode("utf-8"))
        parts = sorted((path for path in source.iterdir() if re.fullmatch(r"part-\d{6}\.wav", path.name)),
                       key=lambda path: path.name)
        return (isinstance(data, dict) and set(data) == {"version", "parts"}
                and type(data["version"]) is int and data["version"] == 1
                and type(data["parts"]) is int and data["parts"] > 0 and data["parts"] == len(parts)
                and [p.name for p in parts] == [f"part-{i:06d}.wav" for i in range(1, len(parts) + 1)])
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _atomic_json(path: Path, data: dict) -> None:
    fd, name = tempfile.mkstemp(prefix=".checkpoint-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(data, handle, ensure_ascii=False, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    except BaseException:
        try:
            os.unlink(name)
        except OSError:
            pass
        raise


def _write_partial(path: Path, segments: list[str]) -> None:
    """Rebuild the human-readable view once per run; shards remain the checkpoint source."""
    payload = "\n".join(text for text in segments if text.strip())
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())


def transcribe_to_text(source: Path, executable: Path, model: Path, language: str,
                       output: Path, *, allow_partial_recording: bool = False,
                       on_progress: Callable[[int, str, Path], None] | None = None) -> str:
    """Transcribe, checkpoint each accepted segment, and resume only an identical job."""
    source, output = Path(source), Path(output)
    partial = output.with_name(output.stem + ".partial" + output.suffix)
    checkpoint = output.with_name(output.stem + ".asr-checkpoint")
    if output.exists() or output.is_symlink():
        raise ValueError("出力先は既に存在します。新しい名前を指定してください。")
    if source.is_dir() and not is_complete_recording_folder(source) and not allow_partial_recording:
        raise ValueError("録音完了マーカーがないフォルダです。分割録音だけを処理する場合は明示確認が必要です。")

    # The real adapter validates every WAV before checkpoint creation. Keep the
    # historical, patched adapter contract usable by UI/CLI tests.
    real_adapter = transcribe is engine.transcribe
    if real_adapter:
        parts = engine._input_parts(source)
        for part in parts:
            engine._validate_wav(part)
    elif source.is_dir():
        parts = sorted((path for path in source.iterdir() if re.fullmatch(r"part-\d{6}\.wav", path.name)),
                       key=lambda path: path.name)
    else:
        parts = [source] if source.exists() else []
    executable, model = Path(executable), Path(model)
    if real_adapter and (not executable.is_absolute() or executable.name.lower() not in {"whisper-cli", "whisper-cli.exe"}):
        raise ValueError("信頼できる配布元から取得した whisper-cli の実行ファイルを選んでください。")
    if real_adapter and (not executable.is_file() or not model.is_file()):
        raise ValueError("whisper-cli またはモデルが見つかりません。")
    def file_identity(path: Path) -> dict:
        try:
            stat = path.stat()
            return {"path": str(path.resolve()), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns,
                    "sha256": _hash_file(path)}
        except OSError:
            return {"path": str(path.absolute()), "size": None, "mtime_ns": None}
    identity = {
        "version": 1,
        "sources": [{"path": str(path.resolve()), "size": path.stat().st_size, "sha256": _hash_file(path)} for path in parts],
        "executable": file_identity(executable),
        "model": file_identity(model),
        "language": language,
        "segment_seconds": engine.ASR_SEGMENT_SECONDS,
    }
    if not re.fullmatch(r"[a-z]{2,3}|auto", language):
        raise ValueError("文字起こしの言語指定を確認してください。")

    manifest_path = checkpoint / "manifest.json"
    resumed: dict[int, str] = {}
    if checkpoint.exists() or checkpoint.is_symlink():
        if checkpoint.is_symlink() or not checkpoint.is_dir() or not manifest_path.is_file() or manifest_path.is_symlink():
            raise ValueError(f"途中保存データの形式を確認できません。変更しません: {checkpoint}")
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            raise ValueError(f"途中保存データを読み込めません。変更しません: {checkpoint}") from None
        if (not isinstance(manifest, dict) or manifest.get("identity") != identity
                or manifest.get("state") not in {"partial", "complete"}
                or not isinstance(manifest.get("parts"), list)):
            raise ValueError(f"入力・認識器・モデル・言語が前回と異なるため再開できません。変更しません: {checkpoint}")
        for expected_index, record in enumerate(manifest["parts"], 1):
            if (not isinstance(record, dict) or type(record.get("index")) is not int
                    or record["index"] != expected_index or not isinstance(record.get("file"), str)
                    or not re.fullmatch(r"unit-\d{6}(?:-[a-f0-9]{8})?\.json", record["file"])):
                raise ValueError(f"途中保存データの形式を確認できません。変更しません: {checkpoint}")
            unit = checkpoint / record["file"]
            if unit.is_symlink() or not unit.is_file():
                raise ValueError(f"途中保存区間がありません。変更しません: {unit}")
            try:
                unit_data = json.loads(unit.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                raise ValueError(f"途中保存区間を読み込めません。変更しません: {unit}") from None
            if (not isinstance(unit_data, dict) or unit_data.get("index") != record["index"]
                    or not isinstance(unit_data.get("text"), str)):
                raise ValueError(f"途中保存区間の形式を確認できません。変更しません: {unit}")
            resumed[record["index"]] = unit_data["text"]
        if manifest["state"] == "complete":
            raise ValueError(f"ASRは完了済みです。出力の保存先を確認してください: {checkpoint}")
        if partial.exists() or partial.is_symlink():
            if partial.is_symlink() or not partial.is_file():
                raise ValueError(f"途中保存先が通常のファイルではありません。変更しません: {partial}")
            expected = "\n".join(text for text in resumed.values() if text.strip()).encode("utf-8")
            try:
                existing = partial.read_bytes()
            except OSError:
                raise ValueError(f"途中保存先を読み込めません。変更しません: {partial}") from None
            if not expected.startswith(existing):
                raise ValueError(f"途中保存先がチェックポイントと一致しません。変更しません: {partial}")
        _write_partial(partial, [resumed[i] for i in sorted(resumed)])
    else:
        if partial.exists() or partial.is_symlink():
            raise ValueError(f"途中保存先が既に存在し、対応する再開データがありません。上書きしません: {partial}")
        checkpoint.mkdir()
        _atomic_json(manifest_path, {"identity": identity, "state": "partial", "parts": []})

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    segments = [resumed[i] for i in sorted(resumed)]
    has_text = any(text.strip() for text in segments)

    def checkpoint_part(index: int, text: str) -> None:
        nonlocal has_text, segments, manifest
        if index in resumed:
            return
        unit_name = f"unit-{index:06d}.json"
        unit_path = checkpoint / unit_name
        if unit_path.exists() or unit_path.is_symlink():
            unit_name = f"unit-{index:06d}-{os.urandom(4).hex()}.json"
            # Orphaned units are retained; the committed manifest is authoritative.
            unit_path = checkpoint / unit_name
        _atomic_json(unit_path, {"index": index, "text": text})
        manifest["parts"].append({"index": index, "file": unit_name})
        manifest["parts"].sort(key=lambda item: item["index"])
        _atomic_json(manifest_path, manifest)
        segments.append(text)
        if text.strip():
            if has_text:
                with partial.open("a", encoding="utf-8", newline="\n") as handle:
                    handle.write("\n" + text)
                    handle.flush()
                    os.fsync(handle.fileno())
            else:
                mode = "a" if partial.exists() else "x"
                with partial.open(mode, encoding="utf-8", newline="\n") as handle:
                    handle.write(text)
                    handle.flush()
                    os.fsync(handle.fileno())
            has_text = True
        elif not partial.exists():
            partial.touch(exist_ok=False)
        if on_progress is not None:
            on_progress(index, text, partial)

    try:
        if resumed and real_adapter:
            transcript = transcribe(source, executable, model, language, on_part=checkpoint_part,
                                    resume_parts=resumed)
        else:
            transcript = transcribe(source, executable, model, language, on_part=checkpoint_part)
        payload = transcript.encode("utf-8")
        with output.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        if output.read_bytes() != payload:
            raise RuntimeError(f"保存ファイルの読み戻しに失敗しました: {output}")
        manifest["state"] = "complete"
        _atomic_json(manifest_path, manifest)
    except Exception as exc:
        if partial.exists() or partial.is_symlink():
            raise RuntimeError(f"{exc}\n完了区間の途中保存: {partial}") from exc
        raise
    return transcript
