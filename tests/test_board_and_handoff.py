"""홈페이지 연동 — 도메인 간 로그인 인계 · 영업자 QR · 익명 비공개 게시판.

## 왜 인계가 필요한가

glhac.com(홈페이지)과 glhac.co.kr(AI 시스템)은 **서로 다른 도메인**이라 쿠키·localStorage 가
공유되지 않는다. 홈페이지에서 로그인해도 AI 시스템은 그 사실을 모른다.
그래서 한 번만 쓰이고 곧 만료되는 인계코드를 넘긴다 — 토큰을 URL 에 실으면 주소창·
브라우저 기록·Referer 에 남아 흘러나간다.

## 게시판 규칙

가입을 요구하면 문의가 줄고, 공개로 두면 어느 업체가 무슨 원료로 고민 중인지 경쟁사에
그대로 보인다. 그래서 **무가입 + 전부 비공개**다. 볼 수 있는 사람은 비밀번호를 아는
글쓴이와 로그인한 직원뿐이다.

실행: <venv>/bin/python -m pytest tests/test_board_and_handoff.py -q
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("GLHAC_DB_URL", "sqlite:///./glhac_board_test.db")
os.environ.setdefault("GLHAC_DEV", "1")
# 도배 차단은 여기서 검증 대상이 아니다(전용 테스트를 따로 둔다). 켜 두면 네 번째
# 글부터 429 가 나서 뒤 테스트가 줄줄이 무너진다 — 같은 IP 로 도니까.
os.environ["GLHAC_BOARD_RATE_MAX"] = "0"

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402


def _tok(c, u="consultant1", p="pw"):
    return c.post("/auth/login", json={"username": u, "password": p}).json()["token"]


def _post(c, **kw):
    body = {"title": "할랄 인증 문의", "body": "저희 제품 인증 가능한지 문의드립니다",
            "author_name": "김대리", "contact": "010-1234-5678", "password": "glhac2026"}
    body.update(kw)
    return c.post("/board/posts", json=body)


# ── 도메인 간 인계 ───────────────────────────────────────────────────────
def test_handoff_login_never_returns_a_token():
    """홈페이지 자바스크립트에 토큰이 닿지 않아야 한다 — 인계코드만 준다."""
    with TestClient(app) as c:
        r = c.post("/auth/handoff/login",
                   json={"username": "consultant1", "password": "pw"})
        assert r.status_code == 200
        d = r.json()
        assert "token" not in d and "refresh_token" not in d
        assert d["handoff"] and d["redirect"].endswith(d["handoff"])


def test_handoff_code_works_once():
    """도청·재생 공격을 막는다 — 한 번 쓰면 죽는다."""
    with TestClient(app) as c:
        code = c.post("/auth/handoff/login",
                      json={"username": "consultant1", "password": "pw"}).json()["handoff"]
        first = c.post("/auth/handoff/exchange", json={"handoff": code})
        assert first.status_code == 200 and first.json()["token"]
        assert first.json()["username"] == "consultant1"
        again = c.post("/auth/handoff/exchange", json={"handoff": code})
        assert again.status_code == 401


def test_handoff_rejects_unknown_code():
    with TestClient(app) as c:
        assert c.post("/auth/handoff/exchange", json={"handoff": "아무거나"}).status_code == 401


def test_logged_in_user_can_issue_handoff():
    """게시판 때문에 홈페이지도 로그인 상태를 갖는다 — 다시 로그인시키지 않는다."""
    with TestClient(app) as c:
        h = {"Authorization": "Bearer " + _tok(c)}
        code = c.post("/auth/handoff/issue", headers=h).json()["handoff"]
        assert c.post("/auth/handoff/exchange", json={"handoff": code}).json()["token"]


# ── 영업자 QR ───────────────────────────────────────────────────────────
def test_consultant_qr_code_is_stable():
    """명함에 박는 코드라 매번 달라지면 안 된다."""
    with TestClient(app) as c:
        h = {"Authorization": "Bearer " + _tok(c)}
        a = c.get("/consultant/qr/info", headers=h).json()
        b = c.get("/consultant/qr/info", headers=h).json()
        assert a["code"] == b["code"]
        assert a["url"].endswith("?ref=" + a["code"])


def test_qr_image_renders():
    with TestClient(app) as c:
        h = {"Authorization": "Bearer " + _tok(c)}
        png = c.get("/consultant/qr?fmt=png", headers=h)
        assert png.status_code == 200 and png.content[:4] == b"\x89PNG"
        svg = c.get("/consultant/qr?fmt=svg", headers=h)
        assert svg.status_code == 200 and b"<svg" in svg.content


# ── 게시판: 비공개가 핵심이다 ────────────────────────────────────────────
def test_anonymous_can_post_without_account():
    with TestClient(app) as c:
        r = _post(c)
        assert r.status_code == 200 and r.json()["post_id"]


def test_posts_are_invisible_without_login():
    """목록도 상세도 직원만 본다 — 경쟁사가 남의 문의를 읽으면 안 된다."""
    with TestClient(app) as c:
        pid = _post(c, title="비공개확인").json()["post_id"]
        assert c.get("/board/posts").status_code in (401, 403)
        assert c.get("/board/posts/%s" % pid).status_code in (401, 403)


def test_author_reopens_with_password():
    with TestClient(app) as c:
        pid = _post(c, title="재열람").json()["post_id"]
        r = c.post("/board/posts/%s/open" % pid, json={"password": "glhac2026"})
        assert r.status_code == 200
        assert r.json()["title"] == "재열람"
        assert r.json()["contact"] == "010-1234-5678"   # 한국 번호를 그대로 보관


def test_wrong_password_is_indistinguishable_from_missing_post():
    """글번호를 훑어 존재 여부를 알아내지 못하게 같은 응답을 준다."""
    with TestClient(app) as c:
        pid = _post(c).json()["post_id"]
        bad_pw = c.post("/board/posts/%s/open" % pid, json={"password": "틀린비번"})
        no_post = c.post("/board/posts/없는글번호/open", json={"password": "glhac2026"})
        assert bad_pw.status_code == no_post.status_code == 401
        assert bad_pw.json()["detail"]["code"] == no_post.json()["detail"]["code"]


def test_qr_visitor_is_attributed_to_the_consultant():
    """QR 로 들어온 문의는 그 영업자 건으로 남는다 — 수수료 근거."""
    with TestClient(app) as c:
        h = {"Authorization": "Bearer " + _tok(c)}
        ref = c.get("/consultant/qr/info", headers=h).json()["code"]
        assert _post(c, title="QR경유", ref=ref).json()["assigned"] is True
        mine = c.get("/board/posts?mine=true", headers=h).json()["items"]
        assert any(x["title"] == "QR경유" and x["ref_code"] == ref for x in mine)


def test_unknown_ref_does_not_block_the_post():
    """없는 코드로 들어와도 문의 자체는 접수한다 — 고객을 막을 이유가 없다."""
    with TestClient(app) as c:
        r = _post(c, title="엉뚱한코드", ref="ZZZZ-9999")
        assert r.status_code == 200 and r.json()["assigned"] is False


def test_staff_reply_marks_it_answered_and_author_sees_it():
    with TestClient(app) as c:
        pid = _post(c, title="답변확인").json()["post_id"]
        h = {"Authorization": "Bearer " + _tok(c)}
        assert c.post("/board/posts/%s/replies" % pid,
                      json={"body": "제품 정보를 보내주시면 진단해 드리겠습니다"},
                      headers=h).json()["status"] == "answered"
        seen = c.post("/board/posts/%s/open" % pid, json={"password": "glhac2026"}).json()
        assert seen["status"] == "answered" and len(seen["replies"]) == 1


def test_auditor_can_also_handle_the_board():
    """한국은 오디터가 샤리아 업무까지 겸한다 — 문의 응대 권한이 있어야 한다."""
    with TestClient(app) as c:
        pid = _post(c, title="오디터응대").json()["post_id"]
        h = {"Authorization": "Bearer " + _tok(c, "auditor1")}
        assert c.get("/board/posts", headers=h).status_code == 200
        assert c.post("/board/posts/%s/replies" % pid,
                      json={"body": "현장 심사 일정은 따로 안내드리겠습니다"},
                      headers=h).status_code == 200


def test_applicant_cannot_read_other_peoples_posts():
    """일반 신청기업 계정으로도 남의 문의는 못 본다."""
    with TestClient(app) as c:
        _post(c)
        h = {"Authorization": "Bearer " + _tok(c, "applicant1")}
        assert c.get("/board/posts", headers=h).status_code == 403


def test_rate_limit_blocks_flooding(monkeypatch):
    """무가입이라 봇이 들어온다 — 같은 IP 도배는 막는다."""
    from app import main as m
    monkeypatch.setattr(m, "_BOARD_RL_MAX", 2)
    m._BOARD_RL.clear()
    with TestClient(app) as c:
        assert _post(c, title="도배1").status_code == 200
        assert _post(c, title="도배2").status_code == 200
        blocked = _post(c, title="도배3")
        assert blocked.status_code == 429
        assert blocked.json()["detail"]["code"] == "TOO_MANY_POSTS"
    m._BOARD_RL.clear()


def test_inquiry_menu_is_registered_for_staff():
    """좌측 메뉴는 DB(sys_menu)에서 온다 — index.html 의 NAV 는 DB 가 빌 때만 쓰는 폴백이다.
    여기 등록하지 않으면 화면은 있는데 메뉴에 안 떠서 아무도 못 찾는다(실측으로 걸렸다)."""
    from app import main as m, models
    with TestClient(app) as c:
        db = m.SessionLocal()
        try:
            menu = db.query(models.SysMenu).filter_by(menu_code="INQUIRY").first()
            assert menu is not None and menu.route_path == "inquiry"
            roles = {r.role_id for r in
                     db.query(models.SysRoleMenu).filter_by(menu_id=menu.menu_id).all()}
            assert {"consultant", "auditor", "sharia", "ops", "admin"} <= roles
            assert "client" not in roles          # 클라이언트가 남의 문의를 보면 안 된다
        finally:
            db.close()
        # 실제 응답에도 나오는가
        tk = _tok(c)
        menus = c.get("/me/menus?lang=ko", headers={"Authorization": "Bearer " + tk}).json()
        paths = [ch["routePath"] for g in menus for ch in g.get("children", [])]
        assert "inquiry" in paths


def test_seed_menu_is_idempotent():
    """매 기동마다 도는 함수다 — 두 번 불러도 메뉴가 늘어나면 안 된다."""
    from app import main as m, models
    with TestClient(app):
        db = m.SessionLocal()
        try:
            m._ensure_inquiry_menu(db)
            m._ensure_inquiry_menu(db)
            assert db.query(models.SysMenu).filter_by(menu_code="INQUIRY").count() == 1
            mid = db.query(models.SysMenu).filter_by(menu_code="INQUIRY").first().menu_id
            assert db.query(models.SysRoleMenu).filter_by(menu_id=mid).count() == 5
        finally:
            db.close()


def test_only_ops_and_admin_can_delete_posts():
    """컨설턴트가 자기 실적에 불리한 글을 지울 수 있으면 안 된다."""
    with TestClient(app) as c:
        pid = _post(c, title="삭제권한").json()["post_id"]
        for who in ("consultant1", "auditor1"):
            h = {"Authorization": "Bearer " + _tok(c, who)}
            assert c.delete("/board/posts/%s" % pid, headers=h).status_code == 403
        assert c.delete("/board/posts/%s" % pid).status_code in (401, 403)   # 익명
        h = {"Authorization": "Bearer " + _tok(c, "admin", "admin")}
        assert c.delete("/board/posts/%s" % pid, headers=h).json()["deleted"] is True


def test_delete_removes_replies_and_leaves_an_audit_trail():
    """지운 사실은 남아야 한다 — 나중에 '왜 없어졌냐'를 답할 수 있어야 한다."""
    from app import main as m, models
    with TestClient(app) as c:
        pid = _post(c, title="스팸글").json()["post_id"]
        hs = {"Authorization": "Bearer " + _tok(c)}
        c.post("/board/posts/%s/replies" % pid, json={"body": "답변입니다"}, headers=hs)
        ha = {"Authorization": "Bearer " + _tok(c, "admin", "admin")}
        assert c.delete("/board/posts/%s" % pid, headers=ha).json()["replies_deleted"] == 1
        assert c.get("/board/posts/%s" % pid, headers=hs).status_code == 404
        db = m.SessionLocal()
        try:
            assert db.query(models.BoardReply).filter_by(post_id=pid).count() == 0
            row = (db.query(models.AuditLog)
                     .filter_by(action="board.delete", resource_id=pid).first())
            assert row is not None and "스팸글" in str(row.meta)
        finally:
            db.close()


def test_deleting_a_missing_post_is_404():
    with TestClient(app) as c:
        h = {"Authorization": "Bearer " + _tok(c, "admin", "admin")}
        assert c.delete("/board/posts/없는글", headers=h).status_code == 404


# ── 검색 ─────────────────────────────────────────────────────────────────
def test_staff_can_search_posts():
    """문의가 쌓이면 목록을 눈으로 훑을 수 없다 — 특히 연락처로 되찾는 일이 잦다."""
    with TestClient(app) as c:
        _post(c, title="검색A 원료", body="글리세린 원료 확인 부탁드립니다",
              contact="010-1111-2222", password="findme01")
        _post(c, title="검색B 포장재", body="포장재 인증 대상인지요",
              contact="pack@example.com", password="findme02")
        h = {"Authorization": "Bearer " + _tok(c)}
        for kw, want in [("포장재", "검색B 포장재"), ("글리세린", "검색A 원료"),
                         ("010-1111", "검색A 원료"), ("pack@example", "검색B 포장재")]:
            rows = c.get("/board/posts", params={"q": kw}, headers=h).json()["items"]
            assert [r["title"] for r in rows] == [want], (kw, rows)
        assert c.get("/board/posts", params={"q": "없는말zzz"}, headers=h).json()["items"] == []


# ── 수정 ─────────────────────────────────────────────────────────────────
def test_author_edits_own_post_with_password():
    with TestClient(app) as c:
        pid = _post(c, title="수정전", password="editpw77").json()["post_id"]
        r = c.post("/board/posts/%s/edit" % pid,
                   json={"password": "editpw77", "title": "수정후",
                         "contact": "010-9999-0000"})
        assert r.status_code == 200 and r.json()["title"] == "수정후"
        assert r.json()["contact"] == "010-9999-0000"
        assert r.json()["edited_at"]
        # 남의 비번으로는 안 된다
        assert c.post("/board/posts/%s/edit" % pid,
                      json={"password": "틀림", "title": "탈취"}).status_code == 401


def test_staff_edit_is_recorded_in_audit():
    """남의 글을 고치는 일이다 — '내가 쓴 것과 다르다'는 말에 답할 수 있어야 한다."""
    from app import main as m, models
    with TestClient(app) as c:
        pid = _post(c, title="원래제목", password="editpw77").json()["post_id"]
        h = {"Authorization": "Bearer " + _tok(c)}
        r = c.patch("/board/posts/%s" % pid, json={"title": "직원이 고친 제목"}, headers=h)
        assert r.status_code == 200 and "title" in r.json()["changed"]
        db = m.SessionLocal()
        try:
            row = (db.query(models.AuditLog)
                     .filter_by(action="board.edit", resource_id=pid).first())
            assert row is not None and "원래제목" in str(row.meta)
        finally:
            db.close()


# ── 대댓글 · 글쓴이 답글 ─────────────────────────────────────────────────
def test_author_can_reply_and_it_reopens_the_post():
    """되물을 수 없으면 대화가 한 번에 끝난다. 되물으면 다시 담당자 차례다."""
    with TestClient(app) as c:
        pid = _post(c, title="되묻기", password="askpw770").json()["post_id"]
        h = {"Authorization": "Bearer " + _tok(c)}
        rid = c.post("/board/posts/%s/replies" % pid,
                     json={"body": "성적서를 보내주세요"}, headers=h).json()["reply_id"]
        r = c.post("/board/posts/%s/replies" % pid,
                   json={"body": "어디로 보내면 되나요?", "password": "askpw770",
                         "parent_reply_id": rid})
        assert r.status_code == 200 and r.json()["by"] == "author"
        assert r.json()["status"] == "open"          # 다시 담당자 차례
        seen = c.post("/board/posts/%s/open" % pid, json={"password": "askpw770"}).json()
        kids = [x for x in seen["replies"] if x["parent_reply_id"] == rid]
        assert len(kids) == 1 and kids[0]["is_author"]


def test_replies_are_two_levels_deep_at_most():
    """더 깊어지면 화면에서 누구에게 한 말인지 알아보기 어렵다."""
    with TestClient(app) as c:
        pid = _post(c, title="깊이", password="deeppw77").json()["post_id"]
        h = {"Authorization": "Bearer " + _tok(c)}
        rid = c.post("/board/posts/%s/replies" % pid, json={"body": "1단"},
                     headers=h).json()["reply_id"]
        kid = c.post("/board/posts/%s/replies" % pid,
                     json={"body": "2단", "parent_reply_id": rid},
                     headers=h).json()["reply_id"]
        r = c.post("/board/posts/%s/replies" % pid,
                   json={"body": "3단", "parent_reply_id": kid}, headers=h)
        assert r.status_code == 422 and r.json()["detail"]["code"] == "TOO_DEEP"


def test_reply_edit_permissions():
    with TestClient(app) as c:
        pid = _post(c, title="답글수정", password="reppw770").json()["post_id"]
        hc = {"Authorization": "Bearer " + _tok(c)}
        rid = c.post("/board/posts/%s/replies" % pid, json={"body": "처음 답변"},
                     headers=hc).json()["reply_id"]
        url = "/board/posts/%s/replies/%s" % (pid, rid)
        assert c.patch(url, json={"body": "고친 답변"}, headers=hc).status_code == 200
        ha = {"Authorization": "Bearer " + _tok(c, "auditor1")}
        assert c.patch(url, json={"body": "남이 고침"}, headers=ha).status_code == 403
        hadm = {"Authorization": "Bearer " + _tok(c, "admin", "admin")}
        assert c.patch(url, json={"body": "관리자 정정"}, headers=hadm).status_code == 200
        # 글쓴이는 직원 답변을 못 고친다
        assert c.patch(url, json={"body": "훔쳐고치기",
                                  "password": "reppw770"}).status_code == 403


# ── 미로그인 열람 ────────────────────────────────────────────────────────
def test_public_list_hides_content():
    """게시판이 비어 보이면 남길 마음이 안 생긴다 — 다만 내용은 가린다."""
    with TestClient(app) as c:
        _post(c, title="공개목록", body="원료 상세 내용입니다 비밀",
              author_name="김대현", contact="010-3333-4444", password="pub12345")
        rows = c.get("/board/public").json()["items"]   # 로그인 없이
        assert rows and "body" not in rows[0] and "contact" not in rows[0]
        me = [r for r in rows if r["title"] == "공개목록"][0]
        assert me["author_masked"] == "김**"
        assert me["contact_masked"] == "010-****-4444"


def test_find_by_contact_needs_password_for_content():
    """폰으로 남기고 PC 에서 확인하는 흐름 — 목록은 연락처로, 내용은 비밀번호로."""
    with TestClient(app) as c:
        _post(c, title="기기이동", contact="010-5555-6666", password="move1234")
        rows = c.post("/board/find", json={"contact": "010-5555-6666"}).json()
        assert rows and rows[0]["status"] == "open"
        hit = c.post("/board/find", json={"contact": "010-5555-6666",
                                          "password": "move1234"}).json()
        assert hit[0]["title"] == "기기이동"
        assert c.post("/board/find", json={"contact": "010-5555-6666",
                                           "password": "틀림"}).status_code == 401
        assert c.post("/board/find", json={"contact": "010-0000-0000"}).status_code == 404


# ── 비밀번호 규칙 ────────────────────────────────────────────────────────
def test_password_must_not_reuse_contact_digits():
    """목록에 연락처가 일부 보인다 — 비번을 전화번호로 쓰면 가림막이 사라진다.
    4자리 이상 이어서 겹치면 막는다. 구분기호로 끊어 써도 마찬가지다(실측 우회)."""
    with TestClient(app) as c:
        for pw in ("01049281733", "pw4928", "004928", "49-28-77", "4 9 2 8", "49281733"):
            r = _post(c, contact="010-4928-1733", password=pw, title="규칙")
            assert r.status_code == 422, (pw, r.status_code)
        for pw in ("glhac2026", "170733x", "a1b2c3d4"):
            assert _post(c, contact="010-4928-1733", password=pw,
                         title="규칙통과").status_code == 200, pw


# ── 페이징 ───────────────────────────────────────────────────────────────
def test_public_list_is_paged():
    """문의가 쌓이면 한 화면에 다 못 담는다 — 총 몇 쪽인지도 알려 줘야 한다."""
    with TestClient(app) as c:
        for i in range(12):
            _post(c, title="페이징%02d" % i, contact="010-7000-%04d" % i,
                  password="glhac2026")
        r = c.get("/board/public", params={"page": 1, "size": 5}).json()
        assert len(r["items"]) == 5 and r["page"] == 1 and r["size"] == 5
        assert r["total"] >= 12 and r["pages"] == (r["total"] + 4) // 5
        first = [x["post_id"] for x in r["items"]]
        second = c.get("/board/public", params={"page": 2, "size": 5}).json()["items"]
        assert not set(first) & {x["post_id"] for x in second}     # 겹치지 않는다
        last = c.get("/board/public", params={"page": r["pages"], "size": 5}).json()
        assert 1 <= len(last["items"]) <= 5
        # 범위를 벗어난 쪽은 빈 목록 — 오류가 아니다
        assert c.get("/board/public", params={"page": 9999, "size": 5}).json()["items"] == []


def test_page_size_is_capped():
    """size=100000 한 번으로 전체를 긁어가지 못하게 한다."""
    with TestClient(app) as c:
        for i in range(3):
            _post(c, title="상한%d" % i, contact="010-7100-%04d" % i, password="glhac2026")
        assert c.get("/board/public", params={"size": 100000}).json()["size"] == 50
        h = {"Authorization": "Bearer " + _tok(c)}
        assert c.get("/board/posts", params={"size": 100000}, headers=h).json()["size"] == 100
        # 음수·0 은 조용히 최소값으로 — 화면 버그로 페이지가 0 이 되어도 안 깨진다
        r = c.get("/board/public", params={"page": -3, "size": -5}).json()
        assert r["page"] == 1 and r["size"] >= 1
        # 숫자가 아니면 FastAPI 가 먼저 422 로 막는다(내 정리 코드까지 오지 않는다)
        assert c.get("/board/public", params={"page": "abc"}).status_code == 422


def test_search_and_paging_work_together():
    with TestClient(app) as c:
        for i in range(7):
            _post(c, title="검색페이징 %d" % i, body="포장재 관련 문의 내용입니다 %d" % i,
                  contact="010-7200-%04d" % i, password="glhac2026")
        h = {"Authorization": "Bearer " + _tok(c)}
        r = c.get("/board/posts", params={"q": "검색페이징", "size": 3}, headers=h).json()
        assert r["total"] == 7 and r["pages"] == 3 and len(r["items"]) == 3
        assert all("검색페이징" in x["title"] for x in r["items"])
