"""표 텍스트층의 셀 사이 인용과 원문 언어 보존. 모든 업체·인명은 합성값이다."""
import json
from copy import deepcopy
from uuid import uuid4

import pytest

from app import ai_local, intake, models
from app.document_extraction import normalized_text
import app.main as m


TABLE_TEXT = (
    "사업자등록증명\nCertificate of Business Registration\n"
    "상\n호\n( 법\n인\n명)\nN a m e\no f\nB u s i n e s s\n"
    "주식회사누리샘플\nNURI SAMPLE INC\n"
    "사\n업\n자\n등\n록\n번\n호\nBusiness Taxpayer ID Number\n123-45-67890\n"
    "성\n명\n( 대\n표\n자)\nN a m e\no f\nR e p r e s e n t a t i v e\n"
    "대\n표\n유\n형\nT y p e\no f\nR e p r e s e n t a t i o n\n"
    "김가상\nKim Gasang\n단독대표(Sole Representation)"
)
TABLE_FIELDS = {"company_name": "주식회사누리샘플", "company_name_en": "NURI SAMPLE INC",
                "responsible_person": "김가상"}
TABLE_QUOTES = {"company_name": "상호 (법인명) 주식회사누리샘플",
                "company_name_en": "Name of Business NURI SAMPLE INC",
                "responsible_person": "성명 (대표자) Name of Representative 김가상"}


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr(ai_local, "provider", lambda: "openai")
    monkeypatch.setattr(ai_local, "llm_json", lambda *a, **kw: pytest.fail("고정 AI 응답 필요"))
    monkeypatch.setattr(ai_local, "_openai_chat", lambda *a, **kw: pytest.fail("실 API 호출 금지"))
    monkeypatch.setattr(intake, "_ZIP_LOOKUP", False)
    monkeypatch.setattr(m, "verify_enqueue", lambda *a, **kw: False)


def reply(monkeypatch, fields, evidence, doc_type="nib_business_license"):
    calls = []

    def fake(system, user):
        calls.append((system, user))
        return {"doc_type": doc_type, "confidence": .95,
                "fields": deepcopy(fields), "evidence": deepcopy(evidence)}

    monkeypatch.setattr(ai_local, "llm_json", fake)
    return calls


def signup(monkeypatch, text):
    monkeypatch.setattr(intake, "parse_file", lambda *a, **kw: text)
    return m._auth_ocr_extract_file(b"%PDF-synthetic", allow_ai=True)


def test_business_license_spec_appends_representative_and_english_fields():
    desc, fmt = intake._FIELD_SPEC["nib_business_license"]
    original_keys = ["company_name", "nib", "address", "city", "province", "country", "zip",
                     "phone", "business_type", "establishment_date", "corporate_reg_no"]
    added_keys = ["responsible_person", "responsible_person_en", "company_name_en", "address_en"]
    assert list(json.loads(fmt).items()) == [(key, None) for key in original_keys + added_keys]
    for label in ("대표자(성명)", "영문 대표자명", "영문 회사명", "영문 주소"):
        assert label in desc


def test_signup_keeps_representative_and_english_fields_from_ai(monkeypatch):
    fields = {**TABLE_FIELDS, "nib": "123-45-67890", "responsible_person_en": "Kim Gasang",
              "address": "가상시 샘플로 14", "address_en": "14 Sample Road, Example City"}
    quotes = {**TABLE_QUOTES, "nib": "Business Taxpayer ID Number 123-45-67890",
              "responsible_person_en": "Name of Representative Kim Gasang",
              "address": "사업장 소재지: 가상시 샘플로 14",
              "address_en": "Business Address: 14 Sample Road, Example City"}
    text = TABLE_TEXT + "\n" + quotes["address"] + "\n" + quotes["address_en"]
    calls = reply(monkeypatch, fields, quotes)
    result = signup(monkeypatch, text)
    assert result["fields"] == fields
    assert set(result["filled"]) == set(fields) and result["missing"] == []
    assert "llm_error" not in result
    for key in fields:
        assert result["field_sources"][key] == "ai"
        assert result["field_meta"][key]["accepted"]
        assert result["field_meta"][key]["checks"]["evidence"]
    assert len(calls) == 1
    desc, fmt = intake._FIELD_SPEC["nib_business_license"]
    assert desc in calls[0][0] and fmt in calls[0][0]


