"""Local panel and generation-owned bridge lifecycle. Import has no startup effects."""
import asyncio
import json
import logging
import os
import stat
import threading
import time
import sys
from pathlib import Path
if __name__=="__main__":
    if "--supervised-worker" not in sys.argv:
        from runtime_supervisor import run
        raise SystemExit(run())
    import bootstrap
    if not bootstrap.ensure_environment():
        raise SystemExit(1)
from http.server import ThreadingHTTPServer
import config
import state
import http_util
from senders import create_sender, create_moments_sender, create_moments_social_sender
from ob_client import _run_ob_client
from bridge_core import WeFlowBridge
from memory_store import MemoryStore
from autopilot import Autopilot
from moments_interactions import MomentsInteractions
from stable_id import validate_username

log=logging.getLogger("ob11-bridge")
state.name_monitor=None
_initialized=False
_uia_cache={"ready":False,"last_error":"尚未检查"}
_uia_lock=threading.Lock()
_uia_restore_result={"ok":True,"status":"not_attempted","senders":[]}
_pending_sender_closes=[]
_moments_manual_request=None
_moments_manual_status={"status":"idle","started_at":0.0,"finished_at":0.0,"blocked_reason":"","error":""}
_service_control_lock=threading.Lock()
_service_control={"state":"idle","action":"","message":"","error":""}

def _close_uia_senders(senders):
    global _uia_restore_result, _pending_sender_closes
    unique=[]
    for sender in [*_pending_sender_closes,*senders]:
        if sender is not None and not any(sender is existing for existing in unique):
            unique.append(sender)
    results=[]
    failed=[]
    _pending_sender_closes=list(unique)
    for index,sender in enumerate(unique):
        close=getattr(sender,"close",None)
        if not callable(close):
            continue
        try:
            result=close()
            result=dict(result) if isinstance(result,dict) else {"ok":False,"status":"restore_not_confirmed"}
        except BaseException as error:
            result={"ok":False,"status":"restore_exception","error":str(error)}
            if not isinstance(error,Exception):
                _pending_sender_closes=[*failed,*unique[index:]]
                _uia_restore_result={"ok":False,"status":"restore_interrupted","senders":[*results,result]}
                interactions=getattr(state,"moments_interactions",None)
                if interactions is not None:
                    interactions.cancel_pending()
                raise
        if result.get("ok") is not True:
            failed.append(sender)
            log.warning("UIA 临时状态还原未确认: %s",result)
        results.append(result)
    _pending_sender_closes=failed
    _uia_restore_result={"ok":not failed,"status":"restored" if results and not failed else "restore_failed" if failed else "not_active","senders":results}
    if failed:
        interactions=getattr(state,"moments_interactions",None)
        if interactions is not None:
            interactions.cancel_pending()
    return dict(_uia_restore_result)

def refresh_uia_diagnostics():
    global _uia_cache
    if not _uia_lock.acquire(blocking=False):
        return dict(_uia_cache)
    try:
        with state.run_lock:
            sender=state.sender_instance
            generation=state.bridge_generation
        if sender is None:
            return dict(_uia_cache)
        snapshot=sender.diagnostics()
        with state.run_lock:
            if state.running and state.sender_instance is sender and state.bridge_generation == generation:
                _uia_cache=snapshot
        return dict(_uia_cache)
    finally:
        _uia_lock.release()

def prepare_uia(sender, generation):
    """Startup preparation may restore known-build accessibility; diagnostics is read-only."""
    try:
        if sender is not state.sender_instance or generation != state.bridge_generation or not state.running:
            return
        prepare=getattr(sender,"prepare",None)
        if callable(prepare):
            prepare(cancel_event=state.send_cancel,generation=generation)
        if sender is state.sender_instance and generation == state.bridge_generation and state.running:
            refresh_uia_diagnostics()
    except Exception as error:
        log.warning("微信主窗口 UIA 初始化失败: %s",error)

