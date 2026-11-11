from __future__ import annotations

import threading
import time
from dataclasses import dataclass


class CollectorError(Exception):
    def __init__(self, status: str, message: str):
        super().__init__(message)
        self.status = status


class Cancelled(CollectorError):
    def __init__(self, message="Operation cancelled or timed out"):
        super().__init__("SearchIncomplete", message)


class Budget:
    def __init__(self, timeout=60, cancel=None, *, deadline=None):
        if not isinstance(timeout, (int, float)) or not 0 < timeout <= 600:
            raise ValueError("Timeout must be between 0 and 600 seconds")
        self.deadline = min(time.monotonic() + timeout, deadline) if deadline is not None else time.monotonic() + timeout
        self.cancel = cancel if cancel is not None else threading.Event()

    @property
    def remaining(self):
        return max(0.0, self.deadline - time.monotonic())

    @property
    def cancelled(self):
        return self.cancel.is_set() or self.remaining <= 0

    def check(self):
        if self.cancelled:
            raise Cancelled()

    def wait(self, seconds):
        self.check()
        self.cancel.wait(min(seconds, self.remaining))
        self.check()

    def child(self, timeout):
        return Budget(timeout, self.cancel, deadline=self.deadline)


@dataclass(frozen=True)
class Anime:
    anime_id: int
    title: str
    original_title: str = ""
    titles: tuple[str, ...] = ()
    season: int | None = None


@dataclass(frozen=True)
class Caption:
    episode: str
    website: str
    name: str = ""
    updated_at: str = ""


@dataclass(frozen=True)
class Blog:
    url: str
    platform: str
    id: str


@dataclass(frozen=True)
class Post:
    url: str
    title: str
    html: str
    labels: tuple[str, ...] = ()
    summary: bool = False
    category_url: str | None = None


@dataclass(frozen=True)
class Attachment:
    url: str
    filename: str
    page: str


@dataclass(frozen=True)
class Candidate:
    anime: Anime
    episode: str
    maker: str
    page: str
    attachment: Attachment
    member: str | None = None
    source_episode: str | None = None
    numbering_evidence: str | None = None


@dataclass(frozen=True)
class Mapping:
    title: str
    season: int
    anime_id: int
    aliases: tuple[str, ...] = ()
    library_path: str | None = None
    episode_offset: int = 0


@dataclass(frozen=True)
class Settings:
    anilist_enabled: bool = True
    blogs: tuple[Blog, ...] = ()
    mappings: tuple[Mapping, ...] = ()
