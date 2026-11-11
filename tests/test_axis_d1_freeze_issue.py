"""d1: 범위 동결, 검토자 증빙 승인, 발급 게이트의 TestClient 회귀."""
import base64
import io
import json
import time
import zipfile

import pytest
from fastapi.testclient import TestClient

from app import main as m, models
from app.db import SessionLocal


PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="


@pytest.fixture
def api(monkeypatch):
    monkeypatch.setenv("GLHAC_RATE_LIMIT_PER_MIN", "0")
    monkeypatch.setenv("GLHAC_TWO_PERSON", "0")
    monkeypatch.setattr(m, "_AUTOPARSE", False)
    monkeypatch.setattr(m, "_DOC_VERIFY", False)
    with TestClient(m.app) as client:
        headers = {}
        for role, username, password in (("applicant", "applicant1", "pw"),
                                         ("auditor", "auditor1", "pw"),
                                         ("operator", "operator1", "pw"),
                                         ("admin", "admin", "admin")):
            r = client.post("/auth/login", json={"username": username, "password": password})
            assert r.status_code == 200, r.text
            headers[role] = {"Authorization": "Bearer " + r.json()["token"]}

        def request(method, path, body=None, role="applicant"):
            return client.request(method, path, json=body, headers=headers[role])

        yield request


def ok(response):
    assert response.status_code == 200, response.text
    return response.json()


def blocked(response, code):
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == code, response.text


def case(api, name="sugar", linked=True):
    cid = ok(api("POST", "/cases", {"company_name": "D1 scope test"}))["case_id"]
    pid = ok(api("POST", f"/cases/{cid}/products", {"name": "Reviewed product"}))["product_id"]
    mid = ok(api("POST", f"/cases/{cid}/materials", {"name": name}))["material_id"]
    if linked:
        ok(api("POST", f"/cases/{cid}/matrix/link", {"product_id": pid, "material_id": mid, "linked": True}))
    return cid, pid, mid


def freeze(api, cid):
    with SessionLocal() as db:
        c = db.get(models.CaseApplication, cid)
        c.status, c.pathway, c.fatwa_status = "fatwa_review", "reguler", "provisional"
        db.add(models.FatwaDecision(case_id=cid, decision="approved"))
        db.add(models.Invoice(case_id=cid, amount=100, status="paid"))
        db.commit()
    ok(api("POST", f"/cases/{cid}/fatwa/final-approve", role="operator"))


def pending(cid, name="lard", structural=False):
    with SessionLocal() as db:
        d = models.DocumentAsset(case_id=cid, filename="materials.txt", doc_type="material_list",
                                 fields={"material_names": [name], "_sources": {
                                     "material_names": "structural" if structural else "llm"}})
        db.add(d)
        db.commit()
        return d.document_id


@pytest.mark.parametrize("operation", ["product_add", "product_patch", "product_delete", "product_force",
                                       "material_add", "material_patch", "material_rename", "material_delete",
                                       "link", "unlink", "auto_link", "reuse", "dedup", "pending"])