@pytest.mark.parametrize("stage", ["signup", "classify", "parse"])
def test_interleaved_bilingual_cells_keep_all_three_fields(monkeypatch, stage):
    reply(monkeypatch, TABLE_FIELDS, TABLE_QUOTES)
    assert all(normalized_text(q) not in normalized_text(TABLE_TEXT) for q in TABLE_QUOTES.values())
    if stage == "signup":
        result = signup(monkeypatch, TABLE_TEXT)
        meta = result["field_meta"]
        assert set(TABLE_FIELDS) <= set(result["filled"])
    else:
        result = (intake.classify("사업자등록증명.txt", TABLE_TEXT) if stage == "classify" else
                  intake.parse_typed_text("nib_business_license", "사업자등록증명.txt", TABLE_TEXT))
        meta = result["fields"]["_field_meta"]
    assert {key: result["fields"][key] for key in TABLE_FIELDS} == TABLE_FIELDS
    for key in TABLE_FIELDS:
        assert meta[key]["accepted"] and meta[key]["checks"]["evidence"]
        assert meta[key]["evidence"] == TABLE_QUOTES[key]


@pytest.mark.parametrize("value", ["실체적 권리관계", "하단에만있는가상업체"])
def test_footer_value_far_from_label_is_not_evidence(monkeypatch, value):
    text = ("사업자등록증명\n상호 (법인명) 미기재\n" + "다른항목의내용\n" * 60 +
            "안내문: [상호, 성명(대표자), 업태]은 실체적 권리관계를 증명하는 효력이 없습니다.\n" + value)
    reply(monkeypatch, {"company_name": value}, {"company_name": "상호 (법인명) " + value})
    result = signup(monkeypatch, text)
    meta = result["field_meta"]["company_name"]
    assert meta["checks"]["in_source"]
    assert not meta["accepted"] and not meta["checks"]["evidence"]
    assert "missing_or_invalid_evidence" in meta["reasons"]
    assert "company_name" not in result["fields"]


@pytest.mark.parametrize("key", TABLE_FIELDS)
def test_value_absent_from_source_is_rejected(monkeypatch, key):
    value = "Missing Fictional Value"
    reply(monkeypatch, {key: value}, {key: TABLE_QUOTES[key].replace(TABLE_FIELDS[key], value)})
    result = signup(monkeypatch, TABLE_TEXT)
    assert key not in result["fields"]
    assert not result["field_meta"][key]["accepted"]
    assert "not_in_source_or_invalid_format" in result["field_meta"][key]["reasons"]


def test_english_only_signup_preserves_company_address_and_person(monkeypatch):
    fields = {"company_name": "Example Dairy Ltd.", "company_name_en": "Example Dairy Ltd.",
              "responsible_person": "Mira Example", "address": "14 Orchard Road, Example City"}
    quotes = {"company_name": "Name of Business: Example Dairy Ltd.",
              "company_name_en": "Name of Business: Example Dairy Ltd.",
              "responsible_person": "Representative: Mira Example",
              "address": "Business Address: 14 Orchard Road, Example City"}
    text = "Certificate of Business Registration\n" + "\n".join(quotes.values())
    calls = reply(monkeypatch, fields, quotes)
    result = signup(monkeypatch, text)
    assert result["fields"] == fields
    assert "company_name" in result["filled"]
    assert all(result["field_meta"][k]["accepted"] for k in fields)
    assert len(calls) == 1
    system, _ = calls[0]
    assert "번역·음차·로마자화하지 마세요" in system
    assert "영문 원문을 넣으세요" in system


