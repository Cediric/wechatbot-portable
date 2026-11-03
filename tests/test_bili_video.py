"""WeChat Bilibili shares. Network, model and WeChat are never used by these tests."""
import importlib.util
import json
from pathlib import Path
import struct
import sys
import tempfile
import threading
import types
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit


BRIDGE = Path(__file__).resolve().parents[1] / "wechat-weflow-bridge-ob11"
sys.path.insert(0, str(BRIDGE))
SPEC = importlib.util.spec_from_file_location("bili_video_test_target", BRIDGE / "bili_video.py")
bili = importlib.util.module_from_spec(SPEC)
with patch.dict(sys.modules, {"config": types.SimpleNamespace(MEDIA_MAX_MB=2)}):
    SPEC.loader.exec_module(bili)

BV = "BV17x411w7KC"
WATCH = "https://www.bilibili.com/video/" + BV
CDN = "https://cdn.example.bilivideo.com/video.m4s?secret=redacted"
FTYP = struct.pack(">I4s4s", 12, b"ftyp", b"iso5")
MDAT = struct.pack(">I4s4s", 12, b"mdat", b"abcd")


class Response:
    def __init__(self, status=200, body=b"", **headers):
        self.status_code = status
        self.body = body
        self.headers = headers
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True

    def iter_content(self, chunk):
        for start in range(0, len(self.body), chunk):
            yield self.body[start:start + chunk]


class Session:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.cookies = {}
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def get(self, url, **kw):
        self.calls.append((url, kw))
        return self.responses.pop(0)


def api(result):
    return Response(body=json.dumps({"code": 0, "data": result}).encode())


def view():
    return api({"bvid": BV, "title": "样片", "owner": {"name": "UP A"},
                "pages": [{"page": 1, "part": "A", "cid": 31, "duration": 4},
                          {"page": 2, "part": "B", "cid": 47, "duration": 5}]})


def play(url=CDN):
    return api({"dash": {"video": [{"id": 16, "baseUrl": url}]}})


