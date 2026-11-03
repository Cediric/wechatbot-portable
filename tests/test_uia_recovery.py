"""Offline PE fixtures; no process, Windows UI or Weixin binary is executed."""
import hashlib
import ctypes
import json
import pathlib
import struct
import sys
import tempfile
import threading
import unittest
import types
from unittest.mock import patch

BRIDGE = pathlib.Path(__file__).resolve().parents[1] / "wechat-weflow-bridge-ob11"
if str(BRIDGE) not in sys.path:
    sys.path.insert(0, str(BRIDGE))
import uia_recovery as probe


def reviewed_evidence():
    return {"dll_path": str(BRIDGE / "Weixin.dll"), "sha256": probe.SUPPORTED_BUILD["sha256"],
            "file_version": "4.1.13.65", "architecture": "AMD64", "size_of_image": probe.SUPPORTED_BUILD["size_of_image"],
            "candidates": [{"flag_rva": rva, "structural_data_flag": True,
                "section": {"writable": True, "executable": False}} for rva in probe.SUPPORTED_BUILD["read_rvas"]]}


class FakeProcess:
    def __init__(self):
        self.current_identity = {"pid": 42, "creation_filetime": 123,
            "executable": probe._canonical(BRIDGE / "Weixin.exe"), "dll_path": probe._canonical(BRIDGE / "Weixin.dll"),
            "module_base": 0x180000000, "module_size": probe.SUPPORTED_BUILD["size_of_image"],
            "sha256": probe.SUPPORTED_BUILD["sha256"], "file_version": "4.1.13.65", "architecture": "AMD64"}
        self.values = {rva: 0 for rva in probe.SUPPORTED_BUILD["read_rvas"]}
        self.writes, self.reads, self.closed = [], [], False
        self.protect, self.state, self.kind = 4, 0x1000, 0x1000000
        self.short_write, self.raise_write, self.wrong_readback = False, False, False
        self.identity_calls, self.change_at = 0, 0

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.closed = True

    def close(self):
        self.closed = True

    def identity(self):
        self.identity_calls += 1
        if self.change_at and self.identity_calls >= self.change_at:
            self.current_identity["module_base"] += 0x10000000
        return dict(self.current_identity)

    def page_info(self, rva):
        base = self.current_identity["module_base"]
        return {"allocation_base": base, "base": base + rva, "region_size": 4096,
                "protect": self.protect, "state": self.state, "type": self.kind}

    def read_byte(self, rva):
        self.reads.append(rva)
        return self.values[rva]

    def write_byte(self, rva, value):
        self.writes.append((rva, value))
        self.values[rva] = value if not self.wrong_readback else 0
        if self.raise_write and value == 1:
            raise RuntimeError("changed byte before failing")
        return not (self.short_write and value == 1)

    def write_attempted(self):
        return bool(self.writes)

    def process_exited(self):
        return bool(getattr(self, "exited", False))


class ControlledTimers:
    """Callbacks are deterministic; force=True models a dispatched cancellation."""
    def __init__(self):
        self.items = []
        self.fail_start = False

    def __call__(self, delay, callback):
        owner = self
        class Timer:
            def __init__(self):
                self.delay, self.callback = delay, callback
                self.started = self.cancelled = False
                self.daemon = False

            def start(self):
                if owner.fail_start:
                    raise RuntimeError("cannot start restoration worker")
                self.started = True

            def cancel(self):
                self.cancelled = True

            def fire(self, *, force=False):
                if force or not self.cancelled:
                    self.callback()
        timer = Timer()
        self.items.append(timer)
        return timer


def image(*, flags=0xC0000040, functions=True, anchor=True, branch=0x1060, target=0x3000):
    data = bytearray(0xC00)
    data[:2] = b"MZ"
    struct.pack_into("<I", data, 60, 0x80)
    data[0x80:0x84] = b"PE\0\0"
    struct.pack_into("<HH", data, 0x84, 0x8664, 4)
    struct.pack_into("<H", data, 0x94, 240)
    struct.pack_into("<H", data, 0x98, 0x20B)
    struct.pack_into("<I", data, 0x98 + 56, 0x5000)
    struct.pack_into("<I", data, 0x98 + 108, 16)
    if functions:
        struct.pack_into("<II", data, 0x98 + 136, 0x4000, 12)
    sections = [(b".text", 0x1000, 0x400, 0x60000020),
                (b".rdata", 0x2000, 0x600, 0x40000040),
                (b".data", 0x3000, 0x800, flags),
                (b".pdata", 0x4000, 0xA00, 0x40000040)]
    for i, (name, rva, off, chars) in enumerate(sections):
        ptr = 0x188 + i * 40
        data[ptr:ptr + 8] = name.ljust(8, b"\0")
        struct.pack_into("<IIII", data, ptr + 8, 0x200, rva, 0x200, off)
        struct.pack_into("<I", data, ptr + 36, chars)
    if functions:
        struct.pack_into("<III", data, 0xA00, 0x1000, 0x1100, 0x2050)
    data[0x600:0x600 + len(probe.CORE_LABEL)] = probe.CORE_LABEL
    if anchor:
        data[0x440:0x447] = b"\x48\x8d\x15" + struct.pack("<i", 0x2000 - 0x1047)
    gate(data, 0x1020, target, branch)
    return data


def gate(data, start, target, branch):
    ptr = 0x400 + start - 0x1000
    data[ptr:ptr + 22] = (b"\x48\x85\xc9\x0f\x84" + struct.pack("<i", branch - start - 9)
        + b"\x80\x3d" + struct.pack("<i", target - start - 16)
        + b"\x00\x0f\x84" + struct.pack("<i", branch - start - 22))


