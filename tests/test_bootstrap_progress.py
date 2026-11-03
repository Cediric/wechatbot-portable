"""Real Windows PowerShell 5.1 children; no installs, services, APIs or secrets."""
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import time
import unittest


PORTABLE = Path(__file__).resolve().parents[1] / "portable"
STAGE = "阶段：AstrBot环境：导入检查"


@unittest.skipUnless(os.name == "nt", "Requires native Windows PowerShell 5.1")
class PowerShellProgressTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="wechatbot-progress-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.child = self.root / "child.py"
        self.result = self.root / "result.json"
        self.finished = self.root / "finished"
        self.fixture = self.root / "invoke-progress.ps1"
        self.fixture.write_text(r'''param($Source,$Python,$Child,$Result,$Mode)
$ErrorActionPreference='Stop'
[Console]::OutputEncoding=New-Object System.Text.UTF8Encoding($false)
$env:PYTHONUTF8='1'
$tokens=$null;$errors=$null
$ast=[Management.Automation.Language.Parser]::ParseFile($Source,[ref]$tokens,[ref]$errors)
if($errors.Count){throw 'Source parse failed'}
$wanted=@('Protect-WechatbotDiagnostic','Get-WechatbotDiagnosticExcerpt','Invoke-Checked')
$ast.FindAll({param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $wanted -contains $node.Name},$true) | ForEach-Object {Invoke-Expression $_.Extent.Text}
$value=[pscustomobject]@{version=$PSVersionTable.PSVersion.ToString();captured=@();error=''}
if($Mode -eq 'capture'){
    $value.captured=@(Invoke-Checked $Python @('-X','utf8','-B',$Child) 'synthetic-find' -CaptureOutput -ShowStages)
}elseif($Mode -eq 'default'){
    Invoke-Checked $Python @('-X','utf8','-B',$Child) 'synthetic-default'
}else{
    try {Invoke-Checked $Python @('-X','utf8','-B',$Child) 'synthetic-bootstrap' -ShowStages}
    catch {$value.error=[string]$_.Exception.Message}
}
$value | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $Result -Encoding UTF8
''', encoding="utf-8-sig")
        self.ps = Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"

    def start(self, body, mode="progress"):
        self.child.write_text(body, encoding="utf-8")
        env = os.environ.copy()
        env["PROGRESS_PASSWORD"] = "ENV_PASSWORD_CANARY_123"
        process = subprocess.Popen([str(self.ps), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(self.fixture),
            "-Source", str(PORTABLE / "bootstrap.ps1"), "-Python", sys.executable, "-Child", str(self.child), "-Result", str(self.result), "-Mode", mode],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", env=env)
        self.addCleanup(lambda: process.kill() if process.poll() is None else None)
        lines = []
        updates = queue.Queue()
        def drain():
            for line in process.stdout:
                item = (time.monotonic(), line.rstrip("\r\n"))
                lines.append(item)
                updates.put(item)
            process.stdout.close()
        reader = threading.Thread(target=drain, daemon=True)
        reader.start()
        return process, lines, updates, reader

    def finish(self, process, lines, reader):
        exit_code = process.wait(timeout=25)
        reader.join(timeout=3)
        self.assertFalse(reader.is_alive())
        output = "\n".join(line for _, line in lines)
        self.assertEqual(exit_code, 0, output)
        data = json.loads(self.result.read_text(encoding="utf-8-sig"))
        self.assertTrue(data["version"].startswith("5.1."), data["version"])
        return output, data

    def test_exact_stdout_stage_arrives_before_slow_child_finishes_and_secrets_stay_hidden(self):
        body = "import pathlib,sys,time\n" + f"print({STAGE!r},flush=True)\n" + (
            'print("OPAQUE_PRIVATE_CANARY_123",flush=True)\n'
            'print("阶段：AstrBot环境：导入检查 token=TAIL_CANARY_123",flush=True)\n'
            'print("ValueError: ENV_PASSWORD_CANARY_123",file=sys.stderr,flush=True)\n'
            f'print({STAGE!r},file=sys.stderr,flush=True)\n'
            'time.sleep(2.5)\n'
            'print("阶段：启动本包桥接、AstrBot 与控制面板",flush=True)\n'
            f'pathlib.Path({str(self.finished)!r}).write_text("done")\n')
        process, lines, updates, reader = self.start(body)
        early = updates.get(timeout=12)
        self.assertEqual(early[1], STAGE)
        self.assertIsNone(process.poll())
        self.assertFalse(self.finished.exists())
        # Even an exact-looking stderr stage and a suffixed stdout line stay
        # buffered; only the one approved, complete stdout line streams.
        time.sleep(.25)
        self.assertEqual([line for _, line in lines], [STAGE])
        output, data = self.finish(process, lines, reader)
        self.assertEqual(output.splitlines().count(STAGE), 1)
        self.assertEqual(output.splitlines().count("阶段：启动本包桥接、AstrBot 与控制面板"), 1)
        self.assertEqual(data["error"], "")
        for canary in ("OPAQUE_PRIVATE_CANARY_123", "TAIL_CANARY_123", "ENV_PASSWORD_CANARY_123"):
            self.assertNotIn(canary, output)

    def test_failure_keeps_stage_and_safe_cause_without_raw_secrets(self):
        process, lines, _, reader = self.start('import sys,time\n' + f'print({STAGE!r},flush=True)\n' +
            'time.sleep(.2)\nprint("ImportError: synthetic failure token=FAILURE_CANARY_123",file=sys.stderr,flush=True)\nsys.exit(7)\n')
        output, data = self.finish(process, lines, reader)
        self.assertIn(STAGE, data["error"])
        self.assertIn("子进程退出码 7", data["error"])
        self.assertIn("ImportError: synthetic failure", data["error"])
        self.assertNotIn("FAILURE_CANARY_123", output + data["error"])

    def test_capture_output_excludes_late_native_stderr_and_does_not_stream(self):
        process, lines, _, reader = self.start('import sys,time\nprint("C:/synthetic/python.exe",flush=True)\ntime.sleep(.2)\nprint("warning: late stderr",file=sys.stderr,flush=True)\n', mode="capture")
        output, data = self.finish(process, lines, reader)
        self.assertEqual(data["captured"], ["C:/synthetic/python.exe"])
        self.assertEqual(output, "")

    def test_default_invocation_still_buffers_until_child_exit(self):
        process, lines, _, reader = self.start('import pathlib,time\n' + f'print({STAGE!r},flush=True)\n' +
            f'pathlib.Path({str(self.finished)!r}).write_text("started")\ntime.sleep(1.2)\n', mode="default")
        deadline = time.monotonic() + 12
        while not self.finished.exists() and time.monotonic() < deadline:
            time.sleep(.02)
        self.assertTrue(self.finished.exists())
        self.assertIsNone(process.poll())
        self.assertEqual(lines, [])
        output, data = self.finish(process, lines, reader)
        self.assertEqual(output.strip(), STAGE)
        self.assertEqual(data["error"], "")


if __name__ == "__main__":
    unittest.main()