def get_status():
    import name_refresh
    import runtime_supervisor
    interactions=getattr(state,"moments_interactions",None)
    result={"running":state.running,"paused":state.paused.is_set(),"connected":state._ob_ws_ready.is_set(),
            "weflow_connected":bool(state.bridge_instance and getattr(state.bridge_instance,"_sse_session",None)),
            "ob_connected":state._ob_ws_ready.is_set(),"mode":state.group_reply_mode,"group_reply_mode":state.group_reply_mode,
            "caption_provider":"main_multimodal","sender":"UIA","version":"6.33",
            "uptime":round(time.time()-state.started_at,1) if state.started_at else 0,
            "memory_count":state.memory_store.count() if state.memory_store else 0,
            "autopilot_enabled":bool(state.autopilot and state.autopilot.enabled),
            "autopilot_last_run":state.autopilot.last_run if state.autopilot else 0,
            "autopilot_last_result":state.autopilot.last_result if state.autopilot else "未运行",
            "autopilot_pending":state.autopilot.pending_snapshot() if state.autopilot else None,
            "bili_sharing":state.bili_sharing.status() if state.bili_sharing else {"enabled":bool(getattr(config,"BILI_SHARE_ENABLED",False)), "last_result":"等待启动" if not state.running else "主动分享已关闭"},
            "moments_interactions":interactions.status_snapshot() if interactions else {"enabled":False,"running":False,"last_result":"未初始化","pending":None},
            "uia":dict(_uia_cache),"uia_restore_result":dict(_uia_restore_result),"name_sync":name_refresh.get_sync_status(),
            "outbox":state.outbox.stats() if state.outbox else {"pending":0},
            "send_queue":state.send_queue.stats() if state.send_queue else {"pending":0}}
    result.update(state.metrics_snapshot())
    result["runtime_watchdog"] = runtime_supervisor.snapshot()
    result["moments_manual_request"]=dict(_moments_manual_status)
    result["service_control"]=dict(_service_control)
    if getattr(config, "CHARACTER_INTERESTS_ENABLED", False):
        from character_interests import current_topics
        result["character_interests"] = current_topics()
    if state.autopilot and callable(getattr(state.autopilot,"status_snapshot",None)):
        schedule=state.autopilot.status_snapshot()
        result["autopilot_schedule"]=schedule
        for key in ("next_run","last_check","blocked_reason","action_error"):
            result["autopilot_"+key]=schedule[key]
    package_root = os.environ.get("WECHATBOT_PORTABLE_ROOT")
    if package_root:
        profile_file = os.path.join(package_root, "portable", "runtime", "astrbot-instance", "data", "wechatbot-profile-status.json")
        try:
            if os.path.getsize(profile_file) <= 16384:
                with open(profile_file, "r", encoding="utf-8") as stream:
                    value = json.load(stream)
                if isinstance(value, dict):
                    result["model_profile_status"] = {key: str(value[key])[:500] for key in ("state", "reason") if key in value}
        except (OSError, ValueError, TypeError):
            pass
    return result

def _package_service_launcher():
    """Manage only the portable instance belonging to this running panel."""
    package=os.environ.get("WECHATBOT_PORTABLE_ROOT")
    if not package:
        return None  # Source-mode installations may connect to an external AstrBot.
    if Path(package).resolve() != Path(__file__).resolve().parent.parent:
        raise RuntimeError("面板与便携包路径不一致，请通过当前包的一键启动重新打开")
    import startup_launcher
    return startup_launcher


