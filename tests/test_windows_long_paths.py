"""Package-only startup hook: path ownership, real alias and repair lifecycle."""
from __future__ import annotations

import importlib.util
import json
import ntpath
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
PORTABLE = ROOT / "portable"
sys.path.insert(0, str(PORTABLE))
import portable_bootstrap as boot
import windows_long_paths as paths


class PathRewriteTests(unittest.TestCase):
    def test_order_relative_external_and_neighbor_paths_are_preserved(self):
        values = ["", "relative/path", "C:relative", "\\drive-relative", "C:/owned/venv/Lib/site-packages",
                  r"C:\owned\python\Lib", r"C:\owned\venv-neighbor\Lib", r"D:\external\Lib", 5]
        rewritten = paths.rewrite_paths(values, r"C:\owned\venv", r"C:\owned\python")
        self.assertEqual(rewritten, values[:4] + [r"\\?\C:\owned\venv\Lib\site-packages", r"\\?\C:\owned\python\Lib"] + values[6:])
        self.assertEqual(values[4], "C:/owned/venv/Lib/site-packages")

    def test_unc_and_extended_prefixes_are_correct_and_idempotent(self):
        values = [r"\\server\share\venv\Lib", r"\\?\UNC\server\share\venv\Lib",
                  r"\\?\C:\owned\python\DLLs", r"\\other\share\outside"]
        rewritten = paths.rewrite_paths(values, r"\\server\share\venv", r"C:\owned\python")
        self.assertEqual(rewritten, [r"\\?\UNC\server\share\venv\Lib", values[1], values[2], values[3]])
        self.assertEqual(paths.rewrite_paths(rewritten, r"\\server\share\venv", r"C:\owned\python"), rewritten)
        self.assertEqual(paths.extended_path(r"\\server\share"), r"\\?\UNC\server\share")
        self.assertEqual(paths.extended_path(r"\\?\C:/owned/python/DLLs"), r"\\?\C:\owned\python\DLLs")

    def test_parent_components_cannot_escape_owned_root_and_devices_stay_unchanged(self):
        for value in (r"C:\owned\venv\..\outside", r"\\.\C:\owned\venv", r"\\?\GLOBALROOT\Device\HarddiskVolume1", "中:\\owned"):
            self.assertEqual(paths.rewrite_paths([value], r"C:\owned\venv", r"C:\owned\python"), [value])
        self.assertEqual(paths.extended_path(r"C:\owned\venv\Lib\..\DLLs"), r"\\?\C:\owned\venv\DLLs")