def test_frozen_scope_writes(api, operation):
    cid, pid, mid = case(api)
    pending(cid)
    freeze(api, cid)
    routes = {
        "product_add": ("POST", f"/cases/{cid}/products", {"name": "Unreviewed product"}),
        "product_patch": ("PATCH", f"/cases/{cid}/products/{pid}", {"description": "Changed"}),
        "product_delete": ("DELETE", f"/cases/{cid}/products/{pid}", None),
        "product_force": ("DELETE", f"/cases/{cid}/products/{pid}?force=true", None),
        "material_add": ("POST", f"/cases/{cid}/materials", {"name": "gelatin"}),
        "material_patch": ("PATCH", f"/materials/{mid}", {"supplier": "Changed"}),
        "material_rename": ("PATCH", f"/materials/{mid}/rename", {"name": "gelatin"}),
        "material_delete": ("DELETE", f"/materials/{mid}", None),
        "link": ("POST", f"/cases/{cid}/matrix/link", {"product_id": pid, "material_id": mid, "linked": True}),
        "unlink": ("POST", f"/cases/{cid}/matrix/link", {"product_id": pid, "material_id": mid, "linked": False}),
        "auto_link": ("POST", f"/cases/{cid}/auto-link-materials", None),
        "reuse": ("POST", f"/cases/{cid}/materials/reuse", {"names": ["sugar"]}),
        "dedup": ("POST", f"/cases/{cid}/materials/dedup", None),
        "pending": ("POST", f"/cases/{cid}/pending-extractions/apply", {"values": ["lard"]}),
    }
    blocked(api(*routes[operation]), "SCOPE_FROZEN")
    matrix = ok(api("GET", f"/cases/{cid}/matrix"))
    assert [p["name"] for p in matrix["products"]] == ["Reviewed product"]
    assert [v["name"] for v in matrix["materials"]] == ["sugar"]
    assert matrix["links"] == [[pid, mid]]


@pytest.mark.parametrize("change", ["product", "material", "link"])
def test_issue_rechecks_frozen_snapshot(api, change):
    cid, pid, mid = case(api)
    freeze(api, cid)
    with SessionLocal() as db:
        if change == "product":
            db.get(models.Product, pid).name = "Unreviewed product"
        elif change == "material":
            db.get(models.Material, mid).supplier = "Unreviewed supplier"
        else:
            db.query(models.ProductMaterial).filter_by(case_id=cid).delete()
        db.commit()
    blocked(api("POST", f"/cases/{cid}/certificate/issue", role="operator"), "SCOPE_CHANGED")


def test_high_risk_material_blocks_issue(api):
    cid, _, _ = case(api, "gelatin")
    freeze(api, cid)
    blocked(api("POST", f"/cases/{cid}/certificate/issue", role="operator"), "CRITICAL_MATERIAL_NO_EVIDENCE")


def test_applicant_evidence_flag_is_not_approval(api):
    cid, _, _ = case(api)
    result = ok(api("POST", f"/cases/{cid}/materials", {"name": "gelatin", "evidence_provided": True}))
    assert result["screen"]["result"] == "NEEDS_EVIDENCE"
    with SessionLocal() as db:
        assert not db.get(models.Material, result["material_id"]).evidence_provided


def test_evidence_submission_review_and_revocation(api):
    cid, _, mid = case(api, "gelatin")
    freeze(api, cid)
    result = ok(api("POST", f"/cases/{cid}/materials/{mid}/evidence", {
        "filename": "proof.png", "file_b64": PNG, "evidence_type": "source_declaration"}))
    assert result["screen_result"] == "NEEDS_EVIDENCE"
    assert not result["recleared"]
    did = ok(api("GET", f"/cases/{cid}/materials/{mid}/evidence"))[0]["document_id"]
    blocked(api("POST", f"/cases/{cid}/certificate/issue", role="operator"), "CRITICAL_MATERIAL_NO_EVIDENCE")
    assert api("PATCH", f"/documents/{did}/review", {"review_status": "approved"}).status_code == 403
    ok(api("PATCH", f"/documents/{did}/review", {"review_status": "approved"}, "auditor"))
    with SessionLocal() as db:
        assert db.get(models.Material, mid).evidence_provided
    ok(api("PATCH", f"/documents/{did}/review", {"review_status": "rejected"}, "auditor"))
    with SessionLocal() as db:
        assert not db.get(models.Material, mid).evidence_provided
    blocked(api("POST", f"/cases/{cid}/certificate/issue", role="operator"), "CRITICAL_MATERIAL_NO_EVIDENCE")


@pytest.mark.parametrize("name", ["lard", "pork gelatin", "gelatin"])
def test_pending_risk_blocks_issue(api, name):
    cid, _, _ = case(api)
    freeze(api, cid)
    pending(cid, name)
    blocked(api("POST", f"/cases/{cid}/certificate/issue", role="operator"), "PENDING_CRITICAL_MATERIAL")


