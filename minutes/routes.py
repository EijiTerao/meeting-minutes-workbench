"""Explicit, local-first provider settings and bounded OpenAI Responses calls."""
from __future__ import annotations

import http.client
import json
import os
import re
import stat
import tempfile
import ntpath
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Stage:
    provider: str
    model: str = ""


@dataclass(frozen=True)
class Settings:
    transcription: str = "import"
    whisper: str = ""
    whisper_model: str = ""
    language: str = "ja"
    correction: Stage = Stage("off")
    generation: Stage = Stage("off")
    verification: Stage = Stage("off")

    def __post_init__(self) -> None:
        _validate_settings(self)


_KEY = re.compile(r"(?i)(sk-[a-z0-9_-]{8,}|api[_ -]?key\s*[:=]|bearer\s+[a-z0-9._-]{8,})")
_MODEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_STAGE_NAMES = ("correction", "generation", "verification")
_CONFIG_KEYS = {"transcription", "whisper", "whisper_model", "language", *_STAGE_NAMES}
_MAX_CONFIG_BYTES = 16_384


def _safe_string(value: Any, label: str, *, empty: bool = False) -> str:
    if not isinstance(value, str) or (not empty and not value.strip()):
        raise ValueError(f"{label} must be a non-empty string")
    if any(ord(c) < 32 or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF for c in value) or _KEY.search(value):
        raise ValueError(f"{label} contains a prohibited value")
    if value.startswith(("http://", "https://")):
        raise ValueError(f"{label} must not be a URL")
    return value


def _validate_settings(s: Settings) -> None:
    if s.transcription not in ("import", "whisper"):
        raise ValueError("transcription must be import or whisper")
    _safe_string(s.language, "language")
    if not re.fullmatch(r"[a-z]{2,3}|auto", s.language):
        raise ValueError("language must be a language code")
    if s.transcription == "whisper":
        executable = _safe_string(s.whisper, "whisper executable path")
        model_path = _safe_string(s.whisper_model, "whisper model path")
        executable_is_windows = ntpath.isabs(executable)
        executable_path = Path(executable).expanduser()
        if not (executable_is_windows or executable_path.is_absolute()):
            raise ValueError("whisper executable path must be absolute")
        basename = ntpath.basename(executable) if executable_is_windows else executable_path.name
        if basename.lower() not in ("whisper-cli", "whisper-cli.exe"):
            raise ValueError("whisper executable path must name whisper-cli")
        model_is_windows = ntpath.isabs(model_path)
        resolved_model = Path(model_path).expanduser()
        if not (model_is_windows or resolved_model.is_absolute()):
            raise ValueError("whisper model path must be absolute")
        # Windows paths cannot be probed as native files when tests run elsewhere.
        if (not model_is_windows or os.name == "nt") and not resolved_model.is_file():
            raise ValueError("whisper model path must point to an existing file")
        if (not executable_is_windows or os.name == "nt") and not executable_path.is_file():
            raise ValueError("whisper executable path must point to an existing file")
    elif s.whisper or s.whisper_model:
        _safe_string(s.whisper, "whisper path", empty=True)
        _safe_string(s.whisper_model, "whisper model", empty=True)
    for name in _STAGE_NAMES:
        stage = getattr(s, name)
        if not isinstance(stage, Stage) or stage.provider not in ("off", "ollama", "openai"):
            raise ValueError(f"invalid {name} provider")
        if stage.provider == "off":
            if stage.model:
                raise ValueError(f"{name} model must be empty when disabled")
        else:
            model = _safe_string(stage.model, f"{name} model")
            if not _MODEL.fullmatch(model):
                raise ValueError(f"invalid {name} model name")


def default_path() -> Path:
    return Path.home() / ".meeting-minutes-workbench" / "settings.json"


def _settings_from_dict(raw: Any) -> Settings:
    if not isinstance(raw, dict) or set(raw) - _CONFIG_KEYS:
        raise ValueError("invalid settings: unknown or malformed fields")
    values = dict(raw)
    for name in _STAGE_NAMES:
        if name in values:
            item = values[name]
            if not isinstance(item, dict) or set(item) - {"provider", "model"} or "provider" not in item:
                raise ValueError(f"invalid {name} stage")
            values[name] = Stage(item["provider"], item.get("model", ""))
    return Settings(**values)


def _pairs_no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate configuration key")
        result[key] = value
    return result


def _reject_surrogates(value: Any) -> None:
    if isinstance(value, str):
        if any(0xD800 <= ord(char) <= 0xDFFF for char in value):
            raise ValueError("settings contain an invalid Unicode value")
    elif isinstance(value, dict):
        for key, child in value.items():
            _reject_surrogates(key)
            _reject_surrogates(child)
    elif isinstance(value, list):
        for child in value:
            _reject_surrogates(child)


def load_settings(path: Path | None = None) -> Settings:
    target = Path(path) if path is not None else default_path()
    try:
        # Do not follow a config symlink; it could redirect a local read unexpectedly.
        if target.is_symlink():
            raise ValueError("settings file must not be a symlink")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        fd = os.open(target, flags)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ValueError("settings file must be a regular file")
            with os.fdopen(fd, "rb") as stream:
                raw_bytes = stream.read(_MAX_CONFIG_BYTES + 1)
            if len(raw_bytes) > _MAX_CONFIG_BYTES:
                raise ValueError("settings file exceeds size limit")
            raw = raw_bytes.decode("utf-8")
        except BaseException:
            # fdopen owns and closes the descriptor after successful construction.
            try:
                os.close(fd)
            except OSError:
                pass
            raise
    except FileNotFoundError:
        return Settings()
    except (OSError, UnicodeError) as exc:
        raise ValueError("cannot read settings file") from exc
    try:
        decoded = json.loads(raw, object_pairs_hook=_pairs_no_duplicates)
        _reject_surrogates(decoded)
        return _settings_from_dict(decoded)
    except (json.JSONDecodeError, TypeError, KeyError, RecursionError, UnicodeError) as exc:
        raise ValueError("settings file is corrupt or invalid") from exc


