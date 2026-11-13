"""PDF 락/OCR 취소와 운영 DPI 보존 회귀 — 실제 파이프, 렌더 인자 계측."""
import base64
import json
import math
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import CancelledError, ThreadPoolExecutor
from types import SimpleNamespace

import fitz
import pytest

from app import ai_local, document_extraction as extraction, intake
import app.main as main


def pdf(width=595, height=842, *, text="", pages=1):
    with fitz.open() as document:
        for _ in range(pages):
            page = document.new_page(width=width, height=height)
            if text:
                page.insert_text((50, 50), text)
        return document.tobytes()


def wait_for(predicate, timeout=1):
    until = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < until, "작업이 제한 시간 안에 진행되지 않음"
        time.sleep(0.01)


@pytest.fixture
def delayed_worker(monkeypatch, tmp_path):
    """앱의 실제 _worker_call에 지연/부분 JSON 응답 프로세스를 연결한다."""
    state = SimpleNamespace(delay=30, partial=False, children=[], markers=[])
    program = r'''
import json, pathlib, sys, time
marker, delay, partial = sys.argv[1], float(sys.argv[2]), int(sys.argv[3])
for line in sys.stdin:
    req = json.loads(line)
    label = req.get("label", "판독 결과")
    output = json.dumps({"ok": True, "lines": [{"text": label, "confidence": 1}],
                         "text": label, "langs_used": ["id"]}, ensure_ascii=False) + "\n"
    if partial:
        sys.stdout.write(output[:8])
        sys.stdout.flush()
    pathlib.Path(marker).write_text(json.dumps(req))
    time.sleep(delay)
    sys.stdout.write(output[8:] if partial else output)
    sys.stdout.flush()
'''

    def start():
        marker = tmp_path / f"worker-{len(state.children)}.started"
        process = subprocess.Popen(
            [sys.executable, "-u", "-c", program, str(marker), str(state.delay), str(int(state.partial))],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, bufsize=1)
        state.children.append(process)
        state.markers.append(marker)
        ai_local._w["proc"] = process

    monkeypatch.setattr(ai_local, "_w", {"proc": None, "last": 0.0, "reaper": None})
    monkeypatch.setattr(ai_local, "_worker_start", start)
    monkeypatch.setattr(ai_local, "OCR_MODE", "worker")
    monkeypatch.setattr(ai_local, "OCR_TIMEOUT", 60)
    monkeypatch.setattr(ai_local, "_ocr_image_inproc", lambda *a, **kw: pytest.fail("중단 뒤 OCR 폴백"))
    monkeypatch.setattr(ai_local, "_ocr_text_multi_inproc", lambda *a, **kw: pytest.fail("중단 뒤 OCR 폴백"))
    try:
        yield state
    finally:
        ai_local._worker_stop(force=True)
        for process in state.children:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=2)
            process.stdin.close()
            process.stdout.close()


def extract(entry, raw, path, dpi=None):
    if entry == "parse":
        return intake.parse_file("scan.pdf", raw, dpi=dpi)
    if entry == "multi":
        return ai_local.ocr_text_multi(str(path), ["id"])
    return ai_local.ocr_image(str(path))


