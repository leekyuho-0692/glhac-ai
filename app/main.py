"""GL-HAC AI API — M1(dual-pathway) + M3(OCR/판정) + M2(인증·UI). 설계 24.14 / B.4."""
import io
import os
import base64
import logging
from datetime import datetime, date
from fastapi import FastAPI, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

logging.basicConfig(
    level=os.environ.get("GLHAC_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s :: %(message)s")
log = logging.getLogger("glhac")
from sqlalchemy import func, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from .db import Base, engine, get_db, SessionLocal
from . import models, schemas, state_machine as sm, screening, ai_local, auth, rbac
from .ontology_seed import seed

app = FastAPI(title="GL-HAC AI Dual-Pathway API", version="0.2.0")

# CORS — 기본은 동일 출처만. GLHAC_CORS_ORIGINS(콤마구분)로 SPA 출처 명시 허용.
_cors_env = os.environ.get("GLHAC_CORS_ORIGINS", "").strip()
_cors_origins = [o.strip() for o in _cors_env.split(",") if o.strip()]
if _cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["Authorization", "Content-Type"],
    )


@app.exception_handler(Exception)
async def _unhandled_exc(request: Request, exc: Exception):
    """미처리 예외 — 스택 유출 없이 일반화된 500 반환, 서버에는 상세 로깅."""
    log.exception("unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(status_code=500, content={"code": "INTERNAL_ERROR"})


def _sandboxed_path(raw_path: str) -> str:
    """경로 traversal 방지 — GLHAC_UPLOAD_DIR 접두만 허용(P0)."""
    import os as _os
    p = _os.path.realpath(raw_path or "")
    sandbox = _os.path.realpath(_os.environ.get("GLHAC_UPLOAD_DIR", "/tmp"))
    if not p.startswith(sandbox + _os.sep):
        raise HTTPException(400, {"code": "PATH_NOT_ALLOWED", "sandbox": sandbox})
    return p


# 문서 P0(§9.3): 업로드 검증 — 크기·확장자 allowlist·매직바이트 sniff
_UPLOAD_MAX_BYTES = 10 * 1024 * 1024
_UPLOAD_EXT_ALLOW = {"pdf", "png", "jpg", "jpeg", "webp", "gif",
                     "xlsx", "xls", "docx", "doc", "csv", "txt", "hwp"}


def _validate_upload(file_b64, filename, max_bytes=_UPLOAD_MAX_BYTES):
    """base64 업로드 검증 후 정제된 b64 문자열 반환. 위반 시 400/413/415."""
    b64 = (file_b64 or "").split(",")[-1]
    try:
        raw = base64.b64decode(b64)
    except Exception:  # noqa: BLE001
        raise HTTPException(400, {"code": "BAD_UPLOAD"})
    if not raw:
        raise HTTPException(400, {"code": "EMPTY_UPLOAD"})
    if len(raw) > max_bytes:
        raise HTTPException(413, {"code": "UPLOAD_TOO_LARGE", "max_bytes": max_bytes})
    ext = (filename or "").rsplit(".", 1)[-1].lower() if "." in (filename or "") else ""
    if ext and ext not in _UPLOAD_EXT_ALLOW:
        raise HTTPException(415, {"code": "UNSUPPORTED_TYPE", "ext": ext})
    head = raw[:8]
    if head.startswith(b"%PDF"):
        kind = "pdf"
    elif head.startswith(b"\x89PNG"):
        kind = "png"
    elif head.startswith(b"\xff\xd8\xff"):
        kind = "jpg"
    elif head.startswith(b"GIF8"):
        kind = "gif"
    elif head[:4] == b"RIFF":
        kind = "webp"
    elif head.startswith(b"PK\x03\x04"):
        kind = "zip"   # xlsx/docx
    else:
        kind = None
    # 이미지/PDF는 확장자와 실제 내용 일치 강제(위장 차단)
    expect = {"pdf": "pdf", "png": "png", "jpg": "jpg", "jpeg": "jpg", "gif": "gif", "webp": "webp"}
    if ext in expect and kind is not None and kind != expect[ext]:
        raise HTTPException(415, {"code": "CONTENT_MISMATCH", "ext": ext, "sniffed": kind})
    return b64


def _migrate():
    """경량 마이그레이션 — 모델 정의와 기존 테이블을 대조해 누락 컬럼 idempotent 추가.
    create_all은 기존 테이블을 ALTER하지 않으므로, baseline 이후 추가된 전 컬럼을 자동 보강(P0-5)."""
    from sqlalchemy import text as _sql, inspect as _inspect
    insp = _inspect(engine)
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if not insp.has_table(table.name):
                continue  # create_all이 신규 테이블은 처리
            existing = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name in existing:
                    continue
                try:
                    typ = col.type.compile(engine.dialect)
                    conn.execute(_sql(f'ALTER TABLE {table.name} ADD COLUMN "{col.name}" {typ}'))
                except Exception:  # noqa: BLE001 — 제약 있는 컬럼 등은 skip
                    pass


# Phase B: 핫 컬럼 인덱스(테이블스캔 제거) — SQLite/Postgres 양립, 기존 DB 포함.
_HOT_INDEXES = [
    ("material", "case_id"), ("document_asset", "case_id"), ("product", "case_id"),
    ("workflow_event", "case_id"), ("invoice", "case_id"), ("audit_finding", "case_id"),
    ("halal_certificate", "case_id"), ("fatwa_decision", "case_id"),
    ("product_material", "case_id"), ("onsite_checklist", "case_id"),
    ("hpas_evaluation", "case_id"), ("sjph_evidence", "case_id"),
    ("discussion", "case_id"), ("pendamping_assignment", "case_id"),
    ("lph_assignment", "case_id"), ("external_identity", "case_id"),
    ("document_asset", "material_id"), ("document_asset", "product_id"),
]
# 복합/부분 유니크(경합 시 중복행 방지) — CREATE UNIQUE INDEX 는 양 엔진 모두 지원.
_UNIQUE_INDEXES = [
    ("uq_product_material", "product_material", "case_id, product_id, material_id", None),
    ("uq_onsite_item", "onsite_checklist", "case_id, item_key", None),
    ("uq_hpas_element", "hpas_evaluation", "case_id, element", None),
    ("uq_gendoc_version", "generated_document", "case_id, doc_type, version", None),
    ("uq_cert_active", "halal_certificate", "case_id", "status = 'active'"),  # 부분 유니크
]


def _ensure_indexes():
    """인덱스·유니크 인덱스 idempotent 생성. 기존 데이터에 중복이 있으면 유니크 생성만 skip(로그)."""
    from sqlalchemy import text as _sql, inspect as _inspect
    import logging
    log = logging.getLogger("glhac.migrate")
    insp = _inspect(engine)
    tables = set(insp.get_table_names())
    with engine.begin() as conn:
        for table, col in _HOT_INDEXES:
            if table not in tables:
                continue
            name = f"ix_{table}_{col}"
            try:
                conn.execute(_sql(f'CREATE INDEX IF NOT EXISTS {name} ON {table} ({col})'))
            except Exception as e:  # noqa: BLE001
                log.warning("index %s skipped: %s", name, e)
    for name, table, cols, where in _UNIQUE_INDEXES:
        if table not in tables:
            continue
        ddl = f'CREATE UNIQUE INDEX IF NOT EXISTS {name} ON {table} ({cols})'
        if where:
            ddl += f' WHERE {where}'
        try:
            with engine.begin() as conn:   # 개별 트랜잭션 — 중복으로 실패해도 나머지 진행
                conn.execute(_sql(ddl))
        except Exception as e:  # noqa: BLE001
            log.warning("unique index %s skipped (기존 중복 가능): %s", name, e)


def _notify_worker_loop(interval=30):
    """비동기 발송 워커 — GLHAC_NOTIFY_WORKER=1 일 때만 기동. 주기적으로 큐 드레인."""
    import time
    while True:
        time.sleep(interval)
        try:
            db = SessionLocal()
            try:
                drain_notifications(db)
            finally:
                db.close()
        except Exception:  # noqa: BLE001
            pass


@app.on_event("startup")
def _startup():
    auth.enforce_secret()   # 프로덕션에서 기본 시크릿이면 부팅 차단(토큰 위조 방지)
    Base.metadata.create_all(bind=engine)
    _migrate()
    _ensure_indexes()   # Phase B: 핫 인덱스 + 경합 방지 유니크
    db = SessionLocal()
    try:
        seed(db)
        auth.seed_users(db)
        screening.load_ontology(db)
    finally:
        db.close()
    if os.environ.get("GLHAC_NOTIFY_WORKER") == "1":
        import threading
        threading.Thread(target=_notify_worker_loop, daemon=True).start()


def _get_case(db, case_id, user=None) -> models.CaseApplication:
    c = db.get(models.CaseApplication, case_id)
    if not c:
        raise HTTPException(404, {"code": "CASE_NOT_FOUND", "case_id": case_id})
    if user:
        auth.check_org(user, c)
    return c


# ---- 지역(province) 서버 계산 (P1: 프런트 N+1·휴리스틱 제거) ----
_PROVINCES = [
    ("Aceh", []), ("Sumatera Utara", ["medan"]), ("Riau", ["pekanbaru"]),
    ("Sumatera Barat", ["padang"]), ("Sumatera Selatan", ["palembang"]),
    ("Lampung", ["bandar lampung"]), ("Banten", ["serang", "tangerang"]),
    ("DKI Jakarta", ["jakarta"]), ("Jawa Barat", ["bandung", "bekasi", "bogor", "depok"]),
    ("Jawa Tengah", ["semarang", "solo", "surakarta"]), ("DI Yogyakarta", ["yogyakarta", "jogja"]),
    ("Jawa Timur", ["surabaya", "malang"]), ("Bali", ["denpasar"]),
    ("Nusa Tenggara Barat", ["mataram", "lombok"]), ("Nusa Tenggara Timur", ["kupang"]),
    ("Kalimantan Barat", ["pontianak"]), ("Kalimantan Tengah", ["palangkaraya"]),
    ("Kalimantan Selatan", ["banjarmasin"]), ("Kalimantan Timur", ["samarinda", "balikpapan"]),
    ("Sulawesi Selatan", ["makassar"]), ("Sulawesi Tengah", ["palu"]),
    ("Sulawesi Utara", ["manado"]), ("Maluku", ["ambon"]),
    ("Papua Barat", ["manokwari"]), ("Papua", ["jayapura"]),
]


def _province_of(addr):
    if not addr:
        return None
    a = str(addr).lower()
    for name, aliases in _PROVINCES:
        if name.lower() in a:
            return name
        for al in aliases:
            if al in a:
                return name
    return None


def _case_dict(c):
    return {"case_id": c.case_id, "org_id": c.org_id, "company_name": c.company_name,
            "province": _province_of(c.factory_address or c.address),
            "status": c.status, "pathway": c.pathway, "risk_category": c.risk_category,
            "is_msme": c.is_msme, "sehati_eligible": c.sehati_eligible,
            "fatwa_status": c.fatwa_status, "scope_frozen": c.scope_frozen,
            "nib": c.nib, "responsible_person": c.responsible_person,
            "halal_supervisor": c.halal_supervisor, "email": c.email, "phone": c.phone,
            "address": c.address, "factory_reg_no": c.factory_reg_no,
            "factory_address": c.factory_address, "due_date": c.due_date,
            "notify_consent": bool(c.notify_consent)}


def _notify(db, case, event_type, title, body="", channels=None, role=None):
    """이벤트 알림을 큐(unsent)에 적재. 실제 발송은 비동기 워커(drain)가 처리 — Rizky #5."""
    n = models.Notification(
        org_id=(case.org_id if case else None), case_id=(case.case_id if case else None),
        role=role, event_type=event_type, channels=channels or ["inapp"],
        title=title, body=body, status="unsent")
    db.add(n)
    return n


_NOTIFY_NONRETRY = ("no_credentials", "no_contact", "not_implemented")


def drain_notifications(db, batch=50, max_attempts=3):
    """미발송 알림 큐를 드레인 — 원자 클레임(중복발송 방지) + 수신동의 게이트 + 재시도."""
    from . import notify as _nt
    # 크래시로 남은 'sending'(고아) 회수 — 단일 워커 가정. 멀티워커는 리스 타임스탬프 필요.
    db.query(models.Notification).filter(models.Notification.status == "sending").update(
        {"status": "unsent"}, synchronize_session=False)
    db.commit()
    # 원자적 클레임: unsent → sending. 동시 드레인/워커가 같은 행을 이중 발송하지 못하게.
    candidates = (db.query(models.Notification).filter(models.Notification.status == "unsent")
                  .order_by(models.Notification.created_at).limit(batch).all())
    claimed = []
    for n in candidates:
        got = (db.query(models.Notification)
               .filter(models.Notification.notification_id == n.notification_id,
                       models.Notification.status == "unsent")
               .update({"status": "sending"}, synchronize_session=False))
        if got:
            claimed.append(n)
    db.commit()

    stats = {"processed": 0, "sent": 0, "retried": 0, "failed": 0, "consent_skipped": 0}
    for n in claimed:
        stats["processed"] += 1
        contact, consent = None, False
        if n.case_id:
            c = db.get(models.CaseApplication, n.case_id)
            contact = c.phone if c else None
            consent = bool(c.notify_consent) if c else False
        # 수신동의 게이트 — 외부채널(sms/whatsapp/kakao)은 동의 시에만. inapp은 항상 발송.
        requested = n.channels or ["inapp"]
        eff = [ch for ch in requested if ch == "inapp" or consent]
        dropped = [ch for ch in requested if ch != "inapp" and not consent]
        try:
            results = _nt.dispatch(n, contact=contact, channels=eff)
        except Exception as e:  # noqa: BLE001
            results = [{"channel": "?", "ok": False, "reason": str(e)}]
        retryable = any((not r.get("ok")) and r.get("reason") not in _NOTIFY_NONRETRY
                        for r in results)
        n.attempts = (n.attempts or 0) + 1
        if not retryable:
            n.status = "sent"
            stats["sent"] += 1
            if dropped:
                n.last_error = "consent_skipped:" + ",".join(dropped)
                stats["consent_skipped"] += 1
        elif n.attempts >= max_attempts:
            n.status = "failed"
            n.last_error = "max_attempts"
            stats["failed"] += 1
        else:
            n.status = "unsent"   # 재시도 위해 큐로 복귀
            n.last_error = "; ".join(r.get("reason", "") for r in results if not r.get("ok"))
            stats["retried"] += 1
    db.commit()
    return stats


@app.post("/admin/notify-drain")
def notify_drain(user=Depends(auth.require_roles()), db: Session = Depends(get_db)):
    """미발송 알림 큐 드레인(관리자/cron 트리거) — 운영 시 주기 실행."""
    return drain_notifications(db)


def _save_ai_extraction(db, case_id, source, extracted, confidence=None, evidence=None,
                        model_name="local-ocr", model_version="v3"):
    """AI/OCR 결과 근거저장 (§7.2) — 원문 근거·신뢰도·모델버전·리뷰상태 함께 보존."""
    row = models.AiExtraction(case_id=case_id, source=source, model_provider="local",
                              model_name=model_name, model_version=model_version,
                              extracted_json=extracted, confidence=confidence,
                              evidence=(evidence or "")[:4000], reviewer_status="unreviewed")
    db.add(row)
    db.flush()
    return row


def _register_from_judgment(db, c, res):
    created = []
    for ing in res["ingredients"]:
        m = models.Material(case_id=c.case_id, name=ing["name"], e_number=ing.get("e_number"),
                            screen_result=ing["result"], screen_status=ing["status"],
                            screen_severity=ing["severity"], matched_uid=ing.get("matched_uid"))
        db.add(m)
        db.flush()
        created.append({"material_id": m.material_id, "name": m.name, "result": m.screen_result})
    # §7.2 AI 근거저장 — 라벨 판정 원문/신뢰도 보존(Human-in-the-loop 추적)
    ext = _save_ai_extraction(db, c.case_id, "label_judgment",
                              {"ingredients": res.get("ingredients"),
                               "critical_count": res.get("critical_count"),
                               "pathway_implication": res.get("pathway_implication")},
                              confidence=res.get("confidence"),
                              evidence=res.get("raw_text") or res.get("explanation", ""),
                              model_name="ocr_pipeline")
    sm.record_event(db, c, c.status, c.status, "materials.from_label", "ai", None,
                    {"count": len(created), "source": "label_ocr", "ai_extraction_id": ext.id})
    db.commit()
    return {"created": created, "count": len(created),
            "critical_count": res["critical_count"], "pathway_implication": res["pathway_implication"],
            "explanation": res.get("explanation", ""), "ai_extraction_id": ext.id}


# ---------- health / auth ----------
@app.get("/health")
def health(db: Session = Depends(get_db)):
    """라이브니스+DB 체크 — 로드밸런서/오케스트레이터용."""
    try:
        from sqlalchemy import text as _sql
        db.execute(_sql("SELECT 1"))
        dbok = True
    except Exception:  # noqa: BLE001
        dbok = False
    status = "ok" if dbok else "degraded"
    return JSONResponse(status_code=200 if dbok else 503,
                        content={"status": status, "service": "glhac-ai", "version": "0.2.0", "db": dbok})


@app.get("/public-config")
def public_config():
    """로그인 화면용 공개 설정 — dev 모드에서만 데모 계정 노출."""
    return {"dev_mode": auth.dev_mode()}


@app.get("/verify/{qr_token}")
def verify_certificate(qr_token: str, db: Session = Depends(get_db)):
    """공개 인증서 검증(§11.5·§14 P1) — 인증 불필요, 민감정보 미노출."""
    cert = db.query(models.HalalCertificate).filter_by(qr_token=qr_token).first()
    if not cert:
        raise HTTPException(404, {"code": "CERT_NOT_FOUND", "valid": False})
    c = db.get(models.CaseApplication, cert.case_id)
    valid = cert.status == "active"
    try:
        if valid and date.fromisoformat(cert.expiry_date) < date.today():
            valid = False
    except Exception:  # noqa: BLE001
        pass
    return {"valid": valid, "certificate_no": cert.certificate_no,
            "company_name": (c.company_name if c else None),
            "status": cert.status, "issue_date": cert.issue_date,
            "expiry_date": cert.expiry_date, "scope": cert.scope}


@app.get("/rbac/actions")
def rbac_actions(user=Depends(auth.get_current_user)):
    """UI 버튼 노출용 — 현재 사용자가 수행 가능한 action_id 맵(단일 매트릭스 파생)."""
    return {"role": user["role"], "actions": rbac.allowed_actions(user)}


@app.get("/rbac/matrix")
def rbac_matrix(user=Depends(auth.require_roles())):
    """전체 action_id -> 허용 역할 매트릭스(admin 전용, 투명성/감사)."""
    return {"matrix": {a: sorted(r) for a, r in rbac.ACTION_ROLES.items()},
            "endpoints": {a: {"method": m, "path": p} for a, (m, p, _b) in rbac.ACTION_ENDPOINTS.items()}}


@app.get("/ai/health")
def ai_health():
    return ai_local.health()


@app.post("/auth/login")
def login(body: schemas.LoginReq, db: Session = Depends(get_db)):
    rl_key = (body.username or "").lower()
    if auth.rate_limited(rl_key):
        raise HTTPException(429, {"code": "TOO_MANY_ATTEMPTS", "retry_after_sec": auth._RL_WINDOW})
    u = db.query(models.User).filter_by(username=body.username).first()
    if not u or not auth.verify_pw(body.password, u.password_hash):
        auth.record_attempt(rl_key)
        raise HTTPException(401, {"code": "BAD_CREDENTIALS"})
    auth.clear_attempts(rl_key)
    if auth.needs_rehash(u.password_hash):   # 레거시 sha256 → pbkdf2 자동 승격
        u.password_hash = auth.hash_pw(body.password)
        db.commit()
    return {"token": auth.make_token(u), "role": u.role, "org_id": u.org_id, "username": u.username}


def _parse_biz_doc(text):
    """사업자/공장 등록증 OCR 텍스트 → 프로필 필드 추출 (Rizky #1)."""
    import re
    t = text or ""
    out = {}
    m = re.search(r"\d{3}-\d{2}-\d{5}", t)                       # 사업자등록번호
    if m:
        out["nib"] = m.group(0)
    for label, key in [("상호", "company_name"), ("법인명", "company_name"),
                       ("공장명", "factory_name"), ("대표자", "responsible_person"),
                       ("성명", "responsible_person")]:
        mm = re.search(label + r"[)\s:：·]*([^\n]{1,40})", t)
        if mm and not out.get(key):
            out[key] = mm.group(1).strip(" :：·|")
    mm = re.search(r"(사업장\s*소재지|소재지|사업장|주소)[)\s:：·]*([^\n]{2,80})", t)
    if mm:
        out["address"] = mm.group(2).strip(" :：·|")
    mm = re.search(r"(공장\s*소재지|공장\s*주소)[)\s:：·]*([^\n]{2,80})", t)
    if mm:
        out["factory_address"] = mm.group(2).strip(" :：·|")
    mm = re.search(r"(업태|업종|종목)[)\s:：·]*([^\n]{1,40})", t)
    if mm:
        out["business_type"] = mm.group(2).strip(" :：·|")
    return out


@app.post("/auth/ocr-extract")
def auth_ocr_extract(body: schemas.OCRExtractReq):
    """회원가입 전 등록증 OCR 자동추출(공개) — 이미지 b64만 수용(경로 없음)."""
    import base64
    import tempfile
    import os as _os
    try:
        raw = base64.b64decode(body.image_b64)
    except Exception:  # noqa: BLE001
        raise HTTPException(400, {"code": "BAD_IMAGE"})
    path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            f.write(raw)
            path = f.name
        res = ai_local.ocr_image(path, "korean")
    finally:
        if path:
            try:
                _os.unlink(path)
            except Exception:  # noqa: BLE001
                pass
    if not res.get("ok"):
        return {"ocr_available": False, "fields": {}, "error": res.get("error")}
    text = "\n".join(l.get("text", "") for l in res.get("lines", []))
    return {"ocr_available": True, "fields": _parse_biz_doc(text), "raw": text[:1500]}


@app.post("/auth/register")
def register(body: schemas.RegisterReq, db: Session = Depends(get_db)):
    if db.query(models.User).filter_by(username=body.username).first():
        raise HTTPException(409, {"code": "DUPLICATE_ACCOUNT"})
    org = "org_" + models.uid()[:8]
    u = models.User(username=body.username, password_hash=auth.hash_pw(body.password),
                    role="applicant", org_id=org)
    db.add(u)
    # OCR 추출 프로필이 있으면 초기 케이스에 프리필(Rizky #1)
    if body.company_name or body.nib:
        c = models.CaseApplication(org_id=org, company_name=body.company_name or "My Company",
                                   nib=body.nib, responsible_person=body.responsible_person,
                                   address=body.address, factory_address=body.factory_address,
                                   is_msme=True)
        db.add(c)
        db.flush()
        sm.record_event(db, c, None, "onboarding", "case.create.register", "applicant", u.user_id)
    db.commit()
    return {"token": auth.make_token(u), "role": u.role, "org_id": u.org_id, "username": u.username}


@app.get("/auth/me")
def me(user=Depends(auth.get_current_user)):
    return user


# ===================== admin 시스템 설정 (admin 전용) =====================
_ALL_ROLES = ["applicant", "consultant", "penyelia_halal", "pendamping_pph",
              "auditor", "fatwa_liaison", "operator", "admin"]


@app.get("/admin/users")
def admin_list_users(user=Depends(auth.require_roles()), db: Session = Depends(get_db),
                     limit: int = Query(500, ge=1, le=1000), offset: int = Query(0, ge=0),
                     q: str = Query(""), sort: str = Query("username"),
                     dir: str = Query("asc"), meta: int = Query(0)):
    """DataList 표준(§8.2): 서버 검색·정렬·페이지네이션·meta(total)."""
    M = models.User
    Q = db.query(M)
    if q:
        like = "%%%s%%" % q
        Q = Q.filter(or_(M.username.ilike(like), M.role.ilike(like), M.org_id.ilike(like)))
    total = Q.count()
    col = {"username": M.username, "role": M.role, "org_id": M.org_id}.get(sort, M.username)
    Q = Q.order_by(col.desc() if dir == "desc" else col.asc())
    rows = Q.offset(offset).limit(limit).all()
    items = [{"user_id": u.user_id, "username": u.username, "role": u.role, "org_id": u.org_id} for u in rows]
    return {"items": items, "total": total, "limit": limit, "offset": offset} if meta else items


@app.post("/admin/users")
def admin_create_user(body: schemas.AdminUserReq, user=Depends(rbac.require_action("admin.user.manage")),
                      db: Session = Depends(get_db)):
    if body.role not in _ALL_ROLES:
        raise HTTPException(422, {"code": "BAD_ROLE", "allowed": _ALL_ROLES})
    if db.query(models.User).filter_by(username=body.username).first():
        raise HTTPException(409, {"code": "DUPLICATE_USER"})
    u = models.User(username=body.username, password_hash=auth.hash_pw(body.password),
                    role=body.role, org_id=body.org_id or "org_demo")
    db.add(u)
    db.commit()
    return {"user_id": u.user_id, "username": u.username, "role": u.role, "org_id": u.org_id}


@app.patch("/admin/users/{user_id}")
def admin_patch_user(user_id: str, body: schemas.AdminUserPatchReq,
                     user=Depends(auth.require_roles()), db: Session = Depends(get_db)):
    u = db.get(models.User, user_id)
    if not u:
        raise HTTPException(404, {"code": "USER_NOT_FOUND"})
    if body.role is not None:
        if body.role not in _ALL_ROLES:
            raise HTTPException(422, {"code": "BAD_ROLE"})
        u.role = body.role
    if body.password:
        u.password_hash = auth.hash_pw(body.password)
    db.commit()
    return {"user_id": u.user_id, "username": u.username, "role": u.role}


@app.delete("/admin/users/{user_id}")
def admin_delete_user(user_id: str, user=Depends(auth.require_roles()),
                      db: Session = Depends(get_db)):
    u = db.get(models.User, user_id)
    if u:
        if u.username == "admin":
            raise HTTPException(400, {"code": "CANNOT_DELETE_ADMIN"})
        db.delete(u)
        db.commit()
    return {"deleted": user_id}


@app.get("/admin/orgs")
def admin_list_orgs(user=Depends(auth.require_roles()), db: Session = Depends(get_db)):
    orgs = {}
    for o in db.query(models.Org).all():
        orgs[o.org_id] = {"org_id": o.org_id, "name": o.name, "users": 0, "cases": 0}
    for u in db.query(models.User).all():
        orgs.setdefault(u.org_id, {"org_id": u.org_id, "name": None, "users": 0, "cases": 0})["users"] += 1
    for c in db.query(models.CaseApplication).all():
        orgs.setdefault(c.org_id, {"org_id": c.org_id, "name": None, "users": 0, "cases": 0})["cases"] += 1
    return list(orgs.values())


@app.post("/admin/orgs")
def admin_create_org(body: schemas.AdminOrgReq, user=Depends(auth.require_roles()),
                     db: Session = Depends(get_db)):
    if not db.get(models.Org, body.org_id):
        db.add(models.Org(org_id=body.org_id, name=body.name))
        db.commit()
    return {"org_id": body.org_id, "name": body.name}


@app.get("/admin/cases")
def admin_all_cases(user=Depends(auth.require_roles()), db: Session = Depends(get_db),
                    limit: int = Query(500, ge=1, le=1000), offset: int = Query(0, ge=0),
                    q: str = Query(""), sort: str = Query("created_at"),
                    dir: str = Query("desc"), meta: int = Query(0)):
    """DataList 표준(§8.2): 서버 검색(q)·정렬(sort/dir)·페이지네이션(limit/offset)·meta(total)."""
    M = models.CaseApplication
    Q = db.query(M)
    if q:
        like = "%%%s%%" % q
        Q = Q.filter(or_(M.company_name.ilike(like), M.status.ilike(like), M.org_id.ilike(like)))
    total = Q.count()
    col = {"company_name": M.company_name, "status": M.status,
           "org_id": M.org_id, "created_at": M.created_at}.get(sort, M.created_at)
    Q = Q.order_by(col.asc() if dir == "asc" else col.desc())
    rows = Q.offset(offset).limit(limit).all()
    items = [{"case_id": c.case_id, "org_id": c.org_id, "company_name": c.company_name,
              "status": c.status, "pathway": c.pathway, "fatwa_status": c.fatwa_status} for c in rows]
    return {"items": items, "total": total, "limit": limit, "offset": offset} if meta else items


@app.get("/admin/ontology/stats")
def admin_ontology_stats(user=Depends(auth.require_roles()), db: Session = Depends(get_db)):
    n = db.query(models.IngredientOntology).count()
    rules = [{"code": r.code, "jurisdiction": r.jurisdiction, "status": r.status}
             for r in db.query(models.RuleVersion).all()]
    return {"ontology_count": n, "rule_versions": rules}


@app.post("/admin/ontology/reseed")
def admin_ontology_reseed(user=Depends(auth.require_roles()), db: Session = Depends(get_db)):
    from .ontology_seed import seed
    db.query(models.IngredientOntology).delete()
    db.commit()
    seed(db)
    return {"reseeded": True, "ontology_count": db.query(models.IngredientOntology).count()}


@app.get("/admin/kma1360-exempt")
def admin_kma_exempt(user=Depends(auth.require_roles())):
    from .screening import KMA1360_EXEMPT
    return {"terms": sorted(KMA1360_EXEMPT)}


@app.get("/admin/audit")
def admin_global_audit(user=Depends(auth.require_roles()), db: Session = Depends(get_db)):
    evs = (db.query(models.WorkflowEvent)
           .order_by(models.WorkflowEvent.created_at.desc()).limit(60).all())
    return [{"case_id": e.case_id, "from": e.from_status, "to": e.to_status, "action": e.action,
             "actor": e.actor_type, "hash": (e.row_hash or "")[:8], "at": str(e.created_at)}
            for e in evs]


@app.post("/admin/seed-reset")
def admin_seed_reset(body: schemas.SeedResetReq, user=Depends(auth.require_roles()),
                     db: Session = Depends(get_db)):
    if not auth.dev_mode():
        raise HTTPException(403, {"code": "DEMO_SEED_DISABLED", "hint": "GLHAC_DEV=1 에서만 허용"})
    if body.confirm != "RESET":
        raise HTTPException(400, {"code": "CONFIRM_REQUIRED", "hint": "confirm='RESET'"})
    added = []
    for u, pw, role, org in auth.DEFAULT_USERS:
        if not db.query(models.User).filter_by(username=u).first():
            db.add(models.User(username=u, password_hash=auth.hash_pw(pw), role=role, org_id=org))
            added.append(u)
    db.commit()
    return {"reset": True, "demo_users_ensured": added}


@app.get("/cases/{case_id}/audit-verify")
def audit_verify(case_id: str, user=Depends(auth.get_current_user), db: Session = Depends(get_db)):
    """감사 해시체인 재계산 검증 (B.5 replay)."""
    import hashlib
    import json as _json
    _get_case(db, case_id, user)
    evs = (db.query(models.WorkflowEvent).filter_by(case_id=case_id)
           .order_by(models.WorkflowEvent.created_at, models.WorkflowEvent.event_id).all())
    prev, broken = "", None
    for e in evs:
        body = _json.dumps({"case": e.case_id, "from": e.from_status, "to": e.to_status,
                            "action": e.action, "payload": e.payload or {}},
                           sort_keys=True, ensure_ascii=False)
        rh = hashlib.sha256((prev + body).encode("utf-8")).hexdigest()
        if rh != e.row_hash:
            broken = e.event_id
            break
        prev = e.row_hash
    return {"count": len(evs), "integrity_ok": broken is None, "broken_at": broken}


@app.post("/cases/{case_id}/report")
def gen_report(case_id: str, user=Depends(auth.get_current_user), db: Session = Depends(get_db)):
    """종합 준비 보고서 (서버 생성, 결정적)."""
    c = _get_case(db, case_id, user)
    mats = db.query(models.Material).filter_by(case_id=case_id).all()
    crit = [m.name for m in mats if m.screen_result in ("BLOCK", "NEEDS_EVIDENCE")]
    blockers = sm.evaluate_blocking(db, c)
    have = _ensure_hpas(db, case_id)
    findings = db.query(models.AuditFinding).filter_by(case_id=case_id).all()
    invs = db.query(models.Invoice).filter_by(case_id=case_id).all()
    cert = db.query(models.HalalCertificate).filter_by(case_id=case_id).first()
    prods = [p.name for p in db.query(models.Product).filter_by(case_id=case_id)]
    L = ["=" * 50, "AI 할랄 인증 준비 보고서", "=" * 50,
         "회사: %s (NIB: %s)" % (c.company_name or "-", c.nib or "-"),
         "경로: %s · 상태: %s · MSME: %s" % (c.pathway, c.status, c.is_msme),
         "공장: %s / %s" % (c.factory_reg_no or "-", c.factory_address or "-"), "",
         "[제품] %s" % (", ".join(prods) or "-"),
         "[원재료] %d건, 증빙필요/차단 %d건: %s" % (len(mats), len(crit), ", ".join(crit[:15]) or "없음"),
         "[차단사유] %s" % (", ".join(b["code"] for b in blockers) or "없음"),
         "[SJPH 5요소] " + ", ".join("%s=%s" % (k, have[k].status) for k in HPAS_ELEMENTS),
         "[심사지적] 총 %d (미해결 %d)" % (len(findings), sum(1 for f in findings if f.status == "open")),
         "[청구] %d건 (미결제 %d)" % (len(invs), sum(1 for i in invs if i.status == "unpaid")),
         "[Fatwa] %s · scope동결 %s" % (c.fatwa_status, c.scope_frozen),
         "[인증서] %s" % (cert.certificate_no if cert else "미발급"), "",
         "※ 준비용 보고서. 공식 발급은 BPJPH/SIHALAL 절차로 확정."]
    return {"report": "\n".join(L)}


# ---------- cases ----------
@app.get("/cases")
def list_cases(user=Depends(auth.get_current_user), db: Session = Depends(get_db),
               limit: int = Query(500, ge=1, le=1000), offset: int = Query(0, ge=0)):
    q = db.query(models.CaseApplication)
    if user["role"] != "admin":
        q = q.filter_by(org_id=user["org_id"])
    rows = (q.order_by(models.CaseApplication.created_at.desc())
            .offset(offset).limit(limit).all())
    return [{"case_id": c.case_id, "company_name": c.company_name, "status": c.status,
             "pathway": c.pathway, "due_date": c.due_date,
             "province": _province_of(c.factory_address or c.address)}
            for c in rows]


# ===================== 알림 (Rizky #5) =====================
def _my_notifs(db, user):
    q = db.query(models.Notification)
    if user["role"] != "admin":
        q = q.filter_by(org_id=user["org_id"])
    return [n for n in q.order_by(models.Notification.created_at.desc()).limit(80).all()
            if not n.role or n.role == user["role"]]


@app.get("/notifications")
def list_notifications(user=Depends(auth.get_current_user), db: Session = Depends(get_db)):
    return [{"id": n.notification_id, "case_id": n.case_id, "event_type": n.event_type,
             "title": n.title, "body": n.body, "read": bool(n.read), "status": n.status,
             "channels": n.channels, "created_at": str(n.created_at)} for n in _my_notifs(db, user)]


@app.get("/notifications/unread-count")
def unread_count(user=Depends(auth.get_current_user), db: Session = Depends(get_db)):
    return {"count": sum(1 for n in _my_notifs(db, user) if not n.read)}


@app.post("/notifications/{nid}/read")
def read_notification(nid: str, user=Depends(auth.get_current_user), db: Session = Depends(get_db)):
    n = db.get(models.Notification, nid)
    if not n:
        raise HTTPException(404, {"code": "NOTIF_NOT_FOUND"})
    if user["role"] != "admin" and n.org_id != user["org_id"]:
        raise HTTPException(403, {"code": "FORBIDDEN_ORG"})
    n.read = True
    db.commit()
    return {"ok": True}


@app.post("/notifications/read-all")
def read_all_notifications(user=Depends(auth.get_current_user), db: Session = Depends(get_db)):
    for n in _my_notifs(db, user):
        n.read = True
    db.commit()
    return {"ok": True}


@app.post("/admin/scan-expiry")
def scan_expiry(user=Depends(auth.require_roles()), db: Session = Depends(get_db)):
    """만료 임박 인증서 스캔 → 90/60/30일 알림 생성 (Rizky #7). 운영 시 cron 주기 실행."""
    from datetime import date as _date
    today = _date.today()
    created = 0
    for cert in db.query(models.HalalCertificate).filter_by(status="active").all():
        try:
            days = (_date.fromisoformat(cert.expiry_date) - today).days
        except Exception:  # noqa: BLE001
            continue
        th = next((t for t in (30, 60, 90) if days <= t), None)
        if th is None or days < 0:
            continue
        c = db.get(models.CaseApplication, cert.case_id)
        if not c:
            continue
        dup = (db.query(models.Notification)
               .filter(models.Notification.case_id == cert.case_id,
                       models.Notification.event_type == "expiry_soon",
                       models.Notification.title.like("%%%d일 전%%" % th)).first())
        if dup:
            continue
        _notify(db, c, "expiry_soon", "인증서 만료 임박 · %d일 전" % th,
                "%s — 인증서 %s 만료 D-%d (만료 %s). 갱신하세요." % (
                    c.company_name or "", cert.certificate_no or "", days, cert.expiry_date),
                channels=["inapp", "sms", "kakao", "whatsapp"], role="applicant")
        created += 1
    db.commit()
    return {"created": created}


@app.post("/cases")
def create_case(body: schemas.CaseCreate, user=Depends(auth.require_roles("applicant", "consultant")),
                db: Session = Depends(get_db)):
    org = body.org_id if (user["role"] == "admin" and body.org_id) else user["org_id"]
    c = models.CaseApplication(org_id=org, company_name=body.company_name, is_msme=bool(body.is_msme))
    db.add(c)
    db.flush()
    sm.record_event(db, c, None, "onboarding", "case.create", user["role"], user["uid"])
    db.commit()
    return _case_dict(c)


# ── v3: 모의 심사(mock audit) — 내부 심사(클라이언트 미노출) ──────────────
MOCK_AUDIT_STAGES = {
    "document_pre_audit_requested", "document_pre_audit_in_review",
    "document_pre_audit_approved", "onsite_audit_scheduled",
    "onsite_audit_in_progress", "hpas_evaluation_ready", "final_package_preparation",
}
# 현재 상태 → (pass 시 다음 긍정 단계, reject 시 시정 단계). 유효 전이일 때만 적용.
_MOCK_NEXT = {
    "document_pre_audit_requested": ("document_pre_audit_in_review", None),
    "document_pre_audit_in_review": ("document_pre_audit_approved", None),
    "onsite_audit_in_progress": ("audit_closed", "corrective_action_required"),
    "corrective_action_submitted": ("audit_closed", "corrective_action_required"),
}


@app.get("/mock-audit/queue")
def mock_audit_queue(user=Depends(auth.require_roles("auditor", "fatwa_liaison", "operator")),
                     db: Session = Depends(get_db)):
    """모의심사 대상 큐. 사전심사~최종패키지 단계 케이스(조직 스코프)."""
    q = db.query(models.CaseApplication)
    if user["role"] != "admin":
        q = q.filter_by(org_id=user["org_id"])
    out = []
    for c in q.order_by(models.CaseApplication.created_at.desc()).all():
        if c.status in MOCK_AUDIT_STAGES:
            out.append({"case_id": c.case_id, "company": c.company_name,
                        "stage": c.status, "pathway": c.pathway})
    return out


@app.get("/cases/{case_id}/mock-audit")
def mock_audit_get(case_id: str, user=Depends(auth.get_current_user), db: Session = Depends(get_db)):
    c = _get_case(db, case_id, user)
    evs = (db.query(models.WorkflowEvent)
           .filter(models.WorkflowEvent.case_id == case_id,
                   models.WorkflowEvent.action.like("mock_audit.%"))
           .order_by(models.WorkflowEvent.created_at.desc()).all())
    return {"case_id": case_id, "status": c.status,
            "decisions": [{"result": (e.payload or {}).get("result"),
                           "reason": (e.payload or {}).get("reason"),
                           "actor": e.actor_id,
                           "at": e.created_at.isoformat() if e.created_at else None} for e in evs]}


@app.post("/cases/{case_id}/mock-audit/decision")
def mock_audit_decide(case_id: str, body: schemas.MockAuditDecisionReq,
                      user=Depends(auth.require_roles("auditor", "fatwa_liaison", "operator")),
                      db: Session = Depends(get_db)):
    """모의심사 판정(통과/거부). 감사 이벤트로 기록. 거부 시 사유 필수. 클라이언트 미노출."""
    c = _get_case(db, case_id, user)
    result = (body.result or "").lower()
    if result not in ("pass", "reject"):
        raise HTTPException(422, {"code": "INVALID_RESULT", "allowed": ["pass", "reject"]})
    if result == "reject" and not (body.reason or "").strip():
        raise HTTPException(422, {"code": "REASON_REQUIRED"})
    sm.record_event(db, c, c.status, c.status, "mock_audit.decision", user["role"], user["uid"],
                    {"result": result, "reason": body.reason or ""})
    # ② 상태전이 연동: pass→다음 긍정 단계, reject→시정조치 (유효 전이·가드 통과 시에만)
    transitioned_to = None
    mapping = _MOCK_NEXT.get(c.status)
    if mapping:
        target = mapping[0] if result == "pass" else mapping[1]
        if target:
            ok, _blk = sm.can_transition(db, c, target)
            if ok:
                frm = c.status
                sm.apply_side_effects(c, target)
                c.status = target
                sm.record_event(db, c, frm, target, "mock_audit." + result, user["role"], user["uid"],
                                {"reason": body.reason or ""})
                transitioned_to = target
    db.commit()
    return {"ok": True, "result": result, "transitioned_to": transitioned_to}


# ── v3: 역할별 작업 큐(worklist) — 조직 스코프, read-only ──────────────
AUDIT_STAGES = {"document_pre_audit_requested", "document_pre_audit_in_review",
                "document_pre_audit_approved", "lph_assignment", "onsite_audit_scheduled",
                "onsite_audit_in_progress", "corrective_action_required",
                "corrective_action_submitted"}
FATWA_STAGES = {"fatwa_review"}


def _stage_queue(db, user, stages):
    q = db.query(models.CaseApplication)
    if user["role"] != "admin":
        q = q.filter_by(org_id=user["org_id"])
    return [{"case_id": c.case_id, "company": c.company_name, "stage": c.status, "pathway": c.pathway}
            for c in q.order_by(models.CaseApplication.created_at.desc()).all() if c.status in stages]


@app.get("/audit/queue")
def audit_queue(user=Depends(auth.require_roles("auditor", "operator")),
                db: Session = Depends(get_db)):
    """심사 큐 — 사전심사~시정조치 단계 케이스(오디터·운영자)."""
    return _stage_queue(db, user, AUDIT_STAGES)


@app.get("/fatwa/queue")
def fatwa_queue(user=Depends(auth.require_roles("fatwa_liaison", "operator")),
                db: Session = Depends(get_db)):
    """파트와 심의 대기 큐 — fatwa_review 단계 케이스(샤리아·운영자)."""
    return _stage_queue(db, user, FATWA_STAGES)


@app.get("/cases/{case_id}")
def get_case(case_id: str, user=Depends(auth.get_current_user), db: Session = Depends(get_db)):
    return _case_dict(_get_case(db, case_id, user))


@app.get("/cases/{case_id}/timeline")
def timeline(case_id: str, user=Depends(auth.get_current_user), db: Session = Depends(get_db)):
    c = _get_case(db, case_id, user)
    evs = (db.query(models.WorkflowEvent).filter_by(case_id=case_id)
           .order_by(models.WorkflowEvent.created_at).all())
    return {"case": _case_dict(c), "blockers": sm.evaluate_blocking(db, c),
            "events": [{"from": e.from_status, "to": e.to_status, "action": e.action,
                        "actor": e.actor_type, "hash": (e.row_hash or "")[:12]} for e in evs]}


@app.get("/cases/{case_id}/materials")
def list_materials(case_id: str, user=Depends(auth.get_current_user), db: Session = Depends(get_db)):
    _get_case(db, case_id, user)
    rows = db.query(models.Material).filter_by(case_id=case_id).all()
    ev = dict(db.query(models.DocumentAsset.material_id, func.count(models.DocumentAsset.document_id))
              .filter(models.DocumentAsset.case_id == case_id,
                      models.DocumentAsset.material_id.isnot(None))
              .group_by(models.DocumentAsset.material_id).all())
    return [{"material_id": m.material_id, "name": m.name, "e_number": m.e_number,
             "mat_type": m.mat_type, "source": m.source, "supplier": m.supplier,
             "cert": m.cert, "cert_no": m.cert_no, "v1_risk": m.v1_risk, "note": m.note,
             "result": m.screen_result, "status": m.screen_status, "severity": m.screen_severity,
             "matched_uid": m.matched_uid, "evidence_count": ev.get(m.material_id, 0)} for m in rows]


# ---------- products / materials ----------
@app.post("/cases/{case_id}/products")
def add_product(case_id: str, body: schemas.ProductCreate,
                user=Depends(auth.require_roles("applicant", "consultant")),
                db: Session = Depends(get_db)):
    _get_case(db, case_id, user)
    p = models.Product(case_id=case_id, name=body.name, category=body.category)
    db.add(p)
    db.commit()
    return {"product_id": p.product_id}


@app.post("/cases/{case_id}/materials")
def add_material(case_id: str, body: schemas.MaterialCreate,
                 user=Depends(auth.require_roles("applicant", "consultant", "penyelia_halal")),
                 db: Session = Depends(get_db)):
    _get_case(db, case_id, user)
    sk = True if body.source_known is None else bool(body.source_known)
    r = screening.screen_merged(body.name, body.e_number, body.source, body.cert_no,
                                bool(body.evidence_provided), sk, body.note or "")
    m = models.Material(case_id=case_id, name=body.name, e_number=body.e_number,
                        mat_type=body.mat_type, source=body.source, supplier=body.supplier,
                        cert=r.get("v1_cert"), cert_no=body.cert_no, note=body.note,
                        v1_risk=r.get("v1_risk"), evidence_provided=bool(body.evidence_provided),
                        source_known=sk, screen_result=r["result"], screen_status=r["status"],
                        screen_severity=r["severity"], matched_uid=r.get("matched_uid"))
    db.add(m)
    db.commit()
    return {"material_id": m.material_id, "screen": r}


@app.delete("/materials/{material_id}")
def delete_material(material_id: str,
                    user=Depends(auth.require_roles("applicant", "consultant", "penyelia_halal")),
                    db: Session = Depends(get_db)):
    m = db.get(models.Material, material_id)
    if m:
        _get_case(db, m.case_id, user)
        db.delete(m)
        db.commit()
    return {"deleted": material_id}


@app.post("/ai/screen-text")
def ai_screen_text(body: schemas.TextScreenReq, user=Depends(auth.get_current_user)):
    """v1 AI 성분 스캐너(텍스트) 상속 + ontology 매칭."""
    return screening.screen_text(body.text)


@app.get("/cases/{case_id}/products")
def list_products(case_id: str, user=Depends(auth.get_current_user), db: Session = Depends(get_db)):
    _get_case(db, case_id, user)
    rows = db.query(models.Product).filter_by(case_id=case_id).all()
    ph = dict(db.query(models.DocumentAsset.product_id, func.count(models.DocumentAsset.document_id))
              .filter(models.DocumentAsset.case_id == case_id,
                      models.DocumentAsset.doc_type == "product_photo",
                      models.DocumentAsset.product_id.isnot(None))
              .group_by(models.DocumentAsset.product_id).all())
    return [{"product_id": p.product_id, "name": p.name, "category": p.category,
             "photo_count": ph.get(p.product_id, 0)} for p in rows]


@app.post("/cases/{case_id}/materials/{material_id}/evidence")
def add_material_evidence(case_id: str, material_id: str, body: schemas.MaterialEvidenceReq,
                          user=Depends(auth.require_roles("applicant", "consultant", "penyelia_halal")),
                          db: Session = Depends(get_db)):
    """원재료 증빙 업로드 → evidence_provided=true → 자동 재스크리닝 (순환점 C1)."""
    from .intake import _ctype
    c = _get_case(db, case_id, user)
    m = db.get(models.Material, material_id)
    if not m or m.case_id != case_id:
        raise HTTPException(404, {"code": "MATERIAL_NOT_FOUND"})
    b64 = _validate_upload(body.file_b64, body.filename)
    db.add(models.DocumentAsset(case_id=case_id, filename=body.filename, doc_type=body.evidence_type,
                                material_id=material_id, review_status="pending",
                                content_b64=b64 if len(b64) < 4_000_000 else None,
                                content_type=_ctype(body.filename)))
    prev = m.screen_result
    m.evidence_provided = True
    sc = screening.screen_merged(m.name, m.e_number, m.source, m.cert_no, True,
                                 m.source_known if m.source_known is not None else True, m.note or "")
    m.screen_result, m.screen_status, m.screen_severity = sc["result"], sc["status"], sc["severity"]
    m.matched_uid = sc.get("matched_uid")
    sm.record_event(db, c, c.status, c.status, "material.evidence", user["role"], user["uid"],
                    {"material_id": material_id, "evidence_type": body.evidence_type,
                     "from": prev, "to": sc["result"]})
    db.commit()
    return {"material_id": material_id, "evidence_type": body.evidence_type,
            "screen_result": sc["result"], "was": prev,
            "recleared": prev == "NEEDS_EVIDENCE" and sc["result"] in ("CLEARED", "PASS")}


# ── S3-4 Excel 내보내기 ────────────────────────────────────────────────────────
@app.get("/cases/{case_id}/materials/export.xlsx")
def export_materials_xlsx(case_id: str, user=Depends(auth.get_current_user),
                          db: Session = Depends(get_db)):
    """원재료 목록 + 매트릭스 Excel(.xlsx) 내보내기."""
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment
    from fastapi.responses import StreamingResponse
    _get_case(db, case_id, user)
    mats = db.query(models.Material).filter_by(case_id=case_id).all()
    prods = db.query(models.Product).filter_by(case_id=case_id).all()
    links = db.query(models.ProductMaterial).filter_by(case_id=case_id).all()
    link_set = {(lk.product_id, lk.material_id) for lk in links}

    wb = Workbook()
    # Sheet1: 원재료 목록
    ws1 = wb.active
    ws1.title = "원재료 목록"
    hdr = ["성분명", "E-number", "유형", "원천", "공급사", "인증번호", "AI판정", "v1위험도", "증빙여부"]
    for ci, h in enumerate(hdr, 1):
        c = ws1.cell(1, ci, h)
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor="652672")
        c.alignment = Alignment(horizontal="center")
    for ri, m in enumerate(mats, 2):
        ws1.append([m.name, m.e_number or "", m.mat_type or "", m.source or "",
                    m.supplier or "", m.cert_no or "", m.screen_result or "",
                    m.v1_risk or "", "Y" if m.evidence_provided else "N"])
    ws1.column_dimensions["A"].width = 22

    # Sheet2: 원재료-제품 매트릭스
    ws2 = wb.create_sheet("매트릭스")
    ws2.cell(1, 1, "원재료\\제품").font = Font(bold=True)
    for ci, p in enumerate(prods, 2):
        c = ws2.cell(1, ci, p.name)
        c.font = Font(bold=True)
    for ri, m in enumerate(mats, 2):
        ws2.cell(ri, 1, m.name)
        for ci, p in enumerate(prods, 2):
            ws2.cell(ri, ci, "O" if (p.product_id, m.material_id) in link_set else "")

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return StreamingResponse(buf, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                             headers={"Content-Disposition": f"attachment; filename=materials_{case_id[:8]}.xlsx"})


