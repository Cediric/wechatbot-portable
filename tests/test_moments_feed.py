"""Original-field/XML identity and bounded read-only timeline contract tests."""
import importlib.util
import json
from pathlib import Path
import sys
import threading
import types
import unittest
from unittest.mock import Mock, patch

SOURCE = Path(__file__).resolve().parents[1] / "wechat-weflow-bridge-ob11"


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, SOURCE / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def raw_xml(*, author="wxid_friend", post_id="10001", comments="", likes="", tags=("CommentUserList", "CommentUser", "LikeUserList", "LikeUser")):
    cl, ci, ll, li = tags
    return f"<SnsBuffer><TimeLineObject><id>{post_id}</id><username>{author}</username><contentDesc>今天散步</contentDesc></TimeLineObject><{cl}>{comments}</{cl}><{ll}>{likes}</{ll}></SnsBuffer>"


def row(**kwargs):
    result = {"id": "10001", "username": "wxid_friend", "nickname": "朋友",
        "createTime": 1791719900, "contentDesc": "今天散步", "comments": [], "likes": ["本人"], "media": [], "rawXml": raw_xml()}
    result.update(kwargs)
    return result


def native_detail(source=None, *, comments=None, likes=None, **kwargs):
    source = source or row()
    value = int(source["id"])
    signed = value if value < 1 << 63 else value - (1 << 64)
    result = {"id": source["id"], "tid": str(signed), "username": source["username"],
        "contentDesc": source["contentDesc"], "createTime": source["createTime"], "identityVerified": True,
        "commentsVerified": True, "likesVerified": True,
        "commentsDetail": comments or [], "likesDetail": likes or []}
    result.update(kwargs)
    return result


def native_comment(**kwargs):
    result = {"id": "20001", "username": "wxid_other", "nickname": "同名联系人", "content": "原始评论正文",
        "refCommentId": "20000", "refUsername": "wxid_self", "refNickname": "本人", "createTime": 0}
    result.update(kwargs)
    return result


def details_payload(details):
    return {"success": True, "version": 1, "capabilities": {"snsIdentityDetails": 1},
            "count": len(details), "timeline": details}


class Response:
    def __init__(self, data=None, *, code=200, chunks=None):
        self.status_code = code
        self.body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.chunks = chunks
        self.closed = False

    def iter_content(self, chunk_size):
        yield from self.chunks if self.chunks is not None else [self.body]

    def close(self):
        self.closed = True