def test_korean_translation_of_english_only_values_is_rejected(monkeypatch):
    text = ("Certificate of Business Registration\nName of Business: Example Dairy Ltd.\n"
            "Business Address: 14 Orchard Road, Example City")
    fields = {"company_name": "예시유업", "address": "예시시 과수원로 14"}
    reply(monkeypatch, fields, {key: text for key in fields})
    result = signup(monkeypatch, text)
    assert not result["fields"]
    assert all(not result["field_meta"][key]["accepted"] for key in fields)


@pytest.mark.parametrize("gap,accepted", [(0, True), (200, True), (201, False)])
def test_exact_normalized_span_limit(monkeypatch, gap, accepted):
    reply(monkeypatch, {"company_name": "Nori"}, {"company_name": "Name Nori"})
    result = signup(monkeypatch, "사업자등록증명\nName " + "X" * gap + " Nori")
    assert result["field_meta"]["company_name"]["accepted"] is accepted


@pytest.mark.parametrize("text,quote,value,accepted", [
    ("상호 Name 주식회사누리샘플", "상호주식회사누리샘플", "주식회사누리샘플", False),
    ("상호 Name 주식회사누리샘플", "상호 () 주식회사누리샘플", "주식회사누리샘플", True),
    ("Nori Labs\nName Nori adjacent Labs", "Name Nori Labs", "Nori Labs", False),
    ("Name Nori adjacent Labs later Nori Labs", "Name Nori Labs", "Nori Labs", True),
    ("Name " + "X" * 201 + " Name cell Nori Labs", "Name Nori Labs", "Nori Labs", True),
    ("Nori Labs Name", "Name Nori Labs", "Nori Labs", False),
    ("Name Nori Labs\nOther Company", "Name Nori Labs", "Other Company", False),
    ("Name cell Nori Labs\nOther Company", "Name Nori Labs", "Other Company", False),
])
def test_fragment_order_single_piece_repetition_and_value_scope(monkeypatch, text, quote, value, accepted):
    reply(monkeypatch, {"company_name": value}, {"company_name": quote})
    result = signup(monkeypatch, "사업자등록증명\n" + text)
    assert result["field_meta"]["company_name"]["accepted"] is accepted


@pytest.mark.parametrize("doc_type,key,value,label", [
    ("factory_registration", "company_name", "Nori Labs", "Name"),
    ("halal_certificate", "issuer", "Example Authority", "Issuer"),
    ("quality_cert", "issuer", "Example Authority", "Issuer"),
    ("coa_msds", "manufacturer", "Nori Labs", "Manufacturer"),
])
@pytest.mark.parametrize("gap,accepted", [("adjacent cell", True), ("X" * 201, False)])
def test_layout_rule_also_applies_to_other_document_types(monkeypatch, doc_type, key, value, label, gap, accepted):
    reply(monkeypatch, {key: value}, {key: label + " " + value}, doc_type)
    result = intake.parse_typed_text(doc_type, "scan.txt", label + "\n" + gap + "\n" + value)
    assert result["fields"]["_field_meta"][key]["accepted"] is accepted


def test_list_quotes_each_require_their_own_local_value():
    text = "Products Alpha nearby One\nProducts Beta nearby Two\nAlpha One\nBeta Two"
    fields = intake._checked_fields({"product_names": ["Alpha One", "Beta Two"]},
        {"product_names": ["Products Alpha One", "Products Beta Two"]}, {}, text)
    # 값이 가까이 있을 때만 허용한다. 각 인용의 실제 값을 모두 200자 밖으로 옮기면 거절한다.
    far_text = text.split("\nAlpha One")[0] + "\n" + "X" * 201 + "\nAlpha One\nBeta Two"
    rejected = intake._checked_fields({"product_names": ["Alpha One", "Beta Two"]},
        {"product_names": ["Products Alpha One", "Products Beta Two"]}, {}, far_text)
    assert fields["product_names"] == ["Alpha One", "Beta Two"]
    assert "product_names" not in rejected


