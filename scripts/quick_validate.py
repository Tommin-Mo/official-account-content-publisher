#!/usr/bin/env python3
"""Fast, side-effect-free release gate for the public Skill tree."""
from __future__ import annotations

import json
import py_compile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REQUIRED = {
    "SKILL.md", "VERSION", "agents/openai.yaml", "scripts/cli.py", "scripts/publisher.py",
    "scripts/browser_skill.py", "scripts/cover.py", "scripts/audit.py",
    "scripts/build.py", "scripts/quick_validate.py",
    "references/content-bundle.md", "references/content-policy.md",
    "references/browser-backend.md", "references/official-api.md",
    "references/release-gate.md",
    "third_party/LOCK.json", "THIRD_PARTY_NOTICES.md",
}


def main() -> int:
    missing = sorted(path for path in REQUIRED if not (ROOT / path).is_file())
    if missing:
        print(json.dumps({"status": "failed", "error_code": "package_incomplete", "missing": missing}, ensure_ascii=False))
        return 2
    try:
        if (ROOT / "VERSION").read_text(encoding="utf-8").strip() != "0.2.0":
            raise ValueError("unexpected candidate version")
        lock = json.loads((ROOT / "third_party" / "LOCK.json").read_text(encoding="utf-8"))
        if lock.get("browser-skill", {}).get("cli_version") != "0.2.0":
            raise ValueError("BrowserSkill provenance is missing")
        for path in (ROOT / "scripts").glob("*.py"):
            py_compile.compile(str(path), doraise=True)
    except (OSError, ValueError, json.JSONDecodeError, py_compile.PyCompileError) as error:
        print(json.dumps({"status": "failed", "error_code": "package_invalid", "message": str(error)}, ensure_ascii=False))
        return 2
    print(json.dumps({"status": "ready", "side_effects": False, "checked_files": len(REQUIRED)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
