from __future__ import annotations

import concurrent.futures
import os
import time
import uuid
from pathlib import Path

from .models import Cancelled, CollectorError
from .storage import _parent, checked_path, is_link


CHUNK_SIZE = 128
WORKERS = 4
MEDIA_EXTENSIONS = {".mkv", ".mp4", ".avi"}


def iter_media(path, budget=None):
    if budget is not None:
        budget.check()
    path = checked_path(path)
    if path.is_file() or not path.exists() and path.parent.is_dir() and path.suffix.lower() in MEDIA_EXTENSIONS:
        if path.suffix.lower() not in MEDIA_EXTENSIONS:
            raise ValueError("Unsupported media extension")
        yield str(path)
        return
    if not path.is_dir():
        raise ValueError("Media path does not exist")
    def walk(directory):
        if budget is not None:
            budget.check()
        with _parent(Path(directory) / ".arr-subtitle-probe") as (_, descriptor), os.scandir(descriptor if descriptor is not None else directory) as entries:
            for entry in entries:
                if budget is not None:
                    budget.check()
                child = Path(directory) / entry.name
                info = entry.stat(follow_symlinks=False)
                if is_link(child, info):
                    continue
                if entry.is_dir(follow_symlinks=False):
                    yield from walk(child)
                elif entry.is_file(follow_symlinks=False) and Path(entry.name).suffix.lower() in MEDIA_EXTENSIONS:
                    yield str(child)
    yield from walk(path)


def group_key(query):
    from .identity import normalize
    return (*tuple((key, query.get(key)) for key in ("title", "animeId", "season", "libraryPath")),
            ("fileTitle", normalize(query.get("fileTitle") or query.get("title") or "")))


def _parse(path, anime_id, season):
    from .identity import parse_media
    query, number = parse_media(path)
    query = dict(query)
    if season is not None and query.get("season") not in (None, season):
        raise CollectorError("AmbiguousMatch", "Explicit season conflicts with media path")
    if anime_id is not None:
        query["animeId"] = anime_id
    if season is not None:
        query["season"] = season
    target = {"episode": number, "mediaPath": str(checked_path(path)), "fileTitle": query.get("fileTitle"), "query": query}
    return query, target


def _failure(target, error):
    return {"episode": target.get("episode", ""), "mediaPath": target.get("mediaPath"), "status": getattr(error, "status", "SearchIncomplete"), "error": str(error)}


def collect_paths(engine, paths, output, budget, *, search_only=False, anime_id=None, season=None, progress=None):
    started = time.monotonic()
    report = {"requestId": uuid.uuid4().hex, "anime": None, "episodes": [], "attempts": [], "requests": {}, "receivedBytes": 0, "elapsedMilliseconds": 0, "firstCandidateMilliseconds": None, "success": False, "cancelled": False, "enumerationComplete": False}
    source = iter(paths)
    anime = []
    seen = False
    complete = True
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as executor:
            while True:
                budget.check()
                chunk = []
                enumeration_error = None
                for _ in range(CHUNK_SIZE):
                    if budget.cancelled:
                        break
                    try:
                        chunk.append(next(source))
                    except StopIteration:
                        break
                    except (OSError, Cancelled) as error:
                        enumeration_error = error
                        break
                if not chunk:
                    if enumeration_error is not None:
                        raise enumeration_error
                    budget.check()
                    report["enumerationComplete"] = True
                    break
                seen = True
                groups = {}
                for path in chunk:
                    try:
                        query, target = _parse(path, anime_id, season)
                        key = group_key(query)
                        if key not in groups:
                            groups[key] = (dict(query), [])
                        groups[key][1].append(target)
                    except (ValueError, OSError, CollectorError) as error:
                        result = _failure({"mediaPath": str(path)}, error)
                        report["episodes"].append(result)
                        complete = False
                        if progress:
                            progress(result)
                pending = {}
                remaining = iter(groups.values())
                def fill():
                    while len(pending) < WORKERS and not budget.cancelled and not isinstance(enumeration_error, Cancelled):
                        item = next(remaining, None)
                        if item is None:
                            return
                        query, targets = item
                        submitted = time.monotonic()
                        future = executor.submit(engine.collect, query, targets, output, budget, search_only=search_only, progress=progress)
                        pending[future] = (targets, submitted)
                fill()
                while pending:
                    done, _ = concurrent.futures.wait(pending, timeout=0.1, return_when=concurrent.futures.FIRST_COMPLETED)
                    for future in done:
                        targets, submitted = pending.pop(future)
                        try:
                            result = future.result()
                            report["episodes"].extend(result.get("episodes", []))
                            report["attempts"].extend(result.get("attempts", []))
                            complete = complete and result.get("success", False)
                            if result.get("anime") is not None:
                                anime.append(result["anime"])
                            first = result.get("firstCandidateMilliseconds")
                            if first is not None:
                                absolute = (submitted - started) * 1000 + first
                                previous = report["firstCandidateMilliseconds"]
                                report["firstCandidateMilliseconds"] = absolute if previous is None else min(previous, absolute)
                            if progress:
                                for episode in result.get("episodes", []):
                                    progress(episode)
                        except (ValueError, OSError, CollectorError) as error:
                            complete = False
                            report["episodes"].extend(_failure(target, error) for target in targets)
                    fill()
                if budget.cancelled or isinstance(enumeration_error, Cancelled):
                    for _, targets in remaining:
                        report["episodes"].extend(_failure(target, Cancelled()) for target in targets)
                    raise Cancelled()
                if enumeration_error is not None:
                    raise enumeration_error
    except Cancelled:
        report["cancelled"] = budget.cancel.is_set()
        report["timedOut"] = not budget.cancel.is_set()
        complete = False
    except OSError as error:
        report["attempts"].append({"source": "filesystem", "status": "SearchIncomplete", "error": str(error)})
        complete = False
    if not seen and not report["cancelled"] and not report.get("timedOut") and not report["attempts"]:
        raise ValueError("No supported videos found")
    report["anime"] = anime[0] if len(anime) == 1 else None
    report["requests"] = engine.session.counts
    report["receivedBytes"] = engine.session.received_bytes
    report["elapsedMilliseconds"] = round((time.monotonic() - started) * 1000)
    report["success"] = bool(report["episodes"]) and complete and not report["cancelled"]
    return report


def collect_path(engine, path, output, budget, *, search_only=False, anime_id=None, season=None, progress=None):
    if anime_id is not None:
        first = None
        for media in iter_media(path, budget):
            budget.check()
            query, _ = _parse(media, anime_id, season)
            key = group_key(query)
            if first is None:
                first = key
            elif key != first:
                raise ValueError("A single anime ID cannot be applied to multiple series or seasons")
    return collect_paths(engine, iter_media(path, budget), output, budget, search_only=search_only, anime_id=anime_id, season=season, progress=progress)
