from __future__ import annotations

import json
import os
import re
import stat
import unicodedata
from dataclasses import replace
from pathlib import Path
from urllib.parse import quote, urlsplit
from xml.etree import ElementTree
from xml.parsers import expat

from .models import Anime, Cancelled, Caption, CollectorError


def normalize(text):
    return " ".join(unicodedata.normalize("NFKC", text).upper().split())


def equal(a, b):
    return normalize(a) == normalize(b)


def _word(char):
    return bool(char) and unicodedata.category(char)[0] in "LN"


def _occurrences(text, value):
    start = 0
    while value and (index := text.find(value, start)) >= 0:
        yield text[index - 1] if index else "", text[index + len(value):index + len(value) + 1]
        start = index + 1


def mentions(text, titles):
    text = normalize(text)
    return any(not _word(before) and not _word(after) and after not in ("!", "'", "’")
               for title in titles if title and title.strip()
               for before, after in _occurrences(text, normalize(title)))


def explicit_season(text):
    match = re.search(r"(?:\bS(?:EASON\s*)?0*(\d+)(?=\b|E\d)|(?<!\d)(\d+)\s*(?:기|期)|시즌\s*(\d+))", text, re.I)
    return int(next(value for value in match.groups() if value is not None)) if match else None


def _partial(title, query):
    value = normalize(query)
    if not value:
        return False
    if not unicodedata.category(value[-1]).startswith("P"):
        return value in normalize(title)
    return any(not _word(before) and (not before or unicodedata.category(before) != "Po")
               and (not after or unicodedata.category(after) != "Po")
               for before, after in _occurrences(normalize(title), value))


def select_candidates(items, query, settings):
    items = list(items)

    def names(anime):
        return (anime.title, anime.original_title, *anime.titles,
                *(alias for mapping in settings.mappings
                  if mapping.anime_id == anime.anime_id and mapping.library_path is None
                  for alias in mapping.aliases))

    exact = [item for item in items if any(name.strip() and equal(name, query) for name in names(item))]
    return exact or [item for item in items if any(name.strip() and _partial(name, query) for name in names(item))]


def _error(message, status="SearchIncomplete"):
    return CollectorError(status, message)


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def _invalid_constant(value):
    raise ValueError("Invalid JSON constant")


def _json(data, depth=32):
    try:
        result = json.loads(data, object_pairs_hook=_pairs, parse_constant=_invalid_constant)
        stack = [(result, 0)]
        while stack:
            item, level = stack.pop()
            if isinstance(item, (dict, list)):
                if level >= depth:
                    raise ValueError("JSON depth limit exceeded")
                stack.extend((value, level + 1) for value in (item.values() if isinstance(item, dict) else item))
        return result
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise _error("Invalid JSON response or metadata") from exc


def _positive(value):
    return type(value) is int and value > 0


def _text(value, required=False):
    if not isinstance(value, str) or (required and not value.strip()):
        raise _error("Invalid title or text field")
    return value


def _anime(item, number=None):
    if not isinstance(item, dict) or not _positive(number if number is not None else item.get("animeNo")):
        raise _error("Invalid Anissia anime entry")
    original = item.get("originalSubject")
    return Anime(number if number is not None else item["animeNo"], _text(item.get("subject"), True),
                 _text("" if original is None else original))


