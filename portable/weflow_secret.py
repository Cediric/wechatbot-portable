"""Read only WeFlow's local HTTP token; never print credentials in diagnostics.

The CLI is an internal pipe protocol. Its successful stdout contains a secret
and must be captured in memory, never forwarded to logs or a terminal.
"""
from __future__ import annotations

import argparse
import base64
import ctypes
import json
import os
from pathlib import Path
import subprocess
import sys


class WeFlowSecretError(RuntimeError):
    """A fixed reason code, deliberately excluding values and native errors."""

    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


def _json_file(path, reason, maximum):
    try:
        target = Path(path)
        if target.stat().st_size > maximum:
            raise ValueError
        value = json.loads(target.read_text(encoding="utf-8-sig"))
        if not isinstance(value, dict):
            raise ValueError
        return value
    except (OSError, ValueError, TypeError, RuntimeError):
        raise WeFlowSecretError(reason) from None


def _paths(config_path, local_state_path=None):
    """Restrict the helper to the named WeFlow config and adjacent Local State."""
    try:
        config = Path(config_path).resolve()
        state = Path(local_state_path).resolve() if local_state_path else config.with_name("Local State")
        if (config.name.casefold() != "weflow-config.json" or state.name != "Local State"
                or state.parent != config.parent or str(config).startswith("\\\\")):
            raise ValueError
        return config, state
    except (OSError, ValueError, TypeError, RuntimeError):
        raise WeFlowSecretError("local_weflow_paths_invalid") from None


def _base64(value, reason):
    try:
        if not isinstance(value, str) or not value or len(value) > 65536:
            raise ValueError
        return base64.b64decode(value, validate=True)
    except (ValueError, TypeError):
        raise WeFlowSecretError(reason) from None


