"""원재료 화면이 심사에 필요한 것을 보여주는가 — 제조사·유형·색·인증번호·증빙.

배경(현장 지적): 원재료 판정 자체는 정확한데 화면이 판단 근거를 감췄다.
  · 제조사·유형이 DB에 있는데도 표에 없었다 — 오디터가 서류를 다시 열어야 했다.
  · 위험도를 상태와 같은 색으로 칠해, 인증서로 할랄이 된 고위험 성분이 초록으로 보였다.
  · 증빙은 개수만 보이고 무엇을 냈는지 열어볼 수 없었다.
  · 'Air Pam'(수돗물)이 온톨로지에 안 걸려 의심으로 남았다.

실행: <venv>/bin/python -m pytest tests/test_material_detail.py -q
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("GLHAC_DB_URL", "sqlite:///./glhac_matdetail_test.db")

from fastapi.testclient import TestClient   # noqa: E402

import app.main as m                        # noqa: E402
from app import domain_dict as dd           # noqa: E402
from app import screening                   # noqa: E402
from app.main import app                    # noqa: E402


def _tok(c, u="applicant1", p="pw"):
    r = c.post("/auth/login", json={"username": u, "password": p})
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["token"]}


def _case(c, h):
    return c.post("/cases", json={"company_name": "PT Bahan Uji"}, headers=h).json()["case_id"]


# ── 6) 수돗물은 할랄이다 ──────────────────────────────────────────────────
def test_인니어_수돗물_표기가_물로_매칭된다():
    """'Air PAM'은 수도사업자(PAM/PDAM) 물 — 흔한 표기인데 사전에 없어 의심으로 빠졌다."""
    for nm in ("Air Pam", "air pdam", "Air Keran", "air ledeng", "tap water"):
        r = screening.screen_merged(nm)
        assert r["matched_uid"] == "ing.water", (nm, r["matched_uid"])
        assert r["status"] == "halal", (nm, r["status"])


def test_물이_아닌_air는_물로_보지_않는다():
    """'air kelapa'(코코넛수)·'air jeruk'(라임즙)까지 물로 넘기면 판정이 무너진다."""
    for nm in ("air kelapa", "air jeruk nipis"):
        r = screening.screen_merged(nm)
        assert r["matched_uid"] != "ing.water", (nm, r["matched_uid"])


# ── 4) 인증번호 → 할랄 (위험도는 그대로) ─────────────────────────────────
def test_인증번호가_있으면_의심이_할랄로_올라간다():
    base = screening.screen_merged("Bahan Tidak Dikenal")
    assert base["status"] == "mushbooh"
    got = screening.screen_merged("Bahan Tidak Dikenal", cert_no="ID00410000008400120")
    assert got["status"] == "halal"
    assert got["cert_promoted"] is True
    assert got["decision_by"] == "cert_no"


def test_인증번호로_올려도_위험도는_바뀌지_않는다():
    """위험도는 '그 성분이 원래 얼마나 위험한가'다. 번호 하나로 지우면 오디터가 볼 것이 없어진다."""
    a = screening.screen_merged("gelatin")
    b = screening.screen_merged("gelatin", cert_no="ID001")
    assert a["severity"] == b["severity"] == "high"
    assert b["status"] == "halal" and a["status"] == "mushbooh"


def test_하람은_인증번호로도_올라가지_않는다():
    """하람은 인증서 문제가 아니다 — 번호가 있어도 통과시키면 안 된다."""
    r = screening.screen_merged("lard", cert_no="ID999")
    assert r["status"] == "haram"
    assert r["cert_promoted"] is False


def test_빈_인증번호는_승격시키지_않는다():
    for cn in ("", "   ", None):
        r = screening.screen_merged("Bahan Tidak Dikenal", cert_no=cn)
        assert r["status"] == "mushbooh", cn


# ── 2) 원재료 유형 7종 ───────────────────────────────────────────────────
def test_서류_표기가_표준_유형으로_정규화된다():
    """서류에는 'BAHAN BAKU'·'CLEANING AGENT'처럼 제각각 적힌다."""
    cases = {"BAHAN BAKU": "raw", "Bahan Tambahan": "additive",
             "bahan penolong": "processing_aid", "Bahan Pengawet": "preservative",
             "CLEANING AGENT": "cleaning", "Pelumas": "lubricant", "KEMASAN": "packaging"}
    for raw, code in cases.items():
        assert m._mat_type_code(raw) == code, (raw, m._mat_type_code(raw))


def test_옛_코드_sanitizer는_세척제로_읽는다():
    """종전 ENUM의 'sanitizer'와 사전의 'cleaning'은 같은 것 — 옛 데이터가 사라지면 안 된다."""
    assert m._mat_type_code("sanitizer") == "cleaning"


def test_모르는_유형은_지어내지_않고_서류값을_보여준다():
    assert m._mat_type_code("알 수 없는 무엇") is None
    assert m._mat_type_label("알 수 없는 무엇") == "알 수 없는 무엇"   # 정보를 버리지 않는다


def test_유형_라벨은_3개국어로_나온다():
    for lang, want in (("ko", "보존제"), ("en", "Preservative"), ("id", "Bahan Pengawet")):
        assert m._mat_type_label("Bahan Pengawet", lang) == want


def test_유형_7종이_전부_ENUM에_있다():
    codes = [v for v, _ in m._ENUMS["material_type"]]
    assert codes == ["raw", "additive", "processing_aid", "preservative",
                     "cleaning", "lubricant", "packaging"]


def test_유형_라벨은_사전에서만_온다():
    """표를 두 벌 두면 한쪽만 고쳐진다 — ENUM 라벨과 사전 라벨이 같아야 한다."""
    for code, label in m._ENUMS["material_type"]:
        keys = [k for k in dd.by_axis("MATERIAL")
                if (dd.actions(k) or {}).get("material_type") == code]
        assert keys, code
        assert dd.label(keys[0], "ko") == label, code


# ── 1)·5) 목록 API가 제조사·증빙을 내보내는가 ───────────────────────────
def test_목록에_제조사와_유형이_실린다():
    with TestClient(app) as c:
        h = _tok(c)
        cid = _case(c, h)
        c.post(f"/cases/{cid}/materials",
               json={"name": "Gula Pasir", "mat_type": "BAHAN BAKU",
                     "manufacturer": "PT Gula Nusantara"}, headers=h)
        row = c.get(f"/cases/{cid}/materials", headers=h).json()[0]
        assert row["manufacturer"] == "PT Gula Nusantara"
        assert row["manufacturer_is_supplier"] is False
        assert row["mat_type_code"] == "raw"
        assert row["mat_type_label"] == "원료"


def test_제조사가_비면_공급사를_보여주고_그렇다고_알린다():
    """서류에 한 칸만 있는 경우가 흔하다. 빈칸으로 두면 '정보 없음'으로 읽힌다."""
    with TestClient(app) as c:
        h = _tok(c)
        cid = _case(c, h)
        c.post(f"/cases/{cid}/materials",
               json={"name": "Garam", "supplier": "PT Pemasok"}, headers=h)
        row = c.get(f"/cases/{cid}/materials", headers=h).json()[0]
        assert row["manufacturer"] == "PT Pemasok"
        assert row["manufacturer_is_supplier"] is True   # 화면이 구분해 표시할 수 있게


def test_목록이_증빙_문서를_개수뿐_아니라_목록으로_준다():
    with TestClient(app) as c:
        h = _tok(c)
        cid = _case(c, h)
        mid = c.post(f"/cases/{cid}/materials", json={"name": "Lesitin"},
                     headers=h).json()["material_id"]
        row = c.get(f"/cases/{cid}/materials", headers=h).json()[0]
        assert row["evidence_count"] == 0 and row["evidence"] == []
        c.post(f"/cases/{cid}/materials/{mid}/evidence",
               json={"evidence_type": "halal_certificate", "filename": "cert.pdf",
                     "file_b64": "aGVsbG8="}, headers=h)
        row = c.get(f"/cases/{cid}/materials", headers=h).json()[0]
        assert row["evidence_count"] == 1
        assert row["evidence"][0]["filename"] == "cert.pdf"
        assert row["evidence"][0]["document_id"]      # 화면이 이걸로 문서를 연다


# ── 4) 수정 통로 ─────────────────────────────────────────────────────────
def test_인증번호를_나중에_넣어도_판정이_갱신된다():
    with TestClient(app) as c:
        h = _tok(c)
        cid = _case(c, h)
        mid = c.post(f"/cases/{cid}/materials", json={"name": "Bahan Tidak Dikenal"},
                     headers=h).json()["material_id"]
        assert c.get(f"/cases/{cid}/materials", headers=h).json()[0]["status"] == "mushbooh"
        r = c.patch(f"/materials/{mid}", json={"cert_no": "ID00410000001"}, headers=h)
        assert r.status_code == 200, r.text
        assert r.json()["screen"]["cert_promoted"] is True
        row = c.get(f"/cases/{cid}/materials", headers=h).json()[0]
        assert row["status"] == "halal" and row["cert_no"] == "ID00410000001"


def test_인증번호로_올린_사실이_감사기록에_남는다():
    """번호만 적힌 것과 증빙으로 해소된 것은 다르다 — 오디터가 되짚을 수 있어야 한다."""
    import app.models as models
    with TestClient(app) as c:
        h = _tok(c)
        cid = _case(c, h)
        mid = c.post(f"/cases/{cid}/materials", json={"name": "Bahan Tidak Dikenal 2"},
                     headers=h).json()["material_id"]
        c.patch(f"/materials/{mid}", json={"cert_no": "ID77"}, headers=h)
        db = m.SessionLocal()
        try:
            logs = [a for a in db.query(models.AuditLog).all() if a.action == "material.update"]
            assert logs, "감사기록 없음"
            det = logs[-1].meta or {}
            assert det.get("cert_promoted") is True
            assert det.get("before", {}).get("status") == "mushbooh"
            assert det.get("after", {}).get("status") == "halal"
        finally:
            db.close()


def test_제조사만_고치면_판정은_그대로다():
    with TestClient(app) as c:
        h = _tok(c)
        cid = _case(c, h)
        mid = c.post(f"/cases/{cid}/materials", json={"name": "gelatin"},
                     headers=h).json()["material_id"]
        before = c.get(f"/cases/{cid}/materials", headers=h).json()[0]
        c.patch(f"/materials/{mid}", json={"manufacturer": "PT Apa Saja"}, headers=h)
        after = c.get(f"/cases/{cid}/materials", headers=h).json()[0]
        assert after["manufacturer"] == "PT Apa Saja"
        assert (after["status"], after["severity"]) == (before["status"], before["severity"])


def test_남의_케이스_원재료는_수정할_수_없다():
    with TestClient(app) as c:
        h = _tok(c)
        cid = _case(c, h)
        mid = c.post(f"/cases/{cid}/materials", json={"name": "Gula"},
                     headers=h).json()["material_id"]
        other = _tok(c, "auditor1", "pw")     # 배정도 없는 오디터
        r = c.patch(f"/materials/{mid}", json={"cert_no": "X"}, headers=other)
        assert r.status_code in (401, 403), r.status_code


# ── 증빙 미리보기·다운로드 (오디터 포함) ────────────────────────────────
def test_오디터도_증빙_파일을_받을_수_있다():
    """오디터는 원재료를 못 고치지만(읽기전용) 증빙은 봐야 판단한다."""
    with TestClient(app) as c:
        h = _tok(c)
        cid = _case(c, h)
        mid = c.post(f"/cases/{cid}/materials", json={"name": "Pewarna"},
                     headers=h).json()["material_id"]
        c.post(f"/cases/{cid}/materials/{mid}/evidence",
               json={"evidence_type": "halal_certificate", "filename": "c.pdf",
                     "file_b64": "aGVsbG8="}, headers=h)
        did = c.get(f"/cases/{cid}/materials", headers=h).json()[0]["evidence"][0]["document_id"]
        aud = _tok(c, "auditor1", "pw")
        assert c.get(f"/cases/{cid}/materials", headers=aud).status_code == 200
        r = c.get(f"/documents/{did}/file", headers=aud)
        assert r.status_code == 200, r.status_code
        assert r.content == b"hello"          # 미리보기·다운로드가 같은 이 파일을 쓴다


def test_파일이_없는_증빙은_그렇다고_알린다():
    """행은 있는데 파일이 없으면 버튼을 내주면 안 된다 — 눌러도 안 열린다."""
    import app.models as models
    with TestClient(app) as c:
        h = _tok(c)
        cid = _case(c, h)
        mid = c.post(f"/cases/{cid}/materials", json={"name": "Perisa"},
                     headers=h).json()["material_id"]
        db = m.SessionLocal()
        try:
            db.add(models.DocumentAsset(case_id=cid, material_id=mid, filename="empty.pdf",
                                        doc_type="halal_certificate", content_b64=None))
            db.commit()
        finally:
            db.close()
        row = c.get(f"/cases/{cid}/materials", headers=h).json()[0]
        assert row["evidence"][0]["has_file"] is False
