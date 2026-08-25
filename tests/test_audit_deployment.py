"""심사원 배정 양식 — 시스템이 채우되, 모르는 칸은 비워 둔다.

배경: 심사원 파견은 서명해서 LPH에 내는 문서다. 그런데 시스템이 값을 지어내면
(제품 유형이 없다고 제품명을 대신 넣는 식으로) 틀려도 아무도 모른 채 서명되고 확정된다.
그래서 이 양식의 규칙은 하나다 — 있는 값만 채우고, 없으면 빈칸으로 내보내고,
어디가 빈칸인지 화면에 알린다.

실행: <venv>/bin/python -m pytest tests/test_audit_deployment.py -q
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("GLHAC_DB_URL", "sqlite:///./glhac_deploy_test.db")

from sqlalchemy import create_engine          # noqa: E402
from sqlalchemy.orm import sessionmaker       # noqa: E402

import app.main as m                          # noqa: E402
import app.models as models                   # noqa: E402

CID = "c_deploy"


def _db(tmp_path, **case_kw):
    eng = create_engine("sqlite:///%s" % (tmp_path / "deploy.db"))
    models.Base.metadata.create_all(bind=eng)
    db = sessionmaker(bind=eng)()
    kw = dict(case_id=CID, org_id="org1", company_name="CV. UJI COBA",
              nib="1234567890", address="Jl. Mawar No. 1, Jakarta")
    kw.update(case_kw)
    db.add(models.CaseApplication(**kw))
    db.commit()
    return db


def _case(db):
    return db.get(models.CaseApplication, CID)


# ── 기본 채움 ────────────────────────────────────────────────────────────
def test_기본정보는_케이스에서_채워진다(tmp_path):
    db = _db(tmp_path)
    d = m._deploy_form_data(db, _case(db))
    assert d["id_no"] == "1234567890"
    assert d["company_name"] == "CV. UJI COBA"
    assert d["office_address"] == "Jl. Mawar No. 1, Jakarta"


def test_빈칸은_지어내지_않고_빈_문자열(tmp_path):
    db = _db(tmp_path)
    d = m._deploy_form_data(db, _case(db))
    for k in ("product_service", "trademark", "audit_date"):
        assert d[k] == "", k


def test_상표는_업체가_입력한_값을_쓴다(tmp_path):
    db = _db(tmp_path, profile_ext={"trademark": "  MAWAR  "})
    d = m._deploy_form_data(db, _case(db))
    assert d["trademark"] == "MAWAR"          # 앞뒤 공백은 정리


# ── 제품 유형: 개념이 다른 값으로 대신하지 않는다 ─────────────────────────
def test_제품유형이_없으면_제품명으로_대신하지_않는다(tmp_path):
    db = _db(tmp_path)
    db.add(models.Product(case_id=CID, name="Sambal Terasi Pedas", category=None))
    db.commit()
    d = m._deploy_form_data(db, _case(db))
    assert d["product_service"] == ""
    assert "Sambal" not in d["product_service"]


def test_제품_카테고리가_있으면_중복없이_모은다(tmp_path):
    db = _db(tmp_path)
    for nm, cat in (("A", "Bumbu"), ("B", "Bumbu"), ("C", "Minuman")):
        db.add(models.Product(case_id=CID, name=nm, category=cat))
    db.commit()
    d = m._deploy_form_data(db, _case(db))
    assert d["product_service"] == "Bumbu, Minuman"


def test_카테고리가_없으면_업체가_신고한_제품유형을_쓴다(tmp_path):
    db = _db(tmp_path, profile_ext={"product_type": "Seasoning"})
    d = m._deploy_form_data(db, _case(db))
    assert d["product_service"] == "Seasoning"


# ── 심사팀 소스 우선순위 ─────────────────────────────────────────────────
def test_심사원풀이_1순위_직책은_영문으로_변환(tmp_path):
    db = _db(tmp_path)
    db.add(models.AuditorPool(case_id=CID, name="Budi", role_in_team="ketua"))
    db.add(models.AuditorPool(case_id=CID, name="Sari", role_in_team="anggota"))
    db.commit()
    d = m._deploy_form_data(db, _case(db))
    assert [t["name"] for t in d["team"]] == ["Budi", "Sari"]
    assert [t["position"] for t in d["team"]] == ["Lead Auditor", "Auditor"]


def test_풀이_비면_심사계획의_심사원목록을_쓴다(tmp_path):
    db = _db(tmp_path)
    db.add(models.AuditPlan(case_id=CID, lph_name="LPH X",
                            auditors=["Budi", "Sari"]))
    db.commit()
    d = m._deploy_form_data(db, _case(db))
    assert [t["name"] for t in d["team"]] == ["Budi", "Sari"]
    # 계획에는 직책이 없다 — 없는 값을 만들지 않는다
    assert all(t["position"] == "" for t in d["team"])


def test_심사원풀이_심사계획보다_우선한다(tmp_path):
    db = _db(tmp_path)
    db.add(models.AuditorPool(case_id=CID, name="Budi", role_in_team="ketua"))
    db.add(models.AuditPlan(case_id=CID, auditors=["다른사람"]))
    db.commit()
    d = m._deploy_form_data(db, _case(db))
    assert [t["name"] for t in d["team"]] == ["Budi"]


# ── 심사일 ───────────────────────────────────────────────────────────────
def test_심사계획의_예정일이_심사일이_된다(tmp_path):
    import datetime
    db = _db(tmp_path)
    db.add(models.AuditPlan(case_id=CID, scheduled_date=datetime.date(2026, 9, 1)))
    db.commit()
    d = m._deploy_form_data(db, _case(db))
    assert d["audit_date"] == "2026-09-01"


# ── 블록 생성 ────────────────────────────────────────────────────────────
def test_양식은_정식_3칸을_유지한다(tmp_path):
    db = _db(tmp_path)
    db.add(models.AuditorPool(case_id=CID, name="Budi", role_in_team="ketua"))
    db.commit()
    blocks = m._deploy_form_blocks(m._deploy_form_data(db, _case(db)))
    tbl = [b for b in blocks if b["type"] == "table"][0]
    assert len(tbl["rows"]) == 3
    assert tbl["rows"][0][1] == "Budi"
    assert tbl["rows"][1] == ["2", "", ""]     # 손으로 채울 자리


def test_이해상충_고지가_양식에_들어간다(tmp_path):
    db = _db(tmp_path)
    blocks = m._deploy_form_blocks(m._deploy_form_data(db, _case(db)))
    paras = " ".join(b.get("text", "") for b in blocks if b["type"] == "para")
    assert "SJPH/HPAS" in paras
    assert "last two years" in paras


def test_서명란은_두_명_설정값으로_그린다(tmp_path):
    db = _db(tmp_path)
    blocks = m._deploy_form_blocks(m._deploy_form_data(db, _case(db)))
    sig = [b for b in blocks if b["type"] == "signature"][0]
    assert len(sig["slots"]) == len(m.DEPLOY_SIGNERS) == 2
    assert all(s["signed"] is False for s in sig["slots"])   # 서명은 사람이 한다


def test_제목은_한_번만_찍힌다(tmp_path):
    """머리말이 이미 제목을 그린다 — 블록에서 또 넣으면 두 번 보인다."""
    db = _db(tmp_path)
    blocks = m._deploy_form_blocks(m._deploy_form_data(db, _case(db)))
    assert not [b for b in blocks if b["type"] == "heading"]


def test_라벨은_번역하지_않는다(tmp_path):
    """LPH에 내는 영문 정식 양식 — 라벨이 언어에 따라 바뀌면 다른 문서가 된다."""
    db = _db(tmp_path)
    blocks = m._deploy_form_blocks(m._deploy_form_data(db, _case(db)))
    labels = [b["label"] for b in blocks if b["type"] == "kv"]
    assert "Trademark" in labels and "Company Name" in labels


def test_PDF가_실제로_만들어진다(tmp_path):
    db = _db(tmp_path)
    db.add(models.AuditorPool(case_id=CID, name="Budi", role_in_team="ketua"))
    db.commit()
    d = m._deploy_form_data(db, _case(db))
    pdf = m._render_pdf_rich("Audit Team Deployment Form", m._deploy_form_blocks(d),
                             subtitle=d["company_name"], footer="GL-HAC")
    assert pdf[:4] == b"%PDF" and len(pdf) > 2000