@app.get("/cases/{case_id}/materials/{material_id}/evidence")
def list_material_evidence(case_id: str, material_id: str,
                           user=Depends(auth.get_current_user), db: Session = Depends(get_db)):
    _get_case(db, case_id, user)
    rows = db.query(models.DocumentAsset).filter_by(case_id=case_id, material_id=material_id).all()
    return [{"document_id": d.document_id, "filename": d.filename, "evidence_type": d.doc_type,
             "review_status": d.review_status, "has_file": bool(d.content_b64)} for d in rows]


@app.post("/cases/{case_id}/products/{product_id}/photo")
def add_product_photo(case_id: str, product_id: str, body: schemas.ProductPhotoReq,
                      user=Depends(auth.require_roles("applicant", "consultant")),
                      db: Session = Depends(get_db)):
    """제품 사진 업로드 (설계 G2)."""
    from .intake import _ctype
    _get_case(db, case_id, user)
    p = db.get(models.Product, product_id)
    if not p or p.case_id != case_id:
        raise HTTPException(404, {"code": "PRODUCT_NOT_FOUND"})
    b64 = _validate_upload(body.file_b64, body.filename)
    db.add(models.DocumentAsset(case_id=case_id, filename=body.filename, doc_type="product_photo",
                                product_id=product_id, review_status="pending",
                                content_b64=b64 if len(b64) < 4_000_000 else None,
                                content_type=_ctype(body.filename)))
    db.commit()
    return {"product_id": product_id, "ok": True}


