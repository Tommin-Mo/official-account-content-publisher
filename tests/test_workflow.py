import copy
import hashlib
import json
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))

from browser_skill import BskClient, BrowserSkillBackend, LocalHtmlBridge
from publisher import (ContentValidator, PublisherError, article_review_hash, content_hash,
                       finalize_bundle, layout_profile, render_html, review_bundle)


def jpeg(width=900, height=383):
    segment = b"\xff\xc0\x00\x11\x08" + height.to_bytes(2, "big") + width.to_bytes(2, "big") + b"\x03\x01\x11\x00\x02\x11\x00\x03\x11\x00"
    return b"\xff\xd8" + segment + b"\xff\xd9" + b"x" * 256


def reviewed_bundle(kind="brief"):
    words = "这是经过来源核对的事实，用于说明读者应该如何行动。" * 30
    value = {
        "schema_version": "1.1",
        "brief": {"theme": "内容工作流", "audience": "内容团队", "content_type": kind, "intent": "方法分享", "target_length": 600, "voice_profile": "清晰", "conversion_goal": "none"},
        "sources": [{"id": "S1", "url": "https://example.com/a", "title": "来源", "publisher": "示例研究机构", "published_at": "2026-09-02", "retrieved_at": "2026-09-03"}],
        "facts": [{"id": "F1", "claim": "经核对的事实", "source_id": "S1", "date": "2026-09-02", "scope": "公开样本", "unit": "篇", "confidence": "high"}],
        "insights": [],
        "article": {"title_candidates": ["内容工作流"], "selected_title": "内容工作流", "summary": "将事实转为行动。", "author": "", "body_blocks": [
            {"type": "h2", "text": "从证据出发"}, {"type": "paragraph", "text": words, "fact_refs": ["F1"]},
            {"type": "list", "items": ["先核验来源", "再解释机制"]}], "cta": {"primary": "none", "secondary": "none"}},
        "visual": {"cover_prompt": "", "provider": "gpt", "cover_path": "", "aspect_ratio": "2.35:1"},
        "delivery": {"account_alias": "测试账号", "comments": "open", "fans_only": False, "source_url": "", "delivery_choice": "draft"},
        "warnings": [], "article_review_hash": "", "content_hash": "",
    }
    return review_bundle(value)


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.temp.cleanup()

    def finalize(self, bundle=None):
        image = Path(self.temp.name) / "cover.jpg"; image.write_bytes(jpeg())
        return finalize_bundle(bundle or reviewed_bundle(), str(image), "gpt")

    def test_review_does_not_require_cover(self):
        value = reviewed_bundle()
        self.assertEqual(value["stage"], "content_reviewed")
        self.assertTrue(value["article_review_hash"])
        self.assertFalse(value["content_hash"])

    def test_finalization_binds_cover_and_hash(self):
        value = self.finalize()
        self.assertEqual(value["stage"], "finalized")
        self.assertTrue(value["content_hash"])
        self.assertEqual(ContentValidator().validate_final(value), [])

    def test_article_change_invalidates_review_hash(self):
        value = reviewed_bundle(); value["article"]["summary"] = "改过的摘要"
        with self.assertRaisesRegex(PublisherError, "审核后被修改"):
            self.finalize(value)

    def test_cover_change_invalidates_final_hash(self):
        value = self.finalize(); value["visual"]["provider"] = "workbuddy"
        with self.assertRaisesRegex(PublisherError, "审核后被修改"):
            ContentValidator().validate_final(value)

    def test_unknown_fact_ref_rejected(self):
        value = reviewed_bundle(); value["article"]["body_blocks"][1]["fact_refs"] = ["F999"]
        with self.assertRaisesRegex(PublisherError, "不存在"):
            ContentValidator().validate_review(value)

    def test_unreferenced_fact_rejected(self):
        value = reviewed_bundle(); value["article"]["body_blocks"][1]["fact_refs"] = []
        with self.assertRaisesRegex(PublisherError, "明确引用"):
            ContentValidator().validate_review(value)

    def test_direct_quote_must_remain_exact(self):
        value = reviewed_bundle(); value["facts"][0]["quote"] = "不可替换的引文"
        with self.assertRaisesRegex(PublisherError, "直接引用"):
            ContentValidator().validate_review(value)

    def test_analysis_cannot_reuse_one_source_as_two_evidence_clusters(self):
        value = reviewed_bundle("brief")
        value["brief"]["content_type"] = "analysis"
        value["article"]["body_blocks"][1]["text"] *= 12
        value["insights"] = [
            {"id": "I1", "evidence_refs": ["F1"], "evidence_cluster": "C1", "data_relation": "关系", "mechanism": "机制", "alternative": "替代解释", "limitation": "限制", "implication": "影响", "direction": "方向", "confidence": "high"},
            {"id": "I2", "evidence_refs": ["F1"], "evidence_cluster": "C2", "data_relation": "关系", "mechanism": "机制", "alternative": "替代解释", "limitation": "限制", "implication": "影响", "direction": "方向", "confidence": "high"},
        ]
        with self.assertRaisesRegex(PublisherError, "独立来源"):
            ContentValidator().validate_review(value)

    def test_list_requires_items_not_text(self):
        value = reviewed_bundle(); value["article"]["body_blocks"][-1] = {"type": "list", "text": "假的列表"}
        with self.assertRaisesRegex(PublisherError, "列表块"):
            ContentValidator().validate_review(value)

    def test_short_prewrite_article_is_a_warning_not_a_delivery_blocker(self):
        value = reviewed_bundle(); value["article"]["body_blocks"][1]["text"] = "太短"
        warnings = ContentValidator().validate_review(value)
        self.assertTrue(any("推荐区间" in warning for warning in warnings))

    def test_renderer_has_profile_specific_palette(self):
        blocks = [{"type": "quote", "text": "证据"}]
        self.assertNotEqual(render_html(blocks, "analysis"), render_html(blocks, "narrative"))

    def test_all_content_types_route_to_a_profile(self):
        expected = {"analysis", "tutorial", "campaign", "narrative"}
        for kind in ("brief", "engagement", "event", "product", "case", "tutorial", "analysis", "interview"):
            self.assertIn(layout_profile(kind), expected)

    def test_html_cannot_render_local_image_without_upload(self):
        with self.assertRaises(PublisherError):
            render_html([{"type": "image", "path": "/private/test.jpg"}])

    def test_review_hash_excludes_cover_output(self):
        value = reviewed_bundle(); before = article_review_hash(value)
        value["visual"]["cover_path"] = "/different-cover.jpg"
        self.assertEqual(before, article_review_hash(value))

    def test_content_hash_includes_cover_output(self):
        value = self.finalize(); before = content_hash(value)
        value["visual"]["cover_hash"] = "changed"
        self.assertNotEqual(before, content_hash(value))


