"""Offline integration check with the pinned AstrBot SDK and a loopback SSE server.

Run using portable/runtime/astrbot-venv/Scripts/python.exe. No desktop/API calls.
"""
from __future__ import annotations
import asyncio
import importlib.util
import json
from pathlib import Path
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import types
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / 'portable/astrbot_plugin_wechatbot_profile'
sys.path.insert(0, str(ROOT / 'wechat-weflow-bridge-ob11'))
from astrbot.core.provider.sources.openai_source import ProviderOpenAIOfficial
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.message.components import Plain
from astrbot.core.utils.active_event_registry import active_event_registry
from astrbot.core.pipeline.process_stage import follow_up


def plugin_module():
    package = types.ModuleType('_sdk_stream_profile')
    package.__path__ = [str(PLUGIN)]
    sys.modules[package.__name__] = package
    spec = importlib.util.spec_from_file_location('_sdk_stream_profile.main', PLUGIN / 'main.py')
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


async def verify(personal_style=False):
    release = threading.Event()
    received = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_POST(self):
            received.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
            self.send_response(200); self.send_header('Content-Type','text/event-stream'); self.end_headers()
            def chunk(delta, finish=None):
                value = {'id':'chatcmpl-local','object':'chat.completion.chunk','created':1,'model':'deepseek-flash',
                    'choices':[{'index':0,'delta':delta,'finish_reason':finish}]}
                self.wfile.write(('data: '+json.dumps(value,ensure_ascii=False)+'\n\n').encode('utf-8')); self.wfile.flush()
            chunk({'role':'assistant','reasoning_content':'internal reasoning stays inside history'})
            chunk({'content':'第一句。'})
            assert release.wait(5), 'first sentence was not delivered while tail was pending'
            if personal_style:
                for char in '我' * 26 + '。invalid!':
                    chunk({'content':char})
            else:
                chunk({'content':'第二句'})
                chunk({'content':' '})
                chunk({'content':'尾巴。'})
            chunk({},'stop'); self.wfile.write(b'data: [DONE]\n\n'); self.wfile.flush()
    server = ThreadingHTTPServer(('127.0.0.1',0),Handler)
    server_thread = threading.Thread(target=server.serve_forever,daemon=True); server_thread.start()
    module = plugin_module()
    settings = {'main_llm_api_base':f'http://127.0.0.1:{server.server_port}/v1',
        'main_llm_model':'deepseek-flash','main_llm_thinking':'on','main_llm_thinking_effort':'low',
        'main_llm_context_tokens':128000,'main_llm_output_tokens':4096}
    if personal_style:
        settings.update(reply_style_max_chars=25,reply_style_no_letters=True,
                        reply_style_allowed_punctuation='！，。？（）”：；~《》')
    profile = module.main_profile(settings,ROOT)
    provider = ProviderOpenAIOfficial(module.provider_config(settings,package_root=ROOT),{})
    module.bind_context_guard(provider,profile)
    calls = []
    class OwnedEvent:
        message_obj = types.SimpleNamespace(raw_message={'wechat_reply_request':{
            'request_id':'loopback-check','target_id':123,'is_group':False,'generation':1,'revision':1,
            'session_id':'wxid_verified','request_message_id':'native-message'}})
        bot = None
        def __init__(self):
            self.extra = {}
            self.bot = types.SimpleNamespace(call_action=self.call_action)
        def set_extra(self,key,value):
            self.extra[key] = value
        def get_extra(self,key,default=None):
            return self.extra.get(key,default)
        def get_sender_id(self):
            return '123'
        async def _parse_onebot_json(self,chain):
            return [{'type':'text','data':{'text':comp.text}} for comp in chain.chain if isinstance(comp,Plain)]
        async def call_action(self, action, **params):
            calls.append((action,params))
            if params['message']:
                release.set()
            return {'message_id':len(calls)}
    event = OwnedEvent()
    module.bind_streaming(event,profile)
    async def ordinary_sdk_pipeline():
        async for response in provider.text_chat_stream(prompt='local synthetic test',request_max_retries=1):
            if response.is_chunk and response.result_chain:
                yield response.result_chain
    try:
        await asyncio.wait_for(event.send_streaming(ordinary_sdk_pipeline()),8)
        answer = ''.join(segment['data']['text'] for _,params in calls for segment in params['message'])
        assert len(received) == 1, 'must use only one main-model HTTP call'
        assert received[0]['stream'] is True
        assert answer == ("第一句。" + "我" * 26 + "。invalid!" if personal_style else "第一句。第二句 尾巴。"), repr(answer)
        assert calls[-1][1]['message'] == [] and calls[-1][1]['wechat_stream']['final'] is True
        # Real SDK conversion preserves hidden thinking in a tool-history request.
        payload = {'model':'deepseek-flash','messages':[{'role':'assistant','content':[
            {'type':'think','think':'original hidden reasoning'}, {'type':'text','text':'answer'}],
            'tool_calls':[{'id':'call1','type':'function','function':{'name':'tool','arguments':'{}'}}]}]}
        provider._finally_convert_payload(payload)
        assert payload['messages'][0]['reasoning_content'] == 'original hidden reasoning'
        assert all(part['type'] != 'think' for part in payload['messages'][0]['content'])
        # A new input must start its own request rather than be consumed by the
        # SDK's active runner carrying an obsolete bridge delivery ticket.
        old, new, other = OwnedEvent(), OwnedEvent(), OwnedEvent()
        old.unified_msg_origin = new.unified_msg_origin = 'wechatbot-portable:FriendMessage:123'
        other.unified_msg_origin = 'wechatbot-portable:FriendMessage:456'
        stop = Mock()
        runner = types.SimpleNamespace(run_context=types.SimpleNamespace(context=types.SimpleNamespace(event=old)),request_stop=stop)
        for item in (old,new,other):
            active_event_registry.register(item)
        follow_up.register_active_runner(old.unified_msg_origin,runner)
        try:
            assert module.stop_superseded_runs(new) == 1
            assert old.get_extra('agent_stop_requested') is True
            assert not other.get_extra('agent_stop_requested')
            stop.assert_called_once()
            assert follow_up.try_capture_follow_up(new) is None
        finally:
            follow_up.unregister_active_runner(old.unified_msg_origin,runner)
            for item in (old,new,other):
                active_event_registry.unregister(item)
        print(json.dumps({'ok':True,'sdk':'AstrBot 4.28.2','http_calls':len(received),'streaming':True,
            'answer_parts':len(calls)-1,'first_before_tail':release.is_set(),'reasoning_hidden_and_preserved':True,
            'new_input_not_consumed_by_stale_runner':True,'legacy_style_settings':personal_style}))
    finally:
        release.set()
        await provider.terminate()
        server.shutdown(); server.server_close(); server_thread.join(2)


if __name__ == '__main__':
    asyncio.run(verify(personal_style='--personal-style' in sys.argv))
