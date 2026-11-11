from __future__ import annotations

import argparse
import dataclasses
import signal
import sys
import threading
import uuid
from contextlib import contextmanager

from . import __version__
from . import storage
from .models import Budget, Cancelled, CollectorError
from .network import Session
from .serialization import dumps, plain


class ArgumentError(ValueError):
    pass


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ArgumentError(message)


def _positive(value):
    try:
        result = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("Expected a positive integer") from error
    if result < 1:
        raise argparse.ArgumentTypeError("Expected a positive integer")
    return result


def parser():
    result = Parser(prog="arr-subtitle", description="애니메이션 한국어 자막 검색과 수집")
    result.add_argument("--version", action="version", version=__version__)
    result.add_argument("command", nargs="?", help="search, match, download, scan, collect, blogs 또는 영상 경로")
    result.add_argument("arguments", nargs="*")
    result.add_argument("--config")
    result.add_argument("--json", action="store_true")
    result.add_argument("--season", type=_positive)
    result.add_argument("--anime-id", type=_positive)
    result.add_argument("--episodes")
    result.add_argument("--output")
    result.add_argument("--timeout", type=float, default=60)
    return result


def parse_episodes(value):
    from .subtitles import episode
    if not isinstance(value, str) or len(value) > 10000:
        raise ValueError("Invalid episode range")
    result = []
    for part in value.split(","):
        bounds = part.strip().split("-")
        if len(bounds) == 1:
            result.append(episode(bounds[0]))
        elif len(bounds) == 2 and all(bound.isascii() and bound.isdigit() for bound in bounds):
            start, end = map(int, bounds)
            if end < start or end - start >= 1000:
                raise ValueError("Invalid episode range")
            result.extend(str(number) for number in range(start, end + 1))
        else:
            raise ValueError("Invalid episode range")
        if len(result) > 1000:
            raise ValueError("Episode count exceeds limit")
    if not result:
        raise ValueError("No episodes requested")
    return list(dict.fromkeys(result))


@contextmanager
def _interrupts(event):
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    previous = signal.getsignal(signal.SIGINT)
    signal.signal(signal.SIGINT, lambda *_: event.set())
    try:
        yield
    finally:
        signal.signal(signal.SIGINT, previous)


def _print_report(report, output, as_json):
    if as_json:
        print(dumps(report), file=output)
        return
    if isinstance(report, dict) and "episodes" in report:
        for item in report["episodes"]:
            identity = item.get("mediaPath") or item.get("episode", "")
            detail = item.get("path") or item.get("page") or item.get("error") or ""
            print(f"{identity}: {item.get('status', '')} {detail}", file=output)
        if report.get("manifestPath"):
            print(f"Manifest: {report['manifestPath']}", file=output)
    else:
        print(dumps(report), file=output)


