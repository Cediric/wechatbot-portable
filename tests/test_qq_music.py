"""Song requests and provider responses, without WeChat or a network connection."""
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "wechat-weflow-bridge-ob11"))
import qq_music as music


def track(title="晴天", artist="周杰伦", mid="0039MnYb0qxYhV", **extra):
    return {"songname": title, "singer": [{"name": artist}], "songmid": mid, **extra}


class SongRequests(unittest.TestCase):
    def setUp(self):
        music._CACHE.clear()
        self.addCleanup(music._CACHE.clear)
        self.get = self.enterContext(patch.object(music.requests, "get"))

    def reply(self, text, *rows):
        self.get.return_value.json.return_value = {"code": 0, "data": {"song": {"list": list(rows)}}}
        request = music.parse_song_request(text)
        self.assertIsNotNone(request)
        return music.lookup_song_reply(request)

    def test_discussion_is_left_to_normal_chat(self):
        for text in ("今天大家点歌了吗", "点歌功能怎么用", "点歌机坏了", "我想听你讲个故事",
                     "来一首诗", "点歌吗？", "我想听《晴天》背后的故事", "点歌\n别的消息",
                     "@另一个群友 点歌《晴天》", "他说：点歌 晴天"):
            with self.subTest(text=text):
                self.assertIsNone(music.parse_song_request(text))

    def test_usage_does_not_contact_provider(self):
        self.assertIn("想听哪首", self.reply("/点歌"))
        self.get.assert_not_called()

    def test_explicit_singer_and_original_preferred(self):
        req = music.parse_song_request("请帮我点一首周杰伦的《晴天》吧")
        self.assertEqual(music.SongRequest("晴天", "周杰伦"), req)
        reply = self.reply("我想听周杰伦的《晴天》", track(artist="其他歌手"),
                           track(title="晴天 (Live)", mid="000LiveRecording"),
                           track(pay={"payplay": 1}))
        self.assertIn("会员", reply)
        self.assertIn("《晴天》 · 周杰伦", reply)
        self.assertTrue(reply.endswith("/0039MnYb0qxYhV"))
        _, kwargs = self.get.call_args
        self.assertEqual("周杰伦 晴天", kwargs["params"]["w"])
        self.assertIn("timeout", kwargs)
        self.assertNotIn("cookies", kwargs)

    def test_plain_singer_and_title_combinations(self):
        for text in ("来首周杰伦的晴天", "点歌 周杰伦 晴天", "点歌 晴天 - 周杰伦", "/music 周杰伦 - 晴天"):
            with self.subTest(text=text):
                self.assertIn(" · 周杰伦", self.reply(text, track(artist="其他歌手"), track()))

    def test_title_with_de_is_preserved(self):
        self.assertIn("隐形的翅膀", self.reply("点歌 隐形的翅膀", track("隐形的翅膀", "张韶涵")))

    def test_explicit_version_and_artist_must_match(self):
        for text in ("播放《晴天 (Live)》", "来首孙燕姿的《晴天》", "来首孙燕姿的晴天"):
            with self.subTest(text=text):
                self.assertIn("没找到", self.reply(text, track()))

    def test_bad_or_unavailable_records_do_not_become_links(self):
        reply = self.reply("点歌 晴天", track(mid="x/../../bad"), {}, None, track(switch=0))
        self.assertNotIn("https://", reply)
        self.assertIn("没找到", reply)

    def test_failures_and_no_results_are_distinct(self):
        self.assertIn("没找到", self.reply("点歌《未收录的曲子》"))
        req = music.SongRequest("晴天")
        self.get.side_effect = music.requests.Timeout()
        self.assertIn("暂时", music.lookup_song_reply(req))
        self.get.side_effect = None
        for payload in ({"code": 2001}, {"code": 0, "data": None}, {"code": 0}, []):
            self.get.return_value.json.return_value = payload
            self.assertIn("暂时", music.lookup_song_reply(req))
        self.get.return_value.json.side_effect = ValueError()
        self.assertIn("暂时", music.lookup_song_reply(req))

    def test_cache_expires_and_does_not_cache_failures(self):
        with patch.object(music.time, "monotonic", Mock(side_effect=[1000, 1010, 1700, 1700])):
            self.assertIn("周杰伦", self.reply("点歌 晴天", track()))
            self.assertIn("周杰伦", self.reply("点歌 晴天"))
            self.assertIn("没找到", self.reply("点歌 晴天"))
        self.assertEqual(2, self.get.call_count)


if __name__ == "__main__":
    unittest.main()
