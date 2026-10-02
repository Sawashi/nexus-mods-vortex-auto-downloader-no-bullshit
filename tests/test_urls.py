import pytest

from nexus_dl.urls import CollectionRef, ModRef, parse_collection_url, parse_link, parse_links, parse_mod_url


@pytest.mark.parametrize(
    "text",
    [
        "https://www.nexusmods.com/cyberpunk2077/mods/107",
        "https://www.nexusmods.com/cyberpunk2077/mods/107?tab=files",
        "https://www.nexusmods.com/cyberpunk2077/mods/107/",
        "https://www.nexusmods.com/cyberpunk2077/mods/107#description",
        "  http://nexusmods.com/CyberPunk2077/mods/107  ",
        "www.nexusmods.com/cyberpunk2077/mods/107",
        "https://www.nexusmods.com/games/cyberpunk2077/mods/107",
        "https://next.nexusmods.com/cyberpunk2077/mods/107",
    ],
)
def test_parses_mod_links(text):
    assert parse_mod_url(text) == ModRef("cyberpunk2077", 107)


@pytest.mark.parametrize(
    "text",
    [
        "",
        "https://www.nexusmods.com/games/cyberpunk2077",  # the game page, not a mod
        "https://www.nexusmods.com/cyberpunk2077/mods/",
        "https://www.nexusmods.com/cyberpunk2077/mods/abc",
        "https://example.com/cyberpunk2077/mods/107",
        "https://www.nexusmods.com.evil.example/cyberpunk2077/mods/107",
        "not a url",
    ],
)
def test_rejects_other_links(text):
    assert parse_mod_url(text) is None and parse_link(text) is None


@pytest.mark.parametrize(
    "text, revision",
    [
        ("https://www.nexusmods.com/games/cyberpunk2077/collections/a1wdcv", None),
        ("https://www.nexusmods.com/cyberpunk2077/collections/a1wdcv", None),
        ("https://www.nexusmods.com/cyberpunk2077/collections/a1wdcv/", None),
        ("https://www.nexusmods.com/cyberpunk2077/collections/a1wdcv?tab=mods", None),
        ("https://next.nexusmods.com/cyberpunk2077/collections/A1WDCV", None),
        ("https://www.nexusmods.com/games/cyberpunk2077/collections/a1wdcv/revisions/2", 2),
        ("www.nexusmods.com/cyberpunk2077/collections/a1wdcv/revisions/17", 17),
    ],
)
def test_parses_collection_links(text, revision):
    ref = parse_collection_url(text)
    assert ref is not None and ref.domain == "cyberpunk2077" and ref.slug.lower() == "a1wdcv"
    assert ref.revision == revision and parse_link(text) == ref


def test_collection_links_are_not_mod_links_and_vice_versa():
    assert parse_mod_url("https://www.nexusmods.com/cyberpunk2077/collections/a1wdcv") is None
    assert parse_collection_url("https://www.nexusmods.com/cyberpunk2077/mods/107") is None
    assert parse_collection_url("https://www.nexusmods.com/games/cyberpunk2077/collections") is None


def test_parse_links_mixes_mods_and_collections_dedupes_and_reports_rejects():
    text = """
    # my stuff
    https://www.nexusmods.com/cyberpunk2077/mods/107
    https://www.nexusmods.com/cyberpunk2077/mods/107?tab=files
    https://www.nexusmods.com/games/cyberpunk2077/collections/a1wdcv
    https://www.nexusmods.com/cyberpunk2077/collections/a1wdcv

    https://www.nexusmods.com/cyberpunk2077/mods/790
    https://www.nexusmods.com/games/cyberpunk2077
    """
    links, rejected = parse_links(text)
    assert links == [ModRef("cyberpunk2077", 107), CollectionRef("cyberpunk2077", "a1wdcv"), ModRef("cyberpunk2077", 790)]
    assert rejected == ["https://www.nexusmods.com/games/cyberpunk2077"]


def test_the_placeholder_text_is_not_a_link():
    pytest.importorskip("tkinter")
    from nexus_dl.app import PLACEHOLDER

    assert parse_links(PLACEHOLDER)[0] == []


def test_ref_builds_urls():
    ref = ModRef("cyberpunk2077", 107)
    assert ref.key == "cyberpunk2077:107"
    assert ref.url == "https://www.nexusmods.com/cyberpunk2077/mods/107"
    assert ref.files_url(5) == "https://www.nexusmods.com/cyberpunk2077/mods/107?tab=files&file_id=5"
    collection = CollectionRef("cyberpunk2077", "a1wdcv")
    assert collection.url == "https://www.nexusmods.com/cyberpunk2077/collections/a1wdcv"
    assert CollectionRef("cyberpunk2077", "a1wdcv", 2).url.endswith("/collections/a1wdcv/revisions/2")