@app.get("/cases/{case_id}/products/{product_id}/photos")
def list_product_photos(case_id: str, product_id: str,
                        user=Depends(auth.get_current_user), db: Session = Depends(get_db)):
    _get_case(db, case_id, user)
    rows = db.query(models.DocumentAsset).filter_by(case_id=case_id, product_id=product_id,
                                                    doc_type="product_photo").all()
    return [{"document_id": d.document_id, "filename": d.filename, "has_file": bool(d.content_b64)}
            for d in rows]


@app.patch("/documents/{document_id}/reclassify")
def reclassify_document(document_id: str, body: schemas.DocTypeReq,
                        user=Depends(auth.require_roles("consultant")), db: Session = Depends(get_db)):
    """문서 doc_type 수동 (재)분류 — AI 오분류 교정 (설계 P1-#6)."""
    d = db.get(models.DocumentAsset, document_id)
    if not d:
        raise HTTPException(404, {"code": "DOC_NOT_FOUND"})
    c = _get_case(db, d.case_id, user)
    prev = d.doc_type
    d.doc_type = body.doc_type
    sm.record_event(db, c, c.status, c.status, "documents.reclassify", user["role"], user["uid"],
                    {"document_id": document_id, "from": prev, "to": body.doc_type})
    db.commit()
    return {"document_id": document_id, "doc_type": d.doc_type}


