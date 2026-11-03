"""OneBot v11 API dispatch, durable incoming events and checked outgoing jobs."""
import asyncio
import base64
import hashlib
import itertools
import json
import logging
import math
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import wave
import importlib.util
from pathlib import Path
from urllib.parse import unquote, urlsplit
import config
import state
import name_refresh
from sticker_map import resolve_face
from tts import synthesize_voice

log = logging.getLogger("ob11-bridge")
_msg_id = itertools.count(int(time.time()*1000))
_initialize_lock = threading.Lock()
_UNPREPARED = object()


def _delivery_context_module():
    # Source-loaded validation harnesses do not put the bridge on sys.path.
    # Use the canonical module name so bridge_core and this boundary share state.
    name = "delivery_context"
    if name not in sys.modules:
        with _initialize_lock:
            if name not in sys.modules:
                spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name("delivery_context.py"))
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                sys.modules[name] = module
    return sys.modules[name]


def _note_delivery(target_id, is_group, method, status, text="", request=None, operation_id=""):
    """Observe the existing sending boundary; never infer delivery from a plan."""
    kind = {"send_text": "text", "send_voice": "voice", "send_image": "image", "send_sticker": "sticker"}.get(method)
    if not kind:
        return
    request = request or {}
    session_id = request.get("session_id")
    if not session_id:
        identity = state.identity_registry.get_group_record(target_id) if is_group else state.identity_registry.get_private_record(target_id)
        session_id = (identity or {}).get("session_id") or (identity or {}).get("username")
    if not session_id:
        return
    _delivery_context_module().record(session_id, kind, status, text=text,
        turn_id=request.get("request_id", ""), message_id=request.get("request_message_id", ""),
        actor_id=request.get("source_actor_id", "") or request.get("mode", {}).get("source_actor_id", ""),
        operation_id=operation_id)


def _sender_result_snapshot():
    """Read cached metadata only; never run another desktop diagnostic scan."""
    getter = getattr(state.sender_instance, "last_send_result", None)
    return getter, dict(getter() or {}) if callable(getter) else {}


def _record_partial_delivery(getter, previous, method, target_id, is_group, progress, request=None, operation_id=""):
    """Keep fresh, sender-confirmed parts of an otherwise unconfirmed operation."""
    result = dict(getter() or {}) if callable(getter) else {}
    changed = result.get("result_sequence") != previous.get("result_sequence") if "result_sequence" in result else result != previous
    if not changed or result.get("status") != "partial":
        return []
    parts = max(0, _as_int(result.get("sent_parts")))
    progress["confirmed_parts"] = progress.get("confirmed_parts", 0) + parts
    progress["partial"] = bool(progress.get("partial") or parts)
    if request and parts:
        request["confirmed_parts"] = request.get("confirmed_parts", 0) + parts
        request["last_sent_at"] = time.monotonic()
        if method == "send_text":
            request["text_delivered"] = True
    bubbles = result.get("confirmed_texts")
    if method != "send_text" or not isinstance(bubbles, list) or not all(isinstance(b, str) for b in bubbles) or len(bubbles) > parts:
        return []
    for index, bubble in enumerate(bubbles):
        _note_delivery(target_id, is_group, method, "confirmed", text=bubble, request=request,
            operation_id=f"{operation_id}:part:{index}" if operation_id else "")
    return list(bubbles)


def _note_text_timing(request, is_group, text, started, completed):
    """Measure one bound turn; sender confirmation does not prove remote delivery."""
    if not request:
        return
    received = request.get("started")
    if type(received) not in (int, float) or not math.isfinite(received):
        return
    first = not request.get("_first_text_timing_logged")
    request["_first_text_timing_logged"] = True
    turn = hashlib.sha256(str(request["request_id"]).encode()).hexdigest()[:12]
    send_seconds = max(0.0, completed - started)
    sample = getattr(state, "note_performance_sample", None)
    if first and callable(sample) and received <= started <= completed:
        sample("first_text", completed - received)
    log.info("[耗时] turn=%s stage=%s chat=%s chars=%d seconds=%.3f received_to_send_seconds=%.3f send_seconds=%.3f result=sender_confirmed",
             turn, "reply_first_text_done" if first else "reply_text_done", "group" if is_group else "private",
             len(str(text)), max(0.0, completed - received) if first else send_seconds,
             max(0.0, started - received), send_seconds)


def _note_voice_prepare(request, is_group, text, started, completed, result):
    turn = hashlib.sha256(str(request["request_id"]).encode()).hexdigest()[:12]
    sample = getattr(state, "note_performance_sample", None)
    if result == "ready" and callable(sample) and completed >= started:
        sample("voice_prepare", completed - started)
    log.info("[耗时] turn=%s stage=voice_synthesis chat=%s chars=%d seconds=%.3f result=%s",
             turn, "group" if is_group else "private", len(text), max(0.0, completed-started), result)


def _note_voice_send(request, is_group, text, queued, started, completed):
    received = request.get("started")
    if type(received) not in (int, float) or not math.isfinite(received):
        return
    turn = hashlib.sha256(str(request["request_id"]).encode()).hexdigest()[:12]
    sample = getattr(state, "note_performance_sample", None)
    if callable(sample) and received <= started <= completed:
        sample("first_voice", completed - received)
        if queued <= started:
            sample("voice_queue", started - queued)
    log.info("[耗时] turn=%s stage=reply_voice_done chat=%s chars=%d seconds=%.3f received_to_send_seconds=%.3f queue_seconds=%.3f send_seconds=%.3f result=sender_confirmed",
             turn, "group" if is_group else "private", len(text), max(0.0, completed-received),
             max(0.0, started-received), max(0.0, started-queued), max(0.0, completed-started))


class PendingVoice:
    # A complete queue job may accept voice text without having sent audio yet.
    # This truthy result is acceptance, and deliberately not a 'sent' queue state.
    delivery_state = "voice_pending"


class PartialReply:
    delivery_state = "partial"


