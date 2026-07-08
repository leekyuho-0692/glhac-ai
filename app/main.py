"""GL-HAC AI API — M1(dual-pathway) + M3(OCR/판정) + M2(인증·UI). 설계 24.14 / B.4."""
import io
import os
import time
import base64
import logging
from datetime import datetime, date, timedelta
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
from . import observability as obs
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


@app.middleware("http")
async def _observability_mw(request: Request, call_next):
    """관측성(§11.3) — 요청 ID·구조화 접근로그·메트릭(라우트 템플릿 기준)."""
    import uuid
    rid = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:16]
    request.state.request_id = rid
    # 역할 best-effort 추출(로깅용, 인증 강제 아님)
    role = None
    authz = request.headers.get("Authorization") or ""
    if authz.startswith("Bearer "):
        p = auth.verify_token(authz[7:])
        role = p.get("role") if p else None
    t0 = time.perf_counter()
    status = 500
    try:
        resp = await call_next(request)
        status = resp.status_code
        resp.headers["X-Request-ID"] = rid
    finally:
        dur = time.perf_counter() - t0
        rt = request.scope.get("route")
        route = getattr(rt, "path", None) or "unmatched"
        try:
            obs.observe_request(route, request.method, status, dur)
            obs.access_log(rid, request.method, route, status, dur * 1000, role)
        except Exception:  # noqa: BLE001
            pass
    return resp


@app.exception_handler(Exception)
async def _unhandled_exc(request: Request, exc: Exception):
    """미처리 예외 — 스택 유출 없이 일반화된 500 반환, 서버에는 상세 로깅."""
    rid = getattr(request.state, "request_id", "-")
    log.exception("unhandled error [rid=%s] on %s %s", rid, request.method, request.url.path)
    return JSONResponse(status_code=500, content={"code": "INTERNAL_ERROR", "request_id": rid})


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


def _audit(db, user, action, resource_type=None, resource_id=None, case_id=None, meta=None, commit=True):
    """접근/조회 감사로그 기록(§2.4). commit=False면 상위 트랜잭션에 합류."""
    row = models.AuditLog(actor_id=user.get("uid"), actor_role=user.get("role"),
                          org_id=user.get("org_id"), action=action,
                          resource_type=resource_type, resource_id=resource_id,
                          case_id=case_id, meta=meta)
    db.add(row)
    if commit:
        db.commit()
    return row


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
            "notify_consent": bool(c.notify_consent),
            "draft_state": c.draft_state, "return_reason": c.return_reason,
            "profile_ext": c.profile_ext or {}}


def _notify(db, case, event_type, title, body="", channels=None, role=None):
    """이벤트 알림을 큐(unsent)에 적재. 실제 발송은 비동기 워커(drain)가 처리 — Rizky #5."""
    n = models.Notification(
        org_id=(case.org_id if case else None), case_id=(case.case_id if case else None),
        role=role, event_type=event_type, channels=channels or ["inapp"],
        title=title, body=body, status="unsent")
    db.add(n)
    return n


_NOTIFY_NONRETRY = ("no_credentials", "no_contact", "not_implemented")
_USER_CHANNELS = {"sms", "whatsapp", "email", "kakao"}   # 수신동의 필요(사용자 대상)


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
        contacts, consent = {}, False
        if n.case_id:
            c = db.get(models.CaseApplication, n.case_id)
            if c:
                contacts = {"phone": c.phone, "email": c.email,
                            "webhook": os.environ.get("GLHAC_WEBHOOK_URL")}
                consent = bool(c.notify_consent)
        # 수신동의 게이트 — 사용자 대상 채널(sms/whatsapp/email/kakao)은 동의 시에만.
        # inapp·webhook(시스템 연동)은 항상 발송.
        requested = n.channels or ["inapp"]
        eff = [ch for ch in requested if ch not in _USER_CHANNELS or consent]
        dropped = [ch for ch in requested if ch in _USER_CHANNELS and not consent]
        try:
            results = _nt.dispatch(n, contacts=contacts, channels=eff)
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
            obs.inc("glhac_notification_failed_total")
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


@app.get("/metrics")
def metrics():
    """Prometheus 메트릭 노출(§11.3) — 집계값만(PII 없음). 스크레이프용."""
    from fastapi.responses import PlainTextResponse
    return PlainTextResponse(obs.render_prometheus(),
                             media_type="text/plain; version=0.0.4; charset=utf-8")


@app.get("/metrics/summary")
def metrics_summary(user=Depends(auth.require_roles("operator"))):
    """메트릭 JSON 요약(operator/admin)."""
    return obs.snapshot()


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
    sig = (db.query(models.Signature).filter_by(subject_type="certificate", subject_id=cert.id)
           .order_by(models.Signature.signed_at.desc()).first())
    sig_valid = False
    if sig:
        _, expected = _sign_payload(_cert_canonical(cert))
        sig_valid = (expected == sig.signature_value)
    return {"valid": valid, "certificate_no": cert.certificate_no,
            "company_name": (c.company_name if c else None),
            "status": cert.status, "issue_date": cert.issue_date,
            "expiry_date": cert.expiry_date, "scope": cert.scope,
            "signed": bool(sig), "signature_valid": sig_valid}


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
    return {**auth.make_tokens(u), "role": u.role, "org_id": u.org_id, "username": u.username}


@app.post("/auth/refresh")
def refresh_token(body: schemas.RefreshReq, db: Session = Depends(get_db)):
    """refresh 토큰으로 새 access 토큰 발급(§9.1). token_version 취소 반영."""
    payload = auth.verify_token(body.refresh_token)
    if not payload or payload.get("typ") != "refresh":
        raise HTTPException(401, {"code": "BAD_REFRESH"})
    u = db.get(models.User, payload.get("uid"))
    if not u or payload.get("tv", 0) != (u.token_version or 0):
        raise HTTPException(401, {"code": "TOKEN_REVOKED"})
    return {"token": auth.make_token(u, "access"), "role": u.role,
            "org_id": u.org_id, "username": u.username}


@app.post("/auth/logout")
def logout(user=Depends(auth.get_current_user), db: Session = Depends(get_db)):
    """로그아웃 — token_version 증가로 이 사용자의 모든 토큰(access+refresh) 즉시 무효화."""
    u = db.get(models.User, user["uid"])
    if u:
        u.token_version = (u.token_version or 0) + 1
        db.commit()
    return {"ok": True, "revoked": True}


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
    # 공개 엔드포인트 — 크기 상한 강제(DoS 방지). 포맷은 OCR가 처리하므로 미제약.
    raw = base64.b64decode(_validate_upload(body.image_b64, None))
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


@app.get("/auth/check-username")
def check_username(username: str, db: Session = Depends(get_db)):
    """회원가입 id 중복검사 (자체 DB). SIHALAL 회사 등록조회는 /sihalal/lookup 별도 사용."""
    dup = bool(db.query(models.User).filter_by(username=username).first())
    return {"username": username, "available": not dup, "duplicate": dup, "source": "internal"}


@app.post("/auth/register")
def register(body: schemas.RegisterReq, db: Session = Depends(get_db)):
    if db.query(models.User).filter_by(username=body.username).first():
        raise HTTPException(409, {"code": "DUPLICATE_ACCOUNT"})
    company_name = (body.company_name or "").strip()
    # 회사1:직원N — 소속회사명으로 기존 org(Company) 매핑 (Phase 2)
    existing = db.query(models.Org).filter(models.Org.name == company_name).first() if company_name else None
    if existing:
        org = existing.org_id
        company_role = "client_staff"    # 기존 회사 추가 직원 = 업무자
    else:
        org = "org_" + models.uid()[:8]
        company_role = "client_admin"    # 새 회사 첫 가입자 = 기업업무 관리자
        db.add(models.Org(org_id=org, name=company_name or "My Company", address=body.address))
    u = models.User(username=body.username, password_hash=auth.hash_pw(body.password),
                    role="applicant", org_id=org, company_role=company_role)
    db.add(u)
    # 새 회사(관리자)만 초기 케이스 프리필(Rizky #1) — 기존 회사 직원은 기존 케이스 활용
    if not existing and (company_name or body.nib):
        c = models.CaseApplication(org_id=org, company_name=company_name or "My Company",
                                   nib=body.nib, responsible_person=body.responsible_person,
                                   address=body.address, factory_address=body.factory_address,
                                   is_msme=True)
        db.add(c)
        db.flush()
        sm.record_event(db, c, None, "onboarding", "case.create.register", "applicant", u.user_id)
    db.commit()
    return {**auth.make_tokens(u), "role": u.role, "org_id": u.org_id, "username": u.username,
            "company_role": company_role}


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
    _audit(db, user, "admin.user.patch", "user", user_id, None,
           {"role": body.role, "password_changed": bool(body.password)}, commit=False)
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


@app.get("/admin/audit-logs")
def admin_list_audit_logs(actor_id: str = None, action: str = None,
                          resource_type: str = None, resource_id: str = None,
                          case_id: str = None, org_id: str = None,
                          limit: int = 100, offset: int = 0,
                          user=Depends(auth.require_roles()), db: Session = Depends(get_db)):
    """조회/접근 감사로그 열람(§2.4) — admin 전용, 필터·페이징 지원."""
    q = db.query(models.AuditLog)
    if actor_id:
        q = q.filter(models.AuditLog.actor_id == actor_id)
    if action:
        q = q.filter(models.AuditLog.action == action)
    if resource_type:
        q = q.filter(models.AuditLog.resource_type == resource_type)
    if resource_id:
        q = q.filter(models.AuditLog.resource_id == resource_id)
    if case_id:
        q = q.filter(models.AuditLog.case_id == case_id)
    if org_id:
        q = q.filter(models.AuditLog.org_id == org_id)
    total = q.count()
    limit = max(1, min(int(limit), 500))
    offset = max(0, int(offset))
    rows = (q.order_by(models.AuditLog.created_at.desc())
              .offset(offset).limit(limit).all())
    items = [{
        "id": r.id, "actor_id": r.actor_id, "actor_role": r.actor_role,
        "org_id": r.org_id, "action": r.action,
        "resource_type": r.resource_type, "resource_id": r.resource_id,
        "case_id": r.case_id, "meta": r.meta,
        "created_at": r.created_at.isoformat() if r.created_at else None,
    } for r in rows]
    return {"total": total, "limit": limit, "offset": offset, "items": items}


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
               limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0)):
    """케이스 목록(§8.1 페이징). total 포함 봉투 — 클라이언트가 페이지 순회로 전량 적재.
    기존 default 500·no-total은 500건 초과 조직에서 조용히 누락됐음."""
    q = db.query(models.CaseApplication)
    if user["role"] != "admin":
        q = q.filter_by(org_id=user["org_id"])
    total = q.count()
    rows = (q.order_by(models.CaseApplication.created_at.desc())
            .offset(offset).limit(limit).all())
    items = [{"case_id": c.case_id, "company_name": c.company_name, "status": c.status,
              "pathway": c.pathway, "due_date": c.due_date, "draft_state": c.draft_state,
              "province": _province_of(c.factory_address or c.address)}
             for c in rows]
    return {"total": total, "limit": limit, "offset": offset,
            "count": len(items), "items": items}


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


@app.post("/admin/notifications/drain")
def admin_drain_notifications(user=Depends(auth.require_roles("operator")),
                              db: Session = Depends(get_db)):
    """미발송 알림 큐를 즉시 발송(drain) — 관리자 수동 트리거(워커 미기동 시에도 실발송).
    크리덴셜(GLHAC_TWILIO_*/GLHAC_SMTP_*) 설정 시 실제 전송, 미설정 시 stub 로그 후 sent 처리."""
    stats = drain_notifications(db)
    _audit(db, user, "notification.drain", "notification", None, None, stats)
    return stats


@app.get("/admin/notifications")
def admin_list_notifications(status: str = None, limit: int = 100,
                             user=Depends(auth.require_roles("operator")),
                             db: Session = Depends(get_db)):
    """알림 발송 현황 — 상태별 집계 + 목록(관리자). 실발송 가시성."""
    q = db.query(models.Notification)
    if user["role"] != "admin":
        q = q.filter_by(org_id=user["org_id"])
    all_rows = q.all()
    counts = {"unsent": 0, "sent": 0, "failed": 0, "sending": 0}
    for n in all_rows:
        counts[n.status] = counts.get(n.status, 0) + 1
    fq = q
    if status:
        fq = fq.filter(models.Notification.status == status)
    rows = fq.order_by(models.Notification.created_at.desc()).limit(min(int(limit), 500)).all()
    items = [{"id": n.notification_id, "event_type": n.event_type, "title": n.title,
              "channels": n.channels, "status": n.status, "attempts": n.attempts,
              "last_error": n.last_error, "role": n.role, "case_id": n.case_id,
              "created_at": str(n.created_at)} for n in rows]
    return {"counts": counts, "total": len(items), "items": items}


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
                      user=Depends(rbac.require_action("audit.mock_decide")),
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


@app.get("/fatwa/dashboard")
def fatwa_dashboard(user=Depends(auth.require_roles("fatwa_liaison", "operator")),
                    db: Session = Depends(get_db)):
    """파트와 위원회 대시보드 — 심의 대기/가승인/최종승인/반려 집계 + 케이스 목록."""
    q = db.query(models.CaseApplication)
    if user["role"] != "admin":
        q = q.filter_by(org_id=user["org_id"])
    cases = q.all()
    fds = {f.case_id: f for f in db.query(models.FatwaDecision).all()}
    counts = {"review": 0, "provisional": 0, "approved": 0, "rejected": 0, "conditional": 0}
    items = []
    for c in cases:
        fs = c.fatwa_status or "none"
        in_scope = c.status in ("fatwa_review", "fatwa_approved") or fs in (
            "provisional", "approved", "rejected", "conditional")
        if not in_scope:
            continue
        if c.status == "fatwa_review":
            counts["review"] += 1
        if fs in counts:
            counts[fs] += 1
        fd = fds.get(c.case_id)
        items.append({"case_id": c.case_id, "company": c.company_name, "status": c.status,
                      "fatwa_status": fs, "decision_no": fd.decision_no if fd else None,
                      "committee_head": fd.committee_head if fd else None,
                      "final_approved_at": str(fd.final_approved_at) if (fd and fd.final_approved_at) else None})
    return {"counts": counts, "total": len(items), "items": items}


@app.get("/fatwa/queue")
def fatwa_queue(user=Depends(auth.require_roles("fatwa_liaison", "operator")),
                db: Session = Depends(get_db)):
    """파트와 심의 대기 큐 — fatwa_review 단계 케이스(샤리아·운영자)."""
    return _stage_queue(db, user, FATWA_STAGES)