@app.get("/cases/{case_id}/matrix")
def get_matrix(case_id: str, user=Depends(auth.get_current_user), db: Session = Depends(get_db)):
    _get_case(db, case_id, user)
    prods = db.query(models.Product).filter_by(case_id=case_id).all()
    mats = db.query(models.Material).filter_by(case_id=case_id).all()
    links = db.query(models.ProductMaterial).filter_by(case_id=case_id).all()
    return {"products": [{"product_id": p.product_id, "name": p.name} for p in prods],
            "materials": [{"material_id": m.material_id, "name": m.name, "result": m.screen_result} for m in mats],
            "links": [[lk.product_id, lk.material_id] for lk in links]}


@app.post("/cases/{case_id}/matrix/link")
def matrix_link(case_id: str, body: schemas.MatrixLinkReq,
                user=Depends(auth.require_roles("applicant", "consultant", "penyelia_halal")),
                db: Session = Depends(get_db)):
    _get_case(db, case_id, user)
    q = db.query(models.ProductMaterial).filter_by(case_id=case_id, product_id=body.product_id,
                                                   material_id=body.material_id)
    existing = q.first()
    if body.linked and not existing:
        db.add(models.ProductMaterial(case_id=case_id, product_id=body.product_id,
                                      material_id=body.material_id))
        try:
            db.commit()
        except IntegrityError:   # 동시 더블서밋 — 유니크 인덱스가 중복 차단, 이미 링크됨으로 취급
            db.rollback()
    elif not body.linked and existing:
        db.delete(existing)
        db.commit()
    else:
        db.commit()
    return {"product_id": body.product_id, "material_id": body.material_id, "linked": body.linked}


_CO_PLACEHOLDER = ("", "My Company", "scan", "ABC", "T", "UI", "Demo Co", "Demo Co FE")


def _apply_intake_autofill(db, c, res):
    """분류 결과 → DocumentAsset 저장 + 신청서 자동채움(회사/NIB/제품/원재료). 임시저장(commit은 호출측)."""
    for d in res["classified"]:
        db.add(models.DocumentAsset(case_id=c.case_id, filename=d["filename"], doc_type=d["doc_type"],
                                    confidence=float(d.get("confidence") or 0), fields=d.get("fields"),
                                    text_excerpt=d.get("excerpt"), content_b64=d.get("content_b64"),
                                    content_type=d.get("content_type")))
    agg = res.get("extracted", {})
    applied = {"company_set": False, "nib_set": False, "products": 0, "materials": 0}
    if agg.get("company_name") and (not c.company_name or c.company_name in _CO_PLACEHOLDER):
        c.company_name = agg["company_name"]
        applied["company_set"] = True
    if agg.get("nib") and not c.nib:
        c.nib = agg["nib"]
        applied["nib_set"] = True
    have_p = {p.name for p in db.query(models.Product).filter_by(case_id=c.case_id)}
    for pn in agg.get("products", []):
        if pn and pn not in have_p:
            db.add(models.Product(case_id=c.case_id, name=pn))
            applied["products"] += 1
            have_p.add(pn)
    have_m = {m.name for m in db.query(models.Material).filter_by(case_id=c.case_id)}
    for mn in agg.get("materials", []):
        if mn and mn not in have_m:
            sc = screening.screen_merged(mn, None, None, None, False, True, "")
            db.add(models.Material(case_id=c.case_id, name=mn, screen_result=sc["result"],
                                   screen_status=sc["status"], screen_severity=sc["severity"],
                                   matched_uid=sc.get("matched_uid"), v1_risk=sc.get("v1_risk"),
                                   cert=sc.get("v1_cert")))
            applied["materials"] += 1
            have_m.add(mn)
    res["applied"] = applied
    return applied


@app.post("/cases/{case_id}/intake-zip")
def intake_zip_ep(case_id: str, body: schemas.ZipIntakeReq,
                  user=Depends(auth.require_roles("applicant", "consultant")),
                  db: Session = Depends(get_db)):
    """압축파일 업로드 → 파일 전수 파싱 → AI 분류 → 필수서류 라우팅 (Document Intake AI)."""
    c = _get_case(db, case_id, user)
    raw = base64.b64decode(body.zip_b64.split(",")[-1])
    from .intake import intake_zip
    res = intake_zip(raw)
    applied = _apply_intake_autofill(db, c, res)
    sm.record_event(db, c, c.status, c.status, "documents.intake", "ai", user["uid"],
                    {"file_count": res["file_count"], "missing": res["missing"], "applied": applied})
    db.commit()
    return res


@app.post("/cases/{case_id}/intake-zip-stream")
def intake_zip_stream_ep(case_id: str, body: schemas.ZipIntakeReq,
                         user=Depends(auth.require_roles("applicant", "consultant")),
                         db: Session = Depends(get_db)):
    """스트리밍 인테이크 — 파일별 진행률(NDJSON) + 마지막에 자동채움/임시저장 결과."""
    import json as _json
    from fastapi.responses import StreamingResponse
    from .intake import intake_zip_iter
    c = _get_case(db, case_id, user)
    raw = base64.b64decode(body.zip_b64.split(",")[-1])

    def gen():
        res = None
        for kind, payload in intake_zip_iter(raw):
            if kind in ("start", "progress"):
                yield _json.dumps({"type": kind, **payload}, ensure_ascii=False) + "\n"
            elif kind == "result":
                res = payload
        applied = _apply_intake_autofill(db, c, res)
        sm.record_event(db, c, c.status, c.status, "documents.intake", "ai", user["uid"],
                        {"file_count": res["file_count"], "missing": res["missing"], "applied": applied})
        db.commit()
        yield _json.dumps({"type": "result", **res}, ensure_ascii=False) + "\n"

    return StreamingResponse(gen(), media_type="application/x-ndjson")


@app.patch("/cases/{case_id}/profile")
def update_profile(case_id: str, body: schemas.CaseProfileReq,
                   user=Depends(auth.require_roles("applicant", "consultant")),
                   db: Session = Depends(get_db)):
    c = _get_case(db, case_id, user)
    for f in ("company_name", "nib", "responsible_person", "halal_supervisor", "email",
              "phone", "address", "factory_reg_no", "factory_address", "due_date"):
        v = getattr(body, f)
        if v is not None:
            setattr(c, f, v)
    if body.notify_consent is not None:
        c.notify_consent = bool(body.notify_consent)
    if body.phone is not None:
        c.phone = _normalize_phone(body.phone)   # 국가코드 정규화
    db.commit()
    return _case_dict(c)


def _normalize_phone(p):
    """전화 정규화 — 인니(+62) 기본. 이미 +면 유지, 0 시작이면 +62로 치환."""
    if not p:
        return p
    s = "".join(ch for ch in str(p) if ch.isdigit() or ch == "+")
    if s.startswith("+"):
        return s
    if s.startswith("0"):
        return "+62" + s[1:]
    if s.startswith("62"):
        return "+" + s
    return "+" + s if s else s


@app.post("/cases/{case_id}/parse-file")
def parse_file_ep(case_id: str, body: schemas.ParseFileReq,
                  user=Depends(auth.require_roles("applicant", "consultant")),
                  db: Session = Depends(get_db)):
    """개별 파일 업로드 → 칸(doc_type)에 맞게 파싱 → 해당 필드 자동채움."""
    c = _get_case(db, case_id, user)
    raw = base64.b64decode(body.file_b64.split(",")[-1])
    from .intake import parse_typed
    r = parse_typed(body.doc_type, body.filename, raw)
    f = r.get("fields") or {}
    applied = {}
    if body.doc_type == "nib_business_license":
        for k, col in (("company_name", "company_name"), ("nib", "nib"), ("address", "address")):
            if f.get(k):
                setattr(c, col, f[k])
                applied[col] = f[k]
    elif body.doc_type == "factory_registration":
        for k in ("factory_reg_no", "factory_address"):
            if f.get(k):
                setattr(c, k, f[k])
                applied[k] = f[k]
    elif body.doc_type in ("material_list", "product_label"):
        names = f.get("material_names") or f.get("ingredients") or []
        have = {m.name for m in db.query(models.Material).filter_by(case_id=case_id)}
        for mn in names:
            if mn and mn not in have:
                sc = screening.screen_merged(mn, None, None, None, False, True, "")
                db.add(models.Material(case_id=case_id, name=mn, screen_result=sc["result"],
                                       screen_status=sc["status"], screen_severity=sc["severity"],
                                       matched_uid=sc.get("matched_uid"), v1_risk=sc.get("v1_risk")))
        applied["materials_added"] = len(names)
    elif body.doc_type == "product_list":
        names = f.get("product_names") or []
        have = {p.name for p in db.query(models.Product).filter_by(case_id=case_id)}
        for pn in names:
            if pn and pn not in have:
                db.add(models.Product(case_id=case_id, name=pn))
        applied["products_added"] = len(names)
    from .intake import _ctype
    _b64 = _validate_upload(body.file_b64, body.filename)
    db.add(models.DocumentAsset(case_id=case_id, filename=body.filename, doc_type=body.doc_type,
                                confidence=float(r.get("confidence") or 0), fields=f,
                                text_excerpt=r.get("excerpt"),
                                content_b64=_b64 if len(_b64) < 4_000_000 else None,
                                content_type=_ctype(body.filename)))
    sm.record_event(db, c, c.status, c.status, "documents.parse_file", "ai", user["uid"],
                    {"doc_type": body.doc_type, "applied": applied})
    db.commit()
    return {"doc_type": body.doc_type, "extracted": f, "applied": applied,
            "confidence": r.get("confidence", 0)}


@app.get("/cases/{case_id}/documents")
def list_documents(case_id: str, user=Depends(auth.get_current_user), db: Session = Depends(get_db)):
    _get_case(db, case_id, user)
    rows = db.query(models.DocumentAsset).filter_by(case_id=case_id).all()
    return [{"document_id": d.document_id, "filename": d.filename, "doc_type": d.doc_type,
             "confidence": d.confidence, "fields": d.fields, "excerpt": d.text_excerpt,
             "review_status": d.review_status, "has_file": bool(d.content_b64)} for d in rows]


@app.get("/documents/{document_id}/file")
def get_document_file(document_id: str, user=Depends(auth.get_current_user),
                      db: Session = Depends(get_db)):
    """업로드 원본 파일 조회 — 전체 역할(로그인) 허용, ABAC 조직격리."""
    import base64 as _b64lib
    from urllib.parse import quote
    from fastapi.responses import Response
    d = db.get(models.DocumentAsset, document_id)
    if not d or not d.content_b64:
        raise HTTPException(404, {"code": "FILE_NOT_AVAILABLE"})
    c = _get_case(db, d.case_id, user)   # 조직격리 검증
    # 문서 P0: 다운로드 감사로그 — 누가·언제·어떤 문서를 내려받았는지 추적
    sm.record_event(db, c, c.status, c.status, "document.download", user["role"], user["uid"],
                    {"document_id": document_id, "filename": d.filename})
    db.commit()
    raw = _b64lib.b64decode(d.content_b64)
    return Response(content=raw, media_type=d.content_type or "application/octet-stream",
                    headers={"Content-Disposition": "inline; filename*=UTF-8''" +
                             quote(d.filename or "document")})


@app.patch("/documents/{document_id}/review")
def review_document(document_id: str, body: schemas.DocReviewReq,
                    user=Depends(auth.require_roles("consultant")), db: Session = Depends(get_db)):
    d = db.get(models.DocumentAsset, document_id)
    if not d:
        raise HTTPException(404, {"code": "DOC_NOT_FOUND"})
    c = _get_case(db, d.case_id, user)
    d.review_status = body.review_status
    sm.record_event(db, c, c.status, c.status, "documents.review", user["role"], user["uid"],
                    {"document_id": document_id, "review_status": body.review_status})
    db.commit()
    return {"document_id": document_id, "review_status": d.review_status}


HPAS_ELEMENTS = ["commitment", "materials", "process", "product", "monitoring"]
HPAS_KO = {"commitment": "책임과 약속", "materials": "원재료", "process": "할랄제품공정",
           "product": "제품", "monitoring": "모니터링·평가"}


def _ensure_hpas(db, case_id):
    have = {h.element: h for h in db.query(models.HpasEvaluation).filter_by(case_id=case_id)}
    for el in HPAS_ELEMENTS:
        if el not in have:
            h = models.HpasEvaluation(case_id=case_id, element=el, status="not_started")
            db.add(h)
            have[el] = h
    db.commit()
    return have


@app.get("/cases/{case_id}/sjph")
def get_sjph(case_id: str, user=Depends(auth.get_current_user), db: Session = Depends(get_db)):
    c = _get_case(db, case_id, user)
    have = _ensure_hpas(db, case_id)
    penyelia_ok = db.query(models.PenyeliaHalal).filter_by(org_id=c.org_id, status="active").count() > 0
    els = [{"element": el, "element_ko": HPAS_KO[el], "status": have[el].status, "note": have[el].note}
           for el in HPAS_ELEMENTS]
    ok = sum(1 for e in els if e["status"] == "ok")
    return {"elements": els, "completion": round(ok / len(HPAS_ELEMENTS) * 100),
            "penyelia_ok": penyelia_ok, "complete": ok == len(HPAS_ELEMENTS) and penyelia_ok}


@app.patch("/cases/{case_id}/sjph")
def patch_sjph(case_id: str, body: schemas.SjphElementReq,
               user=Depends(auth.require_roles("penyelia_halal", "consultant", "applicant")),
               db: Session = Depends(get_db)):
    _get_case(db, case_id, user)
    have = _ensure_hpas(db, case_id)
    h = have.get(body.element)
    if not h:
        raise HTTPException(422, {"code": "BAD_ELEMENT"})
    h.status = body.status
    if body.note is not None:
        h.note = body.note
    db.commit()
    return {"element": body.element, "status": h.status}


@app.post("/cases/{case_id}/sjph/manual")
def sjph_manual(case_id: str, user=Depends(auth.get_current_user), db: Session = Depends(get_db)):
    """SJPH/HPAS 매뉴얼 결정적 생성 (v1 상속)."""
    c = _get_case(db, case_id, user)
    have = _ensure_hpas(db, case_id)
    pen = db.query(models.PenyeliaHalal).filter_by(org_id=c.org_id, status="active").first()
    mats = db.query(models.Material).filter_by(case_id=case_id).all()
    crit = [m.name for m in mats if m.screen_result in ("BLOCK", "NEEDS_EVIDENCE")]
    lines = ["[SJPH/HPAS 매뉴얼 — %s]" % (c.company_name or c.case_id[:8]),
             "할랄감독자(Penyelia Halal): %s" % (pen.name if pen else "(미지정 ⛔)"),
             "경로: %s" % c.pathway, ""]
    for el in HPAS_ELEMENTS:
        lines.append("· %s: %s%s" % (HPAS_KO[el], have[el].status,
                                     (" — " + have[el].note) if have[el].note else ""))
    lines += ["", "원재료 %d건, 증빙 필요/차단 %d건: %s" % (len(mats), len(crit), ", ".join(crit[:8]) or "없음"),
              "※ 본 매뉴얼은 준비용. 공식 SJPH는 BPJPH/SIHALAL 절차로 확정."]
    manual = "\n".join(lines)
    g = _save_gendoc(db, c, "sjph_manual", manual, user)  # 버전 저장(Rizky #3·#11)
    db.commit()
    return {"manual": manual, "version": g.version, "gen_doc_id": g.gen_doc_id}


def _next_version(db, case_id, doc_type):
    return db.query(models.GeneratedDocument).filter_by(case_id=case_id, doc_type=doc_type).count() + 1


def _save_gendoc(db, c, doc_type, content, user, status="draft"):
    g = models.GeneratedDocument(case_id=c.case_id, org_id=c.org_id, doc_type=doc_type,
                                 version=_next_version(db, c.case_id, doc_type), content=content,
                                 status=status, created_by=user["uid"])
    db.add(g)
    db.flush()
    return g


@app.post("/cases/{case_id}/audit-report")
def gen_audit_report(case_id: str,
                     user=Depends(auth.require_roles("auditor", "fatwa_liaison", "operator")),
                     db: Session = Depends(get_db)):
    """현장심사 보고서 자동 생성 (Rizky #4) — Findings·부적합·시정조치·권고. draft로 저장 후 오디터 승인."""
    c = _get_case(db, case_id, user)
    finds = db.query(models.AuditFinding).filter_by(case_id=case_id).all()
    major_open = [f for f in finds if f.severity == "major" and f.status == "open"]
    onsite = {r.item_key: r for r in db.query(models.OnsiteChecklist).filter_by(case_id=case_id).all()}
    nc = sum(1 for r in onsite.values() if r.result == "nonconformity")
    comply = sum(1 for r in onsite.values() if r.result == "comply")
    L = ["[현장심사 보고서 · Audit Report — %s]" % (c.company_name or c.case_id[:8]),
         "경로: %s · 상태: %s" % (c.pathway, c.status),
         "현장 체크리스트: 충족 %d · 부적합 %d" % (comply, nc), "",
         "■ 지적사항 · Findings (%d건)" % len(finds)]
    L += ["- [%s/%s] %s%s" % (f.severity, f.status, f.finding, (" (" + f.area + ")") if f.area else "")
          for f in finds] or ["- 없음"]
    L += ["", "■ 부적합 · Nonconformity — 미해결 중대 %d건" % len(major_open)]
    L += ["- %s" % f.finding for f in major_open] or ["- 없음"]
    L += ["", "■ 시정조치 · Corrective actions"]
    ca = ["- %s → %s" % (f.finding, f.corrective_action) for f in finds if f.corrective_action]
    L += ca or ["- 없음"]
    L += ["", "■ 권고사항 · Recommendations",
          ("- 미해결 중대 부적합 종결 후 최종 패키지 상정" if major_open else "- 모든 지적 종결 — 파트와 상정 가능"),
          "", "※ 오디터 검토·승인 필요."]
    content = "\n".join(L)
    g = _save_gendoc(db, c, "audit_report", content, user)
    db.commit()
    return {"report": content, "version": g.version, "gen_doc_id": g.gen_doc_id, "status": g.status}


@app.get("/cases/{case_id}/gen-docs")
def list_gendocs(case_id: str, doc_type: str = None,
                 user=Depends(auth.get_current_user), db: Session = Depends(get_db)):
    _get_case(db, case_id, user)
    q = db.query(models.GeneratedDocument).filter_by(case_id=case_id)
    if doc_type:
        q = q.filter_by(doc_type=doc_type)
    rows = q.order_by(models.GeneratedDocument.doc_type,
                      models.GeneratedDocument.version.desc()).all()
    return [{"gen_doc_id": g.gen_doc_id, "doc_type": g.doc_type, "version": g.version,
             "status": g.status, "created_at": str(g.created_at)} for g in rows]


