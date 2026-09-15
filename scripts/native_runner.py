#!/usr/bin/env python3
"""Native-browser (bsk) draft runner for the WeChat Official Account editor.

Design goal: drive one real "save draft" round-trip using only WorkBuddy's
native browser commands (fill / click / upload / press / get-html) plus
strictly validated evaluate probes.  Raw evaluate results are treated as
UNRELIABLE by default: every consequential probe must return a well-formed
value or the step fails closed.  A toast, a disappeared button or a
navigation is never accepted as proof of success.

Safety contract (mirrors references/browser-backend.md):
- one bsk session, one borrowed backend tab, one editor tab at a time;
- draft baseline before any write; same-name old drafts are excluded;
- the submit intent is persisted BEFORE the save click; after the click any
  lost confirmation is `draft_uncertain` and is never re-sent;
- publish / group-send / delete controls are never clicked;
- no cookies, tokens, full body text or private paths in any output record;
- cleanup verifies the borrowed tab is back in the user window before the
  session is stopped; a failed return leaves the session open for a human.

CLI:  python3 native_runner.py --bundle <final.json> --out <result-dir>
                               [--bsk <path-to-bsk>] [--budget 90]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from publisher import (  # noqa: E402
    UNSAFE_BUTTONS,
    PublisherError,
    layout_profile,
    render_html,
    semantic_html_signature,
    validate_local_image,
)

MP_ORIGIN = "https://mp.weixin.qq.com"
LIST_PATH = "/cgi-bin/appmsg"
LIST_QUERY = "t=media/appmsg_list&type=77&action=list&begin=0&count=10&lang=zh_CN"
LOGIN_EXPIRED_RE = re.compile(r"登录超时|请重新登录|登录失效|扫码登录|重新扫码|扫码后登录")
# Controls this runner must never activate, wherever they appear.
NEVER_CLICK_TEXTS = set(UNSAFE_BUTTONS) | {"发表", "删除"}
TITLE_PM_SELECTOR = 'div.ProseMirror[contenteditable="true"][data-placeholder="请在这里输入标题"]'
BODY_PM_SELECTOR = 'div.ProseMirror[contenteditable="true"]:not([data-placeholder])'
DIGEST_SELECTOR = "#js_description"
SAFE_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
APPID_RE = re.compile(r'data-appid="([A-Za-z0-9_-]{1,64})"')
CARD_TITLE_RE = re.compile(r'weui-desktop-publish__cover__title[^>]*>\s*<span>([^<]{1,200})</span>')
PM_DIV_RE = re.compile(r'<div[^>]*class="ProseMirror[^"]*"[^>]*>')


def _safe_url(value: Any) -> str:
    """Origin+path only; query strings may carry platform tokens."""
    if not isinstance(value, str):
        return ""
    parsed = urllib.parse.urlsplit(value)
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}" if parsed.scheme and parsed.netloc else ""


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class NativeRunnerError(PublisherError):
    """Same code contract as PublisherError; distinct type for tracing."""


# --------------------------------------------------------------------------- #
# bsk transport (injectable fake in tests)
# --------------------------------------------------------------------------- #
class BskClient:
    def __init__(self, executable: str | None = None):
        self.executable = executable or os.environ.get("WECHAT_BSK_BIN") or "bsk"

    def _cmd(self, args: list[str], timeout: int = 30) -> dict[str, Any]:
        completed = subprocess.run(
            [self.executable, *args], text=True, capture_output=True, timeout=timeout, check=False
        )
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError:
            payload = {}
        if completed.returncode != 0:
            code = str(payload.get("code", "unknown"))[:32] if isinstance(payload, dict) else "unknown"
            raise NativeRunnerError(
                "browser_command_failed",
                f"bsk {args[0]} 未完成（{code}），尚未确认公众号写入。",
                True,
                diagnostics={"bsk_op": args[0], "bsk_error": code},
            )
        return payload if isinstance(payload, dict) else {}

    # -- thin, named wrappers ------------------------------------------------
    def status(self) -> dict[str, Any]:
        return self._cmd(["status", "--json"], timeout=10)

    def session_start(self) -> str:
        value = self._cmd(["session", "start", "--no-focus", "--json"], timeout=30)
        session = value.get("session_id") or (value.get("value") if isinstance(value.get("value"), str) else None)
        if not session or not re.fullmatch(r"[a-z0-9]{2,16}", str(session)):
            raise NativeRunnerError("browser_protocol_error", "bsk 未返回有效会话标识，未开始任何写入。", True)
        return str(session)

    def session_stop(self, session: str) -> bool:
        try:
            self._cmd(["session", "stop", session, "--json"], timeout=60)
            return True
        except NativeRunnerError:
            return False

    def tab_list(self, session: str, scope: str) -> list[dict[str, Any]]:
        value = self._cmd(["tab", "list", "--scope", scope, "--session", session, "--json"], timeout=15)
        tabs = value.get("tabs", [])
        return [t for t in tabs if isinstance(t, dict)] if isinstance(tabs, list) else []

    def tab_borrow(self, session: str, tab_id: str) -> None:
        self._cmd(["tab", "borrow", tab_id, "--session", session, "--json"], timeout=90)

    def tab_return(self, session: str, tab_id: str) -> bool:
        try:
            self._cmd(["tab", "return", tab_id, "--session", session, "--json"], timeout=60)
            return True
        except NativeRunnerError:
            return False

    def tab_close(self, session: str, tab_id: str) -> bool:
        try:
            self._cmd(["tab", "close", tab_id, "--session", session, "--json"], timeout=30)
            return True
        except NativeRunnerError:
            return False

    def get_html(self, session: str, tab_id: str | None = None) -> str:
        args = ["get-html", "--json", "--quiet", "--session", session]
        if tab_id:
            args += ["--tab-id", str(tab_id)]
        value = self._cmd(args, timeout=30)
        html = value.get("html")
        if not isinstance(html, str) or not html:
            raise NativeRunnerError("browser_page_unreadable", "无法读取页面 HTML，步骤失败关闭。", True)
        return html

    def evaluate(self, session: str, expression: str, tab_id: str | None = None, timeout: int = 20) -> Any:
        args = ["evaluate", "--json", "--session", session]
        if tab_id:
            args += ["--tab-id", str(tab_id)]
        args.append(expression)
        payload = self._cmd(args, timeout=timeout)
        # bsk exits 0 even when the page script threw: surface the JS error
        # explicitly instead of masking it as an empty result.
        if payload.get("ok") is False and isinstance(payload.get("error"), dict):
            detail = str(payload["error"].get("text", ""))[:200]
            raise NativeRunnerError(
                "evaluate_js_error",
                "页面脚本执行出错（表达式缺陷，非通道问题），步骤失败关闭。",
                True,
                diagnostics={"js_error_head": detail},
            )
        return payload.get("value")

    def observe(self, session: str, tab_id: str | None = None) -> str:
        """Semantic a11y snapshot as plain text (used for ref discovery)."""
        args = ["observe", "--session", session]
        if tab_id:
            args += ["--tab-id", str(tab_id)]
        completed = subprocess.run([self.executable, *args], text=True,
                                   capture_output=True, timeout=30, check=False)
        if completed.returncode != 0:
            raise NativeRunnerError("browser_command_failed",
                                    "bsk observe 未完成，步骤失败关闭。", True)
        return completed.stdout

    def fill(self, session: str, selector: str, value: str, tab_id: str | None = None) -> int:
        args = ["fill", "--json", "--selector", selector, "--value", value, "--session", session]
        if tab_id:
            args += ["--tab-id", str(tab_id)]
        payload = self._cmd(args, timeout=30)
        return int(payload.get("value_length", 0) or 0)

    def click_selector(self, session: str, selector: str, tab_id: str | None = None) -> None:
        args = ["click", "--json", "--selector", selector, "--session", session]
        if tab_id:
            args += ["--tab-id", str(tab_id)]
        self._cmd(args, timeout=30)

    def press(self, session: str, key: str, modifiers: str = "", tab_id: str | None = None) -> None:
        args = ["press", key, "--json", "--session", session]
        if modifiers:
            args += ["--modifiers", modifiers]
        if tab_id:
            args += ["--tab-id", str(tab_id)]
        self._cmd(args, timeout=15)

    def upload(self, session: str, selector: str, file_path: str, tab_id: str | None = None) -> None:
        args = ["upload", "--json", "--selector", selector, "--file", file_path, "--session", session]
        if tab_id:
            args += ["--tab-id", str(tab_id)]
        self._cmd(args, timeout=120)

    def wait_ms(self, ms: int) -> None:
        # daemon-side sleep; intentionally session-independent
        self._cmd(["wait-ms", f"{ms}ms", "--json"], timeout=max(5, ms // 1000 + 3))


# --------------------------------------------------------------------------- #
# Runner
# --------------------------------------------------------------------------- #
class NativeDraftRunner:
    def __init__(self, bsk: BskClient | None = None, budget_s: int = 90, borrow_poll_s: int = 20):
        self.bsk = bsk or BskClient()
        self.budget_s = budget_s
        self.borrow_poll_s = borrow_poll_s
        self.write_attempted = False
        self.submission_clicked = False
        self.phases: dict[str, int] = {}
        self._auto_start: float | None = None
        self._our_tabs: dict[str, str] = {}  # tab_id -> kind (editor|blank)

    # -- infrastructure -------------------------------------------------------
    def _phase(self, name: str) -> None:
        if self._auto_start is not None:
            self.phases[name] = int((time.monotonic() - self._auto_start) * 1000)

    def _check_budget(self) -> None:
        if self._auto_start is None:
            return
        if time.monotonic() - self._auto_start > self.budget_s:
            raise NativeRunnerError(
                "browser_workflow_timeout",
                "自动操作超过时间预算，已停止；不会自动重复保存。",
                not self.write_attempted,
                write_attempted=self.write_attempted,
                submission_clicked=self.submission_clicked,
            )

    def _eval_checked(self, session: str, expression: str, tab_id: str | None = None,
                      *, expect_json: bool = False, timeout: int = 20) -> Any:
        """evaluate with fail-closed validation: {} / None / error → unreliable."""
        value = self.bsk.evaluate(session, expression, tab_id=tab_id, timeout=timeout)
        if value is None or value == {} or value == "":
            raise NativeRunnerError(
                "evaluate_unreliable",
                "页面脚本通道返回空结果，无法安全继续；尚未确认任何写入。",
                True,
            )
        if expect_json and isinstance(value, str):
            try:
                return json.loads(value)
            except json.JSONDecodeError as error:
                raise NativeRunnerError(
                    "evaluate_unreliable", "页面脚本通道返回了无法解析的结果，步骤失败关闭。", True
                ) from error
        return value

    _TAG_COUNTER = 0

    def _tag_and_click(self, session: str, texts: tuple[str, ...], tab_id: str | None = None,
                       *, pool: str = "button,a,[role=button]", timeout_s: float = 0,
                       contains: bool = False) -> None:
        """Two-phase guarded click: a validated evaluate pass TAGS the unique
        visible element (matching one of `texts`, whitespace-stripped), then
        bsk performs a REAL CDP click on the tag selector.  JS dispatch is
        never used for behaviour: several page controls (the creation menu)
        ignore synthetic clicks.  Polls up to `timeout_s` for the control."""
        for text in texts:
            if text in NEVER_CLICK_TEXTS:
                raise NativeRunnerError(
                    "group_send_blocked",
                    f"目标控件“{text}”属于发布/群发/删除类禁点控件，已拒绝执行。",
                    False,
                )
        NativeDraftRunner._TAG_COUNTER += 1
        marker = f"nr{NativeDraftRunner._TAG_COUNTER}"
        if contains:
            predicate = "wanted.some(w=>norm(e.innerText).includes(w))"
        else:
            predicate = "wanted.includes(norm(e.innerText))"
        expr = (
            "(()=>{const wanted=%s;const norm=v=>String(v||'').replace(/\\s+/g,'');"
            "const matches=[...document.querySelectorAll('%s')].filter(e=>"
            "%s&&(()=>{const r=e.getBoundingClientRect();"
            "return r.width>10&&r.height>10})());"
            "if(matches.length!==1)return JSON.stringify({ok:false,count:matches.length});"
            "matches[0].setAttribute('data-nr-tag','%s');return JSON.stringify({ok:true});})()"
            % (json.dumps([re.sub(r"\s+", "", t) for t in texts]), pool, predicate, marker)
        )
        deadline = time.monotonic() + max(0.0, timeout_s)
        while True:
            self._check_budget()
            result = self._eval_checked(session, expr, tab_id, expect_json=True)
            if isinstance(result, dict) and result.get("ok"):
                break
            count = result.get("count", "?") if isinstance(result, dict) else "?"
            if time.monotonic() >= deadline:
                code = ("element_ambiguous"
                        if isinstance(count, int) and count > 1 else "element_missing")
                raise NativeRunnerError(
                    code,
                    f"控件{'/'.join(texts)}匹配数不唯一（{count}），步骤失败关闭。",
                    True,
                )
            time.sleep(0.3)
        self.bsk.click_selector(session, f'[data-nr-tag="{marker}"]', tab_id=tab_id)

    # -- page parsing (native get-html channel) -------------------------------
    @staticmethod
    def _parse_draft_cards(html: str) -> list[dict[str, str]]:
        """Extract {appid,title} per masonry card from the draft list HTML."""
        cards: list[dict[str, str]] = []
        positions = [(m.start(), m.group(1)) for m in APPID_RE.finditer(html)]
        for index, (start, appid) in enumerate(positions):
            end = positions[index + 1][0] if index + 1 < len(positions) else len(html)
            match = CARD_TITLE_RE.search(html[start:end])
            if match and SAFE_ID_RE.fullmatch(appid):
                cards.append({"appid": appid, "title": match.group(1).strip()})
        return cards

    @staticmethod
    def _extract_title_pm_text(html: str) -> str | None:
        match = re.search(r'data-placeholder="请在这里输入标题">([^<]*)</div>', html)
        return match.group(1) if match else None

    @staticmethod
    def _extract_body_pm_html(html: str) -> str | None:
        """Find the single ProseMirror body div (no data-placeholder) and
        return its inner HTML via balanced div matching."""
        bodies = []
        for match in PM_DIV_RE.finditer(html):
            tag = match.group(0)
            if "data-placeholder" not in tag and "contenteditable=\"true\"" in tag:
                bodies.append((match.end(), match.group(0)))
        if len(bodies) != 1:
            return None
        start, _tag = bodies[0]
        depth, i = 1, start
        while i < len(html) and depth > 0:
            nxt_open = html.find("<div", i)
            nxt_close = html.find("</div>", i)
            if nxt_close == -1:
                return None
            if nxt_open != -1 and nxt_open < nxt_close:
                depth += 1
                i = nxt_open + 4
            else:
                depth -= 1
                i = nxt_close + 6
        return html[start:i - 6]

    @staticmethod
    def _cover_bound(html: str) -> bool:
        """Bound = a visible preview whose background-image is a real URL."""
        previews = re.findall(r'js_cover_preview_new[^>]*style="([^"]*)"', html)
        return any("background-image" in style and re.search(r'url\(["\']?http', style)
                   and "display: none" not in style for style in previews)

    def _click_visible_synthetic(self, session: str, texts: tuple[str, ...], tab_id: str,
                                 *, pool: str = "button", contains: bool = False,
                                 timeout_s: float = 0.0) -> None:
        """Synthetic click on the single VISIBLE element matching `texts`
        (whitespace-stripped).  Verified working for weui dialog buttons.
        With `timeout_s` > 0 the click is retried while the control finishes
        rendering (dialog transitions expose the text before the button)."""
        for text in texts:
            if text in NEVER_CLICK_TEXTS:
                raise NativeRunnerError(
                    "group_send_blocked",
                    f"目标控件“{text}”属于发布/群发/删除类禁点控件，已拒绝执行。",
                    False,
                )
        predicate = ("wanted.some(w=>norm(e.innerText).includes(w))" if contains
                     else "wanted.includes(norm(e.innerText))")
        expr = (
            "(()=>{const wanted=%s;const norm=v=>String(v||'').replace(/\\s+/g,'');"
            "const matches=[...document.querySelectorAll('%s')].filter(e=>%s&&"
            "(()=>{const r=e.getBoundingClientRect();return r.width>10&&r.height>10})());"
            "if(matches.length!==1)return JSON.stringify({ok:false,count:matches.length});"
            "matches[0].click();return JSON.stringify({ok:true});})()"
            % (json.dumps([re.sub(r"\s+", "", t) for t in texts]), pool, predicate)
        )
        deadline = time.monotonic() + max(0.0, timeout_s)
        while True:
            self._check_budget()
            result = self._eval_checked(session, expr, tab_id, expect_json=True)
            if isinstance(result, dict) and result.get("ok"):
                return
            count = result.get("count", "?") if isinstance(result, dict) else "?"
            if time.monotonic() >= deadline:
                code = ("element_ambiguous"
                        if isinstance(count, int) and count > 1 else "element_missing")
                raise NativeRunnerError(
                    code,
                    f"控件{'/'.join(texts)}匹配数不唯一（{count}），步骤失败关闭。",
                    True,
                )
            time.sleep(0.4)

    # -- steps ----------------------------------------------------------------
    def _find_backend_tab(self, session: str) -> str:
        tabs = self.bsk.tab_list(session, "user")
        with_token = []
        for tab in tabs:
            url = str(tab.get("url", ""))
            parsed = urllib.parse.urlsplit(url)
            if parsed.netloc == "mp.weixin.qq.com" and parsed.path.startswith("/cgi-bin/"):
                if urllib.parse.parse_qs(parsed.query).get("token"):
                    with_token.append(str(tab["tab_id"]))
        if not with_token:
            raise NativeRunnerError(
                "logged_tab_not_found",
                "未找到已登录的公众号后台标签，未写入任何内容。",
                True,
                next_step="在 Chrome 中打开并登录公众号后台后重试。",
            )
        if len(with_token) != 1:
            raise NativeRunnerError(
                "logged_tab_ambiguous",
                f"检测到 {len(with_token)} 个公众号后台标签，无法唯一确定目标。",
                False,
                next_step="只保留一个公众号后台标签后重试。",
            )
        return with_token[0]

    def _borrow(self, session: str, tab_id: str) -> None:
        """Borrow the backend tab.

        The borrow RPC can outlive the CLI's fixed 30s internal timeout while
        still completing asynchronously (verified live).  After any failure,
        poll the agent window for a bounded window and retry at most once
        before declaring the step failed.
        """
        try:
            self.bsk.tab_borrow(session, tab_id)
            return
        except NativeRunnerError:
            pass  # fall through to the verification poll
        for attempt in (1, 2):
            deadline = time.monotonic() + self.borrow_poll_s
            while time.monotonic() < deadline:
                self._check_budget()
                agent_ids = {str(t.get("tab_id")) for t in self.bsk.tab_list(session, "agent")}
                if tab_id in agent_ids:
                    return  # the earlier dispatch did land
                time.sleep(0.7)
            if attempt == 1:
                try:
                    self.bsk.tab_borrow(session, tab_id)
                    return
                except NativeRunnerError:
                    continue
        user_ids = {str(t.get("tab_id")) for t in self.bsk.tab_list(session, "user")}
        if tab_id in user_ids:
            raise NativeRunnerError(
                "tab_borrow_failed",
                "接管已登录公众号标签未完成，未写入任何内容。",
                True,
                next_step="保持 Chrome 打开后重试；若反复出现请检查浏览器扩展状态。",
            )
        raise NativeRunnerError(
            "tab_borrow_failed",
            "接管公众号标签后标签去向不明，为保护你的标签页已停止。",
            False,
        )

    def _verify_account(self, session: str, tab_id: str, alias: str) -> None:
        expr = (
            "(()=>{const normalize=v=>String(v||'').normalize('NFKC').replace(/[\\s\\u200b]+/g,'');"
            "const wanted=normalize(%s);const selectors=['.weui-desktop-account__nickname',"
            "'.acount_box-nickname','.account_box-panel-head__nickname','#js_name',"
            "'.weui-desktop-account__name'];"
            "const names=[...new Set(selectors.flatMap(s=>[...document.querySelectorAll(s)])"
            ".map(e=>normalize(e.textContent)).filter(Boolean))];"
            "return JSON.stringify({names:names,match:names.length===1&&names[0]===wanted});})()"
            % json.dumps(alias)
        )
        probe = self._eval_checked(session, expr, tab_id, expect_json=True)
        names = [str(n) for n in probe.get("names", [])]
        if not names:
            # dormant login templates in page markup cause false positives;
            # only VISIBLE text may declare the session expired.
            visible = self._eval_checked(session, "document.body.innerText.slice(0, 8000)", tab_id)
            if LOGIN_EXPIRED_RE.search(str(visible)):
                raise NativeRunnerError(
                    "browser_login_required", "公众号后台登录已失效，未写入任何内容。", True,
                    next_step="在当前 Chrome 标签重新登录公众号后台后重试。",
                )
            raise NativeRunnerError(
                "account_identity_unreadable", "公众号后台未显示账号标识，未写入任何内容。", True,
                next_step="等待页面加载完成后重试。",
            )
        if len(names) != 1:
            raise NativeRunnerError(
                "account_identity_ambiguous",
                f"后台出现 {len(names)} 个不同账号标识，无法唯一确认账号。",
                False,
            )
        if not probe.get("match"):
            raise NativeRunnerError(
                "account_mismatch",
                "当前登录的公众号与内容包指定账号不一致，未写入任何内容。",
                False,
                next_step="切换到正确账号后重新执行。",
            )

    def _ensure_list_page(self, session: str, tab_id: str) -> None:
        expr = (
            "(()=>{const q=new URLSearchParams(location.search);"
            "const onList=location.pathname==='%s'&&q.get('action')==='list'&&q.get('type')==='77';"
            "if(onList)return JSON.stringify({onList:true});"
            "const token=q.get('token');if(!token)return JSON.stringify({onList:false,token:false});"
            "location.assign('/cgi-bin/appmsg?token='+encodeURIComponent(token)+'&lang=zh_CN&%s');"
            "return JSON.stringify({onList:false,navigated:true});})()"
            % (LIST_PATH, LIST_QUERY)
        )
        probe = self._eval_checked(session, expr, tab_id, expect_json=True)
        if probe.get("onList"):
            return
        if not probe.get("navigated"):
            raise NativeRunnerError(
                "wechat_login_expired", "公众号后台缺少有效会话参数，未写入任何内容。", True,
                next_step="重新登录公众号后台后重试。",
            )
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            self._check_budget()
            html = self.bsk.get_html(session, tab_id)
            if "weui-desktop-card" in html and "data-appid" in html:
                return
            self.bsk.wait_ms(400)
        raise NativeRunnerError("draft_baseline_unavailable", "草稿列表未能加载，未写入任何内容。", True)

    def _draft_baseline(self, session: str, tab_id: str, title: str) -> set[str]:
        html = self.bsk.get_html(session, tab_id)
        cards = self._parse_draft_cards(html)
        return {c["appid"] for c in cards if c["title"] == title and SAFE_ID_RE.fullmatch(c["appid"])}

    def _open_editor(self, session: str, borrowed_tab: str) -> str:
        """Open the article editor; return the editor tab id."""
        self._tag_and_click(session, ("新的创作",), borrowed_tab, timeout_s=6)
        # a transient window.open blank tab may appear here; tracked for cleanup
        before = {str(t.get("tab_id")) for t in self.bsk.tab_list(session, "agent")}
        # the creation menu is a weui dropdown whose items are <li>, not
        # buttons; real CDP click required (synthetic clicks are ignored).
        self._tag_and_click(session, ("文章",), borrowed_tab,
                            pool="li.weui-desktop-dropdown__list-ele",
                            contains=True, timeout_s=8)
        deadline = time.monotonic() + 25
        editor_id = ""
        while time.monotonic() < deadline and not editor_id:
            self._check_budget()
            for tab in self.bsk.tab_list(session, "agent"):
                tid = str(tab.get("tab_id"))
                url = _safe_url(tab.get("url", ""))
                if tid in before:
                    continue
                if url in ("about:blank", "about://blank"):
                    self._our_tabs.setdefault(tid, "blank")
                elif url.startswith(f"{MP_ORIGIN}{LIST_PATH}") and tid != borrowed_tab:
                    editor_id = tid
            if not editor_id:
                self.bsk.wait_ms(500)
        if not editor_id:
            raise NativeRunnerError(
                "editor_open_failed", "文章编辑器未能打开，未写入任何内容。", True,
                next_step="检查公众号后台是否正常加载后重试。",
            )
        self._our_tabs[editor_id] = "editor"
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            self._check_budget()
            html = self.bsk.get_html(session, editor_id)
            if 'data-placeholder="请在这里输入标题"' in html:
                return editor_id
            self.bsk.wait_ms(500)
        raise NativeRunnerError("editor_open_failed", "文章编辑器未加载完成，未写入任何内容。", True)

    def _fill_fields(self, session: str, editor_tab: str, title: str, digest: str,
                     body_html: str) -> None:
        written = self.bsk.fill(session, TITLE_PM_SELECTOR, title, tab_id=editor_tab)
        if written <= 0:
            raise NativeRunnerError("field_write_failed", "标题写入失败，尚未保存草稿。", True)
        scroll = (
            "(()=>{const e=document.querySelector('%s');if(!e)return JSON.stringify({ok:false});"
            "e.scrollIntoView({block:'center'});e.focus();return JSON.stringify({ok:true});})()"
            % DIGEST_SELECTOR
        )
        self._eval_checked(session, scroll, editor_tab, expect_json=True)
        self.bsk.click_selector(session, DIGEST_SELECTOR, tab_id=editor_tab)
        written = self.bsk.fill(session, DIGEST_SELECTOR, digest, tab_id=editor_tab)
        if written <= 0:
            raise NativeRunnerError("field_write_failed", "摘要写入失败，尚未保存草稿。", True)
        self.bsk.press(session, "End", tab_id=editor_tab)
        paste = (
            "(async()=>{const pm=[...document.querySelectorAll(\"div.ProseMirror[contenteditable=true]"
            ":not([data-placeholder])\")][0];if(!pm)return JSON.stringify({ok:false,reason:'no_pm'});"
            "pm.focus();const dt=new DataTransfer();dt.setData('text/html',%s);"
            "dt.setData('text/plain',%s);"
            "pm.dispatchEvent(new ClipboardEvent('paste',{clipboardData:dt,bubbles:true,cancelable:true}));"
            "await new Promise(r=>setTimeout(r,400));"
            "return JSON.stringify({ok:true,chars:pm.textContent.length});})()"
            % (json.dumps(body_html), json.dumps(re.sub(r"<[^>]+>", " ", body_html)))
        )
        result = self._eval_checked(session, paste, editor_tab, expect_json=True, timeout=30)
        if not result.get("ok") or not int(result.get("chars", 0)):
            raise NativeRunnerError("field_write_failed", "正文写入未确认，尚未保存草稿。", True)

    def _readback_fields(self, session: str, editor_tab: str, title: str, digest: str,
                         body_html: str) -> dict[str, bool]:
        html = self.bsk.get_html(session, editor_tab)
        title_ok = self._extract_title_pm_text(html) == title
        body_pm = self._extract_body_pm_html(html)
        body_ok = bool(body_pm) and semantic_html_signature(body_pm) == semantic_html_signature(body_html)
        expr = (
            "(()=>{const t=document.querySelector('%s');"
            "return JSON.stringify({digest:t?t.value:'',counter:[...document.querySelectorAll"
            "('em.frm_counter')].map(e=>e.textContent)});})()" % DIGEST_SELECTOR
        )
        probe = self._eval_checked(session, expr, editor_tab, expect_json=True)
        digest_ok = str(probe.get("digest", "")) == digest
        counter_ok = any(re.fullmatch(r"[1-9]\d*/\d+", c or "") for c in probe.get("counter", []))
        readback = {"title": bool(title_ok), "digest": bool(digest_ok),
                    "body": bool(body_ok), "body_counter": bool(counter_ok)}
        if not all(readback.values()):
            failed = [k for k, v in readback.items() if not v]
            raise NativeRunnerError(
                "field_readback_mismatch",
                f"编辑器字段回读不一致（{'、'.join(failed)}），尚未保存草稿。",
                True,
                diagnostics={"failed_fields": failed},
            )
        return readback

    def _upload_cover(self, session: str, editor_tab: str, cover: Path) -> None:
        """Cover recipe verified live on the current editor:

        synthetic click on the visible `.js_imagedialog` → 选择图片 dialog →
        upload via the observe ref of 上传文件 (input-mode interception) →
        confirm by filename in the dialog → select item → 下一步 (crop) →
        确认 (per-ratio crop, e.g. 2.35:1 + 1:1) → preview bound check.
        """
        stem = cover.stem
        open_expr = (
            "(()=>{const els=[...document.querySelectorAll('#js_cover_area .js_imagedialog')]"
            ".filter(e=>{const r=e.getBoundingClientRect();return r.width>10&&r.height>10});"
            "if(els.length!==1)return JSON.stringify({ok:false,count:els.length});"
            "els[0].click();return JSON.stringify({ok:true});})()"
        )
        opened = self._eval_checked(session, open_expr, editor_tab, expect_json=True)
        if not opened.get("ok"):
            raise NativeRunnerError(
                "cover_dialog_unavailable",
                f"封面入口匹配数不唯一（{opened.get('count')}），尚未保存草稿。", True,
            )
        # wait for the 选择图片 dialog
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            self._check_budget()
            html = self.bsk.get_html(session, editor_tab)
            if "选择图片" in html:
                break
            self.bsk.wait_ms(400)
        else:
            raise NativeRunnerError("cover_dialog_unavailable", "选择图片对话框未打开，尚未保存草稿。", True)
        # upload via observe ref (class names re-render unpredictably)
        deadline = time.monotonic() + 10
        upload_ref = ""
        while time.monotonic() < deadline and not upload_ref:
            self._check_budget()
            tree = self.bsk.observe(session, tab_id=editor_tab)
            refs = re.findall(r'@(e\d+)\s+(?:link|button)\s+"上传文件"', tree)
            if len(refs) == 1:
                upload_ref = refs[0]
            elif len(refs) > 1:
                raise NativeRunnerError("element_ambiguous",
                                        "上传文件控件匹配数不唯一，尚未保存草稿。", True)
            else:
                self.bsk.wait_ms(500)
        if not upload_ref:
            raise NativeRunnerError("cover_dialog_unavailable", "上传文件控件未找到，尚未保存草稿。", True)
        try:
            self.bsk.upload(session, f"@{upload_ref}", str(cover), tab_id=editor_tab)
        except NativeRunnerError:
            # the CLI may report a transient permission denial while the file
            # still lands; the filename check below is the authoritative
            # evidence, so never retry the upload here.
            pass
        # the CLI may report a transient permission error while the upload
        # still lands; the authoritative evidence is the filename in the list
        deadline = time.monotonic() + 30
        item_found = False
        while time.monotonic() < deadline and not item_found:
            self._check_budget()
            probe = self._eval_checked(
                session,
                "(()=>{const d=[...document.querySelectorAll('.weui-desktop-dialog')]"
                ".filter(x=>{const r=x.getBoundingClientRect();return r.width>100&&r.height>100})[0];"
                "if(!d)return JSON.stringify({open:false});"
                "const items=[...d.querySelectorAll('.weui-desktop-img-picker__item')];"
                "const target=items.find(i=>i.innerText.includes(%s));"
                "return JSON.stringify({open:true,found:!!target,count:items.length});})()"
                % json.dumps(stem),
                editor_tab, expect_json=True,
            )
            if probe.get("found"):
                item_found = True
                break
            self.bsk.wait_ms(800)
        if not item_found:
            raise NativeRunnerError(
                "cover_upload_failed", "封面未出现在素材库列表，尚未保存草稿。", True,
            )
        # select the uploaded item, then walk the crop steps
        self._click_visible_synthetic(
            session, (stem,), editor_tab, timeout_s=6,
            pool=".weui-desktop-dialog .weui-desktop-img-picker__item", contains=True,
        )
        self._click_visible_synthetic(session, ("下一步",), editor_tab, timeout_s=6)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            self._check_budget()
            html = self.bsk.get_html(session, editor_tab)
            if "编辑封面" in html:
                break
            self.bsk.wait_ms(400)
        else:
            raise NativeRunnerError("cover_dialog_unavailable", "封面裁切步骤未进入，尚未保存草稿。", True)
        # the crop UI exposes the 编辑封面 text before its 确认 button becomes
        # visible/interactable (verified live); poll instead of single-shot
        self._click_visible_synthetic(session, ("确认",), editor_tab, timeout_s=8)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            self._check_budget()
            html = self.bsk.get_html(session, editor_tab)
            if self._cover_bound(html):
                return
            self.bsk.wait_ms(500)
        raise NativeRunnerError("cover_not_bound", "封面裁切确认后未绑定到编辑器，尚未保存草稿。", True)

    def _persist_intent(self, out_dir: Path, marker: str) -> None:
        out_dir.mkdir(parents=True, exist_ok=True)
        record = {
            "marker": marker,
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "write_attempted": self.write_attempted,
            "submission_clicked": self.submission_clicked,
        }
        (out_dir / f"native-runner-{marker}.json").write_text(
            json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def _wait_editor_ready(self, session: str, _borrowed_tab: str, seconds: int) -> bool:
        """Wait until some agent tab hosts a loaded article editor.

        The edit click may navigate the list tab in place or open a new tab;
        both are handled by scanning every agent tab for the editor route.
        """
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self._check_budget()
            for tab in self.bsk.tab_list(session, "agent"):
                tid = str(tab.get("tab_id"))
                raw_url = str(tab.get("url", ""))
                url = _safe_url(raw_url)
                if "action=edit" not in raw_url:
                    continue
                if url.startswith(f"{MP_ORIGIN}{LIST_PATH}"):
                    try:
                        html = self.bsk.get_html(session, tid)
                    except NativeRunnerError:
                        continue
                    if 'data-placeholder="请在这里输入标题"' in html:
                        return True
            self.bsk.wait_ms(600)
        return False

    def _verify_saved_draft(self, session: str, borrowed_tab: str, title: str, digest: str,
                            body_html: str, baseline_ids: set[str]) -> str:
        """Locate the saved draft by title (excluding baseline) and re-verify."""
        self._ensure_list_page(session, borrowed_tab)
        html = self.bsk.get_html(session, borrowed_tab)
        cards = self._parse_draft_cards(html)
        new_cards = [c for c in cards if c["title"] == title and c["appid"] not in baseline_ids]
        if len(new_cards) != 1:
            raise NativeRunnerError(
                "draft_uncertain",
                f"草稿已提交，但草稿箱命中 {len(new_cards)} 条新同名草稿，需人工核对；不会自动重试。",
                False, write_attempted=True, submission_clicked=self.submission_clicked,
                diagnostics={"title_matches": len([c for c in cards if c["title"] == title])},
            )
        draft_id = new_cards[0]["appid"]
        open_expr = (
            "(()=>{const id=%s;"
            "const cards=[...document.querySelectorAll('.weui-desktop-card[data-appid]')]"
            ".filter(c=>c.getAttribute('data-appid')===id);if(cards.length!==1)"
            "return JSON.stringify({ok:false});"
            "const tip=[...cards[0].querySelectorAll('.weui-desktop-tooltip')]"
            ".find(t=>(t.textContent||'').trim()==='编辑');if(!tip)"
            "return JSON.stringify({ok:false,reason:'no_edit'});"
            "const btn=tip.parentElement&&tip.parentElement.querySelector('a');"
            "if(!btn)return JSON.stringify({ok:false,reason:'no_btn'});btn.click();"
            "return JSON.stringify({ok:true});})()" % json.dumps(draft_id)
        )
        opened = self._eval_checked(session, open_expr, borrowed_tab, expect_json=True)
        if not opened.get("ok"):
            raise NativeRunnerError(
                "draft_uncertain",
                "草稿已提交，但编辑入口无法唯一定位，需人工核对；不会自动重试。",
                False, write_attempted=True, submission_clicked=self.submission_clicked,
            )
        if not self._wait_editor_ready(session, borrowed_tab, 20):
            raise NativeRunnerError(
                "draft_uncertain",
                "草稿已提交，但重新打开编辑页失败，需人工核对；不会自动重试。",
                False, write_attempted=True, submission_clicked=self.submission_clicked,
            )
        # whichever tab now hosts the editor: verify fields + cover there
        editor_tab = ""
        for tab in self.bsk.tab_list(session, "agent"):
            tid = str(tab.get("tab_id"))
            raw_url = str(tab.get("url", ""))
            if "action=edit" in raw_url:
                try:
                    if 'data-placeholder="请在这里输入标题"' in self.bsk.get_html(session, tid):
                        editor_tab = tid
                        break
                except NativeRunnerError:
                    continue
        if not editor_tab:
            raise NativeRunnerError(
                "draft_uncertain",
                "草稿已提交，但编辑页标签无法定位，需人工核对；不会自动重试。",
                False, write_attempted=True, submission_clicked=self.submission_clicked,
            )
        if editor_tab != borrowed_tab and editor_tab not in self._our_tabs:
            self._our_tabs[editor_tab] = "editor"
        self._readback_fields(session, editor_tab, title, digest, body_html)
        if not self._cover_bound(self.bsk.get_html(session, editor_tab)):
            raise NativeRunnerError(
                "draft_uncertain",
                "草稿已提交，但重新打开后封面未绑定，需人工核对；不会自动重试。",
                False, write_attempted=True, submission_clicked=self.submission_clicked,
            )
        return draft_id

    def _cleanup(self, session: str, borrowed_tab: str) -> dict[str, Any]:
        cleanup: dict[str, Any] = {"editor_tabs_closed": [], "blank_tabs_closed": [],
                                   "tab_returned": False, "tab_back_in_user_window": False,
                                   "session_stopped": False}
        for tid, kind in self._our_tabs.items():
            if tid == borrowed_tab:
                continue
            if self.bsk.tab_close(session, tid):
                key = "blank_tabs_closed" if kind == "blank" else "editor_tabs_closed"
                cleanup[key].append(tid)
        if borrowed_tab:
            cleanup["tab_returned"] = self.bsk.tab_return(session, borrowed_tab)
            user_ids = {str(t.get("tab_id")) for t in self.bsk.tab_list(session, "user")}
            cleanup["tab_back_in_user_window"] = borrowed_tab in user_ids
            if not cleanup["tab_back_in_user_window"]:
                # leave the session open so a human can recover the user tab;
                # never destroy a user-owned tab with the session window.
                cleanup["session_stopped"] = False
                cleanup["warning"] = "借用标签未确认归还用户窗口，会话保持打开，请人工检查。"
                return cleanup
        cleanup["session_stopped"] = self.bsk.session_stop(session)
        return cleanup

    # -- entry point ----------------------------------------------------------
    def run(self, bundle_path: Path, out_dir: Path) -> dict[str, Any]:
        result: dict[str, Any] = {
            "runner": "native_browser_v1",
            "stage": "internal_error",
            "draft_id": "",
            "fields_read_back": {},
            "timing_ms": {"phases": {}, "auto_total_ms": None, "human_wait_ms": None},
            "write_attempted": False,
            "submission_clicked": False,
            "cleanup": {},
            "content_hashes": {},
        }
        session = ""
        borrowed_tab = ""
        started = time.monotonic()
        try:
            bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
            if bundle.get("delivery", {}).get("delivery_choice") != "draft":
                raise NativeRunnerError(
                    "publish_blocked",
                    "浏览器路线仅支持存草稿；公开发布未通过验收，已拒绝执行。",
                    False,
                )
            article = bundle["article"]
            alias = str(bundle["delivery"].get("account_alias", "")).strip()
            if not alias:
                raise NativeRunnerError("account_required", "内容包缺少账号别名，未执行。", False)
            unsupported = []
            if str(article.get("author", "")).strip():
                unsupported.append("作者")
            if str(bundle["delivery"].get("source_url", "")).strip():
                unsupported.append("原文链接")
            if bundle["delivery"].get("comments", "open") != "open":
                unsupported.append("留言设置")
            if bool(bundle["delivery"].get("fans_only")):
                unsupported.append("粉丝留言限制")
            if unsupported:
                raise NativeRunnerError(
                    "browser_field_unsupported",
                    "浏览器路线尚不能写入：" + "、".join(unsupported) + "；未执行。",
                    False,
                )
            title = str(article["selected_title"])
            digest = str(article.get("summary", ""))
            body_html = render_html(article["body_blocks"],
                                    layout_profile(bundle["brief"]["content_type"]))
            cover = validate_local_image(bundle["visual"]["cover_path"])
            result["content_hashes"] = {
                "title_sha256": _sha(title), "digest_sha256": _sha(digest),
                "body_signature": semantic_html_signature(body_html),
                "cover_sha256": _sha(str(cover)),
            }
            status = self.bsk.status()
            if len(status.get("browsers", [])) != 1:
                raise NativeRunnerError(
                    "profile_required", "检测到多个浏览器连接，无法唯一确认公众号账号。", False
                )
            session = self.bsk.session_start()
            human_started = time.monotonic()
            borrowed_tab = self._find_backend_tab(session)
            self._borrow(session, borrowed_tab)
            result["timing_ms"]["human_wait_ms"] = int((time.monotonic() - human_started) * 1000)
            self._auto_start = time.monotonic()

            self._verify_account(session, borrowed_tab, alias)
            self._phase("account_check")
            self._ensure_list_page(session, borrowed_tab)
            baseline_ids = self._draft_baseline(session, borrowed_tab, title)
            self._phase("baseline")
            self._open_editor(session, borrowed_tab)
            self._phase("editor_open")
            self._verify_account(session, borrowed_tab, alias)
            editor_tab = next((t for t, k in self._our_tabs.items() if k == "editor"), "")
            self._fill_fields(session, editor_tab, title, digest, body_html)
            self._phase("fields")
            self._upload_cover(session, editor_tab, cover)
            self._phase("cover")
            self._readback_fields(session, editor_tab, title, digest, body_html)
            self._phase("readback")
            # --- durable intent BEFORE the irreversible click ----------------
            self._persist_intent(out_dir, "draft_submit_intent")
            try:
                self._tag_and_click(session, ("保存为草稿",), editor_tab, timeout_s=6)
            except PublisherError as click_error:
                if click_error.code in ("browser_command_failed", "evaluate_unreliable",
                                        "browser_timeout", "browser_page_unreadable"):
                    # the dispatch may have reached the page even though the
                    # response was lost: outcome unknown → never re-send.
                    self.write_attempted = True
                    self.submission_clicked = True
                    raise NativeRunnerError(
                        "draft_uncertain",
                        "保存点击的派发结果未知（响应丢失），需人工核对草稿箱；不会自动重试。",
                        False, write_attempted=True, submission_clicked=True,
                    ) from click_error
                raise  # pre-dispatch refusal: nothing was clicked, retryable
            self.write_attempted = True
            self.submission_clicked = True
            self._persist_intent(out_dir, "draft_save_clicked")
            self._phase("save_click")
            try:  # toast is supplemental only; it never decides success
                deadline = time.monotonic() + 4
                while time.monotonic() < deadline:
                    if self.bsk.evaluate(session, "(()=>document.body.innerText.includes('已保存'))()",
                                         tab_id=editor_tab):
                        break
                    self.bsk.wait_ms(300)
            except NativeRunnerError:
                pass
            draft_id = self._verify_saved_draft(
                session, borrowed_tab, title, digest, body_html, baseline_ids
            )
            self._phase("verify")
            result.update({
                "stage": "drafted",
                "draft_id": draft_id,
                "fields_read_back": {"title": True, "digest": True, "body": True, "cover": True},
            })
            result["timing_ms"]["phases"] = dict(self.phases)
            result["timing_ms"]["auto_total_ms"] = int((time.monotonic() - self._auto_start) * 1000)
        except PublisherError as error:
            # After the save click every failure is uncertain-by-definition:
            # the runner must never classify a post-submission fault as a
            # clean, retryable block.
            result["stage"] = ("draft_uncertain"
                               if (error.write_attempted or self.submission_clicked)
                               else f"blocked_{error.code}")
            result["error"] = {
                "code": error.code, "message": str(error),
                "retry_allowed": error.retry_allowed, "next_step": error.next_step,
            }
            if self._auto_start is not None:
                result["timing_ms"]["phases"] = dict(self.phases)
                result["timing_ms"]["auto_total_ms"] = int((time.monotonic() - self._auto_start) * 1000)
        finally:
            result["write_attempted"] = self.write_attempted
            result["submission_clicked"] = self.submission_clicked
            if session:
                try:
                    result["cleanup"] = self._cleanup(session, borrowed_tab)
                except Exception as cleanup_error:  # noqa: BLE001 - cleanup must surface
                    result["cleanup"] = {"error": f"cleanup_failed:{type(cleanup_error).__name__}"}
            result["total_elapsed_ms"] = int((time.monotonic() - started) * 1000)
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / "native-runner-result.json").write_text(
                json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Native bsk draft runner (draft-only).")
    parser.add_argument("--bundle", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path, help="directory for result + intent markers")
    parser.add_argument("--bsk", default=None, help="path to the bsk executable")
    parser.add_argument("--budget", type=int, default=90, help="automatic-operation budget in seconds")
    args = parser.parse_args()
    runner = NativeDraftRunner(bsk=BskClient(args.bsk) if args.bsk else None, budget_s=args.budget)
    result = runner.run(args.bundle, args.out)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("stage") == "drafted" else 1


if __name__ == "__main__":
    main()
