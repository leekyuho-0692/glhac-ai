"""해제/파일/요청 기한의 범위와 부분 결과 저장. 실제 OCR·LLM은 호출하지 않는다."""
import asyncio
import base64
import json
import threading
import time
from types import SimpleNamespace

import pytest

from app import document_extraction as extraction, intake, schemas
import app.main as main
from test_archive_7zip_fallback import archive


def classification(name, text):
    return {"doc_type": "nib_business_license", "fields": {}, "confidence": 0.9}


@pytest.fixture(autouse=True)
def fake_pipeline(monkeypatch):
    monkeypatch.setattr(intake, "parse_file", lambda name, raw, **kw: raw.decode())
    monkeypatch.setattr(intake, "classify", classification)
    monkeypatch.setenv("GLHAC_INTAKE_FILE_TIMEOUT", "2")
    monkeypatch.setenv("GLHAC_INTAKE_TOTAL_DEADLINE", "3")
    monkeypatch.setenv("GLHAC_INTAKE_CONCURRENCY", "3")
    yield
    end = time.monotonic() + 2
    while time.monotonic() < end:
        workers = [t for t in threading.enumerate()
                   if t.name.startswith(("intake-file", "intake-ai", "intake-parse"))]
        if not workers:
            break
        for worker in workers:
            worker.join(0.02)
    assert not workers, "시간 초과 작업/생산자가 종료되지 않았다"
    assert extraction._AI_SLOTS._value == extraction.AI_CONCURRENCY


def route(monkeypatch, raw, streaming):
    """기존 stream_response 패턴으로 실제 SSE 소비·DB 반영 경계를 통과한다."""
    applied, commits, messages = [], [], []
    case = SimpleNamespace(status="application_draft")
    db = SimpleNamespace(commit=lambda: commits.append(True))
    monkeypatch.setattr(main, "_get_case", lambda *a: case)
    monkeypatch.setattr(main, "case_snapshot", lambda *a: {})
    monkeypatch.setattr(main, "document_snapshots", lambda *a: {})
    monkeypatch.setattr(main, "_apply_intake_autofill",
                        lambda db, c, result, *a: applied.append(result) or {})
    monkeypatch.setattr(main.sm, "record_event", lambda *a: None)
    monkeypatch.setattr(main, "_notify_package_gaps", lambda *a: None)
    monkeypatch.setattr(main, "_INTAKE_HEARTBEAT_SECONDS", 0.02)
    body = schemas.ZipIntakeReq(zip_b64=base64.b64encode(raw).decode())
    endpoint = main.intake_zip_stream_ep if streaming else main.intake_zip_ep
    response = endpoint("synthetic", body, user={"role": "applicant", "uid": "test"}, db=db)
    if not streaming:
        return response, applied, commits, []

    async def drive():
        async def receive():
            await asyncio.Event().wait()

        async def send(message):
            messages.append(message)

        await asyncio.wait_for(response({"type": "http", "asgi": {"spec_version": "2.0"}},
                                        receive, send), 3)

    asyncio.run(drive())
    content = b"".join(message.get("body", b"") for message in messages).decode()
    events = [json.loads(line[5:]) for line in content.splitlines() if line.startswith("data:")]
    assert not any(event["type"] == "error" for event in events), events
    assert events[-1]["type"] == "result"
    return events[-1], applied, commits, events


def test_second_file_extraction_timeout_keeps_first_and_third(monkeypatch):
    monkeypatch.setenv("GLHAC_INTAKE_FILE_TIMEOUT", "0.06")
    parsed, classified = [], []

    def parse(name, raw, **kw):
        parsed.append(name)
        if name == "2.txt":
            time.sleep(0.12)
            intake._check_archive_work()
        return raw.decode()

    def classify(name, text):
        classified.append(name)
        return classification(name, text)

    monkeypatch.setattr(intake, "parse_file", parse)
    monkeypatch.setattr(intake, "classify", classify)
    files = [(f"{i}.txt", str(i).encode()) for i in (1, 2, 3)]
    output = intake.intake_zip(b"", files=files)
    docs = output["classified"]
    assert parsed == [name for name, _ in files]
    assert classified == ["1.txt", "3.txt"]
    assert [doc["filename"] for doc in docs] == parsed
    assert docs[0]["doc_type"] == docs[2]["doc_type"] == "nib_business_license"
    assert docs[1]["fields"] == {"_ocr_error": "TIMEOUT"}
    assert set(docs[1]) == set(docs[0])
    assert base64.b64decode(docs[1]["content_b64"]) == b"2"
    assert not output["timed_out"] and output["skipped_files"] == []
    assert intake._ARCHIVE_CONTROL.get() == (None, None)


