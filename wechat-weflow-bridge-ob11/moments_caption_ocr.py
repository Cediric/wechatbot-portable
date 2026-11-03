"""Read only the caller's PNG with Windows' installed Chinese OCR engine.

This module does not capture the screen, automate an application, or use a network.
An unreadable caption raises an error; callers must verify the complete result.
"""

import ctypes
import json
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import zlib


_SCRIPT = Path(__file__).with_suffix(".ps1")
_MAX_PNG_BYTES = 8 * 1024 * 1024
_MAX_DIMENSION = 4096
_MAX_PIXELS = 4 * 1024 * 1024
_MAX_TEXT = 8192
_MAX_OUTPUT_BYTES = 64 * 1024
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _validate_png(png_bytes):
    if not isinstance(png_bytes, bytes) or not 57 <= len(png_bytes) <= _MAX_PNG_BYTES:
        raise RuntimeError("OCR input must be a bounded PNG byte string")
    if png_bytes[:8] != _PNG_SIGNATURE:
        raise RuntimeError("OCR input is not a PNG")
    offset, chunks, saw_data, saw_end = 8, 0, False, False
    while offset < len(png_bytes):
        chunks += 1
        if chunks > 1024 or offset + 12 > len(png_bytes):
            raise RuntimeError("OCR PNG chunk bounds are invalid")
        size = struct.unpack_from(">I", png_bytes, offset)[0]
        kind = png_bytes[offset + 4:offset + 8]
        end = offset + 12 + size
        if end > len(png_bytes) or any(not (65 <= c <= 90 or 97 <= c <= 122) for c in kind):
            raise RuntimeError("OCR PNG chunk bounds are invalid")
        payload = png_bytes[offset + 8:offset + 8 + size]
        expected_crc = struct.unpack_from(">I", png_bytes, offset + 8 + size)[0]
        if zlib.crc32(payload, zlib.crc32(kind)) & 0xFFFFFFFF != expected_crc:
            raise RuntimeError("OCR PNG checksum is invalid")
        if chunks == 1:
            if kind != b"IHDR" or size != 13:
                raise RuntimeError("OCR PNG header is invalid")
            width, height, depth, color, compression, filtering, interlace = struct.unpack(">IIBBBBB", payload)
            valid_depths = {0: (1, 2, 4, 8, 16), 2: (8, 16), 3: (1, 2, 4, 8), 4: (8, 16), 6: (8, 16)}
            if not (0 < width <= _MAX_DIMENSION and 0 < height <= _MAX_DIMENSION
                    and width * height <= _MAX_PIXELS):
                raise RuntimeError("OCR PNG dimensions exceed the caption bounds")
            if depth not in valid_depths.get(color, ()) or compression or filtering or interlace not in (0, 1):
                raise RuntimeError("OCR PNG format is invalid")
        elif kind == b"IHDR":
            raise RuntimeError("OCR PNG has duplicate headers")
        elif kind == b"IDAT":
            saw_data = True
        elif kind == b"IEND":
            if size or end != len(png_bytes) or not saw_data:
                raise RuntimeError("OCR PNG end marker is invalid")
            saw_end = True
        elif kind[0] < 97 and kind != b"PLTE":
            raise RuntimeError("OCR PNG has an unsupported critical chunk")
        offset = end
    if not saw_end:
        raise RuntimeError("OCR PNG is incomplete")


def _powershell_path():
    """Resolve Windows' own directory, without trusting PATH or an environment variable."""
    if sys.platform != "win32":
        raise RuntimeError("Windows caption OCR is unavailable on this platform")
    buffer = ctypes.create_unicode_buffer(32768)
    length = ctypes.windll.kernel32.GetWindowsDirectoryW(buffer, len(buffer))
    if not 0 < length < len(buffer):
        raise RuntimeError("Windows PowerShell directory could not be verified")
    executable = Path(buffer.value) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    if not executable.is_file():
        raise RuntimeError("Windows PowerShell 5.1 is unavailable")
    return executable


def recognize_caption(png_bytes: bytes, timeout=4.0) -> str:
    """Return Chinese OCR text, or raise RuntimeError without a success fallback.

    The PNG must be at most 8 MiB, 4096 pixels per side and 4,194,304 pixels
    total. Only the installed zh-Hans-CN engine is used. The subprocess timeout
    is required to be between 0.1 and 10 seconds.
    """
    _validate_png(png_bytes)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not .1 <= timeout <= 10:
        raise RuntimeError("OCR timeout is outside the bounded range")
    executable = _powershell_path()
    if not _SCRIPT.is_file():
        raise RuntimeError("Windows caption OCR helper is unavailable")
    try:
        with tempfile.TemporaryDirectory(prefix="wechatbot-caption-") as folder:
            image_path = Path(folder) / "caption.png"
            image_path.write_bytes(png_bytes)
            result = subprocess.run(
                [str(executable), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                 "-File", str(_SCRIPT), "-ImagePath", str(image_path)],
                shell=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=float(timeout), check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if result.returncode != 0:
                raise RuntimeError("Windows caption OCR failed")
            if not isinstance(result.stdout, bytes) or not 0 < len(result.stdout) <= _MAX_OUTPUT_BYTES:
                raise RuntimeError("Windows caption OCR output bounds are invalid")
            try:
                value = json.loads(result.stdout.decode("utf-8-sig"))
            except (UnicodeError, ValueError) as exc:
                raise RuntimeError("Windows caption OCR output is invalid") from exc
            if not isinstance(value, str) or not value.strip() or len(value) > _MAX_TEXT or "\x00" in value:
                raise RuntimeError("Windows caption OCR did not return a bounded caption")
            return value.strip()
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("Windows caption OCR timed out") from exc
    except OSError as exc:
        raise RuntimeError("Windows caption OCR could not run") from exc
