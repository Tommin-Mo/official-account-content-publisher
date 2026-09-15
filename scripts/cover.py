#!/usr/bin/env python3
"""封面提示词、Provider 确认与 GPT 生图适配。"""
from __future__ import annotations
import base64
import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any
import urllib.error
import urllib.request
from publisher import PublisherError, image_dimensions, validate_local_image

PROVIDERS = {"gpt", "workbuddy", "wechat-cover"}
GPT_IMAGE_URL = "https://api.openai.com/v1/images/generations"
GPT_DEFAULT_MODEL = "gpt-image-2"
# Both dimensions are divisible by 16 and the ratio is within the WeChat
# cover validator's 2.35:1 tolerance.
GPT_WECHAT_COVER_SIZE = "1888x800"

def build_prompt(bundle: dict[str, Any]) -> str:
    article, brief = bundle["article"], bundle["brief"]
    insights = bundle.get("insights", [])
    core = insights[0].get("implication", "") if insights else article.get("summary", "")
    core = str(core).strip().rstrip("。！？!? ")
    return (f"微信公众号横版封面，2.35:1。主题：{brief['theme']}。核心看点：{core}。"
            f"面向：{brief['audience']}。风格：{brief.get('voice_profile') or '清晰、可信、克制'}。"
            "保留中央安全区供后续人工或已审计的本地排版工具添加中文标题；不要生成文字、水印、品牌标志或二维码。")


def provider_confirmation(provider: str, model: str = "") -> dict[str, Any]:
    if provider not in PROVIDERS:
        raise PublisherError("cover_provider_required", "封面 Provider 必须是 GPT、WorkBuddy 或已安装的 wechat-cover。")
    return {
        "status": "needs_user_action",
        "error_code": "cover_provider_confirmation_required",
        "provider": provider,
        "model": model or (GPT_DEFAULT_MODEL if provider == "gpt" else ""),
        "user_message": "首次使用需要确认封面生成 Provider；确认前不会调用外部服务或保存偏好。",
        "next_step": "确认 Provider、模型和可能产生的生图费用后，再继续。",
        "retry_allowed": True,
        "write_attempted": False,
    }


def save_provider_reference(state_dir: str, provider: str, model: str = "", *, confirmed: bool = False) -> dict[str, str]:
    """只保存 Provider 与模型，不接收、不保存或不显示 API 密钥。"""
    if provider not in PROVIDERS: raise PublisherError("cover_provider_required", "封面 Provider 必须是 GPT、WorkBuddy 或已安装的 wechat-cover。")
    if not confirmed:
        raise PublisherError("cover_provider_confirmation_required", "首次使用需要确认封面 Provider；尚未保存偏好或调用外部服务。", True,
                             next_step="确认 Provider、模型和可能产生的生图费用后，再继续。")
    root = Path(state_dir); root.mkdir(parents=True, exist_ok=True)
    value = {"provider": provider, "model": model or (GPT_DEFAULT_MODEL if provider == "gpt" else ""), "aspect_ratio": "2.35:1"}
    (root / "cover-provider.json").write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    return value


def cover_plan(bundle: dict[str, Any], provider: str, model: str = "") -> dict[str, Any]:
    if provider not in PROVIDERS: raise PublisherError("cover_provider_required", "请先选择一个已授权的封面 Provider。")
    return {"provider": provider, "model": model, "prompt": build_prompt(bundle), "aspect_ratio": "2.35:1",
            "minimum_size": [900, 383], "square_preview": True,
            "title_overlay": {"text": bundle["article"]["selected_title"], "safe_zone": "center", "status": "not_applied"}}


def _gpt_transport(payload: dict[str, Any], api_key: str) -> dict[str, Any]:
    request = urllib.request.Request(
        GPT_IMAGE_URL,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=150) as response:
            return json.loads(response.read())
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
        raise PublisherError("cover_generation_failed", "封面生成服务暂时不可用；尚未继续投递。", True,
                             next_step="检查 Provider 凭据、网络或服务状态后重新生成封面。") from error


def generate_cover(bundle: dict[str, Any], provider: str, model: str, destination: str, *, confirmed: bool,
                   api_key: str = "", transport: Any = None) -> dict[str, Any]:
    """Generate one final-ratio cover after explicit user confirmation.

    The callable ``transport`` exists solely for deterministic tests.  Runtime
    credentials are read only at call time and are never persisted.
    """
    if not confirmed:
        raise PublisherError("cover_generation_confirmation_required", "封面生成会调用外部 Provider；尚未执行。", True,
                             next_step="确认本次生图后再继续。")
    if provider != "gpt":
        raise PublisherError("cover_provider_unavailable", "当前未配置该 Provider 的可审计生图适配器，尚未调用外部服务。", True,
                             next_step="选择 GPT，或先安装并审核对应 Provider 的本地适配器。")
    secret = api_key or os.environ.get("OPENAI_API_KEY", "")
    if not secret:
        raise PublisherError("cover_credential_required", "未找到 GPT 生图凭据；尚未调用外部服务。", True,
                             next_step="在安全凭据管理中配置 Provider 凭据后重试。")
    payload = {
        "model": model or GPT_DEFAULT_MODEL,
        "prompt": build_prompt(bundle),
        "size": GPT_WECHAT_COVER_SIZE,
        "quality": "medium",
        "output_format": "jpeg",
        "output_compression": 90,
        "n": 1,
    }
    response = (transport or _gpt_transport)(payload, secret)
    try:
        encoded = response["data"][0]["b64_json"]
        raw = base64.b64decode(encoded, validate=True)
    except (KeyError, IndexError, TypeError, ValueError) as error:
        raise PublisherError("cover_generation_failed", "封面 Provider 返回了无效图片数据；尚未继续投递。", True,
                             next_step="检查 Provider 返回结果后重新生成封面。") from error
    target = Path(destination)
    target.parent.mkdir(parents=True, exist_ok=True)
    # Never overwrite a user-selected or already-finalized cover until the
    # new provider output has passed all local validation.
    with tempfile.NamedTemporaryFile("wb", suffix=target.suffix or ".jpg", dir=target.parent, delete=False) as temporary:
        temporary.write(raw)
        temporary_path = Path(temporary.name)
    try:
        result = validate_cover(str(temporary_path), provider)
    except PublisherError:
        temporary_path.unlink(missing_ok=True)
        raise
    os.replace(temporary_path, target)
    # Return the stable requested path, not the now-replaced temporary name.
    result = validate_cover(str(target), provider)
    return {"status": "cover_generated", **result, "model": payload["model"], "size": payload["size"]}

def validate_cover(path: str, provider: str, expected_hash: str | None = None) -> dict[str, Any]:
    if provider not in PROVIDERS: raise PublisherError("cover_provider_required", "请选择已授权的封面 Provider。")
    try: file = validate_local_image(path)
    except PublisherError as error: raise PublisherError("cover_generation_failed", str(error)) from error
    width, height = image_dimensions(path)
    if width < 900 or height < 383 or abs(width / height - 2.35) > 0.03:
        raise PublisherError("cover_generation_failed", "封面必须至少为 900×383，并符合 2.35:1 比例。")
    digest = hashlib.sha256(file.read_bytes()).hexdigest()
    if expected_hash and expected_hash != digest: raise PublisherError("cover_generation_failed", "封面文件在内容冻结后发生了变化。")
    return {"cover_path": str(file), "cover_hash": digest, "aspect_ratio": "2.35:1", "width": width, "height": height, "provider": provider}
