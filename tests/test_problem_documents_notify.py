"""문제 서류: 결정적 판정, 케이스 묶음, 대기·중복 방지, 수신자·언어·권한 회귀."""
import copy
from datetime import datetime, timedelta
from email import message_from_string
from pathlib import Path
import re
import smtplib
import subprocess

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import auth, models, notify as nt
from app.db import get_db
import app.main as m


def document(**kw):
    data = dict(document_id="d1", case_id="case1", filename="evidence.pdf", doc_type="coa_msds",
                content_b64="eA==", text_full="Original text", text_excerpt="Original text",
                confidence=0.95, fields={}, created_at=datetime(2026, 11, 1, 3, 4, 5))
    data.update(kw)
    return models.DocumentAsset(**data)


@pytest.mark.parametrize("code,changes", [
    ("OCR_FAILED", {"fields": {"_ocr_error": "TIMEOUT"}}),
    ("OCR_FAILED", {"fields": {"_ocr_error": "ERROR"}}),
    ("ENCRYPTED", {"fields": {"_ocr_error": "ENCRYPTED"}}),
    ("CORRUPT", {"fields": {"_ocr_error": "CORRUPT"}}),
    ("EMPTY_TEXT", {"text_full": "", "text_excerpt": ""}),
    ("LOW_CONFIDENCE", {"confidence": m.PKG_UNREADABLE_CONF - 0.01}),
    ("TRUNCATED", {"fields": {"_truncated": {"kind": "pages", "limit": 40, "total": 60}}}),
    ("VERIFY_FAILED", {"fields": {"_verify": {"verdict": "fail", "field_issues": [
        {"field": "nib", "issue": "wrong number"}]}}}),
    ("OWNERSHIP_MISMATCH", {"fields": {"_verify": {"verdict": "warn", "field_issues": [
        {"field": "company_name", "code": "company_name_mismatch", "issue": "different company"}]}}}),
    ("AI_ERROR", {"fields": {"_llm_error": "unavailable"}}),
    ("PORK_DNA_UNCLEAR", {}),
])
def test_each_reason_is_pure(code, changes):
    d = document(**changes)
    before = copy.deepcopy(d.fields)
    measurements = [models.MaterialMeasurement(document_id="d1", param_key="pork_dna", verdict="review")] if code == "PORK_DNA_UNCLEAR" else []
    reasons = m._problem_document_reasons(d, measurements)
    assert [r["code"] for r in reasons] == [code]
    assert d.fields == before
    assert m._problem_document_reasons(d, measurements) == reasons
    for lang in ("ko", "en", "id"):
        assert len(m.PROBLEM_DOC_MSG[code][lang]) == 3
        text = m._problem_document_explanation(reasons[0], lang)
        assert text and "{" not in text
        if lang != "ko":
            assert not re.search("[가-힣]", text)
        if code == "VERIFY_FAILED":
            assert "nib" in text and "wrong number" in text
        if code == "TRUNCATED":
            assert "40" in text


def test_normal_and_precedence_and_multiple_reasons():
    assert m._problem_document_reasons(document(confidence=m.PKG_UNREADABLE_CONF)) == []
    assert m._problem_document_reasons(document(content_b64=None, text_full="", text_excerpt="", confidence=None)) == []
    assert m._problem_document_reasons(document(), [models.MaterialMeasurement(document_id="other", verdict="review")]) == []
    d = document(confidence=0, text_full="", fields={
        "_ocr_error": "ENCRYPTED", "_truncated": {"kind": "chars", "limit": 20000, "total": 30000},
        "_verify": {"verdict": "fail", "summary": "value missing"}, "_llm_error": "timeout"})
    assert [r["code"] for r in m._problem_document_reasons(d)] == ["ENCRYPTED", "TRUNCATED", "VERIFY_FAILED", "AI_ERROR"]
    # 원본 전문이 명시적으로 비면 옛 발췌가 남아 있어도 빈 본문이다.
    assert [r["code"] for r in m._problem_document_reasons(document(text_full="", confidence=0))] == ["EMPTY_TEXT"]
    assert [r["code"] for r in m._problem_document_reasons(document(text_full=None))] == []


