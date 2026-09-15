#!/usr/bin/env python3
"""Deterministic, allow-listed package builder for this Skill."""
from __future__ import annotations

import argparse
import hashlib
import re
import stat
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PUBLIC_FILES = (
    "SKILL.md", "THIRD_PARTY_NOTICES.md", "VERSION", "agents/openai.yaml",
    "references/browser-backend.md", "references/content-bundle.md", "references/content-policy.md",
    "references/legacy-audit.md", "references/official-api.md", "references/release-gate.md",
    "references/upstream-audit.md", "scripts/audit.py", "scripts/browser_skill.py", "scripts/build.py",
    "scripts/cli.py", "scripts/cover.py", "scripts/native_runner.py", "scripts/publisher.py", "scripts/quick_validate.py",
    "docs/native-browser-adaptation.md",
    "third_party/LOCK.json",
)
TEXT_SUFFIXES = {".md", ".py", ".json", ".yaml", ".yml"}
FORBIDDEN_PARTS = {".git", ".state", "tests", "__pycache__", "dist", "build"}
SECRET_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"(?:APPSECRET|ACCESS_TOKEN|API_KEY)\s*=\s*['\"][^'\"]{8,}", re.I),
    re.compile(r"(?:cookie|authorization)\s*[:=]\s*['\"][^'\"]{8,}", re.I),
)


def source_files(target: str) -> list[Path]:
    selected = [ROOT / item for item in PUBLIC_FILES]
    if target == "internal":
        runtime = ROOT / "runtime"
        if runtime.is_dir():
            selected.extend(path for path in runtime.rglob("*") if path.is_file())
    result = []
    for path in selected:
        if not path.is_file():
            raise ValueError(f"required package file missing: {path.relative_to(ROOT)}")
        if path.is_symlink():
            raise ValueError(f"symbolic link is not packageable: {path.relative_to(ROOT)}")
        relative = path.relative_to(ROOT)
        if any(part in FORBIDDEN_PARTS for part in relative.parts) or path.suffix == ".pyc":
            continue
        result.append(path)
    return sorted(set(result), key=lambda path: path.relative_to(ROOT).as_posix())


def scan(files: list[Path]) -> None:
    for path in files:
        relative = path.relative_to(ROOT).as_posix()
        # The scanner's own literal detection patterns are not package data.
        if path.resolve() == Path(__file__).resolve():
            continue
        if relative.startswith("runtime/") and path.suffix.lower() not in {".md", ".json", ".txt", ".sha256", ".zip", ".tar", ".gz", ".exe"}:
            # Runtime binaries are only allowed in the internal package and
            # are tracked by a manifest; they are never part of public zip.
            continue
        if path.suffix.lower() in TEXT_SUFFIXES:
            value = path.read_text(encoding="utf-8", errors="ignore")
            if any(pattern.search(value) for pattern in SECRET_PATTERNS):
                raise ValueError(f"sensitive pattern in {relative}")
            if "/Users/" in value or "C:\\Users\\" in value:
                raise ValueError(f"private path in {relative}")


def build(target: str, output: Path) -> dict[str, object]:
    files = source_files(target)
    if target == "public" and any(path.relative_to(ROOT).as_posix().startswith("runtime/") for path in files):
        raise ValueError("public package must not include a browser runtime")
    scan(files)
    if target == "public" and len(files) >= 200:
        raise ValueError("public package has too many files")
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in files:
            info = zipfile.ZipInfo(path.relative_to(ROOT).as_posix(), date_time=(2020, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            archive.writestr(info, path.read_bytes(), compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
    if target == "public" and output.stat().st_size >= 10 * 1024 * 1024:
        output.unlink(missing_ok=True)
        raise ValueError("public package exceeds 10 MiB")
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    return {"target": target, "artifact": output.name, "files": len(files), "bytes": output.stat().st_size, "sha256": digest}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", choices=("public", "internal"), required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    try:
        import json
        print(json.dumps(build(args.target, Path(args.out)), ensure_ascii=False))
        return 0
    except (OSError, ValueError) as error:
        print('{"status":"failed","error_code":"package_invalid"}')
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
