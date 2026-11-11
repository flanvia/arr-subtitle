from __future__ import annotations

import io
import posixpath
import re
import stat
import unicodedata
import zipfile
import zlib
from decimal import Decimal
from html.parser import HTMLParser
from urllib.parse import parse_qs, unquote, urlencode, urljoin, urlsplit

from .models import Attachment, CollectorError


SUBTITLE_EXTENSIONS = frozenset({".smi", ".srt", ".ass", ".ssa"})
ARCHIVE_EXTENSIONS = frozenset({".zip", ".rar", ".7z"})
MAX_SUBTITLE = 10 * 1024 * 1024
MAX_ARCHIVE = 100 * 1024 * 1024
_NUMBER = r"[0-9]{1,4}(?:\.[0-9]+)?"
_SEASON = re.compile(
    r"(?i)(?<!\w)(?:season\s*[0-9]+|s[0-9]+(?![0-9e]))"
    r"|시즌\s*[0-9]+|(?<![0-9])[0-9]+\s*(?:기|期)"
)
_AUXILIARY = re.compile(r"(?i)(?<![a-z])(?:op|ed|ncop|nced|fonts?)(?![a-z])|폰트|글꼴")


def _error(message, status="InvalidSubtitle"):
    return CollectorError(status, message)


def episode(value) -> str:
    if isinstance(value, bool):
        raise ValueError("Invalid episode number")
    text = str(value).strip()
    if len(text) > 32 or not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", text):
        raise ValueError("Invalid episode number")
    number = Decimal(text)
    if number > 99999:
        raise ValueError("Episode number exceeds limit")
    return format(number, "f").rstrip("0").rstrip(".") if "." in text else str(int(number))


def _extension(name):
    return posixpath.splitext(name.replace("\\", "/"))[1].lower()


def _stem(name):
    name = name.replace("\\", "/").rsplit("/", 1)[-1]
    return posixpath.splitext(name)[0] if _extension(name) in SUBTITLE_EXTENSIONS | ARCHIVE_EXTENSIONS | {".mkv", ".mp4", ".avi"} else name


def _unique(values):
    return tuple(dict.fromkeys(episode(value) for value in values))


def episodes(text, require_label=False) -> tuple[str, ...]:
    text = unicodedata.normalize("NFKC", _stem(str(text)))
    text = re.sub(r"(?i)(?<![0-9])[0-9]{3,4}[pi]\b|(?<!\w)[248]k\b", " ", text)
    serial = re.search(r"(?i)(?<![a-z0-9])s[0-9]+((?:e[0-9]{1,4}(?:\.[0-9]+)?)+)(?![0-9])", text)
    if serial:
        return _unique(re.findall(r"(?i)e(" + _NUMBER + r")", serial[1]))
    text = _SEASON.sub(" ", text)
    interval = re.search(
        r"(?<![\w.])([0-9]{1,4})(?:\s*~\s*|-(?=[0-9])|\s+-\s*(?=[0-9]{1,4}\s*(?:화|회)))([0-9]{1,4})(?![0-9])",
        text,
    )
    if interval:
        first, last = map(int, interval.groups())
        if last < first or last - first > 1000:
            raise _error("Invalid episode range", "AmbiguousMatch")
        return tuple(str(number) for number in range(first, last + 1))
    labelled = re.findall(
        r"(?i)(?<![0-9.])(" + _NUMBER + r")\s*(?:화|회)(?![^\W\d_])"
        r"|(?<![a-z0-9])(?:ep(?:isode)?|e)\s*(" + _NUMBER + r")(?![0-9])",
        text,
    )
    if labelled:
        return _unique(a or b for a, b in labelled)
    if require_label:
        return ()
    trailing = re.search(
        r"(?:^|[\s_.-])(" + _NUMBER + r")"
        r"(?:[^\W\d_][\w-]*)?(?:[.\s]*[\[(].*)?[.\s]*$", text,
    )
    return (episode(trailing[1]),) if trailing else ()


def _titles(anime):
    return tuple(sorted({name for name in (anime.title, anime.original_title, *anime.titles) if name}, key=len, reverse=True))


def _strip_title(text, anime):
    for title in _titles(anime):
        parts = re.split(r"[\s._-]+", unicodedata.normalize("NFKC", title))
        pattern = r"[\s._-]+".join(re.escape(part) for part in parts)
        match = re.search(r"(?<!\w)" + pattern + r"(?![\w!'’])", text, re.IGNORECASE)
        if match:
            return text[:match.start()] + " " + text[match.end():]
    return text