@app.get("/cases/{case_id}")
def get_case(case_id: str, user=Depends(auth.get_current_user), db: Session = Depends(get_db)):
    c = _get_case(db, case_id, user)
    _audit(db, user, "case.read", "case", case_id, case_id)
    return _case_dict(c)


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
    p = models.Product(case_id=case_id, name=body.name, category=body.category,
                       registration_type=body.registration_type, status="draft")
    db.add(p)
    db.commit()
    return {"product_id": p.product_id}


@app.post("/cases/{case_id}/materials")
def add_material(case_id: str, body: schemas.MaterialCreate,
                 user=Depends(rbac.require_action("material.add")),
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
                    user=Depends(rbac.require_action("material.delete")),
                    db: Session = Depends(get_db)):
    m = db.get(models.Material, material_id)
    if m:
        _get_case(db, m.case_id, user)
        db.delete(m)
        db.commit()
    return {"deleted": material_id}


@app.patch("/materials/{material_id}/rename")
def rename_material(material_id: str, body: schemas.MaterialRenameReq,
                    user=Depends(auth.require_roles("applicant", "consultant")),
                    db: Session = Depends(get_db)):
    """원재료명 교정(OCR 오독 수기 수정) → 재스크리닝. 정확한 성분명으로 판정 갱신."""
    nm = (body.name or "").strip()
    if not nm:
        raise HTTPException(422, {"code": "NAME_REQUIRED"})
    m = db.get(models.Material, material_id)
    if not m:
        raise HTTPException(404, {"code": "MATERIAL_NOT_FOUND"})
    c = _get_case(db, m.case_id, user)
    prev = m.name
    m.name = nm
    sc = screening.screen_merged(nm, m.e_number, m.source, m.cert_no,
                                 bool(m.evidence_provided),
                                 m.source_known if m.source_known is not None else True, m.note or "")
    m.screen_result, m.screen_status, m.screen_severity = sc["result"], sc["status"], sc["severity"]
    m.matched_uid = sc.get("matched_uid")
    sm.record_event(db, c, c.status, c.status, "material.rename", user["role"], user["uid"],
                    {"material_id": material_id, "from": prev, "to": nm, "result": sc["result"]})
    db.commit()
    return {"material_id": material_id, "name": nm, "screen_result": sc["result"],
            "matched_uid": sc.get("matched_uid")}


def _norm_material(name):
    """원재료명 정규화 — 공백·괄호주석·구두점·국가/원산지 접미 제거해 중복 판정 키."""
    import re
    s = (name or "").lower()
    s = re.sub(r"\([^)]*\)", "", s)           # (중국), (대상) 등 괄호 제거
    s = re.sub(r"[\s\-_·,#]|중국산|국내산|수입", "", s)
    return s.strip()


@app.post("/cases/{case_id}/materials/dedup")
def dedup_materials(case_id: str, user=Depends(auth.require_roles("applicant", "consultant")),
                    db: Session = Depends(get_db)):
    """중복 원재료 정리 — 정규화 동일명(괄호·공백·원산지 차이) 그룹당 1개만 유지.
    증빙 보유 > CLEARED > 짧은 이름 우선 유지, 나머지 삭제. OCR 오탈자는 보수적으로 미병합."""
    _get_case(db, case_id, user)
    mats = db.query(models.Material).filter_by(case_id=case_id).all()
    ev = dict(db.query(models.DocumentAsset.material_id, func.count(models.DocumentAsset.document_id))
              .filter(models.DocumentAsset.case_id == case_id,
                      models.DocumentAsset.material_id.isnot(None))
              .group_by(models.DocumentAsset.material_id).all())
    groups = {}
    for m in mats:
        key = _norm_material(m.name)
        if key:
            groups.setdefault(key, []).append(m)
    removed = 0
    for key, grp in groups.items():
        if len(grp) < 2:
            continue
        # 유지 우선순위: 증빙 많음 → CLEARED → 이름 짧음
        grp.sort(key=lambda m: (-(ev.get(m.material_id, 0)),
                                0 if m.screen_result in ("CLEARED", "PASS") else 1,
                                len(m.name or "")))
        keep = grp[0]
        for m in grp[1:]:
            if not ev.get(m.material_id):   # 증빙 붙은 건 안전하게 보존
                db.delete(m); removed += 1
    _audit(db, user, "material.dedup", "case", case_id, case_id, {"removed": removed})
    db.commit()
    return {"removed": removed, "remaining": len(mats) - removed}


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
    links = db.query(models.ProductMaterial).filter_by(case_id=case_id).all()
    mstat = {m.material_id: m.screen_status for m in db.query(models.Material).filter_by(case_id=case_id)}
    mc, risk = {}, {}
    for lk in links:
        mc[lk.product_id] = mc.get(lk.product_id, 0) + 1
        if mstat.get(lk.material_id) in ("haram", "mushbooh"):
            risk[lk.product_id] = risk.get(lk.product_id, 0) + 1
    return [{"product_id": p.product_id, "name": p.name, "category": p.category,
             "registration_type": p.registration_type, "status": p.status or "draft",
             "material_count": mc.get(p.product_id, 0), "risk_count": risk.get(p.product_id, 0),
             "photo_count": ph.get(p.product_id, 0)} for p in rows]


@app.get("/cases/{case_id}/products/{product_id}")
def get_product(case_id: str, product_id: str, user=Depends(auth.get_current_user),
                db: Session = Depends(get_db)):
    """제품 상세 + 소속 원재료(Material Matrix) — Rizky Product Detail."""
    _get_case(db, case_id, user)
    p = db.get(models.Product, product_id)
    if not p or p.case_id != case_id:
        raise HTTPException(404, {"code": "PRODUCT_NOT_FOUND"})
    lmids = [lk.material_id for lk in db.query(models.ProductMaterial).filter_by(case_id=case_id, product_id=product_id)]
    mats = (db.query(models.Material).filter(models.Material.material_id.in_(lmids)).all() if lmids else [])
    cert = db.query(models.HalalCertificate).filter_by(case_id=case_id, status="active").first()
    return {"product_id": p.product_id, "name": p.name, "category": p.category,
            "registration_type": p.registration_type, "status": p.status or "draft",
            "certificate_no": cert.certificate_no if cert else None,
            "expiry_date": cert.expiry_date if cert else None,
            "materials": [{"material_id": m.material_id, "name": m.name,
                           "screen_status": m.screen_status, "screen_severity": m.screen_severity,
                           "cert": m.cert, "supplier": m.supplier,
                           "evidence_provided": bool(m.evidence_provided)} for m in mats]}


@app.patch("/cases/{case_id}/products/{product_id}")
def update_product(case_id: str, product_id: str, body: schemas.ProductUpdate,
                   user=Depends(auth.require_roles("applicant", "consultant")),
                   db: Session = Depends(get_db)):
    _get_case(db, case_id, user)
    p = db.get(models.Product, product_id)
    if not p or p.case_id != case_id:
        raise HTTPException(404, {"code": "PRODUCT_NOT_FOUND"})
    if body.category is not None:
        p.category = body.category
    if body.registration_type is not None:
        p.registration_type = body.registration_type
    if body.status is not None:
        p.status = body.status
    db.commit()
    return {"product_id": p.product_id, "status": p.status, "registration_type": p.registration_type}


def _exif_gps(b64_or_bytes):
    """이미지에서 EXIF GPS 위경도 추출 → (lat, lng) 또는 None. 현장실사 사진 위치파싱."""
    try:
        import io
        from PIL import Image, ExifTags
        data = b64_or_bytes if isinstance(b64_or_bytes, (bytes, bytearray)) else base64.b64decode(
            str(b64_or_bytes).split(",")[-1])
        gps = Image.open(io.BytesIO(data)).getexif().get_ifd(ExifTags.IFD.GPSInfo)
        if not gps:
            return None
        def dms(v, ref):
            d, m, s = (float(x) for x in v)
            r = d + m / 60 + s / 3600
            return -r if ref in ("S", "W") else r
        lat, latr, lng, lngr = gps.get(2), gps.get(1), gps.get(4), gps.get(3)
        if lat and lng and latr and lngr:
            return (round(dms(lat, latr), 6), round(dms(lng, lngr), 6))
    except Exception:  # noqa: BLE001
        return None
    return None


@app.post("/cases/{case_id}/materials/{material_id}/evidence")
def add_material_evidence(case_id: str, material_id: str, body: schemas.MaterialEvidenceReq,
                          user=Depends(rbac.require_action("material.evidence")),
                          db: Session = Depends(get_db)):
    """원재료 증빙 업로드 → evidence_provided=true → 자동 재스크리닝 (순환점 C1)."""
    from .intake import _ctype
    c = _get_case(db, case_id, user)
    m = db.get(models.Material, material_id)
    if not m or m.case_id != case_id:
        raise HTTPException(404, {"code": "MATERIAL_NOT_FOUND"})
    b64 = _validate_upload(body.file_b64, body.filename)
    gps = _exif_gps(b64)
    db.add(models.DocumentAsset(case_id=case_id, filename=body.filename, doc_type=body.evidence_type,
                                material_id=material_id, review_status="pending",
                                content_b64=b64 if len(b64) < 4_000_000 else None,
                                content_type=_ctype(body.filename),
                                lat=gps[0] if gps else None, lng=gps[1] if gps else None,
                                geo_source="exif" if gps else None))
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
    gps = _exif_gps(b64)
    db.add(models.DocumentAsset(case_id=case_id, filename=body.filename, doc_type="product_photo",
                                product_id=product_id, review_status="pending",
                                content_b64=b64 if len(b64) < 4_000_000 else None,
                                content_type=_ctype(body.filename),
                                lat=gps[0] if gps else None, lng=gps[1] if gps else None,
                                geo_source="exif" if gps else None))
    db.commit()
    return {"product_id": product_id, "ok": True, "gps": bool(gps),
            "lat": gps[0] if gps else None, "lng": gps[1] if gps else None}


@app.get("/cases/{case_id}/products/{product_id}/photos")
def list_product_photos(case_id: str, product_id: str,
                        user=Depends(auth.get_current_user), db: Session = Depends(get_db)):
    _get_case(db, case_id, user)
    rows = db.query(models.DocumentAsset).filter_by(case_id=case_id, product_id=product_id,
                                                    doc_type="product_photo").all()
    return [{"document_id": d.document_id, "filename": d.filename, "has_file": bool(d.content_b64),
             "lat": d.lat, "lng": d.lng, "geo_source": d.geo_source} for d in rows]


@app.patch("/documents/{document_id}/geo")
def set_document_geo(document_id: str, body: schemas.GeoReq,
                     user=Depends(auth.require_roles("applicant", "consultant", "auditor", "operator")),
                     db: Session = Depends(get_db)):
    """사진 위치 수동 기록 — EXIF GPS 부재 시 브라우저 geolocation 폴백."""
    d = db.get(models.DocumentAsset, document_id)
    if not d:
        raise HTTPException(404, {"code": "DOC_NOT_FOUND"})
    c = _get_case(db, d.case_id, user)
    d.lat, d.lng = float(body.lat), float(body.lng)
    d.geo_source = body.source if body.source in ("browser", "manual") else "browser"
    _audit(db, user, "document.geo.set", "document", document_id, d.case_id,
           {"lat": d.lat, "lng": d.lng, "source": d.geo_source})
    db.commit()
    return {"document_id": document_id, "lat": d.lat, "lng": d.lng, "geo_source": d.geo_source}


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


def _apply_profile_extras(c, agg, force=False):
    """추출된 주소·책임자·공장등록번호를 케이스 프로필에 반영. 기본은 빈값만, force=True면 덮어씀.
    반환: 채운 필드명 리스트."""
    filled = []
    addr = agg.get("address")
    if addr and (force or not (c.factory_address or c.address)):
        c.factory_address = addr; filled.append("factory_address")
    if agg.get("responsible_person") and (force or not c.responsible_person):
        c.responsible_person = agg["responsible_person"]; filled.append("responsible_person")
    if agg.get("factory_reg_no") and (force or not c.factory_reg_no):
        c.factory_reg_no = agg["factory_reg_no"]; filled.append("factory_reg_no")
    return filled


def _apply_agg_to_case(db, c, agg):
    """추출 필드(회사/NIB/주소/책임자/제품/원재료)를 케이스에 반영 — DocumentAsset 생성은 하지 않음(재처리용)."""
    applied = {"company_set": False, "nib_set": False, "products": 0, "materials": 0, "profile": []}
    if agg.get("company_name") and (not c.company_name or c.company_name in _CO_PLACEHOLDER):
        c.company_name = agg["company_name"]; applied["company_set"] = True
    if agg.get("nib") and not c.nib:
        c.nib = agg["nib"]; applied["nib_set"] = True
    applied["profile"] += _apply_profile_extras(c, agg)
    have_p = {p.name for p in db.query(models.Product).filter_by(case_id=c.case_id)}
    for pn in agg.get("products", []):
        if pn and pn not in have_p:
            db.add(models.Product(case_id=c.case_id, name=pn)); applied["products"] += 1; have_p.add(pn)
    have_m = {_norm_material(m.name) for m in db.query(models.Material).filter_by(case_id=c.case_id)}
    for mn in agg.get("materials", []):
        nk = _norm_material(mn)
        if mn and nk and nk not in have_m:
            sc = screening.screen_merged(mn, None, None, None, False, True, "")
            db.add(models.Material(case_id=c.case_id, name=mn, screen_result=sc["result"],
                                   screen_status=sc["status"], screen_severity=sc["severity"],
                                   matched_uid=sc.get("matched_uid"), v1_risk=sc.get("v1_risk"),
                                   cert=sc.get("v1_cert")))
            applied["materials"] += 1; have_m.add(nk)
    return applied


@app.post("/documents/{document_id}/reprocess")
def reprocess_document(document_id: str, dpi: int = None,
                       user=Depends(auth.require_roles("consultant", "operator")),
                       db: Session = Depends(get_db)):
    """저장된 원본을 재추출·재분류 — OCR/의존성 개선 후 구업로드 문서 치유(설계 B).
    dpi 지정 시 스캔 문서를 고해상도로 재OCR(예: dpi=300, confident-misread 완화 시도)."""
    from .intake import parse_file, classify, aggregate_fields
    d = db.get(models.DocumentAsset, document_id)
    if not d:
        raise HTTPException(404, {"code": "DOC_NOT_FOUND"})
    c = _get_case(db, d.case_id, user)
    if not d.content_b64:
        raise HTTPException(422, {"code": "NO_CONTENT", "detail": "원본 미보관 문서는 재처리 불가"})
    data = base64.b64decode(d.content_b64.split(",")[-1])
    text = parse_file(d.filename, data, dpi=dpi)
    r = classify(d.filename, text)
    prev = d.doc_type
    d.doc_type = r.get("doc_type", "other")
    d.confidence = float(r.get("confidence") or 0)
    d.fields = r.get("fields") or {}
    d.text_excerpt = (text or "")[:300]
    d.translations = None  # 원문 재추출 → 기존 번역 캐시 무효화
    applied = _apply_agg_to_case(db, c, aggregate_fields([{"fields": d.fields}]))
    sm.record_event(db, c, c.status, c.status, "documents.reprocess", user["role"], user["uid"],
                    {"document_id": document_id, "from": prev, "to": d.doc_type,
                     "text_len": len(text or ""), "applied": applied})
    db.commit()
    return {"document_id": document_id, "doc_type": d.doc_type, "confidence": d.confidence,
            "text_len": len(text or ""), "applied": applied}