class Anissia:
    def __init__(self, session):
        self.session = session

    def _get(self, path, budget):
        budget.check()
        document = _json(self.session.get("https://api.anissia.net" + path, budget).data)
        if not isinstance(document, dict) or document.get("code") != "ok" or "data" not in document:
            raise _error("Invalid Anissia response")
        return document["data"]

    def detail(self, id, budget):
        if not _positive(id):
            raise ValueError("Invalid anime ID")
        return _anime(self._get(f"/anime/animeNo/{id}", budget), id)

    def search(self, query, budget):
        if not isinstance(query, str) or not query.strip() or len(query) > 256:
            raise ValueError("Invalid search query")
        found = {}
        for page in range(100):
            data = self._get(f"/anime/list/{page}?q={quote(query, safe='')}", budget)
            if not isinstance(data, dict) or not isinstance(data.get("content"), list):
                raise _error("Invalid Anissia search page")
            if "last" in data and type(data["last"]) is not bool:
                raise _error("Invalid Anissia pagination flag")
            total = data.get("totalPages")
            if total is not None and (type(total) is not int or total < 0):
                raise _error("Invalid Anissia page count")
            previous = len(found)
            for item in data["content"]:
                anime = _anime(item)
                if anime.anime_id in found and found[anime.anime_id] != anime:
                    raise _error("Conflicting Anissia search entries")
                found[anime.anime_id] = anime
            if data.get("last") is True or (total is not None and total <= page + 1):
                return list(found.values())
            if len(found) == previous:
                raise _error("Anissia pagination did not advance")
        raise _error("Anissia search page limit reached")

    def captions(self, id, budget):
        from .subtitles import episode

        if not _positive(id):
            raise ValueError("Invalid anime ID")
        data = self._get(f"/anime/caption/animeNo/{id}", budget)
        if not isinstance(data, list):
            raise _error("Invalid Anissia caption list")
        captions = []
        for item in data:
            if not isinstance(item, dict) or type(item.get("episode")) not in (str, int, float):
                raise _error("Invalid caption episode")
            website = _text(item.get("website"), True)
            try:
                url = urlsplit(website)
                if url.scheme not in ("http", "https") or not url.hostname or url.username or url.password or url.port not in (None, 80, 443):
                    raise ValueError("Invalid caption URL")
                number = episode(str(item["episode"]))
            except (ValueError, TypeError) as exc:
                raise _error("Invalid Anissia caption") from exc
            captions.append(Caption(number, website, _text(item.get("name", "")), _text(item.get("updDt", ""))))
        captions.sort(key=lambda item: (item.name, item.website))
        captions.sort(key=lambda item: item.updated_at, reverse=True)
        return captions


def metadata_titles(session, title, budget):
    if not isinstance(title, str) or not title.strip() or len(title) > 256:
        raise ValueError("Invalid metadata search title")
    query = "query ($search: String) { Page(page: 1, perPage: 10) { media(search: $search, type: ANIME) { id format seasonYear title { romaji english native } synonyms relations { edges { relationType node { id } } } } } }"
    budget.check()
    document = _json(session.post_json("https://graphql.anilist.co", {"query": query, "variables": {"search": title}}, budget).data)
    try:
        if not isinstance(document, dict) or "errors" in document:
            raise ValueError("AniList returned errors")
        media = document["data"]["Page"]["media"]
        if not isinstance(media, list):
            raise ValueError("Invalid media list")
        result = []
        for item in media:
            if not isinstance(item, dict) or not _positive(item.get("id")):
                raise ValueError("Invalid metadata ID")
            titles = item["title"]
            synonyms = item.get("synonyms", [])
            if not isinstance(titles, dict) or not isinstance(synonyms, list):
                raise ValueError("Invalid metadata titles")
            names = []
            for value in [*(titles.get(key) for key in ("romaji", "english", "native")), *synonyms]:
                if value is not None:
                    name = _text(value)
                    if name.strip() and name not in names:
                        names.append(name)
            if not names:
                raise ValueError("Missing metadata titles")
            year = item.get("seasonYear")
            if year is not None and not _positive(year):
                raise ValueError("Invalid metadata year")
            edges = item.get("relations", {}).get("edges", [])
            if not isinstance(edges, list):
                raise ValueError("Invalid metadata relations")
            related = tuple(edge["node"]["id"] for edge in edges)
            if any(not _positive(number) for number in related):
                raise ValueError("Invalid related metadata ID")
            format = item.get("format")
            result.append({"id": item["id"], "titles": tuple(names), "format": _text("" if format is None else format),
                           "year": year, "relatedIds": related})
        return result
    except (KeyError, TypeError, AttributeError, ValueError) as exc:
        raise _error("Invalid AniList response") from exc


