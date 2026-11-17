"""Bounded first-run setup for the public desktop workbench.

Discovery is local and read-only. It never executes a discovered program, reads
credentials, or connects to a provider. Configuration creation is create-only.
"""
from __future__ import annotations

import json
import os
import platform
import shutil
import stat
import tempfile
from pathlib import Path
from typing import Any

from .routes import Settings, Stage, default_path, load_settings


_KNOWN_MODELS = (
    "ggml-tiny.bin", "ggml-base.bin", "ggml-small.bin", "ggml-medium.bin",
    "ggml-large-v3.bin", "ggml-large-v3-turbo.bin",
)
_PROVIDERS = ("off", "ollama")


def _regular_file(path: Path, *, executable: bool = False, min_bytes: int = 0) -> bool:
    try:
        info = path.lstat()
    except OSError:
        return False
    return (stat.S_ISREG(info.st_mode) and info.st_size >= min_bytes
            and (not executable or os.access(path, os.X_OK)))


def _whisper_candidate(explicit: Path | None) -> Path | None:
    if explicit is not None:
        candidate = explicit.expanduser()
    else:
        found = shutil.which("whisper-cli")
        candidate = Path(found) if found else Path()
    if not candidate or candidate.name not in ("whisper-cli", "whisper-cli.exe") or not candidate.is_absolute():
        return None
    # Resolve PATH aliases (common package-manager links) to the concrete binary.
    try:
        resolved = candidate.resolve(strict=True)
    except OSError:
        return None
    if (resolved.name not in ("whisper-cli", "whisper-cli.exe")
            or not _regular_file(resolved, executable=True)):
        return None
    return resolved


def _model_candidate(explicit: Path | None) -> Path | None:
    if explicit is not None:
        candidate = explicit.expanduser()
        if (candidate.name not in _KNOWN_MODELS or not candidate.is_absolute()
                or not _regular_file(candidate, min_bytes=10 * 1024 * 1024)):
            return None
        try:
            return candidate.resolve(strict=True)
        except OSError:
            return None

    # Only inspect these two known, application-owned directories. No recursion.
    roots = (Path(__file__).resolve().parent.parent / "models",
             Path.home() / ".meeting-minutes-workbench" / "models")
    for root in roots:
        try:
            if root.is_symlink() or not root.is_dir():
                continue
            for name in _KNOWN_MODELS:
                candidate = root / name
                if _regular_file(candidate, min_bytes=10 * 1024 * 1024):
                    return candidate.resolve(strict=True)
        except OSError:
            continue
    return None


def inspect_setup(*, settings_path: Path | None = None, whisper: Path | None = None,
                  whisper_model: Path | None = None, provider: str = "off",
                  model: str = "") -> tuple[dict[str, Any], Settings | None]:
    """Return a privacy-minimized report and proposed settings, without writing."""
    target = Path(settings_path) if settings_path is not None else default_path()
    executable = _whisper_candidate(whisper)
    model_path = _model_candidate(whisper_model)
    whisper_available = executable is not None and model_path is not None
    proposed = Settings(
        transcription="whisper" if whisper_available else "import",
        whisper=str(executable) if whisper_available else "",
        whisper_model=str(model_path) if whisper_available else "",
        correction=Stage("off"), generation=Stage("off"), verification=Stage("off"),
    )

    if provider not in _PROVIDERS:
        raise ValueError("setup provider must be off or ollama; paid API providers are not enabled by setup")
    if provider == "off":
        if model:
            raise ValueError("model can only be set with an explicit provider")
    else:
        if not model:
            raise ValueError("an explicit provider requires --model")
        if "cloud" in model.casefold():
            raise ValueError("setup requires an already installed local Ollama model, not a cloud model")
        selected = Stage(provider, model)
        proposed = Settings(proposed.transcription, proposed.whisper, proposed.whisper_model,
                            proposed.language, Stage("off"), selected, Stage("off"))

    present = target.exists() or target.is_symlink()
    state = "missing"
    configured: Settings | None = None
    if present:
        try:
            configured = load_settings(target)
            state = "existing_valid"
        except ValueError:
            state = "existing_invalid"

    report: dict[str, Any] = {
        "status": state,
        "platform": {"os": platform.system(), "architecture": platform.machine()},
        "effective": {
            "transcription": proposed.transcription,
            "transcription_reason": ("existing local whisper-cli and allowlisted model found"
                                     if whisper_available else "audio import; no vetted local recognizer/model pair found"),
            "correction": proposed.correction.provider,
            "generation": proposed.generation.provider,
            "verification": proposed.verification.provider,
        },
        "discovery": {
            "whisper_cli": "available" if executable else "not_found",
            "model": model_path.name if model_path else "not_found",
            "credentials_inspected": False,
            "network_used": False,
            "executed_discovered_programs": False,
        },
        "external_paid_provider_configured": bool(configured and any(
            stage.provider in ("openai", "anthropic")
            for stage in (configured.correction, configured.generation, configured.verification)
        )),
        "settings_path": "explicit" if settings_path is not None else "default",
        "write_performed": False,
    }
    if configured is not None:
        report["effective"] = {
            "transcription": configured.transcription,
            "correction": configured.correction.provider,
            "generation": configured.generation.provider,
            "verification": configured.verification.provider,
        }
    return report, configured if configured is not None else proposed


