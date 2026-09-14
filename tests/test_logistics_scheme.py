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


def test_물류_인증서_발급이_scheme을_따른다():
    """물류 발급은 번호 GLHAC-HL, scope=jasa, frozen_jasa 동결. 제품과 섞이면 안 된다."""
    from app import models
    with TestClient(app) as cl:
        h = _tok(cl, "admin", "admin")
        cid = cl.post("/cases", json={"company_name": "발급물류", "org_id": "org_demo",
                                      "scheme": "logistics", "logistics_scope": ["penyimpanan", "pendistribusian"]},
                      headers=h).json()["case_id"]
        db = next(m.get_db())
        cc = db.get(models.CaseApplication, cid)
        r = m._do_issue_certificate(db, cc, {"uid": "admin", "role": "admin"})
        assert r["certificate_no"].startswith("GLHAC-HL-")
        assert r["scope"] == ["penyimpanan", "pendistribusian"]      # jasa
        assert r["frozen_jasa"] == ["penyimpanan", "pendistribusian"]
        assert not r["frozen_product_ids"]                          # 제품 동결 없음


def test_제품_인증서는_물류연동을_하지_않는다():
    from app import models
    with TestClient(app) as cl:
        h = _tok(cl, "admin", "admin")
        cid = cl.post("/cases", json={"company_name": "발급제품", "org_id": "org_demo"}, headers=h).json()["case_id"]
        db = next(m.get_db())
        cc = db.get(models.CaseApplication, cid)
        r = m._do_issue_certificate(db, cc, {"uid": "admin", "role": "admin"})
        assert r["certificate_no"].startswith("HC-")               # 기존 제품 번호 유지
        assert r["scheme"] == "product"
        # 물류 연동 이벤트가 생기지 않는다
        assert db.query(models.IntegrationEvent).filter_by(
            case_id=cid, provider="logistics_audit").count() == 0


def test_물류발급이_sync_이벤트를_남긴다():
    """webhook URL 미설정이어도 IntegrationEvent 에 pending 으로 근거를 남긴다(재전송 가능)."""
    from app import models
    with TestClient(app) as cl:
        h = _tok(cl, "admin", "admin")
        cid = cl.post("/cases", json={"company_name": "동기화", "org_id": "org_demo",
                                      "scheme": "logistics", "logistics_scope": ["pengemasan"]},
                      headers=h).json()["case_id"]
        db = next(m.get_db())
        cc = db.get(models.CaseApplication, cid)
        m._do_issue_certificate(db, cc, {"uid": "admin", "role": "admin"})
        ev = db.query(models.IntegrationEvent).filter_by(
            case_id=cid, provider="logistics_audit").first()
        assert ev is not None and ev.event_type == "certificate.issued"
        assert ev.status in ("pending", "processed", "failed")
        assert ev.payload["jasa"] == ["pengemasan"]


def test_서류제출전에는_종류를_바꿀수있다():
    with TestClient(app) as cl:
        h = _tok(cl)
        cid = cl.post("/cases", json={"company_name": "잠금전"}, headers=h).json()["case_id"]
        r = cl.patch("/cases/%s/profile" % cid,
                     json={"company_name": "잠금전", "scheme": "logistics",
                           "logistics_scope": ["penyimpanan"]}, headers=h)
        assert r.status_code == 200, r.text
        assert cl.get("/cases/%s" % cid, headers=h).json()["scheme"] == "logistics"


