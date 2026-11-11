from __future__ import annotations

import base64
import hashlib
import json
import sys
from dataclasses import replace
from pathlib import PurePosixPath

from arr_subtitle.blogs import detect_blog, in_scope, known_blog
from arr_subtitle.engine import Engine
from arr_subtitle.identity import Resolver
from arr_subtitle.models import Attachment, Budget, Candidate, CollectorError
from arr_subtitle.network import Session, json_data, validate_url
from arr_subtitle.settings import parse_settings
from arr_subtitle.subtitles import episode, payload, validate


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(value):
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _text(value, name, maximum=8192, empty=False):
    if (not isinstance(value, str) or len(value) > maximum or (not empty and not value.strip())
            or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise ValueError(f"Invalid {name}")
    return value


def _config(config):
    if not isinstance(config, dict) or set(config) - {"fallback_blogs", "mappings_json", "anilist_enabled", "timeout"}:
        raise ValueError("Invalid provider configuration")
    try:
        if len(_json(config).encode("utf-8")) > 1024 * 1024:
            raise ValueError("Provider configuration exceeds size limit")
    except (TypeError, RecursionError) as error:
        raise ValueError("Invalid provider configuration") from error
    timeout = config.get("timeout", 20)
    if type(timeout) is not int or not 1 <= timeout <= 20:
        raise ValueError("Timeout must be an integer between 1 and 20 seconds")
    enabled = config.get("anilist_enabled", True)
    if type(enabled) is not bool:
        raise ValueError("anilist_enabled must be boolean")
    raw_blogs = config.get("fallback_blogs", "")
    raw_mappings = config.get("mappings_json", "[]")
    if not isinstance(raw_blogs, str) or not isinstance(raw_mappings, str):
        raise ValueError("Blog and mapping settings must be strings")
    try:
        mappings = json_data(raw_mappings)
    except (ValueError, RecursionError) as error:
        raise ValueError("mappings_json must contain a JSON array") from error
    if not isinstance(mappings, list):
        raise ValueError("mappings_json must contain a JSON array")
    blogs = []
    seen = set()
    for line in raw_blogs.splitlines():
        url = line.strip()
        if not url:
            continue
        validate_url(url)
        blog = known_blog(url)
        entry = ({"url": blog.url, "platform": blog.platform.capitalize(), "id": blog.id}
                 if blog else {"url": url, "platform": "Generic", "id": hashlib.sha256(url.encode()).hexdigest()})
        key = (entry["platform"], entry["id"])
        if key not in seen:
            blogs.append(entry)
            seen.add(key)
    normalized = {"aniListEnabled": enabled, "blogs": blogs, "mappings": mappings}
    settings = parse_settings(normalized)
    return settings, timeout, _digest(normalized)


def _language(language):
    if not isinstance(language, dict):
        raise ValueError("Language must be an object")
    for field in ("hi", "forced"):
        if field in language and type(language[field]) is not bool:
            raise ValueError(f"language.{field} must be boolean")
    return language.get("alpha3") == "kor" and not language.get("hi", False) and not language.get("forced", False)


def _query(video):
    if not isinstance(video, dict):
        raise ValueError("Video must be an object")
    title = _text(video.get("series"), "series title", 256)
    season = video.get("season")
    if type(season) is not int or not 1 <= season <= 10000:
        raise CollectorError("AmbiguousMatch", "A positive season and explicit series title are required")
    number = video.get("episode")
    if type(number) not in (int, str) or isinstance(number, str) and len(number) > 32:
        raise ValueError("A single episode number is required")
    return {"title": title, "season": season}, episode(str(number))


def _identity(candidate):
    return {
        "anime_id": candidate.anime.anime_id,
        "title": candidate.anime.title,
        "season": candidate.anime.season,
        "episode": candidate.episode,
        "maker": candidate.maker,
        "page": candidate.page,
        "attachment_url": candidate.attachment.url,
        "attachment_filename": candidate.attachment.filename,
        "attachment_page": candidate.attachment.page,
        "member": candidate.member,
        "source_episode": candidate.source_episode,
        "numbering_evidence": candidate.numbering_evidence,
    }


def _check_identity(value):
    fields = {"anime_id", "title", "season", "episode", "maker", "page", "attachment_url",
              "attachment_filename", "attachment_page", "member", "source_episode", "numbering_evidence"}
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError("Invalid candidate identity")
    if type(value["anime_id"]) is not int or value["anime_id"] < 1:
        raise ValueError("Invalid candidate anime ID")
    if type(value["season"]) is not int or not 1 <= value["season"] <= 10000:
        raise ValueError("Invalid candidate season")
    _text(value["title"], "candidate title", 256)
    _text(value["maker"], "candidate maker", 1024, empty=True)
    _text(value["attachment_filename"], "attachment filename", 1024)
    if not isinstance(value["episode"], str) or len(value["episode"]) > 32 or episode(value["episode"]) != value["episode"]:
        raise ValueError("Invalid candidate episode")
    for field in ("page", "attachment_url", "attachment_page"):
        validate_url(_text(value[field], field))
    for field in ("source_episode", "numbering_evidence"):
        if value[field] is not None:
            _text(value[field], field, 4096, empty=True)
    member = value["member"]
    if member is not None:
        _text(member, "archive member", 1024)
        normalized = member.replace("\\", "/")
        path = PurePosixPath(normalized)
        if path.is_absolute() or ".." in path.parts or ":" in normalized or normalized.startswith("/"):
            raise ValueError("Unsafe archive member")


def _results(session, settings, query, number, budget):
    anime = Resolver(session, settings).resolve(query, budget)
    settings = _detected_settings(session, settings, budget)
    candidates, attempts, complete, _ = Engine(session, settings).candidates(anime, budget)
    selected = [candidate for candidate in candidates if candidate.episode == number]
    if any(candidate.anime.anime_id != anime.anime_id or candidate.anime.season != query["season"] for candidate in selected):
        raise CollectorError("AmbiguousMatch", "Candidate anime or season conflicts with resolved video")
    failed = sum(1 for attempt in attempts if attempt.get("error"))
    if not selected and not complete:
        raise CollectorError("SearchIncomplete", "Required subtitle discovery did not complete")
    if not complete:
        print(f"arr_subtitle: partial discovery, {failed} source attempts require attention", file=sys.stderr, flush=True)
    return selected


def _detected_settings(session, settings, budget, page=None):
    blogs = tuple(replace(detect_blog(session, blog.url, budget), url=blog.url)
                  if blog.platform.lower() == "generic" and (page is None or in_scope(blog, page))
                  else blog for blog in settings.blogs)
    return replace(settings, blogs=blogs)


class ArrSubtitleProvider:
    def search(self, video, languages, config):
        settings, timeout, config_hash = _config(config)
        if not isinstance(languages, list):
            raise ValueError("Languages must be a list")
        accepted = [_language(language) for language in languages]
        if not isinstance(video, dict):
            raise ValueError("Video must be an object")
        if video.get("kind") != "episode" or not any(accepted):
            return []
        query, number = _query(video)
        budget = Budget(timeout)
        with Session() as session:
            candidates = _results(session, settings, query, number, budget)
        results = {}
        for candidate in candidates:
            identity = _identity(candidate)
            _check_identity(identity)
            identifier = _digest(identity)
            provider_payload = {"version": 1, "query": query, "episode": number,
                                "config_hash": config_hash, "identity": identity, "id": identifier}
            results[identifier] = {
                "id": identifier,
                "provider": "arr_subtitle",
                "language": {"alpha3": "kor", "hi": False, "forced": False},
                "provider_payload": provider_payload,
                "matches": ["series", "season", "episode"],
                "hash_verifiable": False,
                "hearing_impaired_verifiable": False,
                "page_link": candidate.page,
                "filename": candidate.member or candidate.attachment.filename,
                "uploader": candidate.maker,
            }
        return [results[key] for key in sorted(results)]

    def download(self, provider_payload, language, config):
        settings, timeout, config_hash = _config(config)
        if not _language(language):
            raise ValueError("Only ordinary Korean subtitles are supported")
        expected = {"version", "query", "episode", "config_hash", "identity", "id"}
        if not isinstance(provider_payload, dict) or set(provider_payload) != expected:
            raise ValueError("Invalid provider payload")
        if type(provider_payload["version"]) is not int or provider_payload["version"] != 1:
            raise ValueError("Unsupported provider payload version")
        try:
            if len(_json(provider_payload).encode("utf-8")) > 64 * 1024:
                raise ValueError("Provider payload exceeds size limit")
        except (TypeError, RecursionError) as error:
            raise ValueError("Invalid provider payload") from error
        if provider_payload["config_hash"] != config_hash:
            raise ValueError("Provider settings changed; search again")
        query = provider_payload["query"]
        if not isinstance(query, dict) or set(query) != {"title", "season"}:
            raise ValueError("Invalid provider query")
        checked_query, number = _query({"series": query["title"], "season": query["season"], "episode": provider_payload["episode"]})
        if number != provider_payload["episode"]:
            raise ValueError("Invalid provider episode")
        identity = provider_payload["identity"]
        _check_identity(identity)
        if identity["episode"] != number or identity["season"] != query["season"] or provider_payload["id"] != _digest(identity):
            raise ValueError("Provider payload identity mismatch")
        budget = Budget(timeout)
        with Session() as session:
            verification_budget = budget.child(timeout * 0.75)
            anime = Resolver(session, settings).resolve(checked_query, verification_budget)
            if (anime.anime_id != identity["anime_id"] or anime.title != identity["title"]
                    or anime.season != identity["season"]):
                raise CollectorError("AmbiguousMatch", "Selected anime identity changed")
            settings = _detected_settings(session, settings, verification_budget, identity["page"])
            candidate = Candidate(anime, number, identity["maker"], identity["page"],
                                  Attachment(identity["attachment_url"], identity["attachment_filename"], identity["attachment_page"]),
                                  identity["member"], identity["source_episode"], identity["numbering_evidence"])
            selected = Engine(session, settings).verify_candidate(anime, candidate, verification_budget)
            if _identity(selected) != identity:
                raise CollectorError("AmbiguousMatch", "Selected subtitle is no longer a verified candidate")
            content, extension, member = payload(session, selected, budget)
            if selected.member is not None and member != selected.member:
                raise ValueError("Downloaded archive member changed")
            extension = extension.lower().lstrip(".")
            formats = {"smi": "sami", "srt": "srt", "ass": "ass", "ssa": "ass"}
            if extension not in formats:
                raise ValueError("Unsupported subtitle format")
            _, encoding = validate(content, "." + extension)
        return {"content_b64": base64.b64encode(content).decode("ascii"),
                "content_sha256": hashlib.sha256(content).hexdigest(),
                "format": formats[extension], "encoding": encoding}