_AUDIO_PROBE = r"""
import hashlib, math, sys
from mutagen import File
from mutagen.mp3 import MP3
from mutagen.oggvorbis import OggVorbis
from mutagen.oggopus import OggOpus
from mutagen.flac import FLAC
metadata = File(sys.argv[1], options=(MP3, OggVorbis, OggOpus, FLAC))
if metadata is None:
    raise ValueError('unknown compressed audio')
info = metadata.info
length = float(info.length)
rate = int(getattr(info, 'sample_rate', 48000))
channels = int(getattr(info, 'channels', 2))
if not math.isfinite(length) or not 0 < length <= 60 or not 1 <= rate <= 192000 or not 1 <= channels <= 8 or length * rate * channels * 4 > float(sys.argv[2]):
    raise ValueError('audio duration or decoded size limit')
import pygame
native_md5 = isinstance(metadata, FLAC) and info.bits_per_sample in (8, 16) and channels in (1, 2)
pygame.mixer.init(frequency=rate if native_md5 else 16000,
                  size=-info.bits_per_sample if native_md5 else -16,
                  channels=channels if native_md5 else 1, allowedchanges=0)
try:
    sound = pygame.mixer.Sound(sys.argv[1])
    duration = float(sound.get_length())
    if not math.isfinite(duration) or not 0 < duration <= 60:
        raise ValueError('decoded duration limit')
    if isinstance(metadata, FLAC) and abs(duration - length) > 2 / pygame.mixer.get_init()[0] + 0.000001:
        raise ValueError('FLAC decoded sample count does not match STREAMINFO')
    if native_md5:
        raw = sound.get_raw()
        if len(raw) != info.total_samples * channels * (info.bits_per_sample // 8):
            raise ValueError('FLAC native sample count mismatch')
        signature = getattr(info, 'md5_signature', 0)
        if signature and hashlib.md5(raw).digest() != signature.to_bytes(16, 'big'):
            raise ValueError('FLAC native PCM MD5 mismatch')
    # SDL converts 24/32-bit FLAC to reduced precision; those retain duration
    # validation without claiming an original-PCM checksum comparison.
    print(duration)
finally:
    pygame.mixer.quit()
"""


class SendFailure(RuntimeError):
    def __init__(self, message, progress):
        self.progress = dict(progress)
        if self.progress.get("partial"):
            message = f"{message}；已有部分内容被界面接受，请核对会话，不要整体重试"
        super().__init__(message)


def _cleanup_owned(paths):
    for path in paths:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        except OSError:
            log.warning("临时附件清理失败: %s", path)

def _as_int(value, default=0):
    try:
        return int(value)
    except (TypeError,ValueError):
        return default

def _valid_integer_id(value, minimum=1):
    # CQ codes use decimal strings; JSON IDs must not silently truncate floats
    # or interpret True as user 1.
    if isinstance(value,bool):
        return False
    if isinstance(value,int):
        return value>=minimum
    return isinstance(value,str) and bool(re.fullmatch(r"[0-9]+",value.strip())) and int(value)>=minimum

def _unescape(text):
    return str(text).replace("&#91;", "[").replace("&#93;", "]").replace("&#44;", ",").replace("&amp;", "&")

def _normalize_message(message, auto_escape=False):
    if isinstance(message, dict):
        return _normalize_message([message],auto_escape)
    if isinstance(message, list):
        if any(not isinstance(x,dict) or not isinstance(x.get("data",{}),dict) for x in message):
            raise ValueError("消息段格式无效")
        return message
    if not isinstance(message,str):
        raise ValueError("message 必须是文本或消息段")
    if auto_escape:
        return [{"type":"text","data":{"text":message}}] if message else []
    result, end = [], 0
    for match in re.finditer(r"\[CQ:([a-z_]+)((?:,[^\]]*)?)\]", message):
        if match.start()>end:
            result.append({"type":"text","data":{"text":_unescape(message[end:match.start()])}})
        fields = {}
        for pair in match.group(2).lstrip(",").split(","):
            if "=" in pair:
                key,value=pair.split("=",1)
                fields[key]=_unescape(value)
        result.append({"type":match.group(1),"data":fields})
        end=match.end()
    if end<len(message):
        result.append({"type":"text","data":{"text":_unescape(message[end:])}})
    return result

def _resolve_contact(target_id, is_group=False):
    record = state.identity_registry.get_group_record(target_id) if is_group else state.identity_registry.get_private_record(target_id)
    if record and record.get("name"):
        return record["name"]
    legacy = state._ob_id_to_contact.get(target_id)
    if legacy and str(legacy)!=str(target_id):
        return legacy
    raise ValueError("未知联系人 ID，等待联系人同步后重试")

def _decode_base64_file(data, suffix=".png"):
    limit = int(config.MEDIA_MAX_MB*1024*1024)
    if len(data)>limit*4/3+8:
        raise ValueError("附件超过大小限制")
    decoded=base64.b64decode(data, validate=True)
    if not decoded or len(decoded)>limit:
        raise ValueError("附件为空或过大")
    fd,path=tempfile.mkstemp(prefix="akasha-owned-",suffix=suffix)
    try:
        with os.fdopen(fd,"wb") as f:
            f.write(decoded)
    except BaseException:
        _cleanup_owned((path,))
        raise
    return path

def _resolve_media(value, suffix=".png", image=False):
    value=str(value or "")
    if value.startswith("base64://"):
        path=_decode_base64_file(value[9:],suffix)
        if image:
            try:
                _validate_media_file(path, image=True)
            except Exception:
                os.unlink(path)
                raise ValueError("图片格式无效")
        return path,True
    if value.startswith(("http://","https://")):
        import media_util
        return media_util.download(value,os.path.join(config.DATA_DIR,"outgoing_media"),suffix,image=image),True
    if value.startswith("file://"):
        parsed=urlsplit(value)
        value=unquote(parsed.path)
        if re.match(r"^/[A-Za-z]:",value):
            value=value[1:]
        if re.fullmatch(r"[A-Za-z]:", parsed.netloc):
            value=parsed.netloc+value
        elif parsed.netloc and parsed.netloc.lower() != "localhost":
            value="//"+parsed.netloc+value
    candidates=[value] if os.path.isabs(value) else []
    if config.ASTRBOT_ATTACHMENTS and not os.path.isabs(value):
        root=os.path.realpath(config.ASTRBOT_ATTACHMENTS)
        for sub in ("", "wechat_images", "wechat_stickers", "wechat_voice"):
            path=os.path.realpath(os.path.join(root,sub,value))
            if os.path.commonpath((root,path))==root:
                candidates.append(path)
    for path in candidates:
        if os.path.isfile(path):
            if os.path.getsize(path)>config.MEDIA_MAX_MB*1024*1024:
                raise ValueError("附件超过大小限制")
            return path,False
    raise ValueError("找不到附件文件")

def _send_sticker_file(contact, keyword, target_id=0, is_group=False, generation=None, *, prepared=_UNPREPARED, dispatch=None):
    if not config.STICKER_ENABLED:
        return False
    import sticker_selector
    path=sticker_selector.select_sticker(keyword) if prepared is _UNPREPARED else prepared
    if not path:
        return False
    if dispatch:
        return bool(dispatch("send_sticker", sticker_path=path))
    return bool(state.dispatch_send("send_sticker",contact,sticker_path=path,target_id=target_id,is_group=is_group,generation=generation))

