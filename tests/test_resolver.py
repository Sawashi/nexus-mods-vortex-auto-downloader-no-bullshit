import pytest

from nexus_dl.errors import Cancelled
from nexus_dl.resolver import Resolver

from .helpers import FakeApi, coll_ref, collection, file, mod, need, offsite, pin, ref


def keys(plan):
    return [item.ref.key for item in plan.items]


def resolve(api, roots, **options):
    return Resolver(api, **options).resolve([ref(r) if isinstance(r, int) else r for r in roots])


def test_single_mod_without_requirements():
    plan = resolve(FakeApi(mod(1)), [1])
    assert keys(plan) == ["cyberpunk2077:1"]
    assert plan.unavailable == [] and plan.externals == []


def test_requirements_come_before_the_mods_that_need_them():
    api = FakeApi(mod(1, [need(2)]), mod(2, [need(3)]), mod(3))
    assert keys(resolve(api, [1])) == ["cyberpunk2077:3", "cyberpunk2077:2", "cyberpunk2077:1"]


def test_shared_requirement_is_planned_and_fetched_once():
    api = FakeApi(mod(1, [need(2), need(3)]), mod(2, [need(3)]), mod(3))
    plan = resolve(api, [1])
    assert keys(plan) == ["cyberpunk2077:3", "cyberpunk2077:2", "cyberpunk2077:1"]
    assert api.calls.count("cyberpunk2077:3") == 1
    three = plan.items[0]
    assert sorted(r.mod_id for r in three.required_by) == [1, 2]


def test_two_roots_share_requirements():
    api = FakeApi(mod(1, [need(9)]), mod(2, [need(9)]), mod(9))
    assert keys(resolve(api, [1, 2])) == ["cyberpunk2077:9", "cyberpunk2077:1", "cyberpunk2077:2"]


def test_requirement_cycle_terminates():
    api = FakeApi(mod(1, [need(2)]), mod(2, [need(1)]))
    plan = resolve(api, [1])
    assert sorted(keys(plan)) == ["cyberpunk2077:1", "cyberpunk2077:2"]
    assert api.calls.count("cyberpunk2077:1") == 1


def test_offsite_requirements_are_reported_not_downloaded():
    api = FakeApi(mod(1, [offsite("Some tool", "https://github.com/a/b", "needed for X"), need(2)]), mod(2))
    plan = resolve(api, [1])
    assert keys(plan) == ["cyberpunk2077:2", "cyberpunk2077:1"]
    assert [(e.owner_name, e.name, e.url, e.notes) for e in plan.externals] == [
        ("mod 1", "Some tool", "https://github.com/a/b", "needed for X")
    ]


def test_unavailable_requirement_is_recorded_and_the_rest_continues():
    api = FakeApi(mod(1, [need(2), need(3)]), mod(3), missing=[2])
    plan = resolve(api, [1])
    assert keys(plan) == ["cyberpunk2077:3", "cyberpunk2077:1"]
    (problem,) = plan.unavailable
    assert problem.ref == ref(2) and problem.required_by == ref(1) and "not found" in problem.reason.lower()


def test_unavailable_mod_is_only_looked_up_once():
    api = FakeApi(mod(1, [need(2)]), mod(3, [need(2)]), missing=[2])
    plan = resolve(api, [1, 3])
    assert api.calls.count("cyberpunk2077:2") == 1
    assert len(plan.unavailable) == 1


def test_mod_with_nothing_to_download_is_unavailable():
    api = FakeApi(mod(1, [need(2)]), mod(2, files=[file(1, category="OLD_VERSION", primary=False)]))
    plan = resolve(api, [1])
    assert keys(plan) == ["cyberpunk2077:1"]
    assert plan.unavailable[0].reason == "no downloadable file"


def test_depth_limit():
    api = FakeApi(mod(1, [need(2)]), mod(2, [need(3)]), mod(3, [need(4)]), mod(4))
    plan = resolve(api, [1], max_depth=1)
    assert keys(plan) == ["cyberpunk2077:2", "cyberpunk2077:1"]
    assert any("Depth limit" in note for note in plan.notes)


def test_max_mods_limits_only_what_requirements_pull_in():
    api = FakeApi(mod(1, [need(2), need(3), need(4)]), mod(2), mod(3), mod(4))
    plan = resolve(api, [1], max_mods=2)
    assert len(plan.items) == 3  # the mod asked for + two discovered
    assert any("limit" in note for note in plan.notes)


def test_mods_asked_for_are_never_capped():
    api = FakeApi(*(mod(i) for i in range(1, 6)))
    assert len(resolve(api, [1, 2, 3, 4, 5], max_mods=1).items) == 5


def test_requirements_can_be_switched_off():
    api = FakeApi(mod(1, [need(2), offsite("x")]), mod(2))
    plan = resolve(api, [1], include_requirements=False)
    assert keys(plan) == ["cyberpunk2077:1"]
    assert api.calls == ["cyberpunk2077:1"]


