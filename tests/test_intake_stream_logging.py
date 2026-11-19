"""SSE의 HTTP 200 뒤에 숨은 실패·부분 완료를 원문 없이 운영 로그에 남긴다."""
import base64
import io
import json
import logging
import re
import time
import zipfile
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app import intake
import app.main as m


def archive(entries):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, raw in entries:
            z.writestr(name, raw)
    return buf.getvalue()


@pytest.fixture
def case(monkeypatch):
    monkeypatch.setattr(m, "verify_enqueue", lambda did: False)
    monkeypatch.setattr(intake, "parse_file", lambda name, raw, **kw: raw.decode())
    monkeypatch.setattr(intake, "classify", lambda *a: {
        "doc_type": "other", "confidence": 0.9, "fields": {}})
    monkeypatch.setattr(intake.ai_local, "llm_json", lambda *a, **kw: {"error": "LLM_UNAVAILABLE"})
    with TestClient(m.app) as client:
        token = client.post("/auth/login", json={"username": "applicant1", "password": "pw"}).json()["token"]
        headers = {"Authorization": "Bearer " + token}
        cid = client.post("/cases", headers=headers, json={"company_name": "Synthetic Private Company"}).json()["case_id"]
        yield client, headers, cid
        if m._autoparse_q:
            m._autoparse_q.join()


def post_zip(case, raw):
    client, headers, cid = case
    return client.post(f"/cases/{cid}/intake-zip-stream", headers=headers,
                       json={"zip_b64": base64.b64encode(raw).decode()})


def events(response):
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    return [json.loads(line[5:]) for line in response.text.splitlines() if line.startswith("data:")]


def stream_log(caplog, cid):
    records = [r for r in caplog.records if r.name == "glhac" and r.getMessage().startswith("ZIP 인테이크")]
    assert len(records) == 1
    record = records[0]
    message = record.getMessage()
    assert f"case_id={cid}" in message
    assert "role=applicant" in message
    assert re.search(r"\belapsed=\d+\.\ds\b", message)
    assert "Synthetic Private Company" not in message
    return record


@pytest.mark.parametrize("reason", ["archive_error", "archive"])
def test_archive_timeout_logs_warning_before_error_event(case, monkeypatch, caplog, reason):
    now = [100.0]
    # main만 가짜 시계를 사용한다. 클라이언트·스레드·logging의 시계는 유지한다.
    monkeypatch.setattr(m, "time", SimpleNamespace(**{**vars(time), "monotonic": lambda: now[0]}))
    message = "압축 요청 전체 처리 시간이 초과되었습니다"

    def timeout(*args, **kwargs):
        now[0] = 112.34
        raise intake.ArchiveError(message)

    target = "_validate_archive_upload" if reason == "archive_error" else "_intake_zip_events"
    monkeypatch.setattr(m, target, timeout)
    with caplog.at_level(logging.INFO, logger="glhac"):
        response = post_zip(case, archive([("document.txt", b"synthetic")]))
    assert events(response) == [{"type": "error", "message": message}]
    record = stream_log(caplog, case[2])
    assert record.levelno == logging.WARNING
    assert record.getMessage().startswith("ZIP 인테이크 실패")
    assert f"reason={reason} message={message}" in record.getMessage()
    assert "elapsed=12.3s" in record.getMessage()
    assert case[0].get(f"/cases/{case[2]}/documents", headers=case[1]).json() == []


@pytest.mark.parametrize("guard", ["path", "size"])
def test_archive_error_logs_omit_private_filenames(case, monkeypatch, caplog, guard):
    name = "비공개업체-서류.txt"
    if guard == "path":
        name = "../" + name
    else:
        monkeypatch.setattr(intake, "ZIP_MAX_SINGLE_FILE", 1)
    with caplog.at_level(logging.INFO, logger="glhac"):
        output = events(post_zip(case, archive([(name, b"private document body")])))
    assert output[0]["type"] == "error" and name in output[0]["message"]
    record = stream_log(caplog, case[2])
    assert record.levelno == logging.WARNING
    assert "reason=archive_error" in record.getMessage()
    assert "비공개업체" not in caplog.text
    assert "private document body" not in caplog.text


@pytest.mark.parametrize("reason", ["bad_inner_file", "no_result"])
def test_other_error_events_log_warning_without_private_detail(case, monkeypatch, caplog, reason):
    detail = {"code": "CONTENT_MISMATCH", "filename": "private-company.pdf", "content": "private body"}

    def failed_events(*args, **kwargs):
        if reason == "bad_inner_file":
            raise HTTPException(415, detail)
        return iter(())

    monkeypatch.setattr(m, "_intake_zip_events", failed_events)
    with caplog.at_level(logging.INFO, logger="glhac"):
        output = events(post_zip(case, archive([("document.txt", b"synthetic")])))
    expected = {"type": "error", "message": "인테이크 결과가 없습니다"}
    if reason == "bad_inner_file":
        expected.update(status=415, message="압축 내부 파일 형식이 올바르지 않습니다", detail=detail)
    assert output == [expected]
    record = stream_log(caplog, case[2])
    assert record.levelno == logging.WARNING
    assert record.getMessage().startswith("ZIP 인테이크 실패")
    assert f"reason={reason} message={expected['message']}" in record.getMessage()
    assert "private-company.pdf" not in caplog.text
    assert "private body" not in caplog.text


