"""물류 인증(scheme) — 신청 맨 앞에서 제품/물류를 고르면 서류·현장항목·부록이 갈린다.

왜 pathway 가 아니라 새 축인가
  pathway(SEHATI/Reguler)는 시스템이 원재료를 보고 유도한다. 물류사에는 판정 근거가 될
  원재료가 없어 유도할 수 없다 → 신청자가 처음에 선언하는 값이다. 그래서 별도 축(scheme).

경계
  이 시스템은 '할랄 물류업체 인증'(자격 심사·발급)만 한다. 운송 건별 무결성 증명은
  별도 물류 시스템 소관이다. 여기서 발급한 인증서를 그쪽이 참조한다.

실행: <venv>/bin/python -m pytest tests/test_logistics_scheme.py -q
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("GLHAC_DB_URL", "sqlite:///./glhac_scheme_test.db")
os.environ.setdefault("GLHAC_DEV", "1")

import pytest                                # noqa: E402
from fastapi.testclient import TestClient    # noqa: E402

from app import intake, schemas              # noqa: E402
from app import main as m                    # noqa: E402
from app.main import app                     # noqa: E402


def _tok(c, u="applicant1", p="pw"):
    r = c.post("/auth/login", json={"username": u, "password": p})
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["token"]}


# ── 스키마 검증(앱 없이) ────────────────────────────────────────────────────

def test_물류는_jasa_최소1개를_요구한다():
    with pytest.raises(Exception):
        schemas.CaseCreate(scheme="logistics")            # scope 없음 → 거부


def test_물류_scope_는_허용값만_남긴다():
    o = schemas.CaseCreate(scheme="logistics",
                           logistics_scope=["penyimpanan", "엉뚱", "pengemasan"])
    assert o.logistics_scope == ["penyimpanan", "pengemasan"]


def test_제품이면_scope_를_버린다():
    o = schemas.CaseCreate(scheme="product", logistics_scope=["penyimpanan"])
    assert o.logistics_scope is None


def test_알수없는_scheme_는_거부된다():
    with pytest.raises(Exception):
        schemas.CaseCreate(scheme="drone")


# ── 서류·현장항목·부록이 scheme 별로 갈리는가 ───────────────────────────────

def test_서류요구가_갈린다():
    prod = intake.doc_requirements(pathway="reguler")["required"]
    logi = intake.doc_requirements(scheme="logistics")["required"]
    assert "material_list" in prod and "material_list" not in logi      # 제품 전용
    assert "vehicle_list" in logi and "vehicle_list" not in prod        # 물류 전용
    assert "sjph_manual" in prod and "sjph_manual" in logi              # 공통


def test_현장항목이_갈린다():
    prod = {k for k, _ in m.onsite_items("product")}
    logi = {k for k, _ in m.onsite_items("logistics")}
    assert "production_process" in prod and "production_process" not in logi
    assert "vehicle_cleaning" in logi and "vehicle_cleaning" not in prod
    assert "contamination_prevention" in prod & logi                    # 공통


def test_부록이_갈린다():
    prod = [l for l, _, _ in m.sjph_appendices("product")]
    logi = [l for l, _, _ in m.sjph_appendices("logistics")]
    assert any("생산 공정흐름도" in l for l in prod)
    assert not any("생산 공정흐름도" in l for l in logi)
    assert any("세척 SOP" in l for l in logi)
    assert len(prod) == len(logi) == 17                                 # 부록 수는 같다


def test_물류_서류라벨이_3개국어로_있다():
    for lang in ("ko", "en", "id"):
        labels = intake.doc_labels(lang) if hasattr(intake, "doc_labels") else None
    from app import domain_dict as D
    for lang in ("ko", "en", "id"):
        L = D.doc_labels(lang)
        for dt in ("logistics_scope", "warehouse_layout", "vehicle_list", "cleaning_sop"):
            assert L.get(dt), "%s/%s 라벨 없음" % (lang, dt)


# ── 엔드투엔드: 케이스 생성 → 현장 체크리스트가 물류 항목 ──────────────────

def test_제품_케이스는_기본_product():
    with TestClient(app) as c:
        h = _tok(c)
        j = c.post("/cases", json={"company_name": "제품테스트"}, headers=h).json()
        assert j["scheme"] == "product"


def test_물류_케이스_생성과_체크리스트():
    with TestClient(app) as c:
        h = _tok(c)
        # scope 없이는 422
        r = c.post("/cases", json={"company_name": "물류무scope", "scheme": "logistics"}, headers=h)
        assert r.status_code == 422, r.text
        # scope 포함하면 생성
        j = c.post("/cases", json={"company_name": "물류테스트", "scheme": "logistics",
                                   "logistics_scope": ["penyimpanan", "pendistribusian"]},
                   headers=h).json()
        assert j["scheme"] == "logistics"
        assert j["logistics_scope"] == ["penyimpanan", "pendistribusian"]
        # 이 케이스의 현장 체크리스트는 물류 항목이어야 한다
        oc = c.get("/cases/%s/onsite-checklist" % j["case_id"], headers=h).json()
        keys = {i["item_key"] for i in oc["items"]}
        assert "vehicle_cleaning" in keys
        assert "production_process" not in keys


def test_목록응답에_scheme이_실린다():
    """케이스 목록(/cases)에 scheme·logistics_scope 가 없으면 배지를 못 그린다."""
    with TestClient(app) as c:
        h = _tok(c)
        c.post("/cases", json={"company_name": "물류목록", "scheme": "logistics",
                               "logistics_scope": ["penyimpanan"]}, headers=h)
        items = c.get("/cases?limit=50", headers=h).json()["items"]
        logi = [x for x in items if x["company_name"] == "물류목록"]
        assert logi and logi[0]["scheme"] == "logistics"
        assert logi[0]["logistics_scope"] == ["penyimpanan"]
        # 제품 케이스는 scheme=product 로 실린다
        c.post("/cases", json={"company_name": "제품목록"}, headers=h)
        items = c.get("/cases?limit=50", headers=h).json()["items"]
        prod = [x for x in items if x["company_name"] == "제품목록"]
        assert prod and prod[0]["scheme"] == "product"


def test_물류_체크리스트가_물류서류를_한글라벨로_준다():
    """doc-checklist 행 루프가 제품 고정이면 물류 서류가 코드로 뜬다(실측 버그)."""
    with TestClient(app) as c:
        h = _tok(c)
        cid = c.post("/cases", json={"company_name": "물류CL", "scheme": "logistics",
                                     "logistics_scope": ["penyimpanan"]}, headers=h).json()["case_id"]
        cl = c.get("/cases/%s/doc-checklist?lang=ko" % cid, headers=h).json()["checklist"]
        types = {r["doc_type"]: r["doc_type_ko"] for r in cl}
        assert "warehouse_layout" in types
        assert types["warehouse_layout"] != "warehouse_layout"   # 코드가 아닌 한글 라벨
        assert types["vehicle_list"] != "vehicle_list"
        assert "product_list" not in types                       # 제품 서류는 안 나온다