def _send_text_with_mode(contact,text,mode="text",sticker_keyword="",*,target_id=0,is_group=False,generation=None,prepared_audio=_UNPREPARED,prepared_sticker=_UNPREPARED,dispatch=None):
    def send(method,*args,**kwargs):
        return dispatch(method,*args,**kwargs) if dispatch else state.dispatch_send(method,contact,*args,target_id=target_id,is_group=is_group,generation=generation,**kwargs)
    mode=str(mode or "text").lower()
    if mode == "text_voice":
        mode = "voice"
    if mode in ("voice","text_voice") and not config.VOICE_ENABLED:
        mode="text"
    if mode=="sticker":
        # Text accompanies decoration so the user's actual answer stays complete.
        ok=send("send_text",text)
        if ok and prepared_sticker is not None:
            return _send_sticker_file(contact,sticker_keyword,target_id,is_group,generation,prepared=prepared_sticker,dispatch=dispatch)
        return ok
    if mode in ("voice","text_voice"):
        audio=synthesize_voice(text) if prepared_audio is _UNPREPARED else prepared_audio
        if audio and os.path.isfile(audio):
            return send("send_voice",audio)
        # Only a synthesis failure before a desktop send can safely fall back.
        return send("send_text",text)
    ok=send("send_text",text)
    if ok and mode=="text_sticker" and prepared_sticker is not None:
        return _send_sticker_file(contact,sticker_keyword,target_id,is_group,generation,prepared=prepared_sticker,dispatch=dispatch)
    return ok

def _validate_media_file(path, image=False):
    if not path or not os.path.isfile(path):
        raise ValueError("找不到附件文件")
    size=os.path.getsize(path)
    if size<1 or size>config.MEDIA_MAX_MB*1024*1024:
        raise ValueError("附件为空或超过大小限制")
    if image:
        try:
            from PIL import Image
            with Image.open(path) as source:
                if source.format not in ("JPEG","PNG","GIF","WEBP","BMP"):
                    raise ValueError("不支持的图片格式")
                if source.width<1 or source.height<1 or source.width*source.height>40_000_000:
                    raise ValueError("图片像素数量超过上限")
                source.verify()
            # JPEG verify() checks structure without decoding scan data. A
            # truncated scan must fail before the earlier text is dispatched.
            with Image.open(path) as source:
                source.load()
        except Exception as error:
            raise ValueError("图片格式无效") from error
    else:
        with open(path,"rb") as stream:
            header=stream.read(16)
        if header.startswith(b"RIFF") and header[8:12]==b"WAVE":
            try:
                with wave.open(path,"rb") as source:
                    frames=source.getnframes()
                    rate=source.getframerate()
                    expected=frames*source.getnchannels()*source.getsampwidth()
                    if frames<1 or rate<1 or frames/rate>60 or expected>config.MEDIA_MAX_MB*1024*1024 or len(source.readframes(frames))!=expected:
                        raise ValueError("语音为空、不完整或超过微信单次 60 秒上限")
            except (wave.Error,EOFError) as error:
                raise ValueError("WAV 语音格式无效") from error
        elif not (header.startswith((b"ID3",b"OggS",b"fLaC")) or len(header)>=2 and header[0]==0xff and header[1]&0xe0==0xe0):
            raise ValueError("语音文件需为 WAV、MP3、Ogg 或 FLAC")
        else:
            _validate_compressed_audio(path)


def _validate_compressed_audio(path):
    # SDL dummy never opens a real speaker or changes the parent's mixer/device.
    # Keep codecs isolated, bounded in time and reject duration before full decode.
    environment=dict(os.environ,SDL_AUDIODRIVER="dummy",PYGAME_HIDE_SUPPORT_PROMPT="1",
                     PYTHONPATH=os.pathsep.join(os.path.abspath(path or os.curdir) for path in sys.path if isinstance(path,str)))
    try:
        result=subprocess.run([sys.executable,"-c",_AUDIO_PROBE,os.path.abspath(path),str(128*1024*1024)],
                              stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,
                              env=environment,timeout=8,check=False,
                              creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0) if os.name=="nt" else 0)
        duration=float(result.stdout.strip())
        if result.returncode or not math.isfinite(duration) or not 0<duration<=60:
            raise ValueError("invalid decoded audio")
    except subprocess.TimeoutExpired as error:
        raise ValueError("压缩语音预检超时，已取消整条消息") from error
    except (ValueError,OSError) as error:
        raise ValueError("语音无法完整解码或超过 60 秒，请检查文件与音频依赖") from error