class FakeBsk:
    def __init__(self): self.started = 0
    def ready(self): return {"browsers": [{"instance_id": "one"}]}
    def start(self): self.started += 1; return "s1"
    def stop(self, _): return True
    def navigate(self, *_): return None
    def evaluate(self, *_): return {"value": "https://mp.weixin.qq.com/cgi-bin/appmsg"}


class BrowserPreflightTests(unittest.TestCase):
    def browser_bundle(self, directory):
        image = Path(directory) / "cover.jpg"; image.write_bytes(jpeg())
        return finalize_bundle(reviewed_bundle(), str(image), "gpt")

    def test_invalid_cover_rejects_before_session_start(self):
        client = FakeBsk(); backend = BrowserSkillBackend(client=client)
        with self.assertRaisesRegex(PublisherError, "图片文件"):
            backend.preflight(reviewed_bundle())
        self.assertEqual(client.started, 0)

    def test_browser_publication_is_blocked_before_session_start(self):
        client = FakeBsk()
        with tempfile.TemporaryDirectory() as d:
            backend = BrowserSkillBackend(client=client)
            with self.assertRaisesRegex(PublisherError, "公开发布"):
                backend.deliver(self.browser_bundle(d), "publish", True)
        self.assertEqual(client.started, 0)

    def test_native_cover_upload_uses_a_selector_and_local_file_once(self):
        calls = []
        class Client(BskClient):
            def __init__(self): self.executable = "test"
            def _run(self, *args, **kwargs): calls.append((args, kwargs)); return type("Result", (), {"value": {}})()
        with tempfile.TemporaryDirectory() as d:
            cover = Path(d) / "cover.jpg"; cover.write_bytes(jpeg())
            Client().upload("s1", "input[type=file]", cover)
        self.assertEqual(calls[0][0][:2], ("upload", "--session"))
        self.assertIn("--file", calls[0][0])
        self.assertNotIn("evaluate", calls[0][0])

    def test_evaluate_passes_a_shorter_daemon_timeout(self):
        calls = []
        class Client(BskClient):
            def __init__(self): self.executable = "test"
            def _run(self, *args, **kwargs): calls.append((args, kwargs)); return type("Result", (), {"value": {}})()
        Client().evaluate("s1", "1+1", timeout=3)
        self.assertEqual(calls[0][0], ("evaluate", "--session", "s1", "--timeout", "2s", "1+1"))
        self.assertEqual(calls[0][1]["timeout"], 3)

    def test_html_bridge_serves_exact_final_markup(self):
        markup = render_html([{"type": "h2", "text": "标题"}, {"type": "list", "items": ["第一步", "第二步"]}])
        with LocalHtmlBridge(markup) as url:
            response = urllib.request.urlopen(url, timeout=2)
            self.assertEqual(response.read().decode(), markup)
            self.assertEqual(response.headers.get_content_type(), "text/html")