def _no_symlinks(path):
    path = Path(os.path.abspath(path))
    for component in (path, *path.parents):
        if component.is_symlink() or (hasattr(component, "is_junction") and component.is_junction()):
            raise _error("Symbolic links are not allowed for media or metadata", "AmbiguousMatch")
    return path


def parse_media(path):
    from .subtitles import episode

    path = _no_symlinks(path)
    name = re.sub(r"\[[^\]]*\]", " ", path.stem).strip()
    name = re.sub(r"\s*\((?:\d{3,4}p|HEVC|AVC)[^)]*\)", "", name, flags=re.I).strip()
    match = re.fullmatch(r"(.*?)[\s._-]*S(\d+)E(\d+)(.*)", name, re.I)
    file_season = None
    if match:
        title, file_season, number, tail = match.groups()
        file_season = int(file_season)
    else:
        match = re.fullmatch(r"(.*?)\s+-\s*(\d{1,4}(?:\.\d+)?)(?:v\d+)?(?:화)?(.*)", name)
        if not match:
            match = re.fullmatch(r"(.*?)\s+(\d{1,4}(?:\.\d+)?)(?:v\d+)?(?:화)?\s*()", name)
        if match:
            title, number, tail = match.groups()
        elif re.fullmatch(r"\d{1,4}(?:\.\d+)?", name):
            title, number, tail = "", name, ""
        else:
            raise _error("Cannot identify episode from filename", "AmbiguousMatch")
    if re.match(r"\s*(?:-|[eE]|[&+~])\s*\d", tail):
        raise _error("Multi-episode video is not supported", "UnsupportedSource")
    if re.match(r"p\b", tail, re.I):
        raise _error("Resolution is not an episode number", "AmbiguousMatch")
    title = title.strip().rstrip("-").strip()
    folder = re.fullmatch(r"Season\s+0*(\d+)", path.parent.name, re.I)
    series = path.parent.parent if folder else path.parent
    seasons = {value for value in (int(folder[1]) if folder else None, file_season, explicit_season(title)) if value is not None}
    if len(seasons) > 1 or 0 in seasons:
        raise _error("Folder and filename season conflict or unsupported special season", "AmbiguousMatch")
    return {"title": series.name, "season": next(iter(seasons), None), "libraryPath": str(series),
            "fileTitle": title or None, "mediaPath": str(path)}, episode(number)


def _read_local(path):
    path = _no_symlinks(path)
    descriptors = []
    try:
        if os.open in os.supports_dir_fd and hasattr(os, "O_NOFOLLOW"):
            # Open each ancestor without following links, including replacements during traversal.
            directory = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
            descriptors.append(directory)
            for part in path.parts[1:-1]:
                directory = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
                descriptors.append(directory)
            descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0), dir_fd=directory)
        else:
            if not path.exists():
                return None
            before = path.stat(follow_symlinks=False)
            if not stat.S_ISREG(before.st_mode):
                raise _error("Local metadata is not a regular file", "AmbiguousMatch")
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            descriptors.append(descriptor)
            opened = os.fstat(descriptor)
            if not os.path.samestat(before, opened):
                raise _error("Local metadata changed while opening", "AmbiguousMatch")
            _no_symlinks(path)
        if descriptor not in descriptors:
            descriptors.append(descriptor)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > 1024 * 1024:
            raise _error("Local metadata is not a regular file or exceeds size limit", "AmbiguousMatch")
        with os.fdopen(os.dup(descriptor), "rb") as stream:
            data = stream.read(1024 * 1024 + 1)
        if len(data) > 1024 * 1024:
            raise _error("Local metadata exceeds size limit", "AmbiguousMatch")
        return data
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise _error("Cannot safely read local metadata", "AmbiguousMatch") from exc
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _xml(data):
    parser = expat.ParserCreate()
    depth = 0

    def start(name, attributes):
        nonlocal depth
        depth += 1
        if depth > 32:
            raise ValueError("XML depth limit exceeded")

    def end(name):
        nonlocal depth
        depth -= 1

    def forbidden(*args):
        raise ValueError("XML DTD and entities are forbidden")

    parser.StartElementHandler = start
    parser.EndElementHandler = end
    parser.StartDoctypeDeclHandler = forbidden
    parser.EntityDeclHandler = forbidden
    parser.ExternalEntityRefHandler = forbidden
    try:
        parser.Parse(data, True)
        return ElementTree.fromstring(data)
    except (ValueError, expat.ExpatError, ElementTree.ParseError) as exc:
        raise _error("Invalid or unsafe local XML metadata", "AmbiguousMatch") from exc