@pytest.mark.parametrize("body", [{"renames": {"lard": "sugar"}}, {"dismiss": ["lard"]}])
def test_applicant_cannot_hide_pending_risk(api, body):
    cid, _, _ = case(api)
    pending(cid)
    response = api("POST", f"/cases/{cid}/pending-extractions/apply", body)
    assert response.status_code == 403, response.text
    assert ok(api("GET", f"/cases/{cid}/pending-extractions"))["count"] == 1


@pytest.mark.parametrize("phase", ["entry", "approval"])
def test_self_declare_rechecks_materials(api, phase):
    cid, _, _ = case(api, "gelatin")
    with SessionLocal() as db:
        c = db.get(models.CaseApplication, cid)
        c.pathway, c.risk_category, c.is_msme = "self_declare", "low", True
        c.status = "self_declaration_submitted" if phase == "entry" else "committee_verification"
        db.commit()
    route = "transition" if phase == "entry" else "committee/decide"
    body = {"to_state": "committee_verification"} if phase == "entry" else {"decision": "approve"}
    response = api("POST", f"/cases/{cid}/{route}", body, "operator")
    assert response.status_code == 409, response.text
    with SessionLocal() as db:
        assert db.get(models.CaseApplication, cid).fatwa_status != "approved"


def test_product_material_links_required(api):
    cid, _, _ = case(api, linked=False)
    freeze(api, cid)
    blocked(api("POST", f"/cases/{cid}/certificate/issue", role="operator"), "PRODUCT_MATERIAL_LINKS_MISSING")


def test_reviewed_scope_can_issue(api):
    cid, _, _ = case(api)
    freeze(api, cid)
    request = ok(api("POST", f"/cases/{cid}/certificate/issue", role="operator"))
    issued = ok(api("POST", f"/approvals/{request['approval_id']}/approve", {}, "admin"))
    assert issued["result"]["scope"] == ["Reviewed product"]


def material_table():
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.title = "Daftar Bahan Halal"
    ws.append(["Daftar Bahan Halal"])
    ws.append(["No", "Nama", "Jenis Bahan", "Produsen", "Negara", "Supplier"])
    ws.append([1, "Sugar", "BAHAN BAKU", "Maker", "INDONESIA", "Supplier"])
    ws.append([2, "Gelatin", "BAHAN BAKU", "Maker", "INDONESIA", "Supplier"])
    buf = io.BytesIO()
    wb.save(buf)
    wb.close()
    return buf.getvalue()


def names(cid):
    with SessionLocal() as db:
        return sorted(v.name for v in db.query(models.Material).filter_by(case_id=cid))