def _panel_services(action):
    """Panel start/stop owns AstrBot too; internal reconnects remain bridge-only."""
    global _service_control
    if not _service_control_lock.acquire(blocking=False):
        return dict(get_status(),ok=False,error="服务正在启动或停止，请等待本次操作完成")
    try:
        if _service_control["state"] == "exiting":
            return {"ok":False,"error":"当前面板正在完全退出。"}
        _service_control={"state":{"start":"starting","stop":"stopping","exit":"exiting"}[action],
                          "action":action,"message":"正在启动机器人服务" if action == "start" else "正在停止机器人和 AstrBot", "error":""}
        if action in ("stop", "exit"):
            _stop_bridge()
            # Keep the panel's idle scheduler threads. _stop_bridge cancels
            # their work and removes senders; tearing down a thread still
            # awaiting a model would race a subsequent start.
            launcher=_package_service_launcher()
            if launcher is not None:
                launcher._astrbot_lifecycle().stop_owned(timeout=8)
            if _uia_restore_result.get("ok") is not True:
                raise RuntimeError("机器人服务已停止，但微信临时状态尚未还原，请重试停止")
            if action == "exit":
                from desktop_coordinator import restore_external_states
                if not restore_external_states():
                    raise RuntimeError("微信的录音或输入状态还在还原，请稍后重试完全退出。")
                _service_control["message"]="本包服务已停止，面板正在退出。"
                return {"ok":True,"message":_service_control["message"]}
            message=("机器人和本包 AstrBot 已停止，面板保留，可再次启动" if launcher is not None
                     else "机器人已停止，面板保留；外部 AstrBot 由其启动入口管理")
            _service_control={"state":"stopped","action":action,"message":message,"error":""}
        else:
            if _pending_sender_closes and not _close_uia_senders([])["ok"]:
                raise RuntimeError("微信临时状态尚未还原，请先重试停止")
            launcher=_package_service_launcher()
            if launcher is not None:
                launcher.launch_astrbot()
            _start_bridge()
            message=("机器人已启动，AstrBot 正在连接；面板将显示连接结果" if launcher is not None
                     else "机器人已启动，正在连接已配置的 AstrBot")
            _service_control={"state":"running","action":action,"message":message,"error":""}
        return dict(get_status(),ok=True,message=message)
    except Exception as error:
        log.exception("面板服务%s未完成",action)
        message=str(error) or "服务操作未完成，请查看运行日志"
        _service_control={"state":"failed","action":action,"message":message,"error":message}
        return dict(get_status(),ok=False,error=message)
    finally:
        _service_control_lock.release()


def _start_panel_services():
    return _panel_services("start")


def _stop_panel_services():
    return _panel_services("stop")

def _exit_panel_services():
    return _panel_services("exit")


def _configure_bili_sharing():
    if state.running and getattr(config, "BILI_SHARE_ENABLED", False):
        if state.bili_sharing is None:
            from bili_sharing import BiliSharing
            state.bili_sharing = BiliSharing(state.bridge_generation)
            state.bili_sharing.start()
        else:
            state.bili_sharing.configure()
    elif state.bili_sharing:
        state.bili_sharing.stop()
        state.bili_sharing = None

def _configure_moments_interactions():
    """Caller owns run_lock; only cancellation, no joins or model/desktop work."""
    controller=getattr(state,"moments_interactions",None)
    if controller is None:
        return
    enabled=bool(getattr(config,"MOMENTS_INTERACTIONS_ENABLED",False))
    # A failed restoration belongs to the whole shared WeChat desktop, including
    # failures from chat or automatic publishing. Do not replace a cancelled
    # token while any prior sender still owns an unconfirmed temporary state.
    controller.cancel_pending()
    if enabled and (_pending_sender_closes or _uia_restore_result.get("ok") is not True):
        if not _close_uia_senders([])["ok"]:
            raise RuntimeError("上次 UIA 临时状态尚未还原，请先重试停止桥接")
    controller.set_enabled(enabled)
    if not enabled:
        sender=controller.sender
        controller.sender=None
        if sender is not None:
            _close_uia_senders([sender])
    elif state.running and controller.sender is None:
        controller.sender=create_moments_social_sender()
    if state.paused.is_set() or not state.running:
        controller.cancel_pending()
    if state.running:
        controller.start()

