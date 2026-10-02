"""Read-only client for the Nexus Mods GraphQL v2 API (works anonymously, no API key needed)."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable

import requests

from . import __version__
from .budget import RequestBudget
from .urls import CollectionRef, ModRef, parse_mod_url

ENDPOINT = "https://api.nexusmods.com/v2/graphql"
APP_NAME = "NexusAutoDownloader"  # Nexus's API policy asks every app to identify itself
USER_AGENT = f"{APP_NAME}/{__version__} (personal use)"
REQUIREMENTS_PAGE = 50

# File categories that are never "the file to download".
STALE_CATEGORIES = {"OLD_VERSION", "REMOVED", "ARCHIVED"}

_REQUIREMENT_FIELDS = "modId gameId modName url notes externalRequirement"
_FILE_FIELDS = "fileId name uri version category date sizeInBytes primary requirementsAlert"

_MOD_QUERY = f"""
query ($modId: ID!, $gameId: ID!, $count: Int!) {{
  mod(modId: $modId, gameId: $gameId) {{
    modId gameId name version status adultContent
    game {{ domainName id }}
    modRequirements {{
      nexusRequirements(offset: 0, count: $count) {{ totalCount nodes {{ {_REQUIREMENT_FIELDS} }} }}
      dlcRequirements {{ notes gameExpansion {{ name }} }}
    }}
  }}
  modFiles(modId: $modId, gameId: $gameId) {{ {_FILE_FIELDS} }}
}}"""

_REQUIREMENTS_QUERY = f"""
query ($modId: ID!, $gameId: ID!, $offset: Int!, $count: Int!) {{
  mod(modId: $modId, gameId: $gameId) {{
    modRequirements {{
      nexusRequirements(offset: $offset, count: $count) {{ totalCount nodes {{ {_REQUIREMENT_FIELDS} }} }}
    }}
  }}
}}"""

# One request describes a whole collection: every pinned file with its name, size and category.
_COLLECTION_QUERY = f"""
query ($slug: String!, $domain: String!, $revision: Int) {{
  collection(slug: $slug, viewAdultContent: true, domainName: $domain) {{ name slug }}
  collectionRevision(slug: $slug, revision: $revision, viewAdultContent: true, domainName: $domain) {{
    revisionNumber status adultContent totalSize
    modFiles {{ fileId optional file {{ {_FILE_FIELDS} modId mod {{ name status }} }} }}
    externalResources {{ name resourceType resourceUrl optional version }}
  }}
}}"""

_GAME_BY_DOMAIN = "query ($d: String!) { game(domainName: $d) { id domainName } }"
_GAME_BY_ID = "query ($i: ID!) { game(id: $i) { id domainName } }"


class NexusApiError(Exception):
    """The API could not be reached or returned an unusable answer."""


class ModUnavailable(NexusApiError):
    """The mod or collection is hidden, removed, unpublished or has nothing to download."""


@dataclass(frozen=True)
class FileInfo:
    file_id: int
    name: str
    version: str
    category: str
    date: int
    size_bytes: int | None
    primary: bool
    requirements_alert: bool
    uri: str = ""

    @property
    def expected_name(self) -> str | None:
        """The file name Nexus saves this file under, when the API tells it (newer files only get a storage path)."""
        return self.uri if self.uri and "/" not in self.uri else None


@dataclass(frozen=True)
class Requirement:
    name: str
    notes: str
    url: str
    ref: ModRef | None  # None: off-site (or unresolvable), reported but never downloaded


@dataclass
class ModInfo:
    ref: ModRef
    name: str
    version: str
    adult: bool
    requirements: list[Requirement]
    dlc: list[str]
    files: list[FileInfo]


@dataclass(frozen=True)
class CollectionMod:
    """One entry of a collection: the exact file its curator chose."""

    ref: ModRef
    mod_name: str
    mod_status: str
    file: FileInfo
    optional: bool


@dataclass(frozen=True)
class ExternalResource:
    """Something a collection needs that is not hosted on Nexus."""

    name: str
    url: str
    kind: str
    version: str
    optional: bool


@dataclass
class CollectionInfo:
    name: str
    slug: str
    revision: int
    adult: bool
    total_size: int | None
    mods: list[CollectionMod]
    externals: list[ExternalResource]
    missing: list[str] = field(default_factory=list)  # entries whose file no longer exists on Nexus


def pick_files(files: list[FileInfo], *, all_main: bool = False) -> list[FileInfo]:
    """The file(s) a normal visitor would download: the author's primary file, else the newest MAIN one."""
    current = [f for f in files if f.category not in STALE_CATEGORIES]
    primary = [f for f in current if f.primary]
    mains = sorted((f for f in current if f.category == "MAIN"), key=lambda f: f.date)
    if all_main and mains:
        return sorted(mains + [f for f in primary if f not in mains], key=lambda f: f.date)
    if primary:
        return [max(primary, key=lambda f: f.date)]
    if mains:
        return [mains[-1]]
    return [max(current, key=lambda f: f.date)] if current else []