class BiliVideoTests(unittest.TestCase):
    def test_native_card_and_text_parse_without_requests(self):
        for raw, expected in (
            (f"看看{BV}如何", WATCH),
            (f"【看一下】 {BV}?p=2", WATCH + "?p=2"),
            ("AV170001", "https://www.bilibili.com/video/av170001"),
            (f"m.bilibili.com/video/{BV}/?share=x&p=3", WATCH + "?p=3"),
            (f"http://www.bilibili.com/video/{BV}，好看", WATCH),
            ("b23.tv/a1B2xY3", "https://b23.tv/a1B2xY3"),
            (f"<appmsg><title>{BV}</title><url><![CDATA[https://b23.tv/Ab_19?p=2&amp;track=99]]></url></appmsg>",
             "https://b23.tv/Ab_19?p=2"),
        ):
            with self.subTest(raw=raw):
                self.assertEqual(bili.find_video(raw).url, expected)

    def test_ignore_spoofed_nested_or_unsupported_references(self):
        for raw in (
            f"https://ab23.tv/video/{BV}", f"notbilibili.com/video/{BV}",
            f"https://bilibili.com.evil.test/video/{BV}", f"https://www.bilibili.com@localhost/video/{BV}",
            f"https://u@www.bilibili.com/video/{BV}", f"ftp://www.bilibili.com/video/{BV}",
            "https://b23.tv:2741/a", "b23.tv/a/b", f"https://elsewhere.example/?redirect={WATCH}",
            "https://www.bilibili.com/read/cv12345", "https://live.bilibili.com/12345",
            f"user_{BV}", BV + "suffix", "xxAV170001", WATCH + "?p=0", WATCH + "?p=1&p=2",
            f"<appmsg><title>{BV}</title><url>https://other.test/shop</url></appmsg>",
        ):
            with self.subTest(raw=raw):
                self.assertIsNone(bili.find_video(raw))

    def test_miniapp_ids_bound_to_verified_weappinfo_and_exact_video_route(self):
        def xml(appid, pagepath, other=""):
            return (f"<msg><appmsg><title>{BV}</title><weappinfo><appid>{appid}</appid>"
                    f"<pagepath><![CDATA[{pagepath}]]></pagepath></weappinfo>{other}</appmsg></msg>")
        app = "wx7564fd5313d24844"
        for path, result in (
            ("pages/video/video?avid=545861869", "av545861869"),
            ("pages/video/video?bvid=1aq4y1j7zh&p=2", "BV1aq4y1j7zh?p=2"),
            ("/pages/video/video.html?bvid=BV1aq4y1j7zh&page=3", "BV1aq4y1j7zh?p=3"),
        ):
            card = xml(app, path)
            self.assertTrue(bili.is_bili_card(card))
            self.assertEqual(bili.find_video(card).url, "https://www.bilibili.com/video/" + result)
            self.assertEqual(bili.find_video(card), bili.miniapp_video(app, path))
            self.assertIsNone(bili.find_video(xml("wxother", path)))
            self.assertIsNone(bili.find_video(path))
        self.assertFalse(bili.is_bili_card(xml("wxother", "x", "<appid>"+app+"</appid>")))
        for path in ["pages/live/live?bvid="+BV, "https://foreign.example/pages/video/video?avid=123",
                     "pages/video/video?bvid=123", "pages/video/video?avid=1&avid=2", "pages/video/video?avid=9&p=0"]:
            self.assertTrue(bili.is_bili_card(xml(app, path)))
            self.assertIsNone(bili.find_video(xml(app, path)))

    def test_short_card_to_page_two_samples_downloaded_bytes_and_removes_input(self):
        redirects = Response(302, Location=WATCH + "?p=2&tracking=xx")
        stream = Response(body=FTYP + MDAT)
        s = Session(redirects, view(), play(), stream)
        sources = []

        def frames(path, cancelled=None):
            self.assertEqual(Path(path).read_bytes(), FTYP + MDAT)
            sources.append(path)
            return ["owned_01.jpg", "owned_02.jpg"]

        with patch.object(bili, "_session", return_value=s), patch.object(bili.multimodal, "video_frame_paths", side_effect=frames):
            note, images = bili.inspect_video(bili.find_video("https://b23.tv/abC"))
        self.assertEqual(len(images), 2)
        self.assertIn("样片", note)
        self.assertIn("无声音", note)
        self.assertIn("2 张", note)
        self.assertEqual(parse_qs(urlsplit(s.calls[2][0]).query)["cid"], ["47"])
        self.assertFalse(Path(sources[0]).exists())
        self.assertTrue(redirects.closed and stream.closed)
        self.assertTrue(all(c[1]["allow_redirects"] is False for c in s.calls))

    def test_short_link_preserves_explicit_page(self):
        s = Session(Response(302, Location=WATCH + "?p=1"), view(), play("http://127.0.0.1/a"))
        with patch.object(bili, "_session", return_value=s):
            note, images = bili.inspect_video(bili.find_video("https://b23.tv/Xy9?p=2"))
        self.assertEqual(parse_qs(urlsplit(s.calls[2][0]).query)["cid"], ["47"])
        self.assertEqual(images, [])
        self.assertIn("未取得视频画面", note)

    def test_short_redirect_checked_before_following(self):
        for target in ("http://127.0.0.1/b", "file:///C:/file", "https://www.bilibili.com:3344/x",
                       "https://cdn.bilivideo.com/test", "https://www.bilibili.com/redirect?u=https://ev.il"):
            with self.subTest(target=target):
                s = Session(Response(302, Location=target))
                with patch.object(bili, "_session", return_value=s):
                    note, frames = bili.inspect_video(bili.find_video("b23.tv/XY"))
                self.assertEqual(len(s.calls), 1)
                self.assertIn("未取得视频画面", note)
                self.assertFalse(frames)

    def test_invalid_media_urls_or_redirects_never_reach_transport(self):
        for url in ("https://a.bilivideo.com.evil.test/z", "https://127.0.0.1/z",
                    "https://a.bilivideo.com:8000/z", "https://userinfo@a.bilivideo.cn/z"):
            s = Session(view(), play(url))
            with self.subTest(url=url), patch.object(bili, "_session", return_value=s):
                note, frames = bili.inspect_video(bili.find_video(BV))
            self.assertFalse(frames)
            self.assertIn("样片", note)
            self.assertEqual(len(s.calls), 2)
        s = Session(view(), play(), Response(302, Location="http://169.254.169.254/"))
        with patch.object(bili, "_session", return_value=s):
            note, frames = bili.inspect_video(bili.find_video(BV))
        self.assertEqual(len(s.calls), 3)
        self.assertFalse(frames)
        self.assertNotIn("secret=", note)

    def test_absent_part_does_not_fall_back_to_first(self):
        s = Session(view())
        with patch.object(bili, "_session", return_value=s):
            note, frames = bili.inspect_video(bili.find_video(WATCH + "?p=3"))
        self.assertEqual(len(s.calls), 1)
        self.assertFalse(frames)
        self.assertIn("指定", note)

    def test_restricted_video_and_cancellation(self):
        s = Session(Response(body=b'{"code":-404}'))
        with patch.object(bili, "_session", return_value=s):
            note, frames = bili.inspect_video(bili.find_video(BV))
        self.assertIn("受限", note)
        self.assertFalse(frames)
        event = threading.Event()
        event.set()
        with patch.object(bili, "_session", return_value=Session()):
            note, frames = bili.inspect_video(bili.find_video(BV), event)
        self.assertIn("取消", note)
        self.assertEqual(frames, [])

    def test_html_or_playlist_disguised_as_video_is_never_opened_by_ffmpeg(self):
        s = Session(view(), play(), Response(body=b"#EXTM3U\nfile:///private"))
        with patch.object(bili, "_session", return_value=s), patch.object(bili.multimodal, "video_frame_paths") as ff:
            note, frames = bili.inspect_video(bili.find_video(BV))
        ff.assert_not_called()
        self.assertIn("MP4", note)
        self.assertFalse(frames)

    def test_range_download_retains_only_complete_dash_fragments(self):
        limit = 32
        s = Session(Response(206, FTYP + MDAT + struct.pack(">I4s", 25, b"moof"),
                             **{"Content-Range": "bytes 0-31/999999", "Content-Length": "32"}))
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "media.mp4"
            bili._download(s, CDN, True, path, limit, bili.time.monotonic() + 10, None)
            self.assertEqual(path.read_bytes(), FTYP + MDAT)
        self.assertEqual(s.calls[0][1]["headers"]["Range"], "bytes=0-31")

    def test_invalid_range_or_oversized_metadata_rejected(self):
        s = Session(view(), play(), Response(206, FTYP, **{"Content-Range": "bytes 40-55/56"}))
        with patch.object(bili, "_session", return_value=s):
            note, frames = bili.inspect_video(bili.find_video(BV))
        self.assertFalse(frames)
        self.assertIn("范围", note)
        s = Session(Response(body=b"x" * (1048576 + 1)))
        with patch.object(bili, "_session", return_value=s):
            note, frames = bili.inspect_video(bili.find_video(BV))
        self.assertFalse(frames)
        self.assertIn("上限", note)

    def test_feed_only_canonical_public_video_rows_bounded_and_unique(self):
        accepted = [{"bvid": f"BV1{i:09d}", "title": f"第{i}条"} for i in range(9)]
        rows = [dict(accepted[-1], is_ad=True), dict(accepted[-1], is_live=True),
                dict(accepted[-1], goto="live"), dict(accepted[-1], state=-2),
                {"bvid": "https://ev.il", "title": "redirect"}, accepted[0], *accepted]
        s = Session(api({"list": rows}))
        with patch.object(bili, "_session", return_value=s):
            feed = bili.public_feed()
        self.assertEqual(feed, [(f"https://www.bilibili.com/video/BV1{i:09d}", f"第{i}条") for i in range(6)])
        self.assertEqual(urlsplit(s.calls[0][0]).hostname, "api.bilibili.com")
        with patch.object(bili, "_session", return_value=Session(Response(403))):
            self.assertEqual(bili.public_feed(), [])

    def test_anonymous_search_keeps_only_video_results_and_rejects_identifiers(self):
        s = Session(api({"result":[{"bvid": BV, "type":"video", "title":"<em class=\"keyword\">街头摄影</em>"},
                                    {"bvid":"BV1aq4y1j7zh", "type":"live", "title":"直播"}]}))
        with patch.object(bili, "_session", return_value=s):
            self.assertEqual(bili.public_search("街头摄影", page=2), [(WATCH, "街头摄影")])
        parsed = urlsplit(s.calls[0][0])
        self.assertEqual((parsed.hostname,parsed.path), ("api.bilibili.com", "/x/web-interface/search/type"))
        self.assertEqual(parse_qs(parsed.query), {"search_type":["video"],"keyword":["街头摄影"],"page":["2"],"page_size":["12"]})
        with patch.object(bili, "_session") as network:
            for bad in ("wxid_secret", "https://foreign.example/hi", "13800138000", "请看我的密码", "A"*25):
                self.assertEqual(bili.public_search(bad), [])
            network.assert_not_called()


if __name__ == "__main__":
    unittest.main()