def run_moments_interactions():
    """Reserve a manual browse atomically; it never enables a disabled feature."""
    global _moments_manual_request
    with state.run_lock:
        controller=getattr(state,"moments_interactions",None)
        if _pending_sender_closes or _uia_restore_result.get("ok") is not True:
            if controller is not None:
                controller.cancel_pending()
            return {"ok":False,"status":"restore_failed","error":"UIA 临时状态尚未还原，请先重试停止桥接"}
        if not state.running or state.paused.is_set():
            return {"ok":False,"status":"unavailable","error":"请先启动并继续桥接"}
        if (controller is None or not bool(getattr(config,"MOMENTS_INTERACTIONS_ENABLED",False))
                or not controller.enabled or controller.sender is None):
            return {"ok":False,"status":"disabled","error":"请先启用朋友圈互动"}
        try:
            validate_username(getattr(config,"BOT_WXID",""))
        except (ValueError, TypeError):
            message="请配置当前账号的真实微信 ID"
            _moments_manual_status.update(status="account_identity_missing",finished_at=time.time(),
                                          blocked_reason="account_identity_missing",error=message)
            log.info("[moments-interactions] manual blocked status=account_identity_missing")
            return {"ok":False,"status":"account_identity_missing","error":message}
        snapshot=controller.status_snapshot()
        if _moments_manual_request is not None or snapshot.get("running") is True:
            return {"ok":False,"status":"busy","error":"朋友圈互动正在处理"}
        request=object()
        _moments_manual_request=request
        _moments_manual_status.update(status="queued",started_at=time.time(),finished_at=0.0,blocked_reason="",error="")
        log.info("[moments-interactions] manual queued")
        generation=state.bridge_generation
        token=controller._cancel
        sender=controller.sender

        def work():
            global _moments_manual_request
            result={"ok":False,"status":"failed","error":"手动朋友圈任务未完成"}
            try:
                # Expected identities bind this delayed job to its original
                # lifecycle. The controller rechecks them after acquiring its
                # decision lock and throughout the model/UIA transaction.
                with state.run_lock:
                    if _moments_manual_request is request:
                        _moments_manual_status["status"]="running"
                log.info("[moments-interactions] manual started")
                result=controller.run_once(expected_generation=generation,
                    expected_cancel=token,expected_sender=sender,manual=True)
            except Exception as error:
                result={"ok":False,"status":"failed","error":"手动朋友圈浏览异常："+type(error).__name__}
                log.warning("[moments-interactions] manual failed error=%s",type(error).__name__)
            finally:
                with state.run_lock:
                    if _moments_manual_request is request:
                        _moments_manual_status.update(status=result.get("status","failed"),
                            blocked_reason=result.get("blocked_reason",""),error=result.get("error",""),finished_at=time.time())
                        _moments_manual_request=None
                log.info("[moments-interactions] manual finished status=%s",result.get("status","failed"))
        try:
            threading.Thread(target=work,daemon=True,name="moments-interactions-manual").start()
        except BaseException:
            _moments_manual_request=None
            _moments_manual_status.update(status="failed",error="手动浏览线程未能启动",finished_at=time.time())
            raise
        return {"ok":True,"status":"started","message":"已开始浏览，是否互动由模型判断"}

