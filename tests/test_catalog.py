import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


class CatalogTests(unittest.TestCase):
    def test_catalog_pins_commit_that_contains_every_core_file(self):
        spec = importlib.util.spec_from_file_location("catalog_publish", ROOT / "scripts/publish_catalog.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        commits, trees, blobs = {}, {}, {}
        def api(repo, endpoint, data=None):
            if endpoint == "git/ref/heads/provider-catalog":
                return {"object": {"sha": "a" * 40}}
            if endpoint == "git/blobs":
                number = str(len(blobs) + 1)
                blobs[number] = data
                return {"sha": number}
            if endpoint == "git/trees":
                number = str(len(trees) + 1)
                trees[number] = data["tree"]
                return {"sha": number}
            if endpoint == "git/commits":
                number = str(len(commits) + 1) * 40
                commits[number] = data
                return {"sha": number}
            raise AssertionError(endpoint)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            provider = root / "providers/arr_subtitle"
            provider.mkdir(parents=True)
            (provider / "provider.py").write_text("class Provider: pass\n")
            (provider / "provider.json").write_text(json.dumps({"files": {"provider.py": "hash"}}))
            with patch.object(module, "api", side_effect=api), patch.object(module.subprocess, "run") as update:
                module.publish(root, "owner/project", "v0.2.0")
        first, second = list(commits.values())
        self.assertEqual(second["parents"], ["1" * 40])
        self.assertEqual(first["author"]["email"], "146043537+heavycaffeiner@users.noreply.github.com")
        tree = trees[second["tree"]]
        catalog_blob = next(item["sha"] for item in tree if item["path"] == "catalog.json")
        import base64
        catalog = json.loads(base64.b64decode(blobs[catalog_blob]["content"]))
        source = catalog["providers"][0]["manifest"]["source"]
        self.assertEqual(source["commit"], "1" * 40)
        first_tree = trees[first["tree"]]
        self.assertTrue(any(item["path"] == source["path"] + "/provider.py" for item in first_tree))
        request = json.loads(update.call_args.kwargs["input"])
        self.assertFalse(request["force"])
