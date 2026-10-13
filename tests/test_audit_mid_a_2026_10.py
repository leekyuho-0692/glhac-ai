"""2026-10 감사 중간 결함 묶음 MA(결제·무결성) MA1~MA7 테스트.

모든 테스트는 실제 엔드포인트/함수를 호출한다(구조 확인만 하는 단언 금지).
각 테스트 주석에 '수정 전 코드에서 왜 실패하는지'를 적는다.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("GLHAC_DB_URL", "sqlite:///./glhac_auditmid_a_test.db")
os.environ.setdefault("GLHAC_DEV", "1")
os.environ.setdefault("GLHAC_NOTIFY_WORKER", "0")
os.environ.setdefault("GLHAC_RATE_LIMIT_PER_MIN", "0")
os.environ.pop("GLHAC_PG_WEBHOOK_SECRET", None)

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from app.main import app  # noqa: E402
from app import models  # noqa: E402
from app.db import SessionLocal  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ---------- 헬퍼 ----------
def _tok(c, u, p):
    r = c.post("/auth/login", json={"username": u, "password": p})
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["token"]}


def _op(c):
    return _tok(c, "operator1", "pw")


def _db_case(company="MA-Co"):
    db = SessionLocal()
    try:
        c = models.CaseApplication(org_id="org_demo", company_name=company, status="onboarding")
        db.add(c)
        db.commit()
        return c.case_id
    finally:
        db.close()


def _add(obj):
    db = SessionLocal()
    try:
        db.add(obj)
        db.commit()
        db.refresh(obj)
        db.expunge(obj)
        return obj
    finally:
        db.close()


def _contract(cid, currency="KRW", status="confirmed"):
    return _add(models.Contract(case_id=cid, currency=currency, status=status)).contract_id


def _invoice(cid, status="waiting_payment", total=1_100_000.0, ref=None):
    return _add(models.Invoice(case_id=cid, service_type="pre_audit", amount=total / 1.1,
                               ppn=total - total / 1.1, total=total, status=status,
                               payment_ref=ref)).invoice_id


def _payments(iid):
    db = SessionLocal()
    try:
        return db.query(models.Payment).filter_by(invoice_id=iid).all()
    finally:
        db.close()


def _inv_status(iid):
    db = SessionLocal()
    try:
        return db.get(models.Invoice, iid).status
    finally:
        db.close()


def _code(r):
    d = r.json().get("detail")
    return d.get("code") if isinstance(d, dict) else None


# ======== MA1: 통화 KRW 통일 — 계약 통화에서 가져온다 ========

def test_ma1_모델_기본통화는_KRW():
    """Payment/Deposit 기본 통화 KRW (수정 전 IDR)."""
    cid = _db_case()
    p = _add(models.Payment(invoice_id="x", case_id=cid, amount=1.0, method="manual"))
    d = _add(models.Deposit(bank_name="B", amount=1.0))
    assert p.currency == "KRW" and d.currency == "KRW"


def test_ma1_결제기록은_계약통화를_따른다():
    """POST /invoices/{id}/payment — 계약 통화(USD)가 Payment 에 기록된다.
    수정 전: currency 미지정이라 모델 기본값 IDR 이 박혀 USD 가 되지 않는다."""
    cid = _db_case()
    _contract(cid, currency="USD")
    iid = _invoice(cid)
    with TestClient(app) as c:
        r = c.post(f"/invoices/{iid}/payment", headers=_op(c),
                   json={"method": "bank_transfer", "amount": 1_100_000.0})
        assert r.status_code == 200, r.text
    assert [p.currency for p in _payments(iid)] == ["USD"]


def test_ma1_계약없으면_KRW_수기확정_결제도_계약통화():
    """PATCH status=paid 의 수기 Payment — 계약 없으면 KRW, 있으면 계약 통화.
    수정 전: 하드코딩 "IDR"."""
    cid = _db_case()
    iid = _invoice(cid)
    cid2 = _db_case("MA-Co2")
    _contract(cid2, currency="USD")
    iid2 = _invoice(cid2)
    with TestClient(app) as c:
        h = _op(c)
        for i in (iid, iid2):
            r = c.patch(f"/invoices/{i}/status", headers=h, json={"status": "paid", "reason": "수기확인"})
            assert r.status_code == 200, r.text
    assert [p.currency for p in _payments(iid)] == ["KRW"]
    assert [p.currency for p in _payments(iid2)] == ["USD"]


def test_ma1_PG웹훅_결제는_계약통화():
    """POST /pg/webhook/manual settlement — 수정 전 하드코딩 "IDR"."""
    cid = _db_case()
    _contract(cid, currency="USD")
    iid = _invoice(cid, ref="ORD-MA1-WH", total=1_100_000.0)
    with TestClient(app) as c:
        r = c.post("/pg/webhook/manual", json={"order_id": "ORD-MA1-WH", "transaction_status": "settlement",
                                               "gross_amount": "1100000.00", "event_id": "evt-ma1-wh"})
        assert r.status_code == 200, r.text
    assert _inv_status(iid) == "paid"
    assert [p.currency for p in _payments(iid)] == ["USD"]


def test_ma1_입금자동확정은_Deposit_Payment_모두_계약통화():
    """POST /admin/deposits COMMIT — 입금(Deposit)·결제(Payment) 통화가 계약 통화.
    수정 전: Deposit 은 기본값 IDR, Payment 도 IDR."""
    cid = _db_case()
    _contract(cid, currency="USD")
    iid = _invoice(cid, ref="PAY-MA1-DEP", total=1_100_000.0)
    with TestClient(app) as c:
        r = c.post("/admin/deposits", headers=_op(c),
                   json={"bank_name": "B", "account_no": "1234567890", "depositor_name": "MA-Co",
                         "amount": 1_100_000.0, "ref_memo": "PAY-MA1-DEP"})
        assert r.status_code == 200, r.text
        assert r.json()["verdict"] == "COMMIT", r.text
        dep_id = r.json()["deposit_id"]
    db = SessionLocal()
    try:
        assert db.get(models.Deposit, dep_id).currency == "USD"
    finally:
        db.close()
    assert [p.currency for p in _payments(iid)] == ["USD"]


def test_ma1_기본_부가세율_0_10_저장값은_존중():
    """GET /billing/settings 기본 0.10, 저장한 값(0.11)은 그대로(수정 전 기본 0.11)."""
    with TestClient(app) as c:
        h = _op(c)
        s = c.get("/billing/settings", headers=h).json()
        assert s["ppn_rate"] == 0.10
        body = {k: s[k] for k in ("sector_base", "per_product_extra", "onsite_travel", "options",
                                  "payment_terms", "due_days", "auto_notice_contract",
                                  "auto_notice_paid", "bank_account")}
        r = c.post("/billing/settings", headers=h, json=dict(body, ppn_rate=0.11))
        assert r.status_code == 200, r.text
        assert c.get("/billing/settings", headers=h).json()["ppn_rate"] == 0.11
        r = c.post("/billing/settings", headers=h, json=dict(body, ppn_rate=0.10))
        assert r.status_code == 200, r.text


def test_ma1_청구서_생성_부가세는_설정_기본_10퍼센트():
    """POST /cases/{id}/invoices — PPN = 금액 x ppn_rate(기본 0.10). 수정 전 0.11 하드코딩."""
    cid = _db_case()
    _contract(cid)
    with TestClient(app) as c:
        r = c.post(f"/cases/{cid}/invoices", headers=_tok(c, "consultant1", "pw"),
                   json={"service_type": "pre_audit", "amount": 1_000_000})
        assert r.status_code == 200, r.text
        assert r.json()["ppn"] == 100_000.0 and r.json()["total"] == 1_100_000.0


# ======== MA2: 청구 상태 전이표 ========

def _patch(c, h, iid, status, reason="사유"):
    return c.patch(f"/invoices/{iid}/status", headers=h, json={"status": status, "reason": reason})


@pytest.mark.parametrize("frm,to", [
    ("draft", "invoice_created"), ("draft", "waiting_payment"), ("draft", "cancelled"),
    ("invoice_created", "waiting_payment"), ("invoice_created", "cancelled"),
    ("waiting_payment", "payment_processing"), ("waiting_payment", "need_verification"),
    ("waiting_payment", "expired"), ("waiting_payment", "cancelled"),
    ("payment_processing", "need_verification"), ("payment_processing", "rejected"),
    ("need_verification", "rejected"), ("rejected", "waiting_payment"), ("expired", "waiting_payment"),
])
def test_ma2_허용_전이는_200(frm, to):
    """표의 허용 전이는 통과(paid 목적지는 MA3 합산 검증이 별도라 여기서 제외).
    수정 전에는 cancelled 가 BAD_STATUS(422)라 *→cancelled 가 실패한다."""
    iid = _invoice(_db_case(), status=frm)
    with TestClient(app) as c:
        r = _patch(c, _op(c), iid, to)
        assert r.status_code == 200, r.text
    assert _inv_status(iid) == to


@pytest.mark.parametrize("frm,to", [
    ("draft", "paid"), ("draft", "payment_processing"), ("invoice_created", "paid"),
    ("waiting_payment", "draft"), ("waiting_payment", "rejected"),
    ("payment_processing", "waiting_payment"), ("need_verification", "waiting_payment"),
    ("rejected", "paid"), ("expired", "paid"), ("cancelled", "waiting_payment"),
    ("refunded", "paid"),
    ("paid", "waiting_payment"), ("paid", "rejected"), ("paid", "refunded"), ("paid", "need_verification"),
])
def test_ma2_금지_전이는_409(frm, to):
    """수정 전에는 어떤 전이든 200 (paid → waiting_payment 로 되돌려 결제 게이트 우회 가능했음)."""
    iid = _invoice(_db_case(), status=frm)
    with TestClient(app) as c:
        r = _patch(c, _op(c), iid, to)
        assert r.status_code == 409, r.text
        assert _code(r) == "INVALID_INVOICE_TRANSITION"
        assert r.json()["detail"]["from"] == frm
    assert _inv_status(iid) == frm   # 상태·결제 부작용 없음
    assert _payments(iid) == []


def test_ma2_사유_필수_빈값_공백_누락_422():
    """수정 전에는 reason 이 비어도 200."""
    iid = _invoice(_db_case(), status="waiting_payment")
    with TestClient(app) as c:
        h = _op(c)
        for body in ({"status": "expired", "reason": ""}, {"status": "expired", "reason": "   "},
                     {"status": "expired"}):
            r = c.patch(f"/invoices/{iid}/status", headers=h, json=body)
            assert r.status_code == 422, r.text
            assert _code(r) == "REASON_REQUIRED"
    assert _inv_status(iid) == "waiting_payment"


def test_ma2_레거시_unpaid는_waiting_payment로_취급():
    iid = _invoice(_db_case(), status="unpaid")
    with TestClient(app) as c:
        assert _patch(c, _op(c), iid, "expired").status_code == 200


def test_ma2_알수없는_상태는_422():
    iid = _invoice(_db_case())
    with TestClient(app) as c:
        r = _patch(c, _op(c), iid, "bogus")
        assert r.status_code == 422 and _code(r) == "BAD_STATUS"


# ======== MA3: 부분입금 paid 확정 차단 ========

def _pay(cid, iid, amount, status):
    _add(models.Payment(invoice_id=iid, case_id=cid, amount=amount, method="bank_transfer", status=status))


def test_ma3_모자라면_409_UNDERPAID_상태_결제_불변():
    """수정 전에는 pending 60 만 있어도 paid 로 확정되어 pending 이 confirmed 로 바뀐다."""
    cid = _db_case()
    iid = _invoice(cid, status="need_verification", total=100.0)
    _pay(cid, iid, 60.0, "pending")
    with TestClient(app) as c:
        r = _patch(c, _op(c), iid, "paid", "검증")
        assert r.status_code == 409, r.text
        d = r.json()["detail"]
        assert d["code"] == "UNDERPAID" and d["paid_sum"] == 60.0 and d["total"] == 100.0
    assert _inv_status(iid) == "need_verification"
    assert [p.status for p in _payments(iid)] == ["pending"]


def test_ma3_confirmed_와_pending_합이_total_이상이면_확정되고_pending은_confirmed():
    cid = _db_case()
    iid = _invoice(cid, status="need_verification", total=100.0)
    _pay(cid, iid, 40.0, "confirmed")
    _pay(cid, iid, 60.0, "pending")
    with TestClient(app) as c:
        r = _patch(c, _op(c), iid, "paid", "검증")
        assert r.status_code == 200, r.text
    assert _inv_status(iid) == "paid"
    assert sorted(p.status for p in _payments(iid)) == ["confirmed", "confirmed"]


def test_ma3_confirmed_만_부분이면_pending_없어도_UNDERPAID():
    """수정 전: pending 이 없고 confirmed(+)가 있으면 아무 검증 없이 paid."""
    cid = _db_case()
    iid = _invoice(cid, status="waiting_payment", total=100.0)
    _pay(cid, iid, 30.0, "confirmed")
    with TestClient(app) as c:
        r = _patch(c, _op(c), iid, "paid", "수기")
        assert r.status_code == 409 and _code(r) == "UNDERPAID", r.text
        assert r.json()["detail"]["paid_sum"] == 30.0


def test_ma3_초과입금과_분할_pending_합산은_허용():
    cid = _db_case()
    iid = _invoice(cid, status="need_verification", total=100.0)
    _pay(cid, iid, 70.0, "pending")
    _pay(cid, iid, 50.0, "pending")
    with TestClient(app) as c:
        assert _patch(c, _op(c), iid, "paid", "검증").status_code == 200


def test_ma3_결제기록이_전혀_없으면_수기_전액_확정_기존동작_유지():
    """결제 기록 없이 관리자가 수기 확정하면 total 전액 manual Payment 를 만든다(기존 동작)."""
    cid = _db_case()
    iid = _invoice(cid, status="waiting_payment", total=100.0)
    with TestClient(app) as c:
        assert _patch(c, _op(c), iid, "paid", "수기 확인").status_code == 200
    ps = _payments(iid)
    assert len(ps) == 1 and ps[0].amount == 100.0 and ps[0].method == "manual"


# ======== MA4: 해시체인 동시성 (서로 다른 세션·커밋) ========

def _run_threads(n, body):
    import threading
    barrier = threading.Barrier(n)
    errors = []

    def runner(i):
        db = SessionLocal()
        try:
            barrier.wait()
            body(db, i)
            db.commit()
        except Exception as e:  # noqa: BLE001
            errors.append(repr(e))
            db.rollback()
        finally:
            db.close()

    ts = [threading.Thread(target=runner, args=(i,)) for i in range(n)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    return errors


def test_ma4_스레드8개_각자세션_커밋_WorkflowEvent_체인_무결():
    """스레드 8개가 각자 세션에서 record_event 2회(flush 후 지연, 커밋은 나중) → 체인 검증 무결·prev_hash 중복 0.
    flush 까지만 쥐는 프로세스 Lock(이전 구현)은 이 시나리오에서 같은 prev_hash 를 허용한다 — 다른 세션이
    미커밋 꼬리를 못 보기 때문. 락이 커밋까지 가야 통과한다."""
    import time
    from app import state_machine as sm
    from app.main import _verify_wf_chain
    cid = _db_case("MA4-wf")

    def body(db, i):
        case = db.get(models.CaseApplication, cid)
        sm.record_event(db, case, case.status, case.status, "ma4.a", "system", f"t{i}", {"i": i})
        time.sleep(0.02)   # flush~commit 사이 창을 넓혀 경쟁을 재현
        sm.record_event(db, case, case.status, case.status, "ma4.b", "system", f"t{i}", {"i": i})

    assert _run_threads(8, body) == []
    db = SessionLocal()
    try:
        evs = (db.query(models.WorkflowEvent).filter_by(case_id=cid)
               .order_by(models.WorkflowEvent.created_at, models.WorkflowEvent.event_id).all())
        assert len(evs) == 16
        prevs = [e.prev_hash for e in evs]
        assert len(set(prevs)) == len(prevs), "prev_hash 중복 = 체인 분기"
        r = _verify_wf_chain(evs)
        assert r["integrity_ok"] and r["status"] == "ok", r
    finally:
        db.close()


def test_ma4_스레드8개_전역_AuditLog_체인_무결():
    """전역 _audit 체인 — 8세션이 flush 후 지연 뒤 커밋해도 분기 없음."""
    import time
    from app.main import _audit, _verify_audit_chain
    cid = _db_case("MA4-audit")
    marker = "ma4.audit"

    def body(db, i):
        _audit(db, {"uid": f"u{i}", "role": "operator", "org_id": "org_demo"}, marker, "case", cid, cid,
               {"i": i}, commit=False)
        time.sleep(0.02)

    assert _run_threads(8, body) == []
    db = SessionLocal()
    try:
        rows = (db.query(models.AuditLog).order_by(models.AuditLog.created_at, models.AuditLog.id).all())
        mine = [r for r in rows if r.action == marker]
        assert len(mine) == 8
        prevs = [r.meta["_chain"]["prev"] for r in mine]
        assert len(set(prevs)) == 8, "prev 중복 = 전역 체인 분기"
        ok = _verify_audit_chain(rows)
        assert ok["integrity_ok"], ok
    finally:
        db.close()


def test_ma4_같은_요청_다중_이벤트는_데드락_없이_이어진다():
    """한 세션이 record_event→_audit→record_event (락 재진입). 쓰기 락을 이미 쥐고 있어도 BEGIN IMMEDIATE 를
    다시 걸지 않아야 한다(걸면 'cannot start a transaction within a transaction')."""
    from app import state_machine as sm
    from app.main import _audit
    cid = _db_case("MA4-reent")
    db = SessionLocal()
    try:
        case = db.get(models.CaseApplication, cid)
        e1 = sm.record_event(db, case, case.status, case.status, "r.1")
        _audit(db, {"uid": "u", "role": "operator", "org_id": "org_demo"}, "r.audit", None, None, cid, {}, commit=False)
        e2 = sm.record_event(db, case, case.status, case.status, "r.2")
        db.commit()
        assert e2.prev_hash == e1.row_hash
    finally:
        db.close()


# ======== MA5: 원재료 삭제 고아행 ========

def test_ma5_원재료_삭제시_연결삭제_문서보존():
    """DELETE /materials/{id} — 제품연결·증빙링크 삭제, 문서는 보존하고 material_id 만 해제,
    다른 원재료의 행은 건드리지 않는다. 수정 전: 모두 고아로 남아 문서가 지워진 원재료를 가리킨다."""
    cid = _db_case("MA5")
    m1 = _add(models.Material(case_id=cid, name="삭제대상")).material_id
    m2 = _add(models.Material(case_id=cid, name="유지")).material_id
    _add(models.ProductMaterial(case_id=cid, product_id="p1", material_id=m1))
    _add(models.ProductMaterial(case_id=cid, product_id="p1", material_id=m2))
    d1 = _add(models.DocumentAsset(case_id=cid, filename="a.pdf", material_id=m1)).document_id
    d2 = _add(models.DocumentAsset(case_id=cid, filename="b.pdf", material_id=m2)).document_id
    _add(models.MaterialEvidenceLink(case_id=cid, document_id=d1, material_id=m1))
    _add(models.MaterialEvidenceLink(case_id=cid, document_id=d2, material_id=m2))
    with TestClient(app) as c:
        r = c.delete(f"/materials/{m1}", headers=_tok(c, "consultant1", "pw"))
        assert r.status_code == 200, r.text
    db = SessionLocal()
    try:
        assert db.get(models.Material, m1) is None
        assert db.query(models.ProductMaterial).filter_by(material_id=m1).count() == 0
        assert db.query(models.MaterialEvidenceLink).filter_by(material_id=m1).count() == 0
        doc = db.get(models.DocumentAsset, d1)
        assert doc is not None and doc.material_id is None          # 문서 보존
        # 다른 원재료는 그대로
        assert db.query(models.ProductMaterial).filter_by(material_id=m2).count() == 1
        assert db.query(models.MaterialEvidenceLink).filter_by(material_id=m2).count() == 1
        assert db.get(models.DocumentAsset, d2).material_id == m2
        # 감사로그도 같은 트랜잭션에서 커밋됨
        assert db.query(models.AuditLog).filter_by(action="material.delete", resource_id=m1).count() == 1
    finally:
        db.close()


# ======== MA6: alembic 단일 head · _migrate 오류 처리 ========

def test_ma6_alembic_head_는_하나():
    """ScriptDirectory.get_heads() 길이 1. 수정 전: 0007_audit_log / 42fd1a346122 두 개."""
    from alembic.config import Config
    from alembic.script import ScriptDirectory
    cfg = Config(os.path.join(ROOT, "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(ROOT, "alembic"))   # 레포 루트 기준 절대경로
    heads = ScriptDirectory.from_config(cfg).get_heads()
    assert len(heads) == 1, heads


def test_ma6_fresh_DB_에서_upgrade_head_가_성공(tmp_path):
    """병합 리비전 포함 `alembic upgrade head` 가 새 SQLite 에서 끝까지 적용된다."""
    import subprocess
    env = dict(os.environ, GLHAC_DB_URL="sqlite:///" + str(tmp_path / "mig.db"))
    r = subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=ROOT, env=env,
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr[-800:]


@pytest.fixture
def _ma6_tables():
    """_migrate 가 보는 메타데이터에 임시 테이블 3개를 얹고 끝나면 제거(다른 테스트 오염 방지)."""
    import sqlalchemy as sa
    from app.db import Base, engine
    made = []
    with engine.begin() as conn:
        conn.exec_driver_sql('CREATE TABLE ma6_add (id TEXT PRIMARY KEY)')
        conn.exec_driver_sql('CREATE TABLE ma6_bad (id TEXT PRIMARY KEY)')
        conn.exec_driver_sql('CREATE TABLE ma6_dup (id TEXT PRIMARY KEY, dupcol TEXT)')
    made.append(sa.Table("ma6_add", Base.metadata, sa.Column("id", sa.String, primary_key=True),
                         sa.Column("extra", sa.String)))
    made.append(sa.Table("ma6_bad", Base.metadata, sa.Column("id", sa.String, primary_key=True),
                         sa.Column('bad"col', sa.String)))   # 따옴표 때문에 ALTER 가 구문 오류
    made.append(sa.Table("ma6_dup", Base.metadata, sa.Column("id", sa.String, primary_key=True),
                         sa.Column("dupcol", sa.String)))
    yield
    for t in made:
        Base.metadata.remove(t)
    with engine.begin() as conn:
        for n in ("ma6_add", "ma6_bad", "ma6_dup"):
            conn.exec_driver_sql(f"DROP TABLE IF EXISTS {n}")


def test_ma6_migrate_누락컬럼_추가_오류는_로그_duplicate는_무시(_ma6_tables, caplog, monkeypatch):
    """실제 _migrate() 호출.
    · 누락 컬럼은 추가된다  · 구문 오류 ALTER 는 log.error(테이블·컬럼·오류) 후 계속(예외 전파 없음)
    · duplicate column 은 로그 없이 무시. 수정 전: 모든 예외를 삼켜 error 로그가 0건."""
    import logging
    import sqlalchemy
    from app.db import engine
    from app.main import _migrate

    real_inspect = sqlalchemy.inspect

    class _StaleInspector:   # ma6_dup 의 dupcol 이 없다고 보고 → ALTER 시도 → SQLite 'duplicate column name'
        def __init__(self, insp):
            self._i = insp

        def has_table(self, n):
            return self._i.has_table(n)

        def get_columns(self, n):
            cols = self._i.get_columns(n)
            return [c for c in cols if not (n == "ma6_dup" and c["name"] == "dupcol")]

    monkeypatch.setattr(sqlalchemy, "inspect", lambda e: _StaleInspector(real_inspect(e)))
    import app.main as _m
    dup_hits = []
    real_dup = _m._is_duplicate_column_error
    monkeypatch.setattr(_m, "_is_duplicate_column_error", lambda e: dup_hits.append(1) or real_dup(e))
    with caplog.at_level(logging.ERROR, logger="glhac.migrate"):
        _migrate()
    msgs = [r.getMessage() for r in caplog.records if r.name == "glhac.migrate"]
    assert len(msgs) == 1, msgs                                  # bad 한 건만
    assert "ma6_bad" in msgs[0] and 'bad"col' in msgs[0]         # 테이블·컬럼 포함
    assert not any("ma6_dup" in m for m in msgs)                 # duplicate 는 무시
    assert len(dup_hits) == 2                                    # dup·bad 두 ALTER 실패가 판별 함수를 거쳤다
    cols = {c["name"] for c in real_inspect(engine).get_columns("ma6_add")}
    assert "extra" in cols                                       # 정상 컬럼은 추가됨(부팅 계속)


# ======== MA7: 인테이크 할랄 인증서 = external, 우리 인증서로 오인 금지 ========

def _intake_external_cert(cid, cert_no="EXT-ID-001", expiry=None):
    """실제 인테이크 처리 함수(_process_doc)로 외부 인증서 레코드를 만든다."""
    from datetime import date, timedelta
    from app.main import _process_doc
    expiry = expiry or (date.today() + timedelta(days=10)).isoformat()
    db = SessionLocal()
    try:
        c = db.get(models.CaseApplication, cid)
        applied = {}
        _process_doc(db, c, {"doc_type": "halal_certificate",
                             "fields": {"cert_no": cert_no, "issue_date": "2024-01-01",
                                        "expiry_date": expiry, "scope": ["외부 제품"]}}, applied)
        db.commit()
        assert applied.get("halal_cert") == cert_no
        return db.query(models.HalalCertificate).filter_by(case_id=cid, certificate_no=cert_no).one().id
    finally:
        db.close()


def test_ma7_인테이크_인증서는_external():
    """수정 전: status="active"."""
    cid = _db_case("MA7-a")
    xid = _intake_external_cert(cid)
    db = SessionLocal()
    try:
        assert db.get(models.HalalCertificate, xid).status == "external"
    finally:
        db.close()


def test_ma7_external_은_우리_인증서로_조회되지_않는다():
    """GET certificate · /verify · suspend/revoke · 만료스캔이 external 을 무시한다.
    수정 전: issued=True, 정지/철회가 외부 인증서를 변경, 만료 알림이 외부 인증서 기준으로 발송."""
    cid = _db_case("MA7-b")
    xid = _intake_external_cert(cid, "EXT-ID-002")
    db = SessionLocal()
    try:
        db.get(models.HalalCertificate, xid).qr_token = "ma7-ext-token"   # 토큰이 있어도 공개검증 불가
        db.commit()
    finally:
        db.close()
    with TestClient(app) as c:
        h = _tok(c, "admin", "admin")
        assert c.get(f"/cases/{cid}/certificate", headers=h).json() == {"issued": False}
        assert c.get(f"/cases/{cid}/certificate/pdf", headers=h).status_code == 404
        assert c.get("/verify/ma7-ext-token").status_code == 404
        for act in ("suspend", "revoke"):
            r = c.post(f"/cases/{cid}/certificate/{act}", headers=h, json={"reason": "외부 인증서 테스트"})
            assert r.status_code == 404, (act, r.text)
            assert _code(r) == "CERT_NOT_FOUND"
        before = db_count_notifs(cid)
        r = c.post("/admin/scan-expiry", headers=h)
        assert r.status_code == 200, r.text
        assert db_count_notifs(cid) == before          # 외부 인증서 만료 임박 알림 없음
    db = SessionLocal()
    try:
        assert db.get(models.HalalCertificate, xid).status == "external"   # 변경되지 않음
    finally:
        db.close()


def db_count_notifs(cid):
    db = SessionLocal()
    try:
        return db.query(models.Notification).filter_by(case_id=cid, event_type="expiry_soon").count()
    finally:
        db.close()


def test_ma7_정식발급은_별도_active_레코드_external_은_그대로():
    """external 이 있어도 정식 발급이 새 active 를 만들고(대체 아님), 이후 조회는 active 를 가리킨다."""
    from app.main import _do_issue_certificate
    cid = _db_case("MA7-c")
    xid = _intake_external_cert(cid, "EXT-ID-003")
    db = SessionLocal()
    try:
        c = db.get(models.CaseApplication, cid)
        res = _do_issue_certificate(db, c, {"uid": "u1", "role": "admin", "org_id": "org_demo"})
        db.commit()
        assert not res.get("existing")
        certs = db.query(models.HalalCertificate).filter_by(case_id=cid).all()
        assert sorted(x.status for x in certs) == ["active", "external"]
        assert db.get(models.HalalCertificate, xid).certificate_no == "EXT-ID-003"
        active = [x for x in certs if x.status == "active"][0]
        assert active.id != xid and active.certificate_no.startswith("HC-")
    finally:
        db.close()
    with TestClient(app) as c:
        h = _tok(c, "admin", "admin")
        body = c.get(f"/cases/{cid}/certificate", headers=h).json()
        assert body["certificate_no"] == active.certificate_no


def test_ma7_분석요약_발급실적은_external_제외():
    """GET /analytics/summary 의 월별 발급 집계가 외부 인증서(발급일 2024-01)를 세지 않는다.
    수정 전: certs_by_month 에 '2024-01': 1."""
    cid = _db_case("MA7-d")
    _intake_external_cert(cid, "EXT-ID-004")
    with TestClient(app) as c:
        r = c.get("/analytics/summary", headers=_tok(c, "admin", "admin"))
        assert r.status_code == 200, r.text
        assert "2024-01" not in (r.json().get("certs_by_month") or {})
