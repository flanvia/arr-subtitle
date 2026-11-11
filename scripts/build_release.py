from __future__ import annotations

import argparse
import os
import platform
import shutil
import ssl
import struct
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUILD_PYTHON = "3.14.10"


def native_target(system=None, machine=None):
    system = (system or platform.system()).lower()
    machine = (machine or platform.machine()).lower()
    systems = {"linux": "linux", "darwin": "darwin", "windows": "windows"}
    machines = {"x86_64": "x64", "amd64": "x64", "aarch64": "arm64", "arm64": "arm64"}
    if system not in systems or machine not in machines:
        raise ValueError(f"Unsupported native target: {system}/{machine}")
    return f"{systems[system]}-{machines[machine]}"


def binary_architecture(path):
    with Path(path).open("rb") as stream:
        header = stream.read(64)
        if header.startswith(b"\x7fELF"):
            if header[4] != 2 or header[5] not in (1, 2):
                raise ValueError("Expected a 64-bit ELF executable")
            machine = struct.unpack_from("<H" if header[5] == 1 else ">H", header, 18)[0]
            return {62: "linux-x64", 183: "linux-arm64"}[machine]
        if header.startswith(b"MZ"):
            stream.seek(struct.unpack_from("<I", header, 60)[0])
            signature = stream.read(6)
            if signature[:4] != b"PE\0\0":
                raise ValueError("Invalid PE signature")
            return {0x8664: "windows-x64", 0xAA64: "windows-arm64"}[struct.unpack_from("<H", signature, 4)[0]]
        if header[:4] in (b"\xcf\xfa\xed\xfe", b"\xfe\xed\xfa\xcf"):
            endian = "<" if header[:4] == b"\xcf\xfa\xed\xfe" else ">"
            return {0x1000007: "darwin-x64", 0x100000C: "darwin-arm64"}[struct.unpack_from(endian + "I", header, 4)[0]]
    raise ValueError("Expected a native thin executable")


def verify_runtime(directory, target, name="arr-subtitle", *, static_openssl=False):
    executable = directory / (name + (".exe" if target.startswith("windows-") else ""))
    if binary_architecture(executable) != target:
        raise ValueError("Executable architecture does not match native runner")
    files = [p.name.lower() for p in directory.rglob("*") if p.is_file()]
    if not any("python3" in name or "libpython" in name or name == "python" for name in files):
        raise ValueError("Python runtime is missing from bundle")
    if not static_openssl and (not any("libssl" in name for name in files) or not any("libcrypto" in name for name in files)):
        raise ValueError("OpenSSL libraries are missing from bundle")
    return executable


def archive_bundle(directory, output, target):
    suffix = ".zip" if target.startswith("windows-") else ".tar.gz"
    archive = output / ("arr-subtitle-" + target + suffix)
    if archive.exists():
        raise FileExistsError(archive)
    with tempfile.TemporaryDirectory(prefix="archive-", dir=output) as temporary:
        staged = Path(temporary) / archive.name
        if suffix == ".zip":
            with zipfile.ZipFile(staged, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
                for path in sorted(directory.rglob("*")):
                    if path.is_file():
                        bundle.write(path, Path("arr-subtitle") / path.relative_to(directory))
        else:
            with tarfile.open(staged, "w:gz") as bundle:
                bundle.add(directory, arcname="arr-subtitle")
        with staged.open("rb+") as stream:
            os.fsync(stream.fileno())
        os.link(staged, archive)
    return archive


def clean_environment():
    return {key: value for key, value in os.environ.items()
            if not key.upper().startswith(("PYTHON", "DOTNET"))
            and key not in ("LD_LIBRARY_PATH", "DYLD_LIBRARY_PATH")}


def freeze(entry, name, work, output, ca_file=None):
    command = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onedir",
               "--noupx", "--name", name, "--paths", str(ROOT / "src"),
               "--collect-submodules", "arr_subtitle", "--copy-metadata", "beautifulsoup4",
               "--distpath", str(output), "--workpath", str(work / name),
               "--specpath", str(work)]
    if ca_file:
        hook = work / "ca_hook.py"
        hook.write_text("import os, sys\nfrom pathlib import Path\n"
                        "os.environ.setdefault('SSL_CERT_FILE', str(Path(sys._MEIPASS) / 'ca.pem'))\n", encoding="utf-8")
        command.extend(["--add-data", f"{ca_file}{os.pathsep}.", "--runtime-hook", str(hook)])
    subprocess.run([*command, str(entry)], cwd=work, check=True)


def smoke_archive(archive, smoke, target, work, *, static_openssl=False):
    unpacked = work / "unpacked"
    unpacked.mkdir()
    shutil.unpack_archive(str(archive), str(unpacked))
    executable = verify_runtime(unpacked / "arr-subtitle", target, static_openssl=static_openssl)
    environment = clean_environment()
    environment.update({"XDG_CONFIG_HOME": str(work / "config"), "XDG_DATA_HOME": str(work / "data"),
                        "APPDATA": str(work / "config"), "LOCALAPPDATA": str(work / "data")})
    for argument in ("--help", "--version"):
        subprocess.run([str(executable), argument], cwd=unpacked, env=environment, check=True)
    empty = work / "empty-media"
    empty.mkdir()
    result = subprocess.run([str(executable), str(empty)], cwd=unpacked, env=environment, timeout=30)
    if result.returncode == 0:
        raise ValueError("Empty folder must not report a successful download")
    subprocess.run([str(smoke)], cwd=unpacked, env=environment, check=True, timeout=120)


def build(output):
    target = native_target()
    # Standalone Python builds can embed both OpenSSL modules in libpython.
    static_openssl = all(name in sys.builtin_module_names for name in ("_ssl", "_hashlib"))
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    for name in ("cli", "smoke"):
        if (output / name).exists():
            raise FileExistsError(output / name)
    for name in ("README.md", "config.example.json"):
        if not (ROOT / name).is_file():
            raise FileNotFoundError(ROOT / name)
    with tempfile.TemporaryDirectory(prefix="release-", dir=output) as temporary:
        work = Path(temporary)
        entry = work / "entry.py"
        entry.write_text("from arr_subtitle.cli import main\nraise SystemExit(main())\n", encoding="utf-8")
        ca_file = None
        if target.startswith("darwin-"):
            source = ssl.get_default_verify_paths().cafile
            if not source:
                raise ValueError("Build Python has no CA certificate file")
            ca_file = work / "ca.pem"
            shutil.copyfile(source, ca_file)
        freeze(entry, "arr-subtitle", work, output / "cli", ca_file)
        freeze(ROOT / "scripts" / "smoke.py", "arr-subtitle-smoke", work, output / "smoke", ca_file)
        directory = output / "cli" / "arr-subtitle"
        verify_runtime(directory, target, static_openssl=static_openssl)
        smoke = verify_runtime(output / "smoke" / "arr-subtitle-smoke", target, "arr-subtitle-smoke", static_openssl=static_openssl)
        for name in ("README.md", "config.example.json"):
            shutil.copy2(ROOT / name, directory / name)
        if (ROOT / "LICENSE").is_file():
            shutil.copy2(ROOT / "LICENSE", directory / "LICENSE")
        archive = archive_bundle(directory, output, target)
        smoke_archive(archive, smoke, target, work, static_openssl=static_openssl)
    return archive


def main(argv=None):
    parser = argparse.ArgumentParser(description="Build and smoke-test the native standalone CLI")
    parser.add_argument("--output", type=Path, default=ROOT / "artifacts" / "python-release")
    arguments = parser.parse_args(argv)
    print(build(arguments.output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