def test_boolean_negation_suspect_issuer_and_rule_conflict_guards_are_preserved():
    fields = intake._checked_fields({"has_commitment": True}, {"has_commitment": "Commitment included"},
                                   {}, "Commitment not included")
    assert "has_commitment" not in fields
    assert not fields["_field_meta"]["has_commitment"]["checks"]["evidence"]
    fields = intake._checked_fields({"company_name": "상호"}, {"company_name": "Label 상호"}, {}, "Label cell 상호")
    assert "company_name" not in fields and fields["_field_meta"]["company_name"]["checks"]["suspect"]
    fields = intake._checked_fields({"phone": "02-123-4567"}, {"phone": "Telephone: 02-123-4567"},
                                   {}, "Tax Office\nTelephone: 02-123-4567")
    assert "phone" not in fields and "issuer_contact" in fields["_field_meta"]["phone"]["reasons"]
    fields = intake._checked_fields({"company_name": "Nori Labs"}, {"company_name": "Name Nori Labs"},
                                   {"company_name": "Other Company"}, "Name cell Nori Labs")
    assert fields["company_name"] == "Nori Labs"
    assert "rule_disagreement" in fields["_field_meta"]["company_name"]["reasons"]


@pytest.mark.parametrize("lang,english", [("ko", False), (None, False), ("fr", False),
                                         ("en", True), ("id", True), ("en-US", True), ("id_ID", True)])
@pytest.mark.parametrize("split_documents", [False, True])
def test_aggregate_prefers_applicant_language_and_keeps_both_spellings(lang, english, split_documents):
    ko = {"company_name": TABLE_FIELDS["company_name"], "responsible_person": "김가상"}
    en = {"company_name": TABLE_FIELDS["company_name_en"], "responsible_person": "Kim Gasang"}
    values = [ko, en] if split_documents else [{**ko, **{k + "_en": v for k, v in en.items()}}]
    docs = [{"doc_type": "nib_business_license", "fields": fields} for fields in values]
    original = deepcopy(docs)
    agg = intake.aggregate_fields(docs, lang=lang)
    for key in ko:
        assert agg[key] == (en[key] if english else ko[key])
        assert agg[key + "_ko"] == ko[key] and agg[key + "_en"] == en[key]
    assert docs == original


@pytest.mark.parametrize("lang,fields", [("en", {"company_name": "한글만있는가상업체"}),
                                        ("ko", {"company_name": "English Only Example"})])
def test_missing_preferred_language_uses_available_original(lang, fields):
    assert intake.aggregate_fields([{"doc_type": "nib_business_license", "fields": fields}],
                                   lang=lang)["company_name"] == fields["company_name"]


@pytest.mark.parametrize("doc_type,accepted", [("nib_business_license", True),
                                             ("factory_registration", False),
                                             ("supplier_declaration", False)])
def test_aggregate_english_address_only_comes_from_company_document(doc_type, accepted):
    address = "14 Sample Road, Example City"
    agg = intake.aggregate_fields([{"doc_type": doc_type, "fields": {"address_en": address}}])
    assert agg.get("address_en") == (address if accepted else None)


