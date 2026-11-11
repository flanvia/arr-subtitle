from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import tempfile
import tomllib
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CORE_FILES = ("__init__.py", "models.py", "settings.py", "serialization.py", "network.py",
              "identity.py", "blogs.py", "subtitles.py", "numbering.py", "engine.py", "storage.py")


def _dependencies():
    requirements = []
    for line in (ROOT / "requirements.txt").read_text("utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        match = re.fullmatch(r"([A-Za-z0-9_.-]+)==([A-Za-z0-9_.!+~-]+)((?:\s+--hash=sha256:[a-f0-9]{64})+)", line.strip())
        if match is None:
            raise ValueError("Plugin requirements must use exact versions and SHA256 hashes")
        requirements.append({"name": match[1], "version": match[2], "hashes": re.findall(r"sha256:[a-f0-9]{64}", match[3])})
    if not requirements:
        raise ValueError("Plugin dependency lock is empty")
    return requirements


def _git(*args):
    return subprocess.check_output(["git", *args], cwd=ROOT, text=True).strip()


def _source():
    origin = _git("remote", "get-url", "origin")
    match = re.fullmatch(r"(?:https://github\.com/|git@github\.com:)([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?", origin)
    if match is None:
        raise ValueError("Plugin source requires a GitHub origin")
    commit = _git("rev-parse", "HEAD")
    if re.fullmatch(r"[0-9a-f]{40}", commit) is None:
        raise ValueError("Invalid source revision")
    return match[1], commit


def _json(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n").encode("utf-8")


def _atomic(path, data):
    descriptor, temporary = tempfile.mkstemp(prefix=".plugin-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def build(output: Path):
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    project = tomllib.loads((ROOT / "pyproject.toml").read_text("utf-8"))["project"]
    repo, commit = _source()
    adapter = ROOT / "integrations/bazarr_plus/provider.py"
    if adapter.is_symlink() or not adapter.is_file():
        raise ValueError("Missing or unsafe provider source")
    files = {"provider.py": adapter.read_bytes()}
    for filename in CORE_FILES:
        source = ROOT / "src/arr_subtitle" / filename
        if source.is_symlink() or not source.is_file():
            raise ValueError(f"Missing or unsafe core source: {filename}")
        files[f"arr_subtitle/{filename}"] = source.read_bytes()
    digest = hashlib.sha256()
    hashes = {}
    for name, data in sorted(files.items()):
        hashes[name] = hashlib.sha256(data).hexdigest()
        digest.update(name.encode("utf-8") + b"\0" + str(len(data)).encode("ascii") + b"\0" + data + b"\0")
    source_root = output / "providers/arr_subtitle"
    try:
        source_path = source_root.relative_to(ROOT).as_posix()
        catalog_path = (output / "catalog.json").relative_to(ROOT).as_posix()
    except ValueError:
        source_path, catalog_path = "providers/arr_subtitle", "catalog.json"
    manifest = {
        "schema_version": 1, "api_version": "bazarr.provider-hub.v1", "provider_id": "arr_subtitle",
        "name": "arr-subtitle", "version": project["version"],
        "description": "한국어 애니메이션 회차 자막을 검색합니다.",
        "entry_module": "provider", "entry_class": "ArrSubtitleProvider",
        "supported_media": ["episode"], "languages": ["kor"], "secret_fields": [],
        "config_schema": {"type": "object", "additionalProperties": False, "properties": {
            "fallback_blogs": {"type": "string", "title": "추가 블로그", "description": "한 줄에 공개 블로그 URL 하나를 입력합니다.", "default": ""},
            "mappings_json": {"type": "string", "title": "작품 매핑", "description": "title, season, animeId 등을 포함한 JSON 배열입니다.", "default": "[]"},
            "anilist_enabled": {"type": "boolean", "title": "AniList 제목 식별", "description": "영문 및 로마자 제목 식별에 AniList를 사용합니다.", "default": True},
            "timeout": {"type": "integer", "title": "제한 시간(초)", "description": "검색과 다운로드 각각에 적용할 내부 제한 시간입니다.", "minimum": 1, "maximum": 20, "default": 20},
        }},
        "files": hashes, "bundle_sha256": digest.hexdigest(), "dependencies": {"requirements": _dependencies()},
        "source": {"type": "github", "repo": repo, "ref": commit, "commit": commit,
                   "path": source_path, "trusted": False,
                   "catalog_url": f"https://github.com/{repo}/blob/main/{catalog_path}"},
    }
    archive = output / f"arr-subtitle-bazarr-plus-{project['version']}.zip"
    descriptor, temporary = tempfile.mkstemp(prefix=".plugin-", suffix=".zip", dir=output)
    os.close(descriptor)
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
            for name, data in sorted({**files, "provider.json": _json(manifest)}.items()):
                info = zipfile.ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o100644 << 16
                bundle.writestr(info, data)
        with open(temporary, "r+b") as handle:
            os.fsync(handle.fileno())
        os.replace(temporary, archive)
    finally:
        Path(temporary).unlink(missing_ok=True)
    for name, data in {**files, "provider.json": _json(manifest)}.items():
        target = source_root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        _atomic(target, data)
    catalog = output / "catalog.json"
    _atomic(catalog, _json({"schema_version": 1, "name": "arr-subtitle 커뮤니티 카탈로그", "providers": [{"manifest": manifest}]}))
    return archive, catalog


def main(argv=None):
    parser = argparse.ArgumentParser(description="Build the standalone Bazarr+ provider bundle")
    parser.add_argument("--output", type=Path, default=ROOT / "artifacts/plugin")
    args = parser.parse_args(argv)
    archive, catalog = build(args.output)
    print(archive)
    print(catalog)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
