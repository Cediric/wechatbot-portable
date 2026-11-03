"""Synthetic current-user DPAPI/AES-GCM fixtures; no real credentials or API."""
import base64
import contextlib
import ctypes
import io
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

PORTABLE = Path(__file__).resolve().parents[1] / "portable"
sys.path.insert(0, str(PORTABLE))
import weflow_secret as secret


class WeFlowSecretTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / "weflow" / "WeFlow-config.json"
        self.state = self.config.with_name("Local State")
        self.config.parent.mkdir()
        self.key = bytes(range(32))
        self.nonce = bytes(range(12))
        self.plain = "synthetic-http-token"
        self.wrapped = b"synthetic-dpapi-blob"
        self.write(self.state, {"os_crypt": {"encrypted_key": base64.b64encode(b"DPAPI" + self.wrapped).decode()}})

    @staticmethod
    def write(path, value):
        path.write_text(json.dumps(value), encoding="utf-8")

    def ciphertext(self, version=b"v10", body=None):
        encrypted = AESGCM(self.key).encrypt(self.nonce, body or self.plain.encode(), None)
        return "safe:" + base64.b64encode(version + self.nonce + encrypted).decode()

    def test_v10_and_v11_read_only_decrypt_just_the_http_token(self):
        for version in (b"v10", b"v11"):
            with self.subTest(version=version):
                self.write(self.config, {"httpApiToken": self.ciphertext(version), "decryptKey": "do-not-read",
                                         "aiModelApiKey": "do-not-read"})
                before = self.config.read_bytes(), self.state.read_bytes()
                with patch.object(secret, "_unprotect_dpapi", return_value=self.key) as unprotect:
                    self.assertEqual(secret.read_http_token(self.config), self.plain)
                unprotect.assert_called_once_with(self.wrapped)
                self.assertEqual((self.config.read_bytes(), self.state.read_bytes()), before)

    def test_legacy_plaintext_and_blank_token_do_not_read_local_state(self):
        for value in (self.plain, ""):
            with self.subTest(blank=value == ""), patch.object(secret, "_json_file", side_effect=AssertionError("state read")):
                self.assertEqual(secret.decode_http_token(value, self.state), value)

    def test_authentication_tag_dpapi_and_unicode_fail_closed_without_values(self):
        ciphertext = self.ciphertext()
        raw = bytearray(base64.b64decode(ciphertext[5:]))
        raw[-1] ^= 1
        corrupted = "safe:" + base64.b64encode(raw).decode()
        for value, unprotect in ((corrupted, lambda blob: self.key),
                                 (ciphertext, lambda blob: b"bad-key"),
                                 (self.ciphertext(body=b"\xff"), lambda blob: self.key),
                                 (ciphertext, lambda blob: (_ for _ in ()).throw(RuntimeError(self.plain)))):
            with self.subTest(value_class="synthetic"), self.assertRaises(secret.WeFlowSecretError) as caught:
                secret.decode_http_token(value, self.state, unprotect=unprotect)
            self.assertNotIn(self.plain, str(caught.exception))
            self.assertNotIn(value, str(caught.exception))

    def test_unsupported_envelope_format_and_missing_state_do_not_return_ciphertext(self):
        for value in ("safe:not-base64", "safe:" + base64.b64encode(b"v20" + b"x" * 40).decode(),
                      "safe:" + base64.b64encode(b"v10short").decode(), "lock:synthetic", 42):
            with self.subTest(value_type=type(value).__name__), self.assertRaises(secret.WeFlowSecretError):
                secret.decode_http_token(value, self.state)
        self.state.unlink()
        with self.assertRaisesRegex(secret.WeFlowSecretError, "local_state_unavailable"):
            secret.decode_http_token(self.ciphertext(), self.state)

    def test_local_state_key_header_is_required(self):
        self.write(self.state, {"os_crypt": {"encrypted_key": base64.b64encode(b"OTHER" + self.wrapped).decode()}})
        with patch.object(secret, "_unprotect_dpapi", side_effect=AssertionError("unprotect")), \
                self.assertRaisesRegex(secret.WeFlowSecretError, "local_state_key_unsupported"):
            secret.decode_http_token(self.ciphertext(), self.state)

    def test_valid_ciphertext_with_an_envelope_as_plaintext_is_rejected(self):
        for value in (b"safe:synthetic-double-envelope", b" lock:synthetic-double-envelope "):
            with self.subTest(envelope=value[:5]), self.assertRaisesRegex(secret.WeFlowSecretError, "http_token_storage_unsupported"):
                secret.decode_http_token(self.ciphertext(body=value), self.state, unprotect=lambda blob: self.key)

    def test_ctypes_dpapi_uses_no_ui_and_zeroes_and_frees_native_output(self):
        allocated = []
        def unprotect(source, description, entropy, reserved, prompt, flags, output):
            self.assertEqual(flags, 1)
            blob = source._obj
            self.assertEqual(ctypes.string_at(blob.data, blob.size), self.wrapped)
            buffer = (ctypes.c_ubyte * len(self.key)).from_buffer_copy(self.key)
            allocated.append(buffer)
            output._obj.size = len(self.key)
            output._obj.data = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))
            return 1
        crypt = types.SimpleNamespace(CryptUnprotectData=unittest.mock.Mock(side_effect=unprotect))
        kernel = types.SimpleNamespace(LocalFree=unittest.mock.Mock(return_value=None))
        with patch.object(secret.os, "name", "nt"), \
                patch.object(secret.ctypes, "windll", types.SimpleNamespace(crypt32=crypt, kernel32=kernel), create=True):
            self.assertEqual(secret._unprotect_dpapi(self.wrapped), self.key)
        crypt.CryptUnprotectData.assert_called_once()
        kernel.LocalFree.assert_called_once()
        self.assertEqual(bytes(allocated[0]), b"\0" * len(self.key))

    def test_ctypes_dpapi_failure_has_only_fixed_reason(self):
        crypt = types.SimpleNamespace(CryptUnprotectData=unittest.mock.Mock(return_value=0))
        kernel = types.SimpleNamespace(LocalFree=unittest.mock.Mock(return_value=None))
        with patch.object(secret.os, "name", "nt"), \
                patch.object(secret.ctypes, "windll", types.SimpleNamespace(crypt32=crypt, kernel32=kernel), create=True), \
                self.assertRaisesRegex(secret.WeFlowSecretError, "^dpapi_decrypt_failed$"):
            secret._unprotect_dpapi(self.wrapped)
        kernel.LocalFree.assert_not_called()

    def test_only_adjacent_named_weflow_files_are_allowed(self):
        for config, state in ((self.root / "Other-config.json", self.state),
                              (self.config, self.root / "other" / "Local State"),
                              (self.config, self.root / "other" / "Cookies")):
            with self.subTest(filename=config.name), self.assertRaisesRegex(secret.WeFlowSecretError, "local_weflow_paths_invalid"):
                secret.read_http_token(config, state)

    def test_bridge_runtime_fallback_passes_no_secret_in_argv_or_logs(self):
        runtime = self.root / "runtime"
        python = runtime / "bridge-venv" / "Scripts" / "python.exe"
        python.parent.mkdir(parents=True)
        python.touch()
        encrypted = self.ciphertext()
        result = types.SimpleNamespace(returncode=0, stdout=json.dumps({"ok": True, "token": self.plain}), stderr="")
        with patch.object(secret, "decode_http_token", side_effect=secret.WeFlowSecretError("crypto_dependency_unavailable")), \
                patch.object(secret.subprocess, "run", return_value=result) as run:
            self.assertEqual(secret.resolve_http_token(encrypted, self.config, runtime=runtime), self.plain)
        self.assertNotIn(encrypted, run.call_args.args[0])
        self.assertNotIn(self.plain, run.call_args.args[0])
        self.assertEqual(json.loads(run.call_args.kwargs["input"]), {"stored_token": encrypted})
        self.assertEqual(run.call_args.kwargs["timeout"], 10)

    def test_runtime_fallback_never_accepts_encrypted_output_or_leaks_stderr(self):
        runtime = self.root / "runtime"
        python = runtime / "bridge-venv" / "Scripts" / "python.exe"
        python.parent.mkdir(parents=True)
        python.touch()
        encrypted = self.ciphertext()
        for reply in (types.SimpleNamespace(returncode=0, stdout=json.dumps({"ok": True, "token": encrypted}), stderr=""),
                      types.SimpleNamespace(returncode=2, stdout="private-output", stderr=self.plain)):
            with patch.object(secret, "decode_http_token", side_effect=secret.WeFlowSecretError("crypto_dependency_unavailable")), \
                    patch.object(secret.subprocess, "run", return_value=reply), self.assertRaises(secret.WeFlowSecretError) as caught:
                secret.resolve_http_token(encrypted, self.config, runtime=runtime)
            self.assertNotIn(self.plain, str(caught.exception))
            self.assertNotIn(encrypted, str(caught.exception))

    def test_cli_captured_pipe_protocol_and_redacted_failure(self):
        self.write(self.config, {"httpApiToken": self.ciphertext()})
        output = io.StringIO()
        with patch.object(secret, "_unprotect_dpapi", return_value=self.key), contextlib.redirect_stdout(output):
            self.assertEqual(secret.main(["--config", str(self.config)]), 0)
        self.assertEqual(json.loads(output.getvalue()), {"ok": True, "token": self.plain})
        output = io.StringIO()
        with patch.object(secret, "_unprotect_dpapi", side_effect=RuntimeError(self.plain)), contextlib.redirect_stdout(output):
            self.assertEqual(secret.main(["--config", str(self.config)]), 2)
        self.assertEqual(json.loads(output.getvalue()), {"ok": False, "reason": "http_token_decrypt_failed"})
        self.assertNotIn(self.plain, output.getvalue())


if __name__ == "__main__":
    unittest.main()
