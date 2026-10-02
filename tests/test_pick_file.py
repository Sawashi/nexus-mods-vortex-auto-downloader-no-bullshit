from nexus_dl.nexus_api import pick_files

from .helpers import file


def ids(files):
    return [f.file_id for f in files]


def test_primary_file_wins_over_newer_main():
    files = [file(1, primary=True, date=10), file(2, primary=False, date=20)]
    assert ids(pick_files(files)) == [1]


def test_newest_main_when_nothing_is_primary():
    files = [file(1, primary=False, date=10), file(2, primary=False, date=20)]
    assert ids(pick_files(files)) == [2]


def test_stale_files_are_never_picked_even_if_flagged_primary():
    files = [
        file(1, category="OLD_VERSION", primary=True, date=50),
        file(2, category="REMOVED", primary=True, date=60),
        file(3, category="ARCHIVED", primary=True, date=70),
        file(4, category="MAIN", primary=False, date=10),
    ]
    assert ids(pick_files(files)) == [4]


def test_falls_back_to_newest_non_stale_file_without_a_main():
    files = [
        file(1, category="OLD_VERSION", primary=False, date=99),
        file(2, category="OPTIONAL", primary=False, date=10),
        file(3, category="MISCELLANEOUS", primary=False, date=20),
    ]
    assert ids(pick_files(files)) == [3]


def test_nothing_downloadable():
    assert pick_files([]) == []
    assert pick_files([file(1, category="OLD_VERSION"), file(2, category="REMOVED")]) == []


def test_all_main_returns_every_main_file_oldest_first():
    files = [
        file(1, primary=False, date=30),
        file(2, primary=True, date=10),
        file(3, category="OPTIONAL", primary=False, date=40),
    ]
    assert ids(pick_files(files, all_main=True)) == [2, 1]


def test_all_main_includes_a_primary_file_outside_main():
    files = [file(1, category="MAIN", primary=False, date=10), file(2, category="UPDATE", primary=True, date=20)]
    assert ids(pick_files(files, all_main=True)) == [1, 2]
