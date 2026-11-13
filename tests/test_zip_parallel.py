"""ZIP의 순차 추출·AI 병렬성·순서·취소·DB 경계. 실제 OCR 모델은 사용하지 않는다."""
import base64
import io
import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import fitz
import pytest

from app import ai_local, intake, document_extraction as extraction
import app.main as main


def classification(name, text):
    return {"doc_type": "other", "confidence": 0.9, "fields": {"label": str(text)},
            "field_sources": {"label": "structural"}}


def test_six_seconds_become_two_with_identical_events(monkeypatch):
    files = [(f"{i}.txt", str(i).encode()) for i in range(6)]
    active = peak = 0
    lock = threading.Lock()

    def slow(name, text):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        try:
            time.sleep(1)
            return classification(name, text)
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(intake, "parse_file", lambda name, raw, **kw: raw.decode())
    monkeypatch.setattr(intake, "classify", slow)
    monkeypatch.setenv("GLHAC_INTAKE_CONCURRENCY", "1")
    start = time.monotonic()
    sequential = list(intake.intake_zip_iter(b"", files=files))
    serial_time = time.monotonic() - start
    assert peak == 1
    monkeypatch.setenv("GLHAC_INTAKE_CONCURRENCY", "3")
    start = time.monotonic()
    parallel = list(intake.intake_zip_iter(b"", files=files))
    parallel_time = time.monotonic() - start
    assert parallel == sequential
    assert peak == 3
    assert 1.8 <= parallel_time < 3.5
    assert parallel_time < serial_time * 0.6
    print(f"\n6 files x 1s: sequential={serial_time:.3f}s parallel={parallel_time:.3f}s")


def test_out_of_order_completion_still_emits_input_order_and_skips(monkeypatch):
    files = intake._ExtractedFiles()
    files.extend((f"dir/{i}.txt", str(i).encode()) for i in range(6))
    files.skip(".DS_Store", "METADATA")
    completed = []

    def parse(name, raw, ocr_sink):
        ocr_sink.append({"text": raw.decode(), "confidence": 0.9})
        return raw.decode()

    def classify(name, text):
        time.sleep(0.1 if text == "0" else 0.01)
        completed.append(name)
        return classification(name, text)

    monkeypatch.setenv("GLHAC_INTAKE_CONCURRENCY", "3")
    monkeypatch.setattr(intake, "parse_file", parse)
    monkeypatch.setattr(intake, "classify", classify)
    events = list(intake.intake_zip_iter(b"", limit=4, files=files))
    assert completed[0] != "dir/0.txt"
    assert [payload["file"] for kind, payload in events if kind == "progress"] == [f"{i}.txt" for i in range(4)]
    result = events[-1][1]
    assert result["file_count"] == 6 and result["skipped_entries"] == 3
    assert result["skipped_files"] == [{"filename": ".DS_Store", "reason": "METADATA"},
                                       {"filename": "dir/4.txt", "reason": "ENTRY_LIMIT"},
                                       {"filename": "dir/5.txt", "reason": "ENTRY_LIMIT"}]
    for i, document in enumerate(result["classified"]):
        assert base64.b64decode(document["content_b64"]) == str(i).encode()
        assert document["ocr_lines"][0]["text"] == str(i)


def test_real_classifier_preserves_document_types_and_fields(monkeypatch):
    files = [("사업자등록증.txt", "사업자등록증\n상호: 주식회사 샘플\n사업자등록번호: 123-45-67890".encode()),
             ("원재료목록.txt", "[전성분표]\n[제품/시트: 샘플제품]\n[원재료목록]\nWater\nSugar".encode()),
             ("공정흐름도.txt", "공정흐름도\n원료입고 → 혼합 → 가열 → 포장".encode())]
    monkeypatch.setattr(ai_local, "provider", lambda: "none")
    monkeypatch.setenv("GLHAC_INTAKE_CONCURRENCY", "1")
    sequential = list(intake.intake_zip_iter(b"", files=files))
    monkeypatch.setenv("GLHAC_INTAKE_CONCURRENCY", "3")
    parallel = list(intake.intake_zip_iter(b"", files=files))
    assert parallel == sequential
    docs = parallel[-1][1]["classified"]
    assert {d["doc_type"] for d in docs} == {"nib_business_license", "material_list", "process_flow"}
    assert docs[1]["fields"]["material_names"] == ["Water", "Sugar"]


