"""Portable bootstrap tests never install software, use the desktop or call APIs."""
from __future__ import annotations
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import struct
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
PORTABLE = ROOT / "portable"
sys.path.insert(0, str(PORTABLE))
sys.path.insert(0, str(PORTABLE / "astrbot_plugin_wechatbot_profile"))
import portable_bootstrap as boot
import weflow_setup as setup
import policy

class PortablePolicyTests(unittest.TestCase):
    def settings(self, **values):
        return {"main_llm_api_base":"https://api.example.test/v1", "main_llm_api_key":"synthetic-secret", "main_llm_model":"visual-model", "main_llm_prompt":"Use current conversation facts.",
            "main_llm_context_tokens":4096, "main_llm_max_tokens":512, "main_llm_temperature":.4, "main_llm_timeout":22, **values}

    def test_dedicated_provider_receives_all_profile_options(self):
        config=policy.provider_config(self.settings(main_llm_thinking="on",main_llm_reasoning_effort="high"))
        self.assertEqual(config["id"],"wechatbot-main")
        self.assertEqual(config["custom_extra_body"],{"reasoning_effort":"high","max_tokens":512})
        self.assertEqual((config["max_context_tokens"],config["timeout"]),(4096,22))
        self.assertEqual(config["modalities"],["text","image"])
        self.assertTrue(config["enable"])

    def test_qwen_off_and_temperature_are_passed_to_actual_provider(self):
        config=policy.provider_config(self.settings(main_llm_model="qwen-vl",main_llm_thinking="off"))
        self.assertEqual(config["custom_extra_body"],{"enable_thinking":False,"max_tokens":512,"temperature":.4})

    def test_fixed_temperature_models_match_the_shared_main_client(self):
        for model in ("o1", "o3-mini", "o4-mini", "gpt-5", "gpt-5.1", "gpt-5-mini"):
            with self.subTest(model=model):
                config=policy.provider_config(self.settings(main_llm_model=model, main_llm_thinking="auto"))
                self.assertNotIn("temperature",config["custom_extra_body"])

    def test_loopback_main_model_is_enabled_without_remote_key(self):
        for base in ("http://127.0.0.1:11434/v1", "http://localhost:9000/v1", "http://[::1]:9000/v1"):
            with self.subTest(base=base):
                provider=policy.provider_config(self.settings(main_llm_api_base=base,main_llm_api_key=""))
                self.assertTrue(provider["enable"])
        provider=policy.provider_config(self.settings(main_llm_api_key=""))
        self.assertFalse(provider["enable"])

    def test_missing_model_never_enables_paid_provider(self):
        config=policy.provider_config(self.settings(main_llm_model="",main_llm_api_key=""))
        self.assertFalse(config["enable"])
        self.assertEqual(config["key"],[])

    def test_snapshot_provider_id_changes_and_contains_no_secret(self):
        old=policy.main_profile(self.settings())
        new=policy.main_profile(self.settings(main_llm_api_key="second-key"))
        self.assertNotEqual(policy.profile_digest(old),policy.profile_digest(new))
        self.assertNotIn("synthetic-secret",policy.profile_digest(old))

    def test_prompt_context_and_media_preserve_current_conversation(self):
        profile=policy.main_profile(self.settings())
        req=types.SimpleNamespace(system_prompt="Verified identity and role facts.",prompt="Current question",image_urls=["/test/image.jpg"],contexts=[{"role":"user","content":"old"*3000},{"role":"assistant","content":"reply"}],extra_user_content_parts=[])
        original_media=list(req.image_urls)
        policy.apply_request(req,profile)
        self.assertIn("Verified identity and role facts.",req.system_prompt)
        self.assertIn(profile["prompt"],req.system_prompt)
        self.assertEqual(req.contexts,[])
        self.assertEqual(req.prompt,"Current question")
        self.assertEqual(req.image_urls,original_media)
        policy.apply_request(req,profile)
        self.assertEqual(req.system_prompt.count("[微信bot 主模型提示词]"),1)

    def test_full_system_prompt_is_preserved_or_rejected(self):
        profile=policy.main_profile(self.settings(main_llm_context_tokens=1024,main_llm_max_tokens=512))
        req=types.SimpleNamespace(system_prompt="事实"*5000,prompt="hi",image_urls=[],contexts=[],extra_user_content_parts=[])
        with self.assertRaises(RuntimeError):policy.apply_request(req,profile)

    def test_media_identity_note_uses_ids_not_display_names(self):
        message={"message":[{"type":"image","data":{"wechat_session_id":"100@chatroom","wechat_actor_id":"wxid_synthetic","wechat_message_id":"123","nickname":"Wrong display"}}]}
        note=policy.media_identity_note(message)
        self.assertIn("wxid_synthetic",note)
        self.assertIn("100@chatroom",note)
        self.assertNotIn("Wrong display",note)
        message["message"][0]["data"]["wechat_actor_id"]="line\nINJECT"
        self.assertEqual(policy.media_identity_note(message),"")

class PortableBootstrapTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.runtime=self.root/"runtime";self.runtime.mkdir()
        self.bridge=self.root/"bridge";self.bridge.mkdir()
        self.instance=self.runtime/"instance"
        for name,value in (("RUNTIME",self.runtime),("BRIDGE",self.bridge),("INSTANCE",self.instance),("ROOT",self.root)):
            fixture=patch.object(boot,name,value);fixture.start();self.addCleanup(fixture.stop)

    def test_config_creation_does_not_replace_existing_data_or_settings(self):
        example={"astrbot_attachments":"","main_llm_model":""}
        boot.atomic_json(self.bridge/"config.example.json",example)
        self.assertEqual(boot.prepare_bridge_config()["main_llm_model"],"")
        settings={"astrbot_attachments":"C:/Users/Custom/My attachments","main_llm_api_key":"preserved-secret","user_extra":{"keep":True}}
        boot.atomic_json(self.bridge/"config.json",settings)
        current=boot.prepare_bridge_config()
        for key,value in settings.items():self.assertEqual(current[key],value)
        self.assertEqual(current["ffmpeg_path"],str(self.runtime/"bin/ffmpeg.exe"))

    def test_owned_attachment_path_rebases_after_move(self):
        old="D:/old-package"+os.sep
        boot.atomic_json(self.runtime/"package-state.json",{"package_root":"D:/old-package"})
        boot.atomic_json(self.bridge/"config.json",{"astrbot_attachments":old+"portable/data"})
        settings=boot.prepare_bridge_config()
        self.assertEqual(settings["astrbot_attachments"],str(self.instance/"data/attachments"))

    def test_owned_ffmpeg_path_rebases_after_move_but_external_tool_is_preserved(self):
        old_root=self.root/"old-package"
        boot.atomic_json(self.runtime/"package-state.json",{"package_root":str(old_root)})
        boot.atomic_json(self.bridge/"config.json",{"ffmpeg_path":str(old_root/"portable/runtime/bin/ffmpeg.exe"),"main_llm_api_key":"keep-secret"})
        settings=boot.prepare_bridge_config()
        self.assertEqual(settings["ffmpeg_path"],str(self.runtime/"bin/ffmpeg.exe"))
        self.assertEqual(settings["main_llm_api_key"],"keep-secret")
        external=str(self.root/"my-external-ffmpeg.exe")
        boot.atomic_json(self.bridge/"config.json",{"ffmpeg_path":external})
        self.assertEqual(boot.prepare_bridge_config()["ffmpeg_path"],external)

    def test_existing_astrbot_globals_and_other_platforms_are_preserved(self):
        self.instance.mkdir()
        (self.instance/".astrbot").write_text("managed instance",encoding="utf-8")
        original={"dashboard":{"host":"127.0.0.1","port":9991,"enable":True,"custom":"keep"},
            "platform":[{"id":"custom-platform","type":"webchat","enable":True}],
            "provider_settings":{"default_image_caption_provider_id":"my-caption","display_reasoning_text":True},
            "platform_settings":{"reply_with_quote":True},
            "agent_runner":{"config":{"model":{"provider_id":"my-custom-agent"},"other":"keep"}},
            "provider":[{"id":"my-custom-provider","key":["my-key"],"model":"custom-model"}],"user_extra":{"keep":True}}
        boot.atomic_json(self.instance/"data/cmd_config.json",original)
        with patch.object(boot,"run_checked") as run:
            boot.prepare_astrbot(self.root/"fake-python.exe",PortablePolicyTests().settings())
        run.assert_not_called()
        current=boot.load_json(self.instance/"data/cmd_config.json")
        for key in ("dashboard","provider_settings","platform_settings","agent_runner","user_extra"):
            self.assertEqual(current[key],original[key])
        self.assertIn(original["platform"][0],current["platform"])
        self.assertIn(original["provider"][0],current["provider"])

    def test_runtime_rejects_global_interpreters_before_install(self):
        with patch.dict(os.environ,{"WECHATBOT_PORTABLE_UV":sys.executable,"WECHATBOT_PORTABLE_PYTHON":sys.executable}),self.assertRaises(RuntimeError):
            boot.managed_paths()

    def test_download_reuses_only_matching_hash(self):
        path=self.root/"archive.zip";path.write_bytes(b"verified")
        with patch.object(boot.urllib.request,"urlopen") as network:
            self.assertEqual(boot.verified_download("https://example.test/a",path,hashlib.sha256(b"verified").hexdigest()),path)
            network.assert_not_called()
        with patch.dict(os.environ,{"WECHATBOT_OFFLINE":"1"}),self.assertRaises(RuntimeError):
            boot.verified_download("https://example.test/a",path,"0"*64)

    def test_subprocess_is_argv_hidden_without_shell_or_stdin(self):
        with patch.object(boot.subprocess,"run",return_value=types.SimpleNamespace(returncode=0)) as run:
            boot.run_checked(["C:/A B/python.exe","--test"],cwd=self.root)
        args,kwargs=run.call_args
        self.assertEqual(args[0],["C:/A B/python.exe","--test"])
        self.assertNotIn("shell",kwargs)
        self.assertEqual(kwargs["stdin"],boot.subprocess.DEVNULL)

    def test_captured_errors_do_not_echo_provider_secrets(self):
        with patch.object(boot.subprocess,"run",return_value=types.SimpleNamespace(returncode=7,stderr="SECRET",stdout="SECRET")):
            with self.assertRaises(RuntimeError) as error:boot.run_checked(["exe"],capture=True)
        self.assertNotIn("SECRET",str(error.exception))

    def test_missing_requested_voice_opens_verified_installer_only_on_start(self):
        with patch.object(boot,"run_checked") as run:
            for action in ("prepare","check","open","restart-astrbot"):
                boot.prepare_voice_driver({"voice_enabled":True},{"virtual_cable":False},action)
            boot.prepare_voice_driver({"voice_enabled":False},{"virtual_cable":False},"all")
            boot.prepare_voice_driver({"voice_enabled":True},{"virtual_cable":True},"all")
            run.assert_not_called()
            report=boot.prepare_voice_driver({"voice_enabled":True},{"virtual_cable":False},"all")
            self.assertTrue(report["voice_restart_required"])
            self.assertEqual(run.call_args.args[0][-1],boot.HERE/"install_voice_driver.ps1")
        with patch.object(boot,"run_checked",side_effect=RuntimeError("cancelled")):
            report=boot.prepare_voice_driver({"voice_enabled":True},{"virtual_cable":False},"all")
            self.assertEqual(report["voice_driver_setup_state"],"cancelled_or_failed")
            self.assertFalse(report["voice_restart_required"])

    def test_astrbot_stop_uses_owned_bridge_venv_with_directory_as_argv(self):
        interpreter = self.runtime / "bridge-venv" / "Scripts" / "python.exe"
        environment = {"ASTRBOT_ROOT": str(self.instance)}
        with patch.dict(sys.modules, {"astrbot_lifecycle": None}), patch.object(boot, "run_checked") as run:
            boot.stop_astrbot(interpreter, environment)
        command = run.call_args.args[0]
        self.assertEqual(command[:5], [interpreter, "-X", "utf8", "-B", "-c"])
        self.assertIn("stop_owned()", command[5])
        self.assertEqual(command[6], boot.HERE)
        self.assertNotIn(str(boot.HERE), command[5])
        self.assertIs(run.call_args.kwargs["env"], environment)
        self.assertEqual(run.call_args.kwargs["timeout"], 40)

    def test_restart_astrbot_prepares_then_gracefully_stops_before_launch(self):
        uv, python = self.runtime / "uv.exe", self.runtime / "base-python.exe"
        bridge_python = self.runtime / "bridge-venv/Scripts/python.exe"
        astrbot_python = self.runtime / "astrbot-venv/Scripts/python.exe"
        instance_config = self.instance / "data/cmd_config.json"
        boot.atomic_json(instance_config, {"dashboard": {"port": 6185}})
        boot.atomic_json(self.bridge / "config.json", {})
        with patch.dict(sys.modules, {"astrbot_lifecycle": None}), \
             patch.object(boot, "managed_paths", return_value=(uv, python)), \
             patch.object(boot, "ensure_environment", side_effect=[bridge_python, astrbot_python]), \
             patch.object(boot, "prepare_bridge_config", return_value={}), \
             patch.object(boot, "prepare_astrbot", return_value={"ASTRBOT_ROOT": str(self.instance)}), \
             patch.object(boot, "prepare_ffmpeg", return_value=self.runtime / "bin"), \
             patch.object(boot, "native_diagnostics", return_value={"virtual_cable": True}), \
             patch.object(boot, "run_checked", return_value=types.SimpleNamespace(returncode=0)) as run:
            self.assertEqual(boot.main(["restart-astrbot"]), 0)
        self.assertEqual(run.call_count, 2)
        stop, start = [call.args[0] for call in run.call_args_list]
        self.assertEqual(stop[0], bridge_python)
        self.assertIn("stop_owned()", stop[5])
        self.assertEqual(start, [bridge_python, "-X", "utf8", "-B", self.bridge / "startup_launcher.py", "all"])
        self.assertEqual(run.call_args.kwargs["env"]["ASTRBOT_ROOT"], str(self.instance))

    def test_unconfirmed_astrbot_stop_never_launches_replacement(self):
        with patch.object(boot, "run_checked", side_effect=RuntimeError("normal shutdown unconfirmed")) as run:
            with self.assertRaises(RuntimeError):
                boot.stop_astrbot(self.runtime / "bridge-venv/Scripts/python.exe", {})
        run.assert_called_once()

