"""Check sampling on the bridge request and AstrBot provider used for chat."""
import importlib.util
import json
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "wechat-weflow-bridge-ob11"
sys.path.insert(0, str(SOURCE))
import config_model
import model_client

spec = importlib.util.spec_from_file_location("sampling_policy", ROOT / "portable/astrbot_plugin_wechatbot_profile/policy.py")
policy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(policy)


class SamplingOptionsTests(unittest.TestCase):
    def test_sampling_in_actual_post_and_astrbot_extra_body(self):
        cases = [
            ("deepseek-flash", "auto", -1, {}),
            ("deepseek-flash", "auto", 0, {"top_p": .95}),
            ("deepseek-flash", "on", .98, {"top_p": .98}),
            ("deepseek-flash", "on", 1, {"top_p": 1}),
            ("deepseek-flash", "off", .75, {"temperature": .7}),
            ("deepseek-v4-pro", "auto", .75, {}),
            ("deepseek-v4-pro", "on", .75, {}),
            ("deepseek-v4-pro", "off", .75, {"temperature": .7, "top_p": .75}),
            ("gpt-5.1", "off", .99, {}),
            ("o3", "auto", .99, {}),
            ("gpt-4o", "auto", -1, {"temperature": .7}),
            ("gpt-4o", "auto", .4, {"temperature": .7, "top_p": .4}),
            ("qwen3-max", "on", .9, {"temperature": .7, "top_p": .9}),
        ]
        session = Mock()
        session.post.return_value.status_code = 200
        session.post.return_value.iter_content.return_value = [
            json.dumps({"choices": [{"message": {"content": "ok"}}]}).encode()]
        for model, thinking, top_p, want in cases:
            with self.subTest(model=model, thinking=thinking, top_p=top_p):
                settings = {"main_llm_model": model, "main_llm_thinking": thinking, "main_llm_top_p": top_p,
                            "main_llm_temperature": .7, "main_llm_api_base": "https://example.invalid/v1",
                            "main_llm_api_key": "synthetic"}
                cfg = types.SimpleNamespace(**{k.upper(): v for k, v in settings.items()})
                with patch.dict(sys.modules, {"config": cfg, "http_util": types.SimpleNamespace(session=lambda: session)}):
                    model_client.request("main", [{"role": "user", "content": "示例"}])
                direct = session.post.call_args.kwargs["json"]
                plugin = policy.provider_config(settings, package_root=ROOT)["custom_extra_body"]
                for body in (direct, plugin):
                    self.assertEqual({k: body[k] for k in ("temperature", "top_p") if k in body}, want)

    def test_auxiliary_profiles_keep_existing_sampling(self):
        values = model_client.profile_settings("memory", {"main_llm_top_p": .96,
            "memory_llm_model": "deepseek-flash", "memory_llm_temperature": .35})
        self.assertNotIn("top_p", values)
        self.assertEqual(model_client.sampling_options(values, {}), {"temperature": .35})
        values["thinking"] = "on"
        self.assertEqual(model_client.sampling_options(values, model_client.reasoning_options(values)), {})

    def test_top_p_sentinel_zero_and_validation(self):
        self.assertEqual(model_client.profile_settings("main", {})["top_p"], -1)
        self.assertEqual(config_model.get_field("main_llm_top_p").default, -1)
        for n in (-1, 0, .95, 1):
            self.assertEqual(config_model.normalize_config({"main_llm_top_p": n})["main_llm_top_p"], n)
        for n in (-2, -.5, 1.2, float("nan")):
            with self.assertRaises(ValueError):
                config_model.normalize_config({"main_llm_top_p": n})
            with self.assertRaises(model_client.ModelError):
                model_client.profile_settings("main", {"main_llm_top_p": n})


if __name__ == "__main__":
    unittest.main()
