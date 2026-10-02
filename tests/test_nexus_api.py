import pytest
import requests

from nexus_dl import __version__
from nexus_dl.budget import RequestBudget
from nexus_dl.nexus_api import ModUnavailable, NexusApi, NexusApiError
from nexus_dl.urls import CollectionRef, ModRef


class FakeResponse:
    def __init__(self, status=200, payload=None, headers=None, text=""):
        self.status_code, self._payload, self.headers, self.text = status, payload, headers or {}, text

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class FakeSession:
    def __init__(self, *responses):
        self.headers = {}
        self.responses = list(responses)
        self.sent = []

    def post(self, url, json=None, timeout=None):
        self.sent.append(json)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def ok(data):
    return FakeResponse(200, {"data": data})


def api(*responses, **options):
    sleeps = []
    session = FakeSession(*responses)
    client = NexusApi(session=session, sleep=sleeps.append, **options)
    return client, session, sleeps


# -- transport ------------------------------------------------------------------------------------


def test_every_request_identifies_the_app_as_nexus_asks():
    client, session, _ = api()
    assert session.headers["Application-Name"] == "NexusAutoDownloader"
    assert session.headers["Application-Version"] == __version__
    assert session.headers["User-Agent"].startswith("NexusAutoDownloader/")
    assert "apikey" not in {name.lower() for name in session.headers}  # anonymous: no personal API key


def test_server_trouble_is_retried_with_backoff_and_retry_after():
    client, session, sleeps = api(
        FakeResponse(503), FakeResponse(429, headers={"Retry-After": "7"}), ok({"game": {"id": 3333, "domainName": "x"}})
    )
    assert client.game_id("x") == 3333
    assert sleeps == [1.0, 7.0] and len(session.sent) == 3


def test_gives_up_after_the_retries():
    client, _, sleeps = api(*[FakeResponse(500)] * 3, retries=2)
    with pytest.raises(NexusApiError, match="gave up after 3 attempts"):
        client.game_id("x")
    assert len(sleeps) == 2


def test_network_errors_are_retried():
    client, _, _ = api(requests.ConnectionError("down"), ok({"game": {"id": 1, "domainName": "x"}}))
    assert client.game_id("x") == 1


def test_client_errors_are_not_retried():
    client, session, _ = api(FakeResponse(403, text="forbidden"))
    with pytest.raises(NexusApiError, match="HTTP 403"):
        client.game_id("x")
    assert len(session.sent) == 1


def test_every_http_attempt_is_counted_against_the_budget():
    budget = RequestBudget(clock=lambda: 1_000_000.0)
    client, _, _ = api(FakeResponse(502), ok({"game": {"id": 1, "domainName": "x"}}), budget=budget)
    client.game_id("x")
    assert budget.usage().hour_used == 2


def test_requests_wait_when_the_budget_is_used_up():
    clock = [1_000_000.0]
    budget = RequestBudget(hourly=1, clock=lambda: clock[0])
    budget.record(1)
    waits = []

    def sleep(seconds):
        waits.append(seconds)
        clock[0] += seconds

    client = NexusApi(session=FakeSession(ok({"game": {"id": 1, "domainName": "x"}})), sleep=sleep, budget=budget,
                      on_wait=lambda delay, which: waits.append(which))
    client.game_id("x")
    assert "hourly" in waits and sum(w for w in waits if not isinstance(w, str)) >= 3600


# -- mods -----------------------------------------------------------------------------------------


def _mod_payload(status="published", total=1, nodes=()):
    return {
        "mod": {
            "modId": 5, "gameId": 3333, "name": "Five", "version": "1.0", "status": status, "adultContent": False,
            "game": {"domainName": "cyberpunk2077", "id": 3333},
            "modRequirements": {
                "nexusRequirements": {"totalCount": total, "nodes": list(nodes)},
                "dlcRequirements": [{"notes": "needed", "gameExpansion": {"name": "Phantom Liberty"}}],
            },
        },
        "modFiles": [{"fileId": 9, "name": "Main", "version": "1", "category": "MAIN", "date": 5,
                      "sizeInBytes": "1234", "primary": 1, "requirementsAlert": 0}],
    }


def _node(mod_id, **extra):
    return {"modId": str(mod_id), "gameId": "3333", "modName": f"mod {mod_id}", "url": "", "notes": "",
            "externalRequirement": False, **extra}


def test_mod_info_reads_files_dlc_and_requirements():
    client, _, _ = api(
        ok({"game": {"id": 3333, "domainName": "cyberpunk2077"}}),
        ok(_mod_payload(nodes=[_node(7), _node(8, externalRequirement=True, modId="0", gameId="0",
                                                url="https://github.com/a/b", notes="tool")])),
    )
    info = client.mod_info(ModRef("cyberpunk2077", 5))
    assert info.name == "Five" and info.dlc == ["Phantom Liberty (needed)"]
    assert (info.files[0].file_id, info.files[0].size_bytes, info.files[0].primary) == (9, 1234, True)
    assert [r.ref for r in info.requirements] == [ModRef("cyberpunk2077", 7), None]
    assert info.requirements[1].url == "https://github.com/a/b" and info.requirements[1].notes == "tool"


def test_long_requirement_lists_are_paged():
    page_two = {"mod": {"modRequirements": {"nexusRequirements": {"totalCount": 3, "nodes": [_node(30)]}}}}
    client, session, _ = api(
        ok({"game": {"id": 3333, "domainName": "cyberpunk2077"}}),
        ok(_mod_payload(total=3, nodes=[_node(10), _node(20)])),
        ok(page_two),
    )
    info = client.mod_info(ModRef("cyberpunk2077", 5))
    assert [r.ref.mod_id for r in info.requirements] == [10, 20, 30]
    assert session.sent[-1]["variables"]["offset"] == 2  # the second page starts after the two already seen