def post_episodes(title, anime):
    return episodes(_strip_title(unicodedata.normalize("NFKC", title), anime))


def attachment_title(name):
    stem = re.sub(r"\[[^\]]*\]", " ", _stem(name)).strip()
    stem = _SEASON.sub(" ", stem)
    match = re.search(
        r"(?i)(?:^|[\s_.-])(?:s[0-9]+e[0-9]+|(?:ep(?:isode)?\s*|제\s*)?"
        + _NUMBER + r"(?:\s*(?:화|회)|(?=[^0-9]*$)|\s*[-~]))", stem,
    )
    return (stem[:match.start()] if match else stem).strip(" ._-\t")


def _season(text):
    from .identity import explicit_season

    return explicit_season(text)


def _identity_key(text):
    text = unicodedata.normalize("NFKC", text).casefold()
    return re.sub(r"[\s._-]+", "", text)


def validate_identity(name, anime, episode):
    requested = _unique((episode,))[0]
    expected = anime.season
    if expected is None:
        for title in _titles(anime):
            expected = _season(title)
            if expected is not None:
                break
    # Directories can carry season evidence even when the basename is numeric.
    for component in name.replace("\\", "/").split("/"):
        season = _season(component)
        if season is not None and expected is not None and season != expected:
            raise _error("Attachment season conflicts with request", "AmbiguousMatch")
    stem = unicodedata.normalize("NFKC", _stem(name))
    stripped = _strip_title(stem, anime)
    observed = episodes(stripped)
    if observed and requested not in observed:
        raise _error("Attachment episode conflicts with request", "AmbiguousMatch")
    if len(observed) > 1:
        raise _error("Multiple episodes in one subtitle are not supported", "AmbiguousMatch")
    prefix = attachment_title(stripped)
    prefix = re.sub(r"(?i)\b(?:attachment|subtitles?|episode|ep|batch)\b|자막", " ", prefix).strip(" ._-\t")
    if not prefix:
        return
    key = _identity_key(prefix)
    if any(key == _identity_key(_SEASON.sub(" ", title)) for title in _titles(anime)):
        return
    phrase = " ".join(unicodedata.normalize("NFKC", prefix).casefold().split())
    if len(key) >= 4:
        for title in _titles(anime):
            normalized = " ".join(unicodedata.normalize("NFKC", title).casefold().split())
            if normalized.startswith(phrase + " ") and _season(stem) == _season(title):
                return
    raise _error("Attachment title is not a verified anime alias", "AmbiguousMatch")


class _SamiParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.sami = False
        self.sync = False
        self.paragraph = 0
        self.ignored = 0
        self.korean = False

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.ignored += 1
        if self.ignored:
            return
        if tag == "sami":
            self.sami = True
        if tag == "sync":
            start = dict(attrs).get("start", "") or ""
            self.sync |= bool(re.fullmatch(r"[0-9]+", start))
            self.paragraph = 0
        if tag == "p":
            self.paragraph = 1

    def handle_endtag(self, tag):
        if tag in {"script", "style"}:
            self.ignored = max(0, self.ignored - 1)
        if tag == "p":
            self.paragraph = 0

    def handle_data(self, data):
        if self.paragraph and not self.ignored and re.search("[가-힣]", data):
            self.korean = True