def _start_bridge(initially_paused=False, supervised_boot=False):
    import runtime_supervisor
    initialize_runtime()
    with state.run_lock:
        if _service_control["state"] == "exiting":
            raise RuntimeError("当前面板正在完全退出。")
        if runtime_supervisor.recovery_in_progress():
            raise RuntimeError("旧进程正在恢复，尚未启动新的发送器")
        if state.running:
            return get_status()
        if not supervised_boot:
            runtime_supervisor.user_mode("paused" if initially_paused else "active")
        if _pending_sender_closes and not _close_uia_senders([])["ok"]:
            raise RuntimeError("上次 UIA 临时状态尚未还原，请先重试停止桥接")
        state.bridge_generation+=1
        state.client_generation+=1
        generation=state.bridge_generation
        state.send_cancel=threading.Event()
        if initially_paused:
            state.paused.set()
            state.send_cancel.set()
        else:
            state.paused.clear()
        try:
            state.sender_instance=create_sender()
            from send_queue import SendQueue
            state.send_queue=SendQueue(config.SEND_QUEUE_LIMIT)
            state.running=True
            state.started_at=time.time()
            state.ob_client_started=True
            state.ob_client_thread=threading.Thread(target=_run_ob_client,args=(state.client_generation,),daemon=True,name="ob11-client")
            state.ob_client_thread.start()
            state.bridge_thread=threading.Thread(target=_bridge_loop,args=(generation,state.sender_instance),daemon=True,name="bridge")
            state.bridge_thread.start()
            import name_refresh
            state.name_monitor=name_refresh.start_name_monitor(generation)
            state.record_activity("lifecycle","桥接已启动")
            if not initially_paused:
                threading.Thread(target=prepare_uia,args=(state.sender_instance,generation),daemon=True,name="uia-prepare").start()
            if state.autopilot:
                if state.autopilot.enabled and state.autopilot.moments_sender is None:
                    state.autopilot.moments_sender=create_moments_sender()
                state.autopilot._cancel=threading.Event() if state.autopilot.enabled else state.autopilot._cancel
                state.autopilot.start()
            _configure_moments_interactions()
            _configure_bili_sharing()
            if state.memory_store is not None:
                state.memory_store.set_extraction_paused(False)
        except BaseException:
            _stop_bridge(user_requested=False)
            raise
    return get_status()

def _stop_bridge(user_requested=True):
    global _uia_cache
    with state.run_lock:
        if user_requested:
            import runtime_supervisor
            runtime_supervisor.user_mode("stopped")
        state.running=False
        if state.bili_sharing:
            state.bili_sharing.stop()
            state.bili_sharing=None
        if state.memory_store is not None:
            state.memory_store.set_extraction_paused(True)
        sender=state.sender_instance
        state.sender_instance=None
        state.send_cancel.set()
        state.bridge_generation+=1
        state.client_generation+=1
        state.paused.clear()
        state._ob_ws_ready.clear()
        state.ob_client_started=False
        queue=state.send_queue
        state.send_queue=None
        monitor=state.name_monitor
        state.name_monitor=None
        with state.bridge_lock:
            bridge=state.bridge_instance
            state.bridge_instance=None
        ws,loop=state._ob_ws,state._ob_ws_loop
        state._ob_ws=None
        if queue:
            queue.close()
        if monitor:
            monitor.stop()
        moments=None
        interaction_sender=None
        if state.autopilot:
            state.autopilot._cancel.set()
            moments=state.autopilot.moments_sender
            state.autopilot.moments_sender=None
        interactions=getattr(state,"moments_interactions",None)
        if interactions is not None:
            interactions.cancel_pending()
            interaction_sender=interactions.sender
            interactions.sender=None
        _close_uia_senders([interaction_sender,moments,sender])
        if bridge:
            getattr(bridge, "close", bridge._sched.stop)()
            response=getattr(bridge,"_sse_session",None)
            if response:
                def close_owned_response():
                    try:
                        response.close()
                    except Exception:
                        pass
                threading.Thread(target=close_owned_response,daemon=True,name="sse-close").start()
        if ws and loop and loop.is_running():
            asyncio.run_coroutine_threadsafe(ws.close(),loop)
        _uia_cache={"ready":False,"last_error":"桥接已停止"}
        state.record_activity("lifecycle","桥接已停止，未发送任务已取消")
    return get_status()

def _pause_bridge():
    with state.run_lock:
        if not state.running:
            return get_status()
        import runtime_supervisor
        runtime_supervisor.user_mode("paused")
        state.paused.set()
        state.send_cancel.set()
        interactions=getattr(state,"moments_interactions",None)
        if interactions is not None:
            interactions.cancel_pending()
        state.record_activity("lifecycle","桥接已暂停")
    return get_status()

