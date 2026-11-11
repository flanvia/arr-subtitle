from __future__ import annotations

import contextlib
import hashlib
import os
import re
import stat
import sys
import uuid
from pathlib import Path

from .models import Budget, CollectorError, Settings
from .network import json_data, redact
from .serialization import dumps, plain
from .settings import parse_settings


SUBTITLE_LIMIT = 10 * 1024 * 1024
SETTINGS_LIMIT = 1024 * 1024
EXTENSIONS = (".smi", ".srt", ".ass", ".ssa")


def is_link(path, info=None):
    info = info if info is not None else os.lstat(path)
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400) or bool(getattr(os.path, "isjunction", lambda _: False)(path))


def checked_path(path):
    value = os.fspath(path)
    if not value or "\x00" in value:
        raise ValueError("Invalid filesystem path")
    result = Path(os.path.abspath(value))
    for part in [*reversed(result.parents), result]:
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        if is_link(part, info):
            raise OSError("Symbolic links and junctions are not allowed")
    return result


@contextlib.contextmanager
def _parent(path, create=False):
    path = checked_path(path)
    if os.name == "posix":
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        fd = os.open(path.anchor, flags)
        try:
            for component in path.parent.parts[1:]:
                if create:
                    try:
                        os.mkdir(component, mode=0o700, dir_fd=fd)
                    except FileExistsError:
                        pass
                child = os.open(component, flags, dir_fd=fd)
                os.close(fd)
                fd = child
            yield path, fd
        finally:
            os.close(fd)
    else:
        if create:
            path.parent.mkdir(parents=True, exist_ok=True)
        checked_path(path)
        yield path, None


def _read(path, limit):
    with _parent(path) as (path, directory):
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        fd = os.open(path.name if directory is not None else path, flags, dir_fd=directory)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or is_link(path, info):
                raise OSError("Expected a regular file")
            if info.st_size > limit:
                raise ValueError("File exceeds size limit")
            data = stream.read(limit + 1)
            if len(data) > limit:
                raise ValueError("File exceeds size limit")
            return data


def atomic_write(path, data, *, overwrite=False, budget=None):
    if not isinstance(data, bytes):
        raise TypeError("File content must be bytes")
    if budget:
        budget.check()
    with _parent(path, create=True) as (path, directory):
        name = ".arr-subtitle-" + uuid.uuid4().hex + ".tmp"
        temporary = name if directory is not None else path.parent / name
        destination = path.name if directory is not None else path
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600, dir_fd=directory)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            if budget:
                budget.check()
            checked_path(path)
            if overwrite:
                os.replace(temporary, destination, src_dir_fd=directory, dst_dir_fd=directory)
            elif directory is not None:
                os.link(temporary, destination, src_dir_fd=directory, dst_dir_fd=directory, follow_symlinks=False)
            else:
                os.rename(temporary, destination)
            if directory is not None:
                os.fsync(directory)
        finally:
            try:
                os.unlink(temporary, dir_fd=directory)
            except FileNotFoundError:
                pass


def _field(value, name, default=None):
    return value.get(name, default) if isinstance(value, dict) else getattr(value, name, default)


def stem(anime, target, output):
    media = target.get("mediaPath")
    if media is not None:
        path = checked_path(media)
        if not path.parent.is_dir() or path.exists() and not path.is_file():
            raise ValueError("Media parent directory does not exist or media is not a file")
        return path.with_suffix("")
    if not output:
        raise ValueError("Output directory is required")
    anime_id = _field(anime, "anime_id", _field(anime, "animeId"))
    if type(anime_id) is not int or anime_id < 1:
        raise ValueError("Invalid anime ID for output filename")
    title = _field(anime, "title", "")
    safe = re.sub(r'[<>:"/\\|?*\x00-\x1f\x7f]', "_", title).strip(" .")[:100].rstrip(" .")
    if not safe:
        safe = "anime"
    if re.fullmatch(r"(?i)(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?", safe):
        safe = "_" + safe
    from .subtitles import episode
    number = episode(target["episode"])
    directory = checked_path(output)
    season = _field(anime, "season")
    if season is not None:
        if type(season) is not int or season < 1:
            raise ValueError("Invalid season")
        directory /= f"Season {season}"
    return directory / f"{safe} [anissia-{anime_id}] - {number.zfill(2)}"


