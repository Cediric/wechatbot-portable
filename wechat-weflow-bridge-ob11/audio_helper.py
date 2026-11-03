"""
audio_helper.py — Windows 音频设备辅助
======================================

用于：
- 查看/列出当前播放设备
- 发语音时自动切到 VB-Cable，发完自动恢复原默认设备
- Web 面板显示当前设备 / 打开系统声音设置

依赖（可选但推荐）：
    pip install pycaw
没有 pycaw 时仍可运行，但自动切换/恢复不可用，需要手动设置默认播放设备。
"""

import logging
import subprocess
from functools import wraps
from contextlib import contextmanager

log = logging.getLogger("ob11-bridge")


@contextmanager
def _audio_com():
    """pycaw is called from panel/worker threads, not just the main thread."""
    try:
        import comtypes
    except ImportError:
        yield
        return
    comtypes.CoInitialize()
    try:
        yield
    finally:
        comtypes.CoUninitialize()


def _with_com(function):
    @wraps(function)
    def call(*args, **kwargs):
        with _audio_com():
            return function(*args, **kwargs)
    return call


def _pycaw_available():
    try:
        from pycaw.pycaw import AudioUtilities
        return AudioUtilities is not None
    except Exception:
        return False


def _default_role_ids(data_flow):
    """Windows has distinct console/media/communications defaults."""
    from pycaw.constants import EDataFlow, ERole
    from pycaw.pycaw import AudioUtilities
    enumerator = AudioUtilities.GetDeviceEnumerator()
    flow = EDataFlow.eRender if data_flow == "render" else EDataFlow.eCapture
    result = {}
    for role in (ERole.eConsole, ERole.eMultimedia, ERole.eCommunications):
        device = enumerator.GetDefaultAudioEndpoint(flow.value, role.value)
        result[str(role.value)] = device.GetId()
    return result


@_with_com
def list_playback_devices(*, include_default=True) -> list[dict]:
    """列出当前活动的播放设备。"""
    if not _pycaw_available():
        return []
    try:
        from pycaw.constants import DEVICE_STATE, EDataFlow
        from pycaw.pycaw import AudioUtilities

        default = get_default_playback_device_info() if include_default else None
        devices = AudioUtilities.GetAllDevices(
            data_flow=EDataFlow.eRender.value,
            device_state=DEVICE_STATE.ACTIVE.value,
        )
        result = []
        for d in devices:
            try:
                name = getattr(d, "FriendlyName", "") or str(d)
                result.append({
                    "id": d.id,
                    "name": name,
                    "is_default": bool(default and d.id == default.get("id")),
                })
            except Exception:
                continue
        return result
    except Exception as e:
        log.warning(f"[audio] 列出播放设备失败: {e}")
        return []


@_with_com
def get_default_playback_device_info() -> dict | None:
    """返回当前默认播放设备 {'id','name'}；失败返回 None。"""
    if not _pycaw_available():
        return None
    try:
        from pycaw.pycaw import AudioUtilities
        dev = AudioUtilities.GetSpeakers()
        if dev is None:
            return None
        return {
            "id": dev.id,
            "name": getattr(dev, "FriendlyName", "") or str(dev),
            "role_ids": _default_role_ids("render"),
        }
    except Exception as e:
        log.debug(f"[audio] 获取默认播放设备失败: {e}")
        return None


def get_default_playback_device() -> str:
    """返回当前默认播放设备名（字符串），方便 Web 展示。"""
    info = get_default_playback_device_info()
    if info:
        return info["name"]
    return "未知（未安装 pycaw，请在系统声音设置中确认）"


def has_cable_device() -> bool:
    """检测是否存在 VB-Cable 虚拟声卡设备。"""
    devices = list_playback_devices()
    return any("CABLE" in (d.get("name") or "").upper() for d in devices)


@_with_com
def list_recording_devices(*, include_default=True) -> list[dict]:
    """列出当前活动的录音设备。"""
    if not _pycaw_available():
        return []
    try:
        from pycaw.constants import DEVICE_STATE, EDataFlow
        from pycaw.pycaw import AudioUtilities

        default = get_default_recording_device_info() if include_default else None
        devices = AudioUtilities.GetAllDevices(
            data_flow=EDataFlow.eCapture.value,
            device_state=DEVICE_STATE.ACTIVE.value,
        )
        result = []
        for d in devices:
            try:
                name = getattr(d, "FriendlyName", "") or str(d)
                result.append({
                    "id": d.id,
                    "name": name,
                    "is_default": bool(default and d.id == default.get("id")),
                })
            except Exception:
                continue
        return result
    except Exception as e:
        log.warning(f"[audio] 列出录音设备失败: {e}")
        return []