@app.get("/gen-docs/{gen_doc_id}")
def get_gendoc(gen_doc_id: str, user=Depends(auth.get_current_user), db: Session = Depends(get_db)):
    g = db.get(models.GeneratedDocument, gen_doc_id)
    if not g:
        raise HTTPException(404, {"code": "GENDOC_NOT_FOUND"})
    _get_case(db, g.case_id, user)  # 조직격리
    return {"gen_doc_id": g.gen_doc_id, "doc_type": g.doc_type, "version": g.version,
            "status": g.status, "content": g.content, "created_at": str(g.created_at)}


@app.post("/gen-docs/{gen_doc_id}/approve")
def approve_gendoc(gen_doc_id: str, user=Depends(auth.require_roles("auditor", "operator")),
                   db: Session = Depends(get_db)):
    g = db.get(models.GeneratedDocument, gen_doc_id)
    if not g:
        raise HTTPException(404, {"code": "GENDOC_NOT_FOUND"})
    _get_case(db, g.case_id, user)
    g.status = "approved"
    db.commit()
    return {"ok": True, "status": "approved", "version": g.version}


@app.get("/cases/{case_id}/findings")
def list_findings(case_id: str, user=Depends(auth.get_current_user), db: Session = Depends(get_db)):
    _get_case(db, case_id, user)
    rows = db.query(models.AuditFinding).filter_by(case_id=case_id).all()
    return [{"finding_id": f.finding_id, "area": f.area, "finding": f.finding, "severity": f.severity,
             "corrective_action": f.corrective_action, "due_date": f.due_date, "status": f.status,
             "auditor": f.auditor} for f in rows]


@app.post("/cases/{case_id}/findings")
def add_finding(case_id: str, body: schemas.FindingReq,
                user=Depends(auth.require_roles("auditor", "consultant")),
                db: Session = Depends(get_db)):
    c = _get_case(db, case_id, user)
    sev = body.severity or "minor"
    if sev not in ("major", "minor", "observation"):  # P1: enum 검증(NC 게이트 무력화 방지)
        raise HTTPException(422, {"code": "BAD_SEVERITY", "allowed": ["major", "minor", "observation"]})
    f = models.AuditFinding(case_id=case_id, area=body.area, finding=body.finding,
                            severity=sev, corrective_action=body.corrective_action,
                            due_date=body.due_date, auditor=user["username"])
    db.add(f)
    sm.record_event(db, c, c.status, c.status, "audit.finding.add", user["role"], user["uid"],
                    {"severity": body.severity})
    db.commit()
    return {"finding_id": f.finding_id}


@app.patch("/findings/{finding_id}")
def update_finding(finding_id: str, body: schemas.FindingStatusReq,
                   user=Depends(auth.require_roles("auditor", "consultant")),
                   db: Session = Depends(get_db)):
    f = db.get(models.AuditFinding, finding_id)
    if not f:
        raise HTTPException(404, {"code": "FINDING_NOT_FOUND"})
    c = _get_case(db, f.case_id, user)
    f.status = body.status
    sm.record_event(db, c, c.status, c.status, "audit.finding.close", user["role"], user["uid"],
                    {"finding_id": finding_id, "status": body.status})
    db.commit()
    return {"finding_id": finding_id, "status": f.status}


@app.delete("/findings/{finding_id}")
def delete_finding(finding_id: str, user=Depends(auth.require_roles("auditor", "consultant")),
                   db: Session = Depends(get_db)):
    f = db.get(models.AuditFinding, finding_id)
    if f:
        _get_case(db, f.case_id, user)
        db.delete(f)
        db.commit()
    return {"deleted": finding_id}


# ── S3-1 현장 체크리스트 16항목 ──────────────────────────────────────────────
ONSITE_ITEMS = [
    ("halal_policy", "할랄 정책 문서"),
    ("organizational_structure", "조직 구조"),
    ("training_records", "교육 기록"),
    ("raw_material_control", "원재료 관리"),
    ("supplier_evaluation", "공급업체 평가"),
    ("storage_facility", "보관 시설"),
    ("production_equipment", "생산 설비"),
    ("cleaning_sanitation", "세척·위생"),
    ("production_process", "생산 공정"),
    ("contamination_prevention", "교차오염 방지"),
    ("product_labeling", "제품 라벨링"),
    ("packaging_material", "포장재"),
    ("product_traceability", "제품 추적성"),
    ("internal_audit", "내부 심사"),
    ("corrective_action_system", "시정조치 체계"),
    ("continuous_improvement", "지속 개선"),
]


@app.get("/cases/{case_id}/onsite-checklist")
def get_onsite_checklist(case_id: str, user=Depends(auth.get_current_user),
                         db: Session = Depends(get_db)):
    _get_case(db, case_id, user)
    rows = {r.item_key: r for r in db.query(models.OnsiteChecklist).filter_by(case_id=case_id).all()}
    items = [{"item_key": k, "label": ko,
              "result": rows[k].result if k in rows else "not_checked",
              "note": rows[k].note if k in rows else None} for k, ko in ONSITE_ITEMS]
    comply = sum(1 for x in items if x["result"] == "comply")
    nc = sum(1 for x in items if x["result"] == "nonconformity")
    return {"items": items, "comply": comply, "nonconformity": nc,
            "total": len(ONSITE_ITEMS), "completion": round(comply / len(ONSITE_ITEMS) * 100)}


@app.post("/cases/{case_id}/onsite-checklist")
def update_onsite_checklist(case_id: str, body: schemas.OnsiteChecklistReq,
                            user=Depends(auth.require_roles("auditor", "consultant")),
                            db: Session = Depends(get_db)):
    _get_case(db, case_id, user)
    if body.item_key not in {k for k, _ in ONSITE_ITEMS}:
        raise HTTPException(400, {"code": "INVALID_ITEM_KEY"})
    if body.result not in ("not_checked", "comply", "nonconformity"):
        raise HTTPException(400, {"code": "INVALID_RESULT", "allowed": ["not_checked", "comply", "nonconformity"]})
    row = db.query(models.OnsiteChecklist).filter_by(case_id=case_id, item_key=body.item_key).first()
    if not row:
        row = models.OnsiteChecklist(case_id=case_id, item_key=body.item_key)
        db.add(row)
    row.result = body.result
    row.note = body.note
    row.updated_at = datetime.utcnow()
    try:
        db.commit()
    except IntegrityError:   # 동시 생성 경합 — 기존 행에 반영
        db.rollback()
        row = db.query(models.OnsiteChecklist).filter_by(case_id=case_id, item_key=body.item_key).first()
        if row:
            row.result = body.result
            row.note = body.note
            row.updated_at = datetime.utcnow()
            db.commit()
    return {"item_key": body.item_key, "result": body.result, "ok": True}


# ── S3-5 심사원 풀 배정 ───────────────────────────────────────────────────────
@app.get("/cases/{case_id}/auditor-pool")
def get_auditor_pool(case_id: str, user=Depends(auth.get_current_user),
                     db: Session = Depends(get_db)):
    _get_case(db, case_id, user)
    rows = db.query(models.AuditorPool).filter_by(case_id=case_id).order_by(
        models.AuditorPool.assigned_at).all()
    return [{"id": x.id, "name": x.name, "cert_no": x.cert_no,
             "role_in_team": x.role_in_team} for x in rows]


@app.post("/cases/{case_id}/auditor-pool")
def add_auditor_pool(case_id: str, body: schemas.AuditorPoolReq,
                     user=Depends(auth.require_roles("operator")),
                     db: Session = Depends(get_db)):
    c = _get_case(db, case_id, user)
    cur = db.query(models.AuditorPool).filter_by(case_id=case_id).count()
    if cur >= 3:
        raise HTTPException(409, {"code": "MAX_AUDITORS_REACHED", "max": 3})
    x = models.AuditorPool(case_id=case_id, name=body.name, cert_no=body.cert_no,
                            role_in_team=body.role_in_team or "anggota")
    db.add(x)
    sm.record_event(db, c, c.status, c.status, "audit.pool.add", user["role"], user["uid"],
                    {"name": body.name})
    db.commit()
    return {"id": x.id, "name": x.name, "role_in_team": x.role_in_team}


@app.delete("/cases/{case_id}/auditor-pool/{pool_id}")
def delete_auditor_pool(case_id: str, pool_id: str,
                        user=Depends(auth.require_roles("operator")),
                        db: Session = Depends(get_db)):
    _get_case(db, case_id, user)
    row = db.query(models.AuditorPool).filter_by(id=pool_id, case_id=case_id).first()
    if not row:
        raise HTTPException(404)
    db.delete(row)
    db.commit()
    return {"deleted": pool_id}


# ── S3-6 LPH 레퍼런스 (어드민 관리) ──────────────────────────────────────────
@app.get("/admin/lph-references")
def list_lph_references(user=Depends(auth.require_roles("admin", "consultant")),
                        db: Session = Depends(get_db)):
    rows = db.query(models.LphReference).filter_by(status="active").all()
    return [{"lph_id": x.lph_id, "name": x.name, "accreditation_no": x.accreditation_no,
             "region": x.region} for x in rows]


@app.post("/admin/lph-references")
def create_lph_reference(body: schemas.LphReferenceReq,
                         user=Depends(auth.require_roles("admin")),
                         db: Session = Depends(get_db)):
    x = models.LphReference(name=body.name, accreditation_no=body.accreditation_no,
                             region=body.region, status=body.status or "active")
    db.add(x)
    db.commit()
    return {"lph_id": x.lph_id, "name": x.name}


@app.get("/cases/{case_id}/lph-assignment")
def get_lph(case_id: str, user=Depends(auth.get_current_user), db: Session = Depends(get_db)):
    _get_case(db, case_id, user)
    rows = db.query(models.LphAssignment).filter_by(case_id=case_id).all()
    return [{"lph_assignment_id": x.lph_assignment_id, "lph_name": x.lph_name,
             "auditor_ref": x.auditor_ref, "source": x.source} for x in rows]


@app.post("/cases/{case_id}/lph-assignment")
def add_lph(case_id: str, body: schemas.LphAssignReq,
            user=Depends(rbac.require_action("lph.assign")), db: Session = Depends(get_db)):
    c = _get_case(db, case_id, user)
    x = models.LphAssignment(case_id=case_id, lph_name=body.lph_name, auditor_ref=body.auditor_ref,
                             source=body.source or "manual")
    db.add(x)
    sm.record_event(db, c, c.status, c.status, "audit.lph.assign", user["role"], user["uid"],
                    {"lph": body.lph_name})
    _notify(db, c, "audit_scheduled", "심사 일정 · LPH 배정",
            "%s — %s 배정, 현장심사가 예정되었습니다." % (c.company_name or "", body.lph_name),
            channels=["inapp", "sms"], role="applicant")
    db.commit()
    return {"lph_assignment_id": x.lph_assignment_id}


@app.post("/cases/{case_id}/certificate/issue")
def issue_certificate(case_id: str, body: schemas.IssueReq = schemas.IssueReq(),
                      user=Depends(rbac.require_action("certificate.issue")),
                      db: Session = Depends(get_db)):
    c = _get_case(db, case_id, user)
    if c.fatwa_status != "approved":
        raise HTTPException(409, {"code": "FATWA_NOT_APPROVED"})
    if not c.scope_frozen:
        raise HTTPException(409, {"code": "SCOPE_NOT_FROZEN"})
    # 문서 P0(§4.2): 발급 full guard — 결제 완료·미해결 Major 부적합 없음
    if db.query(models.Invoice).filter_by(case_id=case_id, status="unpaid").count() > 0:
        raise HTTPException(409, {"code": "PAYMENT_PENDING"})
    if sm.open_major_nc(db, case_id) > 0:
        raise HTTPException(409, {"code": "UNRESOLVED_MAJOR_NC"})
    # 문서 P0(§4.2): 필수문서 all-approved — 반려/재작업 상태 문서가 남아있으면 발급 불가
    bad_docs = (db.query(models.DocumentAsset)
                .filter(models.DocumentAsset.case_id == case_id,
                        models.DocumentAsset.review_status.in_(("rejected", "rework"))).count())
    if bad_docs > 0:
        raise HTTPException(409, {"code": "DOCUMENTS_NOT_APPROVED", "unresolved": bad_docs})
    ex = db.query(models.HalalCertificate).filter_by(case_id=case_id, status="active").first()
    if ex:
        return {"certificate_no": ex.certificate_no, "issue_date": ex.issue_date,
                "expiry_date": ex.expiry_date, "scope": ex.scope, "existing": True}
    today = date.today()
    try:
        expiry = date(today.year + 4, today.month, today.day)
    except ValueError:
        expiry = date(today.year + 4, today.month, 28)
    prods = [p.name for p in db.query(models.Product).filter_by(case_id=case_id)]
    cert = models.HalalCertificate(case_id=case_id, scope=prods, issue_date=str(today),
                                   expiry_date=str(expiry))
    db.add(cert)
    db.flush()
    cert.certificate_no = "HC-" + cert.id[:8].upper()
    import secrets as _secrets
    cert.qr_token = _secrets.token_urlsafe(24)   # §6.4 공개 검증 토큰
    # 상태 전이 — certificate_issued는 보호상태(raw transition 금지). 이 전용 엔드포인트가 소유.
    frm = c.status
    if "certificate_issued" in sm.TRANSITIONS.get(c.status, set()):
        c.status = "certificate_issued"
    sm.record_event(db, c, frm, c.status, "certificate.issue", "system", user["uid"],
                    {"certificate_no": cert.certificate_no, "reason": (body.reason or "").strip() or None})
    # S3-3: freeze snapshot — 발급 시 제품/원재료 ID 동결
    prod_ids = [p.product_id for p in db.query(models.Product).filter_by(case_id=case_id)]
    mat_ids = [m.material_id for m in db.query(models.Material).filter_by(case_id=case_id)]
    cert.frozen_product_ids = prod_ids
    cert.frozen_material_ids = mat_ids
    _notify(db, c, "certificate_issued", "인증서 발급",
            "%s — 할랄 인증서 %s 발급 완료." % (c.company_name or "", cert.certificate_no),
            channels=["inapp", "sms", "kakao", "whatsapp"], role="applicant")
    try:
        db.commit()
    except IntegrityError:   # 동시 발급 경합 — 부분 유니크가 이중 활성 인증서 차단
        db.rollback()
        ex = db.query(models.HalalCertificate).filter_by(case_id=case_id, status="active").first()
        if ex:
            return {"certificate_no": ex.certificate_no, "issue_date": ex.issue_date,
                    "expiry_date": ex.expiry_date, "scope": ex.scope, "existing": True}
        raise
    return {"certificate_no": cert.certificate_no, "issue_date": str(today),
            "expiry_date": str(expiry), "scope": prods,
            "frozen_product_ids": prod_ids, "frozen_material_ids": mat_ids,
            "qr_token": cert.qr_token, "verify_url": "/verify/" + cert.qr_token}


@app.post("/cases/{case_id}/renew/request")
def renew_request(case_id: str, body: schemas.RenewRequestReq = schemas.RenewRequestReq(),
                  user=Depends(rbac.require_action("certificate.renew_request")),
                  db: Session = Depends(get_db)):
    """문서 P0: 갱신 신청(신청↔승인 분리) — 신청자/컨설턴트가 갱신 의사를 등록. 실제 파생은 operator 승인."""
    src = _get_case(db, case_id, user)
    if src.status != "certificate_issued":
        raise HTTPException(400, {"code": "NOT_ISSUED", "detail": "인증서 발급 케이스만 갱신 신청 가능합니다."})
    sm.record_event(db, src, src.status, src.status, "case.renew_requested", user["role"], user["uid"],
                    {"reason": (body.reason or "").strip() or None})
    db.commit()
    return {"case_id": case_id, "renewal_requested": True,
            "message": "갱신 신청이 접수되었습니다. 운영자 승인 후 갱신 케이스가 생성됩니다."}


@app.post("/cases/{case_id}/renew")
def renew_case(case_id: str, user=Depends(rbac.require_action("certificate.renew")),
               db: Session = Depends(get_db)):
    """S8-2: C5 갱신 케이스 파생(승인·실행) — 문서 P0: operator 전용(신청과 권한 분리)."""
    src = _get_case(db, case_id, user)
    if src.status != "certificate_issued":
        raise HTTPException(400, {"code": "NOT_ISSUED", "detail": "인증서 발급 케이스만 갱신 가능합니다."})
    import uuid
    new_id = uuid.uuid4().hex
    new_c = models.CaseApplication(
        case_id=new_id, org_id=src.org_id,
        company_name="Renewal: " + (src.company_name or ""),
        is_msme=src.is_msme,
        status="onboarding", pathway="undetermined", scope_frozen=False,
    )
    db.add(new_c)
    prods = db.query(models.Product).filter_by(case_id=case_id).all()
    for p in prods:
        db.add(models.Product(product_id=uuid.uuid4().hex, case_id=new_id, name=p.name, category=p.category))
    mats = db.query(models.Material).filter_by(case_id=case_id).all()
    for m in mats:
        nm = models.Material(material_id=uuid.uuid4().hex, case_id=new_id,
                             name=m.name, e_number=m.e_number, mat_type=m.mat_type,
                             source=m.source, supplier=m.supplier, cert_no=m.cert_no)
        db.add(nm)
        try:
            screening.apply_screen(nm)  # P1: 갱신 케이스 원료 재스크리닝(clean 오인 방지)
        except Exception:  # noqa: BLE001
            pass
    db.commit()
    sm.record_event(db, new_c, None, "onboarding", "case.renew", user["role"], user["uid"],
                    {"parent_case_id": case_id})
    return {"new_case_id": new_id, "parent_case_id": case_id,
            "products_copied": len(prods), "materials_copied": len(mats)}


