#!/usr/bin/env python3
"""Adversarial tests for the native bsk draft runner.

Every scenario runs against a stateful FakeBsk that emulates the real page
state machine (draft list → editor → save → draft list → reopen) without any
network or browser.  The suite proves the runner's fail-closed behaviour:

- account mismatch / unreadable identity → no write, no session left behind;
- ambiguous save button → refuse before persisting any intent;
- transport loss after the save click → draft_uncertain, never re-sent;
- same-name old drafts → baseline exclusion; if the new card cannot be
  uniquely identified the outcome is draft_uncertain;
- cover not bound / body drift → blocked BEFORE the save click;
- delivery_choice != draft → refused before any session is created;
- group-send / publish / delete controls are never clicked;
- the borrowed tab is verified back in the user window before session stop.
"""
from __future__ import annotations

import json
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import native_runner as nr  # noqa: E402
from native_runner import BskClient, NativeDraftRunner, NativeRunnerError  # noqa: E402

ALIAS = "验收测试号"
TITLE = "SMP草稿验收测试"
DIGEST = "测试摘要文本。"
BODY_HTML = "<h2>检查项</h2><p>第一段文字。</p><ul><li>项目一</li><li>项目二</li></ul>"


def make_bundle(tmp_path: Path, delivery_choice: str = "draft") -> Path:
    bundle = {
        "schema_version": "1.1",
        "stage": "finalized",
        "brief": {"content_type": "brief"},
        "article": {
            "selected_title": TITLE,
            "summary": DIGEST,
            "author": "",
            "body_blocks": [
                {"type": "h2", "text": "检查项"},
                {"type": "paragraph", "text": "第一段文字。"},
                {"type": "list", "items": ["项目一", "项目二"]},
            ],
        },
        "delivery": {"account_alias": ALIAS, "delivery_choice": delivery_choice,
                     "comments": "open", "fans_only": False, "source_url": ""},
        "visual": {"cover_path": None},
    }
    path = tmp_path / "bundle.json"
    path.write_text(json.dumps(bundle, ensure_ascii=False), encoding="utf-8")
    return path


def make_cover(tmp_path: Path) -> Path:
    # a minimal PNG: magic bytes only; validate_local_image checks the header
    cover = tmp_path / "cover.png"
    cover.write_bytes(
        b"\x89PNG\r\n\x1a\n" + b"0" * 64
    )
    return cover


def list_html(cards: list[dict[str, str]]) -> str:
    parts = []
    for card in cards:
        parts.append(
            f'<div data-appid="{card["appid"]}" class="weui-desktop-card weui-desktop-publish">'
            f'<a class="weui-desktop-publish__cover__title"><span>{card["title"]}</span></a>'
            f'<div class="weui-desktop-card__action"><span class="weui-desktop-tooltip">编辑</span>'
            f'<a class="weui-desktop-icon-btn"></a>'
            f'<span class="weui-desktop-tooltip">删除</span>'
            f'<a class="weui-desktop-link weui-desktop-link_send-multi"></a></div></div>'
        )
    return "<html><body>" + "".join(parts) + "</body></html>"


def editor_html(title: str, body_html: str | None, cover_bound: bool,
                crop_step: int = 0) -> str:
    title_node = (
        f'<textarea id="title"></textarea>'
        f'<div contenteditable="true" translate="no" class="ProseMirror" '
        f'data-placeholder="请在这里输入标题">{title}</div>'
    )
    body_node = ""
    if body_html is not None:
        body_node = f'<div contenteditable="true" class="ProseMirror editor_content">{body_html}</div>'
    # single-quoted url inside the double-quoted style attribute, as real pages do
    cover_style = "background-image: url('https://mmbiz.example/x.png')" if cover_bound else ""
    preview = f'<div class="js_cover_preview_new" style="{cover_style}"></div>' if cover_style else ""
    counter = f'<em class="frm_counter">{len(re.sub(r"<[^>]+>", "", body_html))}/10000</em>' if body_html else ""
    # the picker dialog stays mounted in the editor DOM; crop phase adds its own
    dialogs = '<div class="weui-desktop-dialog">选择图片</div>'
    if crop_step >= 1:
        dialogs += '<div class="weui-desktop-dialog">编辑封面</div>'
    return (
        f"<html><body>测试公众号 编辑器 {title_node}{body_node}"
        f'<textarea id="js_description"></textarea>{preview}{counter}{dialogs}'
        f"<button>保存为草稿</button><button>发表</button></body></html>"
    )


