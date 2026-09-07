"""v3 변경 검증 — operator 역할 · mockAudit RBAC · 권한 이관(cert_issue).
TestClient 기반(서버 불필요). 실행: <venv>/bin/python tests/test_v3_smoke.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("GLHAC_DB_URL", "sqlite:///./glhac_v3_test.db")
os.environ.setdefault("GLHAC_DEV", "1")   # 데모 계정 시드(테스트 전용)

from fastapi.testclient import TestClient  # noqa: E402
from conftest import app_db_file  # noqa: E402
from app.main import app  # noqa: E402


def _tok(c, u, p):
    r = c.post("/auth/login", json={"username": u, "password": p})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def _h(tok):
    return {"Authorization": "Bearer " + tok}


def test_password_pbkdf2_and_legacy_upgrade():
    """비밀번호가 pbkdf2로 저장·검증되고, 레거시 sha256 해시는 로그인 시 자동 승격."""
    from app import auth, models
    from app.db import SessionLocal
    h = auth.hash_pw("secret1")
    assert h.startswith("pbkdf2_sha256$") and auth.verify_pw("secret1", h) and not auth.verify_pw("x", h)
    assert auth.needs_rehash(auth._hash_legacy("secret1")) and not auth.needs_rehash(h)
    # 레거시 해시 사용자 심어 로그인 → pbkdf2로 재해시되는지 (TestClient 컨텍스트가 테이블 생성)
    with TestClient(app) as c:
        db = SessionLocal()
        try:
            db.add(models.User(username="legacyuser", password_hash=auth._hash_legacy("pw"),
                               role="applicant", org_id="org_demo"))
            db.commit()
        finally:
            db.close()
        assert c.post("/auth/login", json={"username": "legacyuser", "password": "pw"}).status_code == 200
        db = SessionLocal()
        try:
            u = db.query(models.User).filter_by(username="legacyuser").first()
            assert u.password_hash.startswith("pbkdf2_sha256$"), "레거시 해시가 승격되지 않음"
        finally:
            db.close()


def test_login_rate_limited():
    """동일 계정 실패 10회 초과 시 429."""
    from app import auth
    auth.clear_attempts("rl_probe")
    with TestClient(app) as c:
        codes = [c.post("/auth/login", json={"username": "rl_probe", "password": "bad"}).status_code
                 for _ in range(12)]
    assert 429 in codes and codes[:10] == [401] * 10, codes
    auth.clear_attempts("rl_probe")


def test_label_judgment_path_traversal_blocked():
    """/ai/label-judgment 임의파일 읽기 차단(P0)."""
    with TestClient(app) as c:
        tok = _tok(c, "consultant1", "pw")
        r = c.post("/ai/label-judgment", json={"image_path": "/etc/passwd"}, headers=_h(tok))
        assert r.status_code == 400 and r.json()["detail"]["code"] == "PATH_NOT_ALLOWED", r.text


def test_public_config_reports_dev():
    with TestClient(app) as c:
        assert c.get("/public-config").json()["dev_mode"] is True


def test_invoice_negative_rejected():
    """음수 청구액은 422(Phase B 검증)."""
    with TestClient(app) as c:
        tok = _tok(c, "consultant1", "pw")
        cid = c.post("/cases", json={"org_id": "org_demo", "company_name": "InvTest"},
                     headers=_h(tok)).json()["case_id"]
        r = c.post(f"/cases/{cid}/invoices", json={"service_type": "onsite", "amount": -5},
                   headers=_h(tok))
        assert r.status_code == 422, r.text


def test_dashboard_events_org_isolation():
    """타 조직 워크플로 이벤트가 /dashboard/summary 로 누수되지 않아야(Phase A)."""
    with TestClient(app) as c:
        atok = _tok(c, "admin", "admin")
        cid = c.post("/cases", json={"org_id": "org_demo", "company_name": "EvtCo"},
                     headers=_h(atok)).json()["case_id"]
        c.post(f"/cases/{cid}/transition", json={"to_state": "application_draft"}, headers=_h(atok))
        c.post("/auth/register", json={"username": "isouser2", "password": "halal-test-1"})
        utok = _tok(c, "isouser2", "halal-test-1")
        ev = c.get("/dashboard/summary", headers=_h(utok)).json()["events"]
        assert all(e["case_id"] != cid for e in ev), ("타조직 이벤트 누수", ev)


def test_notify_consent_gate():
    """수신동의 없으면 외부채널(sms) 미발송·inapp만 발송(Phase C 컴플라이언스)."""
    from app import models
    from app.db import SessionLocal
    from app.main import drain_notifications
    with TestClient(app):
        db = SessionLocal()
        try:
            c = models.CaseApplication(company_name="NoConsent Co", org_id="org_demo",
                                       phone="0812", notify_consent=False, status="draft")
            db.add(c); db.flush()
            n = models.Notification(org_id="org_demo", case_id=c.case_id,
                                    channels=["inapp", "sms"], title="t", status="unsent")
            db.add(n); db.commit(); nid = n.notification_id
            drain_notifications(db)
            db.expire_all()
            row = db.get(models.Notification, nid)
            assert row.status == "sent", row.status
            assert "consent_skipped" in (row.last_error or ""), row.last_error
        finally:
            db.close()


def test_phase_b_indexes_created():
    """핫 인덱스 + 경합 방지 유니크 인덱스가 생성됐는지(Phase B)."""
    from sqlalchemy import inspect as _inspect
    from app.db import engine
    with TestClient(app):   # startup에서 _ensure_indexes 실행
        insp = _inspect(engine)
        names = set()
        for t in insp.get_table_names():
            names |= {ix["name"] for ix in insp.get_indexes(t)}
    for uq in ["uq_product_material", "uq_onsite_item", "uq_hpas_element",
               "uq_gendoc_version", "uq_cert_active"]:
        assert uq in names, f"유니크 인덱스 누락: {uq}"
    for ix in ["ix_material_case_id", "ix_document_asset_case_id", "ix_workflow_event_case_id"]:
        assert ix in names, f"핫 인덱스 누락: {ix}"


def test_operator_role_seeded():
    """v3 신규 역할 operator 시드·로그인."""
    with TestClient(app) as c:
        r = c.post("/auth/login", json={"username": "operator1", "password": "pw"})
        assert r.status_code == 200, r.text
        assert r.json()["role"] == "operator", r.json()


def test_mockaudit_rbac():
    """모의심사 큐: operator·auditor 허용 / applicant 차단(클라이언트 미노출)."""
    with TestClient(app) as c:
        op = _tok(c, "operator1", "pw")
        ap = _tok(c, "applicant1", "pw")
        aud = _tok(c, "auditor1", "pw")
        assert c.get("/mock-audit/queue", headers=_h(op)).status_code == 200
        assert c.get("/mock-audit/queue", headers=_h(aud)).status_code == 200
        assert c.get("/mock-audit/queue", headers=_h(ap)).status_code == 403


def test_permission_transfer_cert_issue():
    """인증서 발급 = 최고운영자(최종 결제자) 전용: consultant·fatwa 차단(403) / operator 통과(≠403)."""
    with TestClient(app) as c:
        cons = _tok(c, "consultant1", "pw")
        op = _tok(c, "operator1", "pw")
        fat = _tok(c, "fatwa1", "pw")
        # consultant·fatwa_liaison(샤리아)는 발급 불가 — 발급은 최고운영자만
        assert c.post("/cases/none/certificate/issue", headers=_h(cons)).status_code == 403
        assert c.post("/cases/none/certificate/issue", headers=_h(fat)).status_code == 403
        # operator는 역할 통과(케이스 없어 403이 아닌 404 등)
        assert c.post("/cases/none/certificate/issue", headers=_h(op)).status_code != 403


def test_transition_bypass_blocked():
    """P0-1: applicant가 /transition으로 승인·발급 상태 직접 진입 차단(403)."""
    import sqlite3
    dbfile = app_db_file()   # 환경변수가 아니라 앱 엔진이 단일 출처(conftest 주석 참조)
    with TestClient(app) as c:
        adm = _tok(c, "admin", "admin")
        ap = _tok(c, "applicant1", "pw")
        cid = c.post("/cases", json={"company_name": "Bypass", "org_id": "org_demo"},
                     headers=_h(adm)).json()["case_id"]
        conn = sqlite3.connect(dbfile)
        conn.execute("UPDATE case_application SET status='fatwa_review' WHERE case_id=?", (cid,))
        conn.commit()
        conn.close()
        # PROTECTED_STATES — raw 전이 금지
        assert c.post(f"/cases/{cid}/transition", json={"to_state": "fatwa_approved"},
                      headers=_h(ap)).status_code == 403
        assert c.post(f"/cases/{cid}/transition", json={"to_state": "certificate_issued"},
                      headers=_h(ap)).status_code == 403
        # fatwa_status는 여전히 승인 아님
        assert c.get(f"/cases/{cid}", headers=_h(adm)).json()["fatwa_status"] != "approved"


def test_operator_provisionable():
    """P0-4: admin이 operator 역할 사용자를 생성할 수 있어야 함."""
    with TestClient(app) as c:
        adm = _tok(c, "admin", "admin")
        r = c.post("/admin/users", json={"username": "op_new", "password": "halal-test-1", "role": "operator"},
                   headers=_h(adm))
        assert r.status_code == 200, r.text


def test_sod_and_severity_enum():
    """P1: 가승인 SoD(operator 차단·샤리아 허용) + finding severity enum 검증."""
    with TestClient(app) as c:
        adm = _tok(c, "admin", "admin")
        fat = _tok(c, "fatwa1", "pw")
        op = _tok(c, "operator1", "pw")
        aud = _tok(c, "auditor1", "pw")
        cid = c.post("/cases", json={"company_name": "SoD", "org_id": "org_demo"},
                     headers=_h(adm)).json()["case_id"]
        # SoD: operator는 가승인(patch_fatwa) 불가, 샤리아만
        assert c.patch(f"/cases/{cid}/fatwa", json={"decision": "approved"},
                       headers=_h(op)).status_code == 403
        assert c.patch(f"/cases/{cid}/fatwa", json={"decision": "approved"},
                       headers=_h(fat)).status_code == 200
        # severity enum: 'Major'(오타) 거부, 'major' 허용
        assert c.post(f"/cases/{cid}/findings", json={"finding": "x", "severity": "Major"},
                      headers=_h(aud)).status_code == 422
        assert c.post(f"/cases/{cid}/findings", json={"finding": "x", "severity": "major"},
                      headers=_h(aud)).status_code == 200


def test_two_stage_fatwa_approval():
    """2단계 승인: 샤리아 가승인(provisional) → 최고운영자 최종승인(approved)."""
    with TestClient(app) as c:
        adm = _tok(c, "admin", "admin")
        fat = _tok(c, "fatwa1", "pw")
        op = _tok(c, "operator1", "pw")
        cid = c.post("/cases", json={"company_name": "2Stage", "org_id": "org_demo"},
                     headers=_h(adm)).json()["case_id"]
        # 샤리아 가승인 → fatwa_status=provisional (최종 아님)
        assert c.patch(f"/cases/{cid}/fatwa", json={"decision": "approved"},
                       headers=_h(fat)).status_code == 200
        assert c.get(f"/cases/{cid}", headers=_h(adm)).json()["fatwa_status"] == "provisional"
        # 샤리아는 최종승인 불가(403)
        assert c.post(f"/cases/{cid}/fatwa/final-approve", headers=_h(fat)).status_code == 403
        # 최고운영자 최종승인 → approved + scope 동결
        assert c.post(f"/cases/{cid}/fatwa/final-approve", headers=_h(op)).status_code == 200
        d = c.get(f"/cases/{cid}", headers=_h(adm)).json()
        assert d["fatwa_status"] == "approved" and d["scope_frozen"] is True


def test_mockaudit_decision_validation():
    """모의심사 결정: 잘못된 결과/사유 누락 검증(케이스 없으면 404이지만 권한은 통과)."""
    with TestClient(app) as c:
        op = _tok(c, "operator1", "pw")
        # 권한은 통과해야 하므로 403이 아니어야 함
        r = c.post("/cases/none/mock-audit/decision", json={"result": "pass"}, headers=_h(op))
        assert r.status_code != 403, r.text


def test_mockaudit_decision_recorded_and_retrieved():
    """판정(pass/reject) → 이력 조회 루프. 거부 사유 누락은 422."""
    with TestClient(app) as c:
        adm = _tok(c, "admin", "admin")
        cid = c.post("/cases", json={"company_name": "MockAudit Co", "org_id": "org_demo"},
                     headers=_h(adm)).json()["case_id"]
        # 거부인데 사유 없음 → 422
        assert c.post(f"/cases/{cid}/mock-audit/decision", json={"result": "reject"},
                      headers=_h(adm)).status_code == 422
        # 통과 판정 기록
        assert c.post(f"/cases/{cid}/mock-audit/decision", json={"result": "pass"},
                      headers=_h(adm)).json()["result"] == "pass"
        # 거부 판정(사유 포함) 기록
        c.post(f"/cases/{cid}/mock-audit/decision",
               json={"result": "reject", "reason": "증빙 부족"}, headers=_h(adm))
        # 이력 조회 — 2건, 최신이 reject
        hist = c.get(f"/cases/{cid}/mock-audit", headers=_h(adm)).json()
        results = [d["result"] for d in hist["decisions"]]
        assert "pass" in results and "reject" in results, results
        assert any(d.get("reason") == "증빙 부족" for d in hist["decisions"]), hist


def test_mockaudit_reject_transitions_to_corrective():
    """② 상태전이: onsite_audit_in_progress에서 reject → corrective_action_required 전이."""
    import os
    import sqlite3
    dbfile = app_db_file()   # 환경변수가 아니라 앱 엔진이 단일 출처(conftest 주석 참조)
    with TestClient(app) as c:
        adm = _tok(c, "admin", "admin")
        cid = c.post("/cases", json={"company_name": "MT", "org_id": "org_demo"},
                     headers=_h(adm)).json()["case_id"]
        conn = sqlite3.connect(dbfile)
        conn.execute("UPDATE case_application SET status=? WHERE case_id=?",
                     ("onsite_audit_in_progress", cid))
        conn.commit()
        conn.close()
        r = c.post(f"/cases/{cid}/mock-audit/decision",
                   json={"result": "reject", "reason": "gap"}, headers=_h(adm)).json()
        assert r.get("transitioned_to") == "corrective_action_required", r
        assert c.get(f"/cases/{cid}", headers=_h(adm)).json()["status"] == "corrective_action_required"


def test_mockaudit_no_transition_on_non_mock_state():
    """비-모의심사 단계(onboarding)에서는 판정해도 전이 없음(기록만)."""
    with TestClient(app) as c:
        adm = _tok(c, "admin", "admin")
        cid = c.post("/cases", json={"company_name": "NM", "org_id": "org_demo"},
                     headers=_h(adm)).json()["case_id"]
        r = c.post(f"/cases/{cid}/mock-audit/decision", json={"result": "pass"},
                   headers=_h(adm)).json()
        assert r.get("transitioned_to") is None, r


def test_worklist_queues_rbac():
    """작업 큐 RBAC: audit=오디터·operator / fatwa=샤리아·operator, applicant 차단."""
    with TestClient(app) as c:
        aud = _tok(c, "auditor1", "pw")
        fat = _tok(c, "fatwa1", "pw")
        op = _tok(c, "operator1", "pw")
        ap = _tok(c, "applicant1", "pw")
        assert c.get("/audit/queue", headers=_h(aud)).status_code == 200
        assert c.get("/audit/queue", headers=_h(op)).status_code == 200
        assert c.get("/audit/queue", headers=_h(ap)).status_code == 403
        assert c.get("/fatwa/queue", headers=_h(fat)).status_code == 200
        assert c.get("/fatwa/queue", headers=_h(op)).status_code == 200
        assert c.get("/fatwa/queue", headers=_h(ap)).status_code == 403


def test_ask_injects_domain_ontology():
    """도메인 시스템(온톨로지)에서 질문 관련 할랄 근거를 직접 회수해 주입(학습 불필요)."""
    with TestClient(app) as c:
        adm = _tok(c, "admin", "admin")
        cid = c.post("/cases", json={"company_name": "Ask Co", "org_id": "org_demo"},
                     headers=_h(adm)).json()["case_id"]
        r = c.post(f"/cases/{cid}/ask", json={"question": "gelatin 원료 써도 되나요?"},
                   headers=_h(adm))
        assert r.status_code == 200, r.text
        body = r.json()
        assert "domain_sources" in body and "long_term_sources" in body
        names = [str(s.get("name", "")).lower() for s in body["domain_sources"]]
        assert any("gelatin" in n for n in names), body["domain_sources"]


def test_context_health_endpoint():
    """CHU-1 장기기억 연동 상태 — CHU-1 미가동 시에도 200 + ok 키(폴백)."""
    with TestClient(app) as c:
        r = c.get("/ai/context/health")
        assert r.status_code == 200
        assert "ok" in r.json(), r.json()


def test_search_context_fallback_returns_list():
    """CHU-1 검색 어댑터 — 미가동/장애 시 예외 없이 [] 리스트 폴백."""
    from app import ai_local
    res = ai_local.search_context("할랄 인증 절차", top_k=2)
    assert isinstance(res, list)


def test_mock_evidence_documents_tagged_and_listed():
    """모의감사 클라이언트 뷰 백엔드: doc_type=mock_evidence_* 태깅 저장 + 목록에
    created_at 노출(섹션별 업로드 일시) + 영상(mp4) 확장자 업로드 허용."""
    import base64
    png = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAA"
           "C0lEQVR4nGNgYGAAAAAEAAH2FzhVAAAAAElFTkSuQmCC")   # 1x1 PNG
    mp4 = base64.b64encode(b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom").decode()
    with TestClient(app) as c:
        adm = _tok(c, "admin", "admin")
        cid = c.post("/cases", json={"company_name": "Mock EV Co", "org_id": "org_demo"},
                     headers=_h(adm)).json()["case_id"]
        r1 = c.post(f"/cases/{cid}/documents",
                    json={"filename": "storage.png", "file_b64": png,
                          "doc_type": "mock_evidence_material_storage"}, headers=_h(adm))
        assert r1.status_code == 200, r1.text
        assert r1.json()["doc_type"] == "mock_evidence_material_storage"
        r2 = c.post(f"/cases/{cid}/documents",
                    json={"filename": "line.mp4", "file_b64": mp4,
                          "doc_type": "mock_evidence_production_video"}, headers=_h(adm))
        assert r2.status_code == 200, r2.text   # 영상 확장자 허용 확인
        docs = c.get(f"/cases/{cid}/documents", headers=_h(adm)).json()
        by = {d["doc_type"]: d for d in docs}
        assert "mock_evidence_material_storage" in by and "mock_evidence_production_video" in by
        assert by["mock_evidence_material_storage"]["created_at"], "created_at 미노출"


def test_onsite_sign_stored_and_retrieved():
    """현장감사 전자서명 신규 라우트: POST /cases/{id}/onsite/sign 저장(party별 latest-wins),
    GET 조회 반환. 잘못된 party·비-dataURL 이미지 거부. Phase 4 audit_report.sign과 별개."""
    sig = ("data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJ"
           "AAAAC0lEQVR4nGNgYGAAAAAEAAH2FzhVAAAAAElFTkSuQmCC")
    with TestClient(app) as c:
        adm = _tok(c, "admin", "admin")
        cid = c.post("/cases", json={"company_name": "Onsite Sign Co", "org_id": "org_demo"},
                     headers=_h(adm)).json()["case_id"]
        # 초기: 둘 다 None
        assert c.get(f"/cases/{cid}/onsite/sign", headers=_h(adm)).json() == {"auditor": None, "supervisor": None}
        # 감사자 서명 저장
        r = c.post(f"/cases/{cid}/onsite/sign", json={"party": "auditor", "image": sig, "name": "홍길동"}, headers=_h(adm))
        assert r.status_code == 200 and r.json()["party"] == "auditor", r.text
        # 잘못된 party 거부
        assert c.post(f"/cases/{cid}/onsite/sign", json={"party": "x", "image": sig}, headers=_h(adm)).status_code == 422
        # 비-dataURL 이미지 거부
        assert c.post(f"/cases/{cid}/onsite/sign", json={"party": "supervisor", "image": "nope"}, headers=_h(adm)).status_code == 422
        # 감독관 서명 저장 + 감사자 갱신(latest-wins)
        c.post(f"/cases/{cid}/onsite/sign", json={"party": "supervisor", "image": sig, "name": "김할랄"}, headers=_h(adm))
        c.post(f"/cases/{cid}/onsite/sign", json={"party": "auditor", "image": sig, "name": "이감사"}, headers=_h(adm))
        got = c.get(f"/cases/{cid}/onsite/sign", headers=_h(adm)).json()
        assert got["auditor"]["name"] == "이감사", got            # 최신 우선
        assert got["supervisor"]["name"] == "김할랄", got
        assert got["auditor"]["image"].startswith("data:image/"), got


if __name__ == "__main__":
    tests = [test_password_pbkdf2_and_legacy_upgrade,
             test_login_rate_limited,
             test_label_judgment_path_traversal_blocked,
             test_public_config_reports_dev,
             test_invoice_negative_rejected,
             test_dashboard_events_org_isolation,
             test_notify_consent_gate,
             test_phase_b_indexes_created,
             test_operator_role_seeded, test_mockaudit_rbac,
             test_permission_transfer_cert_issue, test_transition_bypass_blocked,
             test_operator_provisionable, test_sod_and_severity_enum,
             test_two_stage_fatwa_approval,
             test_mockaudit_decision_validation,
             test_mockaudit_decision_recorded_and_retrieved,
             test_mockaudit_reject_transitions_to_corrective,
             test_mockaudit_no_transition_on_non_mock_state,
             test_worklist_queues_rbac,
             test_ask_injects_domain_ontology,
             test_context_health_endpoint, test_search_context_fallback_returns_list,
             test_mock_evidence_documents_tagged_and_listed,
             test_onsite_sign_stored_and_retrieved]
    ok = 0
    for fn in tests:
        try:
            fn(); ok += 1; print("[PASS]", fn.__name__)
        except AssertionError as e:
            print("[FAIL]", fn.__name__, "—", e)
        except Exception as e:  # noqa
            import traceback; traceback.print_exc()
            print("[ERROR]", fn.__name__, "—", type(e).__name__, e)
    print(f"\n{ok}/{len(tests)} passed")
    sys.exit(0 if ok == len(tests) else 1)