class ProbeTests(unittest.TestCase):
    def test_unique_structure_is_never_runtime_authorization(self):
        result = probe.inspect_image(image())
        self.assertEqual(result["status"], "unique_structural_candidate")
        self.assertEqual(result["strict_candidates"], [0x3000])
        self.assertFalse(result["runtime_change_authorized"])
        self.assertEqual(result["candidates"][0]["comparisons"][0]["branch_targets"], [0x1060, 0x1060])
        self.assertEqual(result["candidates"][0]["comparisons"][0]["same_function_anchor_refs"][0]["label_rva"], 0x2000)

    def test_signed_displacements_are_decoded(self):
        data = image(target=0x1005)
        result = probe.inspect_image(data)
        self.assertEqual(result["candidates"][0]["flag_rva"], 0x1005)
        self.assertFalse(result["candidates"][0]["structural_data_flag"])

    def test_near_string_without_same_function_is_not_strict(self):
        data = image()
        struct.pack_into("<III", data, 0xA00, 0x1000, 0x1040, 0x2050)
        result = probe.inspect_image(data)
        self.assertEqual(result["strict_candidates"], [])
        self.assertEqual(result["status"], "review_required")
        self.assertTrue(result["candidates"][0]["comparisons"][0]["nearest_anchor_refs"])

    def test_missing_function_table_or_anchor_is_not_strict(self):
        for data in (image(functions=False), image(anchor=False)):
            with self.subTest():
                self.assertEqual(probe.inspect_image(data)["strict_candidates"], [])

    def test_cross_function_branch_is_not_strict(self):
        self.assertEqual(probe.inspect_image(image(branch=0x1190))["strict_candidates"], [])

    def test_executable_or_readonly_target_cannot_be_data_flag(self):
        for flags in (0xE0000040, 0x40000040):
            with self.subTest(flags=flags):
                result = probe.inspect_image(image(flags=flags))
                self.assertEqual(result["strict_candidates"], [])
                self.assertFalse(result["candidates"][0]["structural_data_flag"])

    def test_non_boolean_initial_byte_is_not_strict(self):
        data = image()
        data[0x800] = 2
        self.assertEqual(probe.inspect_image(data)["strict_candidates"], [])

    def test_multiple_candidate_flags_are_ambiguous(self):
        data = image()
        gate(data, 0x1080, 0x3001, 0x10D0)
        result = probe.inspect_image(data)
        self.assertEqual(result["strict_candidates"], [0x3000, 0x3001])
        self.assertEqual(result["status"], "review_required")
        self.assertIsNone(result["unique_candidate_rva"])

    def test_repeated_comparison_same_flag_remains_one_candidate(self):
        data = image()
        gate(data, 0x1080, 0x3000, 0x10D0)
        result = probe.inspect_image(data)
        self.assertEqual(result["strict_candidates"], [0x3000])
        self.assertEqual(len(result["candidates"][0]["comparisons"]), 2)

    def test_pe_bounds_architecture_and_overlap_fail_closed(self):
        malformed = []
        data = image(); struct.pack_into("<I", data, 60, 0xFFFFFF00); malformed.append(data)
        data = image(); struct.pack_into("<H", data, 0x84, 0x14C); malformed.append(data)
        data = image(); struct.pack_into("<I", data, 0x188 + 20, 0xFFFFFF00); malformed.append(data)
        data = image(); struct.pack_into("<I", data, 0x188 + 40 + 12, 0x1100); malformed.append(data)
        data = image(); struct.pack_into("<I", data, 0x98 + 140, 13); malformed.append(data)
        for bad in malformed:
            with self.subTest():
                with self.assertRaises(ValueError):
                    probe.inspect_image(bad)

    def test_file_hash_manifest_and_dll_remain_unchanged(self):
        with tempfile.TemporaryDirectory(dir=pathlib.Path(__file__).parent) as directory:
            dll = pathlib.Path(directory) / "fixture.dll"
            original = bytes(image())
            dll.write_bytes(original)
            # AppContainer blocks strict GetFinalPathName on a synthetic PE.
            # Canonical-path behavior is checked separately; keep fixture I/O local.
            with patch.object(probe.Path, "resolve", lambda path, strict=False: path.absolute()):
                result = probe.inspect_dll(dll)
            self.assertEqual(result["sha256"], hashlib.sha256(original).hexdigest())
            output = probe.write_manifest(result, pathlib.Path(directory) / "evidence.json")
            self.assertFalse(json.loads(output.read_text(encoding="utf-8"))["runtime_change_authorized"])
            self.assertEqual(dll.read_bytes(), original)
            with self.assertRaises(ValueError):
                probe.write_manifest(result, dll)


