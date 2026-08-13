"""올린 문서는 제자리를 찾아가야 한다 — SJPH 증빙 자동 편철.

배경: Demo test files(CV. CITRA PRATAMA) 16건 전수 확인에서, 인니 실무 기록물
6건(Catatan…·Denah…)이 doc_type=other로만 남고 어디에도 안 걸렸다. SJPH 증빙 항목에
제자리가 있는데도 채우는 경로가 없어서 증빙 완성도가 0/10이었다.

이 테스트가 지키는 성질
  · 파일명이 세 언어 중 무엇이든 증빙 항목을 찾는다.
  · 사람이 이미 넣은 증빙을 자동 판단이 덮지 않는다.
  · 여러 번 돌려도 결과가 같다(빈 항목만 채운다).

실행: <venv>/bin/python -m pytest tests/test_evidence_autofile.py -q
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("GLHAC_DB_URL", "sqlite:///./glhac_autofile_test.db")

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

import app.main as m  # noqa: E402
import app.models as models  # noqa: E402

CASE = "case_af"
# 실제 제출 파일명 그대로 — 규칙이 아니라 현물에 맞춰야 한다
FILES = [
    ("Catatan pembelian barang.PNG", "image/png", "purchase_log"),
    ("Catatan pemeriksaan barang.PNG", "image/png", "receiving_log"),
    ("Catatan penyimpanan barang.PNG", "image/png", "usage_log"),
    ("Catatan hasil produksi.PNG", "image/png", "production_log"),
    ("Catatan penjualan.PNG", "image/png", "distribution_log"),
    ("Denah ruang produksi.pdf", "application/pdf", "facility_layout"),
    ("Diagram alir proses produksi.PNG", "image/png", "production_flow"),
    ("Halal_Posters.pdf", "application/pdf", "training"),
    ("Internal_Audit.pdf", "application/pdf", "internal_audit"),
]


def _db(tmp_path):
    eng = create_engine("sqlite:///%s" % (tmp_path / "autofile.db"))
    models.Base.metadata.create_all(bind=eng)
    db = sessionmaker(bind=eng)()
    db.add(models.CaseApplication(case_id=CASE, org_id="org1", company_name="CV. CITRA PRATAMA"))
    for i, (fn, ct, _) in enumerate(FILES):
        db.add(models.DocumentAsset(document_id="d%d" % i, case_id=CASE, filename=fn,
                                    doc_type="other", content_type=ct))
    db.commit()
    return db


def _fill(db):
    out = {}
    for d in db.query(models.DocumentAsset).filter_by(case_id=CASE).all():
        k = m._autofile_evidence(db, CASE, d)
        if k:
            out[d.filename] = k
    db.commit()
    return out


def test_indonesian_records_find_their_slot(tmp_path):
    """인니어 파일명 그대로 증빙 항목에 들어간다 — 이 6건이 통째로 붕 떠 있었다."""
    db = _db(tmp_path)
    got = _fill(db)
    for fn, _, key in FILES:
        assert got.get(fn) == key, "%s → %s (기대 %s)" % (fn, got.get(fn), key)


def test_autofile_is_idempotent(tmp_path):
    """두 번 돌려도 같은 결과 — 빈 항목만 채운다."""
    db = _db(tmp_path)
    first = _fill(db)
    assert _fill(db) == {}, "두 번째 실행이 또 채웠다"
    assert db.query(models.SjphEvidence).filter_by(case_id=CASE).count() == len(first)


def test_human_choice_is_never_overwritten(tmp_path):
    """사람이 고른 증빙을 자동 판단이 덮으면 안 된다."""
    db = _db(tmp_path)
    db.add(models.SjphEvidence(case_id=CASE, item_key="purchase_log",
                               filename="사람이 고른 구매기록.pdf", document_id="manual"))
    db.commit()
    _fill(db)
    row = db.query(models.SjphEvidence).filter_by(case_id=CASE, item_key="purchase_log").first()
    assert row.document_id == "manual", "자동 편철이 사람 선택을 덮었다"


def test_unknown_document_is_left_alone(tmp_path):
    """모르는 문서는 아무 항목에도 밀어넣지 않는다 — 틀린 자리보다 빈자리가 낫다."""
    db = _db(tmp_path)
    db.add(models.DocumentAsset(document_id="dx", case_id=CASE, doc_type="other",
                                filename="무슨 서류인지 모를 파일.pdf", content_type="application/pdf"))
    db.commit()
    assert "무슨 서류인지 모를 파일.pdf" not in _fill(db)


def test_image_is_not_parsed_for_body_text(tmp_path):
    """이미지는 본문을 읽지 않는다 — OCR 비용이 편철 하나 값보다 크다."""
    db = _db(tmp_path)
    d = models.DocumentAsset(document_id="dimg", case_id=CASE, doc_type="other",
                             filename="이름으로는 모를 사진.PNG", content_type="image/png",
                             content_b64="x" * 100)
    db.add(d)
    db.commit()
    assert m._evidence_key_for_doc(d) == (None, None)


def test_slot_keys_exist_in_the_evidence_list(tmp_path):
    """사전이 내놓는 증빙 키가 실제 항목 목록에 있어야 한다 — 없으면 조용히 버려진다."""
    db = _db(tmp_path)
    valid = {k for k, _ in m.SJPH_EVIDENCE_ITEMS}
    for key in _fill(db).values():
        assert key in valid, key
