"""Synthetic reviewed profiles only; no native processes or UI are accessed."""
import ctypes
from dataclasses import FrozenInstanceError, replace
import io
from pathlib import Path
import struct
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

BRIDGE = Path(__file__).resolve().parents[1] / "wechat-weflow-bridge-ob11"
if str(BRIDGE) not in sys.path:
    sys.path.insert(0, str(BRIDGE))
import uia_build_profiles as profiles
import uia_recovery as recovery
from test_uia_recovery import ControlledTimers, FakeProcess, image

SECOND = profiles.BuildProfile("synthetic-second", "4.9.0.1", "2" * 64, "AMD64", 0x10000, (0x2000,), 0x2000)


def adapter_for(profile):
    adapter = FakeProcess()
    adapter.current_identity.update(file_version=profile.file_version, sha256=profile.sha256,
                                    architecture=profile.architecture, module_size=profile.size_of_image)
    adapter.values = dict.fromkeys(profile.read_rvas, 0)
    return adapter


def evidence_for(profile):
    return {**profile.metadata(), "dll_path": str(BRIDGE / "Weixin.dll"),
            "candidates": [{"flag_rva": rva, "structural_data_flag": True,
                            "section": {"writable": True, "executable": False}} for rva in profile.read_rvas]}


def native_for(adapter):
    # Exercise the real native binding/address/write guards with an in-memory
    # kernel call. __init__ and all actual process APIs are deliberately absent.
    native = object.__new__(recovery.NativeProcessProbe)
    native.permit_write, native._may_have_changed, native._restoration_complete = True, False, False
    native._bound_identity = native._profile = native._read_identity = None
    native.identity, native.fast_identity = adapter.identity, adapter.identity
    native.page_info, native.read_byte, native.process_exited = adapter.page_info, adapter.read_byte, adapter.process_exited
    native._module = lambda: {key: adapter.current_identity[key] for key in ("dll_path", "module_base", "module_size")}
    native.ctypes, native.handle = ctypes, 1
    def write(handle, address, byte, size, count):
        value = ctypes.cast(byte, ctypes.POINTER(ctypes.c_ubyte)).contents.value
        result = adapter.write_byte(address - adapter.current_identity["module_base"], value)
        ctypes.cast(count, ctypes.POINTER(ctypes.c_size_t)).contents.value = size
        return result
    native.kernel = types.SimpleNamespace(WriteProcessMemory=write)
    return native