def test_서류제출후에는_종류가_잠긴다():
    import base64
    with TestClient(app) as cl:
        h = _tok(cl)
        cid = cl.post("/cases", json={"company_name": "잠금후"}, headers=h).json()["case_id"]
        # 문서 하나 업로드 → scheme 잠김
        b64 = base64.b64encode(b"%PDF-1.4 test").decode()
        up = cl.post("/cases/%s/documents" % cid,
                     json={"filename": "x.pdf", "file_b64": b64, "doc_type": "other"}, headers=h)
        assert up.status_code == 200, up.text
        # 이제 종류 변경은 409
        r = cl.patch("/cases/%s/profile" % cid,
                     json={"company_name": "잠금후", "scheme": "logistics",
                           "logistics_scope": ["penyimpanan"]}, headers=h)
        assert r.status_code == 409
        assert r.json()["detail"]["code"] == "SCHEME_FROZEN"
        # scheme_frozen 플래그도 켜졌다
        assert cl.get("/cases/%s" % cid, headers=h).json()["scheme_frozen"] is True


def test_물류연동_이벤트_목록과_재전송():
    """미전송 sync 를 목록에서 보고 재전송할 수 있다(URL 미설정이면 pending 유지)."""
    from app import models
    with TestClient(app) as cl:
        h = _tok(cl, "admin", "admin")
        cid = cl.post("/cases", json={"company_name": "재전송대상", "org_id": "org_demo",
                                      "scheme": "logistics", "logistics_scope": ["penyimpanan"]},
                      headers=h).json()["case_id"]
        db = next(m.get_db()); cc = db.get(models.CaseApplication, cid)
        m._do_issue_certificate(db, cc, {"uid": "admin", "role": "admin"})
        # 목록
        lst = cl.get("/admin/logistics-sync", headers=h).json()
        assert lst["counts"]["pending"] >= 1
        target = [x for x in lst["items"] if x["case_id"] == cid][0]
        # 재전송(URL 미설정 → pending 유지, retry_count 증가)
        r = cl.post("/admin/logistics-sync/%s/resend" % target["id"], headers=h).json()
        assert r["retry_count"] == 1
        assert r["status"] in ("pending", "processed", "failed")


def test_모의심사_사진섹션이_scheme을_따른다():
    """모의심사 사진·영상 증빙 섹션이 제품/물류로 갈린다."""
    prod = {k for k, _ in m.mock_evidence_sections("product")}
    logi = {k for k, _ in m.mock_evidence_sections("logistics")}
    assert "production_video" in prod and "production_video" not in logi
    assert "vehicle_condition" in logi and "vehicle_condition" not in prod
    assert "loading_unloading" in logi and "warehouse_storage" in logi


def test_물류_사진섹션_판정은_물류키만_받는다():
    with TestClient(app) as cl:
        h = _tok(cl, "admin", "admin")
        cid = cl.post("/cases", json={"company_name": "사진물류", "org_id": "org_demo",
                                      "scheme": "logistics", "logistics_scope": ["penyimpanan"]},
                      headers=h).json()["case_id"]
        # 제품 섹션 키는 거부
        r1 = cl.post("/cases/%s/mock-audit/evidence-verdict" % cid,
                     json={"section": "production_video", "verdict": "comply"}, headers=h)
        assert r1.status_code == 422
        # 물류 섹션 키는 허용
        r2 = cl.post("/cases/%s/mock-audit/evidence-verdict" % cid,
                     json={"section": "vehicle_condition", "verdict": "comply"}, headers=h)
        assert r2.status_code == 200, r2.text
        # detail 이 scheme·물류 섹션을 돌려준다
        d = cl.get("/cases/%s/mock-audit/detail" % cid, headers=h).json()
        assert d["scheme"] == "logistics"
        keys = {x["section"] for x in d["sections"]}
        assert "vehicle_condition" in keys and "production_video" not in keys


