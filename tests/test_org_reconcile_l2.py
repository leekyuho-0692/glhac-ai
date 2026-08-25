"""L2 조직도 서류 ↔ 신청서 담당자 교차검증 — 회귀테스트.

- 핵심 대조 로직(_reconcile_org / _org_name_score / _org_norm)은 알려진 텍스트로 결정적 검증.
- 엔드포인트(/cases/{id}/halal-org/reconcile)는 플러밍(문서조회·담당자파생·구조·이벤트) 검증.

실행: <venv>/bin/pytest tests/test_org_reconcile_l2.py -q
"""
import base64
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("GLHAC_DB_URL", "sqlite:///./glhac_org_l2_test.db")
os.environ.setdefault("GLHAC_DEV", "1")

from fastapi.testclient import TestClient  # noqa: E402
from app.main import app, _reconcile_org, _org_name_score, _org_norm  # noqa: E402

# 1x1 투명 PNG (OCR 미설치 환경에서도 엔드포인트 플러밍만 검증)
_PNG_1x1 = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAC0lEQVR4nGNgYGAA"
            "AAAEAAH2FzhVAAAAAElFTkSuQmCC")


def test_reconcile_logic_missing_and_match():
    persons = [{"name": "홍길동", "role": "top_management"},
               {"name": "김철수", "role": "halal_supervisor"},
               {"name": "이영희", "role": "coordinator"}]
    text = "조직도 홍길동 대표이사 김철수 penyelia halal 생산부"   # 이영희 없음
    r = _reconcile_org(persons, text)
    matched = {m["name"] for m in r["matched"]}
    assert "홍길동" in matched and "김철수" in matched, r
    miss = {m["name"]: m for m in r["mismatches"]}
    assert "이영희" in miss and miss["이영희"]["type"] == "missing_in_doc"
    assert not any(m["type"] == "penyelia_absent" for m in r["mismatches"])  # penyelia 매칭됨
    assert r["summary"]["matched"] == 2 and r["summary"]["total"] == 3


def test_reconcile_penyelia_absent_high():
    persons = [{"name": "홍길동", "role": "top_management"},
               {"name": "김철수", "role": "halal_supervisor"}]
    text = "조직도 홍길동 대표 생산부 품질관리부"   # 할랄감독자(김철수) 없음
    r = _reconcile_org(persons, text)
    assert any(m["type"] == "penyelia_absent" and m["severity"] == "high"
               for m in r["mismatches"]), r


def test_reconcile_honorific_normalization():
    text = "struktur organisasi bapak budi santoso ketua tim ibu siti aminah"
    tn = _org_norm(text)
    assert _org_name_score("Budi Santoso", tn, tn.split()) >= 0.9   # 경칭 Bapak 제거 후 매칭
    assert _org_name_score("Siti Aminah", tn, tn.split()) >= 0.9


def _tok(c, u, p):
    r = c.post("/auth/login", json={"username": u, "password": p})
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["token"]}


def test_reconcile_endpoint_no_chart_400():
    with TestClient(app) as c:
        h = _tok(c, "consultant1", "pw")
        cid = c.post("/cases", json={"company_name": "PT Tanpa Bagan"}, headers=h).json()["case_id"]
        r = c.post(f"/cases/{cid}/halal-org/reconcile", json={}, headers=h)
        assert r.status_code == 400, r.text  # 조직도 이미지 없음


def test_reconcile_endpoint_with_chart_200():
    with TestClient(app) as c:
        h = _tok(c, "consultant1", "pw")
        cid = c.post("/cases", json={"company_name": "PT Sinar Halal"}, headers=h).json()["case_id"]
        c.patch(f"/cases/{cid}/profile", json={"responsible_person": "홍길동",
                "halal_supervisor": "김철수",
                "profile_ext": {"pic_name": "이영희", "cp_name": "박민수"}}, headers=h)
        c.post("/orgs/org_demo/penyelia", json={"name": "최지훈"}, headers=h)
        # 조직도 이미지 업로드 + layout 삽입
        d = c.post(f"/cases/{cid}/documents",
                   json={"filename": "org.png", "file_b64": _PNG_1x1, "doc_type": "manual_section"},
                   headers=h).json()
        docid = d["document_id"]
        c.post(f"/cases/{cid}/sjph-manual/layout",
               json={"order": [], "inserts": {"org_chart": {"document_id": docid, "filename": "org.png"}}},
               headers=h)
        r = c.post(f"/cases/{cid}/halal-org/reconcile", json={}, headers=h)
        assert r.status_code == 200, r.text
        j = r.json()
        assert j["document_id"] == docid
        assert set(j) >= {"matched", "mismatches", "summary", "ocr_available"}
        # penyelia는 org 단위라 같은 프로세스의 앞선 테스트가 org_demo에 더 남길 수 있고,
        # 대조 목록은 (이름, 역할)로 중복이 접힌다 → 절대 수치로는 판정할 수 없다.
        # 이 테스트의 주장은 "이 케이스가 신고한 담당자 전원이 대조 대상에 든다"이므로
        # 이름으로 확인한다(전체 실행에서만 깨지던 원인).
        _names = {m.get("name") for m in j["mismatches"]} | {m.get("name") for m in j["matched"]}
        for _n in ("홍길동", "김철수", "최지훈", "이영희", "박민수"):
            assert _n in _names, (_n, sorted(x for x in _names if x))
        assert j["summary"]["total"] >= 5, j["summary"]


if __name__ == "__main__":
    test_reconcile_logic_missing_and_match()
    test_reconcile_penyelia_absent_high()
    test_reconcile_honorific_normalization()
    test_reconcile_endpoint_no_chart_400()
    test_reconcile_endpoint_with_chart_200()
    print("✅ L2 교차검증 테스트 전부 통과")