class BrowserTabSelectionTests(unittest.TestCase):
    class Client(BskClient):
        def __init__(self, response): self.response = response
        def _run(self, *_args, **_kwargs): return type("Result", (), {"value": self.response})()

    def test_logged_wechat_tab_selects_only_the_official_account_tab(self):
        client = self.Client({"tabs": [{"tab_id": 1, "url": "https://docs.qq.com/x"}, {"tab_id": 2, "url": "https://mp.weixin.qq.com/cgi-bin/appmsg?token=valid"}]})
        self.assertEqual(client.logged_wechat_tab("s1"), "2")

    def test_expired_wechat_tab_is_rejected_before_borrowing(self):
        client = self.Client({"tabs": [{"tab_id": 2, "url": "https://mp.weixin.qq.com/cgi-bin/appmsg?action=list"}]})
        with self.assertRaisesRegex(PublisherError, "登录已失效") as raised:
            client.logged_wechat_tab("s1")
        self.assertEqual(raised.exception.code, "wechat_login_expired")

    def test_multiple_logged_wechat_tabs_are_rejected(self):
        client = self.Client({"tabs": [{"tab_id": 1, "url": "https://mp.weixin.qq.com/cgi-bin/a"}, {"tab_id": 2, "url": "https://mp.weixin.qq.com/cgi-bin/b"}]})
        with self.assertRaisesRegex(PublisherError, "多个公众号"):
            client.logged_wechat_tab("s1")