@pytest.mark.parametrize("reason", ["nib_conflict", "company_conflict", "ownership_unconfirmed", "factory_conflict"])
def test_actual_h1_h3_ownership_keys(reason):
    conflict = {"field": "nib", "reason": reason, "status": "pending"}
    d = document(fields={"_autofill": {"conflicts": [conflict]}})
    rows = m._problem_document_reasons(d)
    assert [r["code"] for r in rows] == ["OWNERSHIP_MISMATCH"]
    assert reason in rows[0]["params"]["fields"]
    d.fields = {"_autofill": {"conflicts": [{**conflict, "status": "confirmed"}]}}
    assert m._problem_document_reasons(d) == []


def test_truncation_keeps_all_limits():
    markers = [{"kind": "pages", "limit": 40, "total": 60}, {"kind": "chars", "limit": 20000, "total": 30000},
               {"kind": "chunks", "limit": 12, "total": 20}]
    reason = m._problem_document_reasons(document(fields={"_truncated": markers[0], "_truncations": markers}))[0]
    ko = m._problem_document_explanation(reason)
    assert all(s in ko for s in ("40쪽", "20000자", "12개 구간"))


@pytest.fixture
def db(tmp_path, monkeypatch):
    eng = create_engine("sqlite:///" + str(tmp_path / "pdn.db"), connect_args={"check_same_thread": False})
    models.Base.metadata.create_all(eng)
    monkeypatch.setenv("GLHAC_PROBLEM_DOC_NOTIFY_DELAY_S", "0")
    monkeypatch.setattr(m, "_autoparse_inflight", set())
    monkeypatch.setattr(m, "_verify_inflight", set())
    # 이 기능 자체는 분석/검증 모델을 절대 부르지 않는다.
    monkeypatch.setattr(m.intake.ai_local, "llm_json", lambda *a, **k: pytest.fail("notification called an LLM"))
    with sessionmaker(bind=eng)() as session:
        yield session
    eng.dispose()


@pytest.fixture
def case(db):
    c = models.CaseApplication(case_id="case1", org_id="org1", company_name="PT HALAL",
                               email="company@example.test", notify_consent=True, profile_ext={})
    db.add_all([c, models.Org(org_id="org1", name="PT HALAL"),
                models.Org(org_id="staff", name="GL-HAC"),
                models.User(user_id="aud1", username="aud1", role="auditor", password_hash="unused",
                            org_id="staff", contact_email="auditor@example.test"),
                models.User(user_id="op1", username="op1", role="operator", password_hash="unused",
                            org_id="staff", contact_email="operator@example.test")])
    db.flush()
    m.sm.record_event(db, c, c.status, c.status, "ops.auditor_assigned", "operator", "op1", {"auditor_id": "aud1"})
    db.add(document(fields={"_ocr_error": "ENCRYPTED"}))
    db.commit()
    return c


def notices(db):
    return db.query(models.Notification).filter_by(event_type="problem.documents").all()


def test_company_delivery_and_auditor_inapp_in_indonesian(db, case):
    case.profile_ext = {"lang": "id"}
    db.add(document(document_id="d2", filename="scan.pdf", fields={"_ocr_error": "ERROR"}))
    db.commit()
    assert m._notify_problem_documents(db, case)["notified"]
    [n] = notices(db)
    assert (n.role, n.channels) == ("applicant", ["inapp", "email"])
    assert "evidence.pdf" in n.body and "scan.pdf" in n.body and "2026-11-01T03:04:05Z" in n.body
    assert "PDF dilindungi kata sandi" in n.body and "300 dpi" in n.body and "/ui/v4/" in n.body
    assert not re.search("[가-힣]", n.title + n.body)
    [a] = db.query(models.Notification).filter_by(event_type="problem.documents.informed").all()
    assert a.role == "auditor" and a.channels == ["inapp"] and a.payload["auditor_id"] == "aud1"
    assert "업체에 문제 서류 2건 안내함" in a.body
    # 저장된 메일 언어와 무관하게 알림 화면은 읽는 사람의 언어로 조립한다.
    assert "암호가 걸린 PDF" in m.notify_text(n, "ko")[1]
    assert "password protected" in m.notify_text(n, "en")[1]
    result = m._problem_documents_result(db, case, "id")
    assert result["count"] == 2 and result["last_notified_at"] and result["last_recipient_type"] == "applicant"
    assert result["documents"][0]["reasons"][0]["explanation"].startswith("PDF dilindungi")
    assert m._verify_wf_chain(db.query(models.WorkflowEvent).order_by(models.WorkflowEvent.created_at).all())["integrity_ok"]