def run(argv=None, *, transport=None, stdout=None, stderr=None, cancel=None):
    from .batch import collect_path
    output = stdout if stdout is not None else sys.stdout
    error_output = stderr if stderr is not None else sys.stderr
    argv = list(sys.argv[1:] if argv is None else argv)
    arguments = parser()
    if not argv:
        arguments.print_help(file=output)
        return 0
    if "--help" in argv or "-h" in argv:
        arguments.print_help(file=output)
        return 0
    if argv == ["--version"]:
        print(__version__, file=output)
        return 0
    event = cancel if cancel is not None else threading.Event()
    lock = threading.Lock()
    def progress(*updates):
        with lock:
            for update in updates:
                value = plain(update)
                if isinstance(value, dict):
                    print(" ".join(str(value[key]) for key in ("stage", "mediaPath", "episode", "status", "source", "detail", "error") if value.get(key) is not None), file=error_output, flush=True)
                else:
                    print(str(value), file=error_output, flush=True)
    as_json = "--json" in argv
    try:
        args = arguments.parse_intermixed_args(argv)
        budget = Budget(args.timeout, event)
        if args.command is None:
            raise ArgumentError("Command or media path required")
        command, positional = args.command, args.arguments
        known = {"search", "match", "download", "scan", "collect", "blogs"}
        if command not in known:
            if positional:
                raise ArgumentError("Unexpected positional arguments")
            positional = [command]
            command = "download"
        if command in ("search", "match", "download", "scan") and len(positional) != 1:
            raise ArgumentError("Exactly one query or media path is required")
        if command != "collect" and args.episodes is not None:
            raise ArgumentError("episodes is only supported by collect")
        if command in ("search", "blogs") and any(value is not None for value in (args.anime_id, args.season, args.output)):
            raise ArgumentError("Media options are not supported by this command")
        if command == "collect" and (len(positional) > 1 or not positional and args.anime_id is None or args.episodes is None or not args.output):
            raise ArgumentError("collect requires title or anime ID, episodes and output")
        config = args.config if args.config is not None else storage.config_path()
        settings, settings_hash = storage.load_settings(config)
        if args.config is not None and settings_hash == "missing":
            raise ArgumentError("Explicit settings file does not exist")
        with _interrupts(event), Session(transport=transport, request_started=lambda url: progress({"stage": "request", "source": url})) as session:
            if command == "blogs":
                from .blogs import detect_blog, known_blog
                if positional == ["list"]:
                    report = plain(settings.blogs)
                elif len(positional) == 2 and positional[0] in ("add", "remove"):
                    action, url = positional
                    if action == "add":
                        blog = detect_blog(session, url, budget)
                        platforms = {"blogspot": "Blogspot", "tistory": "Tistory", "naver": "Naver", "generic": "Generic"}
                        if blog.platform.lower() not in platforms:
                            raise ArgumentError("Unsupported blog platform")
                        blog = dataclasses.replace(blog, platform=platforms[blog.platform.lower()])
                    else:
                        from .network import validate_url
                        validate_url(url)
                        blog = next((item for item in settings.blogs if item.url.rstrip("/") == url.rstrip("/")), None) or known_blog(url)
                        if blog is None:
                            raise ArgumentError("Blog not registered")
                        matched = next((item for item in settings.blogs if item.platform.lower() == blog.platform.lower() and item.id == blog.id), None)
                        if matched is None:
                            raise ArgumentError("Blog not registered")
                        blog = matched
                    blogs = tuple(item for item in settings.blogs if (item.platform.lower(), item.id) != (blog.platform.lower(), blog.id))
                    if action == "add":
                        blogs += (blog,)
                    storage.update_settings(config, dataclasses.replace(settings, blogs=blogs), settings_hash, budget)
                    report = plain(blog)
                else:
                    raise ArgumentError("Expected blogs list, blogs add URL or blogs remove URL")
                _print_report(report, output, args.json)
                return 130 if event.is_set() else 0
            from .engine import Engine
            engine = Engine(session, settings)
            if command == "search":
                report = engine.search(positional[0], budget, progress=progress)
            elif command == "collect":
                query = {"title": positional[0] if positional else None, "animeId": args.anime_id, "season": args.season}
                targets = [{"episode": number, "mediaPath": None} for number in parse_episodes(args.episodes)]
                report = engine.collect(query, targets, str(storage.checked_path(args.output)), budget, progress=progress)
            else:
                path = storage.checked_path(positional[0])
                if command == "scan" and not path.is_dir():
                    raise ArgumentError("scan requires a directory")
                if command == "match" and (not path.parent.is_dir() or path.exists() and not path.is_file()):
                    raise ArgumentError("match requires a video file")
                report = collect_path(engine, path, args.output or "", budget, search_only=command == "match", anime_id=args.anime_id, season=args.season, progress=progress)
            if event.is_set():
                report = {**report, "cancelled": True, "success": False}
            if command not in ("search", "match"):
                try:
                    report = storage.write_manifest(report, args.output if command == "collect" else None)
                except (OSError, ValueError) as error:
                    report = {**report, "success": False, "manifestError": str(error)}
                    progress({"stage": "manifest", "error": str(error)})
            _print_report(report, output, args.json)
            return 130 if event.is_set() or report.get("cancelled") else 0 if report.get("success") else 1
    except (Cancelled, KeyboardInterrupt) as error:
        interrupted = event.is_set() or isinstance(error, KeyboardInterrupt)
        report = {"requestId": uuid.uuid4().hex, "episodes": [], "attempts": [], "success": False, "cancelled": interrupted, "timedOut": not interrupted, "error": "Operation cancelled" if interrupted else "Operation timed out"}
        if "command" in locals() and command in ("collect", "scan", "download"):
            try:
                report = storage.write_manifest(report, args.output if command == "collect" else None)
            except (OSError, ValueError) as error:
                report["manifestError"] = str(error)
                print(f"Error: {error}", file=error_output)
        _print_report(report, output, as_json)
        return 130 if interrupted else 1
    except (ValueError, TypeError) as error:
        print(f"Error: {error}", file=error_output)
        if as_json:
            _print_report({"success": False, "error": str(error)}, output, True)
        return 2
    except (OSError, CollectorError) as error:
        print(f"Error: {error}", file=error_output)
        if as_json:
            _print_report({"success": False, "status": getattr(error, "status", "WriteFailed"), "error": str(error)}, output, True)
        return 1


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="strict")
    return run(argv)