def test_dlc_requirements_are_collected():
    plan = resolve(FakeApi(mod(1, dlc=["Phantom Liberty"])), [1])
    assert plan.dlc == [("mod 1", "Phantom Liberty")]


def test_totals():
    api = FakeApi(mod(1, [need(2)], files=[file(1, size=300)]), mod(2, files=[file(2, size=None)]))
    plan = resolve(api, [1])
    assert plan.file_count == 2 and plan.total_bytes == 300


def test_stop_request_cancels_resolution():
    api = FakeApi(mod(1, [need(2)]), mod(2))
    resolver = Resolver(api, should_stop=lambda: True)
    with pytest.raises(Cancelled):
        resolver.resolve([ref(1)])


# -- collections ----------------------------------------------------------------------------------


def test_collection_downloads_the_exact_pinned_files_in_listed_order():
    api = FakeApi(collections=[collection("abc", pin(5, 51), pin(3, 31), pin(9, 91), name="My pack", revision=4)])
    plan = resolve(api, [coll_ref("abc")])
    assert keys(plan) == ["cyberpunk2077:5", "cyberpunk2077:3", "cyberpunk2077:9"]
    assert [i.files[0].file_id for i in plan.items] == [51, 31, 91]
    (summary,) = plan.collections
    assert (summary.name, summary.revision, [r.mod_id for r in summary.mods]) == ("My pack", 4, [5, 3, 9])


def test_a_collection_needs_no_per_mod_lookups_and_does_not_follow_requirements():
    api = FakeApi(mod(1, [need(2)]), mod(2), collections=[collection("abc", pin(1))])
    plan = resolve(api, [coll_ref("abc")])
    assert keys(plan) == ["cyberpunk2077:1"] and api.calls == [] and api.collection_calls == ["abc"]


def test_a_collection_may_list_several_files_of_one_mod():
    api = FakeApi(collections=[collection("abc", pin(1, 11), pin(1, 12), pin(1, 11))])
    plan = resolve(api, [coll_ref("abc")])
    assert keys(plan) == ["cyberpunk2077:1"]
    assert [f.file_id for f in plan.items[0].files] == [11, 12]
    assert [r.mod_id for r in plan.collections[0].mods] == [1]


def test_optional_entries_are_marked_and_can_be_skipped():
    api = FakeApi(collections=[collection("abc", pin(1), pin(2, optional=True))])
    kept = resolve(api, [coll_ref("abc")])
    assert keys(kept) == ["cyberpunk2077:1", "cyberpunk2077:2"] and kept.items[1].optional and not kept.items[0].optional
    skipped = resolve(api, [coll_ref("abc")], skip_optional=True)
    assert keys(skipped) == ["cyberpunk2077:1"] and skipped.collections[0].skipped_optional == 1


def test_collection_external_resources_and_missing_files_are_reported():
    api = FakeApi(collections=[collection(
        "abc", pin(1), name="Pack",
        externals=[("Texture pack", "https://example.org/tex", "browse", "1.2", False)],
        missing=["file 77 is no longer on Nexus"],
    )])
    plan = resolve(api, [coll_ref("abc")])
    (external,) = plan.externals
    assert (external.owner_name, external.name, external.url, external.notes) == (
        "Pack", "Texture pack", "https://example.org/tex", "browse 1.2")
    assert [(p.ref, p.reason) for p in plan.unavailable] == [(coll_ref("abc"), "file 77 is no longer on Nexus")]


def test_unavailable_collection_does_not_stop_the_rest():
    api = FakeApi(mod(1))
    plan = resolve(api, [coll_ref("nope"), 1])
    assert keys(plan) == ["cyberpunk2077:1"] and plan.unavailable[0].ref == coll_ref("nope")


def test_unpublished_mod_inside_a_collection_is_unavailable():
    api = FakeApi(collections=[collection("abc", pin(1), pin(2, status="hidden"))])
    plan = resolve(api, [coll_ref("abc")])
    assert keys(plan) == ["cyberpunk2077:1"]
    assert plan.unavailable[0].ref == ref(2) and "hidden" in plan.unavailable[0].reason


def test_collection_members_are_not_capped():
    api = FakeApi(collections=[collection("abc", *(pin(i) for i in range(1, 8)))])
    assert len(resolve(api, [coll_ref("abc")], max_mods=1).items) == 7


def test_pinned_file_wins_when_the_same_mod_is_also_a_requirement():
    api = FakeApi(mod(5, [need(1)]), mod(1), collections=[collection("abc", pin(1, 99))])
    plan = resolve(api, [5, coll_ref("abc")])
    assert keys(plan) == ["cyberpunk2077:1", "cyberpunk2077:5"]  # the collection is planned first
    assert [f.file_id for f in plan.items[0].files] == [99]
    assert api.calls == ["cyberpunk2077:5"]  # mod 1 never needed a lookup of its own
    assert ref(5) in plan.items[0].required_by
