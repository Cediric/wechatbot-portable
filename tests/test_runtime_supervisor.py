"""Runtime recovery checks: synthetic workers only, no UIA/network/process starts."""
from concurrent.futures import Future
from contextlib import nullcontext
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import desktop_coordinator as desktop
import runtime_supervisor as runtime


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.now = [100.0]
        self.state = SimpleNamespace(running=True, paused=threading.Event(), send_cancel=threading.Event(),
            send_queue=None, bridge_thread=None, ob_client_thread=None, autopilot=None, moments_interactions=None,
            run_lock=threading.RLock())
        self.exit = Mock()
        self.monitor = runtime.WorkerMonitor(self.state, Path(self.temp.name)/"health.json",
            clock=lambda: self.now[0], exit_process=self.exit)

    @unittest.skipUnless(runtime.os.name == "nt", "Windows reader blocks atomic replace")
    def test_brief_supervisor_read_does_not_drop_heartbeat(self):
        path = self.monitor.path
        runtime._write(path, {"beat": 1})
        with path.open("rb") as reader, patch.object(runtime.time, "sleep", side_effect=lambda _: reader.close()) as pause:
            runtime._write(path, {"beat": 2})
        pause.assert_called_once()
        self.assertEqual(runtime._read(path), {"beat": 2})

    def test_modes_do_not_resurrect_stop_or_unverified_work(self):
        self.assertIsNone(runtime.restart_mode(0, {"mode": "active"}))
        self.assertIsNone(runtime.restart_mode(1, {"mode": "stopped"}))
        self.assertEqual(runtime.restart_mode(75, {"mode": "paused", "cleanup_confirmed": True}), "paused")
        self.assertEqual(runtime.restart_mode(75, {"mode": "active", "cleanup_confirmed": True}), "active")
        self.assertEqual(runtime.restart_mode(1, {"mode": "active", "gate_active": False,
            "desktop": {"active": False, "external_state": []}}), "paused")
        self.assertEqual(runtime.restart_mode(1, {}), "paused")
        self.assertEqual(runtime.restart_mode(1, {"mode": "active", "gate_active": True}), "paused")

    def test_stalled_desktop_cancels_before_one_recovery_job(self):
        self.monitor.publish = Mock()
        worker = Mock()
        with patch.object(desktop, "desktop_snapshot", return_value={"active": True,"idle_seconds": 46}), \
                patch.object(runtime.threading, "Thread", return_value=worker) as thread:
            self.monitor.tick()
            self.monitor.tick()
        self.assertTrue(self.state.send_cancel.is_set())
        self.assertTrue(self.monitor.recovering)
        self.assertEqual(thread.call_count, 1)
        worker.start.assert_called_once()
        self.exit.assert_not_called()

    def test_normal_progress_and_user_pause_are_not_faults(self):
        self.monitor.publish = Mock()
        with patch.object(desktop, "desktop_snapshot", return_value={"active": True,"idle_seconds": 2}):
            self.monitor.tick()
        self.assertFalse(self.monitor.recovering)
        self.state.paused.set()
        with patch.object(desktop, "desktop_snapshot", return_value={"active": True,"idle_seconds": 100}):
            self.monitor.tick()
        self.assertFalse(self.monitor.recovering)

    def test_lifecycle_transition_does_not_block_heartbeat_or_trigger_recovery(self):
        entered, finish = threading.Event(), threading.Event()
        dead = Mock()
        dead.is_alive.return_value = False
        self.state.bridge_thread = dead
        self.now[0] += 20
        self.monitor.publish = Mock()
        def reconnect():
            with self.state.run_lock:
                entered.set()
                finish.wait(2)
                live = Mock()
                live.is_alive.return_value = True
                self.state.bridge_thread = live
        updater = threading.Thread(target=reconnect)
        updater.start()
        try:
            self.assertTrue(entered.wait(1))
            with patch.object(runtime.threading, "Thread") as recovery:
                self.monitor.tick()
                self.monitor.publish.assert_called_once()
                recovery.assert_not_called()
            self.assertFalse(self.state.send_cancel.is_set())
            self.assertFalse(self.monitor.recovering)
        finally:
            finish.set()
            updater.join(2)
        self.monitor.tick()
        self.assertFalse(self.monitor.recovering)

    def test_restore_confirmation_precedes_process_exit(self):
        observations = []
        self.monitor.publish = lambda: observations.append(self.monitor.cleanup_confirmed)
        self.monitor.exit_process.side_effect = lambda code: observations.append(("exit",code))
        manager = Mock()
        manager.close.return_value = {"ok": True}
        with patch.object(desktop, "restore_external_states", return_value=True), \
                patch("uia_recovery.shared_gate_manager", return_value=manager):
            self.monitor._restore_and_exit()
        self.assertEqual(observations, [True, ("exit",75)])

    def test_unrestored_external_state_keeps_old_process(self):
        self.monitor.publish = Mock()
        self.monitor.stop_event.set()
        with patch.object(desktop, "restore_external_states", return_value=False):
            self.monitor._restore_and_exit()
        self.assertEqual(self.monitor.status,"blocked")
        self.exit.assert_not_called()

    def test_user_stop_during_cleanup_exits_without_restart_code(self):
        self.monitor.mode = "stopped"
        self.monitor.publish = Mock()
        manager = Mock()
        manager.close.return_value = {"ok": True}
        with patch.object(desktop,"restore_external_states",return_value=True), \
                patch("uia_recovery.shared_gate_manager",return_value=manager):
            self.monitor._restore_and_exit()
        self.exit.assert_called_once_with(0)

    def test_blocked_restore_is_observable_without_blocking_heartbeat(self):
        self.monitor.publish = Mock()
        self.monitor.recovering = True
        self.monitor.recovery_started = 70
        self.monitor.status = "recovering"
        self.monitor.tick()
        self.assertEqual(self.monitor.status,"blocked")
        self.monitor.publish.assert_called()

    def test_nested_desktop_context_keeps_original_activity(self):
        with desktop.desktop_operation():
            first = desktop._activity
            with desktop.desktop_operation():
                self.assertIs(desktop._activity, first)
            self.assertIs(desktop._activity, first)
        self.assertFalse(desktop.desktop_snapshot()["active"])

    def test_late_old_clipboard_writer_is_cancelled_after_recovery(self):
        adapter = Mock()
        adapter.snapshot_with_sequence.return_value = ({13:b"old"}, 1)
        adapter.set_text.return_value = 2
        adapter.replace.return_value = False # user copied newer content
        with desktop.ClipboardLease(adapter) as lease:
            lease.set_text("prepared")
            self.assertIn("clipboard",desktop.desktop_snapshot()["external_state"])
            self.assertTrue(lease._restore_for_recovery())
            with self.assertRaises(desktop.DesktopCancelled):
                lease.set_text("late old request")
        self.assertNotIn("clipboard",desktop.desktop_snapshot()["external_state"])
        adapter.replace.assert_called_once_with({13:b"old"},expected_sequence=2)

    def test_in_progress_clipboard_write_is_not_mistaken_for_safe(self):
        entered, finish = threading.Event(), threading.Event()
        adapter = Mock()
        adapter.snapshot_with_sequence.return_value = ({13:b"old"}, 1)
        def write(*args, **kwargs):
            entered.set()
            self.assertTrue(finish.wait(2))
            return 2
        adapter.set_text.side_effect = write
        lease = desktop.ClipboardLease(adapter)
        writer = threading.Thread(target=lambda: lease.set_text("prepared"))
        writer.start()
        try:
            self.assertTrue(entered.wait(1))
            self.assertFalse(lease._restore_for_recovery())
            adapter.replace.assert_not_called()
            self.assertIn("clipboard",desktop.desktop_snapshot()["external_state"])
        finally:
            finish.set()
            writer.join(2)
        self.assertTrue(lease._restore_for_recovery())
        adapter.replace.assert_called_once_with({13:b"old"},expected_sequence=2)

    def test_new_active_mode_gets_its_own_startup_grace(self):
        self.monitor.publish = Mock()
        self.monitor.inactive_since = 10
        self.now[0] = 200
        self.monitor.set_mode("active")
        self.assertEqual(self.monitor.started,200)
        self.assertIsNone(self.monitor.inactive_since)

    def test_partly_failing_clipboard_write_keeps_unknown_state(self):
        adapter = Mock()
        adapter.snapshot_with_sequence.return_value = ({13:b"old"}, 1)
        adapter.set_text.side_effect = OSError("synthetic partial native write")
        lease = desktop.ClipboardLease(adapter)
        try:
            with self.assertRaises(OSError):
                lease.set_text("prepared")
            self.assertFalse(lease._restore_for_recovery())
            self.assertIn("clipboard",desktop.desktop_snapshot()["external_state"])
            adapter.replace.assert_not_called()
        finally:
            desktop.release_external_state(lease._external_token)

    def test_supervisor_waits_for_old_exit_before_next_spawn(self):
        events = []
        class Child:
            def __init__(self, code): self.code=code
            def poll(self): return self.code
            def wait(self): events.append(("wait",self.code)); return self.code
        codes=iter([75,0])
        def spawn(*args,**kwargs):
            events.append("spawn")
            return Child(next(codes))
        with patch("startup_control._pid_guard",return_value=nullcontext()), \
                patch("startup_control.owned_running",return_value=None), \
                patch.object(runtime.subprocess,"Popen",side_effect=spawn), \
                patch.object(runtime,"_read",return_value={"cleanup_confirmed":True,"mode":"active"}), \
                patch.object(runtime.time,"sleep"):
            self.assertEqual(runtime.run(),0)
        self.assertEqual(events,["spawn",("wait",75),"spawn",("wait",0)])

    def test_real_dummy_children_restart_in_a_portable_path(self):
        # This starts two short stdlib-only dummy processes, never main services.
        root = Path(self.temp.name)/"中文便携包 (1)"
        root.mkdir()
        (root/"main.py").write_text(
            "import os,json,pathlib,time\n"
            "n=int(os.environ['WECHATBOT_RESTART_COUNT'])\n"
            "p=pathlib.Path(os.environ['WECHATBOT_HEALTH_PATH'])\n"
            "p.parent.mkdir(parents=True,exist_ok=True)\n"
            "p.write_text(json.dumps({'cleanup_confirmed':True,'mode':'active','heartbeat':time.time()}),encoding='utf-8')\n"
            "with (pathlib.Path(__file__).parent/'trace.txt').open('a',encoding='utf-8') as f: f.write(str(n)+'\\n')\n"
            "raise SystemExit(75 if n==0 else 0)\n", encoding="utf-8")
        with patch.object(runtime,"__file__",str(root/"runtime_supervisor.py")), \
                patch("startup_control._pid_guard",return_value=nullcontext()), \
                patch("startup_control.owned_running",return_value=None):
            self.assertEqual(runtime.run(),0)
        self.assertEqual((root/"trace.txt").read_text(encoding="utf-8"),"0\n1\n")


if __name__ == "__main__":
    unittest.main()