@pytest.mark.parametrize("profile", [{"locale": "id"}, {"tenant": {"branding": {"locale": "id"}}}])
def test_org_language_fallback(db, case, profile):
    db.get(models.Org, case.org_id).profile_ext = profile
    db.commit()
    m._notify_problem_documents(db, case)
    assert notices(db)[0].payload["lang"] == "id"


@pytest.mark.parametrize("email,consent,assigned,role", [
    (None, True, True, "auditor"), ("   ", True, True, "auditor"),
    ("company@example.test", False, True, "auditor"),
    (None, True, False, "operator"), ("company@example.test", False, False, "operator")])
def test_staff_fallback(db, case, email, consent, assigned, role):
    case.email, case.notify_consent = email, consent
    if not assigned:
        m.sm.record_event(db, case, case.status, case.status, m.ASSIGN_RESPONSE_ACTION, "auditor", "aud1",
                          {"auditor_id": "aud1", "decision": "rejected"})
    db.commit()
    m._notify_problem_documents(db, case)
    [n] = db.query(models.Notification).all()
    assert n.role == role and n.channels == ["inapp", "email"]
    assert nt.message_text(n).splitlines()[0] == m.PROBLEM_DOC_COPY["ko"]["fallback"]


def test_no_duplicate_new_problem_sends_whole_list_and_resolved_sends_nothing(db, case):
    assert m._notify_problem_documents(db, case)["notified"]
    assert m._notify_problem_documents(db, case) == {"notified": False, "reason": "unchanged"}
    d1 = db.get(models.DocumentAsset, "d1")
    # 세부 오류 문구/날짜는 통보 지문을 바꾸지 않는다.
    d1.fields = {"_ocr_error": "ENCRYPTED", "_verify": {"verdict": "ok", "at": "later"}}
    db.commit()
    assert not m._notify_problem_documents(db, case)["notified"]
    db.add(document(document_id="d2", filename="new.pdf", fields={"_llm_error": "timeout"}))
    db.commit()
    assert m._notify_problem_documents(db, case)["notified"]
    assert len(notices(db)) == 2
    n = sorted(notices(db), key=lambda n: n.created_at)[-1]
    assert "evidence.pdf" in n.body and "new.pdf" in n.body
    for d in db.query(models.DocumentAsset):
        d.fields, d.confidence, d.text_full = {}, 0.95, "readable"
    db.commit()
    assert m._notify_problem_documents(db, case) == {"notified": False, "reason": "no_problems"}
    assert len(notices(db)) == 2


@pytest.mark.parametrize("status", sorted(m.sm.TERMINAL_STATES))
def test_terminal_never_notifies_even_manually(db, case, status):
    case.status = status
    db.commit()
    assert m._notify_problem_documents(db, case, force=True) == {"notified": False, "reason": "closed"}
    assert m.queue_problem_document_notifications(db) == 0
    assert notices(db) == []


