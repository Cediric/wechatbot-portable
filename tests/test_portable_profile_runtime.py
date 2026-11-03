"""Bounded profile generations and process ownership, with no external calls."""
from __future__ import annotations
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import AsyncMock, Mock, patch

ROOT=Path(__file__).resolve().parents[1]
PLUGIN=ROOT/'portable/astrbot_plugin_wechatbot_profile'
sys.path.insert(0,str(ROOT/'portable'))
import astrbot_lifecycle as lifecycle

def module_stub(name, **attrs):
    result=types.ModuleType(name);result.__dict__.update(attrs);return result

def load_plugin():
    identity=lambda *a,**k: lambda fn: fn
    base=type('Star',(),{'__init__':lambda self,context:None})
    filters=types.SimpleNamespace(EventMessageType=types.SimpleNamespace(ALL=1),event_message_type=identity,on_llm_request=identity)
    package=module_stub('_wechatbot_plugin_unit');package.__path__=[str(PLUGIN)]
    modules={'_wechatbot_plugin_unit':package,'astrbot':module_stub('astrbot'),'astrbot.api':module_stub('astrbot.api',logger=Mock()),
        'astrbot.api.event':module_stub('astrbot.api.event',AstrMessageEvent=object,filter=filters),
        'astrbot.api.star':module_stub('astrbot.api.star',Context=object,Star=base,register=identity),
        'astrbot.core':module_stub('astrbot.core'),'astrbot.core.provider':module_stub('astrbot.core.provider'),
        'astrbot.core.provider.entities':module_stub('astrbot.core.provider.entities',ProviderRequest=object)}
    spec=importlib.util.spec_from_file_location('_wechatbot_plugin_unit.main',PLUGIN/'main.py')
    module=importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules,modules):spec.loader.exec_module(module)
    return module

class Event:
    def __init__(self):self.extra={};self.stopped=False
    def get_platform_id(self):return 'wechatbot-portable'
    def set_extra(self,key,value):self.extra[key]=value
    def stop_event(self):self.stopped=True

class ProfileRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_generations_are_bounded_and_live_snapshot_is_preserved(self):
        module=load_plugin()
        provider=types.SimpleNamespace(terminate=AsyncMock())
        manager=types.SimpleNamespace(load_provider=AsyncMock(),get_provider_by_id=AsyncMock(return_value=provider))
        plugin=module.WechatbotProfile(types.SimpleNamespace(provider_manager=manager))
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'status.json'
            plugin._location={'root':ROOT,'platform':'wechatbot-portable','status':path}
            settings={'main_llm_api_base':'http://127.0.0.1:11434/v1','main_llm_model':'synthetic','main_llm_api_key':''}
            snapshot=[settings,module.main_profile(settings,ROOT)]
            plugin._read=lambda:(plugin._location,tuple(snapshot))
            original=Event();await plugin.select_profile(original)
            frozen=json.dumps(original.extra['wechatbot_main_profile'],sort_keys=True)
            for n in range(1,module.MAX_PROFILE_VERSIONS):
                snapshot[0]=dict(settings,main_llm_prompt=f'generation-{n}')
                snapshot[1]=module.main_profile(snapshot[0],ROOT)
                event=Event();await plugin.select_profile(event)
                self.assertFalse(event.stopped)
            snapshot[0]=dict(settings,main_llm_prompt='unloaded-generation')
            snapshot[1]=module.main_profile(snapshot[0],ROOT)
            blocked=Event();await plugin.select_profile(blocked)
            self.assertTrue(blocked.stopped)
            self.assertEqual(manager.load_provider.await_count,module.MAX_PROFILE_VERSIONS)
            provider.terminate.assert_not_awaited()
            self.assertEqual(json.dumps(original.extra['wechatbot_main_profile'],sort_keys=True),frozen)
            status=json.loads(path.read_text(encoding='utf8'))
            self.assertEqual(status['state'],'restart_required')
            self.assertIn('重启AstrBot.bat',status['reason'])
            # A known generation can still run after the bound is reached.
            snapshot[0]=settings;snapshot[1]=module.main_profile(settings,ROOT)
            known=Event();await plugin.select_profile(known)
            self.assertFalse(known.stopped)
            self.assertEqual(known.extra['selected_provider'],original.extra['selected_provider'])

class LifecycleTests(unittest.TestCase):
    def test_reused_pid_never_gets_an_exit_signal(self):
        with tempfile.TemporaryDirectory() as directory:
            record=Path(directory)/'process.json'
            value={'pid':123,'created':1.0,'executable':'synthetic','command':['run'],'cwd':'unused','instance':'owned'}
            record.write_text(json.dumps(value))
            with patch.object(lifecycle,'RECORD',record),patch.object(lifecycle.psutil,'Process'),patch.object(lifecycle,'fingerprint',return_value=dict(value,created=2.0)),patch.object(lifecycle.subprocess,'run') as signal:
                with self.assertRaises(RuntimeError):lifecycle.stop_owned()
                signal.assert_not_called()
                self.assertTrue(record.exists())

if __name__=='__main__':unittest.main()
