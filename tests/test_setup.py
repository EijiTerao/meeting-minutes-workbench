"""Security and first-run behavior tests for the public desktop setup."""
from __future__ import annotations

import contextlib
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import tkinter as tk
import unittest
from pathlib import Path
from unittest.mock import patch

from minutes import setup
from minutes.__main__ import main
from minutes.routes import Settings, Stage, load_settings


class SetupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        # Canonicalize macOS /tmp aliases: setup deliberately refuses paths that
        # traverse symlinked parent directories.
        self.root = Path(self.temp.name).resolve()
        self.settings_path = self.root / "config" / "settings.json"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _quiet_discovery(self):
        return patch("minutes.setup.shutil.which", return_value=None)

    def _fake_whisper(self) -> tuple[Path, Path]:
        executable = self.root / "whisper-cli"
        executable.write_bytes(b"fixture executable; never run")
        executable.chmod(0o755)
        model = self.root / "ggml-base.bin"
        # Sparse fixture: exercise the size check without allocating 10 MiB.
        with model.open("wb") as stream:
            stream.truncate(10 * 1024 * 1024)
        return executable, model

    def test_check_is_read_only_and_missing_config_is_safe(self) -> None:
        target = self.root / "not-created" / "settings.json"
        with self._quiet_discovery():
            report = setup.run_setup(check_only=True, settings_path=target)

        self.assertEqual(report["status"], "check_only")
        self.assertFalse(report["write_performed"])
        self.assertEqual(report["effective"]["transcription"], "import")
        self.assertEqual(report["effective"]["generation"], "off")
        self.assertEqual(report["effective"]["correction"], "off")
        self.assertEqual(report["effective"]["verification"], "off")
        self.assertEqual(report["discovery"]["credentials_inspected"], False)
        self.assertEqual(report["discovery"]["network_used"], False)
        self.assertEqual(report["discovery"]["executed_discovered_programs"], False)
        self.assertFalse(target.exists())
        self.assertFalse(target.parent.exists())

    def test_apply_creates_private_safe_config_and_reads_it_back(self) -> None:
        with self._quiet_discovery():
            report = setup.run_setup(settings_path=self.settings_path)

        self.assertEqual(report["status"], "created")
        self.assertTrue(report["write_performed"])
        self.assertEqual(report["readback"], {
            "transcription": "import", "correction": "off",
            "generation": "off", "verification": "off",
        })
        self.assertEqual(load_settings(self.settings_path), Settings())
        if os.name != "nt":
            self.assertEqual(self.settings_path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(self.settings_path.parent.stat().st_mode & 0o777, 0o700)

    def test_existing_valid_config_is_byte_preserved_even_when_setup_options_differ(self) -> None:
        self.settings_path.parent.mkdir(parents=True)
        original = (
            '{"transcription":"import","correction":{"provider":"off"},'
            '"generation":{"provider":"ollama","model":"already-selected"},'
            '"verification":{"provider":"off"}}\n'
        ).encode()
        self.settings_path.write_bytes(original)

        with self._quiet_discovery():
            report = setup.run_setup(settings_path=self.settings_path,
                                     provider="ollama", model="different-model")

        self.assertEqual(report["status"], "existing_preserved")
        self.assertFalse(report["write_performed"])
        self.assertEqual(self.settings_path.read_bytes(), original)
        self.assertEqual(load_settings(self.settings_path).generation,
                         Stage("ollama", "already-selected"))

    def test_existing_paid_provider_is_reported_but_never_invoked_or_rewritten(self) -> None:
        self.settings_path.parent.mkdir(parents=True)
        original = (
            '{"transcription":"import","correction":{"provider":"off"},'
            '"generation":{"provider":"openai","model":"example-model"},'
            '"verification":{"provider":"off"}}\n'
        ).encode()
        self.settings_path.write_bytes(original)
        with (
            self._quiet_discovery(),
            patch("socket.create_connection", side_effect=AssertionError("network access")),
            patch("http.client.HTTPSConnection", side_effect=AssertionError("network access")),
        ):
            report = setup.run_setup(settings_path=self.settings_path)

        self.assertTrue(report["external_paid_provider_configured"])
        self.assertEqual(report["status"], "existing_preserved")
        self.assertFalse(report["write_performed"])
        self.assertEqual(self.settings_path.read_bytes(), original)

    def test_malformed_config_is_not_repaired_or_overwritten(self) -> None:
        self.settings_path.parent.mkdir(parents=True)
        original = b'{ definitely not json\n'
        self.settings_path.write_bytes(original)

        with self._quiet_discovery():
            report = setup.run_setup(settings_path=self.settings_path)

        self.assertEqual(report["status"], "existing_invalid")
        self.assertFalse(report["write_performed"])
        self.assertIn("error", report)
        self.assertEqual(self.settings_path.read_bytes(), original)

    def test_cli_check_prints_json_without_creating_settings_and_invalid_cli_exits(self) -> None:
        target = self.root / "cli" / "settings.json"
        stdout = io.StringIO()
        with self._quiet_discovery(), contextlib.redirect_stdout(stdout):
            code = main(["setup", "--check", "--settings", str(target)])
        self.assertEqual(code, 0)
        parsed = json.loads(stdout.getvalue())
        self.assertEqual(parsed["status"], "check_only")
        self.assertFalse(parsed["write_performed"])
        self.assertFalse(target.exists())

        for provider in ("openai", "anthropic"):
            with self.subTest(provider=provider), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    main(["setup", "--settings", str(target), "--provider", provider])
            self.assertNotEqual(raised.exception.code, 0)
            self.assertFalse(target.exists())

    def test_isolated_gui_check_launch_maps_window_reads_back_defaults_and_exits(self) -> None:
        if sys.platform != "darwin" and os.name != "nt" and not os.environ.get("DISPLAY"):
            self.skipTest("a graphical desktop is required for the real Tk launch check")
        target = self.settings_path
        stdout = io.StringIO()
        with self._quiet_discovery(), contextlib.redirect_stdout(stdout):
            code = main(["gui", "--settings", str(target), "--check-launch"])

        self.assertEqual(code, 0)
        receipt = json.loads(stdout.getvalue())
        self.assertEqual(receipt["status"], "launch_check_passed")
        self.assertTrue(receipt["window_mapped"])
        self.assertTrue(receipt["settings_readback"])
        self.assertEqual(receipt["setup_status"], "created")
        self.assertEqual(receipt["ai_stages"], "off")
        self.assertFalse(receipt["audio_started"])
        self.assertFalse(receipt["external_ai_calls"])
        self.assertEqual(load_settings(target), Settings())
        self.assertIsNone(tk._default_root, "check-launch must destroy its Tk root before returning")

    def test_local_ollama_is_explicit_generation_only_and_has_no_network_or_secret_reads(self) -> None:
        executable, model_path = self._fake_whisper()
        # Make accidental use of network/provider clients, subprocess execution,
        # or environment-based credentials fail this test immediately.
        with patch("minutes.setup.os.getenv", side_effect=AssertionError("environment read")), \
             patch("minutes.setup.os.environ.get", side_effect=AssertionError("environment read")), \
             patch("minutes.setup.shutil.which", side_effect=AssertionError("PATH discovery")), \
             patch("socket.create_connection", side_effect=AssertionError("network access")), \
             patch("http.client.HTTPSConnection", side_effect=AssertionError("network access")), \
             patch("subprocess.run", side_effect=AssertionError("process execution")), \
             patch("subprocess.Popen", side_effect=AssertionError("process execution")):
            report, proposed = setup.inspect_setup(
                settings_path=self.settings_path,
                whisper=executable,
                whisper_model=model_path,
                provider="ollama",
                model="qwen3:8b",
            )

        self.assertEqual(proposed.transcription, "whisper")
        self.assertEqual(proposed.generation, Stage("ollama", "qwen3:8b"))
        self.assertEqual(proposed.correction, Stage("off"))
        self.assertEqual(proposed.verification, Stage("off"))
        self.assertFalse(self.settings_path.exists())
        self.assertFalse(report["discovery"]["network_used"])
        self.assertFalse(report["discovery"]["credentials_inspected"])
        self.assertFalse(report["discovery"]["executed_discovered_programs"])

    def test_provider_rejects_cloud_model_and_external_apis_without_writing(self) -> None:
        executable, model_path = self._fake_whisper()
        with (
            patch("minutes.setup.os.getenv", side_effect=AssertionError("environment read")),
            patch("minutes.setup.os.environ.get", side_effect=AssertionError("environment read")),
            patch("minutes.setup.shutil.which", side_effect=AssertionError("PATH discovery")),
            patch("socket.create_connection", side_effect=AssertionError("network access")),
            patch("http.client.HTTPSConnection", side_effect=AssertionError("network access")),
        ):
            for provider in ("openai", "anthropic"):
                with self.subTest(provider=provider):
                    with self.assertRaisesRegex(ValueError, "paid API providers"):
                        setup.run_setup(settings_path=self.settings_path,
                                        whisper=executable, whisper_model=model_path,
                                        provider=provider, model="example-model")
                    self.assertFalse(self.settings_path.exists())
            with self.assertRaisesRegex(ValueError, "local Ollama model"):
                setup.run_setup(settings_path=self.settings_path,
                                whisper=executable, whisper_model=model_path,
                                provider="ollama", model="qwen3-cloud")
            with self.assertRaisesRegex(ValueError, "requires --model"):
                setup.run_setup(settings_path=self.settings_path,
                                whisper=executable, whisper_model=model_path,
                                provider="ollama")
        self.assertFalse(self.settings_path.exists())

    def test_whisper_detection_is_allowlisted_and_falls_back_without_a_pair(self) -> None:
        executable, model_path = self._fake_whisper()
        with patch("minutes.setup.shutil.which", return_value=str(executable)), \
             patch("subprocess.run", side_effect=AssertionError("discovered CLI executed")), \
             patch("subprocess.Popen", side_effect=AssertionError("discovered CLI executed")):
            report, proposed = setup.inspect_setup(settings_path=self.settings_path,
                                                   whisper_model=model_path)
        self.assertEqual(report["discovery"]["whisper_cli"], "available")
        self.assertEqual(report["discovery"]["model"], model_path.name)
        self.assertEqual(proposed.transcription, "whisper")

        bad_executable = self.root / "untrusted-recognizer"
        bad_executable.write_bytes(b"not executed")
        bad_executable.chmod(0o755)
        wrong_model = self.root / "custom.bin"
        with wrong_model.open("wb") as stream:
            stream.truncate(12 * 1024 * 1024)
        with patch("minutes.setup.shutil.which", return_value=None):
            report, fallback = setup.inspect_setup(
                settings_path=self.settings_path,
                whisper=bad_executable,
                whisper_model=wrong_model,
            )
        self.assertEqual(report["discovery"]["whisper_cli"], "not_found")
        self.assertEqual(report["discovery"]["model"], "not_found")
        self.assertEqual(fallback.transcription, "import")
        self.assertEqual(fallback.whisper, "")
        self.assertEqual(fallback.whisper_model, "")

    def test_model_and_binary_symlinks_are_not_treated_as_trusted_files(self) -> None:
        executable, model_path = self._fake_whisper()
        model_alias = self.root / "ggml-base.bin"
        model_path.rename(self.root / "real-model.bin")
        model_alias.symlink_to(self.root / "real-model.bin")
        binary_alias = self.root / "whisper-cli"
        executable.rename(self.root / "real-executable")
        binary_alias.symlink_to(self.root / "real-executable")
        report, proposed = setup.inspect_setup(settings_path=self.settings_path,
                                               whisper=binary_alias, whisper_model=model_alias)
        self.assertEqual(proposed.transcription, "import")
        self.assertEqual(report["discovery"]["whisper_cli"], "not_found")
        self.assertEqual(report["discovery"]["model"], "not_found")

    def test_symlinked_settings_target_and_raced_creation_are_never_replaced(self) -> None:
        outside = self.root / "outside.json"
        outside.write_bytes(b"outside data\n")
        target_link = self.root / "linked-settings.json"
        target_link.symlink_to(outside)
        with self._quiet_discovery():
            report = setup.run_setup(settings_path=target_link)
        self.assertEqual(report["status"], "existing_invalid")
        self.assertEqual(outside.read_bytes(), b"outside data\n")

        raced = self.root / "raced" / "settings.json"
        publish_method = "rename" if os.name == "nt" else "link"
        original_publish = getattr(os, publish_method)

        def create_competing_target(source, destination, *args, **kwargs):
            Path(destination).write_bytes(b"concurrent winner\n")
            return original_publish(source, destination, *args, **kwargs)

        with self.assertRaisesRegex(ValueError, "appeared during setup"):
            with patch.object(setup.os, publish_method, side_effect=create_competing_target):
                setup.create_settings(Settings(), raced)
        self.assertEqual(raced.read_bytes(), b"concurrent winner\n")
        self.assertFalse(any(raced.parent.glob(".settings-*")))


if __name__ == "__main__":
    unittest.main()