_LANG_NAME = {"id": "인도네시아어(Bahasa Indonesia)", "en": "영어(English)"}
_LANG_EN = {"id": "Indonesian (Bahasa Indonesia)", "en": "English"}
# 번역 전용 모델 — gemma3:12b는 한글을 되돌려(echo) 번역 실패 → qwen2.5:7b가 KO→ID 안정적
_TRANSLATE_MODEL = os.environ.get("GLHAC_TRANSLATE_MODEL", "qwen2.5:7b")


def _hangul_ratio(s):
    letters = [ch for ch in s if ch.isalpha()]
    if not letters:
        return 0.0
    return sum(1 for ch in letters if "가" <= ch <= "힣") / len(letters)


def _translate_text(text, lang):
    """한→대상언어 문서 번역 — qwen2.5(영문지시)로 청크 분할 번역. 실패 시 ''.
    한글 echo(번역실패) 청크는 1회 재시도. gemma3는 KO를 그대로 반환해 부적합."""
    tgt = _LANG_EN.get(lang, _LANG_NAME.get(lang, lang))
    sysmsg = ("You are a professional document translator. Translate the given Korean text into "
              "%s. Keep proper nouns, registration/business numbers, dates, and figures as-is. "
              "Preserve line breaks. Output ONLY the translation — no Korean characters, "
              "no explanations, no preamble." % tgt)
    out = []
    for i in range(0, len(text), 1500):
        ch = text[i:i + 1500]
        if not ch.strip():
            continue
        res = ai_local.llm_text(sysmsg, ch, model=_TRANSLATE_MODEL) or ""
        # 번역 실패(한글 다량 잔존) 시 1회 재시도
        if _hangul_ratio(res) > 0.15:
            res = ai_local.llm_text(sysmsg, ch, model=_TRANSLATE_MODEL) or res
        out.append(res)
    return "\n".join(out).strip()


@app.post("/documents/{document_id}/translate")
def translate_document(document_id: str, lang: str = "id",
                       user=Depends(auth.get_current_user), db: Session = Depends(get_db)):
    """문서 추출 텍스트를 조회 시점에 번역(온디맨드) + 캐시 — 설계 문서번역."""
    from .intake import parse_file
    from sqlalchemy.orm.attributes import flag_modified
    d = db.get(models.DocumentAsset, document_id)
    if not d:
        raise HTTPException(404, {"code": "DOC_NOT_FOUND"})
    c = _get_case(db, d.case_id, user)
    if lang not in _LANG_NAME:
        raise HTTPException(422, {"code": "LANG_UNSUPPORTED", "allowed": list(_LANG_NAME)})
    cache = dict(d.translations or {})
    if cache.get(lang):
        _audit(db, user, "document.translate.cached", "document", document_id, d.case_id)
        return {"document_id": document_id, "lang": lang, "translated": cache[lang], "cached": True}
    # 전문 재추출(저장 원본) → 없으면 요약본
    if d.content_b64:
        text = parse_file(d.filename, base64.b64decode(d.content_b64.split(",")[-1]))
    else:
        text = d.text_excerpt or ""
    if not (text or "").strip():
        raise HTTPException(422, {"code": "NO_TEXT", "detail": "번역할 추출 텍스트가 없습니다"})
    translated = _translate_text(text, lang)
    if not translated:
        raise HTTPException(502, {"code": "TRANSLATE_FAILED", "detail": "번역 엔진(gemma3) 응답 없음"})
    cache[lang] = translated
    d.translations = cache
    flag_modified(d, "translations")
    _audit(db, user, "document.translate", "document", document_id, d.case_id,
           {"lang": lang, "chars": len(text)})
    db.commit()
    return {"document_id": document_id, "lang": lang, "translated": translated, "cached": False}


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
    applied = {"company_set": False, "nib_set": False, "products": 0, "materials": 0, "profile": []}
    if agg.get("company_name") and (not c.company_name or c.company_name in _CO_PLACEHOLDER):
        c.company_name = agg["company_name"]
        applied["company_set"] = True
    if agg.get("nib") and not c.nib:
        c.nib = agg["nib"]
        applied["nib_set"] = True
    applied["profile"] += _apply_profile_extras(c, agg)
    have_p = {p.name for p in db.query(models.Product).filter_by(case_id=c.case_id)}
    for pn in agg.get("products", []):
        if pn and pn not in have_p:
            db.add(models.Product(case_id=c.case_id, name=pn))
            applied["products"] += 1
            have_p.add(pn)
    have_m = {_norm_material(m.name) for m in db.query(models.Material).filter_by(case_id=c.case_id)}
    for mn in agg.get("materials", []):
        nk = _norm_material(mn)
        if mn and nk and nk not in have_m:
            sc = screening.screen_merged(mn, None, None, None, False, True, "")
            db.add(models.Material(case_id=c.case_id, name=mn, screen_result=sc["result"],
                                   screen_status=sc["status"], screen_severity=sc["severity"],
                                   matched_uid=sc.get("matched_uid"), v1_risk=sc.get("v1_risk"),
                                   cert=sc.get("v1_cert")))
            applied["materials"] += 1
            have_m.add(nk)
    # 사전심사 업로드 → 신청서 임시저장 진입(작성 이어하기 대상)
    if c.status in ("onboarding", "application_draft"):
        c.status = "application_draft"
        if not c.draft_state or c.draft_state in ("returned",):
            c.draft_state = "saved"
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
    if body.profile_ext is not None:
        c.profile_ext = {**(c.profile_ext or {}), **body.profile_ext}  # 확장 양식 병합 저장
    if not c.draft_state or c.draft_state == "returned":
        c.draft_state = "in_progress"  # 편집 시작 → 작성중(반려분 재편집 포함)
    db.commit()
    return _case_dict(c)


@app.get("/cases/{case_id}/company-check")
def company_check(case_id: str, user=Depends(auth.get_current_user), db: Session = Depends(get_db)):
    """신청서 기업명 vs 신청기업 서류(사업자등록증·공장등록증) 추출 기업명 대조 —
    다르면 mismatch 경고(잘못된 회사 서류 업로드 방지). 공급사 서류는 제외."""
    c = _get_case(db, case_id, user)
    docs = (db.query(models.DocumentAsset).filter_by(case_id=case_id)
            .filter(models.DocumentAsset.doc_type.in_(
                ["nib_business_license", "factory_registration"])).all())
    found = []
    for d in docs:
        dc = (d.fields or {}).get("company_name")
        if dc:
            found.append({"filename": d.filename, "doc_type": d.doc_type, "doc_company": dc,
                          "similarity": _name_sim(c.company_name or "", dc)})
    found.sort(key=lambda x: (x["doc_type"] != "nib_business_license", -x["similarity"]))
    best = max((f["similarity"] for f in found), default=None)
    mismatch = bool(found) and best is not None and best < 0.5
    return {"case_company": c.company_name, "applicant_docs": found,
            "mismatch": mismatch, "best_similarity": best,
            "suggested_company": found[0]["doc_company"] if (mismatch and found) else None}


@app.post("/cases/{case_id}/profile/autofill")
def autofill_profile(case_id: str, force: bool = False,
                     user=Depends(auth.require_roles("applicant", "consultant")),
                     db: Session = Depends(get_db)):
    """신청기업 서류(사업자·공장등록증) 재추출 → 회사명·NIB·주소·책임자·공장등록번호 프로필 자동채움.
    force=True면 기존값 덮어씀(잘못 시드된 값 교정용). 공급사 서류는 제외."""
    from .intake import parse_file, classify, aggregate_fields
    c = _get_case(db, case_id, user)
    docs = (db.query(models.DocumentAsset).filter_by(case_id=case_id)
            .filter(models.DocumentAsset.doc_type.in_(
                ["nib_business_license", "factory_registration"])).all())
    classified = []
    for d in docs:
        fields = d.fields or {}
        # 구 추출본에 주소가 없으면 원본 재추출·재분류로 보강
        if d.content_b64 and not fields.get("address"):
            try:
                text = parse_file(d.filename, base64.b64decode(d.content_b64.split(",")[-1]))
                r = classify(d.filename, text)
                if r.get("fields"):
                    fields = r["fields"]; d.fields = fields
                    d.doc_type = r.get("doc_type", d.doc_type)
                    d.text_excerpt = (text or "")[:300]
            except Exception:  # noqa: BLE001
                pass
        classified.append({"fields": fields, "doc_type": d.doc_type})
    agg = aggregate_fields(classified)
    applied = []
    if agg.get("company_name") and (force or not c.company_name or c.company_name in _CO_PLACEHOLDER):
        c.company_name = agg["company_name"]; applied.append("company_name")
    if agg.get("nib") and (force or not c.nib):
        c.nib = agg["nib"]; applied.append("nib")
    applied += _apply_profile_extras(c, agg, force=force)
    _audit(db, user, "profile.autofill", "case", case_id, case_id,
           {"applied": applied, "force": force})
    db.commit()
    return {"applied": applied,
            "extracted": {k: agg.get(k) for k in ("company_name", "nib", "address",
                                                  "responsible_person", "factory_reg_no")}}


# BPJPH SIHALAL 공개조회(비공식·참고용) — 사업자 할랄시스템 등록 확인
_SIHALAL_BASE = "https://cmsbl.halal.go.id/api/search"
# type → (경로, 검색파라미터, 결과 정규화 매핑). 개인정보(감독자명·종교) 제외.
_SIHALAL_KIND = {
    "penyelia": ("data_penyelia", "nama",
                 lambda x: {"name": x.get("nama_pelaku_usaha"), "meta": x.get("skala_usaha"),
                            "sihalal_id": x.get("id_penyelia")}),
    "lph": ("data_lph", "nama_lph",
            lambda x: {"name": x.get("nama_lph"), "status": x.get("status"),
                       "meta": x.get("wilayah") or x.get("namaprovinsi"),
                       "valid_until": x.get("tgl_berlaku"), "reg_no": x.get("no_reg"),
                       "service": x.get("layanan")}),
    "lhln": ("data_lhln", "nama_lhln",
             lambda x: {"name": x.get("nama_lhln"), "status": x.get("status"),
                        "meta": x.get("negara"), "valid_until": x.get("tgl_berlaku"),
                        "reg_no": x.get("no_reg")}),
}


@app.get("/sihalal/lookup")
def verify_sihalal(q: str = None, nama: str = None, type: str = "penyelia",
                   user=Depends(auth.get_current_user)):
    """BPJPH 공개조회(읽기전용·참고용):
    - type=penyelia: 사업자 SIHALAL 등록 확인(회사명)
    - type=lph: 국내 인증기관(LPH) 인가 확인
    - type=lhln: 해외 할랄 인증기관(LHLN) BPJPH 인정 확인(공급사 해외인증서 검증)
    ⚠ 비공식 공개 엔드포인트(불안정·차단 가능). 개인정보 미반환. 공식 확인은 halal.go.id."""
    kind = type if type in _SIHALAL_KIND else "penyelia"
    query = (q or nama or "").strip()
    if len(query) < 2:
        raise HTTPException(422, {"code": "QUERY_TOO_SHORT"})
    path, param, norm = _SIHALAL_KIND[kind]
    import httpx as _hx
    try:
        r = _hx.get("%s/%s" % (_SIHALAL_BASE, path),
                    params={"page": 1, "size": 10, param: query},
                    headers={"User-Agent": "Mozilla/5.0"}, timeout=8.0)
        d = (r.json() or {}).get("data") or {}
        rows = d.get("datas") or []
        matches = [norm(x) for x in rows if x.get("name") or norm(x).get("name")]
        matches = [m for m in matches if m.get("name")]
        result = {"available": True, "type": kind, "query": query,
                  "total": d.get("total_items", len(matches)),
                  "registered": bool(matches), "matches": matches[:10],
                  "source": "BPJPH SIHALAL (공개조회·참고용)"}
        # 중복등록 감지 — 사업자명 유사도 높은 기존 SIHALAL 기록(ID 포함) 표시
        if kind == "penyelia" and matches:
            scored = sorted(((_name_sim(query, m["name"]), m) for m in matches),
                            key=lambda t: -t[0])
            sim, top = scored[0]
            if sim >= 0.6:
                result["duplicate"] = {"name": top["name"], "sihalal_id": top.get("sihalal_id"),
                                       "scale": top.get("meta"), "similarity": round(sim, 2)}
        return result
    except Exception as e:  # noqa: BLE001
        return {"available": False, "type": kind, "query": query, "error": str(e)[:120],
                "note": "BPJPH 공개조회 불가(일시적·차단). 공식 확인은 halal.go.id"}


# ── 신청서 작성 상태(임시저장/작성완료/반려) — 신청단계 오버레이 ──────────
@app.post("/cases/{case_id}/save-draft")
def save_draft(case_id: str, user=Depends(auth.require_roles("applicant", "consultant")),
               db: Session = Depends(get_db)):
    """임시저장 — 신청서를 제출 전 보관(작성 이어하기). status는 그대로."""
    c = _get_case(db, case_id, user)
    if c.status in ("onboarding", "application_draft"):
        c.status = "application_draft"
    c.draft_state = "saved"
    sm.record_event(db, c, c.status, c.status, "application.save_draft", user["role"], user["uid"])
    db.commit()
    return _case_dict(c)


@app.post("/cases/{case_id}/submit-application")
def submit_application(case_id: str, user=Depends(auth.require_roles("applicant", "consultant")),
                       db: Session = Depends(get_db)):
    """작성완료 제출 — 컨설턴트 검토로 넘김."""
    c = _get_case(db, case_id, user)
    prev = c.status
    c.draft_state = "completed"
    c.return_reason = None
    if c.status in ("onboarding", "application_draft"):
        c.status = "consultant_review"
    sm.record_event(db, c, prev, c.status, "application.submit", user["role"], user["uid"])
    _notify(db, c, "application.submitted", "신청서 작성완료 제출",
            "%s 신청서가 제출되었습니다." % (c.company_name or c.case_id), role="consultant")
    db.commit()
    return _case_dict(c)


