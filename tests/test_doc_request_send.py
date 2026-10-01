"""P33 요청서 발송(POST .../document-requests/send) — 요청 이력·스냅샷을 남기고 알림을 큐에 넣는다.
NOTIFY_MSG 중복키도 함께 막는다(dict 리터럴은 중복을 조용히 병합해 문구가 사라진다).

실행: <venv>/bin/python -m pytest tests/test_doc_request_send.py -q
"""
import ast
import collections
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("GLHAC_DB_URL", "sqlite:///./glhac_docsend_test.db")

from fastapi.testclient import TestClient   # noqa: E402

from app.main import app, NOTIFY_MSG        # noqa: E402

_MAIN = os.path.join(os.path.dirname(os.path.dirname(__file__)), "app", "main.py")


def _tok(c, u="applicant1", p="pw"):
    r = c.post("/auth/login", json={"username": u, "password": p})
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["token"]}


def _case(c, h):
    return c.post("/cases", json={"company_name": "PT Bahan Uji"}, headers=h).json()["case_id"]


def _add(c, cid, h, name):
    r = c.post(f"/cases/{cid}/materials", json={"name": name}, headers=h)
    assert r.status_code in (200, 201), r.text


def test_NOTIFY_MSG_중복키가_없다():
    """문구 카탈로그에 중복 키가 없어야 한다."""
    tree = ast.parse(open(_MAIN, encoding="utf-8").read())
    found = False
    keys = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "NOTIFY_MSG" for t in node.targets):
            found = True
            keys = [k.value for k in node.value.keys
                    if isinstance(k, ast.Constant) and isinstance(k.value, str)]
    assert found, "NOTIFY_MSG 할당을 찾지 못했다"
    # 같은 키를 두 번 쓰면 뒤의 것이 이겨 앞 문구가 조용히 사라진다(실측: document_requested).
    dup = [k for k, n in collections.Counter(keys).items() if n > 1]
    assert not dup, "중복 키: %s" % dup


def test_모든_문구가_3언어를_갖는다():
    """모든 알림 문구는 ko/en/id 3언어를 갖는다 — 인니어 화면에 한글이 남지 않게."""
    for key, val in NOTIFY_MSG.items():
        for lang in ("ko", "en", "id"):
            assert lang in val, "%s: %s 누락" % (key, lang)
            t = val[lang]
            assert isinstance(t, (tuple, list)) and len(t) == 2, "%s/%s: 2원소 아님" % (key, lang)
            assert isinstance(t[0], str) and t[0], "%s/%s: 제목이 빈 문자열" % (key, lang)


def test_부족서류가_없으면_발송을_거부한다():
    """부족한 게 없는데 서류를 요구하면 고객 신뢰를 깎는다 — 400 으로 막는다."""
    with TestClient(app) as c:
        ha = _tok(c, "applicant1")
        cid = _case(c, ha)
        r = c.post(f"/cases/{cid}/document-requests/send", headers=_tok(c, "auditor1"))
        assert r.status_code == 400, r.text
        assert "NO_MISSING_EVIDENCE" in r.text


def test_발송하면_이력과_알림이_생긴다():
    """요청 이력과 알림이 함께 생긴다 — 감사에서 '이 서류를 요구했다'를 증명해야 한다."""
    with TestClient(app) as c:
        ha = _tok(c, "applicant1")
        cid = _case(c, ha)
        _add(c, cid, ha, "Gelatin")
        r = c.post(f"/cases/{cid}/document-requests/send", headers=_tok(c, "auditor1"))
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["request_id"]
        assert d["notification_id"]
        assert d["request_count"] >= 1
        assert d["material_count"] >= 1
        assert isinstance(d["codes"], list) and d["codes"]
        assert d["duplicate_of"] is None


def test_같은_내용_재요청은_막지_않고_경고만_한다():
    """재요청은 정당한 독촉일 수 있다 — 막지 않고 이력에 둘 다 남긴다."""
    with TestClient(app) as c:
        ha = _tok(c, "applicant1")
        cid = _case(c, ha)
        _add(c, cid, ha, "Gelatin")
        h = _tok(c, "auditor1")
        r1 = c.post(f"/cases/{cid}/document-requests/send", headers=h)
        r2 = c.post(f"/cases/{cid}/document-requests/send", headers=h)
        assert r1.status_code == 200, r1.text
        assert r2.status_code == 200, r2.text
        assert r1.json()["request_id"] != r2.json()["request_id"]
        assert r2.json()["duplicate_of"] == r1.json()["request_id"]


def test_언어를_지정하면_그_언어로_이력이_남는다():
    """고객이 받은 문구 그대로를 이력에 남긴다."""
    with TestClient(app) as c:
        ha = _tok(c, "applicant1")
        cid = _case(c, ha)
        _add(c, cid, ha, "Gelatin")
        r = c.post(f"/cases/{cid}/document-requests/send",
                   params={"lang": "id"}, headers=_tok(c, "auditor1"))
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["lang"] == "id"
        assert d["sheet"]["lang"] == "id"


def test_신청자는_발송할_수_없다():
    """클라이언트가 자기에게 서류를 요구하는 것은 무의미하다 — 역할로 막는다."""
    with TestClient(app) as c:
        ha = _tok(c, "applicant1")
        cid = _case(c, ha)
        _add(c, cid, ha, "Gelatin")
        r = c.post(f"/cases/{cid}/document-requests/send", headers=ha)
        assert r.status_code in (401, 403), r.text