def _to_int(value, default=None):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _retry_after(response: requests.Response) -> float:
    return float(_to_int(response.headers.get("Retry-After"), 0) or 0)


class NexusApi:
    def __init__(
        self,
        *,
        session: requests.Session | None = None,
        sleep: Callable[[float], None] = time.sleep,
        retries: int = 4,
        budget: RequestBudget | None = None,
        on_wait: Callable[[float, str], None] | None = None,
    ):
        self.session = session or requests.Session()
        self.session.headers.update(
            {"User-Agent": USER_AGENT, "Application-Name": APP_NAME, "Application-Version": __version__}
        )
        self._sleep = sleep
        self._retries = retries
        self._budget = budget
        self._on_wait = on_wait
        self._domain_to_id: dict[str, int] = {}
        self._id_to_domain: dict[int, str] = {}

    # -- transport ---------------------------------------------------------------------------

    def _post(self, query: str, variables: dict) -> dict:
        delay = 1.0
        for attempt in range(self._retries + 1):
            if self._budget:  # every HTTP attempt counts, retries included
                self._budget.wait_for(1, self._sleep, self._on_wait)
                self._budget.record(1)
            try:
                response = self.session.post(
                    ENDPOINT, json={"query": query, "variables": variables}, timeout=30
                )
            except requests.RequestException as exc:
                problem = f"network error: {exc}"
            else:
                if response.status_code == 200:
                    try:
                        return response.json()
                    except ValueError as exc:
                        raise NexusApiError(f"invalid JSON from the API: {exc}") from exc
                problem = f"HTTP {response.status_code}"
                if response.status_code != 429 and response.status_code < 500:
                    raise NexusApiError(f"{problem}: {response.text[:200]}")
                delay = max(delay, _retry_after(response))
            if attempt == self._retries:
                raise NexusApiError(f"{problem} (gave up after {attempt + 1} attempts)")
            self._sleep(delay)
            delay = min(delay * 2, 30.0)
        raise AssertionError("unreachable")

    @staticmethod
    def _messages(payload: dict) -> str:
        messages = dict.fromkeys(e.get("message", "?") for e in payload.get("errors") or [])  # unique, in order
        return "; ".join(messages) or "no data"

    # -- games -------------------------------------------------------------------------------

    def _remember_game(self, domain: str, game_id: int) -> None:
        self._domain_to_id[domain.lower()] = game_id
        self._id_to_domain[game_id] = domain.lower()

    def game_id(self, domain: str) -> int:
        domain = domain.lower()
        if domain not in self._domain_to_id:
            game = ((self._post(_GAME_BY_DOMAIN, {"d": domain}).get("data")) or {}).get("game")
            if not game:
                raise ModUnavailable(f"unknown game '{domain}'")
            self._remember_game(game["domainName"], int(game["id"]))
        return self._domain_to_id[domain]

    def game_domain(self, game_id: int) -> str | None:
        if game_id not in self._id_to_domain:
            try:
                data = self._post(_GAME_BY_ID, {"i": str(game_id)}).get("data")
            except NexusApiError:
                return None
            game = (data or {}).get("game")
            if not game:
                return None
            self._remember_game(game["domainName"], int(game["id"]))
        return self._id_to_domain[game_id]

    # -- mods --------------------------------------------------------------------------------

    def mod_info(self, ref: ModRef) -> ModInfo:
        game_id = self.game_id(ref.domain)
        variables = {"modId": str(ref.mod_id), "gameId": str(game_id)}
        payload = self._post(_MOD_QUERY, {**variables, "count": REQUIREMENTS_PAGE})
        data = payload.get("data") or {}
        mod = data.get("mod")
        if not mod:
            raise ModUnavailable(self._messages(payload))
        if mod.get("game"):
            self._remember_game(mod["game"]["domainName"], int(mod["game"]["id"]))
        status = mod.get("status") or ""
        if status != "published":
            raise ModUnavailable(f"mod status is '{status}'")

        page = mod["modRequirements"]["nexusRequirements"]
        nodes = list(page["nodes"])
        while len(nodes) < page["totalCount"]:
            more = self._post(_REQUIREMENTS_QUERY, {**variables, "offset": len(nodes), "count": REQUIREMENTS_PAGE})
            extra = (((more.get("data") or {}).get("mod") or {}).get("modRequirements") or {}).get(
                "nexusRequirements", {}
            ).get("nodes")
            if not extra:
                break
            nodes.extend(extra)

        dlc = [
            f"{d['gameExpansion']['name']}" + (f" ({d['notes']})" if d.get("notes") else "")
            for d in mod["modRequirements"]["dlcRequirements"]
        ]
        return ModInfo(
            ref=ref,
            name=mod["name"],
            version=mod.get("version") or "",
            adult=bool(mod.get("adultContent")),
            requirements=[self._requirement(n) for n in nodes],
            dlc=dlc,
            files=[self._file(f) for f in data.get("modFiles") or []],
        )

    def _requirement(self, node: dict) -> Requirement:
        url = node.get("url") or ""
        ref = None
        if not node.get("externalRequirement"):
            mod_id, game_id = _to_int(node.get("modId")), _to_int(node.get("gameId"))
            domain = self.game_domain(game_id) if mod_id and game_id else None
            if domain:
                ref = ModRef(domain, mod_id)
        if ref is None and url:
            ref = parse_mod_url(url)  # flagged off-site, but really a Nexus mod page
        return Requirement(name=node.get("modName") or "", notes=node.get("notes") or "", url=url, ref=ref)

    @staticmethod
    def _file(raw: dict) -> FileInfo:
        return FileInfo(
            file_id=int(raw["fileId"]),
            name=raw.get("name") or "",
            version=raw.get("version") or "",
            category=raw.get("category") or "",
            date=_to_int(raw.get("date"), 0),
            size_bytes=_to_int(raw.get("sizeInBytes")),
            primary=bool(raw.get("primary")),
            requirements_alert=bool(raw.get("requirementsAlert")),
            uri=raw.get("uri") or "",
        )

    # -- collections -------------------------------------------------------------------------

    def collection_info(self, ref: CollectionRef) -> CollectionInfo:
        """The files of a collection revision (the latest published one unless `ref` names a revision)."""
        variables: dict = {"slug": ref.slug, "domain": ref.domain}
        if ref.revision is not None:
            variables["revision"] = ref.revision
        payload = self._post(_COLLECTION_QUERY, variables)
        data = payload.get("data") or {}
        collection, revision = data.get("collection"), data.get("collectionRevision")
        if not collection or not revision:
            raise ModUnavailable(self._messages(payload))
        status = revision.get("status") or ""
        if status != "published":
            raise ModUnavailable(f"collection revision status is '{status}'")

        mods: list[CollectionMod] = []
        missing: list[str] = []
        for entry in revision.get("modFiles") or []:
            raw = entry.get("file")
            if not raw:
                missing.append(f"file {entry.get('fileId')} is no longer on Nexus")
                continue
            mod = raw.get("mod") or {}
            mod_id = int(raw["modId"])
            mods.append(
                CollectionMod(
                    ref=ModRef(ref.domain, mod_id),
                    mod_name=mod.get("name") or f"mod {mod_id}",
                    mod_status=mod.get("status") or "",
                    file=self._file(raw),
                    optional=bool(entry.get("optional")),
                )
            )
        externals = [
            ExternalResource(
                name=item.get("name") or "",
                url=item.get("resourceUrl") or "",
                kind=item.get("resourceType") or "",
                version=item.get("version") or "",
                optional=bool(item.get("optional")),
            )
            for item in revision.get("externalResources") or []
        ]
        return CollectionInfo(
            name=collection.get("name") or ref.slug,
            slug=collection.get("slug") or ref.slug,
            revision=int(revision["revisionNumber"]),
            adult=bool(revision.get("adultContent")),
            total_size=_to_int(revision.get("totalSize")),
            mods=mods,
            externals=externals,
            missing=missing,
        )