def save_settings(settings: Settings, path: Path | None = None) -> None:
    if not isinstance(settings, Settings):
        raise ValueError("settings must be a Settings instance")
    _validate_settings(settings)
    is_default_target = path is None
    target = Path(path) if path is not None else default_path()
    directory = target.parent
    try:
        created_directory = not directory.exists()
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if target.is_symlink():
            raise ValueError("settings file must not be a symlink")
        payload = {
            "transcription": settings.transcription, "whisper": settings.whisper,
            "whisper_model": settings.whisper_model, "language": settings.language,
            **{name: {"provider": getattr(settings, name).provider,
                      "model": getattr(settings, name).model} for name in _STAGE_NAMES},
        }
        fd, temporary = tempfile.mkstemp(prefix=".settings-", dir=directory)
        try:
            if hasattr(os, "fchmod"):
                try:
                    os.fchmod(fd, stat.S_IRUSR | stat.S_IWUSR)
                except OSError:
                    pass
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            if target.is_symlink():
                raise ValueError("settings file must not be a symlink")
            os.replace(temporary, target)
            if created_directory and is_default_target:
                try:
                    os.chmod(directory, 0o700)
                except OSError:
                    pass
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
    except ValueError:
        raise
    except OSError as exc:
        raise ValueError("cannot save settings file") from exc


def openai_json(model: str, system: str, data: dict, schema: dict, *,
                authorized: bool = False, max_output: int = 4096) -> dict:
    """Call fixed Responses endpoint; authorization must be explicit at call site."""
    if authorized is not True:
        raise PermissionError("explicit authorization is required")
    model = _safe_string(model, "model")
    if not _MODEL.fullmatch(model):
        raise ValueError("invalid model name")
    if not isinstance(system, str) or not system.strip() or len(system) > 16_384:
        raise ValueError("system must be a bounded non-empty string")
    if any((ord(char) < 32 and char not in "\n\r\t") or ord(char) == 127 or
           0xD800 <= ord(char) <= 0xDFFF for char in system) or _KEY.search(system):
        raise ValueError("system contains a prohibited value")
    if not isinstance(data, dict) or not isinstance(schema, dict):
        raise ValueError("data and schema must be objects")
    if not isinstance(max_output, int) or isinstance(max_output, bool) or not 1 <= max_output <= 16384:
        raise ValueError("max_output must be between 1 and 16384")
    key = os.environ.get("OPENAI_API_KEY", "")
    if not key or any(ord(c) < 32 for c in key):
        raise ValueError("OPENAI_API_KEY is unavailable")
    try:
        body = {
            "model": model, "store": False, "truncation": "disabled",
            "max_output_tokens": max_output,
            "input": [{"role": "system", "content": system},
                      {"role": "user", "content": json.dumps(data, ensure_ascii=False)}],
            "text": {"format": {"type": "json_schema", "name": "result", "strict": True, "schema": schema}},
        }
        body_bytes = json.dumps(body, ensure_ascii=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise ValueError("request data is not valid bounded JSON") from exc
    if len(body_bytes) > 80_000:
        raise ValueError("provider request exceeded size limit")
    connection = http.client.HTTPSConnection("api.openai.com", timeout=120, context=None)
    try:
        connection.request("POST", "/v1/responses", body=body_bytes,
                           headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
        response = connection.getresponse()
        raw = response.read(1_048_577)
        if len(raw) > 1_048_576:
            raise ValueError("provider response exceeded size limit")
        if response.status != 200:
            raise ValueError(f"provider request failed with HTTP {response.status}")
    except (OSError, http.client.HTTPException) as exc:
        raise ValueError("provider request failed") from exc
    finally:
        connection.close()
    try:
        result = json.loads(raw, object_pairs_hook=_pairs_no_duplicates)
    except (json.JSONDecodeError, UnicodeError, RecursionError) as exc:
        raise ValueError("provider returned invalid JSON") from exc
    except ValueError as exc:
        raise ValueError("provider returned invalid JSON") from exc
    if not isinstance(result, dict) or result.get("status") != "completed":
        raise ValueError("provider response was not completed")
    texts: list[str] = []
    output = result.get("output")
    if not isinstance(output, list):
        raise ValueError("provider response output was malformed")
    for item in output:
        if not isinstance(item, dict) or item.get("type") not in ("message", "reasoning"):
            raise ValueError("provider returned an unsupported output item")
        if item["type"] == "reasoning":
            continue
        content_items = item.get("content")
        if not isinstance(content_items, list):
            raise ValueError("provider message content was malformed")
        for content in content_items:
            if not isinstance(content, dict):
                raise ValueError("provider message content was malformed")
            if content.get("type") == "refusal":
                raise ValueError("provider refused the request")
            if content.get("type") == "output_text" and isinstance(content.get("text"), str):
                texts.append(content["text"])
    if len(texts) != 1:
        raise ValueError("provider response must contain exactly one output_text")
    try:
        parsed = json.loads(texts[0])
    except json.JSONDecodeError as exc:
        raise ValueError("provider output was not valid JSON") from exc
    if not isinstance(parsed, dict):
        raise ValueError("provider output must be a JSON object")
    return parsed
