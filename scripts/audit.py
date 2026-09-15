#!/usr/bin/env python3
"""分发前最小对抗审查：禁止危险发布面与常见敏感信息进入 Skill。"""
from __future__ import annotations
import re, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKIP = {"tests", ".state", "__pycache__", "third_party"}
PROHIBITED = {
    "群发接口": re.compile(r"masssend|message/mass", re.I),
    "自动删除": re.compile(r"delete_material|draft/delete|freepublish/delete", re.I),
    "二维码外传": re.compile(r"TELEGRAM_BOT_TOKEN|TELEGRAM_CHAT_ID|api\.telegram\.org", re.I),
    "明文密钥": re.compile(r"(?:APPSECRET|ACCESS_TOKEN|API_KEY)\s*=\s*['\"][^'\"]{8,}", re.I),
    "旧浏览器运行器": re.compile(r"BAOYU_WECHAT_RUNNER|browser_runner", re.I),
}

def files():
    for path in ROOT.rglob("*"):
        if path.is_file() and not any(part in SKIP for part in path.parts): yield path

def main() -> int:
    findings = []
    for path in files():
        if path.name == "audit.py": continue
        if path.suffix.lower() not in {".py", ".md", ".json", ".yaml", ".yml", ".ts", ".js"}: continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for name, pattern in PROHIBITED.items():
            if pattern.search(text): findings.append(f"{name}: {path.relative_to(ROOT)}")
    if findings:
        print("AUDIT_FAILED\n" + "\n".join(findings)); return 2
    print("AUDIT_OK")
    return 0

if __name__ == "__main__": raise SystemExit(main())