class TransactionTests(unittest.TestCase):
    def setUp(self):
        timers = ControlledTimers()
        timer_patch = patch.object(probe.threading, "Timer", timers)
        timer_patch.start()
        self.addCleanup(timer_patch.stop)
        self.addCleanup(lambda: [timer.cancel() for timer in timers.items])
        self.evidence = reviewed_evidence()
        self.adapter = FakeProcess()
        self.identity = dict(self.adapter.current_identity)
        self.rva = probe.SUPPORTED_BUILD["active_rva"]

    def apply(self):
        return probe.activate_gate(self.adapter, self.evidence, self.identity, allow_runtime_change=True)

    def native_mock(self):
        native = object.__new__(probe.NativeProcessProbe)
        native.permit_write, native._may_have_changed, native._restoration_complete = True, False, False
        native._bound_identity = None
        native.identity, native.page_info, native.read_byte = self.adapter.identity, self.adapter.page_info, self.adapter.read_byte
        native.fast_identity = self.adapter.identity
        native.process_exited = self.adapter.process_exited
        native._address = lambda rva: self.identity["module_base"] + rva
        native.ctypes, native.handle = ctypes, 1
        def write(handle, address, byte, size, count):
            value = ctypes.cast(byte, ctypes.POINTER(ctypes.c_ubyte)).contents.value
            rva = address - self.identity["module_base"]
            result = self.adapter.write_byte(rva, value)
            ctypes.cast(count, ctypes.POINTER(ctypes.c_size_t)).contents.value = size
            return result
        native.kernel = types.SimpleNamespace(WriteProcessMemory=write)
        return native

    def test_default_api_and_native_backend_cannot_write(self):
        with self.assertRaises(PermissionError):
            probe.activate_gate(self.adapter, self.evidence, self.identity)
        self.assertEqual(self.adapter.identity_calls, 0)
        native = object.__new__(probe.NativeProcessProbe)
        native.permit_write = False
        with self.assertRaises(PermissionError):
            native.write_byte(self.rva, 1)

    def test_two_byte_probe_is_readonly_and_checks_identity(self):
        result = probe.inspect_process(self.evidence, 42, self.identity["executable"], 123,
                                     adapter_factory=lambda *_: self.adapter)
        self.assertEqual(self.adapter.reads, list(probe.SUPPORTED_BUILD["read_rvas"]))
        self.assertEqual(self.adapter.writes, [])
        self.assertTrue(self.adapter.closed)
        self.assertFalse(result["ui_controls_verified"])
        self.assertFalse(result["runtime_change_authorized"])

    def test_native_write_requires_bound_build_and_expected_transition(self):
        native = self.native_mock()
        with self.assertRaises(PermissionError):
            native.write_byte(self.rva, 1)
        native._bind_transaction(self.identity, self.evidence)
        self.adapter.values[self.rva] = 1
        with self.assertRaises(ValueError):
            native.write_byte(self.rva, 1)
        self.assertEqual(self.adapter.writes, [])

    def test_native_rechecks_page_at_api_boundary(self):
        native = self.native_mock()
        native._bind_transaction(self.identity, self.evidence)
        original = native.page_info
        calls = []
        def changing(rva):
            calls.append(rva)
            if len(calls) > 1:
                self.adapter.protect = 0x40
            return original(rva)
        native.page_info = changing
        with self.assertRaises(ValueError):
            native.write_byte(self.rva, 1)
        self.assertEqual(self.adapter.writes, [])
        self.assertFalse(native.write_attempted())

    def test_native_changed_module_sha_is_rejected(self):
        native = self.native_mock()
        native._bind_transaction(self.identity, self.evidence)
        self.adapter.current_identity["sha256"] = "bad"
        with self.assertRaises(ValueError):
            native.write_byte(self.rva, 1)
        self.assertEqual(self.adapter.writes, [])

    def test_native_transaction_is_single_use_after_restore(self):
        native = self.native_mock()
        lease = probe.activate_gate(native, self.evidence, self.identity, allow_runtime_change=True)
        self.assertTrue(lease.restore()["ok"])
        self.assertEqual(self.adapter.writes, [(self.rva, 1), (self.rva, 0)])
        for value in (0, 1):
            with self.assertRaises(PermissionError):
                native.write_byte(self.rva, value)
        self.assertEqual(len(self.adapter.writes), 2)

    def test_probe_rejects_changed_identity_after_read(self):
        self.adapter.change_at = 2
        with self.assertRaises(ValueError):
            probe.inspect_process(self.evidence, 42, self.identity["executable"], 123,
                                  adapter_factory=lambda *_: self.adapter)
        self.assertEqual(self.adapter.writes, [])

    def test_wrong_sha_version_or_executable_stops_before_write(self):
        for key, value in (("sha256", "0" * 64), ("file_version", "4.1.15.13")):
            with self.subTest(key=key):
                bad = dict(self.evidence, **{key: value})
                with self.assertRaises(ValueError):
                    probe.activate_gate(self.adapter, bad, self.identity, allow_runtime_change=True)
        self.adapter.current_identity["executable"] += ".fake"
        with self.assertRaises(ValueError):
            self.apply()
        self.assertEqual(self.adapter.writes, [])

    def test_creation_module_base_or_size_changes_stop_write(self):
        for key in ("creation_filetime", "module_base", "module_size"):
            with self.subTest(key=key):
                self.adapter = FakeProcess()
                self.adapter.current_identity[key] += 1
                with self.assertRaises(ValueError):
                    self.apply()
                self.assertEqual(self.adapter.writes, [])

    def test_original_one_or_non_boolean_is_never_overwritten(self):
        for value in (1, 2):
            with self.subTest(value=value):
                self.adapter.values[self.rva] = value
                with self.assertRaises(ValueError):
                    self.apply()
                self.assertEqual(self.adapter.writes, [])

    def test_page_exec_guard_readonly_wrong_image_or_decommitted_refuses(self):
        for attr, value in (("protect", 0x40), ("protect", 0x104), ("protect", 2),
                            ("kind", 0x20000), ("state", 0x10000)):
            with self.subTest(attr=attr, value=value):
                self.adapter = FakeProcess()
                setattr(self.adapter, attr, value)
                with self.assertRaises(ValueError):
                    self.apply()
                self.assertEqual(self.adapter.writes, [])

    def test_apply_only_one_byte_and_restore_confirmed_original(self):
        lease = self.apply()
        self.assertTrue(lease.applied)
        self.assertEqual(self.adapter.writes, [(self.rva, 1)])
        self.assertEqual(self.adapter.values[probe.SUPPORTED_BUILD["read_rvas"][1]], 0)
        restored = lease.restore()
        self.assertTrue(restored["ok"])
        self.assertTrue(lease.restored)
        self.assertEqual(self.adapter.writes, [(self.rva, 1), (self.rva, 0)])
        self.assertEqual(lease.restore()["status"], "already_restored")
        self.assertEqual(len(self.adapter.writes), 2)

    def test_short_write_or_exception_after_change_rolls_back(self):
        for attr in ("short_write", "raise_write"):
            with self.subTest(attr=attr):
                self.adapter = FakeProcess()
                setattr(self.adapter, attr, True)
                with self.assertRaises(RuntimeError):
                    self.apply()
                self.assertEqual(self.adapter.values[self.rva], 0)
                self.assertEqual(self.adapter.writes, [(self.rva, 1), (self.rva, 0)])

    def test_keyboard_interrupt_after_change_rolls_back(self):
        original_write = self.adapter.write_byte
        def interrupted(rva, value):
            original_write(rva, value)
            if value == 1:
                raise KeyboardInterrupt()
            return True
        self.adapter.write_byte = interrupted
        with self.assertRaises(KeyboardInterrupt):
            self.apply()
        self.assertEqual(self.adapter.values[self.rva], 0)

    def test_submit_identity_change_cancels_without_writing(self):
        self.adapter.change_at = 2
        with self.assertRaises(ValueError):
            self.apply()
        self.assertEqual(self.adapter.writes, [])

    def test_competing_activation_before_our_write_is_preserved(self):
        original = self.adapter.read_byte
        def other_actor(rva):
            if self.adapter.reads:
                self.adapter.values[rva] = 1
            return original(rva)
        self.adapter.read_byte = other_actor
        with self.assertRaises(ValueError):
            self.apply()
        self.assertEqual(self.adapter.values[self.rva], 1)
        self.assertEqual(self.adapter.writes, [])
        self.assertEqual(probe.GateLease(self.adapter, self.identity).restore()["status"], "write_not_attempted")

    def test_page_changes_at_submit_prevent_write(self):
        original = self.adapter.page_info
        calls = []
        def changed(rva):
            calls.append(rva)
            if len(calls) > 1:
                self.adapter.protect = 0x40
            return original(rva)
        self.adapter.page_info = changed
        with self.assertRaises(ValueError):
            self.apply()
        self.assertEqual(self.adapter.writes, [])

    def test_restored_lease_never_reverts_later_external_activation(self):
        lease = self.apply()
        self.assertTrue(lease.restore()["ok"])
        self.adapter.values[self.rva] = 1
        self.assertTrue(lease.restore()["ok"])
        self.assertEqual(self.adapter.values[self.rva], 1)
        self.assertEqual(len(self.adapter.writes), 2)

    def test_restore_refuses_reused_pid_module_page_or_other_byte(self):
        for condition in ("creation", "module", "page", "byte"):
            with self.subTest(condition=condition):
                self.adapter = FakeProcess()
                lease = self.apply()
                if condition == "creation":
                    self.adapter.current_identity["creation_filetime"] += 1
                elif condition == "module":
                    self.adapter.current_identity["module_base"] += 1
                elif condition == "page":
                    self.adapter.protect = 0x40
                else:
                    self.adapter.values[self.rva] = 2
                self.assertFalse(lease.restore()["ok"])
                self.assertEqual(self.adapter.writes, [(self.rva, 1)])

    def test_restore_failure_is_reported_and_not_claimed_success(self):
        lease = self.apply()
        def failed_restore(rva, value):
            return False
        self.adapter.write_byte = failed_restore
        result = lease.restore()
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "restore_failed")
        self.assertFalse(lease.restored)