def _local_evidence(query):
    paths = []
    if query.get("libraryPath"):
        paths.extend(Path(query["libraryPath"]) / name for name in ("tvshow.nfo", "metadata.json"))
    if query.get("mediaPath"):
        paths.extend(Path(query["mediaPath"]).with_suffix(extension) for extension in (".nfo", ".json"))
    titles, seasons, ids = [], set(), set()

    def number(value):
        if type(value) is int or isinstance(value, str) and re.fullmatch(r"\d{1,10}", value.strip()):
            return int(value)
        raise _error("Invalid local metadata identity number", "AmbiguousMatch")

    for path in dict.fromkeys(paths):
        data = _read_local(path)
        if data is None:
            continue
        if path.suffix == ".nfo":
            root = _xml(data)
            names = ("showtitle",) if root.tag.rsplit("}", 1)[-1] == "episodedetails" else ("title", "originaltitle")
            for node in root.iter():
                tag = node.tag.rsplit("}", 1)[-1]
                value = "".join(node.itertext()).strip()
                if tag in names and value:
                    titles.append(value)
                elif tag == "season":
                    seasons.add(number(value))
                elif tag == "uniqueid" and node.attrib.get("type", "").lower() == "anissia":
                    ids.add(number(value))
        else:
            document = _json(data, 16)
            if not isinstance(document, dict):
                raise _error("Local JSON metadata must be an object", "AmbiguousMatch")
            for key in ("Name", "OriginalTitle", "title", "originalTitle", "showtitle", "showTitle"):
                if key in document:
                    value = _text(document[key])
                    if value.strip():
                        titles.append(value)
            for key in ("season", "SeasonNumber"):
                if key in document:
                    seasons.add(number(document[key]))
            if "animeId" in document:
                ids.add(number(document["animeId"]))
            providers = document.get("ProviderIds", {})
            if not isinstance(providers, dict):
                raise _error("Invalid local provider IDs", "AmbiguousMatch")
            if "Anissia" in providers:
                ids.add(number(providers["Anissia"]))
    if any(value < 0 for value in seasons) or any(value < 1 for value in ids):
        raise _error("Invalid local metadata identity number", "AmbiguousMatch")
    return tuple(dict.fromkeys(titles)), seasons, ids


def _resolved(anime, aliases, season):
    return replace(anime, titles=tuple(dict.fromkeys(value for value in (anime.title, anime.original_title, *aliases) if value.strip())), season=season)


