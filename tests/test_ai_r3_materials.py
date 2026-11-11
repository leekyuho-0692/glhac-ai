"""R3b 원재료 식별·부정 범위 회귀. 감사 자료는 실행 시 참조하지 않는다.

29개 경계의 출처: glhac-audit4-ai/results/new_edges.json (kind=screen).
실제 XLSX 파싱·HTTP 저장과 기존 DB 이관은 로컬/가짜 LLM만 사용한다.
"""
import base64
import io
import unicodedata
import zipfile

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import app.main as m
from app import intake, models, screening


AUDIT_BOUNDARIES = [
    ("Rum-free flavour", False),
    ("Lard-free vegetable shortening", False),
    ("Non-pork gelatin", False),
    ("Pork-free gelatin", False),
    ("Collard greens", False),
    ("Free from pork and lard", False),
    ("No pork or lard", False),
    ("Without pork gelatin", False),
    ("Does not contain pork", False),
    ("Pork is not present", False),
    ("돼지고기 무첨가", False),
    ("라드 불포함", False),
    ("bebas babi dan lemak babi", False),
    ("tanpa babi", False),
    ("Not pork-free", True),
    ("Not free from pork", True),
    ("Not lard-free shortening", True),
    ("Rum-free flavour with lard", True),
    ("Pork-free gelatin; contains lard", True),
    ("Free from pork; contains lard", True),
    ("Collard greens with lard", True),
    ("L a r d", True),
    ("P o r k", True),
    ("L.A.R.D", True),
    ("B4bi", True),
    ("Porkfat", True),
    ("Lard-free claim false: contains pork", True),
    ("Free from pork but contains pork", True),
    ("Pork-free. Lard added.", True),
]


@pytest.mark.parametrize("name,blocked", AUDIT_BOUNDARIES)
def test_all_29_audit_screening_boundaries(name, blocked):
    result = screening.screen_merged(name)
    assert (result["result"] == "BLOCK") is blocked, result
    assert (screening.screen_raw(name)["result"] == "BLOCK") is blocked
    assert bool(screening.screen_text(name)["haram_hits"]) is blocked
    if blocked:
        assert (result["status"], result["severity"]) == ("haram", "high")
    else:
        assert screening.analyze_text_risk(name) != "high" or "gelatin" in name.lower()


@pytest.mark.parametrize("name", [
    "not free from pork gelatin", "not without lard", "not non-pork gelatin",
    "tidak bebas babi", "tidak bebas dari babi", "bukan tanpa babi",
    "돼지고기가 없지 않은 원료", "돼지가 없지 않은 원료", "라드가 없지 않은 쇼트닝",
])
def test_double_negation_keeps_presence_in_all_screeners(name):
    assert screening.screen_merged(name)["result"] == "BLOCK"
    assert screening.screen_raw(name)["result"] == "BLOCK"
    assert screening.screen_text(name)["haram_hits"]
    assert screening.analyze_text_risk(name) == "high"


@pytest.mark.parametrize("connector", ["and", "or", "dan", "atau", "및", "또는"])
@pytest.mark.parametrize("template,blocked", [
    ("free from pork {} lard", False),
    ("free from pork {} contains lard", True),
    ("pork {} lard-free flavour", True),
    ("pork-free flavour {} lard", True),
    ("not sugar {} pork-free flavour", False),
])
def test_conjunctions_only_share_negation_between_adjacent_list_items(connector, template, blocked):
    name = template.format(connector)
    assert (screening.screen_merged(name)["result"] == "BLOCK") is blocked
    assert bool(screening.screen_text(name)["haram_hits"]) is blocked


@pytest.mark.parametrize("boundary", [",", ";", ":", ".", "!", "?", "\n", "\r\n", "(salt)"])
@pytest.mark.parametrize("template", ["free from {} pork", "pork {} free flavour", "not {} pork-free flavour"])
def test_negation_cannot_cross_clause_boundaries(boundary, template):
    name = template.format(boundary)
    blocked = not template.startswith("not")
    assert (screening.screen_merged(name)["result"] == "BLOCK") is blocked
    assert bool(screening.screen_text(name)["haram_hits"]) is blocked


@pytest.mark.parametrize("name", [
    "No lard", "without pork gelatin", "does not contain lard", "pork is not present",
    "tidak mengandung babi", "bebas dari babi", "돼지고기가 없는 원료", "라드가 없음",
])
def test_plain_negation_does_not_block(name):
    assert screening.screen_merged(name)["result"] != "BLOCK"
    assert not screening.screen_text(name)["haram_hits"]