@pytest.mark.parametrize("stage", ["parse_file", "classify"])
def test_one_file_exception_does_not_stop_others(stage, monkeypatch):
    original = intake.parse_file if stage == "parse_file" else classification

    def fails(name, *args, **kwargs):
        if name == "bad.txt":
            raise RuntimeError("synthetic failure")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(intake, stage, fails)
    monkeypatch.setattr(ai_local, "provider", lambda: "none")
    files = [("first.txt", b"first"), ("bad.txt", b"preserve"), ("last.txt", b"last")]
    docs = intake.intake_zip(b"", files=files)["classified"]
    assert [d["filename"] for d in docs] == [f[0] for f in files]
    assert docs[-1]["text_full"] == "last"
    assert docs[1]["fields"]["_ocr_error" if stage == "parse_file" else "_llm_error"]
    assert base64.b64decode(docs[1]["content_b64"]) == b"preserve"


def test_document_budget_is_isolated_and_reset_per_pool_task(monkeypatch):
    budgets = []

    def classify(name, text):
        budget = extraction.current_budget()
        budgets.append(budget)
        for _ in range(20):
            budget.take()
        return {"doc_type": "other", "fields": {"_ai_budget": budget.snapshot()}}

    monkeypatch.setenv("GLHAC_INTAKE_CONCURRENCY", "3")
    monkeypatch.setattr(intake, "classify", classify)
    files = [(f"{i}.txt", b"identical content") for i in range(8)]
    docs = intake.intake_zip(b"", files=files)["classified"]
    assert len({id(b) for b in budgets}) == 8
    assert all(d["fields"]["_ai_budget"]["used"] == extraction.DOCUMENT_AI_MAX_CALLS for d in docs)
    assert extraction._ACTIVE_BUDGET.get() is None


@pytest.mark.parametrize("value, expected", [(None, 3), ("1", 1), ("5", 5), ("0", 3), ("-2", 3), ("bad", 3)])
def test_concurrency_setting(monkeypatch, value, expected):
    monkeypatch.delenv("GLHAC_INTAKE_CONCURRENCY", raising=False)
    if value is not None:
        monkeypatch.setenv("GLHAC_INTAKE_CONCURRENCY", value)
    assert intake._intake_concurrency() == expected


def test_stopped_cancels_remaining_files_and_next_ai_stage(monkeypatch):
    entered = threading.Barrier(4)
    release = threading.Event()
    stopped = threading.Event()
    parsed, classified, events = [], [], []

    def parse(name, raw, **kwargs):
        parsed.append(name)
        return "body"

    def classify(name, text):
        entered.wait(3)
        assert release.wait(3)
        extraction.check_document_stopped()
        classified.append((name, text))
        return classification(name, text)

    monkeypatch.setenv("GLHAC_INTAKE_CONCURRENCY", "3")
    monkeypatch.setattr(intake, "parse_file", parse)
    monkeypatch.setattr(intake, "classify", classify)
    files = [(f"{i}.txt", b"x") for i in range(12)]
    thread = threading.Thread(target=lambda: events.extend(intake.intake_zip_iter(b"", files=files, stopped=stopped)))
    thread.start()
    try:
        entered.wait(3)
        stopped.set()
        thread.join(1)
        assert not thread.is_alive(), "취소가 실행 중 파서 종료를 기다리면 안 된다"
    finally:
        release.set()
        thread.join(3)
    deadline = time.monotonic() + 2
    while any(t.name.startswith("intake-file") for t in threading.enumerate()) and time.monotonic() < deadline:
        time.sleep(0.01)
    assert len(parsed) == 3 and classified == []
    assert [kind for kind, _ in events] == ["start"]


def test_stream_close_propagates_stop_and_heartbeat(monkeypatch):
    entered, release, exited = threading.Event(), threading.Event(), threading.Event()
    seen = []

    def parser(*args, stopped, **kwargs):
        try:
            seen.append(stopped)
            yield "start", {"total": 1}
            entered.set()
            while not stopped.wait(0.01):
                pass
        finally:
            exited.set()

    monkeypatch.setattr(intake, "intake_zip_iter", parser)
    monkeypatch.setattr(main, "_INTAKE_HEARTBEAT_SECONDS", 0.02)
    stream = main._intake_zip_events(b"", [])
    assert next(stream)[0] == "start"
    assert entered.wait(1)
    assert next(stream)[0] == "heartbeat"
    stream.close()
    assert seen[0].is_set() and exited.wait(1)


def test_ocr_model_creation_language_switch_and_lazy_predict_are_serialized(monkeypatch):
    state = {"active": 0, "peak": 0}
    lock = threading.Lock()

    class Engine:
        def __init__(self, lang, **kwargs):
            self.lang = lang

        def predict(self, path):
            with lock:
                state["active"] += 1
                state["peak"] = max(state["peak"], state["active"])
            try:
                time.sleep(0.02)
                yield {"rec_texts": [self.lang], "rec_scores": [1.0]}
            finally:
                with lock:
                    state["active"] -= 1

    monkeypatch.setitem(sys.modules, "paddleocr", SimpleNamespace(PaddleOCR=Engine))
    monkeypatch.setattr(ai_local, "_ocr", None)
    monkeypatch.setattr(ai_local, "_ocr_lang", None)
    monkeypatch.setattr(ai_local, "_ocr_engines", {})
    langs = ["korean", "ru", "id"] * 3
    with ThreadPoolExecutor(3) as pool:
        results = list(pool.map(lambda item: (ai_local._ocr_image_inproc if item[0] % 2 else
                                             ai_local.ocr_image_lang)("fake.png", item[1]), enumerate(langs)))
    assert state["peak"] == 1
    assert [r["lines"][0]["text"] for r in results] == langs


