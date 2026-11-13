"""외부 7-Zip은 가짜 실행파일로 대체. 사전 가드 실패 시 x 호출 자체가 없어야 한다."""
import base64
import io
import json
import os
import sys
import zipfile

import pytest

from app import intake


RAR = b"Rar!\x1a\x07\x01\x00" + b"fixture" * 100


def archive(entries):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as z:
        for name, content in entries:
            z.writestr(name, content)
    return buffer.getvalue()


def member(name="folder/한글.txt", data=b"hello", **metadata):
    return {"Path": name, "Size": str(len(data)), "Packed Size": str(max(1, len(data))),
            "Attributes": "A_ -rw-r--r--", "Encrypted": "-", **metadata,
            "content": base64.b64encode(data).decode()}


@pytest.fixture
def fake_7z(tmp_path, monkeypatch):
    script = tmp_path / "fake 7zz"
    config = tmp_path / "config.json"
    trace = tmp_path / "calls.jsonl"
    script.write_text(f"#!{sys.executable}\n" + '''import base64, json, pathlib, sys, time
root = pathlib.Path(__file__).parent
cfg = json.loads((root / "config.json").read_text())
args = sys.argv[1:]
with (root / "calls.jsonl").open("a") as log:
    log.write(json.dumps(args) + "\\n")
assert sys.stdin.read() == "", "stdin must be closed: no password prompt"
assert "-p" in args and "--" in args
time.sleep(cfg.get("delay_" + args[0], 0))
if cfg.get("exit_" + args[0]):
    sys.exit(cfg["exit_" + args[0]])
if args[0] == "l":
    assert "-slt" in args and "-sccUTF-8" in args
    if "listing" in cfg:
        print(cfg["listing"])
    else:
        print("7-Zip fixture\\n\\n--\\nPath = source.rar\\nType = Rar5\\n\\n----------")
        for record in cfg["members"]:
            for key, value in record.items():
                if key != "content":
                    print(key + " = " + value)
            print()
elif args[0] == "x":
    assert "-so" in args and "-spd" in args and "-r-" in args
    record = next(r for r in cfg["members"] if r["Path"] == args[-1])
    sys.stdout.buffer.write(base64.b64decode(record["content"]))
else:
    sys.exit(2)
''')
    script.chmod(0o700)
    monkeypatch.setenv("GLHAC_7Z_BIN", str(script))

    def unsupported(data):
        raise intake._NativeArchiveError("압축을 열 수 없습니다")

    monkeypatch.setattr(intake, "_native_archive_entries", unsupported)

    def configure(members=None, **options):
        config.write_text(json.dumps({"members": members if members is not None else [member()], **options}))
        return script

    def calls():
        return [json.loads(line) for line in trace.read_text().splitlines()] if trace.exists() else []

    configure.calls = calls
    configure()
    return configure


def test_listing_and_literal_member_extraction(fake_7z):
    files = [member(), member("folder/a[1]*?.txt", b"literal"), member("-option.txt", b"dash")]
    fake_7z(files)
    assert intake.extract_zip(RAR) == [(r["Path"], base64.b64decode(r["content"])) for r in files]
    calls = fake_7z.calls()
    assert [args[0] for args in calls] == ["l", "x", "x", "x"]
    assert not os.path.exists(calls[0][-1]), "원본 임시파일도 회수해야 한다"


@pytest.mark.parametrize("name", ["../escape.txt", "/escape.txt", "C:\\escape.txt", "safe/../../escape.txt",
                                 "\\\\server\\share.txt", "file.txt:stream", "@list.txt", "safe\\..\\escape.txt"])
def test_rejects_path_escape_before_any_extraction(fake_7z, name):
    fake_7z([member("good.txt"), member(name)])
    with pytest.raises(intake.ArchiveError, match="경로"):
        intake.extract_zip(RAR)
    assert [args[0] for args in fake_7z.calls()] == ["l"]