def _preflight_send(message, mode, keyword, cancelled):
    """Resolve every attachment before the first desktop operation, owning only downloads/decodes."""
    supported={"text","image","record","voice","face","reply","at"}
    merged=[]
    # Complete the cheap structural pass before any network or paid synthesis.
    for segment in message:
        if not isinstance(segment,dict) or not isinstance(segment.get("data",{}),dict):
            raise ValueError("消息段格式无效")
        kind=segment.get("type")
        if kind not in supported:
            raise ValueError(f"暂不支持发送消息段 {kind}")
        data=dict(segment.get("data",{}))
        for field in ("text","prompt","name","file","url"):
            if field in data and not isinstance(data[field],str):
                raise ValueError(f"消息段 {kind} 的 {field} 必须是文本")
        if kind=="text":
            data.setdefault("text","")
        value=data.get("file") or data.get("url")
        text=data.get("text") or data.get("prompt") or ""
        if kind=="image" and not value:
            raise ValueError("图片消息缺少文件或 URL")
        if kind in ("record","voice") and not (value or text.strip()):
            raise ValueError("语音消息缺少文件或合成文字")
        if kind in ("record","voice") and not config.VOICE_ENABLED and not text.strip():
            raise ValueError("语音发送已关闭")
        if kind=="face" and "id" in data and not _valid_integer_id(data["id"],0):
            raise ValueError("表情 ID 必须为非负整数")
        if kind=="face" and not value and not _valid_integer_id(data.get("id"),0):
            raise ValueError("表情消息缺少有效 face ID 或文件")
        if kind=="reply" and "id" in data and (isinstance(data["id"],bool) or not isinstance(data["id"],(str,int)) or not str(data["id"]).strip() or isinstance(data["id"],int) and data["id"]<1):
            raise ValueError("引用 ID 必须为非空文本或正整数")
        if kind=="reply" and not data.get("id") and not data.get("text"):
            raise ValueError("引用消息缺少 ID 或文字")
        if kind=="at" and "qq" in data and data["qq"]!="all" and not _valid_integer_id(data["qq"]):
            raise ValueError("@ 目标必须为正整数或 all")
        if kind=="at" and not data.get("name") and data.get("qq")!="all" and not _valid_integer_id(data.get("qq")):
            raise ValueError("@ 消息缺少有效目标或名称")
        if kind=="text" and merged and merged[-1]["kind"]=="text":
            merged[-1]["data"]["text"]+=data.get("text","")
        else:
            merged.append({"kind":kind,"data":data})
    plan,owned_paths=[],set()
    try:
        for item in merged:
            if cancelled():
                raise RuntimeError("发送准备已取消")
            kind,data=item["kind"],item["data"]
            value=data.get("file") or data.get("url")
            if kind=="text":
                text=data.get("text","")
                if not text:
                    continue
                item["mode"]=mode
                if mode not in ("voice","text_voice"):
                    mode="text"
            elif kind in ("image","face","record","voice"):
                if kind in ("record","voice") and not config.VOICE_ENABLED:
                    item["fallback_text"]=data.get("text") or data.get("prompt")
                elif kind=="face" and not value:
                    item["face_text"]=resolve_face(data["id"])
                else:
                    if kind in ("record","voice") and not value:
                        item["synthesize_text"]=data.get("text") or data.get("prompt")
                    else:
                        path,owned=_resolve_media(value,".wav" if kind in ("record","voice") else ".png",image=kind in ("image","face"))
                        if owned:
                            owned_paths.add(path)
                        # Owned images were fully checked by this resolution:
                        # base64 above, HTTP by media_util.download(image=True).
                        # Local caller files and all audio still need preflight.
                        if not (owned and kind in ("image","face")):
                            _validate_media_file(path,image=kind in ("image","face"))
                        item["path"]=path
            elif kind=="reply":
                if not data.get("text"):
                    continue
            elif kind=="at":
                item["at_text"]="@"+str(data.get("name") or ("全体成员" if data.get("qq")=="all" else state.identity_registry.get_member_name(_as_int(data["qq"]))))
            plan.append(item)
        if not plan:
            raise ValueError("消息没有可发送内容")
        # Invalid caller attachments must fail before a potentially billable TTS call.
        for item in plan:
            if cancelled():
                raise RuntimeError("发送准备已取消")
            if "synthesize_text" in item:
                spoken = item.pop("synthesize_text")
                item["path"]=synthesize_voice(spoken)
                if not item["path"]:
                    raise ValueError("语音合成失败")
                item["spoken_text"] = spoken
                _validate_media_file(item["path"])
            elif item["kind"]=="text":
                item_mode=item["mode"]
                if item_mode in ("voice","text_voice") and config.VOICE_ENABLED:
                    spoken = item["data"]["text"]
                    item["audio"]=synthesize_voice(spoken)
                    if item["audio"]:
                        item["spoken_text"] = spoken
                        _validate_media_file(item["audio"])
                elif item_mode in ("sticker","text_sticker") and config.STICKER_ENABLED:
                    import sticker_selector
                    item["sticker"]=sticker_selector.select_sticker(keyword)
                    if item["sticker"]:
                        _validate_media_file(item["sticker"],image=True)
        if cancelled():
            raise RuntimeError("发送准备已取消")
        return plan,owned_paths
    except BaseException:
        _cleanup_owned(owned_paths)
        raise


