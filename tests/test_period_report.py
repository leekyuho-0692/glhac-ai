"""SRS REPORT — 기간별 보고서·전기 비교·보고서 생성/PDF 흐름."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("GLHAC_DB_URL", "sqlite:///./glhac_report_test.db")
os.environ.setdefault("GLHAC_DEV", "1")
os.environ.setdefault("GLHAC_NOTIFY_WORKER", "0")
from datetime import date, timedelta  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from app.main import app                    # noqa: E402


TODAY = date.today().isoformat()


def _tok(c, u, p):
    r = c.post("/auth/login", json={"username": u, "password": p})
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["token"]}


def _mkcase(c, applicant, name):
    r = c.post("/cases", headers=applicant, json={"company_name": name, "sector": "food"})
    assert r.status_code == 200, r.text
    return r.json()["case_id"]


def _period(c, headers, g, d=TODAY):
    return c.get("/admin/reports/period", headers=headers, params={"granularity": g, "date": d})


def test_기간_라벨_6종():
    c = TestClient(app)
    op = _tok(c, "operator1", "pw")
    r = _period(c, op, "daily")
    assert r.status_code == 200, r.text
    assert r.json()["granularity"] == "daily"
    r = _period(c, op, "weekly")
    assert r.status_code == 200, r.text
    lab = r.json()["period"]["label"]
    assert " ~ " in lab
    r = _period(c, op, "monthly")
    assert r.status_code == 200, r.text
    p = r.json()["period"]
    assert p["label"] == TODAY[:7]
    assert p["in_progress"] is True
    assert r.json()["previous"]["label"] == (date.today().replace(day=1) - timedelta(days=1)).isoformat()[:7]
    r = _period(c, op, "quarterly")
    assert r.status_code == 200, r.text
    assert r.json()["period"]["label"] == "%s Q%s" % (TODAY[:4], (int(TODAY[5:7]) - 1) // 3 + 1)
    r = _period(c, op, "half")
    assert r.status_code == 200, r.text
    assert r.json()["period"]["label"] == "%s H%s" % (TODAY[:4], 1 if int(TODAY[5:7]) <= 6 else 2)
    r = _period(c, op, "yearly")
    assert r.status_code == 200, r.text
    assert r.json()["period"]["label"] == TODAY[:4]


def test_지표_전기비교_분야():
    c = TestClient(app)
    op = _tok(c, "operator1", "pw")
    ap = _tok(c, "applicant1", "pw")
    before = _period(c, op, "monthly").json()
    _mkcase(c, ap, "ReportCo1")
    after = _period(c, op, "monthly").json()
    assert after["metrics"]["new_cases"] >= before["metrics"]["new_cases"] + 1
    bfood = [s for s in before["sectors"] if s["label"] == "식품"][0]
    afood = [s for s in after["sectors"] if s["label"] == "식품"][0]
    assert afood["applied"] > bfood["applied"]
    m = after["metrics"]
    pm = after["previous_metrics"]
    assert after["delta"]["new_cases"] == m["new_cases"] - pm["new_cases"]
    assert len(after["in_progress_steps"]) == 8
    assert len(after["sectors"]) == 5


def test_입력_검증_권한():
    c = TestClient(app)
    op = _tok(c, "operator1", "pw")
    ap = _tok(c, "applicant1", "pw")
    r = _period(c, op, "x")
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["code"] == "BAD_GRANULARITY"
    r = _period(c, op, "monthly", "bad")
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["code"] == "BAD_DATE"
    r = _period(c, ap, "monthly")
    assert r.status_code == 403, r.text


def test_보고서_생성_번호_목록():
    c = TestClient(app)
    op = _tok(c, "operator1", "pw")
    r = c.post("/admin/reports", headers=op, json={"granularity": "monthly", "date": TODAY})
    assert r.status_code == 200, r.text
    r1 = r.json()
    assert r1["report_no"] == "RPT-%s-001" % TODAY[:4]
    assert r1["gen_doc_id"]
    assert r1["period"]
    assert r1["metrics"]
    r = c.post("/admin/reports", headers=op, json={"granularity": "monthly", "date": TODAY})
    assert r.status_code == 200, r.text
    assert r.json()["report_no"] == "RPT-%s-002" % TODAY[:4]
    r = c.get("/admin/reports", headers=op)
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    nos = [i["report_no"] for i in items]
    assert "RPT-%s-001" % TODAY[:4] in nos
    assert "RPT-%s-002" % TODAY[:4] in nos
    assert nos.index("RPT-%s-002" % TODAY[:4]) < nos.index("RPT-%s-001" % TODAY[:4])


def test_보고서_본문_pdf():
    c = TestClient(app)
    op = _tok(c, "operator1", "pw")
    made = c.post("/admin/reports", headers=op, json={"granularity": "monthly", "date": TODAY}).json()
    gid = made["gen_doc_id"]
    r = c.get("/admin/reports/%s" % gid, headers=op)
    assert r.status_code == 200, r.text
    body = r.json()
    assert "인증 현황 보고서" in body["content"]
    assert made["report_no"] in body["content"]
    r = c.get("/admin/reports/%s/pdf" % gid, headers=op)
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("application/pdf")
    assert r.content[:4] == b"%PDF"
    r = c.get("/admin/reports/NO_SUCH_ID", headers=op)
    assert r.status_code == 404, r.text
    assert r.json()["detail"]["code"] == "REPORT_NOT_FOUND"


def test_수치_고정():
    c = TestClient(app)
    op = _tok(c, "operator1", "pw")
    ap = _tok(c, "applicant1", "pw")
    r1 = c.post("/admin/reports", headers=op, json={"granularity": "monthly", "date": TODAY})
    assert r1.status_code == 200, r1.text
    gid = r1.json()["gen_doc_id"]
    fixed = r1.json()["metrics"]["new_cases"]
    _mkcase(c, ap, "ReportCo2")
    r = c.get("/admin/reports", headers=op)
    assert r.status_code == 200, r.text
    item = [i for i in r.json()["items"] if i["gen_doc_id"] == gid][0]
    assert item["metrics"]["new_cases"] == fixed