@pytest.mark.parametrize("entry", ["parse", "image", "multi"])
@pytest.mark.parametrize("cancel", [True, False], ids=["cancel", "deadline"])
@pytest.mark.parametrize("partial", [False, True], ids=["silent", "partial-json"])
def test_ocr_wait_releases_pdf_lock_and_stops_promptly(
        monkeypatch, tmp_path, delayed_worker, entry, cancel, partial):
    raw = pdf()
    native = pdf(text="Another request with enough native PDF text")
    path = tmp_path / "scan.pdf"
    path.write_bytes(raw)
    stopped = threading.Event()
    delayed_worker.partial = partial
    monkeypatch.setattr(fitz.Page, "get_pixmap", lambda *a, **kw: SimpleNamespace(tobytes=lambda fmt: b"png"))
    opened = []
    original_open = fitz.open

    def track_open(*args, **kwargs):
        document = original_open(*args, **kwargs)
        opened.append(document)
        return document

    monkeypatch.setattr(fitz, "open", track_open)
    deadline = time.monotonic() + (10 if cancel else 0.7)

    def run():
        stop_token = extraction._ACTIVE_STOPPED.set(stopped)
        archive_token = intake._ARCHIVE_CONTROL.set((stopped, deadline))
        try:
            return extract(entry, raw, path)
        finally:
            intake._ARCHIVE_CONTROL.reset(archive_token)
            extraction._ACTIVE_STOPPED.reset(stop_token)

    def other_pdf():
        return main._valid_upload_structure(native, "pdf"), str(intake.parse_file("native.pdf", native))

    with ThreadPoolExecutor(2) as pool:
        job = pool.submit(run)
        try:
            wait_for(lambda: delayed_worker.markers and delayed_worker.markers[0].exists())
            process = delayed_worker.children[0]
            image_path = json.loads(delayed_worker.markers[0].read_text())["path"]
            # OCR는 아직 대기 중이어도 다른 요청의 업로드 검증과 텍스트 추출은 끝난다.
            valid, text = pool.submit(other_pdf).result(timeout=0.5)
            assert valid and "Another request" in text
            assert process.poll() is None and not job.done()
            stopped_at = time.monotonic() if cancel else deadline
            if cancel:
                stopped.set()
            with pytest.raises(CancelledError if cancel else intake.ArchiveError):
                job.result(timeout=1.2)
            assert time.monotonic() - stopped_at < 0.5
            assert process.poll() is not None
            assert process.stdin.closed and process.stdout.closed
            assert ai_local._w["proc"] is None
            assert not os.path.exists(image_path)
            assert all(document.is_closed for document in opened)
            assert stopped.is_set() == cancel  # 기한 검사는 취소 이벤트 없이도 작동한다.
        finally:
            stopped.set()
            if not job.done():
                delayed_worker.children[0].kill()

    # 취소한 요청의 늦은 응답이 새 요청에 전달되지 않고 새 워커를 재사용한다.
    delayed_worker.delay = 0
    for label in ("fresh", "next"):
        value = ai_local._worker_call({"op": "ocr_lines", "label": label})
        assert value["lines"][0]["text"] == label
    assert len(delayed_worker.children) == 2


@pytest.mark.parametrize("cancel", [True, False], ids=["cancel", "deadline"])
def test_pipeline_consumer_and_extractor_leave_delayed_ocr(monkeypatch, delayed_worker, cancel):
    raw = pdf()
    stopped, finished = threading.Event(), threading.Event()
    original = intake._extract_document
    monkeypatch.setattr(fitz.Page, "get_pixmap", lambda *a, **kw: SimpleNamespace(tobytes=lambda fmt: b"png"))

    def tracked(*args):
        try:
            return original(*args)
        finally:
            finished.set()

    monkeypatch.setattr(intake, "_extract_document", tracked)
    deadline = time.monotonic() + (10 if cancel else 0.7)
    with ThreadPoolExecutor(1) as pool:
        job = pool.submit(lambda: list(intake.intake_zip_iter(
            b"", files=[("scan.pdf", raw)], stopped=stopped, deadline=deadline)))
        try:
            wait_for(lambda: delayed_worker.markers and delayed_worker.markers[0].exists())
            if cancel:
                stopped.set()
                assert [kind for kind, _ in job.result(timeout=1)] == ["start"]
            else:
                with pytest.raises(intake.ArchiveError, match="전체 처리 시간"):
                    job.result(timeout=1)
            assert finished.wait(0.5)
            assert delayed_worker.children[0].poll() is not None
        finally:
            stopped.set()