def _perform_send(params,target_id,is_group,message,generation,delivery=None,request=None):
    progress=delivery if delivery is not None else {}
    progress.update(partial=False,sent_segments=0,total_segments=0,sent_operations=0,confirmed_parts=0)
    if request:
        progress["stream_confirmed_operations"] = request.get("confirmed_operations",0)
        progress["partial"] = bool(request.get("confirmed_operations") or request.get("confirmed_parts"))
    cancel_event=state.send_cancel
    def cancelled():
        return not state.running or state.paused.is_set() or cancel_event.is_set() or generation!=state.bridge_generation or bool(request and not state.reply_request_active(request["request_id"]))
    if cancelled():
        return False
    final = bool(params.get("wechat_stream", {}).get("final")) if request else False
    if not request and getattr(config, "REQUIRE_REPLY_TICKET", False):
        raise ValueError("自动回复需要已核验的会话票据")
    if request and any(segment.get("type") in ("record", "voice") for segment in message):
        raise ValueError("自动回复不接受外部语音，由本轮主模型正文合成")
    if request and not message and final:
        ok = _finish_stream_voice(request, target_id, is_group, generation, progress, cancelled)
        if ok is True:
            progress["partial"] = False
        return ok
    name_refresh.refresh_target(target_id,is_group)
    contact=_resolve_contact(target_id,is_group)
    reply_ids=[str(seg.get("data",{}).get("id")) for seg in message if seg.get("type")=="reply" and seg.get("data",{}).get("id") is not None]
    with state.reply_state_lock:
        info=state.pending_reply_modes.get(target_id)
        pending_info=info
        # Legacy timed suppression is disabled: later questions still get full replies.
        stale=isinstance(info,dict) and (info.get("generation",generation)!=generation or time.time()-info.get("created_at",time.time())>120)
        mismatch=isinstance(info,dict) and reply_ids and str(info.get("request_message_id")) not in reply_ids
        if stale or mismatch:
            info=None
    if request:
        info = request.get("mode", {})
    receipt_request = request
    if not receipt_request and isinstance(info, dict):
        receipt_request = {"request_message_id": info.get("request_message_id", ""), "mode": info}
    mode=str((info.get("reply_type","text") if isinstance(info,dict) else info) or "text").lower()
    if mode == "text_voice":
        mode = "voice"
    keyword=info.get("sticker_keyword","") if isinstance(info,dict) else ""
    if request:
        texts = [segment.get("data", {}).get("text", "") for segment in message if segment.get("type") == "text"]
        if any(not isinstance(text, str) for text in texts):
            raise ValueError("消息段 text 的 text 必须是文本")
        request["texts"].extend(texts)
        if mode == "voice" and config.VOICE_ENABLED:
            # Keep the spoken script off the text channel, including mixed media packets.
            message = [segment for segment in message if segment.get("type") != "text"]
            if not message:
                progress["voice_pending"] = True
                progress["voice_state"] = "collecting"
                return _finish_stream_voice(request, target_id, is_group, generation, progress, cancelled) if final else PendingVoice()
        # A stream sentence is answer text now; decoration/voice is decided once at the tail.
        mode = "text"
    try:
        plan,owned_paths=_preflight_send(message,mode,keyword,cancelled)
    except Exception:
        # Preflight is atomic: even an earlier valid text has not been sent.
        # Record only the outcome and intended medium, never preparation errors.
        for segment in message:
            if not isinstance(segment, dict):
                continue
            kind = segment.get("type")
            method = None
            if kind in ("text", "at", "reply"):
                method = "send_voice" if kind == "text" and mode in ("voice", "text_voice") and config.VOICE_ENABLED else "send_text"
            elif kind in ("record", "voice"):
                method = "send_voice" if config.VOICE_ENABLED else "send_text"
            elif kind == "image":
                method = "send_image"
            elif kind == "face":
                data = segment.get("data", {})
                has_file = isinstance(data, dict) and (data.get("file") or data.get("url"))
                method = ("send_sticker" if config.STICKER_ENABLED else "send_image") if has_file else "send_text"
            if method:
                _note_delivery(target_id, is_group, method, "cancelled" if cancelled() else "failed", request=receipt_request)
        raise
    progress["total_segments"]=len(plan)
    with state.reply_state_lock:
        if not request and (stale or not mismatch) and state.pending_reply_modes.get(target_id)==pending_info:
            state.pending_reply_modes.pop(target_id,None)
    response_started = None
    last_sent_at = request.get("last_sent_at") if request else None
    confirmed_texts = []
    active_item = None
    def checked_send(method,*args,**kwargs):
        nonlocal response_started, last_sent_at
        if cancelled():
            return False
        import humanize
        if last_sent_at is None:
            response_started = request.get("started") if request else state.pop_response_started(target_id, generation)
            # Direct API sends also get a minimum interval, while ordinary
            # responses account for buffering, tools and inference already spent.
            if not humanize.wait_response_floor(response_started if response_started is not None else time.monotonic(), cancelled):
                return False
        elif not humanize.wait_remaining(last_sent_at, getattr(config, "HUMAN_BUBBLE_INTERVAL_SECONDS", 0.25), cancelled):
            return False
        result_getter, prior_result = _sender_result_snapshot()
        if request:
            kwargs["reply_request_id"] = request["request_id"]
            if method == "send_text":
                kwargs["stream_sentence"] = True
        send_started = time.monotonic()
        visible_text = ""
        if method == "send_text" and args:
            visible_text = args[0]
        elif method == "send_voice":
            visible_text = (active_item or {}).get("spoken_text", "")
        try:
            ok=state.dispatch_send(method,contact,*args,target_id=target_id,is_group=is_group,generation=generation,**kwargs)
        except Exception:
            _note_delivery(target_id, is_group, method, "unconfirmed", request=receipt_request)
            raise
        send_completed = time.monotonic()
        _note_delivery(target_id, is_group, method, "confirmed" if ok else "unconfirmed", text=visible_text, request=receipt_request)
        if ok:
            if method == "send_text" and not cancelled():
                _note_text_timing(request, is_group, args[0], send_started, send_completed)
            progress["sent_operations"]+=1
            if request:
                request["confirmed_operations"] = request.get("confirmed_operations",0) + 1
                progress["stream_confirmed_operations"] = request["confirmed_operations"]
            last_sent_at = time.monotonic()
            if request:
                request["last_sent_at"] = last_sent_at
            if method == "send_text" and args and isinstance(args[0], str):
                confirmed_texts.append(args[0])
                if request:
                    request["text_delivered"] = True
            elif method == "send_voice" and active_item:
                spoken = active_item.get("spoken_text")
                if isinstance(spoken, str) and spoken.strip() and spoken not in confirmed_texts:
                    confirmed_texts.append(spoken)
        else:
            confirmed_texts.extend(_record_partial_delivery(result_getter, prior_result, method,
                target_id, is_group, progress, request=receipt_request))
        progress["partial"]=bool(progress["sent_operations"] or progress["confirmed_parts"] or (request and (request.get("confirmed_operations") or request.get("confirmed_parts"))))
        return ok
    try:
        for item in plan:
            active_item = item
            if cancelled():
                if progress["partial"]:
                    raise SendFailure("发送已取消",progress)
                return False
            kind,data=item["kind"],item["data"]
            if kind=="text":
                ok=_send_text_with_mode(contact,data.get("text",""),item["mode"],keyword,target_id=target_id,is_group=is_group,generation=generation,
                                        prepared_audio=item.get("audio"),prepared_sticker=item.get("sticker"),dispatch=checked_send)
            elif kind in ("record","voice"):
                if not config.VOICE_ENABLED or "fallback_text" in item:
                    text=data.get("text") or data.get("prompt")
                    if not text:
                        raise ValueError("语音发送已关闭")
                    ok=checked_send("send_text",text)
                else:
                    ok=checked_send("send_voice",item["path"])
            elif kind in ("image","face"):
                if "face_text" in item:
                    ok=checked_send("send_text",item["face_text"])
                else:
                    method="send_sticker" if kind=="face" and config.STICKER_ENABLED else "send_image"
                    ok=checked_send(method,item["path"])
            else:
                ok=checked_send("send_text",item.get("at_text") or data.get("text",""))
            if not ok:
                if progress["partial"]:
                    raise SendFailure("UIA 未确认剩余内容发送完成",progress)
                return False
            progress["sent_segments"]+=1
        progress["partial"]=False
        if request and final:
            return _finish_stream_voice(request, target_id, is_group, generation, progress, cancelled)
        return True
    except Exception as error:
        if progress["partial"] and not isinstance(error,SendFailure):
            raise SendFailure(str(error),progress) from error
        raise
    finally:
        store = getattr(state, "memory_store", None)
        if store and callable(getattr(store, "add_chat", None)) and plan:
            record = state.identity_registry.get_group_record(target_id) if is_group else state.identity_registry.get_private_record(target_id)
            session_id = (request or {}).get("session_id") or (record or {}).get("session_id") or (record or {}).get("username")
            body = "\n".join(confirmed_texts)
            if session_id and body.strip():
                try:
                    store.add_chat(body, session_id, actor_id=getattr(config, "BOT_WXID", "") or "self", role="assistant")
                except Exception:
                    log.exception("[memory] 回复已发送，但助手对话记录未保存")
        _cleanup_owned(owned_paths)


_voice_slots = threading.BoundedSemaphore(2)
_reply_media_policy = None
_reply_media_stamp = None


def _decide_tail_media(request, text):
    global _reply_media_policy, _reply_media_stamp
    from media_policy import ReplyMediaPolicy
    options = dict(auto_sticker=getattr(config,"AUTO_STICKER_ENABLED",True),auto_voice=getattr(config,"AUTO_VOICE_ENABLED",False),
        sticker_cooldown=getattr(config,"STICKER_COOLDOWN_SECONDS",120),sticker_min_turns=getattr(config,"STICKER_MIN_TURNS",4),
        voice_cooldown=getattr(config,"VOICE_COOLDOWN_SECONDS",180),voice_min_turns=getattr(config,"VOICE_MIN_TURNS",6),
        voice_max_chars=getattr(config,"VOICE_MAX_CHARS",600),voice_min_chars=getattr(config,"VOICE_MIN_CHARS",24))
    stamp = tuple(sorted(options.items()))
    if _reply_media_policy is None or stamp != _reply_media_stamp:
        if _reply_media_policy is None:
            _reply_media_policy = ReplyMediaPolicy(**options)
        else:
            for name, value in options.items():
                setattr(_reply_media_policy,name,value)
        _reply_media_stamp = stamp
    info = request.get("mode", {})
    voice_allowed = config.VOICE_ENABLED and not request.get("text_delivered") and (info.get("image_requested") is not True or bool(info.get("explicit_voice")))
    return _reply_media_policy.decide(info.get("user_text",""),text,requested_mode=info.get("reply_type","text"),
        sticker_keyword=info.get("sticker_keyword",""),explicit_voice=bool(info.get("explicit_voice")),
        session_id=request["session_id"],request_id=request["request_id"],voice_enabled=voice_allowed,sticker_enabled=config.STICKER_ENABLED)


