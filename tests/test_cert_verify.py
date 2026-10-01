"""인증번호 검증(REQ-CERT-001) — 번호가 적혀 있다는 사실만으로 의심을 할랄로 올리지 않는다.
형식·뒷받침 문서·유효기간을 단계로 확인한다.

실행: <venv>/bin/python -m pytest tests/test_cert_verify.py -q
"""

import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("GLHAC_DB_URL", "sqlite:///./glhac_certverify_test.db")

from app import cert_verify as cv          # noqa: E402
from app import screening                  # noqa: E402


def _doc(cert_no="ID00410000500391022", expiry="2030-01-01", text="Sertifikat Halal"):
    return {"doc_type": "halal_certificate",
            "fields": {"cert_no": cert_no, "expiry_date": expiry},
            "text": text}


def _with_flag(value, fn):
    """플래그를 켜고/끄고 실행한 뒤 원복한다."""
    prev = os.environ.get("GLHAC_CERT_VERIFY")
    os.environ["GLHAC_CERT_VERIFY"] = value
    try:
        return fn()
    finally:
        if prev is None:
            os.environ.pop("GLHAC_CERT_VERIFY", None)
        else:
            os.environ["GLHAC_CERT_VERIFY"] = prev


def test_정규화():
    """공백·대소문자를 걷어낸 형태로만 비교한다."""
    assert cv.normalize("  id 004 1000 ") == "ID0041000"
    assert cv.normalize(None) == ""
    assert cv.normalize("") == ""


def test_발급기관_형식_인식():
    """실사용 번호 체계를 발급기관으로 식별한다."""
    cases = {
        "ID00410000500391022": "BPJPH",
        "LPPOM-00230049860209": "MUI LPPOM",
        "KAI.5986.12633.250019.CN": "issuer-prefixed",
    }
    for num, issuer in cases.items():
        res = cv.classify_format(num)
        assert res["ok"] is True, (num, res)
        assert res["issuer"] == issuer, (num, res)


def test_숫자만_적힌_번호는_발급기관을_알_수_없다():
    """REQ-CERT-002 — 발급기관을 식별할 수 없는 번호는 증빙으로 쓸 수 없다."""
    # 세 값 모두 실 운영 데이터에서 관측됐다.
    for num in ("398240000", "555240000", "827964482"):
        res = cv.classify_format(num)
        assert res["ok"] is False, (num, res)
        assert "발급기관" in res["reason"], (num, res)


def test_번호가_없으면_ABSENT():
    """번호 부재와 검증 실패를 구분한다."""
    assert cv.verify(None)["verdict"] == "ABSENT"
    assert cv.verify("  ")["verdict"] == "ABSENT"


def test_형식불량은_문서를_보지도_않는다():
    """형식이 먼저다 — 문서가 같은 번호를 담고 있어도 형식 불량이면 거기서 끝난다."""
    res = cv.verify("398240000", docs=[_doc(cert_no="398240000")])
    assert res["verdict"] == "INVALID_FORMAT"


def test_문서가_없으면_미검증():
    """현재 운영 데이터의 대다수다 — 번호만 타이핑됐고 인증서는 제출되지 않았다."""
    num = "ID00410000500391022"
    assert cv.verify(num, docs=[])["verdict"] == "UNVERIFIED_NO_DOCUMENT"
    assert cv.verify(num, docs=None)["verdict"] == "UNVERIFIED_NO_DOCUMENT"
    other = {"doc_type": "coa_msds", "fields": {}, "text": "x"}
    assert cv.verify(num, docs=[other])["verdict"] == "UNVERIFIED_NO_DOCUMENT"


def test_번호가_다르면_MISMATCH():
    """제출된 인증서의 번호와 신고한 번호가 다르면 승격하지 않는다."""
    res = cv.verify("ID00410000500391022", docs=[_doc(cert_no="ID00410000185650421")])
    assert res["verdict"] == "MISMATCH"


def test_일치하면_VERIFIED():
    """번호가 문서로 확인되고 유효기간이 남아 있을 때만 승격 가능하다."""
    res = cv.verify("ID00410000500391022", docs=[_doc(expiry="2030-01-01")],
                    today=date(2026, 9, 28))
    assert res["verdict"] == "VERIFIED"
    assert res["matched_document"] is not None
    assert res["expiry"] == "2030-01-01"
    assert cv.promotes(res) is True


def test_유효기간이_지나면_EXPIRED():
    """만료 인증서로는 승격하지 않되, 어느 문서와 대조됐는지는 남긴다."""
    res = cv.verify("ID00410000500391022", docs=[_doc(expiry="2020-01-01")],
                    today=date(2026, 9, 28))
    assert res["verdict"] == "EXPIRED"
    assert cv.promotes(res) is False
    assert res["matched_document"] is not None


