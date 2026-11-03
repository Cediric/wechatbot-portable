"""Qwen Image contracts use synthetic images and mocked HTTP only."""
import base64
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import threading
import types
import unittest
import sys
from unittest.mock import Mock, patch

SOURCE = Path(__file__).resolve().parents[1] / "wechat-weflow-bridge-ob11"


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, SOURCE / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Response:
    def __init__(self, value=None, *, status=200, data=None, headers=None):
        self.data = data if data is not None else json.dumps(value).encode()
        self.status_code, self.headers, self.closed = status, headers or {}, False

    def iter_content(self, _):
        yield self.data

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.closed = True


class ChatResponse(Response):
    def iter_content(self, chunk_size=None):
        yield self.data

    def close(self):
        self.closed = True


class QwenImageTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(sys, "path", [str(SOURCE), *sys.path]))
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = types.ModuleType("config")
        self.config.__dict__.update(BASE_DIR=str(self.root), DATA_DIR=str(self.root),
            MEDIA_MAX_MB=30, CACHE_RETENTION_DAYS=7, TOOLS_IMAGE_MODEL="qwen-image-3.0-pro",
            BOT_IMAGE_MODEL="wan2.7-image-pro", BOT_IMAGE_REFS=[], TOOLS_IMAGE_SIZE="1024*1024",
            DASHSCOPE_API_KEY="synthetic-qwen-key", DASHSCOPE_BASE_URL="https://dashscope.aliyuncs.com/api/v1",
            IMAGE_PROMPT_ENABLED=False)
        self.http = types.ModuleType("http_util")
        self.session = Mock()
        self.http.session = lambda: self.session
        # Image transport fixtures use a main-model fake; disabling the retired
        # auxiliary toggle no longer bypasses authoring in production.
        main = types.SimpleNamespace(request=Mock(side_effect=lambda profile, messages, **kwargs:
            json.loads(messages[-1]["content"])["request"]))
        modules = patch.dict("sys.modules", {"config": self.config, "http_util": self.http, "model_client": main})
        modules.start(); self.addCleanup(modules.stop)
        self.media = load("qwen_test_media", "media_util.py")
        modules = patch.dict("sys.modules", {"media_util": self.media})
        modules.start(); self.addCleanup(modules.stop)
        self.qwen = load("qwen_image", "qwen_image.py")
        modules = patch.dict("sys.modules", {"qwen_image": self.qwen})
        modules.start(); self.addCleanup(modules.stop)
        self.tools = load("qwen_test_tools", "tools.py")
        self.response = Response({"output": {"choices": [{"message": {"content": [
            {"image": "https://example.invalid/result.png?signed=synthetic"}]}}]}})
        self.session.post.return_value = self.response

    def image(self, name="reference.png", *, size=(300, 300), mode="RGB", color="white"):
        from PIL import Image
        path = self.root / name
        Image.new(mode, size, color).save(path)
        return str(path)

    def generate(self, prompt="画一朵白色小花", **kwargs):
        with patch.object(self.tools, "_download_generated_image", return_value="generated.png") as download:
            result = self.tools.generate_image(prompt, **kwargs)
        return result, download

    def test_text_contract_uses_selected_model_and_one_authenticated_post(self):
        result, download = self.generate()
        self.assertEqual(result, "generated.png")
        self.session.post.assert_called_once()
        args, kwargs = self.session.post.call_args
        self.assertEqual(args[0], "https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation")
        self.assertEqual(kwargs["json"]["model"], "qwen-image-3.0-pro")
        self.assertEqual(kwargs["json"]["input"]["messages"], [{"role": "user", "content": [{"text": "画一朵白色小花"}]}])
        self.assertEqual(kwargs["json"]["parameters"], {"n": 1, "watermark": False,
            "prompt_extend": True, "prompt_extend_mode": "direct", "enable_thinking": True, "size": "1024*1024"})
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer synthetic-qwen-key")
        self.assertFalse(kwargs["allow_redirects"])
        self.assertTrue(self.response.closed)
        download.assert_called_once_with("https://example.invalid/result.png?signed=synthetic")

    def test_announcement_gets_safe_provider_status_and_never_response_body(self):
        self.session.post.return_value = Response({"message": "private-body-with-signed-URL"}, status=403)
        with self.assertRaisesRegex(self.tools.ImageGenerationError, "HTTP 403") as failure:
            self.tools.generate_image("draw", raise_on_error=True)
        self.assertNotIn("signed-URL", str(failure.exception))
        self.session.post.assert_called_once()
        self.config.DASHSCOPE_API_KEY = ""
        with self.assertRaisesRegex(self.tools.ImageGenerationError, "生图密钥"):
            self.tools.generate_image("draw", raise_on_error=True)
        self.session.post.assert_called_once()

    def test_three_local_refs_keep_order_and_do_not_use_old_wan_default(self):
        refs = [self.image(f"ref-{index}.png", color=color) for index, color in enumerate(("red", "green", "blue"))]
        self.config.BOT_IMAGE_REFS = refs
        result, _ = self.generate()
        self.assertEqual(result, "generated.png")
        body = self.session.post.call_args.kwargs["json"]
        self.assertEqual(body["model"], "qwen-image-3.0-pro")
        content = body["input"]["messages"][0]["content"]
        self.assertEqual(len(content), 4)
        for record, path in zip(content, refs):
            self.assertTrue(record["image"].startswith("data:image/png;base64,"))
            self.assertEqual(base64.b64decode(record["image"].split(",", 1)[1]), Path(path).read_bytes())
        self.assertEqual(content[-1], {"text": "画一朵白色小花"})

    def test_qwen_21_accepts_four_identity_and_style_references_without_wan_override(self):
        self.config.TOOLS_IMAGE_MODEL = "qwen-image-2.1-pro"
        self.config.BOT_IMAGE_REFS = [self.image(f"ref-{index}.png") for index in range(4)]
        self.config.TOOLS_IMAGE_PROMPT_EXTEND = False
        self.assertEqual(self.generate()[0], "generated.png")
        body = self.session.post.call_args.kwargs["json"]
        self.assertEqual(body["model"], "qwen-image-2.1-pro")
        self.assertEqual(len(body["input"]["messages"][0]["content"]), 5)
        self.assertNotIn("negative_prompt", body["parameters"])
        self.assertEqual(body["parameters"]["prompt_extend"], False)

    def test_qwen_21_reference_limit_is_ten_and_does_not_change_30_limit(self):
        from config_model import normalize_config
        self.assertEqual(len(normalize_config({"bot_image_refs": ["synthetic.png"] * 10})["bot_image_refs"]), 10)
        with self.assertRaises(ValueError):
            normalize_config({"bot_image_refs": ["synthetic.png"] * 11})
        self.assertEqual(self.qwen.reference_limit("qwen-image-2.1-pro"), 10)
        self.assertEqual(self.qwen.reference_limit("qwen-image-2.1-turbo"), 10)
        self.assertEqual(self.qwen.reference_limit("qwen-image-3.0-pro"), 3)
        with self.assertRaises(ValueError):
            self.qwen.prepare_references(["missing.png"] * 11, model="qwen-image-2.1-pro")
        self.session.post.assert_not_called()

    def test_style_reference_notes_are_given_to_main_and_only_main_prompt_reaches_image_api(self):
        self.config.TOOLS_IMAGE_MODEL = "qwen-image-2.1-pro"
        self.config.BOT_IMAGE_REF_NOTES = "电影写实摄影；设定图只用于外貌，风格图用于真实皮肤毛发与自然光影。"
        main = sys.modules["model_client"].request
        main.side_effect = None
        main.return_value = "电影写实摄影，真实银发与耳朵毛发，夜间自然灯光。"
        self.assertEqual(self.generate()[0], "generated.png")
        messages = main.call_args.args[1]
        self.assertIn("区分参考图用途", messages[0]["content"])
        self.assertEqual(json.loads(messages[1]["content"])["reference_notes"], self.config.BOT_IMAGE_REF_NOTES)
        self.assertEqual(self.session.post.call_args.kwargs["json"]["input"]["messages"][0]["content"][-1]["text"], main.return_value)

    def test_ref_count_is_checked_before_any_prompt_or_image_call(self):
        self.config.BOT_IMAGE_REFS = ["missing.png"] * 4
        with patch.object(self.tools, "refine_image_prompt") as prompt:
            self.assertIsNone(self.tools.generate_image("draw"))
        prompt.assert_not_called(); self.session.post.assert_not_called()

    def test_damaged_reference_is_checked_before_prompt(self):
        path = self.root / "damaged.png"; path.write_bytes(b"not an image")
        self.config.BOT_IMAGE_REFS = [str(path)]
        with patch.object(self.tools, "refine_image_prompt") as prompt:
            self.assertIsNone(self.tools.generate_image("draw"))
        prompt.assert_not_called(); self.session.post.assert_not_called()

    def test_native_qwen_formats_allow_alpha_and_small_dimensions(self):
        for name, mode in [("alpha.png", "RGBA"), ("tiny.gif", "P"), ("tiny.tiff", "RGB")]:
            path = self.image(name, size=(1, 10), mode=mode)
            value = self.qwen.reference_input(path)
            self.assertTrue(value.startswith("data:image/"))
            self.assertEqual(base64.b64decode(value.split(",", 1)[1]), Path(path).read_bytes())
        with self.assertRaises(self.qwen.ImageError):
            self.qwen.reference_input(self.image("too-long.png", size=(1, 11)))

    def test_ref_size_and_dimension_bounds_fail_before_decode(self):
        path = self.root / "oversized.png"
        path.write_bytes(b"x")
        with patch.object(self.qwen.os.path, "getsize", return_value=10*1024*1024+1):
            with self.assertRaises(self.qwen.ImageError): self.qwen.reference_input(str(path))
        from PIL import Image
        mocked = Mock(format="PNG", size=(8001, 800))
        manager = Mock(__enter__=Mock(return_value=mocked), __exit__=Mock(return_value=False))
        with patch.object(Image, "open", return_value=manager):
            with self.assertRaises(self.qwen.ImageError): self.qwen.reference_input(str(path))
        mocked.verify.assert_not_called(); mocked.load.assert_not_called()

    def test_declared_data_mime_must_match_real_bytes(self):
        data = base64.b64encode(Path(self.image()).read_bytes()).decode()
        with self.assertRaises(self.qwen.ImageError):
            self.qwen.reference_input("data:image/jpeg;base64," + data)
        with self.assertRaises(self.qwen.ImageError):
            self.qwen.reference_input("data:image/png;base64,broken===")

    def test_remote_reference_is_downloaded_without_auth_and_locally_checked(self):
        image = Path(self.image()).read_bytes()
        remote = Response(data=image)
        self.session.get.return_value = remote
        self.config.BOT_IMAGE_REFS = ["https://example.invalid/reference.png"]
        result, _ = self.generate()
        self.assertEqual(result, "generated.png")
        self.assertNotIn("headers", self.session.get.call_args.kwargs)
        self.assertFalse(self.session.get.call_args.kwargs["allow_redirects"])
        self.assertTrue(remote.closed)

    def test_size_preserves_aspect_and_area_instead_of_silent_downsizing(self):
        self.assertEqual(self.qwen.image_size("3072x1024"), "3072*1024")
        self.assertEqual(self.qwen.image_size("2K"), "2048*2048")
        for value in ("4K", "4096*4096", "100*100", "8192*512"):
            with self.assertRaises(self.qwen.ImageError): self.qwen.image_size(value)
        self.config.TOOLS_IMAGE_SIZE = "4K"
        with patch.object(self.tools, "refine_image_prompt") as prompt:
            self.assertIsNone(self.tools.generate_image("draw"))
        prompt.assert_not_called(); self.session.post.assert_not_called()

    def test_auto_size_and_disabled_provider_thinking_use_official_fields(self):
        self.config.TOOLS_IMAGE_SIZE = "auto"
        self.config.TOOLS_IMAGE_PROMPT_EXTEND = False
        self.config.TOOLS_IMAGE_ENABLE_THINKING = False
        self.generate()
        values = self.session.post.call_args.kwargs["json"]["parameters"]
        self.assertEqual(values, {"n": 1, "watermark": False, "prompt_extend": False})

    def test_agent_edit_rejected_before_prompt_and_standard_model_supported(self):
        self.config.BOT_IMAGE_REFS = [self.image()]
        self.config.TOOLS_IMAGE_PROMPT_EXTEND_MODE = "agent"
        with patch.object(self.tools, "refine_image_prompt") as prompt:
            self.assertIsNone(self.tools.generate_image("draw"))
        prompt.assert_not_called(); self.session.post.assert_not_called()
        self.config.BOT_IMAGE_REFS = []; self.config.TOOLS_IMAGE_MODEL = "qwen-image-3.0"
        self.generate()
        self.assertEqual(self.session.post.call_args.kwargs["json"]["model"], "qwen-image-3.0")

    def test_unsupported_reference_model_is_never_replaced_with_wan(self):
        self.config.TOOLS_IMAGE_MODEL = "legacy-text-model"
        self.config.BOT_IMAGE_MODEL = "unsupported-edit-model"
        self.config.BOT_IMAGE_REFS = [self.image()]
        with patch.object(self.tools, "refine_image_prompt") as prompt:
            self.assertIsNone(self.tools.generate_image("draw"))
        prompt.assert_not_called(); self.session.post.assert_not_called()

    def test_cancel_before_prompt_and_after_prompt_prevents_paid_image_request(self):
        event = threading.Event(); event.set()
        with patch.object(self.tools, "refine_image_prompt") as prompt:
            self.assertIsNone(self.tools.generate_image("draw", cancelled=event))
        prompt.assert_not_called(); self.session.post.assert_not_called()
        event.clear()
        def refine(*args, **kwargs): event.set(); return "refined"
        with patch.object(self.tools, "refine_image_prompt", side_effect=refine):
            self.assertIsNone(self.tools.generate_image("draw", cancelled=event))
        self.session.post.assert_not_called()

    def test_cancel_after_provider_response_skips_download_and_never_retries(self):
        event = threading.Event()
        def post(*args, **kwargs): event.set(); return self.response
        self.session.post.side_effect = post
        result, download = self.generate(cancelled=event)
        self.assertIsNone(result); download.assert_not_called()
        self.session.post.assert_called_once()

    def test_expired_request_stops_before_provider_call(self):
        with self.assertRaises(self.qwen.ImageError):
            self.qwen.request_image("qwen-image-3.0-pro", "draw", [], {}, api_key="synthetic",
                base_url=self.config.DASHSCOPE_BASE_URL, deadline=0)
        self.session.post.assert_not_called()

    def test_provider_failure_does_not_log_error_body_or_repeat_charge(self):
        self.response = Response({"message": "synthetic-qwen-key private body"}, status=429)
        self.session.post.return_value = self.response
        with self.assertLogs("ob11-bridge", "WARNING") as logs:
            result, download = self.generate()
        self.assertIsNone(result); download.assert_not_called(); self.session.post.assert_called_once()
        self.assertNotIn("synthetic-qwen-key", "\n".join(logs.output))
        self.assertTrue(self.response.closed)

    def test_transport_error_does_not_log_embedded_key_or_signed_reference(self):
        self.session.post.side_effect = ValueError("synthetic-qwen-key https://example.invalid/?signed=synthetic")
        with self.assertLogs("ob11-bridge", "WARNING") as logs:
            result, download = self.generate()
        self.assertIsNone(result); download.assert_not_called(); self.session.post.assert_called_once()
        combined = "\n".join(logs.output)
        self.assertNotIn("synthetic-qwen-key", combined); self.assertNotIn("signed=synthetic", combined)

    def test_large_reference_upload_has_its_own_socket_budget_with_one_paid_post(self):
        from requests.exceptions import ConnectionError
        self.session.post.side_effect = ConnectionError("ConnectionResetError(10054, 'synthetic-qwen-key')")
        with self.assertRaises(self.qwen.ImageError) as failure:
            self.qwen.request_image("qwen-image-2.1-pro", "draw", ["x"*(2**20)], {}, api_key="synthetic",
                                    base_url=self.config.DASHSCOPE_BASE_URL, deadline=self.qwen.time.monotonic()+400)
        self.session.post.assert_called_once()
        connect, read = self.session.post.call_args.kwargs["timeout"]
        self.assertEqual(connect,13)
        self.assertGreater(read,390)
        self.assertIn("连接被对端重置",str(failure.exception))
        self.assertIn("等待响应头",str(failure.exception))
        self.assertNotIn("synthetic-qwen-key",str(failure.exception))

    def test_incomplete_success_body_never_resubmits_the_job(self):
        from requests.exceptions import ConnectionError
        self.response.iter_content = Mock(side_effect=ConnectionError("Read timed out"))
        with self.assertRaises(self.qwen.ImageError) as failure:
            self.qwen.request_image("qwen-image-2.1-pro","draw",[],{},api_key="synthetic",
                                    base_url=self.config.DASHSCOPE_BASE_URL)
        self.assertIn("读取服务端响应正文",str(failure.exception))
        self.assertIn("等待服务端响应超时",str(failure.exception))
        self.assertEqual(self.session.post.call_args.kwargs["timeout"][0],5)
        self.session.post.assert_called_once()

    def test_provider_rejection_is_reported_without_private_response_body(self):
        self.session.post.return_value = Response({"message":"signed=private"},status=401)
        with self.assertRaisesRegex(self.qwen.ImageError,"HTTP 401.*密钥认证失败") as error:
            self.qwen.request_image("qwen-image-2.1-pro","draw",[],{},api_key="synthetic",
                                    base_url=self.config.DASHSCOPE_BASE_URL)
        self.assertNotIn("private",str(error.exception))
        self.session.post.assert_called_once()

    def test_large_opaque_reference_preserves_size_and_alpha_reference_stays_exact(self):
        from PIL import Image
        for mode in ("RGB","RGBA"):
            target = self.root / (mode+".png")
            im = Image.new(mode,(768,512), (55,67,70,155) if mode=="RGBA" else (55,67,70))
            im.save(target, compress_level=0)
            original = target.read_bytes()
            url = self.qwen.reference_input(str(target))
            mime, raw = url.split(",",1)
            raw = base64.b64decode(raw)
            self.assertEqual(target.read_bytes(),original)
            if mode == "RGBA":
                self.assertEqual(raw,original)
            else:
                self.assertEqual(mime,"data:image/jpeg;base64")
                self.assertLess(len(raw),len(original))
                import io
                with Image.open(io.BytesIO(raw)) as result:
                    self.assertEqual(result.size,im.size)

    def test_tls_failure_preserves_safe_cause_without_credentials_or_paid_retry(self):
        from requests.exceptions import SSLError
        for code, explanation in (
            ("UNEXPECTED_EOF_WHILE_READING", "服务器或代理提前断开 TLS 连接"),
            ("CERTIFICATE_VERIFY_FAILED", "服务器证书校验失败"),
            ("WRONG_VERSION_NUMBER", "TLS 或代理协议不匹配"),
        ):
            with self.subTest(code=code):
                self.session.post.reset_mock()
                self.session.post.side_effect = SSLError(
                    f"[SSL: {code}] synthetic-qwen-key https://example.invalid/?signed=synthetic")
                with self.assertLogs("ob11-bridge", "WARNING") as logs:
                    result, download = self.generate()
                self.assertIsNone(result); download.assert_not_called()
                self.session.post.assert_called_once()
                combined = "\n".join(logs.output)
                self.assertIn(code, combined); self.assertIn(explanation, combined)
                self.assertNotIn("synthetic-qwen-key", combined)
                self.assertNotIn("example.invalid", combined)
                self.assertNotIn("signed=synthetic", combined)
                self.assertNotEqual(self.session.post.call_args.kwargs.get("verify"), False)

    def test_unknown_tls_failure_does_not_expose_arbitrary_error_text(self):
        from requests.exceptions import SSLError
        self.session.post.side_effect = SSLError("synthetic-qwen-key private TLS error")
        with self.assertLogs("ob11-bridge", "WARNING") as logs:
            result, download = self.generate()
        self.assertIsNone(result); download.assert_not_called()
        self.session.post.assert_called_once()
        combined = "\n".join(logs.output)
        self.assertIn("TLS 连接失败，尚未解析出具体原因", combined)
        self.assertNotIn("synthetic-qwen-key", combined)
        self.assertNotIn("private TLS error", combined)

    def test_real_ssl_exception_wrapping_keeps_python_eof_and_openssl_reasons(self):
        import ssl
        from requests.exceptions import SSLError
        from urllib3.exceptions import MaxRetryError
        for cause, expected in (
            (ssl.SSLEOFError(8, "EOF occurred in violation of protocol (_ssl.c:2427)"), "提前断开"),
            (ssl.SSLError(1, "[SSL: DECRYPTION_FAILED_OR_BAD_RECORD_MAC] bad record"),
             "DECRYPTION_FAILED_OR_BAD_RECORD_MAC"),
        ):
            with self.subTest(expected=expected):
                wrapped = SSLError(MaxRetryError(None,
                    "https://example.invalid/?signed=synthetic", reason=cause))
                detail = self.qwen._transport_error(wrapped)
                self.assertIn(expected, detail)
                self.assertNotIn("example.invalid", detail)
                self.assertNotIn("signed=", detail)

    def test_prompt_context_preserves_native_provenance_and_reference_notes_only(self):
        path = self.image()
        refs = types.SimpleNamespace(resolve=Mock(return_value=path), list_images=Mock(return_value=[
            {"value": "ref:synthetic", "name": "小助手参考", "note": "保留银发和红腰带，忽略播放器文字"}]))
        model = types.SimpleNamespace(request=Mock(return_value="最终绘画提示词"))
        context = {"session_id": "wxid_synthetic_owner", "actor_ids": ["wxid_synthetic_owner"],
            "related_chats": [{"message_id": "1", "content": "画一张赏月", "provenance": "same_private_conversation"}],
            "humanlike_plan": {"reply_type": "text_image"}}
        captured = {}
        def compact(payload, profile, *, system):
            captured.update(payload=payload, profile=profile, system=system)
            return json.dumps(payload, ensure_ascii=False)
        helper = types.SimpleNamespace(compact_context=Mock(side_effect=compact))
        self.config.BOT_IMAGE_REFS = ["ref:synthetic"]; self.config.IMAGE_PROMPT_ENABLED = True
        with patch.dict("sys.modules", {"reference_store": refs, "model_client": model, "interaction_context": helper}):
            self.generate(context=context)
        self.assertEqual(captured["profile"], "main")
        self.assertEqual(captured["payload"]["task"], "image_prompt")
        self.assertEqual(captured["payload"]["session_id"], "wxid_synthetic_owner")
        self.assertEqual(captured["payload"]["related_chats"], context["related_chats"])
        self.assertIn("忽略播放器文字", captured["payload"]["references"][0]["purpose"])
        self.assertNotIn("data:image", str(model.request.call_args))
        self.assertNotIn("reference_count", context)
        self.assertEqual(self.session.post.call_args.kwargs["json"]["input"]["messages"][0]["content"][-1]["text"], "最终绘画提示词")

    def test_whole_record_compaction_failure_prevents_both_paid_calls(self):
        helper = types.SimpleNamespace(compact_context=Mock(side_effect=ValueError("当前任务超过预算")))
        model = types.SimpleNamespace(request=Mock())
        self.config.IMAGE_PROMPT_ENABLED = True
        with patch.dict("sys.modules", {"model_client": model, "interaction_context": helper}):
            self.assertIsNone(self.tools.generate_image("draw", context={"actor_ids": ["wxid_synthetic"]}))
        model.request.assert_not_called(); self.session.post.assert_not_called()

    def test_actual_context_compactor_removes_whole_old_record_and_keeps_current_ids(self):
        model = types.SimpleNamespace(request=Mock(return_value="最终绘画提示词"), ModelError=RuntimeError,
            profile_settings=lambda _: {"context_tokens": 5000, "max_tokens": 512, "prompt": "角色卡"})
        self.config.IMAGE_PROMPT_ENABLED = True
        with patch.dict("sys.modules", {"model_client": model}):
            helper = load("qwen_test_context", "interaction_context.py")
            with patch.dict("sys.modules", {"interaction_context": helper}):
                self.generate(context={"session_id": "wxid_synthetic_owner", "actor_ids": ["wxid_synthetic_owner"],
                    "humanlike_plan": {"reply_type": "text_image"}, "related_chats": [
                        {"message_id": "new", "content": "画一张赏月", "provenance": "same_private_conversation"},
                        {"message_id": "old", "content": "旧记录" * 5000, "provenance": "same_private_conversation"}]})
        context = json.loads(model.request.call_args.args[1][1]["content"])
        self.assertEqual(context["session_id"], "wxid_synthetic_owner")
        self.assertEqual(context["actor_ids"], ["wxid_synthetic_owner"])
        self.assertEqual(context["related_chats"], [{"message_id": "new", "content": "画一张赏月", "provenance": "same_private_conversation"}])
        self.assertEqual(context["humanlike_plan"], {"reply_type": "text_image"})
        self.assertEqual(context["context_compaction"]["removed_records"]["related_chats"], 1)

    def test_request_expiring_after_response_skips_results_and_closes_response(self):
        with patch.object(self.qwen.time, "monotonic", side_effect=[10, 10, 12]):
            with self.assertRaises(self.qwen.ImageError):
                self.qwen.request_image("qwen-image-3.0-pro", "draw", [], {}, api_key="synthetic",
                    base_url=self.config.DASHSCOPE_BASE_URL, deadline=11)
        self.session.post.assert_called_once(); self.assertTrue(self.response.closed)

    def test_redirect_is_not_followed_and_malformed_success_is_rejected(self):
        for response in (Response({}, status=302), Response({"output": {"choices": []}}),
                         Response({"code": "InvalidParameter", "message": "private"}),
                         Response(data=b"not json"), Response({"output": {"choices": [{"message": {"content": [
                             {"image": "https://user:pass@example.invalid/image"}]}}]}})):
            self.session.post.return_value = response
            result, download = self.generate()
            self.assertIsNone(result); download.assert_not_called()

    def test_late_download_is_discarded_with_owned_cache_cleanup(self):
        event = threading.Event()
        def download(_): event.set(); return "generated.png"
        with patch.object(self.tools, "_download_generated_image", side_effect=download), \
             patch.object(self.media, "remove_cached_file", return_value=True) as remove:
            self.assertIsNone(self.tools.generate_image("draw", cancelled=event))
        remove.assert_called_once_with("generated.png", self.tools.TOOLS_DIR)

    def test_final_prompt_estimate_handles_unicode_and_refuses_before_image_post(self):
        for exact, too_long in (("a" * 4500, "a" * 4501), ("画" * 1500, "画" * 1501),
                                ("🌙" * 1125, "🌙" * 1126)):
            self.session.post.reset_mock()
            self.qwen.request_image("qwen-image-3.0-pro", exact, [], {}, api_key="synthetic",
                base_url=self.config.DASHSCOPE_BASE_URL)
            self.session.post.assert_called_once()
            self.session.post.reset_mock()
            with self.assertRaises(self.qwen.ImageError):
                self.qwen.request_image("qwen-image-3.0-pro", too_long, [], {}, api_key="synthetic",
                    base_url=self.config.DASHSCOPE_BASE_URL)
            self.session.post.assert_not_called()

    def test_invalid_unicode_prompt_never_reaches_provider(self):
        with self.assertRaises(self.qwen.ImageError):
            self.qwen.request_image("qwen-image-3.0-pro", "画\ud800", [], {}, api_key="synthetic",
                base_url=self.config.DASHSCOPE_BASE_URL)
        self.session.post.assert_not_called()

    def _actual_prompt_models(self, *, context_tokens=12000, max_tokens=8192, persona="你是小助手。"):
        self.config.__dict__.update(IMAGE_PROMPT_ENABLED=True,
            MAIN_LLM_API_BASE="https://example.invalid/v1", MAIN_LLM_API_KEY="synthetic",
            MAIN_LLM_MODEL="synthetic-model", MAIN_LLM_CONTEXT_TOKENS=context_tokens,
            MAIN_LLM_MAX_TOKENS=max_tokens, BOT_PERSONA=persona,
            BOT_OWNER_WECHAT_IDS=["wxid_synthetic_owner"])
        with patch("sys.path", [str(SOURCE), *sys.path]):
            model = load("qwen_review_model_client", "model_client.py")
            helper = load("qwen_review_interaction_context", "interaction_context.py")
        return model, helper

    def test_no_history_context_keeps_json_and_counts_persona_and_output_reserve(self):
        model, helper = self._actual_prompt_models()
        completion = ChatResponse({"choices": [{"message": {"content": "最终绘图提示词"}}]})
        self.session.post.side_effect = [completion, self.response]
        request = "画一张月下剑灵的艺术画面。"
        with patch.dict("sys.modules", {"model_client": model, "interaction_context": helper}):
            result, _ = self.generate(request)
        self.assertEqual(result, "generated.png")
        self.assertEqual(self.session.post.call_count, 2)
        payload = self.session.post.call_args_list[0].kwargs["json"]
        task = json.loads(payload["messages"][-1]["content"])
        self.assertEqual(task["request"], request)
        self.assertEqual(task["reference_count"], 0)
        self.assertIn("[角色卡]", payload["messages"][0]["content"])
        self.assertIn("wxid_synthetic_owner", payload["messages"][0]["content"])
        self.assertEqual(payload["max_tokens"], 8192)
        # Fixed workflow uses the same Chinese token estimate as model_client;
        # JSON and editable data already had a wider UTF-8 bound in compaction.
        cost = sum(model.estimate_tokens(item["content"]) for item in payload["messages"])
        self.assertLessEqual(cost + payload["max_tokens"], 12000 - 128)
        self.assertTrue(completion.closed)

    def test_no_history_oversized_current_json_stops_both_paid_calls(self):
        model, helper = self._actual_prompt_models(context_tokens=1024, max_tokens=256, persona="")
        with patch.dict("sys.modules", {"model_client": model, "interaction_context": helper}), \
             self.assertLogs("ob11-bridge", "WARNING") as logs:
            self.assertIsNone(self.generate("画" * 3000)[0])
        self.session.post.assert_not_called()
        self.assertIn("生图提示词预算检查失败", "\n".join(logs.output))
        self.assertNotIn("画" * 3000, "\n".join(logs.output))

    def test_persona_larger_than_input_reservation_stops_before_prompt_call(self):
        model, helper = self._actual_prompt_models(persona="小助手，谨慎。" * 1000)
        with patch.dict("sys.modules", {"model_client": model, "interaction_context": helper}):
            self.assertIsNone(self.generate("画一朵花")[0])
        self.session.post.assert_not_called()

    def test_overlong_specialist_result_never_submits_paid_image_request(self):
        model, helper = self._actual_prompt_models()
        self.session.post.return_value = ChatResponse({"choices": [{"message": {"content": "画" * 2000}}]})
        with patch.dict("sys.modules", {"model_client": model, "interaction_context": helper}):
            result, download = self.generate("画一朵花")
        self.assertIsNone(result)
        download.assert_not_called()
        self.session.post.assert_called_once()
        self.assertTrue(self.session.post.call_args.args[0].endswith("/chat/completions"))


if __name__ == "__main__":
    unittest.main()
