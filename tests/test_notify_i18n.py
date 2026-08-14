"""알림은 읽는 사람의 언어로 — 저장은 한국어라 그대로 주면 인니어 화면에 한글이 뜬다.

배경: 알림 제목·본문을 한국어 문장으로 만들어 그대로 저장하고 그대로 보여줬다. 인니
심사자·업체가 보는 화면에 한국어가 떴다(실측: 인니어 화면 잔여 한글의 최대 덩어리 245건).

여기서 지키는 성질
  · 저장되는 한국어 문구는 그대로다 — SMS·WhatsApp 발송과 이미 쌓인 알림이 안 깨진다.
  · 사람이 쓴 본문(보완 사유·코멘트)은 번역하지 않는다. 남의 글을 기계가 바꾸면
    심사 기록이 원문과 달라진다.

실행: <venv>/bin/python -m pytest tests/test_notify_i18n.py -q
"""
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("GLHAC_DB_URL", "sqlite:///./glhac_notify_test.db")

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

import app.main as m  # noqa: E402
import app.models as models  # noqa: E402

HAN = re.compile(r"[가-힣]")


def _db(tmp_path):
    eng = create_engine("sqlite:///%s" % (tmp_path / "notify.db"))
    models.Base.metadata.create_all(bind=eng)
    return sessionmaker(bind=eng)()


def _case(db):
    c = models.CaseApplication(case_id="c1", org_id="org1", company_name="CV. CITRA PRATAMA")
    db.add(c)
    db.commit()
    return c


def test_stored_text_stays_korean(tmp_path):
    """저장은 한국어 그대로 — 발송 채널과 기존 알림이 이 값을 쓴다."""
    db = _db(tmp_path)
    n = m._notify(db, _case(db), "certificate_issued",
                  msg=("certificate_issued", {"company": "CV. CITRA", "cert_no": "HC-1"}))
    assert n.title == "인증서 발급"
    assert n.body == "CV. CITRA — 할랄 인증서 HC-1 발급 완료."


@pytest.mark.parametrize("lang", ["en", "id"])
def test_reader_language_has_no_korean(tmp_path, lang):
    """인니어·영어로 읽으면 한글이 남지 않는다."""
    db = _db(tmp_path)
    n = m._notify(db, _case(db), "certificate_issued",
                  msg=("certificate_issued", {"company": "CV. CITRA", "cert_no": "HC-1"}))
    title, body = m.notify_text(n, lang)
    assert not HAN.search(title), title
    assert not HAN.search(body), body
    assert "HC-1" in body and "CV. CITRA" in body, "업체명·인증번호는 번역하지 않는다"


def test_every_catalog_entry_covers_three_languages():
    """카탈로그에 언어가 빠지면 그 알림만 조용히 한국어로 나간다."""
    for key, langs in m.NOTIFY_MSG.items():
        assert set(langs) == {"ko", "en", "id"}, key
        for lang, tmpl in langs.items():
            assert tmpl[0] and tmpl[0].strip(), "%s/%s 제목 없음" % (key, lang)
            if lang != "ko":
                assert not HAN.search(tmpl[0]), "%s/%s 제목에 한글" % (key, lang)
                if tmpl[1]:
                    assert not HAN.search(tmpl[1]), "%s/%s 본문에 한글" % (key, lang)


def test_placeholders_match_across_languages():
    """언어마다 자리표시자가 다르면 어떤 언어에서만 값이 빠진다."""
    for key, langs in m.NOTIFY_MSG.items():
        ref = None
        for lang, tmpl in langs.items():
            names = set(re.findall(r"\{(\w+)\}", (tmpl[0] or "") + " " + (tmpl[1] or "")))
            if ref is None:
                ref = names
            assert names == ref, "%s: %s 의 자리표시자가 다름 %s vs %s" % (key, lang, names, ref)


def test_human_written_body_is_not_translated(tmp_path):
    """보완 사유는 사람이 쓴 글 — 제목만 번역하고 본문은 원문 그대로."""
    db = _db(tmp_path)
    note = "3번 원재료 공급사 할랄 인증서가 만료되었습니다. 재발급본을 올려주세요."
    n = m._notify(db, _case(db), "preassess.doc_request", body=note,
                  msg=("preassess.doc_request", {}))
    title, body = m.notify_text(n, "id")
    assert body == note, "심사자가 쓴 문장을 기계가 바꿔 쓰면 기록이 원문과 달라진다"
    assert not HAN.search(title)


def test_old_notifications_without_payload_still_render(tmp_path):
    """payload 가 없는 기존 알림은 저장된 문구를 그대로 — 마이그레이션 없이 동작해야 한다."""
    db = _db(tmp_path)
    n = models.Notification(org_id="org1", case_id="c1", event_type="legacy",
                            title="예전 알림", body="예전 본문")
    db.add(n)
    db.commit()
    assert m.notify_text(n, "id") == ("예전 알림", "예전 본문")


def test_unknown_key_falls_back_to_stored_text(tmp_path):
    """카탈로그에 없는 키를 줘도 죽지 않는다."""
    db = _db(tmp_path)
    n = m._notify(db, _case(db), "whatever", title="제목", body="본문",
                  msg=("no.such.key", {}))
    assert m.notify_text(n, "en") == ("제목", "본문")


def test_missing_param_falls_back_instead_of_crashing(tmp_path):
    """파라미터가 비어도 알림이 사라지면 안 된다 — 조립 실패는 저장 문구로 물러선다."""
    db = _db(tmp_path)
    n = m._notify(db, _case(db), "certificate_issued",
                  msg=("certificate_issued", {"company": "CV. CITRA"}))   # cert_no 누락
    title, body = m.notify_text(n, "id")
    assert title and body is not None