class MomentsFeedTests(unittest.TestCase):
    def setUp(self):
        self.config = types.SimpleNamespace(WE_FLOW_BASE_URL="http://127.0.0.1:5031", ACCESS_TOKEN="synthetic-token")
        self.session = Mock()
        self.http = types.SimpleNamespace(session=Mock(return_value=self.session))
        modules = patch.dict(sys.modules, {"config": self.config, "http_util": self.http})
        modules.start(); self.addCleanup(modules.stop)
        self.stable = load("feed_stable", "stable_id.py")
        stable_patch = patch.dict(sys.modules, {"stable_id": self.stable})
        stable_patch.start(); self.addCleanup(stable_patch.stop)
        self.module = load("feed_module", "moments_feed.py")
        self.client = self.module.MomentsFeedClient()
        self.client._capability_key = (self.config.WE_FLOW_BASE_URL, self.config.ACCESS_TOKEN)
        self.client._native_supported = False
        self.client._native_retry_at = float("inf")
        self.disable_details(self.client)

    def disable_details(self, client):
        client._details_supported = False
        client._details_retry_at = float("inf")

    def enable_details(self):
        self.client._details_supported = None
        self.client._details_retry_at = 0

    def test_original_xml_restores_actor_ref_actor_and_real_comment_id(self):
        comment = "<CommentUser><cmtid>20001</cmtid><username>wxid_other</username><nickname>另一位</nickname><content>回复正文</content><ref_username>wxid_self</ref_username><refCommentId>20000</refCommentId><refNickname>本人</refNickname></CommentUser>"
        source = row(rawXml=raw_xml(comments=comment), comments=[{"id": "20001", "nickname": "不可靠缓存名", "content": "回复正文"}])
        post = self.module.normalize_post(source, bot_id="wxid_self")
        self.assertEqual(len(post["comments"]), 1)
        item = post["comments"][0]
        self.assertEqual(item["comment_id"], "20001")
        self.assertEqual(item["author_id"], "wxid_other")
        self.assertEqual(item["ref_author_id"], "wxid_self")
        self.assertEqual(item["ref_comment_id"], "20000")
        self.assertEqual(item["created_at"], source["createTime"])
        self.assertEqual(item["time_source"], "post")
        self.assertTrue(item["identity_verified"])

    def test_native_media_is_retained_for_main_model_and_overrides_cached_thumbnail(self):
        xml = raw_xml().replace("</TimeLineObject>", "<ContentObject><mediaList><media><id>30001</id><type>6</type><url>https://v.qpic.cn/original</url><thumb>https://qpic.cn/thumb</thumb></media></mediaList></ContentObject></TimeLineObject>")
        result = self.module.normalize_post(row(rawXml=xml, media=[{"url":"https://qpic.cn/stale","type":"1"}]))
        self.assertTrue(result["has_media"])
        self.assertEqual(result["media"],[{"id":"30001","type":"6","url":"https://v.qpic.cn/original","thumb":"https://qpic.cn/thumb"}])

    def test_media_invalid_shape_count_and_url_types_rejected(self):
        for media in [["invalid"], [{"url":123}], [{"url":"https://qpic.cn/a"}]*10]:
            with self.subTest(media=media), self.assertRaises(self.module.FeedError):
                self.module.normalize_post(row(media=media))

    def test_native_media_attributes_restore_original_signed_url_and_exact_key(self):
        xml = raw_xml().replace("</TimeLineObject>", '<ContentObject><mediaList><media><id>30001</id><type>2</type>'
            '<url token="synthetic+token" key="18446744073709551615">http://m.qpic.cn/photo/480?sig=keep%2fexact&amp;idx=old&amp;token=stale</url>'
            '<thumb token="synthetic-thumb">https://m.qpic.cn/photo/150</thumb></media></mediaList></ContentObject></TimeLineObject>')
        item = self.module.normalize_post(row(rawXml=xml))["media"][0]
        self.assertEqual(item["url"], "https://m.qpic.cn/photo/0?sig=keep%2fexact&token=synthetic%2Btoken&idx=1")
        self.assertEqual(item["key"], "18446744073709551615")
        self.assertEqual(item["thumb"], "https://m.qpic.cn/photo/0?token=synthetic-thumb&idx=1")

    def test_native_video_inherits_post_type_and_container_key_without_using_thumbnail(self):
        xml = raw_xml().replace("</TimeLineObject>", '<ContentObject><mediaList><media>'
            '<url token="synthetic-video">http://v.qpic.cn/snsvideodownload?sig=keep%2Fvalue</url>'
            '<thumb>https://v.qpic.cn/vweixinthumb</thumb></media></mediaList></ContentObject><enc key="18446744073709551615"/></TimeLineObject>')
        item = self.module.normalize_post(row(rawXml=xml, type=15))["media"][0]
        self.assertEqual(item["type"], "15")
        self.assertEqual(item["key"], "18446744073709551615")
        self.assertEqual(item["url"], "https://v.qpic.cn/snsvideodownload?token=synthetic-video&idx=1&sig=keep%2Fvalue")

    def test_native_xml_fragment_accepts_exact_fields_without_rewriting(self):
        xml = '<id>10001</id><username>wxid_friend</username><createTime>1791719900</createTime><contentDesc><![CDATA[正文 <符号>]]></contentDesc><ContentObject><type>1</type></ContentObject>'
        result = self.module.normalize_post(row(rawXml=xml, contentDesc="正文 <符号>", comments=[{"id": "20001", "nickname": "朋友", "content": "评论", "refCommentId": "", "refNickname": ""}]))
        self.assertEqual(result["post_id"], "10001")
        self.assertEqual(result["author_id"], "wxid_friend")
        self.assertEqual(result["content"], "正文 <符号>")
        self.assertEqual(result["unverified_comment_count"], 1)
        self.assertFalse(result["comments"][0]["identity_verified"])
        self.assertFalse(result["likes_verified"])
        with self.assertRaises(self.module.FeedError):
            self.module.normalize_post(row(rawXml=xml.replace("wxid_friend", "wxid_other")))

    def test_original_xml_proves_exact_one_layer_named_entity_semantics(self):
        xml = '<id>10001</id><username>wxid_friend</username><contentDesc>鱼 &amp; 茶 &lt;3 &quot;是&quot; &apos;好&apos;</contentDesc>'
        cached = '鱼 &amp; 茶 &lt;3 &quot;是&quot; &apos;好&apos;'
        source = row(rawXml=xml, contentDesc=cached)
        post = self.module.normalize_post(source)
        self.assertEqual(post['content'], '鱼 & 茶 <3 "是" \'好\'')
        result = self.module.enrich_post(post, native_detail(source, contentDesc=post['content']))
        self.assertEqual(result['content'], post['content'])
        # No XML proof, an arbitrary HTML entity or a second escape layer is
        # allowed to rewrite a post's text to resemble a different native post.
        self.assertEqual(self.module.normalize_post(row(rawXml='', contentDesc=cached))['content'], cached)
        for encoded, original in [('&nbsp;', '\u00a0'), ('&amp;amp;', '&')]:
            original_xml = '<id>10001</id><username>wxid_friend</username><contentDesc><![CDATA[' + original + ']]></contentDesc>'
            post = self.module.normalize_post(row(rawXml=original_xml, contentDesc=encoded))
            self.assertEqual(post['content'], encoded)
            with self.assertRaises(self.module.FeedError):
                self.module.enrich_post(post, native_detail(contentDesc=original))

    def test_literal_entity_text_in_original_xml_is_preserved(self):
        xml = '<id>10001</id><username>wxid_friend</username><contentDesc><![CDATA[字面 &amp;]]></contentDesc>'
        post = self.module.normalize_post(row(rawXml=xml, contentDesc='字面 &amp;'))
        self.assertEqual(post['content'], '字面 &amp;')

    def test_full_native_xml_local_extra_never_bypasses_flags_completeness(self):
        for deleted, pending in [('0', '0'), ('1', '0'), ('0', '1')]:
            xml = '<SnsDataItem><TimelineObject><id>10001</id><username>wxid_friend</username><contentDesc>今天散步</contentDesc></TimelineObject><LocalExtraInfo><comment_user_list><user_comment><comment_id>20001</comment_id><comment_64id>18446744073709550001</comment_64id><username>wxid_other</username><content>原始评论正文</content><b_deleted>' + deleted + '</b_deleted><is_local_added>' + pending + '</is_local_added></user_comment></comment_user_list><like_user_list><user_comment><username>wxid_self</username><b_deleted>0</b_deleted><is_local_added>1</is_local_added></user_comment></like_user_list></LocalExtraInfo></SnsDataItem>'
            with self.subTest(deleted=deleted, pending=pending):
                post = self.module.normalize_post(row(rawXml=xml), bot_id='wxid_self')
                self.assertFalse(post['comments'][0]['identity_verified'])
                self.assertEqual(post['comments'][0]['source_comment_id'], '18446744073709550001')
                self.assertFalse(post['likes_verified'])
                self.assertFalse(post['liked_by_self'])
                result = self.module.enrich_post(post, native_detail(commentsVerified=False))
                self.assertFalse(result['interaction_identity_verified'])
                self.assertFalse(result['comments'][0]['identity_verified'])

    def test_documented_case_and_underscore_list_variants(self):
        for tags, id_tag, ref_tag in [(("commentUserList", "commentUser", "likeUserList", "likeUser"), "commentId", "replyCommentId"),
                (("commentList", "comment", "likeList", "likeUser"), "id", "ref_comment_id"),
                (("comment_user_list", "user_comment", "like_user_list", "user_comment"), "comment_id", "refCommentId")]:
            cl, ci, ll, li = tags
            xml_comment = f"<{ci}><{id_tag}>opaque-real-123</{id_tag}><username>wxid_other</username><content>评论</content><{ref_tag}>real-122</{ref_tag}><createTime>1791719990</createTime></{ci}>"
            likes = f"<{li}><username>wxid_self</username><nickname>本人</nickname></{li}>"
            post = self.module.normalize_post(row(rawXml=raw_xml(comments=xml_comment, likes=likes, tags=tags)), bot_id="wxid_self")
            self.assertEqual(post["comments"][0]["comment_id"], "opaque-real-123")
            self.assertEqual(post["comments"][0]["ref_comment_id"], "real-122")
            self.assertEqual(post["comments"][0]["time_source"], "comment")
            self.assertTrue(post["liked_by_self"])

    def test_nickname_only_likes_never_establish_self_like(self):
        post = self.module.normalize_post(row(likes=["本人"], rawXml=""), bot_id="wxid_self")
        self.assertFalse(post["likes_verified"])
        self.assertFalse(post["liked_by_self"])
        xml = raw_xml(likes="<LikeUser><username>wxid_other</username><nickname>本人</nickname></LikeUser>")
        post = self.module.normalize_post(row(rawXml=xml), bot_id="wxid_self")
        self.assertTrue(post["likes_verified"])
        self.assertFalse(post["liked_by_self"])

    def test_missing_xml_actor_and_synthetic_comment_id_remain_context_only(self):
        xml = raw_xml(comments="<CommentUser><cmtid>cmt_1791719990_0.234</cmtid><username>wxid_other</username><content>synthetic</content></CommentUser><CommentUser><cmtid>20001</cmtid><nickname>只有名字</nickname><content>unknown</content></CommentUser>")
        post = self.module.normalize_post(row(rawXml=xml, comments=[{"id": "cmt_1791719990_0.234", "nickname": "其他人", "content": "context"}]))
        self.assertEqual(len(post["comments"]), 1)
        self.assertFalse(post["comments"][0]["identity_verified"])
        self.assertEqual(post["comments"][0]["comment_id"], "")
        self.assertEqual(post["comments"][0]["author_id"], "")

    def test_xml_and_cache_identity_mismatch_is_rejected(self):
        for xml in (raw_xml(author="wxid_other"), raw_xml(post_id="10002")):
            with self.assertRaises(self.module.FeedError):
                self.module.normalize_post(row(rawXml=xml))

    def test_missing_post_id_username_or_timestamp_is_rejected(self):
        for fields in ({"id": "cmt_1791719990_random"}, {"id": ""}, {"username": "中文显示名"}, {"username": "group@chatroom"}, {"createTime": 0}, {"createTime": float("nan")}, {"createTime": 1791719900000}):
            with self.assertRaises(self.module.FeedError):
                self.module.normalize_post(row(**fields))

    def test_dtd_entity_malformed_and_oversized_xml_rejected(self):
        for xml in ('<!DOCTYPE doc [<!ENTITY x "expansion">]><doc>&x;</doc>', '<broken>', ' ' + 'x'*self.module.MAX_XML_BYTES):
            with self.assertRaises(self.module.FeedError):
                self.module.normalize_post(row(rawXml=xml))

    def test_duplicate_real_comment_ids_are_ambiguous(self):
        comments = "".join(f"<CommentUser><cmtid>20001</cmtid><username>{actor}</username><content>正文</content></CommentUser>" for actor in ("wxid_a", "wxid_b"))
        with self.assertRaises(self.module.FeedError):
            self.module.normalize_post(row(rawXml=raw_xml(comments=comments)))

    def test_registry_current_names_are_bound_to_exact_username(self):
        registry = Mock()
        registry.get_private_by_session.return_value = {"session_id": "wxid_friend", "name": "现在备注", "nickname": "当前昵称", "remark": "现在备注"}
        result = self.module.normalize_post(row(), registry=registry)
        self.assertEqual(result["author_name"], "现在备注")
        self.assertEqual(result["author_names"], ["现在备注", "当前昵称", "朋友"])
        registry.get_private_by_session.return_value = {"session_id": "wxid_other", "name": "错人"}
        result = self.module.normalize_post(row(), registry=registry)
        self.assertNotIn("错人", result["author_names"])

    def test_real_timeline_key_auth_media_false_and_paging(self):
        first = [row(id=str(10001+i), rawXml=raw_xml(post_id=str(10001+i))) for i in range(20)]
        second = [row(id="10021", rawXml=raw_xml(post_id="10021")), row(id="10022", rawXml=raw_xml(post_id="10022"))]
        responses = [Response({"success": True, "timeline": first}), Response({"success": True, "timeline": second})]
        self.session.get.side_effect = responses
        posts = self.client.fetch(limit=22, bot_id="wxid_self")
        self.assertEqual(len(posts), 22)
        calls = self.session.get.call_args_list
        self.assertEqual(calls[0].args[0], "http://127.0.0.1:5031/api/v1/sns/timeline")
        self.assertEqual(calls[0].kwargs["params"], {"access_token": "synthetic-token", "limit": 20, "offset": 0, "media": 0})
        self.assertEqual(calls[1].kwargs["params"]["offset"], 20)
        self.assertEqual(calls[1].kwargs["params"]["limit"], 2)
        self.assertTrue(all(item.closed for item in responses))

    def test_old_data_key_and_unsuccessful_result_rejected(self):
        for data in ({"success": True, "data": [row()]}, {"success": False, "timeline": []}, []):
            self.session.get.return_value = Response(data)
            with self.assertRaises(self.module.FeedError):
                self.client.fetch()

    def test_native_capability_contract_is_exact_and_legacy_fallback_is_explicit(self):
        client = self.module.MomentsFeedClient()
        self.disable_details(client)
        client._capability_key = (self.config.WE_FLOW_BASE_URL, self.config.ACCESS_TOKEN)
        response = Response({"success": True, "version": 1, "capabilities": {"snsIdentityTimeline": 1}, "timeline": [row()]})
        self.session.get.return_value = response
        self.assertEqual(len(client.fetch()), 1)
        self.assertTrue(client.identity_available)
        self.assertTrue(self.session.get.call_args.args[0].endswith("/api/v1/sns-identity-timeline"))
        self.assertNotIn("media", self.session.get.call_args.kwargs["params"])
        client = self.module.MomentsFeedClient()
        self.disable_details(client)
        client._capability_key = (self.config.WE_FLOW_BASE_URL, self.config.ACCESS_TOKEN)
        missing = Response({}, code=404); legacy = Response({"success": True, "timeline": [row()]})
        self.session.get.side_effect = [missing, legacy]
        self.assertEqual(len(client.fetch()), 1)
        self.assertFalse(client.identity_available)
        self.assertTrue(missing.closed and legacy.closed)
        self.assertTrue(self.session.get.call_args.args[0].endswith("/api/v1/sns/timeline"))
        self.assertEqual(self.session.get.call_args.kwargs["params"]["media"], 0)

    def test_native_wrong_version_caps_or_server_error_never_fallback_silently(self):
        for response in [Response({}, code=500),
                Response({"success": True, "version": True, "capabilities": {"snsIdentityTimeline": 1}, "timeline": []}),
                Response({"success": True, "version": 1, "capabilities": {"snsIdentityTimeline": True}, "timeline": []})]:
            self.session.get.reset_mock(); self.session.get.side_effect = None; self.session.get.return_value = response
            with self.assertRaises(self.module.FeedError):
                self.module.MomentsFeedClient().fetch()
            self.assertEqual(self.session.get.call_count, 1)
            self.assertTrue(response.closed)

    def test_excluding_self_advances_offset_instead_of_stopping_at_self_rows(self):
        pages = [Response({"success": True, "timeline": [row(username="wxid_self", rawXml=raw_xml(author="wxid_self"))]}),
                 Response({"success": True, "timeline": [row()]})]
        self.session.get.side_effect = pages
        result = self.client.fetch(limit=1, exclude_author="wxid_self")
        self.assertEqual(result[0]["author_id"], "wxid_friend")
        self.assertEqual(self.session.get.call_args_list[1].kwargs["params"]["offset"], 1)

    def test_api_auth_failure_does_not_use_error_payload_or_leave_stream(self):
        response = Response({"error": "Unauthorized"}, code=401)
        self.session.get.return_value = response
        with self.assertRaisesRegex(self.module.FeedError, "401"):
            self.client.fetch()
        self.assertTrue(response.closed)

    def test_http_response_size_bound_and_cancel_between_chunks(self):
        response = Response(chunks=[b"x"*100, b"x"*100])
        self.session.get.return_value = response
        with patch.object(self.module, "MAX_HTTP_BYTES", 150):
            with self.assertRaisesRegex(self.module.FeedError, "超出限制"):
                self.client.fetch()
        self.assertTrue(response.closed)
        cancel = threading.Event()
        def chunks(chunk_size):
            yield b'{"success":true,'
            cancel.set()
            yield b'"timeline":[]}'
        response = Response(); response.iter_content = chunks
        self.session.get.return_value = response
        with self.assertRaisesRegex(self.module.FeedError, "取消"):
            self.client.fetch(cancel_event=cancel)
        self.assertTrue(response.closed)

    def test_cancel_before_http_and_invalid_base_never_calls_session(self):
        cancel = threading.Event(); cancel.set()
        with self.assertRaisesRegex(self.module.FeedError, "取消"):
            self.client.fetch(cancel_event=cancel)
        self.session.get.assert_not_called()
        for base in ("file:///tmp/sns", "http://user:secret@127.0.0.1:5031"):
            self.config.WE_FLOW_BASE_URL = base
            with self.assertRaises(self.module.FeedError):
                self.client.fetch()
        self.session.get.assert_not_called()

    def test_limit_is_bounded_and_username_filter_enforced_after_http(self):
        self.session.get.return_value = Response({"success": True, "timeline": [row()]})
        posts = self.client.fetch(limit=10000, usernames=["wxid_allowed"])
        self.assertEqual(posts, [])
        self.assertEqual(self.session.get.call_args.kwargs["params"]["usernames"], "wxid_allowed")
        self.assertEqual(self.session.get.call_args.kwargs["params"]["limit"], 20)
        with self.assertRaises(self.module.FeedError):
            self.client.fetch(usernames=["中文昵称"])

    def test_malformed_entries_skip_without_guessed_identity(self):
        self.session.get.return_value = Response({"success": True, "timeline": [row(id=""), row()]})
        result = self.client.fetch(limit=3)
        self.assertEqual(len(result), 1)
        self.assertEqual(self.client.last_skipped, 1)

    def test_exact_native_details_restore_comment_and_like_identities(self):
        self.enable_details()
        comment = native_comment()
        source = row(rawXml="", comments=[{"id": comment["id"], "nickname": "显示名不同也不猜人", "content": comment["content"]}])
        self.session.get.side_effect = [Response({"success": True, "timeline": [source]}),
            Response(details_payload([native_detail(source, comments=[comment], likes=[{"username": "wxid_self", "nickname": "本人"}])]))]
        result = self.client.fetch(bot_id="wxid_self")
        item = result[0]["comments"][0]
        self.assertEqual(item["comment_id"], "20001")
        self.assertEqual(item["author_id"], "wxid_other")
        self.assertEqual(item["ref_author_id"], "wxid_self")
        self.assertEqual(item["ref_comment_id"], "20000")
        self.assertTrue(item["identity_verified"])
        self.assertEqual(item["time_source"], "post")
        self.assertEqual(result[0]["unverified_comment_count"], 0)
        self.assertTrue(result[0]["liked_by_self"])
        self.assertTrue(self.client.details_identity_available)
        call = self.session.get.call_args_list[-1]
        self.assertTrue(call.args[0].endswith("/api/v1/sns-identity-details"))
        self.assertEqual(call.kwargs["params"], {"access_token": "synthetic-token", "post_ids": "10001"})

    def test_unsigned64_id_and_negative_native_tid_keep_exact_integer_precision(self):
        source = row(id="18446744073709551615", rawXml="")
        detail = native_detail(source)
        self.assertEqual(detail["tid"], "-1")
        result = self.module.enrich_post(self.module.normalize_post(source), detail)
        self.assertEqual(result["post_id"], "18446744073709551615")
        for fields in ({"id": 18446744073709551615}, {"tid": -1}, {"tid": "-0"}, {"tid": "-2"},
                       {"id": "18446744073709551616"}, {"tid": "9223372036854775808"}):
            with self.subTest(fields=fields), self.assertRaises(self.module.FeedError):
                self.module.enrich_post(self.module.normalize_post(source), dict(detail, **fields))

    def test_details_header_identity_body_and_time_must_match_source(self):
        source = row(rawXml="")
        post, detail = self.module.normalize_post(source), native_detail(source)
        for fields in ({"username": "wxid_wrong"}, {"contentDesc": "相似但不相同"},
                       {"createTime": source["createTime"] + 2}, {"createTime": True}, {"identityVerified": 1}):
            with self.subTest(fields=fields), self.assertRaises(self.module.FeedError):
                self.module.enrich_post(post, dict(detail, **fields))
        result = self.module.enrich_post(post, dict(detail, createTime=source["createTime"] + 1))
        self.assertEqual(result["created_at"], source["createTime"])

    def test_native_cached_comment_conflicts_refuse_enrichment(self):
        comment = native_comment()
        for conflict in ({"username": "wxid_wrong"}, {"content": "不同正文"},
                         {"refUsername": "wxid_wrong"}, {"refCommentId": "19999"}):
            cached = dict(comment, **conflict)
            source = row(rawXml="", comments=[cached])
            with self.subTest(conflict=conflict), self.assertRaises(self.module.FeedError):
                self.module.enrich_post(self.module.normalize_post(source), native_detail(source, comments=[comment]), cached_row=source)

    def test_xml_verified_actor_conflict_cannot_be_overridden_by_details(self):
        xml = raw_xml(comments="<CommentUser><cmtid>20001</cmtid><username>wxid_wrong</username><content>原始评论正文</content></CommentUser>")
        source = row(rawXml=xml)
        with self.assertRaises(self.module.FeedError):
            self.module.enrich_post(self.module.normalize_post(source), native_detail(source, comments=[native_comment()]))

    def test_complete_details_use_true_ids_without_attaching_unknown_cached_ids(self):
        comments = [native_comment(id="20001", username="wxid_a"), native_comment(id="20002", username="wxid_b")]
        source = row(rawXml="", comments=[{"id": "20003", "nickname": "同名联系人", "content": "原始评论正文"}])
        result = self.module.enrich_post(self.module.normalize_post(source), native_detail(source, comments=comments), cached_row=source)
        self.assertEqual([(item["comment_id"], item["author_id"]) for item in result["comments"]],
                         [("20001", "wxid_a"), ("20002", "wxid_b")])
        self.assertEqual(result['unverified_comment_count'], 0)

    def test_complete_native_list_discards_generated_and_deleted_cache_ghosts(self):
        actual = native_comment(refCommentId='', refUsername='')
        ghosts = [{'id': 'cmt_1791719900_0.123', 'nickname': actual['nickname'], 'content': actual['content']},
                  {'id': '20009', 'nickname': '旧评论者', 'content': '已删除的评论'}]
        source = row(rawXml='', comments=ghosts)
        normalized = self.module.normalize_post(source)
        self.assertEqual(len(normalized['comments']), 2)
        complete = self.module.enrich_post(normalized, native_detail(source, comments=[actual]), cached_row=source)
        self.assertEqual(len(complete['comments']), 1)
        self.assertEqual(complete['comments'][0]['comment_id'], actual['id'])
        self.assertEqual(complete['comments'][0]['author_id'], actual['username'])
        self.assertEqual(complete['unverified_comment_count'], 0)
        self.assertTrue(complete['interaction_identity_verified'])
        incomplete = self.module.enrich_post(normalized, native_detail(source, commentsVerified=False), cached_row=source)
        self.assertEqual(len(incomplete['comments']), 2)
        self.assertFalse(incomplete['interaction_identity_verified'])
        self.assertTrue(all(item['identity_verified'] is False for item in incomplete['comments']))

    def test_complete_empty_list_does_not_keep_deleted_previous_verified_comment(self):
        before, fresh = self.acknowledged_posts(before=[native_comment()])
        result = self.module.enrich_post(before, native_detail(comments=[]))
        self.assertEqual(result['comments'], [])
        self.assertTrue(result['interaction_identity_verified'])
        self.assertEqual(result['unverified_comment_count'], 0)

    def test_incomplete_native_comments_remain_context_and_preserve_baseline_id(self):
        source = row(rawXml="", comments=[{"id": "20001", "nickname": "同名联系人", "content": "原始评论正文"}])
        result = self.module.enrich_post(self.module.normalize_post(source), native_detail(source, commentsVerified=False))
        self.assertFalse(result["interaction_identity_verified"])
        self.assertEqual(result["comments"][0]["source_comment_id"], "20001")
        self.assertEqual(result["comments"][0]["comment_id"], "")
        self.assertEqual(result["comments"][0]["author_id"], "")
        self.assertEqual(result["unverified_comment_count"], 1)

    def test_incomplete_or_duplicate_native_lists_are_rejected(self):
        post = self.module.normalize_post(row(rawXml=""))
        for fields in ({"commentsVerified": 1}, {"likesVerified": None},
                       {"commentsVerified": False, "commentsDetail": [native_comment()]},
                       {"commentsDetail": [native_comment(), native_comment()]},
                       {"commentsDetail": [native_comment(id="cmt_1791719990_random")]},
                       {"commentsDetail": [native_comment(username="显示昵称")]},
                       {"commentsDetail": [native_comment(refUsername="显示昵称")]},
                       {"commentsDetail": [native_comment(createTime=True)]},
                       {"likesDetail": [{"nickname": "本人"}]},
                       {"likesDetail": [{"username": "wxid_self"}, {"username": "wxid_self"}]}):
            with self.subTest(fields=fields), self.assertRaises(self.module.FeedError):
                self.module.enrich_post(post, native_detail(**fields))

    def test_native_current_registry_names_require_exact_actor_binding(self):
        registry = Mock()
        registry.get_private_by_session.side_effect = lambda username: {"session_id": username, "name": "当前备注"}
        result = self.module.enrich_post(self.module.normalize_post(row(rawXml="")),
                                        native_detail(comments=[native_comment()]), registry=registry)
        self.assertEqual(result["comments"][0]["author_name"], "当前备注")
        self.assertEqual(result["comments"][0]["author_id"], "wxid_other")

    def test_empty_emoji_comment_retains_complete_identity_without_text_ack(self):
        empty = native_comment(content='', refCommentId='', refUsername='')
        before, fresh = self.acknowledged_posts(before=[empty], after=[empty, native_comment(
            id='20002', username='wxid_self', content='确认全文', refCommentId='', refUsername='')])
        item = before['comments'][0]
        self.assertEqual((item['comment_id'], item['author_id'], item['content']), ('20001', 'wxid_other', ''))
        self.assertTrue(item['identity_verified'])
        self.assertTrue(before['interaction_identity_verified'])
        self.assertEqual(before['unverified_comment_count'], 0)
        self.assertIsNone(self.module.find_new_self_comment(before, fresh, bot_id='wxid_self', text=''))
        self.assertEqual(self.module.find_new_self_comment(before, fresh, bot_id='wxid_self', text='确认全文')['comment_id'], '20002')

    def test_native_32_to_64_aliases_join_cached_ids_and_exclude_old_ack(self):
        full = '18446744073709550001'
        cached = native_comment(id='20001', username='wxid_self', content='确认全文', refCommentId='', refUsername='')
        source = row(rawXml='', comments=[cached])
        detail = native_detail(source, comments=[dict(cached, id=full, commentId='20001', comment64Id=full)])
        before = self.module.enrich_post(self.module.normalize_post(source), native_detail(source, comments=[cached]), cached_row=source)
        fresh = self.module.enrich_post(self.module.normalize_post(source), detail, cached_row=source)
        self.assertEqual(len(fresh['comments']), 1)
        self.assertEqual(fresh['comments'][0]['comment_id'], full)
        self.assertEqual(fresh['comments'][0]['source_comment_id'], '20001')
        self.assertTrue(fresh['comments'][0]['native_alias_verified'])
        self.assertIsNone(self.module.find_new_self_comment(before, fresh, bot_id='wxid_self', text='确认全文'))

    def test_native_64_to_known_32_alias_never_acknowledges_existing_comment(self):
        full = '18446744073709550001'
        old = native_comment(id='20001', username='wxid_self', content='确认全文', refCommentId='', refUsername='')
        before, fresh = self.acknowledged_posts(
            before=[dict(old, id=full, commentId='20001', comment64Id=full)], after=[old])
        self.assertEqual(before['comments'][0]['comment_id'], full)
        self.assertEqual(before['comments'][0]['source_comment_id'], '20001')
        self.assertIsNone(self.module.find_new_self_comment(before, fresh, bot_id='wxid_self', text='确认全文'))
        corrupted = dict(before, comments=[dict(before['comments'][0], source_comment_id='cmt_generated')])
        self.assertIsNone(self.module.find_new_self_comment(corrupted, fresh, bot_id='wxid_self', text='确认全文'))

    def test_native_reply_reference_alias_change_cannot_fake_exact_target_ack(self):
        full = '18446744073709550001'
        original = native_comment(id=full, commentId='20001', comment64Id=full,
            username='wxid_other', content='原评论', refCommentId='', refUsername='')
        reply = native_comment(id='20002', username='wxid_self', content='回复全文',
            refCommentId='20001', refUsername='wxid_other')
        legacy_target = native_comment(id='20001', username='wxid_other', content='原评论',
            refCommentId='', refUsername='')
        before, fresh = self.acknowledged_posts(before=[original], after=[legacy_target, reply])
        self.assertIsNone(self.module.find_new_self_comment(before, fresh, bot_id='wxid_self',
            text='回复全文', reply_comment_id=full))

    def test_native_alias_id_ref_conflicts_and_out_of_range_are_rejected(self):
        full = '18446744073709550001'
        target = native_comment(id=full, commentId='20001', comment64Id=full, refCommentId='', refUsername='')
        reply = native_comment(id='18446744073709550002', commentId='20002', comment64Id='18446744073709550002',
            refCommentId=full, refCommentId32='20001', refCommentId64='', refUsername='wxid_other')
        post = self.module.normalize_post(row(rawXml=''))
        result = self.module.enrich_post(post, native_detail(comments=[target, reply]))
        self.assertEqual(result['comments'][-1]['ref_comment_id'], full)
        for comments in ([dict(target, id='20001')], [target, dict(reply, commentId='20001')],
                         [dict(target, id=str(1 << 64), comment64Id=str(1 << 64))],
                         [target, dict(reply, refCommentId='20001')],
                         [target, dict(reply, refCommentId64='18446744073709550003')]):
            with self.subTest(comments=comments), self.assertRaises(self.module.FeedError):
                self.module.enrich_post(post, native_detail(comments=comments))

    def test_details_conflict_disqualifies_only_that_exact_post(self):
        self.enable_details()
        sources = [row(id='10001', rawXml=''), row(id='10002', rawXml=''), row(id='10003', rawXml='')]
        details = [native_detail(sources[0]), native_detail(sources[1], username='wxid_wrong'),
                   native_detail(sources[2], comments=[native_comment(username='wxid_verified')])]
        self.session.get.side_effect = [Response({'success': True, 'timeline': sources}), Response(details_payload(details))]
        result = self.client.fetch(limit=4)
        self.assertEqual([post['post_id'] for post in result], ['10001', '10003'])
        self.assertEqual(self.client.last_skipped, 1)
        self.assertTrue(self.client.details_identity_available)
        self.assertEqual(result[-1]['comments'][0]['author_id'], 'wxid_verified')

    def test_details_batches_max20_exact_postids_and_accepts_reordered_rows(self):
        self.enable_details()
        sources = [row(id=str(10001 + index), rawXml="") for index in range(22)]
        responses = [Response({"success": True, "timeline": sources[:20]}), Response({"success": True, "timeline": sources[20:]}),
                     Response(details_payload([native_detail(source) for source in reversed(sources[:20])])),
                     Response(details_payload([native_detail(source) for source in sources[20:]]))]
        self.session.get.side_effect = responses
        result = self.client.fetch(limit=22)
        self.assertEqual([post["post_id"] for post in result], [source["id"] for source in sources])
        calls = self.session.get.call_args_list
        self.assertEqual(calls[-2].kwargs["params"]["post_ids"].split(","), [source["id"] for source in sources[:20]])
        self.assertEqual(calls[-1].kwargs["params"]["post_ids"], "10021,10022")
        self.assertTrue(all(response.closed for response in responses))

    def test_details_capability_fallback_is_context_only_and_caches_absence(self):
        self.enable_details()
        source = row(rawXml="", comments=[{"id": "20001", "nickname": "只有昵称", "content": "正文"}])
        missing = Response({}, code=404)
        self.session.get.side_effect = [Response({"success": True, "timeline": [source]}), missing,
                                        Response({"success": True, "timeline": [source]})]
        first = self.client.fetch()
        second = self.client.fetch()
        self.assertEqual(len(self.session.get.call_args_list), 3)
        self.assertTrue(missing.closed)
        self.assertFalse(self.client.details_identity_available)
        self.assertFalse(first[0]["comments"][0]["identity_verified"])
        self.assertFalse(second[0]["comments"][0]["identity_verified"])

    def test_details_server_error_bad_cap_count_missing_or_other_postid_fail_closed(self):
        source = row(rawXml="")
        valid = details_payload([native_detail(source)])
        invalid = [Response({}, code=500), Response(dict(valid, version=True)),
                   Response(dict(valid, capabilities={"snsIdentityDetails": True})),
                   Response(dict(valid, count=0)), Response(details_payload([])),
                   Response(details_payload([native_detail(row(id="10002", rawXml=""))]))]
        for response in invalid:
            with self.subTest(response=response):
                self.enable_details()
                self.session.get.side_effect = [Response({"success": True, "timeline": [source]}), response]
                with self.assertRaises(self.module.FeedError):
                    self.client.fetch()
                self.assertTrue(response.closed)

    def test_cancel_between_timeline_and_details_does_not_read_native_db(self):
        self.enable_details()
        cancel = threading.Event()
        response = Response({"success": True, "timeline": [row(rawXml="")]})
        def close():
            response.closed = True
            cancel.set()
        response.close = close
        self.session.get.return_value = response
        with self.assertRaisesRegex(self.module.FeedError, "取消"):
            self.client.fetch(cancel_event=cancel)
        self.assertEqual(self.session.get.call_count, 1)

    def test_opaque_or_negative_post_ids_do_not_enter_native_sql_identity_request(self):
        self.enable_details()
        self.session.get.return_value = Response({"success": True, "timeline": [row(id="opaque-real-123", rawXml=""), row(id="-123", rawXml="")]})
        result = self.client.fetch(limit=3)
        self.assertEqual(len(result), 2)
        self.assertEqual(self.session.get.call_count, 1)
        self.assertFalse(self.client.details_identity_available)

    def acknowledged_posts(self, *, before=None, after=None):
        source = row(rawXml="")
        normalized = self.module.normalize_post(source)
        before_post = self.module.enrich_post(normalized, native_detail(source, comments=before or []))
        after_post = self.module.enrich_post(normalized, native_detail(source, comments=after or []))
        return before_post, after_post

    def test_read_exact_post_details_always_reads_db_without_timeline_or_capability_cache(self):
        source = self.module.enrich_post(self.module.normalize_post(row(rawXml="")), native_detail())
        comment = native_comment(username="wxid_self", content="确认全文", refCommentId="", refUsername="")
        response = Response(details_payload([native_detail(comments=[comment])]))
        self.session.get.return_value = response
        # Client is deliberately cached "details unavailable"; acknowledgement
        # must still perform a fresh read after the irreversible UI action.
        result = self.client.read_post_details(source, bot_id="wxid_self")
        self.assertEqual(result["comments"][0]["author_id"], "wxid_self")
        self.assertTrue(result["interaction_identity_verified"])
        self.assertEqual(self.session.get.call_count, 1)
        self.assertTrue(self.session.get.call_args.args[0].endswith("/api/v1/sns-identity-details"))
        self.assertEqual(self.session.get.call_args.kwargs["params"]["post_ids"], "10001")
        self.assertTrue(response.closed)

    def test_exact_post_read_has_no_legacy_fallback_for_missing_or_incomplete_identity(self):
        source = self.module.enrich_post(self.module.normalize_post(row(rawXml="")), native_detail())
        for response in (Response({}, code=404), Response(details_payload([native_detail(commentsVerified=False)]))):
            with self.subTest(response=response):
                self.session.get.reset_mock()
                self.session.get.return_value = response
                with self.assertRaises(self.module.FeedError):
                    self.client.read_post_details(source)
                self.assertEqual(self.session.get.call_count, 1)
                self.assertTrue(response.closed)

    def test_exact_read_validates_source_and_cancellation_before_http(self):
        source = self.module.normalize_post(row(rawXml=""))
        cancel = threading.Event(); cancel.set()
        with self.assertRaisesRegex(self.module.FeedError, "取消"):
            self.client.read_post_details(source, cancel_event=cancel)
        for fields in ({"post_id": "opaque-real-id"}, {"identity_verified": False}, {"author_id": "显示名称"}):
            with self.assertRaises(self.module.FeedError):
                self.client.read_post_details(dict(source, **fields))
        self.session.get.assert_not_called()

    def test_cancellation_after_details_response_close_does_not_return_evidence(self):
        source = self.module.enrich_post(self.module.normalize_post(row(rawXml="")), native_detail())
        cancel = threading.Event()
        response = Response(details_payload([native_detail()]))
        def close():
            response.closed = True
            cancel.set()
        response.close = close
        self.session.get.return_value = response
        with self.assertRaisesRegex(self.module.FeedError, "取消"):
            self.client.read_post_details(source, cancel_event=cancel)
        self.assertTrue(response.closed)

    def test_unique_new_native_self_comment_acknowledges_exact_full_text(self):
        old = native_comment(id="20001", username="wxid_self", content="确认全文", refCommentId="", refUsername="")
        new = dict(old, id="20002")
        source, fresh = self.acknowledged_posts(before=[old], after=[old, new])
        result = self.module.find_new_self_comment(source, fresh, bot_id="wxid_self", text="确认全文")
        self.assertEqual(result["comment_id"], "20002")
        self.assertEqual(result["author_id"], "wxid_self")
        self.assertIsNone(self.module.find_new_self_comment(source, fresh, bot_id="wxid_self", text="确认"))

    def test_existing_matching_comment_or_explicit_pre_set_never_acknowledges(self):
        comment = native_comment(username="wxid_self", content="确认全文", refCommentId="", refUsername="")
        source, fresh = self.acknowledged_posts(before=[comment], after=[comment])
        self.assertIsNone(self.module.find_new_self_comment(source, fresh, bot_id="wxid_self", text="确认全文"))
        source, fresh = self.acknowledged_posts(after=[comment])
        self.assertIsNone(self.module.find_new_self_comment(source, fresh, bot_id="wxid_self", text="确认全文", previous_comment_ids={"20001"}))

    def test_acknowledgement_requires_before_and_after_complete_identities(self):
        comment = native_comment(username="wxid_self", content="确认全文", refCommentId="", refUsername="")
        source, fresh = self.acknowledged_posts(after=[comment])
        for side in ("before", "after"):
            for fields in ({"interaction_identity_verified": False}, {"identity_verified": False},
                           {"post_id": "10002"}, {"author_id": "wxid_wrong"}, {"content": "不同动态"}):
                before, after = (dict(source, **fields), fresh) if side == "before" else (source, dict(fresh, **fields))
                with self.subTest(side=side, fields=fields):
                    self.assertIsNone(self.module.find_new_self_comment(before, after, bot_id="wxid_self", text="确认全文"))

    def test_acknowledgement_refuses_actor_missing_synthetic_id_and_duplicates(self):
        comment = native_comment(username="wxid_self", content="确认全文", refCommentId="", refUsername="")
        source, fresh = self.acknowledged_posts(after=[comment])
        original = fresh["comments"][0]
        for fields in ({"author_id": ""}, {"identity_verified": False}, {"comment_id": "cmt_1791719990_0.23"},
                       {"author_id": "wxid_other"}):
            altered = dict(fresh, comments=[dict(original, **fields)])
            with self.subTest(fields=fields):
                self.assertIsNone(self.module.find_new_self_comment(source, altered, bot_id="wxid_self", text="确认全文"))
        for comments in ([original, original], [original, dict(original, comment_id="20002")]):
            self.assertIsNone(self.module.find_new_self_comment(source, dict(fresh, comments=comments), bot_id="wxid_self", text="确认全文"))

    def test_reply_acknowledgement_requires_original_real_target_comment_id(self):
        target = native_comment(id="20001", username="wxid_other", content="原评论", refCommentId="", refUsername="")
        reply = native_comment(id="20002", username="wxid_self", content="回复全文", refCommentId="20001", refUsername="wxid_other")
        source, fresh = self.acknowledged_posts(before=[target], after=[target, reply])
        result = self.module.find_new_self_comment(source, fresh, bot_id="wxid_self", text="回复全文", reply_comment_id="20001")
        self.assertEqual(result["comment_id"], "20002")
        self.assertEqual(result["ref_comment_id"], "20001")
        self.assertIsNone(self.module.find_new_self_comment(source, fresh, bot_id="wxid_self", text="回复全文"))
        self.assertIsNone(self.module.find_new_self_comment(source, fresh, bot_id="wxid_self", text="回复全文", reply_comment_id="19999"))
        for fields in ({"ref_comment_id": "20000"}, {"ref_author_id": "wxid_wrong"}):
            with self.subTest(fields=fields):
                altered = dict(fresh, comments=[fresh["comments"][0], dict(fresh["comments"][1], **fields)])
                self.assertIsNone(self.module.find_new_self_comment(source, altered, bot_id="wxid_self", text="回复全文", reply_comment_id="20001"))


if __name__ == "__main__":
    unittest.main()