@pytest.mark.parametrize("metadata", [
    {"Symbolic Link": "../../outside"}, {"Hard Link": "other.txt"},
    {"Symbolic Link": "0"}, {"Hard Link": "-"},
    {"Copy Link": "other.txt"}, {"Mode": "lrwxrwxrwx"}, {"Attributes": "AL"},
    {"Attributes": "Ad"},
    {"Attributes": "A_ lrwxrwxrwx"}, {"Attributes": "A_ prw-r--r--"},
    {"Reparse Point": "+"}, {"Alternate Stream": "+"}, {"Anti": "+"},
    {"Encrypted": "+"}, {"Size": "-1"}, {"Size": "unknown"},
])
def test_rejects_links_special_encrypted_and_invalid_sizes(fake_7z, metadata):
    fake_7z([member("good.txt"), member("bad.txt", **metadata)])
    with pytest.raises(intake.ArchiveError):
        intake.extract_zip(RAR)
    assert [args[0] for args in fake_7z.calls()] == ["l"]


@pytest.mark.parametrize("guard", ["entries", "single", "total", "ratio", "solid_ratio", "shared_entries", "shared_bytes"])
def test_all_size_budgets_are_checked_before_extraction(fake_7z, monkeypatch, guard):
    raw = RAR
    records = [member("first.txt", b"first"), member("second.txt", b"second")]
    if guard == "entries":
        monkeypatch.setattr(intake, "ZIP_MAX_ENTRIES", 1)
    elif guard == "single":
        records[-1]["Size"] = str(intake.ZIP_MAX_SINGLE_FILE + 1)
        records[-1]["Packed Size"] = ""
    elif guard == "total":
        monkeypatch.setattr(intake, "ZIP_MAX_UNCOMPRESSED", 10)
    elif guard == "ratio":
        records[-1].update(Size="101", **{"Packed Size": "1"})
    elif guard == "solid_ratio":
        records[-1].update(Size=str(len(RAR) * 101), **{"Packed Size": ""})
    elif guard == "shared_entries":
        monkeypatch.setattr(intake, "ZIP_MAX_ENTRIES", 2)
        raw = archive([("inner.rar", RAR)])
    elif guard == "shared_bytes":
        monkeypatch.setattr(intake, "ZIP_MAX_UNCOMPRESSED", len(RAR) + 10)
        raw = archive([("inner.rar", RAR)])
    fake_7z(records)
    with pytest.raises(intake.ArchiveError):
        intake.extract_zip(raw)
    assert [args[0] for args in fake_7z.calls()] == ["l"]


def test_metadata_unsupported_and_depth_skips_do_not_extract(fake_7z):
    fake_7z([member(".DS_Store"), member("program.exe"), member("third.zip"), member("ok.txt", b"ok")])
    files = intake.extract_zip(archive([("inner.rar", RAR)]))
    assert files == [("inner.rar/ok.txt", b"ok")]
    assert files.skipped_files == [{"filename": "inner.rar/.DS_Store", "reason": "METADATA"},
                                   {"filename": "inner.rar/program.exe", "reason": "UNSUPPORTED_TYPE"},
                                   {"filename": "inner.rar/third.zip", "reason": "DEPTH_LIMIT"}]
    assert [args[-1] for args in fake_7z.calls() if args[0] == "x"] == ["ok.txt"]


def test_directories_empty_solid_sizes_and_crlf_listing(fake_7z):
    records = [member("folder", b"", Folder="+", Size="", Attributes="RD", **{"Packed Size": ""}),
               member("folder/one.txt", b"one", **{"Packed Size": ""}),
               member("folder/two.txt", b"two", **{"Packed Size": ""})]
    listing = "header\r\n----------\r\n" + "\r\n\r\n".join(
        "\r\n".join(key + " = " + value for key, value in record.items() if key != "content") for record in records)
    fake_7z(records, listing=listing)
    assert intake.extract_zip(RAR) == [("folder/one.txt", b"one"), ("folder/two.txt", b"two")]
    assert [a[-1] for a in fake_7z.calls() if a[0] == "x"] == ["folder/one.txt", "folder/two.txt"]


def test_nested_zip_uses_existing_content_and_path_validator(fake_7z):
    fake_7z([member("inner.zip", archive([("good.txt", b"good")]))])
    assert intake.extract_zip(RAR) == [("inner.zip/good.txt", b"good")]
    fake_7z([member("inner.zip", archive([("../bad.txt", b"bad")]))])
    with pytest.raises(intake.ArchiveError, match="경로"):
        intake.extract_zip(RAR)


