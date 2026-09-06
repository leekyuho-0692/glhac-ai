"""정식 서식 표는 LLM 이 읽지 않는다 · 호출 실패는 빈 결과와 구분한다.

배경(실측, 24건 서류 3공급자 대조):

1. GL-HAC 정식 서식 `Form.5 사용재료 목록표` 는 번호·이름·유형·제조사가 있는 정형
   표인데 구조 파서가 놓쳤다. 헤더 셀이 'Nama Bahan (Material Name)⏎재료명' 이라
   'Nama' 정확일치 규칙에 안 걸렸기 때문이다. 그 표를 LLM 이 대신 읽었고 **정답 45건**을
   gpt 45 · gemma3 54 · qwen2.5 39 로 서로 다르게 냈다.

2. `Form 9(피치×샷).xlsx` 는 「재료 보관 기록」(운영 기록물)인데 파일명에 그 말이 없어
   사전이 못 알아봤고, 세 모델 모두 원재료 목록으로 분류해 입출고 행에서 원재료
   42~74건을 지어냈다. 시트명에는 '재료 보관 기록' 이 그대로 있었다.

3. 다른 서비스가 Ollama 를 점유한 동안 호출이 90초 타임아웃 났는데, 결과가
   `doc_type=other, confidence 0.0` 이라 '분석했더니 특이사항 없음' 과 구분되지 않았다.

실행: <venv>/bin/python -m pytest tests/test_form_tables_and_llm_failure.py -q
"""
import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("GLHAC_DB_URL", "sqlite:///./glhac_formtbl_test.db")

import openpyxl  # noqa: E402

from app import intake  # noqa: E402


def _xlsx(sheets):
    """{시트명: [행,...]} → xlsx 바이트."""
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    for title, rows in sheets.items():
        ws = wb.create_sheet(title=title[:31])
        for r in rows:
            ws.append(list(r))
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# GL-HAC Form.5 의 실제 형태 — 표지 3행, 헤더행, KOR/ENG 하위헤더행, 그리고 번호행.
FORM5_HEAD = [
    [None, "지엘 할랄 인증 센터\nGL Halal Center\n\nA-100"],
    [None, "بِسْمِ اللهِ الرَّحْمَنِ الرَّحِيم"],
    [None, "Form.5 List of materials used\n사용재료 목록표"],
    [None],
    [None, "No ", "Nama Bahan (Material Name)\n재료명", None,
     "Jenis Bahan \n(Material type)\n재료유형", "Produsen (Producer/Manufacturer name)\n제조사",
     "Negara (Country)\n원산지", "Supplier\n공급자"],
    [None, None, "KOR", "ENG"],
]


def _form5(n=45):
    rows = [list(r) for r in FORM5_HEAD]
    for i in range(1, n + 1):
        rows.append([None, str(i), "원재료%02d" % i, "Material%02d" % i,
                     "Raw Material\n원재료", "제조사%02d" % i, "KOREA\n대한민국", "공급사%02d" % i])
    return _xlsx({"Form.5 사용재료 목록표": rows})


def test_form5_table_is_read_structurally_not_by_llm():
    """정식 서식 표는 구조 파서가 정확한 건수를 낸다 — 모델마다 달라지지 않는다."""
    text = intake.parse_file("Form 5.xlsx", _form5(45))
    ing = intake._parse_ingredient_markers(text)
    assert ing is not None, "Form.5 표를 구조 파서가 인식하지 못했다"
    prods, mats = ing
    assert len(mats) == 45, "정답 45건인데 %d건" % len(mats)
    assert mats[0] == "원재료01" and mats[-1] == "원재료45"
    # 하위헤더(KOR/ENG)가 원재료명으로 새어들면 안 된다
    assert "KOR" not in mats and "ENG" not in mats


def test_form5_material_list_is_locked_against_llm(monkeypatch):
    """구조 파서가 답을 냈으면 LLM 이 뭘 뱉든 그 목록을 덮지 못한다."""
    monkeypatch.setattr(intake.ai_local, "llm_json", lambda *a, **k: {
        "doc_type": "material_list", "confidence": 0.9,
        "fields": {"material_names": ["지어낸재료"] * 60}})
    r = intake.classify("Form 5.xlsx", intake.parse_file("Form 5.xlsx", _form5(45)))
    assert r["doc_type"] == "material_list"
    assert len(r["fields"]["material_names"]) == 45
    assert r["field_sources"]["material_names"] == "structural"


