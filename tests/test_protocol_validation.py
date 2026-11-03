"""Whole-message preparation and partial-result checks without sockets or desktop APIs."""
import base64
from concurrent.futures import Future
import hashlib
import importlib.util
from pathlib import Path
import tempfile
import subprocess
import sys
import threading
import time
import types
import unittest
from unittest.mock import MagicMock, Mock, patch
import wave

SOURCE=Path(__file__).resolve().parents[1]/"wechat-weflow-bridge-ob11"


def load(name,filename):
    spec=importlib.util.spec_from_file_location(name,SOURCE/filename)
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def text(value="head"):
    return {"type":"text","data":{"text":value}}


class ImmediateQueue:
    def submit(self,_,func,cancel_check):
        future=Future()
        try:
            future.set_result(False if cancel_check() else func())
        except Exception as error:
            future.set_exception(error)
        return future


def inline(coroutine):
    try:
        coroutine.send(None)
    except StopIteration as done:
        return done.value
    finally:
        coroutine.close()
    raise AssertionError("unexpected real async wait")


class ProtocolValidationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config=types.ModuleType("config")
        cls.config.__dict__.update(MEDIA_MAX_MB=30,VOICE_ENABLED=True,STICKER_ENABLED=True,
                                  ASTRBOT_ATTACHMENTS="",API_SEND_WAIT_SECONDS=8,redact_text=str)
        cls.state=types.ModuleType("state")
        cls.names=types.ModuleType("name_refresh")
        cls.names.refresh_target=Mock()
        cls.tts=types.ModuleType("tts")
        cls.tts.synthesize_voice=Mock(return_value=None)
        face=types.ModuleType("sticker_map")
        face.resolve_face=lambda value:f"[face {value}]"
        human=types.ModuleType("humanize")
        human.voice_script=lambda value:value
        human.wait_response_floor=lambda started,cancelled:not cancelled()
        human.wait_remaining=lambda started,minimum,cancelled:not cancelled()
        cls.sticker=types.ModuleType("sticker_selector")
        cls.sticker.select_sticker=Mock(return_value=None)
        cls.media=types.ModuleType("media_util")
        cls.media.download=Mock()
        cls.patches=patch.dict("sys.modules",{"config":cls.config,"state":cls.state,"name_refresh":cls.names,
            "tts":cls.tts,"sticker_map":face,"humanize":human,"sticker_selector":cls.sticker,"media_util":cls.media})
        cls.patches.start()
        cls.protocol=load("test_preflight_protocol","ob_protocol.py")

    @classmethod
    def tearDownClass(cls):
        cls.patches.stop()

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix=".protocol-test-",dir=str(SOURCE.parent/"tests"))
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.config.DATA_DIR=str(self.root)
        self.config.VOICE_ENABLED=True
        self.config.STICKER_ENABLED=True
        self.config.ASTRBOT_ATTACHMENTS=""
        self.state.running=True
        self.state.paused=threading.Event()
        self.state.send_cancel=threading.Event()
        self.state.bridge_generation=123
        self.state.client_generation=456
        self.state.run_lock=threading.RLock()
        self.state.reply_state_lock=threading.Lock()
        self.state.pending_reply_modes={}
        self.state.pop_response_started=lambda key,generation:None
        self.state.memory_store=None
        self.state._ob_id_to_contact={}
        self.state._ob_ws=None
        self.state.sender_instance=types.SimpleNamespace(_last_send={})
        current_sender=self.state.sender_instance
        current_sender.last_send_result=lambda:dict(current_sender._last_send)
        self.state.identity_registry=types.SimpleNamespace(get_private_record=lambda _: {"name":"friend"},get_group_record=lambda _: {"name":"group"},get_member_name=lambda value:str(value))
        self.state.send_queue=ImmediateQueue()
        self.events=[]
        self.state.dispatch_send=Mock(side_effect=lambda method,*args,**kwargs:self.events.append(("send",method)) or True)
        self.tts.synthesize_voice.reset_mock(return_value=True,side_effect=True)
        self.tts.synthesize_voice.return_value=None
        self.sticker.select_sticker.reset_mock(return_value=True,side_effect=True)
        self.sticker.select_sticker.return_value=None
        self.media.download.reset_mock(return_value=True,side_effect=True)
        self.protocol.synthesize_voice=self.tts.synthesize_voice
        mkstemp=self.protocol.tempfile.mkstemp
        temp_patch=patch.object(self.protocol.tempfile,"mkstemp",side_effect=lambda **kwargs:mkstemp(**{"dir":self.root,**kwargs}))
        temp_patch.start()
        self.addCleanup(temp_patch.stop)

    def image(self,name="caller.png"):
        from PIL import Image
        path=self.root/name
        Image.new("RGB",(8,8)).save(path)
        return str(path)

    def audio(self,name="caller.wav",seconds=.01):
        path=self.root/name
        with wave.open(str(path),"wb") as stream:
            stream.setnchannels(1)
            stream.setsampwidth(2)
            stream.setframerate(8000)
            stream.writeframes(b"\x00\x00"*int(seconds*8000))
        return str(path)

    def send(self,message,delivery=None):
        return self.protocol._perform_send({},1,False,message,123,delivery)

    def test_unsupported_tail_never_sends_or_downloads_valid_head(self):
        with self.assertRaises(ValueError):
            self.send([{"type":"image","data":{"file":"https://example.invalid/image"}},text(),{"type":"video","data":{"file":"video.mp4"}}])
        self.state.dispatch_send.assert_not_called()
        self.media.download.assert_not_called()

    def test_missing_local_tail_never_sends_head_and_preserves_caller_file(self):
        caller=self.image()
        with self.assertRaises(ValueError):
            self.send([{"type":"image","data":{"file":caller}},text(),{"type":"image","data":{"file":str(self.root/"missing.png")}}])
        self.state.dispatch_send.assert_not_called()
        self.assertTrue(Path(caller).exists())

    def test_all_tail_fields_are_validated_before_any_send(self):
        invalid=[{"type":"image","data":{}},{"type":"record","data":{}},{"type":"face","data":{}},
                 {"type":"at","data":{}},{"type":"reply","data":{}},{"type":"text","data":{"text":["bad"]}},
                 {"type":"face","data":{"id":1.5}},{"type":"face","data":{"id":True}},
                 {"type":"at","data":{"qq":True}},{"type":"at","data":{"qq":1.5}},
                 {"type":"reply","data":{"id":{}}},{"type":"reply","data":{"id":True}}]
        for tail in invalid:
            with self.subTest(tail=tail),self.assertRaises(ValueError):
                self.send([text(),tail])
        self.state.dispatch_send.assert_not_called()

    def test_invalid_target_id_is_not_truncated_or_treated_as_user_one(self):
        for target in (True,1.5,"1.5",[],{}):
            with self.subTest(target=target):
                response=inline(self.protocol._handle_ob_api({"action":"send_private_msg","params":{"user_id":target,"message":[text()]}}))
                self.assertEqual(response["status"],"failed")
                self.assertEqual(response["retcode"],1400)
        self.state.dispatch_send.assert_not_called()

    def test_invalid_local_image_and_truncated_wav_fail_before_head(self):
        invalid=self.root/"invalid.png"
        invalid.write_bytes(b"not image")
        with self.assertRaises(ValueError):
            self.send([text(),{"type":"image","data":{"file":str(invalid)}}])
        wav=Path(self.audio())
        wav.write_bytes(wav.read_bytes()[:-4])
        with self.assertRaises(ValueError):
            self.send([text(),{"type":"record","data":{"file":str(wav)}}])
        self.state.dispatch_send.assert_not_called()
        self.assertTrue(invalid.exists() and wav.exists())

    def test_jpeg_with_truncated_scan_never_sends_head(self):
        from PIL import Image
        path=self.root/"caller.jpg"
        Image.new("RGB",(64,64)).save(path)
        path.write_bytes(path.read_bytes()[:-8])
        with Image.open(path) as source:
            source.verify()  # This alone succeeds despite the missing scan end.
        with self.assertRaises(ValueError):
            self.send([text(),{"type":"image","data":{"file":str(path)}}])
        self.state.dispatch_send.assert_not_called()
        self.assertTrue(path.exists())

    def test_compressed_voice_decode_is_completed_before_head_without_real_device(self):
        path=self.root/"caller.mp3"
        path.write_bytes(b"ID3"+b"\x00"*24)
        response=types.SimpleNamespace(returncode=0,stdout=b"1.25\n")
        with patch.object(self.protocol.subprocess,"run",return_value=response) as probe:
            self.assertTrue(self.send([text(),{"type":"record","data":{"file":str(path)}}]))
        options=probe.call_args.kwargs
        self.assertEqual(options["env"]["SDL_AUDIODRIVER"],"dummy")
        self.assertEqual(options["timeout"],8)
        self.assertIs(options["stdin"],subprocess.DEVNULL)
        self.assertEqual(self.events,[("send","send_text"),("send","send_voice")])
        self.assertTrue(path.exists())

    def test_invalid_or_overlong_compressed_voice_never_sends_head(self):
        path=self.root/"caller.ogg"
        path.write_bytes(b"OggS"+b"\x00"*24)
        cases=[types.SimpleNamespace(returncode=1,stdout=b""),types.SimpleNamespace(returncode=0,stdout=b"61.0"),
               types.SimpleNamespace(returncode=0,stdout=b"nan"),subprocess.TimeoutExpired("probe",8)]
        for response in cases:
            with self.subTest(response=response):
                options={"side_effect":response} if isinstance(response,Exception) else {"return_value":response}
                with patch.object(self.protocol.subprocess,"run",**options),self.assertRaises(ValueError):
                    self.send([text(),{"type":"record","data":{"file":str(path)}}])
        self.state.dispatch_send.assert_not_called()
        self.assertTrue(path.exists())

    def test_real_flac_decoder_rejects_lost_frame_crc_or_pcm_checksum_before_head(self):
        # RFC 9639 constant subframes: two 1600-sample mono frames, no playback.
        def crc(data,bits,polynomial):
            result=0
            for value in data:
                result^=value<<(bits-8)
                for _ in range(8):
                    result=((result<<1)^polynomial if result&(1<<(bits-1)) else result<<1)&((1<<bits)-1)
            return result
        def encoded(bits):
            count,frames=1600,[]
            depth={8:1,16:4,24:6}[bits]
            for index in range(2):
                header=b"\xff\xf8\x78"+bytes([depth<<1,index])+(count-1).to_bytes(2,"big")
                frame=header+bytes([crc(header,8,7)])+b"\x00"+index.to_bytes(bits//8,"big")
                frames.append(frame+crc(frame,16,0x8005).to_bytes(2,"big"))
            pcm=b"\x00"*(count*(bits//8))+int(1).to_bytes(bits//8,"little")*count
            streaminfo=count.to_bytes(2,"big")*2+len(frames[0]).to_bytes(3,"big")*2+((16000<<44)|((bits-1)<<36)|(count*2)).to_bytes(8,"big")+hashlib.md5(pcm).digest()
            return b"fLaC\x80\x00\x00\x22"+streaminfo+b"".join(frames),len(frames[-1])
        with patch.object(sys,"path",[str(SOURCE/"third_party"),*sys.path]):
            if importlib.util.find_spec("mutagen") is None or importlib.util.find_spec("pygame") is None:
                self.skipTest("declared audio dependencies unavailable")
            valid,frame_size=encoded(16)
            wrong_md5=bytearray(valid);wrong_md5[26]^=1
            cases=[("valid8",encoded(8)[0],True),("valid16",valid,True),("valid24",encoded(24)[0],True),
                   ("missing",valid[:-frame_size],False),("crc",valid[:-2]+b"\x00\x00",False),("md5",bytes(wrong_md5),False)]
            for name,data,accepted in cases:
                with self.subTest(name=name):
                    self.state.dispatch_send.reset_mock()
                    path=self.root/(name+".flac");path.write_bytes(data)
                    message=[text(),{"type":"record","data":{"file":str(path)}}]
                    if accepted:
                        self.assertTrue(self.send(message))
                        self.assertEqual(self.state.dispatch_send.call_count,2)
                    else:
                        with self.assertRaises(ValueError):
                            self.send(message)
                        self.state.dispatch_send.assert_not_called()
                    self.assertTrue(path.exists())

    def test_owned_base64_is_cleaned_when_later_media_is_invalid(self):
        encoded=base64.b64encode(Path(self.image()).read_bytes()).decode()
        caller_files=set(self.root.iterdir())
        with self.assertRaises(ValueError):
            self.send([{"type":"image","data":{"file":"base64://"+encoded}},text(),{"type":"image","data":{"file":"base64://invalid!"}}])
        self.assertEqual(set(self.root.iterdir()),caller_files)
        self.state.dispatch_send.assert_not_called()

    def test_base64_image_has_one_complete_preflight(self):
        from PIL import Image
        encoded=base64.b64encode(Path(self.image()).read_bytes()).decode()
        with patch.object(Image,"open",wraps=Image.open) as opened:
            self.assertTrue(self.send([text(),{"type":"image","data":{"file":"base64://"+encoded}}]))
        # One structural check and one full decode, not a third redundant open.
        self.assertEqual(opened.call_count,2)
        self.assertEqual(self.events,[("send","send_text"),("send","send_image")])
        self.assertFalse(list(self.root.glob("akasha-owned-*")))

    def test_owned_truncated_base64_image_still_rejects_whole_message(self):
        from PIL import Image
        path=self.root/"truncated.jpg"
        Image.new("RGB",(64,64)).save(path)
        encoded=base64.b64encode(path.read_bytes()[:-8]).decode()
        with self.assertRaises(ValueError):
            self.send([text(),{"type":"image","data":{"file":"base64://"+encoded}}])
        self.state.dispatch_send.assert_not_called()
        self.assertFalse(list(self.root.glob("akasha-owned-*")))

    def test_http_image_uses_downloader_validation_without_second_decode(self):
        from PIL import Image
        blob=Path(self.image()).read_bytes()
        response=MagicMock(headers={"Content-Length":str(len(blob))})
        response.__enter__.return_value=response
        response.iter_content.return_value=[blob]
        http=types.SimpleNamespace(session=lambda:types.SimpleNamespace(get=Mock(return_value=response)))
        with patch.dict(sys.modules,{"http_util":http}):
            media=load("protocol_real_media_util","media_util.py")
        with patch.dict(sys.modules,{"media_util":media}),patch.object(Image,"open",wraps=Image.open) as opened:
            self.assertTrue(self.send([text(),{"type":"image","data":{"file":"https://example.invalid/picture"}}]))
        # media_util.download retains its full validation; preflight reuses it.
        self.assertEqual(opened.call_count,2)
        self.assertEqual(self.events,[("send","send_text"),("send","send_image")])
        self.assertFalse(list((self.root/"outgoing_media").glob("*")))

    def test_onebot_text_preserves_layout_and_punctuation(self):
        body = " 你 好。\nA&B！ "
        self.assertTrue(self.send([text(body)]))
        self.assertEqual(self.state.dispatch_send.call_args.args[:3], ("send_text", "friend", body))

    def test_http_owned_is_cleaned_on_later_error_without_deleting_caller_files(self):
        download=self.image("download.png")
        caller=self.image("caller.png")
        self.media.download.return_value=download
        with self.assertRaises(ValueError):
            self.send([{"type":"image","data":{"file":"https://example.invalid/image"}},
                       {"type":"image","data":{"file":caller}},text(),{"type":"record","data":{"file":"missing.wav"}}])
        self.assertFalse(Path(download).exists())
        self.assertTrue(Path(caller).exists())
        self.state.dispatch_send.assert_not_called()

    def test_downloads_finish_before_first_desktop_send(self):
        def download(url,*args,**kwargs):
            self.events.append(("download",url))
            return self.image(f"remote{len(self.events)}.png")
        self.media.download.side_effect=download
        self.assertTrue(self.send([text(),{"type":"image","data":{"file":"https://example.invalid/one"}},
                                  {"type":"image","data":{"file":"https://example.invalid/two"}}]))
        self.assertEqual([kind for kind,_ in self.events], ["download","download","send","send","send"])
        self.assertFalse(list(self.root.glob("remote*")))

    def test_cancellation_during_download_cleans_owned_without_send(self):
        download=self.image("download.png")
        def resolve(*args,**kwargs):
            self.state.send_cancel.set()
            return download
        self.media.download.side_effect=resolve
        with self.assertRaises(RuntimeError):
            self.send([text(),{"type":"image","data":{"file":"https://example.invalid/image"}}])
        self.assertFalse(Path(download).exists())
        self.state.dispatch_send.assert_not_called()

    def test_bad_tail_prevents_billable_voice_mode_synthesis(self):
        self.state.pending_reply_modes[1]={"reply_type":"voice","generation":123,"created_at":time.time()}
        with self.assertRaises(ValueError):
            self.send([text(),{"type":"image","data":{"file":"missing.png"}}])
        self.tts.synthesize_voice.assert_not_called()
        self.state.dispatch_send.assert_not_called()
        self.assertIn(1,self.state.pending_reply_modes)

    def test_voice_mode_prepares_once_and_keeps_tts_cache(self):
        self.state.pending_reply_modes[1]={"reply_type":"voice","generation":123,"created_at":time.time()}
        audio=self.audio()
        self.tts.synthesize_voice.return_value=audio
        self.assertTrue(self.send([text("voice answer")]))
        self.tts.synthesize_voice.assert_called_once_with("voice answer")
        self.assertEqual(self.state.dispatch_send.call_args.args[:3],("send_voice","friend",audio))
        self.assertTrue(Path(audio).exists())

    def test_empty_text_prefix_and_legacy_uppercase_voice_keep_full_reply(self):
        self.state.pending_reply_modes[1]="VOICE"
        self.tts.synthesize_voice.return_value=self.audio()
        self.assertTrue(self.send([{"type":"text","data":{}},text("full reply")]))
        self.tts.synthesize_voice.assert_called_once_with("full reply")
        self.assertEqual(self.state.dispatch_send.call_args.args[0],"send_voice")

    def test_legacy_mixed_mode_has_one_output_and_synthesis_failure_falls_back_once(self):
        self.state.pending_reply_modes[1]={"reply_type":"text_voice","generation":123,"created_at":time.time()}
        self.assertTrue(self.send([text()]))
        self.assertEqual(self.state.dispatch_send.call_count,1)
        self.assertEqual(self.state.dispatch_send.call_args.args[0], "send_text")
        self.state.dispatch_send.reset_mock()
        self.state.pending_reply_modes[1]={"reply_type":"text_voice","generation":123,"created_at":time.time()}
        self.tts.synthesize_voice.return_value=self.audio()
        self.state.dispatch_send.side_effect=[False]
        self.assertFalse(self.send([text()]))
        self.assertEqual(self.state.dispatch_send.call_count, 1)
        self.assertEqual(self.state.dispatch_send.call_args.args[0], "send_voice")

    def test_too_long_wav_or_synthesis_error_never_sends_head(self):
        audio=self.audio(seconds=61)
        with self.assertRaises(ValueError):
            self.send([text(),{"type":"record","data":{"file":audio}}])
        with self.assertRaises(ValueError):
            self.send([text(),{"type":"record","data":{"text":"synthesize"}}])
        self.state.dispatch_send.assert_not_called()

    def test_voice_disabled_fallback_is_decided_before_send(self):
        self.config.VOICE_ENABLED=False
        self.assertTrue(self.send([text(),{"type":"record","data":{"file":"missing.wav","text":"fallback"}}]))
        self.assertEqual([call.args[0] for call in self.state.dispatch_send.call_args_list],["send_text","send_text"])
        self.tts.synthesize_voice.assert_not_called()

    def test_late_failure_reports_partial_and_cleans_owned(self):
        download=self.image("download.png")
        self.media.download.return_value=download
        self.state.dispatch_send.side_effect=[True,False]
        with self.assertRaises(self.protocol.SendFailure) as failure:
            self.send([text(),{"type":"image","data":{"file":"https://example.invalid/image"}}])
        self.assertTrue(failure.exception.progress["partial"])
        self.assertEqual(failure.exception.progress["sent_segments"],1)
        self.assertEqual(failure.exception.progress["total_segments"],2)
        self.assertFalse(Path(download).exists())
        self.assertEqual(self.state.dispatch_send.call_count,2)

    def test_partial_inside_single_bubbled_text_is_not_hidden(self):
        def dispatch(*args,**kwargs):
            self.state.sender_instance._last_send={"status":"partial","sent_parts":1,"timestamp":time.time()}
            return False
        self.state.dispatch_send.side_effect=dispatch
        with self.assertRaises(self.protocol.SendFailure) as failure:
            self.send([text()])
        self.assertTrue(failure.exception.progress["partial"])
        self.assertEqual(failure.exception.progress["confirmed_parts"],1)
        self.state.dispatch_send.assert_called_once()

    def test_stale_shared_or_thread_local_partial_is_not_this_request(self):
        self.state.sender_instance._last_send={"status":"partial","sent_parts":2,"result_sequence":1}
        self.state.dispatch_send.side_effect=lambda *a,**k:False
        self.assertFalse(self.send([text()]))
        # A different thread's shared result is never a substitute for the getter.
        self.state.sender_instance.last_send_result=lambda:{}
        self.assertFalse(self.send([text()]))

    def test_partial_api_result_has_message_id_echo_and_metadata(self):
        caller=self.image()
        self.state.dispatch_send.side_effect=[True,False]
        response=inline(self.protocol._handle_ob_api({"action":"send_private_msg","echo":0,"params":{
            "user_id":1,"message":[text(),{"type":"image","data":{"file":caller}}]}},generation=456))
        self.assertEqual(response["status"],"failed")
        self.assertEqual(response["echo"],0)
        self.assertIsInstance(response["data"]["message_id"],int)
        self.assertTrue(response["data"]["partial"])
        self.assertEqual(response["data"]["sent_segments"],1)
        self.assertIn("不要整体重试",response["message"])

    def test_cancel_after_first_segment_is_explicit_partial(self):
        caller=self.image()
        def dispatch(*args,**kwargs):
            self.state.send_cancel.set()
            return True
        self.state.dispatch_send.side_effect=dispatch
        with self.assertRaises(self.protocol.SendFailure) as failure:
            self.send([text(),{"type":"image","data":{"file":caller}}])
        self.assertTrue(failure.exception.progress["partial"])
        self.assertEqual(failure.exception.progress["sent_segments"],1)
        self.state.dispatch_send.assert_called_once()

    def test_windows_file_uri_forms_preserve_existing_resolver(self):
        caller=self.image()
        for prefix in ("file://","file:///"):
            resolved,owned=self.protocol._resolve_media(prefix+caller.replace("\\","/"),image=True)
            self.assertEqual(Path(resolved),Path(caller))
            self.assertFalse(owned)


    def _timing_context(self, started=9.7, cancel_on_sleep=False):
        human = load("protocol_actual_humanize", "humanize.py")
        wait = human.wait_remaining
        now, sleeps = [10.0], []
        def sleep(seconds):
            sleeps.append(seconds)
            now[0] += seconds
            if cancel_on_sleep:
                self.state.paused.set()
        human.wait_remaining = lambda start,minimum,cancelled:wait(start,minimum,cancelled,clock=lambda:now[0],sleeper=sleep)
        self.state.pop_response_started = lambda key,generation:started
        patcher = patch.dict(sys.modules, {"humanize":human})
        patcher.start(); self.addCleanup(patcher.stop)
        timer = patch.object(self.protocol.time, "monotonic", side_effect=lambda:now[0])
        timer.start(); self.addCleanup(timer.stop)
        return now, sleeps

    def test_fast_response_floor_and_bubble_gap_spend_only_remaining_time(self):
        now, sleeps = self._timing_context()
        sends = []
        self.state.dispatch_send.side_effect=lambda *args,**kwargs:sends.append(now[0]) or True
        self.assertTrue(self.send([text("first"), {"type":"reply","data":{"id":"original"}}, text("second")]))
        self.assertAlmostEqual(sends[0],10.5)
        self.assertAlmostEqual(sends[1],10.75)
        self.assertAlmostEqual(sum(sleeps),0.75)

    def test_model_that_already_took_time_does_not_sleep_an_extra_floor(self):
        now, sleeps = self._timing_context(started=8.0)
        self.assertTrue(self.send([text()]))
        self.assertEqual(sleeps,[])
        self.assertEqual(now[0],10.0)

    def test_cancellation_during_floor_prevents_all_sends(self):
        _, sleeps = self._timing_context(cancel_on_sleep=True)
        self.assertFalse(self.send([text()]))
        self.state.dispatch_send.assert_not_called()
        self.assertLessEqual(sum(sleeps),0.05)

    def test_cancellation_between_bubbles_reports_partial_without_tail_send(self):
        _, sleeps = self._timing_context(started=8.0,cancel_on_sleep=True)
        delivery={}
        with self.assertRaises(self.protocol.SendFailure):
            self.send([text("first"), {"type":"reply","data":{"id":"original"}}, text("second")],delivery)
        self.assertEqual(self.state.dispatch_send.call_count,1)
        self.assertTrue(delivery["partial"])
        self.assertEqual(delivery["sent_segments"],1)
        self.assertLessEqual(sum(sleeps),0.05)

    def test_successful_assistant_log_uses_original_session_and_never_extracts_user_facts(self):
        self._timing_context(started=8.0)
        memory = Mock()
        self.state.memory_store=memory
        self.state.identity_registry.get_private_record=lambda _: {"name":"renamed friend","session_id":"wxid_original"}
        self.assertTrue(self.send([text("first"), {"type":"reply","data":{"id":"original"}}, text("second")]))
        memory.add_chat.assert_called_once()
        args,kwargs=memory.add_chat.call_args
        self.assertEqual(args, ("first\nsecond","wxid_original"))
        self.assertEqual(kwargs["role"],"assistant")
        memory.extract_memories_async.assert_not_called()

if __name__=="__main__":
    unittest.main()
