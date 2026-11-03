"""Bounded reverse WebSocket client; old/new websockets header compatibility."""
import asyncio
import hashlib
import inspect
import json
import logging
import threading
import time
import websockets
import config
import state
from ob_protocol import _handle_ob_api

log=logging.getLogger("ob11-bridge")

def _connect_kwargs(connect):
    parameters=inspect.signature(connect).parameters
    key="additional_headers" if "additional_headers" in parameters else "extra_headers"
    return {key:{"X-Self-ID":str(state._self_id_int),"X-Client-Role":"Universal","User-Agent":"WeChatBot/6.33"},
            "ping_interval":20,"ping_timeout":20,"open_timeout":10,"close_timeout":3,"max_size":config.MEDIA_MAX_MB*1024*1024*2,"max_queue":16}

def _active(generation):
    return state.running and state.client_generation==generation

async def _outbox_writer(ws,generation):
    while _active(generation):
        if state.paused.is_set() or state.outbox is None:
            await asyncio.sleep(0.1)
            continue
        outbox=state.outbox
        entry=await asyncio.to_thread(outbox.peek,0.5)
        if not entry:
            continue
        row_id,event=entry
        if not _active(generation) or state.paused.is_set():
            continue
        prepare_reply = getattr(state,"prepare_outbox_reply",None)
        if callable(prepare_reply) and not prepare_reply(event,state.bridge_generation):
            await asyncio.to_thread(outbox.acknowledge,row_id)
            state.increment("cancelled")
            continue
        started=time.monotonic()
        await ws.send(json.dumps(event,ensure_ascii=False))
        ticket=event.get("wechat_reply_request",{})
        request_id=ticket.get("request_id") if isinstance(ticket,dict) else None
        if isinstance(request_id,str) and request_id:
            turn=hashlib.sha256(request_id.encode("utf-8")).hexdigest()[:12]
            log.info("[耗时] turn=%s stage=astrbot_forward seconds=%.3f result=websocket_sent",
                     turn,max(0,time.monotonic()-started))
        await asyncio.to_thread(outbox.acknowledge,row_id)
        state.increment("forwarded")

async def _ob_client_main(generation):
    retry=0.5
    while _active(generation):
        tasks=[]
        try:
            async with websockets.connect(config.ASTRBOT_OB_URL,**_connect_kwargs(websockets.connect)) as ws:
                with state.run_lock:
                    if not _active(generation):
                        return
                    state._ob_ws=ws
                await ws.send(json.dumps({"time":int(time.time()),"self_id":state._self_id_int,"post_type":"meta_event","meta_event_type":"lifecycle","sub_type":"connect"}))
                with state.run_lock:
                    if not _active(generation):
                        return
                    state._ob_ws_ready.set()
                state.record_activity("connection","AstrBot 已连接")
                log.info("AstrBot OneBot 已连接，生命周期握手已发送。")
                retry=0.5
                jobs=asyncio.Queue(maxsize=64)
                async def api_worker():
                    while _active(generation):
                        data=await jobs.get()
                        try:
                            await _handle_ob_api(data,ws=ws,generation=generation)
                        except Exception as exc:
                            log.warning("API 请求处理失败: %s",exc)
                        finally:
                            jobs.task_done()
                tasks=[asyncio.create_task(api_worker()) for _ in range(4)]
                writer=asyncio.create_task(_outbox_writer(ws,generation))
                tasks.append(writer)
                async def writer_done():
                    try:
                        await writer
                    finally:
                        await ws.close()
                tasks.append(asyncio.create_task(writer_done()))
                async for raw in ws:
                    if not _active(generation):
                        break
                    try:
                        data=json.loads(raw)
                    except (json.JSONDecodeError,UnicodeDecodeError):
                        log.warning("忽略无效 WebSocket JSON")
                        continue
                    if isinstance(data,dict) and "action" in data:
                        await jobs.put(data)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if _active(generation):
                log.warning("AstrBot 断开: %s",exc)
        finally:
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks,return_exceptions=True)
            with state.run_lock:
                if state.client_generation==generation:
                    state._ob_ws=None
                    state._ob_ws_ready.clear()
        if _active(generation):
            await asyncio.sleep(retry)
            retry=min(5,retry*2)

def _run_ob_client(generation=None):
    generation=state.client_generation if generation is None else generation
    loop=asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    with state.run_lock:
        if not _active(generation):
            loop.close()
            return
        state._ob_ws_loop=loop
    try:
        loop.run_until_complete(_ob_client_main(generation))
    finally:
        pending=asyncio.all_tasks(loop)
        for task in pending:
            task.cancel()
        if pending:
            loop.run_until_complete(asyncio.gather(*pending,return_exceptions=True))
        loop.run_until_complete(loop.shutdown_asyncgens())
        loop.close()
        with state.run_lock:
            if state._ob_ws_loop is loop:
                state._ob_ws_loop=None
            if state.client_generation==generation:
                state._ob_ws=None
                state._ob_ws_ready.clear()