def _finish_stream_voice(request, target_id, is_group, generation, progress, cancelled):
    """Synthesize the main model's complete voice script only before text delivery."""
    if request.get("voice_scheduled"):
        return True
    request["voice_scheduled"] = True
    voice_operation = request["request_id"] + ":voice-tail"
    def note_voice(status):
        _note_delivery(target_id, is_group, "send_voice", status, request=request, operation_id=voice_operation)
    text = "".join(request.get("texts", []))
    if not text.strip() or cancelled():
        progress["voice_pending"] = False
        if not cancelled() and request.get("confirmed_operations"):
            return True  # Media-only stream already has a sender receipt.
        progress["voice_state"] = "cancelled" if cancelled() else "empty"
        if text.strip() and request.get("mode", {}).get("reply_type") in ("voice", "text_voice"):
            note_voice("cancelled")
        return False
    decision = _decide_tail_media(request, text)
    mode = decision["reply_type"]
    if mode == "text_voice":
        mode = "voice"
    if request.get("text_delivered") and mode == "voice":
        # A mode change or old stored plan cannot speak already sent text again.
        mode = "text"
    progress["media_decision"] = decision["reason"]
    def send_tail(method, value):
        if cancelled():
            return False
        import humanize
        started = request.get("last_sent_at")
        if started is None:
            if not humanize.wait_response_floor(request["started"], cancelled):
                return False
        elif not humanize.wait_remaining(started,getattr(config,"HUMAN_BUBBLE_INTERVAL_SECONDS",0.25),cancelled):
            return False
        if cancelled():
            return False
        result_getter, prior_result = _sender_result_snapshot()
        send_started = time.monotonic()
        ok = state.dispatch_send(method,_resolve_contact(target_id,is_group),value,target_id=target_id,is_group=is_group,
            generation=generation,reply_request_id=request["request_id"])
        _note_delivery(target_id, is_group, method, "confirmed" if ok else "unconfirmed",
            text=value if method == "send_text" else "", request=request,
            operation_id=request["request_id"] + ":tail:" + method)
        confirmed_words = [value] if ok and method == "send_text" else []
        if not ok:
            confirmed_words = _record_partial_delivery(result_getter, prior_result, method,
                target_id, is_group, progress, request=request, operation_id=request["request_id"] + ":tail:" + method)
        if ok:
            if method == "send_text" and not cancelled():
                _note_text_timing(request, is_group, value, send_started, time.monotonic())
            request["confirmed_operations"] = request.get("confirmed_operations",0) + 1
            progress["stream_confirmed_operations"] = request["confirmed_operations"]
            if method == "send_text":
                request["text_delivered"] = True
        store = getattr(state, "memory_store", None)
        if confirmed_words and store and callable(getattr(store, "add_chat", None)):
            try:
                store.add_chat("\n".join(confirmed_words), request["session_id"], actor_id=getattr(config, "BOT_WXID", "") or "self", role="assistant")
            except Exception:
                log.exception("[memory] 文字回复已发送，但助手对话记录未保存")
        return ok
    def send_prepared_image():
        # Only this request's internal mode can contain a generated path.
        # OneBot segments/metadata never supply or replace this field.
        path = request.get("mode", {}).pop("image_after_voice", None)
        if not path:
            return True
        if cancelled():
            progress["image_state"] = "cancelled"
            _note_delivery(target_id, is_group, "send_image", "cancelled", request=request,
                operation_id=request["request_id"] + ":image-tail")
            return False
        try:
            ok = state.dispatch_send("send_image", _resolve_contact(target_id, is_group), path,
                target_id=target_id, is_group=is_group, generation=generation, reply_request_id=request["request_id"])
        except Exception:
            ok = False
        progress["image_state"] = "sent" if ok else "unconfirmed"
        _note_delivery(target_id, is_group, "send_image", "confirmed" if ok else "unconfirmed", request=request,
            operation_id=request["request_id"] + ":image-tail")
        if ok:
            request["confirmed_operations"] = request.get("confirmed_operations", 0) + 1
            progress["stream_confirmed_operations"] = request["confirmed_operations"]
        else:
            progress["partial"] = True
            log.warning("[tools] 语音已处理，后续图片发送未确认，不重发语音或图片")
            note = getattr(state, "record_activity", None)
            if callable(note):
                note("error", "回复已部分发送，后续图片未确认；不自动重发", image_state="unconfirmed")
        return ok
    if not request.get("text_delivered") and mode not in ("voice","text_voice"):
        # Unsafe spoken content falls back to the complete ordinary text once.
        if not send_tail("send_text", text):
            return False
        request["text_delivered"] = True
    if mode in ("sticker","text_sticker"):
        import sticker_selector
        path = sticker_selector.select_sticker(decision["sticker_keyword"])
        if path:
            return send_tail("send_sticker",path)
        return True
    if mode not in ("voice","text_voice") or not config.VOICE_ENABLED:
        return True if send_prepared_image() else PartialReply()
    progress["voice_pending"] = mode == "voice"
    if not _voice_slots.acquire(blocking=False):
        progress["voice_state"] = "busy_text_fallback"
        progress["voice_pending"] = False
        if mode == "voice":
            if not send_tail("send_text", text):
                return False
            return True if send_prepared_image() else PartialReply()
        return True
    progress["voice_state"] = "synthesizing"
    request["voice_state"] = "synthesizing"
    def synthesize_then_enqueue():
        try:
            from tts import synthesize_reply_voice
            if cancelled():
                progress["voice_state"] = "cancelled"; return
            synthesis_started = time.monotonic()
            try:
                audio = synthesize_reply_voice(text, cancelled=cancelled)
            except Exception:
                _note_voice_prepare(request,is_group,text,synthesis_started,time.monotonic(),"failed")
                raise
            stale = cancelled()
            _note_voice_prepare(request,is_group,text,synthesis_started,time.monotonic(),"cancelled" if stale else "ready" if audio else "failed")
            if stale:
                progress["voice_state"] = "cancelled"; return
            note_voice("generated" if audio else "failed")
            def deliver():
                if cancelled():
                    return False
                import humanize
                started = request.get("last_sent_at")
                if started is None:
                    if not humanize.wait_response_floor(request["started"], cancelled):
                        return False
                elif not humanize.wait_remaining(started, getattr(config,"HUMAN_BUBBLE_INTERVAL_SECONDS",0.25),cancelled):
                    return False
                if cancelled():
                    progress["voice_state"] = "cancelled"; return False
                method, value = ("send_voice", audio) if audio else ("send_text", text)
                result_getter, prior_result = _sender_result_snapshot()
                send_started = time.monotonic()
                ok = state.dispatch_send(method,_resolve_contact(target_id,is_group),value,target_id=target_id,is_group=is_group,
                    generation=generation,reply_request_id=request["request_id"])
                _note_delivery(target_id, is_group, method, "confirmed" if ok else "unconfirmed", text=text,
                    request=request, operation_id=voice_operation if audio else request["request_id"] + ":text-fallback")
                if ok and not audio:
                    request["text_delivered"] = True
                confirmed_words = [text] if ok else _record_partial_delivery(result_getter, prior_result,
                    method, target_id, is_group, progress, request=request,
                    operation_id=voice_operation if audio else request["request_id"] + ":text-fallback")
                send_completed = time.monotonic()
                progress["voice_state"] = ("sent" if audio else "text_fallback_sent") if ok else "unconfirmed"
                if ok and not cancelled():
                    if method == "send_text":
                        _note_text_timing(request, is_group, value, send_started, send_completed)
                    else:
                        _note_voice_send(request,is_group,text,queued_at,send_started,send_completed)
                if ok:
                    request["confirmed_operations"] = request.get("confirmed_operations",0) + 1
                    progress["stream_confirmed_operations"] = request["confirmed_operations"]
                    progress["voice_pending"] = False
                # Voice-only responses become memory only once actually sent.
                if confirmed_words and mode == "voice" and state.memory_store:
                    try:
                        state.memory_store.add_chat("\n".join(confirmed_words),request["session_id"],actor_id=getattr(config,"BOT_WXID","") or "self",role="assistant")
                    except Exception:
                        # Persistence cannot undo the confirmed voice or prevent
                        # this same reply's already prepared picture delivery.
                        log.exception("[memory] 语音回复已发送，但助手对话记录未保存")
                if ok and not send_prepared_image():
                    return PartialReply()
                return ok
            queue = state.send_queue
            if queue is not None:
                voice_id = next(_msg_id)
                progress["voice_state"] = "queued"
                request["voice_state"] = "queued"
                queued_at = time.monotonic()
                future = queue.submit(voice_id,deliver,cancelled)
                request["voice_future"] = future
                progress["voice_message_id"] = voice_id
                def voice_completed(done):
                    try:
                        result = done.result()
                    except Exception:
                        progress["voice_state"] = "queue_rejected"
                    else:
                        if not result and progress.get("voice_state") not in ("unconfirmed","failed"):
                            progress["voice_state"] = "cancelled"
                    progress["voice_pending"] = False
                    request["voice_state"] = progress.get("voice_state")
                    if progress.get("voice_state") in ("queue_rejected","cancelled","unconfirmed"):
                        if audio:
                            note_voice("unconfirmed" if progress["voice_state"] == "unconfirmed" else "cancelled")
                        note = getattr(state,"record_activity",None)
                        if callable(note):
                            note("error","回复语音未完成，已停止且不自动重发",voice_state=progress["voice_state"])
                future.add_done_callback(voice_completed)
            else:
                progress["voice_state"] = "cancelled"
        except Exception:
            progress["voice_state"] = "failed"
            log.exception("流式回复语音准备失败，文字回复已保留")
        finally:
            if progress.get("voice_state") in ("cancelled", "failed"):
                note_voice("failed" if progress["voice_state"] == "failed" else "cancelled")
            if progress.get("voice_state") != "queued":
                progress["voice_pending"] = False
            future = request.get("voice_future")
            if future is None or future.done():
                request["voice_state"] = progress.get("voice_state")
            _voice_slots.release()
    threading.Thread(target=synthesize_then_enqueue,daemon=True,name="reply-voice").start()
    return PendingVoice() if mode == "voice" else True

