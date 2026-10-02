"""Parsing and building Nexus Mods URLs (mod pages and collections)."""
from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlparse

SITE = "https://www.nexusmods.com"
_HOSTS = {"nexusmods.com", "www.nexusmods.com", "next.nexusmods.com"}
_MOD_PATH = re.compile(r"^/(?:games/)?(?P<domain>[a-z0-9_\-]+)/mods/(?P<id>\d+)(?:/|$)", re.IGNORECASE)
_COLLECTION_PATH = re.compile(
    r"^/(?:games/)?(?P<domain>[a-z0-9_\-]+)/collections/(?P<slug>[a-z0-9_\-]+)"
    r"(?:/revisions/(?P<revision>\d+))?(?:/|$)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ModRef:
    """A mod on a game, e.g. ModRef("cyberpunk2077", 107)."""

    domain: str
    mod_id: int

    @property
    def key(self) -> str:
        return f"{self.domain}:{self.mod_id}"

    @property
    def url(self) -> str:
        return f"{SITE}/{self.domain}/mods/{self.mod_id}"

    def files_url(self, file_id: int) -> str:
        return f"{self.url}?tab=files&file_id={file_id}"


@dataclass(frozen=True)
class CollectionRef:
    """A collection of mods; `revision` None means the latest published one."""

    domain: str
    slug: str
    revision: int | None = None

    @property
    def key(self) -> str:
        return f"{self.domain}:collection:{self.slug}"

    @property
    def url(self) -> str:
        suffix = f"/revisions/{self.revision}" if self.revision else ""
        return f"{SITE}/{self.domain}/collections/{self.slug}{suffix}"


Link = ModRef | CollectionRef


def _nexus_path(text: str) -> str | None:
    """The path of a Nexus link, or None if `text` is not a link to a Nexus site."""
    text = text.strip()
    if not text:
        return None
    if "://" not in text:
        text = "https://" + text
    try:
        parts = urlparse(text)
    except ValueError:
        return None
    return parts.path if (parts.hostname or "").lower() in _HOSTS else None


def parse_mod_url(text: str) -> ModRef | None:
    """Return the mod a Nexus link points at, or None if it is not a mod page link."""
    path = _nexus_path(text)
    match = _MOD_PATH.match(path) if path else None
    return ModRef(match["domain"].lower(), int(match["id"])) if match else None


def parse_collection_url(text: str) -> CollectionRef | None:
    """Return the collection a Nexus link points at (with its revision, if the link names one)."""
    path = _nexus_path(text)
    match = _COLLECTION_PATH.match(path) if path else None
    if not match:
        return None
    revision = int(match["revision"]) if match["revision"] else None
    return CollectionRef(match["domain"].lower(), match["slug"], revision)


def parse_link(text: str) -> Link | None:
    return parse_mod_url(text) or parse_collection_url(text)


def parse_links(text: str) -> tuple[list[Link], list[str]]:
    """Parse one link per line. Returns (unique mods/collections in order, lines that were neither)."""
    links: list[Link] = []
    rejected: list[str] = []
    seen: set[Link] = set()
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        link = parse_link(line)
        if link is None:
            rejected.append(line)
        elif link not in seen:
            seen.add(link)
            links.append(link)
    return links, rejected