class ManagerTests(unittest.TestCase):
    def setUp(self):
        timers = ControlledTimers()
        timer_patch = patch.object(probe.threading, "Timer", timers)
        timer_patch.start()
        self.addCleanup(timer_patch.stop)
        self.addCleanup(lambda: [timer.cancel() for timer in timers.items])
        self.adapters, self.factory_calls = [FakeProcess()], []
        def factory(pid, dll_path, permit_write=False):
            self.factory_calls.append((pid, dll_path, permit_write))
            return self.adapters[len(self.factory_calls) - 1]
        self.manager = probe.GateRecoveryManager(True, factory, lambda hwnd: 42)

    def test_known_build_lease_is_reused_and_close_is_idempotent(self):
        adapter = self.adapters[0]
        self.assertTrue(self.manager.ensure(100, 42))
        self.assertTrue(self.manager.ensure(100, 42))
        self.assertEqual(len(self.factory_calls), 1)
        self.assertEqual(adapter.writes, [(probe.SUPPORTED_BUILD["active_rva"], 1)])
        self.assertTrue(self.manager.snapshot()["active"])
        self.assertTrue(self.manager.close()["ok"])
        self.assertTrue(self.manager.close()["ok"])
        self.assertEqual(len(adapter.writes), 2)
        self.assertTrue(adapter.closed)

    def test_snapshot_is_metadata_only(self):
        self.assertTrue(self.manager.ensure(100, 42))
        adapter = self.adapters[0]
        before = (adapter.identity_calls, len(adapter.reads), len(adapter.writes))
        self.manager.snapshot()
        self.manager.snapshot()
        self.assertEqual((adapter.identity_calls, len(adapter.reads), len(adapter.writes)), before)
        snapshot = self.manager.snapshot()
        self.assertEqual(snapshot["observed_build"]["architecture"], "AMD64")
        self.assertEqual(snapshot["observed_build"]["sha256"], probe.SUPPORTED_BUILD["sha256"])
        self.assertEqual(snapshot["build_check"]["status"], "matched")
        self.manager.close()

    def test_initial_snapshot_does_not_open_or_query_any_process(self):
        def forbidden(*args, **kwargs):
            raise AssertionError("snapshot must not query process metadata")
        manager = probe.GateRecoveryManager(True, forbidden, forbidden)
        self.assertIsNone(manager.snapshot()["observed_build"])
        self.assertEqual(manager.snapshot()["build_check"]["status"], "not_checked")

    def test_build_mismatches_preserve_safe_actual_metadata_before_any_byte_read(self):
        cases = (("file_version", "4.1.15.53", "version_mismatch"),
                 ("file_version", "", "version_unavailable"),
                 ("file_version", None, "version_unavailable"),
                 ("sha256", "f" * 64, "sha256_mismatch"),
                 ("sha256", None, "sha256_unavailable"),
                 ("module_size", probe.SUPPORTED_BUILD["size_of_image"] + 4096, "image_size_mismatch"),
                 ("module_size", None, "image_size_unavailable"))
        for key, value, status in cases:
            with self.subTest(key=key, value=value):
                adapter = FakeProcess()
                adapter.current_identity[key] = value
                manager = probe.GateRecoveryManager(True, lambda *args, **kwargs: adapter, lambda hwnd: 42)
                self.assertFalse(manager.ensure(100, 42))
                self.assertEqual(adapter.reads, [])
                self.assertEqual(adapter.writes, [])
                self.assertTrue(adapter.closed)
                before = adapter.identity_calls
                snapshot = manager.snapshot()
                self.assertEqual(snapshot["build_check"]["status"], status)
                self.assertIsNone(snapshot["verified_build"])
                self.assertIsNone(snapshot["selected_profile"])
                self.assertEqual(snapshot["supported_builds"][0]["file_version"], "4.1.13.65")
                expected_key = "size_of_image" if key == "module_size" else key
                self.assertEqual(snapshot["observed_build"][expected_key], value if value != "" else None)
                self.assertNotIn("dll_path", snapshot["observed_build"])
                self.assertNotIn("executable", snapshot["observed_build"])
                snapshot["observed_build"]["file_version"] = "mutated"
                snapshot["build_check"]["status"] = "mutated"
                self.assertEqual(manager.snapshot()["build_check"]["status"], status)
                self.assertEqual(adapter.identity_calls, before)
                self.assertEqual(adapter.reads, [])
                self.assertEqual(adapter.writes, [])

    def test_missing_or_malformed_identity_never_leaks_arbitrary_values(self):
        canary = "SECRET_PATH_OR_TOKEN_CANARY"
        for identity in (None, {}, {"file_version": canary * 1000, "sha256": canary,
                                   "module_size": True, "architecture": canary,
                                   "dll_path": "C:/" + canary, "api_key": canary}):
            with self.subTest(identity_type=type(identity).__name__):
                adapter = FakeProcess()
                adapter.identity = lambda: identity
                manager = probe.GateRecoveryManager(True, lambda *args, **kwargs: adapter, lambda hwnd: 42)
                self.assertFalse(manager.ensure(100, 42))
                snapshot = manager.snapshot()
                self.assertEqual(snapshot["build_check"]["status"], "version_unavailable")
                self.assertEqual(snapshot["observed_build"], {"file_version": None, "sha256": None,
                                  "size_of_image": None, "architecture": "unknown"})
                self.assertNotIn(canary, json.dumps(snapshot))
                self.assertEqual(adapter.reads, [])
                self.assertEqual(adapter.writes, [])

    def test_wrong_version_is_reported_before_hash_mismatch(self):
        self.adapters[0].current_identity.update(file_version="4.1.15.53", sha256="f" * 64)
        self.assertFalse(self.manager.ensure(100, 42))
        snapshot = self.manager.snapshot()
        self.assertEqual(snapshot["build_check"]["status"], "version_mismatch")
        self.assertEqual(snapshot["observed_build"]["file_version"], "4.1.15.53")
        self.assertEqual(snapshot["observed_build"]["sha256"], "f" * 64)
        self.assertEqual(self.adapters[0].reads, [])
        self.assertEqual(self.adapters[0].writes, [])

    def test_process_metadata_failure_does_not_echo_user_path(self):
        adapter = self.adapters[0]
        def failed_identity():
            raise OSError(5, "access denied", "C:/PRIVATE_PATH_CANARY/Weixin.dll")
        adapter.identity = failed_identity
        self.assertFalse(self.manager.ensure(100, 42))
        snapshot = self.manager.snapshot()
        self.assertIsNone(snapshot["observed_build"])
        self.assertEqual(snapshot["build_check"]["status"], "not_checked")
        self.assertNotIn("PRIVATE_PATH_CANARY", json.dumps(snapshot))
        self.assertIn("系统错误 5", snapshot["last_error"])
        self.assertEqual(adapter.reads, [])
        self.assertEqual(adapter.writes, [])

    def test_disabled_and_wrong_hwnd_never_open_process(self):
        self.manager._enabled = False
        self.assertFalse(self.manager.ensure(100, 42))
        self.manager._enabled = True
        self.assertFalse(self.manager.ensure(100, 43))
        self.assertEqual(self.factory_calls, [])

    def test_unknown_sha_cannot_write(self):
        self.adapters[0].current_identity["sha256"] = "f" * 64
        self.assertFalse(self.manager.ensure(100, 42))
        self.assertEqual(self.adapters[0].writes, [])
        self.assertTrue(self.adapters[0].closed)
        self.assertFalse(self.manager.snapshot()["active"])

    def test_external_activation_is_not_owned_or_restored(self):
        self.adapters[0].values[probe.SUPPORTED_BUILD["active_rva"]] = 1
        self.assertTrue(self.manager.ensure(100, 42))
        self.assertEqual(self.manager.snapshot()["status"], "already_active_external")
        self.assertFalse(self.manager.snapshot()["active"])
        self.manager.close()
        self.assertEqual(self.adapters[0].writes, [])
        self.assertEqual(self.adapters[0].values[probe.SUPPORTED_BUILD["active_rva"]], 1)

    def test_disabling_restores_owned_lease(self):
        self.assertTrue(self.manager.ensure(100, 42))
        self.manager._enabled = False
        self.assertFalse(self.manager.ensure(100, 42))
        self.assertEqual(self.adapters[0].values[probe.SUPPORTED_BUILD["active_rva"]], 0)
        self.assertFalse(self.manager.snapshot()["active"])

    def test_restore_failure_retains_handle_and_can_retry(self):
        self.assertTrue(self.manager.ensure(100, 42))
        self.adapters[0].protect = 0x40
        self.assertFalse(self.manager.close()["ok"])
        self.assertFalse(self.adapters[0].closed)
        self.assertTrue(self.manager.snapshot()["active"])
        self.adapters[0].protect = 4
        self.assertTrue(self.manager.close()["ok"])
        self.assertTrue(self.adapters[0].closed)

    def test_old_process_exit_cleans_handle_and_allows_new_lease(self):
        self.assertTrue(self.manager.ensure(100, 42))
        old = self.adapters[0]
        old.exited = True
        old.identity = lambda: (_ for _ in ()).throw(RuntimeError("process exited"))
        self.adapters.append(FakeProcess())
        self.adapters[1].current_identity["creation_filetime"] = 124
        self.assertTrue(self.manager.ensure(100, 42))
        self.assertTrue(old.closed)
        self.assertEqual(len(old.writes), 1)  # Never writes a terminated process.
        self.assertEqual(self.manager._identity["creation_filetime"], 124)
        self.manager.close()

    def test_failed_apply_and_failed_rollback_keeps_handle_for_safe_retry(self):
        adapter = self.adapters[0]
        original = adapter.write_byte
        def failing(rva, value):
            original(rva, value)
            if value == 1:
                adapter.protect = 0x40
                return False
            return True
        adapter.write_byte = failing
        self.assertFalse(self.manager.ensure(100, 42))
        self.assertEqual(self.manager.snapshot()["status"], "restore_failed")
        self.assertFalse(adapter.closed)
        self.assertFalse(self.manager.ensure(100, 42))
        self.assertEqual(len(adapter.writes), 1)
        adapter.protect = 4
        self.assertTrue(self.manager.close()["ok"])
        self.assertTrue(adapter.closed)
        self.assertEqual(adapter.values[probe.SUPPORTED_BUILD["active_rva"]], 0)

    def test_interrupt_after_change_with_failed_rollback_retains_handle_and_interrupt(self):
        for interruption in (KeyboardInterrupt, SystemExit):
            with self.subTest(interruption=interruption.__name__):
                self.setUp()
                adapter = self.adapters[0]
                original = adapter.write_byte
                def interrupted(rva, value):
                    original(rva, value)
                    if value == 1:
                        adapter.protect = 0x40
                        raise interruption()
                    return True
                adapter.write_byte = interrupted
                with self.assertRaises(interruption):
                    self.manager.ensure(100, 42)
                self.assertEqual(self.manager.snapshot()["status"], "restore_failed")
                self.assertTrue(self.manager.snapshot()["active"])
                self.assertFalse(adapter.closed)
                self.assertFalse(self.manager.close()["ok"])
                self.assertEqual(adapter.values[probe.SUPPORTED_BUILD["active_rva"]], 1)
                adapter.protect = 4
                self.assertTrue(self.manager.close()["ok"])
                self.assertTrue(adapter.closed)
                self.assertEqual(adapter.values[probe.SUPPORTED_BUILD["active_rva"]], 0)

    def test_interrupt_inside_failed_apply_rollback_keeps_lease_for_retry(self):
        for interruption in (KeyboardInterrupt, SystemExit):
            with self.subTest(interruption=interruption.__name__):
                self.setUp()
                adapter = self.adapters[0]
                original = adapter.write_byte
                block_restore = [True]
                def interrupted_restore(rva, value):
                    if value == 0 and block_restore[0]:
                        raise interruption()
                    original(rva, value)
                    return value == 0
                adapter.write_byte = interrupted_restore
                with self.assertRaises(interruption):
                    self.manager.ensure(100, 42)
                self.assertTrue(self.manager.snapshot()["active"])
                self.assertEqual(self.manager.snapshot()["restore_result"]["status"], "restore_interrupted")
                self.assertFalse(adapter.closed)
                block_restore[0] = False
                self.assertTrue(self.manager.close()["ok"])
                self.assertEqual(adapter.values[probe.SUPPORTED_BUILD["active_rva"]], 0)

    def test_interrupt_inside_close_keeps_lease_and_handle_for_retry(self):
        for interruption in (KeyboardInterrupt, SystemExit):
            with self.subTest(interruption=interruption.__name__):
                self.setUp()
                adapter = self.adapters[0]
                self.assertTrue(self.manager.ensure(100, 42))
                original = adapter.write_byte
                def interrupted_restore(rva, value):
                    if value == 0:
                        raise interruption()
                    return original(rva, value)
                adapter.write_byte = interrupted_restore
                with self.assertRaises(interruption):
                    self.manager.close()
                self.assertEqual(self.manager.snapshot()["status"], "restore_failed")
                self.assertTrue(self.manager.snapshot()["active"])
                self.assertFalse(adapter.closed)
                adapter.write_byte = original
                self.assertTrue(self.manager.close()["ok"])
                self.assertEqual(adapter.values[probe.SUPPORTED_BUILD["active_rva"]], 0)

    def test_fast_identity_reuses_hash_only_while_file_metadata_matches(self):
        with tempfile.TemporaryDirectory(dir=pathlib.Path(__file__).parent) as directory:
            dll = pathlib.Path(directory) / "metadata.fixture"
            dll.write_bytes(b"first")
            native = object.__new__(probe.NativeProcessProbe)
            native.dll_path = str(dll)
            native._identity_cache = native._identity_cache_key = None
            native._process = lambda: {"pid": 42, "executable": "weixin.exe", "creation_filetime": 123}
            native._module = lambda: {"dll_path": str(dll), "module_base": 100, "module_size": 1000}
            with patch.object(probe, "_file_version", return_value="4.1.13.65"), patch.object(probe.hashlib, "sha256", wraps=hashlib.sha256) as hasher:
                first = native.identity()
                self.assertEqual(native.fast_identity(), first)
                self.assertEqual(native.fast_identity(), first)
                self.assertEqual(hasher.call_count, 1)
                native.identity()  # Every physical write uses this full mode.
                self.assertEqual(hasher.call_count, 2)
                dll.write_bytes(b"different-size")
                self.assertNotEqual(native.fast_identity()["sha256"], first["sha256"])
                self.assertEqual(hasher.call_count, 3)