@pytest.mark.parametrize("worker", [False, True])
def test_cancelled_document_does_not_wait_for_busy_ocr_lock(monkeypatch, worker):
    from concurrent.futures import CancelledError
    entered, stopped = threading.Event(), threading.Event()
    lock = ai_local._wlock() if worker else ai_local._ocr_lock
    monkeypatch.setattr(ai_local, "_worker_start", lambda: pytest.fail("cancelled OCR started"))

    def call():
        token = extraction._ACTIVE_STOPPED.set(stopped)
        entered.set()
        try:
            return ai_local._worker_call({}) if worker else ai_local._ocr_image_inproc("unused.png")
        finally:
            extraction._ACTIVE_STOPPED.reset(token)

    with ThreadPoolExecutor(1) as pool:
        with lock:
            future = pool.submit(call)
            assert entered.wait(1)
            stopped.set()
            with pytest.raises(CancelledError):
                future.result(timeout=1)


def test_real_pdf_parallel_text_and_ocr_match_sequential(monkeypatch):
    files = []
    for i in range(3):
        with fitz.open() as pdf:
            page = pdf.new_page()
            if i != 1:
                page.insert_text((50, 50), f"Synthetic native PDF document number {i}")
            files.append((f"{i}.pdf", pdf.tobytes()))
    files.append(("broken.pdf", b"%PDF-1.7\nbroken"))
    files.append(("repaired.pdf", files[0][1].rsplit(b"startxref", 1)[0]))
    ocr_threads = []

    def ocr(*args, **kwargs):
        ocr_threads.append(threading.current_thread().name)
        return intake._ParsedText("Synthetic OCR result", {"_ocr_error": "TEST_MARKER"})

    monkeypatch.setattr(intake, "_ocr_bytes", ocr)
    monkeypatch.setattr(ai_local, "provider", lambda: "none")
    monkeypatch.setenv("GLHAC_INTAKE_CONCURRENCY", "1")
    sequential = intake.intake_zip(b"", files=files)
    monkeypatch.setenv("GLHAC_INTAKE_CONCURRENCY", "3")
    parallel = intake.intake_zip(b"", files=files)
    assert parallel == sequential
    assert len(ocr_threads) == 2 and ocr_threads[1].startswith("intake-file")


def test_pdf_tail_uses_parent_ontology_and_preserves_page_locations(monkeypatch):
    with fitz.open() as pdf:
        for i in range(intake.PDF_MAX_PAGES + 1):
            page = pdf.new_page()
            page.insert_text((50, 50), "Ingredients: pork gelatin" if i == intake.PDF_MAX_PAGES else
                             f"Synthetic document ordinary text page {i}")
        raw = pdf.tobytes()
    calls = []
    original_scan = intake._scan_tail

    def scan(*args, **kwargs):
        calls.append(threading.current_thread().name)
        original_scan(*args, **kwargs)

    monkeypatch.setattr(intake, "_scan_tail", scan)
    sequential = intake.parse_file("long.pdf", raw)
    with ThreadPoolExecutor(1) as pool:
        parallel = pool.submit(intake.parse_file, "long.pdf", raw).result(timeout=3)
    assert str(parallel) == str(sequential)
    assert parallel.parse_fields == sequential.parse_fields
    assert parallel.parse_fields["_tail_screening"]["risk_hits"]
    assert len(calls) == 2


@pytest.mark.parametrize("cancel", [False, True])
def test_archive_process_timeout_or_stop_reaps_child(monkeypatch, cancel):
    # PDF 자식은 제거됐다. 남아 있는 7-Zip 자식의 회수 계약을 검증한다.
    import subprocess
    from concurrent.futures import CancelledError

    real_popen = subprocess.Popen
    children = []
    stopped = threading.Event()

    def popen(args, **kwargs):
        # DEVNULL과 같은 EOF를 파이프로 제공하여 핸들 종료도 계측한다.
        kwargs["stdin"] = subprocess.PIPE
        proc = real_popen([sys.executable, "-u", "-c",
                           "import sys,time; sys.stdin.buffer.readline(); time.sleep(10)"], **kwargs)
        proc.stdin.close()
        children.append(proc)
        if cancel:
            stopped.set()
        return proc

    monkeypatch.setattr(subprocess, "Popen", popen)
    monkeypatch.setattr(intake, "_SEVEN_ZIP_TIMEOUT", 0.2)
    token = intake._ARCHIVE_CONTROL.set((stopped, None))
    try:
        with pytest.raises(CancelledError if cancel else intake.ArchiveError):
            intake._seven_zip_run(["7z"], 1024)
    finally:
        intake._ARCHIVE_CONTROL.reset(token)
    assert len(children) == 1 and children[0].poll() is not None
    assert children[0].stdin.closed and children[0].stdout.closed