class ProfileTests(unittest.TestCase):
    def setUp(self):
        registry = patch.object(profiles, "REVIEWED_PROFILES", (profiles.LEGACY_PROFILE, SECOND))
        registry.start(); self.addCleanup(registry.stop)
        self.timers = ControlledTimers()
        timer_patch = patch.object(recovery.threading, "Timer", self.timers)
        timer_patch.start(); self.addCleanup(timer_patch.stop)
        self.addCleanup(lambda: [timer.cancel() for timer in self.timers.items])

    def test_registry_and_profiles_are_immutable_and_metadata_is_detached(self):
        self.assertIsInstance(profiles.REVIEWED_PROFILES, tuple)
        with self.assertRaises(FrozenInstanceError):
            SECOND.active_rva = 1
        with self.assertRaises(TypeError):
            recovery.SUPPORTED_BUILD["active_rva"] = 1
        metadata = profiles.supported_builds()
        metadata[1]["read_rvas"][0] = 1
        self.assertEqual(SECOND.read_rvas, (0x2000,))
        for changes in ({"read_rvas": [0x2000]}, {"read_rvas": (True,)}, {"active_rva": 0x3000},
                        {"size_of_image": True}, {"sha256": "UNKNOWN"}, {"read_rvas": (0x2000, 0x2000)}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                replace(SECOND, **changes)

    def test_exact_selection_uses_version_hash_architecture_and_size(self):
        adapter = adapter_for(SECOND)
        self.assertIs(profiles.select_profile(adapter.identity()), SECOND)
        self.assertIs(profiles.select_profile(SECOND.metadata()), SECOND)
        for key, value, code in (("file_version", "4.9.1.1", "version_mismatch"),
                                 ("sha256", "3" * 64, "sha256_mismatch"),
                                 ("architecture", "unknown", "architecture_unavailable"),
                                 ("architecture", "I386", "architecture_mismatch"),
                                 ("module_size", 0x20000, "image_size_mismatch")):
            with self.subTest(key=key), self.assertRaises(profiles.ProfileSelectionError) as error:
                profiles.select_profile(dict(adapter.identity(), **{key: value}))
            self.assertEqual(error.exception.code, code)

    def test_unknown_hash_never_queries_pages_or_state_even_with_supplied_addresses(self):
        adapter = adapter_for(SECOND)
        adapter.current_identity.update(sha256="3" * 64, read_rvas=[0x4000], active_rva=0x4000)
        pages = []
        adapter.page_info = lambda rva: pages.append(rva)
        manager = recovery.GateRecoveryManager(True, lambda *args, **kwargs: adapter, lambda hwnd: 42)
        self.assertFalse(manager.ensure(100, 42))
        snapshot = manager.snapshot()
        self.assertIsNone(snapshot["selected_profile"])
        self.assertIsNone(snapshot["verified_build"])
        self.assertEqual(snapshot["build_check"]["status"], "sha256_mismatch")
        self.assertEqual(pages, [])
        self.assertEqual(adapter.reads, [])
        self.assertEqual(adapter.writes, [])

    def test_second_profile_readonly_inspection_uses_only_its_reviewed_rvas(self):
        adapter = adapter_for(SECOND)
        result = recovery.inspect_process(evidence_for(SECOND), 42, adapter.current_identity["executable"], 123,
                                          adapter_factory=lambda *args: adapter)
        self.assertEqual(adapter.reads, [0x2000])
        self.assertEqual(adapter.writes, [])
        self.assertEqual(result["profile"]["profile_id"], SECOND.profile_id)
        self.assertFalse(result["runtime_change_authorized"])

    def test_native_unbound_or_cross_profile_addresses_are_rejected(self):
        adapter = adapter_for(SECOND)
        native = native_for(adapter)
        with self.assertRaises(PermissionError):
            native._address(SECOND.active_rva)
        native._bind_transaction(adapter.identity(), evidence_for(SECOND))
        self.assertEqual(native._address(SECOND.active_rva), adapter.current_identity["module_base"] + 0x2000)
        with self.assertRaises(ValueError):
            native._address(profiles.LEGACY_PROFILE.active_rva)
        with self.assertRaises(PermissionError):
            native.write_byte(profiles.LEGACY_PROFILE.active_rva, 1)
        with self.assertRaises(ValueError):
            native._address(True)
        self.assertEqual(adapter.writes, [])

    def test_native_read_binding_rejects_another_executable_or_conflicting_size(self):
        adapter = adapter_for(SECOND)
        native = native_for(adapter)
        adapter.current_identity["executable"] = recovery._canonical(BRIDGE / "Another.exe")
        with self.assertRaises(ValueError):
            native._bind_read_identity(adapter.identity(), SECOND)
        self.assertIsNone(native._profile)
        self.assertEqual(adapter.reads, [])
        self.assertEqual(adapter.writes, [])
        identity = adapter.identity()
        identity["size_of_image"] = SECOND.size_of_image + 4096
        with self.assertRaises(profiles.ProfileSelectionError):
            profiles.select_profile(identity)

    def test_restore_and_watchdog_pin_profile_when_registry_or_default_changes(self):
        adapter = adapter_for(profiles.LEGACY_PROFILE)
        native = native_for(adapter)
        now = [100.0]
        lease = recovery.activate_gate(native, evidence_for(profiles.LEGACY_PROFILE), adapter.identity(),
                                        allow_runtime_change=True, lease_seconds=10, clock=lambda: now[0])
        with patch.object(profiles, "REVIEWED_PROFILES", (SECOND,)), \
                patch.object(recovery, "SUPPORTED_BUILD", SECOND.metadata()):
            now[0] += 10
            self.timers.items[0].fire()
        self.assertEqual(adapter.writes, [(profiles.LEGACY_PROFILE.active_rva, 1),
                                          (profiles.LEGACY_PROFILE.active_rva, 0)])
        self.assertTrue(lease.restored)
        self.assertIs(lease.profile, profiles.LEGACY_PROFILE)

    def test_manager_reuse_and_restore_do_not_switch_to_default_rva(self):
        adapter = adapter_for(SECOND)
        manager = recovery.GateRecoveryManager(True, lambda *args, **kwargs: adapter, lambda hwnd: 42)
        self.assertTrue(manager.ensure(100, 42))
        with patch.object(profiles, "REVIEWED_PROFILES", (profiles.LEGACY_PROFILE,)), \
                patch.object(recovery, "SUPPORTED_BUILD", profiles.LEGACY_PROFILE.metadata()):
            self.assertTrue(manager.ensure(100, 42))
            snapshot = manager.snapshot()
            self.assertEqual(snapshot["selected_profile"]["profile_id"], SECOND.profile_id)
            self.assertTrue(manager.close()["ok"])
        self.assertEqual(adapter.writes, [(0x2000, 1), (0x2000, 0)])
        self.assertTrue(all(rva == 0x2000 for rva in adapter.reads))

    def test_process_restart_selects_new_profile_without_restoring_new_process(self):
        first, second = adapter_for(profiles.LEGACY_PROFILE), adapter_for(SECOND)
        second.current_identity["creation_filetime"] += 1
        adapters = iter((first, second))
        manager = recovery.GateRecoveryManager(True, lambda *args, **kwargs: next(adapters), lambda hwnd: 42)
        self.assertTrue(manager.ensure(100, 42))
        first.exited = True
        first.identity = lambda: (_ for _ in ()).throw(RuntimeError("synthetic old process exited"))
        self.assertTrue(manager.ensure(100, 42))
        self.assertEqual(manager.snapshot()["selected_profile"]["profile_id"], SECOND.profile_id)
        self.assertEqual(first.writes, [(profiles.LEGACY_PROFILE.active_rva, 1)])
        self.assertTrue(manager.close()["ok"])
        self.assertEqual(second.writes, [(0x2000, 1), (0x2000, 0)])

    def test_changed_profile_identity_blocks_restoration_without_reading_new_rva(self):
        adapter = adapter_for(SECOND)
        lease = recovery.activate_gate(adapter, evidence_for(SECOND), adapter.identity(), allow_runtime_change=True)
        previous_reads = list(adapter.reads)
        adapter.current_identity.update(file_version=profiles.LEGACY_PROFILE.file_version,
                                        sha256=profiles.LEGACY_PROFILE.sha256, module_size=profiles.LEGACY_PROFILE.size_of_image)
        result = lease.restore()
        self.assertEqual(result["status"], "identity_changed")
        self.assertEqual(adapter.reads, previous_reads)
        self.assertEqual(adapter.writes, [(0x2000, 1)])
        adapter.exited = True
        lease.restore()

    def test_evidence_cannot_supply_alternate_or_malformed_addresses(self):
        for changes in ({"read_rvas": [0x3000]}, {"read_rvas": None}, {"active_rva": 0x3000},
                        {"candidates": None}, {"candidates": [None]},
                        {"candidates": [{"flag_rva": 0x2000, "structural_data_flag": True, "section": {"writable": "yes", "executable": False}}]},
                        {"candidates": evidence_for(SECOND)["candidates"] * 2}):
            adapter = adapter_for(SECOND)
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                recovery.activate_gate(adapter, dict(evidence_for(SECOND), **changes), adapter.identity(), allow_runtime_change=True)
            self.assertEqual(adapter.reads, [])
            self.assertEqual(adapter.writes, [])

    def test_pe_architecture_is_measured_not_inferred_from_expected_profile(self):
        for machine, expected in ((0x8664, "AMD64"), (0x14C, "I386"), (0xAA64, "ARM64"), (0, "unknown")):
            data = image()
            struct.pack_into("<H", data, 0x84, machine)
            with self.subTest(machine=machine):
                self.assertEqual(recovery._disk_pe_architecture(io.BytesIO(data), len(data)), expected)
        malformed = image(); struct.pack_into("<I", malformed, 60, 0xFFFFFF00)
        self.assertEqual(recovery._disk_pe_architecture(io.BytesIO(malformed), len(malformed)), "unknown")
        self.assertEqual(recovery._disk_pe_architecture(io.BytesIO(b"not a PE"), 8), "unknown")

    def test_native_identity_includes_disk_architecture_and_rejects_changed_file(self):
        with tempfile.TemporaryDirectory() as directory:
            dll = Path(directory) / "fixture.dll"
            dll.write_bytes(image())
            native = object.__new__(recovery.NativeProcessProbe)
            native.dll_path = str(dll)
            native._identity_cache = native._identity_cache_key = None
            native._process = lambda: {"pid": 42, "creation_filetime": 123, "executable": "weixin.exe"}
            native._module = lambda: {"dll_path": str(dll), "module_base": 100, "module_size": 0x5000}
            with patch.object(recovery, "_file_version", return_value="4.9.0.1"):
                result = native.identity()
            self.assertEqual(result["architecture"], "AMD64")
            def changed_file(path):
                dll.write_bytes(b"changed during identity collection")
                return "4.9.0.1"
            with patch.object(recovery, "_file_version", side_effect=changed_file), self.assertRaises(RuntimeError):
                native.identity()


class BuiltinProfileTests(unittest.TestCase):
    def test_reviewed_profiles_keep_new_static_scope_and_only_primary_rvas(self):
        self.assertEqual({item.file_version for item in profiles.REVIEWED_PROFILES}, {"4.1.13.65", "4.1.15.13", "4.1.15.50", "4.1.15.56"})
        for profile, excluded in ((profiles.WECHAT_4_1_15_13, 0x0B966DF0), (profiles.WECHAT_4_1_15_50, 0x0BBA2838), (profiles.WECHAT_4_1_15_56, 0x0BBD3B78)):
            with self.subTest(version=profile.file_version):
                self.assertEqual(profile.review_basis, "static_equivalence")
                self.assertEqual(profile.read_rvas, (profile.active_rva,))
                self.assertNotIn(excluded, profile.read_rvas)

    def test_new_builtin_profiles_bind_native_mock_activate_and_restore_only_primary(self):
        timers = ControlledTimers()
        with patch.object(recovery.threading, "Timer", timers):
            for profile in (profiles.WECHAT_4_1_15_13, profiles.WECHAT_4_1_15_50, profiles.WECHAT_4_1_15_56):
                with self.subTest(version=profile.file_version):
                    adapter = adapter_for(profile)
                    native = native_for(adapter)
                    lease = recovery.activate_gate(native, evidence_for(profile), adapter.identity(), allow_runtime_change=True)
                    self.assertTrue(lease.restore()["ok"])
                    self.assertEqual(adapter.writes, [(profile.active_rva, 1), (profile.active_rva, 0)])
                    self.assertTrue(all(rva == profile.active_rva for rva in adapter.reads))
            for timer in timers.items:
                timer.cancel()

    def test_new_version_with_unknown_hash_gets_no_page_or_state_access(self):
        for profile in (profiles.WECHAT_4_1_15_13, profiles.WECHAT_4_1_15_50, profiles.WECHAT_4_1_15_56):
            with self.subTest(version=profile.file_version):
                adapter = adapter_for(profile)
                adapter.current_identity["sha256"] = "f" * 64
                manager = recovery.GateRecoveryManager(True, lambda *args, **kwargs: adapter, lambda hwnd: 42)
                self.assertFalse(manager.ensure(100, 42))
                self.assertEqual(manager.snapshot()["build_check"]["status"], "sha256_mismatch")
                self.assertEqual(adapter.reads, [])
                self.assertEqual(adapter.writes, [])


if __name__ == "__main__":
    unittest.main()