def validate(data, extension):
    if not isinstance(data, bytes) or not 0 < len(data) <= MAX_SUBTITLE:
        raise _error("Invalid subtitle size")
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        encodings = ("utf-16",)
    else:
        encodings = ("utf-8-sig", "cp949")
    for encoding in encodings:
        try:
            text = data.decode(encoding, errors="strict")
            break
        except UnicodeError:
            pass
    else:
        raise _error("Unsupported or invalid subtitle encoding")
    if "\0" in text or not re.search("[가-힣]", text):
        raise _error("Subtitle contains no Korean text or contains binary data")
    clean = re.sub(r"(?is)<!--.*?-->|<script\b[^>]*>.*?</script\s*>|<style\b[^>]*>.*?</style\s*>", "", text).lstrip("\ufeff \r\n\t")
    if re.search(r"(?i)<(?:html|!doctype\s+html|script|iframe)\b", clean):
        raise _error("HTML is not a subtitle")
    extension = extension.lower()
    valid = False
    if extension == ".smi" and re.match(r"(?is)^(?:<!doctype\s+sami[^>]*>\s*)?<sami\b", clean):
        parser = _SamiParser()
        parser.feed(clean)
        valid = parser.sami and parser.sync and parser.korean
    elif extension == ".srt":
        valid = bool(re.search(r"(?m)^\s*[0-9]{2}:[0-5][0-9]:[0-5][0-9][,.][0-9]{3}\s+-->\s+[0-9]{2}:[0-5][0-9]:[0-5][0-9][,.][0-9]{3}", clean))
    elif extension in {".ass", ".ssa"}:
        valid = bool(re.search(r"(?im)^\s*\[Events\]\s*$", clean) and re.search(r"(?im)^Dialogue:\s*[^\r\n]*[0-9]+:[0-5][0-9]:[0-5][0-9]\.[0-9]{2}", clean))
    if not valid:
        raise _error("Invalid subtitle structure")
    return text, "utf-8" if encoding == "utf-8-sig" else encoding


def _detect(data, preferred=""):
    order = [preferred] if preferred in SUBTITLE_EXTENSIONS else []
    order += [ext for ext in (".smi", ".srt", ".ass", ".ssa") if ext not in order]
    for ext in order:
        try:
            validate(data, ext)
            return ext
        except CollectorError:
            continue
    raise _error("Attachment is neither a Korean subtitle nor a supported ZIP")


def archive_entries(data) -> list[tuple[str, bytes]]:
    if not isinstance(data, bytes) or len(data) > MAX_ARCHIVE:
        raise _error("ZIP exceeds download limit")
    try:
        with zipfile.ZipFile(io.BytesIO(data), metadata_encoding="cp949") as archive:
            members = archive.infolist()
            if len(members) > 500 or sum(info.file_size for info in members) > MAX_ARCHIVE:
                raise _error("ZIP exceeds expansion limits")
            seen = set()
            for info in members:
                name = info.orig_filename.replace("\\", "/")
                key = unicodedata.normalize("NFC", posixpath.normpath(name)).casefold()
                if (not name or name.startswith("/") or ":" in name or "\0" in name
                        or ".." in name.split("/") or key in seen
                        or stat.S_ISLNK(info.external_attr >> 16)
                        or info.flag_bits & 1):
                    raise _error("Unsafe ZIP member")
                seen.add(key)
                if _extension(name) in ARCHIVE_EXTENSIONS:
                    raise _error("Nested archives are not supported")
                if _extension(name) in SUBTITLE_EXTENSIONS and info.file_size > MAX_SUBTITLE:
                    raise _error("Expanded subtitle exceeds limit")
            result = []
            for info in members:
                name = info.orig_filename.replace("\\", "/")
                if info.is_dir() or _extension(name) not in SUBTITLE_EXTENSIONS or _AUXILIARY.search(posixpath.basename(name)):
                    continue
                with archive.open(info) as stream:
                    content = stream.read(MAX_SUBTITLE + 1)
                if len(content) > MAX_SUBTITLE:
                    raise _error("Expanded subtitle exceeds limit")
                validate(content, _extension(name))
                result.append((name, content))
            if not result:
                raise _error("ZIP contains no subtitle files")
            return result
    except (zipfile.BadZipFile, UnicodeError, RuntimeError, NotImplementedError, OSError, EOFError, ValueError, zlib.error) as exc:
        raise _error("Invalid or unsupported ZIP archive") from exc


