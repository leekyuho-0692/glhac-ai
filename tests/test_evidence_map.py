import pytest

from app import evidence_map as em


FINISHED_COA = {
    "doc_type": "coa_msds",
    "fields": {},
    "text": ("CERTIFICATE OF ANALYSIS Product Name : Grabmeal Petit Grain "
             "Parameter Result Ethanol content 0.28 % Lead 0.95 ppm"),
}


def test_별칭_해소():
    """별칭·대소문자·공백이 정규 코드로 해소되는지 지킨다."""
    assert em.resolve_code("slaughter_cert") == "halal_slaughter_cert"
    assert em.resolve_code("halal_slaughter_certificate") == "halal_slaughter_cert"
    assert em.canonical_doc_type("manual_section") == "sjph_manual"
    assert em.canonical_doc_type("  HALAL_CERT ") == "halal_certificate"
    assert em.resolve_code("") is None
    assert em.canonical_doc_type(None) is None


def test_매핑표_정합성():
    """매핑표 ↔ domain_dict ↔ doc_type 어휘 드리프트가 없는지 지킨다."""
    v = em.validate()
    for key, value in v.items():
        assert not value, "validate() reported issues under %r: %r" % (key, value)


def test_조치항목은_판정대상이_아니다():
    """reformulation 은 증빙이 아니라 조치다 — 판정 대상에 넣으면 영구 미충족으로 남는다."""
    assert em.is_action_only("reformulation") is True
    cov = em.coverage(["reformulation"], [])
    assert cov["not_applicable"] == 1
    assert cov["required_effective"] == 0
    assert cov["decision_ready"] is True


def test_인증번호만으로는_충족되지_않는다():
    """REQ-CERT-001 — 인증번호 필드가 없으면 할랄 인증서도 충족이 아니다."""
    base = {"doc_type": "halal_certificate", "fields": {}, "text": "Sertifikat Halal MUI"}
    assert em.check_document("halal_cert", base)["verdict"] == "NOT_SATISFIED"

    with_no = dict(base, fields={"cert_no": "ID004123"})
    res = em.check_document("halal_cert", with_no)
    assert res["verdict"] == "SATISFIED"
    assert res["grade"] == "B"


def test_문서유형이_틀리면_미충족():
    """문서유형이 다르면 내용에 키워드가 있어도 미충족이고 tier 는 None 이다."""
    doc = {"doc_type": "coa_msds", "fields": {"cert_no": "X"}, "text": "halal"}
    res = em.check_document("halal_cert", doc)
    assert res["verdict"] == "NOT_SATISFIED"
    assert res["tier"] is None


def test_전성분표는_조성내역의_보조자료다():
    """완제품 전성분표는 제품의 원료 목록이지 그 원료 내부 조성이 아니다 → SUPPORTING_ONLY."""
    ml = {"doc_type": "material_list", "fields": {}, "text": "komposisi bahan"}
    res = em.check_document("composition_breakdown", ml)
    assert res["verdict"] == "SUPPORTING_ONLY"
    assert res["grade"] == "D"

    coa = {"doc_type": "coa_msds", "fields": {}, "text": "raw material composition 표"}
    assert em.check_document("composition_breakdown", coa)["verdict"] == "SATISFIED"


def test_완제품_시험성적서는_원료조성이_아니다():
    """회귀: 완제품 CoA 1장이 원료 57건의 조성내역을 충족시켰다.
    키워드에서 '%'·'content' 를 제거한 이유가 이것이다."""
    assert em.check_document("composition_breakdown", FINISHED_COA)["verdict"] == "NOT_SATISFIED"


def test_대상을_지목하지_않으면_NOT_SCOPED():
    """스코프 게이트 — 문서가 그 원료를 지목하지 않으면 충족으로 세지 않는다."""
    subject = {"id": "M1", "names": ["Whey Protein Isolate"]}
    assert em.check_document("residual_alcohol_test", FINISHED_COA,
                             subject=subject)["verdict"] == "NOT_SCOPED"
    # subject 를 주지 않으면 통과한다 — 바뀌는 것이 스코프 게이트임을 보인다
    assert em.check_document("residual_alcohol_test", FINISHED_COA)["verdict"] == "SATISFIED"


def test_명시_바인딩이_지목을_대신한다():
    """material_id 명시 바인딩은 이름이 전혀 안 맞아도 스코프를 통과시킨다."""
    doc = {"doc_type": "origin_certificate", "fields": {"origin": "Korea"},
           "text": "certificate of origin", "material_id": "M1"}
    subject = {"id": "M1", "names": ["전혀다른이름"]}
    assert em.check_document("source_declaration", doc, subject=subject)["verdict"] == "SATISFIED"


def test_짧은_약어는_지목으로_보지_않는다():
    """3자 미만 이름(SAN·PP 등)은 우연히 걸린다 — 지목으로 보지 않는다."""
    doc = {"doc_type": "origin_certificate", "fields": {"origin": "KR"},
           "text": "certificate of origin for pisang"}
    subject = {"id": "M9", "names": ["SAN"]}
    assert em.check_document("source_declaration", doc, subject=subject)["verdict"] == "NOT_SCOPED"


def test_사유_고르기_우선순위():
    """회귀(TAURINE 오보고): 이 원료의 primary 문서가 사유를 공급해야 한다.
    남의 NOT_SCOPED 나 보조 전성분표가 자기 문서의 구체적 실패를 가리면
    요청서가 '제출된 서류가 이 원재료를 지목하지 않습니다' 로 엉뚱하게 안내한다."""
    d_other = {"doc_type": "origin_certificate", "fields": {"origin": "Vietnam"},
               "text": "certificate of origin for Taurine", "material_id": "M-OTHER"}
    d_list = {"doc_type": "material_list", "fields": {}, "text": "원재료 목록 MyMaterial"}
    d_own = {"doc_type": "origin_certificate", "fields": {},
             "text": "certificate of origin MyMaterial", "material_id": "M-MINE"}
    subject = {"id": "M-MINE", "names": ["MyMaterial"]}
    cov = em.coverage(["source_declaration"], [d_other, d_list, d_own], subject=subject)
    assert cov["items"][0]["verdict"] == "NOT_SATISFIED"


def test_충족이_미충족보다_먼저다():
    """충족된 문서가 미충족·미스코프 문서보다 항상 먼저다 —
    아니면 이미 충족된 요구가 미충족으로 보고된다."""
    d_ns = {"doc_type": "origin_certificate", "fields": {"origin": "X"},
            "text": "certificate of origin for SomethingElse", "material_id": "M-OTHER"}
    d_ok = {"doc_type": "coa_msds", "fields": {"source": "plant"},
            "text": "derived from vegetable origin MyMaterial"}
    subject = {"id": "M-MINE", "names": ["MyMaterial"]}
    cov = em.coverage(["source_declaration"], [d_ns, d_ok], subject=subject)
    assert cov["items"][0]["verdict"] == "SATISFIED_WEAK"
    assert cov["decision_ready"] is True


def test_doctype_only_는_내용검사를_건너뛴다():
    """상한 측정 모드 — 내용조건을 건너뛴다는 사실이 사유에 남아야 한다."""
    res = em.check_document("composition_breakdown", FINISHED_COA, content_check="doctype_only")
    assert res["verdict"] == "SATISFIED"
    assert "내용조건 미검사" in res["reason"]
