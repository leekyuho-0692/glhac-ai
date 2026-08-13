"""공장은 회사 자산이다 — 같은 org에 여러 회사가 있어도 섞이면 안 된다.

배경: 모델은 org=회사(1:N 공장)를 전제하지만 실제 데이터는 org 하나에 여러 회사가
들어있다. 케이스 생성이 org 공장을 전부 상속하는 탓에, 인도네시아 케이터링 신청서에
한국 공장 두 곳이 붙어 있었다(CV. CITRA PRATAMA 실측).

번호만으로 회사를 가르면 부족했다. 새 신청이 org 프로필을 상속하면서 서로 다른 회사
셋이 같은 사업자번호를 달고 있었다(바이오로제트·질경이·천우건설). 그래서 상호와 번호를
함께 본다.

실행: <venv>/bin/python -m pytest tests/test_facility_scope.py -q
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("GLHAC_DB_URL", "sqlite:///./glhac_facscope_test.db")

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

import app.main as m  # noqa: E402
import app.models as models  # noqa: E402

ORG = "org_shared"


def _db(tmp_path):
    eng = create_engine("sqlite:///%s" % (tmp_path / "facscope.db"))
    models.Base.metadata.create_all(bind=eng)
    db = sessionmaker(bind=eng)()
    for fid, nm in [("f_kr1", "바이오로제트"), ("f_kr2", "동양케미칼"), ("f_id", "CV. CITRA")]:
        db.add(models.Facility(facility_id=fid, org_id=ORG, name=nm))
    # 같은 org · 같은 사업자번호(상속 사고) · 서로 다른 회사
    db.add(models.CaseApplication(case_id="c_bio", org_id=ORG, company_name="바이오로제트",
                                  nib="207-81-34847", facility_ids=["f_kr1"]))
    db.add(models.CaseApplication(case_id="c_cw", org_id=ORG, company_name="천우건설",
                                  nib="207-81-34847", facility_ids=["f_kr2"]))
    db.add(models.CaseApplication(case_id="c_id", org_id=ORG, company_name="CV. CITRA PRATAMA",
                                  nib="0912220034808", facility_ids=["f_id"]))
    db.commit()
    return db


def _names(db, case_id):
    c = db.get(models.CaseApplication, case_id)
    return sorted(f.name for f in m._facilities_for_case(db, c))


def test_each_company_sees_only_its_own_plant(tmp_path):
    """인도네시아 신청서에 한국 공장이 붙던 사고 — 회사별로 갈린다."""
    db = _db(tmp_path)
    assert _names(db, "c_id") == ["CV. CITRA"]
    assert _names(db, "c_bio") == ["바이오로제트"]
    assert _names(db, "c_cw") == ["동양케미칼"]


def test_same_nib_different_company_does_not_share(tmp_path):
    """번호가 같아도 상호가 다르면 남이다 — org 프로필 상속으로 번호가 겹칠 수 있다."""
    db = _db(tmp_path)
    assert "동양케미칼" not in _names(db, "c_bio")
    assert "바이오로제트" not in _names(db, "c_cw")


def test_same_company_next_application_inherits_plants(tmp_path):
    """같은 회사의 다음 신청은 공장을 그대로 물려받는다 — 회사 자산 상속은 살아 있어야 한다."""
    db = _db(tmp_path)
    db.add(models.CaseApplication(case_id="c_bio2", org_id=ORG, company_name="바이오로제트",
                                  nib="207-81-34847", facility_ids=None))
    db.commit()
    assert _names(db, "c_bio2") == ["바이오로제트"]


def test_unclaimed_plant_stays_available(tmp_path):
    """아무도 안 쓰는 공장은 계속 고를 수 있다 — 새 공장을 등록한 직후가 그 상태다."""
    db = _db(tmp_path)
    db.add(models.Facility(facility_id="f_free", org_id=ORG, name="미배정공장"))
    db.commit()
    for cid in ("c_id", "c_bio", "c_cw"):
        assert "미배정공장" in _names(db, cid), cid


def test_already_linked_plant_never_disappears(tmp_path):
    """이미 연결된 공장은 어떤 이유로도 목록에서 사라지지 않는다 — 사라지면 신청서가 빈다."""
    db = _db(tmp_path)
    c = db.get(models.CaseApplication, "c_id")
    c.facility_ids = ["f_id", "f_kr1"]          # 과거에 잘못 붙은 것이라도
    db.commit()
    assert "바이오로제트" in _names(db, "c_id"), "연결된 공장이 목록에서 빠지면 해제할 수도 없다"


def test_company_key_separates_by_name_and_number(tmp_path):
    """식별자는 (번호, 상호) 쌍 — 한쪽만 같으면 다른 회사로 본다."""
    db = _db(tmp_path)
    a, b, c = (db.get(models.CaseApplication, x) for x in ("c_bio", "c_cw", "c_id"))
    assert m._company_key(a) != m._company_key(b)
    assert m._company_key(a) != m._company_key(c)
    assert m._company_key(a) == m._company_key(a)