@app.post("/cases/{case_id}/return-application")
def return_application(case_id: str, body: schemas.ReturnReq,
                       user=Depends(auth.require_roles("consultant", "operator")),
                       db: Session = Depends(get_db)):
    """반려 — 컨설턴트가 신청서를 사유와 함께 작성자에게 되돌림."""
    if not (body.reason or "").strip():
        raise HTTPException(422, {"code": "REASON_REQUIRED"})
    c = _get_case(db, case_id, user)
    prev = c.status
    c.draft_state = "returned"
    c.return_reason = body.reason.strip()
    c.status = "application_draft"
    sm.record_event(db, c, prev, c.status, "application.return", user["role"], user["uid"],
                    {"reason": c.return_reason})
    _notify(db, c, "application.returned", "신청서 반려",
            "반려 사유: %s" % c.return_reason, role="applicant")
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
    raw = base64.b64decode(_validate_upload(body.file_b64, body.filename))   # 크기·타입 검증(§9.3)
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
    from .intake import DOC_KO
    rows = db.query(models.DocumentAsset).filter_by(case_id=case_id).all()
    return [{"document_id": d.document_id, "filename": d.filename, "doc_type": d.doc_type,
             "doc_type_ko": DOC_KO.get(d.doc_type, d.doc_type),
             "confidence": d.confidence, "fields": d.fields, "excerpt": d.text_excerpt,
             "review_status": d.review_status, "has_file": bool(d.content_b64),
             "lat": d.lat, "lng": d.lng, "geo_source": d.geo_source} for d in rows]


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
    # 다운로드 감사 — 워크플로 타임라인 + 통합 감사로그 양쪽
    sm.record_event(db, c, c.status, c.status, "document.download", user["role"], user["uid"],
                    {"document_id": document_id, "filename": d.filename})
    _audit(db, user, "document.download", "document", document_id, d.case_id,
           {"filename": d.filename}, commit=False)
    db.commit()
    raw = _b64lib.b64decode(d.content_b64)
    return Response(content=raw, media_type=d.content_type or "application/octet-stream",
                    headers={"Content-Disposition": "inline; filename*=UTF-8''" +
                             quote(d.filename or "document")})


@app.patch("/documents/{document_id}/review")
def review_document(document_id: str, body: schemas.DocReviewReq,
                    user=Depends(rbac.require_action("document.review")), db: Session = Depends(get_db)):
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
               user=Depends(rbac.require_action("sjph.edit")),
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
    """SJPH/HPAS 매뉴얼 결정적 생성 (v1 상속). 결제 게이트: 청구 존재 시 결제완료 필요(§2.2 비용방지)."""
    c = _get_case(db, case_id, user)
    _invs = db.query(models.Invoice).filter_by(case_id=case_id).all()
    if _invs and not any(i.status == "paid" for i in _invs):
        raise HTTPException(409, {"code": "PAYMENT_REQUIRED",
                                  "detail": "AI SJPH 매뉴얼 생성 전 결제 완료가 필요합니다."})
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


def _render_pdf(title, body, subtitle=None, footer=None):
    """텍스트 문서를 PDF로 렌더(§7). PyMuPDF 내장 'korea' 폰트 → 한글/라틴 모두 지원(외부 TTF 불필요)."""
    import fitz
    W, H = fitz.paper_size("a4")
    margin, fs, lh = 56, 10.5, 15.5
    font, maxw = "korea", (W - 2 * margin)
    doc = fitz.open()

    def wrap(text):
        out = []
        for para in (text or "").split("\n"):
            if not para.strip():
                out.append("")
                continue
            line = ""
            for word in para.split(" "):
                cand = (line + " " + word) if line else word
                if fitz.get_text_length(cand, fontname=font, fontsize=fs) > maxw and line:
                    out.append(line)
                    line = word
                else:
                    line = cand
            out.append(line)
        return out

    pg = doc.new_page(width=W, height=H)
    y = margin
    pg.insert_text((margin, y), title, fontname=font, fontsize=18)
    y += 28
    if subtitle:
        pg.insert_text((margin, y), subtitle, fontname=font, fontsize=10, color=(0.35, 0.35, 0.35))
        y += 20
    pg.draw_line((margin, y), (W - margin, y), color=(0.75, 0.75, 0.75))
    y += 18
    for line in wrap(body):
        if y > H - margin - 20:
            pg = doc.new_page(width=W, height=H)
            y = margin
        pg.insert_text((margin, y), line, fontname=font, fontsize=fs)
        y += lh
    if footer:
        pg.insert_text((margin, H - margin + 4), footer, fontname=font, fontsize=8, color=(0.5, 0.5, 0.5))
    return doc.tobytes()


@app.get("/gen-docs/{gen_doc_id}/pdf")
def get_gendoc_pdf(gen_doc_id: str, user=Depends(auth.get_current_user), db: Session = Depends(get_db)):
    """생성 문서(SJPH Manual·Audit Report)를 PDF로 다운로드(§7)."""
    from fastapi.responses import Response
    from urllib.parse import quote
    g = db.get(models.GeneratedDocument, gen_doc_id)
    if not g:
        raise HTTPException(404, {"code": "GENDOC_NOT_FOUND"})
    c = _get_case(db, g.case_id, user)
    labels = {"sjph_manual": "SJPH Manual", "audit_report": "현장심사 보고서 · Audit Report"}
    title = labels.get(g.doc_type, g.doc_type)
    subtitle = "%s · v%s · %s" % (c.company_name or "", g.version, g.status)
    pdf = _render_pdf(title, g.content or "", subtitle=subtitle,
                      footer="GL-HAC AI · %s" % str(g.created_at)[:19])
    fn = "%s_v%s.pdf" % (g.doc_type, g.version)
    return Response(content=pdf, media_type="application/pdf",
                    headers={"Content-Disposition": "attachment; filename*=UTF-8''" + quote(fn)})


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
                user=Depends(rbac.require_action("finding.add")),
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
                   user=Depends(rbac.require_action("finding.update")),
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
                            user=Depends(rbac.require_action("onsite.checklist")),
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
                     user=Depends(rbac.require_action("auditor_pool.add")),
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


# ---------- LPH 현장심사 일정 (§P2 LPH scheduling) ----------
@app.post("/cases/{case_id}/audit-plan")
def create_audit_plan(case_id: str, body: schemas.AuditPlanReq,
                      user=Depends(auth.require_roles("auditor", "operator", "consultant")),
                      db: Session = Depends(get_db)):
    c = _get_case(db, case_id, user)
    p = models.AuditPlan(case_id=case_id, lph_name=body.lph_name, scheduled_date=body.scheduled_date,
                         scope=body.scope, auditors=body.auditors or [], created_by=user["uid"])
    db.add(p)
    sm.record_event(db, c, c.status, c.status, "audit.plan.create", user["role"], user["uid"],
                    {"scheduled_date": body.scheduled_date})
    _notify(db, c, "audit_scheduled", "현장심사 일정",
            "%s — 현장심사가 %s 로 예정되었습니다." % (c.company_name or "", body.scheduled_date),
            channels=["inapp", "sms"], role="applicant")
    db.commit()
    return {"id": p.id, "scheduled_date": p.scheduled_date, "status": p.status}


@app.get("/cases/{case_id}/audit-plans")
def list_audit_plans(case_id: str, user=Depends(auth.get_current_user), db: Session = Depends(get_db)):
    _get_case(db, case_id, user)
    rows = (db.query(models.AuditPlan).filter_by(case_id=case_id)
            .order_by(models.AuditPlan.scheduled_date).all())
    return [{"id": p.id, "lph_name": p.lph_name, "scheduled_date": p.scheduled_date, "scope": p.scope,
             "auditors": p.auditors or [], "status": p.status, "note": p.note} for p in rows]


@app.patch("/audit-plans/{plan_id}")
def patch_audit_plan(plan_id: str, body: schemas.AuditPlanPatchReq,
                     user=Depends(auth.require_roles("auditor", "operator")),
                     db: Session = Depends(get_db)):
    p = db.get(models.AuditPlan, plan_id)
    if not p:
        raise HTTPException(404, {"code": "PLAN_NOT_FOUND"})
    _get_case(db, p.case_id, user)
    if body.status is not None:
        if body.status not in ("scheduled", "completed", "cancelled"):
            raise HTTPException(400, {"code": "BAD_STATUS"})
        p.status = body.status
    if body.scheduled_date:
        p.scheduled_date = body.scheduled_date
    if body.note is not None:
        p.note = body.note
    db.commit()
    return {"id": p.id, "status": p.status, "scheduled_date": p.scheduled_date}


# ---------- CAR 시정조치 라이프사이클 (§P2 CAR advanced) ----------
@app.post("/findings/{finding_id}/car")
def submit_car(finding_id: str, body: schemas.CarSubmitReq,
               user=Depends(auth.require_roles("applicant", "consultant", "penyelia_halal")),
               db: Session = Depends(get_db)):
    f = db.get(models.AuditFinding, finding_id)
    if not f:
        raise HTTPException(404, {"code": "FINDING_NOT_FOUND"})
    c = _get_case(db, f.case_id, user)
    car = models.CorrectiveAction(case_id=f.case_id, finding_id=finding_id, description=body.description,
                                  evidence=body.evidence, due_date=body.due_date,
                                  submitted_by=user["uid"], status="submitted")
    db.add(car)
    sm.record_event(db, c, c.status, c.status, "car.submit", user["role"], user["uid"],
                    {"finding_id": finding_id})
    db.commit()
    return {"id": car.id, "finding_id": finding_id, "status": car.status}


@app.get("/cases/{case_id}/corrective-actions")
def list_corrective_actions(case_id: str, user=Depends(auth.get_current_user),
                            db: Session = Depends(get_db)):
    _get_case(db, case_id, user)
    rows = (db.query(models.CorrectiveAction).filter_by(case_id=case_id)
            .order_by(models.CorrectiveAction.created_at.desc()).all())
    return [{"id": x.id, "finding_id": x.finding_id, "description": x.description, "evidence": x.evidence,
             "status": x.status, "reviewer": x.reviewer, "reviewer_note": x.reviewer_note,
             "due_date": x.due_date} for x in rows]


@app.patch("/car/{car_id}/review")
def review_car(car_id: str, body: schemas.CarReviewReq,
               user=Depends(auth.require_roles("auditor", "operator")),
               db: Session = Depends(get_db)):
    car = db.get(models.CorrectiveAction, car_id)
    if not car:
        raise HTTPException(404, {"code": "CAR_NOT_FOUND"})
    c = _get_case(db, car.case_id, user)
    if body.status not in ("accepted", "rejected", "closed"):
        raise HTTPException(400, {"code": "BAD_STATUS"})
    car.status = body.status
    car.reviewer = user["uid"]
    car.reviewer_note = body.note
    if body.status in ("accepted", "closed"):   # 시정조치 수용 → finding 종결
        f = db.get(models.AuditFinding, car.finding_id)
        if f:
            f.status = "closed"
    sm.record_event(db, c, c.status, c.status, "car.review", user["role"], user["uid"],
                    {"car_id": car_id, "status": body.status})
    db.commit()
    return {"id": car.id, "status": car.status}