def test_차량_등록과_조회():
    """물류 운송 자산(차량) 등록·조회·필드 보존."""
    with TestClient(app) as cl:
        h = _tok(cl, "consultant1", "pw")
        cid = cl.post("/cases", json={"company_name": "차량물류", "scheme": "logistics",
                                      "logistics_scope": ["pendistribusian"]}, headers=h).json()["case_id"]
        # 번호판 없으면 422
        r0 = cl.post("/orgs/org_demo/vehicles", json={"vehicle_type": "truck"}, headers=h)
        assert r0.status_code == 422
        # 등록
        r = cl.post("/orgs/org_demo/vehicles", json={
            "plate_no": "B 9 XYZ", "vehicle_type": "reefer", "transport_type": "frozen",
            "previous_cargo": "pork", "previous_cargo_halal": False, "sertu": False,
            "case_id": cid}, headers=h)
        assert r.status_code == 200, r.text
        vid = r.json()["vehicle_id"]
        rows = cl.get("/orgs/org_demo/vehicles", headers=h).json()
        v = [x for x in rows if x["vehicle_id"] == vid][0]
        assert v["plate_no"] == "B 9 XYZ" and v["transport_type"] == "frozen"
        assert v["previous_cargo_halal"] is False and v["sertu"] is False
        # Sertu 세정 후 편집
        cl.patch("/vehicles/%s" % vid, json={"sertu": True, "last_cleaned": "2026-09-13"}, headers=h)
        v2 = [x for x in cl.get("/orgs/org_demo/vehicles", headers=h).json()
              if x["vehicle_id"] == vid][0]
        assert v2["sertu"] is True
        # 삭제
        assert cl.delete("/vehicles/%s" % vid, headers=h).json()["deleted"] == vid


def test_차량_등록시_연동이벤트를_남긴다():
    """차량 등록·수정·삭제가 logistics-audit sync 이벤트를 남긴다(URL 미설정=pending)."""
    from app import models
    with TestClient(app) as cl:
        h = _tok(cl, "consultant1", "pw")
        cid = cl.post("/cases", json={"company_name": "차량연동", "scheme": "logistics",
                                      "logistics_scope": ["pendistribusian"]}, headers=h).json()["case_id"]
        r = cl.post("/orgs/org_demo/vehicles", json={"plate_no": "B 5 SYNC",
                    "previous_cargo_halal": False, "sertu": False, "case_id": cid}, headers=h)
        vid = r.json()["vehicle_id"]
        db = next(m.get_db())
        ev = db.query(models.IntegrationEvent).filter_by(
            external_id="B 5 SYNC", provider="logistics_audit").first()
        assert ev is not None and ev.event_type == "vehicle.upsert"
        assert ev.payload["previous_cargo_halal"] is False
        assert ev.payload["company_name"]        # org 없어도 case 로 폴백
        # 삭제도 이벤트
        cl.delete("/vehicles/%s" % vid, headers=h)
        dele = db.query(models.IntegrationEvent).filter_by(
            external_id="B 5 SYNC", event_type="vehicle.delete").first()
        assert dele is not None


def test_차량_sync_url_유도():
    """차량 sync URL 은 인증서 URL 에서 경로만 바꿔 얻는다."""
    import os
    os.environ["GLHAC_LOGISTICS_WEBHOOK_URL"] = "https://x/v1/certs/logistics-sync"
    try:
        assert m._logistics_url("vehicle") == "https://x/v1/vehicles/sync"
        assert m._logistics_url("cert") == "https://x/v1/certs/logistics-sync"
    finally:
        del os.environ["GLHAC_LOGISTICS_WEBHOOK_URL"]


def test_신청메뉴가_제품_로지스틱으로_나뉜다():
    """GRP_2(신청)에 제품 신청/로지스틱 신청 메뉴가 시드되고 /me/menus 로 노출된다."""
    from app import models
    with TestClient(app) as cl:
        db = next(m.get_db())
        m._ensure_apply_menus(db)   # idempotent
        codes = {x.menu_code for x in db.query(models.SysMenu).filter(
            models.SysMenu.menu_code.in_(["APPLYPRODUCT", "APPLYLOGISTICS"])).all()}
        assert codes == {"APPLYPRODUCT", "APPLYLOGISTICS"}
        # consultant 노출
        h = _tok(cl, "consultant1", "pw")
        tree = cl.get("/me/menus?lang=ko", headers=h).json()
        routes = {c.get("routePath") for g in tree for c in g.get("children", [])}
        assert "applyProduct" in routes and "applyLogistics" in routes
        # 기존 '신청'(application)은 사이드바에서 감춰졌다
        assert "application" not in routes


