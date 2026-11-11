import importlib.util
import os
import struct
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("build_release", ROOT / "scripts/build_release.py")
release = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release)


class ReleaseTests(unittest.TestCase):
    def test_native_target_matrix(self):
        for system, target in (("Linux", "linux"), ("Darwin", "darwin"), ("Windows", "windows")):
            for machine, suffix in (("x86_64", "x64"), ("AMD64", "x64"), ("aarch64", "arm64"), ("ARM64", "arm64")):
                self.assertEqual(release.native_target(system, machine), f"{target}-{suffix}")
        for system, machine in (("Linux", "i686"), ("FreeBSD", "x86_64")):
            with self.assertRaises(ValueError):
                release.native_target(system, machine)

    def test_native_binary_architectures(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "binary"
            for machine, target in ((62, "linux-x64"), (183, "linux-arm64")):
                data = bytearray(64)
                data[:6] = b"\x7fELF\x02\x01"
                struct.pack_into("<H", data, 18, machine)
                path.write_bytes(data)
                self.assertEqual(release.binary_architecture(path), target)
            for machine, target in ((0x8664, "windows-x64"), (0xAA64, "windows-arm64")):
                data = bytearray(70)
                data[:2] = b"MZ"
                struct.pack_into("<I", data, 60, 64)
                data[64:68] = b"PE\0\0"
                struct.pack_into("<H", data, 68, machine)
                path.write_bytes(data)
                self.assertEqual(release.binary_architecture(path), target)
            for machine, target in ((0x1000007, "darwin-x64"), (0x100000C, "darwin-arm64")):
                data = bytearray(64)
                data[:4] = b"\xcf\xfa\xed\xfe"
                struct.pack_into("<I", data, 4, machine)
                path.write_bytes(data)
                self.assertEqual(release.binary_architecture(path), target)
            path.write_bytes(b"not executable")
            with self.assertRaises(ValueError):
                release.binary_architecture(path)

    def test_archive_layout_and_no_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bundle = root / "bundle"
            (bundle / "_internal").mkdir(parents=True)
            (bundle / "arr-subtitle").write_bytes(b"executable")
            (bundle / "arr-subtitle").chmod(0o755)
            (bundle / "_internal" / "libpython.so").write_bytes(b"runtime")
            for name in ("LICENSE", "README.md", "config.example.json"):
                (bundle / name).write_text(name)
            for target in ("linux-x64", "darwin-arm64", "windows-arm64"):
                archive = release.archive_bundle(bundle, root, target)
                self.assertTrue(archive.name.startswith("arr-subtitle-" + target))
                if target.startswith("windows-"):
                    with zipfile.ZipFile(archive) as handle:
                        names = handle.namelist()
                        self.assertEqual(handle.read("arr-subtitle/_internal/libpython.so"), b"runtime")
                else:
                    with tarfile.open(archive) as handle:
                        names = handle.getnames()
                        if os.name != "nt":
                            self.assertEqual(handle.getmember("arr-subtitle/arr-subtitle").mode & 0o111, 0o111)
                for name in ("LICENSE", "README.md", "config.example.json"):
                    self.assertIn("arr-subtitle/" + name, names)
                self.assertIn("arr-subtitle/_internal/libpython.so", names)
                before = archive.read_bytes()
                with self.assertRaises(FileExistsError):
                    release.archive_bundle(bundle, root, target)
                self.assertEqual(archive.read_bytes(), before)

    def test_smoke_environment_removes_host_runtimes(self):
        from unittest.mock import patch
        with patch.dict(os.environ, {"PYTHONPATH": "source", "PYTHONHOME": "host", "DOTNET_ROOT": "host", "LD_LIBRARY_PATH": "host"}):
            environment = release.clean_environment()
        self.assertFalse(any(key.upper().startswith(("PYTHON", "DOTNET")) for key in environment))
        self.assertNotIn("LD_LIBRARY_PATH", environment)

    def test_build_without_license_does_not_invent_one(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("README.md", "config.example.json"):
                (root / name).write_text(name)
            output = root / "artifacts"
            def fake_freeze(entry, name, work, destination, ca_file):
                (destination / name).mkdir(parents=True)
            with (patch.object(release, "ROOT", root), patch.object(release, "native_target", return_value="linux-x64"),
                  patch.object(release, "freeze", side_effect=fake_freeze), patch.object(release, "verify_runtime"),
                  patch.object(release, "smoke_archive")):
                archive = release.build(output)
            with tarfile.open(archive) as handle:
                names = handle.getnames()
            self.assertIn("arr-subtitle/README.md", names)
            self.assertIn("arr-subtitle/config.example.json", names)
            self.assertNotIn("arr-subtitle/LICENSE", names)

    def test_release_workflow_has_all_native_targets_and_full_gate(self):
        workflow = (ROOT / ".github/workflows/python.yml").read_text()
        for system in ("linux", "darwin", "windows"):
            for cpu in ("x64", "arm64"):
                self.assertIn("target: " + system + "-" + cpu, workflow)
        self.assertIn("needs: [unit, native]", workflow)
        self.assertIn("python-version: '" + release.BUILD_PYTHON + "'", workflow)
        self.assertIn("--require-hashes --only-binary=:all:", workflow)
        self.assertIn("sha256sum -c SHA256SUMS", workflow)
        self.assertIn("--network none", workflow)

    def test_runtime_verification_rejects_incomplete_bundle(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = bytearray(64)
            data[:6] = b"\x7fELF\x02\x01"
            struct.pack_into("<H", data, 18, 62)
            (root / "arr-subtitle").write_bytes(data)
            for name in ("libpython3.14.so", "libssl.so.3"):
                (root / name).touch()
            with self.assertRaisesRegex(ValueError, "OpenSSL"):
                release.verify_runtime(root, "linux-x64")
            (root / "libcrypto.so.3").touch()
            self.assertEqual(release.verify_runtime(root, "linux-x64"), root / "arr-subtitle")
            (root / "libcrypto.so.3").unlink()
            (root / "libssl.so.3").unlink()
            self.assertEqual(release.verify_runtime(root, "linux-x64", static_openssl=True), root / "arr-subtitle")
            with self.assertRaisesRegex(ValueError, "architecture"):
                release.verify_runtime(root, "linux-arm64")


if __name__ == "__main__":
    unittest.main()