# ---------- Fatwa 위원회 투표 (§6.2 fatwa_votes) ----------
def _fatwa_tally(db, case_id, detail=False):
    votes = db.query(models.FatwaVote).filter_by(case_id=case_id).all()
    fd = db.query(models.FatwaDecision).filter_by(case_id=case_id).first()
    members = (fd.committee_members if fd and fd.committee_members else []) or []
    total = len(members) if members else len(votes)
    approve = sum(1 for v in votes if v.vote == "approve")
    reject = sum(1 for v in votes if v.vote == "reject")
    abstain = sum(1 for v in votes if v.vote == "abstain")
    need = (total // 2) + 1 if total else 1
    quorum = total > 0 and len(votes) >= need
    passed = (quorum and approve >= need) if total else (approve > reject and approve > 0)
    out = {"votes_cast": len(votes), "members": total, "approve": approve, "reject": reject,
           "abstain": abstain, "quorum_met": bool(quorum), "quorum_need": need,
           "result": "passed" if passed else "pending"}
    if detail:
        out["ballots"] = [{"member": v.member, "vote": v.vote, "note": v.note} for v in votes]
    return out


@app.post("/cases/{case_id}/fatwa/vote")
def fatwa_vote(case_id: str, body: schemas.FatwaVoteReq,
               user=Depends(rbac.require_action("fatwa.propose")),
               db: Session = Depends(get_db)):
    c = _get_case(db, case_id, user)
    if body.vote not in ("approve", "reject", "abstain"):
        raise HTTPException(400, {"code": "BAD_VOTE"})
    v = db.query(models.FatwaVote).filter_by(case_id=case_id, member=body.member).first()
    if v:
        v.vote, v.note = body.vote, body.note
    else:
        db.add(models.FatwaVote(case_id=case_id, member=body.member, vote=body.vote, note=body.note))
    sm.record_event(db, c, c.status, c.status, "fatwa.vote", user["role"], user["uid"],
                    {"member": body.member, "vote": body.vote})
    db.commit()
    return _fatwa_tally(db, case_id)


@app.get("/cases/{case_id}/fatwa/votes")
def get_fatwa_votes(case_id: str, user=Depends(auth.get_current_user), db: Session = Depends(get_db)):
    _get_case(db, case_id, user)
    if not _fatwa_privileged(user):   # 위원회 투표 상세는 sharia/operator/admin만
        raise HTTPException(403, {"code": "NOT_AUTHORIZED"})
    _audit(db, user, "fatwa.votes.read", "fatwa", case_id, case_id)
    return _fatwa_tally(db, case_id, detail=True)


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
    obs.inc("glhac_certificate_issued_total")
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
    _audit(db, user, "certificate.read", "certificate", case_id, case_id)
    cert = db.query(models.HalalCertificate).filter_by(case_id=case_id).first()
    if not cert:
        return {"issued": False}
    days = None
    try:
        days = (date.fromisoformat(cert.expiry_date) - date.today()).days
    except Exception:  # noqa: BLE001
        pass
    sig = (db.query(models.Signature).filter_by(subject_type="certificate", subject_id=cert.id)
           .order_by(models.Signature.signed_at.desc()).first())
    return {"issued": True, "certificate_no": cert.certificate_no, "scope": cert.scope,
            "issue_date": cert.issue_date, "expiry_date": cert.expiry_date, "status": cert.status,
            "days_to_expiry": days,
            "frozen_product_ids": cert.frozen_product_ids,
            "frozen_material_ids": cert.frozen_material_ids,
            "qr_token": cert.qr_token,
            "verify_url": ("/verify/" + cert.qr_token) if cert.qr_token else None,
            "signed": bool(sig),
            "signature": ({"signer": sig.signer, "provider": sig.provider,
                           "signed_at": str(sig.signed_at)} if sig else None)}


@app.get("/cases/{case_id}/certificate/pdf")
def certificate_pdf(case_id: str, user=Depends(auth.get_current_user), db: Session = Depends(get_db)):
    """할랄 인증서 PDF(§7·§6.4) — 인증정보·범위·공개검증·서명상태 포함."""
    from fastapi.responses import Response
    from urllib.parse import quote
    c = _get_case(db, case_id, user)
    cert = db.query(models.HalalCertificate).filter_by(case_id=case_id).first()
    if not cert:
        raise HTTPException(404, {"code": "CERT_NOT_FOUND"})
    sig = (db.query(models.Signature).filter_by(subject_type="certificate", subject_id=cert.id)
           .order_by(models.Signature.signed_at.desc()).first())
    lines = [
        "SERTIFIKAT HALAL · 할랄 인증서",
        "",
        "기업 · Perusahaan : %s" % (c.company_name or "-"),
        "인증번호 · No     : %s" % (cert.certificate_no or "-"),
        "상태 · Status     : %s" % cert.status,
        "발급 · Issued     : %s" % (cert.issue_date or "-"),
        "만료 · Valid until : %s" % (cert.expiry_date or "-"),
        "범위 · Scope      : %s" % (", ".join(cert.scope or []) or "-"),
        "",
        "본 제품은 SJPH 및 샤리아 기준에 따라 할랄(HALAL) 인증되었음을 증명합니다.",
        "Produk ini disertifikasi HALAL sesuai SJPH dan kriteria Syariah.",
        "",
        "공개 검증 · Verify : /verify/%s" % (cert.qr_token or "-"),
        "전자서명 · Signed  : %s%s" % ("예 · Yes" if sig else "아니오 · No",
                                       (" (" + (sig.provider or "") + ")") if sig else ""),
    ]
    pdf = _render_pdf("GL-HAC AI · Halal Certificate", "\n".join(lines),
                      subtitle=cert.certificate_no or "",
                      footer="공개 검증 페이지에서 진위를 확인하세요 · Verify authenticity at /verify")
    fn = "certificate_%s.pdf" % (cert.certificate_no or case_id[:8])
    return Response(content=pdf, media_type="application/pdf",
                    headers={"Content-Disposition": "attachment; filename*=UTF-8''" + quote(fn)})


def _idr(n):
    try:
        return "Rp " + format(float(n or 0), ",.0f")
    except Exception:
        return "Rp 0"


def _invoice_ctx(db, invoice_id, user):
    inv = db.get(models.Invoice, invoice_id)
    if not inv:
        raise HTTPException(404, {"code": "INVOICE_NOT_FOUND"})
    c = _get_case(db, inv.case_id, user)
    pay = (db.query(models.Payment).filter_by(invoice_id=invoice_id, status="confirmed")
           .order_by(models.Payment.paid_at.desc()).first())
    return inv, c, pay


@app.get("/invoices/{invoice_id}/receipt")
def invoice_receipt_pdf(invoice_id: str, user=Depends(auth.get_current_user),
                        db: Session = Depends(get_db)):
    """결제 영수증 PDF — 청구·결제 내역. _render_pdf 재사용."""
    from fastapi.responses import Response
    from urllib.parse import quote
    inv, c, pay = _invoice_ctx(db, invoice_id, user)
    status = "waiting_payment" if inv.status == "unpaid" else inv.status
    lines = [
        "발행일 · Date        : %s" % date.today().isoformat(),
        "청구번호 · Invoice No : %s" % (inv.invoice_no or "-"),
        "결제참조 · Pay Ref    : %s" % (inv.payment_ref or "-"),
        "",
        "── 기업 · Company ──",
        "기업명 · Company : %s" % (c.company_name or "-"),
        "NIB              : %s" % (c.nib or "-"),
        "주소 · Address    : %s" % (c.address or c.factory_address or "-"),
        "",
        "── 청구 내역 · Details ──",
        "서비스 · Service : %s" % (inv.service_type or "-"),
        "금액 · DPP        : %s" % _idr(inv.amount),
        "부가세 · PPN 11%%  : %s" % _idr(inv.ppn),
        "────────────────────",
        "합계 · Total      : %s" % _idr(inv.total),
        "",
        "── 결제 · Payment ──",
        "상태 · Status     : %s" % status,
        "결제방식 · Method  : %s" % (pay.method if pay else "-"),
        "결제일 · Paid at   : %s" % (str(pay.paid_at)[:16] if pay else "-"),
        "참조 · Reference   : %s" % (pay.reference if (pay and pay.reference) else "-"),
    ]
    pdf = _render_pdf("GL-HAC AI · 결제 영수증 · Payment Receipt", "\n".join(lines),
                      subtitle=inv.invoice_no or "",
                      footer="본 영수증은 전자적으로 발행되었습니다 · Issued electronically")
    fn = "receipt_%s.pdf" % (inv.invoice_no or invoice_id[:8])
    return Response(content=pdf, media_type="application/pdf",
                    headers={"Content-Disposition": "attachment; filename*=UTF-8''" + quote(fn)})


@app.get("/invoices/{invoice_id}/tax-invoice")
def invoice_tax_pdf(invoice_id: str, user=Depends(auth.get_current_user),
                    db: Session = Depends(get_db)):
    """세금계산서(Faktur Pajak) PDF — 과세표준·PPN 11%. _render_pdf 재사용."""
    from fastapi.responses import Response
    from urllib.parse import quote
    inv, c, pay = _invoice_ctx(db, invoice_id, user)
    lines = [
        "FAKTUR PAJAK · 세금계산서",
        "",
        "Nomor · 번호        : %s" % (inv.invoice_no or "-"),
        "Tanggal · 발행일     : %s" % date.today().isoformat(),
        "",
        "── Penjual · 공급자 ──",
        "Nama : GL-HAC AI (Lembaga Sertifikasi Halal)",
        "",
        "── Pembeli · 구매자 ──",
        "Nama · 기업 : %s" % (c.company_name or "-"),
        "NPWP/NIB    : %s" % (c.nib or "-"),
        "Alamat · 주소: %s" % (c.address or c.factory_address or "-"),
        "",
        "── Rincian · 내역 ──",
        "Jasa · 서비스        : %s" % (inv.service_type or "-"),
        "DPP · 과세표준       : %s" % _idr(inv.amount),
        "PPN 11%%             : %s" % _idr(inv.ppn),
        "────────────────────",
        "Total · 합계         : %s" % _idr(inv.total),
    ]
    pdf = _render_pdf("FAKTUR PAJAK · 세금계산서", "\n".join(lines),
                      subtitle=inv.invoice_no or "",
                      footer="PPN 11% sesuai peraturan perpajakan Indonesia")
    fn = "faktur_%s.pdf" % (inv.invoice_no or invoice_id[:8])
    return Response(content=pdf, media_type="application/pdf",
                    headers={"Content-Disposition": "attachment; filename*=UTF-8''" + quote(fn)})


# ---------- 인증서 lifecycle: 정지/철회/재개 (§5.1·§5.2) ----------
def _cert_status_change(db, case_id, user, action, new_status, from_status, reason,
                        event, title, body):
    """정지/철회/재개 공통 — from_status 인증서만 대상, 사유·통지·감사."""
    c = _get_case(db, case_id, user)
    cert = db.query(models.HalalCertificate).filter_by(case_id=case_id).first()
    if not cert:
        raise HTTPException(404, {"code": "CERT_NOT_FOUND"})
    if cert.status != from_status:
        raise HTTPException(409, {"code": "BAD_CERT_STATE", "have": cert.status, "need": from_status})
    cert.status = new_status
    sm.record_event(db, c, c.status, c.status, action, user["role"], user["uid"],
                    {"certificate_no": cert.certificate_no, "reason": reason,
                     "from": from_status, "to": new_status})
    _notify(db, c, event, title, "%s — %s" % (c.company_name or "", body),
            channels=["inapp", "sms"], role="applicant")
    db.commit()
    return {"certificate_no": cert.certificate_no, "status": cert.status, "reason": reason}


@app.post("/cases/{case_id}/certificate/suspend")
def suspend_certificate(case_id: str, body: schemas.CertStatusReq,
                        user=Depends(rbac.require_action("certificate.suspend")),
                        db: Session = Depends(get_db)):
    """활성 인증서 정지(active → suspended). 사유 필수·신청자 통지."""
    return _cert_status_change(db, case_id, user, "certificate.suspend", "suspended", "active",
                               body.reason.strip(), "certificate_suspended", "인증서 정지",
                               "할랄 인증서가 정지되었습니다.")


@app.post("/cases/{case_id}/certificate/reactivate")
def reactivate_certificate(case_id: str, body: schemas.CertStatusReq,
                           user=Depends(rbac.require_action("certificate.reactivate")),
                           db: Session = Depends(get_db)):
    """정지 인증서 재개(suspended → active). 재심/보완 승인 후."""
    return _cert_status_change(db, case_id, user, "certificate.reactivate", "active", "suspended",
                               body.reason.strip(), "certificate_reactivated", "인증서 재개",
                               "할랄 인증서 정지가 해제되었습니다.")


@app.post("/cases/{case_id}/certificate/revoke")
def revoke_certificate(case_id: str, body: schemas.CertStatusReq,
                       user=Depends(rbac.require_action("certificate.revoke")),
                       db: Session = Depends(get_db)):
    """인증서 철회(active/suspended → withdrawn). 되돌릴 수 없음·통지."""
    c = _get_case(db, case_id, user)
    cert = db.query(models.HalalCertificate).filter_by(case_id=case_id).first()
    if not cert:
        raise HTTPException(404, {"code": "CERT_NOT_FOUND"})
    if cert.status not in ("active", "suspended"):
        raise HTTPException(409, {"code": "BAD_CERT_STATE", "have": cert.status})
    cert.status = "withdrawn"
    sm.record_event(db, c, c.status, c.status, "certificate.revoke", user["role"], user["uid"],
                    {"certificate_no": cert.certificate_no, "reason": body.reason.strip()})
    _notify(db, c, "certificate_revoked", "인증서 철회",
            "%s — 할랄 인증서가 철회되었습니다." % (c.company_name or ""),
            channels=["inapp", "sms"], role="applicant")
    db.commit()
    return {"certificate_no": cert.certificate_no, "status": "withdrawn", "reason": body.reason.strip()}


# ---------- 전자서명 (§6.4 e-signature, 내부 HMAC MVP) ----------
def _cert_canonical(cert):
    return "|".join([cert.certificate_no or "", cert.issue_date or "", cert.expiry_date or "",
                     ",".join(cert.scope or [])])


def _sign_payload(payload: str):
    import hmac as _h
    import hashlib as _hl
    ph = _hl.sha256(payload.encode()).hexdigest()
    sig = _h.new(auth.SECRET, payload.encode(), _hl.sha256).hexdigest()
    return ph, sig


@app.post("/cases/{case_id}/certificate/sign")
def sign_certificate(case_id: str, user=Depends(rbac.require_action("certificate.issue")),
                     db: Session = Depends(get_db)):
    """발급된 인증서에 발급자 전자서명(내부 HMAC) — 무결성+발급자 증빙(§6.4)."""
    c = _get_case(db, case_id, user)
    cert = db.query(models.HalalCertificate).filter_by(case_id=case_id, status="active").first()
    if not cert:
        raise HTTPException(409, {"code": "NO_ACTIVE_CERT"})
    ph, sig = _sign_payload(_cert_canonical(cert))
    row = models.Signature(case_id=case_id, subject_type="certificate", subject_id=cert.id,
                           signer=user["uid"], payload_hash=ph, signature_value=sig)
    db.add(row)
    sm.record_event(db, c, c.status, c.status, "certificate.sign", user["role"], user["uid"],
                    {"signature_id": row.id})
    db.commit()
    return {"signature_id": row.id, "signer": user["uid"], "provider": row.provider,
            "payload_hash": ph, "signed_at": str(row.signed_at)}


# ---------- SIHALAL 외부 연동 이벤트 (§10.1) ----------
_INTEGRATION_EVENTS = {"external_application_created", "document_package_submitted",
                       "external_status_synced", "rejection_received",
                       "additional_document_requested", "certificate_number_imported",
                       "certificate_status_changed"}


@app.post("/integration/sihalal/event")
def integration_event(body: schemas.IntegrationEventReq,
                      user=Depends(auth.require_roles("operator")),
                      db: Session = Depends(get_db)):
    """외부 연동 이벤트 기록 — idempotency_key로 중복수신 방지(§10.1)."""
    if body.event_type not in _INTEGRATION_EVENTS:
        raise HTTPException(400, {"code": "BAD_EVENT_TYPE", "allowed": sorted(_INTEGRATION_EVENTS)})
    dup = db.query(models.IntegrationEvent).filter_by(idempotency_key=body.idempotency_key).first()
    if dup:
        return {"id": dup.id, "idempotent": True, "status": dup.status}
    import json as _json
    import hashlib as _hl
    req_hash = _hl.sha256(_json.dumps(body.payload or {}, sort_keys=True).encode()).hexdigest()
    ev = models.IntegrationEvent(provider="sihalal", event_type=body.event_type,
                                 external_id=body.external_id, idempotency_key=body.idempotency_key,
                                 case_id=body.case_id, payload=body.payload, request_hash=req_hash,
                                 status="received")
    db.add(ev)
    db.commit()
    obs.inc("glhac_integration_event_total", {"type": body.event_type})
    return {"id": ev.id, "idempotent": False, "status": ev.status, "request_hash": req_hash}


@app.get("/cases/{case_id}/integration/events")
def list_integration_events(case_id: str, user=Depends(auth.get_current_user),
                            db: Session = Depends(get_db)):
    _get_case(db, case_id, user)
    rows = (db.query(models.IntegrationEvent).filter_by(case_id=case_id)
            .order_by(models.IntegrationEvent.created_at.desc()).all())
    return [{"id": e.id, "provider": e.provider, "event_type": e.event_type,
             "external_id": e.external_id, "status": e.status,
             "created_at": str(e.created_at)} for e in rows]


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
    _audit(db, user, "fatwa.document.read", "fatwa", case_id, case_id)
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
             "amount": i.amount, "ppn": i.ppn, "total": i.total,
             "status": ("waiting_payment" if i.status == "unpaid" else i.status),
             "payment_ref": i.payment_ref, "due_date": str(i.due_date) if i.due_date else None}
            for i in rows]


@app.post("/cases/{case_id}/invoices")
def add_invoice(case_id: str, body: schemas.InvoiceReq,
                user=Depends(auth.require_roles("consultant")), db: Session = Depends(get_db)):
    c = _get_case(db, case_id, user)
    ppn = round(body.amount * 0.11, 2)
    total = round(body.amount + ppn, 2)
    inv = models.Invoice(case_id=case_id, service_type=body.service_type, amount=body.amount,
                         ppn=ppn, total=total, status="waiting_payment",
                         due_date=datetime.utcnow() + timedelta(days=14))
    db.add(inv)
    db.flush()
    inv.invoice_no = "INV-" + inv.invoice_id[:8].upper()
    inv.payment_ref = "PAY-" + inv.invoice_id[:8].upper()
    _audit(db, user, "payment.invoice.create", "invoice", inv.invoice_id, case_id,
           {"total": total, "service_type": body.service_type}, commit=False)
    sm.record_event(db, c, c.status, c.status, "invoice.create", user["role"], user["uid"],
                    {"service_type": body.service_type, "total": total})
    db.commit()
    return {"invoice_id": inv.invoice_id, "invoice_no": inv.invoice_no, "ppn": ppn, "total": total,
            "status": inv.status, "payment_ref": inv.payment_ref,
            "due_date": str(inv.due_date)}


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