class BrowserReadyProbeTests(unittest.TestCase):
    class Client(BskClient):
        def __init__(self, statuses): self.statuses = iter(statuses)
        def _run(self, *_args, **_kwargs): return type("Result", (), {"value": next(self.statuses)})()

    def test_ready_waits_through_one_empty_probe_then_requires_three_stable_probes(self):
        connected = {"browsers": [{"instance_id": "one"}]}
        client = self.Client([{"browsers": []}, connected, connected, connected])
        with patch("browser_skill.time.sleep"):
            self.assertEqual(client.ready(), connected)

    def test_visible_button_match_ignores_hidden_duplicate_controls(self):
        expression = BrowserSkillBackend._visible_matches("确认")
        self.assertIn("getBoundingClientRect", expression)
        self.assertIn("r.width>10", expression)

    def test_cover_verification_targets_wechat_background_preview(self):
        source = Path(__file__).parents[1] / "scripts" / "browser_skill.py"
        self.assertIn("#js_cover_area .js_cover_preview_new", source.read_text(encoding="utf-8"))

    def test_title_write_uses_visible_editor_and_readback_checks_both_title_fields(self):
        source = (Path(__file__).parents[1] / "scripts" / "browser_skill.py").read_text(encoding="utf-8")
        self.assertIn('self.client.fill(session, \'div.ProseMirror[contenteditable=true][data-placeholder="请在这里输入标题"]\'', source)
        self.assertIn("visibleTitles", source)


class BorrowedTabNavigationTests(unittest.TestCase):
    def test_window_diagnostics_strip_query_tokens(self):
        self.assertEqual(BrowserSkillBackend._safe_url("https://mp.weixin.qq.com/cgi-bin/home?token=secret"),
                         "https://mp.weixin.qq.com/cgi-bin/home")

    def test_editor_navigation_retains_the_borrowed_tab_token_in_page_memory(self):
        backend = BrowserSkillBackend(client=FakeBsk())
        calls = []
        backend._eval = lambda _session, expression: (calls.append(expression) or {"ok": True})
        backend._wait_until = lambda *_args, **_kwargs: True
        backend._require_click = lambda *_args, **_kwargs: None
        backend._go_to_editor("s1")
        self.assertIn("new URL(location.href).searchParams.get('token')", calls[0])
        self.assertIn("location.assign('/cgi-bin/home?token='", calls[0])

    def test_editor_navigation_rejects_a_borrowed_tab_without_a_session_token(self):
        backend = BrowserSkillBackend(client=FakeBsk())
        backend._eval = lambda *_args: {"ok": False}
        with self.assertRaisesRegex(PublisherError, "缺少有效会话"):
            backend._go_to_editor("s1")

    def test_account_identity_waits_for_a_stable_header_before_mismatch(self):
        backend = BrowserSkillBackend(client=FakeBsk())
        backend._eval = lambda *_args: False
        backend._wait_until = lambda *_args, **_kwargs: False
        with self.assertRaises(PublisherError) as raised:
            backend._verify_account("s1", "测试账号")
        self.assertEqual(raised.exception.code, "account_identity_unreadable")
        self.assertTrue(raised.exception.retry_allowed)

    def test_expired_login_is_rejected_before_account_matching(self):
        backend = BrowserSkillBackend(client=FakeBsk())
        backend._eval = lambda *_args: True
        with self.assertRaises(PublisherError) as raised:
            backend._verify_account("s1", "测试账号")
        self.assertEqual(raised.exception.code, "browser_login_required")

    def test_account_mismatch_is_retryable_when_nothing_was_written(self):
        backend = BrowserSkillBackend(client=FakeBsk())
        backend._eval = lambda *_args: False
        backend._wait_until = lambda *_args, **_kwargs: {"loaded": True, "matched": False, "count": 1}
        with self.assertRaises(PublisherError) as raised:
            backend._verify_account("s1", "测试账号")
        self.assertEqual(raised.exception.code, "account_mismatch")
        self.assertTrue(raised.exception.retry_allowed)

    def test_multiple_account_labels_are_ambiguous_not_a_mismatch(self):
        backend = BrowserSkillBackend(client=FakeBsk())
        backend._eval = lambda *_args: False
        backend._wait_until = lambda *_args, **_kwargs: {"loaded": True, "matched": False, "count": 2}
        with self.assertRaises(PublisherError) as raised:
            backend._verify_account("s1", "测试账号")
        self.assertEqual(raised.exception.code, "account_identity_ambiguous")
        self.assertTrue(raised.exception.retry_allowed)

    def test_account_identity_checks_the_current_wechat_header_selector(self):
        backend = BrowserSkillBackend(client=FakeBsk())
        expressions = []
        backend._eval = lambda *_args: False
        backend._wait_until = lambda _session, expression, **_kwargs: (expressions.append(expression) or {"loaded": True, "matched": True})
        backend._verify_account("s1", "测试账号")
        self.assertIn(".acount_box-nickname", expressions[0])
        self.assertNotIn(".mp_account_box", expressions[0])
        self.assertIn("normalize('NFKC')", expressions[0])
        self.assertIn("new Set", expressions[0])


