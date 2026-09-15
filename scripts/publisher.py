#!/usr/bin/env python3
"""微信公众号内容包校验、投递状态机与官方 API / CDP 后端。

本模块只使用 Python 标准库。它不会群发、删除草稿或作品；公开发布需要
显式确认，且同一任务最多提交一次。
"""
from __future__ import annotations

import contextlib
import hashlib
import html
import inspect
import json
import os
import re
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable


SCHEMA_VERSION = "1.1"
CONTENT_TYPES = {
    "brief": (400, 900), "engagement": (800, 1500), "event": (1000, 1800),
    "product": (1200, 2500), "case": (1500, 3000), "tutorial": (1800, 3500),
    "analysis": (2500, 5000), "interview": (2500, 6000),
}
DEEP_TYPES = {"analysis"}
WRITE_STAGES = {"finalized", "preparing", "drafted", "publishing", "published", "draft_uncertain", "publish_uncertain"}
# A task that was created but did not reach the adapter is still a live
# delivery intent.  Treating it as reusable made two processes able to submit
# the same finalized bundle between task creation and the first browser call.
BLOCKING_STAGES = {"finalized", "preparing", "drafted", "publishing", "published", "draft_uncertain", "publish_uncertain"}
TASK_ID_RE = re.compile(r"wx-[0-9a-f]{16}")
UNSAFE_BUTTONS = {"群发", "发送给粉丝", "群发消息", "发送"}


class PublisherError(Exception):
    def __init__(self, code: str, message: str, retry_allowed: bool = False, *, write_attempted: bool = False,
                 submission_clicked: bool = False, next_step: str = "", diagnostics: dict[str, Any] | None = None):
        super().__init__(message)
        self.code, self.retry_allowed = code, retry_allowed
        self.write_attempted, self.submission_clicked, self.next_step = write_attempted, submission_clicked, next_step
        self.diagnostics = diagnostics or {}

    def as_dict(self) -> dict[str, Any]:
        result = {"error_code": self.code, "message": str(self), "user_message": str(self),
                "next_step": self.next_step or ("修复问题后可重试。" if self.retry_allowed else "请先核对当前状态，不要重复提交。"),
                "retry_allowed": self.retry_allowed, "write_attempted": self.write_attempted,
                "submission_clicked": self.submission_clicked}
        if self.diagnostics:
            result["diagnostics"] = self.diagnostics
        return result


