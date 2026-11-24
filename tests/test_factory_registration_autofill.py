"""공장등록증 서식 8-2의 표 검증 회귀. 업체·주소·번호는 모두 합성값이다."""
import pytest

from app import intake


COMPANY = "TEST PLANT CO.,LTD"
REG_NO = "999990000011111"
PHONE = "+82-31-555-0123"
ADDRESS = "123-45, Sample-ro, Example-eup, Test-si, Sample-do, Republic of Korea"
ADDRESS_QUOTE = (
    "Address of Plant Land Category Property Status "
    "123-45, Sample-ro, Example-eup. factory site Owned [v ], Rented [ ] "
    "Test-si, Sample-do. Republic of Korea"
)
TABLE_TEXT = (
    f"Factory Registration Certificate (Form 8-2)\nTel. {COMPANY} {PHONE}\n"
    f"{ADDRESS_QUOTE} Plant Registration Date 2020-01-02\n"
    "No. of Employees Male: 21 Female: 16\n"
    f"Changes\n(Including Dates)\nFactory management number :\n{REG_NO}\n"
    "I hereby apply for registration."
)


@pytest.fixture(autouse=True)
def no_ai(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("공장등록증 단위 검증에서 AI 호출 금지")

    monkeypatch.setattr(intake.ai_local, "llm_json", forbidden)


@pytest.mark.parametrize("label", [
    "Factory management number", "Factory Management No.", "factory management no",
    "공장관리번호", "공장 관리 번호", "공장\n관리\n번호",
    "Plant Registration No.", "Plant registration number",
    "Factory Registration No.", "공장등록번호",
])
def test_registration_labels_accept_value_after_colon_and_newline(label):
    quote = f"{label}  :\n{REG_NO}"
    text = f"Certificate of Factory Registration\nChanges\n(Including Dates)\n{quote}\nI hereby apply for ..."
    assert intake.autofill_value_in_text("factory_reg_no", REG_NO, text)
    assert intake._field_value_grounded("factory_reg_no", REG_NO, text)
    fields = intake._checked_fields({"factory_reg_no": REG_NO}, {"factory_reg_no": quote}, {}, text)
    assert fields["factory_reg_no"] == REG_NO
    assert fields["_field_meta"]["factory_reg_no"]["checks"]["evidence"]


@pytest.mark.parametrize("text,value", [
    ("Factory Registration Certificate\nEXAMPLE CITY F99990000 문서발행일: 2020-01-02", "F99990000"),
    (f"Factory Registration Certificate\nEXAMPLE CITY {REG_NO} 문서발행일: 2020-01-02", REG_NO),
    (f"Factory management number: 999990000022222\nEXAMPLE CITY {REG_NO}", REG_NO),
    (f"Factory management number: {REG_NO}9", REG_NO),
    (f"Factory management number: {REG_NO}", "999990000022222"),
    (f"Supplier:\nFactory management number: {REG_NO}", REG_NO),
    (f"제조업체:\n공장관리번호: {REG_NO}", REG_NO),
])
def test_unlabelled_mismatched_or_foreign_registration_number_is_rejected(text, value):
    assert not intake.autofill_value_in_text("factory_reg_no", value, text)
    assert not intake._field_value_grounded("factory_reg_no", value, text)
    fields = intake._checked_fields({"factory_reg_no": value}, {"factory_reg_no": text}, {}, text)
    assert "factory_reg_no" not in fields
    assert not fields["_field_meta"]["factory_reg_no"]["checks"]["in_source"]


@pytest.mark.parametrize("key", ["address", "factory_address", "address_en"])
@pytest.mark.parametrize("quote", [ADDRESS_QUOTE, "Address of Plant " + ADDRESS])
def test_interleaved_address_accepts_verbatim_and_collapsed_evidence(key, quote):
    assert intake._field_value_grounded(key, ADDRESS, TABLE_TEXT)
    fields = intake._checked_fields({key: ADDRESS}, {key: quote}, {}, TABLE_TEXT)
    assert fields[key] == ADDRESS
    meta = fields["_field_meta"][key]
    assert meta["accepted"] and meta["checks"]["in_source"] and meta["checks"]["evidence"]
    assert not meta["reasons"]


@pytest.mark.parametrize("key", ["address", "factory_address", "address_en"])
@pytest.mark.parametrize("value,text", [
    (ADDRESS.replace("Test-si, Sample-do", "Sample-do, Test-si"), TABLE_TEXT),
    (ADDRESS.replace("Sample-do", "Missing-do"), TABLE_TEXT),
    (ADDRESS, TABLE_TEXT.replace("factory site", "X" * 201 + " factory site")),
])
def test_address_rejects_reordered_missing_or_distant_parts(key, value, text):
    assert not intake._field_value_grounded(key, value, text)
    fields = intake._checked_fields({key: value}, {key: text}, {}, text)
    assert key not in fields
    assert not fields["_field_meta"][key]["checks"]["in_source"]


@pytest.mark.parametrize("key", ["address", "factory_address", "address_en"])
@pytest.mark.parametrize("gaps,accepted", [((0, 0), True), ((30, 30), True),
                                          ((60, 0), True), ((0, 60), True),
                                          ((30, 31), False), ((61, 0), False), ((0, 61), False)])
def test_address_uses_one_total_normalized_skip_budget(key, gaps, accepted):
    value = "123 Sample Road, Example District, Test City"
    text = f"Address of Plant 123 Sample Road {'X' * gaps[0]} Example District {'Y' * gaps[1]} Test City"
    assert intake._field_value_grounded(key, value, text) is accepted
    fields = intake._checked_fields({key: value}, {key: "Address of Plant " + value}, {}, text)
    meta = fields["_field_meta"][key]
    assert meta["accepted"] is accepted
    assert meta["checks"]["in_source"] is accepted
    assert meta["checks"]["evidence"] is accepted


@pytest.mark.parametrize("key", ["address", "factory_address", "address_en"])
def test_address_keeps_short_ownership_cells_between_parts(key):
    value = "275-43, Sinman-ro, Gonjiam-eup, Gwangju-si, Gyeonggi-do, Republic of Korea"
    text = ("Address of Plant 275-43, Sinman-ro, Gonjiam-eup. "
            "factory site Owned [v ], Rented [ ] Gwangju-si, Gyeonggi-do. Republic of Korea")
    assert intake._field_value_grounded(key, value, text)
    fields = intake._checked_fields({key: value}, {key: "Address of Plant " + value}, {}, text)
    assert fields[key] == value
    assert fields["_field_meta"][key]["checks"]["evidence"]


@pytest.mark.parametrize("key", ["address", "factory_address", "address_en"])
@pytest.mark.parametrize("digit", ["12", "１２", "١٢"])
def test_address_rejects_numbers_in_skipped_cells(key, digit):
    value = "123 Sample Road, Test City"
    text = f"Address of Plant 123 Sample Road adjacent cell {digit} Test City"
    assert not intake._field_value_grounded(key, value, text)
    fields = intake._checked_fields({key: value}, {key: "Address of Plant " + value}, {}, text)
    meta = fields["_field_meta"][key]
    assert key not in fields
    assert not meta["checks"]["in_source"] and not meta["checks"]["evidence"]


@pytest.mark.parametrize("key", ["address", "factory_address", "address_en"])
@pytest.mark.parametrize("value,text,quote", [
    ("37-6, Alpha-ro, Epsilon-gun, Zeta-do",
     "Address of Company 37-6, Alpha-ro, Beta-myeon, Gamma-gun "
     "Address of Plant 12, Delta-ri, Epsilon-gun, Zeta-do",
     "Address of Company 37-6, Alpha-ro, Epsilon-gun, Zeta-do"),
    ("123 Sample Road, Test City",
     "Address of Plant 123 Sample Road " + "X" * 61 + " Test City",
     "Address of Plant 123 Sample Road, Test City"),
])
def test_address_rejects_mixed_or_distant_evidence_even_with_value_elsewhere(key, value, text, quote):
    assert not intake._field_value_grounded(key, value, text)
    fields = intake._checked_fields({key: value}, {key: quote}, {}, text)
    assert key not in fields
    assert not fields["_field_meta"][key]["checks"]["in_source"]
    assert not fields["_field_meta"][key]["checks"]["evidence"]

    text += "\nReference: " + value
    fields = intake._checked_fields({key: value}, {key: quote}, {}, text)
    assert fields["_field_meta"][key]["checks"]["in_source"]
    assert not fields["_field_meta"][key]["checks"]["evidence"]
    assert key not in fields


def test_address_number_outside_value_parts_is_not_an_intervening_address():
    value = "123 Sample Road, Test City"
    text = "Address of Plant Land Category 1 123 Sample Road adjacent cell Test City"
    fields = intake._checked_fields({"factory_address": value},
                                    {"factory_address": "Address of Plant " + value}, {}, text)
    assert fields["factory_address"] == value
    assert fields["_field_meta"]["factory_address"]["checks"]["evidence"]


@pytest.mark.parametrize("value,text,accepted", [
    ("A, BC, 12", "A cell BC cell 12", False),
    ("12, Sample Road, Test City", "12 cell Sample Road cell Test City", True),
    ("13, Sample Road, Test City", "12 cell Sample Road cell Test City", False),
    ("123-45,Sample-ro.Test-si", "123-45 Sample-ro adjacent cell Test-si", True),
    ("Sample Road, Test City", "Sample Road " + "X" * 201 + " Sample Road cell Test City", True),
])
def test_address_parts_need_real_anchors_and_all_parts_including_short_ones(value, text, accepted):
    assert intake._field_value_grounded("factory_address", value, text) is accepted
    fields = intake._checked_fields({"factory_address": value}, {"factory_address": value}, {}, text)
    assert fields["_field_meta"]["factory_address"]["accepted"] is accepted


def test_address_matching_does_not_relax_other_fields_or_invent_evidence():
    value = "TEST PLANT, EXAMPLE CITY"
    text = "Manufacturer TEST PLANT adjacent cell EXAMPLE CITY"
    assert not intake._field_value_grounded("manufacturer", value, text)
    fields = intake._checked_fields({"manufacturer": value}, {"manufacturer": "Manufacturer " + value}, {}, text)
    assert "manufacturer" not in fields
    fields = intake._checked_fields({"factory_address": ADDRESS},
                                    {"factory_address": "Invented label " + ADDRESS}, {}, TABLE_TEXT)
    assert fields["_field_meta"]["factory_address"]["checks"]["in_source"]
    assert not fields["_field_meta"]["factory_address"]["checks"]["evidence"]
    assert "factory_address" not in fields


@pytest.mark.parametrize("key", ["phone", "factory_phone"])
@pytest.mark.parametrize("prefix", ["Tel.", f"Tel. {COMPANY}", "Tel:", "Telephone Number:",
                                    "Tel. Fairfax Mobilex AlphaHP Emaille Ltd."])
def test_international_phone_with_label_and_interleaved_company_is_accepted(key, prefix):
    quote = f"{prefix} {PHONE}"
    text = "Factory Registration Certificate\n" + quote
    assert intake._field_value_grounded(key, PHONE, text)
    fields = intake._checked_fields({key: PHONE}, {key: quote}, {}, text)
    assert fields[key] == PHONE
    assert fields["_field_meta"][key]["checks"]["role"]


@pytest.mark.parametrize("key", ["phone", "factory_phone"])
@pytest.mark.parametrize("label", ["", "Fax", "FAX", "fAcSiMiLe", "팩스", "전송", "Mobile",
                                   "HP", "H.P", "휴대", "E-mail", "Email"])
def test_phone_cannot_skip_another_contact_label(key, label):
    value = "+82-31-769-8763"
    text = "Tel. Livinglife Co.,Ltd. " + (label + ". " if label else "") + value
    accepted = not label
    assert intake._field_value_grounded(key, value, text) is accepted
    fields = intake._checked_fields({key: value}, {key: text}, {}, text)
    assert fields["_field_meta"][key]["checks"]["in_source"] is accepted
    assert fields["_field_meta"][key]["accepted"] is accepted
    assert fields.get(key) == (value if accepted else None)


@pytest.mark.parametrize("key", ["phone", "factory_phone"])
@pytest.mark.parametrize("gap,accepted", [("X" * 60, True), ("X" * 61, False),
                                         (" " * 60, True), (" " * 61, False)])
def test_phone_label_gap_is_limited_to_sixty_characters(key, gap, accepted):
    text = "Tel." + gap + PHONE
    assert intake._field_value_grounded(key, PHONE, text) is accepted
    fields = intake._checked_fields({key: PHONE}, {key: text}, {}, text)
    assert fields["_field_meta"][key]["accepted"] is accepted


@pytest.mark.parametrize("key", ["phone", "factory_phone"])
def test_phone_keeps_searching_after_rejecting_a_fax_candidate(key):
    quote = f"Tel. {COMPANY} {PHONE}"
    text = f"Tel. {COMPANY} Fax. {PHONE}\n{quote}"
    assert intake._field_value_grounded(key, PHONE, text)
    fields = intake._checked_fields({key: PHONE}, {key: quote}, {}, text)
    assert fields[key] == PHONE


@pytest.mark.parametrize("key", ["phone", "factory_phone"])
@pytest.mark.parametrize("agency", ["Issuing Authority", "Tax Office", "담당부서"])
def test_issuer_phone_is_still_rejected(key, agency):
    quote = f"Tel. {PHONE}"
    text = f"Factory Registration Certificate\n{agency}\n{quote}"
    assert not intake._field_value_grounded(key, PHONE, text)
    fields = intake._checked_fields({key: PHONE}, {key: quote}, {}, text)
    assert key not in fields
    assert not fields["_field_meta"][key]["checks"]["role"]
    assert "issuer_contact" in fields["_field_meta"][key]["reasons"]


@pytest.mark.parametrize("text", [
    PHONE, f"Tel. {COMPANY} {PHONE}9", f"Tel. Not provided\nFax: {PHONE}", f"Tel. Fax: {PHONE}",
])
def test_phone_still_needs_a_label_and_matching_number(text):
    assert not intake._field_value_grounded("factory_phone", PHONE, text)


def test_factory_fields_are_kept_without_deriving_employee_total():
    values = {"factory_reg_no": REG_NO, "factory_address": ADDRESS, "factory_phone": PHONE,
              "employee_count": 37}
    evidence = {"factory_reg_no": f"Factory management number :\n{REG_NO}",
                "factory_address": ADDRESS_QUOTE, "factory_phone": f"Tel. {COMPANY} {PHONE}",
                "employee_count": "No. of Employees Male: 21 Female: 16"}
    fields = intake._checked_fields(values, evidence, {}, TABLE_TEXT)
    for key in ("factory_reg_no", "factory_address", "factory_phone"):
        assert fields[key] == values[key]
        assert fields["_field_meta"][key]["accepted"]
    assert "employee_count" not in fields
    assert not intake._field_value_grounded("employee_count", 37, TABLE_TEXT)
    assert "employee_count" not in intake._checked_fields({"employee_count": None}, {}, {}, TABLE_TEXT)