@_with_com
def get_default_recording_device_info() -> dict | None:
    """返回当前默认录音设备 {'id','name'}；失败返回 None。"""
    if not _pycaw_available():
        return None
    try:
        from pycaw.pycaw import AudioUtilities
        dev = AudioUtilities.GetMicrophone()
        if dev is None:
            return None
        dev_id = dev.GetId() if hasattr(dev, "GetId") else getattr(dev, "id", "")
        if not dev_id:
            return None
        dev_name = getattr(dev, "FriendlyName", "") or dev_id
        return {"id": dev_id, "name": dev_name, "role_ids": _default_role_ids("capture")}
    except Exception as e:
        log.debug(f"[audio] 获取默认录音设备失败: {e}")
        return None


def set_default_recording_device(device_id: str = "", device_name: str = "", *, devices=None) -> dict:
    """把默认录音设备切到指定设备（按 id 或名称模糊匹配）。"""
    return _set_default_device("capture", device_id, device_name, devices=devices)


def has_cable_recording_device() -> bool:
    """检测是否存在 VB-Cable 录音设备（CABLE Output）。"""
    devices = list_recording_devices()
    return any("CABLE" in (d.get("name") or "").upper() for d in devices)


def set_default_playback_device(device_id: str = "", device_name: str = "", *, devices=None) -> dict:
    """把默认播放设备切到指定设备（按 id 或名称模糊匹配）。"""
    return _set_default_device("render", device_id, device_name, devices=devices)


@_with_com
def _set_default_device(data_flow: str, device_id: str = "", device_name: str = "", *, devices=None) -> dict:
    if not _pycaw_available():
        raise RuntimeError("未安装 pycaw，无法自动切换默认音频设备。请手动设置，或 pip install pycaw")
    from pycaw.pycaw import AudioUtilities

    if devices is None:
        devices = list_playback_devices() if data_flow == "render" else list_recording_devices()
    target = None
    if device_id:
        target = next((d for d in devices if d["id"] == device_id), None)
    if target is None and device_name:
        matches = [d for d in devices if d["name"].casefold() == device_name.casefold()]
        if not matches:
            matches = [d for d in devices if device_name.casefold() in d["name"].casefold()]
        if len(matches) > 1:
            raise LookupError("音频设备名称不唯一，请使用明确的设备 ID")
        target = matches[0] if matches else None
    if target is None:
        names = "、".join(d["name"] for d in devices[:10]) or "无"
        raise LookupError(f"找不到音频设备: {device_name or device_id}。当前设备: {names}")

    from pycaw.constants import ERole
    AudioUtilities.SetDefaultDevice(target["id"], roles=[ERole.eConsole, ERole.eMultimedia, ERole.eCommunications])
    log.info(f"[audio] 默认{'播放' if data_flow == 'render' else '录音'}设备已切换: {target['name']}")
    return target


def restore_default_playback_device(device_id: str, role_ids=None) -> bool:
    """恢复到之前的默认播放设备。"""
    if not device_id:
        return False
    try:
        _restore_roles(device_id, role_ids, "render")
        return True
    except Exception as e:
        log.warning(f"[audio] 恢复默认播放设备失败: {e}")
        return False


def restore_default_recording_device(device_id: str, role_ids=None) -> bool:
    """Best-effort restoration must never hide the original send error."""
    if not device_id:
        return False
    try:
        _restore_roles(device_id, role_ids, "capture")
        return True
    except Exception as e:
        log.warning(f"[audio] 恢复默认录音设备失败: {e}")
        return False


@_with_com
def _restore_roles(device_id, role_ids, data_flow):
    if not role_ids:
        setter = set_default_playback_device if data_flow == "render" else set_default_recording_device
        return setter(device_id=device_id)
    from pycaw.constants import ERole
    from pycaw.pycaw import AudioUtilities
    errors = []
    for role in (ERole.eConsole, ERole.eMultimedia, ERole.eCommunications):
        original = role_ids.get(str(role.value))
        if original:
            try:
                AudioUtilities.SetDefaultDevice(original, roles=[role])
            except Exception as exc:
                errors.append(str(exc))
    if errors:
        raise RuntimeError("; ".join(errors))


def open_sound_settings() -> bool:
    """打开 Windows 声音设置（播放设备页）。"""
    try:
        subprocess.Popen("control mmsys.cpl,,0", shell=True)
        return True
    except Exception as e:
        log.warning(f"[audio] 打开声音设置失败: {e}")
        return False