@pytest.mark.parametrize("left,right,equal", [
    ("Water", "Water (pork extract)", False),
    ("Water (beef extract)", "Water (pork extract)", False),
    ("Gelatin (bovine)", "Gelatin (porcine)", False),
    ("Water", "Water (국내산)", False),
    ("Water (국내산)", "Water (중국산)", False),
    ("Water（국내산）", "Water（중국산）", False),
    ("Water（pork extract）", "Water (pork extract)", True),
    ("Water (pork extract)", " WA TER ( PORK-EXTRACT ) ", True),
    ("Water (pork (extract))", "water ( PORK ( EXTRACT ) )", True),
    ("Water (수입", "Water (국내산", False),
    ("젤라틴", unicodedata.normalize("NFD", "젤라틴"), True),
    ("Water (돼지 추출물)", unicodedata.normalize("NFD", "water (돼지-추출물)"), True),
])
def test_shared_identity_key_preserves_qualifiers_and_composes_unicode(left, right, equal):
    keys = [models.normalize_material_name(n) for n in (left, right)]
    assert (keys[0] == keys[1]) is equal
    assert all(unicodedata.is_normalized("NFC", key) for key in keys)
    assert [m._norm_material(n) for n in (left, right)] == keys


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(m, "_RL_WRITE_HITS", {})
    monkeypatch.setattr(m, "autoparse_enqueue", lambda *a, **k: False)
    monkeypatch.setattr(m, "verify_enqueue", lambda *a, **k: False)
    monkeypatch.setattr(intake.ai_local, "llm_json", lambda *a, **k: {})
    monkeypatch.setattr(intake, "_ZIP_LOOKUP", False)
    with TestClient(m.app) as client:
        yield client


@pytest.fixture
def case(client):
    login = client.post("/auth/login", json={"username": "applicant1", "password": "pw"})
    assert login.status_code == 200, login.text
    headers = {"Authorization": "Bearer " + login.json()["token"]}
    response = client.post("/cases", headers=headers, json={"company_name": "PT R3B MATERIALS"})
    assert response.status_code == 200, response.text
    return response.json()["case_id"], headers


MATERIAL_PAIRS = [
    ("Water", "Water (pork extract)"),
    ("Gelatin (bovine)", "Gelatin (porcine)"),
    ("젤라틴", unicodedata.normalize("NFD", "젤라틴")),
]


def _assert_saved_pair(cid, names):
    expected_keys = {models.normalize_material_name(name) for name in names}
    with m.SessionLocal() as db:
        rows = db.query(models.Material).filter_by(case_id=cid).all()
        assert len(rows) == len(expected_keys)
        assert {row.normalized_name for row in rows} == expected_keys
        for row in rows:
            assert row.screen_result == screening.screen_merged(row.name)["result"]
        return {row.name: row.material_id for row in rows}


def _xlsx(names):
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["Daftar Bahan Halal PT R3B MATERIALS"])
    sheet.append(["No", "Nama Bahan", "Jenis Bahan", "Produsen", "Negara", "No. Sertifikat Halal"])
    for i, name in enumerate(names, 1):
        sheet.append([i, name, "Bahan", "PT X", "ID", ""])
    output = io.BytesIO()
    workbook.save(output)
    workbook.close()
    return output.getvalue()


@pytest.mark.parametrize("route", ["parse-file", "intake-zip"])
@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("names", MATERIAL_PAIRS)
def test_actual_xlsx_uploads_keep_identity_and_risk_in_both_orders(client, case, route, reverse, names):
    cid, headers = case
    names = names[::-1] if reverse else names
    raw = _xlsx(names)
    if route == "intake-zip":
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as archive:
            archive.writestr("Daftar Bahan.xlsx", raw)
        body = {"zip_b64": base64.b64encode(output.getvalue()).decode()}
    else:
        body = {"doc_type": "material_list", "filename": "Daftar Bahan.xlsx",
                "file_b64": base64.b64encode(raw).decode()}
    for _ in range(2):
        response = client.post(f"/cases/{cid}/{route}", headers=headers, json=body)
        assert response.status_code == 200, response.text
        _assert_saved_pair(cid, names)


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("names", MATERIAL_PAIRS)
def test_manual_add_uses_same_identity_and_dedup_preserves_qualifiers(client, case, reverse, names):
    cid, headers = case
    names = names[::-1] if reverse else names
    equal = models.normalize_material_name(names[0]) == models.normalize_material_name(names[1])
    for i, name in enumerate(names):
        response = client.post(f"/cases/{cid}/materials", headers=headers, json={"name": name})
        assert response.status_code == (409 if equal and i else 200), response.text
    before = _assert_saved_pair(cid, names)
    response = client.post(f"/cases/{cid}/materials/dedup", headers=headers)
    assert response.status_code == 200, response.text
    assert _assert_saved_pair(cid, names) == before


@pytest.mark.parametrize("names", MATERIAL_PAIRS)
def test_rename_uses_same_identity_and_refreshes_screening(client, case, names):
    cid, headers = case
    first = client.post(f"/cases/{cid}/materials", headers=headers, json={"name": names[0]})
    other = client.post(f"/cases/{cid}/materials", headers=headers, json={"name": "Sugar"})
    assert first.status_code == other.status_code == 200
    equal = models.normalize_material_name(names[0]) == models.normalize_material_name(names[1])
    response = client.patch(f"/materials/{other.json()['material_id']}/rename",
                            headers=headers, json={"name": names[1]})
    assert response.status_code == (409 if equal else 200), response.text
    if equal:
        assert response.json()["detail"]["material_id"] == first.json()["material_id"]
        _assert_saved_pair(cid, [names[0], "Sugar"])
    else:
        _assert_saved_pair(cid, names)