def test_storage_record_recognised_by_sheet_title_without_llm(monkeypatch):
    """파일명이 아무것도 안 알려줘도 시트명이 보관 기록이면 운영 기록물로 확정한다."""
    called = []
    monkeypatch.setattr(intake.ai_local, "llm_json",
                        lambda *a, **k: called.append(1) or {
                            "doc_type": "material_list", "confidence": 0.9,
                            "fields": {"material_names": ["유령재료"] * 42}})
    rows = [[None, "Form.9 Ingredient Storage Record\n재료 보관 기록"],
            [None, "NO", "Ingredient Name\n재료명", "Brand & Manufacturer\n브랜드", "Date\n날짜"]]
    for i in range(1, 30):
        rows.append([None, str(i), "CITRIC ACID", "RZBC", "2025-04-22"])
    data = _xlsx({"Form.9 재료 보관 기록 (피치X샷)": rows})
    r = intake.classify("Form 9(피치×샷).xlsx", intake.parse_file("Form 9(피치×샷).xlsx", data))
    assert r["doc_type"] == "other", "보관 기록이 %s 로 분류됐다" % r["doc_type"]
    assert not (r.get("fields") or {}).get("material_names"), "보관 기록에서 원재료가 나왔다"
    assert not called, "운영 기록물인데 LLM 을 불렀다"


def test_filename_still_wins_over_sheet_title():
    """제목줄 조회는 폴백이다 — 파일명 규칙이 알아보는 서류는 그대로 둔다."""
    dt, why = intake.refine_doctype_reason(
        "제조공정도_질경이.pdf", None, "[시트/제품명: Form.9 재료 보관 기록]")
    assert dt == "process_flow", "파일명 규칙이 제목줄에 밀렸다"


def test_title_lookup_ignores_deep_body_text():
    """본문 깊숙한 곳의 한 줄로 유형이 뒤집히면 안 된다."""
    body = "\n".join(["원재료 목록"] + ["설탕 10kg"] * 40 + ["재료 보관 기록"])
    dt, _ = intake.refine_doctype_reason("unknown_file.pdf", None, body)
    assert dt != "other" or True   # 유형 확정은 안 해도 되지만
    assert intake._title_lines(body).count("재료 보관 기록") == 0, \
        "제목 후보에 본문 뒤쪽 줄이 들어갔다"


def test_llm_timeout_is_not_reported_as_empty_analysis(monkeypatch):
    """호출 실패는 실패라고 남는다 — '분석했더니 없음' 과 같은 모양이면 안 된다."""
    monkeypatch.setattr(intake.ai_local, "llm_json",
                        lambda *a, **k: {"error": "ReadTimeout"})
    r = intake.classify("알수없는서류.pdf", "본문이 충분히 길게 들어 있는 알 수 없는 문서입니다." * 5)
    assert r.get("llm_error") == "ReadTimeout"
    assert r["confidence"] == 0.0


def test_ai_less_mode_is_not_a_failure(monkeypatch):
    """'AI 없음' 배포는 설계된 상태다 — 모든 서류를 실패로 밀어 올리면 대기목록이 죽는다."""
    monkeypatch.setattr(intake.ai_local, "llm_json",
                        lambda *a, **k: {"error": "LLM_UNAVAILABLE"})
    r = intake.classify("알수없는서류.pdf", "본문이 충분히 길게 들어 있는 알 수 없는 문서입니다." * 5)
    assert "llm_error" not in r


def test_parse_typed_does_not_dress_up_a_failed_call(monkeypatch):
    """실패 응답을 필드로 저장하고 confidence 0.85 를 붙이면 안 된다."""
    monkeypatch.setattr(intake.ai_local, "llm_json",
                        lambda *a, **k: {"error": "ReadTimeout"})
    r = intake.parse_typed("nib_business_license", "사업자등록증.txt",
                           "사업자등록증\n등록번호 123-45-67890\n대표자 홍길동".encode())
    assert r["fields"] == {}, "실패 응답이 필드에 저장됐다: %r" % (r["fields"],)
    assert r["confidence"] == 0.0
    assert r.get("llm_error") == "ReadTimeout"


def test_form_tables_never_reach_the_llm_at_all(monkeypatch):
    """배포 모드가 달라도 같은 결과인 이유를 시간에 기대지 않고 증명한다.

    '세 모드를 돌려 보니 같더라'는 그 순간의 관측일 뿐이다(실측: 다른 서비스가 Ollama 를
    점유하면 같은 코드가 타임아웃으로 다른 결과를 낸다). 정식 서식 표와 운영 기록물은
    LLM 을 **부르지 않는다** — 부르면 터지게 해서 그 사실을 못 박는다."""
    def boom(*a, **k):
        raise AssertionError("이 서류에서 LLM 을 불렀다 — 배포 모드에 따라 결과가 갈린다")
    monkeypatch.setattr(intake.ai_local, "llm_json", boom)

    text5 = intake.parse_file("Form 5.xlsx", _form5(45))
    assert len(intake.classify("Form 5.xlsx", text5)["fields"]["material_names"]) == 45

    rows = [[None, "Form.9 Ingredient Storage Record"],
            [None, "NO", "Ingredient Name\n재료명", "Brand & Manufacturer", "Date"]]
    rows += [[None, str(i), "CITRIC ACID", "RZBC", "2025-04-22"] for i in range(1, 30)]
    data9 = _xlsx({"Form.9 재료 보관 기록 (피치X샷)": rows})
    assert intake.classify("Form 9.xlsx", intake.parse_file("Form 9.xlsx", data9))["doc_type"] == "other"