@pytest.mark.parametrize("path", ["direct", "sync", "stream"])
def test_archive_deadline_ends_after_extraction(monkeypatch, path):
    monkeypatch.setenv("GLHAC_ARCHIVE_DEADLINE", "0.10")

    def parse(name, raw, **kw):
        time.sleep(0.20)
        intake._check_archive_work()
        return raw.decode()

    monkeypatch.setattr(intake, "parse_file", parse)
    raw = archive([("document.txt", b"Synthetic document")])
    start = time.monotonic()
    if path == "direct":
        output = intake.intake_zip(raw)
    else:
        output, applied, commits, events = route(monkeypatch, raw, path == "stream")
        assert len(applied) == len(commits) == 1
        if path == "stream":
            assert any(event["type"] == "heartbeat" for event in events)
    assert time.monotonic() - start >= 0.20
    assert not output["timed_out"]
    assert len(output["classified"]) == 1
    assert output["classified"][0]["fields"] == {}


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("stage", ["extraction", "classification"])
def test_total_timeout_saves_completed_documents(monkeypatch, streaming, stage):
    monkeypatch.setenv("GLHAC_INTAKE_TOTAL_DEADLINE", "0.15")
    monkeypatch.setenv("GLHAC_INTAKE_CONCURRENCY", "1")
    entered, release = threading.Event(), threading.Event()

    def slow(name, value, **kw):
        if name == "2.txt":
            entered.set()
            assert release.wait(2)
        return value.decode() if stage == "extraction" else classification(name, value)

    monkeypatch.setattr(intake, "parse_file" if stage == "extraction" else "classify", slow)
    raw = archive([(f"{i}.txt", str(i).encode()) for i in (1, 2, 3)])
    start = time.monotonic()
    try:
        output, applied, commits, events = route(monkeypatch, raw, streaming)
        assert time.monotonic() - start < 1
        assert entered.is_set() and not release.is_set()
        assert output["timed_out"] is True
        assert [d["filename"] for d in output["classified"]] == ["1.txt"]
        assert output["skipped_files"] == [
            {"filename": "2.txt", "reason": "TIME_LIMIT"},
            {"filename": "3.txt", "reason": "TIME_LIMIT"}]
        assert output["skipped_entries"] == 2 and output["file_count"] == 3
        assert len(applied) == len(commits) == 1
        assert applied[0]["timed_out"]
        assert [d["filename"] for d in applied[0]["classified"]] == ["1.txt"]
        if streaming:
            assert any(event["type"] == "heartbeat" for event in events)
            assert [event["file"] for event in events if event["type"] == "progress"] == ["1.txt"]
    finally:
        release.set()


def test_total_timeout_preserves_later_completed_files_and_other_skips(monkeypatch):
    monkeypatch.setenv("GLHAC_INTAKE_TOTAL_DEADLINE", "0.15")
    release = threading.Event()

    def classify(name, text):
        if name == "slow/0.txt":
            assert release.wait(2)
        return classification(name, text)

    monkeypatch.setattr(intake, "classify", classify)
    files = intake._ExtractedFiles()
    files.extend([(name, b"text") for name in ("slow/0.txt", "1.txt", "2.txt", "3.txt", "4.txt")])
    files.skip(".DS_Store", "METADATA")
    stopped = threading.Event()
    try:
        events = list(intake.intake_zip_iter(b"", files=files, limit=4, stopped=stopped))
        assert events[-1][0] == "result"
        output = events[-1][1]
        assert output["timed_out"] and not stopped.is_set()
        assert [doc["filename"] for doc in output["classified"]] == ["1.txt", "2.txt"]
        assert output["skipped_files"] == [
            {"filename": ".DS_Store", "reason": "METADATA"},
            {"filename": "4.txt", "reason": "ENTRY_LIMIT"},
            {"filename": "slow/0.txt", "reason": "TIME_LIMIT"},
            {"filename": "3.txt", "reason": "TIME_LIMIT"}]
        assert output["skipped_entries"] == 4
    finally:
        release.set()