@pytest.mark.parametrize("queue_name", ["_autoparse_inflight", "_verify_inflight"])
def test_queue_completion_and_default_five_minute_quiet_period(db, case, monkeypatch, queue_name):
    monkeypatch.delenv("GLHAC_PROBLEM_DOC_NOTIFY_DELAY_S")
    now = [datetime(2026, 11, 2)]
    monkeypatch.setattr(m, "_problem_documents_now", lambda: now[0])
    queue = getattr(m, queue_name)
    queue.add("d1")
    assert m._notify_problem_documents(db, case, force=True)["reason"] == "processing"
    now[0] += timedelta(minutes=10)
    assert m.queue_problem_document_notifications(db) == 0
    queue.remove("d1")
    assert m.queue_problem_document_notifications(db) == 0
    now[0] += timedelta(seconds=299)
    assert m.queue_problem_document_notifications(db) == 0
    # 다른 케이스 문서는 대기를 막지 않는다.
    queue.add("other-case-document")
    now[0] += timedelta(seconds=1)
    assert m.queue_problem_document_notifications(db) == 1
    assert len(notices(db)) == 1


def test_new_normal_upload_restarts_delay_and_state_survives_session(db, case, monkeypatch):
    monkeypatch.setenv("GLHAC_PROBLEM_DOC_NOTIFY_DELAY_S", "300")
    now = [datetime(2026, 11, 2)]
    monkeypatch.setattr(m, "_problem_documents_now", lambda: now[0])
    assert m.queue_problem_document_notifications(db) == 0
    now[0] += timedelta(seconds=299)
    db.add(document(document_id="d2"))
    db.commit()
    assert m.queue_problem_document_notifications(db) == 0
    now[0] += timedelta(seconds=1)
    assert m.queue_problem_document_notifications(db) == 0
    with sessionmaker(bind=db.get_bind())() as other:
        now[0] += timedelta(seconds=299)
        assert m.queue_problem_document_notifications(other) == 1
    assert len(notices(db)) == 1


def test_measurements_are_scoped_to_source_document(db, case):
    db.add(document(document_id="d2", filename="dna.pdf"))
    db.add(models.MaterialMeasurement(case_id=case.case_id, document_id="d2", param_key="pork_dna", verdict="review"))
    db.commit()
    rows = m._problem_documents_result(db, case)["documents"]
    assert [[r["code"] for r in row["reasons"]] for row in rows] == [["ENCRYPTED"], ["PORK_DNA_UNCLEAR"]]


@pytest.mark.parametrize("recipient", ["applicant", "auditor", "operator"])
def test_drain_uses_real_contact_routing_and_fake_smtp_and_mail_log(db, case, monkeypatch, recipient):
    if recipient != "applicant":
        case.notify_consent = False
    if recipient == "operator":
        db.query(models.WorkflowEvent).filter_by(action="ops.auditor_assigned").delete()
    if recipient == "applicant":
        case.profile_ext = {"lang": "id"}
    db.commit()
    sent = []

    class SMTP:
        def __init__(self, *a, **k): pass
        def starttls(self): pass
        def login(self, *a): pass
        def sendmail(self, frm, to, msg): sent.append((to, message_from_string(msg)))
        def quit(self): pass

    monkeypatch.setattr(smtplib, "SMTP", SMTP)
    monkeypatch.setenv("GLHAC_SMTP_HOST", "fake.test")
    monkeypatch.setenv("GLHAC_SMTP_FROM", "sender@example.test")
    monkeypatch.setenv("GLHAC_SMTP_SSL", "0")
    m._notify_problem_documents(db, case)
    stats = m.drain_notifications(db)
    expected = {"applicant": "company", "auditor": "auditor", "operator": "operator"}[recipient] + "@example.test"
    assert len(sent) == 1 and sent[0][0] == [expected]
    body = sent[0][1].get_payload(decode=True).decode("utf-8")
    if recipient == "applicant":
        assert "PDF dilindungi" in body
    else:
        assert body.splitlines()[0] == m.PROBLEM_DOC_COPY["ko"]["fallback"]
    [log] = db.query(models.MailLog).all()
    assert (log.recipient, log.body, log.status) == (expected, body, "sent")
    assert stats["sent"] == (2 if recipient == "applicant" else 1)


