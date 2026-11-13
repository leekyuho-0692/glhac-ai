"""순차 PDF/병렬 AI, 전역 상한, 렌더 예산, 스트리밍 압축 취소 회귀."""
import asyncio
import base64
import math
import os
import subprocess
import threading
import time
from concurrent.futures import CancelledError, ThreadPoolExecutor
from types import SimpleNamespace

import fitz
import pytest

from app import ai_local, document_extraction as extraction, intake, schemas
import app.main as main
from test_archive_7zip_fallback import RAR, archive, fake_7z, member


def result(name, text):
    return {"doc_type": "other", "fields": {"label": str(text)}, "confidence": 0.9}


def pdf(width=612, height=792, text=""):
    with fitz.open() as doc:
        page = doc.new_page(width=width, height=height)
        if text:
            page.insert_text((50, 50), text)
        return doc.tobytes()


def test_extraction_is_sequential_and_overlaps_ai(monkeypatch):
    extracted, parse_threads, ai_threads = [], set(), set()
    first_ai = threading.Event()

    def parse(name, raw, **kw):
        if name == "1.txt":
            assert first_ai.wait(2), "AI must start before the next extraction finishes"
        parse_threads.add(threading.get_ident())
        extracted.append(name)
        time.sleep(0.01)
        return raw.decode()

    def classify(name, text):
        ai_threads.add(threading.get_ident())
        first_ai.set()
        time.sleep(0.04)
        return result(name, text)

    monkeypatch.setattr(intake, "parse_file", parse)
    monkeypatch.setattr(intake, "classify", classify)
    files = [(f"{i}.txt", str(i).encode()) for i in range(6)]
    docs = intake.intake_zip(b"", files=files)["classified"]
    assert extracted == [name for name, _ in files]
    assert len(parse_threads) == 1 and parse_threads.isdisjoint(ai_threads)
    assert [doc["filename"] for doc in docs] == extracted


def test_four_requests_share_three_ai_slots(monkeypatch):
    active = peak = 0
    lock, ready = threading.Lock(), threading.Barrier(4)

    def classify(name, text):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        try:
            time.sleep(0.04)
            return result(name, text)
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(intake, "classify", classify)
    monkeypatch.setenv("GLHAC_INTAKE_CONCURRENCY", "12")
    files = [(f"{i}.txt", str(i).encode()) for i in range(6)]

    def request(_):
        ready.wait(2)
        return intake.intake_zip(b"", files=files)

    with ThreadPoolExecutor(4) as pool:
        results = list(pool.map(request, range(4)))
    assert peak == 3 and active == 0
    assert all(value == results[0] for value in results)
    assert extraction._AI_SLOTS._value == extraction.AI_CONCURRENCY


@pytest.mark.parametrize("provider", ["openai", "ollama"])
def test_direct_llm_and_pipeline_share_limit_without_nested_deadlock(monkeypatch, provider):
    active = peak = 0
    lock = threading.Lock()

    def post(*args, **kw):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        try:
            time.sleep(0.03)
            content = '{"doc_type":"other","fields":{},"confidence":0.9}'
            return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {
                "choices": [{"message": {"content": content}}], "message": {"content": content}})
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(ai_local, "provider", lambda: provider)
    monkeypatch.setattr(ai_local.httpx, "post", post)
    monkeypatch.setattr(intake, "classify", lambda name, text: ai_local.llm_json("classify", text))
    with ThreadPoolExecutor(8) as pool:
        jobs = [pool.submit(intake.intake_zip, b"", files=[(f"{i}.txt", b"text") for i in range(6)])
                for _ in range(4)]
        jobs += [pool.submit(ai_local.llm_json if i % 2 else ai_local.llm_text_result, "system", "text")
                 for i in range(4)]
        for job in jobs:
            assert job.result(timeout=5)
    assert peak == 3 and active == 0
    assert extraction._AI_SLOTS._value == extraction.AI_CONCURRENCY


