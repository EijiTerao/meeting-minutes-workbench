#!/usr/bin/env python3
"""Build the SwiftUI Mac application from public sources, without using a user profile."""

from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "native/macos/app"
NAME = "Meeting Minutes Workbench"
PRODUCT = "MeetingMinutesWorkbench"
BUNDLE_ID = "org.meetingminutesworkbench.desktop"
HELPERS = (
    ("SystemAudioCapture.swift", "workbench-system-audio"),
    ("TranscribeAudio.swift", "workbench-apple-transcribe"),
)
DENIED_LOCAL_PATHS = re.compile(rb"/(?:Users|home)/[^/\s]+/")
DENIED_SENSITIVE_BYTES = re.compile(
    rb"iCloud~md~obsidian|auth\.json|me_embedding\.npy"
    rb"|(?:sk-(?:proj-)?|ghp_|github_pat_)[A-Za-z0-9_-]{24,}"
)
DENIED_BYTES = re.compile(DENIED_LOCAL_PATHS.pattern + rb"|" + DENIED_SENSITIVE_BYTES.pattern)
MIN_FREE_BYTES = 1024 ** 3


def run(*args: str, capture: bool = False) -> str:
    return subprocess.run(args, cwd=ROOT, check=True, text=True,
                          stdout=subprocess.PIPE if capture else None).stdout or ""


def verify_architecture(path: Path) -> None:
    if path.is_symlink() or not path.is_file() or not os.access(path, os.X_OK):
        raise ValueError(f"{path.name}: expected a built executable")
    if "arm64" not in run("/usr/bin/lipo", "-archs", str(path), capture=True).split():
        raise ValueError(f"{path.name}: Apple Silicon slice required")


def verify_binary(path: Path) -> None:
    verify_architecture(path)
    if DENIED_BYTES.search(path.read_bytes()):
        raise ValueError(f"{path.name}: potential private path or credential; stopped before packaging")


def install_executable(source: Path, target: Path) -> None:
    verify_architecture(source)
    raw = source.read_bytes()
    if DENIED_SENSITIVE_BYTES.search(raw):
        raise ValueError(f"{source.name}: sensitive marker or credential in unmodified build output; stopped before stripping")
    with tempfile.TemporaryDirectory(prefix="workbench-stripped-binary-", dir=target.parent) as temp_dir:
        candidate = Path(temp_dir) / source.name
        shutil.copyfile(source, candidate)
        candidate.chmod(0o755)
        run("/usr/bin/strip", "-S", str(candidate))
        verify_binary(candidate)
        shutil.copyfile(candidate, target)
    target.chmod(0o755)
    run("/usr/bin/codesign", "--force", "--sign", "-", "--timestamp=none", str(target))


def _existing_path(path: Path) -> Path:
    candidate = path.expanduser().absolute()
    while not candidate.exists():
        parent = candidate.parent
        if parent == candidate:
            raise ValueError(f"Could not find an existing volume for {path}.")
        candidate = parent
    return candidate