def test_a_requirement_flagged_off_site_that_links_to_nexus_is_still_followed():
    node = _node(0, externalRequirement=True, url="https://www.nexusmods.com/cyberpunk2077/mods/99")
    client, _, _ = api(ok({"game": {"id": 3333, "domainName": "cyberpunk2077"}}), ok(_mod_payload(nodes=[node])))
    assert client.mod_info(ModRef("cyberpunk2077", 5)).requirements[0].ref == ModRef("cyberpunk2077", 99)


def test_hidden_or_missing_mods_are_unavailable():
    client, _, _ = api(ok({"game": {"id": 3333, "domainName": "cyberpunk2077"}}), ok(_mod_payload(status="hidden")))
    with pytest.raises(ModUnavailable, match="hidden"):
        client.mod_info(ModRef("cyberpunk2077", 5))
    client, _, _ = api(ok({"game": {"id": 3333, "domainName": "cyberpunk2077"}}),
                       FakeResponse(200, {"data": None, "errors": [{"message": "Mod not found"}, {"message": "Mod not found"}]}))
    with pytest.raises(ModUnavailable) as caught:
        client.mod_info(ModRef("cyberpunk2077", 5))
    assert str(caught.value) == "Mod not found"  # the same message twice is shown once


def test_the_expected_file_name_comes_from_the_uri_unless_it_is_only_a_storage_path():
    plain = NexusApi._file({"fileId": 1, "uri": "RED4ext-2380-1-30-0-1773082858.zip"})
    assert plain.expected_name == "RED4ext-2380-1-30-0-1773082858.zip"
    storage = NexusApi._file({"fileId": 2, "uri": "8f/ba/44/8fba44ab-cdc1-4a1f-8b11-327dac30e29f"})
    assert storage.expected_name is None  # newer files: the real name cannot be derived
    assert NexusApi._file({"fileId": 3}).expected_name is None


def test_unknown_game():
    client, _, _ = api(ok({"game": None}))
    with pytest.raises(ModUnavailable, match="unknown game"):
        client.game_id("nogame")


# -- collections ----------------------------------------------------------------------------------


def _entry(file_id, mod_id, *, optional=False, status="published", category="MAIN"):
    return {"fileId": file_id, "optional": optional,
            "file": {"fileId": file_id, "modId": mod_id, "name": f"file {file_id}", "version": "2", "category": category,
                     "date": 1, "sizeInBytes": "500", "primary": 0, "requirementsAlert": 0,
                     "mod": {"name": f"Mod {mod_id}", "status": status}}}


def _collection_payload(status="published", entries=(), externals=()):
    return {"collection": {"name": "Big Pack", "slug": "abc123"},
            "collectionRevision": {"revisionNumber": 3, "status": status, "adultContent": True, "totalSize": "9000",
                                   "modFiles": list(entries), "externalResources": list(externals)}}


def test_collection_info_pins_exact_files_and_reports_the_rest():
    entries = [_entry(11, 1), _entry(12, 2, optional=True, status="hidden"),
               {"fileId": 13, "optional": False, "file": None}, _entry(14, 4, category="ARCHIVED")]
    externals = [{"name": "Tex", "resourceType": "browse", "resourceUrl": "https://example.org", "optional": True,
                  "version": "1.2"}]
    client, session, _ = api(ok(_collection_payload(entries=entries, externals=externals)))
    info = client.collection_info(CollectionRef("cyberpunk2077", "abc123"))
    assert (info.name, info.revision, info.adult, info.total_size) == ("Big Pack", 3, True, 9000)
    assert [(m.ref.mod_id, m.file.file_id, m.optional, m.mod_status) for m in info.mods] == [
        (1, 11, False, "published"), (2, 12, True, "hidden"), (4, 14, False, "published")]
    assert info.mods[2].file.category == "ARCHIVED"  # a collection may pin an old version on purpose
    assert info.missing == ["file 13 is no longer on Nexus"]
    assert [(e.name, e.url, e.kind, e.version, e.optional) for e in info.externals] == [
        ("Tex", "https://example.org", "browse", "1.2", True)]
    assert "revision" not in session.sent[0]["variables"]  # latest published revision


def test_a_specific_revision_is_requested():
    client, session, _ = api(ok(_collection_payload()))
    client.collection_info(CollectionRef("cyberpunk2077", "abc123", 2))
    assert session.sent[0]["variables"] == {"slug": "abc123", "domain": "cyberpunk2077", "revision": 2}


def test_unknown_or_unpublished_collections_are_unavailable():
    client, _, _ = api(FakeResponse(200, {"data": None, "errors": [{"message": "Collection not found"}]}))
    with pytest.raises(ModUnavailable, match="Collection not found"):
        client.collection_info(CollectionRef("cyberpunk2077", "nope"))
    client, _, _ = api(ok(_collection_payload(status="draft")))
    with pytest.raises(ModUnavailable, match="draft"):
        client.collection_info(CollectionRef("cyberpunk2077", "abc123", 9))


def test_a_whole_collection_costs_one_request():
    budget = RequestBudget(clock=lambda: 1_000_000.0)
    client, _, _ = api(ok(_collection_payload(entries=[_entry(i, i) for i in range(500)])), budget=budget)
    assert len(client.collection_info(CollectionRef("cyberpunk2077", "abc123")).mods) == 500
    assert budget.usage().hour_used == 1
