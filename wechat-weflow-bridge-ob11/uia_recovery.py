"""Read-only gate evidence and exact-build temporary UIA recovery leases.

The CLI reads a DLL file and optionally its reviewed profile's process bytes.
Runtime mutation uses a separate transaction and held restoration handle. The
configured shared manager may use it during startup preparation or cold-tree
recovery; the CLI remains read-only. No screen-reader SPI,
registry, remote thread, code patch, UIA, mouse or keyboard operations exist here.
The technical comparison/Qt-log anchor idea is independently implemented from
https://github.com/fanyuantaier/wechatauto-replica/blob/main/wechatauto/uia_driver.py
Matching bytes alone do not prove the meaning of an undocumented runtime flag.
An offline manifest is evidence for review, never permission to change memory.
"""
from __future__ import annotations

import argparse
import bisect
import hashlib
import json
import mmap
import math
import os
import re
import struct
import tempfile
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

import uia_build_profiles as build_profiles

IMAGE_SCN_MEM_EXECUTE = 0x20000000
IMAGE_SCN_MEM_READ = 0x40000000
IMAGE_SCN_MEM_WRITE = 0x80000000
AMD64 = 0x8664
CORE_LABEL = b"qt.accessibility.core\0"
# Compatibility metadata only. Runtime transactions select and pin a profile;
# changing a preferred/default alias must never change an existing lease's RVA.
SUPPORTED_BUILD = MappingProxyType(dict(build_profiles.LEGACY_PROFILE.metadata(),
                                      read_rvas=build_profiles.LEGACY_PROFILE.read_rvas))
MAX_LEASE_SECONDS = 180.0
# Restore before the authorized limit, leaving room for the native identity and
# write/readback checks. This clock never advances on repeated ensure() calls.
LEASE_SECONDS = 170.0
RESTORE_RETRY_SECONDS = 1.0
# test rcx,rcx; jz rel32; cmp byte ptr [rip+rel32],0; jz rel32.
# Branch targets and the RIP-relative data address are decoded independently.
GATE_BYTES = re.compile(rb"\x48\x85\xc9\x0f\x84.{4}\x80\x3d.{4}\x00\x0f\x84.{4}", re.DOTALL)
LEA_BYTES = re.compile(rb"[\x40-\x4f]?\x8d[\x05\x0d\x15\x1d\x25\x2d\x35\x3d].{4}", re.DOTALL)


@dataclass(frozen=True)
class Section:
    name: str
    rva: int
    virtual_size: int
    raw_offset: int
    raw_size: int
    characteristics: int

    @property
    def end_rva(self):
        return self.rva + max(self.virtual_size, self.raw_size)

    def report(self):
        return {"name": self.name, "rva": self.rva, "end_rva": self.end_rva,
                "raw_offset": self.raw_offset, "raw_size": self.raw_size,
                "virtual_size": self.virtual_size,
                "readable": bool(self.characteristics & IMAGE_SCN_MEM_READ),
                "writable": bool(self.characteristics & IMAGE_SCN_MEM_WRITE),
                "executable": bool(self.characteristics & IMAGE_SCN_MEM_EXECUTE)}


class PEImage:
    """Bounded AMD64 PE32+ parser; all reads are from the supplied byte buffer."""
    def __init__(self, data):
        self.data = data
        if len(data) < 64 or data[:2] != b"MZ":
            raise ValueError("DLL 不是有效的 MZ/PE 文件")
        pe = self.u32(60)
        self.require(pe, 24)
        if data[pe:pe + 4] != b"PE\0\0":
            raise ValueError("PE 签名无效")
        coff = pe + 4
        self.machine = self.u16(coff)
        count, optional_size = self.u16(coff + 2), self.u16(coff + 16)
        if self.machine != AMD64 or not 1 <= count <= 96 or optional_size < 112:
            raise ValueError("仅支持结构完整的 AMD64 PE32+ DLL")
        opt = coff + 20
        self.require(opt, optional_size)
        if self.u16(opt) != 0x20B:
            raise ValueError("DLL 不是 PE32+")
        self.size_of_image = self.u32(opt + 56)
        if self.size_of_image < 4096:
            raise ValueError("SizeOfImage 无效")
        directory_count = self.u32(opt + 108)
        if directory_count > 16 or 112 + directory_count * 8 > optional_size:
            raise ValueError("PE 数据目录越界")
        self.exception_rva = self.u32(opt + 136) if directory_count > 3 else 0
        self.exception_size = self.u32(opt + 140) if directory_count > 3 else 0
        self.sections = []
        section_table = opt + optional_size
        self.require(section_table, count * 40)
        for i in range(count):
            off = section_table + i * 40
            name = bytes(data[off:off + 8]).split(b"\0", 1)[0].decode("ascii", "replace")
            virtual_size, rva, raw_size, raw_offset = struct.unpack_from("<IIII", data, off + 8)
            sec = Section(name, rva, virtual_size, raw_offset, raw_size, self.u32(off + 36))
            if sec.end_rva > self.size_of_image or rva < 1:
                raise ValueError("PE 节虚拟地址越界")
            if raw_size:
                self.require(raw_offset, raw_size)
            self.sections.append(sec)
        ordered = sorted(self.sections, key=lambda sec: sec.rva)
        if any(left.end_rva > right.rva for left, right in zip(ordered, ordered[1:])):
            raise ValueError("PE 节虚拟地址重叠")
        self.functions = self._read_functions()
        self._function_starts = [item[0] for item in self.functions]

    def require(self, offset, size):
        if offset < 0 or size < 0 or offset > len(self.data) - size:
            raise ValueError("PE 数据读取越界")

    def u16(self, offset):
        self.require(offset, 2)
        return struct.unpack_from("<H", self.data, offset)[0]

    def u32(self, offset):
        self.require(offset, 4)
        return struct.unpack_from("<I", self.data, offset)[0]

    def section_at(self, rva, size=1):
        if size < 1:
            return None
        return next((s for s in self.sections if s.rva <= rva and rva + size <= s.end_rva), None)

    def offset_at(self, rva, size=1):
        sec = self.section_at(rva, size)
        if sec is None or rva + size > sec.rva + sec.raw_size:
            return None
        return sec.raw_offset + rva - sec.rva

    def rva_at(self, offset, size=1):
        for sec in self.sections:
            if sec.raw_offset <= offset and offset + size <= sec.raw_offset + sec.raw_size:
                return sec.rva + offset - sec.raw_offset
        return None

    def _read_functions(self):
        if not self.exception_rva and not self.exception_size:
            return []
        if self.exception_size < 12 or self.exception_size % 12 or self.exception_size > 12_000_000:
            raise ValueError("PE 异常函数表长度无效")
        off = self.offset_at(self.exception_rva, self.exception_size)
        if off is None:
            raise ValueError("PE 异常函数表越界")
        funcs = []
        for ptr in range(off, off + self.exception_size, 12):
            start, end, unwind = struct.unpack_from("<III", self.data, ptr)
            sec = self.section_at(start, end - start) if end > start else None
            if sec is None or not sec.characteristics & IMAGE_SCN_MEM_EXECUTE or self.section_at(unwind) is None:
                raise ValueError("PE 异常函数表记录无效")
            funcs.append((start, end, unwind))
        funcs.sort()
        return funcs

    def function_at(self, rva):
        idx = bisect.bisect_right(self._function_starts, rva) - 1
        if idx >= 0 and self.functions[idx][0] <= rva < self.functions[idx][1]:
            begin, end, unwind = self.functions[idx]
            return {"begin_rva": begin, "end_rva": end, "unwind_rva": unwind}
        return None