@pytest.mark.parametrize("case_profile,org_profile,english", [
    ({"lang": "en-US"}, {}, True),
    ({}, {"language": "id"}, True),
    ({}, {"tenant": {"branding": {"locale": "en"}}}, True),
    ({"locale": "ko"}, {"lang": "id"}, False),
    ({}, {}, False),
])
@pytest.mark.parametrize("route", ["zip", "profile"])
def test_existing_language_path_reaches_case_aggregation(case_profile, org_profile, english, route):
    org_id = "evidence-layout-" + uuid4().hex
    digits = str(uuid4().int)[:10]
    nib = digits[:3] + "-" + digits[3:5] + "-" + digits[5:]
    text = TABLE_TEXT.replace("123-45-67890", nib)
    fields = {**TABLE_FIELDS, "responsible_person_en": "Kim Gasang", "nib": nib,
              "address_en": "14 Sample Road, Example City"}
    text += "\nBusiness Address: " + fields["address_en"]
    with m.SessionLocal() as db:
        db.add(models.Org(org_id=org_id, name=TABLE_FIELDS["company_name"], profile_ext=org_profile))
        case = models.CaseApplication(org_id=org_id, company_name=TABLE_FIELDS["company_name"],
                                      nib=fields["nib"], profile_ext=case_profile)
        db.add(case)
        db.commit()
        if route == "zip":
            row = {"filename": "사업자등록증명.txt", "doc_type": "nib_business_license", "confidence": .95,
                   "fields": deepcopy(fields), "text_full": text}
            result = {"classified": [row], "extracted": intake.aggregate_fields([row])}
            m._apply_intake_autofill(db, case, result, base_fields_by_id={})
        else:
            db.add(models.DocumentAsset(case_id=case.case_id, filename="사업자등록증명.txt",
                doc_type="nib_business_license", fields=deepcopy(fields), text_full=text))
            db.commit()
            result = m.autofill_profile(case.case_id, db=db,
                user={"role": "applicant", "org_id": org_id, "uid": "applicant1"})
        assert result["extracted"]["company_name"] == fields["company_name_en" if english else "company_name"]
        assert result["extracted"]["responsible_person"] == fields["responsible_person_en" if english else "responsible_person"]
        for key in ("responsible_person_en", "company_name_en", "address_en"):
            assert result["extracted"][key] == fields[key]
        # 신청서의 국문/영문 별도 칸은 그대로 채우며 대표 집계값만 언어를 따른다.
        assert case.profile_ext["company_name_ko"] == fields["company_name"]
        assert case.profile_ext["company_name_en"] == fields["company_name_en"]
        assert case.responsible_person == fields["responsible_person"]
        assert case.profile_ext["responsible_person_en"] == fields["responsible_person_en"]
        assert case.profile_ext["address_en"] == fields["address_en"]


def test_english_address_conflict_can_be_confirmed_without_overwriting_manual_value():
    org_id = "evidence-layout-" + uuid4().hex
    digits = str(uuid4().int)[:10]
    nib = digits[:3] + "-" + digits[3:5] + "-" + digits[5:]
    address = "14 Sample Road, Example City"
    with m.SessionLocal() as db:
        db.add(models.Org(org_id=org_id, name=TABLE_FIELDS["company_name"]))
        case = models.CaseApplication(org_id=org_id, company_name=TABLE_FIELDS["company_name"],
                                      nib=nib, profile_ext={"address_en": "Previous manual address"})
        db.add(case)
        db.flush()
        doc = models.DocumentAsset(case_id=case.case_id, filename="사업자등록증명.txt",
            doc_type="nib_business_license", fields={"company_name": case.company_name,
                "nib": nib, "address_en": address},
            text_full=TABLE_TEXT.replace("123-45-67890", nib) + "\nBusiness Address: " + address)
        db.add(doc)
        db.commit()
        m._apply_owned_autofill(db, case, doc)
        assert case.profile_ext["address_en"] == "Previous manual address"
        [conflict] = [c for c in m._pending_autofill(db, case.case_id) if c["field"] == "address_en"]
        assert conflict["scope"] == "profile" and conflict["reason"] == "value_conflict"
        m._confirm_autofill(db, case, [{"conflict_id": conflict["conflict_id"], "value": address}],
                           {"role": "applicant", "uid": "applicant1"})
        assert case.profile_ext["address_en"] == address


@pytest.mark.parametrize("text,quote,accepted", [
    ("Name Legal Nori nearby Labs Nori Labs", "Name Legal Nori Labs", True),
    ("Name Nori Labs Legal Nori nearby Labs", "Name Legal Nori Labs", False),
    ("Name Legal Nori Labs Confirmed nearby Seal", "Name Legal Nori Labs Confirmed Seal", True),
    ("Name Legal Nori nearby Labs Confirmed Nori Labs Seal", "Name Legal Nori Labs Confirmed Seal", False),
    ("Name Nori Labs Nori nearby Labs", "Name Nori Nori Labs", False),
])
def test_value_preserves_all_preceding_and_following_fragments(text, quote, accepted):
    assert normalized_text(quote) not in normalized_text(text)
    assert intake._evidence_quote_in_source(quote, "Nori Labs", normalized_text(text)) is accepted