def test_cancellation_during_global_slot_wait_does_not_start_ai(monkeypatch):
    parsed, stopped = threading.Event(), threading.Event()
    monkeypatch.setattr(intake, "parse_file", lambda *a, **kw: parsed.set() or "text")
    monkeypatch.setattr(intake, "classify", lambda *a: pytest.fail("AI started after cancellation"))
    for _ in range(extraction.AI_CONCURRENCY):
        extraction._AI_SLOTS.acquire()
    try:
        with ThreadPoolExecutor(1) as pool:
            job = pool.submit(lambda: list(intake.intake_zip_iter(b"", files=[("a.txt", b"text")], stopped=stopped)))
            assert parsed.wait(1)
            stopped.set()
            assert [kind for kind, _ in job.result(timeout=1)] == ["start"]
    finally:
        for _ in range(extraction.AI_CONCURRENCY):
            extraction._AI_SLOTS.release()


@pytest.mark.parametrize("size,requested,expected", [(3000, 140, 120), (3000, 600, 120),
                                                   (7200, 600, 50), (30000, 140, None),
                                                   (30000, 600, None)])
def test_pdf_pixel_cap_is_checked_before_allocating(monkeypatch, size, requested, expected):
    monkeypatch.setattr(intake, "PDF_MAX_RENDER_PIXELS", 25_000_000)
    calls = []

    def render(page, *, dpi):
        calls.append(dpi)
        pixels = math.ceil(page.rect.width * dpi / 72) * math.ceil(page.rect.height * dpi / 72)
        assert pixels <= 25_000_000
        return SimpleNamespace(tobytes=lambda fmt: b"instrumented png")

    monkeypatch.setattr(fitz.Page, "get_pixmap", render)
    monkeypatch.setattr(intake, "_ocr_bytes", lambda *a: intake._ParsedText("instrumented OCR"))
    parsed = intake.parse_file("scan.pdf", pdf(size, size), dpi=requested)
    assert calls == ([] if expected is None else [expected])
    if expected is None:
        assert parsed.parse_fields["_ocr_error"] == "PIXEL_LIMIT"
        skipped = parsed.parse_fields["_ocr_skipped_pages"]
        assert skipped[0]["page"] == 1 and "판독 불가" in skipped[0]["reason"]
    else:
        assert str(parsed).strip() == "instrumented OCR"


def test_pdf_guard_preserves_native_text_and_later_pages(monkeypatch):
    with fitz.open() as doc:
        doc.new_page(width=30000, height=30000)
        doc.new_page().insert_text((50, 50), "Synthetic native text on the next page")
        raw = doc.tobytes()
    monkeypatch.setattr(fitz.Page, "get_pixmap", lambda *a, **kw: pytest.fail("huge page rendered"))
    parsed = intake.parse_file("scan.pdf", raw)
    assert "Synthetic native text" in parsed
    assert parsed.parse_fields["_ocr_skipped_pages"][0]["page"] == 1


def test_single_upload_and_high_dpi_reprocess_share_pdf_guard(monkeypatch):
    from fastapi.testclient import TestClient
    from app import models
    from app.db import SessionLocal

    monkeypatch.setattr(ai_local, "provider", lambda: "none")
    monkeypatch.setattr(main, "verify_enqueue", lambda *a: False)
    monkeypatch.setattr(fitz.Page, "get_pixmap", lambda *a, **kw: pytest.fail("huge page rendered"))
    with TestClient(main.app) as client:
        def headers(user):
            token = client.post("/auth/login", json={"username": user, "password": "pw"}).json()["token"]
            return {"Authorization": "Bearer " + token}

        applicant = headers("applicant1")
        cid = client.post("/cases", headers=applicant, json={"company_name": "Pixel bound"}).json()["case_id"]
        uploaded = client.post(f"/cases/{cid}/documents", headers=applicant, json={
            "filename": "huge.pdf", "file_b64": base64.b64encode(pdf(30000, 30000)).decode()})
        assert uploaded.status_code == 200
        did = uploaded.json()["document_id"]
        deadline = time.monotonic() + 5
        while True:
            with SessionLocal() as db:
                doc = db.get(models.DocumentAsset, did)
                fields = dict(doc.fields or {})
            if fields.get("_ocr_error") or time.monotonic() >= deadline:
                break
            time.sleep(0.02)
        assert fields["_ocr_error"] == "PIXEL_LIMIT"
        reprocessed = client.post(f"/documents/{did}/reprocess?dpi=600&apply=false", headers=headers("operator1"))
        assert reprocessed.status_code == 200
        assert reprocessed.json()["fields"]["_ocr_error"] == "PIXEL_LIMIT"
        assert "판독 불가" in reprocessed.json()["fields"]["_ocr_skipped_pages"][0]["reason"]


