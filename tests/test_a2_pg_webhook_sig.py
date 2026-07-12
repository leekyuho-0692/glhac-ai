"""[A2] PG 웹훅 서명 의무화 — 프로덕션 미설정 콜백 차단, dev 허용, 서명 검증."""
import os, importlib
from starlette.testclient import TestClient

def _app(dev, secret=None):
    os.environ["GLHAC_DB_URL"]="sqlite:////tmp/test_a2_%s.db"%("dev" if dev else "prod")
    os.environ["GLHAC_SECRET"]="x"*40
    if dev: os.environ["GLHAC_DEV"]="1"
    else: os.environ.pop("GLHAC_DEV",None)
    if secret: os.environ["GLHAC_PG_WEBHOOK_SECRET"]=secret
    else: os.environ.pop("GLHAC_PG_WEBHOOK_SECRET",None)
    import app.main as m; importlib.reload(m); return m.app

def test_prod_unsigned_blocked():
    """비-dev + 시크릿 미설정 → 미검증 콜백 501 차단(위조 결제 방지)."""
    with TestClient(_app(dev=False)) as c:
        r=c.post("/pg/webhook/midtrans", json={"order_id":"X","transaction_status":"settlement"})
        assert r.status_code==501, r.text
        assert r.json()["detail"]["code"]=="PG_SECRET_UNSET"

def test_dev_unsigned_allowed():
    """dev 모드 → 미검증 허용(데모/테스트)."""
    with TestClient(_app(dev=True)) as c:
        r=c.post("/pg/webhook/midtrans", json={"order_id":"X","transaction_status":"settlement"})
        assert r.status_code==200, r.text

def test_bad_signature_401():
    """시크릿 설정 + 잘못된 서명 → 401."""
    with TestClient(_app(dev=False, secret="s3cr3t")) as c:
        r=c.post("/pg/webhook/midtrans", json={"order_id":"X","transaction_status":"settlement"},
                 headers={"x-signature":"WRONG"})
        assert r.status_code==401, r.text
        assert r.json()["detail"]["code"]=="BAD_SIGNATURE"
