"""Independent lifecycle integration checks. All desktop/network workers are mocked."""
import asyncio
from concurrent.futures import Future
import hashlib
import importlib.util
from pathlib import Path
import tempfile
import threading
import types
import unittest
import wave
from unittest.mock import Mock, patch

SOURCE = Path(__file__).resolve().parents[1] / "wechat-weflow-bridge-ob11"


def module(name, **values):
    result = types.ModuleType(name)
    result.__dict__.update(values)
    return result


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, SOURCE / filename)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def run_inline(coroutine):
    # These fixtures return completed futures. Avoid a Windows selector self-socket,
    # which is unrelated to lifecycle behavior and restricted in the test sandbox.
    try:
        coroutine.send(None)
    except StopIteration as completed:
        return completed.value
    finally:
        coroutine.close()
    raise AssertionError("Mocked protocol unexpectedly waited for network work")


class FakeRegistry:
    def __init__(self):
        self.path = ""
        self._lock = threading.RLock()
        self.private = {"1": {"name": "friend", "session_id": "wxid_friend"}}
        self.groups, self.members = {}, {}
    def flush(self):
        pass
    def get_private_record(self, key):
        return dict(self.private.get(str(key), {}))
    def get_group_record(self, key):
        return dict(self.groups.get(str(key), {}))


class FakeMemory:
    def __init__(self, path="", enabled=True, *, extraction_paused=False):
        self.enabled = enabled
        self.extraction_paused = extraction_paused
    def set_extraction_paused(self, value):
        self.extraction_paused = bool(value)
    def count(self):
        return 0


class FakeOutbox:
    def __init__(self, path="", limit=100):
        self.max_events = limit
    def stats(self):
        return {"pending": 0}


class FakeAutopilot:
    def __init__(self, memory=None, sender=None):
        self.enabled = False
        self._cancel = threading.Event()
        self.moments_sender = sender
        self.last_run, self.last_result = 0, "idle"
        self.pending = None
    def pending_snapshot(self):
        return self.pending
    def set_enabled(self, value):
        self.enabled = value
    def start(self):
        pass


class FakeInteractions:
    """Lifecycle fixture: records bound identities without desktop/model work."""
    def __init__(self, memory=None, sender=None):
        self.sender = sender
        self.enabled = False
        self._cancel = threading.Event()
        self.running = False
        self.started = self.stopped = 0
        self.calls = []
        self.pending = None
    def cancel_pending(self):
        self._cancel.set()
    def set_enabled(self, value):
        self.cancel_pending()
        self.enabled = bool(value)
        if self.enabled:
            self._cancel = threading.Event()
    def start(self):
        self.started += 1
    def stop(self):
        self.stopped += 1
        self.cancel_pending()
    def status_snapshot(self):
        return {"enabled": self.enabled, "running": self.running, "pending": self.pending}
    def run_once(self, **expected):
        import state
        self.calls.append(expected)
        stale = (expected.get("expected_cancel") is not self._cancel
                 or expected.get("expected_sender") is not self.sender
                 or expected.get("expected_generation") != state.bridge_generation
                 or self._cancel.is_set() or not state.running or state.paused.is_set())
        self.last_result = "cancelled" if stale else "ran"
        return {"ok": not stale, "status": self.last_result}


