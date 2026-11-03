"""Local-only checks for exact-message media followups and reference lifetime."""
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import threading
import types
import unittest
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[1] / "wechat-weflow-bridge-ob11"
sys.path.insert(0, str(SOURCE))


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, SOURCE / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RecentMediaTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.now = 100.0
        with patch.dict(sys.modules, {"config": types.SimpleNamespace(MEDIA_MAX_MB=30)}):
            self.media = load("recent_real_media_util", "media_util.py")
        self.patcher = patch.dict(sys.modules, {"media_util": self.media})
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.module = load("recent_media_under_test", "recent_media.py")
        self.cache = self.make_cache()

    def make_cache(self, **options):
        cache = self.module.RecentMediaCache(clock=lambda: self.now, automatic_cleanup=False, **options)
        self.addCleanup(cache.close)
        return cache

    def remember(self, seq=1, *, actor="wxid_alice", session="room@chatroom", kind="图片", count=1, size=5, cache=None):
        paths = []
        for index in range(count):
            path = Path(self.folder.name) / f"{session}_{actor}_{seq}_{index}.jpg"
            path.write_bytes(b"x" * size)
            paths.append(str(path))
        ident = {"session_id": session, "sender_username": actor, "sender_stable": True,
                 "is_group": session.endswith("@chatroom")}
        record = {"seq": seq, "at": self.now, "sender_username": actor,
                  "original_message_id": f"raw-{seq}", "message_ids": {"rawid": f"raw-{seq}", "localId": str(seq)},
                  "segments": [{"type": "image", "data": {"file": path}} for path in paths]}
        self.assertTrue((cache or self.cache).remember(ident, record, kind=kind))
        return paths, ident, record

    def select(self, text="这是什么？", *, actor="wxid_alice", session="room@chatroom", **options):
        return self.cache.select(session, actor, text, before_seq=options.pop("before_seq", 99), **options)

    def test_same_actor_followup_reuses_original_with_provenance(self):
        paths, _, _ = self.remember()
        selected = self.select()
        self.assertIsNotNone(selected)
        with selected:
            self.assertEqual(selected.segments[0]["data"]["file"], paths[0])
            self.assertEqual(selected.segments[0]["data"]["wechat_actor_id"], "wxid_alice")
            self.assertEqual(selected.segments[0]["data"]["wechat_message_id"], "raw-1")
            self.assertIn("不是本条文字新发送", selected.note)
            self.assertIn("raw-1", selected.note)
            self.assertNotIn(paths[0], selected.note)

    def test_no_cross_actor_group_or_private_reuse(self):
        self.remember()
        self.assertIsNone(self.select("上面图片是什么", actor="wxid_bob"))
        self.assertIsNone(self.select("上面图片是什么", session="other@chatroom"))
        self.assertIsNone(self.select("上面图片是什么", session="wxid_alice"))

    def test_media_after_text_and_current_batch_are_excluded(self):
        self.remember(seq=10)
        self.assertIsNone(self.select("刚才图片", before_seq=10))
        self.assertIsNone(self.select("刚才图片", before_seq=9))
        self.assertIsNone(self.select("刚才图片", exclude_message_ids=["raw-10"]))
        self.assertIsNone(self.select("刚才图片", exclude_message_ids=["10"]))

    def test_unrelated_turn_ends_implicit_window_but_explicit_reference_remains(self):
        self.remember()
        for text in ("你好", "明天几点见面", "给我生成一张图片", "今天吃什么", "好"):
            self.assertIsNone(self.select(text), text)
        self.assertIsNone(self.select("这是什么"))
        with self.select("刚才那张图片里是什么") as selected:
            self.assertEqual(len(selected.segments), 1)

    def test_implicit_followup_is_once_and_short_lived(self):
        self.remember()
        self.select("你看得懂吗？").close()
        self.assertIsNone(self.select("好看吗？"))
        self.remember(seq=2)
        self.now += 46
        self.assertIsNone(self.select("好看吗？"))
        self.select("刚才的视频和图片").close()

    def test_expiry_releases_cache_lease_but_not_live_selection(self):
        paths, _, _ = self.remember()
        selected = self.select()
        self.now += 181
        self.cache.prune()
        self.assertIsNone(self.select("刚才图片"))
        self.assertFalse(self.media.remove_cached_file(paths[0], self.folder.name))
        selected.close()
        self.assertTrue(self.media.remove_cached_file(paths[0], self.folder.name))
        self.assertEqual(self.media._LEASED_FILES, {})

    def test_close_and_drop_release_only_their_references(self):
        first, _, _ = self.remember(actor="wxid_alice")
        second, _, _ = self.remember(seq=2, actor="wxid_bob")
        self.cache.drop("room@chatroom", "wxid_alice")
        self.assertTrue(self.media.remove_cached_file(first[0], self.folder.name))
        self.assertFalse(self.media.remove_cached_file(second[0], self.folder.name))
        self.cache.close()
        self.cache.close()
        self.assertTrue(self.media.remove_cached_file(second[0], self.folder.name))
        self.assertIsNone(self.select("上面图片"))

    def test_deleted_or_replaced_files_are_not_attached(self):
        paths, _, _ = self.remember()
        os.unlink(paths[0])
        self.assertIsNone(self.select("上面图片"))
        self.assertEqual(self.media._LEASED_FILES, {})
        paths, _, _ = self.remember(seq=2)
        Path(paths[0]).write_bytes(b"replacement is a different image")
        self.assertIsNone(self.select("上面图片"))
        self.assertEqual(self.media._LEASED_FILES, {})

    def test_video_frames_preserve_original_sequence_despite_completion_order(self):
        later, _, _ = self.remember(seq=3, count=2, kind="视频")
        earlier, _, _ = self.remember(seq=2)
        with self.select("看看上面的图片和视频") as selected:
            self.assertEqual([part["data"]["file"] for part in selected.segments], earlier + later)
            self.assertEqual([source["message_id"] for source in selected.sources], ["raw-2", "raw-3"])
            self.assertIn("视频抽帧不含声音", selected.note)

    def test_old_burst_is_not_bundled_into_new_followup(self):
        self.remember(seq=1)
        self.now += 50
        paths, _, _ = self.remember(seq=2)
        with self.select() as selected:
            self.assertEqual([part["data"]["file"] for part in selected.segments], paths)

    def test_duplicate_source_does_not_refresh_ttl_or_add_leases(self):
        _, ident, record = self.remember()
        self.now += 120
        record["at"] = self.now
        self.assertFalse(self.cache.remember(ident, record))
        self.assertEqual(sum(self.media._LEASED_FILES.values()), 1)
        self.now += 61
        self.cache.prune()
        self.assertEqual(self.media._LEASED_FILES, {})

    def test_count_and_byte_budgets_release_evictions(self):
        cache = self.make_cache(max_records=2, max_per_actor=2, max_bytes=12)
        first, _, _ = self.remember(seq=1, size=5, cache=cache)
        self.now += 1
        second, _, _ = self.remember(seq=2, size=5, cache=cache)
        self.now += 1
        third, _, _ = self.remember(seq=3, size=5, cache=cache)
        self.assertTrue(self.media.remove_cached_file(first[0], self.folder.name))
        self.assertFalse(self.media.remove_cached_file(second[0], self.folder.name))
        self.assertFalse(self.media.remove_cached_file(third[0], self.folder.name))
        self.assertEqual(cache._bytes, 10)
        self.assertEqual(len(cache._records), 2)

    def test_unverified_identity_missing_source_and_mismatched_segment_are_rejected(self):
        _, ident, record = self.remember()
        self.cache.drop()
        self.assertFalse(self.cache.remember(dict(ident, sender_stable=False), record))
        self.assertFalse(self.cache.remember(dict(ident, sender_username="wxid_bob"), record))
        self.assertFalse(self.cache.remember(ident, dict(record, original_message_id="")))
        record["segments"][0]["data"]["wechat_actor_id"] = "wxid_bob"
        self.assertFalse(self.cache.remember(ident, record))
        self.assertEqual(self.media._LEASED_FILES, {})

    def test_remote_media_is_never_fetched_or_retained(self):
        _, ident, record = self.remember()
        self.cache.drop()
        record["segments"][0]["data"]["file"] = "https://example.invalid/image.jpg"
        self.assertFalse(self.cache.remember(ident, record))
        record["segments"][0]["data"]["file"] = "data:image/png;base64,aaa"
        self.assertFalse(self.cache.remember(ident, record))
        record["segments"][0]["data"]["file"] = r"\\example.invalid\share\image.jpg"
        with patch("os.stat", side_effect=AssertionError("UNC path must not be probed")):
            self.assertFalse(self.cache.remember(ident, record))
        self.assertEqual(self.media._LEASED_FILES, {})

    def test_attachment_budget_keeps_complete_newest_video(self):
        self.cache.max_files = 3
        self.remember(seq=1, count=2, kind="视频")
        newest, _, _ = self.remember(seq=2, count=2, kind="视频")
        with self.select("刚才视频") as selected:
            self.assertEqual([part["data"]["file"] for part in selected.segments], newest)

    def test_native_video_is_reused_without_sampling_or_model_calls(self):
        _, ident, record = self.remember()
        self.cache.drop()
        record["segments"][0]["type"] = "video"
        self.assertTrue(self.cache.remember(ident, record, kind="视频"))
        with self.select("刚才视频是什么") as selected:
            self.assertEqual(selected.segments[0]["type"], "video")

    def test_automatic_idle_expiry_releases_lease(self):
        _, ident, record = self.remember()
        self.cache.drop()
        import time
        record["at"] = time.monotonic()
        cache = self.module.RecentMediaCache(ttl_seconds=.03)
        self.addCleanup(cache.close)
        expired = threading.Event()
        original = cache.prune
        def prune():
            original()
            expired.set()
        cache.prune = prune
        self.assertTrue(cache.remember(ident, record))
        self.assertTrue(expired.wait(2), "idle TTL cleanup did not run")
        self.assertEqual(self.media._LEASED_FILES, {})


if __name__ == "__main__":
    unittest.main()
