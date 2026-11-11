from __future__ import annotations

import threading
from dataclasses import dataclass
from decimal import Decimal
from urllib.parse import urlsplit

from .models import CollectorError
from .subtitles import _AUXILIARY, episode, episodes, extract, post_episodes, validate_identity


@dataclass(frozen=True)
class Numbering:
    offset: int
    maximum: Decimal
    evidence: str

    def __post_init__(self):
        if isinstance(self.offset, bool) or not isinstance(self.offset, int) or not 0 <= self.offset <= 10000:
            raise ValueError("Invalid numbering offset")
        maximum = Decimal(episode(self.maximum))
        object.__setattr__(self, "maximum", maximum)

    def to_season(self, source) -> str | None:
        number = Decimal(episode(source)) - self.offset
        if number < 1 or self.maximum > 0 and number > self.maximum:
            return None
        return episode(number)

    def to_source(self, season) -> str:
        number = Decimal(episode(season))
        if number < 1 or self.maximum > 0 and number > self.maximum:
            raise ValueError("Episode is outside season range")
        return episode(number + self.offset)


def source_key(blog, category=None) -> tuple[str, str, str]:
    return blog.platform, blog.id, category or ""


def from_caption(caption, post, season, maximum, anime=None) -> Numbering | None:
    if season is None or season <= 1:
        return None
    try:
        caption_number = Decimal(episode(caption.episode))
    except ValueError:
        return None
    # A caption proves correspondence only for its linked post, not a category.
    if urlsplit(caption.website)._replace(fragment="") != urlsplit(post.url)._replace(fragment=""):
        return None
    if anime is not None:
        from .identity import mentions

        if not mentions(post.title, (anime.title, anime.original_title, *anime.titles)):
            return None
    numbers = post_episodes(post.title, anime) if anime is not None else episodes(post.title)
    if len(numbers) != 1:
        return None
    individual = set()
    for attachment in extract(post):
        if _AUXILIARY.search(attachment.filename):
            continue
        observed = post_episodes(attachment.filename, anime) if anime is not None else episodes(attachment.filename)
        if len(observed) == 1:
            if anime is not None:
                try:
                    validate_identity(attachment.filename, anime, observed[0])
                except CollectorError:
                    return None
            individual.add(observed[0])
    if individual != {numbers[0]}:
        return None
    difference = Decimal(numbers[0]) - caption_number
    if difference < 0 or difference != difference.to_integral_value() or difference > 10000:
        raise CollectorError("AmbiguousMatch", "Caption and post episode numbering conflict")
    return Numbering(int(difference), Decimal(episode(maximum)), f"caption {episode(caption.episode)} = source {numbers[0]}; title and individual attachment agree")


class NumberingRegistry:
    def __init__(self):
        self._values = {}
        self._conflicts = set()
        self._lock = threading.Lock()

    def has_conflict(self, key) -> bool:
        with self._lock:
            return key in self._conflicts

    def get(self, key) -> Numbering | None:
        with self._lock:
            return None if key in self._conflicts else self._values.get(key)

    def register(self, key, numbering) -> bool:
        with self._lock:
            if key in self._conflicts:
                return False
            previous = self._values.get(key)
            if previous is not None and (previous.offset != numbering.offset or previous.maximum != numbering.maximum):
                self._conflicts.add(key)
                self._values.pop(key, None)
                return False
            self._values[key] = numbering
            return True