class WeflowBootstrapTests(unittest.TestCase):
    def make_asar(self, raw=b"test"):
        tree={"files":{"main.js":{"offset":"0","size":len(raw),"integrity":{"algorithm":"SHA256","hash":setup.sha(raw),"blockSize":2,"blocks":[setup.sha(raw[i:i+2]) for i in range(0,len(raw),2)]}}}}
        text=json.dumps(tree,separators=(",",":")).encode();padded=text+b"\0"*((-len(text))%4)
        return struct.pack("<4I",4,len(padded)+8,len(padded)+4,len(text))+padded+raw

    def test_asar_integrity_and_bounds_are_required(self):
        data=self.make_asar()
        _,packed=setup.unpack_asar(data)
        self.assertEqual(packed[0][3],b"test")
        for invalid in (data[:-1],data[:-1]+b"X",b"short"):
            with self.subTest(invalid=invalid),self.assertRaises(ValueError):setup.unpack_asar(invalid)

    def test_unknown_original_hash_never_patches(self):
        with self.assertRaises(ValueError):setup.patch_asar(self.make_asar(),PORTABLE)

    def test_modified_main_with_unknown_whole_asar_never_changes_app_or_config(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            executable=root/"WeFlow.exe";executable.write_bytes(b"verified-exe")
            (root/"resources").mkdir()
            (root/"resources/app.asar").write_bytes(b"unknown-other-file-change")
            appdata=root/"appdata"; appconfig=appdata/"weflow/WeFlow-config.json"
            original={"httpApiEnabled":False,"httpApiToken":"","user_setting":"keep"}
            boot.atomic_json(appconfig,original)
            bridge_config=root/"bridge-config.json";boot.atomic_json(bridge_config,{"weflow_base_url":"http://127.0.0.1:5031","access_token":""})
            real_sha=setup.sha
            def digest(raw):
                return setup.EXE_SHA if raw==b"verified-exe" else setup.MODIFIED_MAIN_SHA if raw==b"known-modified-main" else real_sha(raw)
            packed=[(0,"dist-electron/main.js",{},b"known-modified-main")]
            with patch.dict(os.environ,{"APPDATA":str(appdata)}),patch.object(setup,"api_probe",return_value=False),patch.object(setup,"discover_executable",return_value=executable),patch.object(setup,"sha",side_effect=digest),patch.object(setup,"unpack_asar",return_value=({},packed)),patch.object(setup,"ps_command") as lifecycle:
                self.assertFalse(setup.ensure_weflow(bridge_config,root/"runtime"))
            lifecycle.assert_not_called()
            self.assertEqual(boot.load_json(appconfig),original)
            self.assertEqual((root/"resources/app.asar").read_bytes(),b"unknown-other-file-change")

    def test_api_probe_never_reports_cache_only_or_wrong_contract_fresh(self):
        for body,expected in (({"fresh":True,"version":1,"capabilities":{"snsMediaKey":1}},True),
                              ({"fresh":True,"version":1},False),
                              ({"fresh":False,"version":1,"capabilities":{"snsMediaKey":1}},False),
                              ({"fresh":True,"version":2,"capabilities":{"snsMediaKey":1}},False)):
            response=Mock();response.__enter__=Mock(return_value=response);response.__exit__=Mock();response.read.return_value=json.dumps(body).encode()
            for details_status in (404, 405):
                error = setup.urllib.error.HTTPError("http://test.invalid", details_status, "", {"Allow":"GET"}, None)
                with patch.object(setup.urllib.request,"urlopen",side_effect=[response,error]) as read:
                    self.assertEqual(setup.api_probe("http://127.0.0.1:5031","synthetic-token"), expected and details_status == 405)
                    self.assertEqual(read.call_count, 2 if expected else 1)
                    if expected:
                        self.assertEqual(read.call_args.args[0].get_method(), "HEAD")
                        self.assertIn("/sns-identity-details?", read.call_args.args[0].full_url)

    def test_custom_remote_endpoint_is_not_installed_or_rewritten(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/"config.json";original={"weflow_base_url":"https://remote.example.test:5031","access_token":"preserve"};boot.atomic_json(path,original)
            with patch.object(setup,"api_probe",return_value=False),patch.object(setup,"discover_executable") as discover:
                self.assertFalse(setup.ensure_weflow(path,Path(directory)))
            discover.assert_not_called()
            self.assertEqual(boot.load_json(path),original)

if __name__=="__main__":unittest.main()
