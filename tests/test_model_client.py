"""Independent review of the shared client: mocked HTTP only, no credentials."""
import importlib.util
import json
from pathlib import Path
import sys
import threading
import types
import unittest
from unittest.mock import Mock, patch

SOURCE=Path(__file__).resolve().parents[1]/"wechat-weflow-bridge-ob11"


def load(name, file):
    spec=importlib.util.spec_from_file_location(name,SOURCE/file)
    module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


class ModelClientCrossReview(unittest.TestCase):
    def setUp(self):
        catalog=load("review_catalog","model_catalog.py")
        self.config=types.SimpleNamespace()
        for profile,(prefix,*rest) in catalog.PROFILES.items():
            for suffix,value in [("api_base","https://example.invalid/v1"),("api_key","synthetic-"+profile),("model",profile+"-model"),("context_tokens",8192),("max_tokens",512),("thinking","auto")]:
                setattr(self.config,(prefix+"_"+suffix).upper(),value)
        self.session=Mock(); http=types.SimpleNamespace(session=lambda:self.session)
        modules=patch.dict(sys.modules,{"config":self.config,"http_util":http,"model_catalog":catalog})
        modules.start();self.addCleanup(modules.stop)
        self.client=load("review_client","model_client.py")

    def response(self, content="最终正文", **extra):
        response=Mock(status_code=200)
        message={"content":content, **extra}
        response.iter_content.return_value=[json.dumps({"choices":[{"message":message}]},ensure_ascii=False).encode()]
        self.session.post.return_value=response
        return response

    def document_response(self, document):
        response=Mock(status_code=200)
        response.iter_content.return_value=[json.dumps(document,ensure_ascii=False).encode()]
        self.session.post.return_value=response
        return response

    def test_role_api_settings_are_independent_and_no_shared_history(self):
        self.response()
        for profile in ["main","memory","group","autopilot","image_prompt","moments_decision"]:
            messages=[{"role":"system","content":"固定限制"},{"role":"user","content":"只看本条 "+profile}]
            self.assertEqual(self.client.request(profile,messages),"最终正文")
            call=self.session.post.call_args
            self.assertEqual(call.kwargs["headers"]["Authorization"],"Bearer synthetic-"+profile)
            self.assertEqual(call.kwargs["json"]["model"],profile+"-model")
            sent = call.kwargs["json"]["messages"]
            self.assertEqual(sent[1:], messages[1:])
            self.assertTrue(sent[0]["content"].startswith(messages[0]["content"]))
            self.assertIn("[内置工作流约定]", sent[0]["content"])
            self.assertEqual(messages[0]["content"], "固定限制")
        self.assertEqual(self.session.post.call_count,6)

    def test_configured_prompt_appended_once_and_input_is_not_mutated(self):
        self.config.MAIN_LLM_PROMPT="配置中的独立提示词"
        messages=[{"role":"system","content":"固定限制"},{"role":"user","content":"当前输入"}]
        original=json.loads(json.dumps(messages))
        self.response();self.client.request("main",messages)
        sent=self.session.post.call_args.kwargs["json"]["messages"]
        self.assertIn(self.config.MAIN_LLM_PROMPT,sent[0]["content"])
        self.assertEqual(messages,original)
        self.response();self.client.request("main",sent)
        self.assertEqual(self.session.post.call_args.kwargs["json"]["messages"][0]["content"].count(self.config.MAIN_LLM_PROMPT),1)

    def test_shared_private_decision_has_distinct_prompt_but_same_credentials(self):
        self.config.MOMENTS_INTERACTION_PROMPT="朋友圈候选专用格式"
        self.response()
        messages=[{"role":"system","content":"只输出私聊方式判断"},{"role":"user","content":"合成消息"}]
        self.client.request("moments_decision",messages,prompt_override="角色卡与私聊专用要求")
        call=self.session.post.call_args.kwargs
        self.assertEqual(call["headers"]["Authorization"],"Bearer synthetic-moments_decision")
        self.assertNotIn(self.config.MOMENTS_INTERACTION_PROMPT,call["json"]["messages"][0]["content"])
        self.assertIn("私聊专用要求",call["json"]["messages"][0]["content"])

    def test_thinking_modes_keep_provider_specific_options_and_reject_incompatible_settings(self):
        reasoning=self.client.reasoning_options
        self.assertEqual(reasoning({"thinking":"off","thinking_format":"qwen"}),{"enable_thinking":False})
        self.assertEqual(reasoning({"thinking":"on","thinking_format":"qwen","thinking_budget":2048}),{"enable_thinking":True,"thinking_budget":2048})
        self.assertEqual(reasoning({"thinking":"off","thinking_format":"deepseek"}),{"thinking":{"type":"disabled"}})
        self.assertEqual(reasoning({"thinking":"on","thinking_format":"deepseek","reasoning_effort":"high"}),{"thinking":{"type":"enabled"},"reasoning_effort":"high"})
        self.assertEqual(reasoning({"thinking":"on","thinking_format":"openai","reasoning_effort":"low"}),{"reasoning_effort":"low"})
        for values in [{"thinking":"on","thinking_format":"none"},{"thinking_format":"qwen","reasoning_effort":"high"},{"thinking_format":"openai","thinking_budget":1000},{"thinking_budget":float("nan")},{"thinking_budget":True}]:
            with self.subTest(values=values),self.assertRaises(self.client.ModelError):reasoning(values)

    def test_budget_removes_old_turn_with_assistant_and_tool_without_dropping_current_media(self):
        image={"type":"image_url","image_url":{"url":"data:image/jpeg;base64,synthetic"}}
        messages=[{"role":"system","content":"只读规则"},{"role":"user","content":"旧历史"*1800},
                  {"role":"assistant","content":"过期回答"},{"role":"tool","content":"过期工具数据"},
                  {"role":"user","content":[{"type":"text","text":"当前画面"},image]}]
        trimmed=self.client.trim_messages(messages,4096,512)
        self.assertEqual([m["role"] for m in trimmed],["system","user"])
        self.assertEqual(trimmed[-1]["content"][-1],image)
        self.assertEqual(len(messages),5)

    def test_media_and_system_oversize_fail_without_silently_erasing_picture(self):
        image={"type":"image_url","image_url":{"url":"data:image/jpeg;base64,synthetic"}}
        for messages in [[{"role":"system","content":"规则"},{"role":"user","content":[image,image]}],
                         [{"role":"system","content":"不能删的规则"*2000},{"role":"user","content":"当前问题"}]]:
            with self.assertRaises(self.client.ModelError):self.client.trim_messages(messages,2048,1024)

    def test_long_history_is_budgeted_once_and_keeps_tools_with_their_turn(self):
        messages=[{"role":"system","content":"保留角色规则"}]
        for index in range(200):
            messages.extend([{"role":"user","content":"合成旧消息"*50},
                             {"role":"assistant","content":"", "tool_calls":[{"id":str(index),"function":{"name":"synthetic","arguments":"{}"}}]},
                             {"role":"tool","content":"合成工具结果", "tool_call_id":str(index)}])
        current={"role":"user","content":"当前问题"}
        messages.append(current)
        with patch.object(self.client,"_message_cost",wraps=self.client._message_cost) as cost:
            trimmed=self.client.trim_messages(messages,4096,512)
        self.assertEqual(cost.call_count,len(messages))
        self.assertEqual(trimmed[0],messages[0])
        self.assertEqual(trimmed[-1],current)
        self.assertEqual(trimmed[1]["role"],"user")
        self.assertLessEqual(sum(self.client._message_cost(item) for item in trimmed),4096-512-128)
        self.assertEqual(len(messages),602)

    def test_token_estimation_preserves_ascii_unicode_and_media_costs(self):
        for text in ["", "a"*17, "中文 mixed 文本", "🙂\\u200d界\\ud800"]:
            ascii_count=sum(ord(char)<128 for char in text)
            self.assertEqual(self.client.estimate_tokens(text),len(text)-ascii_count+(ascii_count+2)//3+8)
        self.assertEqual(self.client.estimate_tokens([{"type":"text","text":"当前图片"},{"type":"image_url","image_url":{"url":"synthetic"}}]),
                         self.client.estimate_tokens("当前图片")+1536)

    def test_response_only_returns_final_content_and_never_reasoning_as_fallback(self):
        response=self.response("最终正文",reasoning_content="绝对不能发送的内部推理")
        self.assertEqual(self.client.request("main",[{"role":"user","content":"问题"}]),"最终正文")
        response.close.assert_called_once()
        response=self.response("",reasoning_content="绝对不能发送的内部推理")
        with self.assertRaises(self.client.ModelError):self.client.request("main",[{"role":"user","content":"问题"}])
        response.close.assert_called_once()

    def test_text_parts_return_only_text_and_never_reasoning_or_refusal(self):
        response=self.response([
            {"type":"reasoning","text":"SYNTHETIC_REASONING_NEVER_RETURN"},
            {"type":"text","text":'{"text":"合成'},
            {"type":"refusal","text":"SYNTHETIC_REFUSAL_NEVER_RETURN"},
            {"type":"text","text":"回复\"}"},
        ],reasoning_content="SYNTHETIC_MESSAGE_REASONING_NEVER_RETURN")
        self.assertEqual(self.client.request("main",[{"role":"user","content":"问题"}],json_mode=True),
                         '{"text":"合成回复"}')
        self.session.post.assert_called_once();response.close.assert_called_once()

    def test_empty_final_answers_distinguish_reasoning_refusal_and_plain_empty(self):
        for content,extra,description in [
            (None,{"reasoning_content":"SYNTHETIC_REASONING_NEVER_LOG"},"仅返回推理"),
            ("",{"refusal":"SYNTHETIC_REFUSAL_NEVER_LOG"},"返回拒绝信息"),
            (" \n\t ",{},"返回空正文"),
            ([{"type":"reasoning","text":"SYNTHETIC_PART_NEVER_LOG"}],{},"返回空正文"),
        ]:
            with self.subTest(content_type=type(content).__name__,description=description):
                self.session.post.reset_mock();response=self.response(content,**extra)
                with self.assertRaisesRegex(self.client.ModelError,description) as failure:
                    self.client.request("main",[{"role":"user","content":"问题"}])
                self.assertNotIn("SYNTHETIC_",str(failure.exception))
                self.session.post.assert_called_once();response.close.assert_called_once()

    def test_length_finish_reports_safe_counts_without_parameter_changes_or_retry(self):
        self.config.MAIN_LLM_MODEL="deepseek-flash"
        self.config.MAIN_LLM_THINKING="on"
        self.config.MAIN_LLM_THINKING_FORMAT="deepseek"
        self.config.MAIN_LLM_REASONING_EFFORT="low"
        self.config.MAIN_LLM_MAX_TOKENS=4096
        response=self.document_response({"choices":[{"finish_reason":"length","message":{
            "content":None,"reasoning_content":"SYNTHETIC_REASONING_NEVER_LOG"}}],
            "usage":{"completion_tokens":4096,"completion_tokens_details":{"reasoning_tokens":4096}}})
        with self.assertRaisesRegex(self.client.ModelError,"达到长度上限") as failure:
            self.client.request("main",[{"role":"user","content":"只输出JSON对象"}],json_mode=True)
        message=str(failure.exception)
        for field in ("finish=length","content=null","reasoning=present","completion_tokens=4096","reasoning_tokens=4096"):
            self.assertIn(field,message)
        self.assertNotIn("SYNTHETIC_",message)
        payload=self.session.post.call_args.kwargs["json"]
        self.assertEqual(payload["thinking"],{"type":"enabled"})
        self.assertEqual(payload["reasoning_effort"],"low")
        self.assertEqual(payload["max_tokens"],4096)
        self.assertEqual(payload["response_format"],{"type":"json_object"})
        self.session.post.assert_called_once();response.close.assert_called_once()

    def test_length_finish_never_returns_nonempty_or_partial_json_prefix(self):
        for content in ('{"text":"合成回复"}','{"text":"合成'):
            with self.subTest(complete_json=content.endswith("}")):
                response=self.document_response({"choices":[{"finish_reason":"length","message":{"content":content}}]})
                with self.assertRaisesRegex(self.client.ModelError,"正文可能被截断"):
                    self.client.request("main",[{"role":"user","content":"问题"}],json_mode=True)
                response.close.assert_called_once()

    def test_malformed_response_shapes_are_distinct_and_never_echo_payload(self):
        for document,description in [
            (["SYNTHETIC_PRIVATE_PAYLOAD"],"根节点不是对象"),
            ({"choices":{"private":"SYNTHETIC_PRIVATE_PAYLOAD"}},"choices缺失或不是列表"),
            ({"choices":[]},"choices为空"),
            ({"choices":[{"message":"SYNTHETIC_PRIVATE_PAYLOAD"}]},"缺少message对象"),
            ({"choices":[{"message":{"content":{"secret":"SYNTHETIC_PRIVATE_PAYLOAD"}}}]},"正文格式不受支持"),
            ({"choices":[{"message":{"content":[{"type":"text","text":{"secret":"SYNTHETIC_PRIVATE_PAYLOAD"}}]}}]},"文本分段格式无效"),
        ]:
            with self.subTest(description=description):
                response=self.document_response(document)
                with self.assertRaisesRegex(self.client.ModelError,description) as failure:
                    self.client.request("main",[{"role":"user","content":"问题"}])
                self.assertNotIn("SYNTHETIC_PRIVATE_PAYLOAD",str(failure.exception))
                response.close.assert_called_once()

    def test_response_diagnostics_allow_only_known_finish_and_integer_token_counts(self):
        for finish in ("https://secret.invalid/sk-SYNTHETIC",{"secret":"SYNTHETIC"},["SYNTHETIC"]):
            with self.subTest(finish_type=type(finish).__name__):
                response=self.document_response({"choices":[{"finish_reason":finish,"message":{"content":""}}],
                    "usage":{"completion_tokens":"sk-SYNTHETIC","completion_tokens_details":{"reasoning_tokens":True}}})
                with self.assertRaises(self.client.ModelError) as failure:
                    self.client.request("main",[{"role":"user","content":"问题"}])
                message=str(failure.exception)
                self.assertIn("finish=unknown",message)
                self.assertNotIn("SYNTHETIC",message)
                self.assertNotIn("http",message)
                self.assertNotIn("completion_tokens=",message)
                self.assertNotIn("reasoning_tokens=",message)
                response.close.assert_called_once()

    def test_cancel_before_request_and_during_read_no_retry_and_always_close(self):
        cancel=threading.Event();cancel.set()
        with self.assertRaises(self.client.ModelError):self.client.request("main",[{"role":"user","content":"问题"}],cancelled=cancel)
        self.session.post.assert_not_called()
        cancel.clear();response=self.response()
        def chunks(**kwargs):
            yield b'{"choices":'
            cancel.set()
            yield b'[]}'
        response.iter_content.side_effect=chunks
        with self.assertRaises(self.client.ModelError):self.client.request("main",[{"role":"user","content":"问题"}],cancelled=cancel)
        self.session.post.assert_called_once();response.close.assert_called_once()

    def test_cancel_during_preparation_prevents_post_and_during_headers_closes_response(self):
        cancel=threading.Event()
        trim=self.client.trim_messages
        def cancel_after_prepare(*args):
            result=trim(*args);cancel.set();return result
        with patch.object(self.client,"trim_messages",side_effect=cancel_after_prepare),self.assertRaisesRegex(self.client.ModelError,"已取消"):
            self.client.request("main",[{"role":"user","content":"问题"}],cancelled=cancel)
        self.session.post.assert_not_called()
        cancel.clear();response=self.response()
        def cancel_after_headers(*args,**kwargs):
            cancel.set();return response
        self.session.post.side_effect=cancel_after_headers
        with self.assertRaisesRegex(self.client.ModelError,"已取消"):
            self.client.request("main",[{"role":"user","content":"问题"}],cancelled=cancel)
        self.session.post.assert_called_once();response.close.assert_called_once()
        response.iter_content.assert_not_called()

    def test_elapsed_deadline_at_headers_or_end_of_body_is_timeout_without_retry(self):
        for clock_values in [(0,0.2),(0,0.01,0.2)]:
            self.session.post.reset_mock();response=self.response()
            response.iter_content.return_value=[]
            with patch.object(self.client.time,"monotonic",side_effect=clock_values),self.assertRaisesRegex(self.client.ModelError,"读取超时"):
                self.client.request("main",[{"role":"user","content":"问题"}],timeout=0.1)
            self.session.post.assert_called_once();response.close.assert_called_once()
            self.assertEqual(self.session.post.call_args.kwargs["timeout"],(0.1,0.1))

    def test_bad_status_invalid_json_and_oversize_response_are_not_retried(self):
        for status,chunks in [(401,[b"private provider diagnostic"]),(200,[b"not JSON"]),(200,[b"x"*(2*1024*1024+1)])]:
            self.session.post.reset_mock();response=Mock(status_code=status);response.iter_content.return_value=chunks
            self.session.post.return_value=response
            with self.assertRaises(self.client.ModelError) as error:self.client.request("main",[{"role":"user","content":"问题"}])
            self.assertNotIn("private provider diagnostic",str(error.exception))
            self.session.post.assert_called_once();response.close.assert_called_once()

    def test_reasoning_model_default_omits_unsupported_temperature(self):
        for model in ["o3", "gpt-5", "gpt-5.1"]:
            self.config.MAIN_LLM_MODEL=model
            self.response();self.client.request("main",[{"role":"user","content":"问题"}])
            self.assertNotIn("temperature",self.session.post.call_args.kwargs["json"])


if __name__=="__main__":unittest.main()
