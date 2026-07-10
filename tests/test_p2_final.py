"""P2-7 재제출 이전↔새 제출본 비교 — 격리 테스트.

스키마 무변경 검증: resubmit-center 응답의 preassess 항목에 compare(prev_docs/new_docs/
diff/counts)를 기존 DocumentAsset·WorkflowEvent 조회만으로 부가한다(신규 컬럼·마이그레이션 없음).

경계 = 최신 preassess.doc_request(오디터 보완요청) 시각. 그 이전 업로드=이전 제출본,
이후 업로드=새 제출본. doc_type별 최신본을 비교해 신규/변경/재제출/동일/미재제출 판정.

고유 DB(glhac_p2f_test.db) · GLHAC_DEV=1 · TestClient(서버 불필요).
실행: <venv>/bin/python -m pytest tests/test_p2_final.py -q
검증: (a) doc_request 경계 전/후 분리 + diff 상태 집계
      (b) 파일해시로 변경/동일 구분(mock_evidence_ 해시 기록 활용)
      (c) 조직격리 — 타 조직 케이스 resubmit-center 403.
"""
import base64
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ["GLHAC_DB_URL"] = "sqlite:///./glhac_p2f_test.db"   # 격리 강제
os.environ["GLHAC_DEV"] = "1"                                    # 데모 계정 시드

_DBFILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "glhac_p2f_test.db")
if os.path.exists(_DBFILE):
    os.remove(_DBFILE)

from fastapi.testclient import TestClient  # noqa: E402
from app.main import app  # noqa: E402
from app import models  # noqa: E402
from app.db import SessionLocal  # noqa: E402


def _tok(c, u, p):
    r = c.post("/auth/login", json={"username": u, "password": p})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def _h(tok):
    return {"Authorization": "Bearer " + tok}


def _b64(s):
    return base64.b64encode(s.encode("utf-8")).decode("ascii")


def _mk_case(org_id="org_demo", company="PT DiffCo", status="document_pre_audit_requested"):
    db = SessionLocal()
    try:
        c = models.CaseApplication(org_id=org_id, company_name=company, status=status)
        db.add(c)
        db.commit()
        return c.case_id
    finally:
        db.close()


def _upload(c, tok, cid, filename, doc_type, content):
    # 확장자 없는 파일명 → 매직바이트 검증 우회(임의 base64 허용)
    r = c.post("/cases/%s/documents" % cid,
               json={"filename": filename, "file_b64": _b64(content), "doc_type": doc_type},
               headers=_h(tok))
    assert r.status_code == 200, r.text
    return r.json()["document_id"]


def _compare(c, tok, cid):
    r = c.get("/cases/%s/resubmit-center" % cid, headers=_h(tok))
    assert r.status_code == 200, r.text
    pre = next((x for x in r.json()["requests"] if x["source"] == "preassess"), None)
    assert pre is not None, "preassess 항목 없음: %s" % r.text
    return pre.get("compare")


def test_a_prev_new_split_and_diff():
    """경계(보완요청) 전/후 업로드 분리 + 신규/미재제출/재제출/변경/동일 집계."""
    with TestClient(app) as c:
        cid = _mk_case()
        cli = _tok(c, "applicant1", "pw")
        aud = _tok(c, "auditor1", "pw")

        # 경계 이전(이전 제출본)
        _upload(c, cli, cid, "manual_v1", "sjph_manual", "AAAA")            # 재제출(해시無)
        _upload(c, cli, cid, "old_doc", "old_only", "OLD")                  # 미재제출 예정
        _upload(c, cli, cid, "kitchen1", "mock_evidence_kitchen", "IMGSAME")  # 동일(해시)
        _upload(c, cli, cid, "line1", "mock_evidence_line", "IMG_A")        # 변경(해시)
        time.sleep(0.03)

        # 경계 = 오디터 보완요청
        rq = c.post("/cases/%s/preassess/doc-request" % cid,
                    json={"items": [{"doc_type": "sjph_manual", "note": "재제출 요망"}],
                          "message": "서류 보완 바랍니다"}, headers=_h(aud))
        assert rq.status_code == 200, rq.text
        time.sleep(0.03)

        # 경계 이후(새 제출본)
        _upload(c, cli, cid, "manual_v2", "sjph_manual", "BBBB")            # 재제출(해시無)
        _upload(c, cli, cid, "new_doc", "new_only", "NEW")                  # 신규
        _upload(c, cli, cid, "kitchen2", "mock_evidence_kitchen", "IMGSAME")  # 동일(같은 내용)
        _upload(c, cli, cid, "line2", "mock_evidence_line", "IMG_B")        # 변경(다른 내용)

        cmp = _compare(c, cli, cid)
        assert cmp is not None, "compare 부재"
        assert cmp["has_new"] is True
        by = {d["doc_type"]: d["status"] for d in cmp["diff"]}
        assert by.get("new_only") == "added", by
        assert by.get("old_only") == "missing", by
        assert by.get("sjph_manual") == "resubmitted", by            # 해시 미기록 → 재제출
        assert by.get("mock_evidence_kitchen") == "unchanged", by    # 동일 해시
        assert by.get("mock_evidence_line") == "changed", by         # 상이 해시
        cnt = cmp["counts"]
        assert cnt["added"] == 1 and cnt["missing"] == 1, cnt
        assert cnt["resubmitted"] == 1 and cnt["unchanged"] == 1 and cnt["changed"] == 1, cnt
        # prev/new 최신본 파일명 확인(latest-wins)
        new_types = {d["doc_type"]: d["filename"] for d in cmp["new_docs"]}
        assert new_types.get("sjph_manual") == "manual_v2", new_types
        prev_types = {d["doc_type"]: d["filename"] for d in cmp["prev_docs"]}
        assert prev_types.get("sjph_manual") == "manual_v1", prev_types


def test_b_no_boundary_no_compare():
    """보완요청(경계) 자체가 없으면 preassess 항목이 없거나 compare=None(정직)."""
    with TestClient(app) as c:
        cid = _mk_case(company="PT NoBoundary")
        cli = _tok(c, "applicant1", "pw")
        _upload(c, cli, cid, "solo", "sjph_manual", "AAAA")
        r = c.get("/cases/%s/resubmit-center" % cid, headers=_h(cli))
        assert r.status_code == 200, r.text
        pre = next((x for x in r.json()["requests"] if x["source"] == "preassess"), None)
        # doc_request 이벤트가 없으므로 preassess 요청 항목 자체가 집약되지 않는다
        assert pre is None, "경계 없는데 preassess 항목이 생김: %s" % r.text


def test_c_org_isolation_403():
    """타 조직 케이스의 resubmit-center는 403(조직격리)."""
    with TestClient(app) as c:
        other = _mk_case(org_id="org_other", company="PT Other")
        cli = _tok(c, "applicant1", "pw")   # org_demo 사용자
        r = c.get("/cases/%s/resubmit-center" % other, headers=_h(cli))
        assert r.status_code == 403, r.text
