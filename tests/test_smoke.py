"""스모크 테스트 — 자기선언/정규 두 경로 + 가드 차단. 설계 24.2.6/24.11."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("GLHAC_DB_URL", "sqlite:///./glhac_test.db")
os.environ.setdefault("GLHAC_DEV", "1")   # 데모 계정 시드 + 기본 시크릿 허용(테스트 전용)

from fastapi.testclient import TestClient  # noqa: E402
from app.main import app  # noqa: E402

ORG = "org_test"


def _auth(client, u="admin", p="admin"):
    """v3: RBAC 도입 → 로그인 후 토큰을 클라이언트 기본 헤더에 설정.
    admin은 모든 require_roles 우회 + 조직 격리 무시 → 워크플로우(상태머신/가드) 검증에 적합."""
    r = client.post("/auth/login", json={"username": u, "password": p})
    assert r.status_code == 200, r.text
    client.headers.update({"Authorization": "Bearer " + r.json()["token"]})
    return client


def _walk(client, cid, states):
    for s in states:
        r = client.post(f"/cases/{cid}/transition", json={"to_state": s})
        assert r.status_code == 200, (s, r.json())


def _intake_complete(client, cid, company="ABC"):
    """§5.2 신청완비 가드(ai_pre_assessment_ready 진입) 충족 — 회사명·NIB·제품 최소 1개.
    가드가 전면화(하드닝)되어 raw 전이 전에 이 최소 신청정보가 필요하다."""
    # 사업자번호는 케이스마다 다르게 만든다 — 회사가 다른데 번호가 같으면 실제로 막힌다
    # (그게 NIB 중복 검증의 목적이다). 고정값을 쓰면 두 번째 테스트부터 409 가 난다.
    nib = ("%010d" % (abs(hash(cid)) % 10**10))
    # 자기선언 임계값(연매출·매장 수)은 '모르면 판정 불가'다 — 신청 완비에 포함한다.
    client.patch(f"/cases/{cid}/profile", json={
        "company_name": company, "nib": nib,
        "profile_ext": {"annual_revenue": 1_000_000, "outlet_count": 1}})
    client.post(f"/cases/{cid}/products", json={"name": "Sample Product", "category": "Food"})


def test_self_declare_happy_path():
    with TestClient(app) as client:
        _auth(client)
        assert client.get("/health").json()["status"] == "ok"
        client.post(f"/orgs/{ORG}/penyelia", json={"name": "Budi", "training_cert": "PH-1"})
        cid = client.post("/cases", json={"org_id": ORG, "company_name": "ABC", "is_msme": True}).json()["case_id"]
        # 저위험 원재료(citric acid=halal)만
        client.post(f"/cases/{cid}/materials", json={"name": "citric acid", "e_number": "E330"})
        # SIHALAL 동일아이디 연동 (1회 등록 → 동일성 검증)
        ei = client.post(f"/cases/{cid}/sihalal/identity/link", json={"external_email": "abc@x.com"}).json()
        v = client.post(f"/sihalal/identity/{ei['external_identity_id']}/verify",
                        json={"expected_identifier": "abc@x.com"}).json()
        assert v["identifier_match"] is True, v
        _intake_complete(client, cid, company="ABC")
        _walk(client, cid, ["application_draft", "ai_pre_assessment_ready",
                            "ai_pre_assessment_running", "pathway_determination"])
        a = client.post(f"/cases/{cid}/pathway/assess").json()
        assert a["suggested_pathway"] == "self_declare", a
        client.post(f"/cases/{cid}/pathway/confirm", json={"pathway": "self_declare"})
        _walk(client, cid, ["sjph_lite_prepared", "pendamping_verification"])
        client.post(f"/cases/{cid}/pendamping/assign", json={"pendamping_id": "pendamping1"})
        client.post(f"/cases/{cid}/pendamping/verify", json={"decision": "verified"})
        # 펜담핑 검증(verified)은 가드 통과 시 self_declaration_submitted로 자동전이(app: pendamping.verify.auto)
        assert client.get(f"/cases/{cid}").json()["status"] == "self_declaration_submitted"
        _walk(client, cid, ["committee_verification"])
        # KFPH(자기선언 위원회) 승인 → fatwa_status=approved (certificate/issue 하드게이트 통과 선행)
        cd = client.post(f"/cases/{cid}/committee/decide", json={"decision": "approve", "reason": "ok"})
        assert cd.status_code == 200 and cd.json()["fatwa_status"] == "approved", cd.text
        # certificate_issued는 보호상태 — raw transition 금지, 전용 발급 엔드포인트만 허용
        blocked = client.post(f"/cases/{cid}/transition", json={"to_state": "certificate_issued"})
        assert blocked.status_code == 403 and blocked.json()["detail"]["code"] == "USE_DEDICATED_ENDPOINT", blocked.text
        iss = client.post(f"/cases/{cid}/certificate/issue")
        assert iss.status_code == 200, iss.text
        # 인증서 발급은 2인 승인(maker=운영자/admin, checker=fatwa_liaison) — checker가 승인해야 실제 발급
        appr_id = iss.json().get("approval_id")
        assert appr_id, iss.text
        ftok = client.post("/auth/login", json={"username": "fatwa1", "password": "pw"}).json()["token"]
        ap = client.post(f"/approvals/{appr_id}/approve",
                         headers={"Authorization": "Bearer " + ftok})
        assert ap.status_code == 200, ap.text
        assert ap.json()["result"].get("certificate_no"), ap.text
        assert client.get(f"/cases/{cid}").json()["status"] == "certificate_issued"
        # 발급이 상태를 진행시키므로 갱신(certificate_issued 필요)이 동작해야 — 회귀 방지
        rn = client.post(f"/cases/{cid}/renew")
        assert rn.status_code == 200, rn.text


def test_haram_blocks_selfdeclare():
    with TestClient(app) as client:
        _auth(client)
        client.post(f"/orgs/{ORG}/penyelia", json={"name": "Budi"})
        cid = client.post("/cases", json={"org_id": ORG, "is_msme": True}).json()["case_id"]
        client.post(f"/cases/{cid}/materials", json={"name": "lard"})  # haram → BLOCK
        _intake_complete(client, cid, company="Haram Co")
        _walk(client, cid, ["application_draft", "ai_pre_assessment_ready",
                            "ai_pre_assessment_running", "pathway_determination"])
        a = client.post(f"/cases/{cid}/pathway/assess").json()
        assert a["suggested_pathway"] == "reguler"
        # 자기선언 시도 → 가드 차단(409)
        r = client.post(f"/cases/{cid}/pathway/confirm", json={"pathway": "self_declare"})
        assert r.status_code == 409
        assert any(b["code"] == "HAS_CRITICAL_MATERIAL" for b in r.json()["detail"]["blockers"])


def test_selfdeclare_submit_blocked_without_sihalal():
    with TestClient(app) as client:
        _auth(client)
        client.post(f"/orgs/{ORG}/penyelia", json={"name": "Budi"})
        cid = client.post("/cases", json={"org_id": ORG, "is_msme": True}).json()["case_id"]
        client.post(f"/cases/{cid}/materials", json={"name": "sugar"})  # unknown→비임계
        _intake_complete(client, cid, company="NoSihalal Co")
        _walk(client, cid, ["application_draft", "ai_pre_assessment_ready",
                            "ai_pre_assessment_running", "pathway_determination"])
        client.post(f"/cases/{cid}/pathway/confirm", json={"pathway": "self_declare"})
        _walk(client, cid, ["sjph_lite_prepared", "pendamping_verification"])
        client.post(f"/cases/{cid}/pendamping/assign", json={"pendamping_id": "pendamping1"})
        client.post(f"/cases/{cid}/pendamping/verify", json={"decision": "verified"})
        # SIHALAL 미연동 → 제출 차단
        r = client.post(f"/cases/{cid}/transition", json={"to_state": "self_declaration_submitted"})
        assert r.status_code == 409
        assert any(b["code"] == "SIHALAL_IDENTITY_UNVERIFIED" for b in r.json()["detail"]["blockers"])


if __name__ == "__main__":
    test_self_declare_happy_path()
    test_haram_blocks_selfdeclare()
    test_selfdeclare_submit_blocked_without_sihalal()
    print("ALL SMOKE TESTS PASSED")