"""Cross-check real authenticated handlers with mocked reference/voice services."""
import base64
import sys
import types
import unittest
from unittest.mock import Mock, patch

from test_panel import PanelTests


class PanelMediaCrossReview(PanelTests):
    def setUp(self):
        super().setUp()
        self.references=types.SimpleNamespace(list_images=Mock(return_value=[{"id":"a"*32,"value":"ref:"+"a"*32,"builtin":False}]),upload=Mock(return_value={"id":"a"*32,"value":"ref:"+"a"*32}),
                                             read=Mock(return_value=(b"synthetic-text","text/plain")),remove=Mock(return_value=True))
        self.voice=Mock()
        self.voice.list.return_value=[];self.voice.query.return_value={"status":"OK"}
        self.voice.create.return_value={"voice_id":"cosyvoice-v2-test-abc"}
        self.voice.delete.return_value={"deleted":True}
        self.voice.preview.return_value={"audio_id":"tts_1_"+"a"*32+".wav"}
        self.voice.read_preview.return_value=b"synthetic-text"
        self.factory=Mock(return_value=self.voice)
        modules=patch.dict(sys.modules,{"reference_store":self.references,"voice_cloning":types.SimpleNamespace(VoiceCloningClient=self.factory)})
        modules.start();self.addCleanup(modules.stop)

    def test_read_only_reference_and_audio_get_cannot_trigger_paid_calls(self):
        for path in ["/api/references","/api/references/image/"+"a"*32,"/api/voices/audio/tts_1_"+"a"*32+".wav"]:
            self.assertEqual(self.request("GET",path)[0],200)
        self.voice.preview.assert_not_called();self.voice.create.assert_not_called()
        self.references.upload.assert_not_called()
        for path in ["/api/voices/create","/api/voices/preview"]:
            self.assertEqual(self.request("GET",path)[0],404)
        self.voice.preview.assert_not_called();self.voice.create.assert_not_called()

    def test_all_reference_voice_routes_require_session_and_post_csrf(self):
        routes=[("POST","/api/references/upload",{"data_base64":base64.b64encode(b"image").decode()}),
                ("POST","/api/references/remove",{"id":"a"*32}),
                ("POST","/api/voices/create",{"url":"https://example.invalid/a.wav","confirm":"create_voice"}),
                ("POST","/api/voices/preview",{"text":"试听"}),
                ("POST","/api/voices/delete",{"voice_id":"cosyvoice-v2-test-abc","confirm":"delete_voice"})]
        for method,path,data in routes:
            with self.subTest(path=path):
                self.assertEqual(self.json_request(method,path,data,authenticated=False)[0],401)
                self.assertEqual(self.json_request(method,path,data,headers={"X-Panel-Token":"wrong"})[0],403)
                self.assertEqual(self.json_request(method,path,data,headers={"Origin":"https://evil.invalid"})[0],403)
        self.voice.create.assert_not_called();self.voice.delete.assert_not_called();self.voice.preview.assert_not_called()
        self.references.upload.assert_not_called();self.references.remove.assert_not_called()

    def test_voice_create_delete_require_explicit_confirmation_and_unknown_fields_fail(self):
        for path,data in [("/api/voices/create",{"url":"https://example.invalid/a.wav"}),
                          ("/api/voices/delete",{"voice_id":"cosyvoice-v2-test-abc"}),
                          ("/api/voices/preview",{"text":"试听","url":"https://example.invalid/injected"}),
                          ("/api/voices/run-arbitrary",{})]:
            self.assertEqual(self.json_request("POST",path,data)[0],400)
        self.voice.create.assert_not_called();self.voice.delete.assert_not_called();self.voice.preview.assert_not_called()

    def test_reference_decode_happens_locally_and_bad_base64_is_rejected(self):
        code,_,result=self.json_request("POST","/api/references/upload",{"data_base64":base64.b64encode(b"synthetic image").decode(),"name":"构图","note":"仅配色"})
        self.assertEqual(code,201)
        self.references.upload.assert_called_once_with(b"synthetic image",name="构图",note="仅配色")
        self.assertEqual(result["image"]["value"],"ref:"+"a"*32)
        self.assertEqual(self.json_request("POST","/api/references/upload",{"data_base64":"invalid!*"})[0],400)
        self.references.upload.assert_called_once()
        self.voice.create.assert_not_called();self.voice.preview.assert_not_called()

    def test_reference_size_limit_checked_before_read_and_upload(self):
        code,_,_=self.json_request("POST","/api/references/upload",{},headers={"Content-Length":str(28*1024*1024+1)},raw_body=b"{}")
        self.assertEqual(code,413);self.references.upload.assert_not_called()

    def test_selected_reference_config_save_failure_preserves_asset(self):
        self.config.BOT_IMAGE_REFS=["ref:"+"a"*32]
        self.state.update_config=lambda values:{"ok":False,"error":"synthetic save failed"}
        self.assertEqual(self.json_request("POST","/api/references/remove",{"id":"a"*32})[0],409)
        self.references.remove.assert_not_called()

    def test_builtin_remove_refuses_before_any_configuration_mutation(self):
        self.references.list_images.return_value=[{"id":"a"*32,"value":"ref:"+"a"*32,"builtin":True}]
        self.config.BOT_IMAGE_REFS=["ref:"+"a"*32]
        self.assertEqual(self.json_request("POST","/api/references/remove",{"id":"a"*32})[0],400)
        self.assertEqual(self.updates,[])
        self.references.remove.assert_not_called()

    def test_explicit_voice_actions_have_only_their_exact_service_effect(self):
        self.assertEqual(self.json_request("POST","/api/voices/list",{})[0],200)
        self.voice.list.assert_called_once();self.voice.preview.assert_not_called()
        self.assertEqual(self.json_request("POST","/api/voices/query",{"voice_id":"cosyvoice-v2-test-abc"})[0],200)
        self.voice.query.assert_called_once_with(voice_id="cosyvoice-v2-test-abc")
        self.assertEqual(self.json_request("POST","/api/voices/create",{"url":"https://example.invalid/a.wav","confirm":"create_voice"})[0],200)
        self.voice.create.assert_called_once_with(url="https://example.invalid/a.wav")
        self.voice.preview.assert_not_called();self.voice.delete.assert_not_called()
        self.assertEqual(self.json_request("POST","/api/voices/preview",{"text":"试听"})[0],200)
        self.voice.preview.assert_called_once_with(text="试听")


def load_tests(loader,tests,pattern):
    # Reuse the real-handler fixture without rerunning all inherited v5.2 tests.
    return unittest.TestSuite(PanelMediaCrossReview(name) for name in PanelMediaCrossReview.__dict__ if name.startswith("test_"))


if __name__=="__main__":unittest.main()