class FakeBsk(BskClient):
    """Stateful double emulating the pages the runner touches."""

    def __init__(self, **options):
        self.options = options
        self.session = ""
        self.user_tabs: list[dict] = [
            {"tab_id": "t1", "url": f"https://mp.weixin.qq.com/cgi-bin/home?token=SECRET1&lang=zh_CN"}
        ]
        self.agent_tabs: list[dict] = []
        self.pages: dict[str, str] = {"t1": list_html(options.get("baseline_cards", []))}
        self.tab_pages: dict[str, str] = {"t1": "list"}
        self.editor_title = ""
        self.editor_digest = ""
        self.editor_body: str | None = None
        self.cover_bound = False
        self.crop_step = 0
        self.picker_items = 0
        self.save_button_count = options.get("save_button_count", 1)
        self.save_clicked = 0
        self.saved_drafts: list[dict[str, str]] = []
        self.click_log: list[str] = []
        self.broken_after_save = options.get("broken_after_save", False)
        self.borrow_registered = options.get("borrow_registered", True)
        self.account_name = options.get("account_name", ALIAS)
        self.account_names = options.get("account_names", None)
        self.editor_ready = options.get("editor_ready", True)
        self._counter = 1

    # -- session/tab lifecycle ------------------------------------------------
    def status(self):
        return {"browsers": [{}]}

    def session_start(self):
        self.session = "sx01"
        return self.session

    def session_stop(self, session):
        return True

    def tab_list(self, session, scope):
        return list(self.user_tabs if scope == "user" else self.agent_tabs)

    def tab_borrow(self, session, tab_id):
        if not self.borrow_registered:
            raise NativeRunnerError("browser_command_failed", "borrow rpc timeout", True)
        self.agent_tabs = [t for t in self.agent_tabs if t["tab_id"] != tab_id] + [
            t for t in self.user_tabs if t["tab_id"] == tab_id
        ]
        self.user_tabs = [t for t in self.user_tabs if t["tab_id"] != tab_id]

    def tab_return(self, session, tab_id):
        self.user_tabs += [t for t in self.agent_tabs if t["tab_id"] == tab_id]
        self.agent_tabs = [t for t in self.agent_tabs if t["tab_id"] != tab_id]
        return True

    def tab_close(self, session, tab_id):
        self.agent_tabs = [t for t in self.agent_tabs if t["tab_id"] != tab_id]
        return True

    def wait_ms(self, ms):
        return None

    # -- page channel ---------------------------------------------------------
    def get_html(self, session, tab_id=None):
        tab_id = tab_id or "t1"
        kind = self.tab_pages.get(tab_id, "list")
        if kind == "list":
            cards = list(self.options.get("baseline_cards", [])) + self.saved_drafts
            return list_html(cards)
        return editor_html(self.editor_title, self.editor_body, self.cover_bound,
                           self.crop_step)

    def fill(self, session, selector, value, tab_id=None):
        if "请在这里输入标题" in selector:
            self.editor_title = value
        elif selector == "#js_description":
            self.editor_digest = value
        elif "ProseMirror" in selector and "data-placeholder" not in selector:
            self.editor_body = value  # only reachable if a test bypasses paste
        else:
            raise NativeRunnerError("browser_command_failed", "unknown selector", True)
        return len(value)

    def click_selector(self, session, selector, tab_id=None):
        if selector.startswith("[data-nr-tag="):
            wanted = getattr(self, "_last_tag_wanted", [])
            joined = " ".join(wanted)
            if "保存为草稿" in joined:
                self.save_clicked += 1
                if self.editor_ready and not self.options.get("save_no_card", False):
                    self.saved_drafts.append({"appid": f"NEW{self._counter}", "title": self.editor_title})
            elif "新的创作" in joined:
                pass  # menu opens; behaviour continues on the 文章 item click
            elif "文章" in joined:
                self._counter += 1
                tid = f"t{self._counter}"
                self.tab_pages[tid] = "editor"
                self.agent_tabs.append({"tab_id": tid, "url": "https://mp.weixin.qq.com/cgi-bin/appmsg?token=SECRET2&action=edit"})
        return None

    def press(self, session, key, modifiers="", tab_id=None):
        return None

    def upload(self, session, selector, file_path, tab_id=None):
        if self.options.get("upload_ok", True):
            self.picker_items = 1

    def observe(self, session, tab_id=None):
        return 'dialog "选择图片"\n  @e1 button "上传文件"\n  @e2 button "取消"'

    def evaluate(self, session, expression, tab_id=None, timeout=20):
        if self.broken_after_save and self.save_clicked:
            return {}  # transport silently dead after the submission
        if "names:" in expression:
            names = self.account_names or [self.account_name]
            match = names == [ALIAS]
            return json.dumps({"names": names, "match": match}, ensure_ascii=False)
        if "onList" in expression:
            return json.dumps({"onList": True})
        if "data-nr-tag" in expression and "const wanted" in expression:
            # tag phase: record the wanted control; behaviour fires on click
            wanted = json.loads(re.search(r"const wanted=(\[.*?\]);", expression).group(1))
            if any("保存为草稿" in w for w in wanted) and self.save_button_count != 1:
                return json.dumps({"ok": False, "count": self.save_button_count})
            if any("新的创作" in w for w in wanted) and not self.editor_ready:
                return json.dumps({"ok": False, "count": 0})
            self._last_tag_wanted = wanted
            self.click_log.extend(wanted)
            return json.dumps({"ok": True})
        if "ClipboardEvent" in expression:
            match = re.search(r"dt\.setData\('text/html',(\"(?:[^\"\\]|\\.)*\")\)", expression)
            html = json.loads(match.group(1)) if match else ""
            self.editor_body = html
            return json.dumps({"ok": True, "chars": len(re.sub(r"<[^>]+>", "", html))})
        if "digest:" in expression or "js_description" in expression and "digest" in expression:
            counters = []
            if self.editor_body:
                counters.append(f"{len(re.sub(r'<[^>]+>', '', self.editor_body))}/10000")
            return json.dumps({"digest": self.editor_digest, "counter": counters}, ensure_ascii=False)
        if "input[type=file]" in expression:
            return json.dumps({"n": 1 if self.picker_items >= 0 else 0})
        if "js_imagedialog" in expression and ".click()" in expression:
            return json.dumps({"ok": True})  # cover entry synthetic click
        if "img-picker__item" in expression and "found" in expression:
            # filename probe inside the picker dialog
            return json.dumps({"open": True, "found": self.picker_items >= 1,
                               "count": self.picker_items})
        if "img-picker__item" in expression and ".click()" in expression:
            # select_expr: click the uploaded picker item
            if self.picker_items >= 1:
                return json.dumps({"ok": True, "count": self.picker_items})
            return json.dumps({"ok": False})
        if "img-picker__item" in expression:
            return json.dumps({"n": self.picker_items})
        if (".click()" in expression and "const wanted=" in expression
                and "data-nr-tag" not in expression):
            # generic synthetic click on dialog controls (下一步 / 确认)
            wanted = json.loads(re.search(r"const wanted=(\[.*?\]);", expression).group(1))
            if any("下一步" in w for w in wanted):
                self.crop_step = 1
            elif any("确认" in w for w in wanted) and self.picker_items:
                self.cover_bound = self.options.get("cover_binds", True)
            self.click_log.extend(wanted)
            return json.dumps({"ok": True})
        if "data-appid" in expression and "'编辑'" in expression:
            # reopen the (single) new draft in the editor page on the same tab
            if len(self.saved_drafts) == 1:
                self.editor_title = self.saved_drafts[0]["title"]
                self.editor_body = BODY_HTML
                self.editor_digest = DIGEST
                self.cover_bound = self.options.get("cover_binds", True)
                self.tab_pages["t1"] = "editor"
                return json.dumps({"ok": True})
            return json.dumps({"ok": False, "reason": "no_edit"})
        if "已保存" in expression:
            return self.save_clicked > 0
        return json.dumps({})

    # the real BskClient methods the runner never calls on the fake
    def _cmd(self, *a, **k):  # pragma: no cover
        raise AssertionError("FakeBsk must not shell out")


class NativeDraftRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def runner_env(self, **options):
        fake = FakeBsk(**options)
        runner = NativeDraftRunner(bsk=fake, budget_s=30)
        return runner, fake, make_bundle(self.root), make_cover(self.root), self.root / "out"

    def run_with_cover(self, runner, bundle, cover, out):
        with patch.object(nr, "validate_local_image", lambda _path: cover):
            return runner.run(bundle, out)

    def test_publish_choice_refused_before_session(self):
        runner, fake, _bundle, cover, out = self.runner_env()
        result = self.run_with_cover(runner, make_bundle(self.root, delivery_choice="published"), cover, out)
        self.assertEqual(result["stage"], "blocked_publish_blocked")
        self.assertFalse(result["write_attempted"] or result["submission_clicked"])
        self.assertEqual(fake.session, "")

    def test_account_mismatch_blocks_before_write(self):
        runner, fake, bundle, cover, out = self.runner_env(account_name="别的账号")
        result = self.run_with_cover(runner, bundle, cover, out)
        self.assertEqual(result["stage"], "blocked_account_mismatch")
        self.assertFalse(result["write_attempted"])
        self.assertEqual(fake.save_clicked, 0)
        self.assertTrue(result["cleanup"]["tab_back_in_user_window"])
        self.assertTrue(result["cleanup"]["session_stopped"])

    def test_account_identity_ambiguous_blocks(self):
        runner, fake, bundle, cover, out = self.runner_env(account_names=["验收测试号", "另一个号"])
        result = self.run_with_cover(runner, bundle, cover, out)
        self.assertEqual(result["stage"], "blocked_account_identity_ambiguous")
        self.assertEqual(fake.save_clicked, 0)

    def test_ambiguous_save_button_refuses_click(self):
        runner, fake, bundle, cover, out = self.runner_env(save_button_count=2)
        result = self.run_with_cover(runner, bundle, cover, out)
        self.assertEqual(result["stage"], "blocked_element_ambiguous")
        self.assertFalse(result["write_attempted"])
        self.assertEqual(fake.save_clicked, 0)

    def test_lost_confirmation_after_save_click_is_uncertain_and_never_resent(self):
        runner, fake, bundle, cover, out = self.runner_env(broken_after_save=True)
        result = self.run_with_cover(runner, bundle, cover, out)
        self.assertEqual(result["stage"], "draft_uncertain")
        self.assertTrue(result["write_attempted"] and result["submission_clicked"])
        self.assertEqual(fake.save_clicked, 1)
        names = [path.name for path in out.glob("native-runner-*.json")]
        self.assertTrue(any("draft_submit_intent" in name for name in names))
        self.assertTrue(any("draft_save_clicked" in name for name in names))

    def test_same_name_old_draft_is_excluded_by_baseline(self):
        runner, _fake, bundle, cover, out = self.runner_env(baseline_cards=[{"appid": "OLD1", "title": TITLE}])
        result = self.run_with_cover(runner, bundle, cover, out)
        self.assertEqual(result["stage"], "drafted")
        self.assertEqual(result["draft_id"], "NEW2")
        self.assertEqual(result["fields_read_back"], {"title": True, "digest": True, "body": True, "cover": True})

    def test_only_old_same_name_draft_after_save_is_uncertain(self):
        runner, fake, bundle, cover, out = self.runner_env(
            baseline_cards=[{"appid": "OLD1", "title": TITLE}], save_no_card=True
        )
        result = self.run_with_cover(runner, bundle, cover, out)
        self.assertEqual(result["stage"], "draft_uncertain")
        self.assertTrue(result["submission_clicked"])
        self.assertEqual(fake.save_clicked, 1)

    def test_cover_not_bound_blocks_before_save(self):
        runner, fake, bundle, cover, out = self.runner_env(cover_binds=False)
        result = self.run_with_cover(runner, bundle, cover, out)
        self.assertEqual(result["stage"], "blocked_cover_not_bound")
        self.assertFalse(result["write_attempted"])
        self.assertEqual(fake.save_clicked, 0)

    def test_body_drift_blocks_before_save(self):
        runner, fake, bundle, cover, out = self.runner_env()
        original_paste = fake.evaluate

        def drifting_paste(session, expression, tab_id=None, timeout=20):
            value = original_paste(session, expression, tab_id=tab_id, timeout=timeout)
            if isinstance(value, str) and "ClipboardEvent" in expression:
                fake.editor_body = fake.editor_body.replace("项目一", "项目甲")
            return value

        fake.evaluate = drifting_paste
        result = self.run_with_cover(runner, bundle, cover, out)
        self.assertEqual(result["stage"], "blocked_field_readback_mismatch")
        self.assertFalse(result["write_attempted"])
        self.assertEqual(fake.save_clicked, 0)

    def test_group_send_control_never_clicked(self):
        runner, fake, bundle, cover, out = self.runner_env()
        result = self.run_with_cover(runner, bundle, cover, out)
        self.assertEqual(result["stage"], "drafted")
        for forbidden in ("群发", "发送给粉丝", "群发消息", "发送", "发表", "删除"):
            self.assertNotIn(forbidden, fake.click_log)

    def test_draft_success_verifies_fields_and_cleans_up(self):
        runner, fake, bundle, cover, out = self.runner_env()
        result = self.run_with_cover(runner, bundle, cover, out)
        self.assertEqual(result["stage"], "drafted")
        self.assertEqual(result["draft_id"], "NEW2")
        self.assertIsNotNone(result["timing_ms"]["auto_total_ms"])
        self.assertTrue(result["cleanup"]["tab_returned"])
        self.assertTrue(result["cleanup"]["tab_back_in_user_window"])
        self.assertTrue(result["cleanup"]["session_stopped"])
        self.assertTrue(fake.user_tabs)
        self.assertEqual(fake.agent_tabs, [])

    def test_failed_borrow_leaves_session_open_for_human(self):
        runner, fake, bundle, cover, out = self.runner_env(borrow_registered=False)
        runner.borrow_poll_s = 0.2
        result = self.run_with_cover(runner, bundle, cover, out)
        self.assertEqual(result["stage"], "blocked_tab_borrow_failed")
        self.assertTrue(any(tab["tab_id"] == "t1" for tab in fake.user_tabs))

    def test_result_never_leaks_secrets(self):
        runner, _fake, bundle, cover, out = self.runner_env()
        result = self.run_with_cover(runner, bundle, cover, out)
        dump = json.dumps(result, ensure_ascii=False)
        self.assertNotIn("SECRET1", dump)
        self.assertNotIn("SECRET2", dump)
        self.assertNotIn("token", dump.lower())
        self.assertNotIn(BODY_HTML, dump)