@app.post("/cases/{case_id}/certificate/unlock")
def unlock_certificate(case_id: str, body: schemas.UnlockReq,
                       user=Depends(rbac.require_action("certificate.unlock")),
                       db: Session = Depends(get_db)):
    """S3-3: 재인증(renewal) 언락 — scope_frozen 해제. 문서 P0: operator 전용 + 사유 필수(consultant 제거)."""
    c = _get_case(db, case_id, user)
    if not c.scope_frozen:
        raise HTTPException(409, {"code": "NOT_FROZEN"})
    reason = (body.reason or "").strip()
    if len(reason) < 5:
        raise HTTPException(400, {"code": "REASON_REQUIRED"})
    c.scope_frozen = False
    sm.record_event(db, c, c.status, c.status, "certificate.unlock", user["role"], user["uid"],
                    {"reason": reason})
    db.commit()
    return {"scope_frozen": False, "reason": reason, "message": "재인증 모드 언락 완료"}


@app.get("/cases/{case_id}/certificate")
def get_certificate(case_id: str, user=Depends(auth.get_current_user), db: Session = Depends(get_db)):
    _get_case(db, case_id, user)
    cert = db.query(models.HalalCertificate).filter_by(case_id=case_id).first()
    if not cert:
        return {"issued": False}
    days = None
    try:
        days = (date.fromisoformat(cert.expiry_date) - date.today()).days
    except Exception:  # noqa: BLE001
        pass
    return {"issued": True, "certificate_no": cert.certificate_no, "scope": cert.scope,
            "issue_date": cert.issue_date, "expiry_date": cert.expiry_date, "status": cert.status,
            "days_to_expiry": days,
            "frozen_product_ids": cert.frozen_product_ids,
            "frozen_material_ids": cert.frozen_material_ids,
            "qr_token": cert.qr_token,
            "verify_url": ("/verify/" + cert.qr_token) if cert.qr_token else None}


@app.post("/cases/{case_id}/certificate/change-impact")
def change_impact(case_id: str, body: schemas.ChangeImpactReq,
                  user=Depends(auth.get_current_user), db: Session = Depends(get_db)):
    """변경 영향도 — 설계 8.3 가중식(간이)."""
    _get_case(db, case_id, user)
    mats = db.query(models.Material).filter_by(case_id=case_id).all()
    crit = [m.name for m in mats if m.screen_result in ("BLOCK", "NEEDS_EVIDENCE")]
    base = {"supplier_changed": 0.4, "material_added": 0.5, "process_changed": 0.3,
            "material_source_changed": 0.45}.get(body.change_type, 0.3)
    score = round(min(1.0, base + 0.1 * len(crit)), 2)
    level = "high" if score >= 0.6 else ("medium" if score >= 0.35 else "low")
    actions = {"supplier_changed": ["신규 공급사 할랄인증서 업로드", "공급사 신뢰도 재평가"],
               "material_added": ["신규 원재료 스크리닝", "증빙 확보"],
               "process_changed": ["공정 교차오염 재검토"],
               "material_source_changed": ["원산지 선언 업로드"]}.get(body.change_type, ["consultant 검토"])
    prods = [p.name for p in db.query(models.Product).filter_by(case_id=case_id)]
    reason_chain = ["임계원재료 %d건" % len(crit)] + ([", ".join(crit[:5])] if crit else [])
    ci = models.ChangeImpact(case_id=case_id, change_type=body.change_type, impact_score=score,
                             risk_level=level, affected_products=prods, required_actions=actions,
                             reason=" · ".join(reason_chain), actor=user["role"])
    db.add(ci)
    db.commit()
    return {"change_impact_id": ci.change_impact_id, "change_type": body.change_type,
            "impact_score": score, "risk_level": level, "affected_products": prods,
            "required_actions": actions, "reason_chain": reason_chain}


@app.get("/cases/{case_id}/certificate/change-impact-history")
def change_impact_history(case_id: str, user=Depends(auth.get_current_user),
                          db: Session = Depends(get_db)):
    """변경영향 분석 이력 — 사후관리(설계 8.3)."""
    _get_case(db, case_id, user)
    rows = (db.query(models.ChangeImpact).filter_by(case_id=case_id)
            .order_by(models.ChangeImpact.created_at.desc()).all())
    return [{"change_impact_id": r.change_impact_id, "change_type": r.change_type,
             "impact_score": r.impact_score, "risk_level": r.risk_level,
             "affected_products": r.affected_products, "required_actions": r.required_actions,
             "reason": r.reason, "actor": r.actor,
             "created_at": str(r.created_at)} for r in rows]


def _fatwa_privileged(user):
    """Fatwa 위원회 내부정보 열람 권한 — 문서 P0(§3.1): sharia/operator/admin만."""
    return user["role"] in ("fatwa_liaison", "operator", "admin")


@app.get("/cases/{case_id}/fatwa")
def get_fatwa(case_id: str, user=Depends(auth.get_current_user), db: Session = Depends(get_db)):
    c = _get_case(db, case_id, user)
    fd = db.query(models.FatwaDecision).filter_by(case_id=case_id).first()
    prods = db.query(models.Product).filter_by(case_id=case_id).all()
    # 결과(decision/status)는 케이스 참여자 모두 조회 가능. 위원회 내부정보는 권한자만.
    out = {"decision": fd.decision if fd else "pending", "decision_no": fd.decision_no if fd else None,
           "product_scope": fd.product_scope if fd else [],
           "products": [{"product_id": p.product_id, "name": p.name} for p in prods],
           "fatwa_status": c.fatwa_status, "scope_frozen": c.scope_frozen,
           "final_approved_at": str(fd.final_approved_at) if fd and fd.final_approved_at else None}
    if _fatwa_privileged(user):
        out.update({"committee_note": fd.committee_note if fd else None,
                    "committee_head": fd.committee_head if fd else None,
                    "committee_secretary": fd.committee_secretary if fd else None,
                    "committee_members": fd.committee_members if fd else [],
                    "final_approver": fd.final_approver if fd else None})
    else:
        out["committee_restricted"] = True   # 내부정보는 sharia/operator만 열람
    return out


@app.post("/cases/{case_id}/fatwa/final-approve")
def fatwa_final_approve(case_id: str, user=Depends(rbac.require_action("fatwa.approve_final")),
                        db: Session = Depends(get_db)):
    """2단계 승인 — 최고운영자(최종 결제자) 최종승인. 샤리아 가승인(provisional) 선행 필요."""
    c = _get_case(db, case_id, user)
    fd = db.query(models.FatwaDecision).filter_by(case_id=case_id).first()
    if not fd or fd.decision != "approved":
        raise HTTPException(409, {"code": "NO_PROVISIONAL_APPROVAL"})
    if c.fatwa_status == "approved":
        return {"ok": True, "already_final": True, "fatwa_status": "approved"}
    if c.fatwa_status != "provisional":
        raise HTTPException(409, {"code": "NOT_PROVISIONAL", "have": c.fatwa_status})
    fd.final_approved_at = datetime.utcnow()
    fd.final_approver = user["uid"]
    c.fatwa_status = "approved"
    c.scope_frozen = True
    if c.status == "fatwa_review" and sm.allowed(c.status, "fatwa_approved"):
        c.status = "fatwa_approved"
    sm.record_event(db, c, c.status, c.status, "fatwa.final_approve", user["role"], user["uid"], {})
    _notify(db, c, "fatwa_approved", "파트와 최종 승인",
            "%s — 파트와 위원회 최종 승인 완료. 인증서 발급 가능." % (c.company_name or ""),
            channels=["inapp"], role="applicant")
    db.commit()
    return {"ok": True, "fatwa_status": "approved", "final_approved_at": str(fd.final_approved_at)}


@app.patch("/cases/{case_id}/fatwa")
def patch_fatwa(case_id: str, body: schemas.FatwaReq,
                user=Depends(rbac.require_action("fatwa.propose")),  # SoD: 가승인=샤리아 전용(최종승인은 operator)
                db: Session = Depends(get_db)):
    c = _get_case(db, case_id, user)
    fd = db.query(models.FatwaDecision).filter_by(case_id=case_id).first()
    if not fd:
        fd = models.FatwaDecision(case_id=case_id)
        db.add(fd)
        db.flush()
    fd.decision = body.decision
    if body.committee_note is not None:
        fd.committee_note = body.committee_note
    # S3-2: 위원회 정보
    if body.committee_head is not None:
        fd.committee_head = body.committee_head
    if body.committee_secretary is not None:
        fd.committee_secretary = body.committee_secretary
    if body.committee_members is not None:
        fd.committee_members = body.committee_members
    if body.product_scope is not None:
        fd.product_scope = body.product_scope
    if body.decision == "approved":
        # 샤리아 가승인(provisional) — 최종승인은 최고운영자가 별도로 (2단계 승인)
        fd.decided_at = datetime.utcnow()
        if not fd.decision_no:
            fd.decision_no = "FD-" + fd.id[:8].upper()
        c.fatwa_status = "provisional"   # 가승인 — scope는 최종승인 시 동결
    elif body.decision in ("rejected", "conditional"):
        c.fatwa_status = body.decision
    sm.record_event(db, c, c.status, c.status, "fatwa.provisional", user["role"], user["uid"],
                    {"decision": body.decision})
    db.commit()
    return {"decision": fd.decision, "decision_no": fd.decision_no,
            "fatwa_status": c.fatwa_status, "scope_frozen": c.scope_frozen}


@app.post("/cases/{case_id}/fatwa/document")
def fatwa_document(case_id: str, user=Depends(rbac.require_action("fatwa.document.read")),
                   db: Session = Depends(get_db)):
    # 문서 P0(§3.1): 파트와 결정문(위원회 심의 산출물)은 sharia/operator/admin 전용
    c = _get_case(db, case_id, user)
    fd = db.query(models.FatwaDecision).filter_by(case_id=case_id).first()
    prods = db.query(models.Product).filter_by(case_id=case_id).all()
    lines = ["[파트와 결정문 — %s]" % (c.company_name or c.case_id[:8]),
             "결정번호: %s" % (fd.decision_no if fd and fd.decision_no else "(미발급)"),
             "결정: %s" % (fd.decision if fd else "pending"),
             "위원회 의견: %s" % (fd.committee_note if fd and fd.committee_note else "-"),
             "대상 제품: %s" % (", ".join(p.name for p in prods) or "-"),
             "scope 동결: %s" % ("예" if c.scope_frozen else "아니오"),
             "※ 준비용. 공식 ketetapan halal은 BPJPH/MUI 절차로 확정."]
    return {"document": "\n".join(lines)}


@app.get("/cases/{case_id}/invoices")
def list_invoices(case_id: str, user=Depends(auth.get_current_user), db: Session = Depends(get_db)):
    _get_case(db, case_id, user)
    rows = db.query(models.Invoice).filter_by(case_id=case_id).all()
    return [{"invoice_id": i.invoice_id, "invoice_no": i.invoice_no, "service_type": i.service_type,
             "amount": i.amount, "ppn": i.ppn, "total": i.total, "status": i.status} for i in rows]


@app.post("/cases/{case_id}/invoices")
def add_invoice(case_id: str, body: schemas.InvoiceReq,
                user=Depends(auth.require_roles("consultant")), db: Session = Depends(get_db)):
    c = _get_case(db, case_id, user)
    ppn = round(body.amount * 0.11, 2)
    total = round(body.amount + ppn, 2)
    inv = models.Invoice(case_id=case_id, service_type=body.service_type, amount=body.amount,
                         ppn=ppn, total=total)
    db.add(inv)
    db.flush()
    inv.invoice_no = "INV-" + inv.invoice_id[:8].upper()
    sm.record_event(db, c, c.status, c.status, "invoice.create", user["role"], user["uid"],
                    {"service_type": body.service_type, "total": total})
    db.commit()
    return {"invoice_id": inv.invoice_id, "invoice_no": inv.invoice_no, "ppn": ppn, "total": total}


@app.patch("/invoices/{invoice_id}/pay")
def pay_invoice(invoice_id: str, user=Depends(auth.require_roles("applicant", "consultant")),
                db: Session = Depends(get_db)):
    inv = db.get(models.Invoice, invoice_id)
    if not inv:
        raise HTTPException(404, {"code": "INVOICE_NOT_FOUND"})
    c = _get_case(db, inv.case_id, user)
    inv.status = "paid"
    sm.record_event(db, c, c.status, c.status, "invoice.pay", user["role"], user["uid"],
                    {"invoice_id": invoice_id})
    db.commit()
    return {"invoice_id": invoice_id, "status": "paid"}


@app.get("/cases/{case_id}/discussions")
def list_discussions(case_id: str, target: str = None,
                     user=Depends(auth.get_current_user), db: Session = Depends(get_db)):
    """S4-4: target 쿼리 파라미터로 섹션별 필터 지원."""
    _get_case(db, case_id, user)
    q = db.query(models.Discussion).filter_by(case_id=case_id)
    if target:
        q = q.filter(models.Discussion.target == target)
    rows = q.order_by(models.Discussion.created_at).all()
    return [{"id": d.id, "kind": d.kind, "target": d.target, "text": d.text,
             "author_role": d.author_role, "author": d.author,
             "created_at": str(d.created_at)} for d in rows]


@app.post("/cases/{case_id}/discussions")
def add_discussion(case_id: str, body: schemas.DiscussionReq,
                   user=Depends(auth.get_current_user), db: Session = Depends(get_db)):
    c = _get_case(db, case_id, user)
    d = models.Discussion(case_id=case_id, kind=body.kind or "comment", target=body.target,
                          text=body.text, author_role=user["role"], author=user["username"])
    db.add(d)
    sm.record_event(db, c, c.status, c.status, "discussion.add", user["role"], user["uid"],
                    {"kind": body.kind})
    db.commit()
    return {"id": d.id}


_STATE_KO = {
    "onboarding": "온보딩", "application_draft": "신청서 작성",
    "ai_pre_assessment_ready": "AI 사전평가 준비", "ai_pre_assessment_running": "AI 사전평가 진행",
    "pathway_determination": "경로 판정", "self_declare_eligible": "자기선언 적격",
    "sjph_lite_prepared": "간이 SJPH 준비", "pendamping_verification": "동반자 검증",
    "self_declaration_submitted": "자기선언 제출", "committee_verification": "위원회 확인",
    "supplementation_required": "보완 요청", "supplementation_submitted": "보완 제출",
    "consultant_review": "컨설턴트 검토", "document_pre_audit_requested": "문서 사전심사 요청",
    "document_pre_audit_in_review": "문서 사전심사 검토", "document_pre_audit_approved": "문서 사전심사 승인",
    "lph_assignment": "LPH 배정", "onsite_audit_scheduled": "현장심사 예정",
    "onsite_audit_in_progress": "현장심사 진행", "corrective_action_required": "시정조치 요청",
    "corrective_action_submitted": "시정조치 제출", "audit_closed": "심사 종결",
    "hpas_evaluation_ready": "HPAS 평가", "final_package_preparation": "최종 패키지 준비",
    "fatwa_review": "파트와 검토", "fatwa_approved": "파트와 승인", "certificate_issued": "인증서 발급",
    "post_certification_monitoring": "사후 모니터링", "change_impact": "변경 영향", "renewal_preparation": "갱신 준비",
}
_WF_COMMON = [("prep", "신청서 작성", ["onboarding", "application_draft"]),
              ("assess", "AI 사전평가", ["ai_pre_assessment_ready", "ai_pre_assessment_running"]),
              ("pathway", "경로 판정", ["pathway_determination"])]
_WF_SEHATI = [("sd_sjph", "SJPH·동반자 검증", ["self_declare_eligible", "sjph_lite_prepared", "pendamping_verification"]),
              ("sd_submit", "자기선언 제출", ["self_declaration_submitted"]),
              ("sd_committee", "위원회 확인", ["committee_verification"]),
              ("issue", "인증서 발급", ["certificate_issued"])]
_WF_REGULER = [("rg_suppl", "보완·컨설팅", ["supplementation_required", "supplementation_submitted", "consultant_review"]),
               ("rg_doc", "문서 사전심사", ["document_pre_audit_requested", "document_pre_audit_in_review", "document_pre_audit_approved"]),
               ("rg_audit", "LPH·현장심사", ["lph_assignment", "onsite_audit_scheduled", "onsite_audit_in_progress", "corrective_action_required", "corrective_action_submitted", "audit_closed"]),
               ("rg_hpas", "HPAS·최종패키지", ["hpas_evaluation_ready", "final_package_preparation"]),
               ("rg_fatwa", "파트와 결정", ["fatwa_review", "fatwa_approved"]),
               ("issue", "인증서 발급", ["certificate_issued"])]
_WF_POST = [("post", "사후 관리·갱신", ["post_certification_monitoring", "change_impact", "renewal_preparation"])]


def _wf_phases(pathway):
    mid = _WF_SEHATI if pathway == "self_declare" else (_WF_REGULER if pathway == "reguler"
          else [("branch", "경로 결정 대기", [])])
    return _WF_COMMON + mid + _WF_POST


def _calc_readiness(db, case_id):
    """준비도 계산(auth 없이 재사용) — 문서35·SJPH30·원재료20·심사15%."""
    from .intake import REQUIRED_DOCS
    docs = db.query(models.DocumentAsset).filter_by(case_id=case_id).all()
    have_types = {d.doc_type for d in docs if d.review_status != "rejected"}
    doc_score = (sum(1 for r in REQUIRED_DOCS if r in have_types) / len(REQUIRED_DOCS)) if REQUIRED_DOCS else 1.0
    have = _ensure_hpas(db, case_id)
    sjph_score = sum(1 for h in have.values() if h.status == "ok") / len(HPAS_ELEMENTS)
    mats = db.query(models.Material).filter_by(case_id=case_id).all()
    high = sum(1 for m in mats if m.screen_result == "BLOCK")
    med = sum(1 for m in mats if m.screen_result == "NEEDS_EVIDENCE")
    mat_score = max(0.0, 1 - high * 0.35 - med * 0.12)
    open_f = db.query(models.AuditFinding).filter_by(case_id=case_id, status="open").count()
    audit_score = max(0.0, 1 - open_f * 0.2)
    score = round((doc_score * 0.35 + sjph_score * 0.30 + mat_score * 0.20 + audit_score * 0.15) * 100)
    band = "ready" if score >= 80 else ("warn" if score >= 55 else "risk")
    return {"readiness": score, "band": band,
            "breakdown": {"documents": round(doc_score * 100), "sjph": round(sjph_score * 100),
                          "materials": round(mat_score * 100), "audit": round(audit_score * 100)},
            "counts": {"critical_high": high, "needs_evidence": med, "open_findings": open_f}}


