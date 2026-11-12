"""합성 파일만 사용: 중첩 압축의 전역 예산, 사전 검증, 빠른 문서 저장."""
import base64
import io
import stat
import threading
import time
import unicodedata
import zipfile

import fitz
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app import intake, models
import app.main as m


def pdf(text="A synthetic document for upload testing."):
    with fitz.open() as doc:
        doc.new_page().insert_text((72, 72), text)
        return doc.tobytes()


def archive(entries):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, raw in entries:
            z.writestr(name, raw)
    return buf.getvalue()


def test_nested_zip_and_uppercase_extension():
    raw = archive([("outer.pdf", pdf()), ("folder/자료.Zip", archive([("inner.pdf", pdf())]))])
    files = m._validate_archive_upload(raw)
    assert [name for name, _ in files] == ["outer.pdf", "folder/자료.Zip/inner.pdf"]
    assert files.skipped == 0


def test_depth_limit_reports_skipped_without_opening_third_archive():
    deepest = archive([("too-deep.pdf", pdf())])
    raw = archive([("inner.zip", archive([("ok.pdf", pdf()), ("third.Zip", deepest)]))])
    files = m._validate_archive_upload(raw)
    assert [name for name, _ in files] == ["inner.zip/ok.pdf"]
    assert files.skipped_files == [{"filename": "inner.zip/third.Zip", "reason": "DEPTH_LIMIT"}]


def test_uncompressed_budget_is_shared_across_nested_archives(monkeypatch):
    one = archive([("a.txt", b"a" * 100)])
    raw = archive([("one.zip", one), ("two.zip", one)])
    # 각각은 통과하지만 컨테이너 바이트 + 모든 내부 문서 합은 초과.
    monkeypatch.setattr(intake, "ZIP_MAX_UNCOMPRESSED", len(one) * 2 + 150)
    with pytest.raises(intake.ArchiveError, match="전체 합산"):
        intake.extract_zip(raw)


def test_entry_budget_is_shared_and_reports_overflow(monkeypatch):
    monkeypatch.setattr(intake, "ZIP_MAX_ENTRIES", 4)
    raw = archive([("one.zip", archive([("a.txt", b"a"), ("b.txt", b"b")])),
                   ("two.zip", archive([("c.txt", b"c")]))])
    files = intake.extract_zip(raw)
    assert len(files) == 2
    assert files.skipped_files == [{"filename": "two.zip/c.txt", "reason": "ENTRY_LIMIT"}]


def test_skips_metadata_and_unsupported_but_processes_documents(monkeypatch):
    raw = archive([(".DS_Store", b"meta"), ("__MACOSX/._doc.pdf", b"not a pdf"),
                   ("inner.zip", archive([("file.exe", b"program"), ("good.txt", b"ok")]))])
    files = m._validate_archive_upload(raw)
    assert [name for name, _ in files] == ["inner.zip/good.txt"]
    assert [s["reason"] for s in files.skipped_files] == ["METADATA", "METADATA", "UNSUPPORTED_TYPE"]
    monkeypatch.setattr(intake, "classify", lambda *a: {"doc_type": "other", "fields": {}})
    result = intake.intake_zip(raw, files=files)
    assert result["skipped_entries"] == 3
    assert result["skipped_files"] == files.skipped_files


@pytest.mark.parametrize("name,content", [("forged.pdf", b"not a pdf"), ("forged.Zip", b"not a zip")])
def test_content_mismatch_still_rejects_nested_archive(name, content):
    raw = archive([("good.pdf", pdf()), ("inner.zip", archive([(name, content)]))])
    with pytest.raises(HTTPException) as error:
        m._validate_archive_upload(raw)
    assert error.value.status_code == 415
    assert error.value.detail["code"] == "CONTENT_MISMATCH"


@pytest.mark.parametrize("name", ["../escape.txt", "/escape.txt", "C:\\escape.txt", "safe/../../escape.txt"])
def test_nested_path_guard(name):
    with pytest.raises(intake.ArchiveError, match="경로"):
        intake.extract_zip(archive([("inner.zip", archive([(name, b"x")]))]))


def test_nested_symlink_guard():
    info = zipfile.ZipInfo("link.txt")
    info.create_system = 3
    info.external_attr = (stat.S_IFLNK | 0o777) << 16
    with pytest.raises(intake.ArchiveError, match="링크"):
        intake.extract_zip(archive([("inner.zip", archive([(info, b"../../outside")]))]))


@pytest.mark.parametrize("encoding", ["utf-8", "cp949"])
def test_unflagged_korean_zip_names_and_nfc(encoding):
    class EncodedName(zipfile.ZipInfo):
        def _encodeFilenameFlags(self):
            return self.filename.encode(encoding), self.flag_bits & ~0x800
    name = "사업자등록증명.txt"
    stored = unicodedata.normalize("NFD", name) if encoding == "utf-8" else name
    files = intake.extract_zip(archive([(EncodedName(stored), b"text")]))
    assert files[0][0] == name


