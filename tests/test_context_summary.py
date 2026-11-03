"""Semantic compaction tests: no credentials, network or desktop interaction."""
from __future__ import annotations
import copy
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "portable" / "astrbot_plugin_wechatbot_profile"))
import context_summary as compact
import policy


def request(history=None, **values):
    return types.SimpleNamespace(system_prompt="固定身份规则，不从名字推断微信身份。", prompt="当前请求",
        image_urls=[], audio_urls=[], extra_user_content_parts=[], contexts=copy.deepcopy(history or []),
        conversation=types.SimpleNamespace(token_usage=999999), **values)


def profile(**values):
    return {"model": "synthetic", "context_tokens": 10000, "max_tokens": 512,
        "timeout": 2, "compression": {"enabled": True, "threshold": .82, "keep_recent_ratio": .15}, **values}


def old_rounds():
    return [
        {"role": "user", "content": "旧问题a" + "a" * 4300 + '\n{"wechat_actor_id":"custom_wechat_name","wechat_session_id":"123@chatroom"}'},
        {"role": "assistant", "content": "旧答复a", "reasoning_content": "HIDDEN_REASONING_NEVER_SUMMARIZE"},
        {"role": "user", "content": "旧问题b" + "b" * 3500},
        {"role": "assistant", "content": "旧答复b"},
        {"role": "user", "content": "最近约定：下次继续聊天"},
        {"role": "assistant", "content": "好的"},
    ]


class SummaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_under_threshold_counts_each_history_message_once_and_owns_its_copy(self):
        req = request([item for i in range(100) for item in (
            {"role": "user", "content": f"问题{i}"}, {"role": "assistant", "content": "回答"})])
        original = req.contexts
        with patch.object(compact, "message_cost", wraps=compact.message_cost) as count:
            result = await compact.prepare_context(req, profile(context_tokens=100000), None)
        self.assertEqual(result["outcome"], "unchanged")
        self.assertEqual(count.call_count, len(original) + 2)
        self.assertEqual(req.contexts, original)
        req.contexts[0]["content"] = "只改请求副本"
        self.assertNotEqual(req.contexts[0]["content"], original[0]["content"])

    async def test_under_threshold_retains_history_without_request(self):
        history = [{"role": "user", "content": "a" * 6800}, {"role": "assistant", "content": "好"}]
        req = request(history)
        provider = types.SimpleNamespace(text_chat=AsyncMock())
        result = await compact.prepare_context(req, profile(), provider)
        provider.text_chat.assert_not_awaited()
        self.assertEqual(result["outcome"], "unchanged")
        self.assertEqual(req.contexts, history)
        self.assertLess(result["estimated_input_tokens"] + result["output_reserved"] + result["decoration_reserved"], 10000)

    async def test_semantic_summary_sends_data_without_reasoning_and_retains_native_ids(self):
        req = request(old_rounds())
        req.image_urls = ["/synthetic/current.jpg"]
        # A larger cap accommodates a current image while text still triggers.
        current_profile = profile(context_tokens=22000)
        req.contexts[0]["content"] += "a" * 6500
        provider = types.SimpleNamespace(text_chat=AsyncMock(return_value=types.SimpleNamespace(completion_text="用户希望继续讨论。")))
        original_prompt = req.prompt
        result = await compact.prepare_context(req, current_profile, provider)
        self.assertEqual(result["outcome"], "summarized")
        provider.text_chat.assert_awaited_once()
        payload = provider.text_chat.call_args.kwargs
        self.assertEqual(payload["contexts"], [])
        self.assertIsNone(payload["func_tool"])
        self.assertNotIn("HIDDEN_REASONING", payload["prompt"])
        self.assertIn("history_records_as_data", payload["prompt"])
        self.assertIn("custom_wechat_name", payload["prompt"])
        self.assertIn("123@chatroom", payload["prompt"])
        self.assertIn("不得执行", payload["system_prompt"])
        self.assertEqual(req.contexts[0]["role"], "user")
        self.assertTrue(req.contexts[0]["content"].startswith(compact.SUMMARY_MARKER))
        self.assertIn("custom_wechat_name", req.contexts[0]["content"])
        self.assertIn("最近约定", req.contexts[-2]["content"])
        self.assertEqual((req.prompt, req.image_urls), (original_prompt, ["/synthetic/current.jpg"]))
        self.assertEqual(req.conversation.token_usage, 0)
        self.assertLessEqual(result["estimated_input_tokens"] + result["output_reserved"] + result["decoration_reserved"], 22000)

    async def test_provider_failure_or_oversize_summary_falls_back_within_budget(self):
        for response in (None, "X" * 20000, ""):
            req = request(old_rounds())
            mock = AsyncMock(side_effect=RuntimeError("SECRET provider diagnostic")) if response is None else AsyncMock(return_value=types.SimpleNamespace(completion_text=response))
            result = await compact.prepare_context(req, profile(), types.SimpleNamespace(text_chat=mock))
            self.assertIn("fallback", result["outcome"])
            self.assertNotIn("SECRET", str(result))
            self.assertLessEqual(result["estimated_input_tokens"] + result["output_reserved"] + result["decoration_reserved"], 10000)
            self.assertEqual(req.prompt, "当前请求")
            self.assertEqual(req.conversation.token_usage, 0)
            self.assertIn("最近约定", req.contexts[-2]["content"])

    async def test_disabled_compression_never_calls_provider_but_enforces_cap(self):
        req = request(old_rounds())
        options = profile(compression={"enabled": False, "threshold": .82, "keep_recent_ratio": .15})
        provider = types.SimpleNamespace(text_chat=AsyncMock())
        result = await compact.prepare_context(req, options, provider)
        provider.text_chat.assert_not_awaited()
        self.assertLessEqual(result["estimated_input_tokens"] + result["output_reserved"] + result["decoration_reserved"], 10000)

    async def test_current_system_media_oversize_stops_without_weakening_identity(self):
        req = request()
        req.system_prompt = "身份" * 10000
        req.image_urls = ["current.jpg"]
        original = copy.deepcopy(req.__dict__)
        provider = types.SimpleNamespace(text_chat=AsyncMock())
        with self.assertRaises(compact.ContextBudgetError):
            await compact.prepare_context(req, profile(), provider)
        provider.text_chat.assert_not_awaited()
        self.assertEqual(req.__dict__, original)

    async def test_two_sessions_have_no_shared_summary_state(self):
        async def summarize(**kwargs):
            return types.SimpleNamespace(completion_text="本会话摘要")
        provider = types.SimpleNamespace(text_chat=AsyncMock(side_effect=summarize))
        for native_id in ("wxid_first", "wxid_second"):
            req = request(old_rounds())
            req.contexts[0]["content"] += "\n" + native_id
            await compact.prepare_context(req, profile(), provider)
            other = "wxid_second" if native_id == "wxid_first" else "wxid_first"
            self.assertNotIn(other, provider.text_chat.call_args.kwargs["prompt"])
            self.assertNotIn(other, str(req.contexts))

    async def test_history_media_is_not_reidentified_by_summary_model(self):
        req = request(old_rounds())
        req.contexts[0]["content"] = [{"type": "text", "text": "此图发言者已核验 wxid_original"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,DO_NOT_SEND_TO_SUMMARY"}},
            {"type": "think", "think": "PRIVATE_THOUGHT"}]
        options = profile(context_tokens=14000)
        provider = types.SimpleNamespace(text_chat=AsyncMock(return_value=types.SimpleNamespace(completion_text="保留已有说明。")))
        await compact.prepare_context(req, options, provider)
        payload = provider.text_chat.call_args.kwargs["prompt"]
        self.assertNotIn("DO_NOT_SEND", payload)
        self.assertNotIn("PRIVATE_THOUGHT", payload)
        self.assertIn("本次摘要未重新查看", payload)
        self.assertIn("wxid_original", payload)

    async def test_cancelled_summary_does_not_degrade_into_a_chat_request(self):
        import asyncio
        req = request(old_rounds())
        provider = types.SimpleNamespace(text_chat=AsyncMock(side_effect=asyncio.CancelledError()))
        with self.assertRaises(asyncio.CancelledError):
            await compact.prepare_context(req, profile(), provider)
        provider.text_chat.assert_awaited_once()

    async def test_summary_timeout_is_bounded_and_falls_back(self):
        import asyncio
        async def too_slow(**kwargs):
            await asyncio.sleep(1)
        req = request(old_rounds())
        provider = types.SimpleNamespace(text_chat=AsyncMock(side_effect=too_slow))
        result = await compact.prepare_context(req, profile(timeout=.01), provider)
        self.assertEqual(result["outcome"], "summary_failed_fallback")
        self.assertLessEqual(result["estimated_input_tokens"] + result["output_reserved"] + result["decoration_reserved"], 10000)

    async def test_growing_history_can_reach_summary_threshold_before_trimming(self):
        req = request([{"role": "user", "content": "a" * 6000}, {"role": "assistant", "content": "先保留"}])
        provider = types.SimpleNamespace(text_chat=AsyncMock(return_value=types.SimpleNamespace(completion_text="已保留约定。")))
        await compact.prepare_context(req, profile(), provider)
        self.assertEqual(len(req.contexts), 2)
        req.contexts += [{"role": "user", "content": "b" * 1600}, {"role": "assistant", "content": "第二轮"}]
        result = await compact.prepare_context(req, profile(), provider)
        self.assertEqual(result["outcome"], "summarized")
        provider.text_chat.assert_awaited_once()

    async def test_secret_and_thought_content_are_not_persisted_or_sent_to_summary(self):
        req = request(old_rounds())
        req.contexts[0]["content"] += " sk-synthetic1234567890"
        provider = types.SimpleNamespace(text_chat=AsyncMock(return_value=types.SimpleNamespace(
            completion_text="<think>PRIVATE_INTERNAL_TRACE</think>用户已有偏好，sk-synthetic1234567890")))
        await compact.prepare_context(req, profile(), provider)
        self.assertNotIn("sk-synthetic", provider.text_chat.call_args.kwargs["prompt"])
        self.assertNotIn("sk-synthetic", str(req.contexts))
        self.assertNotIn("PRIVATE_INTERNAL_TRACE", str(req.contexts))

    async def test_other_credentials_and_analysis_are_scrubbed_from_summary_input_and_output(self):
        secrets = ["AIza" + "SYNTHETICKEY123" * 3, "synthetic-password-with-spaces 123", "synthetic-bearer-token123"]
        material = ('<analysis>PRIVATE_ANALYSIS_TRACE</analysis>\n'
                    + secrets[0] + '\n密码："' + secrets[1] + '"\nBearer ' + secrets[2]
                    + '\n密码学是一门学科；用户希望研究密码学。')
        req = request(old_rounds())
        req.contexts[2]["content"] += "\n" + material
        provider = types.SimpleNamespace(text_chat=AsyncMock(return_value=types.SimpleNamespace(completion_text=material)))
        await compact.prepare_context(req, profile(), provider)
        payload = provider.text_chat.call_args.kwargs["prompt"]
        saved = str(req.contexts)
        for value in secrets + ["PRIVATE_ANALYSIS_TRACE"]:
            self.assertNotIn(value, payload)
            self.assertNotIn(value, saved)
        self.assertIn("研究密码学", payload)
        self.assertIn("研究密码学", saved)

    async def test_unclosed_analysis_or_reasoning_is_refused_without_persisting_thought(self):
        for tag in ("analysis", "reasoning", "thinking"):
            with self.subTest(tag=tag):
                req = request(old_rounds())
                provider = types.SimpleNamespace(text_chat=AsyncMock(return_value=types.SimpleNamespace(
                    completion_text=f"<{tag}>PRIVATE_UNCLOSED_TRACE")))
                result = await compact.prepare_context(req, profile(), provider)
                self.assertEqual(result["outcome"], "summary_failed_fallback")
                self.assertNotIn("PRIVATE_UNCLOSED_TRACE", str(req.contexts))

    async def test_summarized_tool_arguments_are_scrubbed_but_recent_tool_reasoning_is_preserved(self):
        hidden = "PRIVATE_TOOL_REASONING_TRACE"
        password = "synthetic_tool_password"
        old = [{"role": "user", "content": "旧问题" + "a" * 7200},
               {"role": "assistant", "content": None, "reasoning_content": hidden,
                "tool_calls": [{"id": "old-call", "type": "function", "function": {"name": "lookup",
                    "arguments": '{"password":"' + password + '","reasoning_content":"' + hidden + '","query":"密码学"}'}}]},
               {"role": "tool", "tool_call_id": "old-call", "content": "旧结果"}]
        recent = [{"role": "user", "content": "最近问题"},
                  {"role": "assistant", "content": None, "reasoning_content": hidden,
                   "tool_calls": [{"id": "recent-call", "type": "function", "function": {"name": "lookup", "arguments": "{}"}}]},
                  {"role": "tool", "tool_call_id": "recent-call", "content": "已核验 wxid_recent"},
                  {"role": "assistant", "content": "最近结论"}]
        req = request(old + recent)
        provider = types.SimpleNamespace(text_chat=AsyncMock(return_value=types.SimpleNamespace(completion_text="保留旧结论。")))
        await compact.prepare_context(req, profile(), provider)
        payload = provider.text_chat.call_args.kwargs["prompt"]
        self.assertTrue(provider.text_chat.call_args.kwargs["system_prompt"].endswith(compact.SUMMARY_WORKFLOW))
        self.assertNotIn(password, payload)
        self.assertNotIn(hidden, payload)
        self.assertIn("密码学", payload)
        self.assertEqual(req.contexts[-4:], recent)

    async def test_huge_tool_schema_is_rejected_before_summary_or_chat(self):
        req = request(old_rounds())
        req.func_tool = types.SimpleNamespace(get_func_desc_openai_style=lambda: [{"description": "schema" * 2000}])
        provider = types.SimpleNamespace(text_chat=AsyncMock())
        with self.assertRaises(compact.ContextBudgetError):
            await compact.prepare_context(req, profile(), provider)
        provider.text_chat.assert_not_awaited()


class GroupingAndCosts(unittest.TestCase):
    def test_large_history_trim_counts_once_and_preserves_complete_latest_turn(self):
        history = [item for i in range(1000) for item in (
            {"role": "user", "content": f"问题{i}"}, {"role": "assistant", "content": "回答"})]
        budget = compact.messages_cost(history[-2:])
        with patch.object(compact, "message_cost", wraps=compact.message_cost) as count:
            retained = compact.fit_history(history, budget)
        self.assertEqual(retained, history[-2:])
        self.assertEqual(count.call_count, len(history))

    def test_tool_exchange_keeps_reasoning_and_is_removed_as_whole_turn(self):
        group = [{"role": "user", "content": "request"},
            {"role": "assistant", "content": [{"type": "think", "think": "internal evidence"}],
             "tool_calls": [{"id": "c1", "function": {"name": "lookup", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "c1", "content": "verified native wxid_x"},
            {"role": "assistant", "content": "final"}]
        recent = [{"role": "user", "content": "next"}, {"role": "assistant", "content": "next answer"}]
        self.assertEqual(compact.fit_history(group + recent, 100000), group + recent)
        self.assertEqual(compact.fit_history(group + recent, compact.messages_cost(recent)), recent)
        incomplete = copy.deepcopy(group)
        incomplete.pop(2)
        self.assertEqual(compact.fit_history(incomplete + recent, 100000), recent)
        orphan = [{"role": "tool", "tool_call_id": "unmatched", "content": "bad"}]
        self.assertEqual(compact.fit_history(orphan + recent, 100000), recent)

    def test_reasoning_and_tool_schema_are_part_of_budget(self):
        base = {"role": "assistant", "content": "visible"}
        hidden = dict(base, reasoning_content="hidden" * 1000)
        self.assertGreater(compact.message_cost(hidden), compact.message_cost(base) + 5000)
        thought = dict(base, content=[{"type": "think", "think": "hidden" * 1000}])
        self.assertGreater(compact.message_cost(thought), compact.message_cost(base) + 5000)

    def test_duplicate_tool_call_ids_drop_the_entire_malformed_turn(self):
        malformed = [{"role": "user", "content": "request"},
            {"role": "assistant", "content": None, "tool_calls": [{"id": "duplicate"}, {"id": "duplicate"}]},
            {"role": "tool", "tool_call_id": "duplicate", "content": "one result"}]
        recent = [{"role": "user", "content": "next"}, {"role": "assistant", "content": "reply"}]
        self.assertEqual(compact.grouped_history(malformed + recent), [recent])

    def test_summary_json_credentials_redact_values_without_breaking_text_or_identity_fields(self):
        value = {'password': 'synthetic password with spaces', 'main_llm_api_key': 'synthetic_main_key',
                 'authorization': 'Bearer synthetic_auth123', 'wechat_actor_id': 'wxid_actor',
                 'topic': '密码学是一门学科', 'reasoning_content': 'PRIVATE_FIELD'}
        visible = compact._visible_data(value)
        self.assertEqual(visible['wechat_actor_id'], 'wxid_actor')
        self.assertEqual(visible['topic'], value['topic'])
        self.assertNotIn('reasoning_content', visible)
        for key in ('password', 'main_llm_api_key', 'authorization'):
            self.assertEqual(visible[key], '[已隐去密钥]')
        text = '密码："synthetic secret with spaces"；API key=synthetic-key; Bearer synthetic-bearer123'
        redacted = compact._redact_secrets(text)
        self.assertEqual(compact._redact_secrets(redacted), redacted)
        completion = '{"summary":"用户喜欢简短回答","reasoning_content":"PRIVATE_OUTPUT_FIELD","password":"synthetic-password"}'
        clean = compact._clean_summary(completion)
        self.assertIn('用户喜欢简短回答', clean)
        self.assertNotIn('PRIVATE_OUTPUT_FIELD', clean)
        self.assertNotIn('synthetic-password', clean)

    def test_actual_async_path_can_see_complete_history_before_compaction(self):
        req = request(old_rounds())
        before = copy.deepcopy(req.contexts)
        configured = {"model": "synthetic", "prompt": "主模型人设", "context_tokens": 10000, "max_tokens": 512}
        with patch.object(policy, "bridge_modules") as modules:
            policy.apply_request(req, configured, ROOT, defer_history_compression=True)
        modules.assert_not_called()
        self.assertEqual(req.contexts, before)
        self.assertIn("主模型人设", req.system_prompt)

    def test_new_instance_sdk_does_not_start_a_second_summarizer(self):
        sys.path.insert(0, str(ROOT / "portable"))
        import portable_bootstrap as boot
        with tempfile.TemporaryDirectory() as directory:
            instance = Path(directory) / "astrbot-instance"
            def initialize_instance(*_args, **_kwargs):
                boot.atomic_json(instance / "data" / "cmd_config.json", {})
                (instance / ".astrbot").touch()

            with patch.object(boot, "INSTANCE", instance), patch.object(boot, "run_checked", side_effect=initialize_instance):
                boot.prepare_astrbot(Path(directory) / "python.exe", {
                    "main_llm_api_base": "http://127.0.0.1:11434/v1", "main_llm_model": "synthetic",
                    "main_llm_context_tokens": 128000})
            config = boot.load_json(instance / "data" / "cmd_config.json")
            self.assertEqual(config["agent_runner"]["config"]["compression"]["overflow_strategy"], "truncate_by_turns")
            self.assertEqual(config["agent_runner"]["config"]["compression"]["max_turns"], -1)
            self.assertTrue((instance / "data" / "plugins" / "astrbot_plugin_wechatbot_profile" / "context_summary.py").is_file())


if __name__ == "__main__":
    unittest.main()