def _file_version(path):
    """Read VersionInfo from disk only; unavailable on non-Windows test hosts."""
    if os.name != "nt":
        return ""
    import ctypes
    from ctypes import wintypes
    dll = ctypes.WinDLL("version", use_last_error=True)
    dll.GetFileVersionInfoSizeW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD)]
    dll.GetFileVersionInfoSizeW.restype = wintypes.DWORD
    ignored = wintypes.DWORD()
    size = dll.GetFileVersionInfoSizeW(str(path), ctypes.byref(ignored))
    if not size or size > 16 * 1024 * 1024:
        return ""
    blob = ctypes.create_string_buffer(size)
    dll.GetFileVersionInfoW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p]
    dll.GetFileVersionInfoW.restype = wintypes.BOOL
    if not dll.GetFileVersionInfoW(str(path), 0, size, blob):
        return ""
    pointer, length = ctypes.c_void_p(), wintypes.UINT()
    dll.VerQueryValueW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.UINT)]
    dll.VerQueryValueW.restype = wintypes.BOOL
    if not dll.VerQueryValueW(blob, "\\", ctypes.byref(pointer), ctypes.byref(length)) or length.value < 52:
        return ""
    info = ctypes.cast(pointer, ctypes.POINTER(wintypes.DWORD))
    if info[0] != 0xFEEF04BD:
        return ""
    high, low = info[2], info[3]
    return f"{high >> 16}.{high & 65535}.{low >> 16}.{low & 65535}"


def inspect_image(data):
    pe = PEImage(data)
    labels = []
    ptr = data.find(CORE_LABEL)
    while ptr >= 0:
        rva = pe.rva_at(ptr, len(CORE_LABEL))
        sec = pe.section_at(rva, len(CORE_LABEL)) if rva is not None else None
        if sec and sec.characteristics & IMAGE_SCN_MEM_READ and not sec.characteristics & IMAGE_SCN_MEM_EXECUTE:
            labels.append(rva)
        ptr = data.find(CORE_LABEL, ptr + len(CORE_LABEL))
    label_set, refs = set(labels), []
    for sec in pe.sections:
        if not sec.characteristics & IMAGE_SCN_MEM_EXECUTE:
            continue
        for match in LEA_BYTES.finditer(data, sec.raw_offset, sec.raw_offset + sec.raw_size):
            raw = match.group()
            target = sec.rva + match.end() - sec.raw_offset + struct.unpack_from("<i", raw, len(raw) - 4)[0]
            if target in label_set:
                rva = sec.rva + match.start() - sec.raw_offset
                refs.append({"instruction_rva": rva, "label_rva": target,
                             "bytes": raw.hex(), "function": pe.function_at(rva)})
    refs.sort(key=lambda item: item["instruction_rva"])
    candidates = {}
    for sec in pe.sections:
        if not sec.characteristics & IMAGE_SCN_MEM_EXECUTE:
            continue
        for match in GATE_BYTES.finditer(data, sec.raw_offset, sec.raw_offset + sec.raw_size):
            start = sec.rva + match.start() - sec.raw_offset
            target = start + 16 + struct.unpack_from("<i", data, match.start() + 11)[0]
            target_sec = pe.section_at(target)
            if target_sec is None:
                continue
            first_branch = start + 9 + struct.unpack_from("<i", data, match.start() + 5)[0]
            second_branch = start + 22 + struct.unpack_from("<i", data, match.start() + 18)[0]
            fn = pe.function_at(start)
            same_fn = [ref for ref in refs if fn is not None and ref["function"] == fn]
            nearest = sorted(refs, key=lambda ref: abs(ref["instruction_rva"] - start))[:3]
            flag_off = pe.offset_at(target)
            value = int(data[flag_off]) if flag_off is not None else 0
            branch_in_fn = bool(fn and all(fn["begin_rva"] <= branch < fn["end_rva"] for branch in (first_branch, second_branch)))
            structural = bool(target_sec.characteristics & IMAGE_SCN_MEM_WRITE and
                              not target_sec.characteristics & IMAGE_SCN_MEM_EXECUTE and value in (0, 1))
            record = candidates.setdefault(target, {"flag_rva": target, "flag_rva_hex": hex(target),
                "section": target_sec.report(), "file_initial_byte": value,
                "file_byte_backed": flag_off is not None, "structural_data_flag": structural,
                "comparisons": []})
            context_begin = max(sec.raw_offset, match.start() - 48)
            context_end = min(sec.raw_offset + sec.raw_size, match.end() + 48)
            record["comparisons"].append({"pattern_rva": start, "cmp_rva": start + 9,
                "pattern_bytes": match.group().hex(), "branch_targets": [first_branch, second_branch],
                "function": fn, "branches_within_function": branch_in_fn,
                "same_function_anchor_refs": same_fn,
                "nearest_anchor_refs": [dict(ref, distance=abs(ref["instruction_rva"] - start)) for ref in nearest],
                "context_rva": sec.rva + context_begin - sec.raw_offset,
                "context_hex": bytes(data[context_begin:context_end]).hex()})
    items = sorted(candidates.values(), key=lambda item: item["flag_rva"])
    strong = [item for item in items if item["structural_data_flag"] and any(
        comparison["same_function_anchor_refs"] and comparison["branches_within_function"]
        for comparison in item["comparisons"])]
    return {"architecture": "AMD64", "size_of_image": pe.size_of_image,
            "sections": [sec.report() for sec in pe.sections], "core_label_rvas": labels,
            "core_anchor_refs": refs, "candidates": items,
            "strict_candidates": [item["flag_rva"] for item in strong],
            "status": "unique_structural_candidate" if len(strong) == 1 else "review_required",
            "unique_candidate_rva": strong[0]["flag_rva"] if len(strong) == 1 else None,
            "runtime_change_authorized": False,
            "limitations": ["Raw instruction signatures may match inside other instructions; offline disassembly review is required.",
                            "A Qt logging reference and data comparison do not prove an undocumented flag's semantics.",
                            "No runtime module, memory protection, process identity, or control tree has been checked."]}