@pytest.mark.parametrize("partial", [False, True])
def test_worker_timeout_kills_and_reaps_without_shutdown_grace(monkeypatch, delayed_worker, partial):
    delayed_worker.partial = partial
    monkeypatch.setattr(ai_local, "OCR_TIMEOUT", 0.25)
    started = time.monotonic()
    value = ai_local._worker_call({"op": "ocr_lines"})
    assert value == {"ok": False, "error": "WORKER_TIMEOUT"}
    assert time.monotonic() - started < 0.7
    process = delayed_worker.children[0]
    assert process.poll() is not None and process.stdout.closed and process.stdin.closed


@pytest.mark.parametrize("worker", [True, False])
def test_ocr_lock_wait_also_observes_total_deadline(monkeypatch, worker):
    lock = ai_local._wlock() if worker else ai_local._ocr_lock
    monkeypatch.setattr(ai_local, "_worker_start", lambda: pytest.fail("기한 초과 후 워커 시작"))

    def run():
        token = intake._ARCHIVE_CONTROL.set((None, time.monotonic() + 0.15))
        try:
            return ai_local._worker_call({}) if worker else ai_local._ocr_image_inproc("unused.png")
        finally:
            intake._ARCHIVE_CONTROL.reset(token)

    with ThreadPoolExecutor(1) as pool:
        with lock:
            job = pool.submit(run)
            with pytest.raises(intake.ArchiveError, match="전체 처리 시간"):
                job.result(timeout=0.7)


def fake_recognition(monkeypatch):
    monkeypatch.setattr(intake, "_ocr_bytes", lambda *a, **kw: intake._ParsedText("정상 스캔 결과"))
    monkeypatch.setattr(ai_local, "_worker_call", lambda req: {
        "ok": True, "lines": [{"text": "정상 스캔 결과", "confidence": 1}],
        "text": "정상 스캔 결과", "langs_used": ["id"]})
    monkeypatch.setattr(ai_local, "OCR_MODE", "worker")


@pytest.mark.parametrize("entry", ["parse", "image", "multi"])
@pytest.mark.parametrize("size,dpi", [((595, 842), 300), ((595, 842), 600),
                                      ((842, 1191), 400), ((842, 1191), 600),
                                      ((1191, 1684), 300), ((1191, 1684), 400),
                                      ((612, 1008), 400), ((612, 1008), 600)],
                         ids=["A4-300", "A4-600", "A3-400", "A3-600",
                              "A2-300", "A2-400", "Legal-400", "Legal-600"])
def test_normal_documents_keep_operating_dpi(monkeypatch, tmp_path, entry, size, dpi):
    calls = []
    raw = pdf(*size)
    path = tmp_path / "scan.pdf"
    path.write_bytes(raw)
    fake_recognition(monkeypatch)
    monkeypatch.setattr(intake, "_OCR_DPI", dpi)

    def render(page, *, dpi):
        calls.append(dpi)
        assert math.ceil(page.rect.width * dpi / 72) * math.ceil(page.rect.height * dpi / 72) <= 100_000_000
        return SimpleNamespace(tobytes=lambda fmt: b"png")

    monkeypatch.setattr(fitz.Page, "get_pixmap", render)
    value = extract(entry, raw, path, dpi)
    fields = value.parse_fields if entry == "parse" else value
    assert intake.PDF_MAX_RENDER_PIXELS == 100_000_000
    assert calls == [dpi]
    assert not any(key in fields for key in ("_ocr_error", "_ocr_reduced_pages", "_ocr_skipped_pages"))