def _registry_list(which):
    registry=state.identity_registry
    with registry._lock:
        return [(int(k),dict(v)) for k,v in getattr(registry,which).items()]

async def _handle_ob_api(data, ws=None, generation=None):
    """Reply only with a verified result or an explicit queued (async) status."""
    response={"status":"failed","retcode":1400,"data":None}
    result,delivery={},{}
    action = "invalid"
    if isinstance(data,dict) and "echo" in data:
        response["echo"]=data["echo"]
    try:
        with state.run_lock:
            if generation is not None and generation!=state.client_generation:
                raise RuntimeError("旧连接请求已取消")
            run_generation=state.bridge_generation
            cancel_event=state.send_cancel
        if not isinstance(data,dict) or not isinstance(data.get("params",{}),dict):
            raise ValueError("请求格式无效")
        raw_action=str(data.get("action", ""))
        asynchronous=raw_action.endswith("_async")
        action=raw_action.removesuffix("_async")
        params=data.get("params",{})
        result={}
        if action in ("send_msg","send_private_msg","send_group_msg"):
            if not state.running or state.paused.is_set() or state.sender_instance is None:
                raise ValueError("桥接已停止、暂停或发送器尚未就绪")
            is_group=action=="send_group_msg" or (action=="send_msg" and (params.get("message_type")=="group" or "group_id" in params))
            target_value=params.get("group_id" if is_group else "user_id")
            if not _valid_integer_id(target_value):
                raise ValueError("目标 ID 必须为正整数")
            target_id=int(target_value)
            _resolve_contact(target_id,is_group)
            message=_normalize_message(params.get("message",[]),params.get("auto_escape") is True)
            stream = params.get("wechat_stream")
            if stream is None and getattr(config, "REQUIRE_REPLY_TICKET", False):
                raise ValueError("自动回复需要已核验的会话票据")
            if stream is not None and (not isinstance(stream,dict) or type(stream.get("final")) is not bool):
                raise ValueError("流式完成标识无效")
            if not message and not (stream is not None and stream.get("final")):
                raise ValueError("消息为空")
            message_id=next(_msg_id)
            with state.run_lock:
                if run_generation!=state.bridge_generation or cancel_event.is_set() or not state.running:
                    raise RuntimeError("发送请求已取消")
                if state.send_queue is None:
                    from send_queue import SendQueue
                    state.send_queue=SendQueue(config.SEND_QUEUE_LIMIT)
                cancelled = lambda:cancel_event.is_set() or not state.running or state.paused.is_set() or run_generation!=state.bridge_generation
                if stream is not None:
                    def submit(record):
                        packet_delivery = {}
                        packet_id = message_id
                        def stream_cancelled():
                            return cancelled() or not state.reply_request_active(record["request_id"])
                        def perform_stream():
                            try:
                                ok = _perform_send(params,target_id,is_group,message,run_generation,packet_delivery,record)
                                if not ok:
                                    record["aborted"] = True
                                return ok
                            except BaseException:
                                record["aborted"] = True
                                raise
                        packet_future = state.send_queue.submit(packet_id,perform_stream,stream_cancelled)
                        return {"future":packet_future,"message_id":packet_id,"delivery":packet_delivery,
                                "payload":json.dumps([message,stream.get("final")],ensure_ascii=False,sort_keys=True)}
                    record, packet, duplicate = state.claim_reply_segment(stream,target_id,is_group,run_generation,submit)
                    if packet["payload"] != json.dumps([message,stream.get("final")],ensure_ascii=False,sort_keys=True):
                        raise ValueError("同一流式序号的消息内容不得更改")
                    future, message_id, delivery = packet["future"], packet["message_id"], packet["delivery"]
                    if duplicate:
                        delivery["duplicate"] = True
                else:
                    future=state.send_queue.submit(message_id,lambda:_perform_send(params,target_id,is_group,message,run_generation,delivery),cancelled)
            result={"message_id":message_id}
            if future.done() and future.exception():
                raise future.exception()
            if asynchronous:
                response.update(status="async",retcode=1,data=result)
            else:
                try:
                    ok=await asyncio.wait_for(asyncio.shield(asyncio.wrap_future(future)),config.API_SEND_WAIT_SECONDS)
                except asyncio.TimeoutError:
                    response.update(status="async",retcode=1,data=result)
                else:
                    voice_pending = bool(ok and delivery.get("voice_pending") and delivery.get("voice_state") in ("collecting","synthesizing","queued"))
                    voice_failed = delivery.get("voice_state") in ("queue_rejected","cancelled","unconfirmed","failed","empty")
                    response.update(status="async" if voice_pending else "ok" if ok and not voice_failed else "failed",retcode=1 if voice_pending else 0 if ok and not voice_failed else 1500,data={**result,**delivery}, **({} if ok and not voice_failed else {"message":"UIA 未确认发送完成"}))
        else:
            if action=="get_login_info":
                result={"user_id":state._self_id_int,"nickname":config.BOT_NICKNAMES[0] if config.BOT_NICKNAMES else "微信助手"}
            elif action=="get_version_info":
                result={"app_name":"微信bot","app_version":"7.0.0","protocol_version":"v11"}
            elif action=="get_status":
                result={"online":state.running and state._ob_ws_ready.is_set(),"good":state.running and not state.paused.is_set()}
            elif action=="get_friend_list":
                result=[{"user_id":k,"nickname":v.get("nickname") or v.get("name",""),"remark":v.get("remark","")} for k,v in _registry_list("private")]
            elif action=="get_group_list":
                result=[{"group_id":k,"group_name":v.get("name","")} for k,v in _registry_list("groups")]
            elif action=="get_group_info":
                key=_as_int(params.get("group_id")); record=state.identity_registry.get_group_record(key)
                if not record:
                    raise ValueError("未知群 ID")
                result={"group_id":key,"group_name":record["name"]}
            elif action in ("get_stranger_info","get_group_member_info","get_group_member_list"):
                group_key, group = None, None
                if action.startswith("get_group_member"):
                    if not _valid_integer_id(params.get("group_id")):
                        raise ValueError("群 ID 必须为正整数")
                    group_key = int(params["group_id"])
                    group = state.identity_registry.get_group_record(group_key)
                    if not group:
                        raise ValueError("未知群 ID")
                    members = [(key, value) for key, value in _registry_list("members")
                               if value.get("group_session") == group.get("session_id")]
                else:
                    # Incoming group users have group-scoped stable IDs. A
                    # stranger lookup must resolve that same ID, not a private
                    # chat ID reconstructed from the nickname or wxid.
                    members = _registry_list("private") + _registry_list("members")
                if action=="get_group_member_list":
                    result=[{"group_id":group_key,"user_id":key,"nickname":value.get("name",""),
                             "card":value.get("name",""),"role":"member"} for key,value in members]
                else:
                    if not _valid_integer_id(params.get("user_id")):
                        raise ValueError("用户 ID 必须为正整数")
                    key=int(params["user_id"])
                    if key == state._self_id_int:
                        record = {"name":config.BOT_NICKNAMES[0] if config.BOT_NICKNAMES else "微信助手"}
                    else:
                        record=next((value for known_id,value in members if known_id==key),None)
                    if not record:
                        raise ValueError("未知用户 ID")
                    result={"user_id":key,"nickname":record.get("nickname") or record.get("name","")}
                    if group is not None:
                        result.update(group_id=group_key,card=record.get("name","") or result["nickname"],role="member")
            elif action in ("can_send_image","can_send_record"):
                result={"yes":bool(state.sender_instance and (action=="can_send_image" or config.VOICE_ENABLED))}
            else:
                response.update(retcode=1404,message=f"不支持 API: {action}")
                result=None
            if result is not None:
                response.update(status="ok",retcode=0,data=result)
    except Exception as exc:
        response.update(status="failed",retcode=1500 if isinstance(exc,RuntimeError) else 1400,message=config.redact_text(exc),wording=config.redact_text(exc))
        if "message_id" in result:
            response["data"]={**result,**delivery,**(exc.progress if isinstance(exc,SendFailure) else {})}
        log.warning("OneBot API 失败: action=%s error=%s",action if re.fullmatch(r"[a-z_]{1,64}",action) else "invalid",exc)
    connection=ws if ws is not None else state._ob_ws
    if connection is not None:
        await connection.send(json.dumps(response,ensure_ascii=False))
    return response