class FakeQueue:
    def __init__(self, limit=10):
        self.closed = False
    def close(self):
        self.closed = True
    def stats(self):
        return {"pending": 0}
    def submit(self, message_id, func, cancel_check):
        result = Future()
        if self.closed:
            result.set_exception(RuntimeError("closed"))
        else:
            try:
                result.set_result(False if cancel_check() else func())
            except Exception as error:
                result.set_exception(error)
        return result


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix=".lifecycle-test-", dir=SOURCE.parent / "tests")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = module("config", BASE_DIR=str(self.root), DATA_DIR=str(self.root),
                             BOT_WXID="bot", BOT_NICKNAMES=["小助手"], GROUP_REPLY_MODE="mention",
                             SEND_QUEUE_LIMIT=10, OUTBOX_MAX_EVENTS=100, MEMORY_DB_PATH="synthetic.db",
                             MEMORY_ENABLED=True, AUTOPILOT_ENABLED=False, VOICE_ENABLED=True,
                             MOMENTS_INTERACTIONS_ENABLED=False,
                             STICKER_ENABLED=True, MEDIA_MAX_MB=30, API_SEND_WAIT_SECONDS=.2,
                             ASTRBOT_ATTACHMENTS="", IMAGE_CAPTION_PROVIDER="ollama",
                             IMAGE_CAPTION_MODEL="synthetic", IMAGE_CAPTION_API_KEY="",
                             IMAGE_CAPTION_API_BASE="", IMAGE_CAPTION_PROMPT="describe",
                             OLLAMA_BASE_URL="http://synthetic.invalid", OLLAMA_TIMEOUT=1,
                             ensure_data_dirs=lambda: None, redact_text=str)
        self.config.config = {"access_token": "old", "core_workers": 4, "web_port": 8766,
                              "group_reply_mode": "mention"}
        def save(updates):
            self.config.config.update(updates)
            for key, value in updates.items():
                setattr(self.config, "WE_FLOW_BASE_URL" if key == "weflow_base_url" else key.upper(), value)
            return dict(self.config.config)
        self.config.save_config = save
        self.monitors, self.created_senders, self.background = [], [], []
        self.names = module("name_refresh", get_sync_status=lambda: {"status": "ok"},
                            refresh_target=lambda *a, **kw: "friend")
        def monitor(generation):
            result = types.SimpleNamespace(generation=generation, stopped=False)
            result.stop = lambda: setattr(result, "stopped", True)
            self.monitors.append(result)
            return result
        self.names.start_name_monitor = monitor
        self.queue_module = module("send_queue", SendQueue=FakeQueue)
        self.stable = module("stable_id", IdentityRegistry=FakeRegistry,
                             stable_id=lambda value: int.from_bytes(hashlib.sha256(str(value).encode()).digest()[:8], "big"))
        def validate_synthetic_username(value):
            if not isinstance(value, str) or not value.strip() or value.endswith("@chatroom"):
                raise ValueError("invalid synthetic username")
            return value
        self.stable.validate_username = validate_synthetic_username
        self.imports = patch.dict("sys.modules", {
            "config": self.config, "stable_id": self.stable, "name_refresh": self.names,
            "send_queue": self.queue_module, "outbox": module("outbox", Outbox=FakeOutbox),
            "memory_store": module("memory_store", MemoryStore=FakeMemory),
            "autopilot": module("autopilot", Autopilot=FakeAutopilot),
            "moments_interactions": module("moments_interactions", MomentsInteractions=FakeInteractions),
            "http_util": module("http_util"),
            "ob_client": module("ob_client", _run_ob_client=Mock(name="no_network")),
            "bridge_core": module("bridge_core", WeFlowBridge=Mock(name="no_sse")),
            "sticker_map": module("sticker_map", resolve_face=lambda value: "[表情]"),
            "tts": module("tts", synthesize_voice=Mock(name="no_paid_tts")),
            "humanize": module("humanize", voice_script=lambda value: value, wait_response_floor=lambda started, cancelled: not cancelled(), wait_remaining=lambda started, minimum, cancelled: not cancelled()),
        })
        self.imports.start()
        self.addCleanup(self.imports.stop)
        self.state = load("isolated_lifecycle_state", "state.py")
        self.state_patch = patch.dict("sys.modules", {"state": self.state})
        self.state_patch.start()
        self.addCleanup(self.state_patch.stop)
        case = self
        class Sender:
            def __init__(self):
                self.sent = []
                self.close_count = 0
                self.prepare_count = 0
            def close(self):
                self.close_count += 1
                return {"ok":True,"status":"restored"}
            def prepare(self, cancel_event=None, generation=None):
                if not cancel_event or not cancel_event.is_set():
                    self.prepare_count += 1
            def diagnostics(self):
                return {"ready": True}
            def send_text(self, contact, text, **kwargs):
                if kwargs.get("cancel_event") and kwargs["cancel_event"].is_set():
                    return False
                self.sent.append(("text", contact, text))
                return True
            def send_voice(self, contact, audio, **kwargs):
                self.sent.append(("voice", contact, audio))
                return True
        def sender():
            result = Sender()
            self.created_senders.append(result)
            return result
        self.social_senders = []
        def social_sender():
            result = Sender()
            self.social_senders.append(result)
            return result
        self.sender_module = module("senders", create_sender=sender, create_moments_sender=lambda: None,
                                    create_moments_social_sender=social_sender)
        self.sender_patch = patch.dict("sys.modules", {"senders": self.sender_module})
        self.sender_patch.start()
        self.addCleanup(self.sender_patch.stop)
        self.main = load("isolated_lifecycle_main", "main.py")
        class BackgroundThread:
            def __init__(self, target, args=(), daemon=False, name=""):
                self.target, self.args, self.name = target, args, name
                self.started = False
            def start(self):
                self.started = True
                case.background.append(self)
        self.main.threading = types.SimpleNamespace(Thread=BackgroundThread, Event=threading.Event)
        self.protocol = load("isolated_lifecycle_protocol", "ob_protocol.py")
        async def completed_future(future, timeout=None):
            return future.result()
        self.protocol.asyncio = types.SimpleNamespace(wait_for=completed_future,
                                                     shield=lambda future: future,
                                                     wrap_future=lambda future: future,
                                                     TimeoutError=asyncio.TimeoutError)
        self.main.initialize_runtime()

    def enable_interactions(self):
        self.main._start_bridge()
        self.main.update_config({"moments_interactions_enabled": True})
        return self.state.moments_interactions

    def manual_worker(self):
        return next(worker for worker in reversed(self.background) if worker.name == "moments-interactions-manual")

    def test_interactions_status_and_callback_initialized_without_enabling(self):
        self.assertIs(self.state.run_moments_interactions, self.main.run_moments_interactions)
        self.assertFalse(self.state.moments_interactions.enabled)
        self.assertIsNone(self.state.moments_interactions.sender)
        self.assertEqual(self.main.get_status()["moments_interactions"], {"enabled": False, "running": False, "pending": None})
        del self.state.moments_interactions
        self.assertFalse(self.main.get_status()["moments_interactions"]["enabled"])

    def test_manual_interactions_cannot_enable_stopped_disabled_or_paused_feature(self):
        self.assertFalse(self.main.run_moments_interactions()["ok"])
        self.main._start_bridge()
        self.assertEqual(self.main.run_moments_interactions()["status"], "disabled")
        self.assertFalse(self.config.MOMENTS_INTERACTIONS_ENABLED)
        self.assertEqual(self.social_senders, [])
        self.main.update_config({"moments_interactions_enabled": True})
        self.main._pause_bridge()
        self.assertEqual(self.main.run_moments_interactions()["status"], "unavailable")
        self.assertFalse(any(worker.name == "moments-interactions-manual" for worker in self.background))

    def test_manual_interactions_reserve_once_and_bind_original_lifecycle(self):
        controller = self.enable_interactions()
        expected = {"expected_generation": self.state.bridge_generation,
                    "expected_cancel": controller._cancel, "expected_sender": controller.sender, "manual": True}
        self.assertTrue(self.main.run_moments_interactions()["ok"])
        self.assertEqual(self.main.run_moments_interactions()["status"], "busy")
        worker = self.manual_worker()
        worker.target(*worker.args)
        self.assertEqual(controller.calls, [expected])
        self.assertEqual(controller.last_result, "ran")
        self.assertIsNone(self.main._moments_manual_request)
        self.assertTrue(self.main.run_moments_interactions()["ok"])

    def test_manual_missing_account_identity_is_reported_before_desktop_or_thread(self):
        controller = self.enable_interactions()
        self.config.BOT_WXID = ""
        response = self.main.run_moments_interactions()
        self.assertFalse(response["ok"])
        self.assertEqual(response["status"], "account_identity_missing")
        self.assertEqual(self.main._moments_manual_status["blocked_reason"], "account_identity_missing")
        self.assertFalse(any(worker.name == "moments-interactions-manual" for worker in self.background))
        self.assertEqual(controller.calls, [])

    def test_existing_scheduled_interaction_rejects_manual_reentry(self):
        controller = self.enable_interactions()
        controller.running = True
        self.assertEqual(self.main.run_moments_interactions()["status"], "busy")
        self.assertFalse(any(worker.name == "moments-interactions-manual" for worker in self.background))

    def test_settings_change_cancels_delayed_manual_request_without_chat_restart(self):
        controller = self.enable_interactions()
        generation, old_token = self.state.bridge_generation, controller._cancel
        self.main.run_moments_interactions()
        worker = self.manual_worker()
        self.main.update_config({"group_reply_mode": "batch"})
        self.assertEqual(self.state.bridge_generation, generation)
        self.assertTrue(old_token.is_set())
        self.assertIsNot(controller._cancel, old_token)
        worker.target(*worker.args)
        self.assertEqual(controller.last_result, "cancelled")

    def test_pause_cancels_interaction_without_join_and_resume_detaches_old_sender(self):
        controller = self.enable_interactions()
        sender, token = controller.sender, controller._cancel
        self.main._pause_bridge()
        self.assertTrue(token.is_set())
        self.assertEqual(controller.stopped, 0, "stop joins must not occur inside run_lock")
        self.assertIs(controller.sender, sender)
        self.main._resume_bridge()
        self.assertEqual(sender.close_count, 1)
        self.assertIsNot(controller.sender, sender)
        self.assertFalse(controller._cancel.is_set())

    def test_stop_detaches_and_restores_interactions_once_and_invalidates_delayed_request(self):
        controller = self.enable_interactions()
        sender, token = controller.sender, controller._cancel
        self.main.run_moments_interactions()
        worker = self.manual_worker()
        def close():
            self.assertTrue(token.is_set())
            self.assertIsNone(controller.sender)
            sender.close_count += 1
            return {"ok": True, "status": "restored"}
        sender.close = close
        self.main._stop_bridge()
        self.main._stop_bridge()
        self.assertEqual(sender.close_count, 1)
        self.assertEqual(controller.stopped, 0)
        worker.target(*worker.args)
        self.assertEqual(controller.last_result, "cancelled")

    def test_disable_interactions_restoration_failure_is_retained_before_reenable(self):
        controller = self.enable_interactions()
        sender, token = controller.sender, controller._cancel
        sender.close = Mock(return_value={"ok": False, "status": "restore_failed"})
        self.main.update_config({"moments_interactions_enabled": False})
        self.assertTrue(token.is_set())
        self.assertIsNone(controller.sender)
        self.assertIn(sender, self.main._pending_sender_closes)
        with self.assertRaises(RuntimeError):
            self.main.update_config({"moments_interactions_enabled": True})
        self.assertEqual(len(self.social_senders), 1)
        self.assertTrue(controller._cancel.is_set())
        sender.close.return_value = {"ok": True, "status": "restored"}
        self.main.update_config({"moments_interactions_enabled": True})
        self.assertNotIn(sender, self.main._pending_sender_closes)
        self.assertIsNot(controller.sender, sender)

    def test_failed_autopilot_restore_cancels_existing_social_sender_and_blocks_manual(self):
        controller = self.enable_interactions()
        sender, token = controller.sender, controller._cancel
        moment = types.SimpleNamespace(close=Mock(return_value={"ok": False, "status": "restore_failed"}))
        self.state.autopilot.moments_sender = moment
        with self.assertRaises(RuntimeError):
            self.main.update_config({"autopilot_enabled": False})
        self.assertIs(controller.sender, sender, "failed unrelated restoration must retain the social sender")
        self.assertTrue(token.is_set())
        self.assertIs(controller._cancel, token, "the cancelled token cannot be renewed until restoration succeeds")
        self.assertIn(moment, self.main._pending_sender_closes)
        self.assertEqual(self.main.run_moments_interactions()["status"], "restore_failed")
        self.assertFalse(any(worker.name == "moments-interactions-manual" for worker in self.background))
        controller.run_once(expected_generation=self.state.bridge_generation,
            expected_cancel=token, expected_sender=sender)
        self.assertEqual(controller.last_result, "cancelled", "scheduled work is also cancelled")
        moment.close.return_value = {"ok": True, "status": "restored"}
        self.main.update_config({"autopilot_enabled": False})
        self.assertEqual(self.main._pending_sender_closes, [])
        self.assertFalse(controller._cancel.is_set())
        self.assertIsNot(controller._cancel, token)
        self.assertTrue(self.main.run_moments_interactions()["ok"])

    def test_interrupted_unrelated_restore_cancels_social_token_and_blocks_manual(self):
        controller = self.enable_interactions()
        sender = types.SimpleNamespace(close=Mock(side_effect=KeyboardInterrupt()))
        token = controller._cancel
        with self.assertRaises(KeyboardInterrupt):
            self.main._close_uia_senders([sender])
        self.assertTrue(token.is_set())
        self.assertIn(sender, self.main._pending_sender_closes)
        self.assertEqual(self.main.run_moments_interactions()["status"], "restore_failed")

    def test_interactions_start_failure_rolls_back_and_closes_owned_sender(self):
        self.config.MOMENTS_INTERACTIONS_ENABLED = True
        controller = self.state.moments_interactions
        controller.start = Mock(side_effect=RuntimeError("synthetic scheduling failure"))
        with self.assertRaises(RuntimeError):
            self.main._start_bridge()
        self.assertFalse(self.state.running)
        self.assertIsNone(controller.sender)
        self.assertTrue(controller._cancel.is_set())
        self.assertEqual(self.social_senders[0].close_count, 1)

    def test_manual_thread_start_failure_releases_reservation(self):
        self.enable_interactions()
        with patch.object(self.main.threading.Thread, "start", side_effect=RuntimeError("synthetic worker failure")):
            with self.assertRaises(RuntimeError):
                self.main.run_moments_interactions()
        self.assertIsNone(self.main._moments_manual_request)

    def test_inflight_manual_model_work_does_not_hold_run_lock_or_block_pause(self):
        controller = self.enable_interactions()
        entered, release = threading.Event(), threading.Event()
        original = controller.run_once
        def waiting_model(**expected):
            entered.set()
            self.assertTrue(release.wait(2))
            return original(**expected)
        controller.run_once = waiting_model
        real_workers = []
        def real_thread(**kwargs):
            worker = threading.Thread(**kwargs)
            real_workers.append(worker)
            return worker
        self.main.threading = types.SimpleNamespace(Thread=real_thread, Event=threading.Event)
        try:
            self.assertTrue(self.main.run_moments_interactions()["ok"])
            self.assertTrue(entered.wait(1))
            self.assertEqual(self.main.run_moments_interactions()["status"], "busy")
            paused = threading.Event()
            pause_worker = threading.Thread(target=lambda: (self.main._pause_bridge(), paused.set()))
            pause_worker.start()
            self.assertTrue(paused.wait(1), "model/UIA work must not hold the lifecycle lock")
            pause_worker.join(1)
        finally:
            release.set()
            for worker in real_workers:
                worker.join(2)
        self.assertTrue(controller._cancel.is_set())
        self.assertEqual(controller.last_result, "cancelled")
        self.assertIsNone(self.main._moments_manual_request)

    def test_status_exposes_pending_even_when_autopilot_disabled(self):
        self.assertFalse(self.state.autopilot.enabled)
        self.assertIsNone(self.main.get_status()["autopilot_pending"])
        for pending in ({"intent_id": "fixture-id", "content": "朋友圈正文", "time": "2026-10-11 18:20:30", "image_path": ""},
                        {"status": "record_error", "error": "无法读取记录"}):
            self.state.autopilot.pending = pending
            self.assertEqual(self.main.get_status()["autopilot_pending"], pending)
        self.state.autopilot = None
        self.assertIsNone(self.main.get_status()["autopilot_pending"])

    def test_simultaneous_start_is_one_owned_generation(self):
        barrier = threading.Barrier(9)
        errors = []
        def start():
            barrier.wait()
            try:
                self.main._start_bridge()
            except Exception as error:
                errors.append(error)
        threads = [threading.Thread(target=start) for _ in range(8)]
        for worker in threads:
            worker.start()
        barrier.wait()
        for worker in threads:
            worker.join(2)
        self.assertFalse(any(worker.is_alive() for worker in threads))
        self.assertEqual(errors, [])
        self.assertEqual(len(self.created_senders), 1)
        self.assertEqual(self.state.bridge_generation, 1)
        self.assertTrue(self.state.running)

    def test_stopped_or_replaced_preparation_cannot_change_old_sender(self):
        self.main._start_bridge()
        old=self.created_senders[-1]
        old_worker=next(w for w in self.background if w.name=="uia-prepare")
        self.main._stop_bridge()
        self.main._start_bridge()
        old_worker.target(*old_worker.args)
        self.assertEqual(old.prepare_count,0)
        self.assertEqual(old.close_count,1)
        new=self.created_senders[-1]
        self.main.prepare_uia(new,self.state.bridge_generation)
        self.assertEqual(new.prepare_count,1)

    def test_stop_restores_chat_and_moments_owned_resources_once(self):
        self.main._start_bridge()
        sender=self.created_senders[-1]
        moment=types.SimpleNamespace(close=Mock(return_value={"ok":True,"status":"restored"}))
        self.state.autopilot.moments_sender=moment
        self.main._stop_bridge()
        self.main._stop_bridge()
        self.assertEqual(sender.close_count,1)
        moment.close.assert_called_once()
        self.assertIsNone(self.state.autopilot.moments_sender)
        self.assertFalse(self.main.get_status()["uia"]["ready"])

    def test_failed_restore_is_retained_and_retry_is_truthful(self):
        self.main._start_bridge()
        sender=self.created_senders[-1]
        sender.close=Mock(return_value={"ok":False,"status":"restore_failed"})
        result=self.main._stop_bridge()
        self.assertFalse(result["uia_restore_result"]["ok"])
        with self.assertRaises(RuntimeError):
            self.main._start_bridge()
        sender.close.return_value={"ok":True,"status":"restored"}
        self.assertTrue(self.main._stop_bridge()["uia_restore_result"]["ok"])
        self.assertEqual(self.main._pending_sender_closes,[])

    def test_interrupted_restore_keeps_sender_for_later_retry(self):
        for interrupt in (KeyboardInterrupt,SystemExit):
            with self.subTest(interrupt=interrupt):
                self.main._start_bridge()
                sender=self.created_senders[-1]
                sender.close=Mock(side_effect=interrupt())
                with self.assertRaises(interrupt):
                    self.main._stop_bridge()
                self.assertIn(sender,self.main._pending_sender_closes)
                self.assertFalse(self.main.get_status()["uia_restore_result"]["ok"])
                sender.close=Mock(return_value={"ok":True,"status":"restored"})
                self.assertTrue(self.main._stop_bridge()["uia_restore_result"]["ok"])

    def test_late_diagnostics_cannot_overwrite_stopped_state(self):
        self.main._start_bridge()
        sender=self.created_senders[-1]
        captured=threading.Event()
        release=threading.Event()
        def scan():
            captured.set()
            release.wait(2)
            return {"ready":True,"current_chat":"old"}
        sender.diagnostics=scan
        worker=threading.Thread(target=self.main.refresh_uia_diagnostics)
        worker.start()
        self.assertTrue(captured.wait(1))
        self.main._stop_bridge()
        release.set()
        worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertFalse(self.main.get_status()["uia"]["ready"])

    def test_paused_reconnect_does_not_prepare_desktop(self):
        self.main._start_bridge(initially_paused=True)
        self.assertFalse(any(w.name=="uia-prepare" for w in self.background))

    def test_recovery_config_change_closes_and_replaces_sender(self):
        self.main._start_bridge()
        old=self.created_senders[-1]
        self.main.update_config({"uia_recovery_enabled":False})
        self.assertEqual(old.close_count,1)
        self.assertIsNot(old,self.state.sender_instance)

    def test_simultaneous_start_stop_never_deadlocks_or_keeps_old_sends(self):
        self.main._start_bridge()
        old_generation = self.state.bridge_generation
        old_cancel = self.state.send_cancel
        barrier = threading.Barrier(3)
        errors = []
        def run(action):
            barrier.wait()
            try:
                action()
            except Exception as error:
                errors.append(error)
        start = threading.Thread(target=run, args=(self.main._start_bridge,))
        stop = threading.Thread(target=run, args=(self.main._stop_bridge,))
        start.start(); stop.start(); barrier.wait()
        start.join(2); stop.join(2)
        self.assertFalse(start.is_alive() or stop.is_alive())
        self.assertEqual(errors, [])
        self.assertTrue(old_cancel.is_set())
        self.assertFalse(self.state.dispatch_send("send_text", "friend", "stale", target_id=1, generation=old_generation))

    def test_pause_resume_never_reuses_old_generation_or_cancel_token(self):
        self.main._start_bridge()
        old_generation, old_client = self.state.bridge_generation, self.state.client_generation
        old_cancel, old_monitor = self.state.send_cancel, self.state.name_monitor
        self.main._pause_bridge()
        self.assertFalse(self.state.dispatch_send("send_text", "friend", "paused", target_id=1, generation=old_generation))
        self.main._resume_bridge()
        self.assertTrue(old_cancel.is_set())
        self.assertTrue(old_monitor.stopped)
        self.assertNotEqual(self.state.bridge_generation, old_generation)
        self.assertIsNot(self.state.send_cancel, old_cancel)
        self.assertFalse(self.state.paused.is_set())
        self.assertFalse(self.state.dispatch_send("send_text", "friend", "old", target_id=1, generation=old_generation))
        response = run_inline(self.protocol._handle_ob_api({"action": "send_private_msg", "params": {"user_id": 1, "message": "old client"}}, generation=old_client))
        self.assertEqual(response["status"], "failed")
        self.assertTrue(self.state.dispatch_send("send_text", "friend", "new", target_id=1, generation=self.state.bridge_generation))
        self.assertEqual(self.state.sender_instance.sent[-1][2], "new")

    def test_successful_send_logs_receipt_without_reply_body(self):
        self.main._start_bridge()
        with self.assertLogs("ob11-bridge", level="INFO") as captured:
            self.assertTrue(self.state.dispatch_send("send_text", "friend", "private body sentinel", target_id=1))
        output = "\n".join(captured.output)
        self.assertIn("发送器已确认", output)
        self.assertIn("send_text", output)
        self.assertNotIn("private body sentinel", output)

    def test_hot_config_reconnect_preserves_pause(self):
        self.main._start_bridge()
        self.main._pause_bridge()
        old_generation = self.state.bridge_generation
        self.main.update_config({"access_token": "changed"})
        self.assertTrue(self.state.running)
        self.assertTrue(self.state.paused.is_set(), "Saving connection settings must not silently resume outgoing work")
        self.assertTrue(self.state.send_cancel.is_set())
        self.assertNotEqual(self.state.bridge_generation, old_generation)

    def test_queue_startup_failure_rolls_back_running_state(self):
        self.queue_module.SendQueue = Mock(side_effect=RuntimeError("synthetic queue startup failure"))
        with self.assertRaises(RuntimeError):
            self.main._start_bridge()
        self.assertFalse(self.state.running)
        self.assertTrue(self.state.send_cancel.is_set())
        self.assertIsNone(self.state.send_queue)
        self.assertIsNone(self.state.name_monitor)

    def test_monitor_startup_failure_closes_owned_resources(self):
        self.names.start_name_monitor = Mock(side_effect=RuntimeError("synthetic monitor startup failure"))
        with self.assertRaises(RuntimeError):
            self.main._start_bridge()
        self.assertFalse(self.state.running)
        self.assertTrue(self.state.send_cancel.is_set())
        self.assertIsNone(self.state.send_queue)
        self.assertIsNone(self.state.name_monitor)

    def test_background_thread_start_failure_rolls_back_generation(self):
        thread_type = self.main.threading.Thread
        original_start = thread_type.start
        def start(worker):
            if worker.name == "bridge":
                raise RuntimeError("synthetic thread startup failure")
            original_start(worker)
        with patch.object(thread_type, "start", start), self.assertRaises(RuntimeError):
            self.main._start_bridge()
        self.assertFalse(self.state.running)
        self.assertTrue(self.state.send_cancel.is_set())
        self.assertIsNone(self.state.send_queue)

    def test_delayed_old_sse_cleanup_closes_only_its_owned_response(self):
        self.main._start_bridge()
        old_response = types.SimpleNamespace(closed=False)
        old_response.close = lambda: setattr(old_response, "closed", True)
        old_bridge = types.SimpleNamespace(_sched=types.SimpleNamespace(stop=lambda: None), _sse_session=old_response)
        self.state.bridge_instance = old_bridge
        self.main._stop_bridge()
        close_worker = next(worker for worker in self.background if worker.name == "sse-close")
        self.main._start_bridge()
        new_response = types.SimpleNamespace(closed=False)
        new_bridge = types.SimpleNamespace(_sse_session=new_response)
        self.state.bridge_instance = new_bridge
        close_worker.target(*close_worker.args)
        self.assertTrue(old_response.closed)
        self.assertFalse(new_response.closed)
        self.assertIs(self.state.bridge_instance, new_bridge)

    def test_delayed_old_sse_thread_cannot_publish_over_new_generation(self):
        self.main._start_bridge()
        old_generation = self.state.bridge_generation
        self.main._stop_bridge()
        self.main._start_bridge()
        new_bridge = types.SimpleNamespace()
        self.state.bridge_instance = new_bridge
        old_bridge = types.SimpleNamespace(_sched=types.SimpleNamespace(stop=Mock()))
        self.main.WeFlowBridge = lambda sender: old_bridge
        self.main._bridge_loop(old_generation, self.created_senders[0])
        self.assertIs(self.state.bridge_instance, new_bridge)
        old_bridge._sched.stop.assert_called_once()

    def test_reconnect_sender_failure_stays_stopped_and_reports_failure(self):
        self.main._start_bridge()
        old_cancel = self.state.send_cancel
        self.main.create_sender = Mock(side_effect=RuntimeError("synthetic sender startup failure"))
        with self.assertRaises(RuntimeError):
            self.main.update_config({"access_token": "changed"})
        self.assertFalse(self.state.running)
        self.assertTrue(old_cancel.is_set())
        self.assertFalse(self.state.dispatch_send("send_text", "friend", "must not send", target_id=1))

    def test_non_connection_config_does_not_restart(self):
        self.main._start_bridge()
        generation = self.state.bridge_generation
        result = self.main.update_config({"group_reply_mode": "batch"})
        self.assertEqual(self.state.bridge_generation, generation)
        self.assertEqual(self.state.group_reply_mode, "batch")
        self.assertIn("group_reply_mode", result["changed_keys"])

    def test_config_reconnect_and_stop_are_one_lifecycle_transaction(self):
        self.main._start_bridge()
        stopped_for_update, release_update, user_stop_done = threading.Event(), threading.Event(), threading.Event()
        original_stop = self.main._stop_bridge
        errors = []
        def gated_stop(**kwargs):
            result = original_stop(**kwargs)
            stopped_for_update.set()
            if not release_update.wait(2):
                raise RuntimeError("test did not release configuration transaction")
            return result
        def update():
            try:
                self.main.update_config({"access_token": "changed"})
            except Exception as error:
                errors.append(error)
        def user_stop():
            original_stop()
            user_stop_done.set()
        with patch.object(self.main, "_stop_bridge", gated_stop), patch("runtime_supervisor.user_mode") as desired_mode:
            updater = threading.Thread(target=update)
            updater.start()
            self.assertTrue(stopped_for_update.wait(1))
            stopper = threading.Thread(target=user_stop)
            stopper.start()
            try:
                self.assertFalse(user_stop_done.wait(.1), "stop must wait for the entire configuration reconnect")
            finally:
                release_update.set()
                updater.join(2)
                stopper.join(2)
            self.assertEqual(desired_mode.call_args.args[0], "stopped")
        self.assertEqual(errors, [])
        self.assertFalse(updater.is_alive() or stopper.is_alive())
        self.assertTrue(user_stop_done.is_set())
        self.assertFalse(self.state.running, "the later user stop must remain effective")

    def test_recovery_started_while_start_waits_for_lifecycle_lock_cancels_start(self):
        entered, recovering = threading.Event(), threading.Event()
        errors = []
        def start():
            try:
                self.main._start_bridge()
            except Exception as error:
                errors.append(error)
        with patch.object(self.main, "initialize_runtime", side_effect=entered.set), \
                patch("runtime_supervisor.recovery_in_progress", side_effect=recovering.is_set):
            with self.state.run_lock:
                starter = threading.Thread(target=start)
                starter.start()
                self.assertTrue(entered.wait(1))
                recovering.set()
            starter.join(2)
        self.assertFalse(starter.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], RuntimeError)
        self.assertEqual(self.created_senders, [])
        self.assertFalse(self.state.running)

    def test_runtime_path_change_reports_required_process_restart(self):
        result = self.main.update_config({"web_port": 9001, "memory_db_path": "new-memory.db"})
        self.assertEqual(set(result["restart_required"]), {"web_port", "memory_db_path"})

    def test_memory_scope_change_invalidates_active_generation(self):
        self.config.config["memory_scope"] = "shared"
        self.config.MEMORY_SCOPE = "shared"
        self.main._start_bridge()
        generation, old_cancel = self.state.bridge_generation, self.state.send_cancel
        result = self.main.update_config({"memory_scope": "session"})
        self.assertIn("memory_scope", result["changed_keys"])
        self.assertGreater(self.state.bridge_generation, generation)
        self.assertTrue(old_cancel.is_set())
        self.assertFalse(self.state.send_cancel.is_set())
        self.assertTrue(self.state.running)
        self.assertFalse(self.state.dispatch_send("send_text", "friend", "stale memory answer", target_id=1, generation=generation))

    def test_memory_scope_change_preserves_paused_state(self):
        self.config.config["memory_scope"] = "shared"
        self.main._start_bridge()
        self.main._pause_bridge()
        generation = self.state.bridge_generation
        self.main.update_config({"memory_scope": "session"})
        self.assertGreater(self.state.bridge_generation, generation)
        self.assertTrue(self.state.running)
        self.assertTrue(self.state.paused.is_set())
        self.assertTrue(self.state.send_cancel.is_set())

    def test_memory_search_keeps_one_scope_during_concurrent_change(self):
        actual_memory = load("isolated_actual_memory", "memory_store.py")
        self.config.MEMORY_SCOPE = "session"
        case = self
        class Rows(list):
            def __iter__(self):
                for index, row in enumerate(super().__iter__()):
                    if index == 1:
                        case.config.MEMORY_SCOPE = "shared"
                    yield row
        store = actual_memory.MemoryStore.__new__(actual_memory.MemoryStore)
        store.enabled = True
        store._lock = threading.Lock()
        store._ensure_index = lambda: None
        store._index = Rows({"id": number, "content": "favorite tea", "source": "synthetic", "session_id": session,
                             "created_at": 1, "grams": actual_memory._bigrams("favorite tea")}
                            for number, session in enumerate(("wxid_friend", "other-private", ""), start=1))
        result = store.search("favorite tea", top_k=5, session_id="wxid_friend")
        self.assertEqual({row["session_id"] for row in result}, {"wxid_friend", ""})
        self.assertEqual(self.config.MEMORY_SCOPE, "shared", "the test switches policy while rows are read")

    def test_sync_api_returns_failed_on_unconfirmed_send(self):
        self.main._start_bridge()
        self.state.sender_instance.send_text = lambda *a, **kw: False
        response = run_inline(self.protocol._handle_ob_api({"action": "send_private_msg", "echo": "checked", "params": {"user_id": 1, "message": "hello"}}, generation=self.state.client_generation))
        self.assertEqual(response["status"], "failed")
        self.assertEqual(response["retcode"], 1500)
        self.assertIn("message_id", response["data"])
        self.assertEqual(response["echo"], "checked")

    def test_adjacent_text_segments_share_one_full_voice_reply(self):
        self.main._start_bridge()
        self.state.pending_reply_modes[1] = {"reply_type": "voice", "generation": self.state.bridge_generation}
        audio = self.root / "synthetic.wav"
        with wave.open(str(audio), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(16000)
            output.writeframes(b"\0\0" * 1600)
        with patch.object(self.protocol, "synthesize_voice", return_value=str(audio)) as synth:
            ok = self.protocol._perform_send({}, 1, False, [{"type": "text", "data": {"text": "第一段"}}, {"type": "text", "data": {"text": "第二段"}}], self.state.bridge_generation)
        self.assertTrue(ok)
        synth.assert_called_once_with("第一段第二段")
        self.assertEqual([item[0] for item in self.state.sender_instance.sent], ["voice"])

    def test_late_mismatched_reply_keeps_new_request_mode(self):
        self.main._start_bridge()
        info = {"reply_type": "voice", "request_message_id": "new-request", "generation": self.state.bridge_generation}
        self.state.pending_reply_modes[1] = info.copy()
        self.assertTrue(self.protocol._perform_send({}, 1, False, [{"type": "reply", "data": {"id": "old-request"}}, {"type": "text", "data": {"text": "old answer"}}], self.state.bridge_generation))
        self.assertEqual(self.state.pending_reply_modes.get(1), info)

    def test_outbox_pending_audio_and_acknowledgement_lease_are_protected(self):
        real_outbox = load("isolated_actual_outbox", "outbox.py")
        box = real_outbox.Outbox(str(self.root / "lease.db"))
        self.addCleanup(box.close)
        audio = str((self.root / "inbound.wav").resolve())
        event = {"self_id": 1, "message_id": 9, "time": 1,
                 "message": [{"type": "record", "data": {"file": audio}}]}
        self.assertTrue(box.put(event))
        self.assertIn(audio, box.referenced_files())
        row_id, _ = box.peek()
        box.acknowledge(row_id)
        self.assertIsNone(box.peek())
        self.assertIn(audio, box.referenced_files(), "AstrBot may read the file asynchronously after WS transport acceptance")
        with box.lock:
            box.db.execute("UPDATE media_leases SET expires=0")
            box.db.commit()
        self.assertNotIn(audio, box.referenced_files())
        with box.lock:
            self.assertEqual(box.db.execute("SELECT count(*) FROM media_leases").fetchone()[0], 0)
            indexes = {row[1] for row in box.db.execute("PRAGMA index_list(media_leases)")}
        self.assertIn("idx_media_leases_expires", indexes)

    def test_outbox_acknowledge_prunes_expired_leases(self):
        real_outbox = load("isolated_actual_outbox", "outbox.py")
        box = real_outbox.Outbox(str(self.root / "ack-prune.db"))
        self.addCleanup(box.close)
        with box.lock:
            box.db.execute("INSERT INTO media_leases(path,expires) VALUES(?,?)", ("expired", 0))
            box.db.commit()
        self.assertTrue(box.put({"message_id": 1, "time": 1, "message": []}))
        box.acknowledge(box.peek()[0])
        with box.lock:
            self.assertEqual(box.db.execute("SELECT count(*) FROM media_leases").fetchone()[0], 0)

    def test_outbox_reopen_prunes_expired_leases(self):
        real_outbox = load("isolated_actual_outbox", "outbox.py")
        path = str(self.root / "reopen-prune.db")
        box = real_outbox.Outbox(path)
        with box.lock:
            box.db.execute("INSERT INTO media_leases(path,expires) VALUES(?,?)", ("expired", 0))
            box.db.commit()
        box.close()
        reopened = real_outbox.Outbox(path)
        self.addCleanup(reopened.close)
        with reopened.lock:
            self.assertEqual(reopened.db.execute("SELECT count(*) FROM media_leases").fetchone()[0], 0)

    def test_outbox_writer_rechecks_pause_after_async_peek(self):
        self.main._start_bridge()
        with patch.dict("sys.modules", {"ob_protocol": self.protocol, "websockets": module("websockets")}):
            client = load("isolated_actual_client", "ob_client.py")
        sent, acknowledged = [], []
        def peek(wait_seconds=0):
            self.state.paused.set()
            return 1, {"message": "must wait"}
        self.state.outbox = types.SimpleNamespace(peek=peek, acknowledge=acknowledged.append)
        async def to_thread(callback, *args):
            return callback(*args)
        async def sleep(delay):
            self.state.running = False
        async def send(payload):
            sent.append(payload)
        client.asyncio = types.SimpleNamespace(to_thread=to_thread, sleep=sleep)
        run_inline(client._outbox_writer(types.SimpleNamespace(send=send), self.state.client_generation))
        self.assertEqual(sent, [])
        self.assertEqual(acknowledged, [])


if __name__ == "__main__":
    unittest.main()
