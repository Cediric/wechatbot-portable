"""Persona settings must reach the sole chat provider without name-based identity."""
import copy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "wechat-weflow-bridge-ob11"))
from config_model import get_field, normalize_config
from model_client import ModelError, profile_settings


class PersonaBindingTests(unittest.TestCase):
    def test_decision_profiles_use_same_native_owner_rules_without_chat_instructions(self):
        settings = {"bot_persona": "合成角色卡。", "bot_owner_wechat_ids": ["wxid_synthetic_owner"],
                    "main_llm_prompt": "仅属于主聊天的表达规则。"}
        for role in ("group", "autopilot", "moments_decision", "image_prompt"):
            with self.subTest(role=role):
                result = profile_settings(role, settings)
                self.assertIn("wxid_synthetic_owner", result["prompt"])
                self.assertIn("别名只属于角色设定", result["prompt"])
                self.assertNotIn(settings["main_llm_prompt"], result["prompt"])
        self.assertNotIn(settings["bot_persona"], profile_settings("memory", settings)["prompt"])

    def test_persona_and_chat_instructions_both_reach_main_profile(self):
        settings = {"bot_persona": "这是合成测试角色卡。", "main_llm_prompt": "回答当前问题。",
                    "bot_owner_wechat_ids": ["wxid_synthetic_owner"]}
        original = copy.deepcopy(settings)
        result = profile_settings("main", settings)
        self.assertIn(settings["bot_persona"], result["prompt"])
        self.assertIn(settings["main_llm_prompt"], result["prompt"])
        self.assertIn("wxid_synthetic_owner", result["prompt"])
        self.assertIn("别名只属于角色设定", result["prompt"])
        self.assertEqual(settings, original)

    def test_unbound_owner_does_not_recognize_by_name(self):
        profile = profile_settings("main", {"bot_persona": "合成角色主人的昵称为演示用户。"})
        self.assertIn("不按昵称、备注或自称认定", profile["prompt"])

    def test_owner_rejects_nicknames_groups_and_controls(self):
        for invalid in ("中文昵称", "100@chatroom", "unknown", "wxid_test\nRULE"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                normalize_config({"bot_owner_wechat_ids": [invalid]})
            with self.subTest(invalid=invalid), self.assertRaises(ModelError):
                profile_settings("main", {"bot_persona": "test", "bot_owner_wechat_ids": [invalid]})
        self.assertEqual(normalize_config({"bot_owner_wechat_ids": "wxid_alpha, account.beta"})["bot_owner_wechat_ids"], ["wxid_alpha", "account.beta"])

    def test_main_context_cap_is_enforced_in_schema_and_profile(self):
        self.assertEqual(get_field("main_llm_context_tokens").max, 500000)
        self.assertEqual(profile_settings("main", {"main_llm_context_tokens": 500000})["context_tokens"], 500000)
        with self.assertRaises(ValueError):
            normalize_config({"main_llm_context_tokens": 500001})
        with self.assertRaises(ModelError):
            profile_settings("main", {"main_llm_context_tokens": 500001})

    def test_streaming_and_summary_settings_are_in_profile_snapshot(self):
        profile = profile_settings("main", {"main_streaming_enabled": False, "main_stream_max_chars": 200,
            "main_context_compression_threshold": .75, "main_context_compression_prompt": "保留未完事项。"})
        self.assertEqual(profile["streaming"], {"enabled": False, "max_chars": 200})
        self.assertEqual(profile["compression"]["threshold"], .75)
        self.assertEqual(profile["compression"]["prompt"], "保留未完事项。")


if __name__ == "__main__":
    unittest.main()