def _extract_text(message):
    result=[]
    for seg in message:
        kind=seg.get("type"); data=seg.get("data",{})
        result.append(str(data.get("text", "")) if kind=="text" else resolve_face(data.get("id")) if kind=="face" else "[图片]" if kind=="image" else "[语音]" if kind in ("voice","record") else str(data.get("text", "")))
    return "".join(result).strip()

def make_message_event(message_type,user_id,message,group_id=0,group_name="",nickname=""):
    event={"time":int(time.time()),"self_id":state._self_id_int,"post_type":"message","message_id":next(_msg_id),
           "message_type":message_type,"sub_type":"normal" if message_type=="group" else "friend",
           "user_id":user_id,"message":message,"raw_message":_extract_text(message),
           "sender":{"user_id":user_id,"nickname":nickname or str(user_id)},"font":0}
    if message_type=="group":
        event.update(group_id=group_id,group_name=group_name or str(group_id))
    return event

def push_event(event):
    try:
        with _initialize_lock:
            if state.outbox is None:
                from outbox import Outbox
                state.outbox=Outbox(os.path.join(config.DATA_DIR,"outbox.db"),config.OUTBOX_MAX_EVENTS)
        accepted=state.outbox.put(event)
        if accepted:
            state.increment("received")
        else:
            log.error("离线队列已满，事件保留在桥接待重试缓冲")
        return accepted
    except Exception as exc:
        log.error("离线事件持久化失败: %s",exc)
        return False