@pytest.mark.parametrize("stage", ["l", "x"])
def test_timeout_kills_and_reaps_process(fake_7z, monkeypatch, stage):
    fake_7z(**{"delay_" + stage: 5})
    monkeypatch.setattr(intake, "_SEVEN_ZIP_TIMEOUT", 1.0)
    with pytest.raises(intake.ArchiveError, match="시간"):
        intake.extract_zip(RAR)
    calls = fake_7z.calls()
    assert [args[0] for args in calls] == (["l"] if stage == "l" else ["l", "x"])
    assert not os.path.exists(calls[0][-1])


@pytest.mark.parametrize("size", ["1", "100"])
def test_real_output_must_equal_declared_size(fake_7z, size):
    fake_7z([member(Size=size, **{"Packed Size": "100"})])
    with pytest.raises(intake.ArchiveError, match="크기"):
        intake.extract_zip(RAR)


@pytest.mark.parametrize("listing", ["bad", "header\n----------\nPath = a.txt\nSize = 1\nSize = 2\n",
                                     "header\n----------\nPath = a.txt\nSize = 1\nforged line\n"])
def test_malformed_listing_is_rejected(fake_7z, listing):
    fake_7z(listing=listing)
    with pytest.raises(intake.ArchiveError, match="목록"):
        intake.extract_zip(RAR)
    assert [args[0] for args in fake_7z.calls()] == ["l"]


def test_duplicate_normalized_path_is_rejected(fake_7z):
    fake_7z([member("same.txt"), member("./same.txt")])
    with pytest.raises(intake.ArchiveError, match="중복"):
        intake.extract_zip(RAR)
    assert [args[0] for args in fake_7z.calls()] == ["l"]


def test_partial_libarchive_failure_rolls_back_files_skips_and_budgets(fake_7z, monkeypatch):
    def partial(data):
        yield ".DS_Store", 0, False, lambda _: b""
        yield "file.txt", 5, False, lambda _: b"hello"
        raise intake._NativeArchiveError("압축 헤더가 손상되었습니다")
    monkeypatch.setattr(intake, "_native_archive_entries", partial)
    monkeypatch.setattr(intake, "ZIP_MAX_UNCOMPRESSED", 5)
    fake_7z([member("file.txt", b"hello")])
    result = intake.extract_zip(RAR)
    assert result == [("file.txt", b"hello")]
    assert result.skipped_files == []


def test_safety_guard_failure_never_falls_back(fake_7z, monkeypatch):
    def unsafe(data):
        raise intake.ArchiveError("압축 링크/특수 파일은 허용하지 않습니다")
    monkeypatch.setattr(intake, "_native_archive_entries", unsafe)
    with pytest.raises(intake.ArchiveError, match="링크"):
        intake.extract_zip(RAR)
    assert fake_7z.calls() == []


def test_missing_binary_preserves_original_archive_error(monkeypatch):
    import shutil
    monkeypatch.setattr(shutil, "which", lambda _: None)
    monkeypatch.delenv("GLHAC_7Z_BIN", raising=False)
    original = intake._NativeArchiveError("RAR·7z 해제 라이브러리(libarchive)가 없습니다")
    def fail(data):
        raise original
    monkeypatch.setattr(intake, "_native_archive_entries", fail)
    with pytest.raises(intake.ArchiveError) as raised:
        intake.extract_zip(RAR)
    assert raised.value is original


def test_binary_discovery_order(monkeypatch):
    import shutil
    calls = []
    monkeypatch.delenv("GLHAC_7Z_BIN", raising=False)
    monkeypatch.setattr(shutil, "which", lambda name: calls.append(name) or ("/tool/7z" if name == "7z" else None))
    assert intake._seven_zip_binary() == "/tool/7z"
    assert calls == ["7zz", "7z"]
    calls.clear()
    monkeypatch.setenv("GLHAC_7Z_BIN", "/chosen/7zz")
    assert intake._seven_zip_binary() is None
    assert calls == ["/chosen/7zz"]