@pytest.mark.parametrize("route", ["autoparse", "reprocess", "intake-zip", "intake-zip-stream"])
def test_frozen_material_autoparse_cannot_add(api, monkeypatch, route):
    """보고서 재현: 실제 구조 XLSX 파서와 워커. 열린 범위 대조군으로 추출 성공도 확인."""
    raw = material_table()
    if route == "autoparse":
        monkeypatch.setattr(m, "_AUTOPARSE", True)
        m._autoparse_start()

    def extract(cid):
        if route.startswith("intake"):
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w") as z:
                z.writestr("Daftar bahan.xlsx", raw)
            response = api("POST", f"/cases/{cid}/{route}", {
                "zip_b64": base64.b64encode(buf.getvalue()).decode()})
            assert response.status_code == 200, response.text
            if route.endswith("stream"):
                events = [json.loads(v[6:]) for v in response.text.splitlines() if v.startswith("data: ")]
                assert events[-1]["type"] == "result", events
            return
        data = ok(api("POST", f"/cases/{cid}/documents", {
            "filename": "Daftar bahan.xlsx", "doc_type": "material_list",
            "file_b64": base64.b64encode(raw).decode()}))
        if route == "reprocess":
            ok(api("POST", f"/documents/{data['document_id']}/reprocess", role="operator"))
        else:
            assert data["autoparse_queued"]
            deadline = time.monotonic() + 15
            while m._autoparse_q.unfinished_tasks and time.monotonic() < deadline:
                time.sleep(0.02)
            assert not m._autoparse_q.unfinished_tasks
        with SessionLocal() as db:
            assert db.get(models.DocumentAsset, data["document_id"]).confidence is not None

    opened = ok(api("POST", "/cases", {"company_name": "Open extraction control"}))["case_id"]
    extract(opened)
    assert names(opened) == ["Gelatin", "Sugar"]
    cid, _, _ = case(api)
    freeze(api, cid)
    extract(cid)
    assert names(cid) == ["sugar"]
    items = ok(api("GET", f"/cases/{cid}/pending-extractions"))["items"]
    assert any(v["value"] == "Gelatin" and v["requires_scope_unlock"] for v in items), items
    blocked(api("POST", f"/cases/{cid}/certificate/issue", role="operator"), "PENDING_CRITICAL_MATERIAL")
    ok(api("POST", f"/cases/{cid}/certificate/unlock", {"reason": "원재료 범위 재검토"}, "operator"))
    items = ok(api("GET", f"/cases/{cid}/pending-extractions"))["items"]
    assert any(v["value"] == "Gelatin" for v in items)
    ok(api("POST", f"/cases/{cid}/pending-extractions/apply", {"values": ["Gelatin"]}))
    assert names(cid) == ["Gelatin", "sugar"]


@pytest.mark.parametrize("body", [{"renames": {"lard": "salt"}}, {"dismiss": ["lard"]}])
def test_reviewer_pending_correction_preserves_original_and_reason(api, body):
    cid, _, _ = case(api)
    did = pending(cid)
    assert api("POST", f"/cases/{cid}/pending-extractions/apply", body, "auditor").status_code == 422
    ok(api("POST", f"/cases/{cid}/pending-extractions/apply", {**body, "reason": "원문 OCR 오독 대조 확인"}, "auditor"))
    assert not ok(api("GET", f"/cases/{cid}/pending-extractions"))["items"]
    with SessionLocal() as db:
        assert db.get(models.DocumentAsset, did).fields["material_names"] == ["lard"]
        event = db.query(models.WorkflowEvent).filter_by(case_id=cid, action="extraction.review").one()
        assert event.actor_type == "auditor"
        assert event.payload["items"][0]["value"] == "lard"
        assert event.payload["reason"] == "원문 OCR 오독 대조 확인"


@pytest.mark.parametrize("change", ["scope", "pending", "evidence"])
def test_final_checker_revalidates_issue_request(api, change):
    cid, pid, mid = case(api, "gelatin" if change == "evidence" else "sugar")
    if change == "evidence":
        ok(api("POST", f"/cases/{cid}/materials/{mid}/evidence", {
            "filename": "source.png", "file_b64": PNG, "evidence_type": "source_declaration"}))
        did = ok(api("GET", f"/cases/{cid}/materials/{mid}/evidence"))[0]["document_id"]
        ok(api("PATCH", f"/documents/{did}/review", {"review_status": "approved"}, "auditor"))
    freeze(api, cid)
    request = ok(api("POST", f"/cases/{cid}/certificate/issue", role="operator"))
    if change == "scope":
        with SessionLocal() as db:
            db.get(models.Product, pid).name = "Unreviewed change"
            db.commit()
    elif change == "pending":
        pending(cid)
    else:
        ok(api("PATCH", f"/documents/{did}/review", {"review_status": "rejected"}, "auditor"))
    codes = {"scope": "SCOPE_CHANGED", "pending": "PENDING_CRITICAL_MATERIAL", "evidence": "CRITICAL_MATERIAL_NO_EVIDENCE"}
    blocked(api("POST", f"/approvals/{request['approval_id']}/approve", {}, "admin"), codes[change])
    with SessionLocal() as db:
        assert db.query(models.HalalCertificate).filter_by(case_id=cid, status="active").count() == 0