def _settings_json(settings: Settings) -> bytes:
    payload = {
        "transcription": settings.transcription,
        "whisper": settings.whisper,
        "whisper_model": settings.whisper_model,
        "language": settings.language,
        **{name: {"provider": getattr(settings, name).provider,
                  "model": getattr(settings, name).model}
           for name in ("correction", "generation", "verification")},
    }
    return (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def create_settings(settings: Settings, target: Path) -> Settings:
    """Atomically create a settings file without replacing a concurrent/existing one."""
    target = Path(target)
    if not target.is_absolute():
        target = target.absolute()
    # macOS commonly exposes temporary paths under the fixed system aliases
    # /tmp and /var. Preserve the normal path checks below for every child.
    if platform.system() == "Darwin" and len(target.parts) > 2 and target.parts[1] in {"tmp", "var"}:
        alias = Path("/", target.parts[1])
        canonical = Path("/private", target.parts[1])
        if alias.is_symlink() and alias.resolve(strict=True) == canonical:
            target = canonical.joinpath(*target.parts[2:])
    parent = target.parent
    if target.is_symlink():
        raise ValueError("settings file must not be a symlink")
    if parent.is_symlink():
        raise ValueError("settings directory must not be a symlink")
    try:
        if parent.resolve(strict=False) != parent:
            raise ValueError("settings directory path must not traverse a symlink")
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = parent.lstat()
        if not stat.S_ISDIR(info.st_mode):
            raise ValueError("settings parent must be a directory")
        # mkstemp creates an exclusive private temporary. Hard-link publication is
        # atomic and fails if another process created the target first.
        fd, temporary_name = tempfile.mkstemp(prefix=".settings-", dir=parent)
        temporary = Path(temporary_name)
        try:
            if hasattr(os, "fchmod"):
                os.fchmod(fd, stat.S_IRUSR | stat.S_IWUSR)
            payload = _settings_json(settings)
            with os.fdopen(fd, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            if os.name == "nt":
                # Windows rename fails if target exists, even on volumes that
                # do not support hard links. POSIX rename would overwrite it.
                os.rename(temporary, target)
            else:
                os.link(temporary, target, follow_symlinks=False)
            try:
                dir_fd = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
            except OSError:
                pass
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
    except FileExistsError:
        raise ValueError("settings file appeared during setup; existing file was not changed") from None
    except OSError as exc:
        raise ValueError("cannot create initial settings file") from exc

    # Read back through the normal hardened parser and compare values.
    readback = load_settings(target)
    if readback != settings:
        raise ValueError("settings readback did not match the proposed configuration")
    return readback


def run_setup(*, check_only: bool = False, settings_path: Path | None = None,
              whisper: Path | None = None, whisper_model: Path | None = None,
              provider: str = "off", model: str = "") -> dict[str, Any]:
    report, settings = inspect_setup(settings_path=settings_path, whisper=whisper,
                                     whisper_model=whisper_model, provider=provider, model=model)
    if check_only:
        report["status"] = "check_only" if report["status"] == "missing" else report["status"]
        return report
    if report["status"] == "existing_invalid":
        report["error"] = "existing settings are invalid; file was not overwritten"
        return report
    if report["status"] == "existing_valid":
        report["status"] = "existing_preserved"
        if provider != "off":
            report["warning"] = "existing settings were preserved; the requested provider was not applied"
        if report["external_paid_provider_configured"]:
            report["warning"] = "existing paid-provider settings were preserved; setup did not invoke or change them"
        report["effective"] = {
            "transcription": settings.transcription,
            "correction": settings.correction.provider,
            "generation": settings.generation.provider,
            "verification": settings.verification.provider,
        }
        return report
    target = Path(settings_path) if settings_path is not None else default_path()
    readback = create_settings(settings, target)
    report["status"] = "created"
    report["write_performed"] = True
    report["readback"] = {
        "transcription": readback.transcription,
        "correction": readback.correction.provider,
        "generation": readback.generation.provider,
        "verification": readback.verification.provider,
    }
    return report
