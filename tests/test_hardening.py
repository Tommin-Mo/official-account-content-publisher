"""Adversarial regression cases for durable delivery and artifact integrity."""
from __future__ import annotations

import base64
import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))

from browser_skill import BskClient, BrowserSkillBackend
from cover import generate_cover
from publisher import AtomicTaskStore, ContentValidator, Publisher, PublisherError, WechatAPI, content_hash
from test_publisher import bundle, jpeg


class DurableWriteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = AtomicTaskStore(Path(self.temp.name))

    def tearDown(self):
        self.temp.cleanup()

    def test_response_loss_after_api_dispatch_is_intent_marked_and_blocked(self):
        def transport(_path, _payload):
            raise PublisherError("backend_error", "offline", True)
        with self.assertRaises(PublisherError) as raised:
            Publisher(self.store, lambda: WechatAPI("opaque-token", transport)).deliver(bundle(), "draft", "api")
        self.assertEqual(raised.exception.code, "draft_uncertain")
        task = json.loads(next(self.store.root.glob("wx-*.json")).read_text())
        self.assertTrue(task["effects"]["draft_submit_intent"])
        self.assertEqual(task["stage"], "draft_uncertain")
        with self.assertRaisesRegex(PublisherError, "高风险任务"):
            self.store.create(bundle())

    def test_finalized_task_blocks_a_second_process_before_dispatch(self):
        self.store.create(bundle())
        with self.assertRaisesRegex(PublisherError, "高风险任务"):
            self.store.create(bundle())

    def test_task_id_cannot_escape_ledger_directory(self):
        for value in ("../outside", "wx-123", "wx-0123456789abcdef/../x"):
            with self.assertRaises(PublisherError):
                self.store.path(value)

    def test_browser_intent_reclassifies_retryable_adapter_error(self):
        class Backend:
            def preflight(self, _bundle): return {"status": "ready"}
            def deliver(self, _bundle, _delivery, _confirmed, *, on_effect=None, readiness=None):
                on_effect("draft_submit_intent", True)
                raise PublisherError("browser_command_failed", "bridge lost", True)
        with self.assertRaises(PublisherError) as raised:
            Publisher(self.store, browser_backend=Backend()).deliver(bundle(), "draft", "browser")
        self.assertEqual(raised.exception.code, "draft_uncertain")
        self.assertFalse(raised.exception.retry_allowed)

    def test_browser_cannot_claim_drafted_without_durable_confirmation(self):
        class Backend:
            def preflight(self, _bundle): return {"status": "ready"}
            def deliver(self, _bundle, _delivery, _confirmed, *, on_effect=None, readiness=None):
                on_effect("draft_submit_intent", True)
                return {"stage": "drafted", "draft_id": "d1", "account_alias": "test",
                        "title": "内容工作流的第一步", "fields_read_back": True,
                        "session_count": 1, "about_blank_seen": False}
        with self.assertRaises(PublisherError) as raised:
            Publisher(self.store, browser_backend=Backend()).deliver(bundle(), "draft", "browser")
        self.assertEqual(raised.exception.code, "draft_uncertain")

    def test_browser_success_persists_safe_timing_and_cleanup_diagnostics(self):
        class Backend:
            def preflight(self, _bundle): return {"status": "ready"}
            def deliver(self, _bundle, _delivery, _confirmed, *, on_effect=None, readiness=None):
                on_effect("draft_submit_intent", True); on_effect("draft_save_confirmed", True)
                return {"stage": "drafted", "draft_id": "d1", "account_alias": "test",
                        "title": "内容工作流的第一步", "fields_read_back": True, "session_count": 1,
                        "about_blank_seen": False, "timing_ms": {"editor_ready": 10},
                        "human_wait_ms": 4, "total_elapsed_ms": 14,
                        "cleanup": {"tab_returned": True, "session_stopped": True, "session_preserved": False}}
        outcome = Publisher(self.store, browser_backend=Backend()).deliver(bundle(), "draft", "browser")
        self.assertEqual(outcome.detail["human_wait_ms"], 4)
        self.assertTrue(outcome.task["diagnostics"]["cleanup"]["session_stopped"])

    def test_browser_verify_is_read_only_and_uses_existing_task_identity(self):
        task = self.store.create(bundle())
        task.update({"stage": "drafted", "backend": "browser", "draft_id": "draft1"}); self.store.save(task)
        class Backend:
            def verify_draft(self, candidate):
                self.seen = candidate
                return {"verified": True}
        backend = Backend()
        result = Publisher(self.store, browser_backend=backend).verify(task["task_id"])
        self.assertEqual(result["verification"], "browser_readonly_reopen")
        self.assertEqual(backend.seen["draft_id"], "draft1")