def extract(post) -> tuple[Attachment, ...]:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(post.html, "html.parser")
    found = {}
    for link in soup.select("a[href]"):
        raw = link.get("href")
        if not isinstance(raw, str):
            continue
        url = urljoin(post.url, raw.strip())
        try:
            parts = urlsplit(url)
            if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username is not None or parts.password is not None or parts.port not in {None, 80, 443}:
                continue
        except ValueError:
            continue
        name = next((link.get(attr) for attr in ("download", "data-filename", "data-file-name", "data-origin-name") if isinstance(link.get(attr), str) and link.get(attr)), link.get_text(" ", strip=True))
        name = re.sub(r"(?i)\s+[0-9]+(?:\.[0-9]+)?\s*(?:kb|mb|gb|b)\s*$", "", name)
        drive = parts.hostname.lower() == "drive.google.com"
        if drive:
            match = re.match(r"^/file/d/([A-Za-z0-9_-]+)(?:/|$)", parts.path)
            file_id = match[1] if match else parse_qs(parts.query).get("id", [""])[0] if parts.path in {"/uc", "/open"} else ""
            if not re.fullmatch(r"[A-Za-z0-9_-]+", file_id):
                continue
            url = "https://drive.google.com/uc?" + urlencode({"export": "download", "id": file_id})
        elif _extension(name) not in SUBTITLE_EXTENSIONS | ARCHIVE_EXTENSIONS:
            query = parse_qs(parts.query)
            name = query.get("filename", query.get("name", [unquote(parts.path.rsplit("/", 1)[-1])]))[0]
        if drive or _extension(name) in SUBTITLE_EXTENSIONS | ARCHIVE_EXTENSIONS:
            name = name.replace("\\", "/").rsplit("/", 1)[-1]
            if "\0" not in name:
                found[url] = Attachment(url, name, post.url)
    return tuple(found.values())


def _inherit(numbers):
    numbers = _unique(numbers)
    return numbers if len(numbers) == 1 else ()


def _consistent(numbers, post_numbers):
    if numbers and len(post_numbers) == 1 and not set(numbers).issubset(post_numbers):
        raise _error("Attachment episode conflicts with post", "AmbiguousMatch")
    return numbers or _inherit(post_numbers)


def inspect(session, attachment, post_numbers, budget):
    budget.check()
    post_numbers = _unique(post_numbers)
    named = episodes(attachment.filename)
    extension = _extension(attachment.filename)
    if extension in SUBTITLE_EXTENSIONS:
        return [(attachment.filename, _consistent(named, post_numbers), None)]
    if extension in {".rar", ".7z"}:
        raise _error("Archive format is not supported", "UnsupportedSource")
    response = session.get(attachment.url, budget, limit=MAX_ARCHIVE)
    budget.check()
    if response.data.startswith(b"PK"):
        entries = archive_entries(response.data)
        budget.check()
        observed = [(name, episodes(name)) for name, _ in entries]
        distinct = {number for _, numbers in observed for number in numbers}
        observations = []
        for name, numbers in observed:
            if numbers and len(distinct) <= 1:
                numbers = _consistent(numbers, post_numbers)
            elif not numbers and len(entries) == 1:
                numbers = _inherit(post_numbers)
            observations.append((name, numbers, name))
        return observations
    _detect(response.data, _extension(response.filename or ""))
    response_numbers = episodes(response.filename) if response.filename else ()
    if named and response_numbers and set(named) != set(response_numbers):
        raise _error("Response filename episode conflicts with link", "AmbiguousMatch")
    return [(response.filename or attachment.filename, _consistent(response_numbers or named, post_numbers), None)]


def payload(session, candidate, budget):
    budget.check()
    source = candidate.source_episode or candidate.episode
    source = episode(source)
    response = session.get(candidate.attachment.url, budget, limit=MAX_ARCHIVE)
    budget.check()
    if response.data.startswith(b"PK"):
        entries = archive_entries(response.data)
        if candidate.member is None:
            selected = [(name, data) for name, data in entries if post_episodes(name, candidate.anime) == (source,)]
            if not selected and len(entries) == 1 and not post_episodes(entries[0][0], candidate.anime):
                selected = entries
        else:
            selected = [(name, data) for name, data in entries if name == candidate.member]
        if len(selected) != 1:
            raise _error("Cannot uniquely select ZIP episode", "AmbiguousMatch")
        name, data = selected[0]
        if not post_episodes(name, candidate.anime) and len(entries) != 1:
            raise _error("ZIP member episode is not established", "AmbiguousMatch")
        validate_identity(name, candidate.anime, source)
        extension = _extension(name)
        validate(data, extension)
        budget.check()
        return data, extension, name
    if candidate.member is not None:
        raise _error("Selected archive is no longer a ZIP", "AmbiguousMatch")
    if _extension(candidate.attachment.filename) in {".rar", ".7z"}:
        raise _error("Archive format is not supported", "UnsupportedSource")
    for name in (candidate.attachment.filename, response.filename):
        if name:
            validate_identity(name, candidate.anime, source)
    extension = _detect(response.data, _extension(response.filename or candidate.attachment.filename))
    budget.check()
    return response.data, extension, None