@pytest.mark.parametrize("schema", ["no_column", "null_keys", "old_keys"])
def test_startup_migration_preserves_qualifiers_and_remaps_only_equivalent_unicode(tmp_path, monkeypatch, schema):
    engine = create_engine("sqlite:///" + str(tmp_path / "legacy.db"))
    models.Base.metadata.create_all(engine)
    names = [("water", "Water", "PASS"), ("pork", "Water (pork extract)", "BLOCK"),
             ("nfc", "젤라틴", "PASS"), ("nfd", unicodedata.normalize("NFD", "젤라틴"), "NEEDS_EVIDENCE")]
    with engine.begin() as conn:
        conn.exec_driver_sql("PRAGMA foreign_keys=ON")
        if schema == "no_column":
            conn.exec_driver_sql("DROP INDEX uq_material_case_name")
            conn.exec_driver_sql("ALTER TABLE material DROP COLUMN normalized_name")
        conn.execute(models.CaseApplication.__table__.insert().values(case_id="legacy", org_id="legacy"))
        for mid, name, result in names:
            conn.execute(text("INSERT INTO material (material_id,case_id,name,screen_result) "
                              "VALUES (:id,'legacy',:name,:result)"), {"id": mid, "name": name, "result": result})
        if schema == "old_keys":
            # 인덱스가 있어도 NFC/NFD 구키가 충돌하지 않아 두 행이 남았던 DB.
            for mid, name, _ in names:
                conn.execute(text("UPDATE material SET normalized_name=:key WHERE material_id=:id"),
                             {"id": mid, "key": {"water": None, "pork": "water"}.get(mid, name.lower())})
    with Session(engine) as db:
        db.add(models.Product(product_id="product", case_id="legacy", name="Product"))
        for mid, _, _ in names:
            db.add(models.DocumentAsset(document_id="doc-" + mid, case_id="legacy", filename=mid + ".pdf", material_id=mid))
        db.flush()
        for mid, _, _ in names:
            db.add(models.ProductMaterial(case_id="legacy", product_id="product", material_id=mid))
            db.add(models.MaterialMeasurement(case_id="legacy", material_id=mid, document_id="doc-" + mid,
                                               param_key="pork_dna", verdict="review"))
            db.add(models.MaterialEvidenceLink(case_id="legacy", material_id=mid, document_id="doc-" + mid, link_type="DIRECT"))
        db.add(models.HalalCertificate(case_id="legacy", frozen_material_ids=[mid for mid, _, _ in names]))
        db.commit()
    monkeypatch.setattr(m, "engine", engine)
    m._migrate()
    for _ in range(2):
        m._ensure_material_uniqueness()
        with Session(engine) as db:
            rows = {row.material_id: row for row in db.query(models.Material)}
            assert set(rows) == {"water", "pork", "nfd"}
            assert rows["water"].screen_result == "PASS"
            assert rows["pork"].screen_result == "BLOCK"
            assert rows["nfd"].screen_result == "NEEDS_EVIDENCE"
            assert all(row.normalized_name == models.normalize_material_name(row.name) for row in rows.values())
            assert {row.material_id for row in db.query(models.ProductMaterial)} == set(rows)
            for mid, _, _ in names:
                expected = "nfd" if mid == "nfc" else mid
                assert db.get(models.DocumentAsset, "doc-" + mid).material_id == expected
                assert db.query(models.MaterialMeasurement).filter_by(document_id="doc-" + mid).one().material_id == expected
                assert db.query(models.MaterialEvidenceLink).filter_by(document_id="doc-" + mid).one().material_id == expected
            assert db.query(models.HalalCertificate).one().frozen_material_ids == ["water", "pork", "nfd"]
            assert db.execute(text("PRAGMA foreign_key_check")).all() == []
    with Session(engine) as db:
        db.add(models.Material(case_id="legacy", name=" Water ( PORK-EXTRACT ) "))
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()
    assert any(i["name"] == "uq_material_case_name" and i["unique"] for i in inspect(engine).get_indexes("material"))
    engine.dispose()


def test_nonnull_old_key_is_rebuilt_before_later_safe_material_insert(tmp_path, monkeypatch):
    engine = create_engine("sqlite:///" + str(tmp_path / "nonnull.db"))
    models.Base.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(models.CaseApplication.__table__.insert().values(case_id="legacy", org_id="legacy"))
        conn.execute(text("INSERT INTO material (material_id,case_id,name,normalized_name,screen_result) "
                          "VALUES ('pork','legacy','Water (pork extract)','water','BLOCK')"))
    monkeypatch.setattr(m, "engine", engine)
    m._ensure_material_uniqueness()
    with Session(engine) as db:
        safe, created = m._upsert_material(db, models.Material(case_id="legacy", name="Water", screen_result="PASS"))
        assert created and safe.material_id != "pork"
        assert db.get(models.Material, "pork").normalized_name == "water(porkextract)"
        assert db.get(models.Material, "pork").screen_result == "BLOCK"
        db.commit()
    engine.dispose()
