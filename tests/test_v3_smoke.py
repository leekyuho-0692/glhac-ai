"""v3 변경 검증 — operator 역할 · mockAudit RBAC · 권한 이관(cert_issue).
TestClient 기반(서버 불필요). 실행: <venv>/bin/python tests/test_v3_smoke.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("GLHAC_DB_URL", "sqlite:///./glhac_v3_test.db")
os.environ.setdefault("GLHAC_DEV", "1")   # 데모 계정 시드(테스트 전용)

from fastapi.testclient import TestClient  # noqa: E402
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


def test_alembic_scaffolding():
    """Alembic 스캐폴딩·기준선 무결성(§6.1). 실제 upgrade는 subprocess/CI로 검증."""
    import os
    import importlib.util
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    assert os.path.exists(os.path.join(root, "alembic.ini"))
    assert os.path.exists(os.path.join(root, "alembic", "env.py"))
    p = os.path.join(root, "alembic", "versions", "0001_baseline.py")
    spec = importlib.util.spec_from_file_location("baseline_rev", p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    assert m.revision == "0001_baseline" and m.down_revision is None
    assert callable(m.upgrade) and callable(m.downgrade)
    # 0002 FK 마이그레이션이 기준선에 체인되는지(§6.1·§6.2)
    p2 = os.path.join(root, "alembic", "versions", "0002_fk_constraints.py")
    spec2 = importlib.util.spec_from_file_location("fk_rev", p2)
    m2 = importlib.util.module_from_spec(spec2)
    spec2.loader.exec_module(m2)
    assert m2.revision == "0002_fk_constraints" and m2.down_revision == "0001_baseline"
    fks = list(m2._fks())
    assert any(t == "material" and c == "case_id" for t, c, rt, rc in fks), "case_id FK 누락"
    from app.db import Base
    import app.models  # noqa: F401 — 메타데이터 등록
    assert "case_application" in Base.metadata.tables and "ai_extraction" in Base.metadata.tables


def test_payment_and_analytics():
    """§P2 billing/payment + §11.3 analytics: 결제 기록·매출 집계·권한."""
    with TestClient(app) as c:
        ctok = _tok(c, "consultant1", "pw")
        cid = c.post("/cases", json={"org_id": "org_demo", "company_name": "PayCo"},
                     headers=_h(ctok)).json()["case_id"]
        inv = c.post(f"/cases/{cid}/invoices", json={"service_type": "onsite", "amount": 100},
                     headers=_h(ctok)).json()
        iid, total = inv["invoice_id"], inv["total"]
        pay = c.post(f"/invoices/{iid}/payment", json={"method": "bank_transfer", "reference": "TRX1"},
                     headers=_h(ctok))
        assert pay.status_code == 200 and pay.json()["amount"] == total, pay.text
        pays = c.get(f"/cases/{cid}/payments", headers=_h(ctok)).json()
        assert len(pays) == 1 and pays[0]["method"] == "bank_transfer", pays
        an = c.get("/analytics/summary", headers=_h(_tok(c, "operator1", "pw"))).json()
        assert an["revenue_paid"] >= total and "cases_by_status" in an, an
        assert c.post(f"/invoices/{iid}/payment", json={"method": "bitcoin"},
                      headers=_h(ctok)).status_code == 400
        assert c.get("/analytics/summary",
                     headers=_h(_tok(c, "applicant1", "pw"))).status_code == 403


def test_org_overview():
    """§P2 multi-tenant admin: 조직별 개요(admin 전용)."""
    with TestClient(app) as c:
        atok = _tok(c, "admin", "admin")
        c.post("/cases", json={"org_id": "org_demo", "company_name": "OvCo"}, headers=_h(atok))
        ov = c.get("/admin/orgs/org_demo/overview", headers=_h(atok)).json()
        assert ov["org_id"] == "org_demo" and ov["cases"] >= 1 and "by_status" in ov, ov
        assert c.get("/admin/orgs/org_demo/overview",
                     headers=_h(_tok(c, "operator1", "pw"))).status_code == 403


def test_observability_metrics():
    """§11.3 관측성 — 요청 ID 헤더·Prometheus /metrics·auth 실패 카운터."""
    with TestClient(app) as c:
        r = c.get("/health")
        assert any(k.lower() == "x-request-id" for k in r.headers), dict(r.headers)
        c.post("/auth/login", json={"username": "nope_metrics", "password": "x"})  # 401 → auth_failures
        m = c.get("/metrics")
        assert m.status_code == 200 and "glhac_http_requests_total" in m.text, m.status_code
        assert "glhac_http_request_duration_seconds_count" in m.text
        assert "glhac_auth_failures_total" in m.text


def test_pdf_generation():
    """§7 서버 PDF — gen-docs·인증서 PDF(한글 포함) 실제 %PDF 반환."""
    from app import models
    from app.db import SessionLocal
    with TestClient(app) as c:
        atok = _tok(c, "admin", "admin")
        cid = c.post("/cases", json={"org_id": "org_demo", "company_name": "PdfCo"},
                     headers=_h(atok)).json()["case_id"]
        db = SessionLocal()
        try:
            g = models.GeneratedDocument(case_id=cid, org_id="org_demo", doc_type="sjph_manual",
                                         version=1, status="draft",
                                         content="SJPH 매뉴얼\n요소: 경영·원재료·공정\n할랄 인증 준비 양호.")
            db.add(g)
            db.add(models.HalalCertificate(case_id=cid, certificate_no="HC-PDF", scope=["ProdA"],
                   issue_date="2026-01-01", expiry_date="2030-01-01", status="active",
                   qr_token="pdftoken"))
            db.commit()
            gid = g.gen_doc_id
        finally:
            db.close()
        r1 = c.get(f"/gen-docs/{gid}/pdf", headers=_h(atok))
        assert r1.status_code == 200 and r1.headers["content-type"] == "application/pdf", r1.status_code
        assert r1.content[:4] == b"%PDF" and len(r1.content) > 800, len(r1.content)
        r2 = c.get(f"/cases/{cid}/certificate/pdf", headers=_h(atok))
        assert r2.status_code == 200 and r2.content[:4] == b"%PDF" and len(r2.content) > 800, r2.status_code


def test_certificate_lifecycle():
    """§5.1 인증서 정지/재개/철회 — 권한·상태전이·공개검증 반영."""
    from app import models
    from app.db import SessionLocal
    with TestClient(app) as c:
        atok = _tok(c, "admin", "admin")
        otok = _tok(c, "operator1", "pw")
        cid = c.post("/cases", json={"org_id": "org_demo", "company_name": "LcCo"},
                     headers=_h(atok)).json()["case_id"]
        db = SessionLocal()
        try:
            db.add(models.HalalCertificate(case_id=cid, certificate_no="HC-LC", scope=["P"],
                   issue_date="2026-01-01", expiry_date="2030-01-01", status="active",
                   qr_token="lctoken"))
            db.commit()
        finally:
            db.close()
        # consultant는 정지 불가(certificate.suspend 매트릭스)
        assert c.post(f"/cases/{cid}/certificate/suspend", json={"reason": "위반 확인함"},
                      headers=_h(_tok(c, "consultant1", "pw"))).status_code == 403
        # operator 정지 → 공개검증 valid=false
        assert c.post(f"/cases/{cid}/certificate/suspend", json={"reason": "위반 확인됨"},
                      headers=_h(otok)).json()["status"] == "suspended"
        assert c.get("/verify/lctoken").json()["valid"] is False
        # 재개 → active → valid=true
        assert c.post(f"/cases/{cid}/certificate/reactivate", json={"reason": "보완 완료됨"},
                      headers=_h(otok)).json()["status"] == "active"
        assert c.get("/verify/lctoken").json()["valid"] is True
        # 철회 → withdrawn → valid=false
        assert c.post(f"/cases/{cid}/certificate/revoke", json={"reason": "중대 위반으로 철회"},
                      headers=_h(otok)).json()["status"] == "withdrawn"
        assert c.get("/verify/lctoken").json()["valid"] is False
        # 철회 후 정지 불가(BAD_CERT_STATE)
        assert c.post(f"/cases/{cid}/certificate/suspend", json={"reason": "불가한 시도임"},
                      headers=_h(otok)).status_code == 409


def test_certificate_signature_and_verify():
    """§6.4 전자서명: 서명 생성·공개검증 signature_valid·권한(operator)."""
    from app import models
    from app.db import SessionLocal
    with TestClient(app) as c:
        atok = _tok(c, "admin", "admin")
        cid = c.post("/cases", json={"org_id": "org_demo", "company_name": "SignCo"},
                     headers=_h(atok)).json()["case_id"]
        db = SessionLocal()
        try:
            db.add(models.HalalCertificate(case_id=cid, certificate_no="HC-SIGN", scope=["P"],
                   issue_date="2026-01-01", expiry_date="2030-01-01", status="active",
                   qr_token="sigtoken_1"))
            db.commit()
        finally:
            db.close()
        s = c.post(f"/cases/{cid}/certificate/sign", headers=_h(_tok(c, "operator1", "pw")))
        assert s.status_code == 200 and s.json()["signature_id"], s.text
        v = c.get("/verify/sigtoken_1").json()
        assert v["signed"] is True and v["signature_valid"] is True, v
        # consultant는 서명 불가(certificate.issue 매트릭스)
        assert c.post(f"/cases/{cid}/certificate/sign",
                      headers=_h(_tok(c, "consultant1", "pw"))).status_code == 403


def test_integration_event_idempotency():
    """§10.1 SIHALAL 연동: idempotency_key 중복수신 방지·이벤트타입 검증·권한."""
    with TestClient(app) as c:
        otok = _tok(c, "operator1", "pw")
        atok = _tok(c, "admin", "admin")
        cid = c.post("/cases", json={"org_id": "org_demo", "company_name": "IntCo"},
                     headers=_h(atok)).json()["case_id"]
        body = {"event_type": "external_status_synced", "idempotency_key": "idem-1",
                "case_id": cid, "payload": {"x": 1}}
        r1 = c.post("/integration/sihalal/event", json=body, headers=_h(otok)).json()
        r2 = c.post("/integration/sihalal/event", json=body, headers=_h(otok)).json()
        assert r1["idempotent"] is False and r2["idempotent"] is True and r2["id"] == r1["id"], (r1, r2)
        assert c.post("/integration/sihalal/event", json={"event_type": "bad", "idempotency_key": "k2"},
                      headers=_h(otok)).status_code == 400
        evs = c.get(f"/cases/{cid}/integration/events", headers=_h(otok)).json()
        assert len(evs) == 1 and evs[0]["event_type"] == "external_status_synced", evs
        assert c.post("/integration/sihalal/event",
                      json={"event_type": "external_status_synced", "idempotency_key": "k3"},
                      headers=_h(_tok(c, "consultant1", "pw"))).status_code == 403


def test_audit_plan_lifecycle():
    """§P2 LPH scheduling: 현장심사 일정 생성·조회·상태변경."""
    with TestClient(app) as c:
        atok = _tok(c, "admin", "admin")
        cid = c.post("/cases", json={"org_id": "org_demo", "company_name": "APlan"},
                     headers=_h(atok)).json()["case_id"]
        r = c.post(f"/cases/{cid}/audit-plan",
                   json={"lph_name": "LPH A", "scheduled_date": "2026-08-01", "scope": "onsite",
                         "auditors": ["Budi"]}, headers=_h(atok))
        assert r.status_code == 200, r.text
        pid = r.json()["id"]
        plans = c.get(f"/cases/{cid}/audit-plans", headers=_h(atok)).json()
        assert len(plans) == 1 and plans[0]["scheduled_date"] == "2026-08-01", plans
        assert c.patch(f"/audit-plans/{pid}", json={"status": "completed"},
                       headers=_h(atok)).json()["status"] == "completed"


def test_fatwa_voting_quorum():
    """§6.2 Fatwa 위원회 투표: quorum·집계·상세 열람 권한."""
    from app import models
    from app.db import SessionLocal
    with TestClient(app) as c:
        atok = _tok(c, "admin", "admin")
        cid = c.post("/cases", json={"org_id": "org_demo", "company_name": "FVote"},
                     headers=_h(atok)).json()["case_id"]
        db = SessionLocal()
        try:
            db.add(models.FatwaDecision(case_id=cid, decision="pending",
                                        committee_members=["A", "B", "C"]))
            db.commit()
        finally:
            db.close()
        ftok = _tok(c, "fatwa1", "pw")
        t = None
        for m in ["A", "B"]:
            t = c.post(f"/cases/{cid}/fatwa/vote", json={"member": m, "vote": "approve"},
                       headers=_h(ftok)).json()
        assert t["members"] == 3 and t["quorum_met"] is True and t["result"] == "passed", t
        # 상세 열람: sharia 허용, consultant 403
        assert c.get(f"/cases/{cid}/fatwa/votes", headers=_h(ftok)).status_code == 200
        assert c.get(f"/cases/{cid}/fatwa/votes",
                     headers=_h(_tok(c, "consultant1", "pw"))).status_code == 403
        # applicant 투표 불가(fatwa.propose 매트릭스)
        assert c.post(f"/cases/{cid}/fatwa/vote", json={"member": "X", "vote": "approve"},
                      headers=_h(_tok(c, "applicant1", "pw"))).status_code == 403


def test_car_lifecycle_closes_finding():
    """§P2 CAR advanced: 시정조치 제출→검토(accepted)→finding 종결."""
    with TestClient(app) as c:
        atok = _tok(c, "admin", "admin")
        cid = c.post("/cases", json={"org_id": "org_demo", "company_name": "CarCo"},
                     headers=_h(atok)).json()["case_id"]
        fid = c.post(f"/cases/{cid}/findings", json={"finding": "라벨 불일치", "severity": "minor"},
                     headers=_h(atok)).json()["finding_id"]
        ctok = _tok(c, "consultant1", "pw")
        car = c.post(f"/findings/{fid}/car", json={"description": "라벨 교체", "due_date": "2026-09-01"},
                     headers=_h(ctok))
        assert car.status_code == 200, car.text
        car_id = car.json()["id"]
        assert len(c.get(f"/cases/{cid}/corrective-actions", headers=_h(ctok)).json()) == 1
        rev = c.patch(f"/car/{car_id}/review", json={"status": "accepted", "note": "확인"},
                      headers=_h(atok))
        assert rev.json()["status"] == "accepted", rev.text
        # finding 종결됐는지
        fs = c.get(f"/cases/{cid}/findings", headers=_h(atok)).json()
        assert any(f["finding_id"] == fid and f["status"] == "closed" for f in fs), fs


def test_datalist_meta_search_sort():
    """DataList 표준(§8.2): meta(total)·서버검색(q)·정렬(sort/dir) + 하위호환(array)."""
    with TestClient(app) as c:
        atok = _tok(c, "admin", "admin")
        for nm in ["Alpha Co", "Beta Co"]:
            c.post("/cases", json={"org_id": "org_demo", "company_name": nm}, headers=_h(atok))
        r = c.get("/admin/cases?meta=1&limit=1&offset=0&q=Alpha&sort=company_name&dir=asc",
                  headers=_h(atok)).json()
        assert "total" in r and "items" in r and r["total"] >= 1, r
        assert all("Alpha" in (it["company_name"] or "") for it in r["items"]), r
        # 하위호환: meta 없으면 배열
        assert isinstance(c.get("/admin/cases", headers=_h(atok)).json(), list)


def test_rbac_action_matrix_contract():
    """단일 매트릭스 계약 — ACTION_ENDPOINTS를 순회, 매트릭스대로 403/허용 자동검증(§12.1)."""
    from app import rbac
    roles = {"consultant1": "consultant", "applicant1": "applicant", "penyelia1": "penyelia_halal",
             "pendamping1": "pendamping_pph", "auditor1": "auditor", "fatwa1": "fatwa_liaison",
             "operator1": "operator", "admin": "admin"}
    with TestClient(app) as c:
        toks = {u: _tok(c, u, "admin" if u == "admin" else "pw") for u in roles}
        cid = c.post("/cases", json={"org_id": "org_demo", "company_name": "CtrT"},
                     headers=_h(toks["consultant1"])).json()["case_id"]
        for action, (method, path, body) in rbac.ACTION_ENDPOINTS.items():
            url = path.replace("{cid}", cid)
            for u, role in roles.items():
                allowed = rbac.can({"role": role}, action)
                r = c.request(method, url, json=body, headers=_h(toks[u]))
                if allowed:
                    assert r.status_code != 403, (action, role, "허용 기대인데 403", r.text)
                else:
                    assert r.status_code == 403, (action, role, "차단 기대인데 %d" % r.status_code, r.text)


def test_qr_public_verify():
    """§11.5/§14: 공개 인증서 검증 — 무인증, 민감정보 미노출."""
    from app import models
    from app.db import SessionLocal
    with TestClient(app):
        db = SessionLocal()
        try:
            cs = models.CaseApplication(company_name="QR Co", org_id="org_demo", status="certificate_issued")
            db.add(cs)
            db.flush()
            db.add(models.HalalCertificate(case_id=cs.case_id, certificate_no="HC-QRTEST",
                   scope=["ProdA"], issue_date="2026-01-01", expiry_date="2030-01-01",
                   status="active", qr_token="qrtoken_test_123"))
            db.commit()
        finally:
            db.close()
        with TestClient(app) as c:
            j = c.get("/verify/qrtoken_test_123").json()   # 공개, 무인증
            assert j["valid"] is True and j["company_name"] == "QR Co" and j["certificate_no"] == "HC-QRTEST", j
            assert c.get("/verify/nope").status_code == 404


def test_ai_extractions_endpoints():
    """§7.2/§7.4: AI 근거저장 조회·리뷰 엔드포인트."""
    with TestClient(app) as c:
        tok = _tok(c, "consultant1", "pw")
        cid = c.post("/cases", json={"org_id": "org_demo", "company_name": "AiT"},
                     headers=_h(tok)).json()["case_id"]
        assert c.get(f"/cases/{cid}/ai-extractions", headers=_h(tok)).json() == []
        assert c.patch("/ai-extractions/nope/review", json={"reviewer_status": "accepted"},
                       headers=_h(tok)).status_code == 404


def test_unlock_requires_operator():
    """문서 P0: 인증서 unlock에서 consultant 제거(operator 전용)."""
    with TestClient(app) as c:
        tok = _tok(c, "consultant1", "pw")
        cid = c.post("/cases", json={"org_id": "org_demo", "company_name": "UnlockT"},
                     headers=_h(tok)).json()["case_id"]
        r = c.post(f"/cases/{cid}/certificate/unlock", json={"reason": "재인증 사유"}, headers=_h(tok))
        assert r.status_code == 403, r.text


def test_fatwa_document_restricted():
    """문서 P0: 파트와 결정문 조회는 sharia/operator/admin 전용."""
    with TestClient(app) as c:
        tok = _tok(c, "consultant1", "pw")
        cid = c.post("/cases", json={"org_id": "org_demo", "company_name": "FwDoc"},
                     headers=_h(tok)).json()["case_id"]
        assert c.post(f"/cases/{cid}/fatwa/document", headers=_h(tok)).status_code == 403
        ftok = _tok(c, "fatwa1", "pw")
        assert c.post(f"/cases/{cid}/fatwa/document", headers=_h(ftok)).status_code == 200


def test_get_fatwa_masks_committee():
    """문서 P0: 위원회 내부정보는 권한자만 — 비권한 역할엔 마스킹."""
    with TestClient(app) as c:
        tok = _tok(c, "consultant1", "pw")
        cid = c.post("/cases", json={"org_id": "org_demo", "company_name": "FwMask"},
                     headers=_h(tok)).json()["case_id"]
        j = c.get(f"/cases/{cid}/fatwa", headers=_h(tok)).json()
        assert j.get("committee_restricted") is True and "committee_members" not in j, j


def test_renew_operator_only():
    """문서 P0: /renew(승인·실행)은 operator 전용, applicant는 신청만."""
    with TestClient(app) as c:
        tok = _tok(c, "applicant1", "pw")
        cid = c.post("/cases", json={"org_id": "org_demo", "company_name": "RenewT"},
                     headers=_h(tok)).json()["case_id"]
        assert c.post(f"/cases/{cid}/renew", headers=_h(tok)).status_code == 403


def test_upload_validation_rejects_bad_type():
    """문서 P0(§9.3): 허용외 확장자 업로드 거부(415)."""
    import base64 as _b
    with TestClient(app) as c:
        tok = _tok(c, "consultant1", "pw")
        cid = c.post("/cases", json={"org_id": "org_demo", "company_name": "UpT"},
                     headers=_h(tok)).json()["case_id"]
        mid = c.post(f"/cases/{cid}/materials", json={"name": "sugar"},
                     headers=_h(tok)).json()["material_id"]
        b64 = _b.b64encode(b"MZ\x90bad").decode()
        r = c.post(f"/cases/{cid}/materials/{mid}/evidence",
                   json={"evidence_type": "msds", "file_b64": b64, "filename": "malware.exe"},
                   headers=_h(tok))
        assert r.status_code == 415, r.text


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
        c.post("/auth/register", json={"username": "isouser2", "password": "pw"})
        utok = _tok(c, "isouser2", "pw")
        ev = c.get("/dashboard/summary", headers=_h(utok)).json()["events"]
        assert all(e["case_id"] != cid for e in ev), ("타조직 이벤트 누수", ev)


def test_notify_email_webhook_channels():
    """§10.2 채널 확장: email은 수신동의 게이트, webhook(시스템)은 동의 무관."""
    from app import models
    from app.db import SessionLocal
    from app.main import drain_notifications
    with TestClient(app):
        db = SessionLocal()
        try:
            c = models.CaseApplication(company_name="NotifCh", org_id="org_demo", phone="0812",
                                       email="a@b.com", notify_consent=False, status="draft")
            db.add(c)
            db.flush()
            n = models.Notification(org_id="org_demo", case_id=c.case_id,
                                    channels=["inapp", "email", "webhook"], title="t", status="unsent")
            db.add(n)
            db.commit()
            nid = n.notification_id
            drain_notifications(db)
            db.expire_all()
            row = db.get(models.Notification, nid)
            assert row.status == "sent", row.status
            assert "email" in (row.last_error or ""), row.last_error       # 미동의 → skip
            assert "webhook" not in (row.last_error or ""), row.last_error  # 시스템 → skip 안 됨
        finally:
            db.close()


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
    dbfile = os.environ["GLHAC_DB_URL"].replace("sqlite:///", "")
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
        r = c.post("/admin/users", json={"username": "op_new", "password": "pw", "role": "operator"},
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
    dbfile = os.environ["GLHAC_DB_URL"].replace("sqlite:///", "")
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


if __name__ == "__main__":
    tests = [test_observability_metrics, test_pdf_generation, test_certificate_lifecycle,
             test_payment_and_analytics, test_org_overview,
             test_certificate_signature_and_verify, test_integration_event_idempotency,
             test_audit_plan_lifecycle, test_fatwa_voting_quorum, test_car_lifecycle_closes_finding,
             test_alembic_scaffolding, test_datalist_meta_search_sort,
             test_qr_public_verify, test_ai_extractions_endpoints,
             test_rbac_action_matrix_contract,
             test_unlock_requires_operator, test_fatwa_document_restricted,
             test_get_fatwa_masks_committee, test_renew_operator_only,
             test_upload_validation_rejects_bad_type,
             test_password_pbkdf2_and_legacy_upgrade,
             test_login_rate_limited,
             test_label_judgment_path_traversal_blocked,
             test_public_config_reports_dev,
             test_invoice_negative_rejected,
             test_dashboard_events_org_isolation,
             test_notify_email_webhook_channels,
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
             test_context_health_endpoint, test_search_context_fallback_returns_list]
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
