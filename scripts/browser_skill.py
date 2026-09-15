#!/usr/bin/env python3
"""BrowserSkill adapter for the WeChat Official Account editor.

The editor has no stable BrowserSkill file-upload subcommand.  This adapter
uses its native image-library uploader through a short-lived loopback bridge:
image bytes never travel through a shell argument, BrowserSkill WebSocket,
task ledger, or log.  Every page action is fixed code, never bundle-supplied
JavaScript or configuration.
"""
from __future__ import annotations

import http.server
import hashlib
import html
import json
import os
import re
import secrets
import shutil
import subprocess
import threading
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from publisher import UNSAFE_BUTTONS, PublisherError, render_html, layout_profile, semantic_html_signature, validate_local_image

REQUIRED_BSK_VERSION = "0.2.0"

@dataclass
class BskResult:
    value: Any
    stderr: str = ""


class BskClient:
    def __init__(self, executable: str | None = None):
        self.executable = executable or os.environ.get("WECHAT_BSK_BIN") or shutil.which("bsk") or ""
        self._deadline: float | None = None

    def set_deadline(self, deadline: float | None) -> None:
        """Apply one automatic-operation budget to every bridge command."""
        self._deadline = deadline

    def runtime_version(self) -> str:
        """Read the local CLI identity without starting a browser session."""
        if not self.executable:
            raise PublisherError("browser_unavailable", "未找到 BrowserSkill 运行时。", True,
                                 next_step="完成 BrowserSkill 安装和扩展连接后重试。")
        try:
            completed = subprocess.run([self.executable, "--version"], text=True, capture_output=True,
                                       timeout=5, check=False)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise PublisherError("browser_runtime_unverified", "无法读取 BrowserSkill 运行时版本。", True,
                                 next_step="修复 BrowserSkill 安装后重试。") from error
        matched = re.search(r"\b(\d+\.\d+\.\d+)\b", completed.stdout)
        if completed.returncode or not matched:
            raise PublisherError("browser_runtime_unverified", "BrowserSkill 未返回可验证的运行时版本。", True)
        return matched.group(1)

    def _run(self, *args: str, timeout: int = 20) -> BskResult:
        if not self.executable:
            raise PublisherError("browser_unavailable", "未找到 BrowserSkill 运行时。", True,
                                 next_step="完成 BrowserSkill 安装和扩展连接后重试。")
        if self._deadline is not None:
            remaining = self._deadline - time.monotonic()
            if remaining <= 0:
                # The client does not know whether the adapter has clicked a
                # save control.  Durable intent in the task ledger—not a
                # generic transport timeout—decides whether a retry is safe.
                raise PublisherError("browser_workflow_timeout", "公众号自动操作超过时间预算，已停止继续操作。", True,
                                     next_step="检查当前编辑页；如未触发保存可修复后重试。")
            timeout = max(1, min(timeout, int(remaining) + 1))
        try:
            completed = subprocess.run([self.executable, "--json", *args], text=True, capture_output=True,
                                       timeout=timeout, check=False)
        except subprocess.TimeoutExpired as error:
            operation_parts = [args[0]]
            if len(args) > 1 and not args[1].startswith("-"):
                operation_parts.append(args[1])
            operation = ".".join(part for part in operation_parts if re.fullmatch(r"[a-z-]{1,32}", part)) or "unknown"
            raise PublisherError("browser_timeout", "BrowserSkill 操作超时，尚未确认公众号写入。", True,
                                 next_step="检查 BrowserSkill 连接后重试；如已点击保存，请先核对草稿箱。",
                                 diagnostics={"browser_operation": operation}) from error
        if completed.returncode:
            try:
                failure = json.loads(completed.stdout)
            except json.JSONDecodeError:
                failure = {}
            if args[:2] == ("tab", "borrow") and failure.get("code") in {"cancelled", "timeout"}:
                raise PublisherError("tab_borrow_confirmation_required", "需要你确认 BrowserSkill 接管当前已登录的公众号标签；尚未写入草稿。", True,
                                     next_step="在浏览器出现的“允许接管标签”提示中确认后，重新执行本次草稿投递。")
            if args[0] == "evaluate" and failure.get("code") == "cdp_failed":
                raise PublisherError("browser_page_unreadable", "BrowserSkill 已连接，但无法读取当前公众号后台页面，尚未写入草稿。", True,
                                     next_step="关闭自动化打开的公众号页，在普通 Chrome 标签中手动打开已登录的公众号后台后重试。")
            operation_parts = [args[0]]
            if len(args) > 1 and not args[1].startswith("-"):
                operation_parts.append(args[1])
            operation = ".".join(part for part in operation_parts if re.fullmatch(r"[a-z-]{1,32}", part)) or "unknown"
            signature = str(failure.get("code", "unknown"))
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", signature):
                signature = "unknown"
            raise PublisherError("browser_command_failed", "BrowserSkill 操作未完成，尚未确认公众号写入。", True,
                                 next_step="检查 BrowserSkill 连接和公众号页面后重试。",
                                 diagnostics={"browser_operation": operation, "browser_error_signature": signature})
        try:
            return BskResult(json.loads(completed.stdout), completed.stderr)
        except json.JSONDecodeError as error:
            raise PublisherError("browser_protocol_error", "BrowserSkill 返回了无效结果，未开始公众号写入。", True) from error

    def ready(self) -> dict[str, Any]:
        """Require three consecutive extension probes before creating a session.

        Chrome extensions can reconnect briefly after the local daemon restarts.
        Treating a single empty status as a hard failure causes needless user
        retries; this remains read-only and never opens a browser window.
        """
        # Test doubles intentionally omit an executable.  A real process must
        # match the version recorded in the distribution lock before it drives
        # an authenticated publisher page.
        if getattr(self, "executable", "") and self.runtime_version() != REQUIRED_BSK_VERSION:
            raise PublisherError("browser_runtime_mismatch", "BrowserSkill 运行时版本与本 Skill 锁定版本不一致。", True,
                                 next_step=f"安装 BrowserSkill {REQUIRED_BSK_VERSION} 后重试。")
        stable, latest = 0, None
        deadline = time.monotonic() + 35
        while time.monotonic() < deadline:
            # The daemon's JSON status can take slightly over five seconds
            # immediately after restart.  Eight seconds stays bounded while
            # avoiding a false transport failure before extension diagnosis.
            status = self._run("status", timeout=8).value
            browsers = status.get("browsers", []) if isinstance(status, dict) else []
            if len(browsers) > 1:
                raise PublisherError("profile_required", "检测到多个 BrowserSkill 浏览器连接，无法确认公众号账号。", False,
                                     next_step="保留一个已登录目标公众号的浏览器连接后重试。")
            if len(browsers) == 1:
                stable, latest = stable + 1, status
                if stable == 3:
                    return latest
            else:
                stable = 0
            time.sleep(1)
        raise PublisherError("extension_disconnected", "BrowserSkill 扩展未在 35 秒内稳定连接浏览器。", True,
                             next_step="确认 Chrome 保持打开且扩展显示已连接后重试。")

    def start(self) -> str:
        value = self._run("session", "start", "--no-focus", timeout=20).value
        session = value.get("session_id") if isinstance(value, dict) else None
        if not session:
            raise PublisherError("browser_protocol_error", "BrowserSkill 未返回会话标识，未继续写入。", True)
        return str(session)

    def stop(self, session: str) -> bool:
        try:
            self._run("session", "stop", session, timeout=60)
            return True
        except PublisherError:
            return False

    def logged_wechat_tab(self, session: str) -> str:
        """Find one user-owned, already authenticated Official Account tab.

        The URL is inspected only in memory because it can carry a temporary
        platform token.  It is never returned, logged or persisted.
        """
        value = self._run("tab", "list", "--session", session, "--scope", "user", timeout=10).value
        tabs = value.get("tabs", []) if isinstance(value, dict) else []
        matches = [item for item in tabs if isinstance(item, dict)
                   and str(item.get("url", "")).startswith("https://mp.weixin.qq.com/cgi-bin/")]
        if not matches:
            raise PublisherError("logged_tab_not_found", "未找到已登录的公众号后台标签，尚未写入草稿。", True,
                                 next_step="在 BrowserSkill 已连接的 Chrome 中打开并登录公众号后台后重试。")
        if len(matches) != 1 or not matches[0].get("tab_id"):
            raise PublisherError("logged_tab_ambiguous", "检测到多个公众号后台标签，无法安全确定要接管哪一个。", False,
                                 next_step="只保留一个目标公众号后台标签后重试。")
        # Every authenticated `/cgi-bin/` editor route includes its temporary
        # platform token.  A route without one is the login-expired shell; do
        # not make the user approve tab borrowing only to discover that later.
        token = urllib.parse.parse_qs(urllib.parse.urlsplit(str(matches[0].get("url", ""))).query).get("token", [""])[0]
        if not token:
            raise PublisherError("wechat_login_expired", "公众号后台登录已失效，尚未写入草稿。", True,
                                 next_step="在当前 Chrome 标签中重新登录公众号后台，再重新执行草稿投递。")
        return str(matches[0]["tab_id"])

    def borrow_tab(self, session: str, tab_id: str) -> str:
        # This is the single explicit human-confirmation point.  It may wait
        # for the user, unlike all unattended page operations.
        value = self._run("tab", "borrow", "--session", session, tab_id, timeout=90).value
        borrowed = value.get("tab_id") if isinstance(value, dict) else None
        if not borrowed:
            raise PublisherError("browser_protocol_error", "BrowserSkill 未返回已接管标签的标识，尚未写入草稿。", True)
        return str(borrowed)

    def return_tab(self, session: str, tab_id: str) -> bool:
        try:
            self._run("tab", "return", "--session", session, tab_id, timeout=60)
            return True
        except PublisherError:
            return False

    def wait_ms(self, milliseconds: int) -> None:
        # A short polling sleep must never inherit the long action timeout.
        self._run("wait-ms", str(milliseconds), timeout=max(3, milliseconds // 1000 + 2))

    def fill(self, session: str, selector: str, value: str) -> None:
        self._run("fill", "--session", session, "--selector", selector, "--value", value, timeout=30)

    def upload(self, session: str, selector: str, path: Path) -> None:
        """Use BrowserSkill's native input upload; do not relay image bytes."""
        validate_local_image(str(path))
        self._run("upload", "--session", session, "--selector", selector, "--file", str(path), timeout=90)

    def evaluate(self, session: str, expression: str, *, timeout: int = 20) -> Any:
        # Let the daemon finish a bounded CDP call before the local process
        # deadline.  Killing it externally can leave the owned session stuck.
        daemon_timeout = max(1, timeout - 1)
        return self._run("evaluate", "--session", session, "--timeout", f"{daemon_timeout}s", expression,
                         timeout=timeout).value


class LocalPayloadBridge:
    """Serve one bounded, private payload to one fixed editor action."""
    def __init__(self, payload: bytes, content_type: str, suffix: str):
        self.payload = payload
        self.content_type = content_type
        self.suffix = suffix
        self.token = secrets.token_urlsafe(32)
        self.server: http.server.ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None

    def __enter__(self) -> str:
        payload, token, content_type, suffix = self.payload, self.token, self.content_type, self.suffix

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - HTTP handler convention
                if self.path != f"/{token}/payload{suffix}":
                    self.send_error(404)
                    return
                origin = self.headers.get("Origin", "")
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                if origin == "https://mp.weixin.qq.com":
                    self.send_header("Access-Control-Allow-Origin", origin)
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *_: Any) -> None:
                return

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return f"http://127.0.0.1:{self.server.server_port}/{self.token}/payload{self.suffix}"

    def __exit__(self, *_: Any) -> None:
        if self.server:
            self.server.shutdown()
            self.server.server_close()
        if self.thread:
            self.thread.join(timeout=2)


class LocalHtmlBridge(LocalPayloadBridge):
    def __init__(self, markup: str):
        super().__init__(markup.encode("utf-8"), "text/html; charset=utf-8", ".html")


def _value(result: Any) -> Any:
    return result.get("value") if isinstance(result, dict) and "value" in result else result


class BrowserSkillBackend:
    """One-session WeChat draft backend using the verified native editor route."""
    def __init__(self, client: BskClient | None = None, selector_profile: Path | None = None):
        self.client = client or BskClient()
        # Preserve the constructor used by earlier callers.  The reviewed
        # selectors are fixed in this module, not data that can inject actions.
        self.selector_profile = selector_profile

    def preflight(self, bundle: dict[str, Any]) -> dict[str, Any]:
        self.client.ready()
        if not bundle.get("delivery", {}).get("account_alias"):
            raise PublisherError("account_required", "浏览器投递前需要指定已确认的公众号账号别名。")
        validate_local_image(bundle.get("visual", {}).get("cover_path", ""))
        article, delivery = bundle.get("article", {}), bundle.get("delivery", {})
        unsupported = []
        if str(article.get("author", "")).strip(): unsupported.append("作者")
        if str(delivery.get("source_url", "")).strip(): unsupported.append("原文链接")
        if delivery.get("comments", "open") != "open": unsupported.append("留言设置")
        if bool(delivery.get("fans_only")): unsupported.append("粉丝留言限制")
        if unsupported:
            raise PublisherError("browser_field_unsupported", "当前浏览器路线尚不能可靠写入：" + "、".join(unsupported) + "。", True,
                                 next_step="移除这些设置，或使用已验收的官方 API 路线；不会静默忽略字段。")
        return {"status": "ready"}

    def _eval(self, session: str, expression: str, *, timeout: int = 20) -> Any:
        return _value(self.client.evaluate(session, expression, timeout=timeout))

    @staticmethod
    def _visible_matches(text: str, click: bool = False) -> str:
        action = "matches[0].click();" if click else ""
        return """(()=>{const matches=[...document.querySelectorAll('button,a')].filter(e=>
          (e.innerText||'').trim()===%s&&(()=>{const r=e.getBoundingClientRect();return r.width>10&&r.height>10})());
          if(matches.length!==1)return {ok:false,count:matches.length};%sreturn {ok:true};})()""" % (json.dumps(text), action)

    @staticmethod
    def _safe_url(value: Any) -> str:
        """Keep only origin and path in diagnostics; query strings can carry tokens."""
        if not isinstance(value, str):
            return ""
        parsed = urllib.parse.urlsplit(value)
        return f"{parsed.scheme}://{parsed.netloc}{parsed.path}" if parsed.scheme and parsed.netloc else ""

    def _require_click(self, session: str, text: str, code: str, message: str) -> None:
        result = self._eval(session, self._visible_matches(text, click=True))
        if not isinstance(result, dict) or not result.get("ok"):
            raise PublisherError(code, message, True)

    def _require_unique(self, session: str, text: str, code: str, message: str) -> None:
        result = self._eval(session, self._visible_matches(text))
        if not isinstance(result, dict) or not result.get("ok"):
            raise PublisherError(code, message, True)

    def _reject_unsafe_buttons(self, session: str) -> None:
        """Block group-send affordances before any editor write or save click."""
        result = self._eval(session, """(()=>{const denied=new Set(%s);return [...new Set([...document.querySelectorAll('button,a')]
          .map(e=>(e.innerText||'').trim()).filter(text=>denied.has(text)))]})()""" % json.dumps(sorted(UNSAFE_BUTTONS)))
        if isinstance(result, list) and result:
            raise PublisherError("group_send_blocked", "检测到群发或发送给粉丝控件，已停止且没有继续操作。", False,
                                 next_step="请确认处于文章编辑页，而不是群发页面。")

    def _verify_account(self, session: str, expected: str) -> None:
        login_needed = self._eval(session, """(()=>{const text=(document.body&&document.body.innerText)||'';
          return /登录超时|请重新登录|登录失效|扫码登录|重新扫码/.test(text);})()""")
        if login_needed:
            raise PublisherError("browser_login_required", "BrowserSkill 接管的公众号标签已失去登录态，尚未写入。", True,
                                 next_step="在被接管的公众号标签中完成登录后重试。")
        visible = self._wait_until(session, """(()=>{const normalize=value=>String(value||'').normalize('NFKC').replace(/[\\s\\u200b]+/g,'');
          const wanted=normalize(%s);const selectors=[
          '.weui-desktop-account__nickname','.acount_box-nickname',
          '.account_box-panel-head__nickname',
          '#js_name','.weui-desktop-account__name'];
          const names=[...new Set(selectors.flatMap(selector=>[...document.querySelectorAll(selector)])
            .map(e=>normalize(e.textContent)).filter(Boolean))];
          if(names.length)return {loaded:true,matched:names.length===1&&names[0]===wanted,count:names.length};
          return false;})()""" % json.dumps(expected), timeout_ms=5_000)
        if not isinstance(visible, dict) or not visible.get("loaded"):
            raise PublisherError("account_identity_unreadable", "公众号后台尚未显示账号标识，未写入草稿。", True,
                                 next_step="等待页面加载完成后重试；请勿在同一篇文章上重复保存。")
        if int(visible.get("count", 1)) != 1:
            raise PublisherError("account_identity_ambiguous", "公众号后台账号标识不唯一，未写入。", True,
                                 next_step="关闭多余账号面板或刷新后台，仅显示一个账号名称后重试。")
        if not visible.get("matched"):
            raise PublisherError("account_mismatch", "当前浏览器登录的公众号与指定账号不一致，未写入。", True,
                                 next_step="切换到正确账号后重新开始。")

    def _go_to_editor(self, session: str) -> None:
        """Open a new article through the authenticated backend homepage.

        WeChat's legacy direct editor URL no longer resolves reliably.  Keep the
        temporary token inside the page, return to the normal backend homepage,
        then use the one visible ``文章`` entry point.
        """
        result = self._eval(session, """(()=>{const token=new URL(location.href).searchParams.get('token');
          if(!token)return {ok:false};location.assign('/cgi-bin/home?token='+encodeURIComponent(token)+
          '&lang=zh_CN');return {ok:true};})()""")
        if not isinstance(result, dict) or not result.get("ok"):
            raise PublisherError("pre_navigation_failed", "已登录公众号标签缺少有效会话，未执行写入。", True,
                                 next_step="回到公众号后台刷新登录后重试。")
        if not self._wait_until(session, "(()=>[...document.querySelectorAll('button')].filter(e=>(e.innerText||'').trim()==='文章').length===1)()"):
            raise PublisherError("pre_navigation_failed", "公众号后台首页未加载文章创作入口，未执行写入。", True)
        self._require_click(session, "文章", "pre_navigation_failed", "未能唯一打开公众号文章编辑器，未执行写入。")
        if not self._wait_until(session, "(()=>document.querySelectorAll('#title').length===1&&document.querySelectorAll('div.ProseMirror[contenteditable=true]:not([data-placeholder])').length===1)()"):
            raise PublisherError("pre_navigation_failed", "公众号文章编辑器未加载完成，未执行写入。", True)

    def _wait_until(self, session: str, expression: str, *, timeout_ms: int = 12_000) -> Any:
        """Poll a fixed, read-only page condition instead of relying on fixed sleeps."""
        deadline = time.monotonic() + timeout_ms / 1000
        while time.monotonic() < deadline:
            # The per-probe cap leaves room for a bridge round trip but keeps
            # the caller's total deadline authoritative during disconnection.
            remaining = max(1, int(deadline - time.monotonic()) + 1)
            value = self._eval(session, expression, timeout=min(3, remaining))
            if value:
                return value
            if time.monotonic() < deadline:
                self.client.wait_ms(min(300, max(1, int((deadline - time.monotonic()) * 1000))))
        return None

    def _upload_cover(self, session: str, cover: Path) -> None:
        opened = self._eval(session, """(()=>{const all=[...document.querySelectorAll('#js_cover_area .js_imagedialog')];
          const e=all.find(x=>{const r=x.getBoundingClientRect();return r.width>0&&r.height>0})||all[0];
          if(!e)return {ok:false};e.click();return {ok:true};})()""")
        if not isinstance(opened, dict) or not opened.get("ok"):
            raise PublisherError("cover_dialog_unavailable", "未能打开公众号封面选择窗口，尚未保存草稿。", True)
        selector = ".weui-desktop-upload_global-media input[type=file]"
        if not self._wait_until(session, "(()=>document.querySelectorAll(%s).length===1)()" % json.dumps(selector)):
            raise PublisherError("cover_dialog_unavailable", "封面选择窗口未加载完成，尚未保存草稿。", True)
        baseline = self._eval(session, "(()=>[...document.querySelectorAll('.weui-desktop-img-picker__item')].filter(e=>{const r=e.getBoundingClientRect();return r.width>1&&r.height>1}).length)()")
        if not isinstance(baseline, int):
            raise PublisherError("cover_dialog_unavailable", "封面图片库基线无法读取，尚未保存草稿。", True)
        self.client.upload(session, selector, cover)
        if not self._wait_until(session, "(()=>[...document.querySelectorAll('.weui-desktop-img-picker__item')].filter(e=>{const r=e.getBoundingClientRect();return r.width>1&&r.height>1}).length>%d)()" % baseline):
            raise PublisherError("cover_upload_failed", "封面上传后未出现在公众号图片库，尚未保存草稿。", True)
        selected = self._eval(session, """(()=>{const items=[...document.querySelectorAll('.weui-desktop-img-picker__item')].filter(e=>{
          const r=e.getBoundingClientRect();return r.width>1&&r.height>1});if(items.length<=%d)return {ok:false,count:items.length};
          items[items.length-1].click();return {ok:true};})()""" % baseline)
        if not isinstance(selected, dict) or not selected.get("ok"):
            raise PublisherError("cover_upload_failed", "封面上传后未能在图片库中唯一定位，尚未保存草稿。", True)
        self._require_click(session, "下一步", "cover_dialog_unavailable", "封面裁切窗口未能进入，尚未保存草稿。")
        if not self._wait_until(session, """(()=>{const matches=[...document.querySelectorAll('button,a')].filter(e=>{
          const r=e.getBoundingClientRect();return (e.innerText||'').trim()==='确认'&&r.width>10&&r.height>10});
          return matches.length===1;})()"""):
            raise PublisherError("cover_dialog_unavailable", "封面裁切窗口未加载完成，尚未保存草稿。", True)
        self._require_click(session, "确认", "cover_dialog_unavailable", "封面裁切确认不可用，尚未保存草稿。")
        if not self._wait_until(session, """(()=>{const previews=[...document.querySelectorAll('#js_cover_area .js_cover_preview_new')]
          .filter(e=>{const r=e.getBoundingClientRect();return r.width>10&&r.height>10});
          return previews.length===1&&getComputedStyle(previews[0]).backgroundImage!=='none';})()"""):
            raise PublisherError("cover_upload_failed", "封面裁切后未能确认封面已应用，尚未保存草稿。", True)

    def _fill_rich_body(self, session: str, markup: str) -> None:
        with LocalHtmlBridge(markup) as url:
            result = self._eval(session, """(async()=>{const fields=[...document.querySelectorAll('div.ProseMirror[contenteditable=true]:not([data-placeholder])')];
              if(fields.length!==1)return {ok:false,count:fields.length};const response=await fetch(%s);if(!response.ok)return {ok:false};
              const html=await response.text();const field=fields[0];field.focus();field.innerHTML=html;
              field.dispatchEvent(new InputEvent('input',{bubbles:true,inputType:'insertText',data:null}));field.dispatchEvent(new Event('change',{bubbles:true}));
              return {ok:true,htmlLength:field.innerHTML.length};})()""" % json.dumps(url))
        if not isinstance(result, dict) or not result.get("ok"):
            raise PublisherError("body_editor_unavailable", "正文编辑器未能唯一定位或写入，尚未保存草稿。", True)

    def _fields_read_back(self, session: str, title: str, digest: str, body_html: str) -> bool:
        read = self._eval(session, """(()=>{const titles=[...document.querySelectorAll('#title')];
          const visibleTitles=[...document.querySelectorAll('div.ProseMirror[contenteditable=true][data-placeholder="请在这里输入标题"]')];
          const bodies=[...document.querySelectorAll('div.ProseMirror[contenteditable=true]:not([data-placeholder])')];
          const digest=document.querySelectorAll('#js_description');if(titles.length!==1||visibleTitles.length!==1||bodies.length!==1||digest.length!==1)return {ok:false};
          return {ok:true,title:titles[0].value||'',visibleTitle:visibleTitles[0].innerText||'',digest:digest[0].value||'',body:bodies[0].innerHTML||''};})()""")
        return bool(isinstance(read, dict) and read.get("ok") and read.get("title") == title
                    and read.get("visibleTitle") == title and read.get("digest") == digest
                    and semantic_html_signature(str(read.get("body", ""))) == semantic_html_signature(body_html))

    @staticmethod
    def _preview_text(value: str) -> str:
        """Normalise rendered article text without treating HTML as executable."""
        return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", value or ""))).strip()

    def _preview_read_back(self, session: str, title: str, body_html: str) -> bool:
        """Verify the platform's read-only draft preview.

        Opening a draft from the current Official Account list opens a temporary
        preview, not the ProseMirror editor.  Requiring editor selectors here
        created a false ``draft_uncertain`` after a successful save.
        """
        expected = self._preview_text(body_html)
        read = self._eval(session, """(()=>{const normalise=value=>String(value||'').replace(/\\s+/g,' ').trim();
          const titles=[...document.querySelectorAll('h1')].map(e=>normalise(e.innerText));
          const roots=['#js_content','.rich_media_content','article','main'].flatMap(s=>[...document.querySelectorAll(s)]);
          const visible=roots.filter(e=>{const r=e.getBoundingClientRect();return r.width>1&&r.height>1});
          if(visible.length!==1)return {titles,body:'',root_count:visible.length};
          return {titles,body:normalise(visible[0].innerText),root_count:1};})()""")
        return bool(isinstance(read, dict) and read.get("titles", []).count(title) == 1
                    and expected and expected == str(read.get("body", "")))

    def _reopened_editor_read_back(self, session: str, title: str, digest: str, body_html: str) -> bool:
        """Check the persisted editor, including its bound cover.

        A preview is useful evidence but cannot prove summary or cover binding.
        The draft list must expose exactly one edit control for the stable draft
        object; otherwise the correct outcome is uncertain rather than a weak
        successful save.
        """
        if not self._wait_until(session, "(()=>document.querySelectorAll('#title').length===1&&document.querySelectorAll('#js_description').length===1)()"):
            return False
        if not self._fields_read_back(session, title, digest, body_html):
            return False
        cover = self._eval(session, """(()=>{const items=[...document.querySelectorAll('#js_cover_area .js_cover_preview_new')]
          .filter(e=>{const r=e.getBoundingClientRect();return r.width>10&&r.height>10});
          return {count:items.length,bound:items.length===1&&getComputedStyle(items[0]).backgroundImage!=='none'};})()""")
        return bool(isinstance(cover, dict) and cover.get("bound"))

    def _persisted_editor_signature(self, session: str) -> dict[str, Any] | None:
        read = self._eval(session, """(()=>{const titles=[...document.querySelectorAll('#title')];
          const visibleTitles=[...document.querySelectorAll('div.ProseMirror[contenteditable=true][data-placeholder="请在这里输入标题"]')];
          const bodies=[...document.querySelectorAll('div.ProseMirror[contenteditable=true]:not([data-placeholder])')];
          const digest=document.querySelectorAll('#js_description');
          const covers=[...document.querySelectorAll('#js_cover_area .js_cover_preview_new')].filter(e=>{const r=e.getBoundingClientRect();return r.width>10&&r.height>10});
          if(titles.length!==1||visibleTitles.length!==1||bodies.length!==1||digest.length!==1||covers.length!==1)return {ok:false};
          return {ok:true,title:titles[0].value||'',visibleTitle:visibleTitles[0].innerText||'',digest:digest[0].value||'',body:bodies[0].innerHTML||'',cover:getComputedStyle(covers[0]).backgroundImage!=='none'};})()""")
        return read if isinstance(read, dict) and read.get("ok") else None

    def verify_draft(self, task: dict[str, Any]) -> dict[str, Any]:
        """Read one existing browser draft without creating or saving anything."""
        expected, draft_id = task.get("verification", {}), str(task.get("draft_id", ""))
        title, alias = str(expected.get("title", "")), str(expected.get("account_alias", ""))
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", draft_id) or not title or not alias:
            raise PublisherError("verification_unavailable", "任务缺少可安全复核的草稿标识。")
        self.client.ready()
        session = self.client.start(); borrowed = ""
        try:
            borrowed = self.client.borrow_tab(session, self.client.logged_wechat_tab(session))
            self._verify_account(session, alias)
            moved = self._eval(session, """(()=>{const token=new URLSearchParams(location.search).get('token');if(!token)return {ok:false};
              location.assign('/cgi-bin/appmsg?token='+encodeURIComponent(token)+'&lang=zh_CN&t=media/appmsg_list&type=77&action=list&begin=0&count=10');return {ok:true};})()""")
            if not isinstance(moved, dict) or not moved.get("ok"):
                return {"verified": False}
            if not self._wait_until(session, "(()=>location.pathname==='/cgi-bin/appmsg'&&document.querySelectorAll('a').length>0)()"):
                return {"verified": False}
            opened = self._eval(session, """(()=>{const title=%s,id=%s;const matches=[...document.querySelectorAll('a')].filter(a=>(a.innerText||'').trim()===title&&((a.getAttribute('href')||'').match(/[?&]appmsgid=([^&]+)/)||[])[1]===id);
              if(matches.length!==1)return {ok:false};const card=matches[0].closest('li,.appmsg_item_wrp,.weui-desktop-card')||matches[0].parentElement;
              const edits=card?[...card.querySelectorAll('a,button')].filter(e=>(e.innerText||'').trim()==='编辑'):[];
              if(edits.length!==1)return {ok:false};edits[0].click();return {ok:true};})()""" % (json.dumps(title), json.dumps(draft_id)))
            if not isinstance(opened, dict) or not opened.get("ok"):
                return {"verified": False}
            read = self._persisted_editor_signature(session)
            if not read:
                return {"verified": False}
            return {"verified": bool(read["cover"] and read["title"] == title and read["visibleTitle"] == title
                                      and hashlib.sha256(str(read["digest"]).encode()).hexdigest() == expected.get("digest_hash")
                                      and semantic_html_signature(str(read["body"])) == expected.get("html_hash"))}
        finally:
            if borrowed:
                self.client.return_tab(session, borrowed)
            self.client.stop(session)

    @staticmethod
    def _deadline(started: float, limit_s: float, *, write_attempted: bool) -> None:
        if time.monotonic() - started > limit_s:
            raise PublisherError(
                "browser_workflow_timeout",
                "公众号浏览器流程超过时间预算，已停止继续操作。",
                not write_attempted,
                write_attempted=write_attempted,
                next_step=("请先检查草稿箱，不能自动重复保存。" if write_attempted
                           else "检查 BrowserSkill 和公众号后台后可重新执行。"),
            )

    def _draft_baseline(self, session: str, title: str) -> set[str]:
        """Read existing exact-title drafts before any editor write.

        A title alone is not sufficient evidence: an older draft with the
        same title must never make a newly saved draft look verified.
        """
        moved = self._eval(session, """(()=>{const token=new URLSearchParams(location.search).get('token');if(!token)return {ok:false};
          location.assign('/cgi-bin/appmsg?token='+encodeURIComponent(token)+'&lang=zh_CN&t=media/appmsg_list&type=77&action=list&begin=0&count=10');return {ok:true};})()""")
        if not isinstance(moved, dict) or not moved.get("ok"):
            raise PublisherError("draft_baseline_unavailable", "无法读取草稿基线，尚未保存草稿。", True)
        if not self._wait_until(session, "(()=>location.pathname==='/cgi-bin/appmsg'&&document.querySelectorAll('a').length>0)()"):
            raise PublisherError("draft_baseline_unavailable", "草稿列表未能加载，尚未保存草稿。", True)
        result = self._eval(session, """(()=>[...document.querySelectorAll('a')].flatMap(a=>{
          if((a.innerText||'').trim()!==%s)return [];const href=a.getAttribute('href')||'';
          const id=(href.match(/[?&]appmsgid=([^&]+)/)||[])[1]||'';
          return /^[A-Za-z0-9_-]{1,128}$/.test(id)?[id]:[];}))()""" % json.dumps(title))
        if not isinstance(result, list):
            raise PublisherError("draft_baseline_unavailable", "草稿基线格式异常，尚未保存草稿。", True)
        return {str(item) for item in result}

    def _verify_draft(self, session: str, title: str, digest: str, body_html: str,
                      baseline_ids: set[str]) -> dict[str, str]:
        moved = self._eval(session, """(()=>{const token=new URLSearchParams(location.search).get('token');if(!token)return {ok:false};
          location.assign('/cgi-bin/appmsg?token='+encodeURIComponent(token)+'&lang=zh_CN&t=media/appmsg_list&type=77&action=list&begin=0&count=10');return {ok:true};})()""")
        if not isinstance(moved, dict) or not moved.get("ok"):
            raise PublisherError("draft_uncertain", "草稿已提交但无法进入草稿列表回读，请人工核对，不能自动重试。", write_attempted=True)
        if not self._wait_until(session, "(()=>[...document.querySelectorAll('a')].some(a=>(a.innerText||'').trim()===%s))()" % json.dumps(title)):
            raise PublisherError("draft_uncertain", "草稿已提交但草稿箱未能唯一回读，请人工核对，不能自动重试。", write_attempted=True)
        result = self._eval(session, """(()=>{const matches=[...document.querySelectorAll('a')].filter(a=>(a.innerText||'').trim()===%s);
          if(matches.length!==1)return {ok:false,count:matches.length};const titleLink=matches[0],href=titleLink.getAttribute('href')||'';
          const id=(href.match(/[?&]appmsgid=([^&]+)/)||[])[1]||'';if(!/^[A-Za-z0-9_-]{1,128}$/.test(id))return {ok:false,reason:'no_safe_id'};
          const card=titleLink.closest('li,.appmsg_item_wrp,.weui-desktop-card')||titleLink.parentElement;
          const edits=card?[...card.querySelectorAll('a,button')].filter(e=>(e.innerText||'').trim()==='编辑'):[];
          if(edits.length!==1)return {ok:false,reason:'edit_not_unique',count:edits.length};
          edits[0].click();return {ok:true,draft_id:id};})()""" % json.dumps(title))
        if not isinstance(result, dict) or not result.get("ok") or not result.get("draft_id"):
            raise PublisherError("draft_uncertain", "草稿已提交但草稿箱未能唯一回读，请人工核对，不能自动重试。", write_attempted=True)
        if str(result["draft_id"]) in baseline_ids:
            raise PublisherError("draft_uncertain", "草稿箱只命中保存前已有的同名草稿，请人工核对，不能自动重试。", write_attempted=True)
        if not self._reopened_editor_read_back(session, title, digest, body_html):
            raise PublisherError("draft_uncertain", "草稿已命中但重新打开后字段或封面回读不完整，请人工核对，不能自动重试。", write_attempted=True)
        return {"draft_id": str(result["draft_id"])}

    def deliver(self, bundle: dict[str, Any], delivery: str, confirmed: bool, *, before_publish: Any = None,
                on_effect: Any = None, readiness: dict[str, Any] | None = None) -> dict[str, Any]:
        """Save exactly one draft, retaining the editor after uncertain writes."""
        if readiness is None:
            self.preflight(bundle)
        if delivery != "draft":
            raise PublisherError("browser_publish_unavailable", "公众号浏览器公开发布尚未通过单次点击和作品回读验收，未执行写入。")
        article, alias = bundle["article"], bundle["delivery"]["account_alias"]
        if any(block.get("type") == "image" for block in article["body_blocks"]):
            raise PublisherError("browser_inline_images_unavailable", "浏览器内文图片上传尚未完成精确回读验收，未开始写入。", True,
                                 next_step="先移除内文图片，或改用已验收的官方 API 图片路线。")
        body_html = render_html(article["body_blocks"], layout_profile(bundle["brief"]["content_type"]))
        cover = validate_local_image(bundle["visual"]["cover_path"])
        started, automatic_started = time.monotonic(), 0.0
        phase_ms: dict[str, int] = {}
        session = self.client.start()
        borrowed_tab, initial_url, current_url = "", "", ""
        preserve_page, fields_written = False, False
        result: dict[str, Any] | None = None
        active_error: PublisherError | None = None
        if on_effect:
            on_effect("session_created", True)

        def checkpoint(name: str) -> None:
            phase_ms[name] = int((time.monotonic() - automatic_started) * 1000)

        try:
            borrowed_tab = self.client.borrow_tab(session, self.client.logged_wechat_tab(session))
            automatic_started = time.monotonic()
            self.client.set_deadline(automatic_started + 120)
            initial_url = self._eval(session, "location.href")
            self._verify_account(session, alias)
            baseline_ids = self._draft_baseline(session, article["selected_title"])
            self._go_to_editor(session)
            checkpoint("editor_ready")
            current_url = self._eval(session, "location.href")
            self._verify_account(session, alias)
            self._reject_unsafe_buttons(session)
            fields_written = True
            self.client.fill(session, 'div.ProseMirror[contenteditable=true][data-placeholder="请在这里输入标题"]', article["selected_title"])
            self.client.fill(session, '#js_description', article.get("summary", ""))
            self._fill_rich_body(session, body_html)
            self._upload_cover(session, cover)
            if on_effect:
                on_effect("editor_fields_written", True)
            checkpoint("fields_ready")
            if not self._fields_read_back(session, article["selected_title"], article.get("summary", ""), body_html):
                raise PublisherError("draft_unverified", "编辑器字段回读不一致，尚未保存草稿。", True)
            self._reject_unsafe_buttons(session)
            self._require_unique(session, "保存为草稿", "draft_button_unavailable", "未能唯一找到保存草稿按钮，尚未写入。")
            # This durable marker must precede the click.  If BrowserSkill
            # loses the response, the caller can never treat this as retryable.
            if on_effect:
                on_effect("draft_submit_intent", True)
            preserve_page = True
            self._require_click(session, "保存为草稿", "draft_button_unavailable", "保存草稿按钮在提交前发生变化，未执行写入。")
            if on_effect:
                on_effect("draft_save_clicked", True)
            checkpoint("draft_submit_dispatched")
            # A toast is only supplemental evidence.  It must never decide
            # success or prevent the bounded list/edit readback.
            self._wait_until(session, "(()=>document.body.innerText.includes('已保存'))()", timeout_ms=3_000)
            draft = self._verify_draft(session, article["selected_title"], article.get("summary", ""), body_html, baseline_ids)
            if on_effect:
                on_effect("draft_save_confirmed", True)
            checkpoint("draft_verified")
            result = {"stage": "drafted", **draft, "account_alias": alias,
                      "title": article["selected_title"], "fields_read_back": True,
                      "timing_ms": phase_ms,
                      "human_wait_ms": int((automatic_started - started) * 1000),
                      "total_elapsed_ms": int((time.monotonic() - started) * 1000),
                      "session_count": 1, "about_blank_seen": current_url == "about:blank",
                      "window_guard": {"initial_url": self._safe_url(initial_url),
                                       "target_url": self._safe_url(current_url), "initial_blank_hidden": initial_url == "about:blank"}}
            preserve_page = False
        except PublisherError as error:
            active_error = error
            preserve_page = preserve_page or fields_written
            elapsed = int((time.monotonic() - started) * 1000)
            error.diagnostics.update({"stage_durations_ms": phase_ms, "human_wait_ms": int(max(0, automatic_started - started) * 1000),
                                      "total_elapsed_ms": elapsed, "page_preserved": preserve_page})
            raise
        finally:
            self.client.set_deadline(None)
            cleanup = {"tab_returned": False, "session_stopped": False, "session_preserved": preserve_page}
            if not preserve_page:
                if borrowed_tab:
                    cleanup["tab_returned"] = self.client.return_tab(session, borrowed_tab)
                cleanup["session_stopped"] = self.client.stop(session)
            if result is not None:
                result["cleanup"] = cleanup
                result["total_elapsed_ms"] = int((time.monotonic() - started) * 1000)
            elif active_error is not None:
                active_error.diagnostics["cleanup"] = cleanup
        if result is None:
            raise PublisherError("backend_error", "浏览器未返回投递结果。", True)
        return result