def check_environment(out: Path | None, scratch: Path) -> list[str]:
    """Read-only preflight. Returns all actionable problems found."""
    problems: list[str] = []
    if sys.platform != "darwin":
        problems.append("Run this build on macOS (Apple Silicon).")
        return problems
    if os.uname().machine != "arm64":
        problems.append("Use an Apple Silicon Mac (arm64), not Intel or Rosetta Python.")
    if sys.version_info < (3, 11):
        problems.append("Python 3.11 or newer is required; run with a newer python3.")
    version = subprocess.run(["/usr/bin/sw_vers", "-productVersion"], check=False,
                             text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if version.returncode or tuple(int(part) for part in version.stdout.strip().split(".")[:2]) < (26, 0):
        problems.append("macOS 26 or newer is required.")
    try:
        swift = run("/usr/bin/xcrun", "--find", "swift", capture=True).strip()
        swiftc = run("/usr/bin/xcrun", "--find", "swiftc", capture=True).strip()
        sdk = run("/usr/bin/xcrun", "--sdk", "macosx", "--show-sdk-path", capture=True).strip()
        sdk_version = run("/usr/bin/xcrun", "--sdk", "macosx", "--show-sdk-version", capture=True).strip()
        sdk_parts = tuple(int(part) for part in sdk_version.split(".")[:2])
        if sdk_parts < (26, 0):
            problems.append(f"Selected macOS SDK is {sdk_version}; select an Xcode SDK 26 or newer.")
        swift_version = run(swift, "--version", capture=True)
        match = re.search(r"Swift version (\d+)\.(\d+)", swift_version)
        if not match or tuple(map(int, match.groups())) < (6, 2):
            problems.append("Swift 6.2 or newer is required by native/macos/app/Package.swift; select Xcode 26 or newer.")
        if not Path(swiftc).is_file() or not Path(sdk).is_dir():
            problems.append("Selected Xcode command-line tools or macOS SDK are missing; install/select Xcode 26.")
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        problems.append(f"Could not use the selected Xcode tools: {error}. Install/select Xcode 26.")
    checked_volumes: set[str] = set()
    for label, path in (("output", out or ROOT / "outputs"), ("Swift scratch", scratch)):
        try:
            existing = _existing_path(path)
            volume = str(existing.resolve())
            if volume not in checked_volumes:
                checked_volumes.add(volume)
                free = shutil.disk_usage(existing).free
                if free < MIN_FREE_BYTES:
                    problems.append(f"{label} volume has {free // 1024**2} MiB free; at least 1024 MiB is required ({existing}).")
        except OSError as error:
            problems.append(f"Could not check {label} volume at {path}: {error}.")
    return problems


def build(out: Path, scratch: Path, *, debug: bool = False, archive: bool = False) -> Path:
    problems = check_environment(out, scratch)
    if problems:
        raise ValueError("\n".join(problems))
    out = out.absolute()
    if out.exists() or out.is_symlink():
        raise ValueError("Choose a new output directory; existing builds are not overwritten.")
    out.parent.mkdir(parents=True, exist_ok=True)
    swift = run("/usr/bin/xcrun", "--find", "swift", capture=True).strip()
    swiftc = run("/usr/bin/xcrun", "--find", "swiftc", capture=True).strip()
    sdk = run("/usr/bin/xcrun", "--sdk", "macosx", "--show-sdk-path", capture=True).strip()
    scr = scratch.absolute()
    prefix = str(ROOT) + "=/workbench"
    mode = "debug" if debug else "release"
    flags = ("--package-path", str(PACKAGE), "--scratch-path", str(scr), "-c", mode,
             "--jobs", "2", "-Xswiftc", "-file-prefix-map", "-Xswiftc", prefix,
             "-Xswiftc", "-debug-prefix-map", "-Xswiftc", prefix)
    run(swift, "build", *flags, "--product", PRODUCT)
    bin_folder = Path(run(swift, "build", *flags, "--show-bin-path", capture=True).strip())
    with tempfile.TemporaryDirectory(prefix="workbench-public-mac-") as staging:
        stage = Path(staging)
        app = stage / f"{NAME}.app"
        contents = app / "Contents"
        macos = contents / "MacOS"
        helpers = contents / "Helpers"
        resources = contents / "Resources"
        macos.mkdir(parents=True)
        helpers.mkdir()
        resources.mkdir()
        install_executable(bin_folder / PRODUCT, macos / PRODUCT)
        for src, name in HELPERS:
            binary = stage / name
            run(swiftc, "-sdk", sdk, "-target", "arm64-apple-macosx26.0",
                "-O", "-parse-as-library", "-file-prefix-map", prefix,
                str(ROOT / "native/macos" / src), "-o", str(binary))
            install_executable(binary, helpers / name)
        shutil.copyfile(ROOT / "LICENSE", resources / "LICENSE")
        info = {
            "CFBundleDevelopmentRegion": "ja", "CFBundleExecutable": PRODUCT,
            "CFBundleIdentifier": BUNDLE_ID, "CFBundleName": NAME, "CFBundleDisplayName": NAME,
            "CFBundlePackageType": "APPL", "CFBundleShortVersionString": "0.1.0",
            "CFBundleVersion": "1", "LSMinimumSystemVersion": "26.0",
            "NSHighResolutionCapable": True,
            "NSMicrophoneUsageDescription": "対面やマイクを有効にした会議の文字起こしに使います。",
            "NSSpeechRecognitionUsageDescription": "会議音声をこのMacで文字起こしします。",
            "NSAudioCaptureUsageDescription": "オンライン会議の再生音を取り込みます。",
        }
        with (contents / "Info.plist").open("xb") as handle:
            plistlib.dump(info, handle)
        run("/usr/bin/codesign", "--force", "--sign", "-", "--timestamp=none", str(app))
        run("/usr/bin/codesign", "--verify", "--deep", "--strict", str(app))
        run(str(macos / PRODUCT), "--self-test")
        run(str(helpers / HELPERS[0][1]), "--self-test")
        out.mkdir(mode=0o700)
        allowed = {
            "Contents/Info.plist", f"Contents/MacOS/{PRODUCT}", "Contents/Resources/LICENSE",
            "Contents/_CodeSignature/CodeResources",
            *(f"Contents/Helpers/{name}" for _, name in HELPERS),
        }
        run("/usr/bin/ditto", str(app), str(out / app.name))
        run("/usr/bin/codesign", "--verify", "--deep", "--strict", str(out / app.name))
        if archive:
            archive_path = out / "Meeting-Minutes-Workbench-macOS-arm64-local-test.zip"
            run("/usr/bin/ditto", "-c", "-k", "--norsrc", "--keepParent", str(app), str(archive_path))
            with zipfile.ZipFile(archive_path) as zipped:
                files = {item.filename for item in zipped.infolist() if not item.is_dir()}
                if files != {f"{app.name}/{name}" for name in allowed}:
                    raise ValueError("Archive contained an unexpected file.")
                if zipped.testzip() is not None:
                    raise ValueError("Archive integrity check failed.")
            with archive_path.open("rb") as source:
                digest = hashlib.file_digest(source, "sha256").hexdigest()
            (out / "SHA256SUMS.txt").write_text(f"{digest}  {archive_path.name}\n", encoding="ascii")
        return out / app.name


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, help="new output directory (required to build)")
    parser.add_argument("--scratch", type=Path, default=ROOT / "outputs/.public-swift-build")
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--archive", action="store_true", help="also create the optional local-test ZIP and SHA256SUMS.txt")
    parser.add_argument("--check", action="store_true", help="read-only check of Mac/Xcode/Python/disk prerequisites")
    args = parser.parse_args()
    try:
        if args.check:
            problems = check_environment(args.out, args.scratch)
            if problems:
                parser.exit(1, "Prerequisite check failed:\n- " + "\n- ".join(problems) + "\n")
            print("Prerequisites ready: Apple Silicon, macOS 26+, Python 3.11+, Swift 6.2+, macOS SDK 26+, and at least 1 GiB free.")
        else:
            if args.out is None:
                parser.error("--out is required unless --check is used.")
            print(build(args.out, args.scratch, debug=args.debug, archive=args.archive))
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"Build stopped: {error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