# ---------- 결제 기록 (§P2 billing/payment) ----------
@app.post("/invoices/{invoice_id}/payment")
def record_payment(invoice_id: str, body: schemas.PaymentReq,
                   user=Depends(auth.require_roles("applicant", "consultant", "operator")),
                   db: Session = Depends(get_db)):
    """결제 확인 기록 — Payment 생성 + 인보이스 paid 처리(결제 게이트웨이 시뮬레이션)."""
    inv = db.get(models.Invoice, invoice_id)
    if not inv:
        raise HTTPException(404, {"code": "INVOICE_NOT_FOUND"})
    c = _get_case(db, inv.case_id, user)
    if body.method not in ("bank_transfer", "va", "card", "manual"):
        raise HTTPException(400, {"code": "BAD_METHOD"})
    paid_amt = body.amount if body.amount is not None else inv.total
    auto = body.method in ("va", "card") and abs((paid_amt or 0) - (inv.total or 0)) < 0.01
    p = models.Payment(invoice_id=invoice_id, case_id=inv.case_id, amount=paid_amt,
                       method=body.method, reference=body.reference,
                       status=("confirmed" if auto else "pending"), paid_by=user["uid"])
    db.add(p)
    frm = inv.status
    inv.status = "paid" if auto else "need_verification"   # 은행이체/수기·금액불일치 → 관리자 검증
    _audit(db, user, "payment.record", "invoice", invoice_id, inv.case_id,
           {"method": body.method, "amount": paid_amt, "auto": auto,
            "before": frm, "after": inv.status}, commit=False)
    sm.record_event(db, c, c.status, c.status, "invoice.payment", user["role"], user["uid"],
                    {"invoice_id": invoice_id, "method": body.method, "amount": paid_amt})
    db.commit()
    return {"payment_id": p.id, "invoice_id": invoice_id, "amount": p.amount,
            "method": p.method, "status": p.status, "invoice_status": inv.status}


@app.get("/cases/{case_id}/payments")
def list_payments(case_id: str, user=Depends(auth.get_current_user), db: Session = Depends(get_db)):
    _get_case(db, case_id, user)
    rows = (db.query(models.Payment).filter_by(case_id=case_id)
            .order_by(models.Payment.paid_at.desc()).all())
    return [{"id": p.id, "invoice_id": p.invoice_id, "amount": p.amount, "method": p.method,
             "reference": p.reference, "status": p.status, "paid_at": str(p.paid_at)} for p in rows]


_INVOICE_STATES = {"draft", "invoice_created", "waiting_payment", "payment_processing",
                   "need_verification", "paid", "rejected", "refunded", "expired"}


@app.patch("/invoices/{invoice_id}/status")
def set_invoice_status(invoice_id: str, body: schemas.InvoiceStatusReq,
                       user=Depends(auth.require_roles("operator")), db: Session = Depends(get_db)):
    """결제 상태 확정/변경 — 관리자(operator/admin) 전용, maker-checker + 감사로그(§9)."""
    if body.status not in _INVOICE_STATES:
        raise HTTPException(422, {"code": "BAD_STATUS", "allowed": sorted(_INVOICE_STATES)})
    inv = db.get(models.Invoice, invoice_id)
    if not inv:
        raise HTTPException(404, {"code": "INVOICE_NOT_FOUND"})
    frm = inv.status
    inv.status = body.status
    if body.status == "paid":   # 확정 시 대기중 결제 confirmed 처리
        pend = db.query(models.Payment).filter_by(invoice_id=invoice_id, status="pending").all()
        for p in pend:
            p.status = "confirmed"
        # 확인된 결제(+)가 하나도 없으면 정산 현금흐름용 Payment 생성(단일소스)
        has_pos = db.query(models.Payment).filter_by(invoice_id=invoice_id, status="confirmed").filter(
            models.Payment.amount > 0).first()
        if not pend and not has_pos:
            db.add(models.Payment(invoice_id=invoice_id, case_id=inv.case_id, amount=(inv.total or 0),
                                  currency="IDR", method="manual", status="confirmed", paid_by=user["uid"]))
    _audit(db, user, "payment.status", "invoice", invoice_id, inv.case_id,
           {"before": frm, "after": body.status, "reason": body.reason}, commit=False)
    db.commit()
    return {"invoice_id": invoice_id, "status": inv.status}


# ── Payment P3: PG Webhook (Midtrans/Xendit류) — signature 검증·idempotency·상태자동반영 ──
_PG_PROVIDERS = {"midtrans", "xendit", "manual"}
_PG_STATUS_MAP = {
    "settlement": "paid", "capture": "paid", "paid": "paid", "success": "paid", "succeeded": "paid",
    "pending": "payment_processing", "authorize": "payment_processing", "processing": "payment_processing",
    "deny": "rejected", "cancel": "rejected", "failure": "rejected", "failed": "rejected",
    "expire": "expired", "expired": "expired",
    "refund": "refunded", "refunded": "refunded", "partial_refund": "refunded",
}


def _pg_secret(provider):
    return (os.environ.get("GLHAC_PG_SECRET_" + provider.upper())
            or os.environ.get("GLHAC_PG_WEBHOOK_SECRET"))


def _verify_pg_sig(provider, raw, headers):
    """서명 검증 — 시크릿 미설정 시 None(dev 미검증), 설정 시 HMAC-SHA256 일치 여부.
    실 PG(Midtrans sha512 등)는 계약 후 provider별 포맷 확장 지점."""
    import hmac as _hmac
    import hashlib as _hl
    secret = _pg_secret(provider)
    if not secret:
        return None
    sig = (headers.get("x-signature") or headers.get("x-callback-token") or "").strip()
    expected = _hmac.new(secret.encode(), raw or b"", _hl.sha256).hexdigest()
    return _hmac.compare_digest(sig, expected)


@app.post("/pg/webhook/{provider}")
async def pg_webhook(provider: str, request: Request, db: Session = Depends(get_db)):
    """PG 결제 콜백 수신 → 서명검증 → idempotency → 인보이스 상태 자동반영(§Payment P3).
    실 PG는 계약·크리덴셜 필요. 시크릿(GLHAC_PG_WEBHOOK_SECRET) 설정 시 서명 필수."""
    import json as _json
    if provider not in _PG_PROVIDERS:
        raise HTTPException(404, {"code": "UNKNOWN_PROVIDER", "allowed": sorted(_PG_PROVIDERS)})
    raw = await request.body()
    headers = {k.lower(): v for k, v in request.headers.items()}
    verified = _verify_pg_sig(provider, raw, headers)
    if verified is False:
        raise HTTPException(401, {"code": "BAD_SIGNATURE"})
    try:
        payload = _json.loads(raw or b"{}")
    except Exception:  # noqa: BLE001
        raise HTTPException(400, {"code": "BAD_PAYLOAD"})
    order_id = (payload.get("order_id") or payload.get("payment_ref")
                or payload.get("reference") or "")
    status_raw = str(payload.get("transaction_status") or payload.get("status") or "").lower()
    event_id = (payload.get("event_id") or payload.get("id")
                or ("%s:%s" % (order_id, status_raw) if order_id else uuid.uuid4().hex))
    idem = "pg:%s:%s" % (provider, event_id)
    dup = db.query(models.IntegrationEvent).filter_by(idempotency_key=idem).first()
    if dup:
        return {"idempotent": True, "status": dup.status, "event_id": dup.id}
    new_status = _PG_STATUS_MAP.get(status_raw)
    inv = db.query(models.Invoice).filter_by(payment_ref=order_id).first() if order_id else None
    applied = None
    if inv and new_status:
        frm = inv.status
        inv.status = new_status
        if new_status == "paid":
            amt = payload.get("gross_amount") or payload.get("amount") or inv.total
            db.add(models.Payment(invoice_id=inv.invoice_id, case_id=inv.case_id, method=provider,
                                  amount=float(amt or 0), currency="IDR", status="confirmed",
                                  reference=str(order_id)))
            for p in db.query(models.Payment).filter_by(invoice_id=inv.invoice_id, status="pending"):
                p.status = "confirmed"
        applied = {"invoice_id": inv.invoice_id, "from": frm, "to": new_status}
    ev = models.IntegrationEvent(
        provider=provider, event_type="pg.webhook." + (status_raw or "unknown"),
        external_id=str(order_id or ""), idempotency_key=idem,
        case_id=inv.case_id if inv else None, payload=payload,
        status="processed" if applied else "received")
    db.add(ev)
    db.commit()
    return {"received": True, "verified": bool(verified), "provider": provider,
            "mapped_status": new_status, "applied": applied, "event_id": ev.id}


@app.get("/admin/payments")
def admin_payments(status: str = None, user=Depends(auth.require_roles("operator")),
                   db: Session = Depends(get_db)):
    """전 조직 청구/결제 목록 — 관리자 결제 대시보드용."""
    q = db.query(models.Invoice)
    if status:
        q = q.filter(models.Invoice.status == status)
    rows = q.order_by(models.Invoice.created_at.desc()).limit(500).all()
    cases = {c.case_id: c for c in db.query(models.CaseApplication)}
    pay = {}
    for p in db.query(models.Payment):
        pay.setdefault(p.invoice_id, []).append(p)
    items = []
    for i in rows:
        c = cases.get(i.case_id)
        st = "waiting_payment" if i.status == "unpaid" else i.status
        items.append({"invoice_id": i.invoice_id, "invoice_no": i.invoice_no,
                      "case_id": i.case_id, "company": c.company_name if c else None,
                      "total": i.total, "status": st, "payment_ref": i.payment_ref,
                      "due_date": str(i.due_date) if i.due_date else None,
                      "payments": len(pay.get(i.invoice_id, []))})
    return {"total": len(items), "items": items}


@app.get("/admin/payments/dashboard")
def admin_payments_dashboard(user=Depends(auth.require_roles("operator")),
                             db: Session = Depends(get_db)):
    invs = db.query(models.Invoice).all()
    by, settle, waiting, needv = {}, 0.0, 0, 0
    for i in invs:
        st = "waiting_payment" if i.status == "unpaid" else i.status
        by[st] = by.get(st, 0) + 1
        if st == "paid":
            settle += (i.total or 0)
        elif st == "waiting_payment":
            waiting += 1
        elif st == "need_verification":
            needv += 1
    return {"by_status": by, "settlement": round(settle, 2), "waiting": waiting,
            "need_verification": needv, "refunded": by.get("refunded", 0),
            "total_invoices": len(invs)}


# ── Payment P5: 환불(2단계 승인)·정산·리포트 ──────────────────────────────
@app.post("/invoices/{invoice_id}/refund/request")
def request_refund(invoice_id: str, body: schemas.RefundReq,
                   user=Depends(auth.require_roles("operator")), db: Session = Depends(get_db)):
    """환불 요청(1단계) — 결제완료 인보이스만. 승인은 다른 관리자가(maker-checker)."""
    inv = db.get(models.Invoice, invoice_id)
    if not inv:
        raise HTTPException(404, {"code": "INVOICE_NOT_FOUND"})
    if inv.status not in ("paid",):
        raise HTTPException(409, {"code": "NOT_REFUNDABLE", "detail": "결제완료(paid) 인보이스만 환불 가능"})
    dup = db.query(models.Refund).filter_by(invoice_id=invoice_id, status="requested").first()
    if dup:
        raise HTTPException(409, {"code": "REFUND_PENDING", "refund_id": dup.id})
    amt = body.amount if body.amount is not None else (inv.total or 0)
    r = models.Refund(invoice_id=invoice_id, case_id=inv.case_id, amount=amt,
                      reason=body.reason, status="requested", requested_by=user["uid"])
    db.add(r); db.flush()
    _audit(db, user, "payment.refund.request", "invoice", invoice_id, inv.case_id,
           {"refund_id": r.id, "amount": amt}, commit=False)
    db.commit()
    return {"refund_id": r.id, "status": r.status, "amount": amt}


@app.post("/refunds/{refund_id}/decide")
def decide_refund(refund_id: str, body: schemas.RefundDecideReq,
                  user=Depends(auth.require_roles("operator")), db: Session = Depends(get_db)):
    """환불 승인/거절(2단계) — 요청자≠승인자(maker-checker). 승인 시 인보이스 refunded."""
    if body.decision not in ("approved", "rejected"):
        raise HTTPException(422, {"code": "BAD_DECISION"})
    r = db.get(models.Refund, refund_id)
    if not r:
        raise HTTPException(404, {"code": "REFUND_NOT_FOUND"})
    if r.status != "requested":
        raise HTTPException(409, {"code": "ALREADY_DECIDED", "status": r.status})
    if r.requested_by == user["uid"] and user["role"] != "admin":
        raise HTTPException(403, {"code": "SELF_APPROVAL_FORBIDDEN",
                                  "detail": "요청자는 승인할 수 없습니다(maker-checker)"})
    r.status = body.decision; r.decided_by = user["uid"]; r.decide_note = body.note
    r.decided_at = datetime.utcnow()
    inv = db.get(models.Invoice, r.invoice_id)
    if body.decision == "approved" and inv:
        inv.status = "refunded"
        db.add(models.Payment(invoice_id=inv.invoice_id, case_id=inv.case_id, amount=-(r.amount or 0),
                              method="refund", reference=refund_id, status="confirmed",
                              paid_by=user["uid"]))
    _audit(db, user, "payment.refund." + body.decision, "invoice", r.invoice_id, r.case_id,
           {"refund_id": refund_id, "amount": r.amount, "requester": r.requested_by}, commit=False)
    db.commit()
    return {"refund_id": refund_id, "status": r.status,
            "invoice_status": inv.status if inv else None}


@app.get("/admin/refunds")
def list_refunds(status: str = None, user=Depends(auth.require_roles("operator")),
                 db: Session = Depends(get_db)):
    q = db.query(models.Refund)
    if status:
        q = q.filter(models.Refund.status == status)
    rows = q.order_by(models.Refund.created_at.desc()).limit(300).all()
    invs = {i.invoice_id: i for i in db.query(models.Invoice).all()}
    cases = {c.case_id: c for c in db.query(models.CaseApplication).all()}
    items = []
    for r in rows:
        inv = invs.get(r.invoice_id); c = cases.get(r.case_id)
        items.append({"id": r.id, "invoice_id": r.invoice_id, "invoice_no": inv.invoice_no if inv else None,
                      "company": c.company_name if c else None, "amount": r.amount,
                      "amount_idr": _idr(r.amount or 0), "reason": r.reason, "status": r.status,
                      "requested_by": r.requested_by, "decided_by": r.decided_by,
                      "decide_note": r.decide_note, "created_at": str(r.created_at)})
    return {"total": len(items), "items": items}