def test_footer_nearby_value_cannot_precede_legal_name_fragment(monkeypatch):
    value = "Nori Labs"
    footer = ("안내문: [상호, 성명(대표자), 업태, 종목]의 예시업체 Nori Labs.\n"
              "부가 설명: (법인명) Nori 참고 Labs")
    text = ("사업자등록증명\n상호 (법인명) 다른가상기업\n" + "다른항목의내용\n" * 60 + footer)
    footer_text = normalized_text(footer)
    assert 0 < footer_text.index(normalized_text(value)) - footer_text.index("상호") < 200
    reply(monkeypatch, {"company_name": value}, {"company_name": "상호 (법인명) " + value})
    result = signup(monkeypatch, text)
    meta = result["field_meta"]["company_name"]
    assert meta["checks"]["in_source"]
    assert not meta["accepted"] and not meta["checks"]["evidence"]
    assert "company_name" not in result["fields"]


@pytest.mark.parametrize("reverse_values", [False, True])
@pytest.mark.parametrize("source_values,accepted", [
    ("Alpha One Beta Two", True), ("Beta Two Alpha One", False),
])
def test_list_values_follow_quote_order_independent_of_list_order(reverse_values, source_values, accepted):
    values = ["Alpha One", "Beta Two"]
    if reverse_values:
        values.reverse()
    quote = "Products Alpha One Beta Two Confirmed"
    text = "Products Alpha nearby One Beta nearby Two " + source_values + " Confirmed"
    fields = intake._checked_fields({"product_names": values}, {"product_names": quote}, {}, text)
    assert fields["_field_meta"]["product_names"]["checks"]["in_source"]
    assert fields["_field_meta"]["product_names"]["accepted"] is accepted
    assert fields.get("product_names") == (values if accepted else None)


@pytest.mark.parametrize("failed_candidates_offset,accepted", [(-1, True), (0, False)])
def test_first_fragment_candidate_limit(failed_candidates_offset, accepted):
    quote = "Name Nori Labs"
    failures = intake._EVIDENCE_MAX_START_CANDIDATES + failed_candidates_offset
    gap = "X" * (len(normalized_text(quote)) + intake._EVIDENCE_MAX_SKIPPED_CHARS + 1)
    text = ("Name " + gap + "\n") * failures + "Name nearby Nori Labs"
    assert normalized_text(quote) not in normalized_text(text)
    assert intake._evidence_quote_in_source(quote, "Nori Labs", normalized_text(text)) is accepted


@pytest.mark.parametrize("reverse_docs", [False, True])
@pytest.mark.parametrize("as_iterator", [False, True])
@pytest.mark.parametrize("lang,preferred", [("ko", "_ko"), ("en", "_en"), ("id", "_en")])
def test_explicit_language_fields_override_inferred_values_in_any_document_order(
        reverse_docs, as_iterator, lang, preferred):
    explicit = {"company_name_ko": "명시된가상기업", "company_name_en": "Explicit Example Ltd",
                "responsible_person_ko": "김명시", "responsible_person_en": "Mira Explicit"}
    docs = [
        {"doc_type": "nib_business_license", "fields": {
            "company_name": "추정된가상기업", "responsible_person": "김추정"}},
        {"doc_type": "nib_business_license", "fields": {
            "company_name": "Inferred Example Ltd", "responsible_person": "Mira Inferred"}},
        {"doc_type": "factory_registration", "fields": explicit},
    ]
    if reverse_docs:
        docs.reverse()
    original = deepcopy(docs)
    agg = intake.aggregate_fields(iter(docs) if as_iterator else docs, lang=lang)
    assert {key: agg[key] for key in explicit} == explicit
    for key in ("company_name", "responsible_person"):
        assert agg[key] == explicit[key + preferred]
    assert docs == original