def canonical(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def content_hash(bundle: dict[str, Any]) -> str:
    copy = json.loads(canonical(bundle))
    copy.pop("content_hash", None)
    return hashlib.sha256(canonical(copy).encode()).hexdigest()


def article_review_hash(bundle: dict[str, Any]) -> str:
    """Hash the reviewed article, deliberately excluding generated cover output.

    Keeping this separate from ``content_hash`` removes the historical
    review/cover circular dependency: article review freezes prose first;
    finalisation binds that reviewed prose to one validated cover later.
    """
    value = {
        "schema_version": bundle.get("schema_version"), "brief": bundle.get("brief", {}),
        "sources": bundle.get("sources", []), "facts": bundle.get("facts", []),
        "insights": bundle.get("insights", []), "article": bundle.get("article", {}),
        "delivery": bundle.get("delivery", {}),
    }
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def visible_text(blocks: Iterable[dict[str, Any]]) -> str:
    """Return every reader-visible text fragment used for review and length checks."""
    values: list[str] = []
    for block in blocks:
        if block.get("type") == "image":
            continue
        if str(block.get("text", "")).strip():
            values.append(str(block["text"]))
        if isinstance(block.get("items"), list):
            values.extend(str(item) for item in block["items"] if str(item).strip())
    return "\n".join(values)


def normalized_html(value: str) -> str:
    """Normalise harmless editor serialisation differences for readback.

    This intentionally does not attempt a permissive HTML sanitizer: generated
    HTML is already from a closed semantic-block renderer.  It only ignores
    whitespace and attribute ordering introduced by the platform editor.
    """
    compact = re.sub(r">\s+<", "><", value or "")
    compact = re.sub(r"\s+", " ", compact).strip()
    compact = re.sub(r"\s+(/?>)", r"\1", compact)
    return compact


def semantic_html_signature(value: str) -> str:
    """Compare editor output by readable semantics, not serialised markup.

    The Official Account editor may add harmless wrappers and attributes on
    round-trip.  Title/digest remain exact fields; body verification compares
    visible text and image count after the platform has normalised the HTML.
    """
    source = value or ""
    image_count = len(re.findall(r"<img\b", source, re.I))
    text = re.sub(r"<[^>]+>", " ", source)
    text = html.unescape(re.sub(r"\s+", " ", text)).strip()
    return hashlib.sha256(canonical({"text": text, "images": image_count}).encode()).hexdigest()


def validate_local_image(value: str) -> Path:
    path = Path(value).expanduser()
    if path.is_symlink() or not path.is_file() or path.stat().st_size < 128:
        raise PublisherError("image_invalid", "图片文件不存在、为空或不可读取。")
    header = path.read_bytes()[:32]
    if not (header.startswith(b"\x89PNG\r\n\x1a\n") or header.startswith(b"\xff\xd8\xff")):
        raise PublisherError("image_invalid", "图片格式无效。")
    return path


def image_dimensions(value: str) -> tuple[int, int]:
    path = validate_local_image(value)
    raw = path.read_bytes()[:1024 * 1024]
    if raw.startswith(b"\x89PNG\r\n\x1a\n") and len(raw) >= 24:
        return int.from_bytes(raw[16:20], "big"), int.from_bytes(raw[20:24], "big")
    if raw.startswith(b"\xff\xd8"):
        offset = 2
        sof = set(range(0xC0, 0xC4)) | set(range(0xC5, 0xC8)) | set(range(0xC9, 0xCC)) | set(range(0xCD, 0xD0))
        while offset + 9 <= len(raw):
            if raw[offset] != 0xFF: offset += 1; continue
            marker = raw[offset + 1]
            if marker in {0xD8, 0xD9}: offset += 2; continue
            length = int.from_bytes(raw[offset + 2:offset + 4], "big")
            if length < 2 or offset + 2 + length > len(raw): break
            if marker in sof:
                return int.from_bytes(raw[offset + 7:offset + 9], "big"), int.from_bytes(raw[offset + 5:offset + 7], "big")
            offset += 2 + length
    raise PublisherError("image_invalid", "无法读取图片尺寸；请使用有效的 PNG 或 JPEG 封面。")


def image_mime(value: str) -> str:
    header = validate_local_image(value).read_bytes()[:32]
    if header.startswith(b"\x89PNG\r\n\x1a\n"): return "image/png"
    if header.startswith(b"\xff\xd8\xff"): return "image/jpeg"
    raise PublisherError("image_invalid", "图片格式无效。")


def review_bundle(bundle: dict[str, Any]) -> dict[str, Any]:
    """Return a reviewed bundle without requiring a cover or final hash."""
    copy = json.loads(canonical(bundle))
    if copy.get("schema_version") not in {"1.0", SCHEMA_VERSION}:
        raise PublisherError("invalid_bundle", "内容包版本不受支持。")
    ContentValidator().validate_review(copy)
    copy["schema_version"] = SCHEMA_VERSION
    copy["article_review_hash"] = article_review_hash(copy)
    copy["stage"] = "content_reviewed"
    copy["content_hash"] = ""
    return copy


def finalize_bundle(bundle: dict[str, Any], cover_path: str, provider: str) -> dict[str, Any]:
    """Bind a reviewed article to one validated cover and final delivery hash."""
    copy = json.loads(canonical(bundle))
    if copy.get("article_review_hash") != article_review_hash(copy):
        raise PublisherError("content_tampered", "正文、事实或投递设置在审核后被修改，请重新审核。")
    # Import lazily to keep publisher usable as a stdlib-only module.
    from cover import validate_cover
    validated = validate_cover(cover_path, provider)
    copy.setdefault("visual", {}).update(validated)
    copy["visual"]["cover_prompt"] = copy["visual"].get("cover_prompt", "")
    copy["stage"] = "finalized"
    copy["content_hash"] = ""
    copy["content_hash"] = content_hash(copy)
    ContentValidator().validate_final(copy)
    return copy


def contains_instruction_injection(value: Any) -> bool:
    needle = re.compile(r"ignore (?:all |previous )?instructions|忽略(?:以上|之前|所有)?指令|系统提示", re.I)
    if isinstance(value, str):
        return bool(needle.search(value))
    if isinstance(value, dict):
        return any(contains_instruction_injection(item) for item in value.values())
    if isinstance(value, list):
        return any(contains_instruction_injection(item) for item in value)
    return False


class ContentValidator:
    allowed_blocks = {"paragraph", "h2", "h3", "list", "quote", "data_card", "comparison", "steps", "image", "caption", "cta", "divider", "disclaimer"}
    allowed_cta = {"comment_keyword", "private_message", "read_original", "qr_code", "form_registration", "follow_account", "none"}
    allowed_nested = {
        "brief": {"theme", "audience", "content_type", "intent", "target_length", "voice_profile", "conversion_goal"},
        "source": {"id", "url", "title", "publisher", "published_at", "retrieved_at"},
        "fact": {"id", "claim", "source_id", "date", "unit", "scope", "confidence", "quote"},
        "insight": {"id", "evidence_refs", "evidence_cluster", "data_relation", "mechanism", "alternative", "alternative_explanation", "limitation", "implication", "direction", "confidence"},
        "article": {"title_candidates", "selected_title", "summary", "author", "body_blocks", "cta"},
        "visual": {"cover_prompt", "provider", "cover_path", "aspect_ratio", "cover_media_id", "cover_hash", "width", "height"},
        "delivery": {"account_alias", "comments", "fans_only", "source_url", "delivery_choice"},
        "cta": {"primary", "secondary", "asset_confirmed"},
        "block": {"type", "text", "items", "src", "path", "alt", "fact_refs", "insight_refs"},
    }

    @staticmethod
    def _reject_unknown(value: Any, allowed: set[str], label: str) -> None:
        if not isinstance(value, dict) or set(value) - allowed:
            raise PublisherError("invalid_bundle", f"{label}包含不允许的字段。")

    def validate_review(self, bundle: dict[str, Any]) -> list[str]:
        """Validate prose/evidence before cover generation or final freezing."""
        warnings: list[str] = []
        if bundle.get("schema_version") not in {"1.0", SCHEMA_VERSION}:
            raise PublisherError("invalid_bundle", "内容包版本不受支持。")
        unknown = set(bundle) - {"schema_version", "brief", "sources", "facts", "insights", "article", "visual", "delivery", "warnings", "content_hash", "article_review_hash", "stage"}
        if unknown:
            raise PublisherError("invalid_bundle", "内容包含不允许的字段。")
        if contains_instruction_injection(bundle.get("sources", [])):
            warnings.append("资料中的指令性文字仅作为资料处理，不参与执行。")
        brief, article, visual, delivery = (bundle.get(k, {}) for k in ("brief", "article", "visual", "delivery"))
        self._reject_unknown(brief, self.allowed_nested["brief"], "Brief")
        self._reject_unknown(article, self.allowed_nested["article"], "文章")
        self._reject_unknown(visual, self.allowed_nested["visual"], "封面")
        self._reject_unknown(delivery, self.allowed_nested["delivery"], "投递设置")
        if not isinstance(bundle.get("sources", []), list) or not isinstance(bundle.get("facts", []), list) or not isinstance(bundle.get("insights", []), list):
            raise PublisherError("invalid_bundle", "来源、事实和洞察必须使用列表。")
        source_ids: set[str] = set()
        for source in bundle.get("sources", []):
            self._reject_unknown(source, self.allowed_nested["source"], "来源")
            required_source = {"id", "url", "title", "publisher", "published_at", "retrieved_at"}
            if not required_source.issubset(source) or not all(str(source[key]).strip() for key in required_source):
                raise PublisherError("source_fact_unmatched", "来源缺少网址、标题、发布者或日期。")
            if urllib.parse.urlparse(str(source["url"])).scheme != "https":
                raise PublisherError("source_fact_unmatched", "来源网址必须使用 HTTPS。")
            source_id = str(source["id"])
            if source_id in source_ids:
                raise PublisherError("source_fact_unmatched", "来源 ID 不能重复。")
            source_ids.add(source_id)
        for fact in bundle.get("facts", []): self._reject_unknown(fact, self.allowed_nested["fact"], "事实")
        for insight in bundle.get("insights", []): self._reject_unknown(insight, self.allowed_nested["insight"], "洞察")
        self._reject_unknown(article.get("cta", {}), self.allowed_nested["cta"], "CTA")
        kind = brief.get("content_type")
        if kind not in CONTENT_TYPES:
            raise PublisherError("invalid_bundle", "请选择支持的公众号内容类型。")
        title = article.get("selected_title", "").strip()
        if not title or len(title) > 64:
            raise PublisherError("invalid_bundle", "标题不能为空且不得超过 64 个字符。")
        blocks = article.get("body_blocks")
        if not isinstance(blocks, list) or not blocks:
            raise PublisherError("invalid_bundle", "正文需要至少一个语义内容块。")
        for block in blocks:
            if not isinstance(block, dict) or block.get("type") not in self.allowed_blocks:
                raise PublisherError("invalid_bundle", "正文包含不支持的排版块。")
            self._reject_unknown(block, self.allowed_nested["block"], "正文块")
            if block.get("type") == "list":
                items = block.get("items")
                if not isinstance(items, list) or not items or not all(str(item).strip() for item in items):
                    raise PublisherError("invalid_bundle", "列表块需要至少一项可读内容。")
            elif block.get("type") != "divider" and not str(block.get("text", "")).strip() and block.get("type") != "image":
                raise PublisherError("invalid_bundle", "正文块缺少可读内容。")
            if block.get("type") == "image":
                if block.get("src"):
                    if urllib.parse.urlparse(str(block["src"])).scheme != "https": raise PublisherError("image_invalid", "内文图片链接必须为 HTTPS。")
                elif block.get("path"):
                    validate_local_image(str(block["path"]))
                else: raise PublisherError("image_invalid", "内文图片缺少路径或已上传链接。")
            for key in ("fact_refs", "insight_refs"):
                if key in block and (not isinstance(block[key], list) or not all(str(ref).strip() for ref in block[key])):
                    raise PublisherError("invalid_bundle", "正文引用必须使用非空列表。")
        word_count = len(re.sub(r"\s+", "", visible_text(blocks)))
        low, high = CONTENT_TYPES[kind]
        if word_count < low:
            warnings.append(f"当前正文约 {word_count} 字，低于 {kind} 的推荐区间；请确认内容深度是否符合投递目标。")
        if word_count > high * 1.25:
            warnings.append("正文超过推荐区间，请确认是否需要拆分为系列文章。")
        self._validate_facts(bundle, blocks)
        self._validate_insights(bundle, kind)
        self._validate_cta(article.get("cta", {}), delivery)
        return warnings

    def validate_final(self, bundle: dict[str, Any]) -> list[str]:
        warnings = self.validate_review(bundle)
        visual = bundle.get("visual", {})
        if visual.get("aspect_ratio", "2.35:1") != "2.35:1":
            raise PublisherError("cover_invalid", "公众号主封面必须使用 2.35:1 比例。")
        if not visual.get("cover_path"):
            raise PublisherError("cover_generation_failed", "封面尚未生成或未通过校验。")
        cover = validate_local_image(str(visual["cover_path"]))
        expected_cover_hash = str(visual.get("cover_hash", ""))
        actual_cover_hash = hashlib.sha256(cover.read_bytes()).hexdigest()
        if not expected_cover_hash or actual_cover_hash != expected_cover_hash:
            raise PublisherError("cover_invalid", "封面文件在冻结后被替换或未完成哈希校验，请重新生成并定稿。")
        width, height = image_dimensions(str(visual["cover_path"]))
        if width < 900 or height < 383 or abs(width / height - 2.35) > 0.03:
            raise PublisherError("cover_invalid", "封面必须至少为 900×383，并符合 2.35:1 比例。")
        review_hash = bundle.get("article_review_hash")
        if not review_hash:
            raise PublisherError("content_not_reviewed", "正文尚未通过审核冻结，请先执行 review。")
        if review_hash != article_review_hash(bundle):
            raise PublisherError("content_tampered", "正文、事实或投递设置在审核后被修改，请重新审核。")
        if not bundle.get("content_hash"):
            raise PublisherError("content_not_frozen", "内容尚未完成审核冻结，请先复核后再投递。")
        if bundle["content_hash"] != content_hash(bundle):
            raise PublisherError("content_tampered", "内容包在审核后被修改，请重新审核。")
        return warnings

    def validate(self, bundle: dict[str, Any]) -> list[str]:
        """Compatibility entry point: delivery always needs a final bundle."""
        return self.validate_final(bundle)

    def _validate_facts(self, bundle: dict[str, Any], blocks: list[dict[str, Any]]) -> None:
        facts = bundle.get("facts", [])
        sources = bundle.get("sources", [])
        source_ids = {str(item.get("id", "")) for item in sources if isinstance(item, dict)}
        fact_ids: set[str] = set()
        text_by_fact: dict[str, str] = {}
        for block in blocks:
            if not isinstance(block, dict):
                continue
            fragment = " ".join([str(block.get("text", "")), *[str(item) for item in block.get("items", [])]])
            for ref in block.get("fact_refs", []):
                text_by_fact[str(ref)] = text_by_fact.get(str(ref), "") + " " + fragment
        for index, fact in enumerate(facts, start=1):
            if not isinstance(fact, dict):
                raise PublisherError("source_fact_unmatched", "核心事实格式无效。")
            required = {"claim", "date", "scope", "unit", "confidence"}
            if not required.issubset(fact) or not all(str(fact[k]).strip() for k in required):
                raise PublisherError("source_fact_unmatched", "核心事实缺少来源、日期、口径或单位。")
            fact_id = str(fact.get("id") or f"F{index}")
            if fact_id in fact_ids:
                raise PublisherError("source_fact_unmatched", "核心事实 ID 不能重复。")
            fact_ids.add(fact_id)
            source_id = str(fact.get("source_id", ""))
            # 1.0 bundles used a human-readable ``source`` field.  Accept it
            # during migration, but new bundles must point at a source id.
            if bundle.get("schema_version") == SCHEMA_VERSION:
                if not source_id or source_id not in source_ids:
                    raise PublisherError("source_fact_unmatched", "核心事实必须关联内容包中的来源 ID。")
            elif not str(fact.get("source", "")).strip() and not source_id:
                raise PublisherError("source_fact_unmatched", "核心事实缺少来源、日期、口径或单位。")
            quote = str(fact.get("quote", "")).strip()
            if quote and quote not in visible_text(blocks):
                raise PublisherError("fact_drift", "直接引用与已核对资料不一致。")
            # This is deliberately a narrow deterministic check, not a claim
            # to understand prose: when a referenced claim contains a number
            # or percentage, that exact data token must survive in its block.
            # It catches 10% → 90% drift without restoring brittle full-string
            # fact matching.
            numeric_tokens = re.findall(r"\d+(?:\.\d+)?(?:\s*[%％]|\s*(?:万|亿|千|百|人|家|元|次|年|月|日))?", str(fact.get("claim", "")))
            if numeric_tokens:
                cited = re.sub(r"\s+", "", text_by_fact.get(fact_id, ""))
                if any(re.sub(r"\s+", "", token) not in cited for token in numeric_tokens):
                    raise PublisherError("fact_drift", "正文中引用的核心数字与事实卡不一致。")
        referenced = {str(ref) for block in blocks if isinstance(block, dict) for ref in block.get("fact_refs", [])}
        unknown = referenced - fact_ids
        if unknown:
            raise PublisherError("source_fact_unmatched", "正文引用了不存在的事实 ID。")
        if facts and bundle.get("schema_version") == SCHEMA_VERSION and fact_ids - referenced:
            raise PublisherError("source_fact_unmatched", "每项核心事实都需要由正文块明确引用。")

    def _validate_insights(self, bundle: dict[str, Any], kind: str) -> None:
        insights = bundle.get("insights", [])
        insight_ids: set[str] = set()
        for index, insight in enumerate(insights, start=1):
            if not isinstance(insight, dict):
                raise PublisherError("insufficient_evidence", "洞察格式无效。")
            insight_id = str(insight.get("id") or f"I{index}")
            if insight_id in insight_ids:
                raise PublisherError("insufficient_evidence", "洞察 ID 不能重复。")
            insight_ids.add(insight_id)
        referenced_insights = {str(ref) for block in bundle.get("article", {}).get("body_blocks", [])
                               if isinstance(block, dict) for ref in block.get("insight_refs", [])}
        if referenced_insights - insight_ids:
            raise PublisherError("source_fact_unmatched", "正文引用了不存在的洞察 ID。")
        if kind not in DEEP_TYPES:
            return
        if len({i.get("evidence_cluster") for i in insights if isinstance(i, dict) and i.get("evidence_cluster")}) < 2:
            raise PublisherError("insufficient_evidence", "深度观点或行业分析需要至少两组独立证据。")
        source_origins: dict[str, str] = {}
        for source in bundle.get("sources", []):
            if not isinstance(source, dict):
                continue
            parsed = urllib.parse.urlsplit(str(source.get("url", "")))
            source_origins[str(source.get("id", ""))] = urllib.parse.urlunsplit(
                (parsed.scheme.lower(), parsed.netloc.lower(), parsed.path.rstrip("/"), "", "")
            )
        fact_sources = {
            str(f.get("id", "")): source_origins.get(str(f.get("source_id", "")), "")
            for f in bundle.get("facts", []) if isinstance(f, dict)
        }
        cluster_sources: dict[str, set[str]] = {}
        for insight in insights:
            alternative = insight.get("alternative", insight.get("alternative_explanation", ""))
            required = {"data_relation", "mechanism", "limitation", "implication", "direction", "confidence"}
            if not required.issubset(insight) or not all(str(insight[k]).strip() for k in required) or not str(alternative).strip():
                raise PublisherError("insufficient_evidence", "核心数据尚未完成机制、反方、限制或方向分析。")
            if bundle.get("schema_version") == SCHEMA_VERSION:
                refs = insight.get("evidence_refs", [])
                if not isinstance(refs, list) or not refs or any(str(ref) not in fact_sources for ref in refs):
                    raise PublisherError("insufficient_evidence", "行业分析的每项洞察都需要关联证据。")
                cluster = str(insight.get("evidence_cluster", ""))
                cluster_sources.setdefault(cluster, set()).update(fact_sources[str(ref)] for ref in refs)
        independent_sources = {source for sources in cluster_sources.values() for source in sources if source}
        if len(independent_sources) < 2:
            raise PublisherError("insufficient_evidence", "深度观点或行业分析需要来自至少两个独立来源的证据。")

    def _validate_cta(self, cta: dict[str, Any], delivery: dict[str, Any]) -> None:
        primary = cta.get("primary", "none")
        secondary = cta.get("secondary", "none")
        if primary not in self.allowed_cta or secondary not in self.allowed_cta or (primary == "none" and secondary != "none"):
            raise PublisherError("invalid_bundle", "CTA 配置不合法。")
        if primary in {"qr_code", "form_registration", "read_original"} and not delivery.get("source_url"):
            raise PublisherError("cta_asset_required", "主 CTA 需要用户提供或确认对应链接、二维码或表单。")
        if primary in {"qr_code", "form_registration", "read_original"} and cta.get("asset_confirmed") is not True:
            raise PublisherError("cta_asset_required", "主 CTA 的链接、二维码或表单尚未由用户确认。")


LAYOUT_PROFILES = {
    "analysis": {"accent": "#2b5b84", "card": "#eef5fb"},
    "tutorial": {"accent": "#237a57", "card": "#eff8f2"},
    "campaign": {"accent": "#a14a2a", "card": "#fff5ef"},
    "narrative": {"accent": "#71579a", "card": "#f6f1fb"},
}


def layout_profile(content_type: str) -> str:
    if content_type == "analysis": return "analysis"
    if content_type in {"tutorial", "case"}: return "tutorial"
    if content_type in {"engagement", "event", "product", "brief"}: return "campaign"
    return "narrative"


def render_html(blocks: Iterable[dict[str, Any]], profile: str = "analysis",
                image_resolver: Callable[[dict[str, Any]], str] | None = None) -> str:
    """输出内联、白名单 HTML；不接受原始 HTML/CSS/脚本。"""
    palette = LAYOUT_PROFILES.get(profile, LAYOUT_PROFILES["analysis"])
    styles = {
        "p": "margin:1em 0;line-height:1.85;color:#333;font-size:16px;",
        "h2": "margin:1.6em 0 .7em;font-size:20px;color:#202020;",
        "h3": "margin:1.35em 0 .55em;font-size:17px;color:#202020;",
    }
    out: list[str] = []
    for block in blocks:
        kind, text = block["type"], html.escape(str(block.get("text", "")), quote=True).replace("\n", "<br/>")
        if kind == "paragraph": out.append(f'<p style="{styles["p"]}">{text}</p>')
        elif kind in {"h2", "h3"}: out.append(f'<{kind} style="{styles[kind]}">{text}</{kind}>')
        elif kind == "list":
            items = block.get("items", [])
            out.append('<ul style="padding-left:1.4em;line-height:1.85;">' + "".join(f"<li>{html.escape(str(x))}</li>" for x in items) + "</ul>")
        elif kind == "quote": out.append(f'<blockquote style="margin:1em 0;padding:.7em 1em;border-left:3px solid {palette["accent"]};color:{palette["accent"]};">{text}</blockquote>')
        elif kind in {"data_card", "comparison", "steps"}: out.append(f'<section style="margin:1em 0;padding:1em;background:{palette["card"]};border-radius:6px;">{text}</section>')
        elif kind == "image":
            resolved = image_resolver(block) if image_resolver and not block.get("src") else block.get("src", "")
            src = urllib.parse.urlparse(str(resolved))
            if src.scheme != "https": raise PublisherError("invalid_bundle", "内文图片必须是已上传的 HTTPS 地址。")
            out.append(f'<img src="{html.escape(str(resolved), quote=True)}" style="max-width:100%;height:auto;display:block;margin:1em auto;"/>')
        elif kind == "caption": out.append(f'<p style="text-align:center;color:#888;font-size:13px;">{text}</p>')
        elif kind == "cta": out.append(f'<p style="margin:1.5em 0;padding:1em;background:#f0f7ff;color:#245b96;">{text}</p>')
        elif kind == "divider": out.append('<hr style="border:0;border-top:1px solid #eee;margin:2em 0;"/>')
        elif kind == "disclaimer": out.append(f'<p style="font-size:12px;color:#999;">{text}</p>')
    return "".join(out)


class AtomicTaskStore:
    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, task_id: str) -> Path:
        if not isinstance(task_id, str) or not TASK_ID_RE.fullmatch(task_id):
            raise PublisherError("task_not_found", "任务标识无效。")
        return self.root / f"{task_id}.json"

    def load(self, task_id: str) -> dict[str, Any]:
        try: return json.loads(self.path(task_id).read_text())
        except FileNotFoundError: raise PublisherError("task_not_found", "未找到任务。")

    def save(self, task: dict[str, Any]) -> None:
        target = self.path(task["task_id"])
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=self.root, delete=False) as f:
            json.dump(task, f, ensure_ascii=False, sort_keys=True)
            name = f.name
        os.replace(name, target)

    @contextlib.contextmanager
    def _exclusive(self):
        lock = self.root / ".tasks.lock"
        with open(lock, "a+") as handle:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try: yield
            finally:
                if os.name == "nt": msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else: fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    @contextlib.contextmanager
    def execution_lock(self, digest: str):
        """Hold a per-content lock from task creation through delivery.

        The task record remains the durable duplicate guard after crashes;
        this lock closes the live two-process race without locking unrelated
        articles for the whole browser run.
        """
        lock = self.root / f".run-{digest[:24]}.lock"
        with open(lock, "a+") as handle:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                if os.name == "nt":
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def create(self, bundle: dict[str, Any]) -> dict[str, Any]:
        digest = content_hash(bundle)
        with self._exclusive():
            for path in self.root.glob("*.json"):
                try:
                    existing = json.loads(path.read_text())
                    if existing.get("content_hash") == digest and existing.get("stage") in BLOCKING_STAGES:
                        raise PublisherError("duplicate_task", "相同内容已有高风险任务，不能重复投递。")
                    # A failed API asset upload happened before draft/add.
                    # Reuse only this same side-effect-free task so returned
                    # media references can prevent needless re-uploads.
                    if (existing.get("content_hash") == digest and existing.get("stage") == "failed"
                            and not existing.get("effects", {}).get("draft_submit_intent")
                            and not existing.get("effects", {}).get("draft_save_clicked")
                            and not existing.get("effects", {}).get("publish_clicked")):
                        return existing
                except json.JSONDecodeError: continue
            article = bundle["article"]
            task = {"task_id": f"wx-{uuid.uuid4().hex[:16]}", "stage": "finalized", "content_hash": digest,
                    "created_at": int(time.time()), "publish_attempt": 0, "backend": None,
                    "effects": {"session_created": False, "body_images_uploaded": 0, "body_image_urls": {},
                                "cover_uploaded": False, "cover_media_id": "", "editor_fields_written": False,
                                "draft_submit_intent": False, "draft_save_clicked": False, "draft_save_confirmed": False,
                                "publish_clicked": False},
                    "verification": {"title": article["selected_title"], "content_type": bundle["brief"]["content_type"],
                                     "digest_hash": hashlib.sha256(article.get("summary", "").encode()).hexdigest(),
                                     "html_hash": "", "account_alias": bundle["delivery"].get("account_alias", "")}}
            self.save(task); return task