def stream_response(monkeypatch, raw):
    monkeypatch.setattr(main, "_get_case", lambda *a: object())
    monkeypatch.setattr(main, "_apply_intake_autofill", lambda *a: pytest.fail("unexpected DB write"))
    monkeypatch.setattr(main, "_INTAKE_HEARTBEAT_SECONDS", 0.02)
    body = schemas.ZipIntakeReq(zip_b64=base64.b64encode(raw).decode())
    return main.intake_zip_stream_ep("synthetic", body, user={"role": "applicant", "uid": "test"}, db=None)


@pytest.mark.parametrize("stage,nested", [("l", False), ("x", False), ("x", True)])
def test_7z_stream_starts_before_extraction_and_disconnect_reaps(monkeypatch, fake_7z, stage, nested):
    fake_7z([member(f"{i}.txt") for i in range(6)], **{"delay_" + stage: 5})
    raw = archive([("inner.rar", RAR)]) if nested else RAR
    response = stream_response(monkeypatch, raw)
    assert fake_7z.calls() == [], "7-Zip must not run before the response generator"
    children, messages = [], []
    real_popen = subprocess.Popen

    def popen(*args, **kw):
        proc = real_popen(*args, **kw)
        children.append(proc)
        return proc

    monkeypatch.setattr(subprocess, "Popen", popen)

    async def drive():
        heartbeat = asyncio.Event()

        async def receive():
            await asyncio.wait_for(heartbeat.wait(), 3)
            end = time.monotonic() + 3
            while not any(call[0] == stage for call in fake_7z.calls()):
                assert time.monotonic() < end
                await asyncio.sleep(0.01)
            return {"type": "http.disconnect"}

        async def send(message):
            messages.append(message)
            if b'"heartbeat"' in message.get("body", b""):
                heartbeat.set()

        await asyncio.wait_for(response({"type": "http", "asgi": {"spec_version": "2.0"}}, receive, send), 4)

    start = time.monotonic()
    asyncio.run(drive())
    assert time.monotonic() - start < 2
    assert messages[0]["type"] == "http.response.start"
    assert any(b'"heartbeat"' in message.get("body", b"") for message in messages)
    deadline = time.monotonic() + 2
    while any(proc.poll() is None for proc in children) and time.monotonic() < deadline:
        time.sleep(0.01)
    assert children and all(proc.poll() is not None for proc in children)
    calls = fake_7z.calls()
    assert [call[0] for call in calls] == (["l"] if stage == "l" else ["l", "x"])
    deadline = time.monotonic() + 1
    while os.path.exists(calls[0][-1]) and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not os.path.exists(calls[0][-1])


@pytest.mark.parametrize("streaming", [False, True])
def test_archive_deadline_is_total_across_7z_calls(monkeypatch, fake_7z, streaming):
    fake_7z([member(f"{i}.txt") for i in range(6)], delay_l=0.08, delay_x=0.14)
    monkeypatch.setenv("GLHAC_ARCHIVE_DEADLINE", "0.55")
    monkeypatch.setattr(main, "_get_case", lambda *a: object())
    monkeypatch.setattr(main, "_apply_intake_autofill", lambda *a: pytest.fail("DB write after deadline"))
    start = time.monotonic()
    if streaming:
        response = stream_response(monkeypatch, RAR)
        messages = []

        async def drive():
            async def receive():
                await asyncio.Event().wait()

            async def send(message):
                messages.append(message)

            await asyncio.wait_for(response({"type": "http", "asgi": {"spec_version": "2.0"}}, receive, send), 3)

        asyncio.run(drive())
        body = b"".join(message.get("body", b"") for message in messages).decode()
        assert '"heartbeat"' in body and '"error"' in body and "전체 처리 시간" in body
        assert '"result"' not in body
    else:
        from fastapi import HTTPException
        body = schemas.ZipIntakeReq(zip_b64=base64.b64encode(RAR).decode())
        with pytest.raises(HTTPException) as error:
            main.intake_zip_ep("synthetic", body, user={"role": "applicant", "uid": "test"}, db=None)
        assert error.value.status_code == 400
        assert "전체 처리 시간" in error.value.detail["message"]
    assert 0.5 <= time.monotonic() - start < 1.3
    assert 2 <= len(fake_7z.calls()) < 7