def test_legacy_frozen_scope_without_snapshot_fails_closed(api):
    cid, _, _ = case(api)
    freeze(api, cid)
    with SessionLocal() as db:
        db.query(models.WorkflowEvent).filter_by(case_id=cid, action="scope.freeze").delete()
        db.commit()
    blocked(api("POST", f"/cases/{cid}/certificate/issue", role="operator"), "SCOPE_SNAPSHOT_MISSING")


def test_old_self_reported_evidence_cannot_clear_risk(api):
    cid, _, mid = case(api, "gelatin")
    with SessionLocal() as db:
        material = db.get(models.Material, mid)
        material.evidence_provided, material.screen_result = True, "CLEARED"
        db.commit()
    freeze(api, cid)
    blocked(api("POST", f"/cases/{cid}/certificate/issue", role="operator"), "CRITICAL_MATERIAL_NO_EVIDENCE")


def test_logistics_without_product_material_matrix_can_issue(api):
    cid = ok(api("POST", "/cases", {"company_name": "D1 logistics", "scheme": "logistics",
                                       "logistics_scope": ["penyimpanan"]}))["case_id"]
    freeze(api, cid)
    request = ok(api("POST", f"/cases/{cid}/certificate/issue", role="operator"))
    issued = ok(api("POST", f"/approvals/{request['approval_id']}/approve", {}, "admin"))
    assert issued["result"]["scope"] == ["penyimpanan"]


def test_provenance_only_upload_cannot_resolve_after_review(api):
    cid, _, mid = case(api, "gelatin")
    ok(api("POST", f"/cases/{cid}/materials/{mid}/evidence", {
        "filename": "origin.png", "file_b64": PNG, "evidence_type": "source_declaration",
        "counts_as_evidence": False}))
    did = ok(api("GET", f"/cases/{cid}/materials/{mid}/evidence"))[0]["document_id"]
    ok(api("PATCH", f"/documents/{did}/review", {"review_status": "approved"}, "auditor"))
    freeze(api, cid)
    blocked(api("POST", f"/cases/{cid}/certificate/issue", role="operator"), "CRITICAL_MATERIAL_NO_EVIDENCE")


def test_evidence_approval_does_not_follow_changed_material(api):
    cid, _, mid = case(api, "gelatin")
    ok(api("POST", f"/cases/{cid}/materials/{mid}/evidence", {
        "filename": "source.png", "file_b64": PNG, "evidence_type": "source_declaration"}))
    did = ok(api("GET", f"/cases/{cid}/materials/{mid}/evidence"))[0]["document_id"]
    ok(api("PATCH", f"/documents/{did}/review", {"review_status": "approved"}, "auditor"))
    ok(api("PATCH", f"/materials/{mid}", {"supplier": "New unreviewed supplier"}))
    with SessionLocal() as db:
        assert not db.get(models.Material, mid).evidence_provided
    freeze(api, cid)
    blocked(api("POST", f"/cases/{cid}/certificate/issue", role="operator"), "CRITICAL_MATERIAL_NO_EVIDENCE")


def test_pending_handled_profile_is_not_reviewer_approval(api):
    cid, _, _ = case(api)
    pending(cid)
    with SessionLocal() as db:
        c = db.get(models.CaseApplication, cid)
        c.profile_ext = {"pending_handled": ["lard"]}
        db.commit()
    freeze(api, cid)
    blocked(api("POST", f"/cases/{cid}/certificate/issue", role="operator"), "PENDING_CRITICAL_MATERIAL")


def test_stale_autoparse_session_observes_new_freeze(api):
    cid, _, _ = case(api)
    with SessionLocal() as stale:
        c = stale.get(models.CaseApplication, cid)
        assert not c.scope_frozen
        freeze(api, cid)
        with pytest.raises(m.HTTPException) as exc:
            m._apply_agg_to_case(stale, c, {"products": ["Late product"], "materials": ["gelatin"]})
        assert exc.value.detail["code"] == "SCOPE_FROZEN"
    assert names(cid) == ["sugar"]


