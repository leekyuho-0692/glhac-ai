"""할랄매뉴얼 빌더 — 섹션 순서·이미지 삽입 레이아웃 저장/조회(§12-5).
격리 임시DB(고유명), 서버 불필요. 실행: <venv>/bin/python tests/test_manual_builder_layout.py
스키마 무변경 — WorkflowEvent(action="sjph_manual.layout", latest-wins) 검증.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ["GLHAC_DB_URL"] = "sqlite:///./glhac_manualbuilder_test.db"

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

import app.main as m  # noqa: E402
import app.models as models  # noqa: E402

USER = {"uid": "uMB", "role": "applicant", "org_id": "orgMB"}


def _seed(tmp_path):
    eng = create_engine("sqlite:///%s" % (tmp_path / "mb.db"))
    models.Base.metadata.create_all(bind=eng)
    db = sessionmaker(bind=eng)()
    db.add(models.Org(org_id="orgMB", name="MB Co"))
    db.add(models.CaseApplication(case_id="caseMB", org_id="orgMB", company_name="MB Foods"))
    db.commit()
    return db


def test_default_layout_when_empty(tmp_path):
    """저장 없음 → 기본 4섹션·순서 유지·기본콘텐츠 섹션만 완료(3/4)·미준비."""
    db = _seed(tmp_path)
    r = m.get_sjph_manual_layout("caseMB", USER, db)
    assert [s["key"] for s in r["sections"]] == [k for k, *_ in m.SJPH_MANUAL_SECTIONS]
    assert r["total"] == 4 and r["done"] == 3 and r["ready"] is False
    # org_chart(has_default=False) 만 미완료
    incomplete = [s["key"] for s in r["sections"] if not s["complete"]]
    assert incomplete == ["org_chart"]


def test_reorder_persisted_latest_wins(tmp_path):
    """순서 변경 저장 후 조회 시 반영(latest-wins). 미지의 키는 무시·누락 키는 보정."""
    db = _seed(tmp_path)
    new_order = ["material_process", "org_chart", "halal_declaration", "halal_policy", "GHOST"]
    m.set_sjph_manual_layout("caseMB", {"order": new_order, "inserts": {}}, USER, db)
    r = m.get_sjph_manual_layout("caseMB", USER, db)
    assert [s["key"] for s in r["sections"]] == ["material_process", "org_chart", "halal_declaration", "halal_policy"]
    # 재저장 → 최신이 이김
    m.set_sjph_manual_layout("caseMB", {"order": ["halal_policy"], "inserts": {}}, USER, db)
    r2 = m.get_sjph_manual_layout("caseMB", USER, db)
    assert r2["sections"][0]["key"] == "halal_policy"


def test_image_insert_completes_section_and_ready(tmp_path):
    """org_chart 이미지 삽입 → 해당 섹션 완료·전체 준비완료(4/4). 잘못된 insert는 무시."""
    db = _seed(tmp_path)
    payload = {"order": [], "inserts": {
        "org_chart": {"document_id": "docXYZ", "filename": "orgchart.png"},
        "unknown": {"document_id": "d2"},          # 미지의 키 → 무시
        "halal_policy": {"filename": "no-id"},      # document_id 없음 → 무시
    }}
    m.set_sjph_manual_layout("caseMB", payload, USER, db)
    r = m.get_sjph_manual_layout("caseMB", USER, db)
    org = [s for s in r["sections"] if s["key"] == "org_chart"][0]
    assert org["image"]["document_id"] == "docXYZ" and org["image"]["filename"] == "orgchart.png"
    assert org["complete"] is True
    assert "halal_policy" not in r["inserts"] and "unknown" not in r["inserts"]
    assert r["done"] == 4 and r["ready"] is True
    # WorkflowEvent(latest-wins)로만 저장 — 신규 테이블/컬럼 없음
    evs = db.query(models.WorkflowEvent).filter_by(case_id="caseMB", action="sjph_manual.layout").all()
    assert len(evs) == 1 and evs[0].payload["inserts"]["org_chart"]["document_id"] == "docXYZ"


if __name__ == "__main__":
    import tempfile
    from pathlib import Path
    fails = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            with tempfile.TemporaryDirectory() as d:
                try:
                    fn(Path(d))
                    print("PASS", name)
                except Exception as e:
                    fails += 1
                    import traceback
                    traceback.print_exc()
                    print("FAIL", name, "->", repr(e))
    print("=" * 40)
    print("ALL PASS" if fails == 0 else "%d FAILED" % fails)
    sys.exit(1 if fails else 0)