@pytest.mark.parametrize("ext,magic", [("RaR", b"Rar!\x1a\x07\x00"), ("7Z", b"7z\xbc\xaf\x27\x1c")])
def test_native_archives_share_limits_and_nested_dispatch(monkeypatch, ext, magic):
    monkeypatch.setattr(intake, "_native_archive_entries", lambda data: iter([
        ("doc.txt", 4, False, lambda size: b"text")]))
    assert intake.extract_zip(archive([("inner." + ext, magic)]))[0] == ("inner." + ext + "/doc.txt", b"text")
    monkeypatch.setattr(intake, "ZIP_MAX_UNCOMPRESSED", len(magic) + 3)
    with pytest.raises(intake.ArchiveError):
        intake.extract_zip(archive([("inner." + ext, magic)]))


@pytest.fixture
def case(monkeypatch):
    monkeypatch.setattr(m, "verify_enqueue", lambda did: False)
    monkeypatch.setattr(intake.ai_local, "llm_json", lambda *a, **kw: {"error": "LLM_UNAVAILABLE"})
    with TestClient(m.app) as client:
        token = client.post("/auth/login", json={"username": "applicant1", "password": "pw"}).json()["token"]
        headers = {"Authorization": "Bearer " + token}
        cid = client.post("/cases", headers=headers, json={"company_name": "Synthetic Upload"}).json()["case_id"]
        yield client, headers, cid
        if m._autoparse_q:
            m._autoparse_q.join()


def test_stream_returns_skips_and_rejects_mismatch_before_any_save(case):
    client, headers, cid = case
    bad = archive([("good.txt", b"hello"), ("inner.zip", archive([("bad.pdf", b"bad")]))])
    r = client.post(f"/cases/{cid}/intake-zip-stream", headers=headers,
                    json={"zip_b64": base64.b64encode(bad).decode()})
    assert r.status_code == 415
    assert client.get(f"/cases/{cid}/documents", headers=headers).json() == []
    good = archive([(".DS_Store", b"meta"), ("inner.Zip", archive([("good.txt", b"hello")]))])
    r = client.post(f"/cases/{cid}/intake-zip-stream", headers=headers,
                    json={"zip_b64": base64.b64encode(good).decode()})
    assert r.status_code == 200 and '"skipped_entries": 1' in r.text
    assert '"reason": "METADATA"' in r.text


def test_upload_returns_while_ocr_is_blocked_and_does_not_block_next_upload(case, monkeypatch):
    client, headers, cid = case
    entered, release = threading.Event(), threading.Event()
    calls = []
    def slow_parse(*args, **kw):
        calls.append(threading.current_thread().name)
        entered.set()
        assert release.wait(5), "upload waited for OCR instead of returning"
        return "x" * 310 + "KEPUTUSAN PENETAPAN TIM MANAJEMEN HALAL"
    monkeypatch.setattr(intake, "parse_file", slow_parse)
    monkeypatch.setattr(intake, "classify", lambda *a: {"doc_type": "other", "confidence": 0.9, "fields": {}})
    raw = pdf()
    body = {"filename": "scan.pdf", "file_b64": base64.b64encode(raw).decode()}
    try:
        for _ in range(2):
            start = time.monotonic()
            r = client.post(f"/cases/{cid}/documents", headers=headers, json=body)
            assert r.status_code == 200 and r.json()["autoparse_queued"] is True
            assert time.monotonic() - start < 1.5
            assert entered.wait(1)
        docs = client.get(f"/cases/{cid}/documents", headers=headers).json()
        assert len(docs) == 2 and all(d["file_size"] == len(raw) for d in docs)
        assert all(d["confidence"] is None for d in docs)
    finally:
        release.set()
        m._autoparse_q.join()
    assert len(calls) == 2  # 큐에서 한 번씩만 추출; 편철은 캐시된 본문 사용.
    with m.SessionLocal() as db:
        assert db.query(models.SjphEvidence).filter_by(case_id=cid, item_key="halal_supervisor").count() == 1


def test_relative_dot_directory_is_not_metadata():
    assert intake.extract_zip(archive([("./자료.txt", b"ok")])) == [("자료.txt", b"ok")]


def test_real_7z_stream_and_nested_zip_share_safety_guards(tmp_path, monkeypatch):
    import ctypes.util
    import shutil
    import subprocess
    tool = shutil.which("bsdtar")
    if not tool or not ctypes.util.find_library("archive"):
        pytest.skip("7z fixture writer/libarchive not installed")
    (tmp_path / "inside.Zip").write_bytes(archive([("inner.txt", b"synthetic document")]))
    result = subprocess.run([tool, "--format=7zip", "-cf", "-", "-C", str(tmp_path), "inside.Zip"], capture_output=True)
    assert result.returncode == 0, result.stderr
    assert intake.extract_zip(result.stdout) == [("inside.Zip/inner.txt", b"synthetic document")]
    monkeypatch.setattr(intake, "ZIP_MAX_SINGLE_FILE", 3)
    with pytest.raises(intake.ArchiveError, match="너무 큽니다"):
        intake.extract_zip(result.stdout)


