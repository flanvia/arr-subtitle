from __future__ import annotations

import concurrent.futures
import threading
import time
import uuid
from dataclasses import dataclass, field, replace
from decimal import Decimal
from urllib.parse import unquote, urlsplit

from .models import Anime, Budget, Cancelled, Candidate, CollectorError
from .network import redact
from .serialization import plain


def _attempt(source, status, error=None):
    return {"source": redact(source) if source.startswith(("https://", "http://")) else source, "status": status, "error": str(error) if error else None}


@dataclass
class _State:
    anime: Anime
    mapping: object = None
    maximum: Decimal = Decimal(0)
    captions: list = field(default_factory=list)
    candidates: dict = field(default_factory=dict)
    attempts: list = field(default_factory=list)
    complete: bool = True
    expected: set = field(default_factory=set)
    aliases: dict = field(default_factory=dict)
    numbering: dict = field(default_factory=dict)
    conflicts: set = field(default_factory=set)
    seen: set = field(default_factory=set)
    aliases_checked: bool = False
    lock: object = field(default_factory=threading.RLock)

    def record(self, source, status, error=None, failed=False):
        self.attempts.append(_attempt(source, status, error))
        if failed:
            self.complete = False


class Engine:
    def __init__(self, session, settings):
        self.session = session
        self.settings = settings

    def _catalog(self, query, budget):
        from .identity import Anissia, equal, metadata_titles, select_candidates
        api = Anissia(self.session)
        selected = select_candidates(api.search(query, budget), query, self.settings)
        if selected or not self.settings.anilist_enabled:
            return selected
        exact = [m for m in metadata_titles(self.session, query, budget) if any(equal(t, query) for t in m["titles"])]
        if len(exact) != 1:
            return []
        for title in exact[0]["titles"]:
            found = select_candidates(api.search(title, budget), title, self.settings)
            if found:
                return [replace(a, titles=tuple(dict.fromkeys((*a.titles, *exact[0]["titles"])))) for a in found]
        return []

    def search(self, query, budget, progress=None):
        if not isinstance(query, str) or not query.strip() or len(query) > 256:
            raise ValueError("Invalid search query")
        started = time.monotonic()
        states, attempts = [], []
        complete = True
        try:
            if progress:
                progress({"stage": "search", "source": query})
            anime = self._catalog(query, budget)
            states = [_State(a) for a in anime]
            self._gather(states, budget, progress)
        except Cancelled as error:
            complete = False
            status = "cancelled" if budget.cancel.is_set() else "timeout"
            attempts.append(_attempt("search", status, error))
            for state in states:
                state.record("search", status, error, True)
        except Exception as error:
            complete = False
            attempts.append(_attempt("search", "failed", error))
        results = [self._result(state) for state in sorted(states, key=lambda s: s.anime.anime_id)]
        complete = complete and all(a["complete"] for a in results)
        return {"query": query, "anime": results, "complete": complete, "attempts": attempts, "requests": self.session.counts,
                "elapsedMilliseconds": (time.monotonic() - started) * 1000,
                "success": complete and bool(results) and all(a["subtitles"] and not a["missingEpisodes"] for a in results)}

    def candidates(self, anime, budget, progress=None, *, mapping=None):
        state = _State(anime, mapping=mapping)
        try:
            self._gather([state], budget, progress)
        except Cancelled as error:
            state.record("search", "cancelled" if budget.cancel.is_set() else "timeout", error, True)
        return list(state.candidates.values()), state.attempts, state.complete, state.expected

    def verify_candidate(self, anime, candidate, budget):
        from .blogs import Explorer, detect_blog, in_scope
        from .identity import Anissia, Resolver
        state = _State(anime)
        state.mapping = Resolver(self.session, self.settings).get_mapping({"animeId": anime.anime_id, "season": anime.season})
        captions = Anissia(self.session).captions(anime.anime_id, budget)
        state.captions = captions
        state.maximum = max((Decimal(c.episode) for c in captions if _valid_episode(c.episode)), default=Decimal(0))
        blog = None
        matching_caption = None
        for caption in captions:
            source = detect_blog(self.session, caption.website, budget)
            if in_scope(source, candidate.page):
                blog = source
                if caption.website == candidate.page:
                    matching_caption = caption
                if matching_caption:
                    break
        if blog is None:
            for configured in self.settings.blogs:
                if in_scope(configured, candidate.page):
                    blog = detect_blog(self.session, configured.url, budget)
                    break
        if blog is None:
            raise CollectorError("AmbiguousMatch", "Candidate source is not a permitted blog")
        explorer = Explorer(self.session)
        post = explorer.read(candidate.page, budget, blog)
        if matching_caption:
            self._seed_evidence(state, matching_caption, post, blog)
        else:
            for caption in captions:
                source = detect_blog(self.session, caption.website, budget)
                if source.id.lower() != blog.id.lower() or source.platform.lower() != blog.platform.lower():
                    continue
                seed = explorer.read(caption.website, budget, source)
                self._seed_evidence(state, caption, seed, source)
        caption_numbers = (matching_caption.episode,) if matching_caption and _valid_episode(matching_caption.episode) else ()
        self._post(state, post, blog, candidate.maker, caption_numbers, budget, None)
        for fresh in state.candidates.values():
            if (fresh.episode, fresh.attachment, fresh.member, fresh.source_episode) == (candidate.episode, candidate.attachment, candidate.member, candidate.source_episode):
                return fresh
        raise CollectorError("AmbiguousMatch", "Selected subtitle candidate could not be revalidated")

    def _prepare(self, state, budget, sources, progress):
        from .blogs import Explorer, detect_blog
        from .identity import Anissia, explicit_season
        api = Anissia(self.session)
        if progress:
            progress({"stage": "anime", "source": state.anime.title})
        if not state.anime.original_title:
            detail = api.detail(state.anime.anime_id, budget)
            state.anime = replace(state.anime, title=detail.title, original_title=detail.original_title)
        maps = [state.mapping] if state.mapping else [m for m in self.settings.mappings if m.anime_id == state.anime.anime_id and m.library_path is None]
        if len(maps) > 1:
            raise CollectorError("AmbiguousMatch", "Multiple search mappings for anime")
        state.mapping = maps[0] if maps else None
        names = (*state.anime.titles, state.anime.title, state.anime.original_title, *(state.mapping.aliases if state.mapping else ()))
        state.anime = replace(state.anime, titles=tuple(dict.fromkeys(t for t in names if t)),
                              season=state.mapping.season if state.mapping else state.anime.season or explicit_season(state.anime.title) or explicit_season(state.anime.original_title))
        state.captions = api.captions(state.anime.anime_id, budget)
        state.maximum = max((Decimal(c.episode) for c in state.captions if _valid_episode(c.episode)), default=Decimal(0))
        state.expected.update(c.episode for c in state.captions if _valid_episode(c.episode))
        seeds = []
        for caption in state.captions:
            budget.check()
            try:
                blog = detect_blog(self.session, caption.website, budget)
                key = (blog.platform.lower(), blog.id.lower())
                source = sources.setdefault(key, {"blog": blog, "members": {}, "categories": {}})
                source["members"].setdefault(state.anime.anime_id, (state, caption.name))
                post = Explorer(self.session).read(caption.website, budget, blog)
                source["categories"].setdefault(state.anime.anime_id, set())
                if post.category_url:
                    source["categories"][state.anime.anime_id].add(post.category_url)
                self._seed_evidence(state, caption, post, blog)
                seeds.append((caption, blog, post))
            except Cancelled:
                raise
            except Exception as error:
                state.record(caption.website, "post-failed", error, True)
        for caption, blog, post in seeds:
            try:
                self._post(state, post, blog, caption.name, (caption.episode,) if _valid_episode(caption.episode) else (), budget, progress)
            except Cancelled:
                raise
            except Exception as error:
                state.record(post.url, "post-failed", error, True)

    def _seed_evidence(self, state, caption, post, blog):
        from .identity import equal, explicit_season
        from .subtitles import attachment_title, episodes, extract
        season = explicit_season(post.title)
        if season and state.anime.season and season != state.anime.season:
            raise CollectorError("AmbiguousMatch", "Caption season conflicts with anime")
        state.anime = replace(state.anime, season=state.anime.season or season)
        labels = [t for t in post.labels if t and (equal(t, state.anime.title) or
                  state.anime.title.startswith(t + " ") and post.title.startswith(t + " ") and explicit_season(t) == explicit_season(state.anime.title))]
        if labels:
            state.anime = replace(state.anime, titles=tuple(dict.fromkeys((*state.anime.titles, *labels))))
        key = self._source_key(blog, post)
        numbering = self._caption_numbering(state, caption, post)
        if numbering is not None:
            previous = state.numbering.get(key)
            if previous is not None and previous[0] != numbering[0]:
                state.conflicts.add(key)
                state.numbering.pop(key, None)
                state.record(post.url, "numbering-conflict", "Conflicting caption episode offsets", True)
            elif key not in state.conflicts:
                state.numbering[key] = numbering
        if post.category_url:
            names = {attachment_title(a.filename) for a in extract(post)} - {""}
            category_name = unquote(urlsplit(post.category_url).path.rstrip("/").rsplit("/", 1)[-1])
            aliases = state.aliases.setdefault(post.category_url, set())
            for name in names:
                if equal(category_name, name):
                    aliases.add(name)

    @staticmethod
    def _source_key(blog, post):
        return (blog.platform.lower(), blog.id.lower(), post.category_url or "")

    @staticmethod
    def _caption_numbering(state, caption, post):
        from .numbering import from_caption
        if state.mapping:
            return state.mapping.episode_offset, "explicit configured episode offset"
        if not state.anime.season or state.anime.season <= 1 or not _valid_episode(caption.episode):
            return None
        numbering = from_caption(caption, post, state.anime.season, state.maximum, anime=state.anime)
        return (numbering.offset, numbering.evidence) if numbering else None

    def _gather(self, states, budget, progress):
        sources = {}
        # Discovery establishes numbering before concurrent source execution.
        for state in states:
            try:
                self._prepare(state, budget, sources, progress)
            except Cancelled:
                raise
            except Exception as error:
                state.record(f"anime:{state.anime.anime_id}", "failed", error, True)
        for blog in self.settings.blogs:
            source = sources.setdefault((blog.platform.lower(), blog.id.lower()), {"blog": blog, "members": {}, "categories": {}})
            for state in states:
                source["members"].setdefault(state.anime.anime_id, (state, blog.id))
        if not states:
            return
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            running = {}
            pending = iter(sorted(sources.values(), key=lambda s: (not bool(s["categories"]), s["blog"].url)))
            while True:
                while len(running) < 4 and not budget.cancelled:
                    source = next(pending, None)
                    if source is None:
                        break
                    source_budget = budget.child(min(40, max(0.001, budget.remaining)))
                    running[pool.submit(self._source, source, source_budget, progress)] = source
                if not running:
                    break
                done, _ = concurrent.futures.wait(running, timeout=0.1, return_when=concurrent.futures.FIRST_COMPLETED)
                for future in done:
                    source = running.pop(future)
                    try:
                        future.result()
                    except Cancelled as error:
                        for state, _ in source["members"].values():
                            state.record(source["blog"].url, "timeout", error, True)
                    except Exception as error:
                        for state, _ in source["members"].values():
                            state.record(source["blog"].url, "failed", error, True)
            if budget.cancelled:
                raise Cancelled()

    def _source(self, source, budget, progress):
        from .blogs import Explorer
        from .identity import mentions
        members = list(source["members"].values())
        titles = tuple(dict.fromkeys(t for state, _ in members for t in state.anime.titles))
        categories = tuple(dict.fromkeys(c for values in source["categories"].values() for c in values))
        explorer = Explorer(self.session)
        blog = source["blog"]
        if progress:
            progress({"stage": "source", "source": redact(blog.url)})
        for post in explorer.explore(blog, titles, budget, categories=categories):
            for state, maker in members:
                scoped = post.category_url and post.category_url in source["categories"].get(state.anime.anime_id, ())
                if not scoped and not mentions(post.title + " " + " ".join(post.labels), state.anime.titles):
                    continue
                try:
                    with state.lock:
                        self._post(state, post, blog, maker, (), budget, progress)
                except Cancelled:
                    raise
                except Exception as error:
                    state.record(post.url, "post-failed", error, True)
        for state, _ in members:
            state.record(blog.url, "complete" if explorer.complete else "incomplete", failed=not explorer.complete)

    def _post(self, state, post, blog, maker, caption_numbers, budget, progress):
        from .blogs import Explorer
        from .identity import equal, explicit_season, mentions, metadata_titles
        from .subtitles import episodes, extract, inspect, post_episodes, validate_identity
        marker = (self._source_key(blog, post), post.url, post.summary, bool(post.html), caption_numbers)
        if marker in state.seen:
            return
        state.seen.add(marker)
        category = post.category_url
        if post.summary or not post.html:
            post = Explorer(self.session).read(post.url, budget, blog)
        attachments = extract(post)
        if not attachments:
            post = Explorer(self.session).read(post.url, budget, blog)
            attachments = extract(post)
        aliases = state.aliases.get(post.category_url, set())
        anime = replace(state.anime, titles=tuple(dict.fromkeys((*state.anime.titles, *aliases))))
        key = self._source_key(blog, post)
        if key in state.conflicts:
            state.record(post.url, "numbering-conflict", failed=True)
            return
        trusted_category = bool(category and category == post.category_url and aliases)
        if not caption_numbers and not trusted_category and not mentions(post.title + " " + " ".join(post.labels), anime.titles):
            state.record(post.url, "post-conflict", "Fetched post does not establish anime identity", True)
            return
        season = explicit_season(post.title)
        if season and anime.season and season != anime.season:
            state.record(post.url, "season-conflict", "Post season conflicts with anime", True)
            return
        numbering = (state.mapping.episode_offset, "explicit configured episode offset") if state.mapping else state.numbering.get(key)
        numbers = post_episodes(post.title, anime)
        if not numbers:
            numbers = tuple(_number(Decimal(n) + numbering[0]) for n in caption_numbers) if numbering else caption_numbers
        def season_number(number):
            if not numbering:
                return number
            mapped = Decimal(number) - numbering[0]
            return _number(mapped) if mapped >= 1 and (state.maximum <= 0 or mapped <= state.maximum) else None
        state.expected.update(n for number in numbers if (n := season_number(number)) is not None)
        if not attachments:
            state.record(post.url, "attachment-missing" if numbers else "skipped-non-subtitle", "Subtitle post has no supported attachment" if numbers else None, bool(numbers))
            return
        observations = []
        for attachment in attachments:
            try:
                observations.extend((attachment, name, observed, member) for name, observed, member in inspect(self.session, attachment, numbers, budget))
            except Cancelled:
                raise
            except Exception as error:
                state.record(post.url, "attachment-invalid", error, True)
        observed_numbers = {n for _, _, ns, _ in observations for n in ns}
        if not numbering and anime.season and anime.season > 1 and state.maximum > 0 and any(Decimal(n) > state.maximum for n in observed_numbers):
            state.record(post.url, "numbering-ambiguous", "Explicit episode mapping required", True)
            return
        permitted = episodes(anime.title, require_label=True)
        for attachment, name, observed, member in observations:
            if not observed:
                state.record(post.url, "episode-unresolved", "Attachment has no unique episode evidence", True)
            if len(observed_numbers) == 1 and numbers and any(n not in numbers for n in observed):
                state.record(post.url, "episode-conflict", "Attachment episode conflicts with post", True)
                continue
            for original in observed:
                mapped = season_number(original)
                if mapped is None or permitted and mapped not in permitted:
                    continue
                try:
                    validate_identity(name, anime, original)
                except CollectorError:
                    if not state.aliases_checked and self.settings.anilist_enabled and anime.original_title:
                        state.aliases_checked = True
                        metadata = [m for m in metadata_titles(self.session, anime.original_title, budget) if any(equal(t, anime.original_title) for t in m["titles"])]
                        if len(metadata) == 1:
                            state.anime = replace(state.anime, titles=tuple(dict.fromkeys((*state.anime.titles, *metadata[0]["titles"]))))
                            anime = replace(anime, titles=tuple(dict.fromkeys((*anime.titles, *state.anime.titles))))
                    try:
                        validate_identity(name, anime, original)
                    except CollectorError:
                        state.record(post.url, "attachment-conflict", f"Attachment identity is not established: {name}", True)
                        continue
                candidate = Candidate(anime, mapped, maker, post.url, attachment, member, original if numbering else None, numbering[1] if numbering else None)
                candidate_key = (mapped, post.url, attachment.url, member)
                fresh = candidate_key not in state.candidates
                state.candidates[candidate_key] = candidate
                if fresh and progress:
                    progress({"stage": "found", "source": anime.title, "episode": mapped, "page": redact(post.url)})

    @staticmethod
    def _result(state):
        from .subtitles import episodes
        listings = {}
        for candidate in state.candidates.values():
            key = (candidate.episode, candidate.maker, redact(candidate.page))
            entry = listings.setdefault(key, {"episode": candidate.episode, "maker": candidate.maker, "page": redact(candidate.page), "attachments": [], "sourceEpisode": candidate.source_episode, "numberingEvidence": candidate.numbering_evidence})
            name = candidate.member or candidate.attachment.filename
            if name not in entry["attachments"]:
                entry["attachments"].append(name)
        for entry in listings.values():
            entry["attachments"].sort()
        found = {c.episode for c in state.candidates.values()}
        designated = episodes(state.anime.title, require_label=True)
        maximum = max((int(Decimal(n)) for n in found if Decimal(n) == int(Decimal(n))), default=0)
        if maximum > 10000:
            maximum = 10000
            state.complete = False
            state.record("episodes", "incomplete", "Episode coverage limit reached", True)
        expected = set(designated) if designated else state.expected | {str(n) for n in range(1, maximum + 1)}
        return {"anime": plain(state.anime), "subtitles": sorted(listings.values(), key=lambda s: (Decimal(s["episode"]), s["maker"], s["page"])),
                "missingEpisodes": sorted(expected - found, key=Decimal), "complete": state.complete,
                "attempts": sorted(state.attempts, key=lambda a: (a["source"], a["status"]))}

    def collect(self, query, targets, output, budget, search_only=False, progress=None):
        from .identity import Resolver, equal, parse_media
        from .storage import existing, save
        from .subtitles import payload
        started = time.monotonic()
        report_id = uuid.uuid4().hex
        anime, candidates, attempts = None, [], []
        results = [None] * len(targets)
        first_candidate = None
        if not targets:
            raise ValueError("At least one episode target is required")
        try:
            anime = Resolver(self.session, self.settings).resolve(query, budget)
            mapping = Resolver(self.session, self.settings).get_mapping(query)
            if mapping:
                anime = replace(anime, titles=tuple(dict.fromkeys((*anime.titles, *mapping.aliases))))
            for i, target in enumerate(targets):
                if not _valid_episode(target.get("episode", "")):
                    raise ValueError("Invalid episode target")
                if target.get("mediaPath"):
                    local_query, local_episode = parse_media(target["mediaPath"])
                    local_title = target.get("fileTitle") or local_query.get("fileTitle")
                    if local_title and not any(equal(local_title, title) for title in anime.titles):
                        results[i] = _episode_result(target, "AmbiguousMatch", error="Video title is not a verified anime alias")
                        continue
                    if anime.season and local_query.get("season") and anime.season != local_query["season"]:
                        results[i] = _episode_result(target, "AmbiguousMatch", error="Video season conflicts with requested anime")
                        continue
                    local_query["animeId"] = anime.anime_id
                    resolved = Resolver(self.session, self.settings).resolve(local_query, budget)
                    if local_episode != target["episode"] or resolved.anime_id != anime.anime_id:
                        results[i] = _episode_result(target, "AmbiguousMatch", error="Video identity conflicts with requested anime")
                        continue
                if not search_only:
                    try:
                        results[i] = existing(anime, target, output)
                    except Exception as error:
                        results[i] = _episode_result(target, "WriteFailed", error=str(error))
            if any(r is None for r in results):
                candidates, attempts, complete, _ = self.candidates(anime, budget, progress, mapping=mapping)
                if candidates:
                    first_candidate = (time.monotonic() - started) * 1000
                by_episode = {}
                for candidate in candidates:
                    by_episode.setdefault(candidate.episode, []).append(candidate)
                def download(i):
                    target = targets[i]
                    last_error = None
                    for candidate in by_episode.get(target["episode"], ()):
                        budget.check()
                        if search_only:
                            return _episode_result(target, "Found", page=redact(candidate.page), sourceEpisode=candidate.source_episode, numberingEvidence=candidate.numbering_evidence)
                        try:
                            data, extension, member = payload(self.session, candidate, budget)
                            result = save(data, extension, anime, target, output, redact(candidate.page), budget, member=member)
                            result.update(sourceEpisode=candidate.source_episode, numberingEvidence=candidate.numbering_evidence)
                            if target.get("mediaPath"):
                                result["mediaPath"] = target["mediaPath"]
                            if progress:
                                progress({"stage": "saved", "source": result.get("path", ""), "episode": target["episode"]})
                            return result
                        except Cancelled:
                            raise
                        except Exception as error:
                            last_error = error
                    if last_error:
                        return _episode_result(target, getattr(last_error, "status", "SearchIncomplete"), error=str(last_error))
                    return _episode_result(target, "NotFound" if complete else "SearchIncomplete", error="No matching subtitle in permitted sources" if complete else "Permitted sources could not be fully checked")
                with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
                    pending = iter(i for i, result in enumerate(results) if result is None)
                    running = {}
                    while True:
                        while len(running) < 4 and not budget.cancelled:
                            i = next(pending, None)
                            if i is None:
                                break
                            running[pool.submit(download, i)] = i
                        if not running:
                            break
                        done, _ = concurrent.futures.wait(running, timeout=0.1, return_when=concurrent.futures.FIRST_COMPLETED)
                        for future in done:
                            i = running.pop(future)
                            try:
                                results[i] = future.result()
                            except Cancelled:
                                results[i] = _episode_result(targets[i], "SearchIncomplete", error="Cancelled or timed out")
                            except Exception as error:
                                results[i] = _episode_result(targets[i], "SearchIncomplete", error=str(error))
        except Cancelled as error:
            attempts.append(_attempt("request", "cancelled", error))
        except Exception as error:
            attempts.append(_attempt("request", "failed", error))
            for i, result in enumerate(results):
                if result is None:
                    results[i] = _episode_result(targets[i], getattr(error, "status", "SearchIncomplete"), error=str(error))
        for i, result in enumerate(results):
            if result is None:
                results[i] = _episode_result(targets[i], "SearchIncomplete", error="Cancelled or timed out")
        return {"requestId": report_id, "anime": plain(anime), "episodes": results, "attempts": attempts,
                "requests": self.session.counts, "receivedBytes": self.session.received_bytes, "elapsedMilliseconds": (time.monotonic() - started) * 1000,
                "firstCandidateMilliseconds": first_candidate, "manifestPath": None,
                "success": bool(results) and all(r["status"] in ("Downloaded", "AlreadyPresent", "Found") for r in results)}


def _number(number):
    return format(Decimal(number), "f").rstrip("0").rstrip(".") if "." in format(Decimal(number), "f") else str(number)


def _valid_episode(value):
    from .subtitles import episode
    try:
        return episode(value) == value
    except (ValueError, CollectorError, TypeError):
        return False


def _episode_result(target, status, **fields):
    result = {"episode": target["episode"], "status": status, "path": None, "page": None, "sha256": None, "error": None, "archiveMember": None, "sourceEpisode": None, "numberingEvidence": None}
    if target.get("mediaPath"):
        result["mediaPath"] = target["mediaPath"]
    result.update(fields)
    return result
