"""Selected WeFlow account synchronization; all probes and lifecycle calls mocked."""
import contextlib
import base64
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import patch

PORTABLE = Path(__file__).resolve().parents[1] / "portable"
sys.path.insert(0, str(PORTABLE))
import portable_bootstrap as boot
import weflow_setup as setup
import weflow_secret as secret
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

REAL_API_PROBE = setup.api_probe
REAL_DISCOVER = setup.discover_executable


class WeFlowAccountTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bridge = self.root / "wechat-weflow-bridge-ob11" / "config.json"
        self.app = self.root / "appdata" / "weflow" / "WeFlow-config.json"
        self.runtime = self.root / "runtime"
        self.current = {"httpApiEnabled": True, "httpApiPort": 5031, "httpApiToken": "synthetic-token",
                        "myWxid": "wxid_selected_native", "windowCloseBehavior": "ask", "user_setting": "keep"}
        self.settings = {"weflow_base_url": "http://127.0.0.1:5031", "access_token": "synthetic-token", "bot_wxid": "", "keep": True}
        self.write(self.bridge, self.settings)
        self.write(self.app, self.current)
        for fixture in (patch.dict(os.environ, {"APPDATA": str(self.root / "appdata")}),
                        patch.object(setup, "api_probe", return_value=True),
                        patch.object(setup, "discover_executable", return_value=None),
                        patch.object(setup, "ps_command", side_effect=AssertionError("No real lifecycle calls"))):
            fixture.start()
            self.addCleanup(fixture.stop)

    @staticmethod
    def write(path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")

    def prepare(self):
        return setup.ensure_weflow(self.bridge, self.runtime)

    def directory_account(self, *, bot="", directory=True):
        raw, canonical = "wxid_robot_ab12", "wxid_robot"
        base = self.root / "selected-xwechat-files"
        base.mkdir(exist_ok=True)
        if directory:
            (base / raw).mkdir(exist_ok=True)
        self.write(self.app, {**self.current, "myWxid": raw, "dbPath": str(base)})
        self.write(self.bridge, {**self.settings, "bot_wxid": bot})
        return raw, canonical

    @staticmethod
    def snapshot_response(names, *, status=200, updates=None, raw=None):
        payload = {"success": True, "version": 1, "fresh": True,
                   "capabilities": {"freshIdentitySnapshot": 1}, "snapshotAt": int(time.time()*1000),
                   "contacts": [{"username": name} for name in names]}
        payload.update(updates or {})
        response = io.BytesIO(raw if raw is not None else json.dumps(payload).encode("utf-8"))
        response.status = status
        return response

    def test_verified_directory_suffix_fills_or_migrates_only_its_raw_bot_id(self):
        for existing in ("", "wxid_robot_ab12"):
            with self.subTest(existing_kind="empty" if not existing else "raw_directory"):
                raw, canonical = self.directory_account(bot=existing)
                app_before = self.app.read_bytes()
                output = io.StringIO()
                with patch.object(setup.urllib.request, "urlopen", return_value=self.snapshot_response([canonical])) as request, \
                        contextlib.redirect_stdout(output):
                    self.assertTrue(self.prepare())
                self.assertEqual(boot.load_json(self.bridge)["bot_wxid"], canonical)
                self.assertEqual(self.app.read_bytes(), app_before)
                request.assert_called_once()
                self.assertNotIn(raw, output.getvalue())
                self.assertNotIn(canonical, output.getvalue())
                self.assertNotIn(self.current["httpApiToken"], output.getvalue())

    def test_true_native_id_ending_in_four_hex_is_preferred_without_stripping(self):
        raw, candidate = self.directory_account(bot="wxid_robot_ab12", directory=False)
        before = self.bridge.read_bytes()
        with patch.object(setup.urllib.request, "urlopen", return_value=self.snapshot_response([raw, candidate])):
            self.assertTrue(self.prepare())
        self.assertEqual(self.bridge.read_bytes(), before)
        self.assertEqual(boot.load_json(self.bridge)["bot_wxid"], raw)

    def test_directory_suffix_missing_or_duplicate_native_candidate_is_not_guessed(self):
        for names in (["wxid_unrelated"], ["wxid_robot", "wxid_robot"], []):
            with self.subTest(identity_count=len(names)):
                raw, canonical = self.directory_account(bot="wxid_robot_ab12")
                before = self.bridge.read_bytes(), self.app.read_bytes()
                output = io.StringIO()
                with patch.object(setup.urllib.request, "urlopen", return_value=self.snapshot_response(names)), \
                        contextlib.redirect_stdout(output):
                    self.assertTrue(self.prepare())
                self.assertEqual((self.bridge.read_bytes(), self.app.read_bytes()), before)
                self.assertIn("新鲜身份快照", output.getvalue())
                self.assertNotIn(raw, output.getvalue())
                self.assertNotIn(canonical, output.getvalue())

    def test_directory_suffix_requires_the_actual_selected_directory(self):
        raw, canonical = self.directory_account(directory=False)
        before = self.bridge.read_bytes()
        with patch.object(setup.urllib.request, "urlopen", return_value=self.snapshot_response([canonical])):
            self.assertTrue(self.prepare())
        self.assertEqual(self.bridge.read_bytes(), before)
        self.write(self.app, {**self.current, "myWxid": raw, "dbPath": "relative-db"})
        with patch.object(setup.urllib.request, "urlopen", return_value=self.snapshot_response([canonical])):
            self.assertTrue(self.prepare())
        self.assertEqual(self.bridge.read_bytes(), before)

    def test_invalid_http_or_stale_identity_snapshot_preserves_directory_bot_id(self):
        cases = [(503, {}), (200, {"fresh": False}), (200, {"success": False}),
                 (200, {"version": True}), (200, {"capabilities": {"freshIdentitySnapshot": True}}),
                 (200, {"snapshotAt": int(time.time()*1000)-60000}), (200, {"contacts": None})]
        for status, updates in cases:
            with self.subTest(status=status, invalid_fields=sorted(updates)):
                raw, canonical = self.directory_account(bot="wxid_robot_ab12")
                before = self.bridge.read_bytes(), self.app.read_bytes()
                with patch.object(setup.urllib.request, "urlopen", return_value=self.snapshot_response([canonical], status=status, updates=updates)):
                    self.assertTrue(self.prepare())
                self.assertEqual((self.bridge.read_bytes(), self.app.read_bytes()), before)
        with patch.object(setup.urllib.request, "urlopen", return_value=self.snapshot_response([], raw=b"not-json")):
            self.assertTrue(self.prepare())
        self.assertEqual((self.bridge.read_bytes(), self.app.read_bytes()), before)

    def test_other_explicit_bot_id_is_preserved_without_identity_request(self):
        self.directory_account(bot="wxid_explicit_native")
        before = self.bridge.read_bytes()
        with patch.object(setup.urllib.request, "urlopen", side_effect=AssertionError("No real HTTP")) as request:
            self.assertTrue(self.prepare())
        self.assertEqual(self.bridge.read_bytes(), before)
        request.assert_not_called()

    def test_unsuffixed_account_keeps_the_verified_legacy_path_without_new_http(self):
        with patch.object(setup.urllib.request, "urlopen", side_effect=AssertionError("No extra HTTP")) as request:
            self.assertTrue(self.prepare())
        self.assertEqual(boot.load_json(self.bridge)["bot_wxid"], self.current["myWxid"])
        request.assert_not_called()

    def test_snapshot_native_fields_cannot_be_replaced_by_alias_or_nickname(self):
        self.directory_account()
        before = self.bridge.read_bytes()
        response = self.snapshot_response([], updates={"contacts": [{"username": "wxid_unrelated", "alias": "wxid_robot", "nickname": "wxid_robot"}]})
        with patch.object(setup.urllib.request, "urlopen", return_value=response):
            self.assertTrue(self.prepare())
        self.assertEqual(self.bridge.read_bytes(), before)

    def encrypted_http_token(self):
        key, nonce = bytes(range(32)), bytes(range(12))
        token = self.current["httpApiToken"]
        envelope = "safe:" + base64.b64encode(b"v10" + nonce + AESGCM(key).encrypt(nonce, token.encode(), None)).decode()
        self.write(self.app.with_name("Local State"),
                   {"os_crypt": {"encrypted_key": base64.b64encode(b"DPAPI" + b"synthetic-wrapped-key").decode()}})
        self.write(self.app, {**self.current, "httpApiToken": envelope})
        return key, envelope

    def test_ready_api_matches_selected_account_against_decrypted_token(self):
        key, envelope = self.encrypted_http_token()
        before = self.app.read_bytes()
        output = io.StringIO()
        with patch.object(secret, "_unprotect_dpapi", return_value=key), contextlib.redirect_stdout(output):
            self.assertTrue(self.prepare())
        self.assertEqual(boot.load_json(self.bridge)["bot_wxid"], self.current["myWxid"])
        self.assertEqual(self.app.read_bytes(), before)
        self.assertNotIn(envelope, output.getvalue())
        self.assertNotIn(self.current["httpApiToken"], output.getvalue())

    def test_legacy_plaintext_spaces_match_the_native_authentication_rule(self):
        token = "  " + self.current["httpApiToken"] + "  "
        self.write(self.bridge, {**self.settings, "access_token": token})
        self.write(self.app, {**self.current, "httpApiToken": token})
        self.assertTrue(self.prepare())
        self.assertEqual(boot.load_json(self.bridge)["bot_wxid"], self.current["myWxid"])
        self.assertEqual(boot.load_json(self.bridge)["access_token"], token)

    def test_encrypted_current_token_probes_plaintext_and_syncs_without_rewriting_weflow(self):
        key, envelope = self.encrypted_http_token()
        self.write(self.bridge, {**self.settings, "access_token": "previous-token"})
        before = self.app.read_bytes()
        with patch.object(secret, "_unprotect_dpapi", return_value=key), \
                patch.object(setup, "api_probe", side_effect=[False, True]) as probe, \
                patch.object(setup, "discover_executable", return_value=self.root / "WeFlow.exe"):
            self.assertTrue(self.prepare())
        self.assertEqual(probe.call_args.args[1], self.current["httpApiToken"])
        self.assertNotEqual(probe.call_args.args[1], envelope)
        self.assertEqual(boot.load_json(self.bridge)["access_token"], self.current["httpApiToken"])
        self.assertEqual(boot.load_json(self.bridge)["bot_wxid"], self.current["myWxid"])
        self.assertEqual(self.app.read_bytes(), before)
        setup.ps_command.assert_not_called()

    def test_encrypted_token_failure_preserves_credentials_and_skips_lifecycle(self):
        key, envelope = self.encrypted_http_token()
        before = self.app.read_bytes(), self.bridge.read_bytes()
        output = io.StringIO()
        with patch.object(secret, "_unprotect_dpapi", side_effect=RuntimeError(self.current["httpApiToken"])), \
                patch.object(setup, "api_probe", return_value=False) as probe, \
                patch.object(setup, "discover_executable", return_value=self.root / "WeFlow.exe"), \
                patch.object(boot, "atomic_json", wraps=boot.atomic_json) as save, contextlib.redirect_stdout(output):
            self.assertFalse(self.prepare())
        self.assertEqual(probe.call_count, 1)
        save.assert_not_called()
        setup.ps_command.assert_not_called()
        self.assertEqual((self.app.read_bytes(), self.bridge.read_bytes()), before)
        self.assertIn("http_token_decrypt_failed", output.getvalue())
        self.assertNotIn(envelope, output.getvalue())
        self.assertNotIn(self.current["httpApiToken"], output.getvalue())

    def test_ciphertext_is_never_probe_credential_or_persisted_as_bridge_token(self):
        key, envelope = self.encrypted_http_token()
        before = self.bridge.read_bytes()
        with patch.object(setup.urllib.request, "urlopen", side_effect=AssertionError("HTTP forbidden")) as request:
            for value in (envelope, "lock:synthetic", ""):
                self.assertFalse(REAL_API_PROBE(self.settings["weflow_base_url"], value))
            request.assert_not_called()
        self.assertFalse(setup._sync_selected_account(self.bridge, self.settings["weflow_base_url"], envelope,
                                                      current={**self.current, "httpApiToken": envelope}, sync_token=True))
        self.assertEqual(self.bridge.read_bytes(), before)

    def test_already_ready_api_fills_blank_id_without_changing_weflow_or_logging_ids(self):
        before = self.app.read_bytes()
        output = io.StringIO()
        with contextlib.redirect_stdout(output), patch.object(boot, "atomic_json", wraps=boot.atomic_json) as save:
            self.assertTrue(self.prepare())
        save.assert_called_once()
        self.assertEqual(save.call_args.args[0], self.bridge)
        self.assertEqual(boot.load_json(self.bridge), {**self.settings, "bot_wxid": self.current["myWxid"]})
        self.assertEqual(self.app.read_bytes(), before)
        self.assertIn("已同步 WeFlow 所选账号编号，请确认微信登录同一账号", output.getvalue())
        self.assertNotIn(self.current["myWxid"], output.getvalue())
        self.assertNotIn(self.current["httpApiToken"], output.getvalue())
        setup.discover_executable.assert_not_called()
        setup.ps_command.assert_not_called()

    def test_current_token_success_saves_token_and_selected_id_together(self):
        self.write(self.bridge, {**self.settings, "access_token": "previous-token"})
        before = self.app.read_bytes()
        with patch.object(setup, "api_probe", side_effect=[False, True]), patch.object(setup, "discover_executable", return_value=self.root / "WeFlow.exe"), patch.object(boot, "atomic_json", wraps=boot.atomic_json) as save:
            self.assertTrue(self.prepare())
        self.assertEqual(save.call_count, 1)
        self.assertEqual(boot.load_json(self.bridge)["access_token"], self.current["httpApiToken"])
        self.assertEqual(boot.load_json(self.bridge)["bot_wxid"], self.current["myWxid"])
        self.assertEqual(self.app.read_bytes(), before)

    def test_post_setup_probe_success_also_fills_selected_account(self):
        exe = self.root / "WeFlow.exe"
        exe.write_bytes(b"synthetic-exe")
        asar = self.root / "resources" / "app.asar"
        asar.parent.mkdir()
        asar.write_bytes(b"already-patched")
        digest = setup.sha
        def fake_digest(raw):
            return {b"synthetic-exe": setup.EXE_SHA, b"already-patched": setup.MODIFIED_ASAR_SHA,
                    b"modified-main": setup.MODIFIED_MAIN_SHA}.get(raw, digest(raw))
        packed = [(0, "dist-electron/main.js", {}, b"modified-main")]
        with patch.object(setup, "api_probe", side_effect=[False, False, True]), patch.object(setup, "discover_executable", return_value=exe), patch.object(setup, "sha", side_effect=fake_digest), patch.object(setup, "unpack_asar", return_value=({}, packed)), patch.object(setup, "ps_command", return_value=types.SimpleNamespace(returncode=0)):
            self.assertTrue(self.prepare())
        self.assertEqual(boot.load_json(self.bridge)["bot_wxid"], self.current["myWxid"])
        self.assertEqual(boot.load_json(self.app), self.current)
        self.assertEqual(asar.read_bytes(), b"already-patched")

    def test_tray_exit_retry_finds_custom_install_and_leaves_preferences_intact(self):
        exe = self.root / "custom" / "WeFlow.exe"
        asar = exe.parent / "resources" / "app.asar"
        asar.parent.mkdir(parents=True)
        exe.write_bytes(b"exe")
        asar.write_bytes(b"stock")
        raw, canonical = self.directory_account(bot="wxid_robot_ab12")
        self.write(self.app, {**boot.load_json(self.app), "windowCloseBehavior":"tray"})
        before = self.app.read_bytes()
        digest = {b"exe":setup.EXE_SHA, b"stock":setup.ORIGINAL_ASAR_SHA, b"main":setup.ORIGINAL_MAIN_SHA}
        response = self.snapshot_response([], raw=json.dumps({"success": True, "count": 1,
            "contacts": [{"username": canonical, "nickname": raw}]}).encode())
        with patch.object(setup.urllib.request, "urlopen", return_value=response) as request, \
                patch.object(setup, "discover_executable", return_value=exe), patch.object(setup,"api_probe",return_value=False), \
                patch.object(setup, "sha", side_effect=digest.__getitem__), patch.object(setup, "unpack_asar", return_value=({},[(0,"dist-electron/main.js",{},b"main")])), \
                patch.object(setup, "patch_asar", return_value=b"new"), patch.object(setup, "ps_command", return_value=types.SimpleNamespace(returncode=10)) as lifecycle:
            self.assertFalse(self.prepare())
            output = io.StringIO()
            lifecycle.return_value = types.SimpleNamespace(returncode=1, stderr="Get-FileHash is unavailable")
            with patch.object(setup, "_contact_usernames", return_value=None), contextlib.redirect_stdout(output):
                self.assertFalse(self.prepare())
            self.assertIn("Get-FileHash is unavailable", output.getvalue())
            self.assertNotIn("进程仍在运行", output.getvalue())
        self.assertIn("/api/v1/contacts?", request.call_args.args[0].full_url)
        self.assertEqual(boot.load_json(self.bridge)["bot_wxid"], canonical)
        self.assertEqual(self.app.read_bytes(), before)
        self.assertEqual(asar.read_bytes(), b"stock")
        self.assertFalse((self.runtime / "weflow-backups").exists())
        with patch.object(setup, "ps_command", return_value=types.SimpleNamespace(stdout="[]")):
            self.assertEqual(REAL_DISCOVER(self.runtime), exe.resolve())

    def test_legacy_ready_identity_extension_is_upgraded_before_media_probe_success(self):
        self.write(self.app, {**self.current, "windowCloseBehavior": "quit"})
        exe=self.root/"WeFlow.exe";exe.write_bytes(b"synthetic-exe")
        asar=self.root/"resources"/"app.asar";asar.parent.mkdir();asar.write_bytes(b"legacy-asar")
        digest=setup.sha
        def fake_digest(raw):
            return {b"synthetic-exe":setup.EXE_SHA,b"legacy-asar":setup.LEGACY_MODIFIED_ASAR_SHA,
                    b"legacy-main":setup.LEGACY_MODIFIED_MAIN_SHA}.get(raw,digest(raw))
        packed=[(0,"dist-electron/main.js",{},b"legacy-main")]
        with patch.object(setup,"api_probe",side_effect=[False,False,True]), patch.object(setup,"discover_executable",return_value=exe), \
                patch.object(setup,"sha",side_effect=fake_digest), patch.object(setup,"unpack_asar",return_value=({},packed)), \
                patch.object(setup,"patch_asar",return_value=b"current-asar") as rebuild, \
                patch.object(setup,"ps_command",return_value=types.SimpleNamespace(returncode=0)) as lifecycle:
            self.assertTrue(self.prepare())
        rebuild.assert_called_once()
        self.assertEqual(rebuild.call_args.args[0],b"legacy-asar")
        self.assertEqual(asar.read_bytes(),b"current-asar")
        self.assertEqual([call.args[0][3] for call in lifecycle.call_args_list],["stop","start"])
        stop = lifecycle.call_args_list[0]
        self.assertEqual(stop.args[0][-2:], ["-WaitSeconds", "35"])
        self.assertGreater(stop.kwargs["timeout"], 35)
        self.assertEqual(next((self.runtime/"weflow-backups").glob("*/app.asar")).read_bytes(),b"legacy-asar")

    def test_manual_account_is_preserved_even_if_entered_during_probe(self):
        for during_probe in (False, True):
            with self.subTest(during_probe=during_probe):
                self.write(self.bridge, {**self.settings, **({} if during_probe else {"bot_wxid": "wxid_manual"})})
                def probe(*args):
                    if during_probe:
                        self.write(self.bridge, {**self.settings, "bot_wxid": "wxid_manual"})
                    return True
                with patch.object(setup, "api_probe", side_effect=probe), patch.object(boot, "atomic_json", wraps=boot.atomic_json) as save:
                    self.assertTrue(self.prepare())
                self.assertEqual(boot.load_json(self.bridge)["bot_wxid"], "wxid_manual")
                save.assert_not_called()

    def test_matching_loopback_port_token_enabled_flag_and_native_private_id_are_required(self):
        cases = [({"weflow_base_url": "http://remote.invalid:5031"}, {}),
                 ({"weflow_base_url": "https://127.0.0.1:5031"}, {}),
                 ({"weflow_base_url": "http://127.0.0.1:5031/proxy"}, {}),
                 ({"weflow_base_url": "http://127.0.0.1:5031?secret=value"}, {}),
                 ({"weflow_base_url": "http://127.0.0.1:5031#other"}, {}),
                 ({"access_token": ""}, {"httpApiToken": ""}),
                 ({}, {"httpApiPort": 9999}), ({}, {"httpApiPort": True}),
                 ({}, {"httpApiToken": "another-token"}), ({}, {"httpApiEnabled": False}),
                 ({}, {"myWxid": "111@chatroom"}), ({}, {"myWxid": "昵称"}),
                 ({}, {"myWxid": "unknown"}), ({}, {"myWxid": "wxid bad"}),
                 ({}, {"myWxid": "filehelper"}), ({}, {"myWxid": "wxid_xxx"}),
                 *[({}, {"myWxid": name}) for name in ("weixin", "newsapp", "medianote", "floatbottle", "qqmail", "qmessage", "tmessage", "qqsync", "gh_synthetic_public")],
                 ({}, {"myWxid": "a" * 257})]
        for bridge_changes, app_changes in cases:
            with self.subTest(bridge=bridge_changes, app=app_changes):
                settings = {**self.settings, **bridge_changes}
                self.write(self.bridge, settings)
                self.write(self.app, {**self.current, **app_changes})
                before = self.app.read_bytes()
                with patch.object(boot, "atomic_json", wraps=boot.atomic_json) as save:
                    self.assertTrue(self.prepare())
                self.assertEqual(boot.load_json(self.bridge), settings)
                self.assertEqual(self.app.read_bytes(), before)
                save.assert_not_called()
        for host in ("localhost", "[::1]"):
            with self.subTest(host=host):
                self.write(self.bridge, {**self.settings, "weflow_base_url": "http://" + host + ":5031"})
                self.write(self.app, {**self.current, "myWxid": "custom_wechat_username"})
                self.assertTrue(self.prepare())
                self.assertEqual(boot.load_json(self.bridge)["bot_wxid"], "custom_wechat_username")

    def test_missing_invalid_or_unselected_config_never_guesses_from_other_fields(self):
        for source in (None, b"invalid JSON", json.dumps({**self.current, "myWxid": "", "nickname": "wxid_fake",
                                                        "ownerUsername": "wxid_fake", "dbPath": "/wxid_fake"}).encode()):
            with self.subTest(source=source):
                self.write(self.bridge, self.settings)
                if source is None:
                    self.app.unlink(missing_ok=True)
                else:
                    self.app.write_bytes(source)
                self.assertTrue(self.prepare())
                self.assertEqual(boot.load_json(self.bridge), self.settings)
                if source is not None:
                    self.assertEqual(self.app.read_bytes(), source)

    def test_unsuccessful_capability_probe_never_synchronizes_account(self):
        before = self.app.read_bytes()
        with patch.object(setup, "api_probe", return_value=False), patch.object(boot, "atomic_json", wraps=boot.atomic_json) as save:
            self.assertFalse(self.prepare())
        save.assert_not_called()
        self.assertEqual(boot.load_json(self.bridge), self.settings)
        self.assertEqual(self.app.read_bytes(), before)

    def test_missing_or_relative_appdata_never_reads_a_cwd_account(self):
        for app_data in ("", "relative-appdata"):
            with self.subTest(app_data=app_data), patch.dict(os.environ, {"APPDATA": app_data}), \
                    patch.object(boot, "load_json", side_effect=lambda path: dict(self.settings) if path == self.bridge else dict(self.current)) as read, \
                    patch.object(boot, "atomic_json") as save:
                self.assertTrue(self.prepare())
                self.assertTrue(all(call.args[0] == self.bridge for call in read.call_args_list))
                save.assert_not_called()

    def test_missing_or_relative_appdata_fallback_never_reads_or_syncs_a_cwd_config(self):
        for app_data in ("", "relative-appdata"):
            with self.subTest(app_data=app_data):
                bait = self.root / app_data / "weflow" / "WeFlow-config.json"
                self.write(bait, self.current)
                previous_cwd = Path.cwd()
                try:
                    os.chdir(self.root)
                    with patch.dict(os.environ, {"APPDATA": app_data}), \
                            patch.object(setup, "api_probe", side_effect=[False, True]) as probe, \
                            patch.object(setup, "discover_executable", return_value=self.root / "WeFlow.exe") as discover, \
                            patch.object(boot, "load_json", wraps=boot.load_json) as read, \
                            patch.object(boot, "atomic_json") as save:
                        self.assertFalse(self.prepare())
                    self.assertEqual(probe.call_count, 1)
                    discover.assert_not_called()
                    self.assertEqual([call.args[0] for call in read.call_args_list], [self.bridge])
                    save.assert_not_called()
                    self.assertEqual(boot.load_json(bait), self.current)
                finally:
                    os.chdir(previous_cwd)


if __name__ == "__main__":
    unittest.main()