class LeaseWatchdogTests(unittest.TestCase):
    def setUp(self):
        self.timers = ControlledTimers()
        self.now = 100.0
        self.clock = lambda: self.now
        self.adapter = FakeProcess()
        self.rva = probe.SUPPORTED_BUILD["active_rva"]
        self.managers, self.leases = [], []

    def tearDown(self):
        # All objects are synthetic. An exited fixture allows even deliberate
        # ownership-change cases to release timers and held fixture handles.
        for manager in self.managers:
            if manager._adapter is not None:
                manager._adapter.exited = True
            manager.close()
        for lease in self.leases:
            lease.adapter.exited = True
            lease.restore()
        for timer in self.timers.items:
            timer.cancel()

    def apply(self, **options):
        lease = probe.activate_gate(self.adapter, reviewed_evidence(), self.adapter.identity(),
                                    allow_runtime_change=True, lease_seconds=10,
                                    clock=self.clock, timer_factory=self.timers, **options)
        self.leases.append(lease)
        return lease

    def manager(self, adapter=None, **options):
        source = adapter or self.adapter
        manager = probe.GateRecoveryManager(True, lambda *_args, **_kw: source, lambda _hwnd: 42,
                                             lease_seconds=10, clock=self.clock,
                                             timer_factory=self.timers, **options)
        self.managers.append(manager)
        return manager

    def test_timer_is_armed_before_any_activation_write(self):
        original = self.adapter.write_byte
        def write(rva, value):
            if value == 1:
                self.assertEqual(len(self.timers.items), 1)
                self.assertTrue(self.timers.items[0].started)
                self.assertTrue(self.timers.items[0].daemon)
            return original(rva, value)
        self.adapter.write_byte = write
        lease = self.apply()
        self.assertEqual(lease.metadata()["remaining_seconds"], 10)
        self.now += 10
        self.timers.items[0].fire()
        self.assertTrue(lease.restored)
        self.assertEqual(self.adapter.writes, [(self.rva, 1), (self.rva, 0)])

    def test_timer_start_failure_refuses_activation_before_write(self):
        self.timers.fail_start = True
        with self.assertRaises(probe.GateActivationError) as caught:
            self.apply()
        self.assertTrue(caught.exception.rollback["ok"])
        self.assertEqual(self.adapter.writes, [])
        self.assertTrue(self.timers.items[0].cancelled)

    def test_invalid_or_excessive_duration_cannot_authorize_a_write(self):
        for duration in (0, -1, 171, 180, float("inf"), float("nan"), True):
            with self.subTest(duration=duration), self.assertRaises(ValueError):
                probe.activate_gate(self.adapter, reviewed_evidence(), self.adapter.identity(),
                                    allow_runtime_change=True, lease_seconds=duration,
                                    clock=self.clock, timer_factory=self.timers)
        self.assertEqual(self.adapter.writes, [])

    def test_repeated_ensure_does_not_extend_original_lease(self):
        manager = self.manager()
        self.assertTrue(manager.ensure(100, 42))
        original = self.timers.items[0]
        self.now += 4
        self.assertTrue(manager.ensure(100, 42))
        self.now += 4
        self.assertTrue(manager.ensure(100, 42))
        self.assertEqual(manager.snapshot()["lease"]["remaining_seconds"], 2)
        self.assertEqual(len(self.timers.items), 1)
        self.now += 2
        original.fire()
        snapshot = manager.snapshot()
        self.assertEqual(snapshot["status"], "lease_expired")
        self.assertFalse(snapshot["active"])
        self.assertTrue(snapshot["restore_result"]["ok"])
        self.assertEqual(self.adapter.values[self.rva], 0)

    def test_expired_ensure_cannot_silently_reactivate_same_operation(self):
        manager = self.manager()
        self.assertTrue(manager.ensure(100, 42))
        old_timer = self.timers.items[0]
        self.now += 11  # The timer was delayed; ensure must still honor expiry.
        self.assertFalse(manager.ensure(100, 42))
        self.assertEqual(manager.snapshot()["status"], "lease_expired")
        self.assertEqual(self.adapter.writes, [(self.rva, 1), (self.rva, 0)])
        fresh = FakeProcess()
        manager._factory = lambda *_args, **_kwargs: fresh
        self.assertTrue(manager.ensure(100, 42))
        old_timer.fire(force=True)
        self.assertEqual(fresh.writes, [(self.rva, 1)])
        self.assertEqual(fresh.values[self.rva], 1)

    def test_early_timer_reschedules_remaining_time_without_expiring(self):
        lease = self.apply()
        self.now += 4
        old = self.timers.items[0]
        old.fire()
        self.assertFalse(lease.expired)
        self.assertEqual(self.timers.items[-1].delay, 6)
        self.now += 6
        old.fire(force=True)
        self.assertFalse(lease.restored)  # Canceled callback cannot win.
        self.timers.items[-1].fire()
        self.assertTrue(lease.restored)

    def test_autonomous_restore_does_not_wait_for_desktop_or_manager_lock(self):
        from desktop_coordinator import DESKTOP_LOCK
        restored = threading.Event()
        original = self.adapter.write_byte
        def write(rva, value):
            result = original(rva, value)
            if value == 0:
                restored.set()
            return result
        self.adapter.write_byte = write
        manager = probe.GateRecoveryManager(True, lambda *_args, **_kwargs: self.adapter,
                                             lambda _hwnd: 42, lease_seconds=0.08)
        self.managers.append(manager)
        self.assertTrue(manager.ensure(100, 42))
        # A stuck provider holds both locks until COM returns. The restoration
        # thread must still complete while this thread retains them.
        with DESKTOP_LOCK, manager._lock:
            self.assertTrue(restored.wait(1), "restoration incorrectly waited for the UIA thread")
            self.assertEqual(self.adapter.values[self.rva], 0)
        self.assertEqual(manager.snapshot()["status"], "lease_expired")

    def test_manual_close_does_not_wait_for_stuck_desktop_operation(self):
        from desktop_coordinator import DESKTOP_LOCK
        manager = self.manager()
        self.assertTrue(manager.ensure(100, 42))
        completed = threading.Event()
        results = []
        def close():
            results.append(manager.close())
            completed.set()
        with DESKTOP_LOCK:
            worker = threading.Thread(target=close, daemon=True)
            worker.start()
            self.assertTrue(completed.wait(1))
        worker.join(1)
        self.assertTrue(results[0]["ok"])
        self.assertTrue(self.adapter.closed)

    def test_timer_close_race_restores_and_releases_handle_once(self):
        manager = self.manager()
        self.assertTrue(manager.ensure(100, 42))
        closes = []
        original = self.adapter.close
        self.adapter.close = lambda: (closes.append(True), original())[-1]
        self.now += 10
        barrier = threading.Barrier(3)
        errors = []
        def run(action):
            try:
                barrier.wait()
                action()
            except BaseException as error:
                errors.append(error)
        workers = [threading.Thread(target=run, args=(action,), daemon=True)
                   for action in (self.timers.items[0].fire, manager.close)]
        for worker in workers:
            worker.start()
        barrier.wait()
        for worker in workers:
            worker.join(1)
            self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(self.adapter.writes, [(self.rva, 1), (self.rva, 0)])
        self.assertEqual(len(closes), 1)

    def test_failed_auto_restore_keeps_handle_then_retries(self):
        manager = self.manager(retry_seconds=0.5)
        self.assertTrue(manager.ensure(100, 42))
        original = self.adapter.write_byte
        failures = [True]
        def write(rva, value):
            if value == 0 and failures[0]:
                failures[0] = False
                return False
            return original(rva, value)
        self.adapter.write_byte = write
        self.now += 10
        self.timers.items[0].fire()
        self.assertEqual(manager.snapshot()["status"], "restore_failed")
        self.assertTrue(manager.snapshot()["active"])
        self.assertFalse(self.adapter.closed)
        self.assertEqual(self.timers.items[-1].delay, 0.5)
        self.now += 0.5
        self.timers.items[-1].fire()
        self.assertEqual(manager.snapshot()["status"], "lease_expired")
        self.assertFalse(manager.snapshot()["active"])
        self.assertTrue(manager.close()["ok"])
        self.assertTrue(self.adapter.closed)

    def test_interruption_in_watchdog_retains_handle_and_can_retry(self):
        manager = self.manager()
        self.assertTrue(manager.ensure(100, 42))
        original = self.adapter.write_byte
        interruptions = [True]
        def write(rva, value):
            if value == 0 and interruptions[0]:
                interruptions[0] = False
                raise KeyboardInterrupt()
            return original(rva, value)
        self.adapter.write_byte = write
        self.now += 10
        self.timers.items[0].fire()
        self.assertEqual(manager.snapshot()["restore_result"]["status"], "restore_interrupted")
        self.assertFalse(self.adapter.closed)
        self.now += 1
        self.timers.items[-1].fire()
        self.assertTrue(manager.snapshot()["restore_result"]["ok"])
        self.assertEqual(self.adapter.values[self.rva], 0)

    def test_retry_worker_failure_is_visible_and_manual_retry_remains_safe(self):
        manager = self.manager()
        self.assertTrue(manager.ensure(100, 42))
        self.adapter.protect = 0x40
        self.timers.fail_start = True
        self.now += 10
        self.timers.items[0].fire()
        snapshot = manager.snapshot()
        self.assertEqual(snapshot["status"], "restore_failed")
        self.assertEqual(snapshot["lease"]["watchdog_error"], "RuntimeError")
        self.assertFalse(snapshot["lease"]["watchdog_running"])
        self.assertFalse(self.adapter.closed)
        self.adapter.protect = 4
        self.assertTrue(manager.close()["ok"])

    def test_retry_timer_construction_failure_is_visible(self):
        manager = self.manager()
        self.assertTrue(manager.ensure(100, 42))
        lease = manager._lease
        self.adapter.protect = 0x40
        def broken_factory(*_args):
            raise OSError("worker unavailable")
        lease._timer_factory = broken_factory
        self.now += 10
        self.timers.items[0].fire()
        self.assertEqual(manager.snapshot()["lease"]["watchdog_error"], "OSError")
        self.assertFalse(manager.snapshot()["lease"]["watchdog_running"])
        self.adapter.protect = 4
        self.assertTrue(manager.close()["ok"])

    def test_handle_close_failure_does_not_discard_owned_state(self):
        manager = self.manager()
        self.assertTrue(manager.ensure(100, 42))
        original = self.adapter.close
        def failed_close():
            raise OSError("close failed")
        self.adapter.close = failed_close
        self.assertFalse(manager.close()["ok"])
        self.assertEqual(manager.snapshot()["restore_result"]["status"], "handle_close_failed")
        self.assertEqual(self.adapter.values[self.rva], 0)
        self.assertIs(manager._adapter, self.adapter)
        self.adapter.close = original
        self.assertTrue(manager.close()["ok"])
        self.assertEqual(self.adapter.writes, [(self.rva, 1), (self.rva, 0)])

    def test_witnessed_identity_change_never_restores_later_matching_identity(self):
        lease = self.apply()
        original_identity = dict(self.adapter.current_identity)
        self.adapter.current_identity["module_base"] += 4096
        self.now += 10
        self.timers.items[0].fire()
        self.assertEqual(lease.restore_result["status"], "identity_changed")
        self.assertFalse(lease.metadata()["watchdog_running"])
        self.adapter.current_identity = original_identity
        self.assertFalse(lease.restore()["ok"])
        self.assertEqual(self.adapter.writes, [(self.rva, 1)])

    def test_witnessed_other_value_never_overwrites_later_external_one(self):
        lease = self.apply()
        self.adapter.values[self.rva] = 2
        self.now += 10
        timer = self.timers.items[0]
        timer.fire()
        self.assertEqual(lease.restore_result["status"], "ownership_lost")
        self.adapter.values[self.rva] = 1
        timer.fire(force=True)
        self.assertFalse(lease.restore()["ok"])
        self.assertEqual(self.adapter.writes, [(self.rva, 1)])

    def test_observed_external_zero_stops_old_watchdog_before_new_one(self):
        lease = self.apply()
        self.adapter.values[self.rva] = 0
        self.now += 10
        timer = self.timers.items[0]
        timer.fire()
        self.assertTrue(lease.restored)
        self.adapter.values[self.rva] = 1
        timer.fire(force=True)
        self.assertTrue(lease.restore()["ok"])
        self.assertEqual(self.adapter.values[self.rva], 1)
        self.assertEqual(self.adapter.writes, [(self.rva, 1)])

    def test_failed_activation_retains_watchdog_for_failed_rollback(self):
        manager = self.manager()
        original = self.adapter.write_byte
        def write(rva, value):
            result = original(rva, value)
            if value == 1:
                self.adapter.protect = 0x40
                return False
            return result
        self.adapter.write_byte = write
        self.assertFalse(manager.ensure(100, 42))
        self.assertTrue(manager.snapshot()["active"])
        self.assertFalse(self.adapter.closed)
        self.adapter.protect = 4
        self.now += 10
        self.timers.items[0].fire()
        self.assertEqual(self.adapter.values[self.rva], 0)
        self.assertTrue(manager.snapshot()["restore_result"]["ok"])


if __name__ == "__main__":
    unittest.main()
