"""Real Windows PowerShell 5.1 native extraction; synthetic ZIPs, no installation."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
import warnings
import zipfile

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "portable" / "extract_runtime.ps1"
POWERSHELL = Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"


@unittest.skipUnless(os.name == "nt" and POWERSHELL.is_file(), "Requires Windows PowerShell 5.1 native file APIs")
class RuntimeArchiveTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix=".runtime-archive-", dir=ROOT / "tests"))
        self.destination = self.root / "runtime [check]"
        self.destination.mkdir()
        (self.destination / "config.json").write_bytes(b"preserved-user-sentinel")
        self.long_name = "/".join("deep%d" % index + "x" * 30 for index in range(8)) + "/module [check].py"
        self.contents = {"python.exe": b"synthetic-runtime", "DLLs/_asyncio.pyd": b"synthetic-dll",
                         "data.bin": b"expected-data", self.long_name: b"complete-long-payload"}
        self.archive = self.root / "runtime [source].zip"
        self.zip(self.archive, self.contents.items())
        self.spec = {"helper": str(HELPER), "destination": str(self.destination), "archive": str(self.archive),
                     "sha": self.sha(self.archive), "required": ["python.exe", "DLLs/_asyncio.pyd"],
                     "long_name": self.long_name.replace("/", "\\")}
        self.addCleanup(self.cleanup_owned)

    def cleanup_owned(self):
        # Only this exact test-owned folder is recursively removed. The extended
        # prefix also lets cleanup reach the deliberately long synthetic files.
        base = (ROOT / "tests").resolve()
        target = self.root.resolve()
        self.assertEqual(target.parent, base)
        self.assertTrue(target.name.startswith(".runtime-archive-"))
        if self.root.exists():
            shutil.rmtree("\\\\?\\" + str(target))

    @staticmethod
    def sha(path):
        return hashlib.sha256(path.read_bytes()).hexdigest()

    @staticmethod
    def zip(path, entries):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
                for name, content in entries:
                    archive.writestr(name, content)

    def run_ps(self, body, **extra):
        spec = self.root / "fixture.json"
        spec.write_text(json.dumps(dict(self.spec, **extra)), encoding="utf-8")
        source = self.root / "probe.ps1"
        prefix = r"""
$ErrorActionPreference='Stop'
$ProgressPreference='SilentlyContinue'
$env:PSModulePath=[IO.Path]::Combine($PSHOME,'Modules')
if ($PSVersionTable.PSVersion.Major -ne 5 -or $PSVersionTable.PSVersion.Minor -ne 1) { throw 'Expected Windows PowerShell 5.1' }
$spec=Get-Content -LiteralPath $args[0] -Raw -Encoding UTF8 | ConvertFrom-Json
. $spec.helper
function Invoke-Archive([switch]$Force) {
    Expand-WechatbotRuntimeArchive -Archive $spec.archive -Destination $spec.destination -ExpectedSha256 $spec.sha -RequiredFiles ([string[]]$spec.required) -ForceVerify:$Force
}
function Assert-Check([bool]$Condition,[string]$Reason) { if (-not $Condition) { throw $Reason } }
"""
        source.write_text(prefix + body + "\nWrite-Output 'PASS'\n", encoding="utf-8-sig")
        result = subprocess.run([str(POWERSHELL), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                                 "-File", str(source), str(spec)], stdin=subprocess.DEVNULL,
                                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=45)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("PASS", result.stdout)

    def test_long_paths_brackets_and_unchanged_locked_binary(self):
        (self.destination / "python.exe").write_bytes(self.contents["python.exe"])
        self.run_ps(r"""
$path=$spec.destination+'\python.exe'
$before=[IO.File]::GetLastWriteTimeUtc($path).Ticks
$held=[IO.File]::Open($path,[IO.FileMode]::Open,[IO.FileAccess]::Read,[IO.FileShare]::Read)
try { $null=Invoke-Archive } finally { $held.Dispose() }
Assert-Check ([IO.File]::GetLastWriteTimeUtc($path).Ticks -eq $before) 'Matching locked binary was overwritten'
$deep='\\?\'+$spec.destination+'\'+$spec.long_name
Assert-Check ($deep.Length -gt 300) 'Fixture did not exercise a long path'
Assert-Check ([IO.File]::ReadAllText($deep) -eq 'complete-long-payload') 'Long bracketed payload mismatch'
Assert-Check ([IO.File]::ReadAllText($spec.destination+'\config.json') -eq 'preserved-user-sentinel') 'Existing sentinel changed'
""")

    def test_cache_reuse_and_missing_or_modified_critical_complete_repair(self):
        self.run_ps(r"""
$null=Invoke-Archive
$path=$spec.destination+'\python.exe'
$before=[IO.File]::GetLastWriteTimeUtc($path).Ticks
$again=Invoke-Archive
Assert-Check ($again -like '*reused*') 'Complete runtime cache was not reused'
Assert-Check ([IO.File]::GetLastWriteTimeUtc($path).Ticks -eq $before) 'Cached file was overwritten'
$deep='\\?\'+$spec.destination+'\'+$spec.long_name
[IO.File]::Delete($spec.destination+'\DLLs\_asyncio.pyd'); [IO.File]::Delete($deep)
$null=Invoke-Archive
Assert-Check ([IO.File]::ReadAllText($spec.destination+'\DLLs\_asyncio.pyd') -eq 'synthetic-dll') 'Missing DLL not repaired'
Assert-Check ([IO.File]::ReadAllText($deep) -eq 'complete-long-payload') 'Missing critical did not trigger complete repair'
[IO.File]::WriteAllText($path,'modified-critical')
$null=Invoke-Archive
Assert-Check ([IO.File]::ReadAllText($path) -eq 'synthetic-runtime') 'Modified critical file was reused'
Assert-Check ([IO.File]::Exists($spec.destination+'\.wechatbot-archive.sha256')) 'Successful repair has no marker'
""")

    def test_force_verify_repairs_noncritical_content(self):
        self.run_ps(r"""