def test_success_logs_saved_documents_and_skip_counts_only(case, monkeypatch, caplog):
    entries = [("private-first.txt", b"private document body"),
               ("private-copy.txt", b"private document body"),
               ("private-second.txt", b"another private body"),
               (".DS_Store", b"metadata"), ("private-program.exe", b"unsupported")]
    raw = archive(entries)
    enqueued = []
    monkeypatch.setattr(m, "verify_enqueue", enqueued.append)
    with caplog.at_level(logging.INFO, logger="glhac"):
        output = events(post_zip(case, raw))
    assert [event["type"] for event in output] == ["start", "progress", "progress", "progress", "result"]
    result = output[-1]
    assert result["file_count"] == 3 and result["skipped_entries"] == 2
    assert result["applied"]["dup_skipped"] == 1
    assert "_verify_ids" not in result
    documents = case[0].get(f"/cases/{case[2]}/documents", headers=case[1]).json()
    assert len(documents) == len(enqueued) == len(set(enqueued)) == 2
    record = stream_log(caplog, case[2])
    assert record.levelno == logging.INFO
    message = record.getMessage()
    assert message.startswith("ZIP 인테이크 완료")
    for field in ("file_count=3", "saved_documents=2", "skipped=2", "METADATA 1",
                  "UNSUPPORTED_TYPE 1", "_ocr_error=0", "_llm_error=0"):
        assert field in message
    for name, content in entries:
        assert name not in message
        assert content.decode() not in message
        assert base64.b64encode(content).decode() not in message
    assert base64.b64encode(raw).decode() not in message


@pytest.mark.parametrize("problem", ["TIME_LIMIT", "_ocr_error", "_llm_error"])
def test_partial_or_error_documents_make_summary_warning(case, monkeypatch, caplog, problem):
    if problem == "TIME_LIMIT":
        original = intake._intake_documents

        def partial(files, stopped, deadline):
            yield from original(files[:1], stopped, deadline)
            raise intake.IntakeTotalTimeout({"filename": name, "reason": "TIME_LIMIT"}
                                           for name, _ in files[1:])

        monkeypatch.setattr(intake, "_intake_documents", partial)
    else:
        monkeypatch.setattr(intake, "classify", lambda name, text: {
            "doc_type": "other", "confidence": 0.9,
            "fields": {problem: "PRIVATE ERROR DETAIL"} if name == "2.txt" else {}})
    with caplog.at_level(logging.INFO, logger="glhac"):
        output = events(post_zip(case, archive([(f"{i}.txt", str(i).encode()) for i in (1, 2, 3)])))
    assert output[-1]["type"] == "result"
    record = stream_log(caplog, case[2])
    assert record.levelno == logging.WARNING
    message = record.getMessage()
    assert message.startswith("ZIP 인테이크 완료")
    assert "file_count=3" in message
    if problem == "TIME_LIMIT":
        assert output[-1]["timed_out"] is True
        assert output[-1]["skipped_entries"] == 2
        assert "saved_documents=1 skipped=2 skipped_reasons=TIME_LIMIT 2" in message
    else:
        assert "saved_documents=3 skipped=0" in message
    assert f"_ocr_error={int(problem == '_ocr_error')}" in message
    assert f"_llm_error={int(problem == '_llm_error')}" in message
    assert "PRIVATE ERROR DETAIL" not in caplog.text
    assert all(f"{i}.txt" not in message for i in (1, 2, 3))


def test_disconnect_logs_warning_and_keeps_empty_stream(case, monkeypatch, caplog):
    def disconnected(raw, files, stopped, **kwargs):
        stopped.set()
        return iter(())

    monkeypatch.setattr(m, "_intake_zip_events", disconnected)
    with caplog.at_level(logging.INFO, logger="glhac"):
        output = events(post_zip(case, archive([("document.txt", b"synthetic")])))
    assert output == []
    record = stream_log(caplog, case[2])
    assert record.levelno == logging.WARNING
    assert record.getMessage().startswith("ZIP 인테이크 중단(연결 끊김)")


@pytest.mark.parametrize("target", ["_intake_zip_events", "_apply_intake_autofill"])
def test_unexpected_exception_logs_traceback_and_reraises_original(case, monkeypatch, caplog, target):
    failure = RuntimeError("PRIVATE EXCEPTION BODY")
    cause = ValueError("PRIVATE CHAINED BODY")

    def fail(*args, **kwargs):
        raise failure from cause

    monkeypatch.setattr(m, target, fail)
    with caplog.at_level(logging.INFO, logger="glhac"), pytest.raises(RuntimeError) as caught:
        post_zip(case, archive([("document.txt", b"synthetic")]))
    assert caught.value is failure
    assert caught.value.__cause__ is cause
    record = stream_log(caplog, case[2])
    assert record.levelno == logging.ERROR
    assert record.getMessage().startswith("ZIP 인테이크 예외")
    assert record.exc_info is not None and record.exc_info[2] is not None
    # 전역 예외 핸들러의 별도 로그가 아니라 이번에 추가한 레코드를 검증한다.
    logged = logging.Formatter().format(record)
    assert "Traceback" in logged and "RuntimeError" in logged
    assert "PRIVATE EXCEPTION BODY" not in logged
    assert "PRIVATE CHAINED BODY" not in logged
