"""Real asyncio/WebSocket loopback integration; no live WeChat or paid APIs."""
import asyncio
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
import websockets

SOURCE=Path(__file__).resolve().parents[1]/"wechat-weflow-bridge-ob11"
sys.path.insert(0,str(SOURCE))
import config
import state
import ob_client
import ob_protocol
from outbox import Outbox
from send_queue import SendQueue

class AsyncIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix=".integration-",dir=SOURCE.parent/"tests")
        self.box=Outbox(str(Path(self.tmp.name)/"outbox.db"),10)
        self.queue=SendQueue(10)
        self.saved={key:getattr(state,key) for key in ("running","paused","send_cancel","bridge_generation","client_generation","sender_instance","send_queue","outbox","_ob_ws","_ob_ws_loop","pending_reply_modes","suppress_text_until")}
        state.running=True; state.paused=threading.Event(); state.send_cancel=threading.Event()
        state.bridge_generation=123; state.client_generation=456
        state.sender_instance=object(); state.send_queue=self.queue; state.outbox=self.box
        state.pending_reply_modes={}; state.suppress_text_until={}
        state._ob_ws=None; state._ob_ws_loop=None
    async def asyncTearDown(self):
        state.running=False
        self.queue.close()
        self.queue.worker.join(2)
        self.box.close()
        for key,value in self.saved.items():
            setattr(state,key,value)
        self.tmp.cleanup()

    async def test_api_failure_truthful_and_async_job_visible(self):
        request={"action":"send_private_msg","params":{"user_id":1,"message":"完整回答"},"echo":0}
        with patch.object(ob_protocol,"_resolve_contact",return_value="好友"),patch.object(ob_protocol,"_perform_send",return_value=False):
            result=await ob_protocol._handle_ob_api(request,generation=456)
        self.assertEqual(result["status"],"failed"); self.assertEqual(result["echo"],0)
        self.assertIsInstance(result["data"]["message_id"],int)
        gate=threading.Event()
        def delayed(*args):
            gate.wait(2)
            return True
        with patch.object(ob_protocol,"_resolve_contact",return_value="好友"),patch.object(ob_protocol,"_perform_send",side_effect=delayed),patch.object(config,"API_SEND_WAIT_SECONDS",0.01):
            result=await ob_protocol._handle_ob_api(request,generation=456)
            self.assertEqual(result["status"],"async"); self.assertEqual(result["retcode"],1)
            gate.set()
            await asyncio.to_thread(self.queue.q.join)
        self.assertEqual(self.queue.stats()["recent"][0]["state"],"sent")

    async def test_pause_after_peek_keeps_event(self):
        event={"self_id":1,"message_id":1,"time":1}; self.box.put(event)
        real_peek=self.box.peek
        def pausing_peek(wait_seconds=0):
            row=real_peek(wait_seconds); state.paused.set(); return row
        class WS:
            async def send(self,_):
                raise AssertionError("pause后不应发送")
        with patch.object(self.box,"peek",side_effect=pausing_peek):
            task=asyncio.create_task(ob_client._outbox_writer(WS(),456))
            await asyncio.sleep(0.1)
            task.cancel()
            await asyncio.gather(task,return_exceptions=True)
        self.assertEqual(self.box.stats()["pending"],1)

    async def test_real_reverse_websocket_delivers_outbox_and_api(self):
        self.box.put({"time":1,"self_id":1,"post_type":"message","message_id":99,"message_type":"private","user_id":2,"message":[{"type":"text","data":{"text":"测试"}}]})
        done=asyncio.Event(); received=[]
        async def server_handler(ws):
            first=json.loads(await ws.recv()); self.assertEqual(first["meta_event_type"],"lifecycle")
            await ws.send(json.dumps({"action":"get_login_info","echo":0}))
            for _ in range(2):
                received.append(json.loads(await ws.recv()))
            done.set()
            await ws.wait_closed()
        async with websockets.serve(server_handler,"127.0.0.1",0) as server:
            port=server.sockets[0].getsockname()[1]
            with patch.object(config,"ASTRBOT_OB_URL",f"ws://127.0.0.1:{port}"):
                task=asyncio.create_task(ob_client._ob_client_main(456))
                try:
                    await asyncio.wait_for(done.wait(),5)
                    self.assertTrue(any(x.get("message_id")==99 for x in received))
                    self.assertTrue(any(x.get("status")=="ok" and x.get("echo")==0 for x in received))
                    for _ in range(20):
                        if self.box.stats()["pending"]==0: break
                        await asyncio.sleep(0.02)
                    self.assertEqual(self.box.stats()["pending"],0)
                finally:
                    state.running=False
                    if state._ob_ws:
                        await state._ob_ws.close()
                    await asyncio.wait_for(task,3)

if __name__=="__main__":
    unittest.main()