def rar(entries):
    """RAR5의 저장 방식으로 합성 fixture 생성. 외부 업체 파일/rar 실행파일 불필요.

    형식: https://www.rarlab.com/technote.htm (File header, compression method 0).
    """
    import struct
    import zlib
    def vint(number):
        data = bytearray()
        while number >= 128:
            data.append((number & 127) | 128)
            number >>= 7
        data.append(number)
        return bytes(data)
    def block(body):
        header = vint(len(body)) + body
        return struct.pack("<I", zlib.crc32(header)) + header
    raw = b"Rar!\x1a\x07\x01\x00" + block(b"\x01\x00\x00")
    for name, data in entries:
        name = name.encode("utf-8")
        header = (b"\x02\x02" + vint(len(data)) + b"\x04" + vint(len(data)) + vint(0o100644)
                  + struct.pack("<I", zlib.crc32(data)) + b"\x00\x01" + vint(len(name)) + name)
        raw += block(header) + data
    return raw + block(b"\x05\x00\x00")


def test_real_rar_nested_in_zip_and_utf8_name(monkeypatch):
    import ctypes.util
    if not ctypes.util.find_library("archive"):
        pytest.skip("libarchive not installed")
    raw = archive([("inner.RaR", rar([("한글.txt", b"synthetic document")]))])
    assert m._validate_archive_upload(raw) == [("inner.RaR/한글.txt", b"synthetic document")]
    monkeypatch.setattr(intake, "ZIP_MAX_ENTRIES", 1)
    assert intake.extract_zip(raw).skipped_files == [{"filename": "inner.RaR/한글.txt", "reason": "ENTRY_LIMIT"}]


def test_real_rar_path_and_content_mismatch():
    import ctypes.util
    if not ctypes.util.find_library("archive"):
        pytest.skip("libarchive not installed")
    with pytest.raises(intake.ArchiveError, match="경로"):
        intake.extract_zip(rar([("../escape.txt", b"outside")]))
    with pytest.raises(HTTPException) as error:
        m._validate_archive_upload(rar([("forged.pdf", b"not PDF")]))
    assert error.value.status_code == 415


@pytest.mark.parametrize("name", ["기업_중소기업확인서.pdf", "기업_소상공인 확인서.pdf", "scan.pdf"])
def test_business_size_certificate_is_not_supplier_evidence(name, monkeypatch):
    text = "중소기업 확인서\n기업명: 주식회사 예시\n사업자등록번호: 123-45-67890\n기업 규모를 확인합니다."
    monkeypatch.setattr(intake.ai_local, "llm_json", lambda *a, **kw: pytest.fail("known reference should not call an LLM"))
    result = intake.classify(name, text)
    assert result["doc_type"] == "other"
    assert {k: v for k, v in result["fields"].items() if not k.startswith("_")} == {}
    assert result["fields"]["_classification"]["source"] == "rule"
    assert result["fields"]["_classification"]["review"]
    assert intake.aggregate_fields([result])["nib"] is None
    assert intake.refine_doctype_reason("원료 공급사 확인서.pdf", None, "공급사 서약")[0] == "supplier_declaration"


def test_stream_sends_heartbeat_while_one_document_is_slow(case, monkeypatch):
    import json
    client, headers, cid = case
    monkeypatch.setattr(m, "_INTAKE_HEARTBEAT_SECONDS", 0.01)
    def slow(name, raw, **kwargs):
        time.sleep(0.08)
        return "A synthetic document"
    monkeypatch.setattr(intake, "parse_file", slow)
    raw = archive([("document.txt", b"synthetic")])
    r = client.post(f"/cases/{cid}/intake-zip-stream", headers=headers,
                    json={"zip_b64": base64.b64encode(raw).decode()})
    assert r.status_code == 200
    events = [json.loads(line[5:]) for line in r.text.splitlines() if line.startswith("data:")]
    types = [e["type"] for e in events]
    assert types[0] == "start" and types[-1] == "result"
    assert "heartbeat" in types[1:types.index("progress")]
    assert events[-1]["file_count"] == 1


def test_stream_producer_stops_after_consumer_closes(monkeypatch):
    exited = threading.Event()
    def endless(*a, **kw):
        try:
            while True:
                yield "progress", {}
        finally:
            exited.set()
    monkeypatch.setattr(intake, "intake_zip_iter", endless)
    stream = m._intake_zip_events(b"", [])
    next(stream)
    stream.close()
    assert exited.wait(1)
