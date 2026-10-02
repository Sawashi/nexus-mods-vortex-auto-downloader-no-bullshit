"""Builders and a fake API shared by the tests."""
from __future__ import annotations

from nexus_dl.nexus_api import (
    CollectionInfo,
    CollectionMod,
    ExternalResource,
    FileInfo,
    ModInfo,
    ModUnavailable,
    Requirement,
)
from nexus_dl.urls import CollectionRef, ModRef

GAME = "cyberpunk2077"


def ref(mod_id: int) -> ModRef:
    return ModRef(GAME, mod_id)


def file(
    file_id: int = 1,
    *,
    category: str = "MAIN",
    primary: bool = True,
    date: int = 100,
    size: int | None = 1000,
    name: str = "Main file",
    version: str = "1.0",
) -> FileInfo:
    return FileInfo(file_id, name, version, category, date, size, primary, False)


def need(mod_id: int, name: str = "") -> Requirement:
    return Requirement(name or f"mod {mod_id}", "", "", ref(mod_id))


def offsite(name: str, url: str = "https://github.com/example/tool", notes: str = "") -> Requirement:
    return Requirement(name, notes, url, None)


def mod(mod_id: int, requires=(), *, files=None, name: str = "", dlc=()) -> ModInfo:
    return ModInfo(
        ref=ref(mod_id),
        name=name or f"mod {mod_id}",
        version="1.0",
        adult=False,
        requirements=list(requires),
        dlc=list(dlc),
        files=[file(mod_id * 10)] if files is None else files,
    )


def pin(
    mod_id: int,
    file_id: int | None = None,
    *,
    optional: bool = False,
    name: str = "",
    status: str = "published",
    size: int | None = 500,
) -> CollectionMod:
    """One collection entry: the exact file its curator chose (not the mod's primary file)."""
    pinned = file(file_id or mod_id * 10 + 1, primary=False, size=size, name=f"Pinned {mod_id}", version="2.0")
    return CollectionMod(ref(mod_id), name or f"mod {mod_id}", status, pinned, optional)


def collection(
    slug: str,
    *mods: CollectionMod,
    name: str = "",
    revision: int = 1,
    externals=(),
    missing=(),
    size: int | None = None,
) -> CollectionInfo:
    return CollectionInfo(
        name=name or f"Collection {slug}",
        slug=slug,
        revision=revision,
        adult=False,
        total_size=size,
        mods=list(mods),
        externals=[ExternalResource(*e) if isinstance(e, tuple) else e for e in externals],
        missing=list(missing),
    )


def coll_ref(slug: str, revision: int | None = None) -> CollectionRef:
    return CollectionRef(GAME, slug, revision)


class FakeApi:
    """Serves ModInfo / CollectionInfo objects (or raises ModUnavailable) by key."""

    def __init__(self, *mods: ModInfo, collections=(), missing=()):
        self.mods = {m.ref.key: m for m in mods}
        self.collections = {c.slug: c for c in collections}
        self.missing = {ref(i).key for i in missing}
        self.calls: list[str] = []
        self.collection_calls: list[str] = []

    def mod_info(self, r: ModRef) -> ModInfo:
        self.calls.append(r.key)
        if r.key in self.missing or r.key not in self.mods:
            raise ModUnavailable("Mod not found")
        return self.mods[r.key]

    def collection_info(self, r: CollectionRef) -> CollectionInfo:
        self.collection_calls.append(r.slug)
        if r.slug not in self.collections:
            raise ModUnavailable("Collection not found")
        return self.collections[r.slug]