@unittest.skipUnless(os.name == "nt", "Windows managed-venv ownership")
class HookOwnershipTests(unittest.TestCase):
    def setUp(self):
        meta_path = list(sys.meta_path)
        self.addCleanup(lambda: sys.meta_path.__setitem__(slice(None), meta_path))
        self.temp = tempfile.TemporaryDirectory(prefix=".long-paths-", dir=ROOT / "tests")
        self.addCleanup(self.temp.cleanup)
        self.package = Path(self.temp.name)
        self.portable = self.package / "portable"
        self.runtime = self.portable / "runtime"
        self.venv = self.runtime / "astrbot-venv"
        self.site = self.venv / "Lib" / "site-packages"
        self.site.mkdir(parents=True)
        self.base = self.runtime / "python" / "cpython-3.12.12"
        self.base.mkdir(parents=True)
        (self.portable / "runtime-manifest.json").write_text("{}")
        shutil.copyfile(PORTABLE / "windows_long_paths.py", self.portable / "windows_long_paths.py")
        self.hook = self.site / "_wechatbot_long_paths.py"
        self.hook.write_text("synthetic hook location")

    def activate(self, values, *, base=None, module=None):
        with patch.object(paths, "__file__", str(module or self.hook)), patch.object(sys, "prefix", str(self.venv)), \
             patch.object(sys, "base_prefix", str(base or self.base)), patch.object(sys, "path", list(values)), \
             patch.object(sys, "path_importer_cache", {str(self.site): "owned", "external": "keep"}):
            active = paths.activate()
            return active, list(sys.path), dict(sys.path_importer_cache)

    def test_owned_hook_rewrites_only_venv_and_managed_stdlib(self):
        values = ["", str(self.site), str(self.base / "Lib"), str(self.package / "outside")]
        active, result, cache = self.activate(values)
        self.assertTrue(active)
        self.assertEqual(result, ["", paths.extended_path(values[1]), paths.extended_path(values[2]), values[3]])
        self.assertEqual(cache, {"external": "keep"})

    def test_foreign_base_or_copied_hook_is_inert(self):
        outside = self.package / "external-python"
        outside.mkdir()
        values = [str(self.site), "relative"]
        for kwargs in ({"base": outside}, {"module": self.package / "_wechatbot_long_paths.py"}):
            active, result, _ = self.activate(values, **kwargs)
            self.assertFalse(active)
            self.assertEqual(result, values)

    def test_internal_uv_base_junction_keeps_alias_and_physical_entries(self):
        alias = self.base.with_name("cpython-3.12")
        ps = Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
        command = "New-Item -ItemType Junction -Path '" + str(alias).replace("'", "''") + "' -Target '" + str(self.base).replace("'", "''") + "' | Out-Null"
        result = subprocess.run([str(ps), "-NoProfile", "-Command", command], capture_output=True)
        self.assertEqual(result.returncode, 0, "Could not prepare isolated internal junction")
        active, values, _ = self.activate([str(alias / "Lib"), str(self.base / "Lib")], base=alias)
        self.assertTrue(active)
        self.assertEqual(values, [paths.extended_path(str(alias / "Lib")), paths.extended_path(str(self.base / "Lib"))])

    def test_install_is_byte_stable_and_rejects_foreign_environment(self):
        with patch.object(boot, "HERE", self.portable), patch.object(boot, "RUNTIME", self.runtime):
            boot.install_long_path_hook(self.venv)
            files = [self.hook, self.site / "wechatbot_private_long_paths.pth"]
            before = [(file.read_bytes(), file.stat().st_mtime_ns) for file in files]
            boot.install_long_path_hook(self.venv)
            self.assertEqual([(file.read_bytes(), file.stat().st_mtime_ns) for file in files], before)
            with self.assertRaises(RuntimeError):
                boot.install_long_path_hook(self.package / "astrbot-venv")

    def test_hook_reinstalled_after_uv_repair_before_real_smoke(self):
        (self.venv / "Scripts").mkdir()
        executable = self.venv / "Scripts" / "python.exe"
        executable.write_bytes(b"synthetic")
        shutil.copyfile(PORTABLE / "requirements-astrbot.lock", self.portable / "requirements-astrbot.lock")
        smokes = []
        def run(command, **kwargs):
            if "-c" in command:
                self.assertTrue(self.hook.is_file())
                self.assertTrue((self.site / "wechatbot_private_long_paths.pth").is_file())
                smokes.append(command[-1])
                if len(smokes) == 1:
                    raise RuntimeError("synthetic first smoke failure")
            elif "--reinstall" in command:
                self.hook.unlink()
                (self.site / "wechatbot_private_long_paths.pth").unlink()
        with patch.object(boot, "HERE", self.portable), patch.object(boot, "RUNTIME", self.runtime), patch.object(boot, "run_checked", side_effect=run):
            self.assertEqual(boot.ensure_environment(Path("synthetic-uv"), self.base / "python.exe", "astrbot"), executable)
        self.assertEqual(len(smokes), 2)
        self.assertIn("astrbot.api,anthropic", smokes[1])
        self.assertIn("_wechatbot_long_paths.activate()", smokes[1])


