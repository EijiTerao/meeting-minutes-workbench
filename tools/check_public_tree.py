"""Reject unexpected files and common sensitive data in the staged Git index."""

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REQUIRED = {
    ".github/workflows/test.yml", ".gitignore", "README.md", "examples/minutes.json", "examples/profile.json",
    "capture/recorder.html", "capture/recorder.js",
    "examples/transcript.txt", "examples/benchmark-transcript.txt", "examples/benchmark-minutes.json",
    "examples/pipeline-config.json", "minutes/__init__.py", "minutes/__main__.py",
    "minutes/core.py", "minutes/engine.py", "minutes/gui.py", "tests/test_core.py",
    "minutes/routes.py", "minutes/workflow.py", "tests/test_engine.py", "tests/test_gui.py",
    "tests/test_routes.py", "tests/test_workflow.py", "tests/test_benchmark.py", "tests/recorder.test.cjs",
    "tools/benchmark_minutes.py", "tools/check_public_tree.py",
}
OPTIONAL = {"LICENSE"}
RULES = {
    "personal-path": re.compile(r"/(?:Users|home)/[^/\s]+/|[A-Za-z]:\\Users\\[^\\]+\\"),
    "private-key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "access-token": re.compile(r"\b(?:sk-(?:proj-)?[A-Za-z0-9_-]{24,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|AKIA[A-Z0-9]{16}|xox[baprs]-[A-Za-z0-9-]{16,})\b"),
    "credential-assignment": re.compile(r"""(?im)^\s*(?:api_key|password|token|secret)\s*[:=]\s*['"][^'"]{12,}['"]"""),
    "email": re.compile(r"\b[A-Za-z0-9._+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
}


def git(*args: str) -> bytes:
    return subprocess.check_output(["git", "-C", str(ROOT), *args], stderr=subprocess.DEVNULL)


def main() -> int:
    entries = git("ls-files", "--stage", "-z").split(b"\x00")[:-1]
    errors = found = 0
    actual = set()
    for entry in entries:
        meta, name = entry.split(b"\t", 1)
        path = name.decode("utf-8", errors="replace")
        mode, blob, stage = meta.split()
        if path not in REQUIRED | OPTIONAL or mode != b"100644" or stage != b"0":
            print("Unexpected staged path or file type.", file=sys.stderr)
            errors += 1
            continue
        actual.add(path)
        try:
            data = git("cat-file", "blob", blob.decode("ascii"))
            if len(data) > 150_000 or b"\x00" in data:
                raise ValueError()
            content = data.decode("utf-8")
        except ValueError:
            print(f"{path}: binary, oversized or invalid UTF-8.", file=sys.stderr)
            errors += 1
            continue
        for rule, pattern in RULES.items():
            for match in pattern.finditer(content):
                line = content.count("\n", 0, match.start()) + 1
                print(f"{path}:{line}: {rule}", file=sys.stderr)
                found += 1
    if REQUIRED - actual:
        print("Required publication files are missing.", file=sys.stderr)
        errors += 1
    if errors or found:
        return 1
    print(f"Checked {len(actual)} allowlisted staged files; no matches for the configured patterns.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except subprocess.CalledProcessError:
        print("Could not read the staged index. Stage only verified source files first.", file=sys.stderr)
        raise SystemExit(1) from None