def test_jasa별로_서류가_갈린다():
    """유통만이면 창고 배치도 불필요, 보관만이면 차량 목록 불필요."""
    from app import intake
    dist = set(intake.doc_requirements(scheme="logistics", logistics_scope=["pendistribusian"])["required"])
    stor = set(intake.doc_requirements(scheme="logistics", logistics_scope=["penyimpanan"])["required"])
    both = set(intake.doc_requirements(scheme="logistics",
               logistics_scope=["penyimpanan", "pendistribusian"])["required"])
    assert "vehicle_list" in dist and "warehouse_layout" not in dist      # 유통 → 차량, 창고X
    assert "warehouse_layout" in stor and "vehicle_list" not in stor      # 보관 → 창고, 차량X
    assert {"vehicle_list", "warehouse_layout"} <= both                   # 둘 다 → 둘 다
    # 공통은 항상
    assert {"nib_business_license", "sjph_manual", "cleaning_sop"} <= dist


def test_물류_intake게이트는_제품대신_jasa를_본다():
    """물류는 제품 0개여도 회사명·NIB·jasa 있으면 신청 완비 게이트를 통과한다."""
    from app import state_machine as sm, models
    with TestClient(app) as cl:
        h = _tok(cl, "admin", "admin")
        cid = cl.post("/cases", json={"company_name": "게이트물류", "org_id": "org_demo",
                                      "scheme": "logistics", "logistics_scope": ["pendistribusian"]},
                      headers=h).json()["case_id"]
        db = next(m.get_db()); c = db.get(models.CaseApplication, cid)
        codes = {x["code"] for x in sm.guard_intake_complete(db, c)}
        assert "NO_PRODUCT" not in codes and "NO_JASA" not in codes       # 제품게이트 안 걸림
        assert "NIB_MISSING" in codes                                     # NIB 는 여전히 필요
        c.nib = "1234"; db.commit()
        assert sm.guard_intake_complete(db, c) == []                      # NIB 채우면 통과
        # jasa 없는 물류는 케이스 생성 단계에서 이미 422 로 막힌다(더 앞선 방어)
        r = cl.post("/cases", json={"company_name": "noj", "scheme": "logistics",
                                    "logistics_scope": []}, headers=h)
        assert r.status_code == 422


# ===== 차량 세척 로그 (jasa logistik) =====
def _mk_vehicle(cl, h, org="org_demo", **kw):
    body = {"plate_no": kw.pop("plate_no", "B 1 CLEAN"), **kw}
    return cl.post(f"/orgs/{org}/vehicles", json=body, headers=h).json()["vehicle_id"]


def _veh_clear(cl, h, vid, org="org_demo"):
    return [x for x in cl.get(f"/orgs/{org}/vehicles", headers=h).json()
            if x["vehicle_id"] == vid][0]["halal_clear"]


def test_세척로그로_halal_clear가_유도된다():
    """직전 비할랄 → 일반세척·서명전 False, Sertu+서명 후에만 True."""
    with TestClient(app) as cl:
        h = _tok(cl, "admin", "admin")
        vid = _mk_vehicle(cl, h, previous_cargo="pork", previous_cargo_halal=False)
        assert _veh_clear(cl, h, vid) is False                      # 세척 전
        cl.post(f"/vehicles/{vid}/cleanings",
                json={"cleaned_at": "2026-09-14", "previous_cargo_halal": False,
                      "method": "normal"}, headers=h)
        assert _veh_clear(cl, h, vid) is False                      # 일반세척은 인정 안 됨
        c2 = cl.post(f"/vehicles/{vid}/cleanings",
                     json={"cleaned_at": "2026-09-15", "previous_cargo_halal": False,
                           "method": "sertu", "sertu_steps": 7, "next_due": "2026-12-15"},
                     headers=h).json()
        assert _veh_clear(cl, h, vid) is False                      # 서명 전
        assert cl.post(f"/cleanings/{c2['cleaning_id']}/sign", headers=h).status_code == 200
        assert _veh_clear(cl, h, vid) is True                       # Sertu + 서명 → clear
        v = [x for x in cl.get("/orgs/org_demo/vehicles", headers=h).json()
             if x["vehicle_id"] == vid][0]
        assert v["cleaning_count"] == 2 and v["next_due"] == "2026-12-15"
        assert v["sertu"] is True                                    # 캐시 역동기화