class Resolver:
    def __init__(self, session, settings):
        self.session = session
        self.settings = settings

    def get_mapping(self, query):
        matches = []
        for mapping in self.settings.mappings:
            if query.get("season") is not None and query["season"] != mapping.season:
                continue
            if mapping.library_path is not None and (not query.get("libraryPath") or os.path.abspath(mapping.library_path) != os.path.abspath(query["libraryPath"])):
                continue
            names = (mapping.title, *mapping.aliases)
            by_title = any(equal(name, value) for name in names for value in (query.get("title"), query.get("fileTitle")) if value)
            if by_title or query.get("animeId") == mapping.anime_id:
                matches.append(mapping)
        if len(matches) > 1:
            raise _error("Multiple explicit mappings match", "AmbiguousMatch")
        if matches and query.get("animeId") is not None and query["animeId"] != matches[0].anime_id:
            raise _error("Explicit ID conflicts with mapping", "AmbiguousMatch")
        return matches[0] if matches else None

    def resolve(self, query, budget):
        if not isinstance(query, dict):
            raise ValueError("Anime query must be an object")
        for key in ("title", "fileTitle", "libraryPath", "mediaPath"):
            if query.get(key) is not None and (not isinstance(query[key], str) or not query[key].strip()):
                raise ValueError("Invalid query text or path")
        if query.get("animeId") is not None and not _positive(query["animeId"]):
            raise ValueError("Invalid anime ID")
        if query.get("season") is not None and not _positive(query["season"]):
            raise _error("Special or invalid season requires an explicit mapping", "AmbiguousMatch")
        budget.check()
        mapping = self.get_mapping(query)
        local, seasons, ids = _local_evidence(query)
        season = query.get("season") or (mapping.season if mapping else None)
        if len(seasons) > 1 or (season is not None and any(value != season for value in seasons)) or len(ids) > 1:
            raise _error("Local metadata identity conflicts with path", "AmbiguousMatch")
        if season is None and seasons:
            season = next(iter(seasons))

        def validated(anime):
            if ids and ids != {anime.anime_id}:
                raise _error("Local metadata anime ID conflicts with resolved title", "AmbiguousMatch")
            if local and not any(equal(title, alias) for title in local for alias in anime.titles):
                raise _error("Local metadata title conflicts with resolved anime", "AmbiguousMatch")
            return anime

        api = Anissia(self.session)
        explicit_id = query.get("animeId") or (mapping.anime_id if mapping else None) or next(iter(ids), None)
        if explicit_id is not None:
            anime = _resolved(api.detail(explicit_id, budget), mapping.aliases if mapping else (), season)
            return validated(anime)
        titles = tuple(dict.fromkeys(value for value in (query.get("fileTitle"), query.get("title"), *local) if value))
        if not titles:
            raise _error("No anime title provided", "AmbiguousMatch")
        specific = titles[0]
        errors = []

        def season_fits(anime):
            known = {value for value in (explicit_season(anime.title), explicit_season(anime.original_title), explicit_season(specific)) if value is not None}
            return len(known) <= 1 and (season is None or (known == {season} if known else season == 1))

        def search(title):
            try:
                return [api.detail(item.anime_id, budget) for item in api.search(title, budget)]
            except Cancelled:
                raise
            except (CollectorError, OSError, ValueError) as exc:
                errors.append(exc)
                return []

        for title in titles:
            found = {item.anime_id: item for item in search(title)
                     if any(equal(name, specific) for name in (item.title, item.original_title))}
            if len(found) > 1:
                raise _error("Multiple Anissia titles match; specify anime ID", "AmbiguousMatch")
            if found:
                anime = next(iter(found.values()))
                if season_fits(anime):
                    return validated(_resolved(anime, (), season))
        if self.settings.anilist_enabled:
            for title in titles:
                try:
                    metadata = metadata_titles(self.session, title, budget)
                except Cancelled:
                    raise
                except (CollectorError, OSError, ValueError) as exc:
                    errors.append(exc)
                    continue
                matches = [item for item in metadata if item["format"] in ("TV", "TV_SHORT", "ONA")
                           and any(equal(name, specific) for name in item["titles"])]
                if len(matches) > 1:
                    raise _error("Multiple metadata titles match; specify anime ID", "AmbiguousMatch")
                if not matches:
                    continue
                names = matches[0]["titles"]

                def native(name):
                    return any("\u3040" <= char <= "\u9fff" for char in name)

                for alias in sorted(names, key=native, reverse=True)[:8]:
                    found = {item.anime_id: item for item in search(alias)
                             if any(equal(name, item.title) or equal(name, item.original_title)
                                    or native(name) and normalize(item.original_title).startswith(normalize(name) + " ")
                                    for name in names)}
                    if len(found) > 1:
                        raise _error("Multiple Anissia alias titles match; specify anime ID", "AmbiguousMatch")
                    if found:
                        anime = next(iter(found.values()))
                        if season_fits(anime):
                            return validated(_resolved(anime, names, season))
                        break
        if errors:
            raise _error("Metadata lookup incomplete; specify anime ID or mapping") from errors[0]
        raise _error("Cannot uniquely identify anime and season; specify anime ID or mapping", "AmbiguousMatch")
