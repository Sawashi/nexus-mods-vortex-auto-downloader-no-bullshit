"""Resolve the mods and collections a user asked for into an ordered download plan (requirements first)."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Protocol

from .errors import Cancelled
from .nexus_api import CollectionInfo, CollectionMod, FileInfo, ModInfo, ModUnavailable, pick_files
from .urls import CollectionRef, Link, ModRef


class ModSource(Protocol):
    def mod_info(self, ref: ModRef) -> ModInfo: ...
    def collection_info(self, ref: CollectionRef) -> CollectionInfo: ...


@dataclass
class PlanItem:
    ref: ModRef
    name: str
    version: str
    files: list[FileInfo]
    requires: list[ModRef] = field(default_factory=list)
    required_by: list[ModRef] = field(default_factory=list)
    depth: int = 0
    optional: bool = False  # every file of it is an optional entry of a collection


@dataclass(frozen=True)
class Unavailable:
    ref: ModRef | CollectionRef
    reason: str
    required_by: ModRef | None


@dataclass(frozen=True)
class External:
    """A requirement hosted outside Nexus. Reported to the user, never downloaded."""

    owner: ModRef | CollectionRef
    owner_name: str
    name: str
    url: str
    notes: str


@dataclass
class CollectionSummary:
    name: str
    slug: str
    revision: int
    url: str
    mods: list[ModRef] = field(default_factory=list)
    skipped_optional: int = 0
    size_bytes: int | None = None


@dataclass
class Plan:
    roots: list[ModRef]  # the mods the user pasted (collections are listed in `collections`)
    items: list[PlanItem] = field(default_factory=list)  # install order: requirements before dependants
    collections: list[CollectionSummary] = field(default_factory=list)
    unavailable: list[Unavailable] = field(default_factory=list)
    externals: list[External] = field(default_factory=list)
    dlc: list[tuple[str, str]] = field(default_factory=list)  # (mod name, expansion)
    notes: list[str] = field(default_factory=list)  # e.g. a depth/count limit was hit

    @property
    def file_count(self) -> int:
        return sum(len(i.files) for i in self.items)

    @property
    def total_bytes(self) -> int:
        return sum(f.size_bytes or 0 for i in self.items for f in i.files)

    def names(self) -> dict[str, str]:
        return {i.ref.key: i.name for i in self.items}


class Resolver:
    """`max_mods` limits how many mods are pulled in through requirements; the ones asked for are never capped."""

    def __init__(
        self,
        api: ModSource,
        *,
        include_requirements: bool = True,
        all_main: bool = False,
        skip_optional: bool = False,
        max_depth: int = 8,
        max_mods: int = 60,
        should_stop: Callable[[], bool] = lambda: False,
        log: Callable[[str], None] = lambda _msg: None,
    ):
        self.api = api
        self.include_requirements = include_requirements
        self.all_main = all_main
        self.skip_optional = skip_optional
        self.max_depth = max_depth
        self.max_mods = max_mods
        self.should_stop = should_stop
        self.log = log

    def resolve(self, links: list[Link]) -> Plan:
        plan = Plan(roots=[link for link in links if isinstance(link, ModRef)])
        self._items: dict[str, PlanItem] = {}
        self._order: list[str] = []
        self._failed: set[str] = set()
        self._discovered = 0
        # Collections first: they pin exact files, which then win over a mod's primary file.
        for link in links:
            if isinstance(link, CollectionRef):
                self._add_collection(link, plan)
        for root in plan.roots:
            self._visit(root, 0, None, plan)
        plan.items = [self._items[key] for key in self._order]
        return plan

    # -- collections ---------------------------------------------------------------------------

    def _add_collection(self, ref: CollectionRef, plan: Plan) -> None:
        if self.should_stop():
            raise Cancelled()
        self.log(f"Reading collection {ref.slug} ...")
        try:
            info = self.api.collection_info(ref)
        except ModUnavailable as exc:
            plan.unavailable.append(Unavailable(ref, str(exc), None))
            return
        summary = CollectionSummary(
            name=info.name,
            slug=info.slug,
            revision=info.revision,
            url=CollectionRef(ref.domain, info.slug, info.revision).url,
            size_bytes=info.total_size,
        )
        for reason in info.missing:
            plan.unavailable.append(Unavailable(ref, reason, None))
        for entry in info.mods:
            if entry.optional and self.skip_optional:
                summary.skipped_optional += 1
            elif entry.mod_status and entry.mod_status != "published":
                plan.unavailable.append(Unavailable(entry.ref, f"mod status is '{entry.mod_status}'", None))
            else:
                self._add_pinned(entry)
                if entry.ref not in summary.mods:
                    summary.mods.append(entry.ref)
        for resource in info.externals:
            if resource.optional and self.skip_optional:
                continue
            detail = " ".join(part for part in (resource.kind, resource.version) if part)
            plan.externals.append(External(ref, info.name, resource.name, resource.url, detail))
        plan.collections.append(summary)

    def _add_pinned(self, entry: CollectionMod) -> None:
        """Plan the exact file a collection chose. Collections already include what their mods need."""
        item = self._items.get(entry.ref.key)
        if item is None:
            item = PlanItem(entry.ref, entry.mod_name, entry.file.version, [entry.file], optional=entry.optional)
            self._items[entry.ref.key] = item
            self._order.append(entry.ref.key)
        elif entry.file not in item.files:  # a collection may list several files of one mod
            item.files.append(entry.file)
            item.optional = item.optional and entry.optional

    # -- mods and their requirements -----------------------------------------------------------

    def _visit(self, ref: ModRef, depth: int, parent: ModRef | None, plan: Plan) -> None:
        if self.should_stop():
            raise Cancelled()
        key = ref.key
        if key in self._items:  # already planned (or being planned: a requirement cycle)
            if parent is not None and parent not in self._items[key].required_by:
                self._items[key].required_by.append(parent)
            return
        if key in self._failed:
            return
        if parent is not None and self._discovered >= self.max_mods:
            message = f"Stopped following requirements after {self.max_mods} extra mods (limit)."
            if message not in plan.notes:
                plan.notes.append(message)
            return

        self.log(f"Looking up {ref.key} ...")
        try:
            info = self.api.mod_info(ref)
        except ModUnavailable as exc:
            self._failed.add(key)
            plan.unavailable.append(Unavailable(ref, str(exc), parent))
            return
        files = pick_files(info.files, all_main=self.all_main)
        if not files:
            self._failed.add(key)
            plan.unavailable.append(Unavailable(ref, "no downloadable file", parent))
            return

        item = PlanItem(ref, info.name, info.version, files, required_by=[parent] if parent else [], depth=depth)
        self._items[key] = item  # registered before recursing so cycles terminate
        if parent is not None:
            self._discovered += 1
        if self.include_requirements:
            self._follow_requirements(info, item, depth, plan)
        self._order.append(key)

    def _follow_requirements(self, info: ModInfo, item: PlanItem, depth: int, plan: Plan) -> None:
        for requirement in info.requirements:
            if requirement.ref is None:
                plan.externals.append(
                    External(info.ref, info.name, requirement.name, requirement.url, requirement.notes)
                )
                continue
            item.requires.append(requirement.ref)
            if depth + 1 > self.max_depth:
                message = f"Depth limit ({self.max_depth}) reached below '{info.name}'."
                if message not in plan.notes:
                    plan.notes.append(message)
                continue
            self._visit(requirement.ref, depth + 1, info.ref, plan)
        plan.dlc.extend((info.name, expansion) for expansion in info.dlc)