def _plain_http_token(value):
    if (not isinstance(value, str) or len(value) > 4096
            or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise WeFlowSecretError("http_token_format_invalid")
    token = value.strip()
    if token.startswith(("safe:", "lock:")):
        raise WeFlowSecretError("http_token_storage_unsupported")
    return token


def _unprotect_dpapi(value):
    """Use current-user DPAPI without a dialog or third-party Windows modules."""
    if os.name != "nt":
        raise WeFlowSecretError("windows_current_user_required")

    class DataBlob(ctypes.Structure):
        _fields_ = [("size", ctypes.c_uint32), ("data", ctypes.POINTER(ctypes.c_ubyte))]

    try:
        crypt = ctypes.windll.crypt32
        kernel = ctypes.windll.kernel32
        crypt.CryptUnprotectData.argtypes = [ctypes.POINTER(DataBlob), ctypes.c_void_p,
                                            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                                            ctypes.c_uint32, ctypes.POINTER(DataBlob)]
        crypt.CryptUnprotectData.restype = ctypes.c_int
        kernel.LocalFree.argtypes = [ctypes.c_void_p]
        kernel.LocalFree.restype = ctypes.c_void_p
        buffer = (ctypes.c_ubyte * len(value)).from_buffer_copy(value)
        source = DataBlob(len(value), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
        output = DataBlob()
        # CRYPTPROTECT_UI_FORBIDDEN: failure never prompts for another identity.
        if not crypt.CryptUnprotectData(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(output)):
            raise WeFlowSecretError("dpapi_decrypt_failed")
        try:
            return ctypes.string_at(output.data, output.size)
        finally:
            if output.data:
                ctypes.memset(output.data, 0, output.size)
                kernel.LocalFree(ctypes.cast(output.data, ctypes.c_void_p))
    except WeFlowSecretError:
        raise
    except Exception:
        raise WeFlowSecretError("dpapi_decrypt_failed") from None


def decode_http_token(value, local_state_path, *, unprotect=None):
    """Decode safe: Chromium v10/v11 AES-GCM, or retain legacy plaintext."""
    if not isinstance(value, str):
        raise WeFlowSecretError("http_token_format_invalid")
    if not value.startswith("safe:"):
        if value.startswith("lock:"):
            raise WeFlowSecretError("http_token_storage_unsupported")
        return _plain_http_token(value)
    blob = _base64(value[5:], "http_token_ciphertext_invalid")
    if blob[:3] not in {b"v10", b"v11"} or len(blob) < 3 + 12 + 16:
        raise WeFlowSecretError("http_token_ciphertext_unsupported")
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError:
        raise WeFlowSecretError("crypto_dependency_unavailable") from None
    state = _json_file(local_state_path, "local_state_unavailable", 8 * 1024 * 1024)
    crypt = state.get("os_crypt")
    wrapped = _base64(crypt.get("encrypted_key") if isinstance(crypt, dict) else None,
                      "local_state_key_invalid")
    if not wrapped.startswith(b"DPAPI") or len(wrapped) <= 5:
        raise WeFlowSecretError("local_state_key_unsupported")
    try:
        key = (unprotect or _unprotect_dpapi)(wrapped[5:])
        if not isinstance(key, bytes) or len(key) != 32:
            raise WeFlowSecretError("local_state_key_invalid")
        # cryptography verifies the trailing 16-byte tag before returning data.
        plaintext = AESGCM(key).decrypt(blob[3:15], blob[15:], None).decode("utf-8")
        return _plain_http_token(plaintext)
    except WeFlowSecretError:
        raise
    except Exception:
        raise WeFlowSecretError("http_token_decrypt_failed") from None


def read_http_token(config_path, local_state_path=None, *, unprotect=None):
    config, state = _paths(config_path, local_state_path)
    settings = _json_file(config, "weflow_config_unavailable", 2 * 1024 * 1024)
    return decode_http_token(settings.get("httpApiToken", ""), state, unprotect=unprotect)


def resolve_http_token(value, config_path, *, runtime=None):
    """Fall back to the prepared bridge Python when bootstrap lacks AES-GCM."""
    config, state = _paths(config_path)
    try:
        return decode_http_token(value, state)
    except WeFlowSecretError as error:
        if error.reason != "crypto_dependency_unavailable" or runtime is None:
            raise
    python = Path(runtime) / "bridge-venv" / "Scripts" / "python.exe"
    if not python.is_file():
        raise WeFlowSecretError("bridge_secret_runtime_unavailable")
    try:
        result = subprocess.run([str(python), "-X", "utf8", "-B", str(Path(__file__).resolve()),
                                 "--config", str(config), "--local-state", str(state), "--decode-stdin"],
                                input=json.dumps({"stored_token": value}), stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, timeout=10, text=True, encoding="utf-8",
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if len(result.stdout) > 65536:
            raise WeFlowSecretError("bridge_secret_protocol_invalid")
        reply = json.loads(result.stdout)
        if result.returncode != 0 or not isinstance(reply, dict) or reply.get("ok") is not True:
            raise WeFlowSecretError("bridge_secret_decrypt_failed")
        token = _plain_http_token(reply.get("token"))
        if token.startswith(("safe:", "lock:")):
            raise WeFlowSecretError("bridge_secret_protocol_invalid")
        return token
    except WeFlowSecretError:
        raise
    except Exception:
        raise WeFlowSecretError("bridge_secret_decrypt_failed") from None


def main(argv=None):
    parser = argparse.ArgumentParser(description="Internal captured-pipe WeFlow HTTP token reader")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--local-state", type=Path)
    parser.add_argument("--decode-stdin", action="store_true")
    args = parser.parse_args(argv)
    try:
        config, state = _paths(args.config, args.local_state)
        if args.decode_stdin:
            request = json.loads(sys.stdin.read(65537))
            if not isinstance(request, dict) or set(request) != {"stored_token"}:
                raise WeFlowSecretError("bridge_secret_protocol_invalid")
            token = decode_http_token(request["stored_token"], state)
        else:
            token = read_http_token(config, state)
        sys.stdout.write(json.dumps({"ok": True, "token": token}))
        return 0
    except WeFlowSecretError as error:
        reason = error.reason
    except Exception:
        reason = "bridge_secret_protocol_invalid"
    sys.stdout.write(json.dumps({"ok": False, "reason": reason}))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