def test_본문에서_번호를_찾는다():
    """fields 추출이 비어도 본문에서 번호를 찾는다. 만료일이 없으면 만료로 보지 않는다."""
    doc = {"doc_type": "halal_certificate", "fields": {},
           "text": "Sertifikat Halal Nomor ID00410000500391022 berlaku"}
    res = cv.verify("ID00410000500391022", docs=[doc], today=date(2026, 9, 28))
    assert res["verdict"] == "VERIFIED"


def test_플래그_꺼짐이면_기존_승격이_유지된다():
    """기본 배포는 동작 무변경 — 다만 판정은 기록해 영향 규모를 잴 수 있어야 한다."""
    result = _with_flag("", lambda: screening.screen_merged("Gelatin", cert_no="398240000"))
    assert result["status"] == "halal"
    assert result["result"] == "CLEARED"
    assert result["cert_promoted"] is True
    assert result["cert_verification"]["verdict"] == "INVALID_FORMAT"


def test_플래그_켜짐이면_미검증_번호로_승격하지_않는다():
    """AC-06 — 인증번호만으로 HALAL 이 된 판정이 0건이어야 한다."""
    result = _with_flag("1", lambda: screening.screen_merged("Gelatin", cert_no="398240000"))
    assert result["status"] == "mushbooh"
    assert result["cert_promoted"] is False


def test_플래그_켜짐이어도_검증되면_승격한다():
    """검증 체인을 통과하면 승격한다 — 인증서를 무시하는 것이 아니다."""
    doc = _doc(cert_no="ID00410000500391022", expiry="2030-01-01")
    result = _with_flag("1", lambda: screening.screen_merged(
        "Gelatin", cert_no="ID00410000500391022", docs=[doc]))
    assert result["cert_promoted"] is True
    assert result["decision_by"] == "cert_no_verified"
    assert result["status"] != "mushbooh"
    if result["status"] == "halal":
        assert result["result"] == "CLEARED"


def test_모드_문자열_해석():
    """환경변수 문자열이 의도한 모드로 해석되는지 확인한다."""
    cases = [
        ("", "off"), ("0", "off"), ("off", "off"), ("false", "off"), ("no", "off"),
        ("format", "format"), ("FORMAT", "format"), ("  format  ", "format"),
        ("1", "strict"), ("true", "strict"), ("on", "strict"), ("yes", "strict"),
        ("strict", "strict"),
    ]
    for value, expected in cases:
        got = _with_flag(value, screening._cert_verify_mode)
        assert got == expected, (value, got, expected)


def test_오타값은_off로_떨어진다():
    """알 수 없는 값은 off 로 폴백된다."""
    # 오타가 인증 판정을 조용히 강화하거나 완화해서는 안 된다.
    for value in ("strictt", "formatt", "verify", "yes please"):
        got = _with_flag(value, screening._cert_verify_mode)
        assert got == "off", (value, got)


def test_may_promote_모드별():
    """모드별 승격 허용 여부 — off 전부 / format 은 형식불량만 차단 / strict 는 VERIFIED 만."""
    verdicts = ("VERIFIED", "EXPIRED", "MISMATCH", "UNVERIFIED_NO_DOCUMENT",
                "INVALID_FORMAT", "ABSENT")
    for v in verdicts:
        assert cv.may_promote({"verdict": v}, "off") is True, v
    for v in verdicts:
        assert cv.may_promote({"verdict": v}, "format") is (v != "INVALID_FORMAT"), v
    for v in verdicts:
        assert cv.may_promote({"verdict": v}, "strict") is (v == "VERIFIED"), v
    for v in verdicts:
        assert cv.may_promote({"verdict": v}, "nonsense") is True, v
    assert cv.MODES == ("off", "format", "strict")


def test_format_모드는_형식불량만_차단한다():
    """중간 단계 — 발급기관을 식별할 수 없는 번호만 즉시 막고, 인증서 미제출은 경고로 남긴다."""
    result = _with_flag("format", lambda: screening.screen_merged(
        "Gelatin", cert_no="827964482"))          # 숫자만 — 실 관측값
    assert result["cert_promoted"] is False
    assert result["status"] == "mushbooh"
    assert result["cert_verification"]["verdict"] == "INVALID_FORMAT"
    assert result["cert_verification"]["mode"] == "format"

    result = _with_flag("format", lambda: screening.screen_merged(
        "Gelatin", cert_no="ID00410000500391022"))   # 형식은 정상, 문서 미제출
    assert result["cert_promoted"] is True
    assert result["status"] == "halal"
    assert result["cert_verification"]["verdict"] == "UNVERIFIED_NO_DOCUMENT"
