"""A deployed plugin binds rules to its verified package, never its parents."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import types
import unittest
import uuid
from unittest.mock import AsyncMock, Mock, patch

import test_stream_runtime as streams

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "portable" / "astrbot_plugin_wechatbot_profile"
STYLE = {"reply_style_max_chars": 25, "reply_style_no_letters": True,
         "reply_style_allowed_punctuation": "！，。？（）”：；~《》"}


def copy_plugin(directory):
    target = Path(directory) / "astrbot-instance" / "data" / "plugins" / "astrbot_plugin_wechatbot_profile"
    shutil.copytree(PLUGIN, target, ignore=shutil.ignore_patterns("__pycache__"))
    return target


def load_copied_plugin(target):
    name = "_copied_profile_" + uuid.uuid4().hex
    package = types.ModuleType(name)
    package.__path__ = [str(target)]
    identity = lambda *args, **kwargs: lambda fn: fn
    base = type("Star", (), {"__init__": lambda self, context: None})
    filters = types.SimpleNamespace(EventMessageType=types.SimpleNamespace(ALL=1),
                                    event_message_type=identity, on_llm_request=identity)
    def stub(name, **values):
        result = types.ModuleType(name)
        result.__dict__.update(values)
        return result
    modules = {name: package, "astrbot": stub("astrbot"), "astrbot.api": stub("astrbot.api", logger=Mock()),
               "astrbot.api.event": stub("astrbot.api.event", AstrMessageEvent=object, filter=filters),
               "astrbot.api.star": stub("astrbot.api.star", Star=base, Context=object, register=identity),
               "astrbot.core": stub("astrbot.core"), "astrbot.core.provider": stub("astrbot.core.provider"),
               "astrbot.core.provider.entities": stub("astrbot.core.provider.entities", ProviderRequest=object)}
    spec = importlib.util.spec_from_file_location(name + ".main", target / "main.py")
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, modules):
        spec.loader.exec_module(module)
        copied_streaming = sys.modules[name + ".streaming"]
    return module, copied_streaming


def rule_package(parent, name):
    root = Path(parent) / name
    bridge = root / "wechat-weflow-bridge-ob11"
    bridge.mkdir(parents=True)
    (root / "portable").mkdir()
    (root / "portable" / "runtime-manifest.json").write_text("{}", encoding="utf-8")
    return root


class OwnedEvent(streams.StreamEvent):
    def __init__(self):
        super().__init__()
        self.stopped = False
    def get_platform_id(self):
        return "wechatbot-portable"
    def stop_event(self):
        self.stopped = True


class CopiedPluginTests(unittest.IsolatedAsyncioTestCase):
    async def test_deployed_select_profile_sends_model_text_with_legacy_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            main, copied = load_copied_plugin(copy_plugin(directory))
            manager = types.SimpleNamespace(load_provider=AsyncMock(), get_provider_by_id=AsyncMock(return_value=object()))
            plugin = main.WechatbotProfile(types.SimpleNamespace(provider_manager=manager))
            settings = {**STYLE, "main_llm_api_base": "http://127.0.0.1:11434/v1", "main_llm_model": "synthetic"}
            profile = main.main_profile(settings, ROOT)
            plugin._location = {"root": ROOT, "platform": "wechatbot-portable", "status": Path(directory) / "status.json"}
            plugin._read = lambda: (plugin._location, (settings, profile))
            event = OwnedEvent()
            await plugin.select_profile(event)
            self.assertFalse(event.stopped)
            self.assertIn("selected_provider", event.extra)
            async def chunks():
                yield streams.Chain("我" * 20 + "。")
                yield streams.Chain("你" * 4 + "。hello!")
            await event.send_streaming(chunks())
            body = "".join(segment["data"]["text"] for _, params in event.calls for segment in params["message"])
            self.assertEqual(body, "我" * 20 + "。" + "你" * 4 + "。hello!")
            self.assertTrue(event.calls[-1][1]["wechat_stream"]["final"])

    async def test_bridge_config_outside_declared_root_never_loads_provider(self):
        with tempfile.TemporaryDirectory() as directory:
            main, _ = load_copied_plugin(copy_plugin(directory))
            instance = Path(directory) / "astrbot-instance"
            location = instance / "data" / "wechatbot-profile-location.json"
            location.write_text(json.dumps({"package_root": str(ROOT), "bridge_config": str(Path(directory) / "outside.json"),
                                             "platform_id": "wechatbot-portable"}), encoding="utf-8")
            manager = types.SimpleNamespace(load_provider=AsyncMock(), get_provider_by_id=AsyncMock())
            plugin = main.WechatbotProfile(types.SimpleNamespace(provider_manager=manager))
            event = OwnedEvent()
            with patch.dict(os.environ, {"ASTRBOT_ROOT": str(instance)}):
                await plugin.select_profile(event)
            self.assertTrue(event.stopped)
            manager.load_provider.assert_not_awaited()
            self.assertEqual(event.calls, [])

    async def test_policy_code_catalog_and_dynamic_local_imports_are_root_bound(self):
        with tempfile.TemporaryDirectory() as directory:
            main, _ = load_copied_plugin(copy_plugin(directory))
            roots = [rule_package(directory, name) for name in ("first-code", "second-code")]
            for root, marker in zip(roots, ("first", "second")):
                bridge = root / "wechat-weflow-bridge-ob11"
                (bridge / "model_catalog.py").write_text("MARKER = " + repr(marker), encoding="utf-8")
                (bridge / "local_helper.py").write_text("MARKER = " + repr(marker + "-dynamic"), encoding="utf-8")
                (bridge / "model_client.py").write_text(
                    "from model_catalog import MARKER\n"
                    "def profile_settings(role, settings): return {'origin': MARKER, 'selected': settings['selected']}\n"
                    "def reasoning_options(profile): return {}\n"
                    "def trim_messages(*args):\n"
                    "    import local_helper\n"
                    "    return local_helper.MARKER\n", encoding="utf-8")
            decoy = types.ModuleType("model_client")
            decoy.profile_settings = Mock(side_effect=AssertionError("unowned global module used"))
            path_before = list(sys.path)
            with patch.dict(sys.modules, {"model_client": decoy, "model_catalog": decoy, "local_helper": decoy}):
                for root, marker in zip(roots, ("first", "second")):
                    profile = main.main_profile({"selected": "synthetic"}, root)
                    self.assertEqual(profile["origin"], marker)
                    bridge_modules = main.main_profile.__globals__["bridge_modules"]
                    self.assertEqual(bridge_modules(root)[2](), marker + "-dynamic")
                self.assertIs(sys.modules["model_client"], decoy)
                self.assertIs(sys.modules["model_catalog"], decoy)
                self.assertEqual(sys.path, path_before)
            decoy.profile_settings.assert_not_called()

    async def test_owned_actual_policy_preserves_native_identity_rule(self):
        with tempfile.TemporaryDirectory() as directory:
            main, _ = load_copied_plugin(copy_plugin(directory))
            profile = main.main_profile({"bot_persona": "小助手只认已核验身份。",
                                         "bot_owner_wechat_ids": ["wxid_synthetic_owner"]}, ROOT)
            self.assertIn("wxid_synthetic_owner", profile["prompt"])
            self.assertIn("别名", profile["prompt"])
            with self.assertRaises(RuntimeError):
                main.main_profile({"bot_persona": "小助手", "bot_owner_wechat_ids": ["g@chatroom"]}, ROOT)


if __name__ == "__main__":
    unittest.main()
