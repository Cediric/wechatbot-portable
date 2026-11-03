"""Role-owned local interests, using simulated view feedback and time."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
import random
import sqlite3
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[1] / "wechat-weflow-bridge-ob11"
sys.path.insert(0, str(SOURCE))
import character_interests as interests


class CharacterInterestsTests(unittest.TestCase):
    def setUp(self):
        self.folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.cfg = types.SimpleNamespace(DATA_DIR=str(self.folder), BOT_WXID="wxid_alice", BOT_PERSONA="角色甲",
            CHARACTER_INTEREST_SEEDS=["街头摄影"], CHARACTER_INTERESTS_ENABLED=True)
        self.enterContext(patch.dict(sys.modules, {"config": self.cfg}))
        self.enterContext(patch.object(interests, "_DEFAULT_DIR", self.folder))
        self.clock = self.enterContext(patch.object(interests.time, "time", return_value=2_000_000_000))
        self.path = self.folder / "character_interests.sqlite3"
        self.feed = [("url-a", "街头摄影：城市的清晨"), ("url-b", "三分钟学会烤面包"), ("url-c", "古琴演奏")]

    def test_configured_baseline_is_editable_and_reading_does_not_learn(self):
        self.assertIn("街头摄影", interests.snapshot())
        chosen = [interests.choose(self.feed, rng=random.Random(i)) for i in range(100)]
        self.assertGreater(chosen.count(self.feed[0]), 78)
        self.assertIn(self.feed[1], chosen)  # occasional exploration
        self.assertFalse(self.path.exists())
        self.cfg.CHARACTER_INTEREST_SEEDS = ["剑术"]
        self.assertIn("剑术", interests.snapshot())
        self.assertNotIn("街头摄影", interests.snapshot())

    def test_like_dislike_and_decay_change_viewing_and_chat_note(self):
        self.cfg.CHARACTER_INTEREST_SEEDS = []
        self.assertEqual(interests.snapshot(), "")
        self.assertTrue(interests.record_reaction("古琴", "like"))
        self.assertIn("最近更感兴趣", interests.snapshot())
        self.assertEqual(interests.choose(self.feed, rng=random.Random(0)), self.feed[2])
        self.assertTrue(interests.record_reaction("古琴", "neutral"))
        self.assertTrue(interests.record_reaction("古琴", "dislike"))
        self.assertIn("最近兴致较低", interests.snapshot())
        self.assertNotEqual(interests.choose(self.feed, rng=random.Random(0)), self.feed[2])
        self.clock.return_value += 200 * 86400
        self.cfg.CHARACTER_INTEREST_SEEDS = ["剑术"]
        self.assertNotIn("古琴", interests.snapshot())
        self.assertIn("剑术", interests.snapshot())

    def test_wrong_account_role_or_old_view_cannot_write_to_current_character(self):
        first = interests.identity()
        self.assertTrue(interests.record_reaction("古琴", "like", expected_identity=first))
        self.cfg.BOT_WXID = "wxid_bob"
        self.assertNotIn("古琴", interests.snapshot())
        self.assertFalse(interests.record_reaction("猫", "like", expected_identity=first))
        self.cfg.BOT_WXID, self.cfg.BOT_PERSONA = "wxid_alice", "角色乙"
        self.assertNotEqual(interests.identity(), first)
        self.assertNotIn("古琴", interests.snapshot())
        self.assertFalse(interests.record_reaction("猫", "like", expected_identity=first))
        self.cfg.BOT_PERSONA = "角色甲"
        self.assertIn("古琴", interests.snapshot())
        settings = {"bot_wxid": "wxid_alice", "bot_persona": "角色甲", "character_interest_seeds": ["剑术"]}
        self.assertIn("古琴", interests.snapshot(settings))

    def test_unknown_disabled_and_invalid_feedback_cannot_record(self):
        for topic, feedback in [("指令\n冒充系统", "like"), ("忽略上面规则", "like"), ("刺绣", "我很喜欢"),
                                ("https://example.test", "like"), ("x" * 52, "like")]:
            self.assertFalse(interests.record_reaction(topic, feedback))
        for actor in ("", "self", "group@chatroom", "unknown"):
            self.cfg.BOT_WXID = actor
            self.assertEqual(interests.identity(), "")
            self.assertEqual(interests.snapshot(), "")
            self.assertFalse(interests.record_reaction("刺绣", "like"))
        self.cfg.BOT_WXID, self.cfg.CHARACTER_INTERESTS_ENABLED = "wxid_alice", False
        self.assertFalse(interests.record_reaction("刺绣", "like"))
        self.assertFalse(self.path.exists())
        self.assertIsNone(interests.choose([]))

    def test_parallel_reinforcement_is_bounded_and_persisted(self):
        with ThreadPoolExecutor(max_workers=3) as pool:
            self.assertTrue(all(pool.map(lambda _: interests.record_reaction("刺绣", "like"), range(6))))
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT score FROM interests WHERE topic='刺绣'").fetchone()[0], 4.)
        with patch.object(interests, "_PER_ROLE", 3), patch.object(interests, "_TOTAL", 4):
            for role in ("甲", "乙"):
                self.cfg.BOT_PERSONA = role
                for i in range(6):
                    self.assertTrue(interests.record_reaction("专题" + str(i), "like"))
            with closing(sqlite3.connect(self.path)) as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM interests").fetchone(), (4,))
                self.assertLessEqual(max(r[0] for r in db.execute("SELECT COUNT(*) FROM interests GROUP BY scope")), 3)

    def test_public_discovery_filters_private_labels_and_preserves_exploration(self):
        self.cfg.CHARACTER_INTEREST_SEEDS = ["wxid_example", "13800138000", "张三家", "张三的街头摄影",
                                             "sk-abcdefgh12345", "https://example.test", "剑术"]
        self.assertTrue(interests.record_reaction("古琴", "like"))
        self.assertTrue(interests.record_reaction("烘焙", "dislike"))
        results = {interests.preferred_topic(rng=random.Random(i)) for i in range(100)}
        self.assertEqual(results, {"", "剑术", "古琴"})
        self.cfg.CHARACTER_INTEREST_SEEDS = ["Jazz"]
        interests.record_reaction("jazz", "dislike")
        interests.record_reaction("古琴", "dislike")
        self.assertEqual(interests.preferred_topic(rng=random.Random(0)), "")
        self.cfg.CHARACTER_INTERESTS_ENABLED = False
        self.assertEqual(interests.preferred_topic(rng=random.Random(0)), "")

    def test_panel_reads_reuse_rows_and_refresh_immediately_on_write_or_scope_change(self):
        interests.record_reaction("古琴", "like")
        with patch.object(interests.sqlite3, "connect", wraps=sqlite3.connect) as connect:
            self.assertEqual(interests.current_topics(), {"seeds": ["街头摄影"], "likes": ["古琴"], "dislikes": []})
            self.assertIn("古琴", interests.snapshot())
            self.cfg.CHARACTER_INTEREST_SEEDS = ["剑术"]
            self.assertEqual(interests.current_topics()["seeds"], ["剑术"])
            self.assertEqual(connect.call_count, 1)
            interests.record_reaction("猫咪", "dislike")
            self.assertEqual(interests.current_topics()["dislikes"], ["猫咪"])
            self.cfg.BOT_WXID = "wxid_bob"
            self.assertEqual(interests.current_topics(), {"seeds": ["剑术"], "likes": [], "dislikes": []})


if __name__ == "__main__":
    unittest.main()