class BrowserErrorDiagnosticsTests(unittest.TestCase):
    def test_cdp_failure_is_classified_as_an_unreadable_page(self):
        class Completed:
            returncode = 1
            stdout = '{"code":"cdp_failed"}'
            stderr = ""
        with patch("browser_skill.subprocess.run", return_value=Completed()):
            with self.assertRaises(PublisherError) as raised:
                BskClient(executable="bsk")._run("evaluate", "--session", "private", "1+1")
        self.assertEqual(raised.exception.code, "browser_page_unreadable")
        self.assertTrue(raised.exception.retry_allowed)

    def test_browser_timeout_exposes_safe_operation(self):
        with patch("browser_skill.subprocess.run", side_effect=subprocess.TimeoutExpired("bsk", 1)):
            with self.assertRaises(PublisherError) as raised:
                BskClient(executable="bsk")._run("tab", "borrow", "--session", "private")
        self.assertEqual(raised.exception.code, "browser_timeout")
        self.assertEqual(raised.exception.diagnostics, {"browser_operation": "tab.borrow"})

    def test_borrow_confirmation_timeout_is_not_reported_as_a_generic_failure(self):
        class Completed:
            returncode = 1
            stdout = '{"code":"timeout"}'
            stderr = ""
        with patch("browser_skill.subprocess.run", return_value=Completed()):
            with self.assertRaises(PublisherError) as raised:
                BskClient(executable="bsk")._run("tab", "borrow", "--session", "private", "tab")
        self.assertEqual(raised.exception.code, "tab_borrow_confirmation_required")
        self.assertTrue(raised.exception.retry_allowed)

    def test_browser_command_failure_exposes_only_safe_operation_and_signature(self):
        class Completed:
            returncode = 1
            stdout = '{"code":"bridge_timeout","message":"sensitive page text"}'
            stderr = "sensitive stderr"
        with patch("browser_skill.subprocess.run", return_value=Completed()):
            with self.assertRaises(PublisherError) as raised:
                BskClient(executable="bsk")._run("evaluate", "--session", "private")
        self.assertEqual(raised.exception.code, "browser_command_failed")
        self.assertEqual(raised.exception.diagnostics, {
            "browser_operation": "evaluate", "browser_error_signature": "bridge_timeout"})


class FinalArtifactTests(unittest.TestCase):
    def test_replaced_cover_is_rejected_even_when_bundle_hash_is_recomputed(self):
        value = bundle()
        Path(value["visual"]["cover_path"]).write_bytes(jpeg(900, 383) + b"changed")
        value["content_hash"] = content_hash(value)
        with self.assertRaisesRegex(PublisherError, "封面文件在冻结后"):
            ContentValidator().validate_final(value)

    def test_numeric_fact_cannot_be_rewritten_in_its_referenced_block(self):
        value = bundle()
        value["facts"][0]["claim"] = "市场渗透率为10%"
        value["facts"][0].pop("quote", None)
        value["article"]["body_blocks"][-1]["text"] = "市场渗透率为90%。" * 40
        value["article_review_hash"] = __import__("publisher").article_review_hash(value)
        value["content_hash"] = content_hash(value)
        with self.assertRaisesRegex(PublisherError, "核心数字"):
            ContentValidator().validate_final(value)

    def test_preview_rejects_extra_article_content(self):
        backend = BrowserSkillBackend(client=object())
        backend._eval = lambda *_args, **_kwargs: {"titles": ["标题"], "body": "正文 额外插入段落", "root_count": 1}
        self.assertFalse(backend._preview_read_back("s", "标题", "<p>正文</p>"))

    def test_browser_preflight_blocks_unimplemented_requested_field(self):
        class Client:
            def ready(self): return {"browsers": [{}]}
        value = bundle(); value["article"]["author"] = "作者"
        with self.assertRaisesRegex(PublisherError, "作者"):
            BrowserSkillBackend(client=Client()).preflight(value)

    def test_same_origin_cannot_appear_as_two_independent_analysis_sources(self):
        value = bundle("analysis")
        value["article"]["body_blocks"][-1]["text"] *= 12
        value["sources"].append({"id": "S2", "url": "https://example.com/report?mirror=1", "title": "镜像", "publisher": "示例研究机构", "published_at": "2026-09-01", "retrieved_at": "2026-09-02"})
        value["facts"].append({"id": "F2", "claim": "第二条事实", "source_id": "S2", "date": "2026-09-01", "scope": "样本调查", "unit": "篇", "confidence": "high"})
        value["article"]["body_blocks"].append({"type": "paragraph", "text": "第二条事实用于检验来源。", "fact_refs": ["F2"]})
        value["insights"] = [
            {"id": "I1", "evidence_refs": ["F1"], "evidence_cluster": "A", "data_relation": "相关", "mechanism": "机制", "alternative": "反方", "limitation": "限制", "implication": "影响", "direction": "方向", "confidence": "high"},
            {"id": "I2", "evidence_refs": ["F2"], "evidence_cluster": "B", "data_relation": "相关", "mechanism": "机制", "alternative": "反方", "limitation": "限制", "implication": "影响", "direction": "方向", "confidence": "high"},
        ]
        value["article_review_hash"] = __import__("publisher").article_review_hash(value)
        value["content_hash"] = content_hash(value)
        with self.assertRaisesRegex(PublisherError, "独立来源"):
            ContentValidator().validate_final(value)

    def test_bad_generated_cover_never_replaces_existing_file(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "cover.jpg"; original = jpeg(); target.write_bytes(original)
            with self.assertRaises(PublisherError):
                generate_cover(bundle(), "gpt", "gpt-image-2", str(target), confirmed=True, api_key="opaque",
                               transport=lambda *_: {"data": [{"b64_json": base64.b64encode(b"bad").decode()}]})
            self.assertEqual(target.read_bytes(), original)

    def test_runtime_version_mismatch_is_rejected_before_status_probe(self):
        class Client(BskClient):
            def __init__(self): self.executable = "test"; self.calls = 0
            def runtime_version(self): return "9.9.9"
            def _run(self, *_args, **_kwargs): self.calls += 1; raise AssertionError("status must not run")
        client = Client()
        with self.assertRaisesRegex(PublisherError, "版本"):
            client.ready()
        self.assertEqual(client.calls, 0)


if __name__ == "__main__":
    unittest.main()