def test_package_gap_keeps_missing_items_without_unreadable_notice(db, case, monkeypatch):
    check = {"case_id": case.case_id, "complete": False, "gaps": [], "unprocessed": [],
             "unreadable": [{"document_id": "d1", "filename": "evidence.pdf"}]}
    monkeypatch.setattr(m, "_package_check", lambda *args: copy.deepcopy(check))
    m._notify_package_gaps(db, case)
    db.commit()
    assert db.query(models.Notification).count() == 0
    check["gaps"] = ["company"]
    m._notify_package_gaps(db, case)
    db.commit()
    gaps = db.query(models.Notification).all()
    assert len(gaps) == 3
    assert all("업체 정보 누락" in n.title and "판독 실패" not in n.body + n.title and "evidence.pdf" not in n.body for n in gaps)
    check["unreadable"].append({"document_id": "d2", "filename": "another.pdf"})
    m._notify_package_gaps(db, case)
    db.commit()
    assert db.query(models.Notification).count() == 3
    m._notify_problem_documents(db, case)
    assert len(notices(db)) == 1


def test_package_gap_does_not_relabel_ocr_failure_as_unprocessed(db, case):
    case.nib, case.address, case.responsible_person, case.factory_address = "1234567890123", "address", "owner", "factory"
    db.add(models.Product(case_id=case.case_id, name="Food"))
    db.add(models.Material(case_id=case.case_id, name="Salt"))
    db.get(models.DocumentAsset, "d1").confidence = None
    db.commit()
    chk = m._notify_package_gaps(db, case)
    assert not chk["gaps"] and chk["unreadable"] and chk["unprocessed"]
    assert db.query(models.Notification).count() == 0


def test_case_closed_after_queuing_does_not_send_email(db, case, monkeypatch):
    m._notify_problem_documents(db, case)
    case.status = next(iter(m.sm.TERMINAL_STATES))
    db.commit()
    monkeypatch.setitem(nt.PROVIDERS, "email", lambda *args: pytest.fail("closed case email"))
    m.drain_notifications(db)
    assert notices(db)[0].status == "skipped"
    assert db.query(models.MailLog).count() == 0


def test_changed_reason_on_same_document_resends_current_list(db, case):
    m._notify_problem_documents(db, case)
    db.get(models.DocumentAsset, "d1").fields = {"_ocr_error": "ENCRYPTED", "_llm_error": "timeout"}
    db.commit()
    assert m._notify_problem_documents(db, case)["notified"]
    assert len(notices(db)) == 2


def test_reprocess_with_same_values_restarts_quiet_period(db, case, monkeypatch):
    monkeypatch.setenv("GLHAC_PROBLEM_DOC_NOTIFY_DELAY_S", "300")
    now = [datetime(2026, 11, 2)]
    monkeypatch.setattr(m, "_problem_documents_now", lambda: now[0])
    assert m.queue_problem_document_notifications(db) == 0
    now[0] += timedelta(seconds=301)
    m.sm.record_event(db, case, case.status, case.status, "documents.reprocess", "system", None, {"document_id": "d1"})
    db.commit()
    assert m.queue_problem_document_notifications(db) == 0
    now[0] += timedelta(seconds=300)
    assert m.queue_problem_document_notifications(db) == 1


@pytest.fixture
def api(db):
    app = FastAPI()
    for route in m.app.routes:
        if getattr(route, "path", "") in ("/cases/{case_id}/problem-documents", "/cases/{case_id}/problem-documents/notify"):
            app.add_api_route(route.path, route.endpoint, methods=list(route.methods))
    actor = {"uid": "aud1", "role": "auditor", "org_id": "staff"}
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[auth.get_current_user] = lambda: actor
    with TestClient(app) as client:
        yield client, actor


@pytest.mark.parametrize("role", ["applicant", "auditor", "consultant"])
def test_other_organization_cannot_read_or_resend(api, case, role):
    client, actor = api
    actor.update(uid="stranger", role=role, org_id="other")
    for method, suffix in (("get", ""), ("post", "/notify")):
        assert getattr(client, method)(f"/cases/{case.case_id}/problem-documents{suffix}").status_code in (403, 404)


