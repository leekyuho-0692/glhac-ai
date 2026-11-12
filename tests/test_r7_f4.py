"""R7 f4: report N2/N7, real document persistence and the FORMAL SRS cycle.

Read-only evidence: glhac-final-fe/{REPORT_FINAL.md,scripts/source_probes.cjs}
and glhac-audit2-sec2/test_security_regression.py. No production DB or server.
"""
import base64
from datetime import datetime
import os
from pathlib import Path
import subprocess
import sys
from urllib.parse import quote

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("GLHAC_DEV", "1")
os.environ.setdefault("GLHAC_RATE_LIMIT_PER_MIN", "0")
os.environ.setdefault("GLHAC_NOTIFY_WORKER", "0")
os.environ.setdefault("GLHAC_AUTOPARSE", "0")
os.environ.setdefault("GLHAC_DOC_VERIFY", "0")

from fastapi.testclient import TestClient
from app.main import app
from app.db import SessionLocal
from app import models


def login(client, username="operator1", password="pw"):
    r = client.post("/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["token"]}


@pytest.fixture
def client():
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


@pytest.mark.parametrize("user", ["operator1", "admin"])
@pytest.mark.parametrize("granularity,day,expected", [
    ("daily", "2026-10-31", 1), ("daily", "2026-11-01", 1),
    ("weekly", "2026-11-01", 2), ("weekly", "2026-11-02", 1),
    ("monthly", "2026-10-31", 1), ("monthly", "2026-11-02", 2),
])
def test_n2_nonempty_report_buckets_and_saved_snapshot(client, user, granularity, day, expected):
    op = login(client, user, "admin" if user == "admin" else "pw")
    with SessionLocal() as db:
        # Each data source must contribute to the same calendar-day buckets.
        db.query(models.Payment).delete()
        db.query(models.HalalCertificate).delete()
        db.query(models.WorkflowEvent).delete()
        db.query(models.CaseApplication).delete()
        for i, stamp in enumerate(["2026-10-31T23:59:59.999999", "2026-11-01T00:00:00", "2026-11-02T12:34:56.123456"]):
            cid = f"f4-report-{i}"
            db.add(models.CaseApplication(case_id=cid, org_id="org_demo", company_name=cid,
                                          created_at=datetime.fromisoformat(stamp)))
            db.flush()
            db.add(models.WorkflowEvent(case_id=cid, action="contract.signed", created_at=datetime.fromisoformat(stamp)))
            db.add(models.Payment(case_id=cid, amount=120.5, status="confirmed", paid_at=datetime.fromisoformat(stamp)))
            db.add(models.HalalCertificate(case_id=cid, certificate_no=cid, issue_date=stamp[:10]))
        db.commit()
    params = {"granularity": granularity, "date": day}
    r = client.get("/admin/reports/period", params=params, headers=op)
    assert r.status_code == 200, r.text
    report = r.json()
    for key in ["new_cases", "contracts_signed", "certificates_issued"]:
        assert report["metrics"][key] == expected
        assert sum(row[key] for row in report["trend"]) == expected
    assert sum(row["paid_amount"] for row in report["trend"]) == expected * 120.5
    assert all(len(row["bucket"]) == (10 if granularity != "monthly" else 7) for row in report["trend"])
    made = client.post("/admin/reports", json=params, headers=op)
    assert made.status_code == 200, made.text
    fresh = login(client, user, "admin" if user == "admin" else "pw")
    read = client.get("/admin/reports/" + made.json()["gen_doc_id"], headers=fresh)
    assert read.status_code == 200 and made.json()["report_no"] in read.json()["content"]


@pytest.mark.parametrize("lang", ["en", "id"])
@pytest.mark.parametrize("event,body", [
    ("auditor.assignment_accepted", "월 원료 님이 심사를 수락했습니다."),
    ("auditor.assignment_rejected", "월 원료 님이 심사를 거절했습니다. 사유: 사전 증빙 원문 보존"),
])
def test_n7_event_translation_keeps_user_name_and_reason(client, lang, event, body):
    op = login(client)
    with SessionLocal() as db:
        n = models.Notification(org_id="org_demo", role="operator", event_type=event,
                                title="오디터 배정 수락" if event.endswith("accepted") else "오디터 배정 거절 — 재배정 필요",
                                body=body, channels=["inapp"])
        db.add(n)
        db.commit()
        nid = n.notification_id
    r = client.get("/notifications", params={"lang": lang}, headers=op)
    assert r.status_code == 200, r.text
    row = next(n for n in r.json() if n["id"] == nid)
    assert "월 원료" in row["body"]
    assert "님이 심사를" not in row["body"]
    assert "오디터 배정" not in row["title"]
    if event.endswith("rejected"):
        assert "사전 증빙 원문 보존" in row["body"]
    with SessionLocal() as db:
        assert db.get(models.Notification, nid).body == body


@pytest.mark.parametrize("filename", ["process-flow.txt", "process 'single' & \"double\".txt"])
def test_n1_evidence_upload_download_and_relogin(client, filename):
    ap = login(client, "applicant1")
    cid = client.post("/cases", headers=ap, json={"company_name": "f4 증빙 보존", "sector": "food"}).json()["case_id"]
    payload = b"process flow: receiving -> packaging\n"
    r = client.post(f"/cases/{cid}/sjph-evidence", headers=ap,
                    json={"item_key": "production_flow", "filename": filename, "file_b64": base64.b64encode(payload).decode()})
    assert r.status_code == 200, r.text
    ap = login(client, "applicant1")
    listing = client.get(f"/cases/{cid}/sjph-evidence", headers=ap)
    assert listing.status_code == 200, listing.text
    item = next(i for i in listing.json()["items"] if i["item_key"] == "production_flow")
    assert item["uploaded"] and item["filename"] == filename
    got = client.get("/documents/" + item["document_id"] + "/file", headers=ap)
    assert got.status_code == 200, got.text
    assert got.content == payload
    assert quote(filename) in got.headers["content-disposition"] or filename in got.headers["content-disposition"]


def test_sjph_save_rework_and_relogin(client):
    """Release the report's reserved storage-validation item with real roles."""
    ap = login(client, "applicant1")
    cid = client.post("/cases", headers=ap, json={"company_name": "f4 저장 확인", "sector": "food"}).json()["case_id"]
    consultant = login(client, "consultant1")
    for status, note in [("gap", "증빙 보완 필요"), ("ok", "보완 서류 확인 완료")]:
        saved = client.patch(f"/cases/{cid}/sjph", headers=consultant,
                             json={"element": "process", "status": status, "note": note})
        assert saved.status_code == 200, saved.text
        assert client.post("/auth/logout", headers=consultant).status_code == 200
        consultant = login(client, "consultant1")
        for reader in [consultant, ap]:
            r = client.get(f"/cases/{cid}/sjph", headers=reader)
            assert r.status_code == 200, r.text
            row = next(e for e in r.json()["elements"] if e["element"] == "process")
            assert (row["status"], row["note"]) == (status, note)


@pytest.mark.parametrize("pathway,expected", [("undetermined", 400), ("reguler", 400), ("self_declare", 200)])
def test_n5_actual_server_preview_pathway_contract(client, pathway, expected):
    ap = login(client, "applicant1")
    cid = client.post("/cases", headers=ap, json={"company_name": "f4 경로 " + pathway, "sector": "food"}).json()["case_id"]
    with SessionLocal() as db:
        db.get(models.CaseApplication, cid).pathway = pathway
        db.commit()
    for suffix in ["self-declaration/preview", "self-declaration.pdf"]:
        r = client.get(f"/cases/{cid}/{suffix}", headers=ap)
        assert r.status_code == expected, r.text
        if expected == 400:
            assert r.json()["detail"]["code"] == "NOT_SELF_DECLARE"


def test_frontend_regressions():
    r = subprocess.run(["node", "--test", str(ROOT / "tests/test_r7_f4_ui.cjs")],
                       cwd=ROOT, text=True, capture_output=True)
    assert r.returncode == 0, r.stdout + r.stderr


@pytest.mark.parametrize("lang", ["en", "id"])
def test_l1_package_notice_translates_system_text_only(client, lang):
    op = login(client)
    with SessionLocal() as db:
        n = models.Notification(org_id="org_demo", role="operator", event_type="package.gap",
                                title="[원료 월 회사] 인증 패키지 확인 필요 — 성분(원재료) 누락 · 미판독 서류 2건(판독 전 — 재처리 필요)",
                                body="업체·제품·성분·공장은 한 묶음입니다. 올라온 서류에서 찾지 못했거나 판독되지 않은 항목이 있어 수기 확인·입력이 필요합니다.")
        db.add(n)
        db.commit()
        nid = n.notification_id
    for url in ["/notifications", "/admin/notifications"]:
        r = client.get(url, params={"lang": lang}, headers=op)
        assert r.status_code == 200, r.text
        data = r.json()
        row = next(n for n in (data["items"] if isinstance(data, dict) else data) if n["id"] == nid)
        assert row["title"].startswith("[원료 월 회사]")
        assert not __import__("re").search("[가-힣]", row["title"].split("]", 1)[1])
        assert not __import__("re").search("[가-힣]", row.get("body", ""))


def test_formal_srs_cycle_and_relogin_persistence(tmp_path):
    """CI's unmodified SRS script over TestClient when TCP binding is unavailable.

    Only the HTTP transport is adapted. Business handlers, auth, DB, transitions,
    document generation and all SRS assertions run unchanged in a fresh process.
    """
    env = dict(os.environ, GLHAC_FORMAL_FLOW="1", GLHAC_E2E_FORMAL="1",
               GLHAC_E2E_SKIP_OLLAMA="1", GLHAC_E2E_BASE="http://127.0.0.1:8897",
               GLHAC_DB_URL="sqlite:///" + str(tmp_path / "formal.db"))
    r = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--formal-testclient"],
                       cwd=ROOT, env=env, text=True, capture_output=True, timeout=180)
    (tmp_path / "formal.log").write_text(r.stdout + r.stderr)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "FAIL 0" in r.stdout