$null=Invoke-Archive
$path=$spec.destination+'\data.bin'
[IO.File]::WriteAllText($path,'modified-noncritical')
$null=Invoke-Archive -Force
Assert-Check ([IO.File]::ReadAllText($path) -eq 'expected-data') 'ForceVerify did not repair noncritical file'
""")

    def test_invalid_zip_preinspection_never_writes_or_deletes_sentinel(self):
        cases = []
        for index, names in enumerate([("../outside.txt",), ("/absolute.txt",), ("C:/absolute.txt",),
                                      ("name:stream",), ("NUL.txt",), ("COM\u00b9.txt",), ("bad/\x01name",),
                                      ("a//b",), ("a./b",), ("A.txt", "a.txt"), ("a.txt", "a.txt"),
                                      ("Folder/a", "folder/b"), ("a", "a/b"), ("a/", "a"),
                                      ("LONGFI~1/file",), ("x" * 256,) ]):
            archive = self.root / ("invalid-%d.zip" % index)
            self.zip(archive, [("would-write.txt", b"must-not-write")] + [(name, b"" if name.endswith("/") else b"bad") for name in names])
            cases.append({"archive": str(archive), "sha": self.sha(archive)})
        symlink = self.root / "symlink.zip"
        with zipfile.ZipFile(symlink, "w") as archive:
            archive.writestr("would-write.txt", b"must-not-write")
            entry = zipfile.ZipInfo("link")
            entry.create_system, entry.external_attr = 3, 0o120777 << 16
            archive.writestr(entry, "outside-target")
        cases.append({"archive": str(symlink), "sha": self.sha(symlink)})
        self.run_ps(r"""
foreach ($case in $spec.invalid) {
    $rejected=$false
    try { $null=Expand-WechatbotRuntimeArchive -Archive $case.archive -Destination $spec.destination -ExpectedSha256 $case.sha -RequiredFiles @('would-write.txt') } catch { $rejected=$true }
    Assert-Check $rejected 'Invalid ZIP was accepted'
    Assert-Check (-not [IO.File]::Exists($spec.destination+'\would-write.txt')) 'Invalid ZIP wrote before preinspection'
    Assert-Check (-not [IO.File]::Exists($spec.destination+'\.wechatbot-archive.sha256')) 'Invalid ZIP published a completion marker'
    Assert-Check ([IO.File]::ReadAllText($spec.destination+'\config.json') -eq 'preserved-user-sentinel') 'Invalid ZIP deleted existing sentinel'
}
""", invalid=cases)

    def test_atomic_failure_preserves_original_error_existing_file_and_sentinel(self):
        self.run_ps(r"""
$null=Invoke-Archive
$path=$spec.destination+'\data.bin'
[IO.File]::WriteAllText($path,'modified-and-locked')
$held=[IO.File]::Open($path,[IO.FileMode]::Open,[IO.FileAccess]::Read,[IO.FileShare]::Read)
$rejected=$false
try {
    try { $null=Invoke-Archive -Force } catch {
        $rejected=$true
        Assert-Check ($_.Exception -is [ComponentModel.Win32Exception]) 'Original native error was hidden'
        Assert-Check ($_.Exception.Message -like '*MoveFileExW*') 'Original failing operation was hidden'
    }
} finally { $held.Dispose() }
Assert-Check $rejected 'Replacing a locked changed runtime file was not refused'
Assert-Check ([IO.File]::ReadAllText($path) -eq 'modified-and-locked') 'Atomic failure changed old file'
Assert-Check ([IO.File]::ReadAllText($spec.destination+'\config.json') -eq 'preserved-user-sentinel') 'Atomic failure deleted user sentinel'
Assert-Check (-not [IO.File]::Exists($spec.destination+'\.wechatbot-archive.sha256')) 'Failed repair retained a valid completion marker'
Assert-Check ([IO.Directory]::GetFiles($spec.destination,'.wechatbot-tmp-*').Length -eq 0) 'Owned temporary file was not cleaned'
""")

    def test_wrong_archive_hash_and_existing_reparse_are_rejected(self):
        self.run_ps(r"""
$rejected=$false
try { $null=Expand-WechatbotRuntimeArchive -Archive $spec.archive -Destination $spec.destination -ExpectedSha256 ('0'*64) -RequiredFiles ([string[]]$spec.required) } catch { $rejected=$true }
Assert-Check $rejected 'Incorrect archive hash was accepted'
$base=[IO.Path]::GetDirectoryName($spec.destination)
$spec.destination=[IO.Path]::Combine($base,'reparse-runtime')
$null=[IO.Directory]::CreateDirectory($spec.destination)
[IO.File]::WriteAllText($spec.destination+'\config.json','preserved-user-sentinel')
$outside=[IO.Path]::Combine($base,'outside-owned-fixture')
$null=[IO.Directory]::CreateDirectory($outside)
$null=New-Item -Path ($spec.destination+'\junction') -ItemType Junction -Target $outside
$rejected=$false
try { $null=Invoke-Archive } catch { $rejected=$true }
Assert-Check $rejected 'Existing reparse target was accepted'
Assert-Check (-not [IO.File]::Exists($spec.destination+'\python.exe')) 'Rejected extraction wrote a runtime file'
Assert-Check ([IO.File]::ReadAllText($spec.destination+'\config.json') -eq 'preserved-user-sentinel') 'Rejected extraction deleted sentinel'
""")


if __name__ == "__main__":
    unittest.main()