def test_final_approval_preserves_first_review_snapshot(api):
    cid, pid, _ = case(api)
    with SessionLocal() as db:
        c = db.get(models.CaseApplication, cid)
        m.sm.apply_side_effects(c, "final_package_preparation")
        db.commit()
        original = m.sm.frozen_scope(db, c)
        db.get(models.Product, pid).name = "Changed after package freeze"
        db.commit()
    freeze(api, cid)
    with SessionLocal() as db:
        c = db.get(models.CaseApplication, cid)
        assert m.sm.frozen_scope(db, c) == original
    blocked(api("POST", f"/cases/{cid}/certificate/issue", role="operator"), "SCOPE_CHANGED")


def test_safe_self_declare_entry_freezes_scope_and_can_issue(api):
    cid, _, _ = case(api)
    with SessionLocal() as db:
        c = db.get(models.CaseApplication, cid)
        c.pathway, c.status = "self_declare", "self_declaration_submitted"
        db.commit()
    ok(api("POST", f"/cases/{cid}/transition", {"to_state": "committee_verification"}, "operator"))
    blocked(api("POST", f"/cases/{cid}/materials", {"name": "gelatin"}), "SCOPE_FROZEN")
    ok(api("POST", f"/cases/{cid}/committee/decide", {"decision": "approve"}, "operator"))
    request = ok(api("POST", f"/cases/{cid}/certificate/issue", role="operator"))
    issued = ok(api("POST", f"/approvals/{request['approval_id']}/approve", {}, "admin"))
    assert issued["result"]["scope"] == ["Reviewed product"]


@pytest.mark.parametrize("phase", ["entry", "approval"])
def test_self_declare_pending_risk_is_rechecked(api, phase):
    cid, _, _ = case(api)
    with SessionLocal() as db:
        c = db.get(models.CaseApplication, cid)
        c.pathway, c.status = "self_declare", "self_declaration_submitted"
        db.commit()
    if phase == "approval":
        ok(api("POST", f"/cases/{cid}/transition", {"to_state": "committee_verification"}, "operator"))
    pending(cid, "pork gelatin")
    route = "transition" if phase == "entry" else "committee/decide"
    body = {"to_state": "committee_verification"} if phase == "entry" else {"decision": "approve"}
    response = api("POST", f"/cases/{cid}/{route}", body, "operator")
    assert response.status_code == 409, response.text
    assert "PENDING_CRITICAL_MATERIAL" in response.text


@pytest.mark.parametrize("endpoint", ["parse-file", "materials/from-label", "materials/from-label-b64"])
def test_manual_extractors_reject_frozen_scope_before_parsing(api, endpoint):
    cid, _, _ = case(api)
    freeze(api, cid)
    body = {"filename": "label.png", "file_b64": PNG, "image_b64": PNG,
            "image_path": "/private/tmp/d1-label.png", "doc_type": "material_list"}
    blocked(api("POST", f"/cases/{cid}/{endpoint}", body), "SCOPE_FROZEN")


def test_freeze_during_extraction_defers_before_first_write(api, monkeypatch):
    cid, _, _ = case(api)
    did = ok(api("POST", f"/cases/{cid}/documents", {
        "filename": "Daftar bahan.xlsx", "doc_type": "material_list",
        "file_b64": base64.b64encode(material_table()).decode()}))["document_id"]
    upsert = m._upsert_material
    frozen = False

    def freeze_before_write(db, material):
        nonlocal frozen
        if not frozen:
            frozen = True
            freeze(api, cid)
        return upsert(db, material)

    monkeypatch.setattr(m, "_upsert_material", freeze_before_write)
    result = ok(api("POST", f"/documents/{did}/reprocess", role="operator"))
    assert frozen and result["applied"]["requires_scope_unlock"]
    assert names(cid) == ["sugar"]
    assert any(v["value"] == "Gelatin" for v in ok(api("GET", f"/cases/{cid}/pending-extractions"))["items"])