def _formal_testclient():
    import contextlib
    import runpy
    from unittest.mock import patch
    from urllib.parse import urlsplit

    sys.path.insert(0, str(ROOT / "tests"))
    with TestClient(app) as c, contextlib.ExitStack() as stack:
        for method in ["get", "post", "put", "patch", "delete"]:
            def request(url, _method=method, **kwargs):
                target = urlsplit(url)
                assert target.netloc == "127.0.0.1:8897", "Unexpected external test request"
                return getattr(c, _method)(target.path + ("?" + target.query if target.query else ""), **kwargs)
            stack.enter_context(patch("httpx." + method, request))
        stop = stack.enter_context(patch("sys.exit"))
        result = runpy.run_path(str(ROOT / "tests/e2e_srs_cycle.py"), run_name="__main__")
        assert not result["F"], result["F"]
        assert stop.call_args.args[0] == 0
        cid = result["cid"]
        for user, password in [("applicant1", "pw"), ("consultant1", "pw"),
                               ("auditor1", "pw"), ("operator1", "pw"), ("admin", "admin")]:
            headers = login(c, user, password)
            assert c.post("/auth/logout", headers=headers).status_code == 200
            headers = login(c, user, password)
            for suffix in ["", "/gen-docs"]:
                r = c.get(f"/cases/{cid}{suffix}", headers=headers)
                assert r.status_code == 200, (user, suffix, r.text)
                if suffix:
                    assert r.json(), "Generated artifacts must survive re-login"
        print("Persistence: 5 roles re-login / case and generated documents PASS")


if __name__ == "__main__" and "--formal-testclient" in sys.argv:
    _formal_testclient()