class WechatAPI:
    """小型官方 API 客户端。transport 便于离线验收，生产默认 urllib。"""
    base = "https://api.weixin.qq.com/cgi-bin"
    def __init__(self, access_token: str, transport: Callable[..., dict[str, Any]] | None = None,
                 upload_transport: Callable[..., dict[str, Any]] | None = None):
        if not access_token: raise PublisherError("credential_missing", "未找到公众号安全凭据。")
        self.token, self.transport, self.upload_transport = access_token, transport or self._request, upload_transport

    @classmethod
    def from_credentials(cls, app_id: str, app_secret: str) -> "WechatAPI":
        if not app_id or not app_secret: raise PublisherError("credential_missing", "未找到公众号安全凭据。")
        query = urllib.parse.urlencode({"grant_type": "client_credential", "appid": app_id, "secret": app_secret})
        try:
            with urllib.request.urlopen(f"https://api.weixin.qq.com/cgi-bin/token?{query}", timeout=20) as response:
                result = json.loads(response.read())
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
            raise PublisherError("credential_unavailable", "无法获取公众号临时凭据，请检查网络、IP 白名单与安全凭据。", True) from error
        if not result.get("access_token"):
            raise PublisherError("credential_rejected", "公众号安全凭据被拒绝，请检查账号权限或白名单。")
        return cls(result["access_token"])

    def _request(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        url = f"{self.base}{path}?access_token={urllib.parse.quote(self.token)}"
        request = urllib.request.Request(url, data=canonical(payload).encode(), headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=25) as response: result = json.loads(response.read())
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
            raise PublisherError("backend_error", "公众号接口暂时不可用；尚未确认写入前请检查网络后重试。", True) from error
        if result.get("errcode", 0): raise PublisherError("api_rejected", "公众号接口拒绝了本次请求，请检查账号权限或素材要求。", result.get("errcode") in {40001, 42001})
        return result

    def draft_add(self, article: dict[str, Any]) -> str:
        return self.transport("/draft/add", {"articles": [article]})["media_id"]
    def draft_get(self, media_id: str) -> dict[str, Any]: return self.transport("/draft/get", {"media_id": media_id})
    def submit_publish(self, media_id: str) -> str: return self.transport("/freepublish/submit", {"media_id": media_id})["publish_id"]
    def get_publish(self, publish_id: str) -> dict[str, Any]: return self.transport("/freepublish/get", {"publish_id": publish_id})
    def get_article(self, article_id: str) -> dict[str, Any]: return self.transport("/freepublish/getarticle", {"article_id": article_id})

    def _upload_file(self, path: str, endpoint: str, field: str = "media") -> dict[str, Any]:
        file = validate_local_image(path)
        if self.upload_transport:
            return self.upload_transport(endpoint, field, file)
        boundary = f"----wechatpublisher{uuid.uuid4().hex}"
        content = file.read_bytes()
        body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{field}\"; filename=\"{file.name}\"\r\n"
                f"Content-Type: {image_mime(str(file))}\r\n\r\n").encode() + content + f"\r\n--{boundary}--\r\n".encode()
        separator = "&" if "?" in endpoint else "?"
        url = f"{self.base}{endpoint}{separator}access_token={urllib.parse.quote(self.token)}"
        request = urllib.request.Request(url, data=body, headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        try:
            with urllib.request.urlopen(request, timeout=40) as response: result = json.loads(response.read())
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
            raise PublisherError("image_upload_failed", "图片上传失败；尚未继续创建草稿。", True) from error
        if result.get("errcode", 0): raise PublisherError("image_upload_failed", "公众号拒绝了图片素材，请检查格式、大小与账号权限。", True)
        return result

    def upload_body_image(self, path: str) -> str: return self._upload_file(path, "/media/uploadimg")["url"]
    def upload_cover(self, path: str) -> str: return self._upload_file(path, "/material/add_material?type=image")["media_id"]


@dataclass
class DeliveryResult:
    stage: str
    task: dict[str, Any]
    detail: dict[str, Any]


class Publisher:
    def __init__(self, store: AtomicTaskStore, api_factory: Callable[[], WechatAPI] | None = None,
                 browser_backend: Any | None = None):
        self.store, self.api_factory, self.browser_backend = store, api_factory, browser_backend

    def deliver(self, bundle: dict[str, Any], delivery: str = "draft", backend: str = "auto", confirm_publish: bool = False) -> DeliveryResult:
        ContentValidator().validate_final(bundle)
        if delivery not in {"draft", "publish"}: raise PublisherError("invalid_delivery", "投递方式只能是 draft 或 publish。")
        if delivery == "publish" and not confirm_publish:
            raise PublisherError("publish_confirmation_required", "公开发布需要明确确认；尚未创建任务或调用任何写入接口。")
        # BrowserSkill is the default route for the user's already logged-in
        # account.  API remains an explicit alternative and is never selected
        # implicitly, so backend choice cannot change behind the user's back.
        if backend == "auto": backend = "browser"
        # Capability checks occur before task/fingerprint creation.  A missing
        # browser runtime or API credential must never manufacture a poisoned
        # task that later looks like an uncertain write.
        readiness: Any = None
        browser_adapter: Any = None
        if backend == "api":
            if not self.api_factory: raise PublisherError("backend_unavailable", "官方 API 后端未配置。", True)
            self.api_factory()
        elif backend == "browser":
            browser_adapter = self.browser_backend or self._default_browser_backend()
            readiness = browser_adapter.preflight(bundle)
        else:
            raise PublisherError("backend_unavailable", "未找到可用的公众号投递后端。")
        # The execution lock starts only after the read-only readiness check;
        # a second process cannot create or dispatch the same content while
        # the first is writing it.
        with self.store.execution_lock(content_hash(bundle)):
            task = self.store.create(bundle)
            if backend == "api": return self._deliver_api(task, bundle, delivery)
            if backend == "browser": return self._deliver_browser(task, bundle, delivery, confirm_publish, browser_adapter, readiness)
        raise PublisherError("backend_unavailable", "未找到可用的公众号投递后端。")

    def _article_payload(self, bundle: dict[str, Any]) -> dict[str, Any]:
        article, delivery = bundle["article"], bundle["delivery"]
        return {"title": article["selected_title"], "author": article.get("author", ""), "digest": article.get("summary", ""),
                "content": render_html(article["body_blocks"], layout_profile(bundle["brief"]["content_type"])), "content_source_url": delivery.get("source_url", ""),
                "thumb_media_id": bundle["visual"].get("cover_media_id", ""), "need_open_comment": 1 if delivery.get("comments", "open") == "open" else 0,
                "only_fans_can_comment": 1 if delivery.get("fans_only") else 0}

    def _upload_api_assets(self, api: WechatAPI, bundle: dict[str, Any], task: dict[str, Any]) -> dict[str, Any]:
        """上传前不修改内容包；最终 HTML 仅使用 API 返回的 HTTPS 图片地址。"""
        copy = json.loads(canonical(bundle))
        effects = task["effects"]
        urls = effects.setdefault("body_image_urls", {})
        for index, block in enumerate(copy["article"]["body_blocks"]):
            if block.get("type") == "image" and not block.get("src"):
                cached = urls.get(str(index))
                if cached:
                    block["src"] = cached
                    block.pop("path", None)
                    continue
                block["src"] = api.upload_body_image(block.pop("path"))
                urls[str(index)] = block["src"]
                effects["body_images_uploaded"] += 1
                self.store.save(task)
        if not copy["visual"].get("cover_media_id"):
            cached_cover = effects.get("cover_media_id", "")
            if cached_cover:
                copy["visual"]["cover_media_id"] = cached_cover
            else:
                copy["visual"]["cover_media_id"] = api.upload_cover(copy["visual"]["cover_path"])
                effects["cover_uploaded"] = True
                effects["cover_media_id"] = copy["visual"]["cover_media_id"]
                self.store.save(task)
        return copy

    def _deliver_api(self, task: dict[str, Any], bundle: dict[str, Any], delivery: str) -> DeliveryResult:
        if not self.api_factory: raise PublisherError("backend_unavailable", "官方 API 后端未配置。")
        api = self.api_factory(); task.update({"stage": "preparing", "backend": "api"}); self.store.save(task)
        try: prepared = self._upload_api_assets(api, bundle, task)
        except PublisherError as error:
            # No draft/add request has been sent yet.  An upload failure is
            # recoverable and must not poison the content fingerprint.
            task["stage"] = "failed"; self.store.save(task)
            raise PublisherError("image_upload_failed", "图片上传失败；尚未创建草稿，可以修复素材或网络后重试。", True,
                                 write_attempted=bool(task["effects"]["body_images_uploaded"] or task["effects"]["cover_uploaded"]),
                                 next_step="检查图片格式、大小与网络后重新投递。") from error
        task["verification"]["html_hash"] = semantic_html_signature(
            render_html(prepared["article"]["body_blocks"], layout_profile(prepared["brief"]["content_type"]))
        )
        self.store.save(task)
        # Persist intent before the side effect.  A dropped response must not
        # be interpreted as “nothing happened”.
        task["effects"]["draft_submit_intent"] = True; self.store.save(task)
        try: media_id = api.draft_add(self._article_payload(prepared))
        except PublisherError as error:
            task["stage"] = "draft_uncertain"; self.store.save(task)
            raise PublisherError("draft_uncertain", "草稿创建请求未得到可验证响应，请先检查草稿箱，不能自动重试。",
                                 write_attempted=True, next_step="按标题在草稿箱核对；确认前不要重复投递。") from error
        task["effects"]["draft_save_clicked"] = True
        task["effects"]["draft_save_confirmed"] = True
        self.store.save(task)
        try: draft = api.draft_get(media_id)
        except PublisherError as error:
            task.update({"stage": "draft_uncertain", "draft_id": media_id}); self.store.save(task)
            raise PublisherError("draft_uncertain", "草稿已提交但无法回读，请先检查草稿箱，不能自动重试。") from error
        items = draft.get("news_item") or draft.get("articles") or []
        if isinstance(items, dict): items = items.get("news_item") or items.get("articles") or [items]
        article = items[0] if isinstance(items, list) and items else {}
        expected = self._article_payload(prepared)
        if (article.get("title") != expected["title"] or article.get("digest") != expected["digest"]
                or semantic_html_signature(article.get("content", "")) != semantic_html_signature(expected["content"])):
            task["stage"] = "draft_uncertain"; self.store.save(task)
            raise PublisherError("draft_uncertain", "草稿写入结果无法精确回读，请先在草稿箱核对，不能自动重试。")
        task.update({"stage": "drafted", "draft_id": media_id}); self.store.save(task)
        if delivery == "draft": return DeliveryResult("drafted", task, {"draft_id": media_id})
        task.update({"stage": "publishing", "publish_attempt": 1}); task["effects"]["publish_clicked"] = True; self.store.save(task)
        try: publish_id = api.submit_publish(media_id)
        except PublisherError as error:
            task["stage"] = "publish_uncertain"; self.store.save(task)
            raise PublisherError("publish_uncertain", "发布提交请求未得到可验证响应，请查询作品列表，不能自动重发。") from error
        status: dict[str, Any] | None = None
        # The official API commonly reports an in-progress state before the
        # article is materialised.  Polling is read-only and avoids treating a
        # normal queue delay as an uncertain public post.
        for _ in range(10):
            try:
                candidate = api.get_publish(publish_id)
            except PublisherError as error:
                task.update({"stage": "publish_uncertain", "publish_id": publish_id}); self.store.save(task)
                raise PublisherError("publish_uncertain", "发布提交已发出但状态查询失败，请查询作品列表，不能自动重发。",
                                     write_attempted=True, submission_clicked=True) from error
            if candidate.get("publish_status") in {0, "success", "published"}:
                status = candidate; break
            if candidate.get("publish_status") in {1, "processing", "pending", None}:
                time.sleep(1)
                continue
            status = candidate; break
        if not status or status.get("publish_status") not in {0, "success", "published"}:
            task.update({"stage": "publish_uncertain", "publish_id": publish_id}); self.store.save(task)
            raise PublisherError("publish_uncertain", "发布提交已发出但结果未能确认，请查询作品列表，不能自动重发。",
                                 write_attempted=True, submission_clicked=True)
        article_id = status.get("article_id") or status.get("article_detail", {}).get("article_id")
        link = status.get("article_url") or status.get("article_detail", {}).get("article_url")
        if not article_id or not link:
            task.update({"stage": "publish_uncertain", "publish_id": publish_id}); self.store.save(task)
            raise PublisherError("publish_uncertain", "发布状态缺少作品标识，不能自动重发。")
        try: published = api.get_article(article_id)
        except PublisherError as error:
            task.update({"stage": "publish_uncertain", "publish_id": publish_id}); self.store.save(task)
            raise PublisherError("publish_uncertain", "作品详情无法回读，请查询作品列表，不能自动重发。") from error
        published_items = published.get("news_item") or published.get("articles") or [published]
        if isinstance(published_items, dict): published_items = published_items.get("news_item") or [published_items]
        verified = published_items[0] if isinstance(published_items, list) and published_items else {}
        if verified.get("title") != bundle["article"]["selected_title"]:
            task.update({"stage": "publish_uncertain", "publish_id": publish_id}); self.store.save(task)
            raise PublisherError("publish_uncertain", "作品标题回读不一致，请查询作品列表，不能自动重发。")
        task.update({"stage": "published", "publish_id": publish_id, "article_id": article_id, "article_url": link, "published_at": int(time.time())}); self.store.save(task)
        return DeliveryResult("published", task, {"article_url": link})

    def _deliver_browser(self, task: dict[str, Any], bundle: dict[str, Any], delivery: str, confirmed: bool,
                         backend: Any | None = None, readiness: Any = None) -> DeliveryResult:
        task.update({"stage": "preparing", "backend": "browser"}); self.store.save(task)
        backend = backend or self.browser_backend or self._default_browser_backend()
        task["verification"]["html_hash"] = semantic_html_signature(
            render_html(bundle["article"]["body_blocks"], layout_profile(bundle["brief"]["content_type"]))
        )
        self.store.save(task)

        def before_publish() -> None:
            # This callback is deliberately invoked by the typed browser
            # adapter immediately before its one allowed final-click command,
            # not before filling fields or opening the editor.
            if task.get("publish_attempt"):
                raise PublisherError("duplicate_publish", "该任务已经进入发布提交，不能再次点击发布。")
            task.update({"stage": "publishing", "publish_attempt": 1})
            task["effects"]["publish_clicked"] = True
            self.store.save(task)

        def on_effect(name: str, value: Any) -> None:
            allowed = {"session_created", "body_images_uploaded", "cover_uploaded", "editor_fields_written",
                       "draft_submit_intent", "draft_save_clicked", "draft_save_confirmed"}
            if name not in allowed:
                raise PublisherError("browser_protocol_error", "浏览器适配器报告了不支持的副作用字段。")
            task["effects"][name] = value
            self.store.save(task)

        parameters = inspect.signature(backend.deliver).parameters
        kwargs: dict[str, Any] = {}
        if "on_effect" in parameters:
            kwargs["on_effect"] = on_effect
        if "readiness" in parameters:
            kwargs["readiness"] = readiness
        if delivery == "publish":
            if "before_publish" not in parameters:
                raise PublisherError("browser_adapter_incomplete", "浏览器适配器未提供发布点击前的原子记录能力，未执行写入。")
            kwargs["before_publish"] = before_publish
        try:
            result = backend.deliver(bundle, delivery, confirmed, **kwargs)
        except PublisherError as error:
            intent = bool(task["effects"].get("draft_submit_intent"))
            if task.get("stage") == "publishing" or error.submission_clicked:
                task["stage"] = "publish_uncertain"
            elif error.write_attempted or intent or task["effects"].get("draft_save_clicked"):
                task["stage"] = "draft_uncertain"
            else:
                task["stage"] = "failed"
            if error.diagnostics:
                task["diagnostics"] = error.diagnostics
            self.store.save(task)
            if task["stage"] in {"draft_uncertain", "publish_uncertain"}:
                raise PublisherError(task["stage"], "提交结果无法确认，请先在公众号后台核对，不能自动重试。", False,
                                     write_attempted=True, submission_clicked=task["stage"] == "publish_uncertain",
                                     next_step="使用本任务标题在草稿箱或发表记录核对；确认前不要重复投递。",
                                     diagnostics=error.diagnostics) from error
            raise
        except Exception as error:
            # Do not leak adapter internals.  The durable effect record is the
            # source of truth for both task stage and retry advice.
            intent = bool(task["effects"].get("draft_submit_intent"))
            task["stage"] = "draft_uncertain" if intent else "failed"; self.store.save(task)
            if intent:
                raise PublisherError("draft_uncertain", "提交结果无法确认，请先在公众号后台核对，不能自动重试。", False,
                                     write_attempted=True, next_step="按标题检查草稿箱；确认前不要重复投递。") from error
            raise PublisherError("backend_error", "浏览器投递未完成，尚未提交草稿。", True,
                                 next_step="检查 BrowserSkill 连接后可重试。") from error
        return self._consume_browser_result(task, bundle, delivery, result)

    @staticmethod
    def _default_browser_backend() -> Any:
        from browser_skill import BrowserSkillBackend
        return BrowserSkillBackend()

    def _consume_browser_result(self, task: dict[str, Any], bundle: dict[str, Any], delivery: str, result: dict[str, Any]) -> DeliveryResult:
        if result.get("unsafe_button") in UNSAFE_BUTTONS:
            task["stage"] = "failed"; self.store.save(task)
            raise PublisherError("group_send_blocked", "检测到群发控件，已停止且没有继续操作。")
        if result.get("about_blank_seen") or result.get("session_count") != 1:
            task["stage"] = "failed"; self.store.save(task)
            raise PublisherError("browser_session_invalid", "浏览器会话不符合一任务一页面要求，未继续写入。")
        if result.get("account_alias") != bundle["delivery"]["account_alias"] or result.get("title") != bundle["article"]["selected_title"] or not result.get("fields_read_back"):
            task["stage"] = "draft_uncertain" if delivery == "draft" else "publish_uncertain"; self.store.save(task)
            raise PublisherError(task["stage"], "浏览器未能确认账号、标题或字段回读；请人工核对，不能自动重试。")
        if delivery == "draft" and result.get("stage") == "drafted":
            if not task["effects"].get("draft_submit_intent") or not task["effects"].get("draft_save_confirmed"):
                task["stage"] = "draft_uncertain"; self.store.save(task)
                raise PublisherError("draft_uncertain", "浏览器未提供完整的草稿提交与回读证据，请人工核对，不能自动重试。",
                                     write_attempted=True, next_step="按标题在草稿箱核对；确认前不要重复投递。")
            diagnostics: dict[str, Any] = {}
            timing = result.get("timing_ms")
            if isinstance(timing, dict) and all(isinstance(key, str) and isinstance(value, int)
                                                for key, value in timing.items()):
                diagnostics["stage_durations_ms"] = timing
            for key in ("human_wait_ms", "total_elapsed_ms"):
                if isinstance(result.get(key), int) and result[key] >= 0:
                    diagnostics[key] = result[key]
            cleanup = result.get("cleanup")
            if isinstance(cleanup, dict) and all(isinstance(key, str) and isinstance(value, bool) for key, value in cleanup.items()):
                diagnostics["cleanup"] = cleanup
            task.update({"stage": "drafted", "draft_id": result.get("draft_id")})
            if diagnostics:
                task["diagnostics"] = diagnostics
            self.store.save(task)
            detail = {"draft_id": result.get("draft_id")}
            if "stage_durations_ms" in diagnostics:
                detail["timing_ms"] = timing
            detail.update({key: value for key, value in diagnostics.items() if key != "stage_durations_ms"})
            return DeliveryResult("drafted", task, detail)
        if (delivery == "publish" and result.get("stage") == "published" and result.get("article_url")
                and result.get("publish_button_matches") == 1 and result.get("publish_button_label") == "发布"
                and result.get("work_title") == bundle["article"]["selected_title"] and result.get("work_timestamp")
                and task.get("stage") == "publishing" and task.get("publish_attempt") == 1):
            task.update({"stage": "published", "article_url": result["article_url"], "published_at": result["work_timestamp"]}); self.store.save(task)
            return DeliveryResult("published", task, {"article_url": result["article_url"]})
        task["stage"] = "draft_uncertain" if delivery == "draft" else "publish_uncertain"; self.store.save(task)
        raise PublisherError(task["stage"], "浏览器操作可能已写入但无法精确验证，请人工核对后再继续。")

    def status(self, task_id: str) -> dict[str, Any]: return self.store.load(task_id)
    def verify(self, task_id: str) -> dict[str, Any]:
        """仅查询；不创建浏览器会话、不改变状态，也不根据本地记录猜测线上成功。"""
        task = self.store.load(task_id)
        expected = task.get("verification", {})
        if task.get("backend") == "api" and self.api_factory:
            try:
                api = self.api_factory()
                if task["stage"] == "drafted" and task.get("draft_id"):
                    data = api.draft_get(task["draft_id"]); items = data.get("news_item") or data.get("articles") or []
                    if isinstance(items, dict): items = items.get("news_item") or [items]
                    article = items[0] if isinstance(items, list) and items else {}
                    valid = (article.get("title") == expected.get("title") and
                             hashlib.sha256(article.get("digest", "").encode()).hexdigest() == expected.get("digest_hash") and
                             semantic_html_signature(article.get("content", "")) == expected.get("html_hash"))
                    return {"status": "drafted" if valid else "verification_unavailable", "verification": "api_exact_readback" if valid else "mismatch"}
                if task["stage"] == "published" and task.get("article_id"):
                    data = api.get_article(task["article_id"]); items = data.get("news_item") or data.get("articles") or [data]
                    if isinstance(items, dict): items = items.get("news_item") or [items]
                    article = items[0] if isinstance(items, list) and items else {}
                    valid = article.get("title") == expected.get("title")
                    return {"status": "published" if valid else "verification_unavailable", "article_url": task.get("article_url"), "verification": "api_title_readback" if valid else "mismatch"}
            except PublisherError:
                return {"status": "verification_unavailable", "task_stage": task["stage"], "retry_allowed": False}
        if task.get("backend") == "browser":
            backend = self.browser_backend or self._default_browser_backend()
            verify = getattr(backend, "verify_draft", None)
            if callable(verify):
                try:
                    detail = verify(task)
                    if detail.get("verified"):
                        return {"status": "drafted", "verification": "browser_readonly_reopen", "draft_id": task.get("draft_id")}
                except PublisherError:
                    pass
        return {"status": "verification_unavailable", "task_stage": task["stage"], "retry_allowed": False}
    def abort(self, task_id: str) -> dict[str, Any]:
        task = self.store.load(task_id)
        if task["stage"] in {"publishing", "published", "draft_uncertain", "publish_uncertain"}:
            raise PublisherError("abort_blocked", "该任务已有高风险写入状态，不能标记为中止。")
        task["stage"] = "aborted"; self.store.save(task); return task