@app.get("/cases/{case_id}/readiness")
def get_readiness(case_id: str, user=Depends(auth.get_current_user), db: Session = Depends(get_db)):
    """준비도 가중 점수 (v1 readiness 백엔드화)."""
    _get_case(db, case_id, user)
    return _calc_readiness(db, case_id)


@app.get("/dashboard/summary")
def dashboard_summary(user=Depends(auth.get_current_user), db: Session = Depends(get_db)):
    """대시보드 단일 집계 (P1: 프런트 N+1 제거) — KPI·상태분포·지역·작업큐·준비도·이벤트."""
    q = db.query(models.CaseApplication)
    if user["role"] != "admin":
        q = q.filter_by(org_id=user["org_id"])
    cases = q.order_by(models.CaseApplication.created_at.desc()).all()

    def _issued(s):
        return bool(s) and ("issued" in s or "certificate" in s)

    def _action(s):
        return bool(s) and any(k in s for k in ("corrective", "requested", "reject", "blocked"))
    issued = sum(1 for c in cases if _issued(c.status))
    action = sum(1 for c in cases if not _issued(c.status) and _action(c.status))
    by_status = {}
    reg = {}
    for c in cases:
        by_status[c.status] = by_status.get(c.status, 0) + 1
        p = _province_of(c.factory_address or c.address)
        if p:
            reg[p] = reg.get(p, 0) + 1
    regions = [{"province": k, "count": v} for k, v in sorted(reg.items(), key=lambda x: -x[1])]
    role = user["role"]
    worklist = []
    if role in ("auditor", "operator", "admin"):
        worklist += _stage_queue(db, user, AUDIT_STAGES)
    if role in ("fatwa_liaison", "operator", "admin"):
        worklist += _stage_queue(db, user, FATWA_STAGES)
    readiness = []
    for c in cases[:6]:
        rd = _calc_readiness(db, c.case_id)
        readiness.append({"case_id": c.case_id, "company": c.company_name,
                          "pathway": c.pathway, "readiness": rd["readiness"], "band": rd["band"]})
    # 조직 격리 — 자기 조직 케이스의 이벤트만(admin은 전체). 타조직 워크플로 누수 차단.
    eq = db.query(models.WorkflowEvent)
    if user["role"] != "admin":
        case_ids = [c.case_id for c in cases]
        eq = eq.filter(models.WorkflowEvent.case_id.in_(case_ids)) if case_ids else eq.filter(False)
    events = [{"case_id": e.case_id, "from": e.from_status, "to": e.to_status, "action": e.action,
               "actor": e.actor_type, "hash": (e.row_hash or "")[:10]}
              for e in eq.order_by(models.WorkflowEvent.created_at.desc()).limit(6).all()]
    return {"kpi": {"total": len(cases), "issued": issued, "action": action,
                    "in_progress": len(cases) - issued - action},
            "by_status": by_status, "regions": regions,
            "unassigned": len(cases) - sum(reg.values()),
            "worklist": worklist, "readiness": readiness, "events": events}


@app.get("/cases/{case_id}/workflow")
def get_workflow(case_id: str, user=Depends(auth.get_current_user), db: Session = Depends(get_db)):
    """인증 여정(워크플로우) — 단계 진행·현재상태·다음 전이(가드)·차단사유."""
    c = _get_case(db, case_id, user)
    cur = c.status
    phases = _wf_phases(c.pathway)
    cur_idx = next((i for i, (k, ko, ss) in enumerate(phases) if cur in ss), None)
    out = []
    for i, (k, ko, ss) in enumerate(phases):
        st = "pending" if cur_idx is None else ("done" if i < cur_idx else ("current" if i == cur_idx else "pending"))
        out.append({"key": k, "label": ko, "status": st,
                    "states": [{"code": s, "label": _STATE_KO.get(s, s)} for s in ss]})
    nexts = []
    for ns in sorted(sm.TRANSITIONS.get(cur, set())):
        ok, bl = sm.can_transition(db, c, ns)
        nexts.append({"to": ns, "to_label": _STATE_KO.get(ns, ns), "allowed": ok, "blockers": bl})
    return {"state": cur, "state_label": _STATE_KO.get(cur, cur), "pathway": c.pathway,
            "phases": out, "next_states": nexts, "blockers": sm.evaluate_blocking(db, c)}


_ENUMS = {
    "material_type": [("raw", "원료"), ("additive", "첨가물"), ("processing_aid", "가공보조제"),
                      ("packaging", "포장재"), ("lubricant", "윤활제"), ("sanitizer", "세정/살균제")],
    "material_source": [("animal", "동물"), ("plant", "식물"), ("microbial", "미생물"),
                        ("synthetic", "합성"), ("mineral", "광물"), ("unknown", "미상")],
    "material_cert": [("certified", "인증됨"), ("exempt", "면제"), ("unknown", "미상")],
    "review_status": [("pending", "대기"), ("approved", "승인"), ("rejected", "반려"), ("rework", "재작업")],
    "pathway": [("undetermined", "미정"), ("self_declare", "자기선언"), ("reguler", "정규")],
    "role": [("applicant", "신청기업"), ("consultant", "컨설턴트"), ("penyelia_halal", "할랄감독자"),
             ("pendamping_pph", "동반자"), ("auditor", "심사원"), ("fatwa_liaison", "파트와"),
             ("operator", "최고운영자"), ("admin", "관리자")],
    "finding_severity": [("major", "중대"), ("minor", "경미"), ("observation", "관찰")],
    "finding_status": [("open", "진행"), ("closed", "종결")],
    "sjph_element": [("commitment", "책임과 약속"), ("materials", "원재료"), ("process", "할랄제품공정"),
                     ("product", "제품"), ("monitoring", "모니터링·평가")],
    "sjph_status": [("not_started", "미시작"), ("gap", "미흡"), ("ok", "충족")],
    "invoice_service_type": [("pre_audit", "사전심사"), ("onsite", "현장심사")],
    "invoice_status": [("unpaid", "미결제"), ("paid", "결제")],
    "fatwa_decision": [("pending", "대기"), ("approved", "승인"), ("rejected", "반려"), ("conditional", "조건부")],
    "change_type": [("supplier_changed", "공급사 변경"), ("material_added", "원재료 추가"),
                    ("process_changed", "공정 변경"), ("material_source_changed", "원산지 변경")],
    "discussion_kind": [("comment", "댓글"), ("add", "추가요청"), ("repair", "수정요청"), ("resolved", "해결")],
    "pendamping_decision": [("verified", "검증"), ("rejected", "반려"), ("rework", "재작업")],
    "certificate_status": [("active", "유효"), ("suspended", "정지"), ("withdrawn", "철회")],
    "screen_result": [("PASS", "적합"), ("CLEARED", "증빙확인"), ("NEEDS_EVIDENCE", "증빙필요"),
                      ("BLOCK", "차단"), ("UNKNOWN", "미상")],
    "halal_status": [("halal", "할랄"), ("haram", "하람"), ("mushbooh", "의심")],
    "evidence_type": [("msds", "MSDS"), ("coa", "CoA/성분명세"), ("halal_certificate", "할랄인증서"),
                      ("supplier_declaration", "공급사 선언"), ("process_flow", "공정 흐름도"),
                      ("facility_photo", "시설 사진(pork-free)"), ("product_photo", "제품 사진"),
                      ("sjph_manual", "SJPH 매뉴얼"), ("audit_photo", "심사 사진"), ("committee_note", "위원회 의견")],
    "submission_type": [("new", "신규"), ("renewal", "갱신"), ("change", "변경·추가")],
    "onsite_result": [("not_checked", "미점검"), ("comply", "적합"), ("nonconformity", "부적합")],
    "auditor_role": [("ketua", "팀장 심사원"), ("anggota", "심사원")],
    "finding_area": [("commitment", "책임·약속"), ("raw_material", "원재료"), ("production_process", "생산공정"),
                     ("product", "제품"), ("monitoring", "모니터링"), ("facility", "시설"),
                     ("hygiene", "위생"), ("documentation", "문서화")],
}


@app.get("/meta/enums")
def get_enums(user=Depends(auth.get_current_user)):
    """전 enum 단일 소스 (프론트 select 데이터연결·드리프트 차단) — 설계 §11.2."""
    from .intake import DOC_TYPES, DOC_KO
    out = {k: [{"value": v, "label": lab} for v, lab in items] for k, items in _ENUMS.items()}
    out["doc_type"] = [{"value": d, "label": DOC_KO.get(d, d)} for d in DOC_TYPES]
    return out


@app.post("/ai/explain")
def explain_ingredient(body: schemas.ExplainReq, user=Depends(auth.get_current_user)):
    """성분 설명 — 온톨로지 근거(판정 이유·대체재·증빙) + gemma3 자연어 해설(옵션). 전 역할 허용."""
    exp = screening.explain(body.name, e_number=body.e_number, source=body.source, note=body.note or "")
    if body.llm:
        sysmsg = ("당신은 식품 성분 사전입니다. 주어진 성분이 무엇이고 식품에서 어떤 용도로 쓰이는지 "
                  "한국어 2~3문장으로 설명하세요. 할랄/하람 판정은 하지 말고 성분 자체 설명만 하세요.")
        exp["ai_description"] = ai_local.llm_text(sysmsg, body.name) or ""
    return exp


def _domain_facts(db, question):
    """도메인 시스템(IngredientOntology)에서 질문 관련 할랄 근거를 직접 회수.
    별도 학습 없이 이미 큐레이션된 성분 온톨로지(할랄/하람/의심 + 심각도 + 대체)를 끌어온다."""
    q = (question or "").lower()
    if not q.strip():
        return "", []
    matched = []
    for o in db.query(models.IngredientOntology).all():
        names = [o.canonical_name or ""]
        if isinstance(o.aliases, list):
            names += [str(a) for a in o.aliases]
        if o.e_number:
            names.append(str(o.e_number))
        if any(len(str(nm).lower().strip()) >= 3 and str(nm).lower().strip() in q for nm in names):
            matched.append(o)
    facts = []
    for o in matched[:8]:
        line = "- %s: %s (심각도 %s, %s)" % (o.canonical_name, o.default_status, o.severity, o.category)
        if isinstance(o.alternatives, list) and o.alternatives:
            line += " · 대체: " + ", ".join(str(a) for a in o.alternatives[:3])
        facts.append(line)
    rules = [r.code for r in db.query(models.RuleVersion).filter_by(status="active").all()]
    text = ""
    if facts:
        text = "[할랄 도메인 온톨로지 근거 · 규칙 %s]\n%s" % (", ".join(rules) or "-", "\n".join(facts))
    return text, [{"name": o.canonical_name, "status": o.default_status,
                   "severity": o.severity, "category": o.category} for o in matched[:8]]


@app.post("/cases/{case_id}/ask")
def ask_ai(case_id: str, body: schemas.AskReq,
           user=Depends(auth.get_current_user), db: Session = Depends(get_db)):
    """Consultant Copilot — 케이스 컨텍스트 + gemma3 (설계 C.1). 근거는 케이스 정보."""
    if not body.question or not body.question.strip():
        raise HTTPException(status_code=400, detail="question은 비어있을 수 없습니다")
    c = _get_case(db, case_id, user)
    blockers = sm.evaluate_blocking(db, c)
    mats = db.query(models.Material).filter_by(case_id=case_id).all()
    crit = [m.name for m in mats if m.screen_result in ("BLOCK", "NEEDS_EVIDENCE")]
    have = _ensure_hpas(db, case_id)
    sjph_ok = sum(1 for h in have.values() if h.status == "ok")
    open_find = db.query(models.AuditFinding).filter_by(case_id=case_id, status="open").count()
    cert = db.query(models.HalalCertificate).filter_by(case_id=case_id).first()
    ctx = "\n".join([
        "회사: %s" % (c.company_name or "-"),
        "경로: %s, 상태: %s, MSME: %s" % (c.pathway, c.status, c.is_msme),
        "차단사유: %s" % (", ".join(b["code"] + ((":" + b.get("target", "")) if b.get("target") else "")
                                  for b in blockers) or "없음"),
        "원재료 %d건, 증빙필요/차단 %d건: %s" % (len(mats), len(crit), ", ".join(crit[:10]) or "없음"),
        "SJPH 5요소 ok %d/5" % sjph_ok,
        "미해결 심사지적: %d건" % open_find,
        "fatwa: %s, scope동결: %s, 인증서: %s" % (c.fatwa_status, c.scope_frozen,
                                              cert.certificate_no if cert else "미발급"),
    ])
    sysmsg = ("당신은 BPJPH/SIHALAL 할랄인증 준비 어시스턴트입니다. 아래 케이스 정보만 근거로 한국어로 "
              "간결히 답하세요. 정보에 없으면 모른다고 하세요.\n\n[케이스 정보]\n" + ctx)
    # ① 도메인 온톨로지 근거(우선) — 도메인 시스템에 이미 있는 할랄 지식 직접 회수(학습 불필요)
    dom_text, domain_sources = _domain_facts(db, body.question)
    if dom_text:
        sysmsg += "\n\n" + dom_text
    # ② 홍익AI/CHU-1 장기기억(보조·일반) — 장애 시 로컬만(폴백, 차단 없음)
    long_term_sources = []
    for h in ai_local.search_context(body.question, top_k=3):
        txt = (h.get("text") or "").strip()
        if txt:
            long_term_sources.append({"text": txt[:200], "score": h.get("score")})
    if long_term_sources:
        sysmsg += ("\n\n[장기기억 참고 · CHU-1 (보조 근거, 케이스/도메인 정보와 상충 시 무시)]\n"
                   + "\n".join("- " + s["text"] for s in long_term_sources[:3]))
    ans = ai_local.llm_text(sysmsg, body.question)
    return {"answer": ans or "(LLM 응답 없음)", "context_facts": ctx,
            "domain_sources": domain_sources, "long_term_sources": long_term_sources}


@app.get("/ai/context/health")
def ai_context_health():
    """홍익AI/CHU-1 장기기억 연동 상태."""
    return ai_local.context_health()


SJPH_EVIDENCE_ITEMS = [
    ("halal_supervisor", "할랄감독자 지정서"), ("training", "할랄 교육 이수"),
    ("facility_layout", "시설 배치도"), ("production_flow", "생산 공정 흐름도"),
    ("purchase_log", "구매 기록"), ("receiving_log", "입고 기록"), ("usage_log", "사용 기록"),
    ("production_log", "생산 기록"), ("distribution_log", "출고 기록"), ("internal_audit", "내부 심사 기록"),
]


@app.get("/cases/{case_id}/sjph-evidence")
def list_sjph_evidence(case_id: str, user=Depends(auth.get_current_user), db: Session = Depends(get_db)):
    _get_case(db, case_id, user)
    have = {e.item_key: e for e in db.query(models.SjphEvidence).filter_by(case_id=case_id)}
    items = [{"item_key": k, "label": ko, "uploaded": k in have,
              "filename": have[k].filename if k in have else None,
              "document_id": have[k].document_id if k in have else None} for k, ko in SJPH_EVIDENCE_ITEMS]
    up = sum(1 for i in items if i["uploaded"])
    return {"items": items, "uploaded": up, "total": len(SJPH_EVIDENCE_ITEMS),
            "completion": round(up / len(SJPH_EVIDENCE_ITEMS) * 100)}


@app.post("/cases/{case_id}/sjph-evidence")
def add_sjph_evidence(case_id: str, body: schemas.SjphEvidenceReq,
                      user=Depends(auth.require_roles("applicant", "penyelia_halal", "consultant")),
                      db: Session = Depends(get_db)):
    from .intake import _ctype
    _get_case(db, case_id, user)
    if body.item_key not in {k for k, _ in SJPH_EVIDENCE_ITEMS}:
        raise HTTPException(422, {"code": "BAD_ITEM_KEY"})
    b64 = _validate_upload(body.file_b64, body.filename)
    doc = models.DocumentAsset(case_id=case_id, filename=body.filename, doc_type="sjph_evidence",
                               review_status="pending", content_b64=b64 if len(b64) < 4_000_000 else None,
                               content_type=_ctype(body.filename))
    db.add(doc)
    db.flush()
    ex = db.query(models.SjphEvidence).filter_by(case_id=case_id, item_key=body.item_key).first()
    if ex:
        ex.filename, ex.document_id = body.filename, doc.document_id
    else:
        db.add(models.SjphEvidence(case_id=case_id, item_key=body.item_key,
                                   filename=body.filename, document_id=doc.document_id))
    db.commit()
    return {"item_key": body.item_key, "ok": True}


@app.get("/cases/{case_id}/hpas-auto")
def hpas_auto(case_id: str, user=Depends(auth.get_current_user), db: Session = Depends(get_db)):
    """HPAS 5요소 자동 증빙 유도 (업로드/원재료/제품/매트릭스/SJPH증빙에서) — 설계 G3."""
    c = _get_case(db, case_id, user)
    pen = db.query(models.PenyeliaHalal).filter_by(org_id=c.org_id, status="active").count() > 0
    mats = db.query(models.Material).filter_by(case_id=case_id).all()
    bad = [m for m in mats if m.screen_result in ("BLOCK", "NEEDS_EVIDENCE")]
    prods = db.query(models.Product).filter_by(case_id=case_id).count()
    photos = db.query(models.DocumentAsset).filter_by(case_id=case_id, doc_type="product_photo").count()
    sev = {e.item_key for e in db.query(models.SjphEvidence).filter_by(case_id=case_id)}

    def st(cond):
        return "ok" if cond else "gap"
    elements = [
        {"element": "commitment", "label": "책임과 약속", "status": st(pen and "halal_supervisor" in sev),
         "reason": "할랄감독자 지정+교육 증빙"},
        {"element": "materials", "label": "원재료", "status": st(bool(mats) and not bad),
         "reason": "임계원재료 %d건" % len(bad)},
        {"element": "process", "label": "할랄제품공정", "status": st("production_flow" in sev),
         "reason": "공정 흐름도 증빙"},
        {"element": "product", "label": "제품", "status": st(prods > 0 and photos > 0),
         "reason": "제품 %d·사진 %d" % (prods, photos)},
        {"element": "monitoring", "label": "모니터링·평가", "status": st("internal_audit" in sev),
         "reason": "내부 심사 기록"},
    ]
    ok = sum(1 for e in elements if e["status"] == "ok")
    return {"elements": elements, "auto_completion": round(ok / 5 * 100)}