def _resume_bridge():
    with state.run_lock:
        if state.running and state.paused.is_set():
            # New generation prevents old in-flight model/UI work resurfacing on resume.
            _stop_bridge(user_requested=False)
            _start_bridge()
            state.record_activity("lifecycle","桥接已继续")
    return get_status()

def _bridge_loop(generation,sender):
    bridge=WeFlowBridge(sender)
    with state.bridge_lock:
        if generation!=state.bridge_generation or not state.running:
            getattr(bridge, "close", bridge._sched.stop)()
            return
        state.bridge_instance=bridge
    retry=0.5
    try:
        while state.running and generation==state.bridge_generation:
            try:
                if bridge.listen_sse(generation) is True:
                    retry=0.5
            except Exception as exc:
                log.warning("WeFlow 连接断开（%s）",type(exc).__name__)
            if not state.running or generation!=state.bridge_generation:
                break
            deadline=time.monotonic()+retry
            if state.send_cancel.wait(retry):
                # A pause/cancelled send can leave this event set while the
                # listener still runs. Preserve backoff without clearing it.
                while state.running and generation==state.bridge_generation:
                    remaining=deadline-time.monotonic()
                    if remaining<=0:
                        break
                    time.sleep(min(0.1,remaining))
            retry=min(5,retry*2)
    finally:
        getattr(bridge, "close", bridge._sched.stop)()
        with state.bridge_lock:
            if state.bridge_instance is bridge:
                state.bridge_instance=None

def update_config(updates):
    # Saving and reconnecting are one lifecycle transaction: a concurrent stop
    # must not be undone by the gap between stopping and restarting a connection.
    with state.run_lock:
        previous=dict(config.config)
        interactions=getattr(state,"moments_interactions",None)
        if interactions is not None:
            interactions.cancel_pending()
        try:
            merged=config.save_config(updates)
        except Exception:
            # Invalid settings leave the old policy available for a future
            # scheduled run, while the already-cancelled decision stays stale.
            _configure_moments_interactions()
            raise
        state.group_reply_mode=config.GROUP_REPLY_MODE
        for key in ("image_caption_provider","image_caption_model","image_caption_api_key","image_caption_api_base","image_caption_prompt","ollama_base_url","ollama_timeout"):
            setattr(state,key,getattr(config,key.upper()))
        if state.memory_store:
            state.memory_store.enabled=config.MEMORY_ENABLED
        if state.outbox:
            state.outbox.max_events=config.OUTBOX_MAX_EVENTS
        if state.autopilot:
            state.autopilot.set_enabled(config.AUTOPILOT_ENABLED)
            if not config.AUTOPILOT_ENABLED and not getattr(state.autopilot, "_announcement_account", ""):
                moments=state.autopilot.moments_sender
                state.autopilot.moments_sender=None
                if moments is not None:
                    _close_uia_senders([moments])
            if config.AUTOPILOT_ENABLED and state.autopilot.moments_sender is None:
                state.autopilot.moments_sender=create_moments_sender()
            if state.running:
                state.autopilot.start()
        reconnect_keys=("weflow_base_url","access_token","astrbot_ob_url","bot_wxid","core_workers","send_queue_limit","memory_scope","uia_recovery_enabled","uia_process_id")
        if state.running and any(previous.get(k)!=merged.get(k) for k in reconnect_keys):
            was_paused=state.paused.is_set()
            _stop_bridge(user_requested=False)
            state._self_id_int=state._wxid_to_int(config.BOT_WXID or "wechat_bot")
            _start_bridge(initially_paused=was_paused)
        else:
            _configure_moments_interactions()
        changed=[k for k in merged if previous.get(k)!=merged.get(k)]
        if state.running and any(k.startswith("bili_share_") for k in changed):
            _configure_bili_sharing()
        restart=[k for k in changed if k in ("web_port","memory_db_path")]
        return {"changed_keys":changed,"restart_required":restart,
                "warnings":["面板端口或记忆数据库路径的变化需要重启本项目进程后生效"] if restart else []}

