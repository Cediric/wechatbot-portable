"""Synthetic metadata only; collecting must never disclose config or log text."""
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[1] / "portable" / "collect_diagnostics.py"


def load():
    spec = importlib.util.spec_from_file_location("test_collector", SOURCE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ReadonlyDiagnosticsTests(unittest.TestCase):
    def test_native_metadata_is_strictly_whitelisted(self):
        module = load()
        value = module.safe_build({"file_version": "4.1.15.50", "sha256": "A" * 64,
            "module_size": 123, "architecture": "AMD64", "dll_path": "PRIVATE_PATH",
            "command": "PRIVATE_ARGV", "account": "PRIVATE_ACCOUNT"})
        self.assertEqual(value, {"file_version": "4.1.15.50", "sha256": "a" * 64, "module_size": 123, "architecture": "AMD64"})

    def test_unknown_metadata_cannot_smuggle_private_text(self):
        module = load()
        self.assertEqual(module.safe_build({"file_version": "private-canary", "sha256": "private-canary",
            "module_size": True, "architecture": "private-canary"}),
            {"file_version": "", "sha256": "", "module_size": None, "architecture": "unknown"})

    def test_invalid_native_version_is_not_trusted(self):
        module = load()
        for value in (None, [], {"file_version": "65536.1.1.1"}, {"file_version": "４.１.１５.５０"},
                      {"architecture": []}, {"architecture": {"PRIVATE": "CANARY"}}):
            self.assertEqual(module.safe_build(value)["file_version"], "")
            self.assertEqual(module.safe_build(value)["architecture"], "unknown")

    def test_listener_is_reported_separately_from_unproven_ownership(self):
        module = load()
        def unsafe_ownership():
            raise ValueError("PRIVATE_ACCOUNT_PATH")
        control = SimpleNamespace(_listener_pid=lambda port: 123, owned_running=unsafe_ownership)
        with patch.object(module.os, "name", "nt"), patch.object(module, "bridge_module", return_value=control):
            result = module.service_port("bridge", 8766)
        self.assertEqual(result["status"], "listening")
        self.assertIsNone(result["owned_by_this_package"])
        self.assertNotIn("PRIVATE_ACCOUNT_PATH", json.dumps(result))

    def test_no_unique_listener_does_not_query_or_mutate_ownership(self):
        module = load()
        def unavailable(port):
            raise ValueError("PRIVATE_MESSAGE")
        def forbidden():
            self.fail("must not query ownership without a listener")
        control = SimpleNamespace(_listener_pid=unavailable, owned_running=forbidden)
        with patch.object(module.os, "name", "nt"), patch.object(module, "bridge_module", return_value=control):
            result = module.service_port("bridge", 8766)
        self.assertEqual(result["status"], "no_unique_listener")
        self.assertNotIn("PRIVATE_MESSAGE", json.dumps(result))

    def test_recovery_metadata_uses_selected_profile_instead_of_legacy_default(self):
        module = load()
        profile = {"file_version": "4.1.15.50", "sha256": "b" * 64,
                   "size_of_image": 204587008, "architecture": "AMD64", "dll_path": "PRIVATE_PATH"}
        class SelectionError(ValueError):
            code = "version_mismatch"
        profiles = SimpleNamespace(supported_builds=lambda: [profile], ProfileSelectionError=SelectionError,
                                   select_profile=lambda identity: SimpleNamespace(metadata=lambda: profile))
        with patch.object(module, "bridge_module", return_value=profiles):
            result = module.recovery_match({})
        self.assertEqual(result["selected_recovery_build"]["file_version"], "4.1.15.50")
        self.assertTrue(result["matches_reviewed_recovery_build"])
        self.assertNotIn("PRIVATE_PATH", json.dumps(result))
        def unverified(identity):
            raise SelectionError("PRIVATE_CANARY")
        profiles.select_profile = unverified
        with patch.object(module, "bridge_module", return_value=profiles):
            result = module.recovery_match({})
        self.assertIsNone(result["selected_recovery_build"])
        self.assertEqual(result["build_check"], "version_mismatch")
        self.assertNotIn("PRIVATE_CANARY", json.dumps(result))

    def test_collect_reports_ports_and_file_sizes_without_opening_logs_or_passwords(self):
        module = load()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); bridge = root / "bridge"; runtime = root / "runtime"
            bridge.mkdir(); (runtime / "astrbot-instance/data").mkdir(parents=True)
            (bridge / "config.json").write_text(json.dumps({"web_port": 8766, "main_llm_api_key": "CONFIG_CANARY"}), encoding="utf-8")
            (runtime / "astrbot-instance/data/cmd_config.json").write_text(json.dumps({"dashboard": {"port": 6185, "password": "PASSWORD_CANARY"}}), encoding="utf-8")
            (bridge / "startup_astrbot.log").write_text("CHAT_CANARY API_CANARY", encoding="utf-8")
            (runtime / "startup-error.txt").write_text("PRIVATE_ERROR_CANARY", encoding="utf-8")
            with patch.object(module, "BRIDGE", bridge), patch.object(module, "RUNTIME", runtime), patch.object(module, "wechat_build", return_value={"status": "synthetic"}), patch.object(module, "service_port", side_effect=lambda name, port: {"port": port}) as ports:
                result = module.collect()
            serialized = json.dumps(result)
            for marker in ("CONFIG_CANARY", "PASSWORD_CANARY", "CHAT_CANARY", "API_CANARY", "PRIVATE_ERROR_CANARY", directory):
                self.assertNotIn(marker, serialized)
            self.assertEqual(ports.call_args_list[0].args, ("bridge", 8766))
            self.assertEqual(ports.call_args_list[1].args, ("astrbot", 6185))
            self.assertTrue(result["diagnostic_files"]["wechat-weflow-bridge-ob11/startup_astrbot.log"]["exists"])

    def test_weflow_probe_reports_capability_only_and_never_contacts_custom_addresses(self):
        module = load()
        seen = []
        def probe(url, token, *, open_request):
            self.assertEqual(token, "TOKEN_CANARY")
            self.assertEqual(url, "http://127.0.0.1:5031")
            handlers = open_request.__self__.handlers
            redirects = [h for h in handlers if isinstance(h, module.urllib.request.HTTPRedirectHandler)]
            self.assertEqual(len(redirects), 1)
            self.assertIsNone(redirects[0].redirect_request(None, None, None, None, None, None))
            seen.append(url)
            return len(seen) == 1
        with patch.dict(sys.modules, {"weflow_setup": SimpleNamespace(api_probe=probe)}):
            result = module.weflow_extension({"access_token": "TOKEN_CANARY"})
            self.assertEqual(result["status"], "verified")
            self.assertNotIn("TOKEN_CANARY", json.dumps(result))
            self.assertEqual(module.weflow_extension({"access_token": "TOKEN_CANARY"})["status"], "not_verified")
            self.assertEqual(module.weflow_extension({"access_token": "TOKEN_CANARY",
                "weflow_base_url": "https://remote.example/"})["status"], "not_checked")
            self.assertEqual(module.weflow_extension({})["status"], "not_checked")
        self.assertEqual(len(seen), 2)

    def test_report_write_does_not_modify_existing_user_files(self):
        module = load()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); sentinel = root / "existing.txt"; sentinel.write_bytes(b"preserved")
            with patch.object(module, "RUNTIME", root), patch.object(module, "collect", return_value={"status": "safe"}), patch("builtins.print"):
                self.assertEqual(module.main(), 0)
            self.assertEqual(sentinel.read_bytes(), b"preserved")
            self.assertEqual(json.loads((root / "只读诊断报告.json").read_text(encoding="utf-8")), {"status": "safe"})
            self.assertFalse((root / "只读诊断报告.tmp").exists())


if __name__ == "__main__":
    unittest.main()