def inspect_dll(path):
    path = Path(path).resolve(strict=True)
    if not path.is_file() or path.suffix.lower() != ".dll":
        raise ValueError("探测目标必须为 DLL 文件")
    before = path.stat()
    if before.st_size < 64 or before.st_size > 1024 * 1024 * 1024:
        raise ValueError("DLL 文件大小无效或超过 1 GiB 读取上限")
    with path.open("rb") as source, mmap.mmap(source.fileno(), 0, access=mmap.ACCESS_READ) as data:
        digest = hashlib.sha256(data).hexdigest()
        evidence = inspect_image(data)
    after = path.stat()
    if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
        raise ValueError("探测期间 DLL 文件发生变化；结果已丢弃")
    return {"manifest_version": 1, "purpose": "offline_read_only_uia_gate_evidence",
            "dll_path": str(path), "sha256": digest, "file_size": before.st_size,
            "file_mtime_ns": before.st_mtime_ns, "file_version": _file_version(path),
            "source_reference": "https://github.com/fanyuantaier/wechatauto-replica/blob/main/wechatauto/uia_driver.py",
            **evidence}


def write_manifest(evidence, output):
    output = Path(output).resolve()
    if output.suffix.lower() != ".json" or output == Path(evidence["dll_path"]).resolve():
        raise ValueError("清单输出必须为单独的 JSON 文件")
    output.parent.mkdir(parents=True, exist_ok=True)
    fd, staging = tempfile.mkstemp(prefix="uia-probe-", suffix=".tmp", dir=output.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(evidence, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(staging, output)
    finally:
        if os.path.exists(staging):
            os.unlink(staging)
    return output


def _canonical(path):
    return os.path.normcase(os.path.realpath(os.fspath(path)))


class BuildVerificationError(ValueError):
    """A fixed, public reason; never include arbitrary identity values."""
    def __init__(self, code, reason):
        super().__init__(reason)
        self.code = code


def _safe_observed_build(identity):
    """Bounded public build metadata only, no paths or process identifiers.

    Architecture is reported only from observed PE identity metadata, never
    inferred from a known build's expected architecture.
    """
    identity = identity if isinstance(identity, Mapping) else {}
    version = identity.get("file_version")
    valid_version = build_profiles.valid_version(version)
    digest = identity.get("sha256")
    valid_digest = build_profiles.valid_sha256(digest)
    size = identity.get("module_size")
    valid_size = build_profiles.valid_image_size(size)
    architecture = identity.get("architecture")
    return {"file_version": version if valid_version else None,
            "sha256": digest if valid_digest else None,
            "size_of_image": size if valid_size else None,
            "architecture": architecture if architecture in ("AMD64", "I386", "ARM64", "ARM") else "unknown"}


def _safe_recovery_error(error):
    if isinstance(error, BuildVerificationError):
        return str(error)
    if isinstance(error, OSError):
        code = getattr(error, "winerror", None) or getattr(error, "errno", None)
        suffix = f"（系统错误 {code}）" if isinstance(code, int) and not isinstance(code, bool) and 0 < code <= 65535 else ""
        return f"读取微信进程或模块失败{suffix}；请查看恢复状态。"
    if isinstance(error, ValueError):
        return "微信进程、模块或状态安全核验未通过；请查看恢复状态与构建对照。"
    return "微信无障碍恢复未完成；请查看恢复状态与构建对照。"


BUILD_REASONS = {
    "version_unavailable": "未能读取微信模块的有效文件版本；已停止恢复，未读取或修改状态字节。",
    "version_mismatch": "当前微信模块版本尚未匹配内置已复核恢复方案；已停止恢复，未读取或修改状态字节。",
    "sha256_unavailable": "未能取得微信模块的有效 SHA256；已停止恢复，未读取或修改状态字节。",
    "sha256_mismatch": "当前版本存在已复核方案，但此模块 SHA256 未匹配任何方案；已停止恢复，未读取或修改状态字节。",
    "architecture_unavailable": "未能读取微信模块的真实架构；已停止恢复，未读取或修改状态字节。",
    "architecture_mismatch": "微信模块架构与已复核方案不同；已停止恢复，未读取或修改状态字节。",
    "image_size_unavailable": "未能取得微信模块的有效映像大小；已停止恢复，未读取或修改状态字节。",
    "image_size_mismatch": "微信模块映像大小与已复核方案不同；已停止恢复，未读取或修改状态字节。",
    "ambiguous_profile": "模块匹配多个恢复方案，无法确认唯一地址；已停止恢复，未读取或修改状态字节。",
}


def _select_profile(identity):
    try:
        return build_profiles.select_profile(identity)
    except build_profiles.ProfileSelectionError as error:
        raise BuildVerificationError(error.code, BUILD_REASONS[error.code]) from None


def _validated_build(evidence):
    evidence = evidence if isinstance(evidence, Mapping) else {}
    profile = _select_profile(evidence)
    if evidence.get("profile_id", profile.profile_id) != profile.profile_id:
        raise ValueError("清单中的构建方案标识与模块身份不一致")
    if ("read_rvas" in evidence and (not isinstance(evidence["read_rvas"], (list, tuple))
            or any(not isinstance(rva, int) or isinstance(rva, bool) for rva in evidence["read_rvas"])
            or tuple(evidence["read_rvas"]) != profile.read_rvas)
            or "active_rva" in evidence and (not isinstance(evidence["active_rva"], int)
            or isinstance(evidence["active_rva"], bool) or evidence["active_rva"] != profile.active_rva)):
        raise ValueError("清单地址与内置已复核方案不一致")
    entries = evidence.get("candidates", [])
    if not isinstance(entries, (tuple, list)) or any(not isinstance(item, Mapping) for item in entries):
        raise ValueError("清单中的状态候选格式无效")
    candidates = {}
    for item in entries:
        rva = item.get("flag_rva")
        if isinstance(rva, int) and not isinstance(rva, bool) and rva in profile.read_rvas:
            if rva in candidates:
                raise ValueError("清单中的已复核地址重复")
            candidates[rva] = item
    for rva in profile.read_rvas:
        item = candidates.get(rva, {})
        section = item.get("section", {})
        if (not isinstance(section, Mapping) or item.get("structural_data_flag") is not True
                or section.get("writable") is not True or section.get("executable") is not False):
            raise ValueError("清单中的已复核地址不是不可执行数据段")
    return profile


def _disk_pe_architecture(stream, file_size):
    """Read bounded PE identity bytes from the same hashed disk file handle."""
    stream.seek(0)
    header = stream.read(64)
    if len(header) != 64 or header[:2] != b"MZ":
        return "unknown"
    offset = struct.unpack_from("<I", header, 60)[0]
    if offset < 64 or offset > min(file_size - 6, 16 * 1024 * 1024):
        return "unknown"
    stream.seek(offset)
    signature = stream.read(6)
    if len(signature) != 6 or signature[:4] != b"PE\0\0":
        return "unknown"
    return {0x8664: "AMD64", 0x14C: "I386", 0xAA64: "ARM64", 0x1C0: "ARM", 0x1C4: "ARM"}.get(
        struct.unpack_from("<H", signature, 4)[0], "unknown")


class NativeProcessProbe:
    """A held process handle; read-only by default, exact build/RVA writes only.

    ``permit_write=True`` grants the separate transaction API access, never the
    CLI. Keeping this handle also prevents restoration through a reused PID.
    """
    def __init__(self, pid, dll_path, *, permit_write=False):
        if os.name != "nt":
            raise RuntimeError("运行态诊断仅支持 Windows")
        import ctypes
        from ctypes import wintypes
        if ctypes.sizeof(ctypes.c_void_p) != 8:
            raise ValueError("请使用 64 位 Python 诊断 64 位微信")
        self.ctypes, self.wintypes = ctypes, wintypes
        self.pid, self.dll_path, self.permit_write = int(pid), _canonical(dll_path) if dll_path else None, bool(permit_write)
        if isinstance(pid, bool) or self.pid < 1:
            raise ValueError("需要已核验的正整数 PID")
        self._bound_identity, self._may_have_changed, self._restoration_complete = None, False, False
        self._profile, self._read_identity = None, None
        self._identity_cache, self._identity_cache_key = None, None
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        k = self.kernel
        k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k.OpenProcess.restype = wintypes.HANDLE
        k.CloseHandle.argtypes = [wintypes.HANDLE]
        k.CloseHandle.restype = wintypes.BOOL
        k.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
        k.QueryFullProcessImageNameW.restype = wintypes.BOOL
        k.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
        k.GetProcessTimes.restype = wintypes.BOOL
        k.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        k.GetExitCodeProcess.restype = wintypes.BOOL
        k.ReadProcessMemory.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
        k.ReadProcessMemory.restype = wintypes.BOOL
        rights = 0x0400 | 0x0010
        if self.permit_write:
            rights |= 0x0020 | 0x0008
            k.WriteProcessMemory.argtypes = k.ReadProcessMemory.argtypes
            k.WriteProcessMemory.restype = wintypes.BOOL
        self.handle = k.OpenProcess(rights, False, self.pid)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None

    def _process(self):
        c, w, k = self.ctypes, self.wintypes, self.kernel
        exit_code = w.DWORD()
        if not self.handle or not k.GetExitCodeProcess(self.handle, c.byref(exit_code)) or exit_code.value != 259:
            raise RuntimeError("已核验的微信进程已退出")
        times = [w.FILETIME() for _ in range(4)]
        if not k.GetProcessTimes(self.handle, *(c.byref(item) for item in times)):
            raise c.WinError(c.get_last_error())
        size, executable = w.DWORD(32768), c.create_unicode_buffer(32768)
        if not k.QueryFullProcessImageNameW(self.handle, 0, executable, c.byref(size)):
            raise c.WinError(c.get_last_error())
        return {"pid": self.pid, "creation_filetime": (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime,
                "executable": _canonical(executable.value)}

    def process_exited(self):
        value = self.wintypes.DWORD()
        if not self.handle or not self.kernel.GetExitCodeProcess(self.handle, self.ctypes.byref(value)):
            return False
        return value.value != 259

    def _module(self):
        c, w, k = self.ctypes, self.wintypes, self.kernel
        class Entry(c.Structure):
            _fields_ = [("dwSize", w.DWORD), ("th32ModuleID", w.DWORD), ("th32ProcessID", w.DWORD),
                ("GlblcntUsage", w.DWORD), ("ProccntUsage", w.DWORD), ("modBaseAddr", c.c_void_p),
                ("modBaseSize", w.DWORD), ("hModule", w.HMODULE), ("szModule", w.WCHAR * 256), ("szExePath", w.WCHAR * 260)]
        k.CreateToolhelp32Snapshot.argtypes = [w.DWORD, w.DWORD]
        k.CreateToolhelp32Snapshot.restype = w.HANDLE
        k.Module32FirstW.argtypes = [w.HANDLE, c.POINTER(Entry)]
        k.Module32FirstW.restype = w.BOOL
        k.Module32NextW.argtypes = k.Module32FirstW.argtypes
        k.Module32NextW.restype = w.BOOL
        snapshot = k.CreateToolhelp32Snapshot(0x08 | 0x10, self.pid)
        if snapshot == c.c_void_p(-1).value:
            raise c.WinError(c.get_last_error())
        matches = []
        try:
            entry = Entry(); entry.dwSize = c.sizeof(entry)
            more = k.Module32FirstW(snapshot, c.byref(entry))
            while more:
                if entry.szModule.lower() == "weixin.dll" and (self.dll_path is None or _canonical(entry.szExePath) == self.dll_path):
                    matches.append({"dll_path": _canonical(entry.szExePath), "module_base": int(entry.modBaseAddr),
                                    "module_size": int(entry.modBaseSize)})
                more = k.Module32NextW(snapshot, c.byref(entry))
        finally:
            k.CloseHandle(snapshot)
        if len(matches) != 1:
            raise ValueError("该进程未唯一加载所核验的 Weixin.dll")
        return matches[0]

    def identity(self, full_hash=True):
        before = self._process()
        module = self._module()
        dll = Path(module["dll_path"])
        if self.dll_path is None:
            self.dll_path = module["dll_path"]
        stat = dll.stat()
        cache_key = (tuple(sorted(before.items())), tuple(sorted(module.items())), stat.st_size, stat.st_mtime_ns, stat.st_ino)
        if not full_hash and self._identity_cache_key == cache_key and self._identity_cache is not None:
            if self._process() != before:
                raise RuntimeError("进程身份在快速核验期间变化")
            return dict(self._identity_cache)
        with dll.open("rb") as stream:
            hasher = hashlib.sha256()
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                hasher.update(chunk)
            digest = hasher.hexdigest()
            architecture = _disk_pe_architecture(stream, stat.st_size)
        version = _file_version(dll)
        after_stat = dll.stat()
        after = self._process()
        if before != after or module != self._module() or (stat.st_size, stat.st_mtime_ns, stat.st_ino) != (after_stat.st_size, after_stat.st_mtime_ns, after_stat.st_ino):
            raise RuntimeError("进程或 DLL 文件在身份核验期间变化")
        result = {**before, **module, "sha256": digest, "file_version": version, "architecture": architecture}
        self._identity_cache, self._identity_cache_key = dict(result), cache_key
        return result

    def fast_identity(self):
        return self.identity(full_hash=False)

    def _address(self, rva):
        profile, identity = getattr(self, "_profile", None), getattr(self, "_read_identity", None)
        if profile is None or identity is None:
            raise PermissionError("状态读取未绑定已复核构建与完整进程身份")
        if not isinstance(rva, int) or isinstance(rva, bool) or rva not in profile.read_rvas:
            raise ValueError("只允许本事务构建方案已复核的状态地址")
        if self.fast_identity() != identity or not profile.matches(identity):
            raise ValueError("状态读取前进程或模块身份已变化")
        module = self._module()
        if any(module.get(key) != identity.get(key) for key in ("module_base", "module_size", "dll_path")) or rva >= module["module_size"]:
            raise ValueError("状态地址超出模块范围")
        return module["module_base"] + rva

    def _bind_read_identity(self, identity, profile):
        if not isinstance(profile, build_profiles.BuildProfile) or _select_profile(identity) != profile:
            raise ValueError("读取方案与已复核模块身份不匹配")
        _check_identity(identity, {**profile.metadata(), "dll_path": identity.get("dll_path")},
                        identity["pid"], identity["executable"], identity["creation_filetime"])
        existing = getattr(self, "_profile", None)
        if existing is not None and (existing != profile or getattr(self, "_read_identity", None) != identity):
            raise PermissionError("同一进程句柄不能重新绑定其他模块事务")
        if self.fast_identity() != identity:
            raise ValueError("绑定读取前进程或模块身份已变化")
        self._profile, self._read_identity = profile, dict(identity)

    def page_info(self, rva):
        c, w, k = self.ctypes, self.wintypes, self.kernel
        class MBI(c.Structure):
            _fields_ = [("BaseAddress", c.c_void_p), ("AllocationBase", c.c_void_p),
                ("AllocationProtect", w.DWORD), ("PartitionId", w.WORD), ("RegionSize", c.c_size_t),
                ("State", w.DWORD), ("Protect", w.DWORD), ("Type", w.DWORD)]
        k.VirtualQueryEx.argtypes = [w.HANDLE, c.c_void_p, c.POINTER(MBI), c.c_size_t]
        k.VirtualQueryEx.restype = c.c_size_t
        page = MBI()
        if k.VirtualQueryEx(self.handle, self._address(rva), c.byref(page), c.sizeof(page)) != c.sizeof(page):
            raise c.WinError(c.get_last_error())
        return {"base": int(page.BaseAddress or 0), "allocation_base": int(page.AllocationBase or 0),
                "region_size": int(page.RegionSize), "state": int(page.State),
                "protect": int(page.Protect), "type": int(page.Type)}

    def read_byte(self, rva):
        c = self.ctypes
        value, count = c.c_ubyte(), c.c_size_t()
        if not self.kernel.ReadProcessMemory(self.handle, self._address(rva), c.byref(value), 1, c.byref(count)) or count.value != 1:
            raise RuntimeError("无法完整读取指定的单个状态字节")
        return int(value.value)

    def write_byte(self, rva, value):
        profile = getattr(self, "_profile", None)
        if (not self.permit_write or profile is None or not isinstance(rva, int) or isinstance(rva, bool)
                or rva != profile.active_rva or not isinstance(value, int) or isinstance(value, bool) or value not in (0, 1)):
            raise PermissionError("运行状态写入未授权或地址/值超出允许范围")
        if not self._bound_identity or (value == 0 and not self._may_have_changed):
            raise PermissionError("写入未绑定核验事务，或没有本事务的待恢复状态")
        if (value == 1 and self._may_have_changed) or self._restoration_complete:
            raise PermissionError("该原生事务已尝试激活或已经恢复；不能复用写权限")
        identity = self.identity()
        if identity != self._bound_identity or not profile.matches(identity):
            raise ValueError("原生写入前的进程/模块核验失败")
        if not _writable_image_page(self.page_info(rva), identity, rva):
            raise ValueError("原生写入前页面权限发生变化")
        if self.read_byte(rva) != (0 if value == 1 else 1):
            raise ValueError("状态值与事务预期转换不符")
        address = self._address(rva)
        if not _writable_image_page(self.page_info(rva), identity, rva):
            raise ValueError("提交前页面不再是不可执行的可写映像数据页")
        c = self.ctypes
        byte, count = c.c_ubyte(value), c.c_size_t()
        self._may_have_changed = True
        ok = self.kernel.WriteProcessMemory(self.handle, address, c.byref(byte), 1, c.byref(count))
        if value == 0 and ok and count.value == 1:
            self._restoration_complete = True
        return bool(ok and count.value == 1)

    def _bind_transaction(self, identity, evidence):
        if not self.permit_write:
            raise PermissionError("该原生进程句柄仅允许读取")
        profile = _validated_build(evidence)
        _check_identity(identity, evidence, identity["pid"], identity["executable"], identity["creation_filetime"])
        if self.fast_identity() != identity or self._may_have_changed:
            raise ValueError("原生句柄不匹配或已有未恢复的事务")
        self._bind_read_identity(identity, profile)
        self._bound_identity = dict(identity)

    def write_attempted(self):
        return self._may_have_changed


def _check_identity(current, evidence, pid, executable, creation):
    if isinstance(pid, bool) or int(pid) < 1 or isinstance(creation, bool) or int(creation) < 1 or os.path.basename(executable).lower() != "weixin.exe":
        raise ValueError("仅允许已核验的微信进程身份")
    required = {"pid": int(pid), "creation_filetime": int(creation), "executable": _canonical(executable),
                "dll_path": _canonical(evidence["dll_path"]), "sha256": evidence["sha256"],
                "file_version": evidence["file_version"], "architecture": evidence["architecture"],
                "module_size": evidence["size_of_image"]}
    if any(current.get(key) != value for key, value in required.items()) or not current.get("module_base"):
        raise ValueError("进程启动时间、程序路径或模块身份与核验清单不符")


def _fast_identity(adapter):
    fast = getattr(adapter, "fast_identity", None)
    return fast() if callable(fast) else adapter.identity()


def _writable_image_page(page, identity, rva):
    address = identity["module_base"] + rva
    return (page.get("state") == 0x1000 and page.get("type") == 0x1000000 and
            page.get("protect") in (0x04, 0x08) and page.get("allocation_base") == identity["module_base"] and
            page.get("base", 0) <= address < page.get("base", 0) + page.get("region_size", 0))


def inspect_process(evidence, pid, expected_executable, expected_creation, *, adapter_factory=NativeProcessProbe):
    """Read only the selected profile's reviewed bytes, with identity checks."""
    profile = _validated_build(evidence)
    with adapter_factory(pid, evidence["dll_path"]) as adapter:
        identity = adapter.identity()
        _check_identity(identity, evidence, pid, expected_executable, expected_creation)
        bind = getattr(adapter, "_bind_read_identity", None)
        if callable(bind):
            bind(identity, profile)
        states = []
        for rva in profile.read_rvas:
            page = adapter.page_info(rva)
            states.append({"rva": rva, "rva_hex": hex(rva), "value": adapter.read_byte(rva),
                           "page": page, "writable_non_executable_image": _writable_image_page(page, identity, rva)})
        if adapter.identity() != identity:
            raise ValueError("读取期间进程或已加载模块身份改变，结果已丢弃")
    return {"purpose": "read_only_profile_state_bytes", "identity": identity, "profile": profile.metadata(), "states": states,
            "ui_controls_verified": False, "runtime_change_authorized": False,
            "limitation": "SHA256 verifies the mapped module's disk file, not the entire in-memory image or undocumented flag semantics."}


def pid_from_hwnd(hwnd):
    """Read HWND→PID metadata only; sends no window message and changes no UI."""
    if os.name != "nt" or isinstance(hwnd, bool) or int(hwnd) < 1:
        raise ValueError("需要 Windows 上已核验的主窗口句柄")
    import ctypes
    from ctypes import wintypes
    user = ctypes.WinDLL("user32", use_last_error=True)
    user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user.GetWindowThreadProcessId.restype = wintypes.DWORD
    pid = wintypes.DWORD()
    if not user.GetWindowThreadProcessId(wintypes.HWND(hwnd), ctypes.byref(pid)) or not pid.value:
        raise ctypes.WinError(ctypes.get_last_error())
    return int(pid.value)


class GateLease:
    """Temporary change; callers must keep the adapter handle and restore it.

    A matching byte does not establish exclusive ownership against other tools.
    Restore is best-effort and refuses a changed process/module/non-1 byte.
    """
    def __init__(self, adapter, identity, *, profile=None, lease_seconds=LEASE_SECONDS,
                 retry_seconds=RESTORE_RETRY_SECONDS, clock=None, timer_factory=None):
        if isinstance(lease_seconds, bool) or not math.isfinite(float(lease_seconds)) or not 0 < float(lease_seconds) <= LEASE_SECONDS:
            raise ValueError("临时状态租期必须大于 0 且不超过 170 秒")
        if isinstance(retry_seconds, bool) or not math.isfinite(float(retry_seconds)) or not 0 < float(retry_seconds) <= 5:
            raise ValueError("恢复重试间隔必须大于 0 且不超过 5 秒")
        self.adapter, self.identity = adapter, dict(identity)
        selected = _select_profile(identity)
        if profile is not None and profile != selected:
            raise ValueError("临时状态事务的方案与模块身份不一致")
        self.profile = selected
        self.rva, self.original = self.profile.active_rva, 0
        self.applied, self.restored, self._write_attempted = False, False, False
        # Never take DESKTOP_LOCK here: an unresponsive UIA provider can hold it
        # indefinitely. This lock protects only short native memory operations.
        self._lock = threading.RLock()
        self._clock, self._timer_factory = clock or time.monotonic, timer_factory or threading.Timer
        self._lease_seconds, self._retry_seconds = float(lease_seconds), float(retry_seconds)
        self._started_at = self._deadline = None
        self._timer, self._timer_epoch = None, 0
        self.expired, self.restore_result, self.watchdog_error = False, None, ""
        self._ownership_lost = None

    def metadata(self):
        # Metadata only, also usable while a restore is running. No COM or
        # process query, and no blocking acquisition of the memory lock.
        return {"lease_seconds": self._lease_seconds, "max_lease_seconds": MAX_LEASE_SECONDS,
                "remaining_seconds": max(0.0, self._deadline - self._clock()) if self._deadline is not None else None,
                "expired": self.expired, "restored": self.restored,
                "watchdog_running": self._timer is not None, "watchdog_error": self.watchdog_error,
                "restore_result": dict(self.restore_result) if self.restore_result else None}

    def _stop_watchdog_locked(self):
        self._timer_epoch += 1
        timer, self._timer = self._timer, None
        if timer is not None:
            timer.cancel()

    def _schedule_locked(self, delay):
        self._stop_watchdog_locked()
        epoch = self._timer_epoch
        timer = None
        try:
            timer = self._timer_factory(max(0.0, delay), lambda: self._watchdog_due(epoch))
            timer.daemon = True
            self._timer = timer
            timer.start()
            self.watchdog_error = ""
        except BaseException as error:
            self._timer = None
            self._timer_epoch += 1
            if timer is not None:
                timer.cancel()
            self.watchdog_error = type(error).__name__
            raise

    def _arm_locked(self):
        if self._started_at is not None:
            raise RuntimeError("同一临时状态事务不能延长租期或重新激活")
        self._started_at = self._clock()
        self._deadline = self._started_at + self._lease_seconds
        # Armed before the physical write: activation readback or subsequent
        # COM calls cannot postpone creation of the restoration worker.
        self._schedule_locked(self._lease_seconds)

    def _watchdog_due(self, epoch):
        with self._lock:
            if epoch != self._timer_epoch or self.restored:
                return  # A canceled callback must never touch a newer lease.
            remaining = self._deadline - self._clock()
            if remaining > 0:
                try:
                    self._schedule_locked(remaining)
                except BaseException:
                    pass  # Never conceal a missing worker from metadata.
                return
            self.expired = True
            try:
                result = self._restore_locked()
            except BaseException as error:
                result = {"ok": False, "status": "restore_interrupted", "wrote": False,
                          "error": type(error).__name__}
            self.restore_result = dict(result)
            # Keep the held handle and retry a transient error. A witnessed
            # identity/value ownership change is terminal; never overwrite a
            # later activation that merely happens to be 1 again.
            if not self.restored and not self._ownership_lost:
                try:
                    self._schedule_locked(self._retry_seconds)
                except BaseException:
                    pass  # Visible watchdog_error; ownership remains retained.
            else:
                self._stop_watchdog_locked()

    def restore(self):
        with self._lock:
            result = self._restore_locked()
            self.restore_result = dict(result)
            if self.restored or result["ok"]:
                self._stop_watchdog_locked()
            return result

    def _restore_locked(self):
        if self.restored:
            return {"ok": True, "status": "already_restored", "wrote": False}
        attempted = getattr(self.adapter, "write_attempted", None)
        if not self._write_attempted or (callable(attempted) and not attempted()):
            self.restored = True
            return {"ok": True, "status": "write_not_attempted", "wrote": False}
        try:
            exited = getattr(self.adapter, "process_exited", None)
            if callable(exited) and exited():
                self.restored = True
                return {"ok": True, "status": "process_exited", "wrote": False}
            if self._ownership_lost:
                return {"ok": False, "status": self._ownership_lost, "wrote": False}
            if _fast_identity(self.adapter) != self.identity:
                self._ownership_lost = "identity_changed"
                return {"ok": False, "status": "identity_changed", "wrote": False}
            page = self.adapter.page_info(self.rva)
            if not _writable_image_page(page, self.identity, self.rva):
                return {"ok": False, "status": "page_changed", "wrote": False}
            current = self.adapter.read_byte(self.rva)
            if current == self.original:
                self.restored = True
                return {"ok": True, "status": "already_restored", "wrote": False}
            if current != 1:
                self._ownership_lost = "ownership_lost"
                return {"ok": False, "status": "ownership_lost", "wrote": False}
            if _fast_identity(self.adapter) != self.identity:
                self._ownership_lost = "identity_changed"
                return {"ok": False, "status": "identity_changed", "wrote": False}
            if not _writable_image_page(self.adapter.page_info(self.rva), self.identity, self.rva):
                return {"ok": False, "status": "identity_or_page_changed", "wrote": False}
            ok = self.adapter.write_byte(self.rva, self.original)
            readback = self.adapter.read_byte(self.rva)
            self.restored = bool(readback == self.original)
            return {"ok": bool(ok and self.restored), "status": "restored" if ok and self.restored else "restore_failed", "wrote": True}
        except Exception as error:
            return {"ok": False, "status": "restore_failed", "wrote": False, "error": _safe_recovery_error(error)}


class GateActivationError(RuntimeError):
    def __init__(self, error, lease, rollback):
        super().__init__(f"激活未确认：{error}；回滚结果：{rollback}")
        self.lease, self.rollback = lease, dict(rollback)


def activate_gate(adapter, evidence, expected_identity, *, allow_runtime_change=False,
                  lease_seconds=LEASE_SECONDS, retry_seconds=RESTORE_RETRY_SECONDS,
                  clock=None, timer_factory=None):
    """Explicit opt-in transaction; the CLI remains strictly read-only.

    It changes exactly one reviewed data byte 0→1, verifies readback, and yields
    a lease pending *external* UIA inspection of the same main window. This
    function cannot claim accessibility readiness or authorize a message send.
    """
    if allow_runtime_change is not True:
        raise PermissionError("需要明确授权本次可撤销的进程状态尝试")
    profile = _validated_build(evidence)
    from desktop_coordinator import DESKTOP_LOCK
    with DESKTOP_LOCK:
        identity = _fast_identity(adapter)
        _check_identity(identity, evidence, expected_identity["pid"], expected_identity["executable"], expected_identity["creation_filetime"])
        if identity != expected_identity:
            raise ValueError("当前进程/模块与只读探测结果不符")
        bind = getattr(adapter, "_bind_transaction", None)
        if callable(bind):
            bind(identity, evidence)
        rva = profile.active_rva
        if not _writable_image_page(adapter.page_info(rva), identity, rva):
            raise ValueError("目标页面不是已提交、不可执行的可写映像数据页")
        if adapter.read_byte(rva) != 0:
            raise ValueError("状态原值不是 0；未执行任何写入")
        lease = GateLease(adapter, identity, profile=profile, lease_seconds=lease_seconds,
                          retry_seconds=retry_seconds, clock=clock, timer_factory=timer_factory)
        # These checks precede any write attempt. A competing tool's newly set
        # byte belongs to that tool and must never trigger our rollback.
        if _fast_identity(adapter) != identity or adapter.read_byte(rva) != 0:
            raise ValueError("提交前身份或状态变化；已取消")
        if not _writable_image_page(adapter.page_info(rva), identity, rva):
            raise ValueError("提交前页面权限变化；已取消")
        try:
            with lease._lock:
                lease._arm_locked()
                lease._write_attempted = True
                if not adapter.write_byte(rva, 1) or adapter.read_byte(rva) != 1:
                    raise RuntimeError("单字节写入或读回验证失败")
                lease.applied = True
            return lease
        except BaseException as error:
            # A failing native write may still have changed the byte. Always
            # re-check identity/value and try conditional restoration.
            try:
                rollback = lease.restore()
            except BaseException as rollback_error:
                rollback = {"ok": False, "status": "restore_interrupted", "wrote": False,
                            "error": type(rollback_error).__name__}
                # A second interruption during rollback must carry ownership
                # just as an interruption in the original write does.
                rollback_error.uia_gate_lease = lease
                rollback_error.uia_gate_rollback = rollback
                raise rollback_error from error
            if not isinstance(error, Exception):
                # Keep interruption semantics, but do not discard the only
                # restoration handle if rollback could not safely complete.
                error.uia_gate_lease = lease
                error.uia_gate_rollback = dict(rollback)
                if hasattr(error, "add_note"):
                    error.add_note(f"回滚结果：{rollback}")
                raise
            raise GateActivationError(error, lease, rollback) from error


def _known_build_evidence(identity, profile=None):
    """Fixed reviewed manifest, never scan-and-try an unknown runtime version."""
    identity = identity if isinstance(identity, Mapping) else {}
    selected = _select_profile(identity)
    if profile is not None and profile != selected:
        raise ValueError("构建方案与进程模块身份不一致")
    profile = selected
    evidence = {"dll_path": identity.get("dll_path"), "sha256": identity.get("sha256"),
        "file_version": identity.get("file_version"), "architecture": identity.get("architecture"),
        "size_of_image": identity.get("module_size"), "candidates": [
            {"flag_rva": rva, "structural_data_flag": True,
             "section": {"writable": True, "executable": False}}
            for rva in profile.read_rvas], "profile_id": profile.profile_id,
        "read_rvas": list(profile.read_rvas), "active_rva": profile.active_rva}
    _validated_build(evidence)
    return evidence


class GateRecoveryManager:
    """One shared optional lease; fast validation between writes, full SHA at writes.

    Ensuring the flag does not prove control availability. The caller must get a
    fresh provider for the same main window and verify its ordinary UIA controls.
    """
    def __init__(self, enabled=None, adapter_factory=NativeProcessProbe, hwnd_pid=pid_from_hwnd,
                 *, lease_seconds=LEASE_SECONDS, retry_seconds=RESTORE_RETRY_SECONDS,
                 clock=None, timer_factory=None):
        self._enabled, self._factory, self._hwnd_pid = enabled, adapter_factory, hwnd_pid
        self._lease_options = {"lease_seconds": lease_seconds, "retry_seconds": retry_seconds,
                               "clock": clock, "timer_factory": timer_factory}
        self._lock = threading.RLock()
        self._adapter = self._lease = self._identity = None
        self._status, self._last_error, self._restore_result = "idle", "", None
        self._observed_build = None
        self._selected_profile = None
        self._build_check = {"status": "not_checked", "reason": "尚未取得微信模块构建信息。"}

    @property
    def enabled(self):
        if self._enabled is not None:
            return bool(self._enabled)
        import config
        return bool(getattr(config, "UIA_RECOVERY_ENABLED", False))

    def snapshot(self):
        # Metadata only. This method never touches a process or a window.
        acquired = self._lock.acquire(blocking=False)
        try:
            lease = self._lease
            metadata = lease.metadata() if lease is not None else None
            status, last_error, restore_result = self._status, self._last_error, self._restore_result
            if metadata and metadata["expired"]:
                restore_result = metadata["restore_result"] or restore_result
                status = "lease_expired" if metadata["restored"] else "restore_failed" if restore_result else "expired_restoring"
                if not metadata["restored"]:
                    last_error = metadata["watchdog_error"] or str(restore_result or "租期结束，正在恢复")
            return {"enabled": self.enabled, "active": lease is not None and not metadata["restored"],
                    "status": status, "last_error": last_error, "busy": not acquired,
                    "supported_builds": build_profiles.supported_builds(),
                    "selected_profile": self._selected_profile.metadata() if self._selected_profile is not None else None,
                    "verified_build": self._selected_profile.metadata() if self._selected_profile is not None else None,
                    "observed_build": dict(self._observed_build) if self._observed_build is not None else None,
                    "build_check": dict(self._build_check),
                    "restore_result": dict(restore_result) if restore_result else None,
                    "lease": metadata}
        finally:
            if acquired:
                self._lock.release()

    def _close_locked(self):
        try:
            result = self._lease.restore() if self._lease is not None else {"ok": True, "status": "not_active", "wrote": False}
        except BaseException as error:
            self._restore_result = {"ok": False, "status": "restore_interrupted", "wrote": False,
                                    "error": type(error).__name__}
            self._status, self._last_error = "restore_failed", type(error).__name__
            raise  # The manager still owns its lease and held process handle.
        self._restore_result = result
        self._status = "closed" if result["ok"] else "restore_failed"
        if not result["ok"]:
            self._last_error = str(result)
            return dict(result)  # Keep the held handle for a later safe retry.
        if self._adapter is not None:
            try:
                self._adapter.close()
            except Exception as error:
                result = {"ok": False, "status": "handle_close_failed", "wrote": False, "error": _safe_recovery_error(error)}
                self._restore_result = dict(result)
                self._status, self._last_error = "restore_failed", _safe_recovery_error(error)
                return result
        self._adapter = self._lease = self._identity = None
        self._last_error = ""
        return dict(result)

    def close(self):
        with self._lock:
            return self._close_locked()

    def ensure(self, hwnd, pid):
        from desktop_coordinator import DESKTOP_LOCK
        with DESKTOP_LOCK, self._lock:
            if not self.enabled:
                if self._lease is not None:
                    if not self._close_locked()["ok"]:
                        return False
                self._status = "disabled"
                return False
            adapter = None
            try:
                if self._hwnd_pid(hwnd) != int(pid):
                    raise ValueError("当前主窗口进程与 UIA 树不符")
                if self._lease is not None and not self._lease.applied:
                    if not self._close_locked()["ok"]:
                        return False
                if self._lease is not None:
                    with self._lease._lock:
                        # An expiry ends this operation. Only a later fresh
                        # ensure() may activate a new bounded transaction.
                        if self._lease.restored or self._lease.expired or self._lease.watchdog_error or self._lease._clock() >= self._lease._deadline:
                            self._lease.expired = True
                            if self._close_locked()["ok"]:
                                self._status = "lease_expired"
                            return False
                        try:
                            current = _fast_identity(self._adapter)
                        except Exception:
                            current = None
                        if current == self._identity and current["pid"] == int(pid) and self._adapter.read_byte(self._lease.rva) == 1:
                            self._status = "active_pending_control_check"
                            return True
                    if not self._close_locked()["ok"]:
                        return False
                self._observed_build = None
                self._selected_profile = None
                self._build_check = {"status": "not_checked", "reason": "尚未取得微信模块构建信息。"}
                adapter = self._factory(pid, None, permit_write=True)
                identity = adapter.identity()
                self._observed_build = _safe_observed_build(identity)
                profile = _select_profile(identity)
                evidence = _known_build_evidence(identity, profile)
                self._selected_profile = profile
                self._build_check = {"status": "matched", "reason": "模块版本、SHA256 与映像大小符合已复核构建；仍需核验状态与 UIA 控件。"}
                _check_identity(identity, evidence, pid, identity["executable"], identity["creation_filetime"])
                bind = getattr(adapter, "_bind_read_identity", None)
                if callable(bind):
                    bind(identity, profile)
                current = adapter.read_byte(profile.active_rva)
                if current == 1:
                    adapter.close()
                    self._status, self._last_error = "already_active_external", ""
                    return True  # No lease owns another tool's activation.
                if current != 0:
                    raise ValueError("未知状态值；未修改微信")
                lease = activate_gate(adapter, evidence, identity, allow_runtime_change=True, **self._lease_options)
                self._adapter, self._lease, self._identity = adapter, lease, identity
                if lease.restored or lease.expired:
                    self._close_locked()
                    self._status = "lease_expired"
                    return False
                self._restore_result = None
                self._status, self._last_error = "active_pending_control_check", ""
                return True
            except BaseException as error:
                if isinstance(error, BuildVerificationError):
                    self._build_check = {"status": error.code, "reason": _safe_recovery_error(error)}
                failed_lease = error.lease if isinstance(error, GateActivationError) else getattr(error, "uia_gate_lease", None)
                rollback = error.rollback if isinstance(error, GateActivationError) else getattr(error, "uia_gate_rollback", {})
                if failed_lease is not None and not rollback.get("ok"):
                    self._adapter, self._lease, self._identity = adapter, failed_lease, dict(failed_lease.identity)
                    self._restore_result = dict(rollback)
                    self._status, self._last_error = "restore_failed", _safe_recovery_error(error)
                    if not isinstance(error, Exception):
                        raise
                    return False
                if adapter is not None and adapter is not self._adapter:
                    adapter.close()
                if not isinstance(error, Exception):
                    raise
                self._status, self._last_error = "unavailable", _safe_recovery_error(error)
                return False


_shared_manager = None
_shared_manager_lock = __import__("threading").Lock()


def shared_gate_manager():
    global _shared_manager
    with _shared_manager_lock:
        if _shared_manager is None:
            import atexit
            _shared_manager = GateRecoveryManager()
            atexit.register(_shared_manager.close)
        return _shared_manager


def main(argv=None):
    parser = argparse.ArgumentParser(description="微信bot：只读分析磁盘 DLL 的 UIA 状态候选，不激活、不发送")
    parser.add_argument("dll")
    parser.add_argument("--output", required=True)
    parser.add_argument("--pid", type=int)
    parser.add_argument("--hwnd", type=int)
    parser.add_argument("--expected-exe")
    parser.add_argument("--expected-creation", type=int)
    args = parser.parse_args(argv)
    evidence = inspect_dll(args.dll)
    if args.pid is not None:
        if not args.expected_exe or not args.expected_creation:
            parser.error("--pid 必须同时提供 --expected-exe 和 --expected-creation")
        if args.hwnd is not None and pid_from_hwnd(args.hwnd) != args.pid:
            raise ValueError("已核验主窗口与指定 PID 不匹配")
        evidence["process_probe"] = inspect_process(evidence, args.pid, args.expected_exe, args.expected_creation)
        evidence["process_probe"]["validated_hwnd"] = args.hwnd
    output = write_manifest(evidence, args.output)
    print(json.dumps({"output": str(output), "sha256": evidence["sha256"],
        "file_version": evidence["file_version"], "status": evidence["status"],
        "candidate_count": len(evidence["candidates"]), "strict_candidates": evidence["strict_candidates"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
