"""Caller-supplied PNGs and mocked subprocesses; no screen, UIA or network."""

import importlib.util
import json
from pathlib import Path
import struct
import subprocess
import types
import unittest
from unittest.mock import patch
import zlib


BRIDGE = Path(__file__).resolve().parents[1] / "wechat-weflow-bridge-ob11"
spec = importlib.util.spec_from_file_location("caption_ocr_isolated", BRIDGE / "moments_caption_ocr.py")
ocr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ocr)


def chunk(kind, payload):
    return (struct.pack(">I", len(payload)) + kind + payload
            + struct.pack(">I", zlib.crc32(payload, zlib.crc32(kind)) & 0xFFFFFFFF))


def png(width=1, height=1, depth=8, color=6):
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, depth, color, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"\x00\x00\x00\x00\xff")) + chunk(b"IEND", b""))


class CaptionOcrTests(unittest.TestCase):
    def setUp(self):
        self.executable = Path(r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe")
        self.path_patch = patch.object(ocr, "_powershell_path", return_value=self.executable)
        self.resolve = self.path_patch.start()
        self.addCleanup(self.path_patch.stop)

    def result(self, value="回复 自用测试联系人", returncode=0, raw=None):
        return types.SimpleNamespace(returncode=returncode,
            stdout=json.dumps(value, ensure_ascii=False).encode("utf-8") if raw is None else raw,
            stderr=b"private diagnostics must not be exposed")

    def test_exact_official_executable_bounded_hidden_process_and_temp_cleanup(self):
        paths = []

        def run(argv, **kwargs):
            self.assertEqual(argv[:6], [str(self.executable), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File"])
            self.assertEqual(argv[6], str(BRIDGE / "moments_caption_ocr.ps1"))
            self.assertEqual(argv[7], "-ImagePath")
            path = Path(argv[8])
            paths.append(path)
            self.assertTrue(path.is_absolute())
            self.assertEqual(path.read_bytes(), png())
            self.assertFalse(kwargs["shell"])
            self.assertEqual(kwargs["timeout"], 4.0)
            self.assertEqual(kwargs["creationflags"], getattr(subprocess, "CREATE_NO_WINDOW", 0))
            self.assertEqual(kwargs["stdout"], subprocess.PIPE)
            self.assertEqual(kwargs["stderr"], subprocess.PIPE)
            self.assertFalse(kwargs["check"])
            return self.result()

        with patch.object(ocr.subprocess, "run", side_effect=run):
            self.assertEqual(ocr.recognize_caption(png()), "回复 自用测试联系人")
        self.assertFalse(paths[0].exists())
        self.assertFalse(paths[0].parent.exists())

    def test_timeout_kills_run_and_cleans_private_png(self):
        paths = []

        def run(argv, **kwargs):
            paths.append(Path(argv[-1]))
            self.assertEqual(kwargs["timeout"], .5)
            raise subprocess.TimeoutExpired(argv, .5, output=b"private pixels")

        with patch.object(ocr.subprocess, "run", side_effect=run):
            with self.assertRaisesRegex(RuntimeError, "timed out") as caught:
                ocr.recognize_caption(png(), timeout=.5)
        self.assertNotIn("private", str(caught.exception))
        self.assertFalse(paths[0].parent.exists())

    def test_failure_and_invalid_results_never_fall_back_to_empty_success(self):
        failures = [self.result(returncode=1), self.result(raw=b"not json"), self.result(raw=b'"\xff"'),
            self.result(raw=b'"one" "two"'), self.result({"text": "caption"}), self.result(["caption"]),
            self.result(None), self.result(False), self.result(""), self.result(" \r\n\t"),
            self.result("a" * (ocr._MAX_TEXT + 1)), self.result("caption\x00trailing"),
            self.result(raw=b" " * (ocr._MAX_OUTPUT_BYTES + 1)), self.result(raw=b""),
            self.result(raw="wrong output type")]
        for index, failure in enumerate(failures):
            with self.subTest(index=index), patch.object(ocr.subprocess, "run", return_value=failure) as run:
                with self.assertRaises(RuntimeError) as caught:
                    ocr.recognize_caption(png())
                self.assertNotIn("private", str(caught.exception))
                self.assertFalse(Path(run.call_args.args[0][-1]).parent.exists())

    def test_os_error_cleans_private_png_without_exposing_path(self):
        paths = []

        def run(argv, **kwargs):
            paths.append(Path(argv[-1]))
            raise OSError("private user path")

        with patch.object(ocr.subprocess, "run", side_effect=run):
            with self.assertRaisesRegex(RuntimeError, "could not run") as caught:
                ocr.recognize_caption(png())
        self.assertNotIn("private user", str(caught.exception))
        self.assertFalse(paths[0].parent.exists())

    def test_untrusted_pngs_are_rejected_before_any_subprocess_or_temp_file(self):
        broken_crc = bytearray(png())
        broken_crc[-1] ^= 1
        ihdr = chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0))
        malformed = [None, bytearray(png()), b"not PNG", b"x" * (ocr._MAX_PNG_BYTES + 1),
            png(width=0), png(height=0), png(width=4097), png(height=4097), png(width=4096, height=4096),
            png(depth=3), png(color=9), bytes(broken_crc), png()[:-4], png() + b"suffix",
            b"\x89PNG\r\n\x1a\n" + ihdr + ihdr + chunk(b"IDAT", b"x") + chunk(b"IEND", b""),
            b"\x89PNG\r\n\x1a\n" + ihdr + chunk(b"ABCD", b"x") + chunk(b"IDAT", b"x") + chunk(b"IEND", b""),
            b"\x89PNG\r\n\x1a\n" + ihdr + chunk(b"IEND", b"")]
        with patch.object(ocr.subprocess, "run") as run, patch.object(ocr.tempfile, "TemporaryDirectory") as temp:
            for index, data in enumerate(malformed):
                with self.subTest(index=index), self.assertRaises(RuntimeError):
                    ocr.recognize_caption(data)
            run.assert_not_called()
            temp.assert_not_called()
            self.resolve.assert_not_called()

    def test_unbounded_timeouts_are_rejected_before_subprocess(self):
        with patch.object(ocr.subprocess, "run") as run:
            for value in (None, "4", True, 0, -.1, .09, 10.1, 10 ** 400, float("nan"), float("inf")):
                with self.subTest(value=value), self.assertRaises(RuntimeError):
                    ocr.recognize_caption(png(), timeout=value)
            run.assert_not_called()

    def test_bom_utf8_and_boundary_caption_are_valid(self):
        text = "字" * ocr._MAX_TEXT
        with patch.object(ocr.subprocess, "run", return_value=self.result(raw=b"\xef\xbb\xbf" + json.dumps(text, ensure_ascii=False).encode("utf-8"))):
            self.assertEqual(ocr.recognize_caption(png()), text)

    def test_missing_local_helper_does_not_spawn(self):
        with patch.object(ocr, "_SCRIPT", Path(__file__).with_name("no-such-ocr-helper.ps1")), patch.object(ocr.subprocess, "run") as run:
            with self.assertRaisesRegex(RuntimeError, "helper is unavailable"):
                ocr.recognize_caption(png())
            run.assert_not_called()

    def test_platform_failure_does_not_create_temp_or_spawn(self):
        self.path_patch.stop()
        with patch.object(ocr.sys, "platform", "unsupported"), patch.object(ocr.subprocess, "run") as run, patch.object(ocr.tempfile, "TemporaryDirectory") as temp:
            with self.assertRaisesRegex(RuntimeError, "unavailable on this platform"):
                ocr.recognize_caption(png())
            run.assert_not_called()
            temp.assert_not_called()


if __name__ == "__main__":
    unittest.main()
