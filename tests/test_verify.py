import zipfile
import zlib

import pytest

from nexus_dl.verify import check_file

from .helpers import make_zip, truncate


def test_a_good_zip_passes_the_cheap_and_the_thorough_check(tmp_path):
    path = make_zip(tmp_path / "mod.zip")
    assert check_file(path) is None
    assert check_file(path, path.stat().st_size, deep=True) is None


def test_an_empty_file_is_corrupt(tmp_path):
    path = tmp_path / "mod.zip"
    path.write_bytes(b"")
    assert check_file(path) == "the file is empty"


def test_a_missing_file_cannot_be_read(tmp_path):
    assert check_file(tmp_path / "nope.zip").startswith("it cannot be read")


def test_the_size_must_match_when_it_is_known(tmp_path):
    path = make_zip(tmp_path / "mod.zip")
    size = path.stat().st_size
    assert check_file(path, size) is None
    assert f"should have {size + 7:,}" in check_file(path, size + 7)


def test_a_truncated_zip_is_caught_without_reading_all_of_it(tmp_path):
    path = make_zip(tmp_path / "mod.zip")
    truncate(path, 0.5)  # an interrupted download: the directory at the end of the file is missing
    assert "the zip is damaged" in check_file(path)


def test_garbage_under_a_zip_name_is_corrupt(tmp_path):
    path = tmp_path / "mod.zip"
    path.write_bytes(b"this is not an archive at all")
    assert "the zip is damaged" in check_file(path)


def test_damaged_data_inside_a_zip_needs_the_thorough_check(tmp_path):
    path = make_zip(tmp_path / "mod.zip", {"big.bin": b"A" * 5000}, stored=True)
    data = bytearray(path.read_bytes())
    data[data.index(b"AAAA") + 100] ^= 0xFF  # flip one byte of the stored data: the structure stays intact
    path.write_bytes(bytes(data))
    assert check_file(path) is None  # the cheap check used on every rerun does not read the data
    assert "'big.bin' inside the zip is damaged" in check_file(path, deep=True)


def test_a_rar_or_7z_archive_saved_under_a_zip_name_is_accepted(tmp_path):
    path = tmp_path / "mod.zip"
    path.write_bytes(b"Rar!\x1a\x07\x01\x00" + b"x" * 100)
    assert check_file(path, deep=True) is None
    path.write_bytes(b"7z\xbc\xaf\x27\x1c\x00\x04" + b"x" * 100)
    assert check_file(path, deep=True) is None


@pytest.mark.parametrize("name, good, bad", [
    ("a.rar", b"Rar!\x1a\x07\x01\x00" + b"x" * 50, b"PK\x03\x04" + b"x" * 50),
    ("a.7z", b"7z\xbc\xaf\x27\x1c\x00\x04" + b"x" * 50, b"Rar!\x1a\x07\x00" + b"x" * 50),
])
def test_rar_and_7z_files_must_start_with_their_signature(tmp_path, name, good, bad):
    path = tmp_path / name
    path.write_bytes(good)
    assert check_file(path) is None
    path.write_bytes(bad)
    assert "not a valid" in check_file(path)


def test_other_kinds_of_file_only_get_the_size_check(tmp_path):
    path = tmp_path / "plugin.dll"
    path.write_bytes(b"anything")
    assert check_file(path) is None
    assert check_file(path, 99) is not None


def test_an_encrypted_zip_is_not_blamed_for_crcs_that_cannot_be_tested(tmp_path, monkeypatch):
    path = make_zip(tmp_path / "mod.zip")
    original = zipfile.ZipFile.infolist

    def encrypted(self):
        infos = original(self)
        for info in infos:
            info.flag_bits |= 0x1
        return infos

    monkeypatch.setattr(zipfile.ZipFile, "infolist", encrypted)
    monkeypatch.setattr(zipfile.ZipFile, "testzip", lambda self: pytest.fail("must not test an encrypted zip"))
    assert check_file(path, deep=True) is None


def test_a_compression_python_cannot_read_gives_no_verdict(tmp_path, monkeypatch):
    path = make_zip(tmp_path / "mod.zip")
    for error in (NotImplementedError("compression type 99"), RuntimeError("password required")):
        monkeypatch.setattr(zipfile.ZipFile, "testzip", lambda self, error=error: (_ for _ in ()).throw(error))
        assert check_file(path, deep=True) is None


def test_a_broken_compressed_stream_is_corrupt(tmp_path, monkeypatch):
    path = make_zip(tmp_path / "mod.zip")
    monkeypatch.setattr(zipfile.ZipFile, "testzip", lambda self: (_ for _ in ()).throw(zlib.error("invalid stored block")))
    assert "the zip is damaged" in check_file(path, deep=True)