class DraftPreviewVerificationTests(unittest.TestCase):
    def test_draft_preview_verifies_title_and_rendered_body_without_editor_selectors(self):
        backend = BrowserSkillBackend(client=FakeBsk())
        backend._eval = lambda *_args: {
            "titles": ["验收标题"],
            "body": "这是正文第一段。 第二段包含列表信息。",
        }
        self.assertTrue(backend._preview_read_back("s1", "验收标题", "<p>这是正文第一段。</p><p>第二段包含列表信息。</p>"))

    def test_draft_preview_rejects_title_only_match(self):
        backend = BrowserSkillBackend(client=FakeBsk())
        backend._eval = lambda *_args: {"titles": ["验收标题"], "body": "验收标题"}
        self.assertFalse(backend._preview_read_back("s1", "验收标题", "<p>不能缺少的正文</p>"))

    def test_verify_draft_requires_reopened_editor_readback(self):
        backend = BrowserSkillBackend(client=FakeBsk())
        calls = []
        responses = iter([{"ok": True}, {"ok": True, "draft_id": "draft-new"}])
        backend._eval = lambda _session, expression, **_kwargs: (calls.append(expression) or next(responses))
        waits = []
        backend._wait_until = lambda _session, expression, **_kwargs: (waits.append(expression) or True)
        backend._reopened_editor_read_back = lambda *_args: True
        result = backend._verify_draft("s1", "验收标题", "摘要", "<p>正文</p>", set())
        self.assertEqual(result, {"draft_id": "draft-new"})
        self.assertFalse(any("h1" in expression for expression in waits))

    def test_prewrite_timeout_allows_retry(self):
        with patch("browser_skill.time.monotonic", return_value=91):
            with self.assertRaises(PublisherError) as raised:
                BrowserSkillBackend._deadline(0, 90, write_attempted=False)
        self.assertEqual(raised.exception.code, "browser_workflow_timeout")
        self.assertTrue(raised.exception.retry_allowed)

    def test_post_save_timeout_forbids_repeat_submission(self):
        with patch("browser_skill.time.monotonic", return_value=151):
            with self.assertRaises(PublisherError) as raised:
                BrowserSkillBackend._deadline(0, 150, write_attempted=True)
        self.assertEqual(raised.exception.code, "browser_workflow_timeout")
        self.assertFalse(raised.exception.retry_allowed)

    def test_poll_budget_caps_each_bridge_probe_and_stops_at_total_deadline(self):
        clock = [0.0]
        class PollClient:
            def __init__(self): self.timeouts, self.waits = [], 0
            def evaluate(self, _session, _expression, *, timeout=0): self.timeouts.append(timeout); return False
            def wait_ms(self, _milliseconds): self.waits += 1; clock[0] = 13.0
        client = PollClient(); backend = BrowserSkillBackend(client=client)
        with patch("browser_skill.time.monotonic", side_effect=lambda: clock[0]):
            self.assertIsNone(backend._wait_until("s1", "false", timeout_ms=12_000))
        self.assertEqual(client.timeouts, [3])
        self.assertEqual(client.waits, 1)


if __name__ == "__main__": unittest.main()
