from __future__ import annotations

import argparse
import base64
import json
import subprocess
from pathlib import Path


AUTHOR = {"name": "heavycaffeiner", "email": "146043537+heavycaffeiner@users.noreply.github.com"}


def api(repo, endpoint, data=None):
    command = ["gh", "api", f"repos/{repo}/{endpoint}"]
    if data is not None:
        command += ["--method", "POST", "--input", "-"]
    result = subprocess.run(command, input=json.dumps(data) if data is not None else None,
                            capture_output=True, text=True, check=True)
    return json.loads(result.stdout)


def publish(root, repo, version):
    root = Path(root)
    manifest_path = root / "providers/arr_subtitle/provider.json"
    manifest = json.loads(manifest_path.read_text("utf-8"))
    files = {"providers/arr_subtitle/" + name: (root / "providers/arr_subtitle" / name).read_bytes() for name in manifest["files"]}
    branch = "provider-catalog"
    try:
        parent = api(repo, f"git/ref/heads/{branch}")["object"]["sha"]
    except subprocess.CalledProcessError as error:
        if "404" not in error.stderr:
            raise
        parent = None
    def commit(contents, message, parents):
        tree = []
        for name, data in sorted(contents.items()):
            blob = api(repo, "git/blobs", {"content": base64.b64encode(data).decode("ascii"), "encoding": "base64"})
            tree.append({"path": name, "mode": "100644", "type": "blob", "sha": blob["sha"]})
        tree_id = api(repo, "git/trees", {"tree": tree})["sha"]
        return api(repo, "git/commits", {"message": message, "tree": tree_id, "parents": parents, "author": AUTHOR, "committer": AUTHOR})["sha"]
    # Publish the Python tree first so the manifest can pin an immutable commit.
    source_commit = commit(files, f"build(plugin): publish {version} core bundle", [parent] if parent else [])
    manifest["source"] = {"type": "github", "repo": repo, "ref": source_commit, "commit": source_commit,
                          "path": "providers/arr_subtitle", "trusted": False,
                          "catalog_url": f"https://github.com/{repo}/blob/{branch}/catalog.json"}
    files["providers/arr_subtitle/provider.json"] = (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode()
    files["catalog.json"] = (json.dumps({"schema_version": 1, "name": "arr-subtitle 커뮤니티 카탈로그", "providers": [{"manifest": manifest}]}, ensure_ascii=False, indent=2) + "\n").encode()
    catalog_commit = commit(files, f"build(plugin): pin {version} catalog to source commit", [source_commit])
    if parent:
        subprocess.run(["gh", "api", f"repos/{repo}/git/refs/heads/{branch}", "--method", "PATCH", "--input", "-"],
                       input=json.dumps({"sha": catalog_commit, "force": False}), text=True, check=True, capture_output=True)
    else:
        api(repo, "git/refs", {"ref": f"refs/heads/{branch}", "sha": catalog_commit})
    print(f"https://raw.githubusercontent.com/{repo}/{branch}/catalog.json")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--version", required=True)
    args = parser.parse_args()
    publish(args.root, args.repo, args.version)


if __name__ == "__main__":
    main()
