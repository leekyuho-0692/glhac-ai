"""매뉴얼 도면 배치 — 자동 추정은 초안이고, 사람이 지정한 값이 이긴다.

배경: 어느 문서를 어느 부록에 넣을지 AI가 파일명·유형으로 추측해 조용히 넣었다.
틀려도 화면에 드러나지 않아 아무도 모른다(할랄 방침문과 교육자료가 서로 자리를 바꿔
들어가 있었다). 배치를 내보이고 사람이 고칠 수 있어야 한다.

실행: <venv>/bin/python -m pytest tests/test_manual_placement.py -q
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("GLHAC_DB_URL", "sqlite:///./glhac_place_test.db")

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

import app.main as m  # noqa: E402
import app.models as models  # noqa: E402

CID = "c_place"


def _db(tmp_path):
    eng = create_engine("sqlite:///%s" % (tmp_path / "place.db"))
    models.Base.metadata.create_all(bind=eng)
    db = sessionmaker(bind=eng)()
    db.add(models.CaseApplication(case_id=CID, org_id="org1", company_name="배치 검증"))
    db.add(models.DocumentAsset(document_id="d_flow", case_id=CID,
                                filename="Diagram alir proses produksi.PNG",
                                doc_type="process_flow", content_type="image/png"))
    db.add(models.DocumentAsset(document_id="d_layout", case_id=CID,
                                filename="Denah ruang produksi.pdf",
                                doc_type="other", content_type="application/pdf"))
    db.add(models.DocumentAsset(document_id="d_policy", case_id=CID,
                                filename="Halal_Policy.pdf", doc_type="sjph_manual",
                                content_type="application/pdf"))
    db.add(models.SjphEvidence(case_id=CID, item_key="facility_layout",
                               document_id="d_layout", filename="Denah ruang produksi.pdf"))
    db.commit()
    return db


def _pick(db, slot):
    d, why = m._sjph_pick_for_slot(db, CID, slot)
    return (d.document_id if d else None), why


def test_auto_picks_are_explainable(tmp_path):
    """자동 선택에는 근거가 붙는다 — 근거 없는 배치는 검증할 수 없다."""
    db = _db(tmp_path)
    for slot, doc in (("material_process", "d_flow"), ("facility_layout", "d_layout"),
                      ("halal_policy", "d_policy")):
        got, why = _pick(db, slot)
        assert got == doc, slot
        assert why, slot


def test_policy_and_training_do_not_swap(tmp_path):
    """할랄 방침문과 교육자료는 이름이 비슷해 서로 자리를 바꿔 들어갔다(실측)."""
    db = _db(tmp_path)
    db.add(models.DocumentAsset(document_id="d_poster", case_id=CID,
                                filename="Halal_Posters.pdf", doc_type="other",
                                content_type="application/pdf"))
    db.add(models.SjphEvidence(case_id=CID, item_key="training",
                               document_id="d_poster", filename="Halal_Posters.pdf"))
    db.commit()
    assert _pick(db, "halal_policy")[0] == "d_policy"
    assert _pick(db, "halal_training")[0] == "d_poster"


def _set(db, slots):
    c = db.get(models.CaseApplication, CID)
    db.add(models.WorkflowEvent(case_id=CID, action=m.SJPH_PLACEMENT_ACTION,
                                payload={"slots": slots}, row_hash="x"))
    db.commit()
    return c


def test_manual_choice_wins_over_auto(tmp_path):
    """사람이 지정하면 자동 추정을 덮는다."""
    db = _db(tmp_path)
    _set(db, {"halal_policy": "d_flow"})
    assert m._sjph_placement_override(db, CID)["halal_policy"] == "d_flow"


def test_empty_choice_is_respected(tmp_path):
    """'비움'으로 지정하면 자동으로 되돌아가지 않는다 — 지운 걸 다시 채우면 지정이 무의미하다."""
    db = _db(tmp_path)
    _set(db, {"material_process": None})
    ov = m._sjph_placement_override(db, CID)
    assert "material_process" in ov and ov["material_process"] is None


def test_latest_choice_wins(tmp_path):
    """배치는 latest-wins — 마지막에 정한 사람의 뜻이 남는다."""
    db = _db(tmp_path)
    _set(db, {"halal_policy": "d_flow"})
    _set(db, {"halal_policy": "d_layout"})
    assert m._sjph_placement_override(db, CID)["halal_policy"] == "d_layout"


def test_unknown_slots_are_dropped(tmp_path):
    """모르는 슬롯은 저장돼도 무시한다 — 계약에 없는 키가 배치를 흔들면 안 된다."""
    db = _db(tmp_path)
    _set(db, {"nonexistent_slot": "d_flow"})
    assert m._sjph_placement_override(db, CID) == {}


def test_every_slot_has_three_language_labels():
    """배치 화면은 3개 언어로 나간다 — 라벨이 비면 슬롯이 정체불명으로 보인다."""
    for slot in m._SJPH_DOC_FALLBACK:
        labels = m.SJPH_SLOT_LABELS.get(slot)
        assert labels and len(labels) == 3 and all(x.strip() for x in labels), slot