@unittest.skipUnless(os.name == "nt", "Windows Crypto filename semantics")
class CryptoPathAdapterTests(unittest.TestCase):
    setUp = HookOwnershipTests.setUp

    def filesystem(self, *, outside=False):
        folder = self.site / "Crypto" / "Util" if not outside else self.package / "foreign" / "Crypto" / "Util"
        folder.mkdir(parents=True, exist_ok=True)
        source = folder / "_file_system.py"
        source.write_text("# isolated source identity\n")
        module = SimpleNamespace(__file__=paths.extended_path(str(source)))
        module.pycryptodome_filename = lambda directories, filename: ntpath.join(
            paths.extended_path(str(folder)), "..", *directories[1:], filename)
        return module, folder

    def test_owned_dotdot_filename_normalizes_without_losing_extended_prefix_and_is_idempotent(self):
        module, folder = self.filesystem()
        (folder / "_cpuid_c.pyd").write_bytes(b"synthetic identity")
        original = module.pycryptodome_filename
        raw = original(["Crypto", "Util"], "_cpuid_c.pyd")
        self.assertFalse(os.path.isfile(raw))
        self.assertTrue(paths._adapt_crypto_filesystem(module, str(self.site)))
        wrapper = module.pycryptodome_filename
        corrected = wrapper(["Crypto", "Util"], "_cpuid_c.pyd")
        self.assertTrue(corrected.startswith("\\\\?\\"))
        self.assertTrue(os.path.isfile(corrected))
        self.assertNotIn("..", corrected.split("\\"))
        self.assertTrue(paths._adapt_crypto_filesystem(module, str(self.site)))
        self.assertIs(module.pycryptodome_filename, wrapper)
        escaped = ["Crypto", "..", "..", "..", "foreign"]
        self.assertEqual(wrapper(escaped, "file.pyd"), original(escaped, "file.pyd"))

    def test_foreign_origin_and_foreign_physical_origin_are_inert(self):
        module, _ = self.filesystem(outside=True)
        original = module.pycryptodome_filename
        self.assertFalse(paths._adapt_crypto_filesystem(module, str(self.site)))
        self.assertIs(module.pycryptodome_filename, original)
        owned, _ = self.filesystem()
        original = owned.pycryptodome_filename
        with patch.object(paths.os.path, "realpath", return_value=str(self.package / "foreign" / "_file_system.py")):
            self.assertFalse(paths._adapt_crypto_filesystem(owned, str(self.site)))
        self.assertIs(owned.pycryptodome_filename, original)

    def test_loaded_filesystem_and_only_its_owned_raw_api_alias_are_updated(self):
        module, folder = self.filesystem()
        (folder / "_raw_api.py").write_text("# isolated raw API\n")
        original = module.pycryptodome_filename
        raw_api = SimpleNamespace(__file__=paths.extended_path(str(folder / "_raw_api.py")),
                                  pycryptodome_filename=original)
        with patch.dict(sys.modules, {"Crypto.Util._file_system": module, "Crypto.Util._raw_api": raw_api}):
            paths._install_crypto_path_adapter(str(self.site))
            wrapped = module.pycryptodome_filename
            self.assertIs(raw_api.pycryptodome_filename, wrapped)
            paths._install_crypto_path_adapter(str(self.site))
            self.assertIs(module.pycryptodome_filename, wrapped)
            self.assertEqual(sum(getattr(f, "_wechatbot_crypto_site", None) == ntpath.normcase(str(self.site))
                                 for f in sys.meta_path), 1)

    def test_foreign_or_replaced_raw_api_alias_is_not_touched(self):
        for foreign in (False, True):
            module, folder = self.filesystem()
            raw_source = folder / "_raw_api.py" if not foreign else self.package / "foreign_raw_api.py"
            raw_source.write_text("# isolated raw API\n")
            alias = module.pycryptodome_filename if foreign else lambda *_: "other"
            raw_api = SimpleNamespace(__file__=str(raw_source), pycryptodome_filename=alias)
            with patch.dict(sys.modules, {"Crypto.Util._raw_api": raw_api}):
                self.assertTrue(paths._adapt_crypto_filesystem(module, str(self.site)))
                self.assertIs(raw_api.pycryptodome_filename, alias)

    def test_lazy_finder_wraps_only_the_exact_owned_filesystem_module(self):
        module, folder = self.filesystem()
        delegate = SimpleNamespace(create_module=lambda _: module,
                                   exec_module=lambda value: None)
        spec = SimpleNamespace(origin=module.__file__, loader=delegate)
        finder = paths._CryptoFilesystemFinder(str(self.site))
        with patch.object(paths.PathFinder, "find_spec", return_value=spec) as lookup:
            self.assertIsNone(finder.find_spec("Crypto.Cipher.AES"))
            lookup.assert_not_called()
            found = finder.find_spec("Crypto.Util._file_system", [str(folder)])
        self.assertIs(found, spec)
        self.assertIs(found.loader.create_module(found), module)
        original = module.pycryptodome_filename
        found.loader.exec_module(module)
        self.assertIsNot(module.pycryptodome_filename, original)
        foreign_spec = SimpleNamespace(origin=str(self.package / "foreign.py"), loader=delegate)
        with patch.object(paths.PathFinder, "find_spec", return_value=foreign_spec):
            self.assertIsNone(finder.find_spec("Crypto.Util._file_system", [str(folder)]))
        self.assertIs(foreign_spec.loader, delegate)

    def test_native_aes_vector_on_115_character_owned_package_lazy_and_preloaded(self):
        wheel = PORTABLE / "vendor/wheels/pycryptodome-3.24.0-cp37-abi3-win_amd64.whl"
        if not wheel.is_file():
            self.skipTest("Full package native wheel is unavailable")
        with tempfile.TemporaryDirectory(prefix="crypto-native-") as temp:
            prefix = str(Path(temp) / "中文 空格")
            package = Path(prefix + "深" * (115 - len(prefix)))
            site = package / "portable/runtime/astrbot-venv/Lib/site-packages"
            site.mkdir(parents=True)
            base = package / "portable/runtime/python/cpython-3.12.12"
            base.mkdir(parents=True)
            (package / "portable/runtime-manifest.json").write_text("{}")
            with zipfile.ZipFile(wheel) as archive:
                for name in archive.namelist():
                    if name.startswith("Crypto/") and not name.endswith("/"):
                        target = site / name
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_bytes(archive.read(name))
            script = """import sys,os,json,pathlib,types
site,base,source,preloaded=sys.argv[1:]
sys.prefix=str(pathlib.Path(site).parents[1]);sys.base_prefix=base
sys.path.insert(0,'\\\\\\\\?\\\\'+site)
baseline=False
if preloaded=='1':
 import Crypto.Util._file_system as f
 raw=f.pycryptodome_filename(['Crypto','Util'],'_cpuid_c.pyd')
 assert not os.path.isfile(raw) and os.path.isfile(os.path.normpath(raw))
 try:
  from Crypto.Cipher import AES
 except OSError as e:
  baseline='Not found' in str(e)
 assert baseline
m=types.ModuleType('candidate_hook');m.__file__=str(pathlib.Path(site)/'_wechatbot_long_paths.py')
exec(compile(pathlib.Path(source).read_bytes(),m.__file__,'exec'),m.__dict__)
assert m.activate();assert m.activate()
from Crypto.Cipher import AES
key=bytes.fromhex('000102030405060708090a0b0c0d0e0f');plain=bytes.fromhex('00112233445566778899aabbccddeeff')
cipher=AES.new(key,AES.MODE_ECB).encrypt(plain)
assert cipher.hex()=='69c4e0d86a7b0430d8cdb78070b4c55a'
assert AES.new(key,AES.MODE_ECB).decrypt(cipher)==plain
print(json.dumps({'success':True,'preloaded':preloaded=='1','baseline_not_found':baseline,'root_length':len(str(pathlib.Path(sys.prefix).parents[2]))}))
"""
            for preloaded in ("0", "1"):
                with self.subTest(preloaded=preloaded):
                    result = subprocess.run([sys.executable, "-X", "utf8", "-B", "-c", script,
                                             str(site), str(base), str(PORTABLE / "windows_long_paths.py"), preloaded],
                                            capture_output=True, text=True, timeout=20,
                                            creationflags=subprocess.CREATE_NO_WINDOW)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    report = json.loads(result.stdout)
                    self.assertTrue(report["success"])
                    self.assertEqual(report["root_length"], 115)
                    self.assertEqual(report["baseline_not_found"], preloaded == "1")


if __name__ == "__main__":
    unittest.main()