@app.get("/cases/{case_id}/doc-checklist")
def doc_checklist(case_id: str, user=Depends(auth.get_current_user), db: Session = Depends(get_db)):
    _get_case(db, case_id, user)
    from .intake import REQUIRED_DOCS, DOC_KO
    docs = db.query(models.DocumentAsset).filter_by(case_id=case_id).all()
    by_type = {}
    for d in docs:
        by_type.setdefault(d.doc_type, []).append(
            {"document_id": d.document_id, "filename": d.filename, "review_status": d.review_status})
    checklist = [{"doc_type": dt, "doc_type_ko": DOC_KO[dt],
                  "satisfied": dt in by_type and any(x["review_status"] != "rejected" for x in by_type[dt]),
                  "files": by_type.get(dt, [])} for dt in REQUIRED_DOCS]
    missing = [c["doc_type_ko"] for c in checklist if not c["satisfied"]]
    return {"checklist": checklist, "missing": missing, "complete": len(missing) == 0}


@app.post("/materials/{material_id}/screen")
def screen_material(material_id: str, user=Depends(auth.get_current_user), db: Session = Depends(get_db)):
    m = db.get(models.Material, material_id)
    if not m:
        raise HTTPException(404, {"code": "MATERIAL_NOT_FOUND"})
    _get_case(db, m.case_id, user)  # P0-2 조직격리
    r = screening.apply_screen(m)
    db.commit()
    return r


# ---------- AI: OCR / label judgment ----------
@app.post("/ai/ocr")
def ai_ocr(body: schemas.OCRReq, user=Depends(auth.get_current_user)):
    # P0: 경로 traversal 방지 — 업로드 샌드박스 접두만 허용
    return ai_local.ocr_image(_sandboxed_path(body.image_path), body.lang or "korean")


@app.post("/ai/label-judgment")
def ai_label_judgment(body: schemas.LabelJudgmentReq, user=Depends(auth.get_current_user)):
    from .ocr_pipeline import judge_label
    return judge_label(_sandboxed_path(body.image_path), body.locale or "ko-KR")


@app.get("/cases/{case_id}/ai-extractions")
def list_ai_extractions(case_id: str, user=Depends(auth.get_current_user), db: Session = Depends(get_db)):
    """§7.4: AI 결과 근거 조회 — 화면에서 원문 근거·신뢰도·리뷰이력 확인."""
    _get_case(db, case_id, user)
    rows = (db.query(models.AiExtraction).filter_by(case_id=case_id)
            .order_by(models.AiExtraction.created_at.desc()).limit(100).all())
    return [{"id": r.id, "source": r.source, "model_name": r.model_name,
             "model_version": r.model_version, "confidence": r.confidence,
             "extracted": r.extracted_json, "evidence": r.evidence,
             "reviewer_status": r.reviewer_status, "reviewer_id": r.reviewer_id,
             "reviewer_note": r.reviewer_note, "created_at": str(r.created_at)} for r in rows]


@app.patch("/ai-extractions/{ext_id}/review")
def review_ai_extraction(ext_id: str, body: schemas.AiReviewReq,
                         user=Depends(auth.require_roles("consultant", "penyelia_halal", "auditor", "fatwa_liaison")),
                         db: Session = Depends(get_db)):
    """§7.2: reviewer override 이력 — AI 결과를 실무자가 수용/재정의."""
    r = db.get(models.AiExtraction, ext_id)
    if not r:
        raise HTTPException(404, {"code": "EXTRACTION_NOT_FOUND"})
    _get_case(db, r.case_id, user)
    if body.reviewer_status not in ("accepted", "overridden"):
        raise HTTPException(400, {"code": "BAD_STATUS", "allowed": ["accepted", "overridden"]})
    r.reviewer_status = body.reviewer_status
    r.reviewer_id = user["uid"]
    r.reviewer_note = body.note
    db.commit()
    return {"id": r.id, "reviewer_status": r.reviewer_status}


@app.post("/cases/{case_id}/materials/from-label")
def materials_from_label(case_id: str, body: schemas.LabelJudgmentReq,
                         user=Depends(auth.require_roles("applicant", "consultant")),
                         db: Session = Depends(get_db)):
    c = _get_case(db, case_id, user)
    from .ocr_pipeline import judge_label
    res = judge_label(_sandboxed_path(body.image_path), body.locale or "ko-KR")
    if not res.get("ok"):
        raise HTTPException(422, res)
    return _register_from_judgment(db, c, res)


@app.post("/cases/{case_id}/materials/from-label-b64")
def materials_from_label_b64(case_id: str, body: schemas.LabelB64Req,
                             user=Depends(auth.require_roles("applicant", "consultant")),
                             db: Session = Depends(get_db)):
    c = _get_case(db, case_id, user)
    import tempfile
    raw = base64.b64decode(body.image_b64.split(",")[-1])
    path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            f.write(raw)
            path = f.name
        from .ocr_pipeline import judge_label
        res = judge_label(path, body.locale or "ko-KR")
    finally:
        if path:
            try:
                os.unlink(path)
            except Exception:  # noqa: BLE001
                pass
    if not res.get("ok"):
        raise HTTPException(422, res)
    return _register_from_judgment(db, c, res)


# ---------- pathway ----------
@app.post("/cases/{case_id}/pathway/assess")
def pathway_assess(case_id: str, user=Depends(auth.get_current_user), db: Session = Depends(get_db)):
    return sm.assess_pathway(db, _get_case(db, case_id, user))


@app.post("/cases/{case_id}/pathway/confirm")
def pathway_confirm(case_id: str, body: schemas.PathwayConfirm,
                    user=Depends(auth.require_roles("consultant")), db: Session = Depends(get_db)):
    c = _get_case(db, case_id, user)
    if c.status != "pathway_determination":
        raise HTTPException(409, {"code": "WRONG_STATE", "need": "pathway_determination", "have": c.status})
    a = sm.assess_pathway(db, c)
    c.risk_category = a["risk_category"]
    if body.pathway == "self_declare":
        target = "self_declare_eligible"
    else:
        target = ("supplementation_required"
                  if a["critical_ingredient_count"] > 0 or not a["evidence_complete"]
                  else "consultant_review")
    ok, blockers = sm.can_transition(db, c, target)
    if not ok:
        raise HTTPException(409, {"code": "TRANSITION_BLOCKED", "blockers": blockers})
    frm = c.status
    sm.apply_side_effects(c, target)
    c.status = target
    sm.record_event(db, c, frm, target, "pathway.confirm", user["role"], user["uid"],
                    {"pathway": body.pathway, "override_reason": body.override_reason})
    db.commit()
    return {"pathway": c.pathway, "next_state": target, "assessment": a}


# ---------- transition ----------
@app.post("/cases/{case_id}/transition")
def transition(case_id: str, body: schemas.TransitionReq,
               user=Depends(auth.get_current_user), db: Session = Depends(get_db)):
    c = _get_case(db, case_id, user)
    # P0-1: 승인·발급 상태는 raw 전이 금지(전용 엔드포인트만), 그 외는 역할 게이트
    if body.to_state in sm.PROTECTED_STATES:
        raise HTTPException(403, {"code": "USE_DEDICATED_ENDPOINT", "state": body.to_state})
    if user["role"] != "admin" and user["role"] not in sm.transition_roles(body.to_state):
        raise HTTPException(403, {"code": "NOT_AUTHORIZED_TRANSITION",
                                  "to": body.to_state, "need": sorted(sm.transition_roles(body.to_state))})
    ok, blockers = sm.can_transition(db, c, body.to_state)
    if not ok:
        raise HTTPException(409, {"code": "TRANSITION_BLOCKED", "blockers": blockers})
    frm = c.status
    sm.apply_side_effects(c, body.to_state)
    c.status = body.to_state
    sm.record_event(db, c, frm, body.to_state, body.action or "transition", user["role"], user["uid"])
    _NOTIFY_ON = {"audit_closed": ("audit_closed", "심사 완료", "현장심사가 종결되었습니다."),
                  "document_pre_audit_requested": ("document_requested", "문서 요청", "사전심사 문서 제출이 요청되었습니다."),
                  "onsite_audit_scheduled": ("audit_scheduled", "심사 일정", "현장심사가 예정되었습니다.")}
    if body.to_state in _NOTIFY_ON:
        ev, ti, bo = _NOTIFY_ON[body.to_state]
        _notify(db, c, ev, ti, "%s — %s" % (c.company_name or "", bo),
                channels=["inapp", "sms"], role="applicant")
    db.commit()
    return {"from": frm, "to": body.to_state, "case": _case_dict(c)}


# ---------- penyelia ----------
@app.post("/orgs/{org_id}/penyelia")
def add_penyelia(org_id: str, body: schemas.PenyeliaCreate,
                 user=Depends(auth.require_roles("applicant", "penyelia_halal", "consultant")),
                 db: Session = Depends(get_db)):
    if user["role"] != "admin" and org_id != user["org_id"]:
        raise HTTPException(403, {"code": "ORG_FORBIDDEN"})
    p = models.PenyeliaHalal(org_id=org_id, name=body.name, training_cert=body.training_cert,
                             cert_expiry=date.fromisoformat(body.cert_expiry) if body.cert_expiry else None)
    db.add(p)
    db.commit()
    active = db.query(models.PenyeliaHalal).filter_by(org_id=org_id, status="active").count()
    return {"penyelia_id": p.penyelia_id, "active_count": active}


@app.get("/orgs/{org_id}/penyelia")
def list_penyelia(org_id: str, user=Depends(auth.get_current_user), db: Session = Depends(get_db)):
    if user["role"] != "admin" and org_id != user["org_id"]:  # P0-2 조직격리
        raise HTTPException(403, {"code": "FORBIDDEN_ORG"})
    rows = db.query(models.PenyeliaHalal).filter_by(org_id=org_id).all()
    return {"active_count": sum(1 for r in rows if r.status == "active"),
            "items": [{"penyelia_id": r.penyelia_id, "name": r.name, "status": r.status} for r in rows]}


@app.patch("/orgs/{org_id}/penyelia/{penyelia_id}")
def update_penyelia(org_id: str, penyelia_id: str, body: schemas.PenyeliaUpdate,
                    user=Depends(auth.require_roles("applicant", "penyelia_halal", "consultant")),
                    db: Session = Depends(get_db)):
    if user["role"] != "admin" and org_id != user["org_id"]:
        raise HTTPException(403, {"code": "ORG_FORBIDDEN"})
    p = db.get(models.PenyeliaHalal, penyelia_id)
    if not p or p.org_id != org_id:
        raise HTTPException(404, {"code": "PENYELIA_NOT_FOUND"})
    if body.status not in ("active", "inactive"):
        raise HTTPException(400, {"code": "INVALID_STATUS"})
    p.status = body.status
    db.commit()
    active = db.query(models.PenyeliaHalal).filter_by(org_id=org_id, status="active").count()
    return {"penyelia_id": p.penyelia_id, "status": p.status, "active_count": active}


# ---------- pendamping (자기선언) ----------
@app.post("/cases/{case_id}/pendamping/assign")
def assign_pendamping(case_id: str, body: schemas.PendampingAssign,
                      user=Depends(auth.require_roles("consultant")), db: Session = Depends(get_db)):
    _get_case(db, case_id, user)
    pa = models.PendampingAssignment(case_id=case_id, pendamping_id=body.pendamping_id)
    db.add(pa)
    db.commit()
    return {"assignment_id": pa.assignment_id}


@app.post("/cases/{case_id}/pendamping/verify")
def verify_pendamping(case_id: str, body: schemas.PendampingVerify,
                      user=Depends(auth.require_roles("pendamping_pph")), db: Session = Depends(get_db)):
    c = _get_case(db, case_id, user)
    pa = (db.query(models.PendampingAssignment).filter_by(case_id=case_id)
          .order_by(models.PendampingAssignment.assignment_id.desc()).first())
    if not pa:
        raise HTTPException(404, {"code": "NO_PENDAMPING_ASSIGNMENT"})
    pa.decision = body.decision
    pa.note = body.note
    pa.signature_ref = body.signature_ref
    pa.verified_at = datetime.utcnow()
    switched = False
    if body.decision == "rejected":
        c.pathway = "reguler"
        c.sehati_eligible = "revoked"
        switched = True
        sm.record_event(db, c, c.status, c.status, "pathway.switch", "pendamping", user["uid"],
                        {"reason": "pendamping_rejected"})
    db.commit()
    return {"decision": body.decision, "switched_to_reguler": switched}


# ---------- SIHALAL 식별자 ----------
@app.post("/cases/{case_id}/sihalal/identity/link")
def sihalal_link(case_id: str, body: schemas.SihalalLink,
                 user=Depends(auth.require_roles("applicant", "consultant")),
                 db: Session = Depends(get_db)):
    c = _get_case(db, case_id, user)
    ei = models.ExternalIdentity(case_id=case_id, org_id=c.org_id,
                                 external_username=body.external_username,
                                 external_email=body.external_email,
                                 external_application_no=body.external_application_no)
    db.add(ei)
    db.commit()
    return {"external_identity_id": ei.external_identity_id,
            "verification_status": ei.verification_status}


@app.post("/sihalal/identity/{eid}/verify")
def sihalal_verify(eid: str, body: schemas.SihalalVerify,
                   user=Depends(auth.require_roles("consultant")), db: Session = Depends(get_db)):
    ei = db.get(models.ExternalIdentity, eid)
    if not ei:
        raise HTTPException(404, {"code": "IDENTITY_NOT_FOUND"})
    if user["role"] != "admin" and ei.org_id != user["org_id"]:  # P0-2 조직격리
        raise HTTPException(403, {"code": "FORBIDDEN_ORG"})
    stored = (ei.external_email or ei.external_username or "").strip().lower()
    match = bool(stored) and body.expected_identifier.strip().lower() == stored
    ei.identifier_match = match
    ei.verification_status = "verified" if match else "mismatch_found"
    db.commit()
    return {"verification_status": ei.verification_status, "identifier_match": match}


# ── S4-1: E-number 자동완성 (ontology 검색) ──────────────────────────────────
@app.get("/meta/ingredient-suggest")
def ingredient_suggest(q: str = "", user=Depends(auth.get_current_user),
                       db: Session = Depends(get_db)):
    """E-number 또는 성분명으로 ontology 후보 반환 (최대 10개) — S4-1."""
    if not q or len(q) < 1:
        return []
    q_lower = q.lower()
    rows = db.query(models.IngredientOntology).all()
    hits = []
    for r in rows:
        score = 0
        if r.e_number and r.e_number.lower().startswith(q_lower):
            score = 3
        elif r.canonical_name and q_lower in r.canonical_name.lower():
            score = 2
        elif r.aliases:
            for al in (r.aliases.get("ko", []) + r.aliases.get("en", []) + r.aliases.get("id", [])):
                if q_lower in al.lower():
                    score = 1
                    break
        if score:
            hits.append((score, {"e_number": r.e_number, "canonical_name": r.canonical_name,
                                  "category": r.category, "default_status": r.default_status,
                                  "severity": r.severity, "najis_risk": r.najis_risk}))
    hits.sort(key=lambda x: -x[0])
    return [h for _, h in hits[:10]]


# ── S4-3: 케이스 JSON 내보내기 ──────────────────────────────────────────────
@app.get("/cases/{case_id}/export.json")
def export_case_json(case_id: str, user=Depends(auth.get_current_user),
                     db: Session = Depends(get_db)):
    """케이스 전체 데이터 JSON 번들 — S4-3 SIHALAL/외부 연동 + 검수용."""
    c = _get_case(db, case_id, user)
    prods = db.query(models.Product).filter_by(case_id=case_id).all()
    mats = db.query(models.Material).filter_by(case_id=case_id).all()
    findings = db.query(models.AuditFinding).filter_by(case_id=case_id).all()
    fw = db.query(models.FatwaDecision).filter_by(case_id=case_id).first()
    cert = db.query(models.HalalCertificate).filter_by(case_id=case_id).first()
    cl_items = db.query(models.OnsiteChecklist).filter_by(case_id=case_id).all()
    pool = db.query(models.AuditorPool).filter_by(case_id=case_id).all()
    invs = db.query(models.Invoice).filter_by(case_id=case_id).all()
    return {
        "export_version": "1.0",
        "case": {"case_id": c.case_id, "org_id": c.org_id, "company_name": c.company_name,
                 "nib": c.nib, "status": c.status, "pathway": c.pathway,
                 "is_msme": c.is_msme, "fatwa_status": c.fatwa_status,
                 "scope_frozen": c.scope_frozen, "created_at": str(c.created_at)},
        "products": [{"product_id": p.product_id, "name": p.name, "category": p.category}
                     for p in prods],
        "materials": [{"material_id": m.material_id, "name": m.name, "e_number": m.e_number,
                       "mat_type": m.mat_type, "source": m.source, "supplier": m.supplier,
                       "cert_no": m.cert_no, "screen_result": m.screen_result,
                       "screen_status": m.screen_status, "evidence_provided": m.evidence_provided}
                      for m in mats],
        "audit_findings": [{"finding_id": f.finding_id, "area": f.area, "finding": f.finding,
                            "severity": f.severity, "status": f.status, "due_date": f.due_date}
                           for f in findings],
        "fatwa": {"decision": fw.decision if fw else None,
                  "committee_head": fw.committee_head if fw else None,
                  "product_scope": fw.product_scope if fw else None} if fw else None,
        "certificate": {"certificate_no": cert.certificate_no, "issue_date": cert.issue_date,
                        "expiry_date": cert.expiry_date, "scope": cert.scope} if cert else None,
        "onsite_checklist": [{"item_key": x.item_key, "result": x.result, "note": x.note}
                             for x in cl_items],
        "auditor_pool": [{"name": x.name, "cert_no": x.cert_no, "role_in_team": x.role_in_team}
                         for x in pool],
        "invoices": [{"invoice_no": i.invoice_no, "service_type": i.service_type,
                      "total": i.total, "status": i.status} for i in invs],
    }


# ---------- 정적 UI (M2) ----------
class NoCacheStaticFiles(StaticFiles):
    """UI 정적파일을 항상 재검증(no-cache) — 배포 후 하드리프레시 없이 최신 반영."""
    async def get_response(self, path, scope):
        resp = await super().get_response(path, scope)
        resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        resp.headers["Pragma"] = "no-cache"
        resp.headers["Expires"] = "0"
        return resp


_static = os.path.join(os.path.dirname(__file__), "static")
app.mount("/ui", NoCacheStaticFiles(directory=_static, html=True), name="ui")