def test_repeated_file_timeouts_release_slots_and_process_later_files(monkeypatch):
    monkeypatch.setenv("GLHAC_INTAKE_FILE_TIMEOUT", "0.08")
    monkeypatch.setenv("GLHAC_INTAKE_CONCURRENCY", "1")
    semaphores, called = [], []
    semaphore = threading.BoundedSemaphore

    def tracked_semaphore(value):
        result = semaphore(value)
        semaphores.append((result, value))
        return result

    def classify(name, text):
        called.append(name)
        if name.startswith("slow"):
            try:
                while True:
                    time.sleep(0.01)
                    intake._check_archive_work()
            finally:
                time.sleep(0.02)  # 소비자가 파일 기한 뒤에도 이 스레드를 기다리면 안 된다.
        return classification(name, text)

    monkeypatch.setattr(threading, "BoundedSemaphore", tracked_semaphore)
    monkeypatch.setattr(intake, "classify", classify)
    files = [(f"slow{i}.txt", b"slow") for i in range(6)] + [("last.txt", b"last")]
    output = intake.intake_zip(b"", files=files)
    assert not output["timed_out"]
    assert len(output["classified"]) == 7
    assert all(doc["fields"]["_llm_error"] == "TIMEOUT" for doc in output["classified"][:-1])
    assert output["classified"][-1]["fields"] == {}
    assert called[-1] == "last.txt"
    assert semaphores and all(slot._value == value for slot, value in semaphores)


def test_file_timeout_while_waiting_for_global_ai_slot(monkeypatch):
    monkeypatch.setenv("GLHAC_INTAKE_FILE_TIMEOUT", "0.06")
    called = []
    monkeypatch.setattr(intake, "classify", lambda *a: called.append(a) or classification(*a))
    for _ in range(extraction.AI_CONCURRENCY):
        extraction._AI_SLOTS.acquire()
    try:
        output = intake.intake_zip(b"", files=[("a.txt", b"text"), ("b.txt", b"text")])
        assert not output["timed_out"]
        assert all(doc["fields"]["_llm_error"] == "TIMEOUT" for doc in output["classified"])
    finally:
        for _ in range(extraction.AI_CONCURRENCY):
            extraction._AI_SLOTS.release()
    assert called == []


def test_classification_budget_starts_when_classification_starts(monkeypatch):
    """추출과 분류는 각자 시작 시점부터 파일 기한을 잰다(요청 전체 기한이 상한)."""
    monkeypatch.setenv("GLHAC_INTAKE_FILE_TIMEOUT", "0.12")
    deadlines = []

    def parse(name, raw, **kw):
        deadlines.append(intake._ARCHIVE_CONTROL.get()[1])
        time.sleep(0.07)
        return raw.decode()

    def classify(name, text):
        deadlines.append(intake._ARCHIVE_CONTROL.get()[1])
        time.sleep(0.07)
        return classification(name, text)

    monkeypatch.setattr(intake, "parse_file", parse)
    monkeypatch.setattr(intake, "classify", classify)
    output = intake.intake_zip(b"", files=[("a.txt", b"text")])
    assert deadlines[1] > deadlines[0]
    assert output["classified"][0]["fields"] == {}
    assert not output["timed_out"]


def test_timed_out_classifier_holding_worker_does_not_cascade(monkeypatch):
    """끊을 수 없는 LLM 호출이 시간 초과 뒤에도 풀 워커를 잡고 있어도, 뒤 파일은
    워커를 기다린 시간 때문에 TIMEOUT 나지 않는다(기한은 분류 시작부터)."""
    monkeypatch.setenv("GLHAC_INTAKE_FILE_TIMEOUT", "0.1")
    monkeypatch.setenv("GLHAC_INTAKE_CONCURRENCY", "1")

    def classify(name, text):
        if name == "slow.txt":
            time.sleep(0.3)   # 체크포인트 없는 외부 호출 흉내
        return classification(name, text)

    monkeypatch.setattr(intake, "classify", classify)
    output = intake.intake_zip(b"", files=[("slow.txt", b"slow"), ("next.txt", b"next")])
    docs = output["classified"]
    assert docs[0]["fields"]["_llm_error"] == "TIMEOUT"
    assert docs[1]["fields"] == {} and docs[1]["doc_type"] == "nib_business_license"
    assert not output["timed_out"]


@pytest.mark.parametrize("env,function,default", [
    ("GLHAC_INTAKE_FILE_TIMEOUT", intake.intake_file_deadline, 240),
    ("GLHAC_INTAKE_TOTAL_DEADLINE", intake.intake_total_deadline, 1800)])
@pytest.mark.parametrize("value", [None, "bad", "0", "-1", "nan", "inf", "0.5"])
def test_deadline_settings(monkeypatch, env, function, default, value):
    monkeypatch.delenv(env, raising=False)
    if value is not None:
        monkeypatch.setenv(env, value)
    before = time.monotonic()
    deadline = function()
    after = time.monotonic()
    seconds = 0.5 if value == "0.5" else default
    assert before + seconds <= deadline <= after + seconds