def test_archive_cancel_between_members_stops_before_next_process(monkeypatch, fake_7z):
    fake_7z([member(f"{i}.txt") for i in range(6)])
    stopped = threading.Event()

    def validate(raw, name):
        stopped.set()

    with pytest.raises(CancelledError):
        intake.extract_zip(RAR, validator=validate, stopped=stopped)
    assert [call[0] for call in fake_7z.calls()] == ["l", "x"]


def test_archive_deadline_covers_ai_stage(monkeypatch):
    entered, release = threading.Event(), threading.Event()

    def classify(name, text):
        entered.set()
        assert release.wait(2)
        return result(name, text)

    monkeypatch.setattr(intake, "classify", classify)
    try:
        with pytest.raises(intake.ArchiveError, match="전체 처리 시간"):
            intake.intake_zip(b"", files=[("a.txt", b"text")], deadline=time.monotonic() + 0.15)
        assert entered.is_set()
    finally:
        release.set()


def test_pdf_extraction_does_not_spawn_process(monkeypatch):
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: pytest.fail("PDF spawned a process"))
    monkeypatch.setattr(ai_local, "provider", lambda: "none")
    raw = pdf(text="Synthetic native PDF document without OCR")
    output = intake.intake_zip(b"", files=[(f"{i}.pdf", raw) for i in range(6)])
    assert len(output["classified"]) == 6


@pytest.mark.parametrize("cancel", [False, True])
def test_archive_pdf_validation_lock_respects_deadline_and_cancel(cancel):
    raw = archive([("file.pdf", pdf(text="Synthetic native PDF document"))])
    stopped = threading.Event()
    with ThreadPoolExecutor(1) as pool:
        with intake.PDF_LOCK:
            job = pool.submit(main._validate_archive_upload, raw, stopped=stopped,
                              deadline=time.monotonic() + 0.15)
            if cancel:
                stopped.set()
            with pytest.raises(CancelledError if cancel else intake.ArchiveError):
                job.result(timeout=1)


@pytest.mark.parametrize("multi", [False, True])
@pytest.mark.parametrize("size", [3000, 30000])
def test_path_based_ocr_never_sends_unbounded_pdf_to_worker(monkeypatch, tmp_path, multi, size):
    monkeypatch.setattr(intake, "PDF_MAX_RENDER_PIXELS", 25_000_000)
    path = tmp_path / "organization.pdf"
    path.write_bytes(pdf(size, size))
    calls, rasters = [], []

    def render(page, *, dpi):
        rasters.append(dpi)
        assert math.ceil(page.rect.width * dpi / 72) * math.ceil(page.rect.height * dpi / 72) <= 25_000_000
        return SimpleNamespace(tobytes=lambda fmt: b"instrumented png")

    def worker(req):
        calls.append(req)
        assert req["path"].endswith(".png")
        return {"ok": True, "lines": [{"text": "ocr", "confidence": 1}], "text": "ocr", "langs_used": ["id"]}

    monkeypatch.setattr(ai_local, "OCR_MODE", "worker")
    monkeypatch.setattr(ai_local, "_worker_call", worker)
    monkeypatch.setattr(fitz.Page, "get_pixmap", render)
    value = ai_local.ocr_text_multi(str(path), ["id"]) if multi else ai_local.ocr_image(str(path))
    if size == 30000:
        assert rasters == [] and calls == []
        assert value["error"] == "PIXEL_LIMIT" and not value["ok"]
        assert "판독 불가" in value["_ocr_skipped_pages"][0]["reason"]
    else:
        assert rasters == [120] and len(calls) == 1 and value["ok"]
        assert not os.path.exists(calls[0]["path"])