@pytest.mark.parametrize("entry", ["parse", "image", "multi"])
@pytest.mark.parametrize("size", [3000, 30000], ids=["reduced", "skipped"])
def test_abnormal_pages_record_pixel_limit_reason(monkeypatch, tmp_path, caplog, entry, size):
    caplog.set_level("WARNING", logger=intake.log.name)
    raw = pdf(size, size)
    path = tmp_path / "scan.pdf"
    path.write_bytes(raw)
    fake_recognition(monkeypatch)
    monkeypatch.setattr(intake, "_OCR_DPI", 600)
    calls = []

    def render(page, *, dpi):
        calls.append(dpi)
        assert math.ceil(page.rect.width * dpi / 72) * math.ceil(page.rect.height * dpi / 72) <= 100_000_000
        return SimpleNamespace(tobytes=lambda fmt: b"png")

    monkeypatch.setattr(fitz.Page, "get_pixmap", render)
    value = extract(entry, raw, path, 600)
    fields = value.parse_fields if entry == "parse" else value
    key = "_ocr_skipped_pages" if size == 30000 else "_ocr_reduced_pages"
    marker = fields[key][0]
    assert marker["page"] == 1 and marker["requested_dpi"] == 600
    assert marker["max_pixels"] == 100_000_000
    assert "픽셀 상한" in marker["reason"] and marker["reason"] in caplog.text
    if size == 30000:
        assert calls == [] and marker["rendered_dpi"] is None
        assert fields["_ocr_error"] == "PIXEL_LIMIT"
    else:
        assert calls == [240] and marker["rendered_dpi"] == 240
        assert "_ocr_error" not in fields


def test_pdf_renders_remain_globally_serialized(monkeypatch, tmp_path):
    raw = pdf(pages=3)
    path = tmp_path / "scan.pdf"
    path.write_bytes(raw)
    fake_recognition(monkeypatch)
    active = peak = count = 0
    lock, ready = threading.Lock(), threading.Barrier(4)

    def render(page, *, dpi):
        nonlocal active, peak, count
        with lock:
            active += 1
            peak = max(peak, active)
            count += 1

        def encode(fmt):
            nonlocal active
            time.sleep(0.02)
            with lock:
                active -= 1
            return b"png"

        return SimpleNamespace(tobytes=encode)

    monkeypatch.setattr(fitz.Page, "get_pixmap", render)

    def run(entry):
        ready.wait(timeout=2)
        return extract(entry, raw, path)

    with ThreadPoolExecutor(4) as pool:
        values = list(pool.map(run, ["parse", "image", "multi", "parse"]))
    assert all(values)
    assert peak == 1 and active == 0 and count == 12


@pytest.mark.parametrize("size,expected_dpi", [((595, 842), 600), ((3000, 3000), 240)],
                         ids=["A4-600", "reduced-with-reason"])
def test_upload_and_reprocess_keep_dpi_and_render_metadata(monkeypatch, size, expected_dpi):
    from fastapi.testclient import TestClient
    from app import models
    from app.db import SessionLocal

    calls = []
    fake_recognition(monkeypatch)
    monkeypatch.setattr(ai_local, "provider", lambda: "none")
    monkeypatch.setattr(main, "verify_enqueue", lambda *a: False)

    def render(page, *, dpi):
        calls.append(dpi)
        return SimpleNamespace(tobytes=lambda fmt: b"png")

    monkeypatch.setattr(fitz.Page, "get_pixmap", render)
    with TestClient(main.app) as client:
        def headers(user):
            token = client.post("/auth/login", json={"username": user, "password": "pw"}).json()["token"]
            return {"Authorization": "Bearer " + token}

        applicant = headers("applicant1")
        cid = client.post("/cases", headers=applicant, json={"company_name": "PDF DPI"}).json()["case_id"]
        uploaded = client.post(f"/cases/{cid}/documents", headers=applicant, json={
            "filename": "scan.pdf", "file_b64": base64.b64encode(pdf(*size)).decode()})
        assert uploaded.status_code == 200
        did = uploaded.json()["document_id"]

        def parsed():
            with SessionLocal() as db:
                return bool(db.get(models.DocumentAsset, did).text_full)

        wait_for(parsed, timeout=5)
        response = client.post(f"/documents/{did}/reprocess?dpi=600&apply=false", headers=headers("operator1"))
        assert response.status_code == 200
        assert calls[-1] == expected_dpi
        fields = response.json()["fields"]
        assert "_ocr_error" not in fields
        if expected_dpi == 600:
            assert "_ocr_reduced_pages" not in fields
        else:
            marker = fields["_ocr_reduced_pages"][0]
            assert marker["requested_dpi"] == 600 and marker["rendered_dpi"] == expected_dpi
            assert "픽셀 상한" in marker["reason"]
