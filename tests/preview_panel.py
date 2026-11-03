"""Opt-in demo server. Synthetic data only; no WeChat or external services."""
import argparse
import json
from pathlib import Path
import sys
import tempfile
import time
import types
from http.server import ThreadingHTTPServer

SOURCE=Path(__file__).resolve().parents[1]/"wechat-weflow-bridge-ob11"


def main(argv=None):
    sys.path.insert(0,str(SOURCE))
    audio=types.ModuleType("audio_helper")
    audio.list_playback_devices=lambda:[{"id":"speaker","name":"扬声器（预览）","is_default":True},{"id":"cable","name":"CABLE Input（预览）","is_default":False}]
    audio.get_default_playback_device=lambda:"扬声器（预览）"
    audio.list_recording_devices=lambda:[{"id":"cable-out","name":"CABLE Output（预览）","is_default":True}]
    audio.get_default_recording_device=lambda:"CABLE Output（预览）"
    audio.open_sound_settings=lambda:True
    sys.modules["audio_helper"]=audio
    import config
    import config_model
    import state
    from memory_store import MemoryStore
    from web_panel import WebHandler

    parser=argparse.ArgumentParser(); parser.add_argument("--port",type=int,default=8876)
    parser.add_argument("--pending", action="store_true", help="Show a synthetic pending Moments record")
    args=parser.parse_args(argv)
    temporary=tempfile.TemporaryDirectory(prefix=".preview-",dir=SOURCE.parent/"tests")
    config.CONFIG_FILE=str(Path(temporary.name)/"config.json")
    config.BASE_DIR=temporary.name
    config.config={f.key:f.default for group in config_model.CONFIG_SCHEMA for f in group.fields}
    Path(config.CONFIG_FILE).write_text(json.dumps(config.config,ensure_ascii=False),encoding="utf-8")
    state.memory_store=MemoryStore(str(Path(temporary.name)/"memory.db"),True)
    for i in range(65):
        state.memory_store.add_memory(f"预览会话 {i+1}：更喜欢自然短句与完整解释",source="测试数据",session_id=f"demo-{i%3}",
            actor_id="wxid_demo_actor" if i==0 else "", evidence=f"预览会话 {i+1}：更喜欢自然短句与完整解释" if i==0 else "",
            message_id="preview-message-0001" if i==0 else "", provenance="extracted" if i==0 else "manual")
    status={"running":True,"paused":False,"connected":True,"weflow_connected":True,"ob_connected":True,"group_reply_mode":"mention","version":"6.33",
            "memory_count":65,"metrics":{"received":128,"forwarded":128,"sent":96,"send_failed":0,"cancelled":0},
            "latency":{"median_ms":286,"p95_ms":740,"samples":96},"outbox":{"pending":0,"limit":10000},"send_queue":{"pending":0,"limit":128},
            "name_sync":{"status":"ok","last_sync":time.time(),"interval":3,"revision":8,"changed":2,"contacts":35,"sessions":18},
            "recent_activity":[{"time":time.time()-2,"kind":"rename","message":"旧备注 → 新备注","target":"演示好友"},{"time":time.time()-9,"kind":"sent","message":"完整回复已发送","target":"演示会话"}],
            "uia":{"ready":True,"window":"微信（预览）","editor":True,"current_chat":"文件传输助手（预览）","last_error":"","last_send":{"kind":"text","ok":True},"verification":"合成数据"},
            "autopilot_enabled":False,"autopilot_last_result":"预览模式"}
    status["moments_interactions"]={"enabled":True,"running":False,"last_run":time.time()-60,
        "next_run":time.time()+14400,"next_reply_run":time.time()+5,"next_active_run":time.time()+14400,
        "private_priority":{"seconds_since_private_activity":90,"private_inflight":False},
        "last_result":"本轮已浏览 20 条，选择轻轻点个赞（合成预览）",
        "day":"2026-10-11","daily":{"like":3,"comment":1,"reply":2},"pending":None,
        "feed":{"reply_identity":"partial","unverified_comment_count":2},
        "recent":[{"type":"like","author_name":"演示好友","status":"completed","time":time.time()-60,
            "content":"周末散步，发现一条安静的小路。","text":""}]}
    if args.pending:
        status["autopilot_pending"]={"version":1,"status":"pending_review","intent_id":"0123456789abcdef0123456789abcdef",
                                    "time":time.time(),"content":"微信bot 功能测试（合成演示，未发表）","image_path":"preview.png"}
        status["moments_interactions"]["pending"]={"version":1,"status":"pending_review",
            "intent_id":"abcdef0123456789abcdef0123456789","type":"reply","time":time.time(),
            "author_name":"演示账号","post_id":"123456789","comment_id":"1234",
            "content":"今天的晚霞很好看（合成演示）","text":"下次一起找个好位置看晚霞吧。",
            "comment_author_name":"演示好友","comment_author_id":"wxid_demo_friend",
            "comment_content":"这张颜色真好看，在哪里拍的？"}
    state.get_status=lambda:{**status,"preview":True}
    state.sender_instance=type("SyntheticSender",(),{"diagnostics":lambda self:status["uia"]})()
    state.refresh_uia_diagnostics=lambda:status["uia"]
    def lifecycle(running,paused=False):
        status.update(running=running,paused=paused)
        return status.copy()
    state.start_bridge=lambda:lifecycle(True); state.stop_bridge=lambda:lifecycle(False)
    state.pause_bridge=lambda:lifecycle(True,True); state.resume_bridge=lambda:lifecycle(True)
    def update(updates):
        values=config_model.normalize_config(updates)
        config.config.update(values)
        Path(config.CONFIG_FILE).write_text(json.dumps(config.config,ensure_ascii=False),encoding="utf-8")
        status["group_reply_mode"]=config.config["group_reply_mode"]
        return {"changed_keys":list(values),"restart_required":[]}
    state.update_config=update
    server=ThreadingHTTPServer(("127.0.0.1",args.port),WebHandler); server.daemon_threads=True
    print(f"PREVIEW_READY http://127.0.0.1:{args.port}",flush=True)
    try:
        server.serve_forever(poll_interval=0.2)
    finally:
        server.server_close(); state.memory_store.close(); temporary.cleanup()


if __name__ == "__main__":
    main()
