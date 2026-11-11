from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


CATALOG_REVISION = "57b93e69c760feab1ed7ae0126e4a84916cbf57e"
HOST_REVISION = "76528c2fb9173442c827405b351f6baaecb37578"


def check(catalog_root, bundle_root, runner):
    spec = importlib.util.spec_from_file_location("upstream_catalog_sdk", catalog_root / "sdk/cli.py")
    sdk = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sdk)
    manifest = json.loads((bundle_root / "provider.json").read_text("utf-8"))
    # The catalog SDK restricts source metadata to its own official repository.
    with tempfile.TemporaryDirectory(prefix="arr-sdk-") as temporary:
        root = Path(temporary)
        copied = root / "providers/arr_subtitle"
        shutil.copytree(bundle_root, copied, ignore=shutil.ignore_patterns("__pycache__"))
        sdk_manifest = dict(manifest)
        sdk_manifest["source"] = {**manifest["source"], "repo": "LavX/bazarr-provider-catalog", "ref": "main", "path": "providers/arr_subtitle"}
        (copied / "provider.json").write_text(json.dumps(sdk_manifest), encoding="utf-8")
        sdk.validate_manifest(root, copied, set())
    environment = {key: value for key, value in os.environ.items() if not key.startswith("PYTHON")}
    environment.update(BAZARR_PROVIDER_HUB_BUNDLE=str(bundle_root.resolve()), BAZARR_PROVIDER_HUB_MANIFEST=json.dumps(manifest))
    requests = [
        {"abi": "bazarr.provider-worker.v1", "id": "health", "op": "health", "payload": {}},
        {"abi": "bazarr.provider-worker.v1", "id": "unsupported", "op": "search", "payload": {"video": {"kind": "movie"}, "languages": [{"alpha3": "kor"}], "config": {}}},
        {"abi": "bazarr.provider-worker.v1", "id": "shutdown", "op": "shutdown", "payload": {}},
    ]
    result = subprocess.run([sys.executable, "-I", "-B", str(runner.resolve())], input="".join(json.dumps(r) + "\n" for r in requests), text=True, capture_output=True, env=environment, cwd=bundle_root, timeout=30, check=True)
    replies = [json.loads(line) for line in result.stdout.splitlines()]
    if len(replies) != 3 or not all(r.get("ok") for r in replies) or replies[1]["payload"] != {"candidates": []}:
        raise RuntimeError(f"Provider worker contract failed: {result.stdout} {result.stderr}")
    print("Pinned SDK validation and isolated upstream worker contract passed")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("catalog_root", type=Path)
    parser.add_argument("bundle_root", type=Path)
    parser.add_argument("runner", type=Path)
    arguments = parser.parse_args()
    check(arguments.catalog_root, arguments.bundle_root, arguments.runner)


if __name__ == "__main__":
    main()