def _result(target, status, path, data, **extra):
    result = {"episode": target["episode"], "status": status, "path": str(path), "sha256": hashlib.sha256(data).hexdigest(), **extra}
    if target.get("mediaPath") is not None:
        result["mediaPath"] = str(checked_path(target["mediaPath"]))
    return result


def existing(anime, target, output):
    from .subtitles import validate
    base = stem(anime, target, output)
    found = None
    for language in ("ko", "kor"):
        for extension in EXTENSIONS:
            path = Path(str(base) + "." + language + extension)
            try:
                data = _read(path, SUBTITLE_LIMIT)
            except FileNotFoundError:
                continue
            try:
                validate(data, extension)
            except (ValueError, CollectorError) as error:
                raise CollectorError("WriteFailed", "Existing subtitle is invalid and was preserved") from error
            if found is None:
                found = _result(target, "AlreadyPresent", path, data)
    return found


def save(data, extension, anime, target, output, page, budget, member=None):
    from .subtitles import validate
    if extension not in EXTENSIONS:
        raise ValueError("Unsupported subtitle extension")
    if not isinstance(data, bytes) or len(data) > SUBTITLE_LIMIT:
        raise ValueError("Subtitle exceeds size limit")
    budget.check()
    validate(data, extension)
    base = stem(anime, target, output)
    with _settings_lock(str(base) + ".sidecar", budget):
        previous = existing(anime, target, output)
        if previous is not None:
            return previous
        path = Path(str(base) + ".ko" + extension)
        try:
            atomic_write(path, data, budget=budget)
        except FileExistsError:
            previous = existing(anime, target, output)
            if previous is not None:
                return previous
            raise
    return _result(target, "Downloaded", path, data, page=redact(page), archiveMember=member)


def config_path():
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "arr-subtitle" / "config.json"


def data_path():
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return base / "arr-subtitle"


def load_settings(path):
    try:
        data = _read(path, SETTINGS_LIMIT)
    except FileNotFoundError:
        return Settings(), "missing"
    try:
        settings = parse_settings(json_data(data))
    except (RecursionError, UnicodeError) as error:
        raise ValueError("Invalid settings JSON") from error
    return settings, hashlib.sha256(data).hexdigest().upper()


@contextlib.contextmanager
def _settings_lock(path, budget):
    with _parent(str(path) + ".lock", create=True) as (lock, directory):
        fd = os.open(lock.name if directory is not None else lock, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600, dir_fd=directory)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise OSError("Invalid settings lock")
            if os.name == "nt":
                import msvcrt
                if os.fstat(fd).st_size == 0:
                    os.write(fd, b"\0")
                def acquire():
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                def release():
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                def acquire():
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                def release():
                    fcntl.flock(fd, fcntl.LOCK_UN)
            while True:
                budget.check()
                try:
                    acquire()
                    break
                except (BlockingIOError, PermissionError):
                    budget.wait(0.05)
            try:
                yield
            finally:
                release()
        finally:
            os.close(fd)


def update_settings(path, settings, expected_hash, budget=None):
    budget = budget or Budget()
    value = plain(settings)
    if "anilistEnabled" in value:
        value["aniListEnabled"] = value.pop("anilistEnabled")
    parse_settings(value)
    data = dumps(value).encode("utf-8")
    if len(data) > SETTINGS_LIMIT:
        raise ValueError("Settings exceed size limit")
    path = checked_path(path)
    with _settings_lock(path, budget):
        _, current_hash = load_settings(path)
        if current_hash != expected_hash:
            raise OSError("Settings changed concurrently")
        atomic_write(path, data, overwrite=True, budget=budget)
    return hashlib.sha256(data).hexdigest().upper()


def write_manifest(report, directory=None):
    root = checked_path(directory if directory is not None else data_path() / "runs")
    request_id = report.get("requestId")
    if not isinstance(request_id, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", request_id):
        raise ValueError("Invalid request ID")
    path = root / f"run-{request_id}.json"
    report = {**report, "manifestPath": str(path)}
    atomic_write(path, dumps(report).encode("utf-8"))
    return report
