"""Staff changes: real SQLite transactions, request identity, batching and delivery failures."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
import json
import re
import threading
import time
from types import SimpleNamespace

from fastapi import BackgroundTasks, Depends, FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine, event, inspect, update
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.orm.attributes import flag_modified

from app import auth, models, notify, schemas
from app import staff_change_notify as changes
from app.change_context import ChangeActorMiddleware, actor_context, automatic_changes, current_actor
from app.db import get_db
from app.case_write_service import case_snapshot, write_case
from app.intake_write_service import document_snapshot, write_document_fields
import app.main as m


STAFF = {"uid": "consultant", "role": "consultant", "org_id": "staff", "display_name": "Alex"}


@pytest.fixture
def db(tmp_path, monkeypatch):
    engine = create_engine("sqlite:///" + str(tmp_path / "changes.db"),
                           connect_args={"check_same_thread": False, "timeout": 10})

    @event.listens_for(engine, "connect")
    def pragmas(connection, _record):
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=WAL")

    models.Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False)
    monkeypatch.setenv("GLHAC_CHANGE_NOTIFY_DELAY_S", "300")
    monkeypatch.setenv("GLHAC_RATE_LIMIT_PER_MIN", "0")
    monkeypatch.setattr(m.auth, "dev_mode", lambda: True)
    with factory() as db:
        db.add_all([
            models.User(user_id="consultant", username="consultant@example.test", role="consultant",
                        display_name="Alex", password_hash="unused", org_id="staff"),
            models.User(user_id="operator", username="operator", role="operator", display_name="Pat",
                        password_hash="unused", org_id="staff"),
            models.User(user_id="admin", username="admin", role="admin", display_name="Sam",
                        password_hash="unused", org_id="staff"),
            models.User(user_id="applicant", username="company@example.test", role="applicant",
                        password_hash="unused", org_id="org", display_name="Company owner"),
        ])
        db.flush()
        db.add_all([models.Org(org_id="org", name="PT TEST", address="Before", profile_ext={"city": "Seoul"},
                               consultant_id="consultant"), models.Org(org_id="staff", name="Staff")])
        db.add(models.CaseApplication(case_id="case", org_id="org", company_name="PT TEST",
                                     address="Before", status="onboarding", profile_ext={}))
        db.flush()
        db.add_all([models.Material(material_id="mat", case_id="case", name="A"),
                    models.Product(product_id="prod", case_id="case", name="Juice")])
        db.flush()
        db.add_all([models.DocumentAsset(document_id="doc", case_id="case", filename="evidence.pdf",
                                         doc_type="product_photo", product_id="prod", fields={"city": "Seoul"}),
                    models.ProductMaterial(id="pm", case_id="case", product_id="prod", material_id="mat")])
        db.commit()
        yield db
    engine.dispose()


@pytest.fixture(autouse=True)
def mails(monkeypatch):
    sent = []
    monkeypatch.setenv("GLHAC_STAFF_MAIL_SYNC", "1")
    monkeypatch.setattr(changes.mail_delivery, "send", lambda *args, **kwargs: sent.append(args))
    return sent


@pytest.fixture
def async_mail_threads(monkeypatch, mails):
    monkeypatch.delenv("GLHAC_STAFF_MAIL_SYNC", raising=False)
    threads = []

    def thread(*args, **kwargs):
        worker = threading.Thread(*args, **kwargs)
        threads.append(worker)
        return worker

    monkeypatch.setattr(changes, "threading", SimpleNamespace(Thread=thread))
    yield threads
    for worker in threads:
        worker.join(timeout=5)
        assert not worker.is_alive()


@pytest.fixture
def clock(monkeypatch):
    value = [datetime(2026, 11, 10, 12)]
    monkeypatch.setattr(changes, "now", lambda: value[0])
    return value


@pytest.fixture
def client(db):
    factory = sessionmaker(bind=db.get_bind(), autoflush=False)

    def dependency():
        with factory() as session:
            yield session

    m.app.dependency_overrides[get_db] = dependency
    client = TestClient(m.app)
    yield client
    client.close()
    m.app.dependency_overrides.pop(get_db, None)


def headers(db, uid):
    return {"Authorization": "Bearer " + auth.make_token(db.get(models.User, uid))}


def records(db):
    return db.query(models.ChangeRecord).order_by(models.ChangeRecord.created_at, models.ChangeRecord.id).all()


def notices(db):
    return db.query(models.Notification).filter_by(event_type=changes.EVENT).all()


def rename(db, name, actor=STAFF):
    with actor_context(actor):
        db.get(models.Material, "mat").name = name
        db.commit()


@pytest.mark.parametrize("role", ["consultant", "operator", "admin", "auditor", "custom_staff", "penyelia_halal"])
def test_staff_document_delete_material_and_org_change(db, role):
    with actor_context({**STAFF, "role": role}):
        db.delete(db.get(models.DocumentAsset, "doc"))
        db.get(models.Material, "mat").name = "B"
        db.get(models.Org, "org").address = "After"
        db.commit()
    found = {r.resource_type: r for r in records(db)}
    assert set(found) == {"document_asset", "material", "org"}
    assert found["document_asset"].label == "evidence.pdf"
    assert found["document_asset"].op == "delete"
    assert found["material"].changes == {"name": {"before": "A", "after": "B"}}
    assert found["org"].changes == {"address": {"before": "Before", "after": "After"}}
    assert all(r.actor_role == role and r.org_id == "org" for r in found.values())
    assert found["org"].case_id is None


def test_self_background_sensitive_and_internal_changes_are_ignored(db):
    rename(db, "B", {"uid": "applicant", "role": "applicant", "org_id": "org"})
    db.get(models.Material, "mat").name = "C"
    db.commit()
    with actor_context(STAFF):
        u = db.get(models.User, "applicant")
        u.password_hash, u.token_version = "new-secret", 7
        d = db.get(models.DocumentAsset, "doc")
        d.content_b64, d.text_full, d.ocr_lines = "private-binary", "private-text", [{"text": "secret"}]
        d.translations = {"ko": "secret"}
        d.fields = {**d.fields, "_internal": "hidden", "api_key": "secret", "access_token": "secret"}
        db.get(models.Org, "org").consultant_id = None
        db.get(models.User, "consultant").display_name = "Different staff name"
        db.add(models.AuditFinding(case_id="case", finding="Internal audit"))
        db.add(models.Payment(case_id="case", amount=1))
        db.commit()
        finding = db.query(models.AuditFinding).one()
        finding.finding = "Other internal audit"
        db.commit()
    assert records(db) == []


def test_json_leaf_diff_deleted_keys_inplace_and_truncation(db):
    org = db.get(models.Org, "org")
    org.profile_ext = {"city": "Seoul", "removed": "old", "nested": {"name": "Before"}}
    db.commit()
    with actor_context(STAFF):
        org.profile_ext = {"city": "Jakarta", "nested": {"name": "After", "_secret": "hidden"},
                           "description": "x" * 400, "password_hash": "secret", "apiToken": "secret"}
        db.commit()
        org.profile_ext["city"] = "Bandung"
        flag_modified(org, "profile_ext")
        db.commit()
    first, second = records(db)
    assert first.changes["profile_ext.city"] == {"before": "Seoul", "after": "Jakarta"}
    assert first.changes["profile_ext.removed"] == {"before": "old", "after": None}
    assert first.changes["profile_ext.nested.name"] == {"before": "Before", "after": "After"}
    assert len(first.changes["profile_ext.description"]["after"]) == 200
    assert second.changes == {"profile_ext.city": {"before": "Jakarta", "after": "Bandung"}}
    assert "secret" not in json.dumps([r.changes for r in records(db)])


def test_sql_write_services_and_bulk_status_updates_capture_actual_committed_values(db):
    with actor_context(STAFF):
        c = db.get(models.CaseApplication, "case")
        write_case(db, c, base=case_snapshot(c), changes={"address": "After"}, source="test_manual")
        d = db.get(models.DocumentAsset, "doc")
        write_document_fields(db, d, base_fields=document_snapshot(d), new_fields={"city": "Jakarta"}, source="manual")
        db.query(models.CaseApplication).filter_by(case_id="case").update({"status": "consultant_review"}, synchronize_session=False)
        db.commit()
    found = records(db)
    assert len(found) == 3
    assert {"address": {"before": "Before", "after": "After"}} in [r.changes for r in found]
    assert {"fields.city": {"before": "Seoul", "after": "Jakarta"}} in [r.changes for r in found]
    assert {"status": {"before": "onboarding", "after": "consultant_review"}} in [r.changes for r in found]
    with actor_context(STAFF):
        db.execute(update(models.CaseApplication).where(models.CaseApplication.case_id == "missing").values(address="Never"))
        db.commit()
    assert len(records(db)) == 3


def test_ai_auto_writes_are_suppressed_but_manual_work_before_and_after_is_retained(db):
    with actor_context(STAFF):
        mat = db.get(models.Material, "mat")
        mat.name = "Manual"
        with automatic_changes(db):
            mat.name = "Automatic"
            c = db.get(models.CaseApplication, "case")
            write_case(db, c, base=case_snapshot(c), changes={"address": "AI address"}, source="test_ai")
        mat.name = "Final manual"
        db.commit()
    assert [r.changes for r in records(db)] == [
        {"name": {"before": "A", "after": "Manual"}},
        {"name": {"before": "Automatic", "after": "Final manual"}}]


def test_reprocess_by_logged_in_staff_does_not_capture_ai_results(db, monkeypatch):
    doc = db.get(models.DocumentAsset, "doc")
    doc.content_b64 = "eA=="
    db.commit()
    monkeypatch.setattr(m.intake, "parse_file", lambda *a, **k: "Extracted text")
    monkeypatch.setattr(m.intake, "classify", lambda *a, **k: {
        "doc_type": "other", "confidence": 0.9, "fields": {"city": "Jakarta"}})
    monkeypatch.setattr(m, "verify_enqueue", lambda *a, **k: None)
    with actor_context(STAFF):
        m._reprocess_doc(db, doc, db.get(models.CaseApplication, "case"), apply=False,
                         actor_role="consultant", actor_id="consultant")
    db.refresh(doc)
    assert doc.doc_type == "other" and doc.fields["city"] == "Jakarta"
    assert records(db) == []


@pytest.mark.parametrize("final,expected", [("C", [{"name": {"before": "A", "after": "C"}}]), ("A", [])])
def test_merge_and_revert(db, clock, final, expected):
    rename(db, "B")
    clock[0] += timedelta(seconds=1)
    rename(db, final)
    assert [r["changes"] for r in changes.merge_records(records(db))] == expected
    clock[0] += timedelta(seconds=300)
    assert changes.queue_staff_change_notifications(db) == bool(expected)
    assert all(r.notified_at for r in records(db))
    assert len(notices(db)) == bool(expected)


def test_long_values_with_equal_prefix_are_not_mistaken_for_reversion(db, clock):
    mat = db.get(models.Material, "mat")
    mat.note = "x" * 200 + "A"
    db.commit()
    with actor_context(STAFF):
        mat.note = "x" * 200 + "B"
        db.commit()
    merged = changes.merge_records(records(db))
    assert len(merged) == 1 and "note" in merged[0]["changes"]


def test_debounce_one_email_retry_and_no_duplicates(db, clock, monkeypatch):
    calls = []

    def delivery(contacts, text, notification):
        calls.append((contacts, text))
        return {"channel": "email", "ok": len(calls) > 1, "reason": "temporary"}

    monkeypatch.setitem(notify.PROVIDERS, "email", delivery)
    rename(db, "B")
    clock[0] += timedelta(seconds=299)
    assert changes.queue_staff_change_notifications(db) == 0
    rename(db, "C")
    clock[0] += timedelta(seconds=299)
    assert changes.queue_staff_change_notifications(db) == 0
    clock[0] += timedelta(seconds=1)
    assert changes.queue_staff_change_notifications(db) == 1
    n = notices(db)[0]
    assert n.channels == ["inapp", "email"]
    assert "이름: A → C" in n.body and "B → C" not in n.body
    m.drain_notifications(db)
    assert n.status == "partial" and n.retry_pending
    assert n.channel_status["inapp"]["attempts"] == 1
    m.drain_notifications(db)
    m.drain_notifications(db)
    assert changes.queue_staff_change_notifications(db) == 0
    assert len(notices(db)) == 1 and len(calls) == 2
    assert calls[0][0]["email"] == "company@example.test"
    assert n.status == "sent" and not n.retry_pending
    assert n.channel_status["inapp"]["attempts"] == 1
    assert [log.status for log in db.query(models.MailLog).order_by(models.MailLog.created_at)] == ["failed", "sent"]


def test_parallel_workers_only_queue_once(db, clock):
    rename(db, "B")
    clock[0] += timedelta(seconds=300)
    factory = sessionmaker(bind=db.get_bind(), autoflush=False)
    barrier = threading.Barrier(2)

    def queue():
        with factory() as other:
            barrier.wait(timeout=5)
            return changes.queue_staff_change_notifications(other)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(queue) for _ in range(2)]
        assert sorted(f.result(timeout=15) for f in futures) == [0, 1]
    assert len(notices(db)) == 1


def test_enqueue_failure_rolls_back_notified_marker(db, clock, monkeypatch):
    rename(db, "B")
    clock[0] += timedelta(seconds=300)
    original = m._notify
    monkeypatch.setattr(m, "_notify", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("queue failure")))
    with pytest.raises(RuntimeError):
        changes.queue_staff_change_notifications(db)
    assert records(db)[0].notified_at is None and notices(db) == []
    monkeypatch.setattr(m, "_notify", original)
    assert changes.queue_staff_change_notifications(db) == 1


@pytest.mark.parametrize("failed_stages", [
    ("queue_problem_document_notifications",),
    ("queue_staff_change_notifications",),
    ("drain_notifications",),
    ("queue_problem_document_notifications", "queue_staff_change_notifications", "drain_notifications"),
])
def test_notification_worker_continues_same_iteration_after_stage_failure(monkeypatch, caplog, failed_stages):
    calls = []
    session = SimpleNamespace(rollback=lambda: calls.append("rollback"), close=lambda: calls.append("close"))
    monkeypatch.setattr(m, "SessionLocal", lambda: session)

    class StopWorker(BaseException):
        pass

    def sleep(interval):
        assert interval == 7
        if calls:
            raise StopWorker()

    monkeypatch.setattr(time, "sleep", sleep)
    stages = ("queue_problem_document_notifications", "queue_staff_change_notifications", "drain_notifications")

    def stage(name):
        def run(db):
            assert db is session
            calls.append(name)
            if name in failed_stages:
                raise RuntimeError(name + " failed")
        return run

    for name in stages:
        monkeypatch.setattr(m, name, stage(name))
    with pytest.raises(StopWorker):
        m._notify_worker_loop(interval=7)
    expected = []
    for name in stages:
        expected.append(name)
        if name in failed_stages:
            expected.append("rollback")
            assert any(name in record.message and record.exc_info for record in caplog.records)
    assert calls == expected + ["close"]


def test_immediate_preparation_failure_preserves_deletion_and_pending_records(db, clock, mails, monkeypatch, caplog):
    original = changes._prepare_immediate
    db.info["unrelated"] = "keep"

    def fail(session):
        assert session.info["staff_immediate"]
        session.info["staff_outbox"] = [{"partial": True}]
        raise RuntimeError("preparation unavailable")

    monkeypatch.setattr(changes, "_prepare_immediate", fail)
    with actor_context(STAFF):
        db.delete(db.get(models.CaseApplication, "case"))
        db.commit()
    with Session(db.get_bind()) as other:
        assert other.get(models.CaseApplication, "case") is None
        pending = records(other)
        assert pending and all(row.notified_at is None for row in pending)
    assert mails == []
    assert "staff_immediate" not in db.info and "staff_outbox" not in db.info
    assert db.info["unrelated"] == "keep"
    assert any("_prepare_immediate" in row.message and row.exc_info for row in caplog.records)
    monkeypatch.setattr(changes, "_prepare_immediate", original)
    clock[0] += timedelta(seconds=300)
    assert changes.queue_staff_change_notifications(db) == 1
    assert all(row.notified_at for row in records(db))


@pytest.mark.parametrize("failure", ["second_snapshot", "timestamp", "second_marker"])
def test_immediate_preparation_failure_never_leaves_partial_markers(db, mails, monkeypatch, caplog, failure):
    db.add(models.CaseApplication(case_id="second", org_id="org", company_name="Second"))
    db.commit()
    merged = []
    marked = []
    original_merge = changes.merge_records

    def merge(rows):
        merged.append(rows)
        if failure == "second_snapshot" and len(merged) == 2:
            raise RuntimeError("second snapshot failed")
        return original_merge(rows)

    def fail_clock():
        raise RuntimeError("timestamp failed")

    def fail_mark(record, value, oldvalue, initiator):
        if value is not None:
            marked.append(record.id)
            if len(marked) == 2:
                raise RuntimeError("second marker failed")

    with actor_context(STAFF):
        db.delete(db.get(models.CaseApplication, "case"))
        db.delete(db.get(models.CaseApplication, "second"))
        db.flush()
        assert len(db.info["staff_immediate"]) == 2
        monkeypatch.setattr(changes, "merge_records", merge)
        if failure == "timestamp":
            monkeypatch.setattr(changes, "now", fail_clock)
        if failure == "second_marker":
            event.listen(models.ChangeRecord.notified_at, "set", fail_mark)
        try:
            db.commit()
        finally:
            if failure == "second_marker":
                event.remove(models.ChangeRecord.notified_at, "set", fail_mark)
    assert len(merged) == 2 and all(merged)
    if failure == "second_marker":
        assert len(marked) == 2
    with Session(db.get_bind()) as other:
        assert other.get(models.CaseApplication, "case") is None
        assert other.get(models.CaseApplication, "second") is None
        pending = records(other)
        assert pending and all(row.notified_at is None for row in pending)
    assert mails == []
    assert "_prepare_immediate failed" in caplog.text


@pytest.mark.parametrize("fallback", [True, False])
def test_no_applicant_uses_only_assigned_consultant_or_warns(db, clock, fallback, caplog):
    db.delete(db.get(models.User, "applicant"))
    if not fallback:
        db.get(models.Org, "org").consultant_id = None
    db.commit()
    with actor_context(STAFF):
        db.get(models.Org, "org").address = "After"
        db.commit()
    clock[0] += timedelta(seconds=300)
    assert changes.queue_staff_change_notifications(db) == int(fallback)
    if fallback:
        note = notices(db)[0]
        assert note.case_id is None and note.role == "consultant"
        assert note.payload["recipient_user_id"] == "consultant"
        assert m._notification_contacts(db, note) == ({"email": "consultant@example.test"}, True)
        assert note in m._my_notifs(db, {**STAFF, "uid": "consultant"})
    else:
        assert "no recipient" in caplog.text
    assert all(r.notified_at for r in records(db))


def test_org_notice_targets_each_applicant_without_case(db, clock, client):
    db.add(models.User(user_id="second", username="second@example.test", password_hash="unused", role="applicant", org_id="org"))
    db.commit()
    with actor_context(STAFF):
        db.get(models.Org, "org").address = "After"
        db.commit()
    clock[0] += timedelta(seconds=300)
    assert changes.queue_staff_change_notifications(db) == 2
    all_notes = notices(db)
    assert all(n.case_id is None for n in all_notes)
    own = m._my_notifs(db, {"uid": "applicant", "role": "applicant", "org_id": "org"})
    assert len(own) == 1
    other = next(n for n in all_notes if n not in own)
    response = client.post("/notifications/" + other.notification_id + "/read", headers=headers(db, "applicant"))
    assert response.status_code == 403


@pytest.mark.parametrize("failure", [False, True])
def test_case_purge_sends_after_commit_preserves_records_and_mail_failure_does_not_fail_deletion(db, mails, monkeypatch, failure):
    calls = []
    engine = db.get_bind()

    def send(*args):
        with Session(engine) as other:
            assert other.get(models.CaseApplication, "case") is None
        calls.append(args)
        if failure:
            raise RuntimeError("mail unavailable")

    monkeypatch.setattr(changes.mail_delivery, "send", send)
    with actor_context({**STAFF, "role": "admin"}):
        result = m.admin_cases_purge(schemas.CasePurgeReq(keep=[], dry_run=False, confirm="PURGE", reason="Cleanup",
                                                        vacuum=False, sweep_uploads=False),
                                     user={**STAFF, "role": "admin"}, db=db)
    assert result["deleted_rows"]["case_application"] == 1
    assert len(calls) == 1 and calls[0][0] == "company@example.test"
    assert "evidence.pdf" in calls[0][2] and "Juice" in calls[0][2] and "#case" in calls[0][2]
    found = records(db)
    assert {r.resource_type for r in found} >= {"case_application", "document_asset", "material", "product", "product_material"}
    assert len({(r.resource_type, r.resource_id, r.op) for r in found}) == len(found)
    assert all(r.notified_at for r in found)
    assert db.query(models.MailLog).one().status == ("failed" if failure else "sent")
    assert changes.queue_staff_change_notifications(db) == 0


def test_account_delete_snapshots_email_and_pending_org_changes(db, mails):
    with actor_context(STAFF):
        db.get(models.Org, "org").address = "After"
        db.commit()
        m.admin_delete_user("applicant", user={**STAFF, "role": "admin"}, db=db)
    assert db.get(models.User, "applicant") is None
    assert len(mails) == 1 and mails[0][0] == "company@example.test"
    assert "Before → After" in mails[0][2] and "Company owner" in mails[0][2]
    assert all(r.notified_at for r in records(db))


def test_account_delete_http_uses_authenticated_actor(db, client, mails):
    response = client.delete("/admin/users/applicant", headers=headers(db, "admin"))
    assert response.status_code == 200, response.text
    assert len(mails) == 1 and mails[0][0] == "company@example.test"
    assert records(db)[0].actor_id == "admin"


def test_account_delete_http_does_not_wait_for_smtp(db, client, monkeypatch, async_mail_threads):
    started, finished = threading.Event(), threading.Event()
    worker_calls = []
    original_add = changes.mail_log.add

    def slow_send(*args, **kwargs):
        worker_calls.append(("smtp", threading.current_thread()))
        started.set()
        time.sleep(1)
        finished.set()

    def add(log_db, *args, **kwargs):
        assert log_db is not db
        worker_calls.append(("mail_log", threading.current_thread()))
        return original_add(log_db, *args, **kwargs)

    monkeypatch.setattr(changes.mail_delivery, "send", slow_send)
    monkeypatch.setattr(changes.mail_log, "add", add)
    request_headers = headers(db, "admin")
    before = time.monotonic()
    response = client.delete("/admin/users/applicant", headers=request_headers)
    elapsed = time.monotonic() - before
    assert response.status_code == 200, response.text
    assert elapsed < 0.75
    assert started.wait(timeout=2) and not finished.is_set()
    assert len(async_mail_threads) == 1 and async_mail_threads[0].daemon
    async_mail_threads[0].join(timeout=5)
    assert not async_mail_threads[0].is_alive() and finished.is_set()
    assert worker_calls == [("smtp", async_mail_threads[0]), ("mail_log", async_mail_threads[0])]
    assert db.get(models.User, "applicant") is None
    assert db.query(models.MailLog).one().status == "sent"


@pytest.mark.parametrize("failure", ["render", "smtp", "mail_log"])
def test_async_mail_errors_are_logged_without_failing_deletion(
        db, client, monkeypatch, async_mail_threads, caplog, failure):
    def fail(*args, **kwargs):
        raise RuntimeError("injected " + failure + " failure")

    target, name = {"render": (changes, "render"), "smtp": (changes.mail_delivery, "send"),
                    "mail_log": (changes.mail_log, "add")}[failure]
    monkeypatch.setattr(target, name, fail)
    response = client.delete("/admin/users/applicant", headers=headers(db, "admin"))
    assert response.status_code == 200, response.text
    assert len(async_mail_threads) == 1
    async_mail_threads[0].join(timeout=5)
    assert not async_mail_threads[0].is_alive()
    assert db.get(models.User, "applicant") is None
    assert any(row.exc_info and "injected " + failure in str(row.exc_info[1]) for row in caplog.records)
    if failure == "smtp":
        assert db.query(models.MailLog).one().status == "failed"


@pytest.mark.parametrize("fallback", [True, False])
def test_case_delete_without_applicant_uses_fallback_or_records_only(db, mails, fallback, caplog):
    db.delete(db.get(models.User, "applicant"))
    if not fallback:
        db.get(models.Org, "org").consultant_id = None
    db.commit()
    with actor_context(STAFF):
        db.delete(db.get(models.CaseApplication, "case"))
        db.commit()
    assert len(mails) == int(fallback)
    if fallback:
        assert mails[0][0] == "consultant@example.test"
    else:
        assert "no recipient" in caplog.text
    assert all(r.notified_at for r in records(db))


def test_material_and_product_bulk_delete_links_keep_human_labels(db, client):
    response = client.delete("/materials/mat", headers=headers(db, "consultant"))
    assert response.status_code == 200, response.text
    rows = records(db)
    assert {r.resource_type for r in rows} == {"material", "product_material"}
    assert next(r for r in rows if r.resource_type == "product_material").label == "Juice / A"
    assert all(r.op == "delete" and r.notified_at is None for r in rows)


def test_org_purge_snapshots_consultant_and_child_names(db, mails):
    db.query(models.User).filter_by(user_id="applicant").delete()
    db.query(models.ProductMaterial).delete()
    db.query(models.DocumentAsset).delete()
    db.query(models.Product).delete()
    db.query(models.Material).delete()
    db.query(models.CaseApplication).delete()
    db.add(models.Facility(facility_id="factory", org_id="org", name="Factory 1"))
    db.commit()
    with actor_context(STAFF):
        result = m.admin_purge_orphan_orgs({"org_ids": ["org"], "dry_run": False, "confirm": "PURGE", "reason": "cleanup"},
                                          user={**STAFF, "role": "admin"}, db=db)
    assert result["deleted"] == ["org"]
    assert len(mails) == 1 and mails[0][0] == "consultant@example.test"
    assert "Factory 1" in mails[0][2]
    assert {r.resource_type for r in records(db)} == {"org", "facility"}
    assert all(r.notified_at for r in records(db))


def test_rollback_and_savepoints_do_not_send_mail_or_leave_records(db, mails):
    with actor_context(STAFF):
        # pysqlite otherwise treats the first SAVEPOINT as a root transaction and RELEASE commits it.
        m.database.lock_chain_tail(db, "test-outer-transaction")
        with pytest.raises(RuntimeError):
            with db.begin_nested():
                db.delete(db.get(models.User, "applicant"))
                db.flush()
                raise RuntimeError("abort savepoint")
        db.commit()
        assert records(db) == [] and mails == []
        m.database.lock_chain_tail(db, "test-outer-transaction")
        with db.begin_nested():
            db.delete(db.get(models.User, "applicant"))
            db.flush()
        assert mails == []
        db.rollback()
    assert records(db) == [] and mails == []


def test_successful_savepoint_sends_only_on_outer_commit(db, mails):
    with actor_context(STAFF):
        m.database.lock_chain_tail(db, "test-outer-transaction")
        with db.begin_nested():
            db.delete(db.get(models.User, "applicant"))
            db.flush()
        assert mails == []
        db.commit()
    assert len(mails) == 1 and all(r.notified_at for r in records(db))


def test_account_fields_include_role_change_and_exclude_credentials(db):
    with actor_context(STAFF):
        user = db.get(models.User, "applicant")
        user.role, user.contact_email, user.password_hash = "consultant", "new@example.test", "never-store"
        db.commit()
    assert records(db)[0].changes == {
        "role": {"before": "applicant", "after": "consultant"},
        "contact_email": {"before": None, "after": "new@example.test"}}


def test_authenticated_http_staff_identity_self_and_request_cleanup(db, client):
    response = client.delete("/cases/case/products/prod/photos/doc", headers=headers(db, "consultant"))
    assert response.status_code == 200, response.text
    record = records(db)[0]
    assert record.actor_id == "consultant" and record.actor_name == "Alex"
    assert "@" not in record.actor_name
    response = client.patch("/admin/orgs/org", json={"name": "Renamed"}, headers=headers(db, "operator"))
    assert response.status_code == 200, response.text
    assert records(db)[-1].actor_role == "operator"
    response = client.patch("/materials/mat", json={"name": "Self"}, headers=headers(db, "applicant"))
    assert response.status_code == 200, response.text
    assert len(records(db)) == 2
    assert current_actor() is None
    db.get(models.Org, "org").address = "Background after request"
    db.commit()
    assert len(records(db)) == 2


def test_asgi_background_task_has_no_actor_and_requests_do_not_leak(db):
    application = FastAPI()
    application.add_middleware(ChangeActorMiddleware)
    factory = sessionmaker(bind=db.get_bind(), autoflush=False)

    def dependency():
        with factory() as session:
            yield session

    application.dependency_overrides[get_db] = dependency

    def background():
        assert current_actor() is None
        with factory() as session:
            session.get(models.Org, "org").address = "Background"
            session.commit()

    @application.post("/edit")
    def edit(tasks: BackgroundTasks, user=Depends(auth.get_current_user), session=Depends(get_db)):
        session.get(models.Org, "org").address = user["role"]
        session.commit()
        tasks.add_task(background)
        return {}

    with TestClient(application) as client:
        assert client.post("/edit", headers=headers(db, "consultant")).status_code == 200
        assert client.post("/edit", headers=headers(db, "applicant")).status_code == 200
    assert len(records(db)) == 1 and records(db)[0].changes["address"]["after"] == "consultant"


@pytest.mark.parametrize("lang", ["ko", "en", "id"])
def test_localized_content_actor_privacy_and_field_names(db, clock, lang):
    org = db.get(models.Org, "org")
    org.profile_ext = {"lang": lang}
    db.commit()
    rename(db, "B", {**STAFF, "display_name": "private@example.test"})
    clock[0] += timedelta(seconds=300)
    changes.queue_staff_change_notifications(db)
    note = notices(db)[0]
    title, body = m.notify_text(note, lang)
    assert (title, body) == (note.title, note.body)
    assert "A → B" in body and "UTC" in body and "private@example.test" not in body
    assert ("이름" if lang == "ko" else "Name" if lang == "en" else "Nama") in body
    if lang != "ko":
        assert not re.search("[가-힣]", title + body)


def test_create_all_upgrades_existing_sqlite_and_purge_excludes_history(db):
    engine = db.get_bind()
    models.ChangeRecord.__table__.drop(engine)
    assert "change_record" not in inspect(engine).get_table_names()
    models.Base.metadata.create_all(engine)
    assert "change_record" in inspect(engine).get_table_names()
    assert inspect(engine).get_foreign_keys("change_record") == []
    assert "change_record" not in dict(m._case_scoped_models())
    assert "change_record" not in dict(m._org_scoped_models())
    assert "mail_log" not in dict(m._case_scoped_models())


def test_tracked_column_labels_are_translated():
    from app.staff_change_text import FIELDS
    for model in changes.TRACKED_MODELS:
        assert changes._columns(model) <= FIELDS.keys(), model.__name__
