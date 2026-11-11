from pathlib import Path

from .models import Blog, Mapping, Settings


def parse_settings(data):
    if not isinstance(data, dict):
        raise ValueError("Settings must be an object")
    enabled = data.get("aniListEnabled", True)
    if not isinstance(enabled, bool):
        raise ValueError("aniListEnabled must be boolean")
    blogs, mappings = [], []
    if not isinstance(data.get("blogs", []), list) or not isinstance(data.get("mappings", []), list):
        raise ValueError("Settings collections must be arrays")
    from .network import validate_url
    for item in data.get("blogs", []):
        if not isinstance(item, dict):
            raise ValueError("Invalid blog settings")
        validate_url(item.get("url", ""))
        platform = item.get("platform", "")
        if platform not in ("Blogspot", "Tistory", "Naver", "Generic") or not isinstance(item.get("id"), str) or not item["id"]:
            raise ValueError("Invalid blog settings")
        blogs.append(Blog(item["url"], platform, item["id"]))
    if len({(b.platform, b.id) for b in blogs}) != len(blogs):
        raise ValueError("Duplicate blog")
    for item in data.get("mappings", []):
        if not isinstance(item, dict):
            raise ValueError("Invalid mapping")
        title, season, number = item.get("title"), item.get("season"), item.get("animeId")
        aliases = item.get("aliases", [])
        offset = item.get("episodeOffset", 0)
        path = item.get("libraryPath")
        if not isinstance(title, str) or not title.strip() or type(season) is not int or season < 1 or type(number) is not int or number < 1:
            raise ValueError("Invalid mapping identity")
        if not isinstance(aliases, list) or any(not isinstance(a, str) or not a.strip() for a in aliases) or type(offset) is not int or abs(offset) > 10000:
            raise ValueError("Invalid mapping aliases or offset")
        if path is not None and (not isinstance(path, str) or not path):
            raise ValueError("Invalid mapping path")
        mappings.append(Mapping(title, season, number, tuple(aliases), path, offset))
    keys = [(m.title.upper(), m.season, str(Path(m.library_path).absolute()) if m.library_path else None) for m in mappings]
    if len(set(keys)) != len(keys):
        raise ValueError("Overlapping mappings")
    return Settings(enabled, tuple(blogs), tuple(mappings))