def _settlement(db, org_id=None):
    """정산 집계 — 실제 현금흐름(Payment +결제/-환불) netting (org 선택)."""
    cases = {c.case_id: c for c in db.query(models.CaseApplication).all()}
    invs = db.query(models.Invoice).all()
    if org_id:
        invs = [i for i in invs if cases.get(i.case_id) and cases[i.case_id].org_id == org_id]
    inv_ids = {i.invoice_id for i in invs}
    invoiced = sum(i.total or 0 for i in invs)
    waiting = sum(i.total or 0 for i in invs if i.status in ("waiting_payment", "unpaid",
                                                             "need_verification", "payment_processing"))
    pays = [p for p in db.query(models.Payment).all()
            if p.invoice_id in inv_ids and p.status == "confirmed"]
    gross_paid = sum(p.amount for p in pays if (p.amount or 0) > 0)
    refunded = sum(-(p.amount or 0) for p in pays if (p.amount or 0) < 0)
    net = sum(p.amount or 0 for p in pays)   # +결제 -환불 = 실 순정산
    return {"invoiced": round(invoiced, 2), "paid": round(gross_paid, 2),
            "refunded": round(refunded, 2), "net_settled": round(net, 2),
            "outstanding": round(waiting, 2), "invoice_count": len(invs)}


@app.get("/admin/settlement")
def admin_settlement(org_id: str = None, user=Depends(auth.require_roles("operator")),
                     db: Session = Depends(get_db)):
    """정산 현황 — 청구·결제·환불 netting + IDR 포맷."""
    s = _settlement(db, org_id)
    pend_ref = db.query(models.Refund).filter_by(status="requested").count()
    s.update({"invoiced_idr": _idr(s["invoiced"]), "paid_idr": _idr(s["paid"]),
              "refunded_idr": _idr(s["refunded"]), "net_settled_idr": _idr(s["net_settled"]),
              "outstanding_idr": _idr(s["outstanding"]), "pending_refunds": pend_ref})
    return s


@app.get("/admin/payments/report")
def payments_report(group: str = "month", user=Depends(auth.require_roles("operator")),
                    db: Session = Depends(get_db)):
    """월별/기관별 결제 리포트 — 청구·결제·환불·순정산 집계."""
    cases = {c.case_id: c for c in db.query(models.CaseApplication).all()}
    inv_group = {}   # invoice_id → 그룹키
    rows = {}
    for i in db.query(models.Invoice).all():
        c = cases.get(i.case_id)
        key = (c.org_id if c else "unknown") if group == "org" else (
            i.created_at.strftime("%Y-%m") if i.created_at else "unknown")
        inv_group[i.invoice_id] = key
        b = rows.setdefault(key, {"key": key, "invoiced": 0.0, "paid": 0.0, "refunded": 0.0, "count": 0})
        b["count"] += 1; b["invoiced"] += i.total or 0
    # 실 현금흐름(Payment) 기준 결제·환불 그룹 집계
    for p in db.query(models.Payment).all():
        if p.status != "confirmed":
            continue
        key = inv_group.get(p.invoice_id)
        if key is None or key not in rows:
            continue
        amt = p.amount or 0
        if amt >= 0:
            rows[key]["paid"] += amt
        else:
            rows[key]["refunded"] += -amt
    out = []
    for b in sorted(rows.values(), key=lambda x: x["key"], reverse=(group != "org")):
        b["net"] = round(b["paid"] - b["refunded"], 2)
        for k in ("invoiced", "paid", "refunded"):
            b[k] = round(b[k], 2)
        b["net_idr"] = _idr(b["net"]); b["paid_idr"] = _idr(b["paid"])
        out.append(b)
    return {"group": group, "rows": out, "totals": _settlement(db)}


@app.get("/admin/payments/report.pdf")
def payments_report_pdf(group: str = "month", user=Depends(auth.require_roles("operator")),
                        db: Session = Depends(get_db)):
    """결제 리포트 PDF."""
    from fastapi.responses import Response
    from urllib.parse import quote
    rep = payments_report(group=group, user=user, db=db)
    t = rep["totals"]
    L = ["[정산 총계]",
         "청구 %s · 결제 %s · 환불 %s" % (_idr(t["invoiced"]), _idr(t["paid"]), _idr(t["refunded"])),
         "순정산 %s · 미수 %s · 청구건수 %d" % (_idr(t["net_settled"]), _idr(t["outstanding"]),
                                            t["invoice_count"]), "",
         "[%s별 집계]" % ("기관" if group == "org" else "월")]
    for b in rep["rows"]:
        L.append("• %s — 청구 %d건 %s · 결제 %s · 환불 %s · 순 %s"
                 % (b["key"], b["count"], _idr(b["invoiced"]), _idr(b["paid"]),
                    _idr(b["refunded"]), _idr(b["net"])))
    L.append("")
    L.append("※ 준비용 정산 리포트 · GL-HAC AI")
    pdf = _render_pdf("결제·정산 리포트 · Payment Report",
                      "\n".join(L), subtitle="집계: %s별" % ("기관" if group == "org" else "월"),
                      footer="GL-HAC AI")
    _audit(db, user, "payment.report.pdf", "report", group, None)
    db.commit()
    return Response(content=pdf, media_type="application/pdf",
                    headers={"Content-Disposition": "attachment; filename*=UTF-8''"
                             + quote("payment_report_%s.pdf" % group)})


# ── Payment P2: 입금 자동매칭 ──────────────────────────────
def _name_sim(a, b):
    a = set((a or "").lower().split()); b = set((b or "").lower().split())
    return (len(a & b) / len(a | b)) if (a and b) else 0.0


def _match_deposit(db, dep):
    """입금 → 미결제/검증대기 인보이스 후보 스코어링."""
    invs = db.query(models.Invoice).filter(models.Invoice.status.in_(
        ["waiting_payment", "unpaid", "need_verification", "payment_processing"])).all()
    cases = {c.case_id: c for c in db.query(models.CaseApplication).all()}
    memo = (dep.ref_memo or "").lower()
    out = []
    for inv in invs:
        score = 0.0; reason = []; risk = []
        ref_hit = bool(inv.payment_ref and inv.payment_ref.lower() in memo) or \
                  bool(inv.invoice_no and inv.invoice_no.lower() in memo)
        if ref_hit:
            score += 1.0; reason.append("결제참조 일치")
        amt = dep.amount or 0; tot = inv.total or 0
        amount_ok = abs(amt - tot) < 0.01
        if amount_ok:
            score += 1.0; reason.append("금액 일치")
        elif amt < tot:
            risk.append("부분입금")
        else:
            risk.append("초과입금")
        c = cases.get(inv.case_id)
        if _name_sim(dep.depositor_name, c.company_name if c else "") >= 0.5:
            score += 0.3; reason.append("입금자명 유사")
        if score <= 0:
            continue
        out.append({"invoice": inv, "score": round(score, 2), "reason": reason,
                    "risk": risk, "ref_hit": ref_hit, "amount_ok": amount_ok})
    out.sort(key=lambda x: -x["score"])
    return out


def _judge_match(cands):
    if not cands:
        return "REJECT"
    top = cands[0]
    dup = sum(1 for c in cands if abs(c["score"] - top["score"]) < 0.01) >= 2
    if top["ref_hit"] and top["amount_ok"] and not dup and not top["risk"]:
        return "COMMIT"
    return "DEFER"


@app.post("/admin/deposits")
def add_deposit(body: schemas.DepositReq, user=Depends(auth.require_roles("operator")),
                db: Session = Depends(get_db)):
    """입금내역 등록 → 자동매칭(COMMIT=자동확정 / DEFER=후보생성 / REJECT=미매칭)."""
    acct = body.account_no or ""
    masked = ("****" + acct[-4:]) if len(acct) >= 4 else "****"
    dep = models.Deposit(bank_name=body.bank_name, account_no_masked=masked,
                         depositor_name=body.depositor_name, amount=body.amount,
                         ref_memo=body.ref_memo)
    db.add(dep); db.flush()
    _audit(db, user, "payment.deposit.create", "deposit", dep.id, None,
           {"amount": body.amount, "bank": body.bank_name}, commit=False)
    cands = _match_deposit(db, dep)
    verdict = _judge_match(cands)
    res = {"deposit_id": dep.id, "verdict": verdict, "candidates": len(cands)}
    if verdict == "COMMIT":
        inv = cands[0]["invoice"]
        inv.status = "paid"
        dep.matched_invoice_id = inv.invoice_id; dep.match_status = "matched"
        db.add(models.Payment(invoice_id=inv.invoice_id, case_id=inv.case_id, amount=dep.amount,
                              method="bank_transfer", reference=dep.ref_memo,
                              status="confirmed", paid_by=user["uid"]))
        _audit(db, user, "payment.match.auto", "invoice", inv.invoice_id, inv.case_id,
               {"deposit_id": dep.id, "score": cands[0]["score"], "auto": True}, commit=False)
        res["matched_invoice"] = inv.invoice_no
    else:
        dep.match_status = "candidate" if cands else "unmatched"
        for c in cands[:5]:
            db.add(models.PaymentMatchCandidate(deposit_id=dep.id, invoice_id=c["invoice"].invoice_id,
                   case_id=c["invoice"].case_id, score=c["score"], reason=c["reason"], risk_flags=c["risk"]))
    db.commit()
    return res


@app.get("/admin/deposits")
def list_deposits(user=Depends(auth.require_roles("operator")), db: Session = Depends(get_db)):
    rows = db.query(models.Deposit).order_by(models.Deposit.created_at.desc()).limit(200).all()
    return {"total": len(rows), "items": [{"id": d.id, "bank_name": d.bank_name,
            "account": d.account_no_masked, "depositor_name": d.depositor_name, "amount": d.amount,
            "ref_memo": d.ref_memo, "match_status": d.match_status,
            "matched_invoice_id": d.matched_invoice_id, "deposit_at": str(d.deposit_at)} for d in rows]}


@app.get("/admin/payments/match-candidates")
def list_match_candidates(user=Depends(auth.require_roles("operator")), db: Session = Depends(get_db)):
    rows = (db.query(models.PaymentMatchCandidate).filter_by(decision_status="pending")
            .order_by(models.PaymentMatchCandidate.score.desc()).all())
    deps = {d.id: d for d in db.query(models.Deposit).all()}
    invs = {i.invoice_id: i for i in db.query(models.Invoice).all()}
    cases = {c.case_id: c for c in db.query(models.CaseApplication).all()}
    items = []
    for r in rows:
        d = deps.get(r.deposit_id); inv = invs.get(r.invoice_id); c = cases.get(r.case_id)
        items.append({"id": r.id, "score": r.score, "reason": r.reason, "risk_flags": r.risk_flags,
                      "deposit_id": r.deposit_id, "depositor": d.depositor_name if d else None,
                      "deposit_amount": d.amount if d else None, "ref_memo": d.ref_memo if d else None,
                      "invoice_id": r.invoice_id, "invoice_no": inv.invoice_no if inv else None,
                      "invoice_total": inv.total if inv else None, "company": c.company_name if c else None})
    return {"total": len(items), "items": items}


@app.post("/admin/match/{candidate_id}/decide")
def decide_match(candidate_id: str, body: schemas.MatchDecisionReq,
                 user=Depends(auth.require_roles("operator")), db: Session = Depends(get_db)):
    """매칭 후보 승인/보류/거절 — 승인 시 인보이스 paid + 입금 matched (maker-checker + 감사)."""
    if body.decision not in ("approved", "held", "rejected"):
        raise HTTPException(422, {"code": "BAD_DECISION"})
    cand = db.get(models.PaymentMatchCandidate, candidate_id)
    if not cand:
        raise HTTPException(404, {"code": "CANDIDATE_NOT_FOUND"})
    cand.decision_status = body.decision; cand.reviewer_id = user["uid"]; cand.reviewed_at = datetime.utcnow()
    frm = None
    if body.decision == "approved":
        inv = db.get(models.Invoice, cand.invoice_id); dep = db.get(models.Deposit, cand.deposit_id)
        if inv:
            frm = inv.status; inv.status = "paid"
            db.add(models.Payment(invoice_id=inv.invoice_id, case_id=inv.case_id,
                   amount=(dep.amount if dep else inv.total), method="bank_transfer",
                   reference=(dep.ref_memo if dep else None), status="confirmed", paid_by=user["uid"]))
        if dep:
            dep.matched_invoice_id = cand.invoice_id; dep.match_status = "matched"
        for other in db.query(models.PaymentMatchCandidate).filter_by(
                deposit_id=cand.deposit_id, decision_status="pending"):
            if other.id != cand.id:
                other.decision_status = "held"
    _audit(db, user, "payment.match.decide", "invoice", cand.invoice_id, cand.case_id,
           {"decision": body.decision, "candidate": candidate_id, "before": frm}, commit=False)
    db.commit()
    return {"candidate_id": candidate_id, "decision": body.decision}


# ---------- 대시보드 분석 (§11.3 analytics) ----------
@app.get("/analytics/summary")
def analytics_summary(user=Depends(auth.require_roles("operator")), db: Session = Depends(get_db)):
    """운영 분석 — 상태/경로/월별 추이·인증서·매출 집계(operator/admin)."""
    q = db.query(models.CaseApplication)
    if user["role"] != "admin":
        q = q.filter_by(org_id=user["org_id"])
    cases = q.all()
    by_status, by_pathway, by_month = {}, {}, {}
    for c in cases:
        by_status[c.status] = by_status.get(c.status, 0) + 1
        by_pathway[c.pathway or "undetermined"] = by_pathway.get(c.pathway or "undetermined", 0) + 1
        if c.created_at:
            mk = c.created_at.strftime("%Y-%m")
            by_month[mk] = by_month.get(mk, 0) + 1
    case_ids = [c.case_id for c in cases]
    certs = (db.query(models.HalalCertificate)
             .filter(models.HalalCertificate.case_id.in_(case_ids)).all() if case_ids else [])
    certs_by_month = {}
    for ct in certs:
        try:
            mk = ct.issue_date[:7]
            certs_by_month[mk] = certs_by_month.get(mk, 0) + 1
        except Exception:  # noqa: BLE001
            pass
    invs = (db.query(models.Invoice)
            .filter(models.Invoice.case_id.in_(case_ids)).all() if case_ids else [])
    revenue_paid = round(sum(i.total or 0 for i in invs if i.status == "paid"), 2)
    revenue_outstanding = round(sum(i.total or 0 for i in invs if i.status == "unpaid"), 2)
    return {"cases_total": len(cases), "cases_by_status": by_status, "cases_by_pathway": by_pathway,
            "cases_by_month": by_month, "certificates_active": len([x for x in certs if x.status == "active"]),
            "certs_by_month": certs_by_month, "revenue_paid": revenue_paid,
            "revenue_outstanding": revenue_outstanding}


