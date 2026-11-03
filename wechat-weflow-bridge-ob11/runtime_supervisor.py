"""Process supervision and primitive runtime progress; importing starts nothing."""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import secrets
import subprocess
import sys
import threading
import time

RESTART_EXIT = 75
DESKTOP_IDLE_SECONDS = 45.0
HEARTBEAT_STALE_SECONDS = 15.0
_monitor = None
log = logging.getLogger("ob11-bridge")


def _write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    try:
        os.replace(temporary, path)
    except PermissionError as error:
        if getattr(error, "winerror", None) not in (5, 32, 33):
            raise
        # On Windows the supervisor's brief read can block replacement.
        time.sleep(.025)
        os.replace(temporary, path)


def _read(path):
    try:
        if Path(path).stat().st_size <= 16384:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        pass
    return {}


def snapshot():
    from desktop_coordinator import desktop_snapshot
    result = _monitor.snapshot() if _monitor else {"state": "not_supervised", "reason": "尚未启用进程监督", "restart_count": 0}
    result["desktop"] = desktop_snapshot()
    return result


def recovery_in_progress():
    return bool(_monitor and _monitor.recovering)


def user_mode(mode):
    if _monitor:
        _monitor.set_mode(mode)


class WorkerMonitor:
    def __init__(self, state, health_path, *, exit_process=os._exit, clock=time.monotonic):
        self.state = state
        self.path = Path(health_path)
        self.clock = clock
        self.exit_process = exit_process
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.started = clock()
        self.mode = os.environ.get("WECHATBOT_START_MODE", "active")
        self.recovering = False
        self.status = "restarted" if int(os.environ.get("WECHATBOT_RESTART_COUNT", "0")) else "healthy"
        self.reason = "已在旧进程退出后恢复" if self.status == "restarted" else "运行正常"
        self.restart_count = int(os.environ.get("WECHATBOT_RESTART_COUNT", "0"))
        self.last_recovery_at = float(os.environ.get("WECHATBOT_LAST_RECOVERY_AT", "0"))
        if self.mode == "paused" and self.restart_count:
            self.status, self.reason = "paused", "异常退出后已恢复面板并保留暂停，请核对当前微信后继续"
        self.cleanup_confirmed = False
        self.recovery_started = None
        self.inactive_since = None

    def snapshot(self):
        with self.lock:
            return {"state": self.status, "reason": self.reason,
                    "restart_count": self.restart_count, "last_recovery_at": self.last_recovery_at}

    def set_mode(self, mode):
        with self.lock:
            self.mode = mode
            if mode == "active":
                self.started = self.clock()
                self.inactive_since = None
            if not self.recovering:
                self.status = mode if mode in {"paused", "stopped"} else "healthy"
                self.reason = {"paused": "用户已暂停", "stopped": "用户已停止"}.get(mode, "运行正常")
        self.publish()

    def publish(self):
        from desktop_coordinator import desktop_snapshot
        from uia_recovery import shared_gate_manager
        with self.lock:
            data = {**self.snapshot(), "pid": os.getpid(), "heartbeat": time.time(),
                    "mode": self.mode, "cleanup_confirmed": self.cleanup_confirmed,
                    "desktop": desktop_snapshot(), "gate_active": shared_gate_manager().snapshot()["active"]}
            _write(self.path, data)

    def tick(self):
        from desktop_coordinator import desktop_snapshot
        self.publish()
        if self.recovering:
            if self.clock()-self.recovery_started > 20 and self.status == "recovering":
                with self.lock:
                    self.status, self.reason = "blocked", "临时状态还原尚未完成，保留旧进程且不启动第二个发送器"
                self.publish()
            return
        # Start/stop/reconfigure publishes several fields under this lock.
        # Skip that transition instead of treating its intermediate state as
        # a failed worker, and never block heartbeat publication behind it.
        if not self.state.run_lock.acquire(blocking=False):
            return
        reason = ""
        try:
            if self.mode != "active" or self.state.paused.is_set():
                return
            desktop = desktop_snapshot()
            if self.state.running:
                self.inactive_since = None
            if not self.state.running:
                self.inactive_since = self.inactive_since or self.clock()
                if self.clock()-self.inactive_since >= 10:
                    reason = "桥接运行任务未成功启动"
            elif desktop["active"] and desktop["idle_seconds"] >= DESKTOP_IDLE_SECONDS:
                reason = "微信界面操作长时间没有进展"
            elif self.clock()-self.started > 10:
                queue = self.state.send_queue
                if queue is not None and not queue.worker.is_alive():
                    reason = "发送工作线程已退出"
                elif self.state.bridge_thread is not None and not self.state.bridge_thread.is_alive():
                    reason = "消息接收工作线程已退出"
                elif self.state.ob_client_thread is not None and not self.state.ob_client_thread.is_alive():
                    reason = "模型连接工作线程已退出"
            if reason:
                with self.lock:
                    self.recovering = True
                    self.recovery_started = self.clock()
                    self.status, self.reason = "recovering", reason
                self.state.send_cancel.set()
                for controller in (getattr(self.state, "autopilot", None), getattr(self.state, "moments_interactions", None)):
                    token = getattr(controller, "_cancel", None)
                    if token is not None:
                        token.set()
        finally:
            self.state.run_lock.release()
        if reason:
            self.publish()
            threading.Thread(target=self._restore_and_exit, daemon=True, name="runtime-restore").start()

    def _restore_and_exit(self):
        from desktop_coordinator import restore_external_states
        from uia_recovery import shared_gate_manager
        try:
            # Normal voice finally blocks get time to release keys/devices.
            # Never detach the old sender or guess an active recording finished.
            deadline = self.clock()+15
            while not restore_external_states():
                if self.clock() >= deadline or self.stop_event.wait(.2):
                    raise RuntimeError("临时桌面状态尚未还原")
            restored = shared_gate_manager().close()
            if restored.get("ok") is not True:
                raise RuntimeError("临时无障碍状态尚未还原")
            with self.lock:
                self.cleanup_confirmed = True
                self.last_recovery_at = time.time()
            self.publish()
            log.warning("[runtime] 已取消旧任务并还原临时状态；退出旧进程后恢复，不重发未确认内容")
            self.exit_process(0 if self.mode == "stopped" else RESTART_EXIT)
        except Exception:
            with self.lock:
                self.status, self.reason = "blocked", "临时桌面状态未确认还原；已停止发送，保留故障现场"
            self.publish()
            log.warning("[runtime] 自动恢复暂缓：临时桌面状态未确认还原")

    def start(self):
        self.publish()
        def watch():
            while not self.stop_event.wait(1):
                try:
                    self.tick()
                except Exception as error:
                    code = getattr(error, "winerror", None) or getattr(error, "errno", None)
                    log.warning("[runtime] 健康状态更新失败（%s%s），下一轮心跳再试",
                                type(error).__name__, f" {code}" if code is not None else "")
        threading.Thread(target=watch, daemon=True, name="runtime-health").start()