def test_archive_closed_output_timeout_also_reaps_child(monkeypatch):
    import subprocess

    real_popen = subprocess.Popen
    children = []

    def popen(args, **kwargs):
        proc = real_popen([sys.executable, "-u", "-c", "import os,time; os.close(1); time.sleep(10)"], **kwargs)
        children.append(proc)
        return proc

    monkeypatch.setattr(subprocess, "Popen", popen)
    monkeypatch.setattr(intake, "_SEVEN_ZIP_TIMEOUT", 0.2)
    with pytest.raises(intake.ArchiveError):
        intake._seven_zip_run(["7z"], 1024)
    assert len(children) == 1 and children[0].poll() is not None


def test_asgi_disconnect_stops_parse_without_applying_db(monkeypatch):
    import asyncio
    from app import schemas

    entered, release = threading.Event(), threading.Event()
    classified = []

    def parse(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        return "text"

    monkeypatch.setattr(main, "_get_case", lambda *a: object())
    monkeypatch.setattr(main, "_validate_archive_upload", lambda *a, **kw: [(f"{i}.txt", b"x") for i in range(6)])
    monkeypatch.setattr(main, "_apply_intake_autofill", lambda *a: pytest.fail("DB write after disconnect"))
    monkeypatch.setattr(intake, "parse_file", parse)
    monkeypatch.setattr(intake, "classify", lambda *a: classified.append(a) or classification(*a))
    body = schemas.ZipIntakeReq(zip_b64=base64.b64encode(b"PK\x03\x04").decode())
    response = main.intake_zip_stream_ep("synthetic", body, user={"role": "applicant", "uid": "test"}, db=None)

    async def drive():
        async def receive():
            assert await asyncio.to_thread(entered.wait, 2)
            return {"type": "http.disconnect"}

        async def send(message):
            pass

        await asyncio.wait_for(response({"type": "http", "asgi": {"spec_version": "2.0"}}, receive, send), 2)

    try:
        asyncio.run(drive())
    finally:
        release.set()
    deadline = time.monotonic() + 2
    while any(t.name.startswith("intake-file") for t in threading.enumerate()) and time.monotonic() < deadline:
        time.sleep(0.01)
    assert classified == []


@pytest.mark.parametrize("streaming", [False, True])
def test_routes_keep_db_autofill_commit_and_verification_on_consumer(monkeypatch, streaming):
    import zipfile
    from fastapi.testclient import TestClient

    parse_threads, phases = [], []
    original_parse = intake.parse_file
    original_apply = main._apply_intake_autofill

    def parse(*args, **kwargs):
        parse_threads.append(threading.get_ident())
        return original_parse(*args, **kwargs)

    def apply(*args):
        phases.append(("apply", threading.get_ident()))
        return original_apply(*args)

    monkeypatch.setattr(intake, "parse_file", parse)
    monkeypatch.setattr(ai_local, "provider", lambda: "none")
    monkeypatch.setattr(main, "_apply_intake_autofill", apply)
    monkeypatch.setattr(main, "verify_enqueue", lambda did: phases.append(("verify", threading.get_ident())))
    monkeypatch.setenv("GLHAC_INTAKE_CONCURRENCY", "3")
    content = io.BytesIO()
    with zipfile.ZipFile(content, "w") as archive:
        for i in range(3):
            archive.writestr(f"{i}.txt", f"Synthetic text {i}")
    with TestClient(main.app) as client:
        token = client.post("/auth/login", json={"username": "applicant1", "password": "pw"}).json()["token"]
        headers = {"Authorization": "Bearer " + token}
        cid = client.post("/cases", headers=headers, json={"company_name": "Parallel test"}).json()["case_id"]
        response = client.post(f"/cases/{cid}/intake-zip" + ("-stream" if streaming else ""),
                               headers=headers, json={"zip_b64": base64.b64encode(content.getvalue()).decode()})
        assert response.status_code == 200
        if streaming:
            result = [json.loads(line[5:]) for line in response.text.splitlines() if line.startswith("data:")][-1]
        else:
            result = response.json()
        assert result["file_count"] == 3
        assert len(client.get(f"/cases/{cid}/documents", headers=headers).json()) == 3
    assert phases[0][0] == "apply"
    assert all(tid not in parse_threads for _, tid in phases)
    assert len({tid for _, tid in phases}) == 1