@app.get("/admin/orgs/{org_id}/overview")
def admin_org_overview(org_id: str, user=Depends(auth.require_roles()), db: Session = Depends(get_db)):
    """멀티테넌트 admin — 조직별 개요(admin 전용)."""
    cases = db.query(models.CaseApplication).filter_by(org_id=org_id).all()
    case_ids = [c.case_id for c in cases]
    by_status = {}
    for c in cases:
        by_status[c.status] = by_status.get(c.status, 0) + 1
    users = db.query(models.User).filter_by(org_id=org_id).count()
    certs = (db.query(models.HalalCertificate)
             .filter(models.HalalCertificate.case_id.in_(case_ids), models.HalalCertificate.status == "active").count()
             if case_ids else 0)
    invs = (db.query(models.Invoice).filter(models.Invoice.case_id.in_(case_ids)).all() if case_ids else [])
    return {"org_id": org_id, "cases": len(cases), "users": users, "certificates": certs,
            "by_status": by_status,
            "revenue_paid": round(sum(i.total or 0 for i in invs if i.status == "paid"), 2)}


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


_VERDICT_KO = {"CLEARED": "할랄 허용", "NEEDS_EVIDENCE": "증빙 필요", "BLOCK": "차단(하람)"}


def _material_report(db, c):
    """케이스 원재료 전수를 온톨로지로 분석해 종합 보고서 데이터로 집계 (설계 C·v2 이관)."""
    mats = db.query(models.Material).filter_by(case_id=c.case_id).order_by(models.Material.name).all()
    rows, summary = [], {"total": 0, "cleared": 0, "needs_evidence": 0, "blocked": 0,
                         "najis": 0, "critical": []}
    for m in mats:
        exp = screening.explain(m.name, e_number=m.e_number, source=m.source,
                                cert_no=m.cert_no, note=m.note or "")
        v = exp.get("verdict")
        summary["total"] += 1
        if v == "BLOCK":
            summary["blocked"] += 1
            summary["critical"].append(m.name)
        elif v == "NEEDS_EVIDENCE":
            summary["needs_evidence"] += 1
            summary["critical"].append(m.name)
        else:
            summary["cleared"] += 1
        if exp.get("najis"):
            summary["najis"] += 1
        rows.append({"material_id": m.material_id, "name": m.name, "e_number": m.e_number,
                     "verdict": v, "verdict_ko": _VERDICT_KO.get(v, v), "severity": exp.get("severity"),
                     "category": exp.get("category"), "najis": exp.get("najis"),
                     "required_evidence": exp.get("required_evidence") or [],
                     "alternatives": exp.get("alternatives") or [],
                     "evidence_count": m.evidence_count if hasattr(m, "evidence_count") else None,
                     "explanation": exp.get("explanation") or ""})
    return {"case_id": c.case_id, "company_name": c.company_name,
            "summary": summary, "materials": rows}


@app.get("/cases/{case_id}/material-report")
def material_report(case_id: str, user=Depends(auth.get_current_user), db: Session = Depends(get_db)):
    """성분 AI 분석 보고서 — 원재료 전수 온톨로지 판정·근거·대체재·증빙 집계."""
    c = _get_case(db, case_id, user)
    rep = _material_report(db, c)
    _audit(db, user, "material_report.view", "case", case_id)
    db.commit()
    return rep


@app.get("/cases/{case_id}/material-report.pdf")
def material_report_pdf(case_id: str, user=Depends(auth.get_current_user), db: Session = Depends(get_db)):
    """성분 AI 분석 보고서 PDF."""
    from fastapi.responses import Response
    from urllib.parse import quote
    c = _get_case(db, case_id, user)
    rep = _material_report(db, c)
    s = rep["summary"]
    L = ["[요약]",
         "총 원재료: %d건" % s["total"],
         "할랄 허용: %d · 증빙 필요: %d · 차단(하람): %d · najis 위험: %d"
         % (s["cleared"], s["needs_evidence"], s["blocked"], s["najis"]),
         "주의 성분: %s" % (", ".join(s["critical"]) or "없음"), "",
         "[성분별 분석]"]
    for r in rep["materials"]:
        head = "• %s%s — %s" % (r["name"], (" (%s)" % r["e_number"]) if r["e_number"] else "",
                                r["verdict_ko"])
        if r["severity"]:
            head += " · 심각도 %s" % r["severity"]
        if r["najis"]:
            head += " · najis"
        L.append(head)
        if r["explanation"]:
            L.append("  " + r["explanation"].replace("\n", "\n  "))
        if r["required_evidence"]:
            L.append("  필요 증빙: " + ", ".join(r["required_evidence"]))
        if r["alternatives"]:
            L.append("  할랄 대체재: " + ", ".join(r["alternatives"]))
        L.append("")
    L.append("※ 온톨로지 기반 준비용 분석. 공식 판정은 BPJPH/MUI Fatwa로 확정.")
    pdf = _render_pdf("성분 AI 분석 보고서 · Material Analysis",
                      "\n".join(L),
                      subtitle="%s · 총 %d건" % (c.company_name or "", s["total"]),
                      footer="GL-HAC AI")
    fn = "material_report_%s.pdf" % (c.company_name or case_id)
    _audit(db, user, "material_report.pdf", "case", case_id)
    db.commit()
    return Response(content=pdf, media_type="application/pdf",
                    headers={"Content-Disposition": "attachment; filename*=UTF-8''" + quote(fn)})


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
    _audit(db, user, "ai.extraction.read", "ai_extraction", case_id, case_id)
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
    raw = base64.b64decode(_validate_upload(body.image_b64, body.filename or "label.png"))  # 검증(§9.3)
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
    obs.inc("glhac_state_transition_total", {"to": body.to_state})
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
                    user=Depends(rbac.require_action("penyelia.update")),
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
                      user=Depends(rbac.require_action("pendamping.verify")), db: Session = Depends(get_db)):
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


# ---------- 솔루션 개선 피드백 게시판(전역) ----------
FEEDBACK_ADMIN_ROLES = ("admin", "operator")   # ITO 관리자(추후 확장 가능)


def _fb_is_admin(user):
    return user["role"] in FEEDBACK_ADMIN_ROLES


def _fb_guard(db, fid, user):
    fb = db.get(models.Feedback, fid)
    if not fb:
        raise HTTPException(404, {"code": "FEEDBACK_NOT_FOUND"})
    if not _fb_is_admin(user) and fb.author != user["username"]:
        raise HTTPException(403, {"code": "NOT_AUTHORIZED"})
    return fb


@app.post("/feedback")
def create_feedback(body: schemas.FeedbackReq, user=Depends(auth.get_current_user),
                    db: Session = Depends(get_db)):
    """피드백 제출 — 로그인 전원."""
    if not (body.title or "").strip():
        raise HTTPException(422, {"code": "TITLE_REQUIRED"})
    fb = models.Feedback(author=user["username"], author_role=user["role"],
                         org_id=user.get("org_id"), category=body.category or "improvement",
                         title=body.title.strip(), body=body.body or "", status="open")
    db.add(fb); db.commit()
    return {"feedback_id": fb.feedback_id, "status": fb.status}


@app.post("/feedback/{feedback_id}/images")
def add_feedback_image(feedback_id: str, body: schemas.FeedbackImageReq,
                       user=Depends(auth.get_current_user), db: Session = Depends(get_db)):
    """이미지 첨부 — 본인 또는 admin. base64 <4MB."""
    from .intake import _ctype
    _fb_guard(db, feedback_id, user)
    b64 = (body.file_b64 or "").split(",")[-1]
    img = models.FeedbackImage(feedback_id=feedback_id, filename=body.filename or "feedback.png",
                               content_b64=b64 if len(b64) < 4_000_000 else None,
                               content_type=_ctype(body.filename or "feedback.png"))
    db.add(img); db.commit()
    return {"image_id": img.image_id, "stored": bool(img.content_b64)}


@app.get("/feedback")
def list_feedback(status: str = None, user=Depends(auth.get_current_user),
                  db: Session = Depends(get_db)):
    """목록 — admin/ops=전체, 그외=본인글."""
    q = db.query(models.Feedback)
    if not _fb_is_admin(user):
        q = q.filter(models.Feedback.author == user["username"])
    if status:
        q = q.filter(models.Feedback.status == status)
    rows = q.order_by(models.Feedback.created_at.desc()).limit(300).all()
    imgc, cmtc = {}, {}
    for iid, cnt in (db.query(models.FeedbackImage.feedback_id, func.count(models.FeedbackImage.image_id))
                     .group_by(models.FeedbackImage.feedback_id).all()):
        imgc[iid] = cnt
    for iid, cnt in (db.query(models.FeedbackComment.feedback_id, func.count(models.FeedbackComment.comment_id))
                     .group_by(models.FeedbackComment.feedback_id).all()):
        cmtc[iid] = cnt
    return {"total": len(rows), "is_admin": _fb_is_admin(user),
            "items": [{"feedback_id": f.feedback_id, "title": f.title, "category": f.category,
                       "status": f.status, "author": f.author, "author_role": f.author_role,
                       "body": f.body, "created_at": str(f.created_at), "updated_at": str(f.updated_at),
                       "image_count": imgc.get(f.feedback_id, 0),
                       "comment_count": cmtc.get(f.feedback_id, 0)} for f in rows]}


@app.get("/feedback/{feedback_id}")
def get_feedback(feedback_id: str, user=Depends(auth.get_current_user),
                 db: Session = Depends(get_db)):
    """상세 — admin 또는 본인. 이미지 메타 + 코멘트 스레드."""
    fb = _fb_guard(db, feedback_id, user)
    imgs = db.query(models.FeedbackImage).filter_by(feedback_id=feedback_id).all()
    cmts = (db.query(models.FeedbackComment).filter_by(feedback_id=feedback_id)
            .order_by(models.FeedbackComment.created_at).all())
    return {"feedback_id": fb.feedback_id, "title": fb.title, "category": fb.category,
            "status": fb.status, "author": fb.author, "author_role": fb.author_role,
            "body": fb.body, "created_at": str(fb.created_at), "updated_at": str(fb.updated_at),
            "images": [{"image_id": i.image_id, "filename": i.filename,
                        "content_type": i.content_type, "has_file": bool(i.content_b64)} for i in imgs],
            "comments": [{"comment_id": c.comment_id, "author": c.author, "author_role": c.author_role,
                          "body": c.body, "created_at": str(c.created_at)} for c in cmts]}


@app.get("/feedback/{feedback_id}/image/{image_id}")
def get_feedback_image(feedback_id: str, image_id: str, user=Depends(auth.get_current_user),
                       db: Session = Depends(get_db)):
    """이미지 바이너리 — admin 또는 본인."""
    from fastapi.responses import Response
    _fb_guard(db, feedback_id, user)
    img = db.get(models.FeedbackImage, image_id)
    if not img or img.feedback_id != feedback_id or not img.content_b64:
        raise HTTPException(404, {"code": "IMAGE_NOT_FOUND"})
    return Response(content=base64.b64decode(img.content_b64),
                    media_type=img.content_type or "image/png")


@app.patch("/feedback/{feedback_id}")
def set_feedback_status(feedback_id: str, body: schemas.FeedbackStatusReq,
                        user=Depends(auth.get_current_user), db: Session = Depends(get_db)):
    """상태 변경 — admin/ops만."""
    if not _fb_is_admin(user):
        raise HTTPException(403, {"code": "NOT_AUTHORIZED"})
    if body.status not in ("open", "reviewing", "resolved", "wontfix"):
        raise HTTPException(422, {"code": "BAD_STATUS"})
    fb = db.get(models.Feedback, feedback_id)
    if not fb:
        raise HTTPException(404, {"code": "FEEDBACK_NOT_FOUND"})
    fb.status = body.status; fb.updated_at = datetime.utcnow()
    db.commit()
    return {"feedback_id": feedback_id, "status": fb.status}


@app.post("/feedback/{feedback_id}/comments")
def add_feedback_comment(feedback_id: str, body: schemas.FeedbackCommentReq,
                         user=Depends(auth.get_current_user), db: Session = Depends(get_db)):
    """관리자 답변 — admin/ops만."""
    if not _fb_is_admin(user):
        raise HTTPException(403, {"code": "NOT_AUTHORIZED"})
    if not (body.body or "").strip():
        raise HTTPException(422, {"code": "BODY_REQUIRED"})
    fb = db.get(models.Feedback, feedback_id)
    if not fb:
        raise HTTPException(404, {"code": "FEEDBACK_NOT_FOUND"})
    cm = models.FeedbackComment(feedback_id=feedback_id, author=user["username"],
                                author_role=user["role"], body=body.body.strip())
    db.add(cm); fb.updated_at = datetime.utcnow(); db.commit()
    return {"comment_id": cm.comment_id}


# ---------- 정적 UI (M2) ----------
class NoCacheStaticFiles(StaticFiles):
    """UI 정적파일을 항상 재검증(no-cache) — 배포 후 하드리프레시 없이 최신 반영."""
    async def get_response(self, path, scope):
        resp = await super().get_response(path, scope)
        resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        resp.headers["Pragma"] = "no-cache"
        resp.headers["Expires"] = "0"
        return resp



@app.get("/", include_in_schema=False)
def _root_redirect():
    """맨 URL 접속 시 UI로 이동 (루트 라우트 부재로 인한 404 방지)."""
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url="/ui/")


# ===== 공장·시설 (회사1:공장N) — Phase 3 =====
@app.get("/orgs/{org_id}/facilities")
def list_facilities(org_id: str, user=Depends(auth.get_current_user), db: Session = Depends(get_db)):
    rows = db.query(models.Facility).filter_by(org_id=org_id).order_by(models.Facility.created_at).all()
    return [{"facility_id": f.facility_id, "name": f.name, "address": f.address, "city": f.city,
             "country": f.country, "zip": f.zip, "reg_no": f.reg_no,
             "created_at": str(f.created_at)} for f in rows]


@app.post("/orgs/{org_id}/facilities")
def add_facility(org_id: str, body: schemas.FacilityReq,
                 user=Depends(auth.get_current_user), db: Session = Depends(get_db)):
    if not (body.name or "").strip():
        raise HTTPException(422, {"code": "NAME_REQUIRED"})
    f = models.Facility(org_id=org_id, name=body.name.strip(), address=body.address, city=body.city,
                        country=body.country, zip=body.zip, reg_no=body.reg_no)
    db.add(f)
    db.commit()
    return {"facility_id": f.facility_id, "name": f.name}


@app.delete("/facilities/{facility_id}")
def del_facility(facility_id: str, user=Depends(auth.get_current_user), db: Session = Depends(get_db)):
    f = db.get(models.Facility, facility_id)
    if not f:
        raise HTTPException(404, {"code": "FACILITY_NOT_FOUND"})
    db.delete(f)
    db.commit()
    return {"deleted": facility_id}


_static = os.path.join(os.path.dirname(__file__), "static")
app.mount("/ui", NoCacheStaticFiles(directory=_static, html=True), name="ui")