def start_worker_monitor(state):
    global _monitor
    health = os.environ.get("WECHATBOT_HEALTH_PATH")
    if health:
        _monitor = WorkerMonitor(state, health)
        _monitor.start()
    return _monitor


def restart_mode(exit_code, health):
    """None ends supervision; unsafe crashes recover the panel paused."""
    if exit_code == 0 or health.get("mode") == "stopped":
        return None
    if health.get("mode") == "paused":
        return "paused"
    # A preceding heartbeat is not evidence about the instant of a crash.
    safe = exit_code == RESTART_EXIT and health.get("cleanup_confirmed") is True
    return "active" if safe else "paused"


def run():
    """Only supervise this Popen child; no lookup/termination by port or name."""
    import startup_control
    base = Path(__file__).resolve().parent
    health_path = base / "data" / ("runtime-health-"+secrets.token_hex(8)+".json")
    count = failures = 0
    mode = "active"
    last_recovery = 0
    with startup_control._pid_guard(str(base / "runtime-supervisor.lock")):
        try:
            if startup_control.owned_running() is not None:
                return 0
            while True:
                env = dict(os.environ, WECHATBOT_HEALTH_PATH=str(health_path), WECHATBOT_START_MODE=mode,
                           WECHATBOT_RESTART_COUNT=str(count), WECHATBOT_LAST_RECOVERY_AT=str(last_recovery))
                # Discard the old heartbeat before starting a new owned child.
                health_path.unlink(missing_ok=True)
                started = time.monotonic()
                child = subprocess.Popen([sys.executable, "-u", "-B", str(base/"main.py"), "--supervised-worker"],
                    cwd=str(base), env=env, stdin=subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
                reported_stale = False
                while child.poll() is None:
                    time.sleep(1)
                    health = _read(health_path)
                    if health and time.time()-health.get("heartbeat", 0) > HEARTBEAT_STALE_SECONDS and not reported_stale:
                        # A stale heartbeat cannot prove clipboard/key restoration.
                        # Do not kill a live worker and create a competing sender.
                        log.warning("[runtime] 健康心跳停止，保留当前进程；未启动第二个发送器")
                        reported_stale = True
                code = child.wait()
                health = _read(health_path)
                if not health:
                    log.error("[runtime] 服务尚未进入运行期，保留真实启动错误，不重复安装或启动")
                    return code
                mode = restart_mode(code, health)
                if mode is None:
                    return code
                failures = 0 if time.monotonic()-started >= 120 else failures+1
                if failures >= 4:
                    log.error("[runtime] 连续异常退出，停止自动启动；请查看启动日志")
                    return code
                count += 1
                last_recovery = time.time()
                log.warning("[runtime] 旧进程已退出，恢复本包服务；未确认发送不自动重发")
                time.sleep(min(2**max(0, failures-1), 30))
        except KeyboardInterrupt:
            # Reuse the existing normal stop so desktop state is restored first.
            startup_control.stop_owned_process()
            return 0
        finally:
            health_path.unlink(missing_ok=True)
