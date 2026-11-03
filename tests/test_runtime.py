"""No live desktop or paid services. Exercise lifecycle, delivery and privacy boundaries."""
import asyncio
import base64
from concurrent.futures import Future
import importlib
import json
import os
from pathlib import Path
import sys
import stat
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

SOURCE=Path(__file__).resolve().parents[1]/"wechat-weflow-bridge-ob11"
sys.path.insert(0,str(SOURCE))
import config
import config_model
import bootstrap
import state
import humanize
import ob_protocol
from memory_store import MemoryStore
from outbox import Outbox
from send_queue import SendQueue
from stable_id import IdentityRegistry
from autopilot import Autopilot

def run_inline(coro):
    try:
        coro.send(None)
    except StopIteration as done:
        return done.value
    finally:
        coro.close()
    raise AssertionError("测试协程出现未模拟异步等待")

class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix=".runtime-test-",dir=SOURCE.parent/"tests")
        self.root=Path(self.tmp.name)
        self.store=None
        self.patches=[]
    def tearDown(self):
        if self.store:
            self.store.close()
        for item in reversed(self.patches):
            item.stop()
        self.tmp.cleanup()
    def replace(self,obj,**kwargs):
        item=patch.multiple(obj,**kwargs); item.start(); self.patches.append(item)

    def test_numeric_validation(self):
        for key,value in (("web_port",8766.5),("core_workers",1.5),("buffer_seconds",float("nan")),("bot_image_ref_strength",1.2),("web_port",80)):
            with self.subTest(key=key),self.assertRaises(ValueError):
                config_model.normalize_config({key:value})
        self.assertEqual(config_model.normalize_config({"voice_enabled":"false","private_buffer_seconds":0.2}),{"voice_enabled":False,"private_buffer_seconds":0.2})
        with self.assertRaises(ValueError):
            config_model.normalize_config({"unknown":1})

    def test_required_paths_urls_and_loaded_config_are_validated(self):
        for key in ("memory_db_path", "tts_output_dir", "moments_history_path", "sticker_dir", "weflow_base_url", "astrbot_ob_url"):
            with self.subTest(key=key), self.assertRaises(ValueError):
                config_model.normalize_config({key:" "})
        for value in ("https://example.org:99999", "https://user:secret@example.org", "https://example.org\n/path"):
            with self.assertRaises(ValueError):
                config_model.normalize_config({"weflow_base_url":value})
        path=self.root/"loaded.json"
        path.write_text(json.dumps({"voice_enabled":"false", "old_extension_setting":"preserved"}),encoding="utf-8")
        self.replace(config,CONFIG_FILE=str(path))
        self.assertEqual(config.load_config(),{"voice_enabled":False,"old_extension_setting":"preserved"})
        path.write_text("[]",encoding="utf-8")
        with self.assertRaises(ValueError):
            config.load_config()

    def test_service_probe_uses_configured_addresses(self):
        path=self.root/"services.json"
        path.write_text(json.dumps({"weflow_base_url":"http://localhost:6001", "astrbot_ob_url":"wss://example.org/ws"}),encoding="utf-8")
        self.replace(bootstrap,CONFIG_FILE=str(path))
        self.assertEqual(bootstrap.service_endpoints(),[("WeFlow","localhost",6001),("AstrBot","example.org",443)])

    def test_http_worker_sessions_reuse_without_shared_cookies(self):
        import http_util
        results=[]
        barrier=threading.Barrier(2)
        def worker(name):
            current=http_util.session()
            current.cookies.set("synthetic",name)
            barrier.wait(timeout=2)
            results.append((current, current is http_util.session(),current.cookies.get("synthetic")))
            current.close()
        threads=[threading.Thread(target=worker,args=(name,)) for name in ("a","b")]
        for thread in threads: thread.start()
        for thread in threads: thread.join(3)
        self.assertEqual(len(results),2)
        self.assertIsNot(results[0][0],results[1][0])
        self.assertTrue(all(result[1] for result in results))
        self.assertEqual({result[2] for result in results},{"a","b"})

    @unittest.skipUnless(os.name == "nt", "Windows drive URI compatibility")
    def test_windows_file_uri_variants(self):
        from urllib.parse import quote
        path=self.root/"space file.wav"
        path.write_bytes(b"synthetic audio")
        for value in (str(path), path.as_uri(), "file://"+quote(path.as_posix(),safe="/:"), "file://localhost/"+quote(path.as_posix(),safe="/:")):
            with self.subTest(value=value):
                resolved,owned=ob_protocol._resolve_media(value,suffix=".wav")
                self.assertEqual(Path(resolved),path)
                self.assertFalse(owned)

    def test_atomic_config_and_secret_redaction(self):
        path=self.root/"config.json"
        before=dict(config.config)
        self.replace(config,BASE_DIR=str(self.root),CONFIG_FILE=str(path),config=before.copy())
        with patch.object(config,"apply_runtime_config"):
            result=config.save_config({"access_token":"test-secret","web_port":9000})
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["web_port"],9000)
        self.assertEqual(result["access_token"],"test-secret")
        self.replace(config,config={"access_token":"test-secret"})
        self.assertNotIn("test-secret",config.redact_text("https://x/?access_token=test-secret"))
        with self.assertRaises(ValueError):
            config.save_config({"weflow_base_url":"javascript:bad"})

    def test_unrelated_setting_preserves_legacy_style_keys_and_persona(self):
        old = {"bot_persona": "  原角色 & 标记！\r\n下一行。\n", "reply_style_no_letters": True,
               "reply_style_allowed_punctuation": "！，。？", "reply_style_max_chars": 25}
        path = self.root / "config.json"
        path.write_text(json.dumps(old), encoding="utf-8")
        self.replace(config, BASE_DIR=str(self.root), CONFIG_FILE=str(path))
        self.replace(config, config=config.load_config())
        with patch.object(config, "apply_runtime_config"):
            config.save_config({"voice_enabled": False})
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {**old, "require_reply_ticket": True, "voice_enabled": False})

    @unittest.skipUnless(os.name == "nt", "Windows read-only config regression")
    def test_atomic_config_replaces_readonly_settings(self):
        path = self.root / "config.json"
        before = {"web_port": 8766, "voice_enabled": False}
        path.write_text(json.dumps(before), encoding="utf-8")
        os.chmod(path, stat.S_IREAD)
        self.replace(config, BASE_DIR=str(self.root), CONFIG_FILE=str(path), config=before.copy(),
                     WEB_PORT=config.WEB_PORT, VOICE_ENABLED=config.VOICE_ENABLED)
        self.assertTrue(path.stat().st_file_attributes & stat.FILE_ATTRIBUTE_READONLY)
        saved = config.save_config({"web_port": 9000})
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), saved)
        self.assertEqual(config.config["web_port"], 9000)
        self.assertEqual(config.WEB_PORT, 9000)
        self.assertFalse(path.stat().st_file_attributes & stat.FILE_ATTRIBUTE_READONLY)
        self.assertEqual(list(self.root.glob("config-*.json")), [])

    @unittest.skipUnless(os.name == "nt", "Windows read-only config regression")
    def test_atomic_config_failure_restores_readonly_and_preserves_memory(self):
        path = self.root / "config.json"
        before = {"web_port": 8766, "voice_enabled": False}
        original = json.dumps(before).encode("utf-8")
        path.write_bytes(original)
        os.chmod(path, stat.S_IREAD)
        attrs = path.stat().st_file_attributes
        self.replace(config, BASE_DIR=str(self.root), CONFIG_FILE=str(path), config=before.copy())
        with patch.object(config.os, "replace", side_effect=PermissionError(5, "synthetic denial")), \
                patch.object(config, "apply_runtime_config") as apply, self.assertRaises(PermissionError):
            config.save_config({"web_port": 9000})
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(path.stat().st_file_attributes, attrs)
        self.assertEqual(config.config, before)
        apply.assert_not_called()
        self.assertEqual(list(self.root.glob("config-*.json")), [])

    @unittest.skipUnless(os.name == "nt", "Windows read-only config regression")
    def test_atomic_config_does_not_restore_readonly_on_external_replacement(self):
        path = self.root / "config.json"
        before = {"web_port": 8766, "voice_enabled": False}
        path.write_text(json.dumps(before), encoding="utf-8")
        os.chmod(path, stat.S_IREAD)
        self.replace(config, BASE_DIR=str(self.root), CONFIG_FILE=str(path), config=before.copy())
        real_replace = os.replace
        external = b'{"external": "preserved"}'
        def replaced_elsewhere(source, destination):
            other = self.root / "other.json"
            other.write_bytes(external)
            real_replace(other, destination)
            raise PermissionError(5, "synthetic denial")
        with patch.object(config.os, "replace", side_effect=replaced_elsewhere), \
                patch.object(config, "apply_runtime_config") as apply, self.assertRaises(PermissionError):
            config.save_config({"web_port": 9000})
        self.assertEqual(path.read_bytes(), external)
        self.assertFalse(path.stat().st_file_attributes & stat.FILE_ATTRIBUTE_READONLY)
        self.assertEqual(config.config, before)
        apply.assert_not_called()
        self.assertEqual(list(self.root.glob("config-*.json")), [])

    @unittest.skipUnless(os.name == "nt", "Windows read-only import log regression")
    def test_config_import_preserves_readonly_bridge_log_history(self):
        for name in ("config.py", "config_model.py", "model_catalog.py"):
            (self.root / name).write_bytes((SOURCE / name).read_bytes())
        (self.root / "config.json").write_text('{"web_port":8766}', encoding="utf-8")
        path = self.root / "bridge.log"
        original = b"historical log bytes\n"
        path.write_bytes(original)
        os.chmod(path, stat.S_IREAD)
        result = subprocess.run([sys.executable, "-X", "utf8", "-B", "-c", "import config"],
                                cwd=self.root, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(path.read_bytes(), original)
        self.assertFalse(path.stat().st_file_attributes & stat.FILE_ATTRIBUTE_READONLY)

    def test_disabled_memory_can_be_enabled_and_scoped(self):
        self.store=MemoryStore(str(self.root/"memory.db"),False)
        self.store.enabled=True
        self.store.add_memory("小明喜欢红色书籍",session_id="a")
        self.store.add_memory("小明喜欢红色书籍",session_id="b")
        self.store.add_memory("大家喜欢红色",session_id="")
        self.replace(config,MEMORY_SCOPE="session")
        rows=self.store.search("红色",session_id="a")
        self.assertTrue(rows)
        self.assertNotIn("b",[r["session_id"] for r in rows])
        self.assertEqual(self.store.count("小明"),2)
        self.assertEqual(len(self.store.list_memories(query="小明",offset=1)),1)

    def test_old_memory_is_searchable(self):
        self.store=MemoryStore(str(self.root/"memory.db"),True)
        self.store.add_memory("喜欢星空望远镜",session_id="a")
        for i in range(510):
            self.store.add_memory(f"记录苹果{i}",session_id="a")
        self.replace(config,MEMORY_SCOPE="session")
        self.assertEqual(self.store.search("星空望远镜",session_id="a")[0]["content"],"喜欢星空望远镜")

    def test_humanization_preserves_tail_links_code_and_voice(self):
        text="开头。"+"详细说明。"*200+"结尾保留。"
        self.assertEqual("".join(humanize.prepare_bubbles(text)),text)
        for structured in ("点这里 https://example.org/test?a=1", "```python\nprint('hello')\n```", "1. 第一项\n2. 第二项"):
            self.assertEqual(humanize.prepare_bubbles(structured),[structured])
        self.assertEqual(humanize.voice_script(text),text)
        self.replace(config,HUMAN_LIKE_ENABLED=False)
        self.assertEqual(humanize.prepare_bubbles(text),[text])

    def test_echo_requires_same_session_and_sender_evidence(self):
        state.note_sent_text("好的",session_id="a")
        self.assertFalse(state.is_self_echo("好的",session_id="a",sender_id="friend"))
        self.assertFalse(state.is_self_echo("好的",session_id="b",direction="sent"))
        self.assertTrue(state.is_self_echo("好的",session_id="a",direction="sent"))

    def test_outbox_reopen_dedup_fifo_full(self):
        path=str(self.root/"outbox.db"); box=Outbox(path,2)
        first={"self_id":1,"message_id":1,"time":1}; second={"self_id":1,"message_id":2,"time":1}
        self.assertTrue(box.put(first)); self.assertTrue(box.put(first)); self.assertTrue(box.put(second))
        self.assertFalse(box.put({**second,"message_id":3})); box.close()
        box=Outbox(path,2)
        row,event=box.peek(); self.assertEqual(event,first)
        box.acknowledge(row); self.assertEqual(box.peek()[1],second); box.close()

    def test_send_queue_failure_cancel_and_order(self):
        queue=SendQueue(10); seen=[]
        futures=[queue.submit(i,lambda i=i:seen.append(i) or i!=1,lambda:False) for i in range(3)]
        self.assertEqual([f.result(2) for f in futures],[True,False,True]); self.assertEqual(seen,[0,1,2])
        self.assertFalse(queue.submit(4,lambda:True,lambda:True).result(2))
        queue.close()
        with self.assertRaises(RuntimeError):
            queue.submit(5,lambda:True,lambda:False).result(1)

    def test_protocol_unknown_stale_echo_and_login(self):
        self.replace(state,client_generation=9)
        response=run_inline(ob_protocol._handle_ob_api({"action":"unsupported","echo":0}))
        self.assertEqual(response["status"],"failed"); self.assertEqual(response["echo"],0)
        response=run_inline(ob_protocol._handle_ob_api({"action":"get_login_info","echo":False}))
        self.assertIn("user_id",response["data"]); self.assertEqual(response["status"],"ok")
        response=run_inline(ob_protocol._handle_ob_api({"action":"send_msg","params":{}},generation=8))
        self.assertEqual(response["status"],"failed"); self.assertIn("旧连接",response["message"])

    def test_cq_parser(self):
        segments=ob_protocol._normalize_message("你好[CQ:face,id=1]a&#91;b&#93;")
        self.assertEqual([s["type"] for s in segments],["text","face","text"])
        self.assertEqual(segments[-1]["data"]["text"],"a[b]")
        self.assertEqual(ob_protocol._normalize_message("[CQ:face,id=1]",True)[0]["type"],"text")

    def test_owned_file_cleanup_never_deletes_tmp_named_attachment(self):
        import wave
        from PIL import Image
        path=self.root/"tmp-user-image.png"
        Image.new("RGB", (2, 2), "white").save(path)
        self.replace(state,running=True,paused=threading.Event(),bridge_generation=3,pending_reply_modes={},suppress_text_until={})
        with patch.object(ob_protocol.name_refresh,"refresh_target"),patch.object(ob_protocol,"_resolve_contact",return_value="friend"),patch.object(state,"dispatch_send",return_value=True):
            self.assertTrue(ob_protocol._perform_send({},1,False,[{"type":"image","data":{"file":str(path)}}],3))
        self.assertTrue(path.exists())
        with wave.open(str(path), "wb") as wav:
            wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(8000)
            wav.writeframes(b"\x00\x00" * 800)
        encoded=base64.b64encode(path.read_bytes()).decode()
        with patch.object(ob_protocol.name_refresh,"refresh_target"),patch.object(ob_protocol,"_resolve_contact",return_value="friend"),patch.object(state,"dispatch_send",return_value=False),patch.object(ob_protocol,"_decode_base64_file",return_value=str(path)):
            self.assertFalse(ob_protocol._perform_send({},1,False,[{"type":"record","data":{"file":"base64://"+encoded}}],3))
        self.assertFalse(path.exists())

    def test_voice_disabled_honored_without_tts(self):
        self.replace(config,VOICE_ENABLED=False)
        with patch.object(state,"dispatch_send",return_value=True) as send,patch.object(ob_protocol,"synthesize_voice") as synth:
            self.assertTrue(ob_protocol._send_text_with_mode("friend","完整回复","voice"))
            self.assertEqual(send.call_args.args[0],"send_text"); synth.assert_not_called()

    def test_autopilot_false_string_and_disable_during_model(self):
        self.replace(config,AUTOPILOT_ENABLED=True)
        self.replace(state,running=True,paused=threading.Event(),bridge_generation=4)
        bot=Autopilot(); sender=type("Sender",(),{"send_moment":lambda *a,**kw: self.fail("不得发布")})()
        bot.moments_sender=sender
        with patch.object(bot,"_build_context",return_value=""),patch.object(bot,"_ask_llm",return_value={"should_post":"false","content":"不要发布"}):
            bot.think_and_post()
        def decide(_):
            bot.set_enabled(False)
            return {"should_post":True,"content":"旧任务"}
        with patch.object(bot,"_build_context",return_value=""),patch.object(bot,"_ask_llm",side_effect=decide):
            bot.think_and_post()
        self.assertIn("取消",bot.last_result)

    def test_websocket_old_and_new_headers(self):
        from ob_client import _connect_kwargs
        def modern(uri,additional_headers=None,**kwargs): pass
        def legacy(uri,extra_headers=None,**kwargs): pass
        self.assertIn("additional_headers",_connect_kwargs(modern)); self.assertIn("extra_headers",_connect_kwargs(legacy))

if __name__=="__main__":
    unittest.main()
