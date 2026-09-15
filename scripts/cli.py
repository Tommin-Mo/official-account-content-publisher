#!/usr/bin/env python3
"""Safe JSON command surface for the WeChat publisher.

Compose/review/cover-plan/finalize/preview never open a browser or call a
provider. ``deliver`` is the only mutating command and uses a final bundle.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from browser_skill import BskClient, BrowserSkillBackend
from cover import cover_plan, generate_cover, provider_confirmation, save_provider_reference
from publisher import (AtomicTaskStore, ContentValidator, Publisher, PublisherError, WechatAPI,
                       finalize_bundle, layout_profile, render_html, review_bundle)

ROOT = Path(__file__).resolve().parents[1]


def default_state_dir() -> Path:
    """Keep mutable account state outside the installed Skill tree."""
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "WorkBuddy" / "wechat-official-content-publisher"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "WorkBuddy" / "wechat-official-content-publisher"
    return Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")) / "workbuddy" / "wechat-official-content-publisher"


STATE = Path(os.environ.get("WECHAT_PUBLISHER_STATE", default_state_dir()))


def load_bundle(path: str) -> dict:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise PublisherError("invalid_bundle", "内容包文件不存在或不是有效 JSON。", True) from error


def emit(value: dict, output: str = "") -> None:
    if output:
        target = Path(output)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(value, ensure_ascii=False))


def api_factory() -> WechatAPI:
    token = os.environ.get("WECHAT_ACCESS_TOKEN", "")
    if token: return WechatAPI(token)
    return WechatAPI.from_credentials(os.environ.get("WECHAT_APP_ID", ""), os.environ.get("WECHAT_APP_SECRET", ""))


CONTENT_TYPE_LABELS = {
    "brief": "快讯/通知", "engagement": "互动引流", "event": "活动招募", "product": "产品/服务",
    "case": "案例复盘", "tutorial": "教程/方法论", "analysis": "深度观点/行业分析", "interview": "访谈/人物",
}
DEFAULT_TARGET_LENGTH = {"brief": 700, "engagement": 1200, "event": 1400, "product": 1800,
                         "case": 2200, "tutorial": 2600, "analysis": 3200, "interview": 3600}


def brief_questions(brief: dict) -> list[str]:
    questions: list[str] = []
    if not str(brief.get("theme", "")).strip(): questions.append("这篇文章的主题或标题是什么？")
    if brief.get("content_type") not in CONTENT_TYPE_LABELS:
        questions.append("这篇更接近快讯、互动引流、活动招募、产品服务、案例复盘、教程、行业分析，还是访谈？")
    if not str(brief.get("audience", "")).strip(): questions.append("主要写给谁看？")
    if not str(brief.get("intent", "")).strip(): questions.append("希望读者看完获得什么，或完成什么动作？")
    return questions


def normalized_brief(brief: dict) -> dict:
    kind = str(brief["content_type"])
    return {"theme": str(brief["theme"]).strip(), "audience": str(brief["audience"]).strip(), "content_type": kind,
            "intent": str(brief["intent"]).strip(), "target_length": int(brief.get("target_length") or DEFAULT_TARGET_LENGTH[kind]),
            "voice_profile": str(brief.get("voice_profile") or "清晰、可信、适合公众号阅读"),
            "conversion_goal": str(brief.get("conversion_goal") or "none")}


def _empty_bundle(brief: dict, *, stage: str = "brief_ready", warnings: list[str] | None = None) -> dict:
    return {"schema_version": "1.1", "stage": stage, "brief": brief, "sources": [], "facts": [], "insights": [],
            "article": {"title_candidates": [], "selected_title": "", "summary": "", "author": "", "body_blocks": [], "cta": {"primary": "none", "secondary": "none"}},
            "visual": {"cover_prompt": "", "provider": "", "cover_path": "", "aspect_ratio": "2.35:1"},
            "delivery": {"account_alias": "", "comments": "open", "fans_only": False, "source_url": "", "delivery_choice": "draft"},
            "warnings": warnings or ["该内容包仅完成 Brief；补齐来源、事实、洞察和正文后才能 review。"], "article_review_hash": "", "content_hash": ""}


def compose(source: str) -> dict:
    """Create a no-write brief scaffold, or ask for missing intent before writing."""
    try:
        brief = json.loads(Path(source).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise PublisherError("invalid_bundle", "Brief 文件不存在或不是有效 JSON。", True) from error
    if not isinstance(brief, dict):
        raise PublisherError("invalid_bundle", "Brief 必须是 JSON 对象。", True)
    questions = brief_questions(brief)
    if questions:
        return {"status": "needs_user_action", "stage": "awaiting_brief_confirmation", "questions": questions,
                "write_attempted": False, "next_step": "补齐以上信息并由用户确认内容方向后，再生成全文。"}
    return _empty_bundle(normalized_brief(brief))


def _blocks_from_existing_text(text: str) -> list[dict]:
    blocks, paragraph, items = [], [], []
    def flush() -> None:
        nonlocal paragraph, items
        if paragraph: blocks.append({"type": "paragraph", "text": "\n".join(paragraph)}); paragraph = []
        if items: blocks.append({"type": "list", "items": items}); items = []
    for raw in text.replace("\r\n", "\n").split("\n"):
        line = raw.strip()
        if not line: flush(); continue
        if line.startswith("### "): flush(); blocks.append({"type": "h3", "text": line[4:].strip()}); continue
        if line.startswith("## ") or line.startswith("# "): flush(); blocks.append({"type": "h2", "text": line.lstrip("#").strip()}); continue
        if line.startswith(("- ", "* ")): paragraph and flush(); items.append(line[2:].strip()); continue
        if line.startswith("> "): flush(); blocks.append({"type": "quote", "text": line[2:].strip()}); continue
        items and flush(); paragraph.append(line)
    flush()
    return blocks


def ingest_existing(source: str) -> dict:
    """Import existing copy without rewriting it; review remains a separate choice."""
    try:
        value = json.loads(Path(source).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise PublisherError("existing_copy_invalid", "已有文案文件不存在或不是有效 JSON。", True) from error
    if not isinstance(value, dict) or not str(value.get("title", "")).strip() or not str(value.get("body", "")).strip():
        raise PublisherError("existing_copy_invalid", "已有文案需要提供 title 和 body。", True)
    raw_brief = value.get("brief", {})
    if not isinstance(raw_brief, dict): raise PublisherError("invalid_bundle", "已有文案的 Brief 必须是 JSON 对象。", True)
    questions = brief_questions(raw_brief)
    if questions:
        return {"status": "needs_user_action", "stage": "awaiting_brief_confirmation", "questions": questions,
                "existing_copy_preserved": True, "title": str(value["title"]).strip(), "write_attempted": False,
                "next_step": "确认文章定位后再导入；原文不会被自动改写。"}
    brief = normalized_brief(raw_brief)
    blocks = _blocks_from_existing_text(str(value["body"]))
    result = _empty_bundle(brief, stage="existing_copy_ready", warnings=["已有文案已原样导入；尚未做事实审查、改写、排版或投递。"])
    result["stage"] = "existing_copy_ready"
    result["article"].update({"title_candidates": [str(value["title"]).strip()], "selected_title": str(value["title"]).strip(),
                              "summary": str(value.get("summary") or next((b.get("text", "") for b in blocks if b["type"] == "paragraph"), ""))[:120],
                              "body_blocks": blocks})
    return result


def stored_provider() -> dict:
    path = STATE / "cover-provider.json"
    if not path.is_file():
        raise PublisherError("cover_provider_required", "请先选择一次封面 Provider。", True,
                             next_step="使用 configure-provider 保存 GPT、WorkBuddy 或 wechat-cover 的偏好。")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise PublisherError("cover_provider_required", "封面 Provider 配置无效。", True) from error


def main() -> int:
    parser = argparse.ArgumentParser(prog="wechat-official-content-publisher")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("doctor")
    p = sub.add_parser("compose"); p.add_argument("source"); p.add_argument("--out", default="")
    p = sub.add_parser("configure-provider"); p.add_argument("provider", choices=["gpt", "workbuddy", "wechat-cover"]); p.add_argument("--model", default=""); p.add_argument("--confirm-provider", action="store_true")
    p = sub.add_parser("ingest-existing"); p.add_argument("source"); p.add_argument("--out", default="")
    p = sub.add_parser("review"); p.add_argument("content_bundle"); p.add_argument("--out", default="")
    p = sub.add_parser("cover-plan"); p.add_argument("content_bundle"); p.add_argument("--provider", default=""); p.add_argument("--model", default="")
    p = sub.add_parser("cover-generate"); p.add_argument("content_bundle"); p.add_argument("--out", required=True); p.add_argument("--confirm-generation", action="store_true")
    p = sub.add_parser("finalize"); p.add_argument("content_bundle"); p.add_argument("--cover", required=True); p.add_argument("--provider", default=""); p.add_argument("--out", default="")
    p = sub.add_parser("preview"); p.add_argument("content_bundle"); p.add_argument("--out", default="")
    p = sub.add_parser("deliver"); p.add_argument("content_bundle"); p.add_argument("--delivery", choices=["draft", "publish"], default="draft"); p.add_argument("--backend", choices=["auto", "api", "browser"], default="auto"); p.add_argument("--confirm-publish", action="store_true")
    p = sub.add_parser("status"); p.add_argument("task_id")
    p = sub.add_parser("verify"); p.add_argument("task_id")
    p = sub.add_parser("abort"); p.add_argument("task_id")
    args = parser.parse_args()
    try:
        if args.command == "doctor":
            api_ready = bool(os.environ.get("WECHAT_ACCESS_TOKEN")) or bool(os.environ.get("WECHAT_APP_ID") and os.environ.get("WECHAT_APP_SECRET"))
            try:
                status = BskClient().ready()
                browser = {"status": "ready", "connected_browsers": len(status.get("browsers", [])),
                           "native_cover_upload": True, "draft_delivery": True, "public_publish": False}
            except PublisherError as error:
                browser = {"status": "needs_user_action", "error_code": error.code}
            emit({"status": "ready" if api_ready or browser["status"] == "ready" else "needs_user_action", "api_configured": api_ready, "browser": browser}); return 0
        if args.command == "compose": emit(compose(args.source), args.out); return 0
        if args.command == "ingest-existing": emit(ingest_existing(args.source), args.out); return 0
        if args.command == "configure-provider":
            if not args.confirm_provider: emit(provider_confirmation(args.provider, args.model)); return 0
            emit(save_provider_reference(str(STATE), args.provider, args.model, confirmed=True)); return 0
        if args.command == "review": emit(review_bundle(load_bundle(args.content_bundle)), args.out); return 0
        if args.command == "cover-plan":
            bundle = load_bundle(args.content_bundle); ContentValidator().validate_review(bundle)
            provider_data = stored_provider() if not args.provider else {"provider": args.provider, "model": args.model}
            emit({"status": "cover_plan_ready", **cover_plan(bundle, provider_data.get("provider", ""), args.model or provider_data.get("model", "")), "article_review_hash": bundle.get("article_review_hash", "")}); return 0
        if args.command == "cover-generate":
            bundle = load_bundle(args.content_bundle); ContentValidator().validate_review(bundle)
            provider_data = stored_provider()
            emit(generate_cover(bundle, provider_data["provider"], provider_data.get("model", ""), args.out,
                                confirmed=args.confirm_generation)); return 0
        if args.command == "finalize":
            provider = args.provider or stored_provider().get("provider", "")
            emit(finalize_bundle(load_bundle(args.content_bundle), args.cover, provider), args.out); return 0
        if args.command == "preview":
            bundle = load_bundle(args.content_bundle); ContentValidator().validate_review(bundle)
            emit({"status": "preview_ready", "profile": layout_profile(bundle["brief"]["content_type"]),
                  "html": render_html(bundle["article"]["body_blocks"], layout_profile(bundle["brief"]["content_type"])),
                  "viewports": [375, 393]}, args.out); return 0
        if args.command == "deliver":
            bundle = load_bundle(args.content_bundle)
            ContentValidator().validate_final(bundle)
            api_configured = bool(os.environ.get("WECHAT_ACCESS_TOKEN")) or bool(os.environ.get("WECHAT_APP_ID") and os.environ.get("WECHAT_APP_SECRET"))
            publisher = Publisher(AtomicTaskStore(STATE / "tasks"), api_factory if api_configured else None,
                                  browser_backend=BrowserSkillBackend())
            result = publisher.deliver(bundle, args.delivery, args.backend, args.confirm_publish)
            emit({"status": result.stage, **result.detail, "task_id": result.task["task_id"]}); return 0
        api_configured = bool(os.environ.get("WECHAT_ACCESS_TOKEN")) or bool(os.environ.get("WECHAT_APP_ID") and os.environ.get("WECHAT_APP_SECRET"))
        publisher = Publisher(AtomicTaskStore(STATE / "tasks"), api_factory if api_configured else None,
                              browser_backend=BrowserSkillBackend())
        output = publisher.status(args.task_id) if args.command == "status" else (publisher.verify(args.task_id) if args.command == "verify" else publisher.abort(args.task_id))
        emit(output); return 0
    except PublisherError as error:
        emit({"status": "failed", **error.as_dict()}); return 2
    except (OSError, ValueError, KeyError, TimeoutError) as error:
        emit({"status": "failed", "error_code": "internal_error", "user_message": "处理未完成，尚未确认任何公众号写入。",
              "next_step": "检查内容包或运行环境后重试。", "retry_allowed": True,
              "write_attempted": False, "submission_clicked": False}); return 2


if __name__ == "__main__": raise SystemExit(main())