def _prepare_writable_runtime_data():
    if os.name != "nt":
        return
    databases = (config.MEMORY_DB_PATH, os.path.join(config.DATA_DIR, "outbox.db"),
                 os.path.join(config.DATA_DIR, "moments_interactions.sqlite3"))
    paths = [state.identity_registry.path]
    paths.extend(path + suffix for path in databases for suffix in ("", "-wal", "-shm"))
    for path in paths:
        if os.path.isfile(path) and os.stat(path).st_file_attributes & stat.FILE_ATTRIBUTE_READONLY:
            os.chmod(path, stat.S_IWRITE)


def initialize_runtime():
    global _initialized
    with state.run_lock:
        if _initialized:
            return
        config.ensure_data_dirs()
        _prepare_writable_runtime_data()
        state._self_id_int=state._wxid_to_int(config.BOT_WXID or "wechat_bot")
        state.group_reply_mode=config.GROUP_REPLY_MODE
        for key in ("image_caption_provider","image_caption_model","image_caption_api_key","image_caption_api_base","image_caption_prompt","ollama_base_url","ollama_timeout"):
            setattr(state,key,getattr(config,key.upper()))
        state.memory_store=MemoryStore(config.MEMORY_DB_PATH,config.MEMORY_ENABLED,extraction_paused=True)
        from outbox import Outbox
        state.outbox=Outbox(os.path.join(config.DATA_DIR,"outbox.db"),config.OUTBOX_MAX_EVENTS)
        state.autopilot=Autopilot(state.memory_store,create_moments_sender() if config.AUTOPILOT_ENABLED else None)
        state.moments_interactions=MomentsInteractions(state.memory_store,
            create_moments_social_sender() if getattr(config,"MOMENTS_INTERACTIONS_ENABLED",False) else None)
        state.start_bridge=_start_panel_services; state.stop_bridge=_stop_panel_services
        state.exit_panel_services=_exit_panel_services
        state.pause_bridge=_pause_bridge; state.resume_bridge=_resume_bridge
        state.update_config=update_config; state.get_status=get_status
        state.refresh_uia_diagnostics=refresh_uia_diagnostics
        state.run_moments_interactions=run_moments_interactions
        _initialized=True

def start_web():
    from web_panel import WebHandler
    server=ThreadingHTTPServer(("127.0.0.1",config.WEB_PORT),WebHandler)
    server.daemon_threads=True
    log.info("控制面板 http://127.0.0.1:%s",config.WEB_PORT)
    try:
        server.serve_forever(poll_interval=0.2)
    finally:
        server.server_close()

if __name__=="__main__":
    os.chdir(config.BASE_DIR)
    initialize_runtime()
    # Exclusive open prevents concurrent startups from overwriting each other's PID.
    pidfile=os.path.join(config.BASE_DIR,"bridge.pid")
    import startup_control
    if not startup_control.acquire_pid(pidfile):
        raise SystemExit("本项目桥接已经运行")
    import runtime_supervisor
    monitor = runtime_supervisor.start_worker_monitor(state)
    try:
        mode = os.environ.get("WECHATBOT_START_MODE", "active")
        if mode != "stopped":
            threading.Thread(target=_start_bridge,kwargs={"initially_paused": mode == "paused", "supervised_boot": True},daemon=True).start()
        start_web()
    finally:
        exiting = _service_control["state"] == "exiting"
        if not exiting:
            _stop_bridge(user_requested=False)
        if monitor:
            monitor.publish()
            monitor.stop_event.set()
        if state.autopilot:
            state.autopilot.stop()
        interactions=getattr(state,"moments_interactions",None)
        if interactions is not None:
            interactions.stop()
        state.identity_registry.flush()
        if state.memory_store is not None:
            state.memory_store.close()
        startup_control.release_pid(pidfile)
        if exiting:
            # Model and HTTP executor threads may still be returning from a
            # cancelled call. Persistent writes and desktop cleanup are done.
            logging.shutdown()
            os._exit(0)
