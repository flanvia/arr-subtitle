import hashlib
import inspect
import json
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

from scripts.build_plugin import CORE_FILES, ROOT, build
from test_provider import SAMI, fixture_transport


class BundleTests(unittest.TestCase):
    def test_archive_has_only_declared_python_core_and_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            archive, catalog = build(Path(directory))
            with zipfile.ZipFile(archive) as bundle:
                manifest = json.loads(bundle.read("provider.json"))
                self.assertEqual(set(bundle.namelist()), set(manifest["files"]) | {"provider.json"})
                self.assertEqual(set(manifest["files"]), {"provider.py"} | {f"arr_subtitle/{name}" for name in CORE_FILES})
                digest = hashlib.sha256()
                for name in sorted(manifest["files"]):
                    data = bundle.read(name)
                    self.assertEqual(hashlib.sha256(data).hexdigest(), manifest["files"][name])
                    digest.update(name.encode() + b"\0" + str(len(data)).encode() + b"\0" + data + b"\0")
                self.assertEqual(digest.hexdigest(), manifest["bundle_sha256"])
                self.assertNotIn("arr_subtitle/cli.py", bundle.namelist())
                self.assertNotIn("arr_subtitle/batch.py", bundle.namelist())
            document = json.loads(catalog.read_text("utf-8"))
            self.assertEqual(document["providers"], [{"manifest": manifest}])
            self.assertFalse(manifest["source"]["trusted"])
            revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
            self.assertEqual(manifest["source"]["commit"], revision)
            self.assertEqual(manifest["source"]["ref"], revision)
            self.assertEqual(manifest["supported_media"], ["episode"])
            self.assertEqual(manifest["languages"], ["kor"])
            with zipfile.ZipFile(archive) as bundle:
                for name in manifest["files"]:
                    self.assertEqual((Path(directory) / "providers/arr_subtitle" / name).read_bytes(), bundle.read(name))

    def test_dependency_hashes_match_project_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            archive, _ = build(Path(directory))
            with zipfile.ZipFile(archive) as bundle:
                manifest = json.loads(bundle.read("provider.json"))
            lock = (ROOT / "requirements.txt").read_text("utf-8")
            for dependency in manifest["dependencies"]["requirements"]:
                self.assertIn(f"{dependency['name']}=={dependency['version']}", lock)
                for value in dependency["hashes"]:
                    self.assertIn(value, lock)
            self.assertEqual({item["name"] for item in manifest["dependencies"]["requirements"]},
                             {"beautifulsoup4", "soupsieve", "typing_extensions"})

    def test_build_is_reproducible(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive, _ = build(root / "one")
            other, _ = build(root / "two")
            self.assertEqual(archive.read_bytes(), other.read_bytes())

    def test_bundle_import_search_and_download_without_source_access(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive, _ = build(root / "output")
            install = root / "installed"
            with zipfile.ZipFile(archive) as bundle:
                bundle.extractall(install)
            code = """
import base64
import io
import json
import sys
import zipfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, sys.argv[1])
source_root = Path(sys.argv[2]).resolve()
def deny_source(event, args):
    if event == 'open' and isinstance(args[0], (str, bytes)):
        path = Path(args[0]).resolve()
        if path == source_root or source_root in path.parents:
            raise RuntimeError('Source tree access is forbidden')
sys.addaudithook(deny_source)
import provider
import arr_subtitle
from arr_subtitle.network import Session, Response
assert Path(arr_subtitle.__file__).resolve().is_relative_to(Path(sys.argv[1]).resolve())
assert 'arr_subtitle.cli' not in sys.modules
assert 'arr_subtitle.batch' not in sys.modules
""" + f"\nSAMI = {SAMI!r}\n" + inspect.getsource(fixture_transport) + """
video = {'kind': 'episode', 'series': 'Series', 'season': 1, 'episode': 1}
language = {'alpha3': 'kor', 'hi': False, 'forced': False}
config = {'fallback_blogs': 'https://author.blogspot.com/', 'anilist_enabled': False}
transport, calls = fixture_transport(archive=True)
with patch.object(provider, 'Session', side_effect=lambda: Session(transport, interval=0)):
    candidates = provider.ArrSubtitleProvider().search(video, [language], config)
    assert len(candidates) == 1, candidates
    result = provider.ArrSubtitleProvider().download(candidates[0]['provider_payload'], language, config)
assert result['format'] == 'sami'
assert base64.b64decode(result['content_b64']) == SAMI
assert 'bazarr' not in sys.modules
"""
            environment = {key: value for key, value in os.environ.items() if key not in {"PYTHONPATH", "PYTHONHOME"}}
            result = subprocess.run([sys.executable, "-I", "-B", "-c", code, str(install), str(ROOT / "src")],
                                    cwd=root, env=environment, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "")
            self.assertEqual(result.stderr, "")

    def test_cli_output_directory_option(self):
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run([sys.executable, str(ROOT / "scripts/build_plugin.py"), "--output", directory],
                                    capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((Path(directory) / "catalog.json").is_file())
            self.assertEqual(len(list(Path(directory).glob("*.zip"))), 1)
