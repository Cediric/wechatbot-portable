"""Isolated reference, voice and Moments-media tests; never call a paid API."""
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

SOURCE = Path(__file__).resolve().parents[1] / "wechat-weflow-bridge-ob11"


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, SOURCE / filename)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


def image(mode="RGB", size=(300,300)):
    from PIL import Image
    output = io.BytesIO(); Image.new(mode, size, "white").save(output, format="PNG")
    return output.getvalue()


class Response:
    def __init__(self, value, status=200):
        self.status_code, self.headers, self.closed = status, {}, False
        self.data = value if isinstance(value, bytes) else json.dumps({"output":value}).encode()
    def iter_content(self, *args, **kwargs):
        yield self.data
    def close(self):
        self.closed = True
    def __enter__(self):
        return self
    def __exit__(self, *args):
        self.close()


class MediaGenerationVoiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cfg = types.SimpleNamespace(DATA_DIR=self.temp.name, BASE_DIR=self.temp.name, MEDIA_MAX_MB=30,
            DASHSCOPE_API_KEY="synthetic-key", DASHSCOPE_BASE_URL="https://example.invalid/api/v1",
            TTS_MODEL="cosyvoice-v2", TTS_VOICE="", TTS_OUTPUT_DIR=str(self.root/"tts"),
            IMAGE_PROMPT_ENABLED=True, BOT_IMAGE_REF_NOTES="参考图 1 仅用于构图")
        self.session = Mock(); self.http = types.SimpleNamespace(session=lambda: self.session)
        self.http.new_session = Mock(return_value=self.session)
        self.model = types.SimpleNamespace(request=Mock(return_value="明确的最终图像提示词"))
        modules = patch.dict(sys.modules, {"config":self.cfg, "http_util":self.http, "model_client":self.model})
        modules.start(); self.addCleanup(modules.stop)
        self.media = load("isolated_media", "media_util.py")
        media_patch = patch.dict(sys.modules, {"media_util":self.media})
        media_patch.start(); self.addCleanup(media_patch.stop)
        self.refs = load("isolated_refs", "reference_store.py")
        ref_patch = patch.dict(sys.modules, {"reference_store":self.refs})
        ref_patch.start(); self.addCleanup(ref_patch.stop)
        self.voices = load("isolated_voices", "voice_cloning.py").VoiceCloningClient()
        self.moments = load("isolated_moments_media", "moments_media.py")
        self.tools = load("isolated_tools", "tools.py")

    def test_reference_upload_list_resolve_read_remove_stays_local(self):
        value = self.refs.upload(image(), name="构图", note="仅用于构图")
        self.assertEqual(len(self.refs.list_images()), 2)
        self.assertIn(value["value"],[i["value"] for i in self.refs.list_images()])
        self.assertEqual(self.refs.read(value["id"]), (image(),"image/png"))
        self.assertTrue(Path(self.refs.resolve(value["value"])).is_file())
        self.assertTrue(self.refs.remove(value["id"]))
        self.assertEqual(len(self.refs.list_images()),1)
        self.assertTrue(self.refs.list_images()[0]["builtin"])
        self.session.post.assert_not_called()

    def test_reference_rejects_invalid_small_transparent_and_tampered_images(self):
        for data in [b"not-an-image", image(size=(100,100)), image(mode="RGBA")]:
            with self.subTest(kind=len(data)), self.assertRaises((ValueError,OSError)):
                self.refs.upload(data)
        item = self.refs.upload(image())
        Path(self.refs.resolve(item["value"])).write_bytes(image(size=(301,301)))
        with self.assertRaises(ValueError):
            self.refs.resolve(item["value"])
        for value in ["ref:../secret", "ref:C:/secret", "../secret"]:
            with self.assertRaises(ValueError):
                self.refs.resolve(value)

    def test_builtin_neutral_reference_is_verified_opt_in_and_read_only(self):
        item=self.refs.list_images()[0]
        self.assertIn("通用",item["name"])
        self.assertIn("不用于人物",item["note"])
        self.assertEqual(len(self.refs.read(item["id"])[0]),3387)
        with self.assertRaises(ValueError): self.refs.remove(item["id"])

    def test_main_writes_image_prompt_in_an_isolated_task_with_reference_notes(self):
        ref = self.refs.upload(image(), name="参考一", note="保持发型，不参考背景")
        path = self.tools._reference_input(ref["value"])
        self.assertEqual(self.tools.refine_image_prompt("画一张",[path],reference_values=[ref["value"]]),"明确的最终图像提示词")
        call = self.model.request.call_args
        self.assertEqual(call.args[0],"main")
        request = json.loads(call.args[1][1]["content"])
        self.assertEqual(request["reference_count"],1)
        self.assertEqual(request["task"],"image_prompt")
        self.assertIn("不是聊天回复", call.args[1][0]["content"])
        self.assertIn("保持发型",request["references"][0]["purpose"])
        self.cfg.IMAGE_PROMPT_ENABLED=False
        self.assertEqual(self.tools.refine_image_prompt("原提示"),"明确的最终图像提示词")
        self.assertEqual(self.model.request.call_count, 2)
        self.assertTrue(all(call.args[0] == "main" for call in self.model.request.call_args_list))

    def test_voice_create_query_list_delete_use_official_actions_and_close(self):
        create = Response({"voice_id":"cosyvoice-v2-myvoice-abc"})
        self.session.post.return_value=create
        result=self.voices.create("https://example.invalid/sample.wav")
        self.assertEqual(result["target_model"],"cosyvoice-v2")
        self.assertEqual(self.session.post.call_args.kwargs["json"]["input"]["action"],"create_voice")
        self.assertTrue(create.closed)
        self.session.post.return_value=Response({"status":"OK","target_model":"cosyvoice-v2"})
        self.assertEqual(self.voices.query(result["voice_id"])["status"],"OK")
        self.assertEqual(self.session.post.call_args.kwargs["json"]["input"]["action"],"query_voice")
        self.session.post.return_value=Response({"voice_list":[{"voice_id":result["voice_id"],"status":"OK"}]})
        self.assertEqual(len(self.voices.list()),1)
        self.assertEqual(self.session.post.call_args.kwargs["json"]["input"]["action"],"list_voice")
        self.session.post.return_value=Response({})
        self.assertTrue(self.voices.delete(result["voice_id"])["deleted"])
        self.assertEqual(self.session.post.call_args.kwargs["json"]["input"]["action"],"delete_voice")

    def test_voice_invalid_input_and_active_delete_never_call_service(self):
        for url in ["file:///sample.wav","http://localhost/a.wav","https://127.0.0.1/a.wav","https://[fc00::1]/a.wav","https://user:pass@example.invalid/a.wav"]:
            with self.assertRaises(ValueError): self.voices.create(url)
        for prefix in ["", "tooLongPrefix", "角色"]:
            with self.assertRaises(ValueError): self.voices.create("https://example.invalid/a.wav",prefix)
        self.cfg.TTS_VOICE="cosyvoice-v2-myvoice-abc"
        with self.assertRaises(ValueError): self.voices.delete(self.cfg.TTS_VOICE)
        with self.assertRaises(ValueError): self.voices.list(page_size=101)
        self.session.post.assert_not_called()

    def test_voice_http_error_and_unknown_status_fail_closed(self):
        response=Response({},401); self.session.post.return_value=response
        with self.assertRaises(RuntimeError): self.voices.query("cosyvoice-v2-test-abc")
        self.assertTrue(response.closed)
        self.session.post.return_value=Response({"status":"UNKNOWN"})
        with self.assertRaises(RuntimeError): self.voices.query("cosyvoice-v2-test-abc")

    def test_clone_preview_checks_ready_matching_model_before_billable_tts(self):
        synth=Mock()
        with patch.dict(sys.modules,{"tts":types.SimpleNamespace(synthesize_voice=synth)}):
            self.session.post.return_value=Response({"status":"DEPLOYING","target_model":"cosyvoice-v2"})
            with self.assertRaises(ValueError): self.voices.preview("试听", "cosyvoice-v2-test-abc")
            self.session.post.return_value=Response({"status":"OK","target_model":"cosyvoice-v3-plus"})
            with self.assertRaises(ValueError): self.voices.preview("试听", "cosyvoice-v2-test-abc")
        synth.assert_not_called()

    def test_moments_url_rejects_unapproved_redirect_style_private_dns_and_credentials(self):
        good=[(2,1,6,"",("8.8.8.8",443))]
        with patch.object(self.moments.socket,"getaddrinfo",return_value=good):
            self.assertEqual(self.moments.validate_media_url("https://m.qpic.cn/photo"),"https://m.qpic.cn/photo")
            for url in ["https://127.0.0.1/a","https://qpic.cn.evil.invalid/a","https://x:q@qpic.cn/a","https://qpic.cn:8888/a","https://qpic.cn/a#fragment"]:
                with self.assertRaises(ValueError): self.moments.validate_media_url(url)
        with patch.object(self.moments.socket,"getaddrinfo",return_value=[(2,1,6,"",("192.168.1.2",443))]):
            with self.assertRaises(ValueError): self.moments.validate_media_url("https://qpic.cn/photo")

    def test_moments_download_uses_clean_session_no_redirect_and_cleans_files(self):
        response=Response(image()); response.status=200; response.stream=lambda *args,**kw: [response.data]
        response.release_conn=Mock(); clean=Mock(); clean.request.return_value=response
        factory=Mock(return_value=clean)
        decoded=[]
        def parts(path, **kwargs):
            decoded.append(Path(path).read_bytes())
            return [{"type":"image_url","image_url":{"url":"data:image/jpeg;base64,synthetic"}}]
        post={"identity_verified":True,"has_media":True,"media":[{"url":"https://qpic.cn/photo","type":"1"}]}
        from urllib.parse import urlsplit
        import urllib3
        with patch.dict(sys.modules,{"multimodal":types.SimpleNamespace(media_parts=parts)}), patch.object(self.moments,"_resolved_url",return_value=(urlsplit("https://qpic.cn/photo"),"8.8.8.8")), patch.object(urllib3,"HTTPSConnectionPool",factory):
            result=self.moments.post_parts(post)
        self.assertEqual(decoded,[image()]); self.assertEqual(result[-1]["type"],"image_url")
        self.assertEqual(factory.call_args.args,("8.8.8.8",))
        self.assertEqual(factory.call_args.kwargs["assert_hostname"],"qpic.cn")
        self.assertEqual(factory.call_args.kwargs["server_hostname"],"qpic.cn")
        self.assertEqual(clean.request.call_args.kwargs["headers"],{"Host":"qpic.cn", **self.moments.MEDIA_HEADERS})
        self.assertFalse(clean.request.call_args.kwargs["redirect"])
        self.assertEqual(list((self.root/"moments_media").iterdir()),[])

    def test_moments_media_missing_original_video_cannot_fall_back_to_thumbnail(self):
        with patch.dict(sys.modules,{"multimodal":types.SimpleNamespace(media_parts=Mock())}):
            with self.assertRaises(ValueError):
                self.moments.post_parts({"identity_verified":True,"has_media":True,"media":[{"type":"6","thumb":"https://qpic.cn/thumb"}]})
        self.session.get.assert_not_called()

    def test_moments_native_key_uses_configured_proxy_with_exact_key_and_cleans_media(self):
        self.cfg.WE_FLOW_BASE_URL = "http://127.0.0.1:5031"
        self.cfg.ACCESS_TOKEN = "synthetic-local-token"
        response = Response(image()); self.session.get.return_value = response
        url = "https://m.qpic.cn/photo/0?sig=keep%2fexact&token=synthetic%2Btoken&idx=1"
        model = types.SimpleNamespace(media_parts=Mock(return_value=[{"type":"image_url","image_url":{"url":"data:image/png;base64,synthetic"}}]))
        with patch.dict(sys.modules, {"multimodal": model}), patch.object(self.moments,"_resolved_url") as allowed:
            parts = self.moments.post_parts({"identity_verified":True,"has_media":True,
                "media":[{"url":url,"type":"2","key":"18446744073709551615"}]})
        allowed.assert_called_once_with(url)
        self.assertEqual(self.session.get.call_args.args, ("http://127.0.0.1:5031/api/v1/sns/media/proxy",))
        self.assertEqual(self.session.get.call_args.kwargs["params"], {"url":url,"key":"18446744073709551615","access_token":"synthetic-local-token"})
        self.assertFalse(self.session.get.call_args.kwargs["allow_redirects"])
        self.assertFalse(self.session.trust_env)
        self.assertEqual(parts[-1]["type"], "image_url")
        self.assertTrue(response.closed); self.session.close.assert_called_once()
        self.assertEqual(list((self.root/"moments_media").iterdir()),[])

    def test_native_proxy_decrypt_error_identifies_failure_without_leaking_body(self):
        self.cfg.WE_FLOW_BASE_URL = "http://127.0.0.1:5031"
        self.session.get.return_value = Response('{"error":"图片解密失败：无法识别图片格式","secret":"private"}'.encode(),502)
        with patch.object(self.moments,"_resolved_url"), self.assertRaises(self.moments.MediaDownloadError) as error:
            self.moments._download_native("https://m.qpic.cn/photo",str(self.root/"unused"),"123",None,
                                          deadline=self.moments.time.monotonic()+30)
        self.assertEqual(error.exception.http_status,502)
        self.assertIn("无法解密图片",str(error.exception))
        self.assertNotIn("private",str(error.exception))

    def test_link_card_uses_visible_metadata_without_downloading_the_landing_page(self):
        feed = load("media_native_feed_contract","moments_feed.py")
        xml = ('<TimeLineObject><ContentObject><title>小猫录像</title><description>傍晚拍摄</description>'
               '<mediaList><media><type>4</type><url>https://b23.tv/test</url></media></mediaList>'
               '</ContentObject></TimeLineObject>')
        post = feed.normalize_post({"id":"100","username":"wxid_test","createTime":10000,"rawXml":xml})
        with patch.dict(sys.modules,{"multimodal":types.SimpleNamespace(media_parts=Mock())}), patch.object(self.moments,"_download") as download:
            parts = self.moments.post_parts(post)
        download.assert_not_called()
        self.assertEqual(len(parts),1)
        self.assertEqual(parts[0]["type"],"text")
        self.assertIn("小猫录像 傍晚拍摄",parts[0]["text"])
        self.assertIn("未打开链接",parts[0]["text"])

    def test_moments_direct_request_keeps_signed_query_and_native_client_headers(self):
        from urllib.parse import urlsplit
        import urllib3
        response=Response(image());response.status=200;response.stream=lambda *args,**kwargs:[response.data];response.release_conn=Mock()
        pool=Mock();pool.request.return_value=response
        url="https://m.qpic.cn/photo/0?sig=keep%2fexact&token=synthetic%2Btoken&idx=1"
        with patch.object(self.moments,"_resolved_url",return_value=(urlsplit(url),"8.8.8.8")), patch.object(urllib3,"HTTPSConnectionPool",return_value=pool):
            self.moments._download(url,str(self.root/"download.image"),None,deadline=self.moments.time.monotonic()+30)
        self.assertEqual(pool.request.call_args.args,("GET","/photo/0?sig=keep%2fexact&token=synthetic%2Btoken&idx=1"))
        self.assertEqual(pool.request.call_args.kwargs["headers"]["User-Agent"],"MicroMessenger Client")
        self.assertNotIn("Authorization",pool.request.call_args.kwargs["headers"])
        self.assertNotIn("Cookie",pool.request.call_args.kwargs["headers"])

    def test_moments_http_failure_exposes_status_without_url_or_token_and_never_encodes(self):
        from urllib.parse import urlsplit
        import urllib3
        response=Response(image());response.status=400;response.release_conn=Mock()
        pool=Mock();pool.request.return_value=response
        url="https://m.qpic.cn/photo?token=synthetic-cdn-token"
        encoder=Mock()
        with patch.dict(sys.modules,{"multimodal":types.SimpleNamespace(media_parts=encoder)}), patch.object(self.moments,"_resolved_url",return_value=(urlsplit(url),"8.8.8.8")), patch.object(urllib3,"HTTPSConnectionPool",return_value=pool):
            with self.assertRaises(self.moments.MediaDownloadError) as error:
                self.moments.post_parts({"identity_verified":True,"media":[{"url":url,"type":"2"}]})
        self.assertEqual(error.exception.http_status,400)
        self.assertNotIn("synthetic-cdn-token",str(error.exception))
        encoder.assert_not_called();self.assertTrue(response.closed)
        self.assertEqual(list((self.root/"moments_media").iterdir()),[])

    def test_native_xml_signature_and_client_header_satisfy_cdn_before_main_reads_original(self):
        from urllib.parse import urlsplit
        import urllib3
        feed=load("media_native_feed_contract","moments_feed.py")
        xml=('<TimeLineObject><id>10001</id><username>wxid_friend</username><contentDesc>一张图</contentDesc>'
             '<ContentObject><mediaList><media><type>2</type><url token="synthetic-token">'
             'http://m.qpic.cn/photo/480?sig=unchanged%2fbytes</url></media></mediaList></ContentObject></TimeLineObject>')
        post=feed.normalize_post({"id":"10001","username":"wxid_friend","createTime":1791719900,
                                  "contentDesc":"一张图","rawXml":xml,"media":[]})
        response=Response(image());response.stream=lambda *args,**kwargs:[response.data];response.release_conn=Mock()
        pool=Mock()
        def cdn(method,target,**options):
            response.status=200 if (target=="/photo/0?sig=unchanged%2fbytes&token=synthetic-token&idx=1"
                and options["headers"].get("User-Agent")=="MicroMessenger Client") else 400
            return response
        pool.request.side_effect=cdn
        encoder=Mock(return_value=[{"type":"image_url","image_url":{"url":"data:image/png;base64,synthetic-original"}}])
        with patch.dict(sys.modules,{"multimodal":types.SimpleNamespace(media_parts=encoder)}), \
                patch.object(self.moments,"_resolved_url",side_effect=lambda url:(urlsplit(url),"8.8.8.8")), \
                patch.object(urllib3,"HTTPSConnectionPool",return_value=pool):
            parts=self.moments.post_parts(post)
        self.assertEqual(parts[-1]["image_url"]["url"],"data:image/png;base64,synthetic-original")
        encoder.assert_called_once();self.assertEqual(encoder.call_args.kwargs["kind"],"image")

    def test_native_proxy_failure_does_not_fall_back_to_encrypted_cdn_or_thumbnail(self):
        self.cfg.WE_FLOW_BASE_URL="http://127.0.0.1:5031";self.cfg.ACCESS_TOKEN="synthetic-secret"
        response=Response({},400);self.session.get.return_value=response
        encoder=Mock()
        with patch.dict(sys.modules,{"multimodal":types.SimpleNamespace(media_parts=encoder)}), \
                patch.object(self.moments,"_resolved_url"), patch.object(self.moments,"_download") as direct:
            with self.assertRaises(self.moments.MediaDownloadError) as failure:
                self.moments.post_parts({"identity_verified":True,"media":[{"type":"6","url":"https://v.qpic.cn/original",
                    "thumb":"https://v.qpic.cn/thumb","key":"18446744073709551615"}]})
        self.assertEqual(failure.exception.http_status,400)
        self.assertNotIn("synthetic-secret",str(failure.exception))
        direct.assert_not_called();encoder.assert_not_called();self.assertTrue(response.closed)


if __name__ == "__main__":
    unittest.main()
