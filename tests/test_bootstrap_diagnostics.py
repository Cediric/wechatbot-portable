"""Startup failure diagnostics: synthetic children only, no installers or APIs."""
from collections import UserDict
import ast
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import types
import unittest
import zipfile
from unittest.mock import patch

PORTABLE = Path(__file__).resolve().parents[1] / "portable"
sys.path.insert(0, str(PORTABLE))
import portable_bootstrap as boot


class DiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="wechatbot-diagnostics-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.patch = patch.object(boot, "RUNTIME", self.root)
        self.patch.start(); self.addCleanup(self.patch.stop)
        self.secrets = patch.object(boot, "_DIAGNOSTIC_SECRETS", set())
        self.secrets.start(); self.addCleanup(self.secrets.stop)
        self.stage = patch.object(boot, "STARTUP_STAGE", "synthetic-stage")
        self.stage.start(); self.addCleanup(self.stage.stop)

    def test_nonzero_preserves_import_reason_and_stage_without_argv(self):
        result = types.SimpleNamespace(returncode=1, stdout="SECRET", stderr='Traceback (most recent call last):\n  File "public.py", line 4, in load\n    raise RuntimeError("source-private-literal")\nModuleNotFoundError: No module named anthropic\n')
        with patch.object(boot.subprocess, "run", return_value=result), self.assertRaises(boot.BootstrapCommandError) as error:
            boot.run_checked(["private-command", "source-private-literal"], capture=True, stage="AstrBot：导入检查")
        text = str(error.exception)
        self.assertIn("AstrBot：导入检查", text)
        self.assertIn("ModuleNotFoundError: No module named anthropic", text)
        for secret in ("SECRET", "source-private-literal", "private-command"): self.assertNotIn(secret, text)

    def test_opaque_output_stays_hidden(self):
        with patch.object(boot.subprocess, "run", return_value=types.SimpleNamespace(returncode=7, stdout="SECRET", stderr="SECRET")), self.assertRaises(RuntimeError) as error:
            boot.run_checked(["exe"], capture=True)
        self.assertNotIn("SECRET", str(error.exception))

    def test_stderr_warning_cannot_hide_stdout_component_exit_and_log_path(self):
        result = types.SimpleNamespace(returncode=1, stdout="阶段：AstrBot：服务启动失败\n原因：子进程退出码 17；等待上限：180 秒；本轮日志：startup_astrbot.log\n", stderr="Warning: optional feature failed token=STDERR_CANARY")
        with patch.object(boot.subprocess, "run", return_value=result), self.assertRaises(boot.BootstrapCommandError) as error:
            boot.run_checked(["private-command"], capture=True, stage="启动本包桥接、AstrBot 与控制面板")
        text = str(error.exception)
        for detail in ("AstrBot：服务启动失败", "退出码 17", "180 秒", "startup_astrbot.log", "optional feature failed"):
            self.assertIn(detail, text)
        self.assertNotIn("STDERR_CANARY", text)

    def test_both_busy_streams_remain_bounded_and_keep_component_summary(self):
        stdout = "阶段：AstrBot：服务启动失败\n原因：子进程退出码 17\n" + "\n".join("RuntimeError: stdout-%d " % i + " x" * 600 for i in range(100))
        stderr = "\n".join("Warning: stderr-%d failed " % i + " y" * 600 for i in range(100))
        result = types.SimpleNamespace(returncode=1, stdout=stdout, stderr=stderr)
        with patch.object(boot.subprocess, "run", return_value=result), self.assertRaises(boot.BootstrapCommandError) as error:
            boot.run_checked(["exe"], capture=True)
        text = error.exception.detail
        for detail in ("AstrBot：服务启动失败", "退出码 17", "stdout-99", "stderr-99"):
            self.assertIn(detail, text)
        self.assertLessEqual(len(text), 12000)
        self.assertLessEqual(len(text.splitlines()), 32)

    def test_timeout_retains_both_partial_streams_in_exception_and_saved_report(self):
        failure = subprocess.TimeoutExpired(["private-command"], 220, output=b"RuntimeError: stdout service failed token=OUT_CANARY", stderr=b"ImportError: stderr module missing password=ERR_CANARY")
        with patch.object(boot.subprocess, "run", side_effect=failure), self.assertRaises(boot.BootstrapCommandError) as error:
            boot.run_checked(["private-command"], timeout=220, capture=True)
        for report in (str(error.exception), boot.save_startup_failure(failure)):
            self.assertIn("stdout service failed", report)
            self.assertIn("stderr module missing", report)
            for canary in ("OUT_CANARY", "ERR_CANARY", "private-command"):
                self.assertNotIn(canary, report)

    def test_native_launcher_child_report_reaches_bootstrap_without_credentials_or_history(self):
        bridge = PORTABLE.parent / "wechat-weflow-bridge-ob11"
        stage = self.root / "isolated-launcher"; stage.mkdir()
        config = {"web_port": 8877, "main_llm_api_key": "NATIVE_CONFIG_CANARY"}
        (stage / "config.json").write_text(json.dumps(config), encoding="utf-8")
        (stage / "startup_bridge.log").write_text("RuntimeError: NATIVE_HISTORICAL_CHAT\n", encoding="utf-8")
        child = self.root / "native-launcher-harness.py"
        child.write_text('''import pathlib,sys
sys.path.insert(0,sys.argv[1])
import startup_launcher as launcher
launcher.BASE=pathlib.Path(sys.argv[2])
script="import sys; print('RuntimeError: synthetic backend unavailable NATIVE_CONFIG_CANARY',flush=True); sys.exit(17)"
process=launcher._spawn([sys.executable,'-X','utf8','-c',script],'startup_bridge.log')
process.wait(timeout=15)
print('Warning: optional feature failed password=NATIVE_STDERR_CANARY',file=sys.stderr,flush=True)
raise SystemExit(0 if launcher.open_when_ready([('微信bot',process,'http://127.0.0.1:8877')],timeout=180) else 1)
''', encoding="utf-8")
        boot.remember_diagnostic_secrets(config)
        with self.assertRaises(boot.BootstrapCommandError) as error:
            boot.run_checked([sys.executable, "-X", "utf8", "-B", child, bridge, stage], cwd=self.root, capture=True, timeout=25, stage="启动本包桥接、AstrBot 与控制面板")
        report = boot.save_startup_failure(error.exception)
        for detail in ("微信bot：服务启动失败", "退出码 17", "180 秒", "synthetic backend unavailable", "optional feature failed", str(stage / "startup_bridge.log")):
            self.assertIn(detail, report)
        for canary in ("NATIVE_CONFIG_CANARY", "NATIVE_STDERR_CANARY", "NATIVE_HISTORICAL_CHAT"):
            self.assertNotIn(canary, report)

    def test_both_launcher_entrypoints_outlive_portable_readiness_deadline(self):
        bridge = self.root / "bridge"; bridge.mkdir()
        instance = self.root / "instance"
        boot.atomic_json(bridge / "config.json", {})
        boot.atomic_json(instance / "data/cmd_config.json", {"dashboard": {"port": 6185}})
        for action in ("open", "restart-astrbot"):
            with patch.object(boot, "BRIDGE", bridge), patch.object(boot, "INSTANCE", instance), \
                 patch.object(boot, "managed_paths", return_value=(Path("uv"), Path("python"))), \
                 patch.object(boot, "ensure_environment", return_value=Path(sys.executable)), \
                 patch.object(boot, "prepare_bridge_config", return_value={}), \
                 patch.object(boot, "prepare_astrbot", return_value={}), \
                 patch.object(boot, "prepare_ffmpeg", return_value=self.root / "bin"), \
                 patch.object(boot, "native_diagnostics", return_value={}), \
                 patch.object(boot, "run_checked", return_value=types.SimpleNamespace(returncode=0)) as run, \
                 contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(boot.main([action]), 0)
            launcher = run.call_args
            self.assertEqual(launcher.kwargs["timeout"], 220)
            self.assertEqual(launcher.args[0][-2], bridge / "startup_launcher.py")

    def test_named_mapping_and_environment_secrets_are_exactly_redacted(self):
        boot.remember_diagnostic_secrets(UserDict({"token": "opaque-map-value"}))
        with patch.dict(os.environ, {"CUSTOM_PASSWORD": "opaque-env-value"}), patch.object(boot.subprocess, "run", return_value=types.SimpleNamespace(returncode=1, stdout="", stderr="RuntimeError: opaque-map-value opaque-env-value")), self.assertRaises(RuntimeError) as error:
            boot.run_checked(["exe"], capture=True)
        self.assertNotIn("opaque-map-value", str(error.exception))
        self.assertNotIn("opaque-env-value", str(error.exception))

    def test_fields_bearer_provider_keys_url_and_config_are_private(self):
        value = 'RuntimeError: api_key="field-value" token=token-value password: pass-value Authorization: Bearer bearer-value sk-synthetic123456 https://u:p@host.test/x?signature=opaque\nValueError: {"main_llm_prompt":"private-role-card","api_key":"private-key"}'
        safe = boot.redact_diagnostic(value)
        for secret in ("field-value", "token-value", "pass-value", "bearer-value", "sk-synthetic123456", "u:p", "opaque", "private-role-card", "private-key"):
            self.assertNotIn(secret, safe)
        self.assertIn("RuntimeError", safe)

    def test_non_bearer_authorization_and_multiline_secret_never_escape_report(self):
        failure = ValueError('Authorization: Token AUTH_CANARY_123\npassword="firstline\nMULTILINE_CANARY_123\nlastline"')
        safe = boot.save_startup_failure(failure)
        self.assertNotIn("AUTH_CANARY_123", safe)
        self.assertNotIn("MULTILINE_CANARY_123", safe)

    def test_escaped_known_values_and_embedded_command_values_stay_hidden(self):
        secret = 'prefix"KNOWN_ESCAPED_CANARY_123'
        boot.remember_diagnostic_secrets({'password': secret})
        value = 'Error: password=' + json.dumps(secret) + '\nError: command ["tool","--token","TOKEN_ARG_CANARY_123"] failed\nError: command tool --password PASSWORD_ARG_CANARY_123 failed'
        safe = boot.redact_diagnostic(value)
        for canary in ('KNOWN_ESCAPED_CANARY_123', 'TOKEN_ARG_CANARY_123', 'PASSWORD_ARG_CANARY_123'):
            self.assertNotIn(canary, safe)

    def test_timeout_does_not_render_command_and_retains_safe_partial_error(self):
        failure = subprocess.TimeoutExpired(["exe", "argv-private"], 35, output=b"SECRET", stderr=b"ImportError: DLL load failed token=opaque-timeout")
        with patch.object(boot.subprocess, "run", side_effect=failure), self.assertRaises(boot.BootstrapCommandError) as error:
            boot.run_checked(["exe", "argv-private"], capture=True, timeout=35, stage="AstrBot：导入检查")
        self.assertEqual(error.exception.kind, "timeout")
        self.assertIn("35", str(error.exception))
        self.assertIn("DLL load failed", str(error.exception))
        self.assertNotIn("argv-private", str(error.exception))
        self.assertNotIn("opaque-timeout", str(error.exception))

    def test_successful_non_capture_output_is_filtered_and_capture_keeps_json_in_memory(self):
        result = types.SimpleNamespace(returncode=0, stdout='阶段：synthetic-ready\nSECRET\n{"api_key":"private"}', stderr="")
        output = io.StringIO()
        with patch.object(boot.subprocess, "run", return_value=result), contextlib.redirect_stdout(output):
            self.assertIs(boot.run_checked(["exe"]), result)
            self.assertIs(boot.run_checked(["exe"], capture=True), result)
        self.assertIn("synthetic-ready", output.getvalue())
        self.assertNotIn("SECRET", output.getvalue())
        self.assertNotIn("private", output.getvalue())

    def test_entrypoint_writes_locations_without_source_and_preserves_user_files(self):
        (self.root / "config.json").write_bytes(b"user-config")
        (self.root / "memory.db").write_bytes(b"user-memory")
        def fail(_):
            boot.startup_stage("AstrBot：初始化本包实例")
            boot.remember_diagnostic_secrets({"password": "opaque-password"})
            raise ValueError("init failed opaque-password")
        output = io.StringIO()
        with patch.object(boot, "main", side_effect=fail), contextlib.redirect_stdout(output):
            self.assertEqual(boot.bootstrap_entrypoint(["check"]), 1)
        report = (self.root / "startup-error.txt").read_text(encoding="utf-8")
        self.assertIn("AstrBot：初始化本包实例", report)
        self.assertIn("ValueError", report)
        self.assertIn("调用位置", report)
        self.assertNotIn("opaque-password", report + output.getvalue())
        self.assertNotIn('raise ValueError(', report)
        self.assertEqual((self.root / "config.json").read_bytes(), b"user-config")
        self.assertEqual((self.root / "memory.db").read_bytes(), b"user-memory")

    def test_report_write_failure_raw_timeout_never_prints_argv(self):
        failure = subprocess.TimeoutExpired(['tool', 'RAW_ARGV_CANARY'], 8, stderr=b'ImportError: synthetic partial')
        output = io.StringIO()
        with patch.object(boot, 'main', side_effect=failure), patch.object(boot, 'save_startup_failure', side_effect=PermissionError('report denied')), contextlib.redirect_stdout(output):
            self.assertEqual(boot.bootstrap_entrypoint(['check']), 1)
        self.assertIn('8', output.getvalue())
        self.assertIn('synthetic partial', output.getvalue())
        self.assertNotIn('RAW_ARGV_CANARY', output.getvalue())

    def environment_fixture(self):
        target = self.root / "astrbot-venv"
        (target / "Scripts").mkdir(parents=True)
        (target / "Scripts/python.exe").write_bytes(b"synthetic")
        lock = self.root / "requirements-astrbot.lock"
        lock.write_text("synthetic-lock")
        return target

    def test_smoke_timeout_never_triggers_dependency_reinstall(self):
        self.environment_fixture()
        calls = []
        def run(command, **kwargs):
            calls.append(command)
            if "-c" in command: raise boot.BootstrapCommandError("AstrBot：导入检查", "timeout", kind="timeout")
        with patch.object(boot, "HERE", self.root), patch.object(boot, "install_long_path_hook"), patch.object(boot, "run_checked", side_effect=run), self.assertRaises(boot.BootstrapCommandError):
            boot.ensure_environment(Path("uv"), Path("python"), "astrbot")
        self.assertFalse(any("--reinstall" in command for command in calls))

    def test_verified_astrbot_restart_avoids_sdk_import_and_explicit_check_stays_full(self):
        target = self.environment_fixture()
        with patch.object(boot, "HERE", self.root), patch.object(boot, "install_long_path_hook"), \
             patch.object(boot, "sync_environment_dependencies") as sync, \
             patch.object(boot, "run_checked") as run, contextlib.redirect_stdout(io.StringIO()):
            boot.ensure_environment(Path("uv"), Path("python"), "astrbot")
            self.assertIn("import astrbot,astrbot.cli,astrbot.api,anthropic", run.call_args.args[0][-1])
            run.reset_mock(); sync.reset_mock()
            boot.ensure_environment(Path("uv"), Path("python"), "astrbot")
            sync.assert_not_called()
            run.assert_called_once()
            quick = run.call_args.args[0][-1]
            self.assertIn("find_spec('astrbot')", quick)
            self.assertNotIn("import astrbot", quick)
            self.assertIn("AES.new", quick)
            self.assertEqual(run.call_args.kwargs["timeout"], 30)
            run.reset_mock()
            boot.ensure_environment(Path("uv"), Path("python"), "astrbot", full_check=True)
            self.assertIn("import astrbot,astrbot.cli,astrbot.api,anthropic", run.call_args.args[0][-1])
        self.assertTrue((target / ".wechatbot-import-check.json").is_file())

    def test_changed_lock_invalidates_successful_astrbot_smoke(self):
        self.environment_fixture()
        with patch.object(boot, "HERE", self.root), patch.object(boot, "install_long_path_hook"), \
             patch.object(boot, "sync_environment_dependencies") as sync, \
             patch.object(boot, "run_checked") as run, contextlib.redirect_stdout(io.StringIO()):
            boot.ensure_environment(Path("uv"), Path("python"), "astrbot")
            (self.root / "requirements-astrbot.lock").write_text("changed-lock")
            sync.reset_mock(); run.reset_mock()
            boot.ensure_environment(Path("uv"), Path("python"), "astrbot")
            sync.assert_called_once()
            self.assertIn("import astrbot,astrbot.cli,astrbot.api,anthropic", run.call_args.args[0][-1])

    def test_damaged_success_cache_runs_full_check_and_replaces_cache(self):
        target = self.environment_fixture()
        with patch.object(boot, "HERE", self.root), patch.object(boot, "install_long_path_hook"), \
             patch.object(boot, "sync_environment_dependencies") as sync, \
             patch.object(boot, "run_checked") as run, contextlib.redirect_stdout(io.StringIO()):
            boot.ensure_environment(Path("uv"), Path("python"), "astrbot")
            marker = target / ".wechatbot-import-check.json"
            marker.write_text('{"environment":')
            sync.reset_mock(); run.reset_mock()
            boot.ensure_environment(Path("uv"), Path("python"), "astrbot")
            sync.assert_not_called()
            run.assert_called_once()
            self.assertIn("import astrbot,astrbot.cli,astrbot.api,anthropic", run.call_args.args[0][-1])
            self.assertIn("probe_sha256", boot.load_json(marker))

    def test_quick_probe_failure_uses_full_probe_before_reinstall(self):
        self.environment_fixture()
        with patch.object(boot, "HERE", self.root), patch.object(boot, "install_long_path_hook"), \
             patch.object(boot, "sync_environment_dependencies") as sync, \
             patch.object(boot, "run_checked") as run, contextlib.redirect_stdout(io.StringIO()):
            boot.ensure_environment(Path("uv"), Path("python"), "astrbot")
            sync.reset_mock(); run.reset_mock()
            run.side_effect = [boot.BootstrapCommandError("quick", "synthetic missing", kind="exit"), None]
            boot.ensure_environment(Path("uv"), Path("python"), "astrbot")
            sync.assert_not_called()
            self.assertEqual(run.call_count, 2)
            self.assertIn("import astrbot,astrbot.cli,astrbot.api,anthropic", run.call_args.args[0][-1])

    def test_failed_full_probe_invalidates_previous_success(self):
        target = self.environment_fixture()
        with patch.object(boot, "HERE", self.root), patch.object(boot, "install_long_path_hook"), \
             patch.object(boot, "sync_environment_dependencies"), \
             patch.object(boot, "run_checked") as run, contextlib.redirect_stdout(io.StringIO()):
            boot.ensure_environment(Path("uv"), Path("python"), "astrbot")
            run.side_effect = boot.BootstrapCommandError("full", "synthetic timeout", kind="timeout")
            with self.assertRaises(boot.BootstrapCommandError):
                boot.ensure_environment(Path("uv"), Path("python"), "astrbot", full_check=True)
            self.assertFalse((target / ".wechatbot-import-check.json").exists())

    def test_quick_probe_locates_sdk_without_executing_its_initializers(self):
        self.environment_fixture()
        with patch.object(boot, "HERE", self.root), patch.object(boot, "install_long_path_hook"), \
             patch.object(boot, "sync_environment_dependencies"), patch.object(boot, "run_checked") as run, \
             contextlib.redirect_stdout(io.StringIO()):
            boot.ensure_environment(Path("uv"), Path("python"), "astrbot")
            boot.ensure_environment(Path("uv"), Path("python"), "astrbot")
            quick = run.call_args.args[0][-1]
        for name in ("astrbot", "anthropic"):
            folder = self.root / name; folder.mkdir()
            (folder / "__init__.py").write_text("raise RuntimeError('heavy SDK import must not run')")
        for name in ("cli", "api"):
            (self.root / "astrbot" / (name + ".py")).write_text("raise RuntimeError('must not run')")
        (self.root / "_wechatbot_long_paths.py").write_text("def activate(): return True\n")
        wheel = PORTABLE / "vendor/wheels/pycryptodome-3.24.0-cp37-abi3-win_amd64.whl"
        with zipfile.ZipFile(wheel) as archive:
            for item in archive.infolist():
                if item.filename.startswith("Crypto/") and not item.is_dir():
                    destination = self.root / item.filename
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_bytes(archive.read(item))
        child = subprocess.run([sys.executable, "-X", "utf8", "-B", "-c", quick],
                               cwd=self.root, stdin=subprocess.DEVNULL, capture_output=True, timeout=15,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.assertEqual(child.returncode, 0, child.stderr.decode(errors="replace"))

    def test_astrbot_import_check_does_not_touch_host_or_formal_instance(self):
        self.environment_fixture()
        hostile = self.root / 'hostile-root-file'; hostile.write_bytes(b'host-data')
        formal = self.root / 'formal-instance'
        smokes = []
        def run(command, **kwargs):
            if '-c' in command:
                env = kwargs['env']; smokes.append(env)
                sandbox = Path(env['ASTRBOT_ROOT'])
                self.assertEqual(sandbox, self.root / 'astrbot-import-check')
                self.assertNotEqual(sandbox, formal)
                self.assertNotIn('ASTRBOT_DASHBOARD_INITIAL_PASSWORD', env)
                self.assertNotIn('ASTRBOT_DASHBOARD_RESET_PASSWORD', env)
                self.assertNotIn('ASTRBOT_RESET_DASHBOARD_PASSWORD', env)
                self.assertEqual(kwargs['timeout'], 180)
                boot.atomic_json(sandbox / 'data/cmd_config.json', {'synthetic-import': True})
        with patch.object(boot, 'HERE', self.root), patch.object(boot, 'INSTANCE', formal), patch.object(boot, 'install_long_path_hook'), patch.object(boot, 'run_checked', side_effect=run), patch.dict(os.environ, {'ASTRBOT_ROOT': str(hostile), 'ASTRBOT_DASHBOARD_INITIAL_PASSWORD': 'never-use', 'ASTRBOT_DASHBOARD_RESET_PASSWORD': 'never-reset', 'ASTRBOT_RESET_DASHBOARD_PASSWORD': 'true'}), contextlib.redirect_stdout(io.StringIO()):
            boot.ensure_environment(Path('uv'), Path('python'), 'astrbot')
        self.assertEqual(len(smokes), 1)
        self.assertEqual(hostile.read_bytes(), b'host-data')
        self.assertFalse(formal.exists())

    def captured_astrbot_smoke(self):
        target = self.environment_fixture()
        python = self.root / 'managed-python.exe'
        boot.atomic_json(target / '.wechatbot-environment.json', {
            'root': str(self.root.resolve()), 'python': str(python),
            'lock_sha256': hashlib.sha256((self.root / 'requirements-astrbot.lock').read_bytes()).hexdigest(),
        })
        formal = self.root / 'formal-instance'
        boot.atomic_json(formal / 'data/cmd_config.json', {'password': 'synthetic-preserved'})
        (formal / 'memory.db').write_bytes(b'synthetic-preserved-memory')
        before = {path.relative_to(formal): path.read_bytes() for path in formal.rglob('*') if path.is_file()}
        with patch.object(boot, 'ROOT', self.root), patch.object(boot, 'HERE', self.root), \
             patch.object(boot, 'INSTANCE', formal), patch.object(boot, 'install_long_path_hook'), \
             patch.object(boot, 'sync_environment_dependencies') as sync, \
             patch.object(boot, 'run_checked', return_value=types.SimpleNamespace(returncode=0)) as run, \
             contextlib.redirect_stdout(io.StringIO()):
            boot.ensure_environment(Path('uv'), python, 'astrbot')
        sync.assert_not_called()
        run.assert_called_once()
        command = run.call_args.args[0]
        self.assertEqual(command[:-1], [target / 'Scripts/python.exe', '-X', 'utf8', '-B', '-c'])
        self.assertEqual(run.call_args.kwargs['timeout'], 180)
        self.assertTrue(run.call_args.kwargs['capture'])
        self.assertEqual(Path(run.call_args.kwargs['env']['ASTRBOT_ROOT']), self.root / 'astrbot-import-check')
        self.assertEqual(before, {path.relative_to(formal): path.read_bytes() for path in formal.rglob('*') if path.is_file()})
        return command[-1]

    def test_astrbot_smoke_runs_real_native_aes_and_rejects_bad_encrypt_or_decrypt(self):
        smoke = self.captured_astrbot_smoke()
        wheel = PORTABLE / 'vendor/wheels/pycryptodome-3.24.0-cp37-abi3-win_amd64.whl'
        self.assertEqual(hashlib.sha256(wheel.read_bytes()).hexdigest(),
                         '89a9c14b18f43491d7bec4440c7179eb51a56891c3070e41ae726cb3734c6b9b')
        native = self.root / 'native-crypto'
        with zipfile.ZipFile(wheel) as archive:
            for item in archive.infolist():
                if item.filename.startswith('Crypto/') and not item.is_dir():
                    destination = native / item.filename
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_bytes(archive.read(item))
        # Stub only SDK imports. The actual captured command still imports AES
        # and exercises the verified wheel's native encryption and decryption.
        prefix = '''import sys,types
sys.path.insert(0,sys.argv[1])
for name in ('_wechatbot_long_paths','astrbot','astrbot.cli','astrbot.api','anthropic'):
    sys.modules[name]=types.ModuleType(name)
sys.modules['_wechatbot_long_paths'].activate=lambda:True
from Crypto.Cipher import AES
original=AES.new
class CheckedCipher:
    def __init__(self,cipher): self.cipher=cipher
    def encrypt(self,value):
        return bytes(16) if sys.argv[2]=='encrypt' else self.cipher.encrypt(value)
    def decrypt(self,value):
        return bytes(16) if sys.argv[2]=='decrypt' else self.cipher.decrypt(value)
AES.new=lambda *args,**kwargs:CheckedCipher(original(*args,**kwargs))
exec(sys.argv[3])
'''
        for failure in ('none', 'encrypt', 'decrypt'):
            with self.subTest(failure=failure):
                child = subprocess.run([sys.executable, '-X', 'utf8', '-B', '-c', prefix,
                                        str(native), failure, smoke], cwd=self.root,
                                       stdin=subprocess.DEVNULL, capture_output=True, timeout=30,
                                       creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                self.assertEqual(child.returncode == 0, failure == 'none')
                if failure != 'none':
                    self.assertIn(b'AssertionError', child.stderr)
                self.assertEqual(child.stdout, b'')

    def test_online_environment_uses_only_local_wheels_for_sync_and_reinstall(self):
        self.environment_fixture()
        (self.root / 'vendor/wheels').mkdir(parents=True)
        commands = []; smokes = []
        def run(command, **kwargs):
            commands.append(command)
            if '-c' in command:
                smokes.append(kwargs)
                if len(smokes) == 1: raise boot.BootstrapCommandError('AstrBot：导入检查', 'exit 1', 'ImportError: synthetic missing')
        with patch.object(boot, 'HERE', self.root), patch.object(boot, 'install_long_path_hook'), patch.object(boot, 'run_checked', side_effect=run), patch.dict(os.environ, {}, clear=True), contextlib.redirect_stdout(io.StringIO()):
            self.assertNotIn('WECHATBOT_OFFLINE', os.environ)
            boot.ensure_environment(Path('uv'), Path('python'), 'astrbot')
        syncs = [command for command in commands if 'sync' in command]
        self.assertEqual(len(syncs), 2)
        for command in syncs:
            self.assertIn('--no-build', command)
            self.assertIn('--no-index', command)
            self.assertIn('--require-hashes', command)
            self.assertEqual(command[command.index('--find-links') + 1], self.root / 'vendor/wheels')
        self.assertIn('--reinstall', syncs[1])
        self.assertTrue(all(kwargs['timeout'] == 180 for kwargs in smokes))

    def test_repair_failure_report_keeps_first_real_import_error(self):
        self.environment_fixture()
        def run(command, **kwargs):
            if "-c" in command: raise boot.BootstrapCommandError("AstrBot：导入检查", "exit 1", "ModuleNotFoundError: No module named anthropic")
            if "--reinstall" in command: raise boot.BootstrapCommandError("AstrBot：修复锁定依赖", "exit 2", "error: disk denied")
        with patch.object(boot, "HERE", self.root), patch.object(boot, "install_long_path_hook"), patch.object(boot, "run_checked", side_effect=run), contextlib.redirect_stdout(io.StringIO()):
            try: boot.ensure_environment(Path("uv"), Path("python"), "astrbot")
            except boot.BootstrapCommandError as exc: report = boot.save_startup_failure(exc)
        self.assertIn("No module named anthropic", report)
        self.assertIn("disk denied", report)

    def test_partial_astrbot_init_retry_reuses_persisted_password_and_data(self):
        instance = self.root / "instance"
        instance.mkdir()
        sentinel = instance / "keep.txt"; sentinel.write_bytes(b"user-data")
        passwords = []
        def initialize(command, **kwargs):
            passwords.append(kwargs['env']['ASTRBOT_DASHBOARD_INITIAL_PASSWORD'])
            (instance / '.astrbot').touch()
            if len(passwords) == 1: raise boot.BootstrapCommandError('AstrBot：初始化本包实例', 'exit 1', 'OSError: synthetic init failed')
            boot.atomic_json(instance / 'data/cmd_config.json', {'dashboard': {'password': 'synthetic-hash'}})
        with patch.object(boot, 'INSTANCE', instance), patch.object(boot, 'run_checked', side_effect=initialize), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(RuntimeError): boot.prepare_astrbot(Path('python.exe'), {})
            login_before = (instance / 'dashboard-login.txt').read_bytes()
            boot.prepare_astrbot(Path('python.exe'), {})
        self.assertEqual(len(passwords), 2)
        self.assertEqual(passwords[0], passwords[1])
        self.assertEqual((instance / 'dashboard-login.txt').read_bytes(), login_before)
        self.assertEqual(sentinel.read_bytes(), b'user-data')
        self.assertTrue((instance / '.astrbot').is_file())

    def test_existing_astrbot_config_and_password_never_reinitialize(self):
        instance = self.root / 'existing'; instance.mkdir()
        (instance / '.astrbot').touch()
        boot.atomic_json(instance / 'data/cmd_config.json', {'dashboard': {'password': 'old-hash', 'port': 9000}})
        login = instance / 'dashboard-login.txt'; login.write_bytes(b'old account password file')
        with patch.object(boot, 'INSTANCE', instance), patch.object(boot, 'run_checked') as cli:
            boot.prepare_astrbot(Path('python.exe'), {})
        cli.assert_not_called()
        self.assertEqual(boot.load_json(instance / 'data/cmd_config.json')['dashboard'], {'password': 'old-hash', 'port': 9000})
        self.assertEqual(login.read_bytes(), b'old account password file')

    def test_existing_config_missing_marker_repairs_without_any_password_reset(self):
        instance = self.root / 'missing-marker'; instance.mkdir()
        config = {'dashboard': {'password': 'old-hash', 'port': 9000}}
        boot.atomic_json(instance / 'data/cmd_config.json', config)
        login = instance / 'dashboard-login.txt'; login.write_bytes(b'old-password-file')
        def repair(command, **kwargs):
            self.assertNotIn('ASTRBOT_DASHBOARD_INITIAL_PASSWORD', kwargs['env'])
            self.assertNotIn('ASTRBOT_DASHBOARD_RESET_PASSWORD', kwargs['env'])
            self.assertNotIn('ASTRBOT_RESET_DASHBOARD_PASSWORD', kwargs['env'])
            self.assertEqual(boot.load_json(instance / 'data/cmd_config.json'), config)
            (instance / '.astrbot').touch()
        with patch.object(boot, 'INSTANCE', instance), patch.dict(os.environ, {'ASTRBOT_DASHBOARD_INITIAL_PASSWORD': 'never-use', 'ASTRBOT_DASHBOARD_RESET_PASSWORD': 'never-reset', 'ASTRBOT_RESET_DASHBOARD_PASSWORD': 'true'}), patch.object(boot, 'run_checked', side_effect=repair) as cli, contextlib.redirect_stdout(io.StringIO()):
            boot.prepare_astrbot(Path('python.exe'), {})
        self.assertEqual(cli.call_count, 1)
        self.assertEqual(boot.load_json(instance / 'data/cmd_config.json')['dashboard'], config['dashboard'])
        self.assertEqual(login.read_bytes(), b'old-password-file')

    @unittest.skipUnless(os.name == "nt", "Native Windows PowerShell diagnostic wrapper")
    def test_powershell_preserves_python_cause_report_and_redacts_before_output(self):
        fixture = self.root / "invoke-diagnostics.ps1"
        result_file = self.root / "result.json"
        child = self.root / "synthetic-child.py"
        finder = self.root / "synthetic-find.py"
        finder.write_text('import sys,time\nprint("C:/synthetic/python.exe",flush=True)\ntime.sleep(.1)\nprint("warning: late stderr",file=sys.stderr,flush=True)\n', encoding="utf-8")
        child.write_text('import pathlib,sys\npathlib.Path(sys.argv[1]).write_text("微信bot 启动诊断（已脱敏）\\n阶段：AstrBot：导入检查\\n原因：ModuleNotFoundError: No module named synthetic_dep\\n",encoding="utf-8")\nprint("阶段：AstrBot：导入检查")\nprint("ModuleNotFoundError: No module named synthetic_dep token=private-token sk-synthetic123456",file=sys.stderr)\nsys.exit(1)\n', encoding="utf-8")
        fixture.write_text(r'''param($Source,$Runtime,$Python,$Child,$Finder,$Result)
$ErrorActionPreference='Stop'
$env:PSModulePath=(Join-Path $PSHOME 'Modules')+[IO.Path]::PathSeparator+$env:PSModulePath
$tokens=$null;$errors=$null
$ast=[Management.Automation.Language.Parser]::ParseFile($Source,[ref]$tokens,[ref]$errors)
if($errors.Count){throw 'Source parse failed'}
$wanted=@('Protect-WechatbotDiagnostic','Get-WechatbotDiagnosticExcerpt','Invoke-Checked','Save-WechatbotStartupError')
$ast.FindAll({param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $wanted -contains $node.Name},$true) | ForEach-Object {Invoke-Expression $_.Extent.Text}
$runtimeRoot=$Runtime
$script:taskPythonBootstrapStarted=$true
$script:taskPythonBootstrapStartUtc=[DateTime]::UtcNow
try {Invoke-Checked $Python @('-X','utf8','-B',$Child,(Join-Path $Runtime 'startup-error.txt')) '外层Python阶段';throw 'Expected failure missing'} catch {
    $failure=$_
    Save-WechatbotStartupError $failure | Out-Null
    $message=Protect-WechatbotDiagnostic $failure.Exception.Message
    $stdout=@(Invoke-Checked $Python @('-X','utf8','-B',$Finder) 'synthetic-find' -CaptureOutput)
    $secretProbe=Protect-WechatbotDiagnostic "Error: Authorization: Token AUTH_CANARY_123`npassword=`"firstline`nMULTILINE_CANARY_123`nlastline`""
    [pscustomobject]@{message=[string]$message; report=[string](Get-Content -LiteralPath (Join-Path $Runtime 'startup-error.txt') -Raw -Encoding UTF8); version=$PSVersionTable.PSVersion.ToString(); stdout=$stdout; secret_probe=[string]$secretProbe} | ConvertTo-Json | Set-Content -LiteralPath $Result -Encoding UTF8
}
''', encoding="utf-8-sig")
        ps = Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
        process = subprocess.run([str(ps), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(fixture), "-Source", str(PORTABLE / "bootstrap.ps1"), "-Runtime", str(self.root), "-Python", sys.executable, "-Child", str(child), "-Finder", str(finder), "-Result", str(result_file)], capture_output=True)
        self.assertEqual(process.returncode, 0, process.stderr.decode(errors="replace"))
        data = json.loads(result_file.read_text(encoding="utf-8-sig"))
        self.assertTrue(data["version"].startswith("5.1."))
        for key in ("message", "report"):
            self.assertIn("No module named synthetic_dep", data[key])
            self.assertIn("阶段", data[key])
            self.assertNotIn("private-token", data[key])
            self.assertNotIn("sk-synthetic123456", data[key])
        self.assertIn("微信bot 启动诊断", data["report"])
        self.assertIn("外层启动诊断", data["report"])
        self.assertEqual(data['stdout'], ['C:/synthetic/python.exe'])
        self.assertNotIn('AUTH_CANARY_123', data['secret_probe'])
        self.assertNotIn('MULTILINE_CANARY_123', data['secret_probe'])


class MutableConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="wechatbot-mutable-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bridge = self.root / "bridge"
        self.instance = self.root / "runtime/astrbot-instance"
        for name, value in (("BRIDGE", self.bridge), ("INSTANCE", self.instance),
                            ("RUNTIME", self.root / "runtime")):
            manager = patch.object(boot, name, value)
            manager.start(); self.addCleanup(manager.stop)
        self.target = self.instance / "data/cmd_config.json"
        self.target.parent.mkdir(parents=True)
        self.original = b'{"dashboard":{"password":"synthetic-preserved"}}'
        self.target.write_bytes(self.original)
        # This cleanup is restricted to files created inside this synthetic root.
        self.addCleanup(self.clear_fixture_attributes)

    def clear_fixture_attributes(self):
        if os.name == "nt":
            for path in self.root.rglob("*"):
                if path.is_file() and not path.is_symlink():
                    boot._set_mutable_config_attributes(path, 0x80)

    def readonly(self, path=None, extra=0x20):
        boot._set_mutable_config_attributes(path or self.target, 1 | extra)

    @unittest.skipUnless(os.name == "nt", "Windows native read-only configuration")
    def test_native_sdk_snapshot_fails_readonly_then_persistent_repair_allows_two_saves(self):
        sdk = Path(sys.executable).parent.parent / "Lib/site-packages/astrbot/core/config/astrbot_config.py"
        sdk = Path(sys.executable).parent.parent.parent / "astrbot-venv/Lib/site-packages/astrbot/core/config/astrbot_config.py" if not sdk.is_file() else sdk
        # Extract the public SDK method; do not import AstrBot or access its config.
        if sdk.is_file():
            source = sdk.read_text(encoding="utf-8")
        else:
            with zipfile.ZipFile(PORTABLE / "vendor/wheels/astrbot-4.28.2-py3-none-any.whl") as wheel:
                source = wheel.read("astrbot/core/config/astrbot_config.py").decode("utf-8")
        tree = ast.parse(source)
        cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "AstrBotConfig")
        method = next(node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name == "_write_config_snapshot")
        namespace = {"os": os, "json": json, "tempfile": tempfile}
        exec(compile(ast.Module(body=[method], type_ignores=[]), "public-sdk-snapshot", "exec"), namespace)
        configuration = types.SimpleNamespace(config_path=str(self.target), _save_commit_lock=threading.Lock(), _save_committed_revision=0)
        self.readonly(extra=0x22)
        with self.assertRaises(PermissionError):
            namespace[method.name](configuration, {"password": "synthetic-preserved"}, 1, 2)
        self.assertEqual(self.target.read_bytes(), self.original)
        before = self.target.stat().st_file_attributes
        boot.prepare_mutable_config(self.target)
        self.assertEqual(self.target.read_bytes(), self.original)
        self.assertEqual(self.target.stat().st_file_attributes, before & ~1)
        for revision in (1, 2):
            self.assertTrue(namespace[method.name](configuration, {"password": "synthetic-preserved", "revision": revision}, revision, 2))
        self.assertEqual(json.loads(self.target.read_text(encoding="utf-8-sig"))["password"], "synthetic-preserved")

    @unittest.skipUnless(os.name == "nt", "Windows native read-only configuration")
    def test_readonly_atomic_save_preserves_other_attributes_and_password_file(self):
        login = self.instance / "dashboard-login.txt"
        login.write_bytes(b"synthetic-login-untouched")
        self.readonly(extra=0x22)
        self.readonly(login)
        before = self.target.stat().st_file_attributes
        boot.atomic_json(self.target, {"password": "synthetic-preserved", "saved": True})
        self.assertEqual(self.target.stat().st_file_attributes, before & ~1)
        self.assertEqual(json.loads(self.target.read_text())["password"], "synthetic-preserved")
        self.assertEqual(login.read_bytes(), b"synthetic-login-untouched")
        self.assertTrue(login.stat().st_file_attributes & 1)
        self.assertEqual(list(self.target.parent.glob(".wechatbot-config-*.tmp")), [])

    @unittest.skipUnless(os.name == "nt", "Windows native read-only configuration")
    def test_bridge_config_zero_changes_still_repairs_readonly_without_rewriting(self):
        self.bridge.mkdir()
        path = self.bridge / "config.json"
        data = {"astrbot_attachments": str(self.instance / "data/attachments"),
                "ffmpeg_path": str(self.root / "runtime/bin/ffmpeg.exe"), "password": "synthetic-kept"}
        path.write_text(json.dumps(data), encoding="utf-8")
        original = path.read_bytes(); self.readonly(path)
        info = path.stat()
        self.assertEqual(boot.prepare_bridge_config(), data)
        after = path.stat()
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual((info.st_ino, info.st_mtime_ns), (after.st_ino, after.st_mtime_ns))
        self.assertFalse(after.st_file_attributes & 1)

    def test_writable_noop_does_not_set_attributes_or_rewrite(self):
        before = self.target.stat()
        with patch.object(boot, "_set_mutable_config_attributes") as setter:
            boot.prepare_mutable_config(self.target)
        setter.assert_not_called()
        after = self.target.stat()
        self.assertEqual(self.target.read_bytes(), self.original)
        self.assertEqual((before.st_ino, before.st_mtime_ns), (after.st_ino, after.st_mtime_ns))

    @unittest.skipUnless(os.name == "nt", "Windows native read-only configuration")
    def test_readonly_only_and_normal_configuration_support_repeated_saves(self):
        # Some extractors omit ARCHIVE: clearing READONLY then reports NORMAL.
        self.readonly(extra=0)
        before = self.target.stat()
        self.assertEqual(before.st_file_attributes, 1)
        boot.prepare_mutable_config(self.target)
        after = self.target.stat()
        self.assertEqual(after.st_file_attributes, 0x80)
        self.assertEqual((before.st_ino, before.st_mtime_ns), (after.st_ino, after.st_mtime_ns))
        self.assertEqual(self.target.read_bytes(), self.original)
        for revision in (1, 2):
            boot.atomic_json(self.target, {"password": "synthetic-preserved", "revision": revision})
            self.assertEqual(self.target.stat().st_file_attributes, 0x80)
            self.assertEqual(json.loads(self.target.read_text())["revision"], revision)

    @unittest.skipUnless(os.name == "nt", "Windows native read-only configuration")
    def test_readonly_only_replace_failure_restores_exact_original_attributes(self):
        self.readonly(extra=0)
        with patch.object(boot.os, "replace", side_effect=PermissionError("private-path-canary")):
            with self.assertRaises(RuntimeError) as caught:
                boot.atomic_json(self.target, {"password": "synthetic-new"})
        self.assertEqual(self.target.read_bytes(), self.original)
        self.assertEqual(self.target.stat().st_file_attributes, 1)
        message = str(caught.exception)
        self.assertIn("步骤：替换本包配置", message)
        self.assertIn("无需或已完成", message)
        self.assertNotIn("private-path-canary", message)
        self.assertNotIn("WinError：None", message)
        self.assertEqual(list(self.target.parent.glob(".wechatbot-config-*.tmp")), [])

    @unittest.skipUnless(os.name == "nt", "Windows native read-only configuration")
    def test_bridge_config_readonly_only_can_be_adapted_without_losing_settings(self):
        self.bridge.mkdir()
        path = self.bridge / "config.json"
        data = {"bot_persona": "synthetic-persona", "main_llm_api_key": "synthetic-key", "custom": {"keep": True}}
        path.write_text(json.dumps(data), encoding="utf-8")
        self.readonly(path, extra=0)
        result = boot.prepare_bridge_config()
        self.assertEqual({key: result[key] for key in data}, data)
        self.assertEqual(json.loads(path.read_text()), result)
        self.assertEqual(path.stat().st_file_attributes, 0x80)
        self.assertEqual(result["astrbot_attachments"], str(self.instance / "data/attachments"))
        self.assertEqual(boot.prepare_bridge_config(), result)

    def test_unknown_target_cannot_clear_readonly_or_touch_password(self):
        login = self.instance / "dashboard-login.txt"
        login.write_bytes(b"synthetic-password")
        with patch.object(boot, "_set_mutable_config_attributes") as setter, self.assertRaises(RuntimeError):
            boot.prepare_mutable_config(login)
        setter.assert_not_called()
        self.assertEqual(login.read_bytes(), b"synthetic-password")

    def test_symlink_or_junction_ancestor_refuses_mutable_repair(self):
        actual = self.target.parent.lstat()
        fake = types.SimpleNamespace(st_mode=actual.st_mode, st_file_attributes=0x400)
        real = Path.lstat
        def inspect(path):
            return fake if path == self.target.parent else real(path)
        with patch.object(Path, "lstat", inspect), patch.object(boot, "_set_mutable_config_attributes") as setter, self.assertRaises(RuntimeError):
            boot.prepare_mutable_config(self.target)
        setter.assert_not_called()
        self.assertEqual(self.target.read_bytes(), self.original)

    @unittest.skipUnless(os.name == "nt", "Windows native read-only configuration")
    def test_native_attribute_permission_failure_is_safe_and_preserves_original(self):
        self.readonly()
        error = PermissionError("private-configuration-path-canary")
        error.winerror = 5
        with patch.object(boot, "_set_mutable_config_attributes", side_effect=error), self.assertRaises(RuntimeError) as caught:
            boot.prepare_mutable_config(self.target)
        self.assertIn("WinError：5", str(caught.exception))
        self.assertNotIn("private-configuration-path-canary", str(caught.exception))
        self.assertEqual(self.target.read_bytes(), self.original)
        self.assertTrue(self.target.stat().st_file_attributes & 1)

    @unittest.skipUnless(os.name == "nt", "Windows native read-only configuration")
    def test_atomic_replace_failure_or_interrupt_restores_original_bytes_and_attributes(self):
        for error in (PermissionError("private-path-canary"), KeyboardInterrupt(), SystemExit(17)):
            with self.subTest(error=type(error).__name__):
                self.readonly(extra=0x22)
                original_attributes = self.target.stat().st_file_attributes
                with patch.object(boot.os, "replace", side_effect=error):
                    with self.assertRaises(BaseException):
                        boot.atomic_json(self.target, {"password": "synthetic-new"})
                self.assertEqual(self.target.read_bytes(), self.original)
                self.assertEqual(self.target.stat().st_file_attributes, original_attributes)
                self.assertEqual(list(self.target.parent.glob(".wechatbot-config-*.tmp")), [])

    @unittest.skipUnless(os.name == "nt", "Windows native read-only configuration")
    def test_change_during_clear_is_not_restored_or_overwritten(self):
        self.readonly()
        setter = boot._set_mutable_config_attributes
        calls = []
        def change(path, attributes):
            calls.append(attributes); setter(path, attributes)
            path.write_bytes(b"third-party-value")
        with patch.object(boot, "_set_mutable_config_attributes", side_effect=change), self.assertRaises(RuntimeError) as caught:
            boot.prepare_mutable_config(self.target)
        self.assertEqual(calls, [0x20])
        self.assertEqual(self.target.read_bytes(), b"third-party-value")
        self.assertIn("需检查", str(caught.exception))

    @unittest.skipUnless(os.name == "nt", "Windows native read-only configuration")
    def test_interrupt_immediately_after_attribute_clear_restores_original(self):
        setter = boot._set_mutable_config_attributes
        for extra in (0, 0x22):
            for error in (KeyboardInterrupt(), SystemExit(17)):
                with self.subTest(extra=extra, error=type(error).__name__):
                    self.readonly(extra=extra)
                    original_attributes = self.target.stat().st_file_attributes
                    def interrupted(path, attributes):
                        setter(path, attributes)
                        if not attributes & 1:
                            raise error
                    with patch.object(boot, "_set_mutable_config_attributes", side_effect=interrupted), self.assertRaises(BaseException):
                        boot.prepare_mutable_config(self.target)
                    self.assertEqual(self.target.read_bytes(), self.original)
                    self.assertEqual(self.target.stat().st_file_attributes, original_attributes)

    def test_hardlinked_configuration_refuses_attribute_change(self):
        alias = self.target.with_name("unowned-alias.json")
        os.link(self.target, alias)
        with patch.object(boot, "_set_mutable_config_attributes") as setter, self.assertRaises(RuntimeError):
            boot.prepare_mutable_config(self.target)
        setter.assert_not_called()
        self.assertEqual(alias.read_bytes(), self.original)

    @unittest.skipUnless(os.name == "nt", "Windows native read-only configuration")
    def test_third_party_attribute_change_is_not_restored_or_overwritten(self):
        self.readonly()
        setter = boot._set_mutable_config_attributes
        calls = []
        def change(path, attributes):
            calls.append(attributes)
            setter(path, attributes | 2)
        with patch.object(boot, "_set_mutable_config_attributes", side_effect=change), self.assertRaises(RuntimeError):
            boot.prepare_mutable_config(self.target)
        self.assertEqual(calls, [0x20])
        self.assertEqual(self.target.read_bytes(), self.original)
        self.assertEqual(self.target.stat().st_file_attributes, 0x22)
        # Once a snapshot is captured, an unrelated attribute change must refuse replacement.
        snapshot = boot._mutable_config_snapshot
        count = 0
        def changed_snapshot(path):
            nonlocal count
            count += 1
            if count == 2:
                setter(path, 0x22)
            return snapshot(path)
        setter(self.target, 0x20)
        with patch.object(boot, "_mutable_config_snapshot", side_effect=changed_snapshot), patch.object(boot.os, "replace") as replace, self.assertRaises(RuntimeError):
            boot.atomic_json(self.target, {"password": "synthetic-new"})
        replace.assert_not_called()
        self.assertEqual(self.target.read_bytes(), self.original)
        self.assertEqual(self.target.stat().st_file_attributes, 0x22)

    def test_new_mutable_file_is_created_with_exclusive_own_temp(self):
        target = self.instance / "data/wechatbot-profile-location.json"
        sentinel = target.with_name(target.name + ".tmp")
        sentinel.write_bytes(b"unrelated-temp-untouched")
        boot.atomic_json(target, {"synthetic": True})
        self.assertEqual(sentinel.read_bytes(), b"unrelated-temp-untouched")
        self.assertEqual(json.loads(target.read_text()), {"synthetic": True})
        self.assertEqual(list(target.parent.glob(".wechatbot-config-*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