@pytest.mark.parametrize("role", ["operator", "auditor", "consultant"])
def test_authorized_manual_api_bypasses_delay_and_dedup(api, db, case, monkeypatch, role):
    client, actor = api
    actor.update(uid="aud1" if role == "auditor" else "op1", role=role,
                 org_id="org1" if role == "consultant" else "staff")
    monkeypatch.setenv("GLHAC_PROBLEM_DOC_NOTIFY_DELAY_S", "300")
    path = f"/cases/{case.case_id}/problem-documents"
    first = client.get(path + "?lang=id")
    assert first.status_code == 200 and first.json()["count"] == 1
    assert first.json()["last_notified_at"] is None
    assert db.query(models.WorkflowEvent).filter_by(action=m.PROBLEM_DOC_STATE_EVENT).count() == 0
    for _ in range(2):
        response = client.post(path + "/notify?lang=id")
        assert response.status_code == 200 and response.json()["notified"] is True
        assert response.json()["last_recipient_type"] == "applicant"
    assert len(notices(db)) == 2
    events = db.query(models.WorkflowEvent).filter_by(action=m.PROBLEM_DOC_EVENT).all()
    assert all(e.actor_type == role and e.payload["forced"] for e in events)


def test_applicant_may_read_own_case_but_cannot_resend(api, case):
    client, actor = api
    actor.update(uid="client", role="applicant", org_id=case.org_id)
    assert client.get(f"/cases/{case.case_id}/problem-documents").status_code == 200
    assert client.post(f"/cases/{case.case_id}/problem-documents/notify").status_code == 403


def test_v4_panel_escaping_language_and_duplicate_click_guard():
    root = Path(__file__).resolve().parents[1]
    script = r"""
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const c=vm.createContext({console,Set,encodeURIComponent,AUTH:{role:'auditor'},RS:{},S:{lang:'ko'},App:{},I18N:{ko:{},en:{},id:{}},
  render(){},toast(s){c.message=s},esc:s=>String(s??'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'),jsq:s=>s,
  t:k=>c.I18N[c.S.lang][k],apiFetch:async()=>({ok:true,json:async()=>({})})});
vm.runInContext(fs.readFileSync('app/static/v4/js/problem-documents.js','utf8'),c);
const data={count:1,documents:[{filename:'<img src=x>',uploaded_at:'2026-11-01',reasons:[{explanation:'<script>unsafe</script>'}]}]};
for(const lang of ['ko','en','id']){
  c.S.lang=lang;c.RS.problemDocs['c1:'+lang]=data;
  const html=vm.runInContext("PDN_panel('c1')",c);
  assert.ok(!html.includes('<img'));assert.ok(!html.includes('<script>'));assert.ok(html.includes('&lt;script&gt;'));
  assert.ok(html.includes(c.I18N[lang]['pdn.resend']));
  assert.ok(html.includes(c.I18N[lang]['pdn.count'].replace('{count}',1)));
}
(async()=>{
  let finish,calls=0;c.apiFetch=(url,opts)=>{calls++;assert.equal(opts.method,'POST');assert.ok(url.includes('lang=id'));return new Promise(r=>finish=r)};
  const first=c.App.problemDocsNotify('c1');await c.App.problemDocsNotify('c1');assert.equal(calls,1);
  assert.match(vm.runInContext("PDN_panel('c1')",c),/disabled/);
  finish({ok:true,json:async()=>({...data,notified:true})});await first;
  assert.equal(c.message,c.I18N.id['pdn.sent']);assert.equal(c.RS.problemDocs['c1:ko'],undefined);
  c.RS.problemDocs['c1:id']={...data,closed:true};assert.match(vm.runInContext("PDN_panel('c1')",c),/disabled/);
  c.AUTH.role='applicant';assert.equal(vm.runInContext("PDN_panel('c1')",c),'');
})().catch(e=>{console.error(e);process.exitCode=1});
"""
    result = subprocess.run(["node", "-e", script], cwd=root, text=True, capture_output=True)
    assert result.returncode == 0, result.stdout + result.stderr