def _seed_penyelia(username, expiry):
    """penyelia_halal 계정에 PenyeliaHalal 자격행을 붙인다(공유 DB — 기존 행 제거 후 1건)."""
    from app import models
    db = next(m.get_db())
    u = db.query(models.User).filter_by(username=username).first()
    db.query(models.PenyeliaHalal).filter_by(user_id=u.user_id).delete()
    db.add(models.PenyeliaHalal(org_id="org_demo", user_id=u.user_id, name=username,
                                status="active", cert_expiry=expiry))
    db.commit()


def test_작성자는_서명할수없다_4eyes():
    """penyelia 가 자기 작성 로그에 서명 → 403(작성자≠서명자)."""
    from datetime import date, timedelta
    with TestClient(app) as cl:
        _seed_penyelia("penyelia1", date.today() + timedelta(days=365))
        ho = _tok(cl, "operator1", "pw")                                # 차량 생성=오퍼레이터
        vid = _mk_vehicle(cl, ho, plate_no="B 2 CLEAN", previous_cargo_halal=False)
        hp = _tok(cl, "penyelia1", "pw")
        c = cl.post(f"/vehicles/{vid}/cleanings",                       # 로그 작성=penyelia1
                    json={"method": "sertu", "previous_cargo_halal": False}, headers=hp).json()
        r = cl.post(f"/cleanings/{c['cleaning_id']}/sign", headers=hp)
        assert r.status_code == 403 and r.json()["detail"]["code"] == "SELF_SIGN_FORBIDDEN"


def test_수료증_만료_페냘리아는_서명거부():
    """만료 수료증 페냘리아 서명 → 403(자격 없음)."""
    from datetime import date, timedelta
    with TestClient(app) as cl:
        _seed_penyelia("penyelia1", date.today() - timedelta(days=1))   # 어제 만료
        ho = _tok(cl, "operator1", "pw")                                # 작성=오퍼레이터
        vid = _mk_vehicle(cl, ho, plate_no="B 3 CLEAN", previous_cargo_halal=False)
        c = cl.post(f"/vehicles/{vid}/cleanings",
                    json={"method": "sertu", "previous_cargo_halal": False}, headers=ho).json()
        hp = _tok(cl, "penyelia1", "pw")
        r = cl.post(f"/cleanings/{c['cleaning_id']}/sign", headers=hp)
        assert r.status_code == 403 and r.json()["detail"]["code"] == "NO_SIGN_AUTHORITY"


def test_유효_페냘리아_서명은_통과하고_clear():
    """작성=오퍼레이터, 서명=유효 페냘리아 → 200 + halal_clear True."""
    from datetime import date, timedelta
    with TestClient(app) as cl:
        _seed_penyelia("penyelia1", date.today() + timedelta(days=200))
        ho = _tok(cl, "operator1", "pw")
        vid = _mk_vehicle(cl, ho, plate_no="B 4 CLEAN", previous_cargo_halal=False)
        c = cl.post(f"/vehicles/{vid}/cleanings",
                    json={"method": "sertu", "previous_cargo_halal": False}, headers=ho).json()
        hp = _tok(cl, "penyelia1", "pw")
        r = cl.post(f"/cleanings/{c['cleaning_id']}/sign", headers=hp)
        assert r.status_code == 200 and r.json()["penyelia_sign"] is True
        assert _veh_clear(cl, ho, vid) is True
