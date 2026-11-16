import tempfile
import unittest
import contextlib
import io
import json
from pathlib import Path
from unittest.mock import patch

from tools import benchmark_minutes as bench

ROOT = Path(__file__).resolve().parents[1]


class BenchmarkContractTest(unittest.TestCase):
    def test_long_fixture_has_the_same_operations_beyond_the_first_chunk(self):
        short = (ROOT / "examples/benchmark-transcript.txt").read_text(encoding="utf-8")
        longer = bench.make_long_fixture(short)
        self.assertGreater(len(longer.encode("utf-8")), 28_000)
        self.assertLess(len(longer.encode("utf-8")), 192_000)
        for line in short.splitlines():
            self.assertIn(line, longer)
        self.assertTrue(longer.endswith(short.splitlines()[-1] + "\n"))

    def test_p95_and_median_are_not_fastest_run(self):
        self.assertEqual(bench._series_stats([2, 13, 3, 6, 4]), {
            "runs": 5, "median_seconds": 4.0, "p95_seconds": 13,
        })

    def test_one_folder_is_not_two_independent_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            repeated = str(Path(tmp))
            argv = ["bench", "--candidate", repeated, "--baseline-run", repeated, "--candidate-run", repeated]
            with patch("sys.argv", argv), patch.object(bench, "score", return_value={"ok": True}), \
                    patch.object(bench, "elapsed", return_value=1.0), self.assertRaises(SystemExit) as result:
                bench.main()
            self.assertEqual(result.exception.code, 2)

    def _strict_case(self, mutate=None, *, wrong_primary=False, explicit=False):
        tmp = tempfile.TemporaryDirectory()
        root = Path(tmp.name)
        short = (ROOT / "examples/benchmark-transcript.txt").read_text(encoding="utf-8")
        digest = __import__("hashlib").sha256(short.encode("utf-8")).hexdigest()
        baseline, candidate = [], []
        for group, folders in (("b", baseline), ("c", candidate)):
            for index in range(5):
                folder = root / f"{group}{index}"
                folder.mkdir()
                record = {"seconds": {"workflow": 10.0}, "source_sha256": digest,
                          "generation_provider": "ollama", "generation_model": "model-a"}
                if mutate:
                    mutate(group, index, record)
                (folder / "timing.json").write_text(json.dumps(record), encoding="utf-8")
                folders.append(folder)
        primary = root / "outside"
        primary.mkdir()
        args = ["bench", "--require-parity", "--baseline", str(baseline[0]),
                "--candidate", str(primary if wrong_primary else candidate[0])]
        for folder in baseline:
            args += ["--baseline-run", str(folder)]
        for folder in candidate:
            args += ["--candidate-run", str(folder)]
        if explicit:
            args += ["--candidate-seconds", "10"]
        stdout = io.StringIO()
        stderr = io.StringIO()
        with patch("sys.argv", args), patch.object(bench, "score", return_value={"ok": True}), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            try:
                code = bench.main()
            except SystemExit as exc:
                code = exc.code
        report = json.loads(stdout.getvalue()) if stdout.getvalue() else {}
        return tmp, code, report

    def test_matching_routes_and_hashes_pass_strict_gate(self):
        tmp, code, report = self._strict_case()
        try:
            self.assertEqual(code, 0)
            self.assertEqual(report["benchmark_route"], {"provider": "ollama", "model": "model-a"})
        finally:
            tmp.cleanup()

    def test_mismatched_route_cannot_pass_matching_hashes(self):
        for field, value in (("generation_model", "model-b"), ("generation_provider", "openai")):
            with self.subTest(field=field):
                tmp, code, _ = self._strict_case(
                    lambda group, index, record: record.update({field: value}) if group == "c" and index == 4 else None)
                try:
                    self.assertEqual(code, 2)
                finally:
                    tmp.cleanup()

    def test_missing_or_malformed_route_is_rejected_without_echo(self):
        for change in (lambda record: record.pop("generation_model"),
                       lambda record: record.update(generation_model=" padded "),
                       lambda record: record.update(generation_model="two words"),
                       lambda record: record.update(generation_provider="other")):
            tmp, code, _ = self._strict_case(
                lambda group, index, record: change(record) if group == "c" and index == 0 else None)
            try:
                self.assertEqual(code, 2)
            finally:
                tmp.cleanup()

    def test_strict_gate_rejects_unmeasured_primary_and_explicit_timing(self):
        for options in ({"wrong_primary": True}, {"explicit": True}):
            tmp, code, _ = self._strict_case(**options)
            try:
                self.assertEqual(code, 2)
            finally:
                tmp.cleanup()
