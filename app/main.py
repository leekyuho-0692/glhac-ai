"""GL-HAC AI API — M1(dual-pathway) + M3(OCR/판정) + M2(인증·UI). 설계 24.14 / B.4."""
import io
import hashlib
import os
import secrets
import re
import time
import base64
import logging
from datetime import datetime, date, timedelta
from fastapi.exceptions import RequestValidationError
from fastapi import FastAPI, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware

logging.basicConfig(
    level=os.environ.get("GLHAC_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s :: %(message)s")
log = logging.getLogger("glhac")
from sqlalchemy import func, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified
from .db import Base, engine, get_db, SessionLocal
from . import models, schemas, state_machine as sm, screening, ai_local, auth, rbac
from . import observability as obs
from . import domain_dict as _dd_mod
from .ontology_seed import seed

app = FastAPI(title="GL-HAC AI Dual-Pathway API", version="0.2.0")

# GZip 압축 — 451KB index.html 등 정적/JSON 응답 전송 최적화(1KB 이상만 압축).
app.add_middleware(GZipMiddleware, minimum_size=1000)

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


# ── 법적효력 Phase 4b — 민감/쓰기 엔드포인트 레이트리밋 (per-actor 슬라이딩 카운터·인메모리) ──────────
# 로그인 전용 제한(auth._LOGIN_ATTEMPTS)은 그대로 유지하고, write(POST/PUT/PATCH/DELETE) 요청에
# per-actor(토큰 uid 우선, 없으면 클라이언트 IP) 분당 완만한 한도를 추가한다 — 정상 데모 흐름 무영향
# (기본 넉넉). 초과 시 429 RATE_LIMITED. 한도는 GLHAC_RATE_LIMIT_PER_MIN(기본 120)으로 조정.
# ⚠ 단일 프로세스 인메모리 카운터 — 멀티워커/수평확장 시 외부 store(Redis 등) 필요(정직 표기).
_RL_WRITE_WINDOW = 60
_RL_WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
_RL_WRITE_HITS = {}   # actor_key -> [timestamps] (인메모리·단일프로세스)


def _rl_write_max() -> int:
    """레이트리밋 분당 한도 — 요청 시점에 env(GLHAC_RATE_LIMIT_PER_MIN)를 재조회한다.
    import-time 캐싱을 제거해 같은 프로세스 내에서 한도가 바뀌어도(테스트 하네스 등) 즉시 반영
    → 조합 실행 시 한 파일의 낮은 한도가 다른 파일 쓰기를 429로 오염시키던 비멱등 제거.
    기본 120. 0이면 비활성(레이트리밋 미적용). 429 동작·표기는 불변."""
    try:
        return int(os.environ.get("GLHAC_RATE_LIMIT_PER_MIN", "120"))
    except (TypeError, ValueError):
        return 120


def _rl_actor_key(request: Request) -> str:
    """레이트리밋 액터 식별 — 인증 토큰의 uid 우선, 없으면 클라이언트 IP."""
    authz = request.headers.get("Authorization") or ""
    if authz.startswith("Bearer "):
        p = auth.verify_token(authz[7:])
        if p and p.get("uid"):
            return "uid:" + str(p["uid"])
    client = request.client
    return "ip:" + (client.host if client else "unknown")


@app.middleware("http")
async def _rate_limit_write_mw(request: Request, call_next):
    """P4b — write 요청 per-actor 분당 레이트리밋. 로그인(/auth/*)은 전용 제한(auth._LOGIN_ATTEMPTS)
    으로 별도 관리하므로 여기서는 제외(중복 방지). GET/HEAD/OPTIONS는 미적용."""
    _max = _rl_write_max()   # 요청 시점 env 재조회(캐싱 제거)
    if (_max > 0 and request.method in _RL_WRITE_METHODS
            and not request.url.path.startswith("/auth/")):
        now = time.time()
        key = _rl_actor_key(request)
        arr = [t for t in _RL_WRITE_HITS.get(key, []) if now - t < _RL_WRITE_WINDOW]
        if len(arr) >= _max:
            _RL_WRITE_HITS[key] = arr
            return JSONResponse(status_code=429,
                                content={"code": "RATE_LIMITED",
                                         "detail": "분당 요청 한도 초과 · rate limit exceeded",
                                         "limit_per_min": _max})
        arr.append(now)
        _RL_WRITE_HITS[key] = arr
    return await call_next(request)


# ── 법적효력 Phase 4a — 보안 응답 헤더 미들웨어 (플랫폼 컴플라이언스·순수 추가) ─────────────────────
# 완성도평가 HIGH 지적 반영: clickjacking·MIME 스니핑·정보노출 방어 + CSP. 단일 vanilla JS SPA가
# 인라인 스크립트/스타일·QR(data:)·이미지(blob:)를 서빙하므로 앱을 깨지 않는 합리적 정책을 기본값으로.
# 값은 모두 env로 완화 가능(GLHAC_CSP 등). HSTS는 HTTPS 요청에만 부여 → 로컬 http 개발 무손상.
# (마지막 등록 미들웨어 = 최외곽 → 모든 응답(429 포함)에 헤더 부여.)
_DEFAULT_CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline' https://t1.daumcdn.net; "
                "style-src 'self' 'unsafe-inline'; img-src 'self' data: blob: https://*.daumcdn.net https://*.daum.net; "
                # PDF 미리보기(영수증·견적서 등)는 blob: URL을 <iframe>/<embed>로 렌더 →
                # frame-src/object-src에 blob: 없으면 Chrome이 CSP로 차단("콘텐츠가 차단됨").
                # Daum 우편번호 위젯: 스크립트(t1.daumcdn.net)·검색 iframe(postcode.map.daum.net) 허용.
                "frame-src 'self' blob: https://postcode.map.daum.net; object-src 'self' blob:; "
                "connect-src 'self' https://t1.daumcdn.net; frame-ancestors 'none'")
_SECURITY_HEADERS = {
    "X-Frame-Options": os.environ.get("GLHAC_X_FRAME_OPTIONS", "DENY"),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": os.environ.get("GLHAC_REFERRER_POLICY", "strict-origin-when-cross-origin"),
    "Content-Security-Policy": os.environ.get("GLHAC_CSP", _DEFAULT_CSP),
}
_HSTS_VALUE = os.environ.get("GLHAC_HSTS", "max-age=31536000; includeSubDomains")
_SECURITY_HEADERS_ACTIVE = True   # P4c 컴플라이언스 대시보드가 참조(미들웨어 등록됨=met)


@app.middleware("http")
async def _security_headers_mw(request: Request, call_next):
    """보안 응답 헤더(P4a) — 모든 응답에 부여. 값은 env로 완화 가능. HSTS는 HTTPS 요청
    (url.scheme=='https' 또는 X-Forwarded-Proto=='https')에만 → 로컬 http 개발 무손상."""
    resp = await call_next(request)
    for k, v in _SECURITY_HEADERS.items():
        if v:
            resp.headers.setdefault(k, v)
    xfproto = request.headers.get("X-Forwarded-Proto", "")
    is_https = request.url.scheme == "https" or xfproto == "https"
    if is_https and _HSTS_VALUE:
        resp.headers.setdefault("Strict-Transport-Security", _HSTS_VALUE)
    return resp


@app.exception_handler(RequestValidationError)
async def _validation_exc(request: Request, exc: RequestValidationError):
    """입력 검증 실패 — 앱 규약({"detail": {"code": ...}})으로 맞춰 화면이 읽게 한다.

    FastAPI 기본 422 는 detail 이 **배열**이라, {code} 만 보는 화면 코드에서는 사유가
    빈칸으로 떴다. 잘못 적었다는 사실은 아는데 무엇이 잘못됐는지 못 보는 상태가 된다.
    사람이 고칠 수 있게 첫 오류의 필드와 문구를 함께 낸다."""
    errs = exc.errors()
    first = errs[0] if errs else {}
    loc = [str(x) for x in (first.get("loc") or []) if x != "body"]
    msg = str(first.get("msg") or "")
    msg = msg[len("Value error, "):] if msg.startswith("Value error, ") else msg
    return JSONResponse(status_code=422, content={"detail": {
        "code": "VALIDATION_ERROR",
        "field": ".".join(loc) or None,
        "message": msg,
        "errors": [{"field": ".".join(str(x) for x in (e.get("loc") or []) if x != "body"),
                    "message": str(e.get("msg") or "")} for e in errs[:8]],
    }})


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
                     "xlsx", "xls", "docx", "doc", "csv", "txt", "hwp",
                     "mp4", "mov", "webm", "m4v"}   # 모의감사 생산공정 영상(§P0-5 클라이언트 뷰)


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


def _seed_auditor_profiles(db):
    """P3: 오디터 프로필(전문분야·언어·캐파) 시드 — 프로필 이벤트가 없는 오디터만 1회 적재.
    운영자가 ✎로 편집한 프로필(latest-wins)은 절대 덮어쓰지 않는다."""
    for username, prof in auth.DEFAULT_AUDITOR_PROFILES.items():
        u = db.query(models.User).filter_by(username=username).first()
        if not u:
            continue
        has = (db.query(models.WorkflowEvent)
               .filter(models.WorkflowEvent.case_id == u.user_id,
                       models.WorkflowEvent.action == AUDITOR_PROFILE_ACTION).count())
        if has:
            continue
        sm.record_event(db, _auditor_shim(u.user_id), None, None, AUDITOR_PROFILE_ACTION,
                        "system", "seed", dict(prof))
    db.commit()


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
        seed_menus(db)   # 동적 메뉴 시드(idempotent) — 설계서 §10
        if os.environ.get("GLHAC_DEV") == "1":
            _seed_auditor_profiles(db)   # 데모 시드 계정에만 프로필 부여
        # load_ontology는 ORM 인스턴스를 모듈 캐시에 담으므로 반드시 마지막 —
        # 이후 commit()이 나면 캐시 객체가 expire되어 DetachedInstanceError가 난다.
        screening.load_ontology(db)
    finally:
        db.close()
    _autoparse_start()
    if os.environ.get("GLHAC_NOTIFY_WORKER") == "1":
        import threading
        threading.Thread(target=_notify_worker_loop, daemon=True).start()


# 인증기관 측 역할 — 회사가 아니라 인증기관에 속한다. 조직 경계로 막으면 안 된다.
# (실측: 신규 가입 업체는 자기 org 를 갖는데 샤리아·최고운영자가 org_demo 라 ORG_FORBIDDEN
#  으로 막혀, 가입한 업체가 인증서까지 갈 수 없었다.)
CERTIFIER_ROLES = {"admin", "operator", "fatwa_liaison"}
# 배정받은 케이스만 보는 역할 — 남의 회사 서류를 전부 열어볼 이유는 없다.
ASSIGNED_ROLES = {"auditor", "consultant"}
CONSULTANT_ASSIGN_ACTION = "ops.consultant_assigned"


def _assigned_staff(db, case_id):
    """이 케이스에 배정된 심사자 uid 집합(오디터·컨설턴트). latest-wins 가 아니라
    '배정된 적이 있으면 접근 가능'으로 본다 — 교체돼도 이전 담당자의 조회 이력은 남는다."""
    out = set()
    for e in (db.query(models.WorkflowEvent)
              .filter(models.WorkflowEvent.case_id == case_id,
                      models.WorkflowEvent.action.in_(("ops.auditor_assigned",
                                                       CONSULTANT_ASSIGN_ACTION))).all()):
        p = e.payload or {}
        for k in ("auditor_id", "consultant_id", "user_id", "uid"):
            if p.get(k):
                out.add(str(p[k]))
    return out


def _is_my_client_org(db, user, org_id):
    """내가 영업으로 데려온 업체인가 — 컨설턴트 전용.

    오디터는 심사기관이 케이스마다 배정하지만, 컨설턴트는 업체가 들어올 때 이미 관계가
    있다(초대 코드 또는 운영자 지정). 그 관계가 곧 접근 근거이자 수수료 근거다."""
    if user.get("role") != "consultant" or not org_id:
        return False
    o = db.get(models.Org, org_id)
    return bool(o and o.consultant_id and o.consultant_id == user.get("uid"))


def _my_client_org_ids(db, uid):
    """내가 담당하는 업체들 — 목록 필터용."""
    return {o.org_id for o in db.query(models.Org).filter_by(consultant_id=uid).all()}


def _org_access_ok(db, user, org_id):
    """이 사용자가 이 조직의 정보를 볼 수 있는가 — 자기 조직이거나, 배정받은 케이스의 조직.

    케이스 접근은 배정을 인정하는데(_assert_case_access) 조직 자원은 org_id 를 직접
    비교해 막고 있었다. 그래서 배정된 컨설턴트가 담당 업체의 케이스는 여는데 그 업체의
    할랄감독자·시설·SIHALAL 신원은 못 봤다(실측 403). 심사에 필요한 정보다.
    인증기관 역할(관리자·최고운영자·샤리아)은 조직을 넘어 본다 — 판정이 업무다."""
    if not org_id:
        return False
    if user.get("role") in CERTIFIER_ROLES:
        return True
    if org_id == user.get("org_id"):
        return True
    # 영업으로 데려온 업체는 배정을 기다리지 않는다 — 컨설턴트는 오디터와 성격이 다르다.
    if _is_my_client_org(db, user, org_id):
        return True
    ids = _assigned_case_ids(db, user.get("uid"))
    if not ids:
        return False
    return db.query(models.CaseApplication).filter(
        models.CaseApplication.case_id.in_(ids),
        models.CaseApplication.org_id == org_id).count() > 0


def _assert_org_access(db, user, org_id, code="FORBIDDEN_ORG"):
    if not _org_access_ok(db, user, org_id):
        raise HTTPException(403, {"code": code, "org_id": org_id,
                                  "hint": "배정되지 않은 조직입니다"})


def _assigned_case_ids(db, uid):
    """이 사용자가 배정받은 케이스 id — 목록 필터용."""
    out = set()
    for e in (db.query(models.WorkflowEvent)
              .filter(models.WorkflowEvent.action.in_(("ops.auditor_assigned",
                                                       CONSULTANT_ASSIGN_ACTION))).all()):
        p = e.payload or {}
        for k in ("auditor_id", "consultant_id", "user_id", "uid"):
            if p.get(k) and str(p[k]) == str(uid):
                out.add(e.case_id)
    return out


def _assert_case_access(db, user, c):
    """케이스 접근 판정 — 조직 격리는 유지하되 인증기관 역할과 배정을 인정한다.

    · 인증기관(관리자·최고운영자·샤리아): 전 케이스. 심사·판정이 이들의 업무다.
    · 컨설턴트: 자기 조직 + 영업으로 데려온 업체 + 배정받은 케이스.
      유치 관계를 먼저 보는 이유 — 컨설턴트는 오디터와 성격이 다르다. 오디터는 심사기관이
      케이스마다 배정하지만, 컨설턴트는 업체가 들어올 때 이미 관계가 있다. 배정을 기다리게
      하면 자기가 데려온 고객을 못 본다.
    · 오디터: 자기 조직 + 배정받은 케이스.
    · 그 외(신청기업·할랄감독자·동반자): 자기 조직만.
    """
    role = user.get("role")
    if role in CERTIFIER_ROLES:
        return
    if c.org_id == user.get("org_id"):
        return
    if _is_my_client_org(db, user, c.org_id):
        return
    if role in ASSIGNED_ROLES and str(user.get("uid")) in _assigned_staff(db, c.case_id):
        return
    raise HTTPException(403, {"code": "ORG_FORBIDDEN", "case_org": c.org_id,
                              "user_org": user.get("org_id"),
                              "hint": "배정되지 않은 케이스입니다"})


def _get_case(db, case_id, user=None) -> models.CaseApplication:
    c = db.get(models.CaseApplication, case_id)
    if not c:
        raise HTTPException(404, {"code": "CASE_NOT_FOUND", "case_id": case_id})
    if user:
        _assert_case_access(db, user, c)
    return c


def _audit_row_body(actor_id, action, resource_type, resource_id, case_id, created_at, meta_wo):
    """AuditLog 정규 본문(체인 해시 대상) — 쓰기(_audit)/검증(audit_log_verify)이 동일 직렬화 사용.
    meta의 _chain(체인 자체)은 제외한 나머지 본문에 대해 해시(자기참조 방지). 스키마 무변경(meta JSON)."""
    import json as _json
    return _json.dumps({"actor_id": actor_id, "action": action,
                        "resource_type": resource_type, "resource_id": resource_id,
                        "case_id": case_id, "created_at": str(created_at),
                        "meta": meta_wo or {}}, sort_keys=True, ensure_ascii=False)


def _last_audit_hash(db, scope=None):
    """직전 AuditLog의 체인 해시(meta._chain.row) — created_at desc·id desc. scope=org_id면 조직 스코프.
    WorkflowEvent의 _last_hash(state_machine)와 동일 역할(HMAC 체인의 prev)."""
    q = db.query(models.AuditLog)
    if scope:
        q = q.filter(models.AuditLog.org_id == scope)
    row = q.order_by(models.AuditLog.created_at.desc(), models.AuditLog.id.desc()).first()
    if row and isinstance(row.meta, dict):
        ch = row.meta.get("_chain")
        if isinstance(ch, dict):
            return ch.get("row", "")
    return ""


def _audit(db, user, action, resource_type=None, resource_id=None, case_id=None, meta=None, commit=True):
    """접근/조회 감사로그 기록(§2.4). commit=False면 상위 트랜잭션에 합류.
    법적효력 P3a — 각 로그에 HMAC 체인(meta._chain={prev,row})을 얹어 tamper-evident화(스키마 무변경).
    체인은 WorkflowEvent record_event와 동일한 chain_row_hash(HMAC(SECRET)) 헬퍼를 재사용한다."""
    meta = dict(meta) if isinstance(meta, dict) else {}   # meta 없던 로그도 dict 생성
    created = datetime.utcnow()
    base = {k: v for k, v in meta.items() if k != "_chain"}
    prev = _last_audit_hash(db)
    body = _audit_row_body(user.get("uid"), action, resource_type, resource_id, case_id, created, base)
    base["_chain"] = {"prev": prev, "row": sm.chain_row_hash(prev, body)}
    row = models.AuditLog(actor_id=user.get("uid"), actor_role=user.get("role"),
                          org_id=user.get("org_id"), action=action,
                          resource_type=resource_type, resource_id=resource_id,
                          case_id=case_id, meta=base, created_at=created)
    db.add(row)
    # 동일요청 다중 _audit 시 다음 호출이 이 로그를 _last_audit_hash에서 보도록 flush(체인 단절 방지) —
    # WorkflowEvent record_event의 db.flush()와 동일 사유(autoflush=False 세션).
    db.flush()
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
            "profile_ext": c.profile_ext or {}, "facility_ids": c.facility_ids or []}


# ── 알림 문구 카탈로그 ────────────────────────────────────────────────────
# 알림은 한국어 문장으로 만들어져 그대로 저장·표시됐다. 인니 심사자·업체가 보는 화면에
# 한국어가 그대로 떴다(실측: 인니어 화면 잔여 한글의 최대 덩어리).
#
# 문구를 키+파라미터로 남겨 읽는 사람의 언어로 다시 조립한다. 저장되는 title/body 는
# 한국어 그대로 둔다 — SMS·WhatsApp 발송과 이미 쌓인 알림이 그대로 동작해야 한다.
# 사람이 직접 쓴 본문(보완 사유·코멘트)은 번역하지 않는다. 남의 글을 기계가 바꿔 쓰면
# 심사 기록이 원문과 달라진다.
NOTIFY_MSG = {
    "application.submitted": {
        "ko": ("신청서 작성완료 제출", "{company} 신청서가 제출되었습니다. 경로판정(자기선언/정규) 확정 대기."),
        "en": ("Application submitted", "{company} has submitted its application. Awaiting pathway decision (self-declare / regular)."),
        "id": ("Permohonan telah dikirim", "{company} telah mengirimkan permohonan. Menunggu penetapan jalur (self-declare / reguler)."),
    },
    "audit_closed": {
        "ko": ("심사 완료", "{company} — 현장심사가 종결되었습니다."),
        "en": ("Audit closed", "{company} — the on-site audit has been closed."),
        "id": ("Audit selesai", "{company} — audit lapangan telah ditutup."),
    },
    "audit_scheduled": {
        "ko": ("심사 일정", "{company} — 현장심사가 예정되었습니다."),
        "en": ("Audit scheduled", "{company} — an on-site audit has been scheduled."),
        "id": ("Jadwal audit", "{company} — audit lapangan telah dijadwalkan."),
    },
    "audit_scheduled.lph": {
        "ko": ("심사 일정 · LPH 배정", "{company} — {lph} 배정, 현장심사가 예정되었습니다."),
        "en": ("Audit scheduled · LPH assigned", "{company} — {lph} assigned; an on-site audit has been scheduled."),
        "id": ("Jadwal audit · LPH ditetapkan", "{company} — {lph} ditetapkan; audit lapangan telah dijadwalkan."),
    },
    "audit_scheduled.slots": {
        "ko": ("현장심사 가능일 제시", "{company} — 클라이언트가 방문 가능일 {count}개를 제시했습니다."),
        "en": ("Proposed audit dates", "{company} — the client proposed {count} possible visit dates."),
        "id": ("Usulan tanggal audit", "{company} — klien mengusulkan {count} tanggal kunjungan."),
    },
    "certificate_issued": {
        "ko": ("인증서 발급", "{company} — 할랄 인증서 {cert_no} 발급 완료."),
        "en": ("Certificate issued", "{company} — halal certificate {cert_no} has been issued."),
        "id": ("Sertifikat diterbitkan", "{company} — sertifikat halal {cert_no} telah diterbitkan."),
    },
    "document_requested": {
        "ko": ("문서 요청", "{company} — 사전심사 문서 제출이 요청되었습니다."),
        "en": ("Documents requested", "{company} — documents have been requested for the pre-assessment."),
        "id": ("Dokumen diminta", "{company} — dokumen diminta untuk pra-penilaian."),
    },
    "fatwa_approved": {
        "ko": ("파트와 최종 승인", "{company} — 파트와 위원회 최종 승인 완료. 인증서 발급 가능."),
        "en": ("Fatwa final approval", "{company} — the fatwa committee has given final approval. The certificate can now be issued."),
        "id": ("Persetujuan akhir fatwa", "{company} — komisi fatwa telah memberikan persetujuan akhir. Sertifikat dapat diterbitkan."),
    },
    "contract.request": {
        "ko": ("새 계약 신청 · {company}", "관리자 승인·계약서 발송이 필요합니다."),
        "en": ("New contract request · {company}", "Administrator approval and contract dispatch are required."),
        "id": ("Permintaan kontrak baru · {company}", "Perlu persetujuan administrator dan pengiriman kontrak."),
    },
    "contract.approve": {
        "ko": ("계약서 도착 · {contract_no}", "계약서를 접수·확인해 주세요."),
        "en": ("Contract received · {contract_no}", "Please receive and review the contract."),
        "id": ("Kontrak diterima · {contract_no}", "Mohon terima dan periksa kontrak."),
    },
    "contract.receive": {
        "ko": ("계약 서명 요청 대기 · {company}", "클라이언트 접수 완료 — 서명 요청이 가능합니다."),
        "en": ("Awaiting signature request · {company}", "The client has received it — you can now request signatures."),
        "id": ("Menunggu permintaan tanda tangan · {company}", "Klien telah menerimanya — permintaan tanda tangan dapat dikirim."),
    },
    "contract.sign_request": {
        "ko": ("계약 서명 요청 · {company}", "계약서에 전자 서명해 주세요."),
        "en": ("Signature requested · {company}", "Please sign the contract."),
        "id": ("Permintaan tanda tangan · {company}", "Mohon tanda tangani kontrak."),
    },
    "contract.signed": {
        "ko": ("양자 서명 완료 · {contract_no}", "관리자 최종 확인이 필요합니다."),
        "en": ("Both parties signed · {contract_no}", "Final confirmation by the administrator is required."),
        "id": ("Kedua pihak telah menandatangani · {contract_no}", "Perlu konfirmasi akhir dari administrator."),
    },
    "contract.return": {
        "ko": ("계약 반려 · {company}", None),
        "en": ("Contract returned · {company}", None),
        "id": ("Kontrak dikembalikan · {company}", None),
    },
    "contract.confirm": {
        "ko": ("계약 최종 확인 완료 · {contract_no}", "청구서가 곧 생성됩니다."),
        "en": ("Contract confirmed · {contract_no}", "An invoice will be generated shortly."),
        "id": ("Kontrak dikonfirmasi · {contract_no}", "Faktur akan segera dibuat."),
    },
    "mock_audit.assigned": {
        "ko": ("모의심사 대상 배정", "결제 확정으로 모의심사 대상으로 배정되었습니다."),
        "en": ("Assigned to mock audit", "Payment confirmed — the case has been assigned to a mock audit."),
        "id": ("Ditetapkan untuk audit simulasi", "Pembayaran dikonfirmasi — kasus ditetapkan untuk audit simulasi."),
    },
    "ops.company_approved": {
        "ko": ("신규 업체 승인", "최고운영자가 신규 업체 등록을 승인했습니다."),
        "en": ("New company approved", "The chief operations officer approved the new company registration."),
        "id": ("Perusahaan baru disetujui", "Kepala operasional telah menyetujui pendaftaran perusahaan baru."),
    },
    "ops.auditor_assigned": {
        "ko": ("오디터 배정 — 수락/거절 필요", "{auditor} 님이 심사 담당으로 배정되었습니다. 배정함에서 수락 또는 거절해 주세요."),
        "en": ("Auditor assigned — accept or decline", "{auditor} has been assigned as the auditor. Accept or decline it in your assignment inbox."),
        "id": ("Auditor ditetapkan — terima atau tolak", "{auditor} ditetapkan sebagai auditor. Terima atau tolak di kotak penetapan Anda."),
    },
    # 본문이 사람이 쓴 글인 알림 — 제목만 번역하고 본문은 원문 그대로 둔다.
    "preassess.doc_request": {
        "ko": ("사전심사 추가서류 요청", None),
        "en": ("Pre-assessment: additional documents requested", None),
        "id": ("Pra-penilaian: dokumen tambahan diminta", None),
    },
    "preassess.resubmit": {
        "ko": ("사전심사 재제출", None),
        "en": ("Pre-assessment resubmitted", None),
        "id": ("Pra-penilaian dikirim ulang", None),
    },
}


def notify_text(n, lang="ko"):
    """알림을 읽는 사람의 언어로 조립 — (제목, 본문).

    payload 가 없으면(예전에 쌓인 알림, 카탈로그에 없는 이벤트) 저장된 한국어를 그대로 쓴다.
    본문 템플릿이 None 이면 사람이 쓴 원문을 그대로 둔다."""
    lang = lang if lang in ("ko", "en", "id") else "ko"
    pl = n.payload if isinstance(n.payload, dict) else None
    tmpl = NOTIFY_MSG.get((pl or {}).get("key") or "", {}).get(lang)
    if not tmpl:
        return n.title, n.body
    params = (pl or {}).get("params") or {}
    try:
        title = tmpl[0].format(**params)
    except (KeyError, IndexError, ValueError):
        title = n.title
    body = n.body
    if tmpl[1]:
        try:
            body = tmpl[1].format(**params)
        except (KeyError, IndexError, ValueError):
            body = n.body
    return title, body


def _notify(db, case, event_type, title=None, body="", channels=None, role=None, msg=None):
    """이벤트 알림을 큐(unsent)에 적재. 실제 발송은 비동기 워커(drain)가 처리 — Rizky #5.

    msg=(문구키, params) 를 주면 한국어 문구를 카탈로그에서 만들어 저장하고, 키·파라미터를
    payload 에 남긴다 — 읽는 사람의 언어로 다시 조립하기 위해서다. title/body 를 직접 주면
    종전과 똑같이 동작한다(카탈로그에 없는 알림은 그대로 둔다)."""
    payload = None
    if msg:
        key, params = msg[0], (msg[1] or {})
        payload = {"key": key, "params": params}
        ko = NOTIFY_MSG.get(key, {}).get("ko")
        if ko:
            try:
                title = ko[0].format(**params)
            except (KeyError, IndexError, ValueError):
                title = title or key
            if ko[1]:
                try:
                    body = ko[1].format(**params)
                except (KeyError, IndexError, ValueError):
                    pass
    n = models.Notification(
        org_id=(case.org_id if case else None), case_id=(case.case_id if case else None),
        role=role, event_type=event_type, channels=channels or ["inapp"],
        title=title, body=body, payload=payload, status="unsent")
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
@app.get("/debug/stream-test")
def debug_stream_test(mode: str = Query("ndjson"), n: int = Query(6)):
    """프록시 버퍼링 진단용 — 1초마다 한 줄. 업무 로직 없음(OCR/LLM 미사용).

    인테이크가 몇 분간 침묵하는 원인이 우리 코드인지 앞단(프록시)인지 가르기 위한 것.
    로컬에서 1초 간격으로 도착하는데 터널에서 끝에 몰려 오면 앞단 버퍼링이다."""
    import json as _json
    import time as _t
    from fastapi.responses import StreamingResponse

    def gen():
        for i in range(1, max(1, min(n, 30)) + 1):
            line = _json.dumps({"i": i, "t": round(_t.time(), 2)})
            yield ("data: %s\n\n" % line) if mode == "sse" else (line + "\n")
            _t.sleep(1)

    ct = "text/event-stream" if mode == "sse" else "application/x-ndjson"
    return StreamingResponse(gen(), media_type=ct,
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


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


# ── 멀티테넌트 도메인·화이트라벨 (범위 확장) — 스키마 무변경(Org.profile_ext에 tenant 서브키).
#    profile_ext.tenant = {domain, domain_status, domain_token, verified_at,
#                          branding:{brand_name, logo_url, primary_color, locale, support_email}} ──
TENANT_KEY = "tenant"
TENANT_DOMAIN_STATUSES = ("draft", "domain_pending", "active", "suspended")


def _tenant(o):
    return dict((o.profile_ext or {}).get(TENANT_KEY) or {})


def _set_tenant(db, o, patch):
    pe = dict(o.profile_ext or {})
    t = dict(pe.get(TENANT_KEY) or {})
    t.update(patch)
    pe[TENANT_KEY] = t
    o.profile_ext = pe
    flag_modified(o, "profile_ext")   # JSON 컬럼 in-place 변경 감지
    return t


def _org_by_domain(db, host):
    host = (host or "").split(":")[0].strip().lower()
    if not host:
        return None
    for o in db.query(models.Org).all():
        t = _tenant(o)
        if t.get("domain", "").lower() == host and t.get("domain_status") == "active":
            return o
    return None


@app.get("/public-config")
def public_config(request: Request, db: Session = Depends(get_db)):
    """로그인 화면용 공개 설정 — dev 모드 + 요청 호스트에 매칭되는 테넌트 화이트라벨 브랜딩."""
    out = {"dev_mode": auth.dev_mode()}
    try:
        o = _org_by_domain(db, request.headers.get("host"))
        if o:
            b = dict(_tenant(o).get("branding") or {})
            out["tenant"] = {"org_id": o.org_id, "name": o.name,
                             "brand_name": b.get("brand_name") or o.name,
                             "logo_url": b.get("logo_url"), "primary_color": b.get("primary_color"),
                             "locale": b.get("locale"), "support_email": b.get("support_email")}
    except Exception as e:  # noqa: BLE001 — 브랜딩 실패가 로그인을 막지 않는다
        log.warning("public-config tenant 조회 실패: %s", e)
    return out


@app.get("/admin/orgs/{org_id}/tenant")
def get_org_tenant(org_id: str, user=Depends(auth.require_roles("operator")),
                   db: Session = Depends(get_db)):
    o = db.get(models.Org, org_id)
    if not o:
        raise HTTPException(404, {"code": "ORG_NOT_FOUND"})
    t = _tenant(o)
    return {"org_id": org_id, "domain": t.get("domain"), "domain_status": t.get("domain_status", "draft"),
            "verify_txt_record": ("glhac-verify=" + t["domain_token"]) if t.get("domain_token") else None,
            "verified_at": t.get("verified_at"), "branding": t.get("branding") or {}}


@app.post("/admin/orgs/{org_id}/domain")
def set_org_domain(org_id: str, body: dict = None, user=Depends(auth.require_roles("operator")),
                   db: Session = Depends(get_db)):
    """도메인 등록 — 검증 토큰을 발급하고 domain_pending으로 전환(DNS TXT 레코드로 소유 증명)."""
    import re as _re
    import secrets as _secrets
    o = db.get(models.Org, org_id)
    if not o:
        raise HTTPException(404, {"code": "ORG_NOT_FOUND"})
    dom = str((body or {}).get("domain") or "").strip().lower().rstrip(".")
    if not _re.fullmatch(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+", dom):
        raise HTTPException(422, {"code": "BAD_DOMAIN"})
    for other in db.query(models.Org).filter(models.Org.org_id != org_id).all():
        if _tenant(other).get("domain", "").lower() == dom:
            raise HTTPException(409, {"code": "DOMAIN_TAKEN"})
    token = _secrets.token_hex(16)
    t = _set_tenant(db, o, {"domain": dom, "domain_status": "domain_pending",
                            "domain_token": token, "verified_at": None})
    db.commit()
    return {"org_id": org_id, "domain": dom, "domain_status": t["domain_status"],
            "verify_txt_record": "glhac-verify=" + token,
            "hint": "DNS TXT 레코드에 위 값을 등록한 뒤 verify-domain을 호출하세요."}


@app.post("/admin/orgs/{org_id}/verify-domain")
def verify_org_domain(org_id: str, user=Depends(auth.require_roles("operator")),
                      db: Session = Depends(get_db)):
    """DNS TXT 조회로 도메인 소유 검증 → active. dnspython 없으면 424(수동 승인 경로 제공)."""
    o = db.get(models.Org, org_id)
    if not o:
        raise HTTPException(404, {"code": "ORG_NOT_FOUND"})
    t = _tenant(o)
    dom, token = t.get("domain"), t.get("domain_token")
    if not (dom and token):
        raise HTTPException(409, {"code": "DOMAIN_NOT_SET"})
    try:
        import dns.resolver as _dns
    except ImportError:
        raise HTTPException(424, {"code": "DNS_RESOLVER_UNAVAILABLE",
                                  "hint": "pip install dnspython 또는 admin 수동 승인(activate-domain) 사용"})
    want = "glhac-verify=" + token
    try:
        answers = _dns.resolve(dom, "TXT")
        found = any(want in b.decode() for a in answers for b in a.strings)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(409, {"code": "DNS_LOOKUP_FAILED", "detail": str(e)[:120]})
    if not found:
        raise HTTPException(409, {"code": "TXT_RECORD_NOT_FOUND", "expected": want})
    _set_tenant(db, o, {"domain_status": "active", "verified_at": datetime.utcnow().isoformat()})
    db.commit()
    return {"org_id": org_id, "domain": dom, "domain_status": "active"}


@app.post("/admin/orgs/{org_id}/activate-domain")
def activate_org_domain(org_id: str, body: dict = None, user=Depends(auth.require_roles()),
                        db: Session = Depends(get_db)):
    """DNS 검증 우회 수동 활성/정지 — admin 전용(감사 로그 남김)."""
    o = db.get(models.Org, org_id)
    if not o:
        raise HTTPException(404, {"code": "ORG_NOT_FOUND"})
    st = str((body or {}).get("status") or "active")
    if st not in TENANT_DOMAIN_STATUSES:
        raise HTTPException(422, {"code": "BAD_STATUS", "allowed": list(TENANT_DOMAIN_STATUSES)})
    if not _tenant(o).get("domain"):
        raise HTTPException(409, {"code": "DOMAIN_NOT_SET"})
    _set_tenant(db, o, {"domain_status": st,
                        "verified_at": datetime.utcnow().isoformat() if st == "active" else None})
    _audit(db, user, "org.domain_" + st, "org", org_id, None, {"manual": True}, commit=False)
    db.commit()
    return {"org_id": org_id, "domain_status": st}


@app.post("/admin/orgs/{org_id}/branding")
def set_org_branding(org_id: str, body: dict = None, user=Depends(auth.require_roles("operator")),
                     db: Session = Depends(get_db)):
    """화이트라벨 브랜딩 — 로그인·헤더에 노출될 브랜드명·로고·주색상·로케일·지원메일."""
    import re as _re
    o = db.get(models.Org, org_id)
    if not o:
        raise HTTPException(404, {"code": "ORG_NOT_FOUND"})
    b = body or {}
    color = str(b.get("primary_color") or "").strip()
    if color and not _re.fullmatch(r"#[0-9a-fA-F]{6}", color):
        raise HTTPException(422, {"code": "BAD_COLOR", "hint": "#RRGGBB"})
    logo = str(b.get("logo_url") or "").strip()
    if logo and not logo.startswith(("https://", "/static/")):
        raise HTTPException(422, {"code": "BAD_LOGO_URL", "hint": "https:// 또는 /static/ 경로만 허용"})
    loc = str(b.get("locale") or "").strip()
    if loc and loc not in ("ko", "en", "id"):
        raise HTTPException(422, {"code": "BAD_LOCALE", "allowed": ["ko", "en", "id"]})
    branding = {"brand_name": str(b.get("brand_name") or "")[:60] or None, "logo_url": logo or None,
                "primary_color": color or None, "locale": loc or None,
                "support_email": str(b.get("support_email") or "")[:80] or None}
    _set_tenant(db, o, {"branding": branding})
    db.commit()
    return {"org_id": org_id, "branding": branding}


@app.get("/verify/{qr_token}")
def verify_certificate(qr_token: str, db: Session = Depends(get_db)):
    """공개 인증서 검증(§11.5·§14 P1) — 인증 불필요, 민감정보 미노출.
    제품별 인증서 토큰({parent}.{product_id[:8]})도 동일 경로로 검증(범위 확장)."""
    base_token, _, prod_frag = qr_token.partition(".")
    cert = (db.query(models.HalalCertificate).filter_by(qr_token=base_token).first()
            if base_token else None)
    if not cert:
        raise HTTPException(404, {"code": "CERT_NOT_FOUND", "valid": False})
    product_scope = None
    if prod_frag:
        p = next((x for x in _cert_products(db, cert) if x.product_id.startswith(prod_frag)), None)
        if not p:
            raise HTTPException(404, {"code": "PRODUCT_NOT_IN_SCOPE", "valid": False})
        product_scope = _product_cert_meta(db, cert, p)
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
    official = _official_bpjph_no(db, cert.case_id)
    out = {"valid": valid, "certificate_no": cert.certificate_no,
           "company_name": (c.company_name if c else None),
           "status": cert.status, "issue_date": cert.issue_date,
           "expiry_date": cert.expiry_date, "scope": cert.scope,
           "signed": bool(sig), "signature_valid": sig_valid,
           # 법적효력 Phase 0 — 공식성·서명 성격 고지(오인 차단)
           "halal_no_is_official": bool(official),
           "official_bpjph_no": official,
           "signature_nature": (_sig_nature_for(db, cert.case_id, "certificate") if sig else None),
           "signature_valid_meaning": _SIG_VALID_MEANING,
           "disclaimer": _LEGAL_DISCLAIMER_VERIFY}
    if product_scope:   # 제품별 인증서 검증 — 해당 제품으로 범위 축소 응답
        out.update({"certificate_no": product_scope["certificate_no"],
                    "parent_certificate_no": cert.certificate_no,
                    "product_name": product_scope["product_name"],
                    "scope": [product_scope["product_name"]]})
    return out


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


# AI가 꺼져 있어도 업무는 돌아야 한다. 다만 '조용히' 돌면 안 된다 —
# 자동으로 채워질 줄 알았던 칸이 비어 있는데 이유를 아무도 모르는 상태가 제일 나쁘다.
# 그래서 무엇이 꺼졌고, 그래서 무엇을 손으로 해야 하는지를 한 곳에서 알려준다.
_CAP_FALLBACK = {
    "doc_classify": ("서류 유형 자동판정",
                     "파일명 사전으로 판정합니다. 틀리면 문서 화면에서 유형을 직접 고치세요."),
    "field_extract": ("서류에서 값 자동추출",
                      "회사명·NIB·주소는 신청서 화면에서 직접 입력하세요."),
    "ocr": ("스캔·사진 글자 인식",
            "이미지로 된 서류는 글자를 읽지 못합니다. 원본 PDF·엑셀로 올리거나 값을 직접 입력하세요."),
    "explain": ("성분 해설·AI 질의",
                "판정 근거는 원재료 화면의 매칭 결과와 증빙으로 확인하세요."),
    "translate": ("자동 번역",
                  "화면·문서 번역은 사전에 등록된 문구만 나옵니다."),
}


@app.get("/system/capabilities")
def system_capabilities():
    """이 서버에서 지금 무엇이 되고 무엇이 안 되는지.

    화면은 이 응답만 보고 배너를 띄운다. AI가 없어도 막히지 않는 일과,
    손으로 해야 하는 일을 구분해 알려주는 것이 목적이다."""
    h = ai_local.health()
    llm = h.get("ollama") == "up" and bool(h.get("model_ready"))
    ost = ai_local.ocr_status()
    ocr = bool(ost["ready"])
    rag = bool((ai_local.context_health() or {}).get("ok"))
    caps = {
        # 어느 공급자로 도는지 화면·배포 점검에서 바로 보이게 한다
        # (같은 소스로 none/ollama/openai 세 배포가 돌기 때문).
        "llm": {"ok": llm, "provider": h.get("provider"), "endpoint": h.get("endpoint"),
                "detail": h.get("configured") if llm else h.get("error") or "모델 없음"},
        # 설치와 모델 캐시를 나눠 보여준다 — 배포 직후 "패키지는 있는데 모델이 없다"를
        # 첫 업로드에서 발견하면 이미 늦다.
        "ocr": {"ok": ocr,
                "detail": ("PaddleOCR · 모델 %d종" % ost["models_cached"]) if ocr
                          else (ost["note"] or "미설치"),
                "installed": ost["installed"], "models_cached": ost["models_cached"]},
        "rag": {"ok": rag, "detail": "CHU-1" if rag else "비활성"},
    }
    degraded = []
    if not llm:
        degraded += ["doc_classify", "field_extract", "explain", "translate"]
    if not ocr:
        degraded.append("ocr")
    return {
        "ai_ready": llm and ocr,
        "mode": "full" if (llm and ocr) else ("manual" if not llm else "partial"),
        "capabilities": caps,
        # 꺼진 기능마다 '대신 무엇을 하면 되는지'. 화면이 그대로 보여준다.
        "manual_steps": [{"key": k, "feature": _CAP_FALLBACK[k][0],
                          "instead": _CAP_FALLBACK[k][1]} for k in degraded],
        # AI 없이도 되는 일 — '못 쓰는 시스템'으로 읽히지 않게 함께 알린다.
        # 고정 목록이면 OCR 이 있으나 없으나 같은 말을 한다 → 실제 가용 기능으로 만든다.
        "works_without_ai": ([
            "회원가입·신청서 작성·제품 등록",
            "서류 업로드(유형은 파일명으로 판정 · 수기 교정 가능)",
            "원재료 판정(온톨로지 사전 — LLM 아님)",
            "증빙 첨부·인증번호 입력·판정 갱신",
            "SJPH 매뉴얼·현장심사 보고서·인증서 생성",
            "일정 조율·계약·청구·입금·발급 승인",
        ] + (["스캔·사진 서류 글자 인식(OCR)"] if ocr else [])),
    }


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



# ── 도메인 간 로그인 인계(홈페이지 → AI 시스템) ───────────────────────────
#
# glhac.com 과 glhac.co.kr 은 **서로 다른 도메인**이라 쿠키·localStorage 가 공유되지
# 않는다(브라우저 보안 정책). 그래서 홈페이지에서 로그인해도 AI 시스템은 그 사실을
# 모른다. 명시적으로 넘겨 주는 수밖에 없다.
#
# 토큰을 URL 에 실어 보내지 않는다 — 주소창·브라우저 기록·Referer 에 남아 흘러나간다.
# 대신 **한 번만 쓰이고 곧 만료되는 인계코드**를 넘기고, 받는 쪽이 토큰으로 바꾼다.
# AI 시스템의 공개 주소 — 인계 후 돌아올 곳
APP_BASE_URL = os.environ.get("GLHAC_APP_URL", "https://glhac.co.kr")
_HANDOFF: dict = {}          # code → (발급시각, 토큰묶음)
_HANDOFF_TTL = 90            # 초. 사람이 리다이렉트되는 시간이면 충분하다.


def _handoff_sweep():
    """만료분 정리 — 메모리에 쌓이지 않게. 단일 워커라 프로세스 메모리로 충분하다."""
    now = time.time()
    for k in [k for k, (t, _) in _HANDOFF.items() if now - t > _HANDOFF_TTL]:
        _HANDOFF.pop(k, None)


@app.post("/auth/handoff/login")
def handoff_login(body: schemas.LoginReq, db: Session = Depends(get_db)):
    """홈페이지(glhac.com)용 로그인 — 토큰 대신 일회용 인계코드만 돌려준다.

    토큰이 홈페이지 쪽 자바스크립트에 아예 닿지 않게 한다."""
    rl_key = (body.username or "").lower()
    if auth.rate_limited(rl_key):
        raise HTTPException(429, {"code": "TOO_MANY_ATTEMPTS",
                                  "retry_after_sec": auth._RL_WINDOW})
    u = db.query(models.User).filter_by(username=body.username).first()
    if not u or not auth.verify_pw(body.password, u.password_hash):
        auth.record_attempt(rl_key)
        raise HTTPException(401, {"code": "BAD_CREDENTIALS"})
    auth.clear_attempts(rl_key)
    _handoff_sweep()
    code = secrets.token_urlsafe(24)
    _HANDOFF[code] = (time.time(), {**auth.make_tokens(u), "role": u.role,
                                    "org_id": u.org_id, "username": u.username})
    return {"handoff": code, "expires_in": _HANDOFF_TTL,
            "redirect": "%s/ui/#handoff=%s" % (APP_BASE_URL.rstrip("/"), code)}


@app.post("/auth/handoff/issue")
def handoff_issue(user=Depends(auth.get_current_user)):
    """이미 로그인한 사용자가 다른 도메인으로 건너갈 때 쓰는 인계코드.

    홈페이지(glhac.com)는 게시판 때문에 자체 로그인 상태를 갖는다. 거기서 '인증 신청'을
    누르면 다시 로그인시키지 않고 이 코드를 받아 AI 시스템으로 넘어간다 — 같은 계정이
    두 화면에서 이어진다."""
    from .db import SessionLocal
    db = SessionLocal()
    try:
        u = db.get(models.User, user["uid"])
        if not u:
            raise HTTPException(401, {"code": "USER_NOT_FOUND"})
        _handoff_sweep()
        code = secrets.token_urlsafe(24)
        _HANDOFF[code] = (time.time(), {**auth.make_tokens(u), "role": u.role,
                                        "org_id": u.org_id, "username": u.username})
        return {"handoff": code, "expires_in": _HANDOFF_TTL,
                "redirect": "%s/ui/#handoff=%s" % (APP_BASE_URL.rstrip("/"), code)}
    finally:
        db.close()


@app.post("/auth/handoff/exchange")
def handoff_exchange(body: dict = None):
    """인계코드 → 토큰. 한 번 쓰면 사라진다(재사용·도청 재생 차단)."""
    _handoff_sweep()
    code = ((body or {}).get("handoff") or "").strip()
    got = _HANDOFF.pop(code, None)          # pop — 재사용 불가
    if not got:
        raise HTTPException(401, {"code": "HANDOFF_INVALID",
                                  "message": "만료되었거나 이미 사용된 링크입니다. 다시 로그인해 주세요."})
    return got[1]



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
    """사업자/공장 등록증 텍스트 → 프로필 필드 추출 (Rizky #1).

    한국 양식만 알던 파서다. 실제 신청자는 인도네시아 업체이고 그쪽 서류는
    'Nama Perusahaan / Nomor Induk Berusaha / Nama Pimpinan / Alamat Perusahaan' 으로
    적힌다(실측: CV. CITRA PRATAMA 등록증). 인니 양식을 먼저 보고, 없으면 한국 양식을 본다.
    NIB는 13자리 숫자, 한국 사업자번호는 000-00-00000 로 서로 형태가 달라 섞이지 않는다."""
    import re
    t = text or ""
    out = {}
    # ── 인도네시아 양식 ────────────────────────────────────────────────
    m = re.search(r"Nomor\s+Induk\s+Berusaha\s*[:：]?\s*(\d[\d\s-]{10,20})", t, re.I)
    if m:
        out["nib"] = re.sub(r"[^\d]", "", m.group(1))
    for pat, key in [(r"Nama\s+Perusahaan", "company_name"),
                     (r"Nama\s+Pimpinan", "responsible_person"),
                     (r"Nama\s+Penyelia\s+Halal", "halal_supervisor"),
                     (r"Alamat\s+Perusahaan", "address"),
                     (r"Alamat\s+Pabrik", "factory_address"),
                     (r"Jenis\s+Usaha", "business_type"),
                     (r"Skala\s+Usaha", "business_scale")]:
        mm = re.search(pat + r"\s*[:：]\s*([^\n]{1,120}?)(?=\s+(?:Nama|Nomor|Alamat|Jenis|Skala|Kode)\s|\n|$)",
                       t, re.I)
        if mm:
            v = mm.group(1).strip(" :：·|*)")
            if v and not out.get(key):
                out[key] = v
    # ── 한국 양식 ──────────────────────────────────────────────────────
    m = re.search(r"\d{3}-\d{2}-\d{5}", t)                       # 사업자등록번호
    if m and not out.get("nib"):
        out["nib"] = m.group(0)
    for label, key in [("상호", "company_name"), ("법인명", "company_name"),
                       ("공장명", "factory_name"), ("대표자", "responsible_person"),
                       ("성명", "responsible_person")]:
        mm = re.search(label + r"[)\s:：·]*([^\n]{1,40})", t)
        if mm and not out.get(key):
            out[key] = mm.group(1).strip(" :：·|")
    mm = re.search(r"(사업장\s*소재지|소재지|사업장|주소)[)\s:：·]*([^\n]{2,80})", t)
    if mm and not out.get("address"):
        out["address"] = mm.group(2).strip(" :：·|")
    mm = re.search(r"(공장\s*소재지|공장\s*주소)[)\s:：·]*([^\n]{2,80})", t)
    if mm and not out.get("factory_address"):
        out["factory_address"] = mm.group(2).strip(" :：·|")
    mm = re.search(r"(업태|업종|종목)[)\s:：·]*([^\n]{1,40})", t)
    if mm and not out.get("business_type"):
        out["business_type"] = mm.group(2).strip(" :：·|")
    return out


@app.post("/auth/ocr-extract")
def auth_ocr_extract(body: schemas.OCRExtractReq):
    """회원가입 전 등록증 자동추출(공개) — 이미지·PDF 모두 수용.

    예전에는 무엇이 오든 .png 로 저장해 OCR을 돌렸다. 화면은 PDF도 받는데
    (accept="image/*,.pdf") 서버가 PDF를 이미지로 읽으려다 'Image read Error'로 죽었고,
    화면에는 그게 'OCR 엔진 미설치'로 표시돼 원인을 감췄다(실측: 사업자등록증 PDF).
    이제 intake.parse_file 에 맡긴다 — PDF는 본문 텍스트로, 스캔본·이미지는 OCR로 읽는다."""
    import base64
    # 공개 엔드포인트 — 크기 상한 강제(DoS 방지). 포맷은 파서가 가린다.
    raw = base64.b64decode(_validate_upload(body.image_b64, None))
    from .intake import parse_file
    name = "upload.pdf" if raw[:5] == b"%PDF-" else "upload.png"
    try:
        text = parse_file(name, raw) or ""
    except Exception as e:  # noqa: BLE001
        return {"ocr_available": False, "fields": {},
                "error": "%s: %s" % (type(e).__name__, str(e)[:160])}
    if not text.strip():
        return {"ocr_available": False, "fields": {},
                "error": "문서에서 읽어낼 글자가 없습니다 (빈 파일이거나 해상도가 너무 낮습니다)"}
    return {"ocr_available": True, "fields": _parse_biz_doc(text),
            "source": "pdf_text" if name.endswith(".pdf") else "ocr",
            "raw": text[:1500]}


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
    # 컨설턴트 초대 코드 — 영업으로 데려온 업체임을 밝힌다. 코드가 잘못됐으면 가입 자체를
    # 막지 않고 관계만 붙이지 않는다(업체가 가입을 못 하는 게 더 나쁘다). 다만 왜 안 붙었는지
    # 응답에 남겨 화면이 안내할 수 있게 한다.
    inv, invite_note = None, None
    if (body.invite_code or "").strip():
        code = body.invite_code.strip().upper()
        inv = db.query(models.ConsultantInvite).filter_by(code=code).first()
        if not inv:
            invite_note = "NOT_FOUND"
        elif inv.revoked_at:
            invite_note, inv = "REVOKED", None
        elif inv.expires_at and inv.expires_at <= datetime.utcnow():
            invite_note, inv = "EXPIRED", None
        elif inv.used_count >= inv.max_uses:
            invite_note, inv = "USED_UP", None
    # 회사1:직원N — 소속회사명으로 기존 org(Company) 매핑 (Phase 2)
    existing = db.query(models.Org).filter(models.Org.name == company_name).first() if company_name else None
    if existing:
        org = existing.org_id
        company_role = "client_staff"    # 기존 회사 추가 직원 = 업무자
    else:
        org = "org_" + models.uid()[:8]
        company_role = "client_admin"    # 새 회사 첫 가입자 = 기업업무 관리자
        _o = models.Org(org_id=org, name=company_name or "My Company", address=body.address)
        if inv:
            _o.consultant_id = inv.consultant_id      # 누가 데려왔는지 — 수수료 근거
            _o.consultant_linked_at = datetime.utcnow()
        db.add(_o)
    if inv:
        inv.used_count = (inv.used_count or 0) + 1
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
    _ref = None
    if inv:
        _cp = db.get(models.ConsultantProfile, inv.consultant_id)
        _cu = db.get(models.User, inv.consultant_id)
        _ref = {"consultant": (_cp.display_name if _cp and _cp.display_name
                               else (_cu.username if _cu else None))}
    elif invite_note:
        _ref = {"invite_error": invite_note}
    return {**auth.make_tokens(u), "role": u.role, "org_id": u.org_id, "username": u.username,
            "referral": _ref,
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
    # 담당 컨설턴트 — 누가 이 업체를 데려왔는지. 수수료 정산의 근거라 화면에 보여야 한다.
    prof = {p.consultant_id: p for p in db.query(models.ConsultantProfile).all()}
    unames = {u.user_id: u.username for u in db.query(models.User).all()}
    for o in db.query(models.Org).filter(models.Org.consultant_id.isnot(None)).all():
        row = orgs.get(o.org_id)
        if not row:
            continue
        pr = prof.get(o.consultant_id)
        row["consultant_id"] = o.consultant_id
        row["consultant_name"] = ((pr.display_name if pr and pr.display_name else None)
                                  or unames.get(o.consultant_id))
        row["consultant_linked_at"] = str(o.consultant_linked_at or "")
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
def admin_ontology_reseed(rescreen: bool = True, user=Depends(auth.require_roles()),
                          db: Session = Depends(get_db)):
    """온톨로지 재시딩 + 모듈 캐시 갱신 + 기존 원재료 재판정.

    캐시 갱신과 재판정이 없으면 재시딩이 사실상 무효였다(옛 판정이 그대로 남음).
    rescreen=false 로 재판정을 건너뛸 수 있다.
    """
    db.query(models.IngredientOntology).delete()
    db.commit()
    seed(db)
    screening.load_ontology(db)      # 모듈 캐시 갱신 — 없으면 이후 판정이 옛 온톨로지를 쓴다
    out = {"reseeded": True, "ontology_count": db.query(models.IngredientOntology).count(),
           "rescreened": 0, "changed": 0}
    if rescreen:
        changed = 0
        mats = db.query(models.Material).all()
        for m in mats:
            before = (m.screen_status, m.screen_result)
            screening.apply_screen(m)
            if (m.screen_status, m.screen_result) != before:
                changed += 1
        db.commit()
        out["rescreened"] = len(mats)
        out["changed"] = changed
    _audit(db, user, "ontology.reseed", "system", None, None,
           {"ontology_count": out["ontology_count"], "rescreened": out["rescreened"],
            "changed": out["changed"]})
    db.commit()
    # 반드시 마지막 — 앞선 commit들이 캐시 객체를 expire시키므로 여기서 다시 로드해야
    # 요청 종료 후에도 DetachedInstanceError가 나지 않는다(기동 시 규약과 동일).
    screening.load_ontology(db)
    return out


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


def _db_file_path():
    """SQLite 파일 경로(파일 DB가 아니면 None) — 초기화 후 실제 회수량 측정용."""
    try:
        url = engine.url
        return url.database if url.get_backend_name() == "sqlite" and url.database else None
    except Exception:  # noqa: BLE001
        return None


def _db_file_bytes():
    p = _db_file_path()
    if not p:
        return None
    total = 0
    for suffix in ("", "-wal", "-shm"):   # WAL·SHM까지 합산해야 실제 점유가 나온다
        try:
            total += os.path.getsize(p + suffix)
        except OSError:
            pass
    return total


def _upload_sandbox_files():
    """GLHAC_UPLOAD_DIR 안의 스테이징 파일 목록 — (경로, 바이트).

    업로드 원본은 DB(document_asset.content_b64, 암호화)에 있고 이 디렉터리는 OCR·라벨판정에
    넘길 때 쓰는 작업 공간이다. 케이스 귀속 정보가 없어 부분 초기화로는 지울 근거가 없다.
    그래서 sweep_uploads는 기본 off이고, 켤 때만 샌드박스 안에서만 지운다."""
    sandbox = os.path.realpath(os.environ.get("GLHAC_UPLOAD_DIR", "") or "")
    if not sandbox or not os.path.isdir(sandbox):
        return sandbox, []
    out = []
    for root, _dirs, names in os.walk(sandbox):
        for n in names:
            p = os.path.realpath(os.path.join(root, n))
            if not p.startswith(sandbox + os.sep):   # 심볼릭 링크로 밖을 가리키면 건너뛴다
                continue
            try:
                out.append((p, os.path.getsize(p)))
            except OSError:
                pass
    return sandbox, out


def _case_scoped_models():
    """case_id를 가진 모델 전부 — 단, AuditLog는 제외한다.

    감사로그를 케이스와 함께 지우면 전역 HMAC 체인의 prev가 사라져 영구 단절이 남는다
    (2026-08-04 초기화가 그렇게 깨졌다). 초기화는 케이스 데이터를 지우는 것이지
    '지웠다는 사실'까지 지우는 것이 아니다. CaseApplication은 부모라 호출부에서 마지막에 지운다."""
    out = []
    for name in dir(models):
        m = getattr(models, name)
        t = getattr(m, "__table__", None)
        if t is None or name.startswith("_"):
            continue
        if "case_id" in t.columns and m is not models.AuditLog and m is not models.CaseApplication:
            out.append((t.name, m))
    return sorted(out)


@app.post("/admin/cases/purge")
def admin_cases_purge(body: schemas.CasePurgeReq, user=Depends(auth.require_roles("admin")),
                      db: Session = Depends(get_db)):
    """심사데이터 초기화(보존 화이트리스트 방식) — 스크립트로 DB를 직접 지우는 경로를 대체한다.

    직접 삭제는 감사로그를 남기지 않고, 감사로그까지 지우면 체인이 영구 단절된다.
    이 엔드포인트는 (1) AuditLog를 보존하고 (2) 케이스별·전체 삭제 사실을 감사로그에 남긴다.
    기본은 dry_run 미리보기이며, 실삭제는 confirm='PURGE'와 사유가 있어야 한다."""
    if not auth.dev_mode():
        raise HTTPException(403, {"code": "PURGE_DISABLED", "hint": "GLHAC_DEV=1 에서만 허용"})
    keep = set(body.keep or [])
    cases = db.query(models.CaseApplication).all()
    targets = [c for c in cases if c.case_id not in keep]
    missing = sorted(keep - {c.case_id for c in cases})

    scoped = _case_scoped_models()
    plan = []
    for c in targets:
        counts = {}
        for tname, m in scoped:
            n = db.query(m).filter(m.case_id == c.case_id).count()
            if n:
                counts[tname] = n
        plan.append({"case_id": c.case_id, "company_name": c.company_name,
                     "status": c.status, "rows": counts,
                     "audit_log_preserved": db.query(models.AuditLog)
                     .filter(models.AuditLog.case_id == c.case_id).count()})

    # 업로드 원본은 디스크가 아니라 DB(document_asset.content_b64, 암호화)에 있다.
    # 행을 지우면 내용도 사라지지만 SQLite는 파일을 줄이지 않아 빈 페이지에 잔상이 남는다 → VACUUM.
    sandbox, sandbox_files = _upload_sandbox_files()
    blob_bytes = 0
    for c in targets:
        blob_bytes += int(db.query(func.coalesce(func.sum(func.length(
            models.DocumentAsset.content_b64)), 0))
            .filter(models.DocumentAsset.case_id == c.case_id).scalar() or 0)

    out = {"dry_run": bool(body.dry_run), "keep": sorted(keep), "keep_not_found": missing,
           "delete_count": len(targets), "kept_count": len(cases) - len(targets),
           "cases": plan,
           "files": {"stored_in": "db(document_asset.content_b64, encrypted)",
                     "blob_bytes": blob_bytes,
                     "db_file_bytes": _db_file_bytes(),
                     "vacuum": bool(body.vacuum),
                     "upload_sandbox": sandbox or None,
                     "sandbox_files": len(sandbox_files),
                     "sandbox_bytes": sum(b for _p, b in sandbox_files),
                     "sweep_uploads": bool(body.sweep_uploads)},
           "note": "audit_log는 삭제하지 않는다(체인 보존). 삭제 사실은 감사로그에 기록된다."}
    if body.dry_run:
        return out
    if body.confirm != "PURGE":
        raise HTTPException(400, {"code": "CONFIRM_REQUIRED", "hint": "confirm='PURGE'"})
    if not (body.reason or "").strip():
        raise HTTPException(400, {"code": "REASON_REQUIRED", "hint": "초기화 사유는 감사로그에 남는다"})
    if body.expect_delete is not None and body.expect_delete != len(targets):
        raise HTTPException(409, {"code": "COUNT_MISMATCH", "expected": body.expect_delete,
                                  "actual": len(targets),
                                  "hint": "dry_run 결과와 다르다 — 데이터가 그새 바뀌었다"})

    # 삭제 '전에' 기록한다. 도중에 실패해도 착수 사실은 남아야 한다.
    for item in plan:
        _audit(db, user, "case.purge", "case", item["case_id"], item["case_id"],
               {"company_name": item["company_name"], "reason": body.reason,
                "rows_deleted": item["rows"]}, commit=False)
    _audit(db, user, "admin.cases.purge", "system", None, None,
           {"reason": body.reason, "deleted": [i["case_id"] for i in plan],
            "kept": sorted(keep)}, commit=False)
    db.commit()

    deleted = {}
    for c in targets:
        for tname, m in scoped:   # 자식 먼저
            n = db.query(m).filter(m.case_id == c.case_id).delete(synchronize_session=False)
            if n:
                deleted[tname] = deleted.get(tname, 0) + n
        db.delete(c)              # 부모는 마지막
        deleted["case_application"] = deleted.get("case_application", 0) + 1
    db.commit()

    files = out["files"]
    before_bytes = files["db_file_bytes"]
    removed, freed = [], 0
    if body.sweep_uploads:
        for p, b in sandbox_files:
            try:
                os.remove(p)
                removed.append(os.path.basename(p))
                freed += b
            except OSError as e:   # 지워지지 않은 건 숨기지 않고 보고한다
                files.setdefault("sweep_errors", []).append("%s: %s" % (os.path.basename(p), e))
    files["sandbox_removed"] = len(removed)
    files["sandbox_freed_bytes"] = freed

    if body.vacuum and _db_file_path():
        # 삭제한 원본이 빈 페이지에 남지 않도록 파일에서 회수한다. VACUUM은 트랜잭션 밖에서만 된다.
        db.commit()
        try:
            with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
                conn.exec_driver_sql("VACUUM")
                # WAL 모드에선 VACUUM 결과가 WAL에 쌓인다. TRUNCATE 체크포인트로 본 파일에 반영하고
                # WAL을 잘라내야 삭제분이 WAL에 남지 않는다(잔상 제거의 나머지 절반).
                conn.exec_driver_sql("PRAGMA wal_checkpoint(TRUNCATE)")
            files["vacuum_ok"] = True
        except Exception as e:  # noqa: BLE001 — 회수 실패가 초기화 자체를 되돌리진 않는다
            files["vacuum_ok"] = False
            files["vacuum_error"] = str(e)[:200]
            log.warning("purge VACUUM 실패: %s", e)
    after_bytes = _db_file_bytes()
    files["db_file_bytes_after"] = after_bytes
    if before_bytes is not None and after_bytes is not None:
        files["db_file_reclaimed_bytes"] = before_bytes - after_bytes

    out.update({"dry_run": False, "deleted_rows": deleted,
                "audit_log_preserved": db.query(models.AuditLog).count()})
    return out


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
        rh = sm.chain_row_hash(prev, body)
        if rh != e.row_hash:
            broken = e.event_id
            break
        prev = e.row_hash
    return {"count": len(evs), "integrity_ok": broken is None, "broken_at": broken}


@app.get("/admin/audit-log-verify")
def audit_log_verify(actor: str = Query(None), action: str = Query(None),
                     case: str = Query(None), user=Depends(auth.require_roles()),
                     db: Session = Depends(get_db)):
    """법적효력 P3b — AuditLog HMAC 체인 재생·재계산 검증(admin). audit_verify(WorkflowEvent) 미러.

    끊김을 한 덩어리로 보고하면 안 된다. 로그를 지워 생긴 단절과 본문을 고친 위조는
    성격이 전혀 다른데, 예전에는 둘 다 integrity_ok=false 하나로만 나와 화면에서
    구분할 수 없었다(실측: 위조 0건인데 삭제자국 4건 때문에 false).
    이제 tampered(위조)와 deleted_predecessor(선행 로그 삭제)를 갈라 돌려준다.
    integrity_ok 자체는 보수적으로 유지한다 — 지워서 감춘 것을 '무결'로 부르지 않는다."""
    q = db.query(models.AuditLog)
    if actor:
        q = q.filter(models.AuditLog.actor_id == actor)
    if action:
        q = q.filter(models.AuditLog.action == action)
    if case:
        q = q.filter(models.AuditLog.case_id == case)
    rows = q.order_by(models.AuditLog.created_at, models.AuditLog.id).all()
    filtered = bool(actor or action or case)
    out = _verify_audit_chain(rows, subset=filtered, known_hashes=_audit_chain_hashes(db))
    gaps = out.get("deleted_predecessor") or []
    out["summary"] = ("위조 %d건 · 선행로그 삭제로 인한 단절 %d건 (검사 %d행)"
                      % (out["tampered_count"], len(gaps), out["checked"]))
    return out


def _verify_wf_chain(evs):
    """WorkflowEvent 해시체인 재계산(audit_verify 미러) → (integrity_ok, count, break_at)."""
    import json as _json
    prev, broken = "", None
    for e in evs:
        body = _json.dumps({"case": e.case_id, "from": e.from_status, "to": e.to_status,
                            "action": e.action, "payload": e.payload or {}},
                           sort_keys=True, ensure_ascii=False)
        if sm.chain_row_hash(prev, body) != e.row_hash:
            broken = e.event_id
            break
        prev = e.row_hash
    return {"integrity_ok": broken is None, "count": len(evs), "break_at": broken}


def _audit_chain_hashes(db):
    """전역 AuditLog의 _chain.row 집합 — 부분집합 검증에서 '선행 로그 삭제'를 판별하는 데 쓴다."""
    out = set()
    for (meta,) in db.query(models.AuditLog.meta).all():
        ch = meta.get("_chain") if isinstance(meta, dict) else None
        if isinstance(ch, dict) and ch.get("row"):
            out.add(ch["row"])
    return out


def _verify_audit_chain(rows, subset=False, known_hashes=None):
    """AuditLog HMAC 체인 재계산(audit_log_verify 미러) → (integrity_ok, checked, break_at).

    감사체인은 전역이다(_last_audit_hash가 테이블 전체의 마지막 행을 prev로 삼는다).
    따라서 케이스 등으로 걸러낸 부분집합은 다른 케이스 로그가 사이에 끼는 순간 연결이 끊긴 것처럼
    보이며, 연결성을 그대로 검사하면 정상 데이터를 위조로 오탐한다. subset=True면 행 자기무결성만
    검사한다(audit_log_verify가 필터 시 연결성을 면제하는 것과 같은 규칙).
    단, prev를 만든 행이 DB에 없는 경우(선행 로그 삭제)는 부분집합에서도 실제 단절이므로 보고한다."""
    broken, checked, prev_row, deleted = None, 0, None, []
    tampered = []          # 행 자기무결성 위반 — 본문이 바뀐 진짜 위조 신호
    for r in rows:
        meta = r.meta if isinstance(r.meta, dict) else {}
        ch = meta.get("_chain")
        if not isinstance(ch, dict):
            continue
        base = {k: v for k, v in meta.items() if k != "_chain"}
        body = _audit_row_body(r.actor_id, r.action, r.resource_type, r.resource_id,
                               r.case_id, r.created_at, base)
        checked += 1
        prev = ch.get("prev", "")
        if known_hashes is not None and prev and prev not in known_hashes:
            deleted.append(r.id)   # 선행 로그가 삭제됨 — 부분집합에서도 실제 단절
        self_ok = sm.chain_row_hash(prev, body) == ch.get("row")
        if not self_ok:
            tampered.append(r.id)  # 끝까지 센다 — 몇 건인지가 판단을 가른다
        if broken is None:         # 첫 실패 지점만 기록하고, 탐지는 끝까지 이어간다
            linkage_ok = subset or prev_row is None or prev == prev_row
            if not self_ok or not linkage_ok:
                broken = r.id
        prev_row = ch.get("row")
    # integrity_ok는 보수적으로 둔다 — 단절도 false다. 로그를 지워 활동을 감춘 것을
    # '무결'로 표시하면 안 되기 때문이다. 대신 위조(tampered)와 삭제자국
    # (deleted_predecessor)을 갈라 보고해, 읽는 사람이 '위조 0건'인지 바로 알게 한다.
    out = {"integrity_ok": broken is None and not deleted,
           "checked": checked, "break_at": broken,
           "tampered": tampered, "tampered_count": len(tampered)}
    if subset:
        out["scope"] = "subset"
        out["note"] = ("부분집합(케이스 범위) 검증 — 감사체인은 전역이므로 행 자기무결성만 대조한다. "
                       "선행 로그가 삭제된 단절은 deleted_predecessor로 별도 보고.")
    if deleted:
        out["deleted_predecessor"] = deleted
    return out


@app.get("/cases/{case_id}/evidence-bundle.zip")
def evidence_bundle_zip(case_id: str, user=Depends(auth.require_roles("operator")),
                        db: Session = Depends(get_db)):
    """법적효력 P3c — 장기 법적 증거보존용 아카이브 번들(zip). 스키마 무변경(기존 조회만).
    구성: evidence.json(전이·감사·문서목록) + integrity_report.json(양 체인 무결성) + README.txt(고지)."""
    import io
    import json as _json
    import zipfile
    from fastapi.responses import StreamingResponse
    c = _get_case(db, case_id, user)   # 케이스 소유(org) 검증 포함
    evs = (db.query(models.WorkflowEvent).filter_by(case_id=case_id)
           .order_by(models.WorkflowEvent.created_at, models.WorkflowEvent.event_id).all())
    audits = (db.query(models.AuditLog).filter_by(case_id=case_id)
              .order_by(models.AuditLog.created_at, models.AuditLog.id).all())
    docs = (db.query(models.GeneratedDocument).filter_by(case_id=case_id)
            .order_by(models.GeneratedDocument.created_at).all())
    cert = db.query(models.HalalCertificate).filter_by(case_id=case_id, status="active").first()
    wf_report = _verify_wf_chain(evs)
    # audits는 케이스로 거른 부분집합이고 감사체인은 전역이므로 연결성은 면제한다(오탐 방지).
    al_report = _verify_audit_chain(audits, subset=True, known_hashes=_audit_chain_hashes(db))
    generated_at = datetime.utcnow().isoformat()
    evidence = {
        "generated_at": generated_at,
        "case": {"case_id": c.case_id, "company_name": c.company_name, "nib": c.nib,
                 "pathway": c.pathway, "status": c.status, "fatwa_status": c.fatwa_status,
                 "org_id": c.org_id},
        "certificate": ({"certificate_no": cert.certificate_no, "issue_date": cert.issue_date,
                         "expiry_date": cert.expiry_date, "scope": cert.scope} if cert else None),
        "workflow_events": [{"event_id": e.event_id, "from_status": e.from_status,
                             "to_status": e.to_status, "action": e.action,
                             "actor_type": e.actor_type, "actor_id": e.actor_id,
                             "payload": e.payload, "prev_hash": e.prev_hash,
                             "row_hash": e.row_hash, "created_at": str(e.created_at)} for e in evs],
        "audit_log": [{"id": r.id, "actor_id": r.actor_id, "actor_role": r.actor_role,
                       "action": r.action, "resource_type": r.resource_type,
                       "resource_id": r.resource_id, "case_id": r.case_id,
                       "meta": r.meta, "created_at": str(r.created_at)} for r in audits],
        "generated_documents": [{"gen_doc_id": d.gen_doc_id, "doc_type": d.doc_type,
                                 "version": d.version, "status": d.status,
                                 "created_by": d.created_by,
                                 "created_at": str(d.created_at)} for d in docs],
    }
    integrity_report = {
        "generated_at": generated_at,
        "workflow_event_chain": wf_report,
        "audit_log_chain": al_report,
        "integrity_ok": bool(wf_report["integrity_ok"] and al_report["integrity_ok"]),
        "disclaimer": _LEGAL_DISCLAIMER_VERIFY,
    }
    # 감사체인이 깨졌다면 그 사유를 숨기지 않고 명시한다(삭제 흔적은 남는 것이 정상 동작).
    al_note = ""
    if al_report.get("deleted_predecessor"):
        al_note = ("\n※ AuditLog 단절 %d건: 선행 감사로그가 삭제되어 prev를 찾을 수 없습니다."
                   " 각 행의 본문 해시는 모두 일치하므로 내용 변조는 없습니다.\n"
                   "   Rantai audit terputus: log sebelumnya telah dihapus (isi tiap baris tetap utuh).\n"
                   % len(al_report["deleted_predecessor"]))
    elif not al_report["integrity_ok"]:
        al_note = "\n※ AuditLog 단절 지점: %s\n" % al_report.get("break_at")
    readme = (
        "GL-HAC 증거 아카이브 번들 (Evidence Bundle)\n"
        "Case: %s (%s)\nGenerated: %s\n\n"
        "포함 파일 · Contents:\n"
        " - evidence.json : 워크플로 전이(해시체인)·접근 감사로그·생성문서 목록\n"
        " - integrity_report.json : 양 해시체인 무결성 재검증 리포트(integrity_ok)\n\n"
        "무결성 · Integrity: WorkflowEvent=%s, AuditLog=%s (HMAC 체인 재계산)\n"
        "  · AuditLog는 케이스 범위 부분집합이라 행 자기무결성을 대조합니다"
        "(체인 연결은 전역이므로 전량 검증은 /admin/audit-log-verify).\n%s\n"
        "%s\n" % (c.company_name or "-", case_id, generated_at,
                  wf_report["integrity_ok"], al_report["integrity_ok"], al_note,
                  _LEGAL_DISCLAIMER_PDF))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("evidence.json", _json.dumps(evidence, ensure_ascii=False, indent=2))
        z.writestr("integrity_report.json", _json.dumps(integrity_report, ensure_ascii=False, indent=2))
        z.writestr("README.txt", readme)
    buf.seek(0)
    fname = "evidence-bundle-%s.zip" % case_id
    _audit(db, user, "evidence.bundle.download", "case", case_id, case_id,
           {"workflow_events": len(evs), "audit_log": len(audits)})
    return StreamingResponse(buf, media_type="application/zip",
                             headers={"Content-Disposition": 'attachment; filename="%s"' % fname})


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
         "[청구] %d건 (미결제 %d)" % (len(invs), sum(1 for i in invs if i.status not in sm.INVOICE_SETTLED)),
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
    # 목록도 상세와 같은 규칙을 쓴다 — 목록에 안 보이는데 상세만 열리면 쓸모가 없고,
    # 목록에 보이는데 상세가 403 이면 더 나쁘다.
    q = db.query(models.CaseApplication)
    if user["role"] not in CERTIFIER_ROLES:
        own = models.CaseApplication.org_id == user["org_id"]
        if user["role"] in ASSIGNED_ROLES:
            ids = _assigned_case_ids(db, user["uid"])
            # 영업으로 데려온 업체의 케이스도 내 목록에 있어야 한다.
            _mine = _my_client_org_ids(db, user["uid"])
            if _mine:
                ids = set(ids) | {c.case_id for c in db.query(models.CaseApplication)
                                  .filter(models.CaseApplication.org_id.in_(_mine)).all()}
            q = q.filter(or_(own, models.CaseApplication.case_id.in_(ids))
                         if ids else own)
        else:
            q = q.filter(own)
    total = q.count()
    rows = (q.order_by(models.CaseApplication.created_at.desc())
            .offset(offset).limit(limit).all())
    # E2/M2: 배정 오디터(ops.auditor_assigned latest-wins) — 목록에 담당자 노출·오디터 KPI 집계용
    _cids = [c.case_id for c in rows]
    assign = _ops_latest_assignment(db, _cids)
    # 수정요청 001 P3 — 계약 큐 처리대기 배지용: 케이스별 계약 상태 배치 조회
    _cts = {ct.case_id: ct.status for ct in
            db.query(models.Contract).filter(models.Contract.case_id.in_(_cids)).all()} if _cids else {}
    items = [{"case_id": c.case_id, "company_name": c.company_name, "status": c.status,
              "pathway": c.pathway, "due_date": c.due_date, "draft_state": c.draft_state,
              "province": _province_of(c.factory_address or c.address),
              "contract_status": _cts.get(c.case_id),
              "auditor_id": (assign.get(c.case_id) or {}).get("auditor_id"),
              "auditor_name": (assign.get(c.case_id) or {}).get("auditor_name")}
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
def list_notifications(lang: str = Query("ko"), user=Depends(auth.get_current_user),
                       db: Session = Depends(get_db)):
    """읽는 사람의 언어로 알림을 돌려준다 — 저장은 한국어라 그대로 주면 인니어 화면에 한글이 뜬다."""
    out = []
    for n in _my_notifs(db, user):
        title, body = notify_text(n, lang)
        out.append({"id": n.notification_id, "case_id": n.case_id, "event_type": n.event_type,
                    "title": title, "body": body, "read": bool(n.read), "status": n.status,
                    "channels": n.channels, "created_at": str(n.created_at)})
    return out


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
def admin_list_notifications(status: str = None, limit: int = 100, lang: str = Query("ko"),
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
    items = [{"id": n.notification_id, "event_type": n.event_type,
              "title": notify_text(n, lang)[0],
              "channels": n.channels, "status": n.status, "attempts": n.attempts,
              "last_error": n.last_error, "role": n.role, "case_id": n.case_id,
              "created_at": str(n.created_at)} for n in rows]
    return {"counts": counts, "total": len(items), "items": items}


def _notify_backfill_params(key, title, body):
    """저장된 한국어 문구를 카탈로그 템플릿과 정확히 맞춰 파라미터를 되뽑는다.

    한 글자라도 어긋나면 포기한다(None). 추측으로 채우면 업체명 자리에 엉뚱한 값이 들어가
    알림이 조용히 틀려진다 — 못 맞추면 한국어 그대로 두는 편이 정직하다."""
    ko = NOTIFY_MSG.get(key, {}).get("ko")
    if not ko:
        return None
    out = {}
    for tmpl, val in ((ko[0], title or ""), (ko[1], body or "")):
        if not tmpl:
            continue
        names = re.findall(r"\{(\w+)\}", tmpl)
        pat = "^" + "".join(
            ("(?P<%s>.+?)" % p[1:-1]) if re.fullmatch(r"\{\w+\}", p) else re.escape(p)
            for p in re.split(r"(\{\w+\})", tmpl)) + "$"
        mm = re.match(pat, val, re.S)
        if not mm:
            return None
        for nm in names:
            got = mm.group(nm)
            if nm in out and out[nm] != got:
                return None          # 제목과 본문의 같은 자리 값이 다르면 잘못 맞춘 것
            out[nm] = got
    return out


@app.post("/admin/notifications/backfill-i18n")
def backfill_notification_i18n(dry_run: bool = True,
                               user=Depends(auth.require_roles("operator", "admin")),
                               db: Session = Depends(get_db)):
    """payload 없이 쌓인 기존 알림에 문구키·파라미터를 되채운다 — 그래야 인니어로 읽힌다.

    저장된 한국어가 카탈로그 문구와 정확히 일치하는 것만 채운다. 못 맞춘 알림은 손대지
    않고 한국어 그대로 둔다. dry_run=true 가 기본 — 무엇이 바뀌는지 먼저 보고 정한다."""
    rows = (db.query(models.Notification)
              .filter(models.Notification.payload.is_(None)).all())
    filled, skipped = [], {}
    for n in rows:
        # 한 event_type 에 문구 변종이 여럿인 경우가 있다(audit_scheduled 는 기본·LPH배정·
        # 가능일제시 셋). 정확히 맞는 변종을 찾을 때까지 후보를 다 시도한다.
        cands = [n.event_type] + sorted(k for k in NOTIFY_MSG
                                        if k.startswith(n.event_type + "."))
        key = params = None
        for cand in cands:
            got = _notify_backfill_params(cand, n.title, n.body)
            if got is not None:
                key, params = cand, got
                break
        if params is None:
            skipped[n.event_type] = skipped.get(n.event_type, 0) + 1
            continue
        filled.append({"id": n.notification_id, "event_type": n.event_type,
                       "key": key, "params": params})
        if not dry_run:
            n.payload = {"key": key, "params": params}
    if not dry_run:
        db.commit()
        _audit(db, user, "notification.backfill_i18n", "notification", None,
               meta={"filled": len(filled)})
        db.commit()
    return {"dry_run": dry_run, "candidates": len(rows), "filled": len(filled),
            "skipped_by_event": skipped, "samples": filled[:8]}


# 프런트가 라벨을 서버에서 받아 쓰게 하는 통로. 같은 표를 프런트가 또 들고 있으면
# 한쪽만 고쳐진다 — 실제로 서류명 12개 중 6개, 증빙 9개 중 7개가 서로 달랐다
# ('할랄 인증서' vs '공급사 할랄 인증서'처럼 뜻이 갈리는 차이도 있었다).
_UI_LABEL_AXES = {
    "enum": ("ENUM", "enum_code"),
    "doc": ("DOC", None),                       # 서류명은 doc_labels 로
    "evidence": ("EVIDENCE", "evidence_code"),
    "severity": ("SEVERITY", "severity"),
    "blocker": ("BLOCKER", "blocker_code"),
    "ws_stage": ("WS_STAGE", "ws_stage"),
    "vault_doc": ("VAULT_DOC", "vault_doc"),
    "org_div": ("ORG_DIV", "org_div"),
    "org_role": ("ORG_ROLE", "org_role"),
    "reg_state": ("REG_STATE", "reg_state"),
    "intake_error": ("INTAKE_ERROR", "intake_error"),
    "gate": ("GATE", "gate_code"),
    "gen_doc": ("GEN_DOC", "gen_doc"),
    "verdict": ("VERDICT", "verdict"),
    "billing_service": ("BILLING_SERVICE", "billing_service"),
    "sjph_evidence": ("SJPH_EVIDENCE", "sjph_evidence"),
    "hpas_element": ("HPAS_ELEMENT", "hpas_element"),
    "material_cat": ("MATERIAL_CAT", "screen_category"),
    "source": ("SOURCE", "screen_source"),
    "extract_field": ("EXTRACT_FIELD", "extract_field"),
    "onsite_item": ("ONSITE_ITEM", "onsite_item"),
    "signer": ("SIGNER", "signer"),
    "wf_phase": ("WF_PHASE", "wf_phase"),
}


@app.get("/i18n/labels")
def i18n_labels(lang: str = Query("ko")):
    """화면 라벨 묶음 — 코드값을 읽는 사람 언어의 문구로. 인증 없이 연다(로그인 화면도 쓴다)."""
    lg = (lang or "ko").lower()
    lg = lg if lg in ("ko", "en", "id") else "ko"
    out = {}
    for name, (axis, action) in _UI_LABEL_AXES.items():
        out[name] = (_dd_mod.doc_labels(lg) if action is None
                     else _dd_mod.code_labels(axis, action, lg))
    return {"lang": lg, "labels": out}


def _org_scoped_models():
    """org_id 를 가진 모델 — 감사로그·조직 본체·케이스는 제외.

    감사로그를 조직과 함께 지우면 HMAC 체인이 끊긴다(케이스 초기화와 같은 이유).
    케이스가 남아 있는 조직은 애초에 삭제 대상이 아니므로 CaseApplication 도 뺀다."""
    out = []
    for name in dir(models):
        m = getattr(models, name)
        t = getattr(m, "__table__", None)
        if t is None or name.startswith("_"):
            continue
        if ("org_id" in t.columns and m is not models.AuditLog
                and m is not models.CaseApplication and m is not models.Org):
            out.append((t.name, m))
    return sorted(out)


@app.patch("/admin/orgs/{org_id}")
def admin_rename_org(org_id: str, body: schemas.OrgRenameReq,
                     user=Depends(auth.require_roles("operator", "admin")),
                     db: Session = Depends(get_db)):
    """조직 이름 변경 — 표기만 바꾼다(org_id·소속·케이스는 그대로).

    이름이 틀리면 화면에서 다른 것으로 읽힌다. 실제로 심사기관 직원들이 속한 조직에
    특정 신청업체 이름이 붙어 있어, 담당 지정 화면에서 개별 업체처럼 보였다. 그대로
    담당을 지정했다면 그 조직의 케이스 전부가 한 컨설턴트에게 귀속될 뻔했다."""
    o = db.get(models.Org, org_id)
    if not o:
        raise HTTPException(404, {"code": "ORG_NOT_FOUND", "org_id": org_id})
    name = (body.name or "").strip()
    if not name:
        raise HTTPException(400, {"code": "NAME_REQUIRED"})
    before = o.name
    o.name = name
    _audit(db, user, "org.renamed", "org", org_id,
           meta={"before": before, "after": name}, commit=False)
    db.commit()
    return {"org_id": org_id, "name": name, "before": before}


@app.post("/admin/orgs/purge-orphans")
def admin_purge_orphan_orgs(body: dict = None, user=Depends(auth.require_roles("admin")),
                            db: Session = Depends(get_db)):
    """케이스도 사용자도 없는 조직을 정리한다 — 테스트·리허설이 남긴 빈 껍데기.

    지우지 않는 것
      · 케이스가 하나라도 있는 조직 — 업무 데이터가 딸려 있다.
      · 사용자가 있는 조직 — 지우면 그 계정이 소속 없는 상태가 된다. 계정을 먼저
        정리할지는 사람이 판단할 일이다.
      · 감사로그 — 조직이 사라져도 '있었다는 사실'은 남아야 한다.

    기본은 dry_run 미리보기. 실삭제는 confirm='PURGE' 와 사유가 있어야 한다."""
    if not auth.dev_mode():
        raise HTTPException(403, {"code": "PURGE_DISABLED", "hint": "GLHAC_DEV=1 에서만 허용"})
    b = body or {}
    dry = b.get("dry_run", True)
    only = set(b.get("org_ids") or [])          # 주면 그 조직만 대상으로 좁힌다

    with_cases = {r[0] for r in db.query(models.CaseApplication.org_id).distinct().all() if r[0]}
    with_users = {r[0] for r in db.query(models.User.org_id).distinct().all() if r[0]}
    scoped = _org_scoped_models()

    plan, skipped = [], []
    for o in db.query(models.Org).all():
        if only and o.org_id not in only:
            continue
        if o.org_id in with_cases:
            skipped.append({"org_id": o.org_id, "name": o.name, "reason": "케이스 있음"})
            continue
        if o.org_id in with_users:
            skipped.append({"org_id": o.org_id, "name": o.name, "reason": "사용자 있음"})
            continue
        counts = {}
        for tname, m in scoped:
            n = db.query(m).filter(m.org_id == o.org_id).count()
            if n:
                counts[tname] = n
        plan.append({"org_id": o.org_id, "name": o.name, "rows": counts,
                     "audit_log_preserved": db.query(models.AuditLog)
                     .filter(models.AuditLog.org_id == o.org_id).count()})

    out = {"dry_run": bool(dry), "delete_count": len(plan), "cases": plan,
           "skipped": skipped,
           "note": "audit_log는 삭제하지 않는다(체인 보존). 삭제 사실은 감사로그에 남는다."}
    if dry:
        return out
    if b.get("confirm") != "PURGE":
        raise HTTPException(400, {"code": "CONFIRM_REQUIRED", "hint": "confirm='PURGE'"})
    if not (b.get("reason") or "").strip():
        raise HTTPException(400, {"code": "REASON_REQUIRED", "hint": "사유는 감사로그에 남는다"})
    if b.get("expect_delete") is not None and b["expect_delete"] != len(plan):
        raise HTTPException(409, {"code": "COUNT_MISMATCH", "expected": b["expect_delete"],
                                  "actual": len(plan),
                                  "hint": "dry_run 결과와 다르다 — 데이터가 그새 바뀌었다"})

    # 삭제 '전에' 기록한다 — 도중에 실패해도 착수 사실은 남아야 한다.
    _audit(db, user, "admin.orgs.purge_orphans", "system", None, None,
           {"reason": b.get("reason"), "orgs": [i["org_id"] for i in plan]}, commit=False)
    db.commit()
    for item in plan:
        oid = item["org_id"]
        for tname, m in scoped:
            db.query(m).filter(m.org_id == oid).delete(synchronize_session=False)
        db.query(models.Org).filter(models.Org.org_id == oid).delete(
            synchronize_session=False)
    db.commit()
    out["deleted"] = [i["org_id"] for i in plan]
    return out


# ── 컨설턴트(영업) ────────────────────────────────────────────────────────
# 컨설턴트는 오디터와 성격이 다르다. 오디터는 심사기관이 케이스마다 배정하지만,
# 컨설턴트는 영업으로 업체를 데려온 사람이라 업체가 들어올 때 이미 관계가 있다.
# 그래서 관계는 케이스가 아니라 조직(Org.consultant_id)에 붙는다. 이 관계가
# 접근 권한이자 수수료 정산의 근거가 된다.

def _consultant_public(u, prof):
    """화면·정산에 쓰는 컨설턴트 표기. 계좌·사업자번호는 운영자만 본다."""
    return {"consultant_id": u.user_id, "username": u.username,
            "display_name": (prof.display_name if prof else None) or u.username,
            "company_name": prof.company_name if prof else None,
            "status": (prof.status if prof else "active")}


def _consultant_full(u, prof):
    if not prof:
        return {**_consultant_public(u, None), "profile": None}
    return {**_consultant_public(u, prof),
            "profile": {"biz_reg_no": prof.biz_reg_no, "phone": prof.phone,
                        "email": prof.email, "address": prof.address,
                        "bank_name": prof.bank_name, "bank_account": prof.bank_account,
                        "account_holder": prof.account_holder,
                        "commission_rate": prof.commission_rate,
                        "contract_note": prof.contract_note}}


def _resolve_consultant(db, ident):
    """user_id 또는 username 으로 컨설턴트를 찾는다."""
    u = db.get(models.User, ident) if ident else None
    if not u:
        u = db.query(models.User).filter(models.User.username == ident).first()
    if not u or u.role != "consultant":
        raise HTTPException(404, {"code": "CONSULTANT_NOT_FOUND", "id": ident})
    return u


@app.post("/admin/consultants")
def create_consultant(body: schemas.ConsultantCreate,
                      user=Depends(auth.require_roles("operator", "admin")),
                      db: Session = Depends(get_db)):
    """컨설턴트 계정 등록 — 영업 담당자를 시스템에 들인다(운영자가 만든다).

    자체 가입을 열지 않는 이유: 컨설턴트는 남의 업체 정보를 보고 수수료를 받는
    자리라, 누가 컨설턴트인지는 인증기관이 정해야 한다."""
    if db.query(models.User).filter_by(username=body.username).first():
        raise HTTPException(409, {"code": "DUPLICATE_ACCOUNT"})
    u = models.User(username=body.username, password_hash=auth.hash_pw(body.password),
                    role="consultant", org_id=user.get("org_id") or "org_demo")
    db.add(u)
    db.flush()
    prof = models.ConsultantProfile(
        consultant_id=u.user_id, display_name=body.display_name or body.username,
        company_name=body.company_name, biz_reg_no=body.biz_reg_no, phone=body.phone,
        email=body.email, address=body.address, bank_name=body.bank_name,
        bank_account=body.bank_account, account_holder=body.account_holder,
        commission_rate=body.commission_rate, contract_note=body.contract_note)
    db.add(prof)
    _audit(db, user, "consultant.created", "consultant", u.user_id,
           meta={"username": u.username}, commit=False)
    db.commit()
    return _consultant_full(u, prof)


@app.get("/admin/consultants")
def list_consultants(user=Depends(auth.require_roles("operator", "admin")),
                     db: Session = Depends(get_db)):
    """컨설턴트 목록 — 유치 업체 수와 실적 근거를 함께 준다."""
    profs = {p.consultant_id: p for p in db.query(models.ConsultantProfile).all()}
    orgs = {}
    for o in db.query(models.Org).filter(models.Org.consultant_id.isnot(None)).all():
        orgs.setdefault(o.consultant_id, []).append(o)
    out = []
    for u in db.query(models.User).filter_by(role="consultant").all():
        mine = orgs.get(u.user_id) or []
        out.append({**_consultant_full(u, profs.get(u.user_id)),
                    "client_count": len(mine),
                    "clients": [{"org_id": o.org_id, "name": o.name} for o in mine[:20]]})
    return {"items": sorted(out, key=lambda x: -x["client_count"])}


@app.get("/admin/consultant-clients")
def list_consultant_clients(user=Depends(auth.require_roles("operator", "admin")),
                            db: Session = Depends(get_db)):
    """업체별 담당 컨설턴트 현황 — 누가 어느 업체를 데려왔는지, 담당 없는 업체는 어디인지.

    /admin/orgs 를 쓰지 않는 이유: 그쪽은 관리자 전용인데 이 화면은 최고운영자도 쓴다.
    필요한 것도 다르다 — 여기서는 케이스 수와 담당 컨설턴트만 보면 된다."""
    prof = {p.consultant_id: p for p in db.query(models.ConsultantProfile).all()}
    unames = {u.user_id: u.username for u in db.query(models.User).all()}
    cases = {}
    for c in db.query(models.CaseApplication).all():
        cases[c.org_id] = cases.get(c.org_id, 0) + 1
    out = []
    for o in db.query(models.Org).all():
        pr = prof.get(o.consultant_id) if o.consultant_id else None
        out.append({
            "org_id": o.org_id, "name": o.name, "cases": cases.get(o.org_id, 0),
            "consultant_id": o.consultant_id,
            "consultant_name": (((pr.display_name if pr and pr.display_name else None)
                                 or unames.get(o.consultant_id)) if o.consultant_id else None),
            "linked_at": str(o.consultant_linked_at or "")})
    # 담당 없는 업체를 위로 — 여기가 조치가 필요한 곳이다.
    out.sort(key=lambda x: (x["consultant_id"] is not None, -(x["cases"] or 0),
                            (x["name"] or "")))
    return {"items": out}


@app.delete("/admin/consultants/{consultant_id}")
def delete_consultant(consultant_id: str, force: bool = Query(False),
                      user=Depends(auth.require_roles("operator", "admin")),
                      db: Session = Depends(get_db)):
    """컨설턴트 삭제 — 담당 업체가 남아 있으면 거부한다.

    담당을 먼저 다른 사람에게 넘기거나 해제해야 한다. 그러지 않고 지우면 업체는
    담당 없는 상태로 남는데, 화면에서는 사라진 사람 이름만 보이게 된다.

    지급 기록은 '지급했다는 사실'이라 함부로 지우지 않는다. 다만 테스트·리허설 정리
    (force, GLHAC_DEV 전용)에서는 근거 케이스가 이미 사라졌으므로 함께 지운다 —
    감사로그에는 지급 사실이 남아 있다."""
    u = _resolve_consultant(db, consultant_id)
    uid, uname = u.user_id, u.username     # 삭제 후에는 객체 속성을 읽을 수 없다
    clients = db.query(models.Org).filter_by(consultant_id=uid).count()
    if clients:
        raise HTTPException(409, {"code": "HAS_CLIENTS", "clients": clients,
                                  "hint": "담당 업체를 먼저 넘기거나 해제해야 한다"})
    payouts = db.query(models.ConsultantPayout).filter_by(consultant_id=uid).count()
    if payouts and not force:
        raise HTTPException(409, {"code": "HAS_PAYOUTS", "payouts": payouts,
                                  "hint": "정산 이력이 있다 — 지우려면 force=true"})
    if force and not auth.dev_mode():
        raise HTTPException(403, {"code": "FORCE_DISABLED", "hint": "GLHAC_DEV=1 에서만 허용"})
    _audit(db, user, "consultant.deleted", "consultant", uid,
           meta={"username": uname, "payouts": payouts, "force": bool(force)},
           commit=False)
    db.query(models.ConsultantInvite).filter_by(consultant_id=uid).delete(
        synchronize_session=False)
    if force:
        db.query(models.ConsultantPayout).filter_by(consultant_id=uid).delete(
            synchronize_session=False)
    db.query(models.ConsultantProfile).filter_by(consultant_id=uid).delete(
        synchronize_session=False)
    db.query(models.User).filter_by(user_id=uid).delete(synchronize_session=False)
    db.commit()
    return {"ok": True, "deleted": uname}


@app.get("/consultant/me")
def get_my_consultant_profile(user=Depends(auth.require_roles("consultant")),
                              db: Session = Depends(get_db)):
    u = db.get(models.User, user["uid"])
    prof = db.get(models.ConsultantProfile, user["uid"])
    orgs = db.query(models.Org).filter_by(consultant_id=user["uid"]).all()
    return {**_consultant_full(u, prof),
            "clients": [{"org_id": o.org_id, "name": o.name,
                         "linked_at": str(o.consultant_linked_at or "")} for o in orgs]}


@app.put("/consultant/me")
def update_my_consultant_profile(body: schemas.ConsultantProfileReq,
                                 user=Depends(auth.require_roles("consultant")),
                                 db: Session = Depends(get_db)):
    """본인 정보 수정 — 수수료율·상태는 손대지 못한다(돈과 자격은 인증기관이 정한다)."""
    prof = db.get(models.ConsultantProfile, user["uid"])
    if not prof:
        prof = models.ConsultantProfile(consultant_id=user["uid"])
        db.add(prof)
    for f in ("display_name", "company_name", "biz_reg_no", "phone", "email", "address",
              "bank_name", "bank_account", "account_holder"):
        v = getattr(body, f, None)
        if v is not None:
            setattr(prof, f, v)
    prof.updated_at = datetime.utcnow()
    _audit(db, user, "consultant.profile.updated", "consultant", user["uid"], commit=False)
    db.commit()
    return _consultant_full(db.get(models.User, user["uid"]), prof)


@app.put("/admin/consultants/{consultant_id}")
def update_consultant(consultant_id: str, body: schemas.ConsultantProfileReq,
                      user=Depends(auth.require_roles("operator", "admin")),
                      db: Session = Depends(get_db)):
    """운영자의 컨설턴트 정보 수정 — 수수료율·활성 상태를 여기서 정한다."""
    u = _resolve_consultant(db, consultant_id)
    prof = db.get(models.ConsultantProfile, u.user_id)
    if not prof:
        prof = models.ConsultantProfile(consultant_id=u.user_id)
        db.add(prof)
    changed = {}
    for f in ("display_name", "company_name", "biz_reg_no", "phone", "email", "address",
              "bank_name", "bank_account", "account_holder", "commission_rate",
              "contract_note", "status"):
        v = getattr(body, f, None)
        if v is not None:
            if f in ("commission_rate", "status"):
                changed[f] = v
            setattr(prof, f, v)
    prof.updated_at = datetime.utcnow()
    _audit(db, user, "consultant.updated", "consultant", u.user_id,
           meta=changed or None, commit=False)
    db.commit()
    return _consultant_full(u, prof)


# ── 초대 코드 ─────────────────────────────────────────────────────────────
def _new_invite_code(db):
    """읽어 부르기 쉬운 코드 — 혼동되는 글자(0/O, 1/I)는 뺀다."""
    import secrets
    abc = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    for _ in range(20):
        code = "-".join("".join(secrets.choice(abc) for _ in range(4)) for _ in range(2))
        if not db.query(models.ConsultantInvite).filter_by(code=code).first():
            return code
    raise HTTPException(500, {"code": "CODE_GENERATION_FAILED"})


@app.post("/consultant/invites")
def create_invite(body: schemas.InviteCreate,
                  user=Depends(auth.require_roles("consultant", "operator", "admin")),
                  db: Session = Depends(get_db)):
    """초대 코드 발급 — 영업한 업체에 건네면 그 업체가 내 담당으로 들어온다."""
    from datetime import timedelta
    days = int(body.expires_days or 30)
    inv = models.ConsultantInvite(
        code=_new_invite_code(db), consultant_id=user["uid"],
        company_name=body.company_name, note=body.note,
        max_uses=max(1, int(body.max_uses or 1)),
        expires_at=datetime.utcnow() + timedelta(days=max(1, min(365, days))))
    db.add(inv)
    _audit(db, user, "consultant.invite.created", "invite", inv.invite_id,
           meta={"code": inv.code, "company_name": inv.company_name}, commit=False)
    db.commit()
    return {"invite_id": inv.invite_id, "code": inv.code,
            "company_name": inv.company_name, "max_uses": inv.max_uses,
            "used_count": inv.used_count, "expires_at": str(inv.expires_at)}


@app.get("/consultant/invites")
def list_invites(user=Depends(auth.require_roles("consultant", "operator", "admin")),
                 db: Session = Depends(get_db)):
    q = db.query(models.ConsultantInvite)
    if user["role"] == "consultant":
        q = q.filter_by(consultant_id=user["uid"])
    rows = q.order_by(models.ConsultantInvite.created_at.desc()).limit(200).all()
    now = datetime.utcnow()
    return {"items": [{
        "invite_id": i.invite_id, "code": i.code, "company_name": i.company_name,
        "note": i.note, "max_uses": i.max_uses, "used_count": i.used_count,
        "expires_at": str(i.expires_at or ""), "revoked": bool(i.revoked_at),
        "usable": bool(not i.revoked_at and i.used_count < i.max_uses
                       and (not i.expires_at or i.expires_at > now)),
        "created_at": str(i.created_at)} for i in rows]}


@app.post("/consultant/invites/{invite_id}/revoke")
def revoke_invite(invite_id: str,
                  user=Depends(auth.require_roles("consultant", "operator", "admin")),
                  db: Session = Depends(get_db)):
    """코드 회수 — 잘못 나간 코드로 남이 들어오면 관계와 수수료가 틀어진다."""
    inv = db.get(models.ConsultantInvite, invite_id)
    if not inv:
        raise HTTPException(404, {"code": "INVITE_NOT_FOUND"})
    if user["role"] == "consultant" and inv.consultant_id != user["uid"]:
        raise HTTPException(403, {"code": "NOT_YOUR_INVITE"})
    inv.revoked_at = datetime.utcnow()
    _audit(db, user, "consultant.invite.revoked", "invite", invite_id, commit=False)
    db.commit()
    return {"ok": True, "revoked_at": str(inv.revoked_at)}


# ── 영업자(컨설턴트) 개별 QR ──────────────────────────────────────────────
#
# 영업자는 가입 시 자기 코드를 한 번 발급받고, 그 코드가 박힌 QR 을 명함·자료에 쓴다.
# 고객이 찍으면 홈페이지로 가고, 거기서 가입하면 '누가 데려왔는지'가 남는다
# (기존 consultant_invite 구조를 그대로 쓴다 — 수수료 근거가 이미 그 위에 서 있다).
HOME_BASE_URL = os.environ.get("GLHAC_HOME_URL", "https://glhac.com")


def _consultant_primary_invite(db, consultant_id, company_name=None):
    """영업자의 대표 초대코드 — 없으면 만든다(가입 시 1회 발급, 이후 계속 같은 코드).

    명함에 박아 쓰는 코드라 만료·사용횟수 제한을 두지 않는다. 기간 한정 코드가 필요하면
    기존 /consultant/invites 로 따로 발급한다."""
    inv = (db.query(models.ConsultantInvite)
             .filter_by(consultant_id=consultant_id, is_primary=True)
             .order_by(models.ConsultantInvite.created_at).first())
    if inv:
        return inv
    inv = models.ConsultantInvite(
        code=_new_invite_code(db), consultant_id=consultant_id,
        company_name=company_name, note="가입 시 자동 발급(대표 QR)",
        max_uses=10 ** 9, is_primary=True, expires_at=None)
    db.add(inv)
    db.flush()
    return inv


# ── 홈페이지 상담 게시판(문의하기) ────────────────────────────────────────
#
# 가입 없이 남기고 비밀번호로 다시 본다. **모든 글은 비공개** — 목록조차 직원만 본다.
# 어느 업체가 무슨 원료로 고민 중인지가 경쟁사에 보이면 안 된다.
_BOARD_RL: dict = {}          # ip_hash → [작성시각...]
# 같은 IP 에서 한 시간에 몇 건까지. 운영 중 조정할 일이 생긴다(전시회처럼 한 회선에서
# 여러 업체가 쓰는 자리도 있다). 0 이면 제한 없음 — 테스트가 그렇게 쓴다.
_BOARD_RL_MAX = int(os.environ.get("GLHAC_BOARD_RATE_MAX", "3"))
_BOARD_RL_WINDOW = int(os.environ.get("GLHAC_BOARD_RATE_WINDOW", "3600"))
_BOARD_STAFF = ("consultant", "auditor", "operator", "admin", "fatwa_liaison")


def _ip_hash(request: Request) -> str:
    """도배 차단용 식별자. 원문 IP 는 저장하지 않는다(불필요한 개인정보)."""
    ip = (request.headers.get("x-forwarded-for", "").split(",")[0].strip()
          or (request.client.host if request.client else "?"))
    return hashlib.sha256((ip + auth.SECRET.decode("utf-8", "ignore")).encode()).hexdigest()[:32]


def _board_rate_limited(iph: str) -> bool:
    if _BOARD_RL_MAX <= 0:
        return False
    now = time.time()
    hits = [t for t in _BOARD_RL.get(iph, []) if now - t < _BOARD_RL_WINDOW]
    _BOARD_RL[iph] = hits
    return len(hits) >= _BOARD_RL_MAX


@app.post("/board/posts")
def board_create(body: schemas.BoardPostCreate, request: Request,
                 db: Session = Depends(get_db)):
    """무가입 문의 등록. 로그인 없이 열려 있으므로 도배·봇 대비가 필요하다."""
    iph = _ip_hash(request)
    if _board_rate_limited(iph):
        raise HTTPException(429, {"code": "TOO_MANY_POSTS",
                                  "message": "문의가 너무 잦습니다. 잠시 후 다시 시도해 주세요."})
    # QR 로 들어온 문의는 그 영업자 건으로 귀속한다
    ref = (body.ref or "").strip().upper() or None
    consultant_id = None
    if ref:
        inv = db.query(models.ConsultantInvite).filter_by(code=ref).first()
        if inv and not inv.revoked_at:
            consultant_id = inv.consultant_id
        else:
            ref = None                      # 없는 코드는 조용히 버린다(가입을 막지 않는다)
    p = models.BoardPost(
        title=body.title, body=body.body, author_name=body.author_name,
        contact=body.contact, password_hash=auth.hash_pw(body.password),
        ref_code=ref, consultant_id=consultant_id, ip_hash=iph)
    db.add(p)
    _BOARD_RL.setdefault(iph, []).append(time.time())
    db.commit()
    # 글번호는 알려 준다 — 나중에 자기 글을 찾을 때 쓴다
    return {"post_id": p.post_id, "status": p.status,
            "assigned": bool(consultant_id),
            "message": "접수되었습니다. 글번호와 비밀번호로 다시 확인하실 수 있습니다."}


def _post_view(p, replies):
    return {"post_id": p.post_id, "title": p.title, "body": p.body,
            "author_name": p.author_name, "contact": p.contact,
            "status": p.status, "created_at": str(p.created_at),
            "replies": [{"reply_id": r.reply_id, "body": r.body,
                         "author_role": r.author_role,
                         "created_at": str(r.created_at)} for r in replies]}


@app.post("/board/posts/{post_id}/open")
def board_open(post_id: str, body: schemas.BoardPostOpen, request: Request,
               db: Session = Depends(get_db)):
    """글쓴이가 비밀번호로 자기 글을 연다."""
    iph = _ip_hash(request)
    if auth.rate_limited("board:" + iph):      # 비번 대입 차단(계정 로그인과 같은 장치)
        raise HTTPException(429, {"code": "TOO_MANY_ATTEMPTS"})
    p = db.get(models.BoardPost, post_id)
    if not p or not auth.verify_pw(body.password, p.password_hash):
        auth.record_attempt("board:" + iph)
        # 글이 없는 것과 비번이 틀린 것을 구분해 주지 않는다 — 글번호 탐색을 막는다
        raise HTTPException(401, {"code": "BAD_POST_CREDENTIALS",
                                  "message": "글번호 또는 비밀번호가 맞지 않습니다."})
    auth.clear_attempts("board:" + iph)
    replies = (db.query(models.BoardReply).filter_by(post_id=post_id)
                 .order_by(models.BoardReply.created_at).all())
    return _post_view(p, replies)


@app.get("/board/posts")
def board_list(status: str = None, mine: bool = False,
               user=Depends(auth.require_roles(*_BOARD_STAFF)),
               db: Session = Depends(get_db)):
    """직원용 목록 — 익명 글이라도 담당자는 봐야 답변한다."""
    q = db.query(models.BoardPost)
    if status:
        q = q.filter(models.BoardPost.status == status)
    if mine and user["role"] == "consultant":
        q = q.filter(models.BoardPost.consultant_id == user["uid"])
    rows = q.order_by(models.BoardPost.created_at.desc()).limit(300).all()
    ids = [r.post_id for r in rows]
    cnt = {}
    if ids:
        for pid, n in (db.query(models.BoardReply.post_id, func.count())
                         .filter(models.BoardReply.post_id.in_(ids))
                         .group_by(models.BoardReply.post_id)):
            cnt[pid] = n
    return [{"post_id": r.post_id, "title": r.title, "author_name": r.author_name,
             "contact": r.contact, "status": r.status, "ref_code": r.ref_code,
             "consultant_id": r.consultant_id, "reply_count": cnt.get(r.post_id, 0),
             "created_at": str(r.created_at)} for r in rows]


@app.get("/board/posts/{post_id}")
def board_detail(post_id: str, user=Depends(auth.require_roles(*_BOARD_STAFF)),
                 db: Session = Depends(get_db)):
    p = db.get(models.BoardPost, post_id)
    if not p:
        raise HTTPException(404, {"code": "POST_NOT_FOUND"})
    replies = (db.query(models.BoardReply).filter_by(post_id=post_id)
                 .order_by(models.BoardReply.created_at).all())
    return _post_view(p, replies)


@app.post("/board/posts/{post_id}/replies")
def board_reply(post_id: str, body: schemas.BoardReplyCreate,
                user=Depends(auth.require_roles(*_BOARD_STAFF)),
                db: Session = Depends(get_db)):
    """답변 — 로그인한 직원만. 답변이 달리면 상태를 answered 로 올린다."""
    p = db.get(models.BoardPost, post_id)
    if not p:
        raise HTTPException(404, {"code": "POST_NOT_FOUND"})
    r = models.BoardReply(post_id=post_id, author_id=user["uid"],
                          author_role=user["role"], body=body.body)
    db.add(r)
    if p.status == "open":
        p.status = "answered"
    _audit(db, user, "board.reply", "board_post", post_id, commit=False)
    db.commit()
    return {"reply_id": r.reply_id, "status": p.status}


@app.patch("/board/posts/{post_id}/status")
def board_status(post_id: str, body: schemas.BoardStatusReq,
                 user=Depends(auth.require_roles(*_BOARD_STAFF)),
                 db: Session = Depends(get_db)):
    if body.status not in ("open", "answered", "closed"):
        raise HTTPException(422, {"code": "BAD_STATUS"})
    p = db.get(models.BoardPost, post_id)
    if not p:
        raise HTTPException(404, {"code": "POST_NOT_FOUND"})
    p.status = body.status
    _audit(db, user, "board.status", "board_post", post_id,
           meta={"status": body.status}, commit=False)
    db.commit()
    return {"post_id": post_id, "status": p.status}


@app.get("/consultant/qr")
def consultant_qr(fmt: str = "svg", user=Depends(auth.require_roles("consultant")),
                  db: Session = Depends(get_db)):
    """내 QR — 스캔하면 홈페이지로 가고 내 담당으로 붙는다."""
    inv = _consultant_primary_invite(db, user["uid"])
    db.commit()
    url = "%s/?ref=%s" % (HOME_BASE_URL.rstrip("/"), inv.code)
    import io

    import segno
    qr = segno.make(url, error="m")
    buf = io.BytesIO()
    if fmt == "png":
        qr.save(buf, kind="png", scale=8, border=2, dark="#0e5548")
        return Response(buf.getvalue(), media_type="image/png")
    qr.save(buf, kind="svg", scale=8, border=2, dark="#0e5548")
    return Response(buf.getvalue(), media_type="image/svg+xml")


@app.get("/consultant/qr/info")
def consultant_qr_info(user=Depends(auth.require_roles("consultant")),
                       db: Session = Depends(get_db)):
    """QR 이 가리키는 주소와 코드 — 화면이 같이 보여 준다."""
    inv = _consultant_primary_invite(db, user["uid"])
    db.commit()
    return {"code": inv.code,
            "url": "%s/?ref=%s" % (HOME_BASE_URL.rstrip("/"), inv.code),
            "qr_svg": "/consultant/qr?fmt=svg", "qr_png": "/consultant/qr?fmt=png",
            "used_count": inv.used_count}


@app.get("/invites/{code}/check")
def check_invite(code: str, db: Session = Depends(get_db)):
    """가입 화면이 코드를 확인한다 — 누구 담당으로 들어가는지 미리 보여준다.

    인증 없이 연다(가입 전이다). 컨설턴트의 표기명만 돌려주고 연락처·계좌는 주지 않는다."""
    inv = db.query(models.ConsultantInvite).filter_by(code=(code or "").strip().upper()).first()
    if not inv:
        return {"valid": False, "reason": "NOT_FOUND"}
    if inv.revoked_at:
        return {"valid": False, "reason": "REVOKED"}
    if inv.expires_at and inv.expires_at <= datetime.utcnow():
        return {"valid": False, "reason": "EXPIRED"}
    if inv.used_count >= inv.max_uses:
        return {"valid": False, "reason": "USED_UP"}
    u = db.get(models.User, inv.consultant_id)
    prof = db.get(models.ConsultantProfile, inv.consultant_id)
    return {"valid": True, "company_name": inv.company_name,
            "consultant": (prof.display_name if prof and prof.display_name
                           else (u.username if u else None))}


# ── 유치 관계 ─────────────────────────────────────────────────────────────
@app.put("/admin/orgs/{org_id}/consultant")
def set_org_consultant(org_id: str, body: schemas.OrgConsultantReq,
                       user=Depends(auth.require_roles("operator", "admin")),
                       db: Session = Depends(get_db)):
    """업체의 담당 컨설턴트 지정·변경 — 초대 코드 없이 들어온 업체나 담당 교체용."""
    o = db.get(models.Org, org_id)
    if not o:
        raise HTTPException(404, {"code": "ORG_NOT_FOUND"})
    c = _resolve_consultant(db, body.consultant_id)
    before = o.consultant_id
    o.consultant_id = c.user_id
    o.consultant_linked_at = datetime.utcnow()
    _audit(db, user, "org.consultant.set", "org", org_id,
           meta={"before": before, "after": c.user_id, "reason": body.reason}, commit=False)
    db.commit()
    return {"org_id": org_id, "consultant_id": c.user_id, "username": c.username,
            "linked_at": str(o.consultant_linked_at)}


@app.delete("/admin/orgs/{org_id}/consultant")
def clear_org_consultant(org_id: str,
                         user=Depends(auth.require_roles("operator", "admin")),
                         db: Session = Depends(get_db)):
    o = db.get(models.Org, org_id)
    if not o:
        raise HTTPException(404, {"code": "ORG_NOT_FOUND"})
    before = o.consultant_id
    o.consultant_id = None
    o.consultant_linked_at = None
    _audit(db, user, "org.consultant.cleared", "org", org_id,
           meta={"before": before}, commit=False)
    db.commit()
    return {"ok": True}


# ── 실적·수수료 ───────────────────────────────────────────────────────────
def _commission_base(db, consultant_id, date_from=None, date_to=None):
    """실적 근거 — 담당 업체 케이스의 '결제 완료' 인보이스.

    수수료를 미리 계산해 저장하지 않는다. 규칙(요율)이 바뀌면 과거까지 흔들리기 때문에,
    근거가 되는 인보이스를 그때그때 모아 계산한다. 지급했다는 사실만 따로 기록한다."""
    org_ids = [o.org_id for o in
               db.query(models.Org).filter_by(consultant_id=consultant_id).all()]
    if not org_ids:
        return [], 0.0
    cases = {c.case_id: c for c in db.query(models.CaseApplication)
             .filter(models.CaseApplication.org_id.in_(org_ids)).all()}
    if not cases:
        return [], 0.0
    q = db.query(models.Invoice).filter(models.Invoice.case_id.in_(list(cases)))
    rows, total = [], 0.0
    for iv in q.all():
        if (iv.status or "") != "paid":
            continue
        if date_from and str(iv.created_at or "")[:10] < date_from:
            continue
        if date_to and str(iv.created_at or "")[:10] > date_to:
            continue
        amt = float(iv.amount or 0)      # 부가세 제외 공급가 기준
        total += amt
        c = cases.get(iv.case_id)
        rows.append({"invoice_id": iv.invoice_id, "invoice_no": iv.invoice_no,
                     "case_id": iv.case_id,
                     "company_name": c.company_name if c else None,
                     "service_type": iv.service_type, "amount": amt,
                     "created_at": str(iv.created_at or "")})
    return rows, total


@app.get("/consultant/commission")
def my_commission(date_from: str = Query(None), date_to: str = Query(None),
                  user=Depends(auth.require_roles("consultant")),
                  db: Session = Depends(get_db)):
    return _commission_report(db, user["uid"], date_from, date_to)


@app.get("/admin/consultants/{consultant_id}/commission")
def consultant_commission(consultant_id: str, date_from: str = Query(None),
                          date_to: str = Query(None),
                          user=Depends(auth.require_roles("operator", "admin")),
                          db: Session = Depends(get_db)):
    return _commission_report(db, _resolve_consultant(db, consultant_id).user_id,
                              date_from, date_to)


def _commission_report(db, consultant_id, date_from, date_to):
    prof = db.get(models.ConsultantProfile, consultant_id)
    rows, base = _commission_base(db, consultant_id, date_from, date_to)
    rate = prof.commission_rate if prof and prof.commission_rate is not None else None
    paid = [{"payout_id": p.payout_id, "period": "%s ~ %s" % (p.period_from, p.period_to),
             "amount": p.amount, "status": p.status, "paid_at": str(p.paid_at or "")}
            for p in db.query(models.ConsultantPayout)
            .filter_by(consultant_id=consultant_id)
            .order_by(models.ConsultantPayout.created_at.desc()).limit(50).all()]
    return {"consultant_id": consultant_id,
            "period": {"from": date_from, "to": date_to},
            "client_count": db.query(models.Org).filter_by(
                consultant_id=consultant_id).count(),
            "base_amount": round(base, 2), "invoice_count": len(rows),
            "commission_rate": rate,
            # 요율이 없으면 금액을 지어내지 않는다 — 운영자가 정해야 나온다.
            "commission_amount": (round(base * rate / 100.0, 2)
                                  if rate is not None else None),
            "invoices": rows, "payouts": paid}


@app.post("/admin/consultants/payouts")
def create_payout(body: schemas.PayoutCreate,
                  user=Depends(auth.require_roles("operator", "admin")),
                  db: Session = Depends(get_db)):
    """수수료 지급 기록 — 계산이 아니라 '지급했다'는 사실을 남긴다."""
    c = _resolve_consultant(db, body.consultant_id)
    prof = db.get(models.ConsultantProfile, c.user_id)
    rate = prof.commission_rate if prof else None
    if rate is None:
        raise HTTPException(400, {"code": "RATE_NOT_SET",
                                  "hint": "수수료율을 먼저 설정해야 지급액을 낼 수 있다"})
    rows, base = _commission_base(db, c.user_id, body.period_from, body.period_to)
    p = models.ConsultantPayout(
        consultant_id=c.user_id, period_from=body.period_from, period_to=body.period_to,
        base_amount=round(base, 2), rate=rate, amount=round(base * rate / 100.0, 2),
        invoice_ids=[r["invoice_id"] for r in rows], note=body.note,
        created_by=user["uid"])
    db.add(p)
    _audit(db, user, "consultant.payout.created", "consultant", c.user_id,
           meta={"period": [body.period_from, body.period_to], "amount": p.amount},
           commit=False)
    db.commit()
    return {"payout_id": p.payout_id, "base_amount": p.base_amount, "rate": p.rate,
            "amount": p.amount, "invoice_count": len(rows), "status": p.status}


@app.post("/admin/consultants/payouts/{payout_id}/paid")
def mark_payout_paid(payout_id: str,
                     user=Depends(auth.require_roles("operator", "admin")),
                     db: Session = Depends(get_db)):
    p = db.get(models.ConsultantPayout, payout_id)
    if not p:
        raise HTTPException(404, {"code": "PAYOUT_NOT_FOUND"})
    p.status = "paid"
    p.paid_at = datetime.utcnow()
    _audit(db, user, "consultant.payout.paid", "consultant", p.consultant_id,
           meta={"payout_id": payout_id, "amount": p.amount}, commit=False)
    db.commit()
    return {"ok": True, "paid_at": str(p.paid_at)}


@app.get("/admin/notify-channels")
def admin_notify_channels(user=Depends(auth.require_roles("operator"))):
    """알림봇 채널 설정·구현 상태(읽기전용, 스키마 무변경).

    크리덴셜 '값'은 노출하지 않고 존재여부(bool)만 반환한다. 프런트 알림봇 바에서
    채널별 연결됨/미설정/스텁 배지를 정직하게 표시하기 위한 최소 조회 엔드포인트.
    """
    from . import notify as _nt
    return {"channels": _nt.channel_status()}


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
    # 조직(org) 회사 프로필 역상속 — 신청 직접 진입 시 회사정보 자동 프리필
    org_row = db.get(models.Org, org)
    oext = dict(org_row.profile_ext or {}) if org_row else {}
    c = models.CaseApplication(
        org_id=org, is_msme=bool(body.is_msme),
        company_name=body.company_name or oext.get("company_name") or (org_row.name if org_row else None),
        profile_ext=(oext or None))
    # 회사 프로필 상속 — 단, 상호가 다르면 '그 회사를 특정하는 값'은 물려받지 않는다.
    # org=회사가 전제지만 실제로는 한 org 에 여러 회사가 들어있다. 그대로 상속하면 인도네시아
    # 업체 신청서에 강원도 공장주소와 남의 사업자번호가 찍힌다(실측: CV. CITRA 에
    # 바이오로제트의 공장주소·등록번호 427302017384122 가 들어갔다).
    _org_company = ((org_row.name if org_row else "") or "").strip()
    _same_company = (not _org_company) or (_org_company == (c.company_name or "").strip())
    _identity = {"nib", "responsible_person", "factory_reg_no", "factory_address", "address"}
    for k in ("nib", "responsible_person", "halal_supervisor", "email", "phone",
              "address", "factory_reg_no", "factory_address"):
        if oext.get(k) is None:
            continue
        if not _same_company and k in _identity:
            continue          # 다른 회사의 신원값은 물려받지 않는다
        setattr(c, k, oext[k])
    # 공장도 org 자산 상속(오피스 1:N 공장) — 새 신청에 org 공장 자동 연결(회사프로필 상속과 대칭).
    # 단 다른 회사가 이미 쓰는 공장은 상속하지 않는다(_facilities_for_case 주석 참조).
    facs = _facilities_for_case(db, c)
    if facs:
        c.facility_ids = [f.facility_id for f in facs]
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


# ── P0-5: 모의심사 오디터 뷰 3종 — 스키마 무변경, WorkflowEvent(latest-wins)로 저장 ──
# 5개 증거 섹션 고정 상수(고정 순서·키). 조회 시 섹션별 최신 이벤트가 현재 판정.
MOCK_EVIDENCE_SECTIONS = [("material_storage", "원재료 보관"), ("production_video", "생산 공정 영상"),
                          ("product_storage", "제품 보관"), ("facility", "생산 시설"),
                          ("hygiene", "위생 관리")]


def _mock_manual_latest(db, case_id):
    """할랄매뉴얼 검토 최신 상태(mock_audit.manual, latest-wins)."""
    e = (db.query(models.WorkflowEvent)
         .filter(models.WorkflowEvent.case_id == case_id,
                 models.WorkflowEvent.action == "mock_audit.manual")
         .order_by(models.WorkflowEvent.created_at.desc()).first())
    if not e:
        return None
    p = e.payload or {}
    return {"decision": p.get("decision"), "comment": p.get("comment", ""), "round": p.get("round", 0),
            "actor": e.actor_id, "at": e.created_at.isoformat() if e.created_at else None}


def _mock_evidence_latest(db, case_id):
    """섹션별 최신 판정 맵(mock_audit.evidence append → latest-wins)."""
    evs = (db.query(models.WorkflowEvent)
           .filter(models.WorkflowEvent.case_id == case_id,
                   models.WorkflowEvent.action == "mock_audit.evidence")
           .order_by(models.WorkflowEvent.created_at.asc()).all())
    latest = {}
    for e in evs:
        p = e.payload or {}
        sec = p.get("section")
        if sec:
            latest[sec] = {"verdict": p.get("verdict"), "corrective_action": p.get("corrective_action", ""),
                           "actor": e.actor_id, "at": e.created_at.isoformat() if e.created_at else None}
    return latest


def _mock_report_latest(db, case_id):
    """AI 모의심사 리포트 최신(mock_audit.ai_report)."""
    e = (db.query(models.WorkflowEvent)
         .filter(models.WorkflowEvent.case_id == case_id,
                 models.WorkflowEvent.action == "mock_audit.ai_report")
         .order_by(models.WorkflowEvent.created_at.desc()).first())
    return (e.payload or {}) if e else None


@app.post("/cases/{case_id}/mock-audit/manual")
def mock_audit_manual(case_id: str, body: schemas.MockAuditManualReq,
                      user=Depends(rbac.require_action("audit.mock_decide")),
                      db: Session = Depends(get_db)):
    """① 할랄매뉴얼(SJPH/HPAS) 검토 승인/반려 — 반려 왕복 시 재작성 회차(round) 누적. latest-wins."""
    c = _get_case(db, case_id, user)
    decision = (body.decision or "").lower()
    if decision not in ("approve", "reject"):
        raise HTTPException(422, {"code": "INVALID_DECISION", "allowed": ["approve", "reject"]})
    comment = (body.comment or "").strip()
    if decision == "reject" and not comment:
        raise HTTPException(422, {"code": "REASON_REQUIRED"})
    # round = 지금까지 이 케이스의 mock_audit.manual reject 이벤트 수 + 1
    prior = (db.query(models.WorkflowEvent)
             .filter(models.WorkflowEvent.case_id == case_id,
                     models.WorkflowEvent.action == "mock_audit.manual").all())
    reject_count = sum(1 for e in prior if (e.payload or {}).get("decision") == "reject")
    rnd = reject_count + 1
    sm.record_event(db, c, c.status, c.status, "mock_audit.manual", user["role"], user["uid"],
                    {"decision": decision, "comment": comment, "round": rnd})
    if decision == "reject":
        _notify(db, c, "mock_audit.manual_return", "할랄매뉴얼 보완 요청", body=comment, role="applicant")
    db.commit()
    return {"ok": True, "decision": decision, "round": rnd}


@app.post("/cases/{case_id}/mock-audit/evidence-verdict")
def mock_audit_evidence_verdict(case_id: str, body: schemas.MockAuditEvidenceReq,
                                user=Depends(rbac.require_action("audit.mock_decide")),
                                db: Session = Depends(get_db)):
    """② 5개 증거 섹션별 적합/부적합 판정 — append 기록, 조회 시 섹션별 최신(latest-wins)."""
    c = _get_case(db, case_id, user)
    valid = {k for k, _ in MOCK_EVIDENCE_SECTIONS}
    if body.section not in valid:
        raise HTTPException(422, {"code": "BAD_SECTION", "allowed": [k for k, _ in MOCK_EVIDENCE_SECTIONS]})
    verdict = (body.verdict or "").lower()
    if verdict not in ("comply", "nonconformity"):
        raise HTTPException(422, {"code": "INVALID_VERDICT", "allowed": ["comply", "nonconformity"]})
    ca = (body.corrective_action or "").strip()
    sm.record_event(db, c, c.status, c.status, "mock_audit.evidence", user["role"], user["uid"],
                    {"section": body.section, "verdict": verdict, "corrective_action": ca})
    db.commit()
    return {"ok": True, "section": body.section, "verdict": verdict}


def _mock_ai_report_build(db, c):
    """③ 결정적 집계 → 요약·권고 텍스트. LLM 보강은 호출부에서 옵션 처리."""
    case_id = c.case_id
    latest_ev = _mock_evidence_latest(db, case_id)
    sections, comply, nonconf = [], 0, 0
    for k, ko in MOCK_EVIDENCE_SECTIONS:
        v = latest_ev.get(k, {})
        vd = v.get("verdict")
        if vd == "comply":
            comply += 1
        elif vd == "nonconformity":
            nonconf += 1
        sections.append({"section": k, "label": ko, "verdict": vd,
                         "corrective_action": v.get("corrective_action", "")})
    have = _ensure_hpas(db, case_id)
    hpas_ok = sum(1 for el in HPAS_ELEMENTS if have[el].status == "ok")
    sjph_completion = round(hpas_ok / len(HPAS_ELEMENTS) * 100)
    evidence_docs = db.query(models.DocumentAsset).filter_by(case_id=case_id).count()
    manual = _mock_manual_latest(db, case_id)
    manual_status = manual.get("decision") if manual else None
    manual_round = manual.get("round") if manual else 0
    manual_ko = {"approve": "승인", "reject": "반려"}.get(manual_status, "미검토")
    overall = "적합" if (nonconf == 0 and comply > 0) else ("부적합" if nonconf > 0 else "미판정")
    summary = ("모의심사 준비자료 종합평가 — 증거 섹션 적합 %d · 부적합 %d(총 %d), "
               "SJPH/HPAS 완성도 %d%%, 제출 증거 %d건, 할랄매뉴얼 검토 %s. 종합 %s." % (
                   comply, nonconf, len(MOCK_EVIDENCE_SECTIONS), sjph_completion,
                   evidence_docs, manual_ko, overall))
    recs = []
    for s in sections:
        if s["verdict"] == "nonconformity":
            recs.append("· %s 부적합 — 시정조치 필요%s" % (
                s["label"], (": " + s["corrective_action"]) if s["corrective_action"] else ""))
    if sjph_completion < 100:
        recs.append("· SJPH/HPAS 미완성 요소 보완(현재 %d%%)" % sjph_completion)
    if manual_status != "approve":
        recs.append("· 할랄매뉴얼 검토 승인 필요(현재 %s)" % manual_ko)
    if not recs:
        recs.append("· 준비자료 양호 — 현장심사 단계 진행 권고")
    return {"summary": summary, "recommendations": "\n".join(recs), "verdict_overall": overall,
            "comply": comply, "nonconformity": nonconf, "sections": sections,
            "sjph_completion": sjph_completion, "evidence_docs": evidence_docs,
            "manual_status": manual_status, "manual_round": manual_round}


@app.post("/cases/{case_id}/mock-audit/ai-report/run")
def mock_audit_ai_report_run(case_id: str,
                             user=Depends(rbac.require_action("audit.mock_decide")),
                             db: Session = Depends(get_db)):
    """③ AI 모의심사 리포트 — 결정적 집계(CI 안전) + LLM 보강(옵션·실패 시 폴백)."""
    c = _get_case(db, case_id, user)
    report = _mock_ai_report_build(db, c)
    # LLM 보강은 옵션 — 미가용/실패/타임아웃 시 결정적 요약으로 폴백.
    llm_note = ""
    try:
        prompt = ("다음 모의심사 집계로 심사관용 3~4문장 총평을 한국어로 작성.\n[요약]\n%s\n[권고]\n%s"
                  % (report["summary"], report["recommendations"]))
        out = ai_local.llm_text("당신은 인도네시아 할랄 인증 모의심사관입니다. 간결한 총평만 작성.",
                                 prompt, timeout=30)
        if out and out.strip():
            llm_note = out.strip()
    except Exception:  # noqa: BLE001
        llm_note = ""
    report["llm_note"] = llm_note
    report["generated_at"] = datetime.utcnow().isoformat()
    sm.record_event(db, c, c.status, c.status, "mock_audit.ai_report", user["role"], user["uid"], report)
    db.commit()
    return {"ok": True, "report": report}


@app.get("/cases/{case_id}/mock-audit/detail")
def mock_audit_detail(case_id: str, user=Depends(auth.get_current_user), db: Session = Depends(get_db)):
    """오디터 뷰 통합 조회 — 매뉴얼 최신+round·코멘트 이력, 섹션별 최신 verdict(5), 제출 증거, 리포트 최신."""
    c = _get_case(db, case_id, user)
    manual = _mock_manual_latest(db, case_id)
    manual_evs = (db.query(models.WorkflowEvent)
                  .filter(models.WorkflowEvent.case_id == case_id,
                          models.WorkflowEvent.action == "mock_audit.manual")
                  .order_by(models.WorkflowEvent.created_at.desc()).all())
    manual_history = [{"decision": (e.payload or {}).get("decision"),
                       "comment": (e.payload or {}).get("comment", ""),
                       "round": (e.payload or {}).get("round"), "actor": e.actor_id,
                       "at": e.created_at.isoformat() if e.created_at else None} for e in manual_evs]
    latest_ev = _mock_evidence_latest(db, case_id)
    sections = [{"section": k, "label": ko,
                 "verdict": latest_ev.get(k, {}).get("verdict"),
                 "corrective_action": latest_ev.get(k, {}).get("corrective_action", ""),
                 "at": latest_ev.get(k, {}).get("at")} for k, ko in MOCK_EVIDENCE_SECTIONS]
    docs = [{"document_id": d.document_id, "filename": d.filename, "doc_type": d.doc_type,
             "review_status": d.review_status, "has_file": bool(d.content_b64),
             "content_type": d.content_type,
             "lat": d.lat, "lng": d.lng, "geo_source": d.geo_source,
             "uploaded_by": d.uploaded_by, "uploader_role": d.uploader_role,
             "captured_at": d.captured_at, "file_hash": d.file_hash}
            for d in db.query(models.DocumentAsset).filter_by(case_id=case_id).all()]
    # 섹션별 제출 증거(사진·영상)를 판정 행에 직접 붙인다 — 오디터가 자료를 보지 않고 체크만 하는 것을 방지
    for sec in sections:
        dt = "mock_evidence_" + sec["section"]
        ev = [d for d in docs if d["doc_type"] == dt]
        ev.sort(key=lambda x: str(x.get("captured_at") or ""), reverse=True)
        sec["evidence"] = ev
        sec["evidence_count"] = len(ev)
    return {"case_id": case_id, "status": c.status,
            "manual": manual, "manual_history": manual_history,
            "sections": sections, "documents": docs,
            "evidence_total": sum(s["evidence_count"] for s in sections),
            "evidence_missing": [s["section"] for s in sections if not s["evidence_count"]],
            "ai_report": _mock_report_latest(db, case_id)}


@app.get("/cases/{case_id}/mock-audit/ai-report.pdf")
def mock_audit_ai_report_pdf(case_id: str,
                             user=Depends(rbac.require_action("audit.mock_decide")),
                             db: Session = Depends(get_db)):
    """최신 AI 모의심사 리포트를 리치 PDF로. 리포트 미생성 시 즉석 집계로 렌더."""
    from fastapi.responses import Response
    c = _get_case(db, case_id, user)
    rep = _mock_report_latest(db, case_id) or _mock_ai_report_build(db, c)
    sec_rows = [[s.get("label", s.get("section", "")),
                 {"comply": "적합", "nonconformity": "부적합"}.get(s.get("verdict"), "미판정"),
                 s.get("corrective_action") or "-"] for s in rep.get("sections", [])]
    blocks = [
        {"type": "heading", "text": "AI 모의심사 리포트 · Mock Audit AI Report", "level": 1},
        {"type": "kv", "label": "종합 판정", "value": rep.get("verdict_overall", "-")},
        {"type": "kv", "label": "증거 섹션", "value": "적합 %d · 부적합 %d" % (rep.get("comply", 0), rep.get("nonconformity", 0))},
        {"type": "kv", "label": "SJPH/HPAS 완성도", "value": "%d%%" % rep.get("sjph_completion", 0)},
        {"type": "kv", "label": "제출 증거", "value": "%d건" % rep.get("evidence_docs", 0)},
        {"type": "heading", "text": "증거 섹션 판정 · Evidence Verdicts", "level": 2},
        {"type": "table", "headers": ["섹션", "판정", "시정조치"], "widths": [0.3, 0.18, 0.52],
         "rows": sec_rows or [["-", "미판정", "-"]]},
        {"type": "heading", "text": "종합 요약 · Summary", "level": 2},
        {"type": "para", "text": rep.get("summary", "")},
        {"type": "heading", "text": "권고 · Recommendations", "level": 2},
    ]
    for line in (rep.get("recommendations") or "").split("\n"):
        blocks.append({"type": "para", "text": line})
    if rep.get("llm_note"):
        blocks.append({"type": "heading", "text": "AI 총평 · AI Note", "level": 2})
        blocks.append({"type": "para", "text": rep["llm_note"]})
    pdf = _render_pdf_rich("Mock Audit AI Report", blocks, subtitle=(c.company_name or ""),
                           footer="GL-HAC AI · Mock Audit " + case_id[:8])
    return Response(content=pdf, media_type="application/pdf",
                    headers={"Content-Disposition": "attachment; filename=mock_audit_ai_%s.pdf" % case_id[:8]})


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


@app.get("/materials/{material_id}/auditor-note")
def material_auditor_note(material_id: str, lang: str = Query("ko"),
                          user=Depends(auth.get_current_user), db: Session = Depends(get_db)):
    """오디터 심사 참고 의견 — 왜 걸렸는지·언제 예외인지·이 건의 근거·확인할 것.

    판정을 바꾸지 않는다. 오디터는 '걸린 목록'만으로 서명할 수 없어서, 근거와 확인 사항을
    한자리에 모아 준다. 인증번호가 있어도 자동으로 풀지 않는 이유(대조는 사람이 한다)도
    여기서 설명된다."""
    m = db.get(models.Material, material_id)
    if not m:
        raise HTTPException(404, {"code": "MATERIAL_NOT_FOUND"})
    _get_case(db, m.case_id, user)     # 조직 격리
    return screening.auditor_note(m, (lang or "ko").lower())


# 원재료 유형(Jenis Bahan) — 서류에는 'BAHAN BAKU'·'CLEANING AGENT'처럼 제각각 적힌다.
# 사전 MATERIAL 축의 표기 목록으로 표준 코드에 붙이고, 화면 표기도 사전에서 꺼낸다.
def _mat_type_code(raw):
    """서류 표기 → 표준 코드. 못 알아보면 None(지어내지 않는다).

    코드 목록과 표기 흔들림 처리는 사전이 정본이다 — 여기서 다시 적으면 한쪽만 늘어난다."""
    return _dd_mod.material_type_code(raw)


def _mat_type_label(raw, lang="ko"):
    """표준 코드의 화면 표기. 표준화가 안 되면 서류에 적힌 값을 그대로 보여준다 —
    임의로 비우면 서류에 있던 정보가 화면에서 사라진다."""
    if not raw:
        return ""
    key, _ = _dd_mod.material_type(raw)
    if key:
        return _dd_mod.label(key, lang) or str(raw)
    code = _mat_type_code(raw)
    if code:
        for k in _dd_mod.by_axis("MATERIAL"):
            if (_dd_mod.actions(k) or {}).get("material_type") == code:
                return _dd_mod.label(k, lang) or code
    return str(raw)


@app.get("/cases/{case_id}/materials")
def list_materials(case_id: str, lang: str = Query("ko"),
                   user=Depends(auth.get_current_user), db: Session = Depends(get_db)):
    _get_case(db, case_id, user)
    rows = db.query(models.Material).filter_by(case_id=case_id).all()
    ev = {}
    for d in (db.query(models.DocumentAsset)
              .filter(models.DocumentAsset.case_id == case_id,
                      models.DocumentAsset.material_id.isnot(None)).all()):
        ev.setdefault(d.material_id, []).append(
            {"document_id": d.document_id, "filename": d.filename,
             "evidence_type": d.doc_type, "review_status": d.review_status,
             "has_file": bool(d.content_b64)})
    return [{"material_id": m.material_id, "name": m.name, "e_number": m.e_number,
             "mat_type": m.mat_type, "source": m.source, "supplier": m.supplier,
             # 제조사가 비면 공급사를 보여준다 — 서류에 한 칸만 있는 경우가 흔하고,
             # 빈칸으로 두면 화면에서 '정보가 없다'로 읽혀 오디터가 다시 묻는다.
             "manufacturer": m.manufacturer or m.supplier or "",
             "manufacturer_is_supplier": bool(not m.manufacturer and m.supplier),
             "mat_type_code": _mat_type_code(m.mat_type),
             "mat_type_label": _mat_type_label(m.mat_type, lang),
             "origin": m.origin,
             "cert": m.cert, "cert_no": m.cert_no, "v1_risk": m.v1_risk, "note": m.note,
             "result": m.screen_result, "status": m.screen_status, "severity": m.screen_severity,
             "matched_uid": m.matched_uid,
             "evidence_count": len(ev.get(m.material_id) or []),
             "evidence": ev.get(m.material_id) or []} for m in rows]


# ---------- products / materials ----------
@app.post("/cases/{case_id}/products")
def add_product(case_id: str, body: schemas.ProductCreate,
                user=Depends(auth.require_roles("applicant", "consultant")),
                db: Session = Depends(get_db)):
    _get_case(db, case_id, user)
    # 같은 이름이 이미 있으면 **알려만 준다**. 막지 않는다.
    #
    # 같은 제품을 두 번 넣으면 인증 범위(scope)에 중복 인쇄되고 매트릭스도 두 줄이 되지만,
    # 그건 등록을 거절할 이유는 못 된다. 같은 이름의 다른 규격·용량을 따로 올리는 일이
    # 실제로 있고, 무엇보다 등록하는 쪽이 사정을 안다. 판단은 사람에게 남기고
    # 화면이 "이미 있습니다"라고 말해 주면 된다.
    _key = _norm_material(body.name)
    dup = next((x for x in db.query(models.Product).filter_by(case_id=case_id).all()
                if _norm_material(x.name) == _key), None)
    p = models.Product(case_id=case_id, name=body.name, category=body.category,
                       description=body.description,
                       registration_type=body.registration_type, status="draft")
    db.add(p)
    db.commit()
    out = {"product_id": p.product_id}
    if dup:
        out["duplicate_of"] = dup.product_id
        out["warning"] = "같은 이름의 제품이 이미 있습니다: %s" % dup.name
    return out


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
                        manufacturer=body.manufacturer, origin=body.origin,
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
        # 원재료 삭제는 할랄 문서에서 성분을 빼는 행위다. 무엇을 어떤 판정 상태에서
        # 지웠는지 남겨야 나중에 되짚을 수 있다(삭제 전에 기록 — 행이 사라지면 못 남긴다).
        _audit(db, user, "material.delete", "material", material_id, m.case_id,
               {"name": m.name, "screen_result": m.screen_result,
                "screen_status": m.screen_status, "supplier": m.supplier,
                "cert_no": m.cert_no}, commit=False)
        db.delete(m)
        db.commit()
    return {"deleted": material_id}


@app.patch("/materials/{material_id}")
def patch_material(material_id: str, body: schemas.MaterialPatch,
                   user=Depends(rbac.require_action("material.add")),
                   db: Session = Depends(get_db)):
    """원재료 속성 수정(제조사·유형·인증번호 등) → 즉시 재판정.

    할랄 인증서 번호를 넣으면 의심(mushbooh)이 할랄로 올라간다. 무엇을 근거로 올렸는지
    감사기록에 남긴다 — 번호만 적힌 것과 증빙으로 해소된 것은 다르고, 오디터는 그 번호를
    발급기관 목록과 대조해야 한다."""
    m = db.get(models.Material, material_id)
    if not m:
        raise HTTPException(404, {"code": "MATERIAL_NOT_FOUND"})
    _get_case(db, m.case_id, user)
    before = {"status": m.screen_status, "result": m.screen_result,
              "cert_no": m.cert_no, "manufacturer": m.manufacturer,
              "supplier": m.supplier, "mat_type": m.mat_type}
    changed = {}
    for f in ("mat_type", "source", "supplier", "manufacturer", "origin", "cert", "cert_no", "note"):
        v = getattr(body, f, None)
        if v is not None and (v or "") != (getattr(m, f) or ""):
            setattr(m, f, v or None)
            changed[f] = v
    if not changed:
        return {"material_id": material_id, "changed": {}, "screen": None}
    r = screening.screen_merged(m.name, m.e_number, m.source, m.cert_no,
                                bool(m.evidence_provided),
                                True if m.source_known is None else bool(m.source_known),
                                m.note or "")
    m.cert = r.get("v1_cert")
    m.v1_risk = r.get("v1_risk")
    m.screen_result, m.screen_status = r["result"], r["status"]
    m.screen_severity, m.matched_uid = r["severity"], r.get("matched_uid")
    _audit(db, user, "material.update", "material", material_id, m.case_id,
           {"name": m.name, "changed": changed, "before": before,
            "after": {"status": m.screen_status, "result": m.screen_result},
            "decision_by": r.get("decision_by"),
            "cert_promoted": bool(r.get("cert_promoted"))}, commit=False)
    db.commit()
    return {"material_id": material_id, "changed": changed, "screen": r}


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


# ── 정량 기준·측정값 (P3 데이터 선행) — 정성 온톨로지가 못 다루는 임계 판정의 근거 ──
# 출처: SJPH/HAS 23000 · MUI Fatwa(khamr 0.5%) · BPOM 중금속 한계(식품 일반). 운영 시 규정 버전과 연동.
QUANT_CRITERIA = {
    "ethanol_pct":  {"ko": "에탄올 함량", "unit": "%",   "max": 0.5,  "basis": "MUI Fatwa · khamr 기준 (<0.5%)"},
    "lead_ppm":     {"ko": "중금속 (Pb)", "unit": "ppm", "max": 2.0,  "basis": "BPOM 식품 중금속 한계"},
    "cadmium_ppm":  {"ko": "중금속 (Cd)", "unit": "ppm", "max": 0.3,  "basis": "BPOM 식품 중금속 한계"},
    "mercury_ppm":  {"ko": "중금속 (Hg)", "unit": "ppm", "max": 0.03, "basis": "BPOM 식품 중금속 한계"},
    "arsenic_ppm":  {"ko": "중금속 (As)", "unit": "ppm", "max": 1.0,  "basis": "BPOM 식품 중금속 한계"},
    "pork_dna":     {"ko": "돈지·돼지 DNA", "unit": "detect", "max": 0.0, "basis": "불검출 필수 (PCR)"},
}


def _quant_verdict(param_key, value):
    crit = QUANT_CRITERIA.get(param_key)
    if not crit or value is None:
        return "unknown"
    try:
        return "pass" if float(value) <= float(crit["max"]) else "fail"
    except (TypeError, ValueError):
        return "unknown"


@app.get("/meta/quant-criteria")
def get_quant_criteria(user=Depends(auth.get_current_user)):
    """정량 기준 카탈로그 — 프런트 입력 폼·비교표의 기준 소스."""
    return {"criteria": [dict(key=k, **v) for k, v in QUANT_CRITERIA.items()]}


@app.get("/cases/{case_id}/measurements")
def list_measurements(case_id: str, user=Depends(auth.get_current_user), db: Session = Depends(get_db)):
    _get_case(db, case_id, user)
    rows = (db.query(models.MaterialMeasurement).filter_by(case_id=case_id)
            .order_by(models.MaterialMeasurement.created_at.desc()).all())
    names = {m.material_id: m.name for m in db.query(models.Material).filter_by(case_id=case_id)}
    out = []
    for r in rows:
        crit = QUANT_CRITERIA.get(r.param_key, {})
        out.append({"measurement_id": r.measurement_id, "material_id": r.material_id,
                    "material_name": names.get(r.material_id), "param_key": r.param_key,
                    "param_ko": crit.get("ko", r.param_key), "value": r.value, "unit": r.unit or crit.get("unit"),
                    "threshold": crit.get("max"), "basis": crit.get("basis"), "verdict": r.verdict,
                    "method": r.method, "lab_name": r.lab_name, "tested_at": r.tested_at,
                    "document_id": r.document_id, "note": r.note})
    return {"items": out, "count": len(out),
            "fail_count": sum(1 for x in out if x["verdict"] == "fail")}


@app.post("/cases/{case_id}/measurements")
def add_measurement(case_id: str, body: dict = None,
                    user=Depends(auth.require_roles("applicant", "consultant", "auditor", "operator")),
                    db: Session = Depends(get_db)):
    """정량 측정값 등록(성적서 기반) — 서버가 임계와 대조해 verdict 계산."""
    b = body or {}
    pk = str(b.get("param_key") or "").strip()
    if pk not in QUANT_CRITERIA:
        raise HTTPException(422, {"code": "BAD_PARAM", "allowed": sorted(QUANT_CRITERIA)})
    c = _get_case(db, case_id, user)
    mid = b.get("material_id") or None
    if mid and not db.query(models.Material).filter_by(case_id=case_id, material_id=mid).first():
        raise HTTPException(404, {"code": "MATERIAL_NOT_FOUND"})
    try:
        val = float(b["value"]) if b.get("value") is not None else None
    except (TypeError, ValueError):
        raise HTTPException(422, {"code": "BAD_VALUE"})
    vd = _quant_verdict(pk, val)
    row = models.MaterialMeasurement(
        case_id=case_id, material_id=mid, param_key=pk, value=val,
        unit=str(b.get("unit") or QUANT_CRITERIA[pk]["unit"]), method=str(b.get("method") or ""),
        lab_name=str(b.get("lab_name") or ""), tested_at=str(b.get("tested_at") or "")[:10],
        document_id=b.get("document_id"), verdict=vd, note=str(b.get("note") or ""),
        recorded_by=user["uid"])
    db.add(row)
    sm.record_event(db, c, c.status, c.status, "material.measurement", user["role"], user["uid"],
                    {"param_key": pk, "value": val, "verdict": vd, "material_id": mid})
    db.commit()
    return {"measurement_id": row.measurement_id, "param_key": pk, "value": val,
            "threshold": QUANT_CRITERIA[pk]["max"], "verdict": vd}


@app.delete("/measurements/{measurement_id}")
def delete_measurement(measurement_id: str,
                       user=Depends(auth.require_roles("applicant", "consultant", "auditor", "operator")),
                       db: Session = Depends(get_db)):
    row = db.get(models.MaterialMeasurement, measurement_id)
    if not row:
        raise HTTPException(404, {"code": "NOT_FOUND"})
    _get_case(db, row.case_id, user)   # 조직 격리
    db.delete(row)
    db.commit()
    return {"ok": True}


# ── 원재료 크로스케이스 재사용 (P3 데이터 선행) — 회의: "기존 할랄인증 받은 원재료는
#    새로 만들지 말고 기존 리스트 재사용, 케이스 간 중복 데이터 없이" ──
@app.get("/orgs/materials/catalog")
def org_material_catalog(q: str = Query("", max_length=80), limit: int = Query(50, le=200),
                         user=Depends(auth.get_current_user), db: Session = Depends(get_db)):
    """조직 내 기존 케이스들의 원재료 카탈로그(정규화명 dedup) — 신규 케이스에서 재사용."""
    org_cases = [c.case_id for c in db.query(models.CaseApplication.case_id)
                 .filter(models.CaseApplication.org_id == user.get("org_id")).all()]
    if not org_cases:
        return {"items": [], "count": 0}
    rows = db.query(models.Material).filter(models.Material.case_id.in_(org_cases)).all()
    best = {}
    for m in rows:
        key = _norm_material(m.name)
        if q and q.lower() not in (m.name or "").lower():
            continue
        cur = best.get(key)
        # 우선순위: 증빙 보유 > CLEARED/PASS 판정 > 인증번호 보유 > 최신
        rank = (1 if m.evidence_provided else 0, 1 if m.screen_result in ("CLEARED", "PASS") else 0,
                1 if m.cert_no else 0)
        if not cur or rank > cur[0]:
            best[key] = (rank, m)
    items = [{"name": m.name, "e_number": m.e_number, "mat_type": m.mat_type, "source": m.source,
              "supplier": m.supplier, "cert": m.cert, "cert_no": m.cert_no,
              "screen_result": m.screen_result, "screen_status": m.screen_status,
              "evidence_provided": bool(m.evidence_provided), "source_case_id": m.case_id}
             for _, m in sorted(best.values(), key=lambda x: -x[0][0])][:limit]
    return {"items": items, "count": len(items)}


@app.post("/cases/{case_id}/materials/reuse")
def reuse_materials(case_id: str, body: dict = None,
                    user=Depends(auth.require_roles("applicant", "consultant")),
                    db: Session = Depends(get_db)):
    """카탈로그에서 선택한 원재료를 현재 케이스로 복사 — 이미 있는 정규화명은 건너뜀(중복 방지)."""
    c = _get_case(db, case_id, user)
    names = [str(n).strip() for n in ((body or {}).get("names") or []) if str(n).strip()]
    if not names:
        raise HTTPException(422, {"code": "NO_NAMES"})
    org_cases = [x.case_id for x in db.query(models.CaseApplication.case_id)
                 .filter(models.CaseApplication.org_id == c.org_id).all()]
    src = db.query(models.Material).filter(models.Material.case_id.in_(org_cases)).all()
    have = {_norm_material(m.name) for m in db.query(models.Material).filter_by(case_id=case_id)}
    by_key = {}
    for m in src:
        by_key.setdefault(_norm_material(m.name), m)
    added, skipped = [], []
    for n in names:
        k = _norm_material(n)
        if k in have:
            skipped.append(n)
            continue
        m = by_key.get(k)
        if not m:
            skipped.append(n)
            continue
        nm = models.Material(case_id=case_id, name=m.name, e_number=m.e_number, mat_type=m.mat_type,
                             source=m.source, supplier=m.supplier, cert=m.cert, cert_no=m.cert_no,
                             evidence_provided=False)   # 증빙은 케이스별 재확인(안전측)
        db.add(nm)
        try:
            screening.apply_screen(nm)   # 재스크리닝(온톨로지 갱신 반영)
        except Exception:  # noqa: BLE001
            pass
        have.add(k)
        added.append(m.name)
    sm.record_event(db, c, c.status, c.status, "material.reuse", user["role"], user["uid"],
                    {"added": added, "skipped": skipped})
    db.commit()
    return {"added": added, "skipped": skipped, "added_count": len(added)}


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
             "description": p.description or "",
             "registration_type": p.registration_type, "status": p.status or "draft",
             "material_count": mc.get(p.product_id, 0), "risk_count": risk.get(p.product_id, 0),
             "photo_count": ph.get(p.product_id, 0),
             # 현장심사 보고서 제품표에 그대로 실리는 두 칸 — 비면 보고서도 빈칸으로 나간다
             "report_ready": bool((p.description or "").strip()) and bool(ph.get(p.product_id, 0)),
             "missing_for_report": ([] if (p.description or "").strip() else ["description"])
                                   + ([] if ph.get(p.product_id, 0) else ["photo"])}
            for p in rows]


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
            "description": p.description or "",
            "registration_type": p.registration_type, "status": p.status or "draft",
            "certificate_no": cert.certificate_no if cert else None,
            "expiry_date": cert.expiry_date if cert else None,
            "materials": [{"material_id": m.material_id, "name": m.name,
                           "screen_status": m.screen_status, "screen_severity": m.screen_severity,
                           "result": m.screen_result, "matched_uid": m.matched_uid, "v1_risk": m.v1_risk,
                           "e_number": m.e_number, "cert": m.cert, "supplier": m.supplier,
                           "evidence_count": (1 if m.evidence_provided else 0),
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
    if body.description is not None:
        p.description = body.description
    if body.registration_type is not None:
        p.registration_type = body.registration_type
    if body.status is not None:
        p.status = body.status
    db.commit()
    return {"product_id": p.product_id, "status": p.status,
            "registration_type": p.registration_type, "description": p.description}


@app.delete("/cases/{case_id}/products/{product_id}/photos/{document_id}")
def delete_product_photo(case_id: str, product_id: str, document_id: str,
                         user=Depends(auth.require_roles("applicant", "consultant",
                                                         "operator", "admin")),
                         db: Session = Depends(get_db)):
    """제품 사진 삭제 — 잘못 올린 사진이 현장심사 보고서에 그대로 인쇄되기 때문이다.

    이 사진은 LPH가 보는 보고서의 제품표에 들어간다. 지우는 통로가 없으면 틀린 사진을
    되돌릴 방법이 없다. 무엇을 지웠는지는 감사기록에 남긴다."""
    _get_case(db, case_id, user)
    d = db.get(models.DocumentAsset, document_id)
    if (not d or d.case_id != case_id or d.product_id != product_id
            or d.doc_type != "product_photo"):
        raise HTTPException(404, {"code": "PHOTO_NOT_FOUND"})
    _audit(db, user, "product.photo.delete", "document", document_id, case_id,
           {"product_id": product_id, "filename": d.filename}, commit=False)
    db.delete(d)
    db.commit()
    return {"deleted": document_id}


@app.delete("/cases/{case_id}/products/{product_id}")
def delete_product(case_id: str, product_id: str, force: bool = False,
                   user=Depends(auth.require_roles("applicant", "consultant", "operator", "admin")),
                   db: Session = Depends(get_db)):
    """제품 삭제(오분류·중복 정리) — 제품 + 원재료 연결(ProductMaterial) 제거. 조직격리.

    발급된 인증서의 동결 범위(frozen_product_ids)에 든 제품은 그냥 지우지 않는다.
    인증서는 '이 제품들을 인증한다'는 문서다. 제품 행만 사라지면 인증서가 없는 것을
    가리키고, 그 사실이 아무 데도 남지 않는다. force=true 로 지울 때는 동결 목록에서도
    빼고 무엇을 왜 뺐는지 감사기록에 남긴다(운영자·관리자만)."""
    _get_case(db, case_id, user)
    p = db.get(models.Product, product_id)
    if not p or p.case_id != case_id:
        raise HTTPException(404, {"code": "PRODUCT_NOT_FOUND"})
    certs = [ct for ct in db.query(models.HalalCertificate).filter_by(case_id=case_id).all()
             if product_id in (ct.frozen_product_ids or [])]
    if certs and not force:
        raise HTTPException(409, {"code": "CERT_SCOPE_FROZEN",
                                  "certificates": [ct.certificate_no for ct in certs],
                                  "hint": "인증서 동결 범위에 든 제품입니다. "
                                          "force=true 로 지우면 동결 목록에서도 빠집니다."})
    if certs and user["role"] not in ("operator", "admin"):
        raise HTTPException(403, {"code": "NOT_AUTHORIZED",
                                  "need": ["operator", "admin"], "have": user["role"]})
    for ct in certs:
        ct.frozen_product_ids = [x for x in (ct.frozen_product_ids or []) if x != product_id]
        flag_modified(ct, "frozen_product_ids")   # JSON 컬럼 in-place 변경 감지
    _audit(db, user, "product.delete", "product", product_id, case_id,
           {"name": p.name, "description": p.description,
            "material_links": db.query(models.ProductMaterial)
                                .filter_by(product_id=product_id).count(),
            "removed_from_certificates": [ct.certificate_no for ct in certs]}, commit=False)
    db.query(models.ProductMaterial).filter_by(product_id=product_id).delete()
    db.delete(p)
    db.commit()
    return {"deleted": product_id,
            "removed_from_certificates": [ct.certificate_no for ct in certs]}


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


def _exif_datetime(b64_or_bytes):
    """이미지 EXIF 원본 촬영시각(DateTimeOriginal) 추출 → 문자열 또는 None. 증거 귀속(A08 '언제')."""
    try:
        import io
        from PIL import Image, ExifTags
        data = b64_or_bytes if isinstance(b64_or_bytes, (bytes, bytearray)) else base64.b64decode(
            str(b64_or_bytes).split(",")[-1])
        exif = Image.open(io.BytesIO(data)).getexif()
        dto = None
        try:  # DateTimeOriginal(36867)/DateTimeDigitized(36868)은 Exif 서브 IFD에 존재
            sub = exif.get_ifd(ExifTags.IFD.Exif)
            dto = sub.get(36867) or sub.get(36868)
        except Exception:  # noqa: BLE001
            dto = None
        if not dto:
            dto = exif.get(306)  # 폴백: DateTime(파일 기록시각)
        if dto:
            s = str(dto).strip()
            return s or None
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
    db.add(models.DocumentAsset(case_id=case_id, filename=body.filename,
                                filename_en=_fn_en(body.filename), doc_type=body.evidence_type,
                                material_id=material_id, review_status="pending",
                                content_b64=b64 if len(b64) < 4_000_000 else None,
                                content_type=_ctype(body.filename),
                                lat=gps[0] if gps else None, lng=gps[1] if gps else None,
                                geo_source="exif" if gps else None))
    prev = m.screen_result
    if not body.counts_as_evidence:
        # 출처만 남기는 첨부 — 파일은 원재료에 연결되지만 판정은 손대지 않는다.
        # 원산지증명서가 유래 선언을 대신할 수 없는데도 NEEDS_EVIDENCE를 CLEARED로
        # 바꿔버리면 할랄 문서에서 가장 위험한 종류의 거짓이 된다.
        sm.record_event(db, c, c.status, c.status, "material.provenance", user["role"], user["uid"],
                        {"material_id": material_id, "doc_type": body.evidence_type,
                         "filename": body.filename, "screen_result": prev})
        db.commit()
        return {"material_id": material_id, "evidence_type": body.evidence_type,
                "screen_result": prev, "was": prev, "recleared": False,
                "counts_as_evidence": False}
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
    return [{"document_id": d.document_id, "filename": d.filename,
             "filename_en": d.filename_en, "evidence_type": d.doc_type,
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
    db.add(models.DocumentAsset(case_id=case_id, filename=body.filename,
                                filename_en=_fn_en(body.filename), doc_type="product_photo",
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
    return [{"document_id": d.document_id, "filename": d.filename,
             "filename_en": d.filename_en, "has_file": bool(d.content_b64),
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


@app.post("/cases/{case_id}/documents/re-classify")
def bulk_reclassify_documents(case_id: str,
                              user=Depends(auth.require_roles("consultant", "auditor", "admin")),
                              db: Session = Depends(get_db)):
    """문서 파일명 규칙 일괄 재적용(OCR 없이) — 규칙 강화 이전 저장분의 낡은 분류를 교정.
    파일명 규칙이 명시적으로 매칭될 때만 doc_type을 바꾸고, 사유를 함께 반환한다."""
    from .intake import refine_doctype_reason, DOC_KO
    c = _get_case(db, case_id, user)
    changed = []
    for d in db.query(models.DocumentAsset).filter_by(case_id=case_id).all():
        new_dt, why = refine_doctype_reason(d.filename, d.doc_type)
        if why and new_dt != d.doc_type:
            prev = d.doc_type
            d.doc_type = new_dt
            changed.append({"document_id": d.document_id, "filename": d.filename,
                            "from": prev, "from_ko": DOC_KO.get(prev, prev),
                            "to": new_dt, "to_ko": DOC_KO.get(new_dt, new_dt), "reason": why})
    if changed:
        sm.record_event(db, c, c.status, c.status, "documents.bulk_reclassify", user["role"], user["uid"],
                        {"count": len(changed)})
        db.commit()
    return {"changed_count": len(changed), "changed": changed}


@app.patch("/documents/{document_id}/reclassify")
def reclassify_document(document_id: str, body: schemas.DocTypeReq,
                        user=Depends(auth.require_roles("applicant", "consultant",
                                                        "operator", "admin")),
                        db: Session = Depends(get_db)):
    """문서 doc_type 수동 (재)분류 — AI 오분류 교정 (설계 P1-#6).

    신청기업도 고칠 수 있어야 한다. AI가 없는 환경에서는 유형이 파일명으로만 정해지는데,
    올린 사람이 그걸 되돌리지 못하면 수기 진행이 성립하지 않는다(무엇을 낸 서류인지는
    올린 사람이 가장 잘 안다). 누가 무엇을 어떻게 바꿨는지는 그대로 기록된다.
    조직 경계는 _get_case 가 지킨다 — 남의 케이스 서류는 손대지 못한다."""
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
    addr = _clean_ocr_address(agg.get("address"))   # NIB(사업자등록증) 주소 → 회사 주소(OCR 교정)
    if addr and (force or not c.address):
        c.address = addr; filled.append("address")
    faddr = _clean_ocr_address(agg.get("factory_address"))   # 공장등록증 주소 → 공장 주소(OCR 교정)
    if faddr and (force or not c.factory_address):
        c.factory_address = faddr; filled.append("factory_address")
    if agg.get("responsible_person") and (force or not c.responsible_person):
        c.responsible_person = agg["responsible_person"]; filled.append("responsible_person")
    if agg.get("factory_reg_no") and (force or not c.factory_reg_no):
        c.factory_reg_no = agg["factory_reg_no"]; filled.append("factory_reg_no")
    # 회사 주소 도시/국가/우편 + 전화·사업유형·직원수 → profile_ext(스키마 무변경). 공장쪽은 facility에 별도 반영.
    pe = dict(c.profile_ext or {})
    for _k in ("city", "country", "zip"):
        if agg.get(_k) and (force or not pe.get(_k)):
            pe[_k] = agg[_k]; filled.append(_k)
    # 화면 필드 키(office_phone/business_type/total_employee)에 매핑
    _extra = (("office_phone", agg.get("phone")),   # 회사 전화만(공장 전화는 facility.phone에 별도 저장)
              ("business_type", agg.get("business_type")),
              ("total_employee", agg.get("employee_count")),
              ("establishment_date", agg.get("establishment_date")),
              ("corporate_reg_no", agg.get("corporate_reg_no")),
              ("company_name_ko", agg.get("company_name_ko")),
              ("company_name_en", agg.get("company_name_en")),
              ("responsible_person_en", agg.get("responsible_person_en")))
    for _k, _v in _extra:
        if _v not in (None, "") and (force or not pe.get(_k)):
            pe[_k] = _v; filled.append(_k)
    if pe != (c.profile_ext or {}):
        c.profile_ext = pe
        try:
            from sqlalchemy.orm.attributes import flag_modified
            flag_modified(c, "profile_ext")   # JSON 컬럼 in-place 변경 감지
        except Exception:
            pass
    return filled


def _apply_agg_to_case(db, c, agg):
    """추출 필드(회사/NIB/주소/책임자/제품/원재료)를 케이스에 반영 — DocumentAsset 생성은 하지 않음(재처리용)."""
    applied = {"company_set": False, "nib_set": False, "products": 0, "materials": 0,
               "profile": [], "pending": len(agg.get("pending") or [])}
    if agg.get("company_name") and _co_replaceable(db, c):
        c.company_name = agg["company_name"]; applied["company_set"] = True
    if agg.get("nib") and c.nib != agg["nib"]:
        c.nib = agg["nib"]; applied["nib_set"] = True   # 문서 파싱 NIB가 상속/기존값보다 우선
    applied["profile"] += _apply_profile_extras(c, agg)
    have_p = {_norm_material(p.name) for p in db.query(models.Product).filter_by(case_id=c.case_id)}
    _mat_norms = {_norm_material(m) for m in (agg.get("materials") or []) if m}
    for pn in agg.get("products", []):
        pk = _norm_material(pn)
        if pn and pk and pk not in have_p and pk not in _mat_norms:
            db.add(models.Product(case_id=c.case_id, name=pn)); applied["products"] += 1; have_p.add(pk)
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


# ── 업로드 후 자동 파싱 큐 ──────────────────────────────────────────────
# 업로드 응답은 즉시 끝나야 한다(OCR 은 10초씩 걸린다). 그렇다고 파싱을 사람이
# 누를 때까지 미루면, 클라이언트가 혼자 올린 서류는 값이 안 채워진 채 남는다
# (실측: 업로드 0.0초 · doc_type=other 로 저장만 되고 그대로).
# 그래서 저장 직후 큐에 넣고 백그라운드에서 처리한다. OCR 은 별도 워커에서 도니
# 이 스레드는 대기만 하고 웹 메모리를 늘리지 않는다.
_AUTOPARSE = os.environ.get("GLHAC_AUTOPARSE", "1") == "1"
_autoparse_q = None
_autoparse_state = {"queued": 0, "done": 0, "failed": 0, "running": None}


def _autoparse_worker():
    while True:
        item = _autoparse_q.get()
        doc_id, keep_dt = item if isinstance(item, tuple) else (item, False)
        _autoparse_state["running"] = doc_id
        db = SessionLocal()
        try:
            d = db.get(models.DocumentAsset, doc_id)
            if d is not None and d.content_b64:
                c = db.get(models.CaseApplication, d.case_id)
                if c is not None:
                    _reprocess_doc(db, d, c, keep_doc_type=keep_dt)
                    _autoparse_state["done"] += 1
        except Exception as e:  # noqa: BLE001
            # 자동 파싱이 실패해도 업로드는 유효하다 — 사람이 재처리를 누르면 된다.
            _autoparse_state["failed"] += 1
            log.warning("자동 파싱 실패 %s: %s", doc_id, e)
        finally:
            db.close()
            _autoparse_state["running"] = None
            _autoparse_q.task_done()


def autoparse_enqueue(document_id, keep_doc_type=False):
    """업로드 직후 호출 — 큐가 없으면(비활성) 조용히 넘어간다."""
    if not _AUTOPARSE or _autoparse_q is None:
        return False
    _autoparse_q.put((document_id, keep_doc_type))
    _autoparse_state["queued"] += 1
    return True


def _autoparse_start():
    global _autoparse_q
    if not _AUTOPARSE or _autoparse_q is not None:
        return
    import queue
    import threading
    _autoparse_q = queue.Queue(maxsize=500)
    threading.Thread(target=_autoparse_worker, daemon=True).start()


@app.get("/system/autoparse")
def autoparse_status(user=Depends(auth.get_current_user)):
    """자동 파싱 큐 상태 — 서류를 올렸는데 값이 안 채워질 때 여기부터 본다."""
    return {"enabled": _AUTOPARSE,
            "pending": _autoparse_q.qsize() if _autoparse_q else 0,
            **{k: v for k, v in _autoparse_state.items()}}


def _reprocess_doc(db, d, c, dpi=None, apply=True, actor_role="system", actor_id="auto",
                   keep_doc_type=False):
    """문서 1건 재추출·재분류 — 사람이 누른 재처리와 자동 큐가 같은 코드를 쓴다.
    두 벌로 두면 한쪽만 고쳐져 '수동은 되는데 자동은 안 되는' 상태가 생긴다.

    keep_doc_type=True 면 유형은 그대로 두고 본문·필드만 갱신한다(사용자 지정 존중)."""
    from .intake import aggregate_fields, classify, parse_file
    data = base64.b64decode(d.content_b64.split(",")[-1])
    text = parse_file(d.filename, data, dpi=dpi)
    r = classify(d.filename, text)
    prev = d.doc_type
    if not keep_doc_type:
        d.doc_type = r.get("doc_type", "other")
    d.confidence = float(r.get("confidence") or 0)
    fl = dict(r.get("fields") or {})
    # 어느 값이 구조 파서에서 나왔고 어느 값이 LLM 에서 나왔는지 문서에 남긴다.
    # 이게 없으면 나중에 '왜 이 원재료가 자동 반영되지 않았는지' 설명할 수 없다.
    # 스키마 무변경 — fields 안의 예약 키로 둔다.
    _fs = r.get("field_sources") or {}
    if _fs:
        fl["_sources"] = _fs
    # AI 호출이 실패했으면 그 사실을 문서에 남긴다. 남기지 않으면 '분석했는데 아무것도
    # 없는 서류'와 구분되지 않아, 사람이 다시 돌려야 한다는 걸 알 방법이 없다.
    if r.get("llm_error"):
        fl["_llm_error"] = r["llm_error"]
    d.fields = fl
    d.text_excerpt = (text or "")[:300]
    d.translations = None  # 원문 재추출 → 기존 번역 캐시 무효화
    applied = (_apply_agg_to_case(db, c, aggregate_fields([{"doc_type": d.doc_type,
                                                            "fields": d.fields}]))
               if apply else {"skipped": True})
    sm.record_event(db, c, c.status, c.status, "documents.reprocess", actor_role, actor_id,
                    {"document_id": d.document_id, "from": prev, "to": d.doc_type,
                     "text_len": len(text or ""), "applied": applied})
    db.commit()
    return {"document_id": d.document_id, "doc_type": d.doc_type,
            "confidence": d.confidence, "text_len": len(text or ""), "applied": applied}


def _pending_extractions(db, case_id):
    """LLM 만 뽑아 **아직 케이스에 없는** 제품·원재료 — 문서에 남긴 출처(fields._sources)로 되짚는다.

    이미 반영된 값은 빼야 한다. 종전에는 문서에 남아 있는 LLM 추출값을 전부 내보내서,
    사람이 확인해 반영한 뒤에도 같은 항목이 계속 대기 목록에 남았다(실측: 어떤 케이스는
    대기 157건 전부가 이미 케이스에 있는 값이었고, 정작 새로 볼 것은 0건이었다).
    그러면 목록이 '봐야 할 것'을 알려주지 못하고 그냥 소음이 된다.

    같은 값인지 판단은 반영 쪽(_apply_agg_to_case)과 같은 정규화(_norm_material)를 쓴다 —
    기준이 다르면 반영했는데도 안 사라지는 항목이 생긴다."""
    from .intake import _MATERIAL_SRC, _PRODUCT_SRC, aggregate_fields
    docs = [{"doc_type": d.doc_type, "fields": d.fields or {},
             "document_id": d.document_id, "filename": d.filename}
            for d in db.query(models.DocumentAsset).filter_by(case_id=case_id).all()
            if d.fields]
    agg = aggregate_fields([{"doc_type": x["doc_type"], "fields": x["fields"]} for x in docs])
    have_p = {_norm_material(p.name) for p in
              db.query(models.Product).filter_by(case_id=case_id) if p.name}
    have_m = {_norm_material(m.name) for m in
              db.query(models.Material).filter_by(case_id=case_id) if m.name}
    # 어느 문서에서 나왔는지 붙여 준다 — 확인하는 사람이 원본을 열어봐야 한다
    out = []
    for it in (agg.get("pending") or []):
        _have = have_p if it["kind"] == "product_names" else have_m
        if _norm_material(it["value"]) in _have:
            continue
        src = next((x for x in docs
                    if it["value"] in ((x["fields"].get(it["kind"]) or []))), None)
        out.append({**it,
                    "document_id": src["document_id"] if src else None,
                    "filename": src["filename"] if src else None})
    return out


def _failed_analyses(db, case_id):
    """AI 호출이 실패해 분석이 비어 있는 서류 — 재처리가 필요하다는 표시.

    실패를 목록으로 내주지 않으면 화면에서는 '유형 기타·추출 0건'으로 보여, 분석이
    끝난 평범한 서류와 똑같다. 다시 돌려야 하는 서류를 사람이 골라낼 수 있어야 한다."""
    out = []
    for d in db.query(models.DocumentAsset).filter_by(case_id=case_id).all():
        err = (d.fields or {}).get("_llm_error")
        if err:
            out.append({"document_id": d.document_id, "filename": d.filename,
                        "doc_type": d.doc_type, "error": err,
                        "note": "AI 분석 실패 — 재처리가 필요합니다(분석 결과 아님)."})
    return out


@app.get("/cases/{case_id}/pending-extractions")
def list_pending_extractions(case_id: str, user=Depends(auth.get_current_user),
                             db: Session = Depends(get_db)):
    """확인 대기 목록 — AI 가 뽑았지만 자동 반영하지 않은 제품·원재료.

    LLM 출력은 공급자마다, 같은 모델에서도 실행마다 다르다. 그대로 넣으면 배포에 따라
    심사 대상이 달라져, 사람이 한 번 보고 넣기로 했다."""
    _get_case(db, case_id, user)
    items = _pending_extractions(db, case_id)
    failed = _failed_analyses(db, case_id)
    return {"count": len(items), "items": items,
            "failed_count": len(failed), "failed": failed,
            "note": "AI 추출값입니다 — 원본 서류와 대조한 뒤 반영하세요."}


@app.post("/cases/{case_id}/pending-extractions/apply")
def apply_pending_extractions(case_id: str, body: dict = None,
                              user=Depends(rbac.require_action("material.add")),
                              db: Session = Depends(get_db)):
    """확인한 항목만 케이스에 반영. body.values 미지정이면 전량 반영.

    누가 무엇을 넣었는지 남긴다 — AI 가 뽑은 값을 사람이 승인한 것이므로
    나중에 '이 원재료가 어디서 왔나'를 되짚을 수 있어야 한다."""
    c = _get_case(db, case_id, user)
    b = body or {}
    want = set(b.get("values") or [])
    items = [x for x in _pending_extractions(db, case_id)
             if not want or x["value"] in want]
    agg = {"products": [x["value"] for x in items if x["kind"] == "product_names"],
           "materials": [x["value"] for x in items if x["kind"] == "material_names"]}
    applied = _apply_agg_to_case(db, c, agg)
    _audit(db, user, "extraction.apply", "case", case_id, case_id,
           {"products": agg["products"][:50], "materials": agg["materials"][:50],
            "source": "llm_confirmed"}, commit=False)
    db.commit()
    return {"applied": applied, "count": len(items)}


@app.post("/documents/{document_id}/reprocess")
def reprocess_document(document_id: str, dpi: int = None, apply: bool = True,
                       user=Depends(auth.require_roles("consultant", "operator")),
                       db: Session = Depends(get_db)):
    """저장된 원본을 재추출·재분류 — OCR/의존성 개선 후 구업로드 문서 치유(설계 B).
    dpi 지정 시 스캔 문서를 고해상도로 재OCR(예: dpi=300, confident-misread 완화 시도).
    apply=false면 문서 doc_type·fields만 갱신하고 케이스 제품/원재료 자동채움은 건너뜀(정리된 목록 보존)."""
    d = db.get(models.DocumentAsset, document_id)
    if not d:
        raise HTTPException(404, {"code": "DOC_NOT_FOUND"})
    c = _get_case(db, d.case_id, user)
    if not d.content_b64:
        raise HTTPException(422, {"code": "NO_CONTENT", "detail": "원본 미보관 문서는 재처리 불가"})
    return _reprocess_doc(db, d, c, dpi=dpi, apply=apply,
                          actor_role=user["role"], actor_id=user["uid"])


_LANG_NAME = {"id": "인도네시아어(Bahasa Indonesia)", "en": "영어(English)"}
_LANG_EN = {"id": "Indonesian (Bahasa Indonesia)", "en": "English"}
# 번역 전용 모델 — gemma3:12b는 한글을 되돌려(echo) 번역 실패 → qwen2.5:7b가 KO→ID 안정적
_TRANSLATE_MODEL = os.environ.get("GLHAC_TRANSLATE_MODEL", "qwen2.5:7b")


def _hangul_ratio(s):
    letters = [ch for ch in s if ch.isalpha()]
    if not letters:
        return 0.0
    return sum(1 for ch in letters if "가" <= ch <= "힣") / len(letters)


def detect_doc_lang(text):
    """문서 원문 언어 추정 — ko|id|en. 서류는 한국어·인도네시아어·영어로 들어온다.
    한글이 섞여 있으면 ko(혼재 문서도 번역 대상), 아니면 인도네시아어 불용어 빈도로 id/en 판별."""
    t = (text or "").strip()
    if not t:
        return "en"
    if _hangul_ratio(t) > 0.05:
        return "ko"
    low = " %s " % re.sub(r"[^a-zA-Z ]+", " ", t.lower())
    id_hits = sum(low.count(" %s " % w) for w in
                  ("dan", "yang", "untuk", "dengan", "pada", "tidak", "adalah", "dari",
                   "ini", "atau", "produk", "bahan", "perusahaan", "nomor", "tanggal"))
    en_hits = sum(low.count(" %s " % w) for w in
                  ("and", "the", "for", "with", "not", "is", "of", "this", "or",
                   "product", "material", "company", "number", "date"))
    return "id" if id_hits > en_hits else "en"


def _translate_text(text, lang, src=None):
    """문서 번역 — qwen2.5(영문지시)로 청크 분할. 실패 시 ''(부분 결과를 내지 않는다).
    원문 언어를 감지해 지시를 맞춘다(한국어 고정이면 인니·영문 문서에 틀린 지시가 나간다).
    원문과 목표가 같으면 번역하지 않고 원문을 돌려준다 — 지어내지 않게.
    한글 echo(번역실패) 청크는 1회 재시도. gemma3는 KO를 그대로 반환해 부적합."""
    src = src or detect_doc_lang(text)
    if src == (lang or "").lower():
        return (text or "").strip()
    tgt = _LANG_EN.get(lang, _LANG_NAME.get(lang, lang))
    srcname = {"ko": "Korean", "id": "Indonesian (Bahasa Indonesia)", "en": "English"}.get(src, "Korean")
    # 도메인 고유명사는 번역하지 말라고 이름을 대준다. "proper nouns"만으로는 부족했다
    # — 실측에서 'penyelia halal'(할랄감독자)이 'pemegang kehalalan'이라는 없는 말로
    # 바뀌었다. 심사 문서에서 직책·기관 이름이 바뀌면 그 문서는 틀린 문서가 된다.
    from . import domain_dict as _dd
    _keep = _dd.protected_terms(lang)
    _keepmsg = (" NEVER translate or alter these terms — copy them exactly: %s."
                % ", ".join(_keep[:24])) if _keep else ""
    sysmsg = ("You are a professional document translator. Translate the given %s text into "
              "%s. Keep proper nouns, registration/business numbers, dates, and figures as-is.%s "
              "Preserve line breaks. Output ONLY the translation — no %s characters, "
              "no explanations, no preamble." % (srcname, tgt, _keepmsg, srcname))
    out = []
    for i in range(0, len(text), 1500):
        ch = text[i:i + 1500]
        if not ch.strip():
            continue
        r = ai_local.llm_text_result(sysmsg, ch, model=_TRANSLATE_MODEL)
        # 호출이 실패한 조각은 빈칸으로 두면 안 된다. 다른 조각이 성공하면 전체 결과는
        # '번역됨'으로 보이고 그대로 캐시돼, 문서 한가운데가 조용히 사라진다.
        # 한 조각이라도 실패하면 번역 자체를 포기한다 — 구멍 난 번역을 저장하느니 낫다.
        if r["error"]:
            log.warning("번역 중단 — 조각 %d 호출 실패: %s", i // 1500, r["error"])
            return ""
        res = r["text"] or ""
        # 재시도 신호 둘: (1) 한국어 원문인데 한글이 남았다 (2) 보호 용어가 사라졌다.
        # 둘 다 조용한 오역이라 결과만 보면 알 수 없다. 재시도해도 안 되면 그대로 둔다
        # — 지어낸 번역을 계속 굴리는 것보다 한 번 더 시도하고 멈추는 편이 낫다.
        _lost = _dd.missing_protected(ch, res, lang)
        if (src == "ko" and _hangul_ratio(res) > 0.15) or _lost:
            if _lost:
                log.info("번역 보호용어 소실 재시도: %s", ", ".join(_lost[:5]))
            r2 = ai_local.llm_text_result(sysmsg, ch, model=_TRANSLATE_MODEL)
            if r2["error"]:
                log.warning("번역 중단 — 재시도 실패: %s", r2["error"])
                return ""
            res = r2["text"] or res
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
        raise HTTPException(502, {"code": "TRANSLATE_FAILED",
                                  "detail": "번역 엔진 호출 실패 — 번역본을 저장하지 않았습니다. 잠시 후 다시 시도하세요."})
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


def _auto_link_pm(db, case_id, max_pairs=2000):
    """제품-원재료 자동 연결. 기존 링크는 보존하고 미연결 (제품,원재료) 쌍만 추가.
    과적재(제품×원재료>max_pairs) 케이스는 폭발 방지로 스킵."""
    prods = db.query(models.Product).filter_by(case_id=case_id).all()
    mats = db.query(models.Material).filter_by(case_id=case_id).all()
    if not prods or not mats or len(prods) * len(mats) > max_pairs:
        return 0
    existing = {(lk.product_id, lk.material_id) for lk in
                db.query(models.ProductMaterial).filter_by(case_id=case_id).all()}
    added = 0
    for p in prods:
        for m in mats:
            key = (p.product_id, m.material_id)
            if key in existing:
                continue
            db.add(models.ProductMaterial(case_id=case_id, product_id=p.product_id,
                                          material_id=m.material_id))
            existing.add(key)
            added += 1
    return added


_CO_PLACEHOLDER = ("", "My Company", "scan", "ABC", "T", "UI", "Demo Co", "Demo Co FE")
# 플레이스홀더/테스트 상호 패턴 — 공식 문서 추출 회사명이 덮어써야 하는 대상
_CO_PLACEHOLDER_PAT = (
    r"(?i)(test|demo|sample|dummy|example|placeholder|미정|샘플|테스트|데모|예시|심사\s*패스|\bn/?a\b)")


def _is_co_placeholder(name):
    """빈값·테스트/데모/샘플 등 placeholder 성격의 상호면 True → 공식 추출값으로 덮어쓰기 허용."""
    import re
    if not name or not str(name).strip():
        return True
    n = str(name).strip()
    return n in _CO_PLACEHOLDER or bool(re.search(_CO_PLACEHOLDER_PAT, n))


def _co_replaceable(db, c):
    """회사명을 문서 추출값으로 덮어도 되는가 — 빈값/placeholder이거나,
    org 조직명 그대로(=사용자 미입력, 케이스 생성 시 자동 상속된 초기값)이면 True.
    사용자가 손수 입력한 회사명은 보존한다."""
    if _is_co_placeholder(c.company_name):
        return True
    try:
        org = db.get(models.Org, c.org_id)
        return bool(org and org.name and c.company_name == org.name)
    except Exception:
        return False


# 스캔 OCR 주소의 흔한 오인식 사전(도시·주 오탈자)
_OCR_ADDR_FIX = {
    r"\bJaka[ir]ta\b": "Jakarta", r"\bJakatta\b": "Jakarta", r"\bJakara\b": "Jakarta",
    r"\bSurabaia\b": "Surabaya", r"\bBandimg\b": "Bandung", r"\bSemarrang\b": "Semarang",
    r"\bBekas[il]\b": "Bekasi", r"\bTangerrang\b": "Tangerang",
}


def _clean_ocr_address(s):
    """스캔 OCR 주소 교정 — 'N0 24'→'No. 24', 도시/주 오탈자, 로마숫자 lll→III, 쉼표 간격."""
    import re
    if not s or not isinstance(s, str):
        return s
    out = s
    out = re.sub(r"\bN0(?=[\s.]*\d)", "No.", out)          # 문자 N+숫자 0 → No.
    for pat, rep in _OCR_ADDR_FIX.items():                 # 도시·주 오탈자
        out = re.sub(pat, rep, out, flags=re.I)
    out = re.sub(r"\bDK[lI]?\s+Jakarta\b", "DKI Jakarta", out, flags=re.I)   # DK/DKl Jakarta → DKI
    out = re.sub(r"(?<=\s)[lI]{2,4}(?=[\s,])", lambda m: "I" * len(m.group()), out)  # lll → III
    out = re.sub(r"\s+,", ",", out)                        # 쉼표 앞 공백 제거
    out = re.sub(r",(?=\S)", ", ", out)                    # 쉼표 뒤 공백 보정
    out = re.sub(r"\s{2,}", " ", out)
    return out.strip()


def _set_hpas(db, case_id, element, status, note=None):
    """SJPH/HPAS 5요소 상태 upsert(commitment|materials|process|product|monitoring)."""
    row = db.query(models.HpasEvaluation).filter_by(case_id=case_id, element=element).first()
    if not row:
        row = models.HpasEvaluation(case_id=case_id, element=element)
        db.add(row)
        db.flush()   # 같은 인테이크의 다음 _set_hpas가 이 row를 보도록(중복 add→UNIQUE 충돌 방지)
    row.status = status
    if note:
        row.note = note


def _process_doc(db, c, d, applied):
    """문서 타입별 개별 프로세서 — 분류된 각 파일을 해당 엔티티로 매핑(공정흐름도/할랄인증서/SJPH매뉴얼)."""
    dt = d.get("doc_type")
    flds = d.get("fields") or {}
    if dt == "process_flow":
        steps = [s for s in (flds.get("process_steps") or []) if s]
        if steps:
            _set_hpas(db, c.case_id, "process", "ok",
                      "공정흐름도 제출: " + " → ".join(steps[:8]))
            applied["process_flow"] = len(steps)
    elif dt == "halal_certificate":
        cn = flds.get("cert_no")
        if cn and not db.query(models.HalalCertificate).filter_by(
                case_id=c.case_id, certificate_no=cn).first():
            # 여기 날짜는 **AI 가 서류에서 읽은 값**이다. 그대로 status=active 인증서로
            # 저장하면 나중에 date.fromisoformat 으로 읽는 곳(만료 경보·유효성 판정)이
            # 죽는다. 읽히지 않는 날짜는 비워 두고, 앞뒤가 뒤집힌 쌍은 둘 다 버린다 —
            # 지어낸 유효기간으로 '유효한 인증서'를 만들지 않는다.
            _iss, _exp = _safe_date(flds.get("issue_date")), _safe_date(flds.get("expiry_date"))
            _note = None
            if _iss and _exp and _exp <= _iss:
                _note, _iss, _exp = "DATE_RANGE_INVALID", None, None
            db.add(models.HalalCertificate(
                case_id=c.case_id, certificate_no=cn,
                issue_date=_iss, expiry_date=_exp,
                scope=flds.get("scope"), status="active"))
            applied["halal_cert"] = cn
            if _note:
                applied["halal_cert_note"] = _note
    elif dt == "sjph_manual":
        _m = {"commitment": flds.get("has_commitment"), "materials": flds.get("has_materials"),
              "process": flds.get("has_process"), "product": flds.get("has_product"),
              "monitoring": flds.get("has_monitoring")}
        _cnt = 0
        for _el, _ok in _m.items():
            if _ok:
                _set_hpas(db, c.case_id, _el, "ok", "SJPH 매뉴얼에서 감지")
                _cnt += 1
        if _cnt:
            applied["sjph_manual"] = _cnt


def _sha256_b64(content_b64):
    """base64(dataURL 접두 허용) 콘텐츠의 sha256 hex. 내용 없으면 None — 중복판정·무결성 공용."""
    if not content_b64:
        return None
    import hashlib as _hl
    try:
        _raw = base64.b64decode(str(content_b64).split(",")[-1])
    except Exception:  # noqa: BLE001
        _raw = str(content_b64).encode("utf-8", "ignore")
    return _hl.sha256(_raw).hexdigest()


def _apply_intake_autofill(db, c, res):
    """분류 결과 → DocumentAsset 저장 + 신청서 자동채움(회사/NIB/제품/원재료). 임시저장(commit은 호출측).
    중복 방지: 같은 케이스에 동일 내용(file_hash) 또는 동일 파일명(내용없는 문서)은 재저장하지 않는다."""
    _ex = (db.query(models.DocumentAsset.file_hash, models.DocumentAsset.filename)
             .filter(models.DocumentAsset.case_id == c.case_id).all())
    _seen_h = {h for h, _ in _ex if h}
    _seen_n = {n for h, n in _ex if not h and n}
    dup_skipped = 0
    for d in res["classified"]:
        _h = _sha256_b64(d.get("content_b64"))
        _nm = d["filename"]
        if (_h and _h in _seen_h) or (not _h and _nm in _seen_n):
            dup_skipped += 1
            continue
        if _h:
            _seen_h.add(_h)
        else:
            _seen_n.add(_nm)
        db.add(models.DocumentAsset(case_id=c.case_id, filename=_nm, filename_en=_fn_en(_nm), doc_type=d["doc_type"],
                                    confidence=float(d.get("confidence") or 0), fields=d.get("fields"),
                                    text_excerpt=d.get("excerpt"), ocr_lines=d.get("ocr_lines"),
                                    content_b64=d.get("content_b64"),
                                    content_type=d.get("content_type"), file_hash=_h))
    agg = res.get("extracted", {})
    applied = {"company_set": False, "nib_set": False, "products": 0, "materials": 0, "profile": [],
               "dup_skipped": dup_skipped}
    if agg.get("company_name") and _co_replaceable(db, c):
        c.company_name = agg["company_name"]
        applied["company_set"] = True
    if agg.get("nib") and c.nib != agg["nib"]:
        c.nib = agg["nib"]   # 문서 파싱 NIB가 상속/기존값보다 우선(공식 문서 기준)
        applied["nib_set"] = True
    applied["profile"] += _apply_profile_extras(c, agg)
    # 주소 최종 재정리(OCR 글리치 — 경로 무관 보장)
    if c.address:
        c.address = _clean_ocr_address(c.address)
    if c.factory_address:
        c.factory_address = _clean_ocr_address(c.factory_address)
    have_p = {_norm_material(p.name) for p in db.query(models.Product).filter_by(case_id=c.case_id)}
    _mat_norms = {_norm_material(m) for m in (agg.get("materials") or []) if m}  # 원재료명과 겹치는 제품 오추출 제외
    for pn in agg.get("products", []):
        pk = _norm_material(pn)
        if pn and pk and pk not in have_p and pk not in _mat_norms:
            db.add(models.Product(case_id=c.case_id, name=pn))
            applied["products"] += 1
            have_p.add(pk)
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
    # 공장등록증 파싱 → Facility 자동 생성/갱신(오피스 자산) + 이 신청 대상 연결
    if agg.get("factory_address") or agg.get("factory_reg_no"):
        _reg = agg.get("factory_reg_no")
        _fac = None
        if _reg:
            _fac = db.query(models.Facility).filter_by(org_id=c.org_id, reg_no=_reg).first()
        if not _fac and agg.get("factory_address"):
            _fac = db.query(models.Facility).filter_by(org_id=c.org_id,
                                                       address=agg["factory_address"]).first()
        # reg·주소 매칭 실패 시 같은 회사명 공장 재사용(주소 표기 변동에 의한 중복 생성 방지)
        if not _fac:
            _nm = agg.get("company_name") or c.company_name
            if _nm:
                _fac = db.query(models.Facility).filter_by(org_id=c.org_id, name=_nm).first()
        # 이름·주소 단서가 있을 때만 생성(빈 공장 생성 방지)
        if not _fac and (agg.get("factory_address") or agg.get("company_name") or c.company_name):
            _fac = models.Facility(org_id=c.org_id,
                                   name=agg.get("company_name") or c.company_name or "공장")
            db.add(_fac)
        if _fac:
            if agg.get("factory_address"):
                _fac.address = agg["factory_address"]
            if agg.get("factory_city"):
                _fac.city = agg["factory_city"]
            if agg.get("factory_country"):
                _fac.country = agg["factory_country"]
            if agg.get("factory_zip"):
                _fac.zip = agg["factory_zip"]
            if _reg:
                _fac.reg_no = _reg
            if agg.get("factory_phone"):   # 공장 전화 → facility.profile_ext(스키마 무변경)
                _fpe = dict(_fac.profile_ext or {})
                _fpe["phone"] = agg["factory_phone"]
                _fac.profile_ext = _fpe
                try:
                    from sqlalchemy.orm.attributes import flag_modified
                    flag_modified(_fac, "profile_ext")
                except Exception:
                    pass
            db.flush()
            _fids = list(c.facility_ids or [])
            if _fac.facility_id not in _fids:
                _fids.append(_fac.facility_id)
                c.facility_ids = _fids
            applied["facility"] = _fac.facility_id
    # 제품·원재료 자동 연결(매트릭스) — 아코디언에 원재료가 붙도록
    applied["links"] = _auto_link_pm(db, c.case_id)
    # 문서 타입별 개별 프로세서 — 공정흐름도/할랄인증서/SJPH매뉴얼을 각 엔티티로 매핑
    for _d in res.get("classified", []):
        _process_doc(db, c, _d, applied)
    # CoA/성적서 정량 측정값 자동 반영 → MaterialMeasurement (기준 대비 pass/fail 자동 판정)
    import uuid as _uuid
    _mcount = 0
    for _d in res.get("classified", []):
        _meas = (_d.get("fields") or {}).get("measurements") or {}
        for _pk, _val in _meas.items():
            _crit = QUANT_CRITERIA.get(_pk)
            if not _crit or _val is None:
                continue
            _mx = _crit.get("max")
            _vd = "pass" if (_mx is not None and float(_val) <= float(_mx)) else "fail"
            _ex = (db.query(models.MaterialMeasurement)
                   .filter_by(case_id=c.case_id, param_key=_pk).first())
            if _ex:
                _ex.value = _val; _ex.unit = _crit.get("unit"); _ex.verdict = _vd
            else:
                db.add(models.MaterialMeasurement(
                    measurement_id=_uuid.uuid4().hex, case_id=c.case_id, param_key=_pk,
                    value=_val, unit=_crit.get("unit"), verdict=_vd,
                    lab_name=(_d.get("fields") or {}).get("issuer"), recorded_by="ai"))
            _mcount += 1
    if _mcount:
        applied["measurements"] = _mcount
    # 사전심사 업로드 → 신청서 임시저장 진입(작성 이어하기 대상)
    if c.status in ("onboarding", "application_draft"):
        c.status = "application_draft"
        if not c.draft_state or c.draft_state in ("returned",):
            c.draft_state = "saved"
    res["applied"] = applied
    return applied


@app.post("/cases/{case_id}/auto-link-materials")
def auto_link_materials_ep(case_id: str,
                           user=Depends(auth.require_roles("applicant", "consultant", "penyelia_halal")),
                           db: Session = Depends(get_db)):
    """기존 케이스 소급 — 제품-원재료 자동 연결 실행(수동 매핑은 보존)."""
    c = _get_case(db, case_id, user)
    added = _auto_link_pm(db, c.case_id)
    db.commit()
    total = db.query(models.ProductMaterial).filter_by(case_id=c.case_id).count()
    return {"added": added, "total_links": total}


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
    # 압축을 못 여는 건 서버 잘못이 아니라 입력 문제다 — 스트림 시작 전에 422로 돌려준다.
    # 예전에는 생성기 안에서 BadZipFile 이 터져 500이 났고, 화면은 그걸 '완료'로 표시했다.
    from .intake import ArchiveError, archive_kind
    if archive_kind(raw) is None:
        raise HTTPException(422, {"code": "NOT_AN_ARCHIVE",
                                  "message": "압축 파일이 아닙니다 (ZIP·RAR·7z 만 지원합니다)"})

    def gen():
        """진행상황은 파일이 끝나는 즉시 흘려보낸다.

        list(...)로 감싸면 안 된다 — 16건이면 10분 넘게 한 글자도 나가지 않아 화면은 멈춘
        것처럼 보이고, ngrok 같은 프록시는 유휴 연결로 보고 503으로 끊는다.
        실측: 파일 3건에 첫 줄이 155.9초 만에 도착(총 156.0초)했다.
        압축 오류는 첫 next() 에서 나므로 반복문을 감싸면 지연 없이 잡을 수 있다."""
        def ev(obj):
            # SSE 프레이밍 — 프록시는 application/x-ndjson 을 통째로 모았다가 내보내지만
            # text/event-stream 은 실시간으로 통과시킨다(실측: 터널 ndjson 5줄이 5.6초에
            # 몰려 도착 / SSE 는 0.1·1.2·2.3·3.4·4.5초로 정상). 본문 형식은 그대로 JSON.
            return "data: " + _json.dumps(obj, ensure_ascii=False) + "\n\n"

        res = None
        try:
            for kind, payload in intake_zip_iter(raw):
                if kind in ("start", "progress"):
                    yield ev({"type": kind, **payload})
                elif kind == "result":
                    res = payload
        except ArchiveError as e:
            yield ev({"type": "error", "message": str(e)})
            return
        if res is None:
            yield ev({"type": "error", "message": "인테이크 결과가 없습니다"})
            return
        applied = _apply_intake_autofill(db, c, res)
        sm.record_event(db, c, c.status, c.status, "documents.intake", "ai", user["uid"],
                        {"file_count": res["file_count"], "missing": res["missing"], "applied": applied})
        db.commit()
        yield ev({"type": "result", **res})

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


@app.patch("/cases/{case_id}/profile")
def update_profile(case_id: str, body: schemas.CaseProfileReq,
                   user=Depends(auth.require_roles("applicant", "consultant")),
                   db: Session = Depends(get_db)):
    c = _get_case(db, case_id, user)
    # 같은 사업자번호를 **다른 업체**가 쓰고 있으면 둘 중 하나는 잘못 적은 것이다.
    # 같은 업체의 여러 케이스(신규·갱신)는 정상이므로 막지 않는다.
    #
    # 판단 기준을 org 로만 두면 안 된다 — 실측: 라이브에서 바이오로제트·질경이·천우건설
    # 세 회사가 같은 번호(207-81-34847)를 쓰고 있는데 셋 다 org_demo 라 org 기준으로는
    # 안 걸린다(데모·운영 초기에는 여러 업체가 한 org 에 모인다). 회사명까지 본다.
    if body.nib:
        # 비교는 **정규화끼리** 한다. 기존 행에는 en-dash 가 그대로 남아 있어(실측 3건)
        # SQL 동등비교로는 같은 번호가 안 잡힌다 — 바로 이 검사가 막으려던 그 상황이다.
        _name = (body.company_name or c.company_name or "").strip()
        _key = schemas.normalize_nib(body.nib)
        _cands = [x for x in db.query(models.CaseApplication).filter(
            models.CaseApplication.nib.isnot(None),
            models.CaseApplication.case_id != case_id).all()
            if schemas.normalize_nib(x.nib) == _key]
        for other in _cands:
            same_company = (other.org_id == c.org_id
                            and (other.company_name or "").strip() == _name)
            if not same_company:
                raise HTTPException(409, {
                    "code": "NIB_ALREADY_USED", "nib": body.nib,
                    "used_by": other.company_name,
                    "message": "다른 업체(%s)가 사용 중인 사업자 식별번호입니다."
                               % (other.company_name or "-")})
    # 비운 칸은 '지운 것'으로 본다.
    #
    # 종전에는 값이 None 이면 그냥 건너뛰어, 한 번 잘못 들어간 값을 화면에서 지울 방법이
    # 없었다(실측: 사업자번호에 '123' 이 박힌 케이스는 그 칸을 비워도 DB 에 그대로 남고,
    # 화면이 그 값을 다시 보내니 저장이 매번 422 로 막혔다 — 손쓸 수 없는 상태).
    # 인도네시아 업체는 한국 사업자등록번호가 없고 NIB 도 발급 전일 수 있어, 비워 두는 것이
    # 정상적인 상태다. 보낸 적 없는 필드(model_fields_set 밖)는 그대로 둔다.
    _sent = body.model_fields_set
    for f in ("company_name", "nib", "responsible_person", "halal_supervisor", "email",
              "phone", "address", "factory_reg_no", "factory_address", "due_date"):
        v = getattr(body, f)
        if v is not None:
            setattr(c, f, v)
        elif f in _sent:
            setattr(c, f, None)
    if body.notify_consent is not None:
        c.notify_consent = bool(body.notify_consent)
    if body.phone is not None:
        c.phone = _normalize_phone(body.phone)   # 국가코드 정규화
    if body.profile_ext is not None:
        c.profile_ext = {**(c.profile_ext or {}), **body.profile_ext}  # 확장 양식 병합 저장
        # BPJPH 자기선언 정량 필드 → 컬럼 미러(가드 판정용).
        # 값 검증은 이제 스키마(CaseProfileReq._v_ext)가 앞에서 한다 — 여기 도달하는 값은
        # 이미 int 또는 None 이다. 아래 try 는 옛 데이터·직접 호출 대비 잔여 방어일 뿐이라
        # 조용히 넘겨도 값이 사라지지 않는다(예전에는 여기서 조용히 사라졌다).
        _ext = c.profile_ext or {}
        for _k, _col in (("annual_revenue", "annual_revenue"), ("outlet_count", "outlet_count")):
            if _k in _ext:
                _v = _ext[_k]
                try:
                    setattr(c, _col, int(_v) if _v not in (None, "") else None)
                except (ValueError, TypeError):
                    log.warning("정량 필드 변환 실패 — 무시: %s=%r (case=%s)", _k, _v, c.case_id)
        # 사업 규모(Skala Usaha) → MSME 컬럼 미러. 자기선언 자격과 필수 서류 범위가
        # 여기서 갈리는데, 지금까지 규모는 케이스 생성 시점에만 정할 수 있어 정정할 길이
        # 없었다. 사전이 모르는 표기는 건드리지 않는다(지어낸 규모로 경로를 열지 않는다).
        if _ext.get("business_scale"):
            from . import domain_dict as _dd
            _ms = _dd.is_msme_scale(_ext["business_scale"])
            if _ms is not None and bool(c.is_msme) != _ms:
                sm.record_event(db, c, c.status, c.status, "case.scale.update",
                                user["role"], user["uid"],
                                {"business_scale": _ext["business_scale"],
                                 "is_msme": _ms, "was": bool(c.is_msme)})
                c.is_msme = _ms
    if not c.draft_state or c.draft_state == "returned":
        c.draft_state = "in_progress"  # 편집 시작 → 작성중(반려분 재편집 포함)
    # 회사 프로필 → org 미러(회사 자산 정본) — 다음 신청이 최신 회사정보를 상속
    org_row = db.get(models.Org, c.org_id)
    if not org_row:   # 경량 org(row 없음) 케이스 — 미러 대상 생성
        org_row = models.Org(org_id=c.org_id, name=c.company_name)
        db.add(org_row)
    if org_row:
        snap = dict(org_row.profile_ext or {})
        for k in ("company_name", "nib", "responsible_person", "halal_supervisor", "email",
                  "phone", "address", "factory_reg_no", "factory_address"):
            v = getattr(c, k)
            if v is not None:
                snap[k] = v
        if body.profile_ext:
            snap.update(body.profile_ext)
        org_row.profile_ext = snap
        if c.company_name and not org_row.name:
            org_row.name = c.company_name
        if c.address:
            org_row.address = c.address
    db.commit()
    return _case_dict(c)


@app.patch("/cases/{case_id}/facilities-select")
def select_facilities(case_id: str, body: schemas.FacilitySelectReq,
                      user=Depends(auth.require_roles("applicant", "consultant")),
                      db: Session = Depends(get_db)):
    """이 신청 대상 공장 선택·분류(오피스 공장 중 선택) — facility_ids 저장."""
    c = _get_case(db, case_id, user)
    # 소속 오피스(org)의 공장만 허용 — 다른 회사가 쓰는 공장은 선택 대상이 아니다
    valid = {f.facility_id for f in _facilities_for_case(db, c)}
    c.facility_ids = [fid for fid in (body.facility_ids or []) if fid in valid]
    db.commit()
    return {"facility_ids": c.facility_ids}


@app.post("/cases/{case_id}/assign-consultant")
def assign_consultant(case_id: str, body: dict = None,
                      user=Depends(auth.require_roles("operator", "admin")),
                      db: Session = Depends(get_db)):
    """컨설턴트 배정 — 신규 가입 업체를 담당자에게 넘기는 유일한 경로.

    지금까지 컨설턴트 배정 개념이 없어서, 자기 조직이 아닌 신규 업체는 컨설턴트가
    아예 볼 수 없었다(가입 → 자체 org). 배정 기록이 있어야 접근이 열린다."""
    c = _get_case(db, case_id, user)
    b = body or {}
    uid = (b.get("consultant_id") or "").strip()
    if not uid:
        raise HTTPException(422, {"code": "CONSULTANT_REQUIRED"})
    u = db.query(models.User).filter(
        or_(models.User.user_id == uid, models.User.username == uid)).first()
    if not u or u.role != "consultant":
        raise HTTPException(404, {"code": "CONSULTANT_NOT_FOUND", "given": uid})
    sm.record_event(db, c, c.status, c.status, CONSULTANT_ASSIGN_ACTION,
                    user["role"], user["uid"],
                    {"consultant_id": u.user_id, "consultant_name": u.username,
                     "reason": (b.get("reason") or "").strip() or None})
    _audit(db, user, "case.assign_consultant", "case", case_id, case_id=case_id,
           meta={"consultant_id": u.user_id, "consultant_name": u.username}, commit=False)
    db.commit()
    return {"ok": True, "consultant_id": u.user_id, "consultant_name": u.username}


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

    # 문서만 봐서는 '남의 공장등록증이 붙은' 사고를 못 잡는다 — 케이스에 연결된 시설 자체를
    # 신청자와 대조한다. 국문·영문 상호를 모두 후보로 두고 가장 높은 유사도를 쓴다
    # (신청자 '천우건설' vs 시설 'Dongyang Chemical'처럼 언어가 갈리면 오탐이 난다).
    px = c.profile_ext or {}
    names = [x for x in (c.company_name, px.get("company_name_en"), px.get("company_name_ko")) if x]
    fac_rows = []
    for f in _case_facilities(db, c):
        fpx = f.profile_ext or {}
        fnames = [x for x in (f.name, fpx.get("name_en"), fpx.get("label"),
                              fpx.get("manufacturer_name")) if x]
        sim = max((_name_sim(a2, b2) for a2 in names for b2 in fnames), default=0.0)
        fac_rows.append({"facility_id": f.facility_id, "name": f.name,
                         "name_en": fpx.get("name_en"), "similarity": sim,
                         "is_manufacturer": bool(fpx.get("manufacturer_name") or fpx.get("name_en"))})
    # 신청자≠제조사 구조(유통사 신청)는 정상이므로 '불일치'가 곧 오류는 아니다.
    # 확인이 필요한 대상으로만 표시하고 판단은 오디터가 한다.
    fac_review = [f for f in fac_rows if f["similarity"] < 0.5]
    return {"case_company": c.company_name, "applicant_docs": found,
            "mismatch": mismatch, "best_similarity": best,
            "suggested_company": found[0]["doc_company"] if (mismatch and found) else None,
            "facilities": fac_rows, "facility_review": fac_review,
            "facility_review_count": len(fac_review),
            "note": ("신청자와 이름이 다른 시설이 있습니다 — 위탁제조(OEM)면 정상이나, "
                     "다른 업체의 공장등록증이 잘못 첨부된 것은 아닌지 확인하세요.")
            if fac_review else None}


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
    if agg.get("company_name") and (force or _is_co_placeholder(c.company_name)):
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
    frm = c.status   # P3e: onboarding→application_draft 전이가 감사에 소실되지 않도록 실제 from 캡처
    if c.status in ("onboarding", "application_draft"):
        c.status = "application_draft"
    c.draft_state = "saved"
    sm.record_event(db, c, frm, c.status, "application.save_draft", user["role"], user["uid"])
    db.commit()
    return _case_dict(c)


@app.post("/cases/{case_id}/submit-application")
def submit_application(case_id: str, user=Depends(auth.require_roles("applicant", "consultant")),
                       db: Session = Depends(get_db)):
    """작성완료 제출 — AI 사전평가 체인 경유 후 경로판정 대기로.
    (전수검사 G1 수정: 종전엔 consultant_review로 직접 점프해 pathway_determination을
    건너뛰어 pathway/confirm이 WRONG_STATE로 영구 불가 → 자기선언 경로 진입 차단)

    가드 우회 수정: 종전엔 sm.allowed(인접성)만 보고 GUARDS를 태우지 않아, 회사명·NIB·
    제품이 없는 미완비 신청서도 pathway_determination까지 전진했다(raw /transition은
    가드를 태우므로 동일 전이가 경로에 따라 다르게 통과). 체인 각 단계를 can_transition으로
    검사하고, 막히면 rollback 후 409로 반려한다(부분 전진 금지)."""
    c = _get_case(db, case_id, user)
    prev = c.status
    if c.status in ("onboarding", "application_draft"):
        cur = c.status
        for st in ("application_draft", "ai_pre_assessment_ready",
                   "ai_pre_assessment_running", "pathway_determination"):
            if sm.allowed(cur, st):
                # can_transition은 출발 상태를 case.status에서 읽으므로 c.status를 매 단계 갱신해야 한다.
                can_ok, blockers = sm.can_transition(db, c, st)
                if not can_ok:
                    db.rollback()   # 앞선 단계의 flush된 이벤트까지 되돌려 부분 전진을 남기지 않음
                    raise HTTPException(409, {"code": "TRANSITION_BLOCKED",
                                              "blockers": blockers, "to": st})
                sm.apply_side_effects(c, st)
                sm.record_event(db, c, cur, st, "application.submit.auto", user["role"], user["uid"])
                c.status = st
                cur = st
    # 체인이 모두 통과한 뒤에만 제출 완료로 표시 — 가드에 막히면 draft 상태가 유지된다.
    c.draft_state = "completed"
    c.return_reason = None
    sm.record_event(db, c, prev, c.status, "application.submit", user["role"], user["uid"])
    _notify(db, c, "application.submitted", role="consultant",
            msg=("application.submitted", {"company": c.company_name or c.case_id}))
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


def _safe_date(v):
    """ISO 날짜면 표준형 문자열로, 아니면 None. AI 추출값을 DB 에 넣기 전 관문.

    빈 값과 '못 읽은 값'을 굳이 구분하지 않는다 — 어느 쪽이든 날짜로 쓸 수 없다."""
    if not v:
        return None
    try:
        return date.fromisoformat(str(v).strip()).isoformat()
    except (ValueError, TypeError):
        return None


def _normalize_phone(p):
    """전화 정규화 — 규칙은 schemas 가 정본이다(검증과 저장이 어긋나면 안 된다)."""
    return schemas.normalize_phone(p)


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
    db.add(models.DocumentAsset(case_id=case_id, filename=body.filename,
                                filename_en=_fn_en(body.filename), doc_type=body.doc_type,
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
def list_documents(case_id: str, lang: str = Query("ko"),
                   user=Depends(auth.get_current_user), db: Session = Depends(get_db)):
    _get_case(db, case_id, user)
    DOC_KO = _intake_doc_names(lang)   # 표는 있는데 늘 한국어를 골랐다 — 읽는 사람 언어로
    rows = db.query(models.DocumentAsset).filter_by(case_id=case_id).all()
    return [{"document_id": d.document_id, "filename": d.filename, "doc_type": d.doc_type,
             "doc_type_ko": DOC_KO.get(d.doc_type, d.doc_type),
             "confidence": d.confidence, "fields": d.fields, "excerpt": d.text_excerpt,
             "review_status": d.review_status, "has_file": bool(d.content_b64),
             "created_at": d.created_at.isoformat() if d.created_at else None,
             "lat": d.lat, "lng": d.lng, "geo_source": d.geo_source,
             "uploaded_by": d.uploaded_by, "uploader_role": d.uploader_role,
             "captured_at": d.captured_at, "file_hash": d.file_hash} for d in rows]


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
# HPAS 5요소 표기 — 사전에서. 전에는 여기와 사전에 같은 값이 따로 있었다.
HPAS_KO = _dd_mod.code_labels("HPAS_ELEMENT", "hpas_element", "ko")

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
    """SJPH/HPAS 매뉴얼 결정적 생성 — 공식 템플릿(GLHAC HPAS SJPH Template) 구조로 케이스 데이터 채움.
    결제 게이트: 청구 존재 시 결제완료 필요(§2.2 비용방지). gen-doc 버전 저장(Rizky #3·#11)."""
    c = _get_case(db, case_id, user)
    _invs = db.query(models.Invoice).filter_by(case_id=case_id).all()
    if _invs and not any(i.status == "paid" for i in _invs):
        raise HTTPException(409, {"code": "PAYMENT_REQUIRED",
                                  "detail": "AI SJPH 매뉴얼 생성 전 결제 완료가 필요합니다."})
    blocks = _sjph_manual_blocks(db, c)
    company = c.company_name or c.case_id[:8]
    manual = _sjph_blocks_to_text(company, blocks)
    g = _save_gendoc(db, c, "sjph_manual", manual, user)  # 버전 저장(Rizky #3·#11)
    db.commit()
    return {"manual": manual, "version": g.version, "gen_doc_id": g.gen_doc_id, "blocks": len(blocks)}


# ── 할랄매뉴얼 빌더(§12-5): 섹션 순서·이미지 삽입 상태 저장 ──
# 스키마 무변경 — WorkflowEvent(action="sjph_manual.layout", latest-wins). 생성은 위 sjph/manual 재사용.
# 섹션 상수: (key, KO, EN, has_default) — has_default=False는 이미지 삽입 시 완료.
SJPH_MANUAL_SECTIONS = [
    ("halal_policy", "할랄 기본문구", "Halal Policy", True),
    ("halal_declaration", "할랄 선언서", "Halal Declaration", True),
    ("org_chart", "담당자 · 조직도", "Supervisor & Org Chart", False),
    ("material_process", "재료 · 공정", "Materials & Process", True),
]

# 서명·도장 슬롯 — (key, KO, EN, 기본직책). 스키마 무변경(layout payload.signers)
SJPH_SIGNERS = [
    ("ceo", "대표자 · 책임자", "CEO / Responsible Person", "CEO"),
    ("halal_supervisor", "할랄 감독관", "Halal Supervisor", "Halal Supervisor"),
    ("glhac_auditor", "GL-HAC 심사원", "GL-HAC Halal Auditor", "Halal Auditor"),
    ("glhac_sharia", "GL-HAC 샤리아 위원", "GL-HAC Sharia Board", "Sharia Board"),
]

# 슬롯별 편집 권한(백엔드 역할 기준) — 없는 키는 편집 불가
SJPH_SIGNER_ROLES = {
    "ceo": {"applicant", "consultant", "penyelia_halal", "admin"},
    "halal_supervisor": {"applicant", "consultant", "penyelia_halal", "admin"},
    "glhac_auditor": {"auditor", "fatwa_liaison", "operator", "admin"},
    "glhac_sharia": {"auditor", "fatwa_liaison", "operator", "admin"},
}


def _sjph_manual_layout_latest(db, case_id):
    e = (db.query(models.WorkflowEvent)
         .filter(models.WorkflowEvent.case_id == case_id,
                 models.WorkflowEvent.action == "sjph_manual.layout")
         .order_by(models.WorkflowEvent.created_at.desc()).first())
    return (e.payload or {}) if e else None


def _sjph_norm_signers(raw):
    """layout payload의 signers를 정규화. 알려진 키만, 값은 길이 제한. 빈 항목은 버린다."""
    keys = {s[0] for s in SJPH_SIGNERS}
    if not isinstance(raw, dict):
        return {}
    result = {}
    for k, v in raw.items():
        if k not in keys or not isinstance(v, dict):
            continue
        item = {}
        name = str(v.get("name") or "").strip()[:120]
        pos = str(v.get("position") or "").strip()[:120]
        sid = str(v.get("stamp_document_id") or "").strip()[:64]
        if name:
            item["name"] = name
        if pos:
            item["position"] = pos
        if sid:
            item["stamp_document_id"] = sid
        if item:
            result[k] = item
    return result


def _sjph_manual_layout_view(db, case_id, role=None):
    """저장상태 + 섹션 상수 → 정규화(순서 보정·미지의 키 제거·완료도).
    role을 주면 서명자 슬롯별 편집 가능 여부(can_edit)를 함께 계산한다."""
    saved = _sjph_manual_layout_latest(db, case_id) or {}
    keys = [s[0] for s in SJPH_MANUAL_SECTIONS]
    kset = set(keys)
    meta = {s[0]: {"ko": s[1], "en": s[2], "has_default": s[3]} for s in SJPH_MANUAL_SECTIONS}
    order = [k for k in (saved.get("order") or []) if k in kset]
    for k in keys:
        if k not in order:
            order.append(k)
    inserts = {}
    for k, v in (saved.get("inserts") or {}).items():
        if k in kset and isinstance(v, dict) and v.get("document_id"):
            item = {"document_id": str(v.get("document_id")), "filename": str(v.get("filename") or "")}
            cap = v.get("caption")   # P1-#6: OCR 자동배치 캡션(선택) — 이미지에 딱 붙는 설명. 스키마 무변경(payload).
            if cap:
                item["caption"] = str(cap)[:2000]
            inserts[k] = item
    # A-1 하이브리드: 조직도 서류를 별도 업로드 안 해도, 폼 담당자 파생 조직도(대표+할랄감독자)로
    # org_chart 섹션 완료 인정. 오디터는 필요시 원본 서류 보완 요청 가능(사전심사 doc-request).
    _org_cache = {}

    def _org_derived_ok():
        if "ok" not in _org_cache:
            c = db.get(models.CaseApplication, case_id)
            org = _build_halal_org(c, db) if c else {"top_mgmt": {}, "penyelia": []}
            _org_cache["ok"] = bool((org.get("top_mgmt") or {}).get("name") and org.get("penyelia"))
        return _org_cache["ok"]
    sections = []
    for k in order:
        img = inserts.get(k)
        complete = bool(meta[k]["has_default"] or img)
        auto = False
        if k == "org_chart" and not complete and _org_derived_ok():
            complete, auto = True, True   # 폼 파생 조직도로 자동 인정
        sections.append({"key": k, "ko": meta[k]["ko"], "en": meta[k]["en"],
                         "has_default": meta[k]["has_default"], "image": img,
                         "complete": complete, "auto": auto})
    done = sum(1 for s in sections if s["complete"])
    signers = _sjph_norm_signers(saved.get("signers"))
    # 서명란 이름은 인니 업체가 읽는다 — 한국어·영어만 주면 인니어 화면에 한글이 남는다.
    _sg_id = _dd_mod.code_labels("SIGNER", "signer", "id")
    signer_meta = [{"key": s[0], "ko": s[1], "en": s[2], "id": _sg_id.get(s[0], s[2]),
                    "default_position": s[3],
                    "value": signers.get(s[0]) or {},
                    "can_edit": bool(role) and role in SJPH_SIGNER_ROLES.get(s[0], set())}
                   for s in SJPH_SIGNERS]
    # 도장은 선택 항목 — ready/done 계산에 넣지 않는다(도장 없어도 생성 가능, 하위호환)
    return {"order": order, "inserts": inserts, "sections": sections,
            "done": done, "total": len(sections), "ready": done == len(sections),
            "signers": signers, "signer_slots": signer_meta}


@app.get("/cases/{case_id}/manual-placement")
def get_manual_placement(case_id: str, lang: str = Query("ko"),
                         user=Depends(auth.get_current_user), db: Session = Depends(get_db)):
    """매뉴얼 도면 배치 내역 — 어느 문서가 어느 자리에 들어가는지, 누가 정했는지.

    자동 추정을 감추지 않는다. 슬롯마다 '사람 지정'인지 'AI 추정'인지와 그 근거를 함께
    돌려주고, 바꿔 넣을 수 있는 후보도 같이 준다. 심사자가 눈으로 확인하고 고치는 것이
    전제다 — 조용히 추측해 넣고 틀려도 모르는 구조가 문제였다."""
    _get_case(db, case_id, user)
    li = {"ko": 0, "en": 1, "id": 2}.get((lang or "ko").lower(), 0)
    docs = db.query(models.DocumentAsset).filter_by(case_id=case_id).all()
    ev = {e.item_key: e.document_id
          for e in db.query(models.SjphEvidence).filter_by(case_id=case_id).all()}
    by_id = {d.document_id: d for d in docs}
    override = _sjph_placement_override(db, case_id)
    # 후보: 이미지이거나 PDF — 도면으로 넣을 수 있는 것만
    cands = [{"document_id": d.document_id, "filename": d.filename, "doc_type": d.doc_type}
             for d in docs
             if (d.content_type or "").startswith("image/")
             or (d.filename or "").lower().endswith(".pdf")]
    slots = []
    for key in _SJPH_DOC_FALLBACK:
        auto, why = _sjph_pick_for_slot(db, case_id, key, docs, ev, by_id, lang)
        manual = key in override
        chosen = by_id.get(override[key]) if (manual and override[key]) else (
            None if manual else auto)
        slots.append({
            "slot": key,
            "label": SJPH_SLOT_LABELS.get(key, (key, key, key))[li],
            "anchor": _SJPH_DOC_FALLBACK[key][0],
            "document_id": chosen.document_id if chosen else None,
            "filename": chosen.filename if chosen else None,
            "decided_by": "manual" if manual else ("auto" if chosen else "none"),
            "auto_suggestion": {"document_id": auto.document_id,
                                "filename": auto.filename} if auto else None,
            "reason": why,
        })
    return {"slots": slots, "candidates": cands}


@app.put("/cases/{case_id}/manual-placement")
def set_manual_placement(case_id: str, body: dict = None,
                         user=Depends(auth.require_roles("consultant", "auditor",
                                                         "operator", "admin")),
                         db: Session = Depends(get_db)):
    """배치 지정 저장 — {slots: {슬롯: 문서id | null}}. null 은 '비움'(자동으로 되돌리지 않음).

    지정하지 않은 슬롯은 자동 추정을 그대로 쓴다. 누가 무엇을 어디에 넣었는지 이력이
    남아야 하므로 워크플로 이벤트와 감사로그 양쪽에 기록한다."""
    c = _get_case(db, case_id, user)
    raw = ((body or {}).get("slots") or {})
    ids = {d.document_id for d in
           db.query(models.DocumentAsset).filter_by(case_id=case_id).all()}
    slots = {}
    for k, v in raw.items():
        if k not in _SJPH_DOC_FALLBACK:
            continue                       # 모르는 슬롯은 조용히 버린다(계약 유지)
        if v in (None, "", False):
            slots[k] = None
        elif str(v) in ids:
            slots[k] = str(v)
        else:
            raise HTTPException(422, {"code": "DOCUMENT_NOT_IN_CASE", "slot": k,
                                      "document_id": str(v)})
    sm.record_event(db, c, c.status, c.status, SJPH_PLACEMENT_ACTION,
                    user["role"], user["uid"], {"slots": slots})
    _audit(db, user, "sjph.placement.set", "case", case_id, case_id=case_id,
           meta={"slots": slots}, commit=False)
    db.commit()
    return {"ok": True, "slots": slots}


@app.get("/cases/{case_id}/sjph-manual/layout")
def get_sjph_manual_layout(case_id: str, user=Depends(auth.get_current_user), db: Session = Depends(get_db)):
    """할랄매뉴얼 빌더 상태(섹션 순서·이미지 삽입) 조회 — 없으면 기본 순서."""
    _get_case(db, case_id, user)
    return _sjph_manual_layout_view(db, case_id, user["role"])


@app.post("/cases/{case_id}/sjph-manual/layout")
def set_sjph_manual_layout(case_id: str, body: dict = None,
                           user=Depends(auth.get_current_user), db: Session = Depends(get_db)):
    """섹션 순서·이미지 삽입 상태 저장(latest-wins). 알려진 키만 허용, 미지의 키는 무시."""
    c = _get_case(db, case_id, user)
    b = body or {}
    keys = [s[0] for s in SJPH_MANUAL_SECTIONS]
    kset = set(keys)
    order = [k for k in (b.get("order") or []) if k in kset]
    for k in keys:
        if k not in order:
            order.append(k)
    inserts = {}
    for k, v in (b.get("inserts") or {}).items():
        if k in kset and isinstance(v, dict) and v.get("document_id"):
            item = {"document_id": str(v.get("document_id")), "filename": str(v.get("filename") or "")}
            cap = v.get("caption")   # P1-#6: OCR 자동배치 캡션(선택) — 이미지에 딱 붙는 설명. 스키마 무변경(payload).
            if cap:
                item["caption"] = str(cap)[:2000]
            inserts[k] = item
    # body에 signers 키가 없으면 기존 저장값 유지 — 부분 저장으로 도장이 날아가지 않게.
    # 편집 권한 없는 슬롯은 요청값을 무시하고 기존값으로 되돌린다(신청사↔GL-HAC 상호 변조 차단).
    prev = _sjph_norm_signers((_sjph_manual_layout_latest(db, case_id) or {}).get("signers"))
    if "signers" in b:
        incoming = _sjph_norm_signers(b.get("signers"))
        signers = {}
        for k in {s[0] for s in SJPH_SIGNERS}:
            allowed = user["role"] in SJPH_SIGNER_ROLES.get(k, set())
            # 요청에 명시된 슬롯만 갱신 — 미포함 슬롯은 기존값 보존(부분 저장 안전)
            val = incoming.get(k) if (allowed and k in incoming) else prev.get(k)
            if not allowed and k in incoming and incoming.get(k) != prev.get(k):
                log.warning("sjph 서명자 슬롯 권한 없음 — slot=%s role=%s case=%s", k, user["role"], case_id)
            if val:
                signers[k] = val
    else:
        signers = prev
    sm.record_event(db, c, c.status, c.status, "sjph_manual.layout", user["role"], user["uid"],
                    {"order": order, "inserts": inserts, "signers": signers})
    db.commit()
    return _sjph_manual_layout_view(db, case_id, user["role"])


# ── L2: 할랄팀 조직도 서류 ↔ 신청서 담당자 교차검증 ────────────────────────
# 설계: docs 상세설계서 GLHAC-TDD-2026-0723-ORG (L2). 스키마 무변경(WorkflowEvent 로깅).
# 방향: 신청서 담당자(SSoT)가 제출된 조직도 이미지(org_chart)에 실제로 나타나는지 OCR로 대조.
_ORG_HONORIFICS = re.compile(
    r"\b(bapak|bpk|ibu|bu|pak|sdr|drs|ir|hj|tuan|nyonya)\b\.?", re.IGNORECASE)


def _org_norm(s):
    """이름/텍스트 정규화 — 소문자·경칭제거·영문/숫자/한글만·공백축약."""
    s = str(s or "").lower()
    s = _ORG_HONORIFICS.sub(" ", s)
    s = re.sub(r"[^0-9a-z가-힣\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _org_name_score(name, text_norm, text_tokens):
    """이름이 OCR 텍스트에 존재하는지 점수(0~1). 정규화 후 부분포함 + 슬라이딩윈도 유사도."""
    import difflib
    n = _org_norm(name)
    if not n:
        return 0.0
    if n in text_norm:
        return 1.0
    ntoks = n.split()
    L = len(ntoks)
    if L == 0 or not text_tokens:
        return 0.0
    best = 0.0
    for i in range(0, max(1, len(text_tokens) - L + 1)):
        window = " ".join(text_tokens[i:i + L])
        r = difflib.SequenceMatcher(None, n, window).ratio()
        if r > best:
            best = r
    return round(best, 3)


def _form_halal_persons(db, c, include_extra=True):
    """신청서 담당자 → 조직도 역할 목록(대표·할랄감독자·PIC·CP·Penyelia). L1 deriveHalalOrg와 동형.
    include_extra=True면 L3 정규화 부서대표(profile_ext.halal_org.members)도 포함(대조 대상)."""
    pe = c.profile_ext or {}
    persons, seen = [], set()

    def add(name, role):
        n = str(name or "").strip()
        if n and (n, role) not in seen:
            seen.add((n, role))
            persons.append({"name": n, "role": role})
    add(c.responsible_person, "top_management")
    add(c.halal_supervisor, "halal_supervisor")
    for p in db.query(models.PenyeliaHalal).filter_by(org_id=c.org_id, status="active"):
        add(p.name, "halal_supervisor")
    add(pe.get("pic_name"), "coordinator")
    add(pe.get("cp_name"), "liaison")
    if include_extra:
        for m in _halal_org_extra_members(c):
            r = m["role"] if m["role"] in ("coordinator", "liaison") else "member"
            add(m["name"], r)
    return persons


# 할랄팀 역할 — 신청기업이 자기 조직도에서 읽는 값이라 인니어가 있어야 한다.
_ORG_ROLE_KO = _dd_mod.code_labels("ORG_ROLE", "org_role", "ko")


def org_role_label(code, lang="ko"):
    return _dd_mod.code_labels("ORG_ROLE", "org_role", lang).get(code, code)

def _reconcile_org(persons, ocr_text):
    """신청서 담당자를 OCR 조직도 텍스트와 대조 → matched / mismatches / summary."""
    tnorm = _org_norm(ocr_text)
    ttok = tnorm.split()
    matched, mism = [], []
    penyelia_found = False
    has_penyelia = any(p["role"] == "halal_supervisor" for p in persons)
    for p in persons:
        sc = _org_name_score(p["name"], tnorm, ttok)
        role_ko = _ORG_ROLE_KO.get(p["role"], p["role"])
        if sc >= 0.90:
            matched.append({"name": p["name"], "role": p["role"], "role_ko": role_ko, "score": sc})
            if p["role"] == "halal_supervisor":
                penyelia_found = True
        elif sc >= 0.70:
            mism.append({"type": "review", "name": p["name"], "role": p["role"], "role_ko": role_ko,
                         "detail": "유사 후보(점수 %.2f) — 사람 확인 필요" % sc, "severity": "info", "score": sc})
        else:
            sev = "high" if p["role"] == "halal_supervisor" else "warn"
            mism.append({"type": "missing_in_doc", "name": p["name"], "role": p["role"], "role_ko": role_ko,
                         "detail": "조직도 서류에서 찾지 못함", "severity": sev, "score": sc})
    if has_penyelia and not penyelia_found:
        mism.append({"type": "penyelia_absent", "name": None, "role": "halal_supervisor",
                     "role_ko": "할랄감독자", "detail": "조직도에 할랄감독자(Penyelia) 미표기 — SJPH 필수",
                     "severity": "high", "score": 0.0})
    summary = {"total": len(persons), "matched": len(matched),
               "high": sum(1 for m in mism if m["severity"] == "high"),
               "warn": sum(1 for m in mism if m["severity"] == "warn"),
               "review": sum(1 for m in mism if m["severity"] == "info")}
    return {"matched": matched, "mismatches": mism, "summary": summary}


def _reconcile_ocr_langs(persons):
    """폼 담당자 이름 스크립트로 OCR 언어 선택 — 한글 있으면 korean, 인니어는 id.
    korean 모델은 인텍이 이미 캐시(라틴도 인식) → 폴백으로 항상 포함해 인니 전용도 안전.
    GLHAC_OCR_LANGS 환경변수로 오버라이드 가능(쉼표구분)."""
    env = os.environ.get("GLHAC_OCR_LANGS")
    if env:
        return [x.strip() for x in env.split(",") if x.strip()]
    langs = []
    if any(re.search(r"[가-힣]", p.get("name", "")) for p in persons):
        langs.append("korean")
    langs.append("id")          # 인니어(라틴) — PaddleOCR 3.x Indonesian 모델
    if "korean" not in langs:   # 캐시된 korean을 폴백으로(다운로드 실패·오프라인 대비)
        langs.append("korean")
    return langs


@app.post("/cases/{case_id}/halal-org/reconcile")
def reconcile_halal_org(case_id: str, body: dict = None,
                        user=Depends(auth.get_current_user), db: Session = Depends(get_db)):
    """조직도 이미지(org_chart 섹션 삽입분 또는 body.document_id)를 OCR → 신청서 담당자와 대조."""
    import base64 as _b64
    import tempfile
    import os as _os
    c = _get_case(db, case_id, user)
    doc_id = (body or {}).get("document_id")
    if not doc_id:
        for s in _sjph_manual_layout_view(db, case_id)["sections"]:
            if s["key"] == "org_chart" and s.get("image"):
                doc_id = s["image"].get("document_id")
                break
    if not doc_id:
        raise HTTPException(400, {"code": "NO_ORG_CHART",
                                  "detail": "조직도 이미지가 없습니다 — 먼저 조직도를 삽입/업로드하세요."})
    d = db.get(models.DocumentAsset, doc_id)
    if not d or not d.content_b64:
        raise HTTPException(404, {"code": "FILE_NOT_AVAILABLE"})
    persons = _form_halal_persons(db, c)
    raw = _b64.b64decode(d.content_b64)
    suffix = _os.path.splitext(d.filename or "")[1] or ".png"
    path, ocr_text, ocr_ok, ocr_langs = None, "", True, []
    try:
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as f:
            f.write(raw)
            path = f.name
        langs = _reconcile_ocr_langs(persons)
        res = ai_local.ocr_text_multi(path, langs) or {}
        if res.get("ok"):
            ocr_text = res.get("text", "")
            ocr_langs = res.get("langs_used", [])
        else:
            ocr_ok = False
    except Exception:  # noqa: BLE001  — OCR 엔진 부재/실패 시 대조는 진행(전원 미검출로 표기)
        ocr_ok = False
    finally:
        if path:
            try:
                _os.unlink(path)
            except Exception:  # noqa: BLE001
                pass
    rec = _reconcile_org(persons, ocr_text)
    # A-2: Penyelia 자격(무슬림·임명장SK·교육증명) 미비 → 높음 플래그(차단 아님, 오디터 판단)
    for p in _build_halal_org(c, db).get("penyelia", []):
        miss = []
        if not p.get("is_muslim"):
            miss.append("무슬림 미확인")
        if not p.get("sk"):
            miss.append("임명장(SK) 미제출")
        if not p.get("training_cert"):
            miss.append("교육증명 미제출")
        if miss:
            rec["mismatches"].append({"type": "penyelia_unqualified", "name": p["name"],
                                      "role": "halal_supervisor", "role_ko": "할랄감독자",
                                      "detail": "자격 미비: " + ", ".join(miss), "severity": "high", "score": 0.0})
    rec["summary"]["high"] = sum(1 for m in rec["mismatches"] if m["severity"] == "high")
    rec["ocr_available"] = ocr_ok
    rec["ocr_langs"] = ocr_langs
    rec["document_id"] = doc_id
    sm.record_event(db, c, c.status, c.status, "org.reconcile", user["role"], user["uid"],
                    {"summary": rec["summary"], "document_id": doc_id})
    db.commit()
    return rec


# ── L3: 정규화 할랄팀 조직 모델(halal_org) ──────────────────────────────────
# 설계: docs 상세설계서 GLHAC-TDD-2026-0723-ORG (L3). DB 마이그레이션 없이 기존 JSON 컬럼
# profile_ext.halal_org 에 정규화 저장(부서대표 members). 대표·할랄감독자·PIC·CP는 기존 필드가
# SSoT로 유지되고, halal_org.members 에는 '추가 부서대표'만 저장 → 이중 진실원천 회피.
_MEMBER_ROLES = {"coordinator", "liaison", "qc", "produksi", "purchasing", "gudang", "rnd", "member"}
_DIVISIONS = {"produksi", "qc", "purchasing", "gudang", "rnd", "umum"}


def _halal_org_extra_members(c):
    """profile_ext.halal_org.members(사용자 추가 부서대표) 정규화 목록."""
    st = (c.profile_ext or {}).get("halal_org") or {}
    out = []
    for m in (st.get("members") or []):
        nm = str((m or {}).get("name") or "").strip()
        if not nm:
            continue
        role = m.get("role") if m.get("role") in _MEMBER_ROLES else "member"
        div = m.get("division") if m.get("division") in _DIVISIONS else ""
        out.append({"name": nm[:80], "title": str(m.get("title") or "").strip()[:80],
                    "division": div, "role": role})
    return out


def _build_halal_org(c, db):
    """정규화 조직 모델 = 기존 필드 파생(top·penyelia·PIC·CP) + 추가 부서대표(halal_org.members)."""
    base = _form_halal_persons(db, c, include_extra=False)
    top = next((p for p in base if p["role"] == "top_management"), None)
    # A-2: Penyelia 자격(무슬림·임명장SK·교육증명) — profile_ext.halal_org.penyelia_quals(이름 키)
    _pq = ((c.profile_ext or {}).get("halal_org") or {}).get("penyelia_quals") or {}
    penyelia = [dict({"name": p["name"], "is_muslim": False, "sk": False, "training_cert": False},
                     **(_pq.get(p["name"]) or {}))
                for p in base if p["role"] == "halal_supervisor"]
    members = [{"name": p["name"], "title": "", "role": p["role"], "division": "", "source": "form"}
               for p in base if p["role"] in ("coordinator", "liaison")]
    members += [{**m, "source": "manual"} for m in _halal_org_extra_members(c)]
    stored = (c.profile_ext or {}).get("halal_org") or {}
    return {"top_mgmt": {"name": top["name"] if top else "", "title": "대표자 · Direktur"},
            "penyelia": penyelia, "members": members, "updated_at": stored.get("updated_at")}


@app.get("/cases/{case_id}/halal-org")
def get_halal_org(case_id: str, user=Depends(auth.get_current_user), db: Session = Depends(get_db)):
    """정규화 할랄팀 조직 모델 조회(파생+저장 병합). 심사·매뉴얼·CAR 공용 소스."""
    c = _get_case(db, case_id, user)
    return _build_halal_org(c, db)


@app.put("/cases/{case_id}/halal-org")
def put_halal_org(case_id: str, body: dict = None,
                  user=Depends(auth.require_roles("applicant", "consultant", "auditor", "admin")),
                  db: Session = Depends(get_db)):
    """부분 업데이트 — members(부서대표) / penyelia_quals(자격) 각각 선택 저장. 기존 필드는 불변."""
    c = _get_case(db, case_id, user)
    b = body or {}
    pe = dict(c.profile_ext or {})
    ho = dict(pe.get("halal_org") or {})
    n_mem = None
    if "members" in b:
        clean = []
        for m in (b.get("members") or [])[:30]:
            nm = str((m or {}).get("name") or "").strip()
            if not nm:
                continue
            role = m.get("role") if m.get("role") in _MEMBER_ROLES else "member"
            div = m.get("division") if m.get("division") in _DIVISIONS else ""
            clean.append({"name": nm[:80], "title": str(m.get("title") or "").strip()[:80],
                          "division": div, "role": role})
        ho["members"] = clean
        n_mem = len(clean)
    if isinstance(b.get("penyelia_quals"), dict):
        ho["penyelia_quals"] = {str(k)[:80]: {"is_muslim": bool((v or {}).get("is_muslim")),
                                              "sk": bool((v or {}).get("sk")),
                                              "training_cert": bool((v or {}).get("training_cert"))}
                                for k, v in b["penyelia_quals"].items() if isinstance(v, dict)}
    ho["updated_at"] = datetime.utcnow().isoformat()
    pe["halal_org"] = ho
    c.profile_ext = pe
    flag_modified(c, "profile_ext")
    sm.record_event(db, c, c.status, c.status, "halal_org.update", user["role"], user["uid"],
                    {"members": n_mem, "quals": len(ho.get("penyelia_quals") or {})})
    db.commit()
    return _build_halal_org(c, db)


def _next_version(db, case_id, doc_type):
    return db.query(models.GeneratedDocument).filter_by(case_id=case_id, doc_type=doc_type).count() + 1


def _save_gendoc(db, c, doc_type, content, user, status="draft"):
    g = models.GeneratedDocument(case_id=c.case_id, org_id=c.org_id, doc_type=doc_type,
                                 version=_next_version(db, c.case_id, doc_type), content=content,
                                 status=status, created_by=user["uid"])
    db.add(g)
    db.flush()
    return g


def _latest_onsite_opinion(db, case_id):
    """현장심사 종합 의견 최신값(WorkflowEvent action=onsite.opinion, latest-wins). 없으면 None."""
    e = (db.query(models.WorkflowEvent)
         .filter(models.WorkflowEvent.case_id == case_id,
                 models.WorkflowEvent.action == "onsite.opinion")
         .order_by(models.WorkflowEvent.created_at.desc(),
                   models.WorkflowEvent.event_id.desc()).first())
    op = (e.payload or {}).get("opinion") if e else None
    return op if (op and str(op).strip()) else None


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
          ("- 미해결 중대 부적합 종결 후 최종 패키지 상정" if major_open else "- 모든 지적 종결 — 파트와 상정 가능")]
    op = _latest_onsite_opinion(db, case_id)
    if op:
        L += ["", "■ 오디터 종합 의견 · Auditor overall opinion", op]
    L += ["", "※ 오디터 검토·승인 필요."]
    content = "\n".join(L)
    g = _save_gendoc(db, c, "audit_report", content, user)
    db.commit()
    return {"report": content, "version": g.version, "gen_doc_id": g.gen_doc_id, "status": g.status}


def _g(d, k):
    """profile_ext 등 opaque JSON 방어적 접근 — 없으면 '-'."""
    return str((d or {}).get(k) or "-")


@app.post("/cases/{case_id}/docs/company-info")
def gen_company_info(case_id: str, user=Depends(auth.get_current_user), db: Session = Depends(get_db)):
    """Company Info (Form.1 기업정보) 생성 — 회원가입 OCR·신청서 자동채움 병합. gen-doc 저장."""
    c = _get_case(db, case_id, user)
    org = db.get(models.Org, c.org_id)
    px = c.profile_ext or {}
    pen = db.query(models.PenyeliaHalal).filter_by(org_id=c.org_id, status="active").first()
    if c.product_ids:
        products = db.query(models.Product).filter(models.Product.product_id.in_(c.product_ids)).all()
    else:
        products = db.query(models.Product).filter_by(org_id=c.org_id).all()
    lines = []
    lines.append("[기업정보 · Company Info (Form.1) — %s]" % (c.company_name or c.case_id[:8]))
    lines.append("회사명: %s" % (c.company_name or "-"))
    lines.append("대표자: %s" % (c.responsible_person or "-"))
    lines.append("사업자등록번호: %s" % (c.nib or "-"))
    lines.append("회사주소: %s" % (c.address or (org.address if org else None) or "-"))
    lines.append("공장주소: %s" % (c.factory_address or "-"))
    lines.append("할랄감독자: %s" % (pen.name if pen else (c.halal_supervisor or "-")))
    lines.append("이메일: %s" % (c.email or "-"))
    lines.append("전화: %s" % (c.phone or "-"))
    lines.append("담당자PIC: %s / %s" % (_g(px, "pic_name"), _g(px, "pic_title")))
    lines.append("업무담당CP: %s / %s" % (_g(px, "cp_name"), _g(px, "cp_title")))
    lines.append("등록유형: %s" % _g(px, "registration_type"))
    lines.append("신청유형: %s" % _g(px, "application_type"))
    lines.append("등록현황: %s" % _g(px, "registration_status"))
    lines.append("경로: %s" % (c.pathway or "-"))
    lines.append("SiHALAL: %s" % (c.sehati_eligible or "-"))
    lines.append("생산능력: %s" % _g(px, "production_capacity"))
    lines.append("제품수: %s" % len(products))
    lines.append("제품 목록:")
    if products:
        for p in products:
            lines.append("- %s (%s)" % (p.name, p.category or "-"))
    else:
        lines.append("- 없음")
    lines.append("※ 회원가입 OCR·신청서 자동채움. 확인 후 저장(HIL).")
    content = "\n".join(lines)
    g = _save_gendoc(db, c, "company_info", content, user)
    db.commit()
    return {"document": content, "version": g.version, "gen_doc_id": g.gen_doc_id}


@app.post("/cases/{case_id}/facilities/{facility_id}/docs/facility-info")
def gen_facility_info(case_id: str, facility_id: str,
                      user=Depends(auth.get_current_user), db: Session = Depends(get_db)):
    """Facility Info (Form.2 시설데이터) 생성 — 업체1:공장N, 공장별 생성. gen-doc 저장."""
    c = _get_case(db, case_id, user)
    f = db.get(models.Facility, facility_id)
    if not f or f.org_id != c.org_id:
        raise HTTPException(404, {"code": "FACILITY_NOT_FOUND"})
    fx = f.profile_ext or {}
    lines = []
    lines.append("[시설정보 · Facility Info (Form.2) — %s]" % (f.name or facility_id[:8]))
    lines.append("제조업체명: %s" % (f.name or "-"))
    lines.append("공장주소: %s" % (f.address or "-"))
    lines.append("도시: %s" % (f.city or "-"))
    lines.append("국가: %s" % (f.country or "-"))
    lines.append("우편번호: %s" % (f.zip or "-"))
    lines.append("공장등록번호: %s" % (f.reg_no or "-"))
    lines.append("시설전화: %s" % _g(fx, "phone"))
    lines.append("시설이메일: %s" % _g(fx, "email"))
    lines.append("담당자PIC: %s / %s" % (_g(fx, "pic_name"), _g(fx, "pic_title")))
    lines.append("업무담당CP: %s / %s" % (_g(fx, "cp_name"), _g(fx, "cp_title")))
    lines.append("※ 업체1:공장N — 공장별 시설정보 각각 생성.")
    content = "\n".join(lines)
    g = _save_gendoc(db, c, "facility_info", content, user)
    db.commit()
    return {"document": content, "version": g.version, "gen_doc_id": g.gen_doc_id}


# ===== 프로필 템플릿 미리보기 (Company Info Form.1 · Factory Audit 기업정보) =====
# 저장된 케이스/시설 데이터를 docx 양식 그대로 채워 ①HTML 오버레이 뷰어 ②PDF 저장.
# 폼을 한 번만 조립(단일 소스) → preview(JSON)와 .pdf 라우트가 동일 rows 사용.
BISMILLAH_AR = "بِسْمِ اللهِ الرَّحْمَنِ الرَّحِيمِ"
BISMILLAH_KO = "가장 자비롭고, 은혜로우신, 하나님의 이름으로"


def _pv(*vals):
    """프로필 값 — 첫 비어있지 않은 값(저장 데이터), 없으면 em-dash."""
    for v in vals:
        if v is not None and str(v).strip():
            return str(v).strip()
    return "—"


def _prow(label_en, label_ko, *vals):
    return {"label_en": label_en, "label_ko": label_ko, "value": _pv(*vals)}


def _company_info_form(db, c):
    """Company Info (Form.1 Client Intake) 폼 조립 — 미리보기·PDF 공용 단일 소스.
    필드 매핑은 gen_company_info와 동일(회원가입 OCR·신청서 저장분 기준)."""
    org = db.get(models.Org, c.org_id)
    px = c.profile_ext or {}
    pen = db.query(models.PenyeliaHalal).filter_by(org_id=c.org_id, status="active").first()
    if c.product_ids:
        products = db.query(models.Product).filter(models.Product.product_id.in_(c.product_ids)).all()
    else:
        products = db.query(models.Product).filter_by(org_id=c.org_id).all()
    g = lambda k: px.get(k)  # noqa: E731
    sections = [
        {"name": "기본 정보 · General", "rows": [
            _prow("Date", "날짜"),
            _prow("Agent/Representative Name", "에이전트/대표자 이름", c.responsible_person),
            _prow("Client Name", "고객 이름", c.responsible_person),
            _prow("Client Organization / Company Name", "고객 기관/회사 이름", c.company_name),
        ]},
        {"name": "고객 정보 · Client Information", "rows": [
            _prow("Office Phone", "사무실 전화번호", g("office_phone")),
            _prow("Cell Phone", "핸드폰 전화번호", c.phone),
            _prow("Email Address", "메일 주소", c.email),
            _prow("Address", "회사 주소", c.address, (org.address if org else None)),
            _prow("City", "도시", g("city")),
            _prow("Country", "국가", g("country")),
            _prow("ZIP Code", "우편번호", g("zip")),
            _prow("Occupation/Business Type", "직종/사업 유형", g("business_type")),
        ]},
        {"name": "담당자 · PIC / CP", "rows": [
            _prow("Person In Charge (PIC) Name", "담당자 이름", g("pic_name")),
            _prow("PIC Title", "담당자 직책", g("pic_title")),
            _prow("PIC MobilePhone", "담당자 핸드폰 전화번호", g("pic_phone")),
            _prow("PIC Email Address", "담당자 메일 주소", g("pic_email")),
            _prow("Contact Person (CP) Name", "업무담당자 이름", g("cp_name")),
            _prow("CP Title", "업무 담당자 직책", g("cp_title")),
            _prow("CP Mobile Phone", "업무 담당자 핸드폰 전화번호", g("cp_phone")),
            _prow("CP Email Address", "업무 담당자 메일 주소", g("cp_email")),
        ]},
        {"name": "등록·제품 · Registration & Product", "rows": [
            _prow("Registration Type", "등록유형", g("registration_type")),
            _prow("Application Type", "신청유형", g("application_type")),
            _prow("Registration Status", "등록현황", g("registration_status")),
            _prow("Product Type", "제품유형", g("product_type")),
            _prow("Does Si HALAL exist", "Si할랄 존재여부", g("si_halal")),
            _prow("Product Marketing Type", "제품 마케팅 유형", g("marketing_type")),
            _prow("Total Employee", "총 직원 수", g("total_employee")),
            _prow("Production Capacity", "생산능력", g("production_capacity")),
            _prow("ID TAX Company (*Only Indonesia)", "세금 ID", g("tax_id")),
            _prow("Halal Supervisor", "할랄 감독자", (pen.name if pen else None), c.halal_supervisor),
        ]},
    ]
    return {
        "title": "Client Intake Form · 고객 접수 양식 (Form.1)",
        "header": {"bismillah": BISMILLAH_AR, "bismillah_ko": BISMILLAH_KO,
                   "form_title": "(Form.1) Client Intake Form 고객 접수 양식"},
        "sections": sections,
        "products": [{"no": i + 1, "name": p.name, "category": p.category or "—"}
                     for i, p in enumerate(products)],
    }


def _factory_profile_form(db, c, f):
    """Factory Audit — Company Information 블록 폼 조립(공장별). 미리보기·PDF 공용 단일 소스.
    필드 매핑은 gen_facility_info와 동일(시설 저장분 + 케이스 기준 할랄감독자)."""
    pen = db.query(models.PenyeliaHalal).filter_by(org_id=c.org_id, status="active").first()
    fx = f.profile_ext or {}
    g = lambda k: fx.get(k)  # noqa: E731
    label = fx.get("label") or f.name or "공장"
    sections = [
        {"name": "기업 정보 · Company Information", "rows": [
            _prow("Date", "날짜"),
            _prow("Representative Name", "대표자 이름", c.responsible_person),
            _prow("Company Name", "회사 이름", f.name, c.company_name),
            _prow("Business Registration Number", "사업자등록번호", f.reg_no, c.nib),
            _prow("Office Phone", "사무실 전화번호", g("phone")),
            _prow("Cell Phone", "핸드폰 전화번호", c.phone),
            _prow("Email Address", "메일 주소", g("email"), c.email),
            _prow("Address", "회사 주소", f.address, c.address),
            _prow("City", "도시", f.city),
            _prow("Country", "국가", f.country),
            _prow("ZIP Code", "우편번호", f.zip),
            _prow("Factory Address", "공장 주소", f.address, c.factory_address),
            _prow("Halal Supervisor Name", "할랄 관리자 이름", (pen.name if pen else None), c.halal_supervisor),
            _prow("Halal Supervisor Mobile Phone", "할랄 관리자 핸드폰 전화번호"),
        ]},
        {"name": "담당자 · PIC", "rows": [
            _prow("PIC Name", "담당자 이름", g("pic_name")),
            _prow("PIC Title", "담당자 직책", g("pic_title")),
        ]},
    ]
    return {
        "title": "Factory Audit · Company Information 기업정보",
        "header": {"bismillah": BISMILLAH_AR, "bismillah_ko": BISMILLAH_KO,
                   "form_title": "Factory Audit Template — Company Information 기업정보 (%s)" % label},
        "sections": sections,
        "facility_label": label,
    }


def _profile_form_to_blocks(form, include_form_title=True):
    """폼 dict → _render_pdf_rich 블록. EN/KO 병기 라벨을 표(table) 블록으로 렌더.
    include_form_title=False: form_title을 PDF 상단 title로 뽑아 쓸 때 중복 방지."""
    blocks = []
    h = form.get("header") or {}
    if h.get("bismillah"):
        blocks.append({"type": "para", "text": h["bismillah"]})
    if h.get("bismillah_ko"):
        blocks.append({"type": "para", "text": h["bismillah_ko"]})
    if include_form_title and h.get("form_title"):
        blocks.append({"type": "heading", "text": h["form_title"], "level": 1})
    for s in form.get("sections") or []:
        blocks.append({"type": "heading", "text": s["name"], "level": 2})
        rows = [["%s · %s" % (r["label_en"], r["label_ko"]) if r.get("label_ko") else r["label_en"],
                 r["value"]] for r in s["rows"]]
        blocks.append({"type": "table", "headers": ["항목 · Field", "값 · Value"],
                       "widths": [0.5, 0.5], "rows": rows})
    products = form.get("products")
    if products:
        blocks.append({"type": "heading", "text": "제품 · Products", "level": 2})
        prows = [[str(p["no"]), p["name"], p.get("category") or "—"] for p in products]
        blocks.append({"type": "table", "headers": ["No", "Product 제품", "Category 분류"],
                       "widths": [0.12, 0.55, 0.33], "rows": prows})
    return blocks


@app.get("/cases/{case_id}/docs/company-info/preview")
def preview_company_info(case_id: str, user=Depends(auth.get_current_user), db: Session = Depends(get_db)):
    """업체 프로필(Form.1) 미리보기 폼(JSON) — 저장 데이터 기준, read-only·org격리."""
    c = _get_case(db, case_id, user)
    return _company_info_form(db, c)


@app.get("/cases/{case_id}/docs/company-info.pdf")
def company_info_pdf(case_id: str, user=Depends(auth.get_current_user), db: Session = Depends(get_db)):
    """업체 프로필(Form.1) PDF — preview와 동일 폼 조립을 리치PDF로 렌더(저장 없음)."""
    from fastapi.responses import Response
    c = _get_case(db, case_id, user)
    form = _company_info_form(db, c)
    _ttl = (form.get("header") or {}).get("form_title") or form.get("title", "")
    pdf = _render_pdf_rich(_ttl, _profile_form_to_blocks(form, include_form_title=False),
                           subtitle=(c.company_name or ""),
                           footer="GL-HAC AI · Company Info " + case_id[:8])
    return Response(content=pdf, media_type="application/pdf",
                    headers={"Content-Disposition": "attachment; filename=company_info_%s.pdf" % case_id[:8]})


@app.get("/cases/{case_id}/facilities/{facility_id}/docs/factory-profile/preview")
def preview_factory_profile(case_id: str, facility_id: str,
                            user=Depends(auth.get_current_user), db: Session = Depends(get_db)):
    """공장 프로필(Factory Audit 기업정보) 미리보기 폼(JSON) — 공장별, read-only·org격리."""
    c = _get_case(db, case_id, user)
    f = db.get(models.Facility, facility_id)
    if not f or f.org_id != c.org_id:
        raise HTTPException(404, {"code": "FACILITY_NOT_FOUND"})
    return _factory_profile_form(db, c, f)


@app.get("/cases/{case_id}/facilities/{facility_id}/docs/factory-profile.pdf")
def factory_profile_pdf(case_id: str, facility_id: str,
                        user=Depends(auth.get_current_user), db: Session = Depends(get_db)):
    """공장 프로필(Factory Audit 기업정보) PDF — preview와 동일 폼 렌더(저장 없음)."""
    from fastapi.responses import Response
    c = _get_case(db, case_id, user)
    f = db.get(models.Facility, facility_id)
    if not f or f.org_id != c.org_id:
        raise HTTPException(404, {"code": "FACILITY_NOT_FOUND"})
    form = _factory_profile_form(db, c, f)
    _ttl = (form.get("header") or {}).get("form_title") or form.get("title", "")
    pdf = _render_pdf_rich(_ttl, _profile_form_to_blocks(form, include_form_title=False),
                           subtitle=(f.name or c.company_name or ""),
                           footer="GL-HAC AI · Factory Profile " + facility_id[:8])
    return Response(content=pdf, media_type="application/pdf",
                    headers={"Content-Disposition": "attachment; filename=factory_profile_%s.pdf" % facility_id[:8]})


CONTRACT_STATIC_SECTIONS = [
    ("SECTION 3 · GL HAC Commitment", "GL HAC는 인도네시아 할랄표준(SJPH)에 따라 전문적으로 심사를 수행하고, 심사보고서를 BPJPH에 제출하며, 고객 정보를 법령·BPJPH 요구 외에는 기밀로 유지하고, 인도네시아 할랄표준의 중요한 변경을 고객에게 통지한다."),
    ("SECTION 4 · Client Commitment", "고객은 (a) 인도네시아 할랄제품보증시스템(SJPH)의 모든 요건 준수, (b) 심사에 진실하고 완전한 정보 제공, (c) 심사팀·BPJPH 대표의 현장·문서·인원 접근 보장, (d) 사내 HPAS/SJPH 구축·유지, (e) 수수료 일정 납부, (f) 제품·원재료·공정·소유권의 중대한 변경 즉시 통지, (g) 인증서 발급 시 규정에 따른 할랄 라벨 사용, (h) 인증 종료·정지·철회 시 할랄 라벨 사용 중단을 약정한다."),
    ("SECTION 7 · Confidentiality", "양 당사자는 법령(한국·인도네시아) 또는 BPJPH 요구가 없는 한 상대의 영업정보를 기밀로 유지한다."),
    ("SECTION 8 · Governing Law & Dispute", "본 계약은 대한민국 법률에 따르며, 분쟁은 우선 협의로, 미해결 시 KCAB 중재(서울) 또는 대한민국 법원으로 해결한다."),
    ("SECTION 9 · Duration & Termination", "본 계약은 서명일부터 발효되어 고객의 할랄 인증서가 만료·철회되거나 계약 조건에 따라 종료될 때까지 유효하며, 일방의 중대한 위반 시 종료될 수 있다."),
]


def _gen_contract_core(db, c, b, user, status="issued"):
    """Contract(FORM 4.1) 레코드 생성/갱신 + gen-doc 저장. status는 호출자가 지정(queue: sent 등).
    커밋은 호출자 책임. (contract, gendoc) 반환."""
    case_id = c.case_id
    if c.product_ids:
        products = db.query(models.Product).filter(models.Product.product_id.in_(c.product_ids)).all()
    else:
        products = db.query(models.Product).filter_by(case_id=case_id).all()
    invoice = db.query(models.Invoice).filter_by(case_id=case_id).order_by(models.Invoice.created_at.desc()).first()
    _bfee = b.get("fee")  # M4: 특수조항 수동 금액
    fee = _bfee if _bfee not in (None, "") else (invoice.total if invoice else None)
    categories = list(dict.fromkeys(_guess_scope_category(p.name, p.category) for p in products))
    contract = db.query(models.Contract).filter_by(case_id=case_id).first()
    if not contract:
        contract = models.Contract(case_id=case_id, org_id=c.org_id, party_a=c.company_name,
                                   effective_date=str(datetime.utcnow().date()), scope=categories,
                                   product_ids=[p.product_id for p in products], fee=fee, status=status)
        db.add(contract)
        db.flush()
        contract.contract_no = "HAC-" + contract.contract_id[:8].upper()
    else:
        contract.party_a = c.company_name
        contract.effective_date = str(datetime.utcnow().date())
        contract.scope = categories
        contract.product_ids = [p.product_id for p in products]
        contract.fee = fee
        contract.status = status
        if not contract.contract_no:
            contract.contract_no = "HAC-" + contract.contract_id[:8].upper()
    products_str = ", ".join(p.name for p in products) or "-"
    content = "\n".join([
        "[계약서 · Contract (FORM 4.1) — %s]" % (c.company_name or c.case_id[:8]),
        "계약번호: %s" % contract.contract_no,
        "발효일: %s" % contract.effective_date,
        "Party A (고객사): %s" % (contract.party_a or "-"),
        "Party B: Halal Certification Body (HCB) GL HAC Korea",
        "인증범위: %s" % (", ".join(categories) if categories else "-"),
        "대상제품: %s" % products_str,
        "수수료: %s" % (("%s %s" % (fee, contract.currency)) if fee else "별도 청구서"),
        "상태: %s" % contract.status])
    gendoc = _save_gendoc(db, c, "contract", content, user)
    return contract, gendoc


@app.post("/cases/{case_id}/contract/generate")
def gen_contract(case_id: str, body: dict = None, user=Depends(auth.get_current_user), db: Session = Depends(get_db)):
    """Contract (FORM 4.1) 생성 — 정적 법률조항 + 동적(회사·제품·수수료) 병합. Contract 레코드 + gen-doc 저장.
    M4: body.fee(특수조항 계약금액 수동 override)·body.currency 지원."""
    c = _get_case(db, case_id, user)
    contract, gendoc = _gen_contract_core(db, c, body or {}, user, status="issued")
    db.commit()
    return {"contract_id": contract.contract_id, "contract_no": contract.contract_no,
            "gen_doc_id": gendoc.gen_doc_id, "status": contract.status}


# ── 계약 큐(승인 이관) 워크플로 — 수정요청 001 P2. 역할별 상태 전이. ──
_CONTRACT_QUEUE_PREV = {"requested": "none", "sent": "requested", "received": "sent", "signing": "received"}


@app.post("/cases/{case_id}/contract/request")
def contract_request(case_id: str, user=Depends(auth.require_roles("applicant", "client", "consultant")), db: Session = Depends(get_db)):
    """① 계약 신청(클라이언트) — Contract(status=requested) 생성. 관리자 승인 큐로 이관."""
    c = _get_case(db, case_id, user)
    ct = db.query(models.Contract).filter_by(case_id=case_id).first()
    if not ct:
        ct = models.Contract(case_id=case_id, org_id=c.org_id, party_a=c.company_name,
                             effective_date=str(datetime.utcnow().date()), status="requested")
        db.add(ct)
    elif ct.status in ("draft", "none", None):
        ct.status = "requested"
    _audit(db, user, "contract.request", "contract", ct.contract_id, case_id, {}, commit=False)
    sm.record_event(db, c, c.status, c.status, "contract.request", user["role"], user["uid"], {})
    _notify(db, c, "contract.request", role="operator",
            msg=("contract.request", {"company": c.company_name or case_id[:8]}))
    db.commit()
    return {"status": ct.status}


@app.post("/cases/{case_id}/contract/approve")
def contract_approve(case_id: str, body: dict = None, user=Depends(auth.require_roles("operator")), db: Session = Depends(get_db)):
    """② 승인·계약서 발송(관리자) — 계약서 생성 + status=sent. 클라이언트 접수 큐로 이관."""
    c = _get_case(db, case_id, user)
    contract, gendoc = _gen_contract_core(db, c, body or {}, user, status="sent")
    _audit(db, user, "contract.approve", "contract", contract.contract_id, case_id, {}, commit=False)
    sm.record_event(db, c, c.status, c.status, "contract.approve", user["role"], user["uid"],
                    {"contract_no": contract.contract_no})
    _notify(db, c, "contract.approve", role="client",
            msg=("contract.approve", {"contract_no": contract.contract_no or ""}))
    db.commit()
    return {"contract_id": contract.contract_id, "contract_no": contract.contract_no, "status": contract.status}


@app.post("/cases/{case_id}/contract/receive")
def contract_receive(case_id: str, user=Depends(auth.require_roles("applicant", "client", "consultant")), db: Session = Depends(get_db)):
    """③ 계약서 접수·확인(클라이언트) — sent→received. 오디터 서명요청 큐로 이관."""
    c = _get_case(db, case_id, user)
    ct = db.query(models.Contract).filter_by(case_id=case_id).first()
    if not ct or ct.status not in ("sent", "issued"):
        raise HTTPException(409, {"code": "CONTRACT_NOT_SENT", "detail": "관리자 승인·발송 후 접수할 수 있습니다."})
    ct.status = "received"
    _audit(db, user, "contract.receive", "contract", ct.contract_id, case_id, {}, commit=False)
    sm.record_event(db, c, c.status, c.status, "contract.receive", user["role"], user["uid"], {})
    _notify(db, c, "contract.receive", role="auditor",
            msg=("contract.receive", {"company": c.company_name or ""}))
    db.commit()
    return {"status": ct.status}


@app.post("/cases/{case_id}/contract/request-signature")
def contract_request_signature(case_id: str, user=Depends(auth.require_roles("auditor")), db: Session = Depends(get_db)):
    """④ 서명 요청(오디터) — received→signing. 이후 양자(A 고객/B GLHAC) 전자서명 진행."""
    c = _get_case(db, case_id, user)
    ct = db.query(models.Contract).filter_by(case_id=case_id).first()
    if not ct or ct.status not in ("received",):
        raise HTTPException(409, {"code": "CONTRACT_NOT_RECEIVED", "detail": "클라이언트 접수·확인 후 서명 요청이 가능합니다."})
    ct.status = "signing"
    _audit(db, user, "contract.request_signature", "contract", ct.contract_id, case_id, {}, commit=False)
    sm.record_event(db, c, c.status, c.status, "contract.request_signature", user["role"], user["uid"], {})
    _notify(db, c, "contract.sign_request", role="client",
            msg=("contract.sign_request", {"company": c.company_name or ""}))
    db.commit()
    return {"status": ct.status}


# 계약 큐 반려(되돌리기) — 수정요청 001 P4. 현재 단계를 이전 단계로 되돌리고 사유 기록.
_CONTRACT_RETURN_PREV = {"requested": "draft", "sent": "requested", "received": "sent",
                         "signing": "received", "signed": "signing", "confirmed": "signed"}
_CONTRACT_QUEUE_OWNER = {"requested": "operator", "draft": "client", "sent": "client", "issued": "client",
                         "received": "auditor", "signing": "client", "signed": "operator"}


@app.post("/cases/{case_id}/contract/return")
def contract_return(case_id: str, body: dict = None,
                    user=Depends(auth.require_roles("applicant", "client", "consultant", "auditor", "operator")),
                    db: Session = Depends(get_db)):
    """계약 큐 반려 — 현재 단계를 이전 단계로 되돌림 + 사유 기록. 이전 담당 역할에 알림."""
    c = _get_case(db, case_id, user)
    ct = db.query(models.Contract).filter_by(case_id=case_id).first()
    if not ct:
        raise HTTPException(404, {"code": "CONTRACT_NOT_FOUND"})
    prev = _CONTRACT_RETURN_PREV.get(ct.status)
    if not prev:
        raise HTTPException(409, {"code": "CANNOT_RETURN", "detail": "현재 단계에서는 반려할 수 없습니다."})
    reason = ((body or {}).get("reason") or "").strip()
    frm = ct.status
    ct.status = prev
    _audit(db, user, "contract.return", "contract", ct.contract_id, case_id,
           {"from": frm, "to": prev, "reason": reason}, commit=False)
    sm.record_event(db, c, c.status, c.status, "contract.return", user["role"], user["uid"],
                    {"from": frm, "to": prev, "reason": reason})
    # 반려 사유는 사람이 쓴 글이라 번역하지 않는다 — 제목만 카탈로그로 간다.
    _notify(db, c, "contract.return",
            body=("반려 사유: " + reason) if reason else "계약이 이전 단계로 반려되었습니다.",
            role=_CONTRACT_QUEUE_OWNER.get(prev, "operator"),
            msg=("contract.return", {"company": c.company_name or ""}))
    db.commit()
    return {"status": ct.status, "from": frm, "reason": reason}


@app.get("/cases/{case_id}/contract/pdf")
def get_contract_pdf(case_id: str, user=Depends(auth.get_current_user), db: Session = Depends(get_db)):
    """Contract PDF — 기준 양식(FORM 4.1-HCB-GL HAC) 원본에 실데이터 오버레이(SJPH 매뉴얼과 동일 원칙).
    원본 PDF가 없는 환경에서만 기존 리치 렌더러로 폴백."""
    from fastapi.responses import Response
    c = _get_case(db, case_id, user)
    ct = db.query(models.Contract).filter_by(case_id=case_id).first()
    if not ct:
        raise HTTPException(404, {"code": "CONTRACT_NOT_FOUND"})
    products = (db.query(models.Product).filter(models.Product.product_id.in_(ct.product_ids)).all()
                if ct.product_ids else db.query(models.Product).filter_by(case_id=case_id).all())
    try:
        pdf = _contract_overlay_pdf(db, c, ct, products)
    except Exception as e:  # noqa: BLE001
        log.warning("contract 양식 오버레이 실패, 리치 렌더러 폴백: %s", e)
        pdf = _contract_rich_pdf(db, c, ct, products)
    return Response(content=pdf, media_type="application/pdf",
                    headers={"Content-Disposition": "attachment; filename=contract_%s.pdf" % case_id[:8]})


def _guess_scope_category(name, category):
    """계약서 범위 판정용 카테고리 정규화 — category가 있으면 그대로, 없으면 제품명 키워드로 추정."""
    c = str(category or "").lower()
    if any(k in c for k in ("food", "식품", "snack", "스낵")): return "food"
    if any(k in c for k in ("beverage", "음료", "drink")): return "beverage"
    if any(k in c for k in ("drug", "pharma", "의약", "약")): return "drug"
    if any(k in c for k in ("cosmet", "화장")): return "cosmetic"
    if any(k in c for k in ("good", "생활", "소비재")): return "goods"
    n = str(name or "").lower()
    if any(k in n for k in ("음료", "beverage", "drink", "주스", "juice", "차 ", " tea", "워터", "water")): return "beverage"
    if any(k in n for k in ("샴푸", "로션", "크림", "화장", "cosmet", "soap", "비누", "lotion", "cream", "shampoo")): return "cosmetic"
    if any(k in n for k in ("의약", "정제", "캡슐", "tablet", "capsule", "pharma")): return "drug"
    if any(k in n for k in ("스낵", "snack", "과자", "keripik", "칩", "chip", "젤리", "gummy", "구미", "라면", "noodle", "빵", "면", "food", "식품", "소스", "sauce")): return "food"
    return "food"   # 제조 식품 기본값(가장 흔함)


CONTRACT_TEMPLATE_PDF = os.path.join(os.path.dirname(__file__), "assets", "GLHAC_Contract_Form_4.1.pdf")
CONTRACT_SCOPE_LABELS = [("Foods", "food"), ("Beverages", "beverage"),
                         ("Drugs/Pharmaceuticals", "drug"), ("Cosmetics", "cosmetic"),
                         ("Use Goods", "goods")]


def _contract_overlay_pdf(db, c, ct, products):
    """기준 양식 원본 PDF의 빈칸 위치에 실데이터를 그려 넣는다(좌표는 FORM 4.1 실측)."""
    import fitz
    from datetime import date as _date
    doc = fitz.open(CONTRACT_TEMPLATE_PDF)
    # 표지(1p) 상단에 bismillah 오버레이 — 원본 양식엔 아랍어가 없어 추가(insert_htmlbox shaping+RTL)
    try:
        _css = ("@font-face{font-family:ar;src:url('%s')} "
                "*{font-family:ar;font-size:15px;direction:rtl;text-align:center;color:#0a3d20}"
                % _ARABIC_FONT)
        doc[0].insert_htmlbox(fitz.Rect(106, 16, 506, 44), BISMILLAH_AR, css=_css)
    except Exception:  # noqa: BLE001
        pass
    FS, COL = 9, (0, 0, 0.55)   # 채워넣는 값은 파란색으로 구분
    ed = str(ct.effective_date or "")[:10]
    y, m, dd = (ed.split("-") + ["", "", ""])[:3] if "-" in ed else ("", "", "")

    def put(page, x, y0, text, size=FS):
        if text:
            page.insert_text((x, y0), str(text), fontname="helv", fontsize=size, color=COL)

    # ── page 2: NO / 날짜 / Party A / 표준·범위 체크 / 제품 리스트 (좌표=FORM 4.1 실측, baseline 보정) ──
    p2 = doc[1]
    put(p2, 234, 172, ct.contract_no or "")                             # NO 밑줄 위
    put(p2, 124, 215, y); put(p2, 220, 215, m); put(p2, 260, 215, dd)   # 년/월/일 밑줄 위(x116-158/212-249/252-289, baseline 218)
    put(p2, 135, 254, ct.party_a or c.company_name or "")               # Party A 밑줄
    # SECTION 1 표준 체크(SJPH) — 'Indonesia...(SJPH)' 행 좌측 칸(x≈115, y≈372)
    std = (ct.standard or "SJPH")
    if "SJPH" in std.upper() or "SISTEM" in std.upper():
        p2.insert_text((113, 373), "X", fontname="helv", fontsize=11, color=COL)
    # SECTION 2 범위 체크 — Foods/Beverages/Drugs/Cosmetics/Use Goods 행 좌측 칸(x≈115)
    scope = set(str(x).lower() for x in (ct.scope or []))
    scope |= {_guess_scope_category(p.name, p.category) for p in products}
    rowY = {"food": 468, "beverage": 490, "drug": 510, "cosmetic": 530, "goods": 551}
    for lbl, key in CONTRACT_SCOPE_LABELS:
        if any(key in s or lbl.lower().split("/")[0] in s for s in scope):
            p2.insert_text((113, rowY[key]), "X", fontname="helv", fontsize=11, color=COL)
    # SECTION 2 #3 제품 리스트 a~e (밑줄 위, y 596..655)
    for i, prod in enumerate(products[:5]):
        put(p2, 150, 596 + int(round(i * 14.7)), prod.name or "")

    # ── page 3: 제품 f~h(6~8번째, y 147..176) + 공장 주소 ──
    p3 = doc[2]
    for i, prod in enumerate(products[5:8]):
        put(p3, 150, 147 + int(round(i * 14.7)), prod.name or "")
    facs = _case_facilities(db, c)
    for i, f in enumerate(facs[:2]):
        addr = " ".join(x for x in [getattr(f, "name", None), f.address, f.city, f.country] if x)
        put(p3, 150, 219 + int(round(i * 14.5)), addr)

    # ── page 5: 서명 블록(GL HAC 측 · Party B 대표) ──
    p5 = doc[4]
    sigs = ct.signatures or []
    sb = next((s for s in sigs if s.get("party") == "B"), None)
    sa = next((s for s in sigs if s.get("party") == "A"), None)
    if sb:
        put(p5, 290, 305, sb.get("name") or "")           # GL HAC printed name
        if sb.get("signed_at"):
            put(p5, 290, 341, str(sb["signed_at"])[:10])
    put(p5, 250, 379, ct.party_a or c.company_name or "")  # CLIENT company name in "on behalf of"
    if sa:
        put(p5, 290, 417, sa.get("name") or "")

    # ── page 8: Annex 2 요금표 — Client Company Name([Full Legal Name...] 자리)만 채움.
    #    요금표 본문은 원본에 상세 인쇄되어 있으므로 덮지 않는다. 계약 총액은 통화 라벨 옆에 병기.
    p8 = doc[7]
    # 원본의 placeholder [Full Legal Name...]를 흰 사각형으로 가리고 실명 기입
    p8.draw_rect(fitz.Rect(246, 144, 555, 162), color=None, fill=(1, 1, 1))
    put(p8, 248, 156, ct.party_a or c.company_name or "")
    cur = ct.currency or "KRW"
    p8.draw_rect(fitz.Rect(318, 181, 560, 200), color=None, fill=(1, 1, 1))   # [KRW / USD] (Select one...) 전체 가림
    put(p8, 322, 194, cur + (" (계약총액 %s)" % BILLING_FMT(ct.fee) if ct.fee else ""))

    out = doc.tobytes()
    doc.close()
    return out


def BILLING_FMT(v):
    try:
        return "{:,.0f}".format(float(v))
    except (TypeError, ValueError):
        return str(v)


def _contract_rich_pdf(db, c, ct, products):
    """폴백 — 원본 양식 PDF 부재 시 코드 렌더. bytes 반환."""
    blocks = []
    _logo = os.path.join(os.path.dirname(__file__), "assets", "glhac_logo.png")
    if os.path.exists(_logo):
        try:
            with open(_logo, "rb") as _lf:
                blocks.append({"type": "image", "data": _lf.read(), "width": 150})
        except Exception:  # noqa: BLE001
            pass
    blocks += [
        {"type": "heading", "text": "HALAL CERTIFICATION AGREEMENT · FORM 4.1-HCB-GL HAC", "level": 1},
        {"type": "kv", "label": "NO", "value": ct.contract_no or ""},
        {"type": "kv", "label": "Effective Date", "value": ct.effective_date or ""},
        {"type": "kv", "label": "Party A", "value": ct.party_a or ""},
        {"type": "kv", "label": "Party B", "value": "Halal Certification Body (HCB) GL HAC Korea"},
        {"type": "heading", "text": "SECTION 1 · Halal Standard", "level": 2},
        {"type": "para", "text": "Party A는 다음 할랄인증 기준을 준수한다: " + (ct.standard or "SJPH")},
        {"type": "heading", "text": "SECTION 2 · Scope of Certification", "level": 2},
        {"type": "para", "text": "인증 범위: " + (", ".join(ct.scope or []) or "-")},
        {"type": "table", "headers": ["No", "Product", "Category"],
         "rows": [[i + 1, p.name, p.category or "-"] for i, p in enumerate(products)], "widths": [0.12, 0.55, 0.33]},
    ]
    for heading, body in CONTRACT_STATIC_SECTIONS:
        blocks.append({"type": "heading", "text": heading, "level": 2})
        blocks.append({"type": "para", "text": body})
    blocks.append({"type": "heading", "text": "SECTION 6 · Service Fee", "level": 2})
    blocks.append({"type": "kv", "label": "Fee", "value": (("%s %s" % (ct.fee, ct.currency)) if ct.fee else "별도 청구서")})
    blocks.append({"type": "spacer", "h": 10})
    sigs = ct.signatures or []
    slots = []
    for role, party in [("For GL HAC", "B"), ("For CLIENT (" + (ct.party_a or "") + ")", "A")]:
        match = next((s for s in sigs if s.get("party") == party), None)
        slots.append({"role": role, "name": (match.get("name", "") if match else ""),
                      "signed": bool(match and match.get("signed_at"))})
    blocks.append({"type": "signature", "slots": slots})
    return _render_pdf_rich("Halal Certification Agreement", blocks, subtitle=ct.party_a,
                            footer="GL-HAC AI · Contract " + (ct.contract_no or ""))


@app.get("/cases/{case_id}/contract")
def get_contract(case_id: str, user=Depends(auth.get_current_user), db: Session = Depends(get_db)):
    """계약 진행 상태 조회 — 계약 단계 화면(진행 표시)용. 계약+서명 상태."""
    _get_case(db, case_id, user)
    ct = db.query(models.Contract).filter_by(case_id=case_id).first()
    if not ct:
        return {"exists": False}
    sigs = ct.signatures or []
    return {"exists": True, "contract_id": ct.contract_id, "contract_no": ct.contract_no,
            "status": ct.status, "fee": ct.fee, "currency": ct.currency,
            "effective_date": ct.effective_date,
            "signed_a": any(s.get("party") == "A" for s in sigs),
            "signed_b": any(s.get("party") == "B" for s in sigs),
            "signatures": sigs}


@app.post("/contracts/{contract_id}/sign")
def sign_contract(contract_id: str, party: str = "A", name: str = "",
                  user=Depends(auth.get_current_user), db: Session = Depends(get_db)):
    """계약 서명 기록 — party A(고객)/B(GL HAC). 양측 서명 완료 시 status=signed."""
    ct = db.get(models.Contract, contract_id)
    if not ct:
        raise HTTPException(404, {"code": "CONTRACT_NOT_FOUND"})
    sigs = list(ct.signatures or [])
    sigs.append({"party": party, "name": name, "title": "", "signed_at": str(datetime.utcnow())})
    ct.signatures = sigs
    if any(s.get("party") == "A" for s in sigs) and any(s.get("party") == "B" for s in sigs):
        ct.status = "signed"
        _sc = db.query(models.CaseApplication).filter_by(case_id=ct.case_id).first()
        if _sc:
            _notify(db, _sc, "contract.signed", role="operator",
                    msg=("contract.signed", {"contract_no": ct.contract_no or ""}))
    # 법적효력 Phase 1 — PSrE 공인 전자서명 훅(미설정 시 no-op·기존 서명/응답 완전 불변). 내부 서명은 위에서 병행.
    if _psre_config()["configured"]:
        import types as _t, hashlib as _h, json as _j
        _psre_certify(db, _t.SimpleNamespace(case_id=ct.case_id, status=ct.status),
                      "contract", name or party, contract_id,
                      _h.sha256(_j.dumps(sigs, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest())
    db.commit()
    return {"contract_id": contract_id, "status": ct.status, "signatures": ct.signatures}


@app.post("/cases/{case_id}/contract/confirm")
def confirm_contract(case_id: str, user=Depends(auth.require_roles("operator")), db: Session = Depends(get_db)):
    """최종 계약서 확인(관리자) — 양자 서명(signed) 완료 후 status=confirmed 전이.
    이후에만 청구서(Invoice) 생성이 허용된다(수정요청 001 · 계약 큐 P1 데이터 게이트)."""
    c = _get_case(db, case_id, user)
    ct = db.query(models.Contract).filter_by(case_id=case_id).first()
    if not ct:
        raise HTTPException(404, {"code": "CONTRACT_NOT_FOUND"})
    sigs = ct.signatures or []
    if not (any(s.get("party") == "A" for s in sigs) and any(s.get("party") == "B" for s in sigs)):
        raise HTTPException(409, {"code": "CONTRACT_NOT_SIGNED",
                                  "detail": "양자 서명 완료 후 최종 확인할 수 있습니다."})
    ct.status = "confirmed"
    _audit(db, user, "contract.confirm", "contract", ct.contract_id, case_id, {}, commit=False)
    sm.record_event(db, c, c.status, c.status, "contract.confirm", user["role"], user["uid"],
                    {"contract_id": ct.contract_id})
    _notify(db, c, "contract.confirm", role="client",
            msg=("contract.confirm", {"contract_no": ct.contract_no or ""}))
    db.commit()
    return {"contract_id": ct.contract_id, "status": ct.status}


def _fatwa_quorum_ok(db, case_id):
    """파트와 정족수 검증 — fatwa.sign 이벤트(member별 latest-wins) 조회.
    위원장(chairman) 서명 존재 AND 고유 서명자 총 ≥ 2 여부 반환.
    반환: (ok: bool, missing: list[str], ctx: dict). missing 코드: NO_CHAIRMAN / NEED_2_MEMBERS."""
    events = (db.query(models.WorkflowEvent)
              .filter(models.WorkflowEvent.case_id == case_id,
                      models.WorkflowEvent.action == "fatwa.sign")
              .order_by(models.WorkflowEvent.created_at.asc()).all())
    signers = {}   # member key -> signed(bool), latest-wins per member
    for e in events:
        p = e.payload or {}
        mk = p.get("member")
        if not mk:
            continue
        img = p.get("image")
        signers[mk] = isinstance(img, str) and img.startswith("data:image/")
    valid = [mk for mk, ok in signers.items() if ok]
    has_chairman = any("chairman" in str(mk).lower() for mk in valid)
    missing = []
    if not has_chairman:
        missing.append("NO_CHAIRMAN")
    if len(valid) < 2:
        missing.append("NEED_2_MEMBERS")
    ok = not missing
    return ok, missing, {"chairman_signed": has_chairman, "signer_count": len(valid), "signers": valid}


@app.get("/cases/{case_id}/committee/status")
def committee_status(case_id: str, user=Depends(auth.get_current_user), db: Session = Depends(get_db)):
    """KFPH(Komite Fatwa Produk Halal) 자기선언 판정 상태 — committee.approve/reject 최신 결정 조회(SEHATI).
    ⚠ self-declare(UMK) 전용 트랙 — 정규 MUI 파트와(fatwa_review)와 별개(PP 42/2024).
    A5c: KFPH 심의 근거자료(동반자 검증 요약·자기선언서 존재)를 함께 반환(스키마 무변경 조인)."""
    c = _get_case(db, case_id, user)
    ev = (db.query(models.WorkflowEvent)
          .filter(models.WorkflowEvent.case_id == case_id,
                  models.WorkflowEvent.action.in_(("committee.approve", "committee.reject")))
          .order_by(models.WorkflowEvent.created_at.desc()).first())
    decision = (ev.action.split(".")[1] if ev else None)
    pa = ((db.query(models.PendampingAssignment).filter_by(case_id=case_id)
           .order_by(models.PendampingAssignment.assignment_id.desc()).first())
          if c.pathway == "self_declare" else None)
    sd_doc = (db.query(models.GeneratedDocument)
              .filter_by(case_id=case_id, doc_type="self_declaration").first())
    return {"case_id": case_id, "status": c.status, "pathway": c.pathway,
            "fatwa_status": c.fatwa_status,
            "in_committee": c.status == "committee_verification",
            "decision": decision,
            "approved": decision == "approve",
            "decided_by": (ev.actor_id if ev else None),
            "reason": ((ev.payload or {}).get("reason") if ev else None),
            "decided_at": (ev.created_at.isoformat() if ev and ev.created_at else None),
            "self_declaration_ready": bool(sd_doc) or c.pathway == "self_declare",
            "pendamping": ({"name": _pendamping_name(db, c),
                            "decision": pa.decision, "note": pa.note,
                            "verified_at": pa.verified_at.isoformat() if pa.verified_at else None}
                           if pa else None)}


@app.post("/cases/{case_id}/committee/decide")
def committee_decide(case_id: str, body: schemas.CommitteeDecisionReq,
                     user=Depends(auth.require_roles("fatwa_liaison", "operator")),
                     db: Session = Depends(get_db)):
    """KFPH(Komite Fatwa Produk Halal) 자기선언 할랄 판정 — 파트와연락(fatwa_liaison)이 승인/반려 + 근거.
    ⚠ self-declare(UMK) 전용 — 정규 MUI 파트와와 별개(PP 42/2024).
    committee_verification 자동승인 대체: 승인 시 fatwa_status=approved(발급 가능), 반려 시 차단."""
    c = _get_case(db, case_id, user)
    if c.status != "committee_verification":
        raise HTTPException(409, {"code": "NOT_IN_COMMITTEE_VERIFICATION", "status": c.status})
    if c.pathway != "self_declare":
        raise HTTPException(409, {"code": "NOT_SELF_DECLARE", "pathway": c.pathway})
    reason = (body.reason or "").strip()
    if body.decision not in ("approve", "reject"):
        raise HTTPException(422, {"code": "BAD_DECISION"})
    if body.decision == "reject" and not reason:
        raise HTTPException(422, {"code": "REASON_REQUIRED"})
    if body.decision == "approve":
        c.fatwa_status = "approved"
        c.scope_frozen = True
        sm.record_event(db, c, c.status, c.status, "committee.approve", user["role"], user["uid"],
                        {"reason": reason or None})
        _notify(db, c, "committee_approved", "KFPH 승인",
                "%s — 자기선언 KFPH(Komite Fatwa Produk Halal) 판정 승인. 인증서 발급이 가능합니다." % (c.company_name or ""),
                channels=["inapp", "sms"], role="applicant")
        db.commit()
        return {"case_id": case_id, "decision": "approved", "fatwa_status": "approved"}
    # reject
    c.fatwa_status = "rejected"
    sm.record_event(db, c, c.status, c.status, "committee.reject", user["role"], user["uid"],
                    {"reason": reason})
    _notify(db, c, "committee_rejected", "KFPH 반려",
            "%s — 자기선언 KFPH(Komite Fatwa Produk Halal) 판정 반려: %s" % (c.company_name or "", reason),
            channels=["inapp", "sms"], role="applicant")
    db.commit()
    return {"case_id": case_id, "decision": "rejected", "reason": reason}


# ── A5b: KFPH(Komite Fatwa Produk Halal) 할랄 판정서(Ketetapan Halal) — self-declare(UMK) 전용 ──
#    정규 MUI 파트와 결정문(/fatwa/decree.pdf)과 별개 트랙. PP 42/2024상 self-declare 할랄 판정은 KFPH 소관.
#    스키마 무변경 — _render_pdf_rich 재사용 + GeneratedDocument(doc_type=kfph_ketetapan) 1건 등록.
def _kfph_approve_event(db, case_id):
    """KFPH 승인(committee.approve) 최신 이벤트 — 없으면 None(미승인)."""
    return (db.query(models.WorkflowEvent)
            .filter(models.WorkflowEvent.case_id == case_id,
                    models.WorkflowEvent.action == "committee.approve")
            .order_by(models.WorkflowEvent.created_at.desc()).first())


def _kfph_ref_no(case_id):
    """KFPH 내부 참조번호(비공식) — case_id 기반 결정적 파생값. 공식 BPJPH 번호 아님(Phase0)."""
    return "KFPH/INT/" + (case_id or "")[:8].upper()


def _kfph_ketetapan_guard(db, c):
    """self-declare + KFPH 승인 필수. reguler는 409 NOT_SELF_DECLARE, 미승인은 409 NOT_APPROVED."""
    if c.pathway != "self_declare":
        raise HTTPException(409, {"code": "NOT_SELF_DECLARE", "pathway": c.pathway,
                                  "hint": "KFPH 결정문은 self_declare(SEHATI) 경로 전용입니다."})
    ev = _kfph_approve_event(db, c.case_id)
    if not ev:
        raise HTTPException(409, {"code": "NOT_APPROVED",
                                  "hint": "KFPH 승인(committee.approve) 후에만 결정문을 발급할 수 있습니다."})
    return ev


def _kfph_ketetapan_blocks(db, c, ev):
    """KFPH Ketetapan Halal 리치 PDF 블록 — 사업자·제품·자기선언 참조·KFPH 판정·결정번호·판정일·심의자."""
    rep = c.responsible_person or (c.profile_ext or {}).get("pic_name") or ""
    pd_name = _pendamping_name(db, c)
    prods = db.query(models.Product).filter_by(case_id=c.case_id).all()
    sd_doc = (db.query(models.GeneratedDocument)
              .filter_by(case_id=c.case_id, doc_type="self_declaration").first())
    decided_at = str(ev.created_at)[:10] if ev and ev.created_at else "-"
    reason = (ev.payload or {}).get("reason") if ev else None
    blocks = [
        {"type": "heading", "text": "KETETAPAN HALAL · KFPH — 할랄 판정서", "level": 1},
        {"type": "para", "text": "Komite Fatwa Produk Halal (KFPH) · 자기선언(SEHATI/UMK) 할랄 판정 — "
                                 "정규 MUI 파트와(Komisi Fatwa MUI)와 별개 트랙(PP 42/2024)."},
        {"type": "heading", "text": "사업자 정보 · Data Pelaku Usaha", "level": 2},
        {"type": "kv", "label": "회사명 · Nama Usaha", "value": c.company_name or "-"},
        {"type": "kv", "label": "NIB · Nomor Induk Berusaha", "value": c.nib or "-"},
        {"type": "kv", "label": "주소 · Alamat", "value": c.address or "-"},
        {"type": "kv", "label": "대표/책임자 · Penanggung Jawab", "value": rep or "-"},
        {"type": "heading", "text": "대상 제품 · Produk", "level": 2},
    ]
    if prods:
        rows = [[str(i + 1), p.name, p.category or ""] for i, p in enumerate(prods)]
        blocks.append({"type": "table", "headers": ["No", "Produk · 제품", "Kategori · 분류"],
                       "rows": rows, "widths": [0.12, 0.55, 0.33]})
    else:
        blocks.append({"type": "para", "text": "- 등록 제품 없음 · Tidak ada produk terdaftar -"})
    blocks.append({"type": "heading", "text": "자기선언 참조 · Referensi Surat Pernyataan", "level": 2})
    blocks.append({"type": "para", "text": ("자기선언서(Surat Pernyataan Pelaku Usaha) 등록됨 · v%d"
                                            % sd_doc.version) if sd_doc
                                    else "자기선언서 미등록(제출 후 참조) · Surat Pernyataan belum terdaftar"})
    blocks.append({"type": "kv", "label": "동반자 검증 · Verifikasi Pendamping (PPH)", "value": pd_name or "-"})
    blocks.append({"type": "heading", "text": "KFPH 할랄 판정 · Ketetapan Halal (KFPH)", "level": 2})
    blocks.append({"type": "para", "text": "Komite Fatwa Produk Halal (KFPH) menetapkan bahwa produk "
                                           "tersebut di atas HALAL berdasarkan pernyataan mandiri pelaku "
                                           "usaha (self-declare) dan verifikasi pendamping. · KFPH는 상기 제품을 "
                                           "사업자 자기선언 및 동반자 검증에 근거하여 할랄(HALAL)로 판정합니다."})
    if reason:
        blocks.append({"type": "kv", "label": "판정 근거 · Dasar Penetapan", "value": reason})
    blocks.append({"type": "kv", "label": "결정번호 · No (내부 참조·비공식)", "value": _kfph_ref_no(c.case_id)})
    blocks.append({"type": "kv", "label": "판정일 · Tanggal Penetapan", "value": decided_at})
    blocks.append({"type": "kv", "label": "심의자 · Diputuskan oleh (KFPH)",
                   "value": (ev.actor_id if ev else None) or "-"})
    blocks.append({"type": "spacer", "h": 10})
    blocks.append({"type": "signature", "slots": [
        {"role": "KFPH 심의자 · Komite Fatwa Produk Halal", "name": (ev.actor_id if ev else "") or "",
         "signed": bool(ev)},
    ]})
    blocks.append({"type": "spacer", "h": 8})
    # Phase 0 고지 — 내부 참조·비공식 + 내부 무결성 서명(공인 전자서명 아님). Phase 1: PSrE 공인 시 동적 표기.
    blocks.append({"type": "para", "text": "서명 성격 · Sifat tanda tangan : "
                   + _sig_nature_for(db, c.case_id, "kfph_ketetapan")})
    blocks.append({"type": "para", "text": _SIG_VALID_MEANING})
    blocks.append({"type": "para", "text": _LEGAL_DISCLAIMER_PDF})
    return blocks


@app.get("/cases/{case_id}/committee/ketetapan/preview")
def kfph_ketetapan_preview(case_id: str, user=Depends(auth.get_current_user), db: Session = Depends(get_db)):
    """KFPH 결정문(Ketetapan Halal) 미리보기(HTML 조각) — self_declare + KFPH 승인 전용. read-only·org격리."""
    import html
    from fastapi.responses import HTMLResponse
    c = _get_case(db, case_id, user)
    ev = _kfph_ketetapan_guard(db, c)
    rep = c.responsible_person or (c.profile_ext or {}).get("pic_name") or "-"
    pd_name = _pendamping_name(db, c) or "-"
    prods = db.query(models.Product).filter_by(case_id=case_id).all()
    reason = (ev.payload or {}).get("reason") if ev else None
    decided_at = str(ev.created_at)[:10] if ev and ev.created_at else "-"

    def e(x):
        return html.escape(str(x if x is not None else "-"))

    info = [("회사명 · Nama Usaha", c.company_name), ("NIB · Nomor Induk Berusaha", c.nib),
            ("주소 · Alamat", c.address), ("대표/책임자 · Penanggung Jawab", rep),
            ("동반자 검증 · Pendamping (PPH)", pd_name),
            ("결정번호 · No (내부 참조·비공식)", _kfph_ref_no(case_id)),
            ("판정일 · Tanggal", decided_at),
            ("심의자 · KFPH", (ev.actor_id if ev else None))]
    info_rows = "".join(
        "<tr><th style='text-align:left;padding:5px 12px;color:#475569;white-space:nowrap'>%s</th>"
        "<td style='padding:5px 12px'>%s</td></tr>" % (e(k), e(v)) for k, v in info)
    prod_rows = "".join(
        "<tr><td style='padding:3px 10px'>%d</td><td style='padding:3px 10px'>%s</td>"
        "<td style='padding:3px 10px;color:#64748b'>%s</td></tr>" % (i + 1, e(p.name), e(p.category))
        for i, p in enumerate(prods)) or \
        "<tr><td colspan='3' style='padding:6px 10px;color:#94a3b8'>- 제품 없음 · Tidak ada produk -</td></tr>"
    frag = (
        "<div style='font-size:13.5px;line-height:1.7'>"
        "<h2 style='margin:0 0 4px'>KETETAPAN HALAL · KFPH — 할랄 판정서</h2>"
        "<div style='color:#64748b;margin-bottom:10px'>Komite Fatwa Produk Halal (KFPH) · "
        "자기선언(SEHATI/UMK) — 정규 MUI 파트와와 별개 트랙</div>"
        "<table style='border-collapse:collapse;margin-bottom:12px'>%s</table>"
        "<div style='font-weight:600;margin:8px 0'>대상 제품 · Produk</div>"
        "<table style='border-collapse:collapse;margin-bottom:12px;width:100%%'><thead>"
        "<tr style='color:#475569'><th style='text-align:left;padding:3px 10px'>No</th>"
        "<th style='text-align:left;padding:3px 10px'>Produk · 제품</th>"
        "<th style='text-align:left;padding:3px 10px'>Kategori · 분류</th></tr></thead><tbody>%s</tbody></table>"
        "<div style='font-weight:600;margin:8px 0'>KFPH 할랄 판정 · Ketetapan Halal (KFPH)</div>"
        "<div style='margin-bottom:6px'>Komite Fatwa Produk Halal (KFPH)는 상기 제품을 사업자 자기선언(self-declare) 및 "
        "동반자 검증에 근거하여 <b>할랄(HALAL)</b>로 판정합니다.</div>"
        "%s"
        "<div style='border-top:1px dashed #cbd5e1;padding-top:8px;color:#64748b;font-size:12px;margin-top:8px'>"
        "⚠ %s<br>%s<br>%s</div></div>"
        % (info_rows, prod_rows,
           ("<div style='margin-bottom:6px;color:#475569'>판정 근거 · Dasar : %s</div>" % e(reason)) if reason else "",
           e(_sig_nature_for(db, c.case_id, "kfph_ketetapan")), e(_SIG_VALID_MEANING), e(_LEGAL_DISCLAIMER_PDF)))
    return HTMLResponse(content=frag)


@app.get("/cases/{case_id}/committee/ketetapan.pdf")
def kfph_ketetapan_pdf(case_id: str, user=Depends(auth.get_current_user), db: Session = Depends(get_db)):
    """KFPH 결정문(Ketetapan Halal) PDF — self_declare + KFPH 승인 전용. _render_pdf_rich 재사용 +
    GeneratedDocument(doc_type=kfph_ketetapan) 1건 등록(존재 시 재사용). 내부 참조·비공식(Phase0)."""
    from fastapi.responses import Response
    c = _get_case(db, case_id, user)
    ev = _kfph_ketetapan_guard(db, c)
    blocks = _kfph_ketetapan_blocks(db, c, ev)
    exists = db.query(models.GeneratedDocument).filter_by(case_id=case_id, doc_type="kfph_ketetapan").first()
    if not exists:
        content = "KFPH Ketetapan Halal · %s · No(내부참조·비공식) %s · %s" % (
            c.company_name or "-", _kfph_ref_no(case_id),
            str(ev.created_at)[:10] if ev and ev.created_at else "-")
        _save_gendoc(db, c, "kfph_ketetapan", content, user, status="draft")
        db.commit()
    pdf = _render_pdf_rich("KETETAPAN HALAL · KFPH — 할랄 판정서", blocks,
                           subtitle=(c.company_name or ""),
                           footer="GL-HAC AI · KFPH " + _kfph_ref_no(case_id))
    return Response(content=pdf, media_type="application/pdf",
                    headers={"Content-Disposition": "attachment; filename=kfph_ketetapan_%s.pdf" % case_id[:8]})


@app.post("/cases/{case_id}/fatwa/decree")
def gen_fatwa_decree(case_id, user=Depends(rbac.require_action("fatwa.document.read")), db=Depends(get_db)):
    """Fatwa Decision(HALAL DECREE) 생성 — 위원회 결정·제품·서명 병합. gen-doc 저장.
    정족수 하드게이트: 위원장 서명 + 서명 위원 총 ≥ 2 미충족 시 409 FATWA_QUORUM_NOT_MET."""
    c = _get_case(db, case_id, user)
    ok_q, missing_q, qctx = _fatwa_quorum_ok(db, case_id)
    if not ok_q:
        raise HTTPException(409, {"code": "FATWA_QUORUM_NOT_MET", "missing": missing_q, **qctx})
    _audit(db, user, "fatwa.decree.generate", "fatwa", case_id, case_id)
    fd = db.query(models.FatwaDecision).filter_by(case_id=case_id).first()
    prods = db.query(models.Product).filter_by(case_id=case_id).all()
    cert = db.query(models.HalalCertificate).filter_by(case_id=case_id).first()
    lines = []
    lines.append("[할랄 판결문 · Fatwa Decision (HALAL DECREE) — %s]" % (c.company_name or case_id[:8]))
    lines.append("결정번호: %s" % (fd.decision_no if fd and fd.decision_no else "(미발급)"))
    lines.append("결정: %s" % (fd.decision if fd else "pending"))
    lines.append("위원장: %s" % ((fd.committee_head if fd else None) or "-"))
    lines.append("간사: %s" % ((fd.committee_secretary if fd else None) or "-"))
    lines.append("위원: %s" % (", ".join(fd.committee_members) if fd and fd.committee_members else "-"))
    lines.append("대상제품: %s" % (", ".join(p.name for p in prods) or "-"))
    lines.append("유효기간(Valid Until): %s" % ((cert.expiry_date if cert else None) or "-"))
    lines.append("※ 고정 전문(꾸란·하디스 보일러플레이트)은 정적 자산으로 결합됨.")
    g = _save_gendoc(db, c, "fatwa_decree", "\n".join(lines), user)
    db.commit()
    return {"document": "\n".join(lines), "version": g.version, "gen_doc_id": g.gen_doc_id,
            "decision": (fd.decision if fd else "pending")}


@app.get("/cases/{case_id}/fatwa/decree.pdf")
def get_fatwa_decree_pdf(case_id, user=Depends(rbac.require_action("fatwa.document.read")), db=Depends(get_db)):
    """Fatwa Decree 리치 PDF — 정적 전문(있으면 삽입) + 결정·제품부록 + 위원회 3서명."""
    from fastapi.responses import Response
    c = _get_case(db, case_id, user)
    fd = db.query(models.FatwaDecision).filter_by(case_id=case_id).first()
    prods = db.query(models.Product).filter_by(case_id=case_id).all()
    cert = db.query(models.HalalCertificate).filter_by(case_id=case_id).first()
    head = fd.committee_head if fd else None
    sec = fd.committee_secretary if fd else None
    members = (fd.committee_members or []) if fd else []
    first_member = members[0] if members else None
    votes = {v.member: v.vote for v in db.query(models.FatwaVote).filter_by(case_id=case_id).all()}

    def _signed(nm):
        return bool(nm) and (votes.get(nm) == "approve" or (fd and fd.decided_at is not None))

    preamble_path = os.path.join(os.path.dirname(__file__), "static", "fatwa_preamble.pdf")
    preamble_bytes = open(preamble_path, "rb").read() if os.path.exists(preamble_path) else None
    blocks = []
    if preamble_bytes:
        blocks.append({"type": "static_pdf", "data": preamble_bytes})
    blocks.append({"type": "heading", "text": "SHARIA COMMITTEE — GL Halal Center (GLHAC)", "level": 1})
    blocks.append({"type": "heading", "text": "REGARDING: HALAL PRODUCT DECISION", "level": 2})
    blocks.append({"type": "kv", "label": "Number", "value": fd.decision_no if fd and fd.decision_no else "-"})
    blocks.append({"type": "kv", "label": "Decision", "value": fd.decision if fd else "pending"})
    blocks.append({"type": "kv", "label": "Company", "value": c.company_name or "-"})
    blocks.append({"type": "kv", "label": "Factory", "value": c.factory_address or "-"})
    if not preamble_bytes:
        blocks.append({"type": "para", "text": "[CONSIDERING] 꾸란·하디스·피끄 및 MUI 지침에 근거 (고정 전문 — 정적 자산 미탑재 시 요약 표기)."})
    blocks.append({"type": "para", "text": "[DECIDED] 부록의 제품은 할랄로 결정된다."})
    blocks.append({"type": "heading", "text": "APPENDIX · Products", "level": 2})
    rows = [[str(i + 1), prod.name, prod.category or ""] for i, prod in enumerate(prods)]
    blocks.append({"type": "table", "headers": ["No", "Product", "Category"], "rows": rows, "widths": [0.12, 0.55, 0.33]})
    blocks.append({"type": "kv", "label": "Valid Until", "value": (cert.expiry_date if cert else None) or "-"})
    blocks.append({"type": "kv", "label": "Decision Date", "value": str(fd.decided_at)[:10] if fd and fd.decided_at else "-"})
    blocks.append({"type": "spacer", "h": 10})
    blocks.append({"type": "signature", "slots": [
        {"role": "위원장 (Leader)", "name": head or "", "signed": _signed(head)},
        {"role": "간사 (Secretary)", "name": sec or "", "signed": _signed(sec)},
        {"role": "위원 (Member)", "name": first_member or "", "signed": _signed(first_member)},
    ]})
    pdf = _render_pdf_rich("HALAL DECREE · Fatwa Decision", blocks, subtitle=(c.company_name or ""),
                           footer="GL-HAC AI · Fatwa " + ((fd.decision_no if fd and fd.decision_no else "") or ""))
    return Response(content=pdf, media_type="application/pdf",
                    headers={"Content-Disposition": "attachment; filename=fatwa_decree_%s.pdf" % case_id[:8]})


# ── 심사원 배정 양식 (Audit Team Deployment Form) ────────────────────────
# 심사기관이 어느 심사팀을 어느 업체에 보내는지 확정하는 문서다. 이해충돌 고지가
# 붙어 있어(최근 2년 내 SJPH/HPAS 컨설팅 접촉 시 교체) 서명 전에 심사원이 읽어야 한다.
#
# 서명자는 설정값으로 둔다 — 담당자가 바뀔 때 코드를 고치지 않게. 기본값은 현행 양식.
DEPLOY_SIGNERS = [
    {"key": "coordinator",
     "name": os.environ.get("GLHAC_DEPLOY_COORDINATOR", "Hadiputra Rizky"),
     "title": os.environ.get("GLHAC_DEPLOY_COORDINATOR_TITLE", "Halal Auditor Coordinator")},
    {"key": "impartiality",
     "name": os.environ.get("GLHAC_DEPLOY_IMPARTIALITY", "Park Kyung Mi"),
     "title": os.environ.get("GLHAC_DEPLOY_IMPARTIALITY_TITLE", "Impartiality Committee")},
]

DEPLOY_NOTICE = ("If you have had any contact with the Company in relation to SJPH/HPAS "
                 "consultancy in the last two years, please notify your supervisor "
                 "immediately to be replaced by another Auditor.")

_DEPLOY_POSITION = {"ketua": "Lead Auditor", "anggota": "Auditor",
                    "observer": "Observer", "expert": "Technical Expert"}


def _deploy_form_data(db, c):
    """양식에 들어갈 값 — 없는 값은 빈칸으로 둔다.

    지어내지 않는다. 심사원이 서명하는 문서에 시스템이 만든 값이 들어가면, 틀려도
    아무도 모른 채 확정된다. 비어 있으면 사람이 채우거나 데이터를 고치면 된다."""
    pe = c.profile_ext or {}
    prods = db.query(models.Product).filter_by(case_id=c.case_id).all()
    kinds = []
    for pr in prods:
        k = (pr.category or "").strip()
        if k and k not in kinds:
            kinds.append(k)
    # 제품 유형(category)이 비면 제품명으로 대신하지 않는다 — 다른 개념이다.
    plan = (db.query(models.AuditPlan).filter_by(case_id=c.case_id)
            .order_by(models.AuditPlan.created_at.desc()).first())
    pool = (db.query(models.AuditorPool).filter_by(case_id=c.case_id)
            .order_by(models.AuditorPool.assigned_at).all())
    team = [{"no": i, "name": a.name or "",
             "position": _DEPLOY_POSITION.get((a.role_in_team or "").lower(),
                                              a.role_in_team or ""),
             "cert_no": a.cert_no or ""}
            for i, a in enumerate(pool, 1)]
    if not team and plan and isinstance(plan.auditors, list):
        # 심사원 풀이 비어 있어도 배정 계획에 이름이 잡혀 있으면 그것을 쓴다.
        # 직책은 계획에 없으므로 비운다 — 없는 값을 만들지 않는다.
        team = [{"no": i, "name": str(nm), "position": "", "cert_no": ""}
                for i, nm in enumerate([x for x in plan.auditors if x], 1)]
    if not team:
        # 마지막 소스 — 운영자가 배정한 오디터(ops.auditor_assigned). 배정이 곧 파견이다.
        asg = _ops_latest_assignment(db, [c.case_id]).get(c.case_id) or {}
        if asg.get("auditor_name"):
            team = [{"no": 1, "name": asg["auditor_name"],
                     "position": _DEPLOY_POSITION["ketua"], "cert_no": ""}]
    audit_date = str(plan.scheduled_date)[:10] if plan and plan.scheduled_date else ""
    if not audit_date:
        # 일정 조율로 확정된 날짜(onsite_schedule.confirm)도 정식 심사일이다.
        audit_date = ((_onsite_sched_state(db, c.case_id).get("confirmed") or {})
                      .get("date") or "")
    return {
        "id_no": c.nib or "",                       # NIB — 업체 식별번호
        "company_name": c.company_name or "",
        "office_address": c.address or pe.get("address") or "",
        "factory_address": c.factory_address or pe.get("factory_address") or "",
        # 제품에 유형이 안 잡혀 있으면 업체가 신고한 제품유형을 쓴다(같은 개념).
        "product_service": ", ".join(kinds) or (pe.get("product_type") or "").strip(),
        "trademark": (pe.get("trademark") or "").strip(),
        "audit_date": str(audit_date)[:10],
        "lph_name": (plan.lph_name if plan else "") or "",
        "team": team,
    }


def _deploy_form_blocks(d):
    # 라벨은 번역하지 않는다 — LPH에 제출하는 영문 정식 양식이다.
    kv = lambda label, val: {"type": "kv", "label": label, "value": val or ""}   # noqa: E731
    blocks = [   # 제목은 _render_pdf_rich 머리말이 이미 찍는다 — 여기서 또 찍지 않는다.
        kv("ID No.", d["id_no"]),
        kv("Company Name", d["company_name"]),
        kv("Office Address", d["office_address"]),
        kv("Factory Address / place of business", d["factory_address"]),
        kv("Type of Product / Service", d["product_service"]),
        kv("Trademark", d["trademark"]),
        kv("Audit Date", d["audit_date"]),
        {"type": "spacer", "h": 8},
        {"type": "table", "headers": ["No", "Name", "Position"],
         "widths": [0.1, 0.5, 0.4],
         # 정식 양식의 3칸을 그대로 유지 — 모자란 줄은 비워 손으로 채울 수 있게 둔다.
         "rows": ([[str(t["no"]), t["name"], t["position"]] for t in d["team"]]
                  + [[str(i), "", ""] for i in range(len(d["team"]) + 1, 4)])},
        {"type": "spacer", "h": 10},
        {"type": "para", "text": DEPLOY_NOTICE},
        {"type": "spacer", "h": 14},
        {"type": "signature",
         "slots": [{"role": "%s · %s" % (s0["name"], s0["title"]), "name": "", "signed": False}
                   for s0 in DEPLOY_SIGNERS]},
    ]
    return blocks


@app.get("/cases/{case_id}/audit-deployment")
def audit_deployment_data(case_id: str,
                          user=Depends(auth.require_roles("auditor", "fatwa_liaison",
                                                          "operator", "admin", "consultant")),
                          db: Session = Depends(get_db)):
    """배정 양식에 들어갈 값 — 화면이 미리 보고 빈칸을 채울 수 있게."""
    c = _get_case(db, case_id, user)
    d = _deploy_form_data(db, c)
    missing = [k for k in ("id_no", "company_name", "office_address", "factory_address",
                           "product_service", "trademark", "audit_date") if not d.get(k)]
    return {**d, "missing": missing, "signers": DEPLOY_SIGNERS, "notice": DEPLOY_NOTICE}


@app.get("/cases/{case_id}/audit-deployment.pdf")
def audit_deployment_pdf(case_id: str,
                         user=Depends(auth.require_roles("auditor", "fatwa_liaison",
                                                         "operator", "admin", "consultant")),
                         db: Session = Depends(get_db)):
    """심사원 배정 양식 PDF — 서명해서 보관하는 문서."""
    from fastapi.responses import Response
    c = _get_case(db, case_id, user)
    d = _deploy_form_data(db, c)
    pdf = _render_pdf_rich("Audit Team Deployment Form", _deploy_form_blocks(d),
                           subtitle=d["company_name"] or None,
                           footer="GL-HAC · Audit Team Deployment")
    _audit(db, user, "audit_deployment.download", "case", case_id, case_id)
    fn = "audit_deployment_%s.pdf" % (case_id[:8])
    return Response(pdf, media_type="application/pdf",
                    headers={"Content-Disposition": 'inline; filename="%s"' % fn})


@app.get("/cases/{case_id}/factory-audit.docx")
def get_factory_audit_docx(case_id, lang: str = Query("ko"),
                           user=Depends(auth.get_current_user), db=Depends(get_db)):
    """현장심사 보고서 — 기준 템플릿 그대로의 편집 가능한 docx.

    lang 은 '우리가 채우는 값'의 언어다. 서식 라벨은 템플릿 소유라 그대로 둔다."""
    from fastapi.responses import Response
    c = _get_case(db, case_id, user)
    data = _factory_audit_docx_bytes(db, c, lang)
    _audit(db, user, "document.download", "case", case_id,
           meta={"doc": "factory_audit.docx"})
    db.commit()
    return Response(content=data,
                    media_type="application/vnd.openxmlformats-officedocument."
                               "wordprocessingml.document",
                    headers={"Content-Disposition":
                             _content_disposition("factory_audit_%s_%s.docx"
                                                  % (case_id[:8], (lang or "ko").lower()))})


@app.get("/cases/{case_id}/factory-audit.pdf")
def get_factory_audit_pdf(case_id, lang: str = Query("ko"),
                          user=Depends(auth.get_current_user), db=Depends(get_db)):
    """현장심사 보고서 PDF — 기준 템플릿(Factory Audit Template.docx)을 채워 변환한다.

    예전에는 블록을 코드로 그려 양식이 오디터가 쓰는 서식과 달랐다. 이제 템플릿이 정본이고
    코드는 값만 채운다. LibreOffice 가 없는 환경에서는 예전 렌더러로 떨어진다(다운로드 보장)."""
    from fastapi.responses import Response
    c0 = _get_case(db, case_id, user)
    try:
        pdf = _docx_to_pdf_bytes(_factory_audit_docx_bytes(db, c0, lang))
        _audit(db, user, "document.download", "case", case_id,
               meta={"doc": "factory_audit.pdf", "source": "template", "lang": lang})
        db.commit()
        return Response(content=pdf, media_type="application/pdf",
                        headers={"Content-Disposition":
                                 "attachment; filename=factory_audit_%s_%s.pdf"
                                 % (case_id[:8], (lang or "ko").lower())})
    except Exception as e:  # noqa: BLE001
        log.warning("현장심사 템플릿 PDF 실패 — 기존 렌더러로 폴백: %s", e)
    return _get_factory_audit_pdf_legacy(case_id, user, db)


def _get_factory_audit_pdf_legacy(case_id, user, db):
    """예전 블록 렌더러 — 템플릿/LibreOffice 사용 불가 시 폴백."""
    import base64
    from fastapi.responses import Response
    c = _get_case(db, case_id, user)
    mats = db.query(models.Material).filter_by(case_id=case_id).all()
    finds = db.query(models.AuditFinding).filter_by(case_id=case_id).all()
    hpas = db.query(models.HpasEvaluation).filter_by(case_id=case_id).all()
    pen = db.query(models.PenyeliaHalal).filter_by(org_id=c.org_id, status="active").first()
    lph = db.query(models.LphAssignment).filter_by(case_id=case_id).first()
    photos = db.query(models.DocumentAsset).filter_by(case_id=case_id).all()
    # 문서용 장 제목은 화면 표기와 다르다(번호가 붙는다) — 사전에 별도 축으로 둔다.
    HPAS_KO = _dd_mod.code_labels("HPAS_CHAPTER", "hpas_chapter", "ko")

    def cnc(m):
        return "NC" if (m.screen_result in ("BLOCK", "NEEDS_EVIDENCE")) else "C"

    blocks = []
    blocks.append({"type": "heading", "text": "FACTORY AUDIT REPORT · 현장심사 보고서", "level": 1})
    blocks.append({"type": "kv", "label": "기업", "value": c.company_name or "-"})
    blocks.append({"type": "kv", "label": "공장", "value": c.factory_address or "-"})
    blocks.append({"type": "kv", "label": "심사팀(LPH)", "value": lph.lph_name if lph else "-"})
    blocks.append({"type": "heading", "text": "1. 원재료 목록 · Materials (C/NC)", "level": 2})
    mat_rows = [[str(i + 1), m.name, m.mat_type or "-", m.source or "-", cnc(m),
                 (m.screen_status or m.screen_severity or "-")] for i, m in enumerate(mats)] or [["-", "없음", "", "", "", ""]]
    blocks.append({"type": "table", "headers": ["No", "Name·Brand", "Type", "Source", "C/NC", "Findings"],
                   "widths": [0.08, 0.30, 0.16, 0.16, 0.10, 0.20], "rows": mat_rows})
    blocks.append({"type": "heading", "text": "2. 지적사항 · Findings", "level": 2})
    find_rows = [[f.area or "-", f.finding or "-", f.severity or "-", f.status or "-"] for f in finds] or [["-", "없음", "", ""]]
    blocks.append({"type": "table", "headers": ["구역", "지적", "심각도", "상태"],
                   "widths": [0.25, 0.4, 0.17, 0.18], "rows": find_rows})
    blocks.append({"type": "heading", "text": "3. HPAS 5기준 · Criteria", "level": 2})
    hpa_rows = [[HPAS_KO.get(h.element, h.element), h.status or "-", h.note or "-"] for h in hpas] or [["-", "없음", ""]]
    blocks.append({"type": "table", "headers": ["기준", "상태", "비고"], "widths": [0.34, 0.2, 0.46], "rows": hpa_rows})
    blocks.append({"type": "heading", "text": "4. 증거 사진 · Evidence Photos", "level": 2})
    img_count = 0
    for d in photos:
        if img_count >= 6:
            break
        if d.content_b64 and (d.content_type or "").startswith("image/"):
            try:
                img_data = base64.b64decode(d.content_b64)
                caption = "%s · %s%s" % (d.filename, d.doc_type or "",
                                         (" · GPS %.4f,%.4f" % (d.lat, d.lng) if d.lat and d.lng else ""))
                blocks.append({"type": "image", "data": img_data, "caption": caption})
                img_count += 1
            except Exception:
                continue
    if img_count == 0:
        blocks.append({"type": "para", "text": "첨부된 이미지 증거 없음."})
    blocks.append({"type": "spacer", "h": 10})
    blocks.append({"type": "signature", "slots": [
        {"role": "Lead Auditor", "name": (lph.lph_name if lph else (finds[0].auditor if finds else "")) or "", "signed": bool(finds)},
        {"role": "Halal Supervisor", "name": (pen.name if pen else ""), "signed": bool(pen)},
    ]})
    pdf = _render_pdf_rich("Factory Audit Report", blocks, subtitle=(c.company_name or ""),
                           footer="GL-HAC AI · Factory Audit " + case_id[:8])
    return Response(content=pdf, media_type="application/pdf",
                    headers={"Content-Disposition": "attachment; filename=factory_audit_%s.pdf" % case_id[:8]})


SJPH_APPENDICES = [
    ("Appendix 1. 할랄정책 포스터", "auto", None),
    ("Appendix 2. 할랄관리팀 임명장", "upload", "halal_supervisor"),
    ("Appendix 3. 할랄교육 자료·이수", "upload", "training"),
    ("Appendix 4. 사용재료 목록", "auto", None),
    ("Appendix 5. 전제품 재료목록", "auto", None),
    ("Appendix 6. 재료 구매기록", "upload", "purchase_log"),
    ("Appendix 7. 입고검사 기록", "upload", "receiving_log"),
    ("Appendix 8. 무돈육시설 선언서", "auto", None),
    ("Appendix 9. 시설 배치도", "upload", "facility_layout"),
    ("Appendix 10. 보관 기록", "upload", "usage_log"),
    ("Appendix 11. 생산 공정흐름도", "upload", "production_flow"),
    ("Appendix 12. 생산 기록", "upload", "production_log"),
    ("Appendix 13. 유통·판매 기록", "upload", "distribution_log"),
    ("Appendix 14. 신규재료 승인", "auto", None),
    ("Appendix 15. 내부심사 체크리스트", "upload", "internal_audit"),
    ("Appendix 16. 경영검토 회의록", "upload", None),
    ("Appendix 17. 종합 요약", "auto", None),
]

# ===== SJPH/HPAS Manual — 공식 템플릿(GLHAC HPAS SJPH Template.docx) 정합 정적 원문(EN/KO 병기) =====
# 아래 문구는 템플릿 원문 그대로. 동적값(회사명·감독관·재료·제품)은 _sjph_manual_blocks에서 치환.
SJPH_LEGAL_BASIS = [
    "Law No. 33 of 2014 concerning Halal Product Assurance / 할랄제품보장에 관한 2014년 법률 제33호",
    "Law No. 6 of 2023 concerning Job Creation / 고용창출에 관한 2023년 법률 제6호",
    "Government Regulation No. 39 of 2021 concerning the Implementation of the Halal Product Assurance Sector / 할랄제품보장 분야 시행에 관한 2021년 정부령 제39호",
    "Regulation of the Minister of Religious Affairs No. 42 of 2024 concerning the Administration of Halal Product Assurance / 할랄제품보장 운영에 관한 2024년 종교부 장관령 제42호",
    "Decree of the Minister of Religious Affairs No. 982 of 2019 concerning Halal Certification Services / 할랄 인증 서비스에 관한 2019년 종교부 장관 결정 제982호",
    "Decree of the Minister of Religious Affairs No. 748 of 2021 concerning Types of Products Required to Obtain Halal Certification / 할랄 인증 의무 제품 유형에 관한 2021년 종교부 장관 결정 제748호",
    "Decree of the Minister of Religious Affairs No. 944 of 2024 concerning Types of Products Required to Obtain Halal Certification / 할랄 인증 의무 제품 유형에 관한 2024년 종교부 장관 결정 제944호",
    "Decree of the Head of BPJPH No. 57 of 2021 concerning the Criteria for the Halal Product Assurance System / 할랄제품보장청(BPJPH) 청장 결정 제57호(2021) — 할랄제품보장시스템 기준",
]
SJPH_PURPOSE_EN = ("The Halal Product Assurance System (HPAS) Manual is prepared as a guideline for the "
                   "implementation of the SJPH within the company, in order to maintain the continuity of "
                   "halal production in accordance with halal certification requirements established by the "
                   "Halal Product Assurance Organizing Agency (BPJPH) and the halal determination decisions "
                   "issued by the Indonesian Ulama Council (MUI).")
SJPH_PURPOSE_KO = ("할랄제품보장시스템(SJPH) 매뉴얼은 회사 내 SJPH 운영의 지침으로 작성되었으며, 할랄제품보장청(BPJPH)에서 "
                   "정한 할랄 인증 요건과 인도네시아 울레마 협의회(MUI)의 제품 할랄성 결정에 따라 할랄 생산의 지속성을 "
                   "유지하기 위한 목적을 가진다.")
SJPH_SCOPE_EN = ("The SJPH Manual is a document that serves as a guideline for the implementation of the SJPH "
                 "within the company. This SJPH Manual applies to all company facilities related to the halal "
                 "product process (PPH), including outlets, toll manufacturing facilities, and rented warehouses.")
SJPH_SCOPE_KO = ("SJPH 매뉴얼은 회사 내 SJPH 운영을 위한 지침 문서이다. 본 SJPH 매뉴얼은 아웃렛, 위탁생산 시설 및 임대 "
                 "창고를 포함하여 할랄 제품 공정(PPH)과 관련된 회사의 모든 시설에 적용된다.")
SJPH_POLICY = [
    "Using certified halal ingredients. / 인증된 할랄 재료 사용.",
    "Implementing the Halal Product Process (PPH) at all production stages. / 모든 생산 단계에서 할랄 제품 공정(PPH)을 시행.",
    "Providing adequate resources and facilities to support the implementation of HPAS. / HPAS 구현을 지원하기 위해 적절한 자원 및 시설 제공.",
    "Ensuring that all personnel understand and adhere to this halal policy. / 모든 직원이 이 할랄 정책을 이해하고 준수하도록 보장.",
    "Communicating the halal policy to all relevant stakeholders. / 모든 관련 이해관계자에게 할랄 정책을 전달.",
    "Halal policy poster attached in appendix 1. / 할랄 정책 포스터는 부록 1에 첨부되어 있습니다.",
]
SJPH_HRD = [
    "Internal Training: All personnel involved in the PPH will receive internal training on HPAS. / 내부 교육: PPH에 관련된 모든 직원은 HPAS에 대한 내부 교육을 받게 됩니다.",
    "External Training: The Halal Supervisor will attend training organized by BPJPH or other designated institutions. / 외부 교육: 할랄 감독관은 BPJPH 또는 기타 지정된 기관이 주최하는 교육에 참석하게 됩니다.",
    "Documentation: The company will maintain training records as proof of implementation. / 문서화: 당사는 구현의 증거로 교육 기록을 유지합니다.",
]
SJPH_PROCUREMENT = [
    "All materials are purchased from suppliers who can guarantee their halal status and possess a valid Halal Certificate, except for exempted materials. / 모든 재료는 면제된 재료를 제외하고, 할랄 상태를 보증하고 유효한 할랄 인증서를 소지한 공급업체로부터 구매해야 합니다.",
    "Upon arrival, the receiving team inspects supporting documents and ensures there is no cross-contamination. / 도착 시, 수령 팀은 보조 문서를 검사하고 교차 오염이 없는지 확인해야 합니다.",
    "Materials are stored separately from non-halal materials. / 재료는 비할랄 재료와 별도로 보관해야 합니다.",
    "Appendix 7 (Ingredient Inspection Form) is filled out for every incoming material. / 부록 7(재료 검사 양식)은 새로운 재료가 도착할 때마다 작성됩니다.",
    "Appendix 8 (Ingredient Purchase Records) records every material purchase. / 부록 8(재료 구매 기록)은 모든 재료 구매를 기록합니다.",
    "Appendix 9 (Ingredient Storage Records) is filled out for every material storage event. / 부록 9(재료 보관 기록)는 재료가 보관될 때마다 작성됩니다.",
]
SJPH_HPP_DOCS = [
    ["1.", "Production Facility Layout / 생산 시설 배치도", "[ ]", "생산 시설 이미지 또는 배치도 첨부"],
    ["2.", "Production Flow Diagram / 생산 흐름도", "[ ]", "할랄 생산 공정 흐름도 첨부"],
    ["3.", "Material Receiving Log / 원료 입고 기록", "[ ]", "원료의 원산지·준수 여부 추적 기록"],
    ["4.", "Material Purchase Log / 원료 구매 기록", "[ ]", "영수증·구매 내역서 등 첨부"],
    ["5.", "Production Log / 생산 기록", "[ ]", "일일/배치 생산 결과·로트 코드"],
    ["6.", "Distribution Log / 유통 기록", "[ ]", "제품 유통·판매 기록"],
]
SJPH_CLOSING = ("I hereby declare that all information and documents provided in this application are true and "
                "correct. I understand that any intentional misrepresentation, falsification, or deliberate "
                "concealment of facts that leads to non-compliance or the detection of non-halal/impure (najis) "
                "substances will be subject to legal prosecution in accordance with applicable laws. / "
                "본 신청서에 제공된 모든 정보와 문서가 진실하고 정확함을 서약합니다. 고의적인 허위 진술·위조·사실 은폐로 "
                "비준수 또는 비할랄/불순물(나지스) 성분이 발견될 경우 관련 법률에 따라 법적 처벌을 받을 수 있음을 이해합니다.")


def _sjph_mat_judgment(m):
    """Material screen 결과 → EN/KO 판정 라벨(없으면 '—')."""
    s = (m.screen_status or m.screen_result or "").lower()
    mp = {"halal": "Halal 할랄", "haram": "Haram 하람", "mushbooh": "Mushbooh 의심",
          "syubhat": "Mushbooh 의심", "block": "Blocked 차단", "needs_evidence": "Evidence req. 증빙필요",
          "pass": "Halal 할랄", "clear": "Halal 할랄", "ok": "Halal 할랄"}
    return mp.get(s, (m.screen_status or m.screen_result or "—"))


def _sjph_manual_blocks(db, c):
    """공식 SJPH/HPAS Template 구조로 _render_pdf_rich 블록 생성.
    표지 → 법적근거 → Bismillah → 목적·범위 → 고객정보 → 1~5장 → 종결서약 → HPAS 준비도 → 부록.
    정적 원문은 템플릿 원문 그대로(EN/KO 병기), 동적값은 케이스 데이터로 치환(없으면 '—')."""
    from datetime import datetime as _dt
    px = c.profile_ext or {}
    pen = db.query(models.PenyeliaHalal).filter_by(org_id=c.org_id, status="active").first()
    mats = db.query(models.Material).filter_by(case_id=c.case_id).all()
    prods = db.query(models.Product).filter_by(case_id=c.case_id).all()
    have = _ensure_hpas(db, c.case_id)
    ev = {e.item_key for e in db.query(models.SjphEvidence).filter_by(case_id=c.case_id).all()}
    D = "—"
    company = c.company_name or c.case_id[:8]
    today = _dt.utcnow().strftime("%Y-%m-%d")  # 서버 UTC(요구: Date.now 금지)
    sup_name = pen.name if pen else (c.halal_supervisor or D)
    resp = c.responsible_person or D

    def v(x):
        return x if (x not in (None, "")) else D

    def px2(a, b):
        return "%s / %s" % (v(px.get(a)), v(px.get(b)))

    B = []
    # ---------- 표지 ----------
    B += [
        {"type": "kv", "label": "Company Name / 회사명", "value": company},
        {"type": "kv", "label": "Date / 날짜", "value": today},
        {"type": "kv", "label": "Version / 버전", "value": "GL-HAC HPAS Manual 1.0 · 2026 April 1st version"},
        {"type": "signature", "slots": [
            {"role": "Audited by · GL-HAC Halal Auditor", "name": D, "signed": False},
            {"role": "Reviewed by · GL-HAC Sharia Board", "name": D, "signed": False},
        ]},
    ]
    # ---------- 법적 근거 ----------
    B.append({"type": "heading", "text": "LEGAL BASIS · 법적 근거", "level": 2})
    B += [{"type": "para", "text": "· " + law} for law in SJPH_LEGAL_BASIS]
    # ---------- Bismillah ----------
    B.append({"type": "para", "text": "Bismillah ar-Rahman ar-Rahim — 가장 자비롭고 은혜로우신 하나님의 이름으로"})
    # ---------- 목적·범위 ----------
    B += [
        {"type": "heading", "text": "PURPOSE AND SCOPE · 목적 및 적용 범위", "level": 2},
        {"type": "para", "text": "Purpose / 목적"},
        {"type": "para", "text": SJPH_PURPOSE_EN},
        {"type": "para", "text": SJPH_PURPOSE_KO},
        {"type": "para", "text": "Scope / 범위"},
        {"type": "para", "text": SJPH_SCOPE_EN},
        {"type": "para", "text": SJPH_SCOPE_KO},
    ]
    # ---------- 고객 정보(Client Information — Template Table) ----------
    B.append({"type": "heading", "text": "Client Information · 고객 정보", "level": 2})
    B.append({"type": "table", "headers": ["Field / 항목", "Value / 내용"], "widths": [0.4, 0.6], "rows": [
        ["Client / Company Name 고객·회사명", company],
        ["Address 회사 주소", v(c.address)],
        ["Factory Address 공장 주소", v(c.factory_address)],
        ["Phone 전화", v(c.phone)],
        ["Email 이메일", v(c.email)],
        ["Business Reg. No (NIB/ID TAX) 사업자번호", v(c.nib)],
        ["PIC Name / Title 담당자·직책", px2("pic_name", "pic_title")],
        ["Contact Person Name / Title 업무담당자·직책", px2("cp_name", "cp_title")],
        ["Registration Type 등록유형", v(px.get("registration_type"))],
        ["Application Type 신청유형", v(px.get("application_type"))],
        ["Registration Status 등록현황", v(px.get("registration_status"))],
        ["Production Capacity 생산능력", v(px.get("production_capacity"))],
        ["Total Products 제품 수", str(len(prods))],
    ]})
    # ---------- 1. Halal Commitment & Responsibility ----------
    B.append({"type": "heading", "text": "1. Halal Commitment & Responsibility · 할랄 서약 및 책임", "level": 2})
    B.append({"type": "heading", "text": "A. Halal Policy · 할랄 정책", "level": 2})
    B.append({"type": "para", "text": "%s is fully committed to consistently and continuously producing halal "
                                      "products in accordance with Islamic law and applicable regulations. / "
                                      "%s은(는) 이슬람 율법과 관련 규정에 따라 일관적·지속적으로 할랄 제품을 생산할 것을 "
                                      "전적으로 서약합니다. This policy includes / 이 정책은 다음을 포함합니다:" % (company, company)})
    B += [{"type": "para", "text": "· " + p} for p in SJPH_POLICY]
    B.append({"type": "heading", "text": "B. Halal Management Team · 할랄 관리팀", "level": 2})
    B.append({"type": "para", "text": "The management of %s appoints a Halal Supervisor to be responsible for "
                                      "managing, monitoring, and ensuring the proper implementation of HPAS. / "
                                      "%s 경영진은 HPAS의 올바른 구현을 관리·모니터링·보장할 책임이 있는 할랄 감독관을 "
                                      "임명합니다." % (company, company)})
    B.append({"type": "kv", "label": "Name / 성명", "value": sup_name})
    B.append({"type": "kv", "label": "Position / 직책", "value": "Halal Supervisor / 할랄 감독관"})
    team_rows, n = [], 0
    if resp != D:
        n += 1
        team_rows.append([str(n), resp, "Responsible Person / 책임자", "Management", D])
    n += 1
    team_rows.append([str(n), sup_name, "Halal Supervisor / 할랄 감독관", "Halal Supervisor", D])
    if c.halal_supervisor and c.halal_supervisor not in (sup_name, resp):
        n += 1
        team_rows.append([str(n), c.halal_supervisor, "Halal Supervisor / 할랄 감독관", "Halal Supervisor", D])
    B.append({"type": "table", "headers": ["No", "Name / 성명", "Position / 직책", "In Team / 팀내 역할", "Sign / 서명"],
              "widths": [0.07, 0.3, 0.28, 0.23, 0.12], "rows": team_rows})
    B.append({"type": "para", "text": "The Halal Management Team and/or Halal Supervisor have read and understood "
                                      "the HPAS Manual and will implement all of its criteria. / 할랄 관리팀 및/또는 "
                                      "할랄 감독자는 HPAS 매뉴얼을 읽고 이해하였으며 설명된 모든 기준을 이행합니다."})
    B.append({"type": "heading", "text": "C. Human Resource Development · 인적 자원 개발", "level": 2})
    B += [{"type": "para", "text": "· " + h} for h in SJPH_HRD]
    # ---------- 2. Raw Materials ----------
    B.append({"type": "heading", "text": "2. Raw Materials · 원료", "level": 2})
    B.append({"type": "heading", "text": "2.1 Halal Materials List · 할랄 재료 목록", "level": 2})
    B.append({"type": "para", "text": "All ingredients used in %s's products are halal (whether certified or "
                                      "excluded from the halal certification mandate). Form-5 lists all materials "
                                      "with name, producer, and country of origin. / %s의 제품에 사용되는 모든 재료는 "
                                      "할랄이며, Form-5(사용 재료 목록)에 재료명·생산자·원산지를 포함해 작성됩니다." % (company, company)})
    mat_rows = [[str(i + 1), m.name, v(m.mat_type), v(m.supplier), _sjph_mat_judgment(m)]
                for i, m in enumerate(mats)] or [["—", D, D, D, D]]
    B.append({"type": "table", "headers": ["No", "Material / 재료명", "Type / 유형", "Producer / 생산자", "Status / 판정"],
              "widths": [0.07, 0.32, 0.17, 0.24, 0.2], "rows": mat_rows})
    B.append({"type": "heading", "text": "2.2 Material Procurement Procedure · 재료 조달 절차", "level": 2})
    B += [{"type": "para", "text": "· " + p} for p in SJPH_PROCUREMENT]
    # ---------- 3. Halal Production Process ----------
    B.append({"type": "heading", "text": "3. Halal Production Process (HPP) · 생산 공정", "level": 2})
    B.append({"type": "heading", "text": "A. Pork-Free Statement · 돼지고기 성분 무첨가 서약", "level": 2})
    B.append({"type": "para", "text": "I, the undersigned below / 아래 서명인은 다음과 같이 선언합니다:"})
    B.append({"type": "kv", "label": "Full name / 성명", "value": resp})
    B.append({"type": "kv", "label": "Identity Number / 주민등록번호", "value": D})
    B.append({"type": "kv", "label": "Position / 직위", "value": ("Responsible Person / 책임자" if resp != D else D)})
    B.append({"type": "para", "text": "I hereby declare that our production facility is completely free from pork "
                                      "and its derivatives, and there is no cross-contamination from non-halal "
                                      "products. The facility and all its equipment are maintained clean and free "
                                      "from impurities (najis). / 당사의 생산 시설은 돼지고기 및 그 부산물로부터 완전히 "
                                      "자유로우며, 비할랄 제품으로부터의 교차 오염이 없음을 선언합니다. 생산 시설과 모든 "
                                      "장비는 깨끗하고 불순물(나지스)로부터 자유로운 상태를 유지합니다."})
    B.append({"type": "kv", "label": "Date / 날짜", "value": today})
    B.append({"type": "signature", "slots": [{"role": "Name & Position / 성명·직책", "name": resp, "signed": False}]})
    B.append({"type": "heading", "text": "B. Required HPP Documents and Logs · 필수 HPP 문서·기록", "level": 2})
    B.append({"type": "table", "headers": ["No", "Item / 항목", "Completion / 완료", "Evidence / 증빙"],
              "widths": [0.07, 0.45, 0.15, 0.33], "rows": SJPH_HPP_DOCS})
    # ---------- 4. Product Criteria ----------
    B.append({"type": "heading", "text": "4. Product Criteria · 제품 기준", "level": 2})
    B.append({"type": "heading", "text": "4.1 Product Design · 제품 디자인", "level": 2})
    B.append({"type": "para", "text": "Product names, logos, packaging, and images are not in conflict with "
                                      "Islamic law or social norms. Form-3 lists all products manufactured and "
                                      "indicates which are submitted for halal certification. / 제품명·로고·포장·이미지는 "
                                      "이슬람 율법이나 사회적 규범과 충돌하지 않습니다. Form-3(생산 제품 목록)에 제조된 모든 "
                                      "제품과 할랄 인증 신청 제품을 표시합니다."})
    prod_rows = [[str(i + 1), p.name, v(p.category), v(p.registration_type), v(p.status)]
                 for i, p in enumerate(prods)] or [["—", D, D, D, D]]
    B.append({"type": "table", "headers": ["No", "Product / 제품명", "Category / 분류", "Reg. Type / 등록유형", "Status / 상태"],
              "widths": [0.07, 0.35, 0.23, 0.2, 0.15], "rows": prod_rows})
    B.append({"type": "heading", "text": "4.2 Labeling and Identification · 라벨링 및 식별", "level": 2})
    B.append({"type": "para", "text": "Certified products carry the Halal Label on the packaging in a visible, "
                                      "durable location. Each product has an identification code (e.g., batch "
                                      "number, production date) for traceability. / 인증된 제품은 포장에 눈에 잘 띄고 "
                                      "내구성 있는 위치에 할랄 라벨을 표시하며, 각 제품은 추적성을 위한 식별 코드(예: 배치 "
                                      "번호, 생산 날짜)를 가집니다."})
    # ---------- 5. Monitoring and Evaluation ----------
    B.append({"type": "heading", "text": "5. Monitoring and Evaluation · 모니터링 및 평가", "level": 2})
    B.append({"type": "heading", "text": "5.1 Internal Audit · 내부 감사", "level": 2})
    B.append({"type": "para", "text": "An internal HPAS audit is conducted at least once a year to evaluate the "
                                      "consistency of implementation. Audit results are documented. / HPAS 내부 감사는 "
                                      "구현의 일관성을 평가하기 위해 1년에 최소 한 번 수행되며, 결과는 문서화됩니다."})
    B.append({"type": "heading", "text": "5.2 Management Review · 경영 검토", "level": 2})
    B.append({"type": "para", "text": "Management reviews the implementation of HPAS to ensure effectiveness. The "
                                      "results of the review are reported to the BPJPH. / 경영진은 HPAS의 효과성을 "
                                      "보장하기 위해 구현을 검토하며, 결과는 BPJPH에 보고됩니다."})
    # ---------- 종결 서약 ----------
    B.append({"type": "heading", "text": "Closing Statement · 종결 서약", "level": 2})
    B.append({"type": "para", "text": SJPH_CLOSING})
    B.append({"type": "signature", "slots": [{"role": "Name & Position / 성명·직책", "name": resp, "signed": False}]})
    # ---------- HPAS 5요소 준비도(내부 진행 참고) ----------
    B.append({"type": "heading", "text": "HPAS 5-Element Readiness · HPAS 5요소 준비도", "level": 2})
    chap_rows = [[str(idx + 1), HPAS_KO.get(el, el), (o.status if o else "-"), (o.note if o and o.note else "-")]
                 for idx, el in enumerate(HPAS_ELEMENTS) for o in [have.get(el)]]
    B.append({"type": "table", "headers": ["Ch", "Element / 기준", "Status / 상태", "Note / 비고"],
              "widths": [0.1, 0.34, 0.18, 0.38], "rows": chap_rows})
    # ---------- 부록(17종) + 증빙 게이트 ----------
    B.append({"type": "heading", "text": "Appendices · 부록 (17종, 증빙 소스)", "level": 2})
    appx_rows, done, upload_total = [], 0, 0
    for i, (label, typ, key) in enumerate(SJPH_APPENDICES):
        if typ == "auto":
            st = "자동생성"
        else:
            upload_total += 1
            st = "완료" if (key and key in ev) else "미첨부"
            if st == "완료":
                done += 1
        appx_rows.append([str(i + 1), label, ("자동생성" if typ == "auto" else "증빙업로드"), st])
    B.append({"type": "table", "headers": ["No", "Appendix / 부록", "Source / 소스", "Status / 상태"],
              "widths": [0.08, 0.54, 0.18, 0.2], "rows": appx_rows})
    gate_ok = (done == upload_total)
    B.append({"type": "para", "text": ("✔ 전 증빙 완료 — Manual SJPH 생성 가능. 자동생성 후 실물 사본 보관 필수."
                                       if gate_ok else
                                       "⚠ 증빙 미완료(%d/%d) — 전 항목 완료 전까지 공식 Manual SJPH 생성 비활성. 본 문서는 준비용 초안." % (done, upload_total))})
    B.append({"type": "spacer", "h": 8})
    B.append({"type": "para", "text": "※ 하람·고위험 원재료는 증빙 필수. 공식 SJPH는 BPJPH/SIHALAL 절차로 확정."})
    return B


def _sjph_blocks_to_text(company, blocks):
    """SJPH 블록 → gen-doc 저장용 텍스트(회사명·감독관명 등 내용 포함). 버전관리 content."""
    L = ["[SJPH/HPAS Manual — %s]" % company]
    for b in blocks or []:
        t = b.get("type")
        if t == "heading":
            L += ["", "== %s ==" % b.get("text", "")]
        elif t == "para":
            L.append(b.get("text", ""))
        elif t == "kv":
            L.append("%s: %s" % (b.get("label", ""), b.get("value", "")))
        elif t == "table":
            L.append(" | ".join(str(x) for x in b.get("headers", [])))
            for r in b.get("rows", []):
                L.append(" | ".join(str(x) for x in r))
        elif t == "signature":
            for s in b.get("slots", []):
                L.append("[서명] %s: %s" % (s.get("role", ""), s.get("name", "") or "—"))
    return "\n".join(L)


# ===== SJPH Manual — 원본 docx 템플릿 병합 (회의 2026-07-10: 기준 파일과 완전히 동일한 양식) =====
SJPH_TEMPLATE_DOCX = os.path.join(os.path.dirname(__file__), "assets", "GLHAC_HPAS_SJPH_Template.docx")


def _sjph_docx_para_replace(p, repl):
    txt = p.text
    new = txt
    for k, v in repl.items():
        if k in new:
            new = new.replace(k, v)
    if new != txt:
        if p.runs:
            p.runs[0].text = new
            for r in p.runs[1:]:
                r.text = ""
        else:
            p.add_run(new)


def _sjph_xml_text_replace(el, repl):
    """w:t 노드를 직접 순회하며 치환 — sdt(콘텐츠 컨트롤) 등 Paragraph.runs가 못 보는 영역 보강.
    한 w:t 안에 온전히 들어있는 플레이스홀더만 처리한다(문단 단위 치환의 후처리용)."""
    from docx.oxml.ns import qn as _qn
    for t in el.iter(_qn("w:t")):
        s = t.text or ""
        if not s:
            continue
        new = s
        for k, v in repl.items():
            if k in new:
                new = new.replace(k, v)
        if new != s:
            t.text = new


def _sjph_docx_cell_set(cell, value):
    p = cell.paragraphs[0]
    if p.runs:
        p.runs[0].text = str(value)
        for r in p.runs[1:]:
            r.text = ""
    else:
        p.add_run(str(value))


def _sjph_docx_label_fill(tbl, prefix, value):
    """라벨 폼 셀(예: 'Date 날짜')에 값을 굵게 덧붙임 — 첫 매칭 셀만."""
    v = "" if value is None else str(value).strip()
    if not v:
        return
    for row in tbl.rows:
        for cell in row.cells:
            t = cell.text.strip()
            if t.startswith(prefix) and v not in t:
                run = cell.add_paragraph().add_run(v)
                run.bold = True
                return


def _sjph_clone_rows(tbl, proto_ri, anchor_ri, n):
    """proto_ri 행을 서식 원형으로 n개 복제해 anchor_ri 행 '뒤에' 순서대로 삽입.
    addnext를 연속 호출하면 역순이 되므로 커서를 갱신해 순방향을 유지한다."""
    if n <= 0:
        return
    import copy as _copy
    proto = tbl.rows[proto_ri]._tr
    cur = tbl.rows[anchor_ri]._tr
    for _ in range(n):
        tr = _copy.deepcopy(proto)
        cur.addnext(tr)
        cur = tr


def _sjph_drop_rows(tbl, from_ri, to_ri):
    """from_ri..to_ri(포함) 행을 역순으로 삭제. 인덱스 밀림 방지."""
    for ri in range(to_ri, from_ri - 1, -1):
        if ri < 0 or ri >= len(tbl.rows):
            continue
        tr = tbl.rows[ri]._tr
        tr.getparent().remove(tr)


def _sjph_fill_row(cells, vals):
    """vals를 cells에 순서대로 채운다. 빈 값도 반드시 써서 템플릿 샘플 텍스트를 지운다."""
    for ci, v in enumerate(vals[:len(cells)]):
        _sjph_docx_cell_set(cells[ci], v)


def _sjph_para_after(par):
    """python-docx에는 insert-after가 없어 oxml로 직접 다음 위치에 문단 생성."""
    from docx.oxml import OxmlElement
    from docx.text.paragraph import Paragraph
    el = OxmlElement("w:p")
    par._p.addnext(el)
    return Paragraph(el, par._parent)


# 빌더 섹션 → 템플릿 삽입 앵커(해당 문단 바로 뒤에 이미지 임베드)
SJPH_IMAGE_ANCHORS = {
    "org_chart": "B. Halal Management Team",
    "material_process": "Appendix 11. Production Process Flowchart",
    "halal_declaration": "Appendix 2. Halal Management Team appointment letter",
}


def _sjph_insert_layout_images(doc, db, case_id):
    """매뉴얼 빌더에서 드롭한 이미지(조직도·공정도·서명)를 docx 해당 위치에 삽입."""
    import base64 as _b64
    import io as _io
    from docx.shared import Inches
    try:
        inserts = (_sjph_manual_layout_view(db, case_id).get("inserts") or {})
    except Exception:
        return
    for key, ins in inserts.items():
        anchor = SJPH_IMAGE_ANCHORS.get(key)
        doc_id = (ins or {}).get("document_id")
        if not anchor or not doc_id:
            continue
        d = db.get(models.DocumentAsset, doc_id)
        if not d or not d.content_b64 or not str(d.content_type or "").startswith("image/") or d.case_id != case_id:
            continue   # 비이미지(PDF 등)는 원문 보관만 / 타 케이스 자산은 IDOR 차단
        par = next((p for p in doc.paragraphs if p.text.strip().startswith(anchor)), None)
        if par is None:
            continue
        try:
            img = _b64.b64decode(d.content_b64)
            cap_p = _sjph_para_after(par)
            run = cap_p.add_run()
            run.add_picture(_io.BytesIO(img), width=Inches(5.5))
            cap = (ins or {}).get("caption") or ins.get("filename") or ""
            if cap:
                _sjph_para_after(cap_p).add_run(str(cap)[:600]).italic = True
        except Exception as e:
            log.warning("sjph 이미지 임베드 실패(%s): %s", key, e)
    _sjph_insert_uploaded_images(doc, db, case_id, set(inserts or {}))


# 빌더에서 드롭하지 않아도, 업체가 올린 파일이 있으면 그 자리에 넣는다.
# ZIP·RAR 로 한꺼번에 올린 업체는 빌더를 거치지 않는다 — 그러면 매뉴얼의 공정도·배치도
# 자리가 제목만 남아 빈다. 파일이 있는데 비워 두는 건 매뉴얼로서 의미가 없다.
# 부록·본문 도면 자리(슬롯) — 어떤 문서가 어디로 가는지의 정의. 자동 추정은 '초안'이고
# 사람이 지정한 값이 언제나 이긴다. 조용히 추측해 넣고 틀려도 아무도 모르는 구조를
# 없애기 위해, 슬롯마다 무엇이 왜 선택됐는지 화면에 내보낸다.
SJPH_SLOT_LABELS = {
    "halal_policy": ("할랄 방침문", "Halal policy statement", "Pernyataan kebijakan halal"),
    "halal_training": ("할랄 교육자료", "Halal training material", "Materi pelatihan halal"),
    "facility_layout": ("시설 배치도(부록 9)", "Facility layout (Appendix 9)",
                        "Denah fasilitas (Lampiran 9)"),
    "material_process": ("제조공정도(부록 11)", "Process flowchart (Appendix 11)",
                         "Diagram alir proses (Lampiran 11)"),
    "body_layout": ("시설 배치도(본문)", "Facility layout (body)", "Denah fasilitas (isi)"),
    "body_process": ("제조공정도(본문)", "Process flowchart (body)", "Diagram alir (isi)"),
}
SJPH_PLACEMENT_ACTION = "sjph_manual.placement"

_SJPH_DOC_FALLBACK = {
    # 부록 1은 '할랄 방침문'(대표 서명본), 부록 3은 '할랄 교육자료'다. 둘 다 파일명이
    # Halal_ 로 시작해 헷갈리기 쉬운데 서류로서 전혀 다르다 — 방침문이 아무 데도 안 들어가고
    # 교육 게시물이 방침 자리를 차지하고 있었다(실측: Halal_Policy.pdf / Halal_Posters.pdf).
    "halal_policy": ("Appendix 1. Halal Policy Poster", "term", "KEBIJAKAN_HALAL",
                     "할랄 방침문 · Halal policy statement"),
    "halal_training": ("Appendix 3. Halal Education training material",
                       "evidence", "training",
                       "할랄 교육자료 · Halal training material"),
    "facility_layout": ("Appendix 9. Production facility layout",
                        "evidence", "facility_layout",
                        "시설 배치도 · Production facility layout"),
    "material_process": ("Appendix 11. Production Process Flowchart",
                         "doc_type", "process_flow",
                         "제조공정도 · Production process flowchart"),
    # 본문에도 도면 자리가 따로 있다. 부록에만 넣으면 본문 '생산 시설 배치도 /
    # 생산 공정 흐름도' 항목이 제목만 남아 빈다 — 매뉴얼을 읽는 사람은 본문부터 본다.
    "body_layout": ("생산 시설 배치도", "evidence", "facility_layout",
                    "시설 배치도 · Production facility layout"),
    "body_process": ("생산 공정 흐름도", "doc_type", "process_flow",
                     "제조공정도 · Production process flowchart"),
}


def _sjph_placement_override(db, case_id):
    """사람이 지정한 배치(slot → document_id). latest-wins. 없으면 빈 dict."""
    e = (db.query(models.WorkflowEvent)
         .filter(models.WorkflowEvent.case_id == case_id,
                 models.WorkflowEvent.action == SJPH_PLACEMENT_ACTION)
         .order_by(models.WorkflowEvent.created_at.desc()).first())
    raw = ((e.payload or {}).get("slots") if e else None) or {}
    return {k: v for k, v in raw.items() if k in _SJPH_DOC_FALLBACK}


def _sjph_pick_for_slot(db, case_id, key, docs=None, ev=None, by_id=None, lang="ko"):
    """슬롯에 들어갈 문서와 그 근거 — (문서, 근거) 또는 (None, 사유). 근거는 lang 을 따른다."""
    from . import domain_dict as _dd2
    spec = _SJPH_DOC_FALLBACK.get(key)
    if not spec:
        return None, "unknown_slot"
    _anchor, kind, want, _cap = spec
    docs = docs if docs is not None else \
        db.query(models.DocumentAsset).filter_by(case_id=case_id).all()
    if ev is None:
        ev = {e.item_key: e.document_id
              for e in db.query(models.SjphEvidence).filter_by(case_id=case_id).all()}
    by_id = by_id if by_id is not None else {d.document_id: d for d in docs}
    # 근거 앞머리는 심사자가 읽는 문구다 — 화면 언어를 따르지 않으면 인니어 화면에 한글이 뜬다
    li = {"ko": 0, "en": 1, "id": 2}.get((lang or "ko").lower(), 0)
    why = {"doc_type": ("문서유형", "doc type", "jenis dokumen"),
           "term": ("사전", "dictionary", "kamus"),
           "evidence": ("증빙", "evidence", "bukti")}
    if kind == "doc_type":
        d = next((x for x in docs if x.doc_type == want), None)
        return d, ("%s=%s" % (why["doc_type"][li], want))
    if kind == "term":
        for x in docs:
            base = re.sub(r"\.[A-Za-z0-9]{2,5}$", "", x.filename or "")
            if _dd2.lookup(base) == want:
                return x, ("%s %s" % (why["term"][li], want))
        return None, ("%s %s" % (why["term"][li], want))
    return by_id.get(ev.get(want)), ("%s %s" % (why["evidence"][li], want))


def _sjph_insert_uploaded_images(doc, db, case_id, already):
    """업로드된 서류에서 도면·사진을 찾아 해당 부록 자리에 넣는다.

    우선순위: 빌더 삽입 > 사람이 지정한 배치 > 자동 추정. 사람이 '비움'으로 지정하면
    자동 추정으로 되돌아가지 않는다 — 지운 걸 다시 채워 넣으면 지정한 의미가 없다."""
    import io as _io
    from docx.shared import Inches
    override = _sjph_placement_override(db, case_id)
    docs = db.query(models.DocumentAsset).filter_by(case_id=case_id).all()
    ev = {e.item_key: e.document_id
          for e in db.query(models.SjphEvidence).filter_by(case_id=case_id).all()}
    by_id = {d.document_id: d for d in docs}
    for key, (anchor, kind, want, caption) in _SJPH_DOC_FALLBACK.items():
        if key in already:
            continue          # 빌더에서 직접 넣은 게 있으면 그쪽이 우선
        if key in override:
            oid = override[key]
            if not oid:
                continue                      # 사람이 '비움'으로 지정
            d = by_id.get(oid)
            if d is None:
                continue                      # 지정한 문서가 사라짐 — 자동으로 덮지 않는다
        else:
            d, _why = _sjph_pick_for_slot(db, case_id, key, docs, ev, by_id)
        img = _doc_image_bytes(d)
        if not img:
            continue
        par = next((p for p in doc.paragraphs if p.text.strip().startswith(anchor)), None)
        if par is None:
            continue
        try:
            cap_p = _sjph_para_after(par)
            cap_p.add_run().add_picture(_io.BytesIO(img), width=Inches(5.5))
            tail = _sjph_para_after(cap_p)
            tail.add_run("%s — %s" % (caption, d.filename or "")).italic = True
            if key == "facility_layout":     # 배치도 뒤에 현장·공장 사진을 이어 붙인다
                # 이미 도면으로 넣은 파일(배치도·공정도)은 사진으로 또 넣지 않는다.
                _used = {d.document_id}
                _pf = (db.query(models.DocumentAsset)
                       .filter_by(case_id=case_id, doc_type="process_flow").first())
                if _pf is not None:
                    _used.add(_pf.document_id)
                for sp in _site_photo_docs(db, case_id, _used, limit=3):
                    simg = _doc_image_bytes(sp)
                    if not simg:
                        continue
                    ip = _sjph_para_after(tail)
                    ip.add_run().add_picture(_io.BytesIO(simg), width=Inches(5.5))
                    tail = _sjph_para_after(ip)
                    tail.add_run("현장 사진 · Site photo — %s" % (sp.filename or "")).italic = True
        except Exception as e:  # noqa: BLE001
            log.warning("sjph 업로드 이미지 삽입 실패(%s): %s", key, e)


def _sjph_stamp_bytes(db, doc_id, case_id):
    """DocumentAsset id → 이미지 bytes. 이미지가 아니거나 타 케이스 자산이면 None."""
    if not doc_id:
        return None
    d = db.get(models.DocumentAsset, doc_id)
    if not d or not d.content_b64 or not str(d.content_type or "").startswith("image/"):
        return None
    if d.case_id != case_id:
        log.warning("sjph 도장 IDOR 차단 — doc=%s case=%s", doc_id, case_id)
        return None
    try:
        import base64 as _b64
        return _b64.b64decode(d.content_b64)
    except Exception:
        return None


def _sjph_cell_stamp(cell, img, width_in=1.1):
    """셀에 도장 이미지 삽입 — add_picture는 run에만 가능."""
    import io as _io
    from docx.shared import Inches
    # 셀에 라벨 텍스트가 있으면 그 아래 새 문단에, 빈 셀이면 첫 문단을 재사용
    p = cell.paragraphs[0] if (cell.paragraphs and not cell.text.strip()) else cell.add_paragraph()
    p.add_run().add_picture(_io.BytesIO(img), width=Inches(width_in))


def _sjph_insert_stamps(doc, db, case_id):
    """서명자 이름·직책·도장을 템플릿 서명칸에 병합. 도장이 없어도 [SIGN]류 자리표시자는 지운다(빈 서명란 유지)."""
    try:
        signers = _sjph_norm_signers((_sjph_manual_layout_latest(db, case_id) or {}).get("signers"))
    except Exception:
        signers = {}
    import io as _io2
    from docx.shared import Inches as _Inches2
    cache = {}

    def _img(key):
        sid = (signers.get(key) or {}).get("stamp_document_id")
        if not sid:
            return None
        if sid not in cache:
            cache[sid] = _sjph_stamp_bytes(db, sid, case_id)
        return cache[sid]

    def _label(key):
        v = signers.get(key) or {}
        return "\n".join([x for x in [v.get("name") or "", v.get("position") or ""] if x])

    star_seen = 0
    for t in doc.tables:
        try:
            # (a) 1행 2열 서명표 — 별표는 표지(GL-HAC 심사원·샤리아), 나머지는 본문 대표 서약
            if len(t.rows) == 1 and len(t.columns) == 2:
                lbl = t.rows[0].cells[0].text.strip()
                if not lbl.startswith("Name & Position"):
                    continue
                if lbl.rstrip().endswith("*"):
                    key = "glhac_auditor" if star_seen == 0 else "glhac_sharia"
                    star_seen += 1
                else:
                    key = "ceo"
                txt = _label(key)
                if txt:
                    run = t.rows[0].cells[0].add_paragraph().add_run(txt)
                    run.bold = True
                img = _img(key)
                if img is not None:
                    _sjph_cell_stamp(t.rows[0].cells[1], img)
            # (b) 4열 CEO/Halal Supervisor 표 — 끝에서 두 번째 행이 도장 자리
            elif len(t.columns) == 4 and len(t.rows) >= 3:
                last = t.rows[-1].cells
                if not last[0].text.strip().startswith("CEO"):
                    continue
                if not last[3].text.strip().startswith("Halal Supervisor"):
                    continue
                srow = t.rows[-2].cells
                ci = _img("ceo")
                if ci is not None:
                    _sjph_cell_stamp(srow[0], ci)
                si = _img("halal_supervisor")
                if si is not None:
                    _sjph_cell_stamp(srow[3], si)
        except Exception as e:
            log.warning("sjph 도장 삽입 실패: %s", e)
    # 텍스트 플레이스홀더형 서명 자리 — [CEO SIGN]/[CEO 서명]은 대표자 도장, [SIGN]은 실물 서명란(비움)
    from docx.oxml.ns import qn as _qn2
    from docx.text.paragraph import Paragraph as _Para2
    ceo_img = _img("ceo")
    for _p in list(doc.element.body.iter(_qn2("w:p"))):
        try:
            para = _Para2(_p, doc)
            txt = para.text.strip()
            if txt in ("[CEO SIGN]", "[CEO 서명]"):
                for r in para.runs:
                    r.text = ""
                if ceo_img is not None:
                    para.add_run().add_picture(_io2.BytesIO(ceo_img), width=_Inches2(1.1))
            elif txt == "[SIGN]":
                for r in para.runs:
                    r.text = ""
        except Exception as e:
            log.warning("sjph 서명 자리 처리 실패: %s", e)


def _sjph_tbl(doc, ri, ci, needle, ncols=None):
    """헤더 문구로 표를 찾는다 — 표 인덱스는 템플릿이 바뀌면 밀린다."""
    for t in doc.tables:
        if ncols is not None and len(t.columns) != ncols:
            continue
        if len(t.rows) > ri and len(t.rows[ri].cells) > ci:
            if needle.lower() in t.rows[ri].cells[ci].text.strip().lower():
                return t
    return None


def _sjph_photo_records(db, case_id):
    """업체가 낸 기록물 사진(Catatan…)을 OCR해 표 행으로 복원 → {증빙키: [행]}.

    수량·날짜·담당자는 DB에 없고 이 사진 안에만 있다. 지어내지 않고 여기서 읽어 온다.
    좌표 기반이라 열이 섞여도 행이 복원된다. 실패하면 그 표는 빈칸으로 둔다."""
    import tempfile
    from . import ai_local as _ai
    from . import record_forms as _rf
    out = {}
    rows = (db.query(models.SjphEvidence).filter_by(case_id=case_id).all())
    doc_ids = {e.item_key: e.document_id for e in rows if e.document_id}
    for key in ("purchase_log", "receiving_log", "usage_log",
                "production_log", "distribution_log"):
        did = doc_ids.get(key)
        if not did:
            continue
        d = db.get(models.DocumentAsset, did)
        if not d or not d.content_b64 or not (d.content_type or "").startswith("image/"):
            continue
        try:
            # 인테이크에서 이미 읽어둔 게 있으면 그걸 쓴다 — 같은 사진을 매번 다시
            # OCR하느라 매뉴얼 한 번 만들 때마다 사진당 8~12초를 썼다.
            lines = d.ocr_lines
            if not lines:
                raw = base64.b64decode(str(d.content_b64).split(",")[-1])
                ext = "." + (d.filename or "x.png").rsplit(".", 1)[-1].lower()
                with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as f:
                    f.write(raw)
                    path = f.name
                try:
                    lines = _ai.ocr_image(path, "korean").get("lines") or []
                finally:
                    os.unlink(path)
                if lines:            # 다음 생성부터는 읽지 않도록 남겨둔다
                    d.ocr_lines = lines[:4000]
                    db.commit()
            parsed = _rf.parse_rows(key, lines or [])
            if parsed:
                out[key] = parsed
        except Exception as e:  # noqa: BLE001
            log.warning("기록물 사진 해석 실패 %s: %s", key, e)
    return out


def _sjph_fill_record_tables(doc, mats, prods, links, c, photo=None):
    """부록 6·7·10·12·13 기록양식에 실데이터를 넣는다.

    지어내지 않는 것: 수량·입출고일자·생산일자는 플랫폼이 가진 값이 아니다. 인증 매뉴얼에
    없는 숫자를 채우면 그건 위조다. 그래서 아는 것(재료명·공급사·원산지·제품·판정·담당자)만
    채우고 모르는 칸은 업체가 적도록 비워 둔다."""
    pic = c.halal_supervisor or (c.profile_ext or {}).get("pic_name") or ""
    prod_of = {}
    if links and prods:
        pn = {p.product_id: p.name for p in prods}
        for l in links:
            prod_of.setdefault(l.material_id, pn.get(l.product_id, ""))

    def brand(m):
        return "%s – %s" % (m.name or "", m.supplier or "") if m.supplier else (m.name or "")

    photo = photo or {}
    # 부록 6 재료 구매기록 — r0·r1 헤더, r2~r6 데이터 5행, r7 'etc.'
    t = _sjph_tbl(doc, 0, 1, "material name – brand", 5)
    if t is not None:
        pr = photo.get("purchase_log")
        rows = ([[r["no"], r["name"], r.get("qty", ""), r.get("date", ""), r.get("pic", "") or pic]
                 for r in pr] if pr else
                [[str(i + 1), brand(m), "", "", pic] for i, m in enumerate(mats)])
        if rows:
            _sjph_records_into(t, 2, 6, rows)
    # 부록 7 입고검사 — r0 헤더, r1 데이터 1행뿐이라 복제해서 늘린다
    t = _sjph_tbl(doc, 0, 1, "arrival date", 5)
    pr = photo.get("receiving_log")
    if t is not None and pr:
        _sjph_records_into(t, 1, 1, [
            [r["no"], r.get("date", ""), r["name"], r.get("supplier", ""),
             r.get("conform", "")] for r in pr])
    elif t is not None and mats:
        _sjph_records_into(t, 1, 1, [
            [str(i + 1), "", brand(m), "%s%s" % (m.supplier or "",
                                                 (" / " + m.origin) if m.origin else ""),
             ("Conforming 적합" if m.screen_result in ("PASS", "CLEARED") else "Check 확인필요")]
            for i, m in enumerate(mats)])
    # 부록 10 재료·제품 보관기록 — r0·r1 헤더, r2~r6 데이터
    t = _sjph_tbl(doc, 0, 1, "material name", 8)
    pr = photo.get("usage_log")
    if t is not None and pr:
        _sjph_records_into(t, 2, 6, [
            [r["no"], r["name"], "", r.get("supplier", ""), r.get("in", ""),
             r.get("out", ""), r.get("qty", ""), r.get("pic", "") or pic] for r in pr])
    elif t is not None and mats:
        _sjph_records_into(t, 2, 6, [
            [str(i + 1), m.name or "", prod_of.get(m.material_id, ""), m.supplier or "",
             "", "", "", pic] for i, m in enumerate(mats)])
    # 부록 12 생산기록 — r0 헤더, r1 예시행, r2~r6 데이터
    t = _sjph_tbl(doc, 0, 4, "note", 5)
    pr = photo.get("production_log")
    if t is not None and pr:
        _sjph_records_into(t, 2, 6, [[r["no"], r.get("date", ""), r["name"],
                                      r.get("qty", ""), r.get("note", "")] for r in pr])
    elif t is not None and prods:
        _sjph_records_into(t, 2, 6, [[str(i + 1), "", p.name or "", "", ""]
                                     for i, p in enumerate(prods)])
    # 부록 13 유통·판매기록
    t = _sjph_tbl(doc, 0, 4, "destination", 5)
    pr = photo.get("distribution_log")
    if t is not None and pr:
        _sjph_records_into(t, 2, 6, [[r["no"], r.get("date", ""), r["name"],
                                      r.get("qty", ""), r.get("dest", "")] for r in pr])
    elif t is not None and prods:
        _sjph_records_into(t, 2, 6, [[str(i + 1), "", p.name or "", "", ""]
                                     for i, p in enumerate(prods)])


def _sjph_records_into(tbl, first_ri, last_ri, rows):
    """데이터행 구간(first_ri~last_ri)에 rows 를 채운다. 모자라면 복제, 남으면 삭제."""
    slots = last_ri - first_ri + 1
    n = len(rows)
    if n > slots:
        _sjph_clone_rows(tbl, first_ri, last_ri, n - slots)
    for i, vals in enumerate(rows):
        ri = first_ri + i
        if ri >= len(tbl.rows):
            break
        _sjph_fill_row(tbl.rows[ri].cells, vals)
    if n < slots:
        _sjph_drop_rows(tbl, first_ri + n, last_ri)


def _sjph_check_hpas_yes(doc):
    """HPAS 기준 설문(65행)의 '예' 칸에 체크. 질문 행만 대상이다.

    구분 기준은 물음표다. 대분류('COMMITMENT AND RESPONSIBILITY')·소제목('Halal Policy')·
    안내문('Questions can be added as needed')에는 체크하지 않는다 — 질문이 아닌 줄에
    답을 표시하면 서류가 이상해진다."""
    t = _sjph_tbl(doc, 1, 3, "yes", 6)
    if t is None:
        return 0
    n = 0
    for row in t.rows[2:]:
        cells = row.cells
        if len(cells) < 5:
            continue
        q = (cells[1].text or "").strip()
        if "?" not in q or q.lower().startswith("questions can be added"):
            continue
        _sjph_docx_cell_set(cells[3], "V")     # Yes 네
        _sjph_docx_cell_set(cells[4], "")      # No 아니요
        n += 1
    return n


FACTORY_AUDIT_TEMPLATE = os.path.join(os.path.dirname(__file__), "assets",
                                      "GLHAC_Factory_Audit_Template.docx")


def _doc_image_bytes(d):
    """문서 자산 → 이미지 바이트. PDF 는 첫 장을 그림으로 굽는다(배치도가 PDF 로 온다)."""
    if not d or not d.content_b64:
        return None
    try:
        raw = base64.b64decode(str(d.content_b64).split(",")[-1])
    except Exception:  # noqa: BLE001
        return None
    if (d.content_type or "").startswith("image/"):
        return raw
    if (d.content_type or "") == "application/pdf" or (d.filename or "").lower().endswith(".pdf"):
        try:
            import fitz
            return fitz.open(stream=raw, filetype="pdf")[0].get_pixmap(dpi=150).tobytes("png")
        except Exception:  # noqa: BLE001
            return None
    return None


# 파일명이 현장·공장을 가리키는 말들(3개 언어). 업체는 '창고 사진.pdf' 처럼 보내온다.
_SITE_WORDS = ("사진", "현장", "공장", "창고", "시설", "설비", "실사",
               "foto", "pabrik", "gudang", "ruang", "produksi", "fasilitas", "lokasi",
               "photo", "factory", "warehouse", "site", "facility", "plant")
# 부록 표에 이미 값으로 들어간 기록물 — 사진으로 또 붙이면 중복이다.
_RECORD_KEYS = ("purchase_log", "receiving_log", "usage_log",
                "production_log", "distribution_log")
# 사진이 아니라 '서류'인 유형 — 이름에 공장·사진이 들어가도 현장 사진으로 보지 않는다.
_NOT_A_PHOTO = ("factory_registration", "nib_business_license", "halal_certificate",
                "quality_cert", "coa_msds", "material_list", "product_list",
                "supplier_declaration", "sjph_manual", "origin_certificate")


def _site_photo_docs(db, case_id, exclude_ids=(), limit=4):
    """업로드된 파일 중 현장·공장 이미지를 고른다.

    업체는 별도 '현장사진' 유형으로 올리지 않고 그냥 파일로 넣는다(실측: '창고 사진.pdf').
    그래서 유형만 보지 않고 파일명도 함께 본다. 부록 표에 이미 쓰인 기록물 사진은 뺀다."""
    from . import domain_dict as _dd
    ev = {e.document_id for e in db.query(models.SjphEvidence)
          .filter(models.SjphEvidence.case_id == case_id,
                  models.SjphEvidence.item_key.in_(_RECORD_KEYS)).all() if e.document_id}
    skip = set(exclude_ids) | ev
    out = []
    for d in (db.query(models.DocumentAsset).filter_by(case_id=case_id)
              .order_by(models.DocumentAsset.created_at).all()):
        if d.document_id in skip or len(out) >= limit:
            continue
        name = (d.filename or "").lower()
        # 서류는 사진이 아니다. '공장등록증'이 '공장'이라는 글자 때문에 현장 사진으로
        # 잡히던 오탐을 막는다(실측). 유형이 이미 밝혀진 증서·목록류는 제외한다.
        if d.doc_type in _NOT_A_PHOTO:
            continue
        # 부록 표에 값으로 들어간 기록물(Catatan…)도 사진으로 또 붙이지 않는다.
        base = re.sub(r"\.[A-Za-z0-9]{2,5}$", "", d.filename or "")
        if _dd.evidence_key_of(base) in _RECORD_KEYS:
            continue
        is_photo_type = d.doc_type in ("onsite_evidence_photo", "product_photo",
                                       "mock_evidence_photo", "facility_photo")
        if not (is_photo_type or any(w in name for w in _SITE_WORDS)):
            continue
        if not ((d.content_type or "").startswith("image/") or name.endswith(".pdf")):
            continue
        out.append(d)
    return out


def _fa_insert_images(doc, db, c, lang="ko"):
    """'Manufacturing Process Diagram / 제조공정도' 제목 아래에 실제 도면을 넣는다.

    제목만 있고 그림이 없으면 심사 보고서로서 의미가 없다 — 공정 흐름은 그림으로 봐야 한다.
    업체가 낸 공정도·시설배치도·현장 사진을 순서대로 붙인다. 없으면 없다고 적는다."""
    import io as _io
    from docx.enum.text import WD_ALIGN_PARAGRAPH as _AL
    from docx.shared import Inches as _In

    L = _dd_mod.text_fn(lang)
    anchor = next((p for p in doc.paragraphs
                   if p.text.strip().startswith("Manufacturing Process Diagram")), None)
    if anchor is None:
        return
    docs = db.query(models.DocumentAsset).filter_by(case_id=c.case_id).all()
    ev = {e.item_key: e.document_id
          for e in db.query(models.SjphEvidence).filter_by(case_id=c.case_id).all()}
    by_id = {d.document_id: d for d in docs}
    picks = []
    pf = next((d for d in docs if d.doc_type == "process_flow"), None)
    if pf is not None:
        picks.append((pf, L("제조공정도")))
    lay = by_id.get(ev.get("facility_layout"))
    if lay is not None:
        picks.append((lay, L("시설 배치도")))
    used = {x.document_id for x, _c in picks}
    for d in _site_photo_docs(db, c.case_id, used, limit=3):
        picks.append((d, L("현장 사진")))
    cur, added = anchor, 0
    for d, cap in picks:
        img = _doc_image_bytes(d)
        if not img:
            continue
        para = doc.add_paragraph()
        para.alignment = _AL.CENTER
        try:
            para.add_run().add_picture(_io.BytesIO(img), width=_In(5.9))
        except Exception as e:  # noqa: BLE001
            log.warning("현장심사 이미지 삽입 실패 %s: %s", d.filename, e)
            continue
        capp = doc.add_paragraph()
        capp.alignment = _AL.CENTER
        capp.add_run("%s — %s" % (cap, d.filename or "")).italic = True
        cur._p.addnext(capp._p)
        cur._p.addnext(para._p)
        cur = capp
        added += 1
    if not added:
        note = doc.add_paragraph()
        note.add_run("제출된 공정도 이미지가 없습니다 · No process diagram submitted").italic = True
        anchor._p.addnext(note._p)


def _fa_fill_products(doc, db, c, prods, lang="ko"):
    """제품표(No / Name / Image) — 제품명·설명과 업체가 올린 제품 사진을 넣는다.

    LPH가 보는 보고서에서 '무엇을 심사했는가'를 보여주는 자리다. 사진 없이 이름만 있으면
    심사 대상이 특정되지 않는다. 없는 것은 지어내지 않고 '미제출'로 남긴다 —
    빈칸으로 두면 누락인지 원래 없는 것인지 구분되지 않는다."""
    import io as _io
    from docx.shared import Inches as _In

    tbl = next((t for t in doc.tables
                if len(t.columns) == 3 and t.rows
                and t.rows[0].cells[0].text.strip().lower() == "no"
                and t.rows[0].cells[2].text.strip().lower().startswith("image")), None)
    if tbl is None or not prods:
        return
    photos = {}
    for d in (db.query(models.DocumentAsset)
              .filter(models.DocumentAsset.case_id == c.case_id,
                      models.DocumentAsset.doc_type == "product_photo",
                      models.DocumentAsset.product_id.isnot(None))
              .order_by(models.DocumentAsset.created_at.asc()).all()):
        photos.setdefault(d.product_id, []).append(d)

    first, last = 1, len(tbl.rows) - 1
    slots = last - first + 1
    if len(prods) > slots:
        _sjph_clone_rows(tbl, first, last, len(prods) - slots)
    for i, p in enumerate(prods):
        ri = first + i
        if ri >= len(tbl.rows):
            break
        cells = tbl.rows[ri].cells
        desc = (p.description or "").strip()
        name = p.name or ""
        _sjph_docx_cell_set(cells[0], str(i + 1))
        # 템플릿 실측: 이름 아래 줄에 설명이 붙는다("YUMTEA … / Vitamin Ion powder …")
        _sjph_docx_cell_set(cells[1], (name + ("\n" + desc if desc else "")))
        # 첫 장이 깨져 있으면(전송 중 잘린 파일 등) 다음 장을 쓴다 —
        # 사진은 여러 장 올라오는데 한 장 실패로 '미제출'이 되면 낸 것이 없어진 것처럼 보인다.
        placed = False
        for d in photos.get(p.product_id, []):
            img = _doc_image_bytes(d)
            if not img:
                continue
            try:
                _sjph_docx_cell_set(cells[2], "")
                _sjph_cell_stamp(cells[2], img, width_in=1.9)
                placed = True
                break
            except Exception as e:  # noqa: BLE001
                log.warning("제품 사진 삽입 실패 %s (%s): %s", name, d.filename, e)
        if not placed:
            _sjph_docx_cell_set(cells[2], _dd_mod.text("사진 미제출 · No photo", lang))
    if len(prods) < slots:
        _sjph_drop_rows(tbl, first + len(prods), last)


def _factory_audit_docx_bytes(db, c, lang="ko"):
    """현장심사 보고서 — 기준 템플릿(Factory Audit Template.docx)에 실데이터를 병합.

    예전에는 블록을 코드로 그려 PDF를 만들었다. 그래서 오디터가 쓰는 정식 양식(회사정보
    폼·재료표 C/NC·심사원 서명란)과 모양이 달랐다. SJPH 매뉴얼과 같은 방식으로 바꿔,
    양식은 템플릿 원본 그대로 두고 값만 채운다."""
    import io as _io
    import docx as _docx
    doc = _docx.Document(FACTORY_AUDIT_TEMPLATE)
    # 서식 라벨(Company Information 기업정보 …)은 템플릿 소유라 번역하지 않는다.
    # 우리가 채워 넣는 '값'만 언어를 따른다 — LPH 심사원이 읽는 것은 값이다.
    L = _dd_mod.text_fn(lang)
    px = c.profile_ext or {}
    today = date.today().isoformat()

    def pv(x):
        v = ("" if x is None else str(x)).strip()
        return "" if v in ("", "-", "—", "N/A", "n/a") else v

    mats = db.query(models.Material).filter_by(case_id=c.case_id).all()
    finds = db.query(models.AuditFinding).filter_by(case_id=c.case_id).all()
    lph = db.query(models.LphAssignment).filter_by(case_id=c.case_id).first()
    pen = db.query(models.PenyeliaHalal).filter_by(org_id=c.org_id, status="active").first()
    prods = db.query(models.Product).filter_by(case_id=c.case_id).all()
    find_by_mat = {}
    for f in finds:
        if getattr(f, "target", None):
            find_by_mat.setdefault(f.target, []).append(f.finding or "")

    # ── 회사정보 폼 ───────────────────────────────────────────────────
    info = next((t for t in doc.tables
                 if t.rows and t.rows[0].cells[0].text.strip().startswith("Date 날짜")), None)
    if info is not None:
        pathway_app = "Self-Declare" if (c.pathway == "self_declare") else "Regular"
        fac_addr = pv(c.factory_address) or pv(px.get("factory_address"))
        if pv(c.factory_reg_no):
            fac_addr = (fac_addr + " (Reg. No. %s)" % pv(c.factory_reg_no)).strip()
        for prefix, val in [
            ("Date 날짜", today),
            ("Representative Name", pv(c.responsible_person)),
            ("Company Name", pv(c.company_name)),
            ("Business Regisration Number", pv(c.nib)),
            ("Office Phone", pv(c.phone) or pv(px.get("office_phone"))),
            ("Cell Phone", pv(px.get("pic_phone"))),
            ("Email Address", pv(c.email)),
            ("Address 회사 주소", pv(c.address)),
            ("City 도시", pv(px.get("city"))),
            ("Country 국가", pv(px.get("country"))),
            ("ZIP Code", pv(px.get("zip"))),
            ("Factory Address", fac_addr),
            ("Halal Supervisor Name", pv(c.halal_supervisor) or (pen.name if pen else "")),
            ("Halal Supervisor Mobile Phone", pv(px.get("halal_supervisor_phone"))),
            ("Registration Type", pv(px.get("registration_type"))),
            ("Aplication Type", pv(px.get("application_type")) or pathway_app),
            ("Registration Status", pv(px.get("registration_status")) or "New"),
            ("Product Type", pv(px.get("product_type"))),
            ("Product name / Brand name", ", ".join(p.name for p in prods[:6]) or ""),
            ("Product Marketing Type", pv(px.get("marketing_type"))),
            ("Audit technique", L("현장 심사")),
            ("Laboratory Testing", pv(px.get("lab_testing")) or "N/A"),
            ("Auditor Name", (lph.lph_name if lph else "")),
            ("Observer Name", ""),
        ]:
            _sjph_docx_label_fill(info, prefix, val)

    # ── 재료표 (No·Name&Brand·Type·Producer·C/NC·Findings·Note) ───────
    mt = next((t for t in doc.tables
               if len(t.columns) == 7 and t.rows
               and t.rows[0].cells[1].text.strip().startswith("Name & Brand")), None)
    if mt is not None and mats:
        proto, last = 1, len(mt.rows) - 1
        rows = []
        for i, m in enumerate(mats):
            cnc = "C" if m.screen_result in ("PASS", "CLEARED") else "NC"
            note = L("증빙 제출") if m.evidence_provided else ""
            rows.append([str(i + 1), m.name or "",
                         _mat_type_label(m.mat_type, lang) or (m.mat_type or ""),
                         "%s%s" % (m.supplier or "", (" (%s)" % m.origin) if m.origin else ""),
                         cnc, "; ".join(find_by_mat.get(m.name, []))[:120]
                         or (m.screen_status or ""), note])
        _sjph_records_into(mt, proto, last, rows)

    _fa_fill_products(doc, db, c, prods, lang)   # 제품표(No·제품명+설명·사진)
    _fa_insert_images(doc, db, c, lang)          # 제조공정도·시설배치도·현장 사진

    # ── 서명란 — 오디터 / 할랄 감독자 ─────────────────────────────────
    for t in doc.tables:
        if len(t.columns) != 3 or len(t.rows) < 2:
            continue
        head = t.rows[0].cells[0].text.strip()
        role = t.rows[1].cells[1].text.strip()
        if head.startswith("Name of Auditor") or role == "Lead Auditor":
            _sjph_docx_cell_set(t.rows[1].cells[0], (lph.lph_name if lph else "") or "")
        elif head.startswith("Name") and role == "Halal Supervisor":
            _sjph_docx_cell_set(t.rows[1].cells[0],
                                pv(c.halal_supervisor) or (pen.name if pen else ""))

    # 회사명 플레이스홀더가 남아 있으면 채운다(템플릿 머리말 등)
    _sjph_xml_text_replace(doc.element.body,
                           {"[Your company Name]": c.company_name or "",
                            "[Company Name]": c.company_name or "",
                            "[CEO NAME]": pv(c.responsible_person) or "",
                            "[HALAL SUPERVISOR NAME]": pv(c.halal_supervisor) or ""})
    buf = _io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def _sjph_manual_docx_bytes(db, c):
    """기준 템플릿 docx를 열어 실데이터 병합 — 양식(표지·표·부록17·EN/KO 병기) 원본 그대로 유지."""
    import io as _io
    import docx as _docx
    doc = _docx.Document(SJPH_TEMPLATE_DOCX)
    company = c.company_name or ""
    px = c.profile_ext or {}
    today = date.today().isoformat()

    def _pv(x):
        """'—'·빈문자·N/A는 없는 값으로 취급하는 정규화."""
        s = ("" if x is None else str(x)).strip()
        return "" if s in ("", "-", "—", "N/A", "n/a") else s

    ceo = px.get("ceo_name") or c.responsible_person or ""
    sup = c.halal_supervisor or px.get("pic_name") or ""
    repl = {
        "[Your company Name]": company, "[Your Company Name]": company,
        "[Company Name]": company, "[COMPANY NAME]": company,
        "[YOUR COMPANY NAME]": company,
        "[회사명]": company, "[귀사명]": company,
        "[Company Letterhead]": company,
        "[CEO NAME]": ceo or "CEO", "[HALAL SUPERVISOR NAME]": sup or "Halal Supervisor",
        "[PLACE]": px.get("city") or "", "[Place]": px.get("city") or "", "[장소]": px.get("city") or "",
        "[CEO 이름]": ceo or "CEO",
        "[HALAL SUPERVISOR 이름]": sup or "Halal Supervisor",
        "[할랄감독관 이름]": sup or "Halal Supervisor",
    }
    # doc.paragraphs는 본문 최상위 문단만 잡는다(408/3229). 텍스트박스 안 문단까지 모두 순회해야
    # 회사명·대표자명 플레이스홀더가 남지 않는다. 단, 표 셀 안은 날짜 치환 제외(빈 기록양식 유지).
    repl_para = dict(repl)
    repl_para["(dd/mm/yyyy)"] = today
    from docx.oxml.ns import qn as _qn
    from docx.text.paragraph import Paragraph as _Para

    def _in_table_cell(el):
        par = el.getparent()
        while par is not None:
            if par.tag == _qn("w:tc"):
                return True
            par = par.getparent()
        return False

    for _p in list(doc.element.body.iter(_qn("w:p"))):
        p = _Para(_p, doc)
        _sjph_docx_para_replace(p, repl if _in_table_cell(_p) else repl_para)
        _norm = p.text.strip().replace("\t", "").replace(" ", "")
        if _norm in ("Date:", "Date/날짜:"):
            p.add_run(" " + today)
        elif sup and not _in_table_cell(_p):
            # 1장 B절 할랄 감독관 임명 — 본문 직계의 빈 'Name :' / 'Position :' 두 줄 채움.
            # 표 안에도 'Name' 셀이 여럿 있어 표 바깥 조건이 반드시 필요하다.
            if _norm in ("Name:", "성명:"):
                p.add_run(" " + sup)
            elif _norm in ("Position:", "직책:"):
                p.add_run(" Halal Supervisor / 할랄 감독관")
    _sjph_xml_text_replace(doc.element.body, repl)   # sdt 등 Paragraph가 못 보는 영역 보강
    # 고객 정보 폼(표지 뒤 16x6 표) — 라벨 셀에 실데이터 병합
    info_tbl = next((t for t in doc.tables
                     if t.rows and t.rows[0].cells[0].text.strip().startswith("Date 날짜")), None)
    if info_tbl is not None:
        pathway_app = "Self-Declare" if (c.pathway == "self_declare") else "Regular"
        # 회사명·대표자명 영문 병기: "한글(영문)" — 영문 없거나 한글과 같으면 한글만
        _cn_en = _pv(px.get("company_name_en"))
        client_org = ("%s (%s)" % (company, _cn_en)) if (_cn_en and _cn_en != company) else company
        _rp = _pv(c.responsible_person)
        _rp_en = _pv(px.get("responsible_person_en"))
        client_name = ("%s (%s)" % (_rp, _rp_en)) if (_rp and _rp_en and _rp_en != _rp) else (_rp or _rp_en)
        # 본사주소 + 공장주소·공장등록번호 병기 (템플릿에 공장 칸이 없어 Address 칸에 함께 적음)
        _addr_parts = []
        if _pv(c.address):
            _addr_parts.append(_pv(c.address))
        if _pv(c.factory_address):
            _fa = "Factory 공장: %s" % _pv(c.factory_address)
            if _pv(c.factory_reg_no):
                _fa += " (Reg. No. %s)" % _pv(c.factory_reg_no)
            _addr_parts.append(_fa)
        addr_merged = "\n".join(_addr_parts)
        tax_id_val = _pv(px.get("tax_id")) or _pv(c.nib)          # 사업자번호 폴백
        office_phone_val = _pv(c.phone) or _pv(px.get("office_phone"))
        for prefix, val in [
            ("Date 날짜", today), ("Client Name 고객 이름", client_name),
            ("Client Organization / Company Name", client_org),
            ("Office Phone", office_phone_val), ("Email Address 메일 주소", c.email),
            ("Address 회사 주소", addr_merged), ("City 도시", px.get("city")),
            ("Country 국가", px.get("country")), ("ZIP Code", px.get("zip")),
            ("Occupation/Business Type", px.get("business_type")),
            ("Person In Charge (PIC) Name", px.get("pic_name")),
            ("PIC Title", px.get("pic_title")), ("PIC MobilePhone", px.get("pic_phone")),
            ("PIC Email Address", px.get("pic_email")),
            ("Contact Person (CP) Name", px.get("cp_name")), ("CP Title", px.get("cp_title")),
            ("CP Mobile Phone", px.get("cp_phone")), ("CP Email Address", px.get("cp_email")),
            ("Registration Type 등록유형", px.get("registration_type")),
            ("Aplication Type 신청유형", px.get("application_type") or pathway_app),
            ("Registration Status 등록현황", px.get("registration_status") or "New"),
            ("Product Type 제품유형", px.get("product_type")),
            ("Total Employee 총 직원 수", px.get("total_employee")),
            ("Product Marketing Type", px.get("marketing_type")),
            ("ID TAX Company", tax_id_val),
            ("Production Capacity 생산능력", px.get("production_capacity")),
        ]:
            _sjph_docx_label_fill(info_tbl, prefix, val)
    # 할랄 관리팀 표(본문 1장·부록2 동일 양식) — 이름 셀 치환
    for t in doc.tables:
        for row in t.rows:
            for cell in row.cells:
                s = cell.text.strip()
                if s == "CEO name" and ceo:
                    _sjph_docx_cell_set(cell, ceo)
                elif s.startswith("HALAL SUPERVISOR name") and sup:
                    _sjph_docx_cell_set(cell, sup)
    # Appendix 8 진술서 표(4행 3열: 라벨 / : / 값) — 법인등록번호·회사명·성명 채움
    _brn = _pv(px.get("corporate_reg_no")) or _pv(c.nib)
    for t in doc.tables:
        if len(t.columns) != 3 or len(t.rows) < 3:
            continue
        if not t.rows[0].cells[0].text.strip().startswith("Name"):
            continue
        for row in t.rows:
            lbl = row.cells[0].text.strip()
            if lbl.startswith("Business Registration") and _brn:
                _sjph_docx_cell_set(row.cells[2], _brn)
            elif lbl.startswith("Company") and company:
                _sjph_docx_cell_set(row.cells[2], company)
            elif lbl.startswith("Name") and ceo:
                _sjph_docx_cell_set(row.cells[2], ceo)
    # Appendix 4 사용재료 목록표 — 원재료 실데이터 행 채움 (헤더 2행 아래부터)
    mats = db.query(models.Material).filter_by(case_id=c.case_id).all()
    ap4 = next((t for t in doc.tables
                if t.rows and "Appendix.4" in t.rows[0].cells[0].text), None)
    if ap4 is not None and mats:
        # 템플릿: 0 제목 / 1 공백 / 2·3 헤더 / 4~6 샘플데이터 / 7·8 안내행 / 9 새행안내 / 10 ※주석(전폭병합)
        AP4_PROTO = 4        # 서식 원형 행
        AP4_SAMPLE_LAST = 6  # 샘플 데이터행 마지막
        AP4_SAMPLE_N = AP4_SAMPLE_LAST - AP4_PROTO + 1   # 3
        n = len(mats)
        # 1) 부족분만 row6 뒤에 복제 삽입 (안내행·주석행은 자동으로 아래로 밀림)
        if n > AP4_SAMPLE_N:
            _sjph_clone_rows(ap4, AP4_PROTO, AP4_SAMPLE_LAST, n - AP4_SAMPLE_N)
        # 2) row4부터 n행 채우기
        for i, m in enumerate(mats):
            ri = AP4_PROTO + i
            if ri >= len(ap4.rows):
                break
            _judg = _sjph_mat_judgment(m)
            _sup = []
            if _judg and _judg != "—":
                _sup.append(_judg)
            if m.evidence_provided:
                _sup.append("제출 Submitted")
            # 컬럼 순서: No·재료명(KOR)·재료명(ENG)·유형·제조사·원산지·공급자·할랄인증·인증번호·유효기간·증빙
            vals = [str(i + 1), m.name or "", m.name or "", m.mat_type or "",
                    m.supplier or "", m.origin or "", m.supplier or "",
                    ("Y" if m.cert == "certified" else "N"), m.cert_no or "", "",
                    "\n".join(_sup)]
            _sjph_fill_row(ap4.rows[ri].cells, vals)
        # 3) 재료가 3개 미만이면 남는 샘플행 삭제
        if n < AP4_SAMPLE_N:
            _sjph_drop_rows(ap4, AP4_PROTO + n, AP4_SAMPLE_LAST)
    # Appendix 5 원재료×제품 매트릭스 — 제품명 헤더 치환 + 사용여부 ✔
    # 템플릿 매트릭스는 제품 컬럼이 6개 고정 → 6개 초과 제품은 매트릭스 표를 블록으로 반복 생성(전체 제품 수용).
    prods = db.query(models.Product).filter_by(case_id=c.case_id).all()
    links = db.query(models.ProductMaterial).filter_by(case_id=c.case_id).all()
    used = {(l.product_id, l.material_id) for l in links}
    ap5 = next((t for t in doc.tables
                if t.rows and "Appendix 5" in t.rows[0].cells[0].text), None)
    if ap5 is not None and mats and prods:
        import copy as _copy
        from docx.table import Table as _DocxTable
        # 템플릿 구조: 제품명 헤더=row3·4(병합, product name A~E=col 3~7, 5개), 원재료 데이터=row5부터.
        HDR_RIS, DATA_START, PCOLS, PC0 = (3, 4), 5, 5, 3
        groups = [prods[i:i + PCOLS] for i in range(0, len(prods), PCOLS)][:30]  # 안전상한 30블록

        def _fill_matrix(tbl, group):
            for hri in HDR_RIS:
                if hri < len(tbl.rows):
                    hcells = tbl.rows[hri].cells
                    for pi, pr in enumerate(group):
                        ci = PC0 + pi
                        if ci <= PC0 + PCOLS - 1 and ci < len(hcells):
                            _sjph_docx_cell_set(hcells[ci], pr.name or "")
            # 템플릿: row5~13 샘플데이터 9행 / row14 안내행 / row15 ※주석행
            n = len(mats)
            if n > 9:
                _sjph_clone_rows(tbl, DATA_START, 13, n - 9)
            for i, m in enumerate(mats):
                ri = DATA_START + i
                if ri >= len(tbl.rows):
                    break
                cells = tbl.rows[ri].cells
                _sjph_docx_cell_set(cells[0], str(i + 1))
                if len(cells) > 1:
                    _sjph_docx_cell_set(cells[1], m.name or "")
                if len(cells) > 2:
                    _sjph_docx_cell_set(cells[2], m.name or "")
                for pi, pr in enumerate(group):
                    ci = PC0 + pi
                    if ci <= PC0 + PCOLS - 1 and ci < len(cells):
                        _sjph_docx_cell_set(cells[ci], "V" if (pr.product_id, m.material_id) in used else "-")
            if n < 9:
                _sjph_drop_rows(tbl, DATA_START + n, 13)

        blueprint = _copy.deepcopy(ap5._tbl)      # 빈 원본 구조 보존(추가 블록 복제용)
        _fill_matrix(ap5, groups[0])
        last = ap5
        for g in groups[1:]:
            clone = _copy.deepcopy(blueprint)
            last._tbl.addnext(clone)
            tobj = _DocxTable(clone, ap5._parent)
            _fill_matrix(tobj, g)
            last = tobj
    _sjph_fill_record_tables(doc, mats, prods, links, c,
                             _sjph_photo_records(db, c.case_id))   # 부록 6·7·10·12·13
    _sjph_check_hpas_yes(doc)                              # HPAS 기준 설문 — 전 항목 '예'
    # 빌더 드롭 이미지(조직도·공정도·서명) 임베드
    _sjph_insert_layout_images(doc, db, c.case_id)
    _sjph_insert_stamps(doc, db, c.case_id)      # 승인자 이름·직책·도장
    buf = _io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def _content_disposition(filename):
    """한글 파일명 안전 다운로드 헤더 — RFC 5987 filename* + ASCII fallback 병기."""
    import re as _re
    from urllib.parse import quote as _quote
    ascii_name = _re.sub(r"[^A-Za-z0-9._-]+", "_", filename).strip("_") or "download"
    return "attachment; filename=\"%s\"; filename*=UTF-8''%s" % (ascii_name, _quote(filename, safe=""))


def _sjph_manual_filename(c, ext):
    """SJPH_Manual_{회사명}_{YYYY-MM-DD}.{ext}"""
    import re as _re
    name = c.company_name or c.case_id[:8]
    name = _re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", name).strip() or c.case_id[:8]
    return "SJPH_Manual_%s_%s.%s" % (name[:60], date.today().isoformat(), ext)


def _docx_to_pdf_bytes(docx_bytes):
    """LibreOffice headless 변환 — 호출별 고유 프로필로 동시실행 락 회피."""
    import shutil as _sh
    import subprocess as _sp
    import tempfile as _tf
    so = _sh.which("soffice") or "/Applications/LibreOffice.app/Contents/MacOS/soffice"
    if not os.path.exists(so):
        raise RuntimeError("soffice(LibreOffice) not found")
    with _tf.TemporaryDirectory() as td:
        src = os.path.join(td, "manual.docx")
        with open(src, "wb") as f:
            f.write(docx_bytes)
        _sp.run([so, "--headless", "--norestore",
                 "-env:UserInstallation=file://%s/lo" % td,
                 "--convert-to", "pdf", "--outdir", td, src],
                check=True, timeout=180, capture_output=True)
        with open(os.path.join(td, "manual.pdf"), "rb") as f:
            return f.read()


@app.get("/cases/{case_id}/sjph-manual.docx")
def get_sjph_manual_docx(case_id, user=Depends(auth.get_current_user), db=Depends(get_db)):
    """SJPH Manual — 기준 템플릿 원본 양식 그대로 실데이터 병합한 DOCX."""
    from fastapi.responses import Response
    c = _get_case(db, case_id, user)
    data = _sjph_manual_docx_bytes(db, c)
    return Response(content=data,
                    media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    headers={"Content-Disposition": _content_disposition(_sjph_manual_filename(c, "docx"))})


@app.get("/cases/{case_id}/sjph-manual.pdf")
def get_sjph_manual_pdf(case_id, user=Depends(auth.get_current_user), db=Depends(get_db)):
    """SJPH/HPAS Manual PDF — 기준 docx 템플릿에 실데이터 병합 후 LibreOffice 변환(양식 1:1).
    변환 불가 환경에서만 기존 리치 렌더러로 폴백."""
    from fastapi.responses import Response
    c = _get_case(db, case_id, user)
    try:
        pdf = _docx_to_pdf_bytes(_sjph_manual_docx_bytes(db, c))
    except Exception as e:
        log.warning("sjph docx->pdf 변환 실패, 리치 렌더러 폴백: %s", e)
        blocks = _sjph_manual_blocks(db, c)
        pdf = _render_pdf_rich("Halal Product Assurance System (HPAS) Manual", blocks,
                               subtitle=(c.company_name or ""),
                               footer="GL-HAC AI · SJPH/HPAS Manual " + case_id[:8])
    return Response(content=pdf, media_type="application/pdf",
                    headers={"Content-Disposition": _content_disposition(_sjph_manual_filename(c, "pdf"))})


# ===== 공장 다건 분류 (Facility 1:N) — 스키마 무변경(case.facility_ids + profile_ext.label/source_docs) =====
def _fac_dict(f):
    pe = f.profile_ext or {}
    return {"facility_id": f.facility_id, "label": pe.get("label") or f.name or "공장",
            "name": f.name, "address": f.address, "city": f.city, "country": f.country,
            "zip": f.zip, "reg_no": f.reg_no, "profile_ext": pe, "source_docs": pe.get("source_docs") or []}


def _fac_key(reg_no, name, addr):
    import re
    if reg_no and str(reg_no).strip():
        # 등록번호에 발급일자·라벨 등 잡음이 섞여도 병합되도록 가장 긴 숫자열을 정규키로 사용
        runs = re.findall(r"\d+", str(reg_no))
        canon = max(runs, key=len) if runs else re.sub(r"\s+", "", str(reg_no)).lower()
        return "reg:" + canon
    return "na:" + re.sub(r"\s+", "", ((name or "") + (addr or "")).lower())


def _case_facilities(db, c):
    ids = c.facility_ids or []
    return db.query(models.Facility).filter(models.Facility.facility_id.in_(ids)).all() if ids else []


@app.get("/cases/{case_id}/factories")
def list_factories(case_id, user=Depends(auth.get_current_user), db=Depends(get_db)):
    c = _get_case(db, case_id, user)
    return {"factories": [_fac_dict(f) for f in _case_facilities(db, c)]}


@app.post("/cases/{case_id}/factories")
def add_factory(case_id, body: dict = None, user=Depends(auth.get_current_user), db=Depends(get_db)):
    """빈 공장 수동 추가 — 리스트에 공장N 자동 라벨."""
    c = _get_case(db, case_id, user)
    ids = list(c.facility_ids or [])
    label = (body or {}).get("label") or ("공장%d" % (len(ids) + 1))
    f = models.Facility(org_id=c.org_id, name=(body or {}).get("name"),
                        profile_ext={"label": label, "source_docs": []})
    db.add(f)
    db.flush()
    ids.append(f.facility_id)
    c.facility_ids = ids
    db.commit()
    return _fac_dict(f)


@app.patch("/facilities/{facility_id}")
def update_factory(facility_id, body: dict = None, user=Depends(auth.get_current_user), db=Depends(get_db)):
    """공장 필드/라벨 수정 — 리스트 이름(label) 수기 변경 포함."""
    f = db.get(models.Facility, facility_id)
    if not f:
        raise HTTPException(404, {"code": "FACILITY_NOT_FOUND"})
    if user["role"] != "admin" and f.org_id != user["org_id"]:
        raise HTTPException(403, {"code": "ORG_FORBIDDEN"})
    b = body or {}
    for col in ["name", "address", "city", "country", "zip", "reg_no"]:
        if col in b:
            setattr(f, col, b[col])
    pe = dict(f.profile_ext or {})
    for k in ["label", "phone", "email", "pic_name", "pic_title", "cp_name", "cp_title", "manufacturer_name"]:
        if k in b:
            pe[k] = b[k]
    if isinstance(b.get("profile_ext"), dict):
        pe.update(b["profile_ext"])
    f.profile_ext = pe
    db.commit()
    return _fac_dict(f)


@app.delete("/cases/{case_id}/factories/{facility_id}")
def delete_factory(case_id, facility_id, user=Depends(auth.get_current_user), db=Depends(get_db)):
    """케이스에서 공장 연결을 끊는다. 공장 레코드는 아무 케이스도 참조하지 않을 때만 지운다.

    이전에는 연결 해제가 곧 레코드 삭제였다. 같은 공장을 여러 케이스가 참조하는 상황(위탁제조·
    데모 시드)에서 한 케이스에서 빼면 다른 케이스의 공장까지 사라졌다(실제로 발생시킴).
    '잘못 붙은 공장을 뗀다'와 '공장을 없앤다'는 다른 행위다."""
    c = _get_case(db, case_id, user)
    ids = [x for x in (c.facility_ids or []) if x != facility_id]
    c.facility_ids = ids
    f = db.get(models.Facility, facility_id)
    still_used = [x for x in db.query(models.CaseApplication)
                  .filter(models.CaseApplication.case_id != case_id).all()
                  if facility_id in (x.facility_ids or [])]
    removed = False
    if f and not still_used and (user["role"] == "admin" or f.org_id == c.org_id):
        db.delete(f)
        removed = True
    _audit(db, user, "case.facility.unlink", "facility", facility_id, case_id,
           {"facility_name": f.name if f else None, "record_deleted": removed,
            "still_used_by": len(still_used)}, commit=False)
    db.commit()
    return {"unlinked": facility_id, "remaining": ids, "record_deleted": removed,
            "still_used_by_cases": len(still_used)}


@app.post("/cases/{case_id}/factories/classify")
def classify_factories(case_id, user=Depends(auth.get_current_user), db=Depends(get_db)):
    """공장등록증(factory_registration) 문서를 등록번호 우선 dedup으로 분류 → Facility 생성/갱신.
    문서·공장 없으면 빈 공장1 자동 생성."""
    c = _get_case(db, case_id, user)
    _audit(db, user, "factory.classify", "case", case_id, case_id)
    docs = db.query(models.DocumentAsset).filter_by(case_id=case_id, doc_type="factory_registration").all()
    groups = {}
    for d in docs:
        fld = d.fields or {}
        reg = fld.get("factory_reg_no")
        addr = fld.get("factory_address") or fld.get("address")
        nm = fld.get("manufacturer") or fld.get("company_name")
        g = groups.setdefault(_fac_key(reg, nm, addr), {"reg": reg, "addr": addr, "name": nm, "docs": []})
        g["docs"].append(d.document_id)
    existing = _case_facilities(db, c)
    by_key = {_fac_key(f.reg_no, f.name, f.address): f for f in existing}
    ids = list(c.facility_ids or [])
    n = len(existing)
    for key, g in groups.items():
        f = by_key.get(key)
        if not f:
            n += 1
            f = models.Facility(org_id=c.org_id, name=g["name"], reg_no=g["reg"], address=g["addr"],
                                profile_ext={"label": "공장%d" % n, "source_docs": []})
            db.add(f)
            db.flush()
            ids.append(f.facility_id)
        else:
            if g["name"] and not f.name:
                f.name = g["name"]
            if g["reg"] and not f.reg_no:
                f.reg_no = g["reg"]
            if g["addr"] and not f.address:
                f.address = g["addr"]
        pe = dict(f.profile_ext or {})
        pe["source_docs"] = sorted(set((pe.get("source_docs") or []) + g["docs"]))
        if not pe.get("label"):
            pe["label"] = "공장%d" % n
        f.profile_ext = pe
    if not groups and not existing:
        f = models.Facility(org_id=c.org_id, profile_ext={"label": "공장1", "source_docs": []})
        db.add(f)
        db.flush()
        ids.append(f.facility_id)
    c.facility_ids = list(dict.fromkeys(ids))
    db.commit()
    return {"classified": len(groups), "factories": [_fac_dict(f) for f in _case_facilities(db, c)]}


# ===== M1: 클라이언트 11단계 여정(journey) 진행 — case.status 마일스톤 + 레코드 도출 =====
_MILESTONE = {}
for _mi_index, _statuses in enumerate([
    ["onboarding", "application_draft", "ai_pre_assessment_ready", "ai_pre_assessment_running", "pathway_determination"],
    ["self_declare_eligible", "sjph_lite_prepared", "pendamping_verification", "self_declaration_submitted", "supplementation_required", "supplementation_submitted", "consultant_review", "document_pre_audit_requested", "document_pre_audit_in_review", "document_pre_audit_approved", "lph_assignment"],
    ["onsite_audit_scheduled", "onsite_audit_in_progress", "corrective_action_required", "corrective_action_submitted", "audit_closed", "hpas_evaluation_ready", "final_package_preparation", "committee_verification"],
    ["fatwa_review", "fatwa_approved"],
    ["certificate_issued", "post_certification_monitoring", "change_impact", "renewal_preparation"],
]):
    for _s in _statuses:
        _MILESTONE[_s] = _mi_index


def _mi(status):
    return _MILESTONE.get(status, 0)


@app.get("/cases/{case_id}/journey")
def get_case_journey(case_id: str, user=Depends(auth.get_current_user), db=Depends(get_db)):
    """클라이언트 전 단계 여정(11) — 회원가입→신청→사전검토→계약→입금→할랄매뉴얼→모의→현장→오디터리포트→파트와→인증서."""
    c = _get_case(db, case_id, user)
    st = c.status or "onboarding"
    mi = _mi(st)
    contract = db.query(models.Contract).filter_by(case_id=case_id).first()
    paid = db.query(models.Invoice).filter_by(case_id=case_id).filter(models.Invoice.status == "paid").first()
    sjph = db.query(models.GeneratedDocument).filter_by(case_id=case_id, doc_type="sjph_manual").first()
    areport = db.query(models.GeneratedDocument).filter_by(case_id=case_id, doc_type="audit_report").first()
    fatwa = db.query(models.FatwaDecision).filter_by(case_id=case_id).first()
    cert = db.query(models.HalalCertificate).filter_by(case_id=case_id).first()
    stages = [
        ("signup", "회원가입", True),
        ("application", "신청", st not in ("onboarding", "application_draft")),
        ("preassess", "사전검토", (st == "document_pre_audit_approved" or mi >= 2)),
        ("contract", "계약", bool(contract and contract.status in ("issued", "signed"))),
        ("payment", "입금확인", bool(paid)),
        ("halal_manual", "할랄매뉴얼", bool(sjph)),
        ("mock_audit", "모의실사", mi >= 2),
        ("onsite", "현장실사", (st in ("audit_closed", "hpas_evaluation_ready", "final_package_preparation", "committee_verification") or mi >= 3)),
        ("audit_report", "오디터리포트", (bool(areport) or mi >= 3)),
        ("fatwa", "파트와", (bool(fatwa and fatwa.decision in ("approved", "conditional")) or mi >= 4)),
        ("certificate", "인증서", (bool(cert) or st == "certificate_issued")),
    ]
    current_idx = next((i for i, (_, _, done) in enumerate(stages) if not done), len(stages) - 1)
    result_stages = []
    for i, (key, label, done) in enumerate(stages):
        status = "done" if done else ("current" if i == current_idx else "todo")
        result_stages.append({"key": key, "label": label, "status": status})
    return {"case_id": case_id, "status": st, "milestone": mi, "current": current_idx, "stages": result_stages}


# ===== 고객 상담(consultation) 서브시스템 — 관리자 고객대응 (회의 2026-07-09) =====
@app.post("/consultations")
def create_consultation(body: dict = None, user=Depends(auth.get_current_user), db=Depends(get_db)):
    """문의 제출(로그인 사용자) — 관리자가 응대."""
    b = body or {}
    subject = (b.get("subject") or "").strip()
    message = (b.get("message") or "").strip()
    if not message:
        raise HTTPException(400, {"code": "EMPTY_MESSAGE"})
    row = models.Consultation(org_id=user.get("org_id"), case_id=(b.get("case_id") or None),
                              channel=(b.get("channel") or "inapp"), subject=subject or "(제목없음)",
                              message=message, created_by=user["uid"])
    db.add(row)
    _audit(db, user, "consultation.create", "consultation", None)
    db.commit()
    return {"id": row.id, "status": row.status, "subject": row.subject}


@app.get("/consultations")
def list_consultations(status: str = None, user=Depends(auth.get_current_user), db=Depends(get_db)):
    """상담 목록 — 관리자/운영자는 전체, 그 외는 본인 조직만."""
    q = db.query(models.Consultation)
    if user["role"] not in ("admin", "operator"):
        q = q.filter(models.Consultation.org_id == user.get("org_id"))
    if status:
        q = q.filter(models.Consultation.status == status)
    rows = q.order_by(models.Consultation.created_at.desc()).all()
    return [{"id": r.id, "org_id": r.org_id, "case_id": r.case_id, "channel": r.channel,
             "subject": r.subject, "message": r.message, "status": r.status, "response": r.response,
             "responder": r.responder, "created_at": str(r.created_at)[:19],
             "answered_at": str(r.answered_at)[:19] if r.answered_at else None} for r in rows]


@app.post("/consultations/{cid}/respond")
def respond_consultation(cid: str, body: dict = None,
                         user=Depends(auth.require_roles("admin", "operator")), db=Depends(get_db)):
    """관리자/운영자 응대 — 상태 answered."""
    row = db.get(models.Consultation, cid)
    if not row:
        raise HTTPException(404, {"code": "CONSULTATION_NOT_FOUND"})
    resp = ((body or {}).get("response") or "").strip()
    if not resp:
        raise HTTPException(400, {"code": "EMPTY_RESPONSE"})
    row.response = resp
    row.responder = user["uid"]
    row.status = "answered"
    row.answered_at = datetime.utcnow()
    _audit(db, user, "consultation.respond", "consultation", cid)
    db.commit()
    return {"id": row.id, "status": row.status, "response": row.response}


@app.patch("/consultations/{cid}")
def patch_consultation(cid: str, body: dict = None,
                       user=Depends(auth.require_roles("admin", "operator")), db=Depends(get_db)):
    """상태 변경(관리자/운영자) — open|answered|closed."""
    row = db.get(models.Consultation, cid)
    if not row:
        raise HTTPException(404, {"code": "CONSULTATION_NOT_FOUND"})
    st = (body or {}).get("status")
    if st not in ("open", "answered", "closed"):
        raise HTTPException(400, {"code": "BAD_STATUS"})
    row.status = st
    _audit(db, user, "consultation.patch", "consultation", cid)
    db.commit()
    return {"id": row.id, "status": row.status}


@app.post("/cases/{case_id}/documents")
def upload_case_document(case_id: str, body: dict = None,
                        user=Depends(auth.get_current_user), db=Depends(get_db)):
    """범용 케이스 문서 업로드(모의심사 자료 등) — 클라이언트도 자기 케이스에 첨부."""
    from .intake import _ctype
    c = _get_case(db, case_id, user)
    b = body or {}
    fn = b.get("filename") or "document"
    b64 = b.get("file_b64")
    if not b64:
        raise HTTPException(400, {"code": "NO_FILE"})
    b64 = _validate_upload(b64, fn)
    doc_type = b.get("doc_type") or "other"
    d = models.DocumentAsset(case_id=case_id, filename=fn, filename_en=_fn_en(fn), doc_type=doc_type,
                             content_b64=b64, content_type=_ctype(fn),
                             file_hash=_sha256_b64(b64))
    # A08 증거 귀속 메타 — 모의/현장 증거(사진·영상)에 누가·언제·어디서·무결성 기록
    if doc_type.startswith("mock_evidence_") or doc_type.startswith("onsite_evidence_"):
        import hashlib as _hl
        try:
            _raw = base64.b64decode(str(b64).split(",")[-1])
        except Exception:  # noqa: BLE001
            _raw = (b64 or "").encode("utf-8", "ignore")
        d.uploaded_by = user["uid"]
        d.uploader_role = user["role"]
        d.file_hash = _hl.sha256(_raw).hexdigest()
        d.captured_at = _exif_datetime(b64)       # EXIF 촬영시각(없으면 None)
        _gps = _exif_gps(b64)                      # EXIF GPS(기존 헬퍼 재사용)
        if _gps:
            d.lat, d.lng, d.geo_source = _gps[0], _gps[1], "exif"
    db.add(d)
    db.flush()
    _audit(db, user, "document.upload", "document", d.document_id)
    # 증빙 항목에 제자리가 있으면 바로 편철 — 올려두고 어디에도 안 걸리는 문서를 없앤다
    filed = _autofile_evidence(db, case_id, d, user)
    db.commit()
    # 저장이 끝난 뒤에 큐에 넣는다 — 커밋 전에 넣으면 워커가 아직 없는 행을 찾는다.
    # 사용자가 doc_type 을 직접 지정해 올린 경우에도 본문 추출(회사명·원재료 등)은
    # 필요하므로 그대로 큐에 넣는다.
    # 사람이 유형을 정해 올렸으면 자동 파싱이 그걸 덮지 않는다 — 무엇을 낸 서류인지는
    # 올린 사람이 안다(모의심사 증거·입금증처럼 본문으로는 알 수 없는 것도 있다).
    # 본문 추출(회사명·원재료)은 그대로 하고, 유형만 보존한다.
    queued = autoparse_enqueue(d.document_id, keep_doc_type=bool(b.get("doc_type")))
    return {"document_id": d.document_id, "filename": fn, "doc_type": d.doc_type,
            "sjph_evidence": filed, "autoparse_queued": queued}


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


_ARABIC_FONT = os.path.join(os.path.dirname(__file__), "assets", "arabic_naskh.ttf")


def _has_arabic(s):
    """아랍어 문자(U+0600~06FF) 포함 여부 — bismillah 등 RTL 렌더 판별."""
    return any("\u0600" <= ch <= "\u06ff" for ch in str(s or ""))


def _render_pdf_rich(title, blocks, subtitle=None, footer=None):
    """리치 문서 렌더러(§7 확장) — heading/para/kv/table/image/signature/static_pdf 블록 지원.
    기존 _render_pdf(텍스트 전용)는 그대로 유지하고, 사진·표·서명·정적PDF가 필요한 문서에 사용."""
    import fitz
    W, H = fitz.paper_size("a4")
    margin, fs, lh = 56, 10.5, 15.5
    font, maxw = "korea", (W - 2 * margin)
    doc = fitz.open()

    def wrap(text, fsize, width):
        words = str(text).split()
        lines, line = [], ""
        for w in words:
            test = line + (" " if line else "") + w
            if fitz.get_text_length(test, fontname=font, fontsize=fsize) > width and line:
                lines.append(line)
                line = w
            else:
                line = test
        if line:
            lines.append(line)
        return lines or [""]

    pages = []
    pg = doc.new_page(width=W, height=H)
    pages.append(pg)
    y = margin

    def need(space):
        nonlocal pg, y
        if y + space > H - margin - 20:
            pg = doc.new_page(width=W, height=H)
            pages.append(pg)
            y = margin

    # Title / subtitle / rule
    pg.insert_text((margin, y + 18), title, fontname=font, fontsize=18)
    y += 30
    if subtitle:
        pg.insert_text((margin, y + 10), subtitle, fontname=font, fontsize=10, color=(0.5, 0.5, 0.5))
        y += 16
    pg.draw_line((margin, y), (W - margin, y), color=(0.7, 0.7, 0.7), width=0.5)
    y += 12

    for block in blocks or []:
        t = block.get("type")
        if t == "heading":
            text = block.get("text", "")
            if not text:
                continue
            fsize, gap = (14, 6) if block.get("level", 1) == 1 else (12, 4)
            need(fsize + gap * 2)
            y += gap
            pg.insert_text((margin, y + fsize), text, fontname=font, fontsize=fsize, color=(0.15, 0.15, 0.15))
            y += fsize + gap
        elif t == "para":
            text = block.get("text", "")
            if not text:
                continue
            if _has_arabic(text) and os.path.exists(_ARABIC_FONT):
                # 아랍어(bismillah 등) — insert_htmlbox로 shaping+RTL(내장 korea 폰트는 아랍어 미지원)
                need(28)
                _css = ("@font-face{font-family:ar;src:url('%s')} "
                        "*{font-family:ar;font-size:%dpx;direction:rtl;text-align:center}"
                        % (_ARABIC_FONT, int(fs + 5)))
                try:
                    pg.insert_htmlbox(fitz.Rect(margin, y, W - margin, y + 30), text, css=_css)
                    y += 28
                    continue
                except Exception:  # noqa: BLE001 — 폰트/버전 문제 시 아래 일반 렌더로 폴백
                    pass
            for line in wrap(text, fs, maxw):
                need(lh)
                pg.insert_text((margin, y + fs), line, fontname=font, fontsize=fs)
                y += lh
            y += 4
        elif t == "kv":
            label, value = block.get("label", ""), block.get("value", "")
            # 라벨이 길면 값과 겹침(내장 CJK 폰트가 라틴도 전각폭 취급) — 실폭 상한(글자수×fs)으로 값 시작점 보정
            label_s = str(label) + ": "
            lw = max(fitz.get_text_length(label_s, fontname=font, fontsize=fs), len(label_s) * fs * 0.95)
            # 상한을 넘길 만큼 라벨이 길면 겹쳐 찍지 말고 값을 다음 줄로 내린다.
            stacked = lw + 6 > maxw * 0.55
            val_x = margin + (16 if stacked else max(140, min(lw + 6, maxw * 0.55)))
            val_w = W - margin - val_x
            if val_w < 20:
                val_w = maxw / 2
            val_lines = wrap(value, fs, val_w)
            nl = max(len(val_lines), 1)
            need((nl + (1 if stacked else 0)) * lh + 4)
            pg.insert_text((margin, y + fs), str(label) + ": ", fontname=font, fontsize=fs, color=(0.45, 0.45, 0.45))
            y0 = y + (lh if stacked else 0)
            for i, vl in enumerate(val_lines):
                pg.insert_text((val_x, y0 + fs + i * lh), vl, fontname=font, fontsize=fs)
            y += (nl + (1 if stacked else 0)) * lh + 4
        elif t == "spacer":
            h = int(block.get("h", 0))
            need(h)
            y += h
        elif t == "pagebreak":
            pg = doc.new_page(width=W, height=H)
            pages.append(pg)
            y = margin
        elif t == "table":
            headers = block.get("headers", [])
            rows = block.get("rows", [])
            if not headers:
                continue
            ncols = len(headers)
            widths = block.get("widths")
            col_w = [w * maxw for w in widths] if (widths and len(widths) == ncols) else [maxw / ncols] * ncols
            t_fs, t_lh = 9, lh - 3

            def row_h(cells):
                mx = 1
                for i, cell in enumerate(cells):
                    cw = max(col_w[i] - 4, 10)
                    mx = max(mx, len(wrap(str(cell), t_fs, cw)))
                return mx * t_lh + 4

            def draw_row(cells, fill=None):
                nonlocal y, pg
                rh = row_h(cells)
                need(rh)
                x0 = margin
                for i, cell in enumerate(cells):
                    x1 = x0 + col_w[i]
                    if fill:
                        pg.draw_rect((x0, y, x1, y + rh), fill=fill, color=(0.7, 0.7, 0.7), width=0.4)
                    else:
                        pg.draw_rect((x0, y, x1, y + rh), color=(0.7, 0.7, 0.7), width=0.3)
                    for j, line in enumerate(wrap(str(cell), t_fs, max(col_w[i] - 4, 10))):
                        pg.insert_text((x0 + 3, y + t_fs + 2 + j * t_lh), line, fontname=font, fontsize=t_fs)
                    x0 = x1
                y += rh

            draw_row(headers, fill=(0.94, 0.95, 0.98))
            for r in rows:
                draw_row(list(r))
            y += 6
        elif t == "image":
            data = block.get("data")
            if not data:
                continue
            img_w = block.get("width") or min(maxw, 260)
            try:
                pix = fitz.Pixmap(data)
                img_h = int(pix.height * img_w / pix.width) if pix.width else 120
                need(img_h + 16)
                pg.insert_image(fitz.Rect(margin, y, margin + img_w, y + img_h), stream=data)
                y += img_h + 2
                cap = block.get("caption")
                if cap:
                    pg.insert_text((margin, y + 8), cap, fontname=font, fontsize=8, color=(0.5, 0.5, 0.5))
                    y += 14
                else:
                    y += 4
            except Exception:
                need(lh)
                pg.insert_text((margin, y + fs), "[이미지 로드 실패]", fontname=font, fontsize=fs, color=(0.5, 0.5, 0.5))
                y += lh + 4
        elif t == "signature":
            slots = block.get("slots", [])
            if not slots:
                continue
            per_row = min(3, len(slots))
            slot_w = (maxw - (per_row - 1) * 12) / per_row
            row_h_sig = 62
            for idx, slot in enumerate(slots):
                col = idx % per_row
                if col == 0:
                    need(row_h_sig)
                    if idx > 0:
                        y += row_h_sig
                x = margin + col * (slot_w + 12)
                # 긴 role은 " · " 기준 줄분리 — 내장 CJK 폰트가 라틴도 전각폭이라 한 줄로 찍으면 옆 슬롯과 겹침(SJPH 표지 Audited/Reviewed by)
                role_lines = [p for p in str(slot.get("role", "")).split(" · ") if p][:2]
                for k, rl in enumerate(role_lines):
                    pg.insert_text((x, y + 9 + k * 11), rl, fontname=font, fontsize=9, color=(0.3, 0.3, 0.3))
                pg.insert_text((x, y + 38), str(slot.get("name", "")), fontname=font, fontsize=9)
                pg.draw_line((x, y + 42), (x + slot_w - 8, y + 42), color=(0.5, 0.5, 0.5), width=0.5)
                if slot.get("signed"):
                    pg.insert_text((x, y + 54), "✔ 서명완료", fontname=font, fontsize=8, color=(0, 0.5, 0))
                else:
                    pg.insert_text((x, y + 54), "(미서명)", fontname=font, fontsize=8, color=(0.5, 0.5, 0.5))
            y += row_h_sig + 6
        elif t == "static_pdf":
            data = block.get("data")
            if not data:
                continue
            try:
                src = fitz.open(stream=data, filetype="pdf")
                doc.insert_pdf(src)
                src.close()
                pg = doc.new_page(width=W, height=H)
                pages.append(pg)
                y = margin
            except Exception:
                need(lh)
                pg.insert_text((margin, y + fs), "[정적 PDF 삽입 실패]", fontname=font, fontsize=fs, color=(0.5, 0.5, 0.5))
                y += lh + 4

    # doc.insert_pdf()가 기존 Page 참조를 무효화하므로 stale한 pages 리스트 대신 현재 문서 페이지를 순회
    if footer:
        for p in doc:
            p.insert_text((margin, H - margin + 4), footer, fontname=font, fontsize=8, color=(0.5, 0.5, 0.5))
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
    # 계약서 gen-doc은 텍스트 요약이 아니라 실제 10p FORM 4.1 원본 양식으로 서빙(로고 포함)
    if g.doc_type == "contract":
        ct = db.query(models.Contract).filter_by(case_id=g.case_id).first()
        if ct:
            _prods = (db.query(models.Product).filter(models.Product.product_id.in_(ct.product_ids)).all()
                      if ct.product_ids else db.query(models.Product).filter_by(case_id=g.case_id).all())
            try:
                _pdf = _contract_overlay_pdf(db, c, ct, _prods)
            except Exception as _e:  # noqa: BLE001
                log.warning("gen-doc contract 오버레이 실패, 폴백: %s", _e)
                _pdf = _contract_rich_pdf(db, c, ct, _prods)
            _fn = "contract_%s.pdf" % (ct.contract_no or g.case_id[:8])
            return Response(content=_pdf, media_type="application/pdf",
                            headers={"Content-Disposition": "attachment; filename*=UTF-8''" + quote(_fn)})
    labels = {"sjph_manual": "SJPH Manual", "audit_report": "현장심사 보고서 · Audit Report",
              "company_info": "기업정보 · Company Info (Form.1)",
              "facility_info": "시설정보 · Facility Info (Form.2)",
              "contract": "계약서 · Contract (FORM 4.1)",
              "fatwa_decree": "할랄 판결문 · Fatwa Decision",
              "material_report": "성분 분석 리포트 스냅샷 · Material Report"}
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


# ── P0-4: 현장 심사보고서 재심 루프 + 오디터 E-서명 + 파트와 전달 게이트 ──
# 스키마 무변경 — 보고서 문서는 기존 GeneratedDocument(audit_report) 재사용,
# 루프·서명·게이트 상태는 WorkflowEvent(latest-wins)로 저장. P0-2~3과 동일 패턴.
def _latest_audit_report(db, case_id):
    """최신 audit_report GeneratedDocument(버전 내림차순 1건). 없으면 None."""
    return (db.query(models.GeneratedDocument)
            .filter_by(case_id=case_id, doc_type="audit_report")
            .order_by(models.GeneratedDocument.version.desc()).first())


def _audit_report_event_latest(db, case_id, action):
    """action별 최신 이벤트 payload(+메타). 없으면 None. latest-wins."""
    e = (db.query(models.WorkflowEvent)
         .filter(models.WorkflowEvent.case_id == case_id,
                 models.WorkflowEvent.action == action)
         .order_by(models.WorkflowEvent.created_at.desc()).first())
    if not e:
        return None
    p = dict(e.payload or {})
    p["at"] = e.created_at.isoformat() if e.created_at else None
    p["actor"] = e.actor_id
    return p


def _audit_report_return_count(db, case_id):
    """이 케이스의 audit_report.return 이벤트 수(재심 회차 산정용)."""
    return (db.query(models.WorkflowEvent)
            .filter(models.WorkflowEvent.case_id == case_id,
                    models.WorkflowEvent.action == "audit_report.return").count())


def _fatwa_gate_check(db, c):
    """파트와 상정 선행조건 검증 — (미충족 코드 리스트, 근거 dict) 반환.
    (a) audit_report GeneratedDocument approved 존재
    (b) audit_report.sign 이벤트 존재(오디터 서명)
    (c) SJPH/HPAS complete(HPAS 5요소 ok + PenyeliaHalal active) — get_sjph 로직 재사용."""
    missing = []
    g = _latest_audit_report(db, c.case_id)
    report_approved = bool(g and g.status == "approved")
    if not report_approved:
        missing.append("REPORT_NOT_APPROVED")
    sign = _audit_report_event_latest(db, c.case_id, "audit_report.sign")
    if not sign:
        missing.append("AUDITOR_SIGN_REQUIRED")
    have = _ensure_hpas(db, c.case_id)
    hpas_ok = sum(1 for el in HPAS_ELEMENTS if have[el].status == "ok")
    penyelia_ok = db.query(models.PenyeliaHalal).filter_by(org_id=c.org_id, status="active").count() > 0
    sjph_complete = (hpas_ok == len(HPAS_ELEMENTS)) and penyelia_ok
    if not sjph_complete:
        missing.append("SJPH_INCOMPLETE")
    ctx = {"report_approved": report_approved, "signed": sign is not None,
           "sjph_complete": sjph_complete, "hpas_ok": hpas_ok,
           "hpas_total": len(HPAS_ELEMENTS), "penyelia_ok": penyelia_ok,
           "gen_doc_id": (g.gen_doc_id if g else None),
           "version": (g.version if g else None)}
    return missing, ctx


@app.post("/cases/{case_id}/audit-report/return")
def audit_report_return(case_id: str, body: schemas.AuditReportReturnReq,
                        user=Depends(auth.require_roles("auditor", "operator")),
                        db: Session = Depends(get_db)):
    """① 재심 루프 — 오디터가 보고서 보완 반려. comment 필수. round=기존 return 수+1. 클라이언트 알림."""
    c = _get_case(db, case_id, user)
    comment = (body.comment or "").strip()
    if not comment:
        raise HTTPException(422, {"code": "COMMENT_REQUIRED"})
    rnd = _audit_report_return_count(db, case_id) + 1
    sm.record_event(db, c, c.status, c.status, "audit_report.return", user["role"], user["uid"],
                    {"comment": comment, "round": rnd})
    _notify(db, c, "audit_report.return", "심사보고서 보완 요청", body=comment, role="applicant")
    db.commit()
    return {"ok": True, "round": rnd}


@app.post("/cases/{case_id}/audit-report/resubmit")
def audit_report_resubmit(case_id: str, body: schemas.AuditReportResubmitReq,
                          user=Depends(auth.require_roles("applicant", "consultant")),
                          db: Session = Depends(get_db)):
    """① 재심 루프 — 클라이언트가 수정 재수신. round=현재 return 회차. 오디터 알림."""
    c = _get_case(db, case_id, user)
    rnd = _audit_report_return_count(db, case_id)
    note = (body.note or "").strip()
    sm.record_event(db, c, c.status, c.status, "audit_report.resubmit", user["role"], user["uid"],
                    {"round": rnd, "note": note})
    _notify(db, c, "audit_report.resubmit", "심사보고서 재제출", body=note, role="auditor")
    db.commit()
    return {"ok": True, "round": rnd}


@app.post("/cases/{case_id}/audit-report/reconfirm")
def audit_report_reconfirm(case_id: str, body: schemas.AuditReportReconfirmReq,
                           user=Depends(auth.require_roles("auditor", "operator")),
                           db: Session = Depends(get_db)):
    """① 재심 루프 — 오디터가 수정확인(ok|hold). latest-wins."""
    c = _get_case(db, case_id, user)
    decision = (body.decision or "").lower()
    if decision not in ("ok", "hold"):
        raise HTTPException(422, {"code": "INVALID_DECISION", "allowed": ["ok", "hold"]})
    note = (body.note or "").strip()
    sm.record_event(db, c, c.status, c.status, "audit_report.reconfirm", user["role"], user["uid"],
                    {"decision": decision, "note": note})
    db.commit()
    return {"ok": True, "decision": decision}


@app.post("/cases/{case_id}/audit-report/sign")
def audit_report_sign(case_id: str, body: schemas.AuditReportSignReq,
                      user=Depends(auth.require_roles("auditor", "operator")),
                      db: Session = Depends(get_db)):
    """② 오디터 E-서명 — approved 보고서에 전자서명(파트와 상정의 선행조건). 미approved면 409."""
    c = _get_case(db, case_id, user)
    g = _latest_audit_report(db, case_id)
    if not g or g.status != "approved":
        raise HTTPException(409, {"code": "REPORT_NOT_APPROVED"})
    name = (body.name or "").strip()
    if not name:
        raise HTTPException(422, {"code": "NAME_REQUIRED"})
    at = datetime.utcnow().isoformat()
    sm.record_event(db, c, c.status, c.status, "audit_report.sign", user["role"], user["uid"],
                    {"name": name, "gen_doc_id": g.gen_doc_id, "at": at})
    # 법적효력 Phase 1 — PSrE 공인 전자서명 훅(미설정 시 no-op). 내부 서명 이벤트는 위에서 그대로 병행.
    if _psre_config()["configured"]:
        import hashlib as _h
        _psre_certify(db, c, "audit_report", name, g.gen_doc_id,
                      _h.sha256(("%s|%s|%s" % (g.gen_doc_id, name, at)).encode()).hexdigest())
    _auto_advance(db, c, "hpas_evaluation_ready", user, "audit_report.sign.auto")   # P2 훅: 보고서 서명→HPAS 평가 준비
    db.commit()
    return {"ok": True, "name": name, "gen_doc_id": g.gen_doc_id, "at": at}


@app.post("/cases/{case_id}/audit-report/send-fatwa")
def audit_report_send_fatwa(case_id: str,
                            user=Depends(auth.require_roles("auditor", "operator")),
                            db: Session = Depends(get_db)):
    """③ 파트와 전달 게이트 — (a)approved 보고서 (b)오디터 서명 (c)SJPH complete 검증.
    미충족 시 409 FATWA_GATE_BLOCKED+missing. 통과 시 상태머신 가드가 허용할 때만 fatwa_review로
    전이(강제 점프 금지 — _on_invoice_paid와 동일 철학). 게이트 통과 사실·이벤트는 항상 기록."""
    c = _get_case(db, case_id, user)
    missing, ctx = _fatwa_gate_check(db, c)
    if missing:
        raise HTTPException(409, {"code": "FATWA_GATE_BLOCKED", "missing": missing})
    target = "fatwa_review"
    transitioned_to = None
    ok, _blk = sm.can_transition(db, c, target)
    if ok:                              # 가드 통과 시에만 전이(아니면 게이트 통과만 기록)
        frm = c.status
        sm.apply_side_effects(c, target)
        c.status = target
        sm.record_event(db, c, frm, target, "audit_report.send_fatwa", user["role"], user["uid"],
                        {"gate": "passed", "gen_doc_id": ctx.get("gen_doc_id")})
        transitioned_to = target
    else:
        sm.record_event(db, c, c.status, c.status, "audit_report.send_fatwa", user["role"], user["uid"],
                        {"gate": "passed", "transition_skipped": True,
                         "gen_doc_id": ctx.get("gen_doc_id")})
    _notify(db, c, "audit_report.send_fatwa", "파트와 상정 — 현장심사 보고서",
            body="현장심사 보고서(서명 완료)가 파트와 심의로 상정되었습니다.", role="fatwa_liaison")
    db.commit()
    return {"ok": True, "transitioned_to": transitioned_to, "gate": "passed"}


@app.get("/cases/{case_id}/audit-report/status")
def audit_report_status(case_id: str, user=Depends(auth.get_current_user),
                        db: Session = Depends(get_db)):
    """통합 조회 — 오디터·클라이언트 공용(org 격리). 프런트가 재심 루프+게이트 사유를 이걸로 렌더."""
    c = _get_case(db, case_id, user)
    g = _latest_audit_report(db, case_id)
    sign = _audit_report_event_latest(db, case_id, "audit_report.sign")
    latest_return = _audit_report_event_latest(db, case_id, "audit_report.return")
    latest_resubmit = _audit_report_event_latest(db, case_id, "audit_report.resubmit")
    latest_reconfirm = _audit_report_event_latest(db, case_id, "audit_report.reconfirm")
    sent = _audit_report_event_latest(db, case_id, "audit_report.send_fatwa")
    missing, ctx = _fatwa_gate_check(db, c)
    return {
        "report": {"approved": bool(g and g.status == "approved"),
                   "gen_doc_id": (g.gen_doc_id if g else None),
                   "version": (g.version if g else None),
                   "status": (g.status if g else None)},
        "signed": ({"name": sign.get("name"), "at": sign.get("at"),
                    "gen_doc_id": sign.get("gen_doc_id")} if sign else None),
        "latest_return": ({"comment": latest_return.get("comment"),
                           "round": latest_return.get("round"),
                           "at": latest_return.get("at")} if latest_return else None),
        "latest_resubmit": ({"round": latest_resubmit.get("round"),
                             "note": latest_resubmit.get("note"),
                             "at": latest_resubmit.get("at")} if latest_resubmit else None),
        "latest_reconfirm": ({"decision": latest_reconfirm.get("decision"),
                              "note": latest_reconfirm.get("note"),
                              "at": latest_reconfirm.get("at")} if latest_reconfirm else None),
        "return_count": _audit_report_return_count(db, case_id),
        "fatwa_gate": {"ready": len(missing) == 0, "missing": missing, **ctx},
        "sent_fatwa": sent is not None,
    }


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
def get_onsite_checklist(case_id: str, lang: str = Query("ko"),
                         user=Depends(auth.get_current_user),
                         db: Session = Depends(get_db)):
    _get_case(db, case_id, user)
    rows = {r.item_key: r for r in db.query(models.OnsiteChecklist).filter_by(case_id=case_id).all()}
    # 현장에서 오디터가 읽는 목록이다 — 인니 심사자에게 한국어로 주면 못 읽는다.
    _oi = _dd_mod.code_labels("ONSITE_ITEM", "onsite_item", lang)
    items = [{"item_key": k, "label": _oi.get(k, ko),
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
    c = _get_case(db, case_id, user)
    _auto_advance(db, c, "onsite_audit_in_progress", user, "onsite.checklist.auto")   # P2 훅: 체크 시작→현장심사 진행
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


# ── 현장감사 전자서명(감사자·할랄감독관) — 스키마 무변경, WorkflowEvent(onsite.sign, latest-wins).
#    Phase 4 audit_report.sign(보고서 서명)과 별개: 이건 현장 체크리스트 서명(캔버스 dataURL). ──
@app.post("/cases/{case_id}/onsite/sign")
def onsite_sign(case_id: str, body: dict = None,
                user=Depends(auth.get_current_user), db: Session = Depends(get_db)):
    b = body or {}
    party = b.get("party")
    if party not in ("auditor", "supervisor"):
        raise HTTPException(422, {"code": "BAD_PARTY", "allowed": ["auditor", "supervisor"]})
    image = b.get("image")
    if not isinstance(image, str) or not image.startswith("data:image/"):
        raise HTTPException(422, {"code": "BAD_IMAGE"})
    if len(image) > 400000:
        raise HTTPException(413, {"code": "IMAGE_TOO_LARGE"})
    name = (b.get("name") or "").strip() or None
    c = _get_case(db, case_id, user)
    sm.record_event(db, c, c.status, c.status, "onsite.sign", user["role"], user["uid"],
                    {"party": party, "image": image, "name": name})
    # 법적효력 Phase 1 — PSrE 공인 전자서명 훅(미설정 시 no-op). 내부 서명 이벤트는 위에서 그대로 병행.
    if _psre_config()["configured"]:
        import hashlib as _h
        _psre_certify(db, c, "onsite", name or party, party, _h.sha256(image.encode()).hexdigest())
    db.commit()
    return {"ok": True, "party": party}


@app.get("/cases/{case_id}/onsite/sign")
def get_onsite_sign(case_id: str, user=Depends(auth.get_current_user),
                    db: Session = Depends(get_db)):
    c = _get_case(db, case_id, user)
    events = (db.query(models.WorkflowEvent)
              .filter(models.WorkflowEvent.case_id == case_id,
                      models.WorkflowEvent.action == "onsite.sign")
              .order_by(models.WorkflowEvent.created_at.asc()).all())
    out = {"auditor": None, "supervisor": None}
    for e in events:
        p = e.payload or {}
        party = p.get("party")
        if party in out:
            out[party] = {"image": p.get("image"), "name": p.get("name"),
                          "at": e.created_at.isoformat() if e.created_at else None}
    return out


# ── 현장심사 종합 평가 의견 서버 영속(E7) — 스키마 무변경, WorkflowEvent(onsite.opinion, latest-wins).
#    기존 브라우저 localStorage 저장을 대체: 타 기기·감사추적·AI보고서(gen_audit_report) 반영. ──
@app.post("/cases/{case_id}/onsite-opinion")
def save_onsite_opinion(case_id: str, body: dict = None,
                        user=Depends(auth.require_roles("auditor", "operator")),
                        db: Session = Depends(get_db)):
    b = body or {}
    opinion = b.get("opinion")
    if opinion is None:
        opinion = ""
    if not isinstance(opinion, str):
        raise HTTPException(422, {"code": "BAD_OPINION"})
    opinion = opinion.strip()
    if len(opinion) > 8000:
        raise HTTPException(413, {"code": "OPINION_TOO_LONG"})
    c = _get_case(db, case_id, user)
    sm.record_event(db, c, c.status, c.status, "onsite.opinion", user["role"], user["uid"],
                    {"opinion": opinion})
    db.commit()
    return {"ok": True, "opinion": opinion}


# ── 심사자(샤리아·최고승인자) 현장심사 의견 — 체크리스트는 수정 불가, 항목별 의견·코멘트만 기록.
#    오디터가 수집한 증거를 보고 판단한다. WorkflowEvent(onsite.reviewer_opinion, latest-wins per item). ──
ONSITE_REVIEWER_ACTION = "onsite.reviewer_opinion"


def _onsite_reviewer_opinions(db, case_id):
    """항목별 최신 심사자 의견 — {item_key: {opinion, comment, actor, role, at}}"""
    out = {}
    for e in (db.query(models.WorkflowEvent)
              .filter(models.WorkflowEvent.case_id == case_id,
                      models.WorkflowEvent.action == ONSITE_REVIEWER_ACTION)
              .order_by(models.WorkflowEvent.created_at.asc(),
                        models.WorkflowEvent.event_id.asc()).all()):
        p = e.payload or {}
        k = p.get("item_key")
        if k:
            out[k] = {"opinion": p.get("opinion"), "comment": p.get("comment", ""),
                      "actor": e.actor_id, "role": e.actor_type,
                      "at": e.created_at.isoformat() if e.created_at else None}
    return out


@app.get("/cases/{case_id}/onsite/reviewer-opinions")
def get_onsite_reviewer_opinions(case_id: str, user=Depends(auth.get_current_user),
                                 db: Session = Depends(get_db)):
    _get_case(db, case_id, user)
    ops = _onsite_reviewer_opinions(db, case_id)
    return {"items": ops, "count": len(ops),
            "negative": sum(1 for v in ops.values() if v.get("opinion") == "nonconformity")}


@app.post("/cases/{case_id}/onsite/reviewer-opinion")
def save_onsite_reviewer_opinion(case_id: str, body: dict = None,
                                 user=Depends(auth.require_roles("fatwa_liaison", "operator")),
                                 db: Session = Depends(get_db)):
    """샤리아·최고승인자 항목별 의견(적합/부적합) + 코멘트. 체크리스트 원본은 건드리지 않는다."""
    b = body or {}
    key = str(b.get("item_key") or "").strip()
    if key not in {k for k, _ in ONSITE_ITEMS}:
        raise HTTPException(422, {"code": "INVALID_ITEM_KEY"})
    op = str(b.get("opinion") or "").strip()
    if op not in ("comply", "nonconformity"):
        raise HTTPException(422, {"code": "BAD_OPINION", "allowed": ["comply", "nonconformity"]})
    comment = str(b.get("comment") or "").strip()[:2000]
    if op == "nonconformity" and len(comment) < 5:
        raise HTTPException(422, {"code": "COMMENT_REQUIRED", "hint": "부적합 의견은 코멘트 5자 이상"})
    c = _get_case(db, case_id, user)
    sm.record_event(db, c, c.status, c.status, ONSITE_REVIEWER_ACTION, user["role"], user["uid"],
                    {"item_key": key, "opinion": op, "comment": comment})
    db.commit()
    return {"ok": True, "item_key": key, "opinion": op}


@app.get("/cases/{case_id}/onsite-opinion")
def get_onsite_opinion(case_id: str, user=Depends(auth.get_current_user),
                       db: Session = Depends(get_db)):
    _get_case(db, case_id, user)
    return {"opinion": _latest_onsite_opinion(db, case_id) or ""}


# ── 파트와 위원 전자서명(위원장·위원) — 스키마 무변경, WorkflowEvent(fatwa.sign, latest-wins per member).
#    onsite.sign 패턴 동일. 기존 fatwa 투표/결정(P0-4)과 별개의 위원 개별 캔버스 서명. ──
@app.post("/cases/{case_id}/fatwa/sign")
def fatwa_sign(case_id: str, body: dict = None,
               user=Depends(auth.require_roles("fatwa_liaison", "operator")), db: Session = Depends(get_db)):
    b = body or {}
    member = (b.get("member") or "").strip()
    if not member:
        raise HTTPException(422, {"code": "BAD_MEMBER"})
    image = b.get("image")
    if not isinstance(image, str) or not image.startswith("data:image/"):
        raise HTTPException(422, {"code": "BAD_IMAGE"})
    if len(image) > 400000:
        raise HTTPException(413, {"code": "IMAGE_TOO_LARGE"})
    name = (b.get("name") or "").strip() or None
    c = _get_case(db, case_id, user)
    sm.record_event(db, c, c.status, c.status, "fatwa.sign", user["role"], user["uid"],
                    {"member": member, "image": image, "name": name})
    # 법적효력 Phase 1 — PSrE 공인 전자서명 훅(미설정 시 no-op). 내부 서명 이벤트는 위에서 그대로 병행.
    if _psre_config()["configured"]:
        import hashlib as _h
        _psre_certify(db, c, "fatwa", name or member, member, _h.sha256(image.encode()).hexdigest())
    db.commit()
    return {"ok": True, "member": member}


# ── F05 파트와 위원 관리 — 스키마 무변경 sentinel(WorkflowEvent, case_id="fatwa-committee:{org_id}",
#    action="fatwa.committee" latest-wins). auditor.profile 패턴 동일. 위원장/간사/위원 명단 관리. ──
FATWA_COMMITTEE_ACTION = "fatwa.committee"
FATWA_MEMBER_ROLES = ("chair", "secretary", "member")


def _committee_shim(org_id):
    import types
    return types.SimpleNamespace(case_id="fatwa-committee:" + (org_id or "org_demo"), org_id=org_id)


def _fatwa_committee(db, org_id):
    ev = (db.query(models.WorkflowEvent)
          .filter_by(case_id="fatwa-committee:" + (org_id or "org_demo"), action=FATWA_COMMITTEE_ACTION)
          .order_by(models.WorkflowEvent.created_at.desc()).first())
    return ((ev.payload or {}).get("members") or []) if ev else []


@app.get("/fatwa/committee")
def get_fatwa_committee(user=Depends(auth.get_current_user), db: Session = Depends(get_db)):
    """F05: 파트와 위원 명단 조회 — 서명 슬롯·결정문 위원 목록의 소스."""
    return {"members": _fatwa_committee(db, user.get("org_id"))}


@app.post("/fatwa/committee")
def set_fatwa_committee(body: dict = None,
                        user=Depends(auth.require_roles("operator", "fatwa_liaison")),
                        db: Session = Depends(get_db)):
    """F05: 위원 명단 저장(전체 교체·latest-wins). 위원장 1인 필수, 총 1~7인."""
    members = (body or {}).get("members") or []
    if not isinstance(members, list) or not (1 <= len(members) <= 7):
        raise HTTPException(422, {"code": "BAD_MEMBERS", "hint": "1~7인 목록 필요"})
    clean = []
    for m in members:
        role = str((m or {}).get("role") or "").strip()
        name = str((m or {}).get("name") or "").strip()
        if role not in FATWA_MEMBER_ROLES or not name:
            raise HTTPException(422, {"code": "BAD_MEMBER_ENTRY", "allowed_roles": list(FATWA_MEMBER_ROLES)})
        clean.append({"role": role, "name": name[:80], "title": str((m or {}).get("title") or "")[:80]})
    if sum(1 for m in clean if m["role"] == "chair") != 1:
        raise HTTPException(422, {"code": "CHAIR_REQUIRED", "hint": "위원장(chair) 정확히 1인"})
    shim = _committee_shim(user.get("org_id"))
    sm.record_event(db, shim, "committee", "committee", FATWA_COMMITTEE_ACTION,
                    user["role"], user["uid"], {"members": clean})
    db.commit()
    return {"members": clean}


@app.get("/cases/{case_id}/fatwa/sign")
def get_fatwa_sign(case_id: str, user=Depends(auth.get_current_user),
                   db: Session = Depends(get_db)):
    c = _get_case(db, case_id, user)
    events = (db.query(models.WorkflowEvent)
              .filter(models.WorkflowEvent.case_id == case_id,
                      models.WorkflowEvent.action == "fatwa.sign")
              .order_by(models.WorkflowEvent.created_at.asc()).all())
    out = {}
    for e in events:
        p = e.payload or {}
        m = p.get("member")
        if m:
            out[m] = {"image": p.get("image"), "name": p.get("name"),
                      "at": e.created_at.isoformat() if e.created_at else None}
    return {"signatures": out}


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
    _notify(db, c, "audit_scheduled", channels=["inapp", "sms"], role="applicant",
            msg=("audit_scheduled.lph", {"company": c.company_name or "",
                                         "lph": body.lph_name}))
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


@app.post("/audit-plans/{plan_id}/propose-dates")
def propose_audit_dates(plan_id: str, body: dict = None,
                        user=Depends(auth.require_roles("auditor", "operator")),
                        db: Session = Depends(get_db)):
    """M5: 현장심사 일정변경 제안 — 클라이언트 제시일 불가 시 오디터가 후보일 2~3개 제안(note 저장·알림)."""
    p = db.get(models.AuditPlan, plan_id)
    if not p:
        raise HTTPException(404, {"code": "PLAN_NOT_FOUND"})
    c = _get_case(db, p.case_id, user)
    dates = [str(d) for d in ((body or {}).get("dates") or []) if str(d).strip()][:3]
    if not dates:
        raise HTTPException(400, {"code": "NO_DATES"})
    p.note = "일정변경 제안: " + ", ".join(dates)
    p.status = "scheduled"
    sm.record_event(db, c, c.status, c.status, "audit.plan.propose", user["role"], user["uid"], {"dates": dates})
    _notify(db, c, "audit_scheduled", "현장심사 일정변경 제안",
            "%s — 현장심사 후보일 제안: %s (택1)" % (c.company_name or "", ", ".join(dates)),
            channels=["inapp", "sms"], role="applicant")
    db.commit()
    return {"id": p.id, "proposed": dates, "note": p.note, "status": p.status}


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


# ---------- A08: 현장심사 일정 캘린더 — 클라이언트 주도 조율(§12-7) ----------
# 스키마 무변경 · WorkflowEvent(onsite_schedule.*) append → latest-wins 파생.
# 기존 audit-plan/propose-dates(오디터→클라 후보일)와 방향/네임스페이스 구분:
#   propose    = 클라이언트가 방문 가능 날짜/기간을 제시 (client → auditor)
#   reschedule = 제시일 불가 시 오디터가 후보일 2~3개 재제안 (auditor → client)
#   confirm    = 오디터가 방문일시 확정
_ONSITE_SCHED_ACTIONS = ("onsite_schedule.propose", "onsite_schedule.reschedule",
                         "onsite_schedule.confirm")


def _onsite_sched_state(db, case_id):
    """onsite_schedule.* 이벤트를 시간순으로 접어 현재 조율 상태 파생(이력 포함)."""
    evs = (db.query(models.WorkflowEvent)
           .filter(models.WorkflowEvent.case_id == case_id,
                   models.WorkflowEvent.action.in_(_ONSITE_SCHED_ACTIONS))
           .order_by(models.WorkflowEvent.created_at.asc(),
                     models.WorkflowEvent.event_id.asc()).all())
    history, client_dates, auditor_dates, confirmed, status = [], [], [], None, "none"
    for e in evs:
        p = e.payload or {}
        kind = e.action.split(".")[-1]
        dates = [str(d) for d in (p.get("dates") or [])]
        if not dates and p.get("date"):
            dates = [str(p.get("date"))]
        history.append({"kind": kind, "actor_role": e.actor_type, "actor_id": e.actor_id,
                        "at": e.created_at.isoformat() if e.created_at else None,
                        "dates": dates, "note": p.get("note", ""),
                        "period": p.get("period", ""), "time": p.get("time", "")})
        if kind == "propose":
            client_dates, auditor_dates, confirmed, status = dates, [], None, "proposed"
        elif kind == "reschedule":
            auditor_dates, confirmed, status = dates, None, "reproposed"
        elif kind == "confirm":
            confirmed = {"date": str(p.get("date") or ""), "time": str(p.get("time") or "")}
            status = "confirmed"
    return {"status": status, "client_dates": client_dates, "auditor_dates": auditor_dates,
            "confirmed": confirmed, "history": history}


@app.get("/cases/{case_id}/onsite-schedule")
def get_onsite_schedule(case_id: str, user=Depends(auth.get_current_user),
                        db: Session = Depends(get_db)):
    _get_case(db, case_id, user)
    return _onsite_sched_state(db, case_id)


@app.post("/cases/{case_id}/onsite-schedule/propose")
def onsite_schedule_propose(case_id: str, body: dict = None,
                            user=Depends(auth.require_roles(
                                "applicant", "consultant", "penyelia_halal", "pendamping_pph")),
                            db: Session = Depends(get_db)):
    """클라이언트가 방문 가능 날짜(캘린더 선택)·기간을 제시."""
    c = _get_case(db, case_id, user)
    body = body or {}
    dates = [str(d) for d in (body.get("dates") or []) if str(d).strip()][:31]
    if not dates:
        raise HTTPException(400, {"code": "NO_DATES"})
    payload = {"dates": dates, "period": str(body.get("period") or ""),
               "note": str(body.get("note") or "")}
    sm.record_event(db, c, c.status, c.status, "onsite_schedule.propose",
                    user["role"], user["uid"], payload)
    _notify(db, c, "audit_scheduled", channels=["inapp"], role="auditor",
            msg=("audit_scheduled.slots", {"company": c.company_name or "",
                                           "count": len(dates)}))
    db.commit()
    return _onsite_sched_state(db, case_id)


@app.post("/cases/{case_id}/onsite-schedule/reschedule")
def onsite_schedule_reschedule(case_id: str, body: dict = None,
                               user=Depends(auth.require_roles("auditor", "operator")),
                               db: Session = Depends(get_db)):
    """제시일 불가 시 오디터가 후보일 2~3개 재제안(왕복)."""
    c = _get_case(db, case_id, user)
    body = body or {}
    dates = [str(d) for d in (body.get("dates") or []) if str(d).strip()][:3]
    if not dates:
        raise HTTPException(400, {"code": "NO_DATES"})
    payload = {"dates": dates, "note": str(body.get("note") or "")}
    sm.record_event(db, c, c.status, c.status, "onsite_schedule.reschedule",
                    user["role"], user["uid"], payload)
    _notify(db, c, "audit_scheduled", "현장심사 후보일 재제안",
            "%s — 오디터가 후보일 %s 중 택1을 제안했습니다." % (c.company_name or "", ", ".join(dates)),
            channels=["inapp"], role="applicant")
    db.commit()
    return _onsite_sched_state(db, case_id)


@app.post("/cases/{case_id}/onsite-schedule/confirm")
def onsite_schedule_confirm(case_id: str, body: dict = None,
                            user=Depends(auth.require_roles("auditor", "operator")),
                            db: Session = Depends(get_db)):
    """오디터가 방문일시를 확정."""
    c = _get_case(db, case_id, user)
    body = body or {}
    date = str((body or {}).get("date") or "").strip()
    if not date:
        raise HTTPException(400, {"code": "NO_DATE"})
    tm = str(body.get("time") or "").strip()
    payload = {"date": date, "time": tm, "note": str(body.get("note") or "")}
    sm.record_event(db, c, c.status, c.status, "onsite_schedule.confirm",
                    user["role"], user["uid"], payload)
    _auto_advance(db, c, "onsite_audit_scheduled", user, "onsite_schedule.confirm.auto")   # P2 훅: 일정 확정→현장심사 예정
    _notify(db, c, "audit_scheduled", "현장심사 일정 확정",
            "%s — 현장심사 방문일이 %s%s 로 확정되었습니다." % (
                c.company_name or "", date, (" " + tm) if tm else ""),
            channels=["inapp", "sms"], role="applicant")
    db.commit()
    return _onsite_sched_state(db, case_id)


@app.post("/cases/{case_id}/onsite-schedule/accept-proposed")
def onsite_schedule_accept_proposed(case_id: str, body: dict = None,
                                    user=Depends(auth.require_roles(
                                        "applicant", "consultant", "penyelia_halal", "pendamping_pph")),
                                    db: Session = Depends(get_db)):
    """오디터가 재제안한 후보일(2~3개) 중 클라이언트가 택1 수락 → 일정 확정(왕복 종료).
    스키마 무변경 — onsite_schedule.confirm 이벤트로 접혀 status=confirmed. 선택일은
    반드시 현재 오디터 재제안 후보일에 포함되어야 함(임의 확정 방지)."""
    c = _get_case(db, case_id, user)
    body = body or {}
    date = str(body.get("date") or "").strip()
    if not date:
        raise HTTPException(400, {"code": "NO_DATE"})
    state = _onsite_sched_state(db, case_id)
    if state.get("status") != "reproposed" or not state.get("auditor_dates"):
        raise HTTPException(409, {"code": "NO_PROPOSED_DATES"})
    if date not in (state.get("auditor_dates") or []):
        raise HTTPException(400, {"code": "DATE_NOT_PROPOSED",
                                  "allowed": state.get("auditor_dates")})
    tm = str(body.get("time") or "").strip()
    payload = {"date": date, "time": tm, "note": str(body.get("note") or ""),
               "via": "client_accept"}
    sm.record_event(db, c, c.status, c.status, "onsite_schedule.confirm",
                    user["role"], user["uid"], payload)
    _notify(db, c, "audit_scheduled", "현장심사 후보일 수락·확정",
            "%s — 클라이언트가 후보일 중 %s%s 를 수락해 현장심사 일정이 확정되었습니다." % (
                c.company_name or "", date, (" " + tm) if tm else ""),
            channels=["inapp"], role="auditor")
    _notify(db, c, "audit_scheduled", "현장심사 후보일 수락·확정",
            "%s — 클라이언트가 후보일 중 %s 를 수락했습니다." % (c.company_name or "", date),
            channels=["inapp"], role="operator")
    db.commit()
    return _onsite_sched_state(db, case_id)


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
    _auto_advance(db, c, "corrective_action_submitted", user, "car.submit.auto")   # P2 훅: CAR 제출→시정조치 제출 상태
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


@app.get("/cases/{case_id}/fatwa/status")
def get_fatwa_status(case_id: str, user=Depends(auth.get_current_user), db: Session = Depends(get_db)):
    """클라이언트용 파트와 진행 상태 요약 — 위원회 내부 투표/노트는 제외, 상태·결정·서명 진행 카운트만."""
    c = _get_case(db, case_id, user)
    fd = db.query(models.FatwaDecision).filter_by(case_id=case_id).first()
    votes = db.query(models.FatwaVote).filter_by(case_id=case_id).all()
    members = (fd.committee_members if fd and fd.committee_members else []) or []
    total = len([x for x in [fd.committee_head if fd else None, fd.committee_secretary if fd else None] if x]) + len(members)
    # fatwa_status 필드가 미설정(none)이어도 케이스 단계로 진행 상태 보정
    fs = c.fatwa_status or "none"
    if fs == "none":
        st = c.status or ""
        if _mi(st) >= 4 or st == "certificate_issued":
            fs = "approved"
        elif _mi(st) == 3 or st.startswith("fatwa"):
            fs = "review"
    return {"case_id": case_id, "fatwa_status": fs,
            "decision": (fd.decision if fd else "pending"), "decision_no": (fd.decision_no if fd else None),
            "decided_at": str(fd.decided_at)[:19] if fd and fd.decided_at else None,
            "final_approved_at": str(fd.final_approved_at)[:19] if fd and fd.final_approved_at else None,
            "votes_total": len(votes), "votes_approve": sum(1 for v in votes if v.vote == "approve"),
            "committee_size": total or None}


# ========== 2인 승인(maker-checker) 프레임워크 — 설계서 보강안 §4.3 ==========
MAKER_CHECKER = {
    "certificate.issue":  {"maker": {"operator"}, "checker": {"fatwa_liaison"}, "label": "인증서 발급"},
    "certificate.revoke": {"maker": {"operator"}, "checker": {"admin"}, "label": "인증서 철회"},
}


def _create_approval(db, action_type, c, user, reason):
    """maker의 승인요청 생성(pending). 동일 케이스·액션의 중복 pending 방지."""
    dup = db.query(models.ApprovalRequest).filter_by(
        action_type=action_type, case_id=c.case_id, status="pending").first()
    if dup:
        raise HTTPException(409, {"code": "APPROVAL_PENDING", "approval_id": dup.id})
    ar = models.ApprovalRequest(
        action_type=action_type, case_id=c.case_id, org_id=c.org_id,
        reason=reason or None, payload={"reason": reason or None},
        requested_by=user["uid"], requester_role=user["role"],
        requester_name=user.get("username"))
    db.add(ar)
    sm.record_event(db, c, c.status, c.status, action_type + ".requested",
                    user["role"], user["uid"], {"approval_id": ar.id, "reason": reason or None})
    db.commit()
    rule = MAKER_CHECKER.get(action_type, {})
    return {"approval_id": ar.id, "status": "pending", "action_type": action_type,
            "checker_roles": sorted(rule.get("checker", set())),
            "message": "2인 승인 대기 — 승인자 확인 후 %s이(가) 실행됩니다." % (rule.get("label") or action_type)}


def _check_checker(ar, user):
    """checker 자격 검증 — 역할 매핑 + self-approval 금지(설계서 §4.3)."""
    rule = MAKER_CHECKER.get(ar.action_type)
    if not rule:
        raise HTTPException(400, {"code": "UNKNOWN_ACTION"})
    if user["role"] != "admin" and user["role"] not in rule["checker"]:
        raise HTTPException(403, {"code": "NOT_A_CHECKER", "required": sorted(rule["checker"])})
    if user["uid"] == ar.requested_by:
        raise HTTPException(403, {"code": "SELF_APPROVAL_FORBIDDEN"})


def _exec_approved(db, ar, user):
    """승인된 요청의 실제 액션 실행 — 액션별 본체 함수로 디스패치. checker는 인증기관 역할이라 org 무관 접근."""
    c = db.get(models.CaseApplication, ar.case_id)
    if not c:
        raise HTTPException(404, {"code": "CASE_NOT_FOUND", "case_id": ar.case_id})
    reason = (ar.payload or {}).get("reason")
    if ar.action_type == "certificate.issue":
        _issue_guards(db, c, ar.case_id)   # 발급 재검증(요청 후 상태 변동 대비)
        return _do_issue_certificate(db, c, user, reason)
    if ar.action_type == "certificate.revoke":
        return _do_revoke_certificate(db, c, user, reason)
    raise HTTPException(400, {"code": "UNKNOWN_ACTION", "action": ar.action_type})


@app.get("/approvals")
def list_approvals(status: str = "pending", user=Depends(auth.get_current_user),
                   db: Session = Depends(get_db)):
    """승인 요청 목록(기본 pending) — 각 항목에 내가 결정 가능한지(can_decide) 표시."""
    q = db.query(models.ApprovalRequest)
    if status:
        q = q.filter_by(status=status)
    rows = q.order_by(models.ApprovalRequest.created_at.desc()).limit(200).all()
    out = []
    for ar in rows:
        rule = MAKER_CHECKER.get(ar.action_type, {})
        is_checker = (user["role"] == "admin" or user["role"] in rule.get("checker", set()))
        can_decide = is_checker and user["uid"] != ar.requested_by and ar.status == "pending"
        c = db.get(models.CaseApplication, ar.case_id) if ar.case_id else None
        out.append({
            "approval_id": ar.id, "action_type": ar.action_type, "label": rule.get("label"),
            "case_id": ar.case_id, "company_name": (c.company_name if c else None),
            "reason": ar.reason, "status": ar.status,
            "requested_by": ar.requested_by, "requester_role": ar.requester_role,
            "requester_name": ar.requester_name,
            "created_at": ar.created_at.isoformat() if ar.created_at else None,
            "decided_by": ar.decided_by, "decider_name": ar.decider_name,
            "decision_reason": ar.decision_reason,
            "decided_at": ar.decided_at.isoformat() if ar.decided_at else None,
            "checker_roles": sorted(rule.get("checker", set())), "can_decide": can_decide})
    return out


@app.post("/approvals/{approval_id}/approve")
def approve_request(approval_id: str, body: schemas.ApprovalDecisionReq = schemas.ApprovalDecisionReq(),
                    user=Depends(auth.get_current_user), db: Session = Depends(get_db)):
    """승인(checker) — 자격 검증(역할+self-approval 금지) 후 실제 액션 실행."""
    ar = db.get(models.ApprovalRequest, approval_id)
    if not ar:
        raise HTTPException(404, {"code": "APPROVAL_NOT_FOUND"})
    if ar.status != "pending":
        raise HTTPException(409, {"code": "ALREADY_DECIDED", "status": ar.status})
    _check_checker(ar, user)
    result = _exec_approved(db, ar, user)   # 액션 본체 실행(내부 commit)
    ar.status = "approved"
    ar.decided_by = user["uid"]; ar.decider_role = user["role"]; ar.decider_name = user.get("username")
    ar.decided_at = datetime.utcnow(); ar.result = result
    ar.decision_reason = (body.reason or "").strip() or None
    db.commit()
    return {"approval_id": ar.id, "status": "approved", "result": result}


@app.post("/approvals/{approval_id}/reject")
def reject_request(approval_id: str, body: schemas.ApprovalDecisionReq = schemas.ApprovalDecisionReq(),
                   user=Depends(auth.get_current_user), db: Session = Depends(get_db)):
    """거절(checker) — 자격 검증 후 요청 기각(실행 안 됨)."""
    ar = db.get(models.ApprovalRequest, approval_id)
    if not ar:
        raise HTTPException(404, {"code": "APPROVAL_NOT_FOUND"})
    if ar.status != "pending":
        raise HTTPException(409, {"code": "ALREADY_DECIDED", "status": ar.status})
    _check_checker(ar, user)
    ar.status = "rejected"
    ar.decided_by = user["uid"]; ar.decider_role = user["role"]; ar.decider_name = user.get("username")
    ar.decided_at = datetime.utcnow()
    ar.decision_reason = (body.reason or "").strip() or None
    c = db.get(models.CaseApplication, ar.case_id) if ar.case_id else None
    if c:
        sm.record_event(db, c, c.status, c.status, ar.action_type + ".rejected",
                        user["role"], user["uid"], {"approval_id": ar.id, "reason": ar.decision_reason})
    db.commit()
    return {"approval_id": ar.id, "status": "rejected", "reason": ar.decision_reason}


def _issue_ready_states():
    """인증서 발급이 정당한 직전 상태 — 전이표에서 끌어온다(표가 바뀌면 같이 따라간다)."""
    return {frm for frm, tos in sm.TRANSITIONS.items() if "certificate_issued" in tos}


def _issue_guards(db, c, case_id):
    """인증서 발급 사전조건(§4.2) — 심사 단계 도달·fatwa 승인·스코프 동결·결제완료·
    미해결 Major NC 없음·문서 all-approved. 위반 시 409.

    상태 검사가 빠져 있었다. 그래서 현장심사를 거치지 않은 케이스에도 인증서가 나갔고
    (실측: consultant_review 상태에서 발급됨), 발급 함수의 상태 전이는 조용히 건너뛰어져
    '인증서는 있는데 진행 단계는 심사 중'인 케이스가 남았다. 인증기관 제품에서 이건
    서류상 앞뒤가 안 맞는 상태다."""
    ready = _issue_ready_states()
    if c.status not in ready and c.status != "certificate_issued":
        raise HTTPException(409, {"code": "STATE_NOT_READY", "status": c.status,
                                  "need": sorted(ready),
                                  "hint": "심사·판정 단계를 마쳐야 발급할 수 있습니다"})
    if c.fatwa_status != "approved":
        raise HTTPException(409, {"code": "FATWA_NOT_APPROVED"})
    if not c.scope_frozen:
        raise HTTPException(409, {"code": "SCOPE_NOT_FROZEN"})
    if db.query(models.Invoice).filter(models.Invoice.case_id == case_id,
                                       ~models.Invoice.status.in_(sm.INVOICE_SETTLED)).count() > 0:
        raise HTTPException(409, {"code": "PAYMENT_PENDING"})
    if sm.open_major_nc(db, case_id) > 0:
        raise HTTPException(409, {"code": "UNRESOLVED_MAJOR_NC"})
    bad_docs = (db.query(models.DocumentAsset)
                .filter(models.DocumentAsset.case_id == case_id,
                        models.DocumentAsset.review_status.in_(("rejected", "rework"))).count())
    if bad_docs > 0:
        raise HTTPException(409, {"code": "DOCUMENTS_NOT_APPROVED", "unresolved": bad_docs})


def _do_issue_certificate(db, c, user, reason=None):
    """실제 인증서 발급 실행(가드 통과 후). user=실행자(2인승인 시 승인자). 설계서 §6·§S3-3."""
    case_id = c.case_id
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
    # 상태 전이 — certificate_issued는 보호상태(raw transition 금지). 이 전용 함수가 소유.
    frm = c.status
    if "certificate_issued" in sm.TRANSITIONS.get(c.status, set()):
        c.status = "certificate_issued"
    _issue_payload = {"certificate_no": cert.certificate_no, "reason": (reason or "").strip() or None}
    _issue_payload.update(_tsa_meta(cert.certificate_no or str(cert.id)))   # P3d: TSA configured 시만 병기(미설정 불변)
    sm.record_event(db, c, frm, c.status, "certificate.issue", "system", user["uid"], _issue_payload)
    # S3-3: freeze snapshot — 발급 시 제품/원재료 ID 동결
    prod_ids = [p.product_id for p in db.query(models.Product).filter_by(case_id=case_id)]
    mat_ids = [m.material_id for m in db.query(models.Material).filter_by(case_id=case_id)]
    cert.frozen_product_ids = prod_ids
    cert.frozen_material_ids = mat_ids
    _notify(db, c, "certificate_issued",
            channels=["inapp", "sms", "kakao", "whatsapp"], role="applicant",
            msg=("certificate_issued", {"company": c.company_name or "",
                                        "cert_no": cert.certificate_no}))
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


@app.post("/cases/{case_id}/certificate/issue")
def issue_certificate(case_id: str, body: schemas.IssueReq = schemas.IssueReq(),
                      user=Depends(rbac.require_action("certificate.issue")),
                      db: Session = Depends(get_db)):
    """인증서 발급 요청(maker=operator) — 2인 승인(§4.3). 가드 프리체크 후 승인요청 생성.
    실제 발급은 승인자(sharia)가 /approvals/{id}/approve 로 실행."""
    c = _get_case(db, case_id, user)
    ex = db.query(models.HalalCertificate).filter_by(case_id=case_id, status="active").first()
    if ex:   # 이미 발급됨 — 승인 불필요, 그대로 반환(하위호환)
        return {"certificate_no": ex.certificate_no, "issue_date": ex.issue_date,
                "expiry_date": ex.expiry_date, "scope": ex.scope, "existing": True}
    _issue_guards(db, c, case_id)   # 발급 가능 상태인지 사전 검증(부적합 시 즉시 409)
    return _create_approval(db, "certificate.issue", c, user, (body.reason or "").strip())


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
    """S8-2: C5 갱신 케이스 파생(승인·실행) — 문서 P0: operator 전용(신청과 권한 분리).
    P3: 원 케이스는 renewal_preparation으로 전진(사후관리 체인 경유) — 고아 상태였던
    renewal_preparation에 비즈니스 트리거 부여. 갱신은 사후관리·변경영향 상태에서도 허용."""
    src = _get_case(db, case_id, user)
    if src.status not in ("certificate_issued", "post_certification_monitoring",
                          "change_impact", "renewal_preparation"):   # renewal_preparation=재갱신(반복 호출 호환)
        raise HTTPException(400, {"code": "NOT_ISSUED", "detail": "인증서 발급(또는 사후관리) 케이스만 갱신 가능합니다."})
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
    # P3: 원 케이스를 renewal_preparation까지 전진 — certificate_issued면 사후관리 경유
    cur = src.status
    for st in ("post_certification_monitoring", "renewal_preparation"):
        if sm.allowed(cur, st):
            sm.apply_side_effects(src, st)
            sm.record_event(db, src, cur, st, "case.renew.auto", user["role"], user["uid"],
                            {"new_case_id": new_id})
            cur = st
    src.status = cur
    db.commit()
    return {"new_case_id": new_id, "parent_case_id": case_id, "parent_status": src.status,
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


# ---------- P2-4: 할랄마크번호·파트와참조번호·전체 ZIP (스키마 무변경) ----------
def _halal_mark_no(cert):
    """내부 참조번호(비공식) — 스키마 무변경: 인증서 id 기반 결정적 파생값.
    ⚠ 공식 할랄번호(No. Ketetapan Halal)가 아니다. 공식번호는 BPJPH만 발급하며 SIHALAL import로만 확정.
    값 생성 로직은 유지(발급마다 안정적으로 동일값)하되, 표기는 항상 '내부 참조번호(비공식)'로 한다."""
    if not cert or not cert.id:
        return None
    try:
        seed = int(cert.id[:12], 16)   # id는 hex uuid
    except (ValueError, TypeError):
        seed = abs(hash(cert.id))
    return "ID" + str(10000000000000 + (seed % 89999999999999))


def _case_fatwa_no(db, case_id):
    """파트와 결정번호(No. Fatwa) — FatwaDecision.decision_no 조인(신규 저장 없음)."""
    fd = (db.query(models.FatwaDecision).filter_by(case_id=case_id)
          .order_by(models.FatwaDecision.decided_at.desc().nullslast()).first())
    if not fd:
        fd = db.query(models.FatwaDecision).filter_by(case_id=case_id).first()
    return (fd.decision_no if fd else None) or None


# ── 법적효력 Phase 0(법적 포지셔닝·고지) — 스키마 무변경 ──────────────────────────
#    이 플랫폼은 LPH/PPH 촉진 도구이며 인증 발급기관이 아니다. 공식 할랄인증서는 BPJPH만 발급한다.
#    자체 파생값(_halal_mark_no)을 공식형식으로 오인시키지 않도록 라벨·고지를 부착한다.
_LEGAL_DISCLAIMER_PDF = ("고지 · Disclaimer : 본 문서는 준비/내부 산출물이며, 공식 할랄 인증 발급은 "
                         "BPJPH/SIHALAL 절차로 확정됩니다. · Penerbitan resmi ditentukan oleh proses "
                         "BPJPH/SIHALAL.")
_SIG_NATURE = ("내부 무결성 서명(공인 전자서명 아님) · Internal integrity signature "
               "(not a PSrE qualified e-signature)")
_SIG_VALID_MEANING = ("무결성 확인일 뿐 서명자 신원·법적 부인방지를 보장하지 않습니다. · Hanya memastikan "
                      "integritas dokumen, bukan identitas penanda tangan atau non-repudiasi hukum.")
_LEGAL_DISCLAIMER_VERIFY = ("본 검증은 내부 산출물의 무결성 확인이며 공식 할랄 인증서가 아닙니다. 공식 발급·번호는 "
                            "BPJPH/SIHALAL이 확정합니다. · Verifikasi ini memastikan integritas dokumen "
                            "internal, bukan sertifikat halal resmi. Penerbitan resmi oleh BPJPH/SIHALAL.")


# ── 법적효력 Phase 1 — PSrE 공인 전자서명 커넥터 (스키마 무변경·순수 추가) ──────────────────────
# 정직성(중요): 실제 PSrE(Penyelenggara Sertifikasi Elektronik) 공인 전자서명은 벤더 계약·API
# 자격증명이 있어야 가능하다. 자격증명 미설정 시 서명하는 '척' 위조하지 않고 기존 내부 HMAC 서명으로
# 정직 폴백한다(Phase 0의 "내부 무결성 서명, 공인 아님" 표기 유지) — SIHALAL Layer B(_sihalal_submit_config)
# · notify.py 채널(channel_status의 kakao_ok) 크리덴셜 게이트와 완전히 동일한 패턴.
# 실 PSrE API 요청/응답 필드는 벤더(PrivyID/VIDA/Peruri/Tilaka)마다 다르므로 범용/설정형으로 구현하고,
# 벤더 선정·자격증명 확보 후 필드 매핑을 최종화한다. 미설정(기본값)일 때 기존 서명/PDF/응답은 100% 불변.
_PSRE_CERTIFIED_EVENT = "signature.certified"   # WorkflowEvent action (케이스 스코프·스키마 무변경)


def _psre_config():
    """PSrE 공인 전자서명 자격증명 게이트 — 값은 노출하지 않고 configured(bool)+provider만 판정.
    GLHAC_PSRE_PROVIDER + GLHAC_PSRE_API_URL + (GLHAC_PSRE_TOKEN 또는 GLHAC_PSRE_API_KEY) 완비 시에만 configured.
    (_sihalal_submit_config / notify.channel_status의 kakao_ok 크리덴셜 게이트와 완전 동일 패턴.)"""
    e = os.environ.get
    provider = e("GLHAC_PSRE_PROVIDER")
    url = e("GLHAC_PSRE_API_URL")
    token = e("GLHAC_PSRE_TOKEN") or e("GLHAC_PSRE_API_KEY")
    return {"configured": bool(provider and url and token),
            "provider": provider, "url": url, "token": token}


def _psre_sign(payload_hash, signer, doc_ref):
    """PSrE 공인 전자서명 어댑터 — configured면 PSrE API에 공인서명을 요청(범용/설정형 payload, 예외 graceful).
    성공 시 {provider, signer_identity, tsa_timestamp, signature_ref, certificate_serial} 반환.
    미설정/실패 시 None(→ 호출부는 기존 내부 HMAC 서명을 그대로 유지 = 회귀 0).
    ⚠ 실 PSrE(PrivyID/VIDA/Peruri/Tilaka) 요청/응답 필드는 벤더마다 다르므로 아래 매핑은 범용 설정형이며
    벤더 계약·자격증명 확보 후 최종화한다. 미설정 시 항상 None을 반환해 '서명하는 척' 위조를 금지한다."""
    cfg = _psre_config()
    if not cfg["configured"]:
        return None
    try:
        import httpx
        r = httpx.post(cfg["url"], timeout=15,
                       headers={"Authorization": "Bearer " + cfg["token"],
                                "Content-Type": "application/json"},
                       json={"provider": cfg["provider"], "payload_hash": payload_hash,
                             "signer": signer, "document_ref": doc_ref})
        if not (200 <= r.status_code < 300):
            return None
        try:
            body = r.json()
        except Exception:  # noqa: BLE001
            body = {}
    except Exception:  # noqa: BLE001
        return None
    # 벤더별 응답 키가 상이하므로 관용적 후보들로 매핑(설정형) — 확보 스펙에 맞춰 최종화.
    return {"provider": cfg["provider"],
            "signer_identity": body.get("signer_identity") or body.get("signer") or signer,
            "tsa_timestamp": (body.get("tsa_timestamp") or body.get("timestamp")
                              or datetime.utcnow().isoformat()),
            "signature_ref": (body.get("signature_ref") or body.get("id")
                              or body.get("document_id")),
            "certificate_serial": (body.get("certificate_serial") or body.get("serial"))}


def _psre_certify(db, case, subject, signer, doc_ref, payload_hash):
    """서명 엔드포인트 훅 — PSrE 공인서명 성공 시 signature.certified 이벤트(케이스 스코프)를 기록한다.
    기존 내부 HMAC 서명/서명 이벤트는 호출부에서 그대로 병행(무결성 이중). 미설정/실패 시 None(회귀 0)."""
    res = _psre_sign(payload_hash, signer, doc_ref)
    if not res:
        return None
    st = getattr(case, "status", "") or ""
    payload = {"subject": subject, "provider": res["provider"],
               "signer": res["signer_identity"], "tsa_timestamp": res["tsa_timestamp"],
               "signature_ref": res["signature_ref"], "doc_ref": doc_ref}
    payload.update(_tsa_meta(payload_hash))   # P3d: TSA configured 시에만 신뢰 타임스탬프 병기(미설정 불변)
    sm.record_event(db, case, st, st, _PSRE_CERTIFIED_EVENT, "system", signer, payload)
    return res


# ── 법적효력 Phase 3 — TSA 신뢰 타임스탬프 커넥터 (자격증명 게이트·순수 추가·스키마 무변경) ──────────
# 정직성(중요): RFC3161 TSA 신뢰 타임스탬프는 TSA 서비스 URL(및 선택 토큰)이 있어야 가능하다.
# 미설정(기본값)이면 스탬프하는 '척' 위조하지 않고 내부 시각만 사용한다 — PSrE/SIHALAL/notify 크리덴셜
# 게이트와 완전 동일 패턴. 미설정 시 서명/발급 이벤트 payload·해시·응답은 100% 불변(회귀 0).
def _tsa_config():
    """TSA 신뢰 타임스탬프 자격증명 게이트 — GLHAC_TSA_URL 설정 시 configured. GLHAC_TSA_TOKEN은 선택."""
    e = os.environ.get
    url = e("GLHAC_TSA_URL")
    token = e("GLHAC_TSA_TOKEN")
    return {"configured": bool(url), "url": url, "token": token}


def _tsa_stamp(digest):
    """RFC3161 TSA 신뢰 타임스탬프 요청(범용/설정형·graceful) — configured면 {tsa_time,tsa_ref}, 미설정/실패 None.
    ⚠ 실 TSA 요청/응답 필드는 서비스마다 상이하므로 설정형이며 자격증명 확보 후 최종화한다."""
    cfg = _tsa_config()
    if not cfg["configured"]:
        return None
    try:
        import httpx
        headers = {"Content-Type": "application/json"}
        if cfg["token"]:
            headers["Authorization"] = "Bearer " + cfg["token"]
        r = httpx.post(cfg["url"], timeout=15, headers=headers,
                       json={"digest": digest, "hash_algo": "sha256"})
        if not (200 <= r.status_code < 300):
            return None
        try:
            body = r.json()
        except Exception:  # noqa: BLE001
            body = {}
    except Exception:  # noqa: BLE001
        return None
    return {"tsa_time": (body.get("tsa_time") or body.get("timestamp")
                         or datetime.utcnow().isoformat()),
            "tsa_ref": (body.get("tsa_ref") or body.get("serial") or body.get("id"))}


def _tsa_meta(digest):
    """이벤트 meta에 병합할 TSA 조각 — configured+성공 시 {tsa_time,tsa_ref,tsa:'connected'}, 아니면 {}(불변)."""
    if not _tsa_config()["configured"]:
        return {}
    st = _tsa_stamp(digest)
    return {"tsa_time": st["tsa_time"], "tsa_ref": st["tsa_ref"], "tsa": "connected"} if st else {}


def _sig_nature_for(db, case_id, subject=None):
    """동적 서명 성격 표기 — 해당 케이스(옵션: subject 문서)에 PSrE 공인서명(signature.certified) 이벤트가
    있으면 'PSrE 공인 전자서명 · UU ITE 법적효력' 표기를 반환하고, 없으면 기존 내부 _SIG_NATURE를 반환한다.
    ⚠ PSrE 미설정(기본값)이면 certified 이벤트가 없으므로 항상 _SIG_NATURE를 반환 → 출력 완전 불변(회귀 0)."""
    try:
        rows = (db.query(models.WorkflowEvent)
                .filter(models.WorkflowEvent.case_id == case_id,
                        models.WorkflowEvent.action == _PSRE_CERTIFIED_EVENT)
                .order_by(models.WorkflowEvent.created_at.desc()).all())
    except Exception:  # noqa: BLE001
        rows = []
    if not rows:
        return _SIG_NATURE
    ev = None
    if subject:
        ev = next((r for r in rows if (r.payload or {}).get("subject") == subject), None)
    ev = ev or rows[0]
    p = ev.payload or {}
    provider = p.get("provider") or "PSrE"
    tsa = str(p.get("tsa_timestamp") or "")[:19]
    return ("PSrE 공인 전자서명 · Tanda tangan tersertifikasi PSrE (%s%s) — UU ITE 법적효력"
            % (provider, (", TSA " + tsa) if tsa else ""))


def _official_bpjph_no(db, case_id):
    """공식 BPJPH 할랄번호(No. Ketetapan Halal) 조회 — 스키마 무변경.
    SIHALAL에서 'certificate_number_imported' 이벤트로 import된 경우에만 존재.
    IntegrationEvent/WorkflowEvent payload에서 공식번호를 찾고, 없으면 None(→ 자체값은 비공식 표기)."""
    keys = ("certificate_number", "no_ketetapan_halal", "ketetapan_halal_no",
            "official_no", "certificate_no", "number")

    def _pick(payload):
        if isinstance(payload, dict):
            for k in keys:
                val = payload.get(k)
                if val and str(val).strip():
                    return str(val).strip()
        return None

    rows = (db.query(models.IntegrationEvent)
            .filter_by(case_id=case_id, event_type="certificate_number_imported")
            .order_by(models.IntegrationEvent.created_at.desc()).all())
    for e in rows:
        got = _pick(e.payload)
        if got:
            return got
    wrows = (db.query(models.WorkflowEvent)
             .filter_by(case_id=case_id, action="certificate_number_imported")
             .order_by(models.WorkflowEvent.created_at.desc()).all())
    for e in wrows:
        got = _pick(e.payload)
        if got:
            return got
    return None


def _halal_no_line(db, cert):
    """PDF용 할랄번호 표기 — 공식 BPJPH번호 있으면 공식 라벨, 없으면 '내부 참조번호(비공식)'.
    공식번호 부재 시 'No. Ketetapan Halal' 라벨을 자체값에 붙이지 않는다(법적 오인 차단)."""
    official = _official_bpjph_no(db, cert.case_id)
    if official:
        return "No. Ketetapan Halal (BPJPH) : %s" % official
    # 비공식: 자체값에 'No. Ketetapan Halal' 라벨 금지. 대기 안내는 짧은 별행(줄바꿈 분절 방지).
    return ("내부 참조번호 · Internal Ref (비공식) : %s\nMenunggu No. Ketetapan Halal (BPJPH)"
            % (_halal_mark_no(cert) or "-"))


def _render_certificate_pdf(db, c, cert):
    """디자인 할랄 인증서 PDF — 미리보기(골드 이중테두리·엠블럼·중앙정렬 카드)와 동일 룩앤필."""
    import fitz
    W, H = fitz.paper_size("a4")
    doc = fitz.open()
    pg = doc.new_page(width=W, height=H)
    F = "korea"
    GOLD = (0.706, 0.325, 0.035)      # #B45309
    GOLD2 = (0.79, 0.55, 0.18)
    BROWN = (0.486, 0.247, 0.024)     # #7c3f06
    INK = (0.20, 0.16, 0.10)
    GREY = (0.42, 0.40, 0.36)
    CREAM = (0.996, 0.976, 0.906)     # 옅은 앰버 배경
    LINE = (0.85, 0.74, 0.50)
    cx = W / 2

    def ctxt(y, s, size, color, font=F):
        s = str(s)
        w = fitz.get_text_length(s, fontname=font, fontsize=size)
        pg.insert_text((cx - w / 2, y), s, fontname=font, fontsize=size, color=color)

    def wrap(s, width, size):
        out, cur = [], ""
        for word in str(s).split():
            t = (cur + " " + word).strip()
            if fitz.get_text_length(t, fontname=F, fontsize=size) > width and cur:
                out.append(cur); cur = word
            else:
                cur = t
        if cur:
            out.append(cur)
        return out or ["-"]

    def cfit(y, s, size, color, maxw=W - 130, font=F):
        """중앙정렬 + 폭 초과 시 폰트 축소(프레임 밖 넘침 방지)."""
        s = str(s)
        while size > 5 and fitz.get_text_length(s, fontname=font, fontsize=size) > maxw:
            size -= 0.5
        ctxt(y, s, size, color, font)

    # 배경 + 골드 이중 테두리(미리보기 3px #B45309 + 그라디언트 배경 재현)
    pg.draw_rect(fitz.Rect(40, 40, W - 40, H - 40), fill=CREAM, color=GOLD, width=2.4)
    pg.draw_rect(fitz.Rect(48, 48, W - 48, H - 48), color=GOLD2, width=0.8)

    # 엠블럼(GLHAC 로고 — 할랄마크 포함)
    _logo = os.path.join(os.path.dirname(__file__), "assets", "glhac_logo.png")
    if os.path.exists(_logo):
        try:
            pg.insert_image(fitz.Rect(cx - 80, 74, cx + 80, 142), filename=_logo, keep_proportion=True)
        except Exception:
            ctxt(122, "GL-HAC AI", 22, GOLD)
    else:
        ctxt(122, "GL-HAC AI", 22, GOLD)

    ctxt(174, "S E R T I F I K A T   H A L A L", 14, GOLD)
    ctxt(192, "할랄 인증서 · Halal Certificate", 10, GREY)
    pg.draw_line((cx - 96, 204), (cx + 96, 204), color=GOLD, width=0.8)

    # 회사명
    y = 252
    for ln in wrap(c.company_name or "-", W - 210, 25)[:2]:
        ctxt(y, ln, 25, BROWN); y += 30
    y += 4

    # 범위(제품)
    ctxt(y, "범위 · Scope", 8, GOLD); y += 16
    for ln in wrap(", ".join(cert.scope or []) or "-", W - 180, 10)[:3]:
        ctxt(y, ln, 10, GREY); y += 14
    y += 12
    pg.draw_line((cx - 150, y), (cx + 150, y), color=LINE, width=0.6); y += 26

    # 인증 문구(KO + EN)
    cfit(y, "본 제품은 SJPH 및 샤리아 기준에 따라 할랄(HALAL) 인증되었음을 증명합니다.", 10.5, INK); y += 18
    cfit(y, "This product is certified HALAL in accordance with SJPH and Sharia standards.", 9, GREY); y += 28

    # 인증번호 박스
    bw = 380
    pg.draw_rect(fitz.Rect(cx - bw / 2, y, cx + bw / 2, y + 60), fill=(0.998, 0.992, 0.965), color=LINE, width=0.7)
    ctxt(y + 22, "No. %s" % (cert.certificate_no or "-"), 13, BROWN)
    cfit(y + 43, "발급 · Issued  %s      만료 · Valid until  %s" % (cert.issue_date or "-", cert.expiry_date or "-"), 9.5, INK, maxw=bw - 28)
    y += 60 + 22

    # 참조번호 / 파트와
    official = _official_bpjph_no(db, cert.case_id)
    if official:
        cfit(y, "No. Ketetapan Halal (BPJPH) : %s" % official, 9.5, INK); y += 16
    else:
        cfit(y, "내부 참조 · Internal Ref (비공식) : %s" % (_halal_mark_no(cert) or "-"), 9, GREY); y += 14
        ctxt(y, "· Menunggu No. Ketetapan Halal (BPJPH)", 8, GREY); y += 16
    cfit(y, "파트와 결정번호 · No. Fatwa : %s" % (_case_fatwa_no(db, cert.case_id) or "-"), 9.5, INK); y += 16
    if cert.qr_token:
        cfit(y, "공개검증 · Verify : /verify/%s" % cert.qr_token, 8.5, GOLD); y += 18

    # 상태 스탬프(active 외 — 정지/철회)
    if cert.status and cert.status != "active":
        pg.draw_rect(fitz.Rect(cx - 78, y + 4, cx + 78, y + 40), color=(0.70, 0.12, 0.12), width=1.8)
        ctxt(y + 30, cert.status.upper(), 15, (0.70, 0.12, 0.12)); y += 46

    # 전자서명 + 도장(하단)
    sig = (db.query(models.Signature).filter_by(subject_type="certificate", subject_id=cert.id)
           .order_by(models.Signature.signed_at.desc()).first())
    sy = H - 158
    pg.draw_circle(fitz.Point(cx, sy + 8), 30, color=GOLD, width=1.2)
    ctxt(sy + 4, "GL-HAC", 7.5, GOLD)
    ctxt(sy + 15, "HALAL", 7.5, GOLD)
    ctxt(sy + 56, "전자서명 · %s" % ("Digitally Signed" if sig else "Not signed"), 9, INK)
    if sig:
        ctxt(sy + 70, "(%s · %s)" % ((sig.provider or "internal"), str(sig.signed_at)[:16]), 7.5, GREY)
        ctxt(sy + 82, "서명 성격 · " + _sig_nature_for(db, cert.case_id, "certificate"), 7, GREY)

    # 법적 고지 푸터
    ctxt(H - 70, "GL-HAC AI · Halal Certification Platform · 공개 검증 페이지에서 진위를 확인하세요.", 7.5, GREY)
    for i, ln in enumerate(wrap(_LEGAL_DISCLAIMER_PDF.replace("\n", " "), W - 130, 6.5)[:2]):
        ctxt(H - 58 + i * 9, ln, 6.5, GREY)

    return doc.tobytes()


def _cert_pdf_bytes(db, c, cert):
    """인증서 PDF — 디자인 렌더(certificate_pdf·bundle 공용). 미리보기와 동일 룩."""
    return _render_certificate_pdf(db, c, cert)


# ── 제품별 개별 인증서 (범위 확장) — 스키마 무변경.
#    모(母) 인증서(HalalCertificate)를 근거로 제품 단위 파생 인증서를 렌더한다.
#    · 제품 인증번호 : {certificate_no}-P{n}  (동결 제품 순번, 안정적)
#    · 제품 검증토큰 : {qr_token}.{product_id[:8]}  → /verify/{token} 이 파싱해 제품 스코프로 응답
#    발급 사실·유효성은 모 인증서가 정본이므로 별도 저장이 필요 없다(이중 정본 방지). ──
def _cert_products(db, cert):
    """발급 시 동결된 제품 목록(정본) — 동결 없으면 현재 제품으로 폴백."""
    ids = list(cert.frozen_product_ids or [])
    if ids:
        rows = db.query(models.Product).filter(models.Product.product_id.in_(ids)).all()
        order = {pid: i for i, pid in enumerate(ids)}
        return sorted(rows, key=lambda p: order.get(p.product_id, 999))
    return db.query(models.Product).filter_by(case_id=cert.case_id).order_by(models.Product.name).all()


def _product_cert_meta(db, cert, product):
    prods = _cert_products(db, cert)
    idx = next((i for i, p in enumerate(prods) if p.product_id == product.product_id), None)
    if idx is None:
        return None
    official = _official_bpjph_no(db, cert.case_id)
    return {"product_id": product.product_id, "product_name": product.name,
            "category": product.category,
            "certificate_no": "%s-P%d" % (cert.certificate_no or "HC", idx + 1),
            "parent_certificate_no": cert.certificate_no,
            "halal_mark_no": _halal_mark_no(cert),          # 내부 참조번호(비공식)
            "official_bpjph_no": official,                  # 공식 BPJPH 번호(import된 경우만)
            "halal_no_is_official": bool(official),
            "issue_date": cert.issue_date, "expiry_date": cert.expiry_date,
            "status": cert.status,
            # 모 인증서에 검증토큰이 없는 구(舊) 발급분은 제품 토큰도 없음(공개검증 불가) — "."만 남는 깨진 토큰 방지
            "qr_token": ("%s.%s" % (cert.qr_token, product.product_id[:8])) if cert.qr_token else None}


@app.get("/cases/{case_id}/certificate/products")
def list_product_certificates(case_id: str, user=Depends(auth.get_current_user),
                              db: Session = Depends(get_db)):
    """제품별 개별 인증서 목록 — 모 인증서 발급 후 제품 수만큼 파생."""
    _get_case(db, case_id, user)
    cert = db.query(models.HalalCertificate).filter_by(case_id=case_id, status="active").first()
    if not cert:
        return {"issued": False, "parent_certificate_no": None, "items": [], "count": 0}
    items = [i for i in (_product_cert_meta(db, cert, p) for p in _cert_products(db, cert)) if i]
    return {"issued": True, "parent_certificate_no": cert.certificate_no,
            "items": items, "count": len(items)}


@app.get("/cases/{case_id}/certificate/products/{product_id}.pdf")
def product_certificate_pdf(case_id: str, product_id: str,
                            user=Depends(auth.get_current_user), db: Session = Depends(get_db)):
    """제품 단위 인증서 PDF — 모 인증서 유효성·전자서명을 승계."""
    from fastapi.responses import Response
    c = _get_case(db, case_id, user)
    cert = db.query(models.HalalCertificate).filter_by(case_id=case_id, status="active").first()
    if not cert:
        raise HTTPException(404, {"code": "CERT_NOT_FOUND"})
    p = db.query(models.Product).filter_by(case_id=case_id, product_id=product_id).first()
    if not p:
        raise HTTPException(404, {"code": "PRODUCT_NOT_FOUND"})
    meta = _product_cert_meta(db, cert, p)
    if not meta:
        raise HTTPException(409, {"code": "PRODUCT_NOT_IN_SCOPE",
                                  "detail": "발급 시 동결된 제품 범위에 없습니다."})
    sig = (db.query(models.Signature).filter_by(subject_type="certificate", subject_id=cert.id)
           .order_by(models.Signature.signed_at.desc()).first())
    lines = [
        "SERTIFIKAT HALAL (PRODUK) · 제품별 할랄 인증서", "",
        "기업 · Perusahaan  : %s" % (c.company_name or "-"),
        "제품 · Produk      : %s%s" % (p.name or "-", (" (" + p.category + ")") if p.category else ""),
        "제품 인증번호 · No : %s" % meta["certificate_no"],
        "모 인증번호 · Parent : %s" % (cert.certificate_no or "-"),
        _halal_no_line(db, cert),
        "파트와 결정번호 · No. Fatwa        : %s" % (_case_fatwa_no(db, case_id) or "-"),
        "상태 · Status      : %s" % cert.status,
        "발급 · Issued      : %s" % (cert.issue_date or "-"),
        "만료 · Valid until : %s" % (cert.expiry_date or "-"), "",
        "본 제품은 SJPH 및 샤리아 기준에 따라 할랄(HALAL) 인증되었음을 증명합니다.",
        "Produk ini disertifikasi HALAL sesuai SJPH dan kriteria Syariah.", "",
        "공개 검증 · Verify : %s" % ("/verify/%s" % meta["qr_token"] if meta["qr_token"] else "-"),
        "전자서명 · Signed  : %s" % ("예 · Yes" if sig else "아니오 · No"),
    ]
    if sig:
        lines.append("서명 성격 · Nature : " + _sig_nature_for(db, cert.case_id, "certificate"))
    lines += ["", _LEGAL_DISCLAIMER_PDF]
    _audit(db, user, "certificate.product_pdf", "certificate", product_id, case_id)
    db.commit()
    pdf = _render_pdf("GL-HAC AI · Halal Certificate (Product)", "\n".join(lines),
                      subtitle=meta["certificate_no"],
                      footer="공개 검증 페이지에서 진위를 확인하세요 · Verify authenticity at /verify")
    return Response(content=pdf, media_type="application/pdf",
                    headers={"Content-Disposition": "attachment; filename=cert_%s.pdf" % meta["certificate_no"]})


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
    official = _official_bpjph_no(db, case_id)   # 공식 BPJPH 번호(SIHALAL import 시에만 존재)
    return {"issued": True, "certificate_no": cert.certificate_no, "scope": cert.scope,
            "issue_date": cert.issue_date, "expiry_date": cert.expiry_date, "status": cert.status,
            "days_to_expiry": days,
            "halal_mark_no": _halal_mark_no(cert),          # 내부 참조번호(비공식, 자체 파생값)
            "official_bpjph_no": official,                  # 공식 No. Ketetapan Halal(있으면)
            "halal_no_is_official": bool(official),          # 프런트 라벨 분기용
            "fatwa_decision_no": _case_fatwa_no(db, case_id),  # P2-4 파트와 결정번호(No. Fatwa) 조인
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
    pdf = _cert_pdf_bytes(db, c, cert)   # 할랄마크번호·파트와결정번호 포함(공통 렌더)
    fn = "certificate_%s.pdf" % (cert.certificate_no or case_id[:8])
    return Response(content=pdf, media_type="application/pdf",
                    headers={"Content-Disposition": "attachment; filename*=UTF-8''" + quote(fn)})


@app.get("/cases/{case_id}/certificate/bundle.zip")
def certificate_bundle(case_id: str, user=Depends(auth.get_current_user), db: Session = Depends(get_db)):
    """P2-4: 전체 ZIP 다운로드 — 인증서 PDF + 관련 생성문서(매뉴얼·심사보고서·결정문 등 GeneratedDocument).
    가드는 인증서 조회와 동일(_get_case org 격리 + 인증서 존재)."""
    import io
    import zipfile
    from fastapi.responses import StreamingResponse
    from urllib.parse import quote
    c = _get_case(db, case_id, user)
    cert = db.query(models.HalalCertificate).filter_by(case_id=case_id).first()
    if not cert:
        raise HTTPException(404, {"code": "CERT_NOT_FOUND"})
    _audit(db, user, "certificate.bundle", "certificate", case_id, case_id)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("certificate_%s.pdf" % (cert.certificate_no or case_id[:8]),
                   _cert_pdf_bytes(db, c, cert))
        # 관련 생성문서(버전관리 GeneratedDocument) — 매뉴얼·심사보고서·결정문 등
        seen = {}
        for g in (db.query(models.GeneratedDocument).filter_by(case_id=case_id)
                  .order_by(models.GeneratedDocument.doc_type,
                            models.GeneratedDocument.version).all()):
            base = "%s_v%d" % (g.doc_type or "document", g.version or 1)
            seen[base] = seen.get(base, 0) + 1
            suffix = "" if seen[base] == 1 else "_%d" % seen[base]
            z.writestr("docs/%s%s.txt" % (base, suffix), g.content or "")
    buf.seek(0)
    fn = "halal_bundle_%s.zip" % (cert.certificate_no or case_id[:8])
    return StreamingResponse(buf, media_type="application/zip",
                             headers={"Content-Disposition": "attachment; filename*=UTF-8''" + quote(fn)})


def _idr(n):
    try:
        return "Rp " + format(float(n or 0), ",.0f")
    except Exception:
        return "Rp 0"



def _terbilang(n):
    """숫자 → 인도네시아어 표기(영수증 금액 문구용)."""
    n = int(round(float(n or 0)))
    sat = ["", "satu", "dua", "tiga", "empat", "lima", "enam", "tujuh",
           "delapan", "sembilan", "sepuluh", "sebelas"]

    def h(x):
        if x < 12:
            return sat[x]
        if x < 20:
            return h(x - 10) + " belas"
        if x < 100:
            return h(x // 10) + " puluh" + ((" " + h(x % 10)) if x % 10 else "")
        if x < 200:
            return "seratus" + ((" " + h(x - 100)) if x - 100 else "")
        if x < 1000:
            return h(x // 100) + " ratus" + ((" " + h(x % 100)) if x % 100 else "")
        if x < 2000:
            return "seribu" + ((" " + h(x - 1000)) if x - 1000 else "")
        if x < 1000000:
            return h(x // 1000) + " ribu" + ((" " + h(x % 1000)) if x % 1000 else "")
        if x < 1000000000:
            return h(x // 1000000) + " juta" + ((" " + h(x % 1000000)) if x % 1000000 else "")
        return h(x // 1000000000) + " miliar" + ((" " + h(x % 1000000000)) if x % 1000000000 else "")

    if n == 0:
        return "nol"
    return " ".join(h(n).split())


# 서비스 코드 → 재무문서 표시명(이중언어). 미등록 snake_case 코드는 제목화 폴백.
_SERVICE_LABELS = {
    "pre_audit": "Pra-Audit Sertifikasi Halal · 사전심사",
    "onsite": "Audit Lapangan · 현장심사",
    "onsite_audit": "Audit Lapangan · 현장심사",
    "certification": "Sertifikasi Halal · 인증심사",
    "supervision": "Pengawasan / Surveilans · 감독심사",
    "surveillance": "Pengawasan / Surveilans · 감독심사",
    "registration": "Pendaftaran · 등록",
    "registration_fee": "Biaya Pendaftaran · 등록비",
}


def _service_label(code, prettify=True, short=False):
    """서비스 코드('pre_audit')를 사람이 읽는 서비스명으로. 자유텍스트(공백 포함)는 그대로.
    short=True면 이중언어 라벨의 인도네시아어 부분만(좁은 표 셀용)."""
    if not code:
        lbl = "Layanan Sertifikasi Halal"
    else:
        s = str(code).strip()
        if s.lower() in _SERVICE_LABELS:
            lbl = _SERVICE_LABELS[s.lower()]
        elif prettify and " " not in s and s.replace("_", "").isalnum():
            lbl = s.replace("_", " ").title()   # pre_audit → Pre Audit
        else:
            lbl = s
    if short:
        lbl = lbl.split(" · ")[0].strip()
    return lbl


def _render_finance_pdf(kind, inv, c, pay=None, items=None):
    """재무문서 전용 렌더러 — 영수증(Kwitansi)·견적서(Penawaran)·세금계산서(Faktur Pajak).
    브랜드 헤더·공급자/구매자 패널·항목표·합계박스·서명란을 갖춘 A4 1매. PyMuPDF 'korea' 폰트."""
    import fitz
    W, H = fitz.paper_size("a4")
    doc = fitz.open()
    pg = doc.new_page(width=W, height=H)
    F = "korea"
    # 문서 종류별 액센트 테마 — 견적서=블루·영수증=그린·세금계산서=앰버(한눈에 구분).
    _FIN_THEME = {
        "quotation": {"g": (0.11, 0.36, 0.62), "g2": (0.20, 0.46, 0.72),
                      "soft": (0.92, 0.95, 0.99), "zebra": (0.965, 0.975, 0.99),
                      "lt": (0.82, 0.88, 0.97), "wm": "PENAWARAN", "badge": "DRAFT · 견적"},
        "receipt":   {"g": (0.043, 0.443, 0.271), "g2": (0.09, 0.55, 0.35),
                      "soft": (0.93, 0.965, 0.945), "zebra": (0.966, 0.978, 0.971),
                      "lt": (0.86, 0.94, 0.89), "wm": "KWITANSI", "badge": "LUNAS · 결제완료"},
        "tax":       {"g": (0.70, 0.46, 0.05), "g2": (0.80, 0.55, 0.12),
                      "soft": (0.985, 0.955, 0.90), "zebra": (0.99, 0.975, 0.955),
                      "lt": (0.96, 0.88, 0.72), "wm": "FAKTUR PAJAK", "badge": "PPN 11% · 과세"},
    }
    _th = _FIN_THEME.get(kind, _FIN_THEME["receipt"])
    G = _th["g"]
    G2 = _th["g2"]
    INK = (0.13, 0.15, 0.14)
    GREY = (0.42, 0.44, 0.43)
    LINE = (0.80, 0.82, 0.81)
    SOFT = _th["soft"]
    ZEBRA = _th["zebra"]
    WHITE = (1, 1, 1)
    LT = _th["lt"]
    margin = 46
    RIGHT = W - margin

    def txt(x, y, s, size=10, color=INK):
        pg.insert_text((x, y), str(s), fontname=F, fontsize=size, color=color)

    def rtxt(xr, y, s, size=10, color=INK):
        w = fitz.get_text_length(str(s), fontname=F, fontsize=size)
        pg.insert_text((xr - w, y), str(s), fontname=F, fontsize=size, color=color)

    def rect(x0, y0, x1, y1, fill=None, color=None, width=0.6):
        pg.draw_rect(fitz.Rect(x0, y0, x1, y1), fill=fill, color=color, width=width)

    meta_by_kind = {
        "quotation": {"big": "PENAWARAN", "sub": "견적서 · Quotation", "form": "FORM 5.1-FIN-GL-HAC"},
        "receipt":   {"big": "KWITANSI", "sub": "영수증 · Official Receipt", "form": "FORM 5.2-FIN-GL-HAC"},
        "tax":       {"big": "FAKTUR PAJAK", "sub": "세금계산서 · Tax Invoice", "form": "FORM 5.3-FIN-GL-HAC"},
    }
    dm = meta_by_kind.get(kind, meta_by_kind["receipt"])
    big, sub, form_no = dm["big"], dm["sub"], dm["form"]

    # 금액
    dpp = float(inv.amount or 0)
    ppn = float(inv.ppn) if getattr(inv, "ppn", None) is not None else round(dpp * 0.11)
    total = float(inv.total) if getattr(inv, "total", None) is not None else dpp + ppn
    inv_no = inv.invoice_no or "-"
    today = date.today().isoformat()
    paid = bool(pay) or inv.status in ("paid", "confirmed")

    def clip(s, w, sz):
        s = str(s)
        while fitz.get_text_length(s, fontname=F, fontsize=sz) > w and len(s) > 2:
            s = s[:-2]
        return s

    def section(yy, label):
        rect(margin, yy, RIGHT, yy + 20, fill=G, width=0)
        txt(margin + 10, yy + 14, label, size=9.5, color=WHITE)
        return yy + 30

    def kv(yy, label, value, lw=178, sz=9.5, vcolor=INK):
        txt(margin + 8, yy + 11, label, size=sz, color=GREY)
        vx = margin + 8 + max(lw, fitz.get_text_length(label, fontname=F, fontsize=sz) + 12)
        txt(vx, yy + 11, ": " + str(value), size=sz, color=vcolor)
        return yy + 18

    # ── 헤더(로고 + 문서명 + FORM 번호 + 액센트 바) ──
    _logo = os.path.join(os.path.dirname(__file__), "assets", "glhac_logo.png")
    if os.path.exists(_logo):
        try:
            pg.insert_image(fitz.Rect(margin, 30, margin + 150, 84), filename=_logo, keep_proportion=True)
        except Exception:
            txt(margin, 58, "GL-HAC AI", size=20, color=G)
    else:
        txt(margin, 56, "GL-HAC AI", size=20, color=G)
        txt(margin, 71, "GL Halal Center", size=8.5, color=GREY)
    rtxt(RIGHT, 50, big, size=22, color=G)
    rtxt(RIGHT, 68, sub, size=9.5, color=GREY)
    rtxt(RIGHT, 82, form_no, size=7.5, color=GREY)
    # 종류 배지(좌측, 액센트색 필)
    _bl = {"quotation": "DRAFT · 견적", "tax": "PPN 11% · 과세",
           "receipt": ("LUNAS · 결제완료" if paid else "BELUM LUNAS · 미결제")}.get(kind, "")
    _bw = fitz.get_text_length(_bl, fontname=F, fontsize=8) + 18
    rect(margin, 92, margin + _bw, 109, fill=G, width=0)
    txt(margin + 9, 104, _bl, size=8, color=WHITE)
    rect(0, 116, W, 120, fill=G, width=0)
    # ── 대각선 워터마크(문서 종류) — 아주 옅게 ──
    try:
        _wf = fitz.Font("helv")
        _wtw = fitz.TextWriter(pg.rect)
        _wlen = _wf.text_length(_th["wm"], fontsize=66)
        _wtw.append(fitz.Point((W - _wlen) / 2, H / 2 + 18), _th["wm"], font=_wf, fontsize=66)
        _wtw.write_text(pg, morph=(fitz.Point(W / 2, H / 2), fitz.Matrix(-32)), color=G, opacity=0.06)
    except Exception:
        pass

    y = 140

    # =========================================================
    #  문서별 본문 — 양식·내용이 실제로 다름(공유 스켈레톤 아님)
    # =========================================================
    if kind == "receipt":
        # ── 영수증(KWITANSI): 단순 수령확인 서식(항목표·세금분해 없음) ──
        y = kv(y, "No. Kwitansi · 영수증번호", inv_no)
        y = kv(y, "Tanggal · 발행일", (str(pay.paid_at)[:10] if pay else today))
        y += 8
        rect(margin, y, RIGHT, y + 96, fill=WHITE, color=LINE, width=0.8)
        txt(margin + 12, y + 20, "Telah terima dari · ~로부터 수령", size=8.5, color=GREY)
        txt(margin + 12, y + 38, clip(c.company_name or "-", RIGHT - margin - 24, 13), size=13, color=INK)
        pg.draw_line((margin + 12, y + 52), (RIGHT - 12, y + 52), color=LINE, width=0.4)
        txt(margin + 12, y + 68, "Untuk pembayaran · 지급 사유", size=8.5, color=GREY)
        txt(margin + 12, y + 85, clip("%s (Ref: %s)" % (_service_label(inv.service_type), inv_no),
                                       RIGHT - margin - 24, 10.5), size=10.5, color=INK)
        y += 96 + 18
        # 금액 강조 박스 + Terbilang(금액 문자표기)
        rect(margin, y, RIGHT, y + 58, fill=SOFT, color=G, width=1.0)
        txt(margin + 12, y + 18, "Uang sejumlah · 금액", size=8.5, color=GREY)
        _amt = _idr(total)
        _amt_w = fitz.get_text_length(_amt, fontname=F, fontsize=21)
        rtxt(RIGHT - 14, y + 37, _amt, size=21, color=G)
        words = "# %s Rupiah #" % _terbilang(total).capitalize()
        txt(margin + 12, y + 40, clip(words, (RIGHT - 14 - _amt_w - 24) - (margin + 12), 10), size=10, color=INK)
        y += 58 + 22
        txt(margin + 4, y, "Metode Pembayaran · 결제방식  :  %s" % ((pay.method if pay else None) or "Transfer"),
            size=9.5, color=INK)
        txt(margin + 4, y + 17, "Tgl Pembayaran · 결제일  :  %s" % (str(pay.paid_at)[:16] if pay else today),
            size=9.5, color=INK)
        if paid:
            sx = RIGHT - 154
            rect(sx, y - 8, sx + 154, y + 44, color=G, width=1.8)
            _lc = fitz.get_text_length("LUNAS", fontname=F, fontsize=23)
            txt(sx + (154 - _lc) / 2, y + 26, "LUNAS", size=23, color=G)

    elif kind == "tax":
        # ── 세금계산서(FAKTUR PAJAK): 판매자/구매자 NPWP + 과세표준·PPN ──
        y = kv(y, "Kode & No. Seri Faktur · 코드/일련번호",
               "010.000-%s" % ((inv_no.replace("INV-", "") or "00000000")))
        y = kv(y, "Tanggal · 발행일", today)
        y += 6
        y = section(y, "Pengusaha Kena Pajak / Penjual · 판매자(과세사업자)")
        y = kv(y, "Nama · 상호", "GL-HAC AI (Lembaga Sertifikasi Halal)")
        y = kv(y, "Alamat · 주소", "Jakarta, Indonesia")
        y = kv(y, "NPWP", "00.000.000.0-000.000")
        y += 8
        y = section(y, "Pembeli Jasa Kena Pajak · 구매자")
        y = kv(y, "Nama · 상호", clip(c.company_name or "-", 320, 9.5))
        y = kv(y, "Alamat · 주소", clip(c.address or c.factory_address or "-", 320, 9.5))
        y = kv(y, "NPWP / NIB", getattr(c, "npwp", None) or c.nib or "-")
        y += 8
        y = section(y, "Rincian Pajak · 과세 내역")
        for lab, val, bold in [
            ("Harga Jual / Penggantian · 공급가액", dpp, False),
            ("Dikurangi Potongan Harga · 할인", 0, False),
            ("Dasar Pengenaan Pajak (DPP) · 과세표준", dpp, False),
            ("PPN = 11% × DPP · 부가가치세", ppn, False),
            ("Jumlah · 합계", total, True)]:
            if bold:
                rect(margin, y, RIGHT, y + 24, fill=SOFT, color=G, width=0.8)
                txt(margin + 8, y + 16, lab, size=10.5, color=G)
                rtxt(RIGHT - 10, y + 16, _idr(val), size=12, color=G)
                y += 28
            else:
                txt(margin + 8, y + 13, lab, size=9.5, color=INK)
                rtxt(RIGHT - 10, y + 13, _idr(val), size=10, color=INK)
                pg.draw_line((margin, y + 19), (RIGHT, y + 19), color=LINE, width=0.4)
                y += 22
        y += 12
        txt(margin, y, clip("Faktur Pajak ini sesuai ketentuan Undang-Undang PPN Republik Indonesia.", RIGHT - margin, 8), size=8, color=GREY)
        txt(margin, y + 13, "본 세금계산서는 인도네시아 부가가치세법에 따라 발행되었습니다.", size=8, color=GREY)

    else:
        # ── 견적서(PENAWARAN): 서비스 항목표 + 약관 + 입금계좌 ──
        y = kv(y, "No. Penawaran · 견적번호", inv_no)
        y = kv(y, "Tanggal · 발행일", today)
        y = kv(y, "Berlaku s/d · 유효기간", (date.today() + timedelta(days=30)).isoformat(), vcolor=G)
        y += 6
        colw = (W - 2 * margin - 16) / 2
        for x, head, lines in [
            (margin, "Kepada · 수신 (구매자)",
             [(c.company_name or "-", 9.5, INK), ("NIB: %s" % (c.nib or "-"), 8.5, GREY),
              (c.address or c.factory_address or "-", 8.5, GREY)]),
            (margin + colw + 16, "Dari · 발신 (공급자)",
             [("GL-HAC AI (LSH)", 9.5, INK), ("Lembaga Sertifikasi Halal", 8.5, GREY),
              ("Jakarta · halal@glhac.ai", 8.5, GREY)])]:
            rect(x, y, x + colw, y + 66, fill=WHITE, color=LINE, width=0.6)
            rect(x, y, x + colw, y + 17, fill=SOFT, color=LINE, width=0.6)
            txt(x + 9, y + 12, head, size=8, color=G)
            yy = y + 32
            for s, sz, col in lines:
                txt(x + 9, yy, clip(s, colw - 18, sz), size=sz, color=col)
                yy += 14
        y += 66 + 16
        y = section(y, "Rincian Layanan · 서비스 내역")
        xItem, xQty, xPr = margin, margin + 300, margin + 362
        rh = 20
        rect(margin, y, RIGHT, y + rh, fill=G2, width=0)
        txt(xItem + 6, y + 14, "Uraian · 항목", size=8.5, color=WHITE)
        rtxt(xPr - 8, y + 14, "Qty", size=8.5, color=WHITE)
        rtxt(RIGHT - 8, y + 14, "Jumlah", size=8.5, color=WHITE)
        y += rh
        rows = []
        for it in (items or []):
            rows.append((_service_label(it.get("name"), short=True), float(it.get("qty") or 0), float(it.get("amount") or 0)))
        if not rows:
            rows = [(_service_label(inv.service_type, short=True), 1.0, dpp)]
        for i2, (nm, qty, amt) in enumerate(rows):
            if i2 % 2:
                rect(margin, y, RIGHT, y + rh, fill=ZEBRA, width=0)
            txt(xItem + 6, y + 14, clip(nm, xQty - xItem - 12, 9.5), size=9.5)
            rtxt(xPr - 8, y + 14, format(qty, ",g"), size=9.5)
            rtxt(RIGHT - 8, y + 14, _idr(amt), size=9.5)
            pg.draw_line((margin, y + rh), (RIGHT, y + rh), color=LINE, width=0.4)
            y += rh
        rect(margin, y - rh * (len(rows) + 1), RIGHT, y, color=LINE, width=0.6)
        y += 12
        bx = xQty - 60
        for lab, val, bold in [("Subtotal", dpp, False), ("PPN 11%", ppn, False), ("TOTAL", total, True)]:
            if bold:
                rect(bx, y, RIGHT, y + 24, fill=SOFT, color=G, width=0.8)
                txt(bx + 10, y + 16, lab, size=11, color=G)
                rtxt(RIGHT - 10, y + 16, _idr(val), size=12, color=G)
                y += 28
            else:
                txt(bx + 10, y + 12, lab, size=9.5, color=GREY)
                rtxt(RIGHT - 10, y + 12, _idr(val), size=10, color=INK)
                y += 17
        y += 12
        y = section(y, "Syarat & Ketentuan · 약관")
        for ln in ["1. Penawaran berlaku 30 hari sejak tanggal terbit · 발행일로부터 30일간 유효.",
                   "2. Harga sudah termasuk PPN 11% · 가격은 부가세 11% 포함.",
                   "3. Pembayaran via transfer ke rekening di bawah · 아래 계좌로 이체."]:
            txt(margin + 4, y + 10, clip(ln, RIGHT - margin - 8, 8.5), size=8.5, color=INK)
            y += 15
        y += 4
        rect(margin, y, RIGHT, y + 36, fill=SOFT, color=LINE, width=0.6)
        txt(margin + 10, y + 14, "Rekening Pembayaran · 입금계좌", size=8, color=G)
        txt(margin + 10, y + 29, "Bank BCA  ·  a/c 123-456-7890  ·  a/n GL-HAC AI (LSH)", size=9.5, color=INK)
        y += 36

    # ── 서명란(문서별 라벨) ──
    sigy = H - 142
    sigx = W - margin - 200
    siglabel = {"receipt": "Penerima Pembayaran · 수령인",
                "tax": "Penjual · 판매자 (PKP)",
                "quotation": "Hormat kami · GL-HAC AI"}.get(kind, "GL-HAC AI")
    txt(sigx, sigy, "Jakarta, %s" % today, size=9, color=GREY)
    txt(sigx, sigy + 15, siglabel, size=9.5, color=INK)
    pg.draw_line((sigx, sigy + 56), (sigx + 190, sigy + 56), color=LINE, width=0.6)
    txt(sigx, sigy + 70, "Tanda Tangan & Cap · 서명·직인", size=7.5, color=GREY)
    if kind == "tax":
        txt(sigx, sigy + 83, "NPWP: 00.000.000.0-000.000", size=7.5, color=GREY)

    # ── 공식 푸터(GL HAC 문서관리 밴드 — Contract.pdf 표준) ──
    fy = H - 38
    pg.draw_line((margin, fy), (RIGHT, fy), color=G, width=0.8)
    txt(margin, fy + 13, "GL Halal Center · www.glhac.com", size=7, color=GREY)
    rtxt(RIGHT, fy + 13, "%s · Page 1/1" % form_no, size=7, color=GREY)

    return doc.tobytes()


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
    pdf = _render_finance_pdf("receipt", inv, c, pay)
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
    pdf = _render_finance_pdf("tax", inv, c, pay)
    fn = "faktur_%s.pdf" % (inv.invoice_no or invoice_id[:8])
    return Response(content=pdf, media_type="application/pdf",
                    headers={"Content-Disposition": "attachment; filename*=UTF-8''" + quote(fn)})


@app.get("/invoices/{invoice_id}/quotation.pdf")
def invoice_quotation_pdf(invoice_id: str, user=Depends(auth.get_current_user),
                          db: Session = Depends(get_db)):
    """견적서(Penawaran/Quotation) PDF — 결제 前 문서. 라인아이템·소계·PPN·총액·유효기간.
    스키마 무변경 — 라인아이템은 invoice.line_items 이벤트에서 병합. _render_pdf_rich 재사용."""
    from fastapi.responses import Response
    from urllib.parse import quote
    inv, c, _pay = _invoice_ctx(db, invoice_id, user)
    items = _invoice_line_items(db, inv.case_id, invoice_id)
    pdf = _render_finance_pdf("quotation", inv, c, None, items)
    fn = "quotation_%s.pdf" % (inv.invoice_no or invoice_id[:8])
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


def _revoke_guards(db, case_id):
    """철회 사전조건 — 활성/정지 인증서 존재. 재발급으로 다중 cert 가능 → active/suspended 우선 선택."""
    cert = (db.query(models.HalalCertificate)
            .filter(models.HalalCertificate.case_id == case_id,
                    models.HalalCertificate.status.in_(("active", "suspended")))
            .first())
    if not cert:
        exists = db.query(models.HalalCertificate).filter_by(case_id=case_id).first()
        if not exists:
            raise HTTPException(404, {"code": "CERT_NOT_FOUND"})
        raise HTTPException(409, {"code": "BAD_CERT_STATE", "have": exists.status})
    return cert


def _do_revoke_certificate(db, c, user, reason=None):
    """실제 인증서 철회 실행(가드 통과 후). user=실행자(2인승인 시 승인자=admin). 되돌릴 수 없음·통지."""
    cert = _revoke_guards(db, c.case_id)
    cert.status = "withdrawn"
    sm.record_event(db, c, c.status, c.status, "certificate.revoke", user["role"], user["uid"],
                    {"certificate_no": cert.certificate_no, "reason": (reason or "").strip() or None})
    _notify(db, c, "certificate_revoked", "인증서 철회",
            "%s — 할랄 인증서가 철회되었습니다." % (c.company_name or ""),
            channels=["inapp", "sms"], role="applicant")
    db.commit()
    return {"certificate_no": cert.certificate_no, "status": "withdrawn", "reason": (reason or "").strip() or None}


@app.post("/cases/{case_id}/certificate/revoke")
def revoke_certificate(case_id: str, body: schemas.CertStatusReq,
                       user=Depends(rbac.require_action("certificate.revoke")),
                       db: Session = Depends(get_db)):
    """인증서 철회 요청(maker=operator) — 2인 승인(§4.3). 가드 프리체크 후 승인요청 생성.
    실제 철회는 승인자(admin)가 /approvals/{id}/approve 로 실행."""
    c = _get_case(db, case_id, user)
    _revoke_guards(db, case_id)   # 철회 가능 상태 사전 검증
    return _create_approval(db, "certificate.revoke", c, user, (body.reason or "").strip())


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
    # 법적효력 Phase 1 — PSrE 공인 전자서명 훅(미설정 시 no-op). 내부 HMAC 서명(row)은 위에서 그대로 병행.
    if _psre_config()["configured"]:
        _psre_certify(db, c, "certificate", user["uid"], cert.certificate_no or str(cert.id), ph)
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


# ---------- SEHATI Layer B — SIHALAL 제출 커넥터 (스키마 무변경) ----------
# 정직성(중요): 실제 SIHALAL 라이브 제출은 LP3H 등록·API 자격증명(GLHAC_SIHALAL_*)이 있어야 가능하다.
# 자격증명 미설정 시 제출하는 '척' 위조하지 않고 no_credentials로 정직 폴백한다 — notify.py 채널
# 크리덴셜 게이트와 완전 동일 패턴(_send_kakao / channel_status의 kakao_ok = URL+KEY 참조).
# 실 대행 API 필드 매핑은 공식 스펙(lph-api.halal.go.id) 확보 전이므로 범용/설정형 payload로 제출한다
# (Kakao 커넥터가 senderKey·templateCode를 설정형으로 둔 것과 동일). 스펙·자격증명 확보 후 최종화.
_SIHALAL_SUBMIT_EVENT = "document_package_submitted"   # §10.1 IntegrationEvent 표준 이벤트 재사용


def _sihalal_submit_config():
    """SIHALAL 제출 자격증명 게이트 — 값은 노출하지 않고 configured(bool)만 판정.
    GLHAC_SIHALAL_API_URL + (GLHAC_SIHALAL_TOKEN 또는 GLHAC_SIHALAL_API_KEY) 완비 시에만 실제 제출.
    (notify.channel_status의 kakao_ok = GLHAC_KAKAO_API_URL+KEY 게이트와 동일 패턴.)"""
    e = os.environ.get
    url = e("GLHAC_SIHALAL_API_URL")
    token = e("GLHAC_SIHALAL_TOKEN") or e("GLHAC_SIHALAL_API_KEY")
    return {"configured": bool(url and token), "url": url, "token": token}


def _sihalal_submit_key(case_id):
    return "%s:sihalal_submit" % case_id


def _sihalal_existing_submission(db, case_id):
    """이미 제출된 IntegrationEvent(document_package_submitted·status=submitted) — 멱등 조회."""
    return (db.query(models.IntegrationEvent)
            .filter_by(case_id=case_id, event_type=_SIHALAL_SUBMIT_EVENT, status="submitted")
            .order_by(models.IntegrationEvent.created_at.desc()).first())


def _build_selfdeclare_submission(db, case):
    """SEHATI self-declare 제출 패키지 조립(스키마 무변경 조인) — SIHALAL 제출 payload.
    사업자/NIB · 제품 · 원재료(positive-list 상태 포함) · 자기선언서 참조(self_declaration) ·
    pendamping 검증 · KFPH ketetapan(할랄 판정) 참조. dict 반환."""
    rep = case.responsible_person or (case.profile_ext or {}).get("pic_name") or ""
    prods = db.query(models.Product).filter_by(case_id=case.case_id).all()
    mats = db.query(models.Material).filter_by(case_id=case.case_id).all()
    sd_doc = (db.query(models.GeneratedDocument)
              .filter_by(case_id=case.case_id, doc_type="self_declaration")
              .order_by(models.GeneratedDocument.version.desc()).first())
    pa = (db.query(models.PendampingAssignment).filter_by(case_id=case.case_id)
          .order_by(models.PendampingAssignment.assignment_id.desc()).first())
    kfph_ev = _kfph_approve_event(db, case.case_id)
    return {
        "scheme": "self_declare",
        "business": {"company_name": case.company_name, "nib": case.nib,
                     "address": case.address, "responsible_person": rep},
        "products": [{"product_id": p.product_id, "name": p.name, "category": p.category}
                     for p in prods],
        "materials": [{"name": m.name, "screen_status": m.screen_status,
                       "matched_uid": m.matched_uid,
                       "positive_listed": sm.positive_listed(m)} for m in mats],
        "self_declaration": ({"gen_doc_id": sd_doc.gen_doc_id, "version": sd_doc.version,
                              "status": sd_doc.status} if sd_doc else None),
        "pendamping": ({"name": _pendamping_name(db, case), "decision": pa.decision,
                        "note": pa.note,
                        "verified_at": pa.verified_at.isoformat() if pa.verified_at else None}
                       if pa else None),
        "kfph_ketetapan": {"ref_no": _kfph_ref_no(case.case_id), "approved": bool(kfph_ev),
                           "decided_by": (kfph_ev.actor_id if kfph_ev else None),
                           "decided_at": (kfph_ev.created_at.isoformat()
                                          if kfph_ev and kfph_ev.created_at else None)},
    }


@app.post("/cases/{case_id}/sihalal/submit")
def sihalal_submit(case_id: str,
                   user=Depends(auth.require_roles("pendamping_pph", "operator", "admin")),
                   db: Session = Depends(get_db)):
    """SEHATI self-declare 제출 — SIHALAL 커넥터. self_declare + KFPH 승인(committee.approve) 전용.
    자격증명(GLHAC_SIHALAL_*) 설정 시 실제 제출, 미설정 시 424 no_credentials 정직 폴백(제출의도 기록).
    멱등: 이미 제출된 경우 기존 결과 반환(재제출 금지)."""
    from fastapi.responses import JSONResponse
    c = _get_case(db, case_id, user)
    # self_declare 아니면 409 NOT_SELF_DECLARE, KFPH 미승인이면 409 NOT_APPROVED (KFPH 결정문 가드 재사용)
    _kfph_ketetapan_guard(db, c)

    # 멱등 — 이미 제출됨
    dup = _sihalal_existing_submission(db, case_id)
    if dup:
        return {"ok": True, "submitted": True, "idempotent": True,
                "submission_id": dup.external_id, "status": dup.status}

    cfg = _sihalal_submit_config()
    submission = _build_selfdeclare_submission(db, c)

    # 미설정 → 정직: 제출하지 않되 '제출 의도'는 감사 기록(WorkflowEvent)하고 424 반환
    if not cfg["configured"]:
        sm.record_event(db, c, c.status, c.status, "sihalal.submit_intent",
                        user["role"], user["uid"],
                        {"reason": "no_credentials", "scheme": "self_declare"})
        db.commit()
        obs.inc("glhac_sihalal_submit_total", {"result": "no_credentials"})
        return JSONResponse(status_code=424, content={
            "ok": False, "reason": "no_credentials", "code": "SIHALAL_NOT_CONFIGURED",
            "hint": "SIHALAL 미연동 — LP3H 등록·자격증명(GLHAC_SIHALAL_API_URL/TOKEN) 설정 후 제출 가능"})

    # 설정됨 → 실제 제출(범용 payload, 예외 graceful)
    submission_id = None
    try:
        import httpx
        r = httpx.post(cfg["url"], timeout=15,
                       headers={"Authorization": "Bearer " + cfg["token"],
                                "Content-Type": "application/json"},
                       json={"case_id": case_id, "scheme": "self_declare", "package": submission})
        if not (200 <= r.status_code < 300):
            sm.record_event(db, c, c.status, c.status, "sihalal.submit_intent",
                            user["role"], user["uid"],
                            {"reason": "http_%d" % r.status_code, "scheme": "self_declare"})
            db.commit()
            obs.inc("glhac_sihalal_submit_total", {"result": "http_error"})
            return JSONResponse(status_code=502, content={
                "ok": False, "reason": "sihalal_http_%d" % r.status_code,
                "code": "SIHALAL_SUBMIT_FAILED"})
        try:
            body = r.json()
        except Exception:  # noqa: BLE001
            body = {}
        submission_id = (body.get("submission_id") or body.get("id")
                         or body.get("registration_no") or None)
    except Exception as e:  # noqa: BLE001
        sm.record_event(db, c, c.status, c.status, "sihalal.submit_intent",
                        user["role"], user["uid"],
                        {"reason": "error:%s" % str(e)[:60], "scheme": "self_declare"})
        db.commit()
        obs.inc("glhac_sihalal_submit_total", {"result": "error"})
        return JSONResponse(status_code=502, content={
            "ok": False, "reason": "sihalal_error", "code": "SIHALAL_SUBMIT_FAILED"})

    # 성공 → WorkflowEvent(sihalal.submitted) + IntegrationEvent(멱등키=case_id:sihalal_submit)
    import json as _json
    import hashlib as _hl
    req_hash = _hl.sha256(_json.dumps(submission, sort_keys=True, ensure_ascii=False)
                          .encode()).hexdigest()
    sm.record_event(db, c, c.status, c.status, "sihalal.submitted", user["role"], user["uid"],
                    {"submission_id": submission_id, "scheme": "self_declare"})
    ev = models.IntegrationEvent(provider="sihalal", event_type=_SIHALAL_SUBMIT_EVENT,
                                 external_id=submission_id,
                                 idempotency_key=_sihalal_submit_key(case_id), case_id=case_id,
                                 payload={"submission_id": submission_id, "package": submission},
                                 request_hash=req_hash, status="submitted")
    db.add(ev)
    db.commit()
    obs.inc("glhac_sihalal_submit_total", {"result": "submitted"})
    return {"ok": True, "submitted": True, "idempotent": False,
            "submission_id": submission_id, "status": "submitted"}


@app.post("/cases/{case_id}/sihalal/import-number")
def sihalal_import_number(case_id: str, body: schemas.SihalalImportNumberReq,
                          user=Depends(auth.require_roles("operator", "admin")),
                          db: Session = Depends(get_db)):
    """공식 BPJPH 할랄번호(No. Ketetapan Halal) 수동/콜백 import — Phase0↔Layer B 브릿지.
    certificate_number_imported 이벤트를 기록하면 _official_bpjph_no가 이를 읽어 인증서 표기가
    자동으로 '공식 라벨'(halal_no_is_official=true)로 전환된다. 번호 형식검증은 최소(비어있지 않음)."""
    c = _get_case(db, case_id, user)
    official = (body.official_no or "").strip()
    if len(official) < 4:
        raise HTTPException(422, {"code": "BAD_OFFICIAL_NO",
                                  "hint": "공식 할랄번호(No. Ketetapan Halal)를 확인하세요."})
    key = "%s:certno:%s" % (case_id, official)
    dup = db.query(models.IntegrationEvent).filter_by(idempotency_key=key).first()
    if dup:
        return {"ok": True, "idempotent": True, "official_no": official,
                "halal_no_is_official": bool(_official_bpjph_no(db, case_id))}
    import json as _json
    import hashlib as _hl
    payload = {"certificate_number": official, "official_no": official,
               "source": (body.source or "manual")}
    req_hash = _hl.sha256(_json.dumps(payload, sort_keys=True).encode()).hexdigest()
    ev = models.IntegrationEvent(provider="sihalal", event_type="certificate_number_imported",
                                 external_id=official, idempotency_key=key, case_id=case_id,
                                 payload=payload, request_hash=req_hash, status="received")
    db.add(ev)
    _audit(db, user, "sihalal.import_number", "case", case_id, case_id,
           meta={"official_no": official}, commit=False)
    db.commit()
    obs.inc("glhac_integration_event_total", {"type": "certificate_number_imported"})
    return {"ok": True, "idempotent": False, "official_no": official,
            "halal_no_is_official": bool(_official_bpjph_no(db, case_id))}


@app.get("/cases/{case_id}/sihalal/status")
def sihalal_status(case_id: str, user=Depends(auth.get_current_user),
                   db: Session = Depends(get_db)):
    """SIHALAL 제출/공식번호 상태 — configured·submitted·submission_id·official_no·status(정직표기)."""
    _get_case(db, case_id, user)
    cfg = _sihalal_submit_config()
    sub = _sihalal_existing_submission(db, case_id)
    official = _official_bpjph_no(db, case_id)
    status = ("official_received" if official
              else ("submitted" if sub
                    else ("ready" if cfg["configured"] else "not_configured")))
    return {"configured": cfg["configured"], "submitted": bool(sub),
            "submission_id": (sub.external_id if sub else None),
            "official_no": official, "halal_no_is_official": bool(official),
            "status": status}


@app.get("/admin/sihalal-status")
def admin_sihalal_status(user=Depends(auth.require_roles("operator"))):
    """SIHALAL 커넥터 설정 상태(읽기전용·스키마 무변경) — 시크릿 값 미노출·bool만.
    notify /admin/notify-channels(channel_status)와 동일한 정직표기 방식."""
    cfg = _sihalal_submit_config()
    return {"sihalal": {"configured": cfg["configured"], "implemented": True,
                        "status": "connected" if cfg["configured"] else "unset",
                        "note": "LP3H 등록·자격증명(GLHAC_SIHALAL_API_URL/TOKEN) 필요"}}


@app.get("/admin/psre-status")
def admin_psre_status(user=Depends(auth.require_roles("operator"))):
    """PSrE 공인 전자서명 커넥터 설정 상태(읽기전용·스키마 무변경) — 시크릿 미노출·configured(bool)+provider만.
    admin/sihalal-status·notify channel_status와 동일한 정직표기. 미설정 시 기존 내부 HMAC 서명으로 폴백."""
    cfg = _psre_config()
    return {"psre": {"configured": cfg["configured"], "provider": cfg["provider"],
                     "implemented": True,
                     "status": "connected" if cfg["configured"] else "unset",
                     "note": ("벤더(PrivyID/VIDA/Peruri/Tilaka) 계약·자격증명"
                              "(GLHAC_PSRE_PROVIDER/API_URL/TOKEN) 필요 — 미설정 시 내부 무결성 서명 폴백")}}


@app.get("/admin/tsa-status")
def admin_tsa_status(user=Depends(auth.require_roles("operator"))):
    """법적효력 P3d — TSA 신뢰 타임스탬프 커넥터 설정 상태(읽기전용·스키마 무변경·시크릿 미노출).
    미설정 시 서명/발급 이벤트는 내부 시각만 사용(신뢰 타임스탬프 미연동) — 기존 동작 완전 불변."""
    cfg = _tsa_config()
    return {"tsa": {"configured": cfg["configured"], "implemented": True,
                    "status": "connected" if cfg["configured"] else "unset",
                    "note": ("RFC3161 TSA 자격증명(GLHAC_TSA_URL, 선택 GLHAC_TSA_TOKEN) 필요 — "
                             "미설정 시 신뢰 타임스탬프 미연동(내부 시각만)")}}


# ── 법적효력 Phase 4c — 플랫폼 컴플라이언스 상태 대시보드 (admin·읽기전용·스키마 무변경·시크릿 미노출) ──
@app.get("/admin/compliance")
def admin_compliance(user=Depends(auth.require_roles()), db: Session = Depends(get_db)):
    """플랫폼 컴플라이언스 포스처를 정직 집약한다(값이 아닌 '상태'만 — 시크릿 미노출).
    각 항목 {key, label, label_id, status(met|pending|not_configured|action_required), detail, detail_id}.
    설정형 커넥터(PSrE/SIHALAL/TSA)는 자격증명 미설정 시 not_configured로 표기(위조하지 않음)."""
    e = os.environ.get
    items = []

    def add(key, label, label_id, status, detail, detail_id):
        items.append({"key": key, "label": label, "label_id": label_id,
                      "status": status, "detail": detail, "detail_id": detail_id})

    # PSE 등록(Kominfo 전자시스템 사업자 등록) — 등록번호 보유 시 met
    pse = bool(e("GLHAC_PSE_REG_NO"))
    add("pse_registration", "PSE 등록(Kominfo)", "Pendaftaran PSE (Kominfo)",
        "met" if pse else "action_required",
        ("전자시스템 사업자(PSE) 등록번호 보유" if pse
         else "PSE 등록번호(GLHAC_PSE_REG_NO) 미설정 — Kominfo PSE 등록 필요"),
        ("Nomor registrasi PSE tersedia" if pse
         else "GLHAC_PSE_REG_NO belum diatur — perlu registrasi PSE Kominfo"))

    # UU PDP — PII 필드 암호화(AES-256-GCM 봉투암호화)
    enc = e("GLHAC_ENCRYPTION", "0") == "1"
    add("pii_encryption", "UU PDP · PII 암호화", "UU PDP · Enkripsi PII",
        "met" if enc else "action_required",
        ("PII 컬럼 AES-256-GCM 봉투암호화 적용(GLHAC_ENCRYPTION=1)" if enc
         else "PII 암호화 미적용 — 프로덕션은 GLHAC_ENCRYPTION=1 + 전용 키(GLHAC_ENC_KEY) 권장"),
        ("Enkripsi kolom PII AES-256-GCM aktif" if enc
         else "Enkripsi PII nonaktif — sarankan GLHAC_ENCRYPTION=1 di produksi"))

    # 보안 응답 헤더(P4a) — 미들웨어 등록됨 = met
    add("security_headers", "보안 응답 헤더", "Header Keamanan HTTP",
        "met" if _SECURITY_HEADERS_ACTIVE else "action_required",
        "X-Frame-Options·X-Content-Type-Options·Referrer-Policy·CSP 전 응답 적용(HSTS는 HTTPS)",
        "X-Frame-Options·X-Content-Type-Options·Referrer-Policy·CSP diterapkan (HSTS via HTTPS)")

    # 레이트리밋(P4b) — write 엔드포인트 per-actor 분당 한도
    _rlm = _rl_write_max()
    add("rate_limiting", "레이트리밋", "Pembatasan Laju (Rate Limit)",
        "met" if _rlm > 0 else "action_required",
        "로그인 전용 제한 + write 엔드포인트 per-actor 분당 %d회(인메모리·단일프로세스)" % _rlm,
        "Batas login + %d/menit per-aktor pada endpoint write (in-memory, single-process)" % _rlm)

    # PSrE 공인 전자서명 연동(설정형 커넥터)
    psre = _psre_config()
    add("psre_esign", "PSrE 공인 전자서명 연동", "Integrasi Tanda Tangan Elektronik PSrE",
        "met" if psre["configured"] else "not_configured",
        ("PSrE 연동 설정됨" + (" · " + str(psre["provider"]) if psre.get("provider") else "")
         if psre["configured"]
         else "미설정 — 벤더(PrivyID/VIDA/Peruri/Tilaka) 자격증명 확보 시 연동(미설정 시 내부 무결성 서명 폴백)"),
        ("Integrasi PSrE dikonfigurasi" if psre["configured"]
         else "Belum dikonfigurasi — fallback tanda tangan integritas internal"))

    # SIHALAL 제출 연동(설정형 커넥터)
    sih = _sihalal_submit_config()
    add("sihalal_integration", "SIHALAL 제출 연동", "Integrasi Pengajuan SIHALAL",
        "met" if sih["configured"] else "not_configured",
        ("SIHALAL 제출 자격증명 설정됨" if sih["configured"]
         else "미설정 — LP3H 등록·자격증명(GLHAC_SIHALAL_API_URL/TOKEN) 확보 시 실제 제출"),
        ("Kredensial pengajuan SIHALAL dikonfigurasi" if sih["configured"]
         else "Belum dikonfigurasi — perlu registrasi LP3H & kredensial"))

    # TSA 신뢰 타임스탬프 연동(설정형 커넥터)
    tsa = _tsa_config()
    add("tsa_timestamp", "TSA 신뢰 타임스탬프", "Stempel Waktu Tepercaya (TSA)",
        "met" if tsa["configured"] else "not_configured",
        ("RFC3161 TSA 연동 설정됨" if tsa["configured"]
         else "미설정 — GLHAC_TSA_URL 확보 시 연동(미설정 시 내부 시각만 사용)"),
        ("Integrasi TSA RFC3161 dikonfigurasi" if tsa["configured"]
         else "Belum dikonfigurasi — memakai waktu internal saja"))

    # 감사 체인(WorkflowEvent + AuditLog) — 항상 met, 실제 건수 병기(정직)
    try:
        wf_n = db.query(models.WorkflowEvent).count()
    except Exception:  # noqa: BLE001
        wf_n = None
    try:
        al_n = db.query(models.AuditLog).count()
    except Exception:  # noqa: BLE001
        al_n = None
    add("audit_chain", "감사 체인", "Rantai Audit (Audit Trail)", "met",
        "WorkflowEvent(%s)·AuditLog(%s) 불변 이력 기록" % (
            "?" if wf_n is None else wf_n, "?" if al_n is None else al_n),
        "Riwayat WorkflowEvent(%s) & AuditLog(%s)" % (
            "?" if wf_n is None else wf_n, "?" if al_n is None else al_n))

    # 백업 — 현재 수동 .bak 스냅샷만(자동 백업 파이프라인 미구성)
    add("backup", "백업·복구", "Cadangan & Pemulihan", "action_required",
        "현재 수동 .bak 스냅샷만 — 자동 백업·복구 파이프라인 구성 필요",
        "Hanya snapshot .bak manual — perlu pipeline pencadangan otomatis")

    # LPH 인정(LPH 인정번호 보유 시 met)
    lph = bool(e("GLHAC_LPH_ACCRED"))
    add("lph_accreditation", "LPH 인정", "Akreditasi LPH",
        "met" if lph else "action_required",
        ("LPH 인정번호 보유(GLHAC_LPH_ACCRED)" if lph
         else "LPH 인정번호(GLHAC_LPH_ACCRED) 미설정 — BPJPH/KAN 인정 필요"),
        ("Nomor akreditasi LPH tersedia" if lph
         else "GLHAC_LPH_ACCRED belum diatur — perlu akreditasi BPJPH/KAN"))

    # 기본 시크릿 강제(enforce_secret) — 커스텀 시크릿이면 met, 기본값이면 action_required
    default_secret = auth.secret_is_default()
    add("app_secret", "앱 시크릿", "Rahasia Aplikasi (App Secret)",
        "met" if not default_secret else "action_required",
        ("커스텀 GLHAC_SECRET 설정됨 — 프로덕션 부팅 허용" if not default_secret
         else "기본 시크릿 사용 중 — 프로덕션 부팅은 차단됨(enforce_secret). GLHAC_SECRET 설정 필요"),
        ("GLHAC_SECRET kustom telah diatur" if not default_secret
         else "Memakai rahasia bawaan — boot produksi diblokir; atur GLHAC_SECRET"))

    order = ["met", "pending", "not_configured", "action_required"]
    summary = {s: sum(1 for it in items if it["status"] == s) for s in order}
    return {"generated_at": datetime.utcnow().isoformat(),
            "dev_mode": auth.dev_mode(),
            "summary": summary, "total": len(items), "items": items}


@app.post("/cases/{case_id}/certificate/change-impact")
def change_impact(case_id: str, body: schemas.ChangeImpactReq,
                  user=Depends(auth.get_current_user), db: Session = Depends(get_db)):
    """변경 영향도 — 설계 8.3 가중식(간이)."""
    c = _get_case(db, case_id, user)
    _auto_advance(db, c, "change_impact", user, "change_impact.auto")   # P2 훅: 사후관리→변경영향 상태
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
    frm = c.status   # P3e: 상태 선세팅 전 실제 from_status 캡처(fatwa_review→fatwa_approved 전이 감사 정확화)
    if c.status == "fatwa_review" and sm.allowed(c.status, "fatwa_approved"):
        c.status = "fatwa_approved"
    sm.record_event(db, c, frm, c.status, "fatwa.final_approve", user["role"], user["uid"], {})
    _notify(db, c, "fatwa_approved", channels=["inapp"], role="applicant",
            msg=("fatwa_approved", {"company": c.company_name or ""}))
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


@app.post("/cases/{case_id}/fatwa/return-to-auditor")
def fatwa_return_to_auditor(case_id: str, body: schemas.FatwaReturnReq,
                            user=Depends(auth.require_roles("fatwa_liaison", "operator")),
                            db: Session = Depends(get_db)):
    """P1-#7: 파트와/현장심사에서 문제 발견 시 케이스를 오디터 심사보고서 단계(audit_closed)로
    되돌려 담당 오디터에게 왕복 전달. fatwaDecide('rejected')(fatwa_status 라벨만)와 별개 —
    이건 워크플로 전이. 유효 전이면 가드 경유, 아니면 ops.reject 패턴처럼 status 직접 set + 사유."""
    c = _get_case(db, case_id, user)
    reason = (body.reason or "").strip()
    if not reason:
        raise HTTPException(422, {"code": "REASON_REQUIRED"})
    # P2(G2): 반려 대상 분기 — 기본=오디터 재작업(audit_closed), 선택=클라이언트 보완(supplementation_required,
    # 상태머신 fatwa_review→supplementation_required 전이 활성화)
    target = (body.target or "audit_closed").strip()
    if target not in ("audit_closed", "supplementation_required"):
        raise HTTPException(422, {"code": "BAD_TARGET", "allowed": ["audit_closed", "supplementation_required"]})
    frm = c.status
    ok, _blk = sm.can_transition(db, c, target)
    if ok:                    # 유효 전이면 가드 경유(side-effects 반영)
        sm.apply_side_effects(c, target)
    c.status = target         # 불가하면 직접 set(ops.reject 패턴 — dead-end 회피)
    sm.record_event(db, c, frm, target, "fatwa.returned_to_auditor", user["role"], user["uid"],
                    {"reason": reason, "target": target})
    if target == "supplementation_required":
        _notify(db, c, "fatwa.returned_supplement", "파트와 반려 — 보완자료 제출 요청", body=reason, role="applicant")
    else:
        _notify(db, c, "fatwa.returned_to_auditor", "파트와 반려 — 현장심사 보고서 보완 요청",
                body=reason, role="auditor")
    db.commit()
    return {"ok": True, "from": frm, "to": target, "reason": reason}


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


# P1-#1: 라인아이템은 스키마 무변경으로 WorkflowEvent(action="invoice.line_items") payload에
# latest-wins 저장. Invoice 테이블에 JSON/notes 컬럼이 없어 이벤트 로그로 부착·조회 병합.
def _norm_line_items(raw):
    """입력 라인아이템 정규화 → [{name, qty, unit_price, amount}]. amount 미지정 시 qty*unit_price."""
    out = []
    for it in (raw or []):
        if not isinstance(it, dict):
            continue
        name = str(it.get("name") or "").strip()
        try:
            qty = float(it.get("qty") or 0)
        except (TypeError, ValueError):
            qty = 0.0
        try:
            unit = float(it.get("unit_price") or 0)
        except (TypeError, ValueError):
            unit = 0.0
        amt = it.get("amount")
        try:
            amt = float(amt) if amt is not None else round(qty * unit, 2)
        except (TypeError, ValueError):
            amt = round(qty * unit, 2)
        if not name and amt == 0:
            continue
        out.append({"name": name, "qty": qty, "unit_price": unit, "amount": round(amt, 2)})
    return out


def _invoice_line_items(db, case_id, invoice_id):
    """이 인보이스의 최신 라인아이템(invoice.line_items 이벤트 latest-wins). 없으면 []."""
    evs = (db.query(models.WorkflowEvent)
           .filter_by(case_id=case_id, action="invoice.line_items")
           .order_by(models.WorkflowEvent.created_at.desc(),
                     models.WorkflowEvent.event_id.desc()).all())
    for e in evs:
        p = e.payload or {}
        if p.get("invoice_id") == invoice_id:
            return p.get("items") or []
    return []


@app.get("/cases/{case_id}/invoices")
def list_invoices(case_id: str, user=Depends(auth.get_current_user), db: Session = Depends(get_db)):
    _get_case(db, case_id, user)
    rows = db.query(models.Invoice).filter_by(case_id=case_id).all()
    return [{"invoice_id": i.invoice_id, "invoice_no": i.invoice_no, "service_type": i.service_type,
             "amount": i.amount, "ppn": i.ppn, "total": i.total,
             "status": ("waiting_payment" if i.status == "unpaid" else i.status),
             "payment_ref": i.payment_ref, "due_date": str(i.due_date) if i.due_date else None,
             "line_items": _invoice_line_items(db, case_id, i.invoice_id)}
            for i in rows]


@app.post("/cases/{case_id}/invoices")
def add_invoice(case_id: str, body: schemas.InvoiceReq,
                user=Depends(auth.require_roles("consultant")), db: Session = Depends(get_db)):
    c = _get_case(db, case_id, user)
    # 수정요청 001 · 계약 큐 P1 데이터 게이트 — 계약 최종 확인(confirmed) 후에만 청구서 생성.
    _ct = db.query(models.Contract).filter_by(case_id=case_id).first()
    if not _ct or _ct.status not in ("confirmed", "invoiced", "paid"):
        raise HTTPException(409, {"code": "CONTRACT_NOT_CONFIRMED",
                                  "detail": "계약 최종 확인(관리자) 후에만 청구서를 생성할 수 있습니다."})
    # P1-#1: 라인아이템이 있으면 DPP(amount)는 라인 합계로 산정(특수조항 수동조정 반영).
    items = _norm_line_items(body.line_items)
    amount = round(sum(it["amount"] for it in items), 2) if items else body.amount
    ppn = round(amount * 0.11, 2)
    total = round(amount + ppn, 2)
    inv = models.Invoice(case_id=case_id, service_type=body.service_type, amount=amount,
                         ppn=ppn, total=total, status="waiting_payment",
                         due_date=datetime.utcnow() + timedelta(days=14))
    db.add(inv)
    db.flush()
    inv.invoice_no = "INV-" + inv.invoice_id[:8].upper()
    inv.payment_ref = "PAY-" + inv.invoice_id[:8].upper()
    if items:
        sm.record_event(db, c, c.status, c.status, "invoice.line_items", user["role"], user["uid"],
                        {"invoice_id": inv.invoice_id, "items": items, "amount": amount})
    _audit(db, user, "payment.invoice.create", "invoice", inv.invoice_id, case_id,
           {"total": total, "service_type": body.service_type}, commit=False)
    sm.record_event(db, c, c.status, c.status, "invoice.create", user["role"], user["uid"],
                    {"service_type": body.service_type, "total": total})
    db.commit()
    return {"invoice_id": inv.invoice_id, "invoice_no": inv.invoice_no, "ppn": ppn, "total": total,
            "status": inv.status, "payment_ref": inv.payment_ref,
            "due_date": str(inv.due_date), "line_items": items}


def _on_invoice_paid(db, c, user):
    """인보이스가 paid/confirmed로 확정되는 시점의 케이스 상태 게이트(멱등).
    결제 완료 후 모의심사 진입 단계(document_pre_audit_requested)로 전이 — 단,
    상태머신 가드를 통과할 때만(강제 점프 금지). 이미 모의심사/이후 단계거나
    유효 전이가 아니면 조용히 no-op. 전이 시 담당자(오디터) 배정 태스크+알림 생성.
    스키마 무변경 — 배정 전용 모델이 없어 WorkflowEvent + Notification 큐로 기록."""
    target = "document_pre_audit_requested"
    # 이미 모의심사/그 이후 단계면 재전이·중복알림 금지(멱등)
    if c.status in MOCK_AUDIT_STAGES:
        return None
    ok, _blk = sm.can_transition(db, c, target)
    if not ok:
        return None  # 가드 실패/유효 전이 아님 → 상태 유지(no-op)
    frm = c.status
    sm.apply_side_effects(c, target)
    c.status = target
    sm.record_event(db, c, frm, target, "payment.gate",
                    (user or {}).get("role", "system"), (user or {}).get("uid"),
                    {"trigger": "invoice_paid"})
    # 모의심사 담당자 배정 태스크(배정 전용 모델 부재 → 이벤트로 기록) + 알림
    sm.record_event(db, c, target, target, "mock_audit.task_created",
                    "system", None, {"assigned_role": "auditor"})
    _notify(db, c, "mock_audit.assigned", role="auditor",
            msg=("mock_audit.assigned", {}))
    return target


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
    _on_invoice_paid(db, c, user)   # 결제확정 → 모의심사 진입 게이트(가드 통과 시)
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
    if inv.status == "paid":        # 자동승인(va/card·금액일치) 확정 → 모의심사 진입 게이트
        _on_invoice_paid(db, c, user)
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
    if body.status == "paid":       # 관리자 결제확정 → 모의심사 진입 게이트
        c = _get_case(db, inv.case_id, user)
        _on_invoice_paid(db, c, user)
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
    if verified is None:
        # [A2] 시크릿 미설정 — 프로덕션에선 미검증 콜백 처리 금지(위조 결제 방지). dev만 허용.
        if not auth.dev_mode():
            log.warning("[pg-webhook] %s 시크릿 미설정 — 프로덕션 미검증 콜백 거부", provider)
            raise HTTPException(501, {"code": "PG_SECRET_UNSET",
                                      "hint": "GLHAC_PG_WEBHOOK_SECRET 또는 GLHAC_PG_SECRET_<PROVIDER> 설정 필요"})
        log.info("[pg-webhook] %s 서명 미검증 처리(dev 모드)", provider)
    elif verified is False:
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
def admin_payments(status: str = None, user=Depends(auth.require_roles("fatwa_liaison", "operator")),
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
def admin_payments_dashboard(user=Depends(auth.require_roles("fatwa_liaison", "operator")),
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
def list_refunds(status: str = None, user=Depends(auth.require_roles("fatwa_liaison", "operator")),
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
def admin_settlement(org_id: str = None, user=Depends(auth.require_roles("fatwa_liaison", "operator")),
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
def list_deposits(user=Depends(auth.require_roles("fatwa_liaison", "operator")), db: Session = Depends(get_db)):
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
    revenue_outstanding = round(sum(i.total or 0 for i in invs if i.status not in sm.INVOICE_SETTLED), 2)
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


# 케이스 상태 표기 — 사전(ENUM 축)에서 꺼낸다. 같은 상태 이름을 서버와 프런트가
# 따로 갖고 있었다. 지금은 값이 같지만 한쪽만 고쳐지는 건 시간 문제다
# (실측: 유래 표는 이미 그렇게 갈라져 한국어만 7개가 비어 있었다).
_STATE_KO = _dd_mod.code_labels("ENUM", "enum_code", "ko")


def state_label(code, lang="ko"):
    """상태 코드 → 표기. 모르는 코드는 코드 그대로 — 지어내지 않는다."""
    return _dd_mod.code_labels("ENUM", "enum_code", lang).get(code, code)


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
    from .intake import doc_requirements
    _c = db.get(models.CaseApplication, case_id)      # 면제된 서류는 분모에서도 뺀다
    req = doc_requirements(_c.pathway if _c else None,
                           (_c.profile_ext or {}).get("country") if _c else None,
                           _c.is_msme if _c else None)["required"]
    docs = db.query(models.DocumentAsset).filter_by(case_id=case_id).all()
    have_types = {d.doc_type for d in docs if d.review_status != "rejected"}
    doc_score = (sum(1 for r in req if r in have_types) / len(req)) if req else 1.0
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


# ── 워크플로우 모니터(관리자·운영자) — 전 케이스 진행단계·핸드오프·게이트 집약 ──
# 스키마 무변경: 기존 상태머신/청구/파트와 데이터를 read-only로 fold. stage→처리(대기) 역할 매핑만 신규.
_STAGE_OWNER = {
    "onboarding": "client", "application_draft": "client",
    "ai_pre_assessment_ready": "client", "ai_pre_assessment_running": "client",
    "pathway_determination": "consultant",
    "self_declare_eligible": "client", "sjph_lite_prepared": "client",
    "pendamping_verification": "consultant", "self_declaration_submitted": "client",
    "committee_verification": "ops",
    "supplementation_required": "client", "supplementation_submitted": "consultant",
    "consultant_review": "consultant",
    "document_pre_audit_requested": "ops", "document_pre_audit_in_review": "auditor",
    "document_pre_audit_approved": "auditor", "lph_assignment": "ops",
    "onsite_audit_scheduled": "auditor", "onsite_audit_in_progress": "auditor",
    "corrective_action_required": "client", "corrective_action_submitted": "auditor",
    "audit_closed": "auditor", "hpas_evaluation_ready": "auditor",
    "final_package_preparation": "ops", "fatwa_review": "sharia",
    "fatwa_approved": "ops", "certificate_issued": "sharia",
    "post_certification_monitoring": "client", "change_impact": "client",
    "renewal_preparation": "client",
}


@app.get("/admin/workflow-monitor")
def admin_workflow_monitor(lang: str = Query("ko"),
                           user=Depends(auth.require_roles("fatwa_liaison", "operator")),
                           db: Session = Depends(get_db)):
    """관리자·운영자 워크플로우 모니터 — 전 케이스의 현재단계·다음전이·차단·핸드오프(대기 역할)·
    주요 게이트(결제/AI 사전평가/파트와) 통과·대기를 단일 집약(읽기전용). 신규 쓰기·스키마 변경 없음."""
    # 상태·단계 표기는 현황판을 보는 사람의 언어로 — 코드 그대로 두면 한국어가 남는다.
    _st = _dd_mod.code_labels("ENUM", "enum_code", lang) or _STATE_KO
    _wfp = _dd_mod.code_labels("WF_PHASE", "wf_phase", lang)
    q = db.query(models.CaseApplication)
    if user["role"] != "admin":
        q = q.filter_by(org_id=user["org_id"])
    cases = q.order_by(models.CaseApplication.created_at.desc()).all()
    # batch fold(N+1 회피): 케이스별 게이트 판단용 인보이스·파트와를 1회 조회로 인덱싱
    fds = {f.case_id: f for f in db.query(models.FatwaDecision).all()}
    invs = {}
    for iv in db.query(models.Invoice).all():
        invs.setdefault(iv.case_id, []).append(iv)
    # 상태 진행순 랭크(정규경로 superset) — 게이트 통과/대기 추론용
    _rank = {}
    for i, s in enumerate([s for (_k, _ko, ss) in (_WF_COMMON + _WF_REGULER + _WF_POST) for s in ss]):
        _rank.setdefault(s, i)
    rows, pipeline = [], {}
    gate_sum = {"payment": {"ok": 0, "wait": 0}, "ai_report": {"ok": 0, "wait": 0},
                "fatwa": {"ok": 0, "wait": 0}}
    blocked_total = 0
    for c in cases:
        cur = c.status
        phases = _wf_phases(c.pathway)
        cur_idx = next((i for i, (k, ko, ss) in enumerate(phases) if cur in ss), None)
        phase_key = phases[cur_idx][0] if cur_idx is not None else "branch"
        phase_label = (_wfp.get(phases[cur_idx][0]) or phases[cur_idx][1]) \
            if cur_idx is not None else (_wfp.get("branch") or "경로 결정 대기")
        pipeline[phase_key] = pipeline.get(phase_key, 0) + 1
        nxt = sorted(sm.TRANSITIONS.get(cur, set()))
        next_state = nxt[0] if nxt else None
        blockers = [b.get("code") for b in sm.evaluate_blocking(db, c)]
        if blockers:
            blocked_total += 1
        rank = _rank.get(cur, -1)
        case_invs = invs.get(c.case_id, [])
        paid = any((iv.status or "") in ("paid", "settlement", "settled") for iv in case_invs)
        g_pay = "ok" if paid else ("wait" if case_invs else "na")
        g_ai = ("ok" if rank >= _rank.get("pathway_determination", 99)
                else ("wait" if cur in ("ai_pre_assessment_ready", "ai_pre_assessment_running") else "na"))
        fd = fds.get(c.case_id)
        g_fatwa = ("ok" if (fd and fd.final_approved_at)
                   else ("wait" if (cur in ("hpas_evaluation_ready", "final_package_preparation", "fatwa_review")
                                    or (fd and fd.decision in ("approved", "conditional"))) else "na"))
        for key, gv in (("payment", g_pay), ("ai_report", g_ai), ("fatwa", g_fatwa)):
            if gv in ("ok", "wait"):
                gate_sum[key][gv] += 1
        rows.append({"case_id": c.case_id, "company": c.company_name, "pathway": c.pathway,
                     "status": cur, "status_label": _st.get(cur, cur),
                     "phase_key": phase_key, "phase_label": phase_label,
                     "owner": _STAGE_OWNER.get(cur, "ops"),
                     "next_state": next_state,
                     "next_label": _st.get(next_state, next_state) if next_state else None,
                     "blockers": blockers, "blocker_count": len(blockers),
                     "due_date": c.due_date,
                     "gates": {"payment": g_pay, "ai_report": g_ai, "fatwa": g_fatwa}})
    _porder = ["prep", "assess", "pathway", "branch", "sd_sjph", "sd_submit", "sd_committee",
               "rg_suppl", "rg_doc", "rg_audit", "rg_hpas", "rg_fatwa", "issue", "post"]
    # 단계명은 운영자·심사자가 현황판에서 읽는다 — 화면 언어를 따른다.
    _plabel = {seg[0]: seg[1] for seg in (_WF_COMMON + _WF_SEHATI + _WF_REGULER + _WF_POST)}
    _plabel["branch"] = "경로 결정 대기"
    _plabel.update({k: v for k, v in
                    _dd_mod.code_labels("WF_PHASE", "wf_phase", lang).items() if v})
    pipeline_out = [{"key": k, "label": _plabel.get(k, k), "count": pipeline[k]}
                    for k in _porder if pipeline.get(k)]
    return {"cases": rows, "pipeline": pipeline_out, "gates_summary": gate_sum,
            "totals": {"cases": len(cases), "blocked": blocked_total}}


# ══ M01 최고운영자 운영현황 대시보드 (P0-3차) ═════════════════════════════
# 스키마 무변경: CaseApplication/WorkflowEvent/Notification/User/Facility 조회·집계만.
# 신규 승인/거절/배정은 WorkflowEvent(latest-wins) + Notification 큐로 기록(전용 테이블 없음).
_OPS_PENDING_STATES = {"onboarding"}          # 신규 업체 승인 대기(초기 상태)
_ONSITE_STATES = {"onsite_audit_scheduled", "onsite_audit_in_progress"}
_DOCAUDIT_STATES = {"document_pre_audit_requested", "document_pre_audit_in_review"}


def _ops_cases(db, user):
    """운영자 스코프 케이스(admin=전체, operator=자기 조직)."""
    q = db.query(models.CaseApplication)
    if user["role"] != "admin":
        q = q.filter_by(org_id=user["org_id"])
    return q.order_by(models.CaseApplication.created_at.desc()).all()


def _ops_fac_province(db, user):
    """org_id → province(첫 시설 city/address 기반). 케이스 주소에 지역이 없을 때 보조."""
    q = db.query(models.Facility)
    if user["role"] != "admin":
        q = q.filter_by(org_id=user["org_id"])
    out = {}
    for f in q.all():
        if f.org_id in out:
            continue
        prov = _province_of(f.city or f.address)
        if prov:
            out[f.org_id] = prov
    return out


def _case_province(c, fac_map):
    return _province_of(c.factory_address or c.address) or fac_map.get(c.org_id)


def _ops_latest_company_decision(db, case_ids):
    """케이스별 최신 신규업체 승인/거절 결정(ops.company_*, latest-wins)."""
    out = {}
    if not case_ids:
        return out
    evs = (db.query(models.WorkflowEvent)
           .filter(models.WorkflowEvent.action.in_(["ops.company_approved", "ops.company_rejected"]),
                   models.WorkflowEvent.case_id.in_(case_ids))
           .order_by(models.WorkflowEvent.created_at.asc(),
                     models.WorkflowEvent.event_id.asc()).all())
    for e in evs:   # asc 순회 → 마지막(최신)이 out에 남음
        out[e.case_id] = "approved" if e.action.endswith("approved") else "rejected"
    return out


def _ops_latest_assignment(db, case_ids):
    """케이스별 최신 오디터 배정(ops.auditor_assigned, latest-wins) + 수락/거절 응답 병합.

    수락 루프(스키마 무변경): 배정 이벤트 이후에 기록된 auditor.assignment_response 중
    같은 auditor_id·같은 배정시각 이후의 최신 응답을 accept_status로 붙인다.
      accept_status: pending | accepted | rejected
    """
    out = {}
    if not case_ids:
        return out
    evs = (db.query(models.WorkflowEvent)
           .filter(models.WorkflowEvent.action.in_(("ops.auditor_assigned",
                                                    ASSIGN_RESPONSE_ACTION)),
                   models.WorkflowEvent.case_id.in_(case_ids))
           .order_by(models.WorkflowEvent.created_at.asc(),
                     models.WorkflowEvent.event_id.asc()).all())
    for e in evs:
        p = e.payload or {}
        if e.action == "ops.auditor_assigned":
            out[e.case_id] = dict(p, accept_status="pending", reject_reason=None,
                                  responded_at=None)
        else:   # 응답 — 현재 배정된 오디터의 응답만 반영(재배정 시 자동 무효화)
            cur = out.get(e.case_id)
            if cur and p.get("auditor_id") == cur.get("auditor_id"):
                cur["accept_status"] = p.get("decision") or "pending"
                cur["reject_reason"] = p.get("reason")
                cur["responded_at"] = str(e.created_at)
    return out


def _ops_auditor_users(db, user):
    q = db.query(models.User).filter_by(role="auditor")
    if user["role"] != "admin":
        q = q.filter_by(org_id=user["org_id"])
    return q.all()


def _ops_regions_data(db, user, cases=None, fac_map=None):
    cases = _ops_cases(db, user) if cases is None else cases
    fac_map = _ops_fac_province(db, user) if fac_map is None else fac_map
    buckets = {}
    for c in cases:
        prov = _case_province(c, fac_map)
        key = prov or "미지정"
        buckets.setdefault(key, []).append(
            {"case_id": c.case_id, "company": c.company_name, "status": c.status,
             "status_label": _STATE_KO.get(c.status, c.status), "pathway": c.pathway})
    regions = [{"province": k, "count": len(v), "cases": v}
               for k, v in sorted(buckets.items(), key=lambda x: (-len(x[1]), x[0]))]
    has_data = any(k != "미지정" for k in buckets)
    return {"regions": regions, "has_region_data": has_data, "total": len(cases),
            "unassigned_region": len(buckets.get("미지정", []))}


def _ops_capacity_data(db, user, cases=None):
    cases = _ops_cases(db, user) if cases is None else cases
    by_status = {}
    for c in cases:
        by_status[c.status] = by_status.get(c.status, 0) + 1
    status_rows = [{"status": s, "label": _STATE_KO.get(s, s), "count": n}
                   for s, n in sorted(by_status.items(), key=lambda x: -x[1])]
    backlog = {
        "pending_company": sum(1 for c in cases if c.status in _OPS_PENDING_STATES),
        "doc_audit_wait": sum(1 for c in cases if c.status in _DOCAUDIT_STATES),
        "onsite_wait": sum(1 for c in cases if c.status in _ONSITE_STATES),
        "fatwa_wait": sum(1 for c in cases if c.status in FATWA_STAGES),
        "corrective_wait": sum(1 for c in cases if c.status == "corrective_action_required"),
    }
    assignments = _ops_latest_assignment(db, [c.case_id for c in cases])
    load = {}
    for p in assignments.values():
        aid = p.get("auditor_id")
        if aid:
            load[aid] = load.get(aid, 0) + 1
    auditors = _ops_auditor_users(db, user)
    auditor_load = [{"user_id": a.user_id, "username": a.username, "load": load.get(a.user_id, 0)}
                    for a in sorted(auditors, key=lambda a: -load.get(a.user_id, 0))]
    return {"by_status": status_rows, "backlog": backlog, "auditor_load": auditor_load,
            "total": len(cases)}


def _ops_pending_data(db, user, cases=None, fac_map=None):
    cases = _ops_cases(db, user) if cases is None else cases
    fac_map = _ops_fac_province(db, user) if fac_map is None else fac_map
    onboarding = [c for c in cases if c.status in _OPS_PENDING_STATES]
    decisions = _ops_latest_company_decision(db, [c.case_id for c in onboarding])
    out = []
    for c in onboarding:
        if decisions.get(c.case_id) == "rejected":
            continue   # 이미 거절 처리된 건 대기목록에서 제외
        px = c.profile_ext or {}
        out.append({"case_id": c.case_id, "company": c.company_name, "org_id": c.org_id,
                    "region": _case_province(c, fac_map) or "미지정",
                    "created_at": c.created_at.isoformat() if c.created_at else None,
                    "sector": px.get("business_type") or px.get("registration_type") or None,
                    "nib": c.nib, "responsible_person": c.responsible_person})
    return {"items": out, "count": len(out)}


# ── P2-6 오디터 프로필(전문분야·언어) — 스키마 무변경 sentinel(WorkflowEvent latest-wins) ──
# User 모델에 컬럼을 추가하지 않고, 오디터 user_id를 case_id 스코프로 삼아 "auditor.profile"
# 이벤트 스트림을 만든다(regulations의 reg_id 방식과 동일). 최신 이벤트 payload = 현재 프로필.
AUDITOR_PROFILE_ACTION = "auditor.profile"
AUDITOR_BASE_CAPACITY = 8   # 업무량 진행바 정규화 기준 캐파(오디터 1인 동시 담당 권장 상한)


def _auditor_shim(user_id):
    """record_event(해시체인)용 최소 case 쉼 — 오디터 user_id를 case_id 스코프로 사용."""
    import types
    return types.SimpleNamespace(case_id=user_id, org_id=None)


def _auditor_profile(db, user_id):
    """최신 auditor.profile payload = 현재 프로필(latest-wins). 없으면 None."""
    last = (db.query(models.WorkflowEvent)
            .filter(models.WorkflowEvent.case_id == user_id,
                    models.WorkflowEvent.action == AUDITOR_PROFILE_ACTION)
            .order_by(models.WorkflowEvent.created_at.desc(),
                      models.WorkflowEvent.event_id.desc()).first())
    if last is None:
        return None
    p = last.payload or {}
    return {"specialty": p.get("specialty"),
            "languages": p.get("languages", []) or [],
            "updated_at": last.created_at.isoformat() if last.created_at else None}


def _auditor_load_pct(load):
    """업무량을 기준 캐파 대비 백분율(0~100)로 정규화 — 진행바 색상 판정용."""
    if AUDITOR_BASE_CAPACITY <= 0:
        return 0
    return min(100, round(load / AUDITOR_BASE_CAPACITY * 100))


def _ops_auditors_data(db, user, cases=None):
    cases = _ops_cases(db, user) if cases is None else cases
    assignments = _ops_latest_assignment(db, [c.case_id for c in cases])
    load = {}
    for p in assignments.values():
        aid = p.get("auditor_id")
        if aid:
            load[aid] = load.get(aid, 0) + 1

    def _auditor_row(a):
        prof = _auditor_profile(db, a.user_id) or {}
        n = load.get(a.user_id, 0)
        return {"user_id": a.user_id, "username": a.username, "load": n,
                "load_pct": _auditor_load_pct(n), "capacity": AUDITOR_BASE_CAPACITY,
                "specialty": prof.get("specialty"),
                "languages": prof.get("languages", []) or [],
                "profile_updated_at": prof.get("updated_at")}
    auditors = [_auditor_row(a)
                for a in sorted(_ops_auditor_users(db, user), key=lambda a: load.get(a.user_id, 0))]
    unassigned = []
    for c in cases:
        if c.status in AUDIT_STAGES and c.case_id not in assignments:
            unassigned.append({"case_id": c.case_id, "company": c.company_name,
                               "status": c.status, "status_label": _STATE_KO.get(c.status, c.status),
                               "pathway": c.pathway})
    assigned = []
    for c in cases:
        p = assignments.get(c.case_id)
        if p and c.status in AUDIT_STAGES:
            assigned.append({"case_id": c.case_id, "company": c.company_name,
                             "status": c.status, "status_label": _STATE_KO.get(c.status, c.status),
                             "auditor_id": p.get("auditor_id"), "auditor_name": p.get("auditor_name")})
    return {"auditors": auditors, "unassigned": unassigned, "assigned": assigned}


def _ops_unassigned_clients(db, cases):
    """담당 컨설턴트가 없는 업체 — 수수료 귀속처가 비어 있다는 뜻이다.

    코드 없이 들어왔거나 담당이 해제된 업체다. 운영자가 지정해야 영업 실적이 어디로
    갈지 정해진다. 케이스가 있는 업체를 먼저 보여준다 — 일이 이미 돌고 있는 곳이다."""
    with_case = {}
    for c in cases:
        with_case[c.org_id] = with_case.get(c.org_id, 0) + 1
    rows = []
    for o in db.query(models.Org).filter(models.Org.consultant_id.is_(None)).all():
        n = with_case.get(o.org_id, 0)
        rows.append({"org_id": o.org_id, "name": o.name, "cases": n})
    rows.sort(key=lambda x: (-(x["cases"] or 0), (x["name"] or "")))
    return {"count": len(rows), "with_cases": sum(1 for r in rows if r["cases"]),
            "items": rows[:50]}


@app.get("/ops/dashboard")
def ops_dashboard(user=Depends(auth.require_roles("fatwa_liaison", "operator")), db: Session = Depends(get_db)):
    """M01 최고운영자 운영현황 — 지역/처리캐파/신규업체승인/오디터배정 단일 집약(읽기전용)."""
    cases = _ops_cases(db, user)
    fac_map = _ops_fac_province(db, user)
    # 오디터 배정 응답 현황 — 거절(사유·재배정 필요) / 수락 대기
    assign = _ops_latest_assignment(db, [c.case_id for c in cases])
    name = {c.case_id: c.company_name for c in cases}
    rejected, awaiting = [], []
    for cid, a in assign.items():
        if cid not in name:
            continue
        row = {"case_id": cid, "company": name[cid], "auditor_id": a.get("auditor_id"),
               "auditor_name": a.get("auditor_name"), "reject_reason": a.get("reject_reason"),
               "responded_at": a.get("responded_at")}
        if a.get("accept_status") == "rejected":
            rejected.append(row)
        elif a.get("accept_status") == "pending":
            awaiting.append(row)
    return {"regions": _ops_regions_data(db, user, cases, fac_map),
            "capacity": _ops_capacity_data(db, user, cases),
            "pending_companies": _ops_pending_data(db, user, cases, fac_map),
            "auditors": _ops_auditors_data(db, user, cases),
            "unassigned_clients": _ops_unassigned_clients(db, cases),
            "assignment_rejected": rejected, "assignment_awaiting": awaiting}


@app.get("/ops/regions")
def ops_regions(user=Depends(auth.require_roles("fatwa_liaison", "operator")), db: Session = Depends(get_db)):
    return _ops_regions_data(db, user)


@app.get("/ops/capacity")
def ops_capacity(user=Depends(auth.require_roles("fatwa_liaison", "operator")), db: Session = Depends(get_db)):
    return _ops_capacity_data(db, user)


@app.get("/ops/pending-companies")
def ops_pending_companies(user=Depends(auth.require_roles("operator")), db: Session = Depends(get_db)):
    return _ops_pending_data(db, user)


# ── 청구 기본 요금표(디폴트 금액) — 스키마 무변경 sentinel(WorkflowEvent, case_id="billing-defaults:{org}").
#    샤리아·최고승인자·관리자가 서비스별 기본 심사료를 설정하면 청구서 생성 시 자동 채움. ──
BILLING_DEFAULTS_ACTION = "billing.defaults"
BILLING_SERVICE_TYPES = ("pre_audit", "onsite", "certification", "renewal", "surveillance")
BILLING_FALLBACK = {"pre_audit": 6500000, "onsite": 12000000, "certification": 5000000,
                    "renewal": 8000000, "surveillance": 3000000}
# 청구 서비스 유형 — 인보이스에 찍힌다.
BILLING_SERVICE_KO = _dd_mod.code_labels("BILLING_SERVICE", "billing_service", "ko")


def billing_service_label(code, lang="ko"):
    return _dd_mod.code_labels("BILLING_SERVICE", "billing_service", lang).get(code, code)

def _billing_shim(org_id):
    import types
    return types.SimpleNamespace(case_id="billing-defaults:" + (org_id or "org_demo"), org_id=org_id)


def _billing_defaults(db, org_id):
    ev = (db.query(models.WorkflowEvent)
          .filter_by(case_id="billing-defaults:" + (org_id or "org_demo"),
                     action=BILLING_DEFAULTS_ACTION)
          .order_by(models.WorkflowEvent.created_at.desc()).first())
    saved = ((ev.payload or {}).get("rates") or {}) if ev else {}
    out = dict(BILLING_FALLBACK)
    for k, v in saved.items():
        if k in BILLING_SERVICE_TYPES:
            try:
                out[k] = float(v)
            except (TypeError, ValueError):
                pass
    return out, (bool(ev), (ev.created_at.isoformat() if ev and ev.created_at else None))


@app.get("/billing/defaults")
def get_billing_defaults(user=Depends(auth.get_current_user), db: Session = Depends(get_db)):
    """청구서 생성 시 자동 채움될 서비스별 기본 금액(디폴트)."""
    rates, (customized, at) = _billing_defaults(db, user.get("org_id"))
    return {"rates": [{"service_type": k, "label": BILLING_SERVICE_KO[k], "amount": rates[k],
                       "is_default": rates[k] == BILLING_FALLBACK[k]}
                      for k in BILLING_SERVICE_TYPES],
            "customized": customized, "updated_at": at, "ppn_rate": 0.11}


@app.post("/billing/defaults")
def set_billing_defaults(body: dict = None,
                         user=Depends(auth.require_roles("fatwa_liaison", "operator")),
                         db: Session = Depends(get_db)):
    """기본 요금표 설정 — 샤리아·최고승인자(및 admin). 0 이상 숫자만 허용."""
    rates = (body or {}).get("rates") or {}
    if not isinstance(rates, dict) or not rates:
        raise HTTPException(422, {"code": "NO_RATES"})
    clean = {}
    for k, v in rates.items():
        if k not in BILLING_SERVICE_TYPES:
            raise HTTPException(422, {"code": "BAD_SERVICE_TYPE", "allowed": list(BILLING_SERVICE_TYPES)})
        try:
            f = float(v)
        except (TypeError, ValueError):
            raise HTTPException(422, {"code": "BAD_AMOUNT", "service_type": k})
        if f < 0 or f > 1e12:
            raise HTTPException(422, {"code": "AMOUNT_OUT_OF_RANGE", "service_type": k})
        clean[k] = f
    sm.record_event(db, _billing_shim(user.get("org_id")), "billing", "billing",
                    BILLING_DEFAULTS_ACTION, user["role"], user["uid"], {"rates": clean})
    db.commit()
    merged, _ = _billing_defaults(db, user.get("org_id"))
    return {"rates": merged, "updated_by": user["role"]}


# ── 오디터 업무 현황(샤리아·최고승인자 배정 판단용) — 전체 목록 + 개별 상세 ──
@app.get("/ops/auditors/workload")
def ops_auditors_workload(user=Depends(auth.require_roles("fatwa_liaison", "operator")),
                          db: Session = Depends(get_db)):
    """오디터별 업무 현황 요약 — 담당 케이스(수락 기준)·수락대기·거절·이번주 현장실사·미해결 부적합."""
    cases = _ops_cases(db, user)
    assign = _ops_latest_assignment(db, [c.case_id for c in cases])
    cmap = {c.case_id: c for c in cases}
    today = datetime.utcnow().date()
    monday = today - timedelta(days=today.weekday())
    ms, ss = monday.isoformat(), (monday + timedelta(days=6)).isoformat()
    plans = {}
    if cases:
        for p in (db.query(models.AuditPlan)
                  .filter(models.AuditPlan.case_id.in_(list(cmap)),
                          models.AuditPlan.scheduled_date.isnot(None)).all()):
            plans.setdefault(p.case_id, []).append(str(p.scheduled_date)[:10])
    open_nc = {}
    if cases:
        for f in (db.query(models.AuditFinding)
                  .filter(models.AuditFinding.case_id.in_(list(cmap)),
                          models.AuditFinding.status == "open").all()):
            open_nc[f.case_id] = open_nc.get(f.case_id, 0) + 1
    rows = []
    for a in _ops_auditor_users(db, user):
        mine = [(cid, p) for cid, p in assign.items()
                if p.get("auditor_id") == a.user_id and cid in cmap]
        acc = [cid for cid, p in mine if p.get("accept_status") == "accepted"]
        pend = [cid for cid, p in mine if p.get("accept_status") == "pending"]
        rej = [{"case_id": cid, "company": cmap[cid].company_name, "reason": p.get("reject_reason")}
               for cid, p in mine if p.get("accept_status") == "rejected"]
        week = sum(1 for cid in acc for d in plans.get(cid, []) if ms <= d <= ss)
        prof = _auditor_profile(db, a.user_id) or {}
        rows.append({"user_id": a.user_id, "username": a.username,
                     "specialty": prof.get("specialty"), "languages": prof.get("languages", []) or [],
                     "capacity": AUDITOR_BASE_CAPACITY,
                     "assigned": len(acc), "pending_accept": len(pend), "rejected": len(rej),
                     "rejected_items": rej, "week_onsite": week,
                     "open_findings": sum(open_nc.get(cid, 0) for cid in acc),
                     "load_pct": _auditor_load_pct(len(acc)),
                     "cases": [{"case_id": cid, "company": cmap[cid].company_name,
                                "status": cmap[cid].status,
                                "status_label": _STATE_KO.get(cmap[cid].status, cmap[cid].status),
                                "accept_status": p.get("accept_status"),
                                "next_onsite": sorted(plans.get(cid, []))[0] if plans.get(cid) else None}
                               for cid, p in mine]})
    rows.sort(key=lambda r: (-r["assigned"], r["username"]))
    return {"auditors": rows, "count": len(rows), "week_start": ms, "week_end": ss,
            "unassigned": sum(1 for c in cases if c.case_id not in assign)}


@app.get("/ops/auditors/{user_id}/workload")
def ops_auditor_workload_detail(user_id: str,
                                user=Depends(auth.require_roles("fatwa_liaison", "operator")),
                                db: Session = Depends(get_db)):
    """오디터 개별 업무 상세 — 담당 케이스별 상태·일정·부적합·최근 활동."""
    a = db.get(models.User, user_id)
    if not a or a.role != "auditor":
        raise HTTPException(404, {"code": "AUDITOR_NOT_FOUND"})
    cases = _ops_cases(db, user)
    assign = _ops_latest_assignment(db, [c.case_id for c in cases])
    cmap = {c.case_id: c for c in cases}
    mine = [(cid, p) for cid, p in assign.items()
            if p.get("auditor_id") == user_id and cid in cmap]
    items = []
    for cid, p in mine:
        c = cmap[cid]
        plans = (db.query(models.AuditPlan).filter_by(case_id=cid)
                 .order_by(models.AuditPlan.scheduled_date.asc()).all())
        nc = db.query(models.AuditFinding).filter_by(case_id=cid, status="open").count()
        last = (db.query(models.WorkflowEvent).filter_by(case_id=cid, actor_id=user_id)
                .order_by(models.WorkflowEvent.created_at.desc()).first())
        items.append({"case_id": cid, "company": c.company_name, "status": c.status,
                      "status_label": _STATE_KO.get(c.status, c.status), "pathway": c.pathway,
                      "accept_status": p.get("accept_status"), "reject_reason": p.get("reject_reason"),
                      "schedule": [str(x.scheduled_date)[:10] for x in plans if x.scheduled_date],
                      "open_findings": nc,
                      "last_action": (last.action if last else None),
                      "last_action_at": (last.created_at.isoformat() if last and last.created_at else None)})
    items.sort(key=lambda x: (x["accept_status"] != "accepted", x["company"] or ""))
    prof = _auditor_profile(db, user_id) or {}
    return {"user_id": user_id, "username": a.username, "profile": prof,
            "capacity": AUDITOR_BASE_CAPACITY,
            "assigned": sum(1 for i in items if i["accept_status"] == "accepted"),
            "items": items, "count": len(items)}


@app.get("/ops/auditors")
def ops_auditors(user=Depends(auth.require_roles("fatwa_liaison", "operator")), db: Session = Depends(get_db)):
    return _ops_auditors_data(db, user)


@app.post("/ops/auditors/{user_id}/profile")
def ops_auditor_profile(user_id: str, body: schemas.AuditorProfileReq,
                        user=Depends(auth.require_roles("operator")),
                        db: Session = Depends(get_db)):
    """P2-6 오디터 프로필(전문분야·언어) 설정 — 스키마 무변경, auditor.profile 이벤트 latest-wins.
    대상은 오디터 유저여야 하며 operator는 자기 조직 스코프로 제한(admin은 전체)."""
    target = db.query(models.User).filter_by(user_id=user_id, role="auditor").first()
    if target is None:
        raise HTTPException(404, {"code": "AUDITOR_NOT_FOUND"})
    if user["role"] != "admin" and target.org_id != user["org_id"]:
        raise HTTPException(403, {"code": "FORBIDDEN_ORG_SCOPE"})
    payload = {"specialty": (body.specialty or "").strip() or None,
               "languages": [s for s in (body.languages or []) if s]}
    sm.record_event(db, _auditor_shim(user_id), None, None, AUDITOR_PROFILE_ACTION,
                    user["role"], user["uid"], payload)
    db.commit()
    return {"ok": True, "user_id": user_id, "profile": _auditor_profile(db, user_id)}


@app.get("/ops/calendar")
def ops_calendar(user=Depends(auth.require_roles("fatwa_liaison", "operator")), db: Session = Depends(get_db)):
    """P1-#5 관리자 종합 캘린더 — 전 케이스 현장실사 일정 집약(읽기전용, 스키마 무변경).
    소스: (a)AuditPlan.scheduled_date(LPH 예정) (b)onsite_schedule.confirm 확정일(WorkflowEvent latest-wins).
    admin=전체·operator=자기 조직 스코프(_ops_cases 재사용). 프런트 월그리드/리스트 렌더용."""
    cases = _ops_cases(db, user)
    cmap = {c.case_id: c for c in cases}
    case_ids = list(cmap.keys())
    events = []

    def _push(cid, dstr, kind, status, source, lph="", tm=""):
        d = str(dstr or "").strip()
        if not d:
            return
        c = cmap.get(cid)
        events.append({"date": d[:10], "case_id": cid,
                       "company_name": (c.company_name if c else "") or "",
                       "status_stage": (c.status if c else "") or "",
                       "kind": kind, "status": status, "lph_name": lph or "",
                       "time": tm or "", "source": source})

    # (a) AuditPlan 예정일(취소 제외)
    if case_ids:
        plans = (db.query(models.AuditPlan)
                 .filter(models.AuditPlan.case_id.in_(case_ids),
                         models.AuditPlan.scheduled_date.isnot(None)).all())
        for p in plans:
            if (p.status or "") == "cancelled":
                continue
            _push(p.case_id, p.scheduled_date, "onsite_audit", p.status or "scheduled",
                  "audit_plan", lph=p.lph_name or "")
    # (b) onsite_schedule.confirm 확정일 — 조율 이벤트 보유 케이스만 fold(정확성·부하 최소화)
    if case_ids:
        sched_cids = set(r[0] for r in (
            db.query(models.WorkflowEvent.case_id)
            .filter(models.WorkflowEvent.case_id.in_(case_ids),
                    models.WorkflowEvent.action.in_(_ONSITE_SCHED_ACTIONS)).distinct().all()))
        for cid in sched_cids:
            st = _onsite_sched_state(db, cid)
            conf = st.get("confirmed") or {}
            if conf.get("date"):
                _push(cid, conf.get("date"), "onsite_audit", "confirmed",
                      "onsite_schedule", tm=conf.get("time") or "")
    events.sort(key=lambda e: (e["date"], e["company_name"]))
    by_date = {}
    for e in events:
        by_date.setdefault(e["date"], []).append(e)
    return {"events": events, "by_date": by_date, "count": len(events),
            "case_count": len(set(e["case_id"] for e in events))}


@app.get("/auditor/dashboard")
def auditor_dashboard(user=Depends(auth.require_roles("auditor", "operator", "admin")),
                      db: Session = Depends(get_db)):
    """M2·M3 오디터 운영 KPI — 담당(배정) 케이스 스코프 집계(읽기전용, 스키마 무변경).
    담당 = ops.auditor_assigned 최신 배정이 본인인 케이스. 이번주 현장실사·처리대기·미해결 부적합."""
    q = db.query(models.CaseApplication)
    if user["role"] != "admin":
        q = q.filter_by(org_id=user["org_id"])
    cases = q.all()
    assign = _ops_latest_assignment(db, [c.case_id for c in cases])
    if user["role"] == "auditor":
        # 수락한 배정만 '심사 리스트'에 편입 — 대기/거절 건은 배정함(/auditor/assignments)에서 처리
        mine = [c for c in cases
                if (assign.get(c.case_id) or {}).get("auditor_id") == user["uid"]
                and (assign.get(c.case_id) or {}).get("accept_status") == "accepted"]
    else:
        mine = cases   # operator/admin: 조직/전체 스코프
    mine_ids = [c.case_id for c in mine]
    today = datetime.utcnow().date()
    monday = today - timedelta(days=today.weekday())
    ms, ss = monday.isoformat(), (monday + timedelta(days=6)).isoformat()
    week_onsite = 0
    if mine_ids:
        plans = (db.query(models.AuditPlan)
                 .filter(models.AuditPlan.case_id.in_(mine_ids),
                         models.AuditPlan.scheduled_date.isnot(None)).all())
        for p in plans:
            if (p.status or "") == "cancelled":
                continue
            d = str(p.scheduled_date or "")[:10]
            if ms <= d <= ss:
                week_onsite += 1
    pending_review = sum(1 for c in mine if c.status in AUDIT_STAGES)
    open_findings = 0
    if mine_ids:
        open_findings = (db.query(models.AuditFinding)
                         .filter(models.AuditFinding.case_id.in_(mine_ids),
                                 models.AuditFinding.status == "open").count())
    # 배정 수락 대기 건수(오디터 KPI 타일 · 배정함 진입 유도)
    pending_assign = sum(1 for c in cases
                         if (assign.get(c.case_id) or {}).get("auditor_id") == user["uid"]
                         and (assign.get(c.case_id) or {}).get("accept_status") == "pending"
                         ) if user["role"] == "auditor" else 0
    return {"assigned_count": len(mine), "week_onsite": week_onsite,
            "pending_review": pending_review, "open_findings": open_findings,
            "pending_assign": pending_assign,
            "week_start": ms, "week_end": ss}


@app.post("/ops/companies/{case_id}/approve")
def ops_company_approve(case_id: str, user=Depends(auth.require_roles("operator")),
                        db: Session = Depends(get_db)):
    """신규 업체 승인 — 상태전이는 상태머신 가드 통과 시에만. WorkflowEvent 기록 + 알림."""
    c = _get_case(db, case_id, user)
    frm = c.status
    target = "application_draft"
    transitioned = None
    ok, blk = sm.can_transition(db, c, target)
    if ok:
        sm.apply_side_effects(c, target)
        c.status = target
        transitioned = target
    sm.record_event(db, c, frm, c.status, "ops.company_approved", user["role"], user["uid"],
                    {"transitioned_to": transitioned})
    _notify(db, c, "ops.company_approved", role="consultant",
            msg=("ops.company_approved", {"company": c.company_name or ""}))
    db.commit()
    return {"ok": True, "transitioned_to": transitioned, "blockers": ([] if ok else blk)}


@app.post("/ops/companies/{case_id}/reject")
def ops_company_reject(case_id: str, body: schemas.OpsRejectReq,
                       user=Depends(auth.require_roles("operator")),
                       db: Session = Depends(get_db)):
    """신규 업체 거절 — 사유 필수. 상태 유지(불법전이 금지) + 반려표시 + WorkflowEvent·알림."""
    reason = (body.reason or "").strip()
    if not reason:
        raise HTTPException(422, {"code": "REASON_REQUIRED"})
    c = _get_case(db, case_id, user)
    c.draft_state = "returned"
    c.return_reason = reason
    sm.record_event(db, c, c.status, c.status, "ops.company_rejected", user["role"], user["uid"],
                    {"reason": reason})
    _notify(db, c, "ops.company_rejected", "신규 업체 거절", body=reason, role="consultant")
    db.commit()
    return {"ok": True, "reason": reason}


# ── 오디터 배정 수락/거절 루프 (스키마 무변경 · WorkflowEvent) ──
# 관리자가 오디터를 배정하면 오디터 쪽에 "배정 대기" 리스트가 뜨고, 오디터가 수락/거절한다.
# 수락 → 심사 리스트(담당 케이스) 편입. 거절 → 사유와 함께 관리자에게 회신(재배정 대상).
ASSIGN_RESPONSE_ACTION = "auditor.assignment_response"


@app.get("/auditor/assignments")
def auditor_assignments(status: str = Query("pending", pattern="^(pending|accepted|rejected|all)$"),
                        user=Depends(auth.require_roles("auditor", "operator", "admin")),
                        db: Session = Depends(get_db)):
    """오디터 배정함 — 본인에게 배정된 케이스의 수락/거절 상태별 목록.
    operator/admin은 조직 전체(거절 회신 확인용)."""
    q = db.query(models.CaseApplication)
    if user["role"] != "admin":
        q = q.filter_by(org_id=user["org_id"])
    cases = q.all()
    assign = _ops_latest_assignment(db, [c.case_id for c in cases])
    items = []
    for c in cases:
        a = assign.get(c.case_id)
        if not a:
            continue
        if user["role"] == "auditor" and a.get("auditor_id") != user["uid"]:
            continue
        st = a.get("accept_status") or "pending"
        if status != "all" and st != status:
            continue
        items.append({"case_id": c.case_id, "company_name": c.company_name,
                      "status": c.status, "status_label": _STATE_KO.get(c.status, c.status),
                      "pathway": c.pathway, "created_at": str(c.created_at or "")[:10],
                      "auditor_id": a.get("auditor_id"), "auditor_name": a.get("auditor_name"),
                      "accept_status": st, "reject_reason": a.get("reject_reason"),
                      "responded_at": a.get("responded_at")})
    items.sort(key=lambda x: x["created_at"], reverse=True)
    return {"items": items, "count": len(items), "status": status,
            "pending_count": sum(1 for i in items if i["accept_status"] == "pending")}


@app.post("/cases/{case_id}/assignment/respond")
def auditor_assignment_respond(case_id: str, body: dict = None,
                               user=Depends(auth.require_roles("auditor")),
                               db: Session = Depends(get_db)):
    """오디터가 배정을 수락/거절. 거절 시 사유 필수 → 관리자에게 알림·목록 노출."""
    b = body or {}
    decision = str(b.get("decision") or "").strip().lower()
    if decision not in ("accepted", "rejected"):
        raise HTTPException(422, {"code": "BAD_DECISION", "allowed": ["accepted", "rejected"]})
    reason = str(b.get("reason") or "").strip()
    if decision == "rejected" and len(reason) < 5:
        raise HTTPException(422, {"code": "REASON_REQUIRED", "hint": "거절 사유 5자 이상"})
    c = _get_case(db, case_id, user)
    a = _ops_latest_assignment(db, [case_id]).get(case_id)
    if not a:
        raise HTTPException(404, {"code": "NOT_ASSIGNED"})
    if a.get("auditor_id") != user["uid"]:
        raise HTTPException(403, {"code": "NOT_YOUR_ASSIGNMENT"})
    if a.get("accept_status") in ("accepted", "rejected"):
        raise HTTPException(409, {"code": "ALREADY_RESPONDED", "accept_status": a["accept_status"]})
    sm.record_event(db, c, c.status, c.status, ASSIGN_RESPONSE_ACTION, user["role"], user["uid"],
                    {"auditor_id": user["uid"], "decision": decision,
                     "reason": reason or None})
    if decision == "accepted":
        _notify(db, c, "auditor.assignment_accepted", "오디터 배정 수락",
                body="%s 님이 심사를 수락했습니다." % (a.get("auditor_name") or ""), role="operator")
    else:
        _notify(db, c, "auditor.assignment_rejected", "오디터 배정 거절 — 재배정 필요",
                body="%s 님이 심사를 거절했습니다. 사유: %s" % (a.get("auditor_name") or "", reason),
                role="operator")
    db.commit()
    return {"ok": True, "case_id": case_id, "decision": decision, "reason": reason or None}


@app.post("/ops/cases/{case_id}/assign-auditor")
def ops_assign_auditor(case_id: str, body: schemas.OpsAssignAuditorReq,
                       user=Depends(auth.require_roles("operator")),
                       db: Session = Depends(get_db)):
    """미배정 케이스에 오디터 배정 — 업무량 기반. WorkflowEvent(latest-wins) + 오디터 알림.
    재배정 시 이전 응답은 자동 무효화(_ops_latest_assignment가 배정 이후 응답만 반영)."""
    c = _get_case(db, case_id, user)
    au = db.get(models.User, body.auditor_id)
    if not au or au.role != "auditor":
        raise HTTPException(404, {"code": "AUDITOR_NOT_FOUND", "auditor_id": body.auditor_id})
    if user["role"] != "admin" and au.org_id != user["org_id"]:
        raise HTTPException(403, {"code": "ORG_FORBIDDEN"})
    sm.record_event(db, c, c.status, c.status, "ops.auditor_assigned", user["role"], user["uid"],
                    {"auditor_id": au.user_id, "auditor_name": au.username})
    _notify(db, c, "ops.auditor_assigned", role="auditor",
            msg=("ops.auditor_assigned", {"company": c.company_name or "",
                                          "auditor": au.username}))
    db.commit()
    cases = _ops_cases(db, user)
    assignments = _ops_latest_assignment(db, [x.case_id for x in cases])
    load = sum(1 for p in assignments.values() if p.get("auditor_id") == au.user_id)
    return {"ok": True, "case_id": case_id, "auditor_id": au.user_id,
            "auditor_name": au.username, "load": load}


_ENUMS = {
    # 원재료 유형 — 라벨은 사전(MATERIAL 축)이 정본이다. 여기서 코드 순서만 정한다.
    # 종전 'sanitizer'는 사전의 'cleaning'(세척제)과 같은 것이라 코드를 사전에 맞췄다.
    "material_type": [(c, _dd_mod.label(k, "ko"))
                      for k, c in (("MATERIAL_RAW", "raw"),
                                   ("MATERIAL_ADDITIVE", "additive"),
                                   ("MATERIAL_PROCESSING_AID", "processing_aid"),
                                   ("MATERIAL_PRESERVATIVE", "preservative"),
                                   ("MATERIAL_CLEANING", "cleaning"),
                                   ("MATERIAL_LUBRICANT", "lubricant"),
                                   ("MATERIAL_PACKAGING", "packaging"))],
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
def get_enums(lang: str = Query("ko"), user=Depends(auth.get_current_user)):
    """전 enum 단일 소스 (프론트 select 데이터연결·드리프트 차단) — 설계 §11.2."""
    from .intake import DOC_TYPES
    DOC_KO = _intake_doc_names(lang)
    out = {k: [{"value": v, "label": lab} for v, lab in items] for k, items in _ENUMS.items()}
    out["doc_type"] = [{"value": d, "label": DOC_KO.get(d, d)} for d in DOC_TYPES]
    return out


@app.post("/ai/explain")
def explain_ingredient(body: schemas.ExplainReq, user=Depends(auth.get_current_user)):
    """성분 설명 — 온톨로지 근거(판정 이유·대체재·증빙) + gemma3 자연어 해설(옵션). 전 역할 허용."""
    lang = (body.lang or "ko").lower()
    lang = lang if lang in ("ko", "en", "id") else "ko"
    exp = screening.explain(body.name, e_number=body.e_number, source=body.source,
                            note=body.note or "", lang=lang)
    if body.llm:
        # 해설도 화면 언어를 따른다 — 판정문만 인니어이고 해설은 한국어면 읽는 사람이 반쪽만 본다.
        _in = {"ko": "한국어", "en": "English", "id": "Bahasa Indonesia"}[lang]
        sysmsg = ("당신은 식품 성분 사전입니다. 주어진 성분이 무엇이고 식품에서 어떤 용도로 쓰이는지 "
                  "%s로 2~3문장으로 설명하세요. 할랄/하람 판정은 하지 말고 성분 자체 설명만 하세요." % _in)
        exp["ai_description"] = ai_local.llm_text(sysmsg, body.name) or ""
    return exp


# 스크리닝 판정 — 오디터가 보는 결론.
_VERDICT_KO = _dd_mod.code_labels("VERDICT", "verdict", "ko")


def verdict_label(code, lang="ko"):
    return _dd_mod.code_labels("VERDICT", "verdict", lang).get(code, code)

def _material_source_docs(db, case_id):
    """M2: 성분별 소스 증빙문서 매핑 material_id → [{document_id, filename}]."""
    out = {}
    rows = db.query(models.DocumentAsset).filter_by(case_id=case_id).filter(models.DocumentAsset.material_id.isnot(None)).all()
    for d in rows:
        out.setdefault(d.material_id, []).append({"document_id": d.document_id,
                                                  "filename": d.filename,
                                                  "filename_en": d.filename_en})
    return out


# ══ 사전심사 심사자 뷰(오디터·샤리아·운영자) — 기업/공장/문서 원문 + 분석 드릴다운 ══
# 부정(하람·부적합) 우선 정렬: 심사자는 문제부터 본다.
_NEG_RANK = {"BLOCK": 0, "NEEDS_EVIDENCE": 1, "CLEARED": 2, "PASS": 2}
_DOC_NEG_RANK = {"rejected": 0, "rework": 1, "pending": 2, None: 2, "approved": 3}


def _preassess_company(db, c):
    """기업·업체 정보(신청서 프로필 + org 상세)."""
    px = c.profile_ext or {}
    org = db.get(models.Org, c.org_id)
    oe = (org.profile_ext or {}) if org else {}
    return {"company_name": c.company_name, "org_id": c.org_id,
            "org_name": (org.name if org else None), "org_address": (org.address if org else None),
            "nib": c.nib, "responsible_person": c.responsible_person,
            "halal_supervisor": c.halal_supervisor, "email": c.email, "phone": c.phone,
            "address": c.address, "pathway": c.pathway, "status": c.status,
            "risk_category": c.risk_category, "is_msme": c.is_msme,
            "created_at": str(c.created_at or "")[:10],
            "profile_ext": {k: px.get(k) for k in
                            ("city", "country", "zip", "business_type", "pic_name", "pic_title",
                             "pic_phone", "pic_email", "cp_name", "cp_title", "cp_phone", "cp_email",
                             "registration_type", "application_type", "registration_status",
                             "product_type", "total_employee", "marketing_type", "tax_id",
                             "production_capacity", "establishment_date", "corporate_reg_no",
                             "office_phone", "company_name_ko", "company_name_en",
                             # 영문 표기 — 인니·영문 보고서가 고유명사를 옮기는 근거 데이터
                             "responsible_person_en", "halal_supervisor_en", "pic_name_en",
                             "responsible_person_en") if px.get(k)},
            "org_profile_ext": oe}


def _preassess_factories(db, c):
    out = []
    for f in _case_facilities(db, c):
        d = _fac_dict(f)
        pe = f.profile_ext or {}
        d["source_docs"] = pe.get("source_docs") or []
        d["name_en"] = pe.get("name_en") or pe.get("company_name_en")   # 영문 시설·법인명(입력값)
        out.append(d)
    return out


def _preassess_dossier(db, c, lang="ko"):
    """사전심사 심사자 뷰 종합 데이터 — 기업/공장 상세 + 문서·재료 분석(부정 우선 정렬).

    · materials: 원재료별 판정·근거·필요증빙·대체재·소스문서(원문 링크) — BLOCK→NEEDS_EVIDENCE→CLEARED 순
    · documents: 문서별 분류·신뢰도·추출필드·검수상태 — rejected→rework→pending→approved 순
    """
    case_id = c.case_id
    rep = _material_report(db, c, lang=lang)
    mats = sorted(rep["materials"],
                  key=lambda m: (_NEG_RANK.get(m.get("verdict"), 9), -(1 if m.get("najis") else 0),
                                 (m.get("name") or "")))
    from .intake import DOC_KO
    docs = []
    for d in db.query(models.DocumentAsset).filter_by(case_id=case_id).all():
        docs.append({"document_id": d.document_id, "filename": d.filename,
                     "filename_en": d.filename_en,
                     "doc_type": d.doc_type, "doc_type_ko": DOC_KO.get(d.doc_type, d.doc_type),
                     "confidence": d.confidence, "fields": d.fields or {},
                     "excerpt": (d.text_excerpt or "")[:1200],
                     "review_status": d.review_status, "has_file": bool(d.content_b64),
                     "material_id": d.material_id, "file_hash": d.file_hash,
                     "uploaded_by": d.uploaded_by, "uploader_role": d.uploader_role,
                     "captured_at": d.captured_at,
                     "created_at": d.created_at.isoformat() if d.created_at else None})
    docs.sort(key=lambda x: (_DOC_NEG_RANK.get(x["review_status"], 2),
                             (x["confidence"] or 0), x["filename"] or ""))
    return {"case_id": case_id, "company": _preassess_company(db, c),
            "factories": _preassess_factories(db, c),
            "materials": mats, "summary": rep["summary"],
            "quantitative": rep.get("quantitative") or [],
            "quant_fail": rep.get("quant_fail", 0),
            "documents": docs,
            "doc_counts": {"total": len(docs),
                           "negative": sum(1 for d in docs if d["review_status"] in ("rejected", "rework")),
                           "pending": sum(1 for d in docs if not d["review_status"] or d["review_status"] == "pending"),
                           "approved": sum(1 for d in docs if d["review_status"] == "approved")},
            "review": _preassess_review_latest(db, case_id),
            "doc_requests": _preassess_doc_requests(db, case_id)}


@app.get("/cases/{case_id}/preassess/dossier")
def get_preassess_dossier(case_id: str, lang: str = Query("ko"),
                          user=Depends(auth.require_roles("auditor", "fatwa_liaison", "operator",
                                                          "consultant")),
                          db: Session = Depends(get_db)):
    """사전심사 심사자 뷰(오디터·샤리아·운영자·컨설턴트) 종합 데이터.

    lang 을 안 받으면 성분 판정문이 한국어로 나간다 — 오디터가 읽는 핵심 내용이다."""
    c = _get_case(db, case_id, user)
    return _preassess_dossier(db, c, lang=lang)


def _fn_en(name):
    """한글 파일명의 영문 표시명 — 결정적 용어사전. 영문 파일명이면 None(중복 저장 안 함)."""
    from .filename_l10n import to_en
    if not name:
        return None
    en, _full = to_en(name)
    return en if en != name else None


def _ent(v, lang, v_en=None):
    """보고서 데이터 필드의 표시값.
    en·id일 때 (1) 신청서에 입력된 영문명(v_en) (2) 법인격·직책 같은 일반 용어 치환 순으로 쓴다.
    영문명이 없으면 원문을 남긴다 — 고유명사를 코드에 박으면 그 업체에만 통한다."""
    from .filename_l10n import entity
    return entity(v, lang, v_en)


def _fn_show(d, lang):
    """표시용 파일명 — ko는 원문, en·id는 저장된 영문 표시명(없으면 즉석 변환·그래도 없으면 원문)."""
    nm = d.get("filename") if isinstance(d, dict) else getattr(d, "filename", None)
    if (lang or "ko").lower() == "ko":
        return nm
    en = d.get("filename_en") if isinstance(d, dict) else getattr(d, "filename_en", None)
    return en or _fn_en(nm) or nm


# 사전심사 체크리스트에서 조립되는 짧은 문구 — 표 밖에서 문자열로 만들어져
# 번역을 못 받고 있었다. %d 는 인증번호 건수(언어마다 자리가 달라 문장째 둔다).
# 체크리스트에서 조립되는 짧은 문구 — 사전에서 꺼낸다.
# 사전 키는 '한국어 원문 그대로'다. 건수 자리(%d)가 든 문장은 키에도 %d 가 있어야
# 조회가 되고, 안 그러면 키가 그대로 돌아와 뒤의 % 서식이 TypeError 로 터진다
# (실측: 서류 체크리스트가 3개 언어 모두 500. 공급사 인증번호가 있는 케이스 전부).
_CERT_NO_NOTE = "공급사 인증번호 %d건 확보 — 원본 서류 미제출(번호 대조는 오디터)"
_CHECKLIST_L10N = {ko: {lg: _dd_mod.text(ko, lg) for lg in ("ko", "en", "id")}
                   for ko in ("업로드 파일", "생성 문서", _CERT_NO_NOTE)}

def _intake_doc_names(lang):
    """서류명 표 — 지원 언어면 그 표를, 아니면 한국어 표를 돌려준다(빈칸 방지)."""
    from .intake import DOC_KO, DOC_NAME_L10N
    return DOC_NAME_L10N.get((lang or "ko").lower()) or DOC_KO


# 사전심사 보고서 고정 라벨 — 도메인 사전(domain_dict.json)의 LABEL 축에서 꺼낸다.
# 전에는 여기 EN·ID 두 벌을 손으로 유지했다. 같은 문구를 프런트(UI_STRINGS)와 서버가
# 따로 갖고 있어 한쪽만 고쳐지는 일이 생겼다. 사전 한 곳으로 모은다.


def _pre_rpt_lang(lang):
    """보고서 라벨 번역기 — 미지원 언어·미등록 문구는 한국어 원문을 그대로 돌려준다
    (누락이 빈칸이 되지 않게)."""
    from .domain_dict import text_fn
    return text_fn(lang)


def _preassess_report_filename(c, lang, ext):
    """Preassess_Report_{회사명}_{lang}_{YYYY-MM-DD}.{ext}"""
    import re as _re
    name = c.company_name or c.case_id[:8]
    name = _re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", name).strip() or c.case_id[:8]
    return "Preassess_Report_%s_%s_%s.%s" % (name[:60], (lang or "ko").lower(),
                                             date.today().isoformat(), ext)


@app.post("/util/romanize")
def util_romanize(body: dict = None, user=Depends(auth.get_current_user)):
    """한글 인명·표기를 로마자로 변환(제안값). 확정은 사람이 한다 — 상호는 로마자 표기와
    실제 영문 상호가 다를 수 있다(바이오로제트 → 표기 Baiorojeteu / 상호 BIOROSETTE)."""
    from .romanize import romanize, romanize_name
    b = body or {}
    names = b.get("names") or {}
    kind = (b.get("kind") or "name").lower()
    fn = romanize_name if kind == "name" else romanize
    return {"kind": kind, "result": {k: (fn(v) if v else "") for k, v in names.items()},
            "note": "제안값입니다. 여권·법인 등기 표기와 다를 수 있으니 확인 후 저장하세요."}


@app.get("/cases/{case_id}/preassess-report.docx")
def preassess_report_docx(case_id: str, lang: str = Query("ko"),
                          user=Depends(auth.require_roles("auditor", "fatwa_liaison", "operator",
                                                          "consultant")),
                          db: Session = Depends(get_db)):
    """사전심사 결과 보고서 — 편집 가능한 Word(.docx). 기업·공장·문서·성분(부정 우선)·판정 수록.
    lang=ko|en|id — 제목·표머리·판정어 등 고정 라벨을 현지화한다(성분 근거 서술은 원문 유지)."""
    import io as _io
    import docx as _docx
    from docx.shared import Pt
    from fastapi.responses import Response
    c = _get_case(db, case_id, user)
    dos = _preassess_dossier(db, c, lang=lang)
    L = _pre_rpt_lang(lang)
    _EVID, _ALT = screening.term_tables(lang)   # 증빙코드·대체재도 같은 언어로
    doc = _docx.Document()
    doc.add_heading(L("사전심사 결과 보고서 · Pre-assessment Report"), level=0)
    _cpx = c.profile_ext or {}
    doc.add_paragraph("GL-HAC AI · %s · %s %s"
                      % (_ent(c.company_name, lang, _cpx.get("company_name_en")) or "-",
                         L("생성일"), date.today().isoformat()))

    def table(rows, headers=None):
        t = doc.add_table(rows=0, cols=len(headers or rows[0]))
        t.style = "Light Grid Accent 1"
        if headers:
            hc = t.add_row().cells
            for i, h in enumerate(headers):
                hc[i].text = str(h)
        for r in rows:
            rc = t.add_row().cells
            for i, v in enumerate(r):
                rc[i].text = "" if v is None else str(v)
        return t

    comp = dos["company"]
    doc.add_heading(L("1. 기업 정보 · Company"), level=1)
    px = comp.get("profile_ext") or {}
    table([[L("기업명"), _ent(comp.get("company_name"), lang, px.get("company_name_en"))],
           ["NIB", comp.get("nib")],
           [L("대표/책임자"), _ent(comp.get("responsible_person"), lang,
                                px.get("responsible_person_en"))],
           [L("할랄 감독자"), _ent(comp.get("halal_supervisor"), lang,
                                px.get("halal_supervisor_en"))],
           [L("주소"), comp.get("address")],
           [L("연락처"), "%s / %s" % (comp.get("phone") or "-", comp.get("email") or "-")],
           [L("경로 · Pathway"), comp.get("pathway")], [L("위험등급"), comp.get("risk_category")],
           [L("등록유형"), px.get("registration_type")], [L("신청유형"), px.get("application_type")],
           [L("담당자(PIC)"), "%s %s" % (_ent(px.get("pic_name"), lang,
                                             px.get("pic_name_en")) or "-",
                                        _ent(px.get("pic_title"), lang) or "")],
           [L("총 직원 수"), px.get("total_employee")], [L("생산능력"), px.get("production_capacity")]],
          headers=[L("항목"), L("내용")])

    doc.add_heading(L("2. 공장·시설 정보 · Facilities"), level=1)
    facs = dos.get("factories") or []
    if facs:
        table([[_ent(f.get("label") or f.get("name"), lang, f.get("name_en")),
                f.get("reg_no"), f.get("address"),
                "%s / %s" % (f.get("city") or "-", f.get("country") or "-")] for f in facs],
              headers=[L("공장"), L("등록번호"), L("주소"), L("도시/국가")])
    else:
        doc.add_paragraph(L("등록된 공장 정보 없음."))

    doc.add_heading(L("3. 문서 분석 · Document Analysis"), level=1)
    # 업로드된 '할랄 인증서'는 외부로 나가는 산출물에서 제외한다. 정본은 이 플랫폼이 발급하는
    # 인증서이고, 타기관 인증서가 같은 문서에 실리면 어느 것이 유효한지 헷갈린다.
    # 심사자용 화면(dossier)에는 그대로 남아 있어 검토에는 지장이 없다.
    _all_docs = dos.get("documents") or []
    docs = [d for d in _all_docs if d.get("doc_type") != "halal_certificate"]
    _hidden = len(_all_docs) - len(docs)
    neg = [d for d in docs if d.get("review_status") in ("rejected", "rework")]
    rest = [d for d in docs if d not in neg]
    doc.add_paragraph(L("총 %d건 · 부정(반려·재작업) %d건") % (len(docs), len(neg)))
    if _hidden:
        doc.add_paragraph(L("※ 업로드된 할랄 인증서 %d건은 이 보고서에 싣지 않습니다 — "
                            "할랄 인증서는 본 기관이 발급하며, 제출본은 심사 화면에서 확인합니다.")
                          % _hidden)
    if docs:
        _dn = _intake_doc_names(lang)
        table([[_fn_show(d, lang), _dn.get(d.get("doc_type"), d.get("doc_type_ko")),
                ("%.0f%%" % (100 * d["confidence"])) if d.get("confidence") else "-",
                d.get("review_status") or L("미검수")] for d in (neg + rest)],
              headers=[L("파일"), L("분류"), L("AI 신뢰도"), L("검수 상태")])

    doc.add_heading(L("4. 성분(원재료) 분석 · Material Analysis"), level=1)
    s = dos["summary"]
    doc.add_paragraph(L("총 %d건 · 차단(하람) %d · 증빙필요 %d · 적합 %d · najis 위험 %d")
                      % (s["total"], s["blocked"], s["needs_evidence"], s["cleared"], s["najis"]))
    doc.add_paragraph(L("※ 부정 항목(차단·증빙필요)을 먼저 기재합니다."))
    # 심사자는 판정만으로 확인할 수 없다 — 이 값이 어느 파일에서 왔는지 밝힌다.
    # 성분에 직접 연결된 문서가 없으면 케이스에 올라온 원재료 목록 원본을 출처로 적는다.
    _srcdocs = _material_source_docs(db, case_id)
    _mlist = [_fn_show(d, lang) for d in (dos.get("documents") or [])
              if d.get("doc_type") == "material_list" and d.get("filename")]
    if _mlist:
        doc.add_paragraph("%s: %s" % (L("원재료 정보 출처"), ", ".join(_mlist)))
    VK = {"BLOCK": L("차단(하람)"), "NEEDS_EVIDENCE": L("증빙 필요"),
          "CLEARED": L("적합"), "PASS": L("적합")}
    for m in dos["materials"]:
        h = doc.add_heading("%s — %s" % (m.get("name"), VK.get(m.get("verdict"), m.get("verdict") or "-")), level=2)
        for r in h.runs:
            r.font.size = Pt(12)
        if m.get("explanation"):
            doc.add_paragraph(m["explanation"])
        meta = []
        if m.get("severity"):
            meta.append("%s %s" % (L("심각도"), m["severity"]))
        if m.get("najis"):
            meta.append(L("najis 위험"))
        if m.get("required_evidence"):
            meta.append(L("필요 증빙") + ": "
                        + ", ".join(_EVID.get(x, x) for x in m["required_evidence"]))
        if m.get("alternatives"):
            meta.append(L("대체재") + ": "
                        + ", ".join(_ALT.get(x, x) for x in m["alternatives"]))
        _sd = m.get("source_docs") or _srcdocs.get(m.get("material_id")) or []
        if _sd:
            meta.append(L("출처 문서") + ": " + ", ".join(
                _fn_show(d, lang) or d.get("document_id") for d in _sd))
        elif _mlist:
            meta.append("%s: %s" % (L("출처"), _mlist[0]))
        if meta:
            doc.add_paragraph(" · ".join(meta))

    # 공급사 할랄 인증번호 — 인증서 원본은 오디터가 대조할 몫이고, 시스템은 '무엇을 대조해야 하는지'를
    # 빠짐없이 제시한다. 번호가 없는 원재료는 확보 대상으로 함께 센다(없는 것을 있다고 하지 않는다).
    certs = [m for m in dos["materials"] if (m.get("cert_no") or "").strip()]
    if certs:
        doc.add_heading(L("5. 공급사 할랄 인증번호 · Supplier Halal Certificates"), level=1)
        doc.add_paragraph(L("총 %d건 · 인증번호 미확보 %d건 — 번호 대조는 오디터가 수행합니다.")
                          % (len(certs), len(dos["materials"]) - len(certs)))
        table([[m.get("name"), m.get("supplier") or "-", m.get("cert_no"), m.get("origin") or "-"]
               for m in sorted(certs, key=lambda x: (x.get("name") or ""))],
              headers=[L("원재료"), L("공급사"), L("인증번호"), L("원산지")])

    q = [x for x in (dos.get("quantitative") or []) if x.get("value") is not None]
    if q:
        doc.add_heading(L("6. 정량 기준 비교 · Quantitative"), level=1)
        table([[x["param_ko"], "%s %s" % (x["value"], x.get("unit") or ""),
                "≤ %s" % x.get("threshold"),
                {"pass": L("적합"), "fail": L("부적합")}.get(x.get("verdict"), "-")]
               for x in q], headers=[L("항목"), L("측정값"), L("기준"), L("판정")])

    rv = dos.get("review") or {}
    doc.add_heading(L("7. 오디터 검토 결과 · Auditor Review"), level=1)
    if rv:
        secko = {"documents": L("문서"), "materials": L("재료"), "process": L("제조")}
        table([[secko.get(k, k), L("적합") if (v or {}).get("ok") else L("보완"),
                (v or {}).get("note") or ""]
               for k, v in (rv.get("sections") or {}).items()],
              headers=[L("섹션"), L("판정"), L("코멘트")])
        doc.add_paragraph("%s: %s" % (L("종합 판정"),
                          {"ready": L("적합(진행 가능)"), "supplement": L("보완 필요")}
                          .get(rv.get("verdict"), rv.get("verdict") or L("미검토"))))
        if rv.get("note"):
            doc.add_paragraph(L("검토 총평") + ": " + rv["note"])
    else:
        doc.add_paragraph(L("오디터 검토 미기록."))
    doc.add_paragraph("")
    doc.add_paragraph(L("※ 본 보고서는 AI 온톨로지 기반 준비용 분석이며, "
                        "공식 판정은 BPJPH/MUI Fatwa 절차로 확정됩니다."))

    _audit(db, user, "preassess_report.docx", "case", case_id, case_id, {"lang": lang})
    db.commit()
    buf = _io.BytesIO()
    doc.save(buf)
    return Response(content=buf.getvalue(),
                    media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    headers={"Content-Disposition":
                             _content_disposition(_preassess_report_filename(c, lang, "docx"))})


@app.get("/cases/{case_id}/preassess-report.pdf")
def preassess_report_pdf(case_id: str, lang: str = Query("ko"),
                         user=Depends(auth.require_roles("auditor", "fatwa_liaison", "operator",
                                                         "consultant")),
                         db: Session = Depends(get_db)):
    """사전심사 보고서 PDF — docx를 LibreOffice로 변환(인라인 미리보기용, docx와 동일 양식).
    soffice 부재 시 docx 그대로 반환(다운로드 폴백)."""
    from fastapi.responses import Response
    resp = preassess_report_docx(case_id, lang, user, db)   # 동일 로직 재사용 → docx bytes
    try:
        pdf = _docx_to_pdf_bytes(resp.body)
    except Exception as e:  # noqa: BLE001
        log.warning("preassess docx->pdf 변환 실패, docx 반환: %s", e)
        return resp
    c = _get_case(db, case_id, user)
    return Response(content=pdf, media_type="application/pdf",
                    headers={"Content-Disposition":
                             _content_disposition(
                                 _preassess_report_filename(c, lang, "pdf")).replace(
                                     "attachment;", "inline;")})


@app.post("/cases/{case_id}/material-report/snapshot")
def save_material_report_snapshot(case_id, body: dict = None, user=Depends(auth.get_current_user), db=Depends(get_db)):
    """M2: 오디터 체크 + 성분 리포트 스냅샷을 gen-doc(material_report)로 저장(이력 보존·클라이언트 전달)."""
    c = _get_case(db, case_id, user)
    b = body or {}
    checked = b.get("checked") or []
    note = b.get("note") or ""
    mats = db.query(models.Material).filter_by(case_id=case_id).all()
    src = _material_source_docs(db, case_id)
    lines = [
        "[성분 분석 리포트 스냅샷 — %s]" % (c.company_name or case_id[:8]),
        "체크 완료: %d / 전체 %d" % (len(checked), len(mats)),
        "확인자: %s (%s)" % (user.get("uid"), user.get("role")),
        "",
    ]
    for m in mats:
        docs = src.get(m.material_id) or []
        mark = "✔" if m.material_id in checked else "·"
        doclbl = ", ".join(d["filename"] for d in docs) or "-"
        lines.append("%s %s — 문서: %s" % (mark, m.name, doclbl))
    if note:
        lines += ["", "메모: " + note]
    content = "\n".join(lines)
    g = _save_gendoc(db, c, "material_report", content, user)
    _audit(db, user, "material_report.snapshot", "case", case_id)
    db.commit()
    return {"gen_doc_id": g.gen_doc_id, "version": g.version, "checked": len(checked), "total": len(mats)}


def _material_report(db, c, lang="ko"):
    """케이스 원재료 전수를 온톨로지로 분석해 종합 보고서 데이터로 집계 (설계 C·v2 이관).
    lang은 판정 근거 서술의 언어만 바꾼다 — 판정 값 자체는 언어와 무관하다."""
    # 증빙코드·대체재·판정 표기는 읽는 사람 언어로 — 코드값 그대로 내보내면
    # 인니 심사자 화면에 한국어가 남는다.
    _evt, _altt = screening.term_tables(lang)
    _vk = _dd_mod.code_labels("VERDICT", "verdict", lang) or _VERDICT_KO
    mats = db.query(models.Material).filter_by(case_id=c.case_id).order_by(models.Material.name).all()
    _src = _material_source_docs(db, c.case_id)  # M2: 성분별 소스문서(고유번호·위치)
    rows, summary = [], {"total": 0, "cleared": 0, "needs_evidence": 0, "blocked": 0,
                         "najis": 0, "critical": []}
    _CATS = {"제품 원재료": ("raw", "additive", "processing_aid"), "세척제": ("sanitizer",),
             "포장재": ("packaging",), "윤활제": ("lubricant",)}
    cat_counts = {k: 0 for k in _CATS}
    for m in mats:
        _mt = (m.mat_type or "").lower()
        for _k, _ts in _CATS.items():
            if _mt in _ts:
                cat_counts[_k] += 1
        exp = screening.explain(m.name, e_number=m.e_number, source=m.source,
                                cert_no=m.cert_no, note=m.note or "", lang=lang)
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
                     "verdict": v,
                     # 판정·증빙·대체재 표기는 읽는 사람 언어로. verdict(코드)는 그대로 둔다
                     # — 프런트가 코드로 분기하므로 계약을 바꾸면 안 된다.
                     "verdict_ko": _vk.get(v, v), "severity": exp.get("severity"),
                     "category": exp.get("category"), "najis": exp.get("najis"),
                     "required_evidence": [_evt.get(x, x)
                                           for x in (exp.get("required_evidence") or [])],
                     "alternatives": [_altt.get(x, x) for x in (exp.get("alternatives") or [])],
                     "evidence_count": m.evidence_count if hasattr(m, "evidence_count") else None,
                     "source_docs": _src.get(m.material_id) or [],  # M2: 문서 고유번호·위치→뷰어링크
                     # 공급사 할랄 인증번호 — 오디터가 발급기관에 대조할 값
                     "supplier": m.supplier, "cert": m.cert, "cert_no": m.cert_no,
                     "origin": m.origin,
                     "explanation": exp.get("explanation") or ""})
    category_registration = [{"category": k, "count": cat_counts[k],
                              "registered": cat_counts[k] > 0} for k in _CATS]
    # P3: 정량 측정값(파라미터별 최신) — 정량 기준 비교표의 '측정값' 컬럼 소스
    meas = {}
    for r in (db.query(models.MaterialMeasurement).filter_by(case_id=c.case_id)
              .order_by(models.MaterialMeasurement.created_at.asc()).all()):
        crit = QUANT_CRITERIA.get(r.param_key, {})
        meas[r.param_key] = {"param_key": r.param_key, "param_ko": crit.get("ko", r.param_key),
                             "value": r.value, "unit": r.unit or crit.get("unit"),
                             "threshold": crit.get("max"), "basis": crit.get("basis"),
                             "verdict": r.verdict, "material_id": r.material_id,
                             "lab_name": r.lab_name, "tested_at": r.tested_at,
                             "document_id": r.document_id}
    quant = [meas.get(k) or {"param_key": k, "param_ko": v["ko"], "value": None, "unit": v["unit"],
                             "threshold": v["max"], "basis": v["basis"], "verdict": "unknown"}
             for k, v in QUANT_CRITERIA.items()]
    return {"case_id": c.case_id, "company_name": c.company_name,
            "summary": summary, "materials": rows,
            "category_registration": category_registration,
            "quantitative": quant,
            "quant_measured": len(meas), "quant_total": len(QUANT_CRITERIA),
            "quant_fail": sum(1 for q in quant if q.get("verdict") == "fail")}


@app.get("/cases/{case_id}/material-report")
def material_report(case_id: str, lang: str = Query("ko"),
                    user=Depends(auth.get_current_user), db: Session = Depends(get_db)):
    """성분 AI 분석 보고서 — 원재료 전수 온톨로지 판정·근거·대체재·증빙 집계."""
    c = _get_case(db, case_id, user)
    rep = _material_report(db, c, lang=lang)
    _audit(db, user, "material_report.view", "case", case_id)
    db.commit()
    return rep


@app.get("/cases/{case_id}/material-report.pdf")
def material_report_pdf(case_id: str, lang: str = Query("ko"),
                        user=Depends(auth.get_current_user), db: Session = Depends(get_db)):
    """성분 AI 분석 보고서 PDF."""
    from fastapi.responses import Response
    from urllib.parse import quote
    c = _get_case(db, case_id, user)
    rep = _material_report(db, c, lang=lang)
    s = rep["summary"]
    L = ["[요약]",
         "총 원재료: %d건" % s["total"],
         "할랄 허용: %d · 증빙 필요: %d · 차단(하람): %d · najis 위험: %d"
         % (s["cleared"], s["needs_evidence"], s["blocked"], s["najis"]),
         "주의 성분: %s" % (", ".join(s["critical"]) or "없음"), "",
         "[필수 원재료 카테고리 등록여부]"]
    for cr in rep.get("category_registration", []):
        L.append("• %s: %s (%d건)" % (cr["category"], "등록" if cr["registered"] else "미등록",
                                      cr["count"]))
    _miss = [cr["category"] for cr in rep.get("category_registration", [])
             if cr["category"] != "제품 원재료" and not cr["registered"]]
    if _miss:
        L.append("[경고] %s 원재료 미등록 — 사전심사·현장심사에서 보완 요청 발생 가능" % "·".join(_miss))
    L += ["", "[성분별 분석]"]
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
    r = ai_local.llm_text_result(sysmsg, body.question)
    ans, err = r["text"], r["error"]
    # 답을 못 준 이유를 셋으로 갈라 말한다. 종전에는 전부 '(LLM 응답 없음)' 이라
    # 꺼져 있는 건지, 실패한 건지, 정말 할 말이 없는 건지 사용자가 알 수 없었다.
    # answer 는 비우지 않는다 — 화면이 빈 말풍선을 띄우는 것보다 이유를 읽는 편이 낫다.
    status = "ok"
    if err == "LLM_UNAVAILABLE":
        status, ans = "unavailable", _ASK_MSG["unavailable"]
    elif err:
        status, ans = "failed", _ASK_MSG["failed"]
    elif not ans:
        status, ans = "empty", _ASK_MSG["empty"]
    return {"answer": ans, "status": status, "error": err if status == "failed" else None,
            "retryable": status == "failed", "context_facts": ctx,
            "domain_sources": domain_sources, "long_term_sources": long_term_sources}


# 채팅이 답을 못 준 이유 — 세 가지를 다른 말로 한다. '응답 없음' 한 마디로 뭉치면
# 사용자는 다시 물어봐야 할지, 관리자를 불러야 할지 판단할 수 없다.
_ASK_MSG = {
    "unavailable": "AI가 연결되어 있지 않습니다 — 관리자에게 문의하세요(다시 물어봐도 같습니다).",
    "failed": "AI 호출이 실패했습니다 — 잠시 후 다시 물어보세요(답이 없는 것이 아닙니다).",
    "empty": "AI가 답을 만들지 못했습니다 — 질문을 조금 더 구체적으로 적어 보세요.",
}


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
# 증빙 항목 이름은 업체가 무엇을 올려야 하는지 보는 문구다 — 한국어만 두면 인니 업체가
# 무슨 서류인지 알 수 없다.
# 증빙 항목 이름은 업체가 무엇을 올려야 하는지 보는 문구다 — 사전 한 곳에서 꺼낸다.
SJPH_EVIDENCE_L10N = {lg: _dd_mod.code_labels("SJPH_EVIDENCE", "sjph_evidence", lg)
                      for lg in ("en", "id")}

def _evidence_label(key, ko, lang="ko"):
    return (SJPH_EVIDENCE_L10N.get((lang or "ko").lower()) or {}).get(key) or ko


# HPAS 5요소 — 이름과 '왜 이 판정인지'를 함께 현지화한다. 판정 근거가 한국어로 남으면
# 인니 심사자는 결과만 보고 이유를 못 읽는다.
# HPAS 5요소 — 이름과 '왜 이 판정인지'를 함께 사전에서. 판정 근거가 한국어로 남으면
# 인니 심사자는 결과만 보고 이유를 못 읽는다.
def _hpas_l10n(lang):
    el = _dd_mod.code_labels("HPAS_ELEMENT", "hpas_element", lang)
    rsn = _dd_mod.code_labels("HPAS_REASON", "hpas_reason", lang)
    out = dict(el)
    out.update({"r_" + k: v for k, v in rsn.items()})
    return out


_HPAS_L10N = {lg: _hpas_l10n(lg) for lg in ("ko", "en", "id")}

# 본문까지 열어볼 문서 유형 — 유형이 이미 특정된 문서는 여기서 제외한다.
_BODY_PARSE_TYPES = {"other", "sjph_manual", "sjph_evidence"}
_BODY_PARSE_MAX_B64 = 3_000_000     # 앞머리 400자 보려고 큰 파일을 통째로 열지 않는다


def _evidence_key_for_doc(d):
    """이 문서가 채울 SJPH 증빙 항목 — 파일명 우선, 없으면 본문 앞머리.

    파일명만 보면 놓친다. 'Halal_Team.pdf'는 이름만으로는 팀 소개서인지 지정서(SK)인지
    알 수 없지만 본문 제목이 'KEPUTUSAN PENETAPAN TIM MANAJEMEN HALAL'이다.
    이미지는 본문을 보지 않는다 — OCR 비용이 편철 하나 값보다 크다."""
    from . import domain_dict as dd
    base = re.sub(r"\.[A-Za-z0-9]{2,5}$", "", d.filename or "")
    k = dd.evidence_key_of(base)
    if k:
        return k, "파일명"
    if (d.content_type or "").startswith("image/"):
        return None, None
    # 본문 파싱은 '이름으로 모르는 문서'에만 쓴다. 유형이 이미 밝혀진 문서(COA/MSDS·
    # 원산지증명 등)는 SJPH 증빙 슬롯의 후보가 아니다. 이 가드가 없으면 성적서 54건
    # 22MB를 통째로 파싱하다 편철 한 번에 2분이 넘는다(질경이 실측).
    if (d.doc_type or "other") not in _BODY_PARSE_TYPES:
        return None, None
    head = (d.text_excerpt or "")[:400]
    if not head and d.content_b64 and len(d.content_b64) <= _BODY_PARSE_MAX_B64:
        # 범용 업로드 경로는 본문을 추출하지 않는다(text_excerpt가 비어 있다).
        # 편철 판단에 필요한 앞머리만 여기서 읽는다 — 이미지는 위에서 이미 걸렀다.
        try:
            from .intake import parse_file
            head = (parse_file(d.filename or "",
                               base64.b64decode(str(d.content_b64).split(",")[-1])) or "")[:400]
        except Exception:  # noqa: BLE001
            head = ""
    return (dd.evidence_key_of(head), "본문") if head else (None, None)


def _autofile_evidence(db, case_id, d, user=None):
    """문서를 비어 있는 SJPH 증빙 항목에 편철. 이미 채워진 항목은 건드리지 않는다
    (사람이 고른 증빙을 자동 판단이 덮으면 안 된다). 무엇을 근거로 넣었는지 남긴다."""
    key, how = _evidence_key_for_doc(d)
    if not key or key not in {k for k, _ in SJPH_EVIDENCE_ITEMS}:
        return None
    if db.query(models.SjphEvidence).filter_by(case_id=case_id, item_key=key).first():
        return None
    db.add(models.SjphEvidence(case_id=case_id, item_key=key,
                               filename=d.filename, document_id=d.document_id))
    if user:
        _audit(db, user, "sjph.evidence.autofile", "document", d.document_id,
               meta={"item_key": key, "matched_by": how, "filename": d.filename})
    return key


@app.post("/cases/{case_id}/sjph-evidence/autofile")
def autofile_sjph_evidence(case_id: str,
                           user=Depends(auth.require_roles("applicant", "penyelia_halal",
                                                           "consultant", "admin")),
                           db: Session = Depends(get_db)):
    """이미 올라온 문서를 비어 있는 증빙 항목에 편철한다.

    ZIP 일괄 업로드로 들어온 인니 실무 기록물(Catatan…·Denah…)은 doc_type이 other라
    필수 서류 체크리스트에는 안 걸리지만 SJPH 증빙 항목에는 제자리가 있다.
    비어 있는 항목만 채우므로 여러 번 눌러도 결과가 같다."""
    _get_case(db, case_id, user)
    # 채울 자리가 없으면 문서를 열어보지 않는다 — 다시 눌러도 즉시 끝나야 한다.
    have = {e.item_key for e in db.query(models.SjphEvidence).filter_by(case_id=case_id).all()}
    if have >= {k for k, _ in SJPH_EVIDENCE_ITEMS}:
        return {"filled": [], "count": 0, "note": "빈 증빙 항목 없음"}
    filled = []
    for d in (db.query(models.DocumentAsset).filter_by(case_id=case_id)
              .order_by(models.DocumentAsset.created_at).all()):
        k = _autofile_evidence(db, case_id, d, user)
        if k:
            filled.append({"item_key": k, "filename": d.filename})
    db.commit()
    return {"filled": filled, "count": len(filled)}


@app.get("/cases/{case_id}/sjph-evidence")
def list_sjph_evidence(case_id: str, lang: str = Query("ko"),
                       user=Depends(auth.get_current_user), db: Session = Depends(get_db)):
    _get_case(db, case_id, user)
    have = {e.item_key: e for e in db.query(models.SjphEvidence).filter_by(case_id=case_id)}
    items = [{"item_key": k, "label": _evidence_label(k, ko, lang), "uploaded": k in have,
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
    doc = models.DocumentAsset(case_id=case_id, filename=body.filename,
                                filename_en=_fn_en(body.filename), doc_type="sjph_evidence",
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


# P2-5: 평가 항목별 오디터 판정(3-state) latest-wins — WorkflowEvent(action=evaluation.verdict)
HPAS_AUTO_ELEMENTS = ("commitment", "materials", "process", "product", "monitoring")
# 항목별 SJPH 증빙 키 매핑(증거카운트용)
_ELEMENT_EV_KEYS = {
    "commitment": ["halal_supervisor", "training"],
    "process": ["production_flow", "facility_layout"],
    "monitoring": ["internal_audit", "purchase_log", "receiving_log",
                   "usage_log", "production_log", "distribution_log"],
}


def _eval_verdicts(db, case_id):
    """행별 오디터 판정 latest-wins: element -> 'good'|'gap'|'fail'."""
    out = {}
    rows = (db.query(models.WorkflowEvent)
            .filter_by(case_id=case_id, action="evaluation.verdict")
            .order_by(models.WorkflowEvent.created_at.asc(),
                      models.WorkflowEvent.event_id.asc()).all())
    for ev in rows:
        p = ev.payload or {}
        if p.get("element"):
            out[p["element"]] = p.get("verdict")
    return out


@app.get("/cases/{case_id}/hpas-auto")
def hpas_auto(case_id: str, lang: str = Query("ko"),
              user=Depends(auth.get_current_user), db: Session = Depends(get_db)):
    """HPAS 5요소 자동 증빙 유도 (업로드/원재료/제품/매트릭스/SJPH증빙에서) — 설계 G3.
    P2-5: status(ok/gap 하위호환 유지)에 더해 3-state(state3: good/gap/fail, 오디터 판정 우선),
    항목별 증거카운트(evidence_docs 📎 / evidence_media 🎥) 제공."""
    c = _get_case(db, case_id, user)
    pen = db.query(models.PenyeliaHalal).filter_by(org_id=c.org_id, status="active").count() > 0
    mats = db.query(models.Material).filter_by(case_id=case_id).all()
    bad = [m for m in mats if m.screen_result in ("BLOCK", "NEEDS_EVIDENCE")]
    prods = db.query(models.Product).filter_by(case_id=case_id).count()
    photos = db.query(models.DocumentAsset).filter_by(case_id=case_id, doc_type="product_photo").count()
    vids = (db.query(models.DocumentAsset)
            .filter(models.DocumentAsset.case_id == case_id,
                    models.DocumentAsset.content_type.like("video/%")).count())
    mat_ev = sum(1 for m in mats if m.evidence_provided)
    sev = {e.item_key for e in db.query(models.SjphEvidence).filter_by(case_id=case_id)}
    verdicts = _eval_verdicts(db, case_id)

    def st(cond):
        return "ok" if cond else "gap"

    def ev_docs(el):
        if el == "materials":
            return mat_ev
        if el == "product":
            return photos
        return sum(1 for k in _ELEMENT_EV_KEYS.get(el, []) if k in sev)

    def ev_media(el):
        return vids if el in ("product", "process") else 0

    def merge3(el, det):   # 오디터 판정 우선, 없으면 자동판정(ok→good, gap→gap)
        v = verdicts.get(el)
        if v in ("good", "gap", "fail"):
            return v
        return "good" if det == "ok" else "gap"

    # HPAS 5요소 라벨·판정근거 — 심사자가 화면에서 읽는 문구다.
    _hl = _HPAS_L10N.get((lang or "ko").lower()) or _HPAS_L10N["ko"]
    base = [
        ("commitment", _hl["commitment"], st(pen and "halal_supervisor" in sev), _hl["r_commitment"]),
        ("materials", _hl["materials"], st(bool(mats) and not bad), _hl["r_materials"] % len(bad)),
        ("process", _hl["process"], st("production_flow" in sev), _hl["r_process"]),
        ("product", _hl["product"], st(prods > 0 and photos > 0), _hl["r_product"] % (prods, photos)),
        ("monitoring", _hl["monitoring"], st("internal_audit" in sev), _hl["r_monitoring"]),
    ]
    elements = [{"element": el, "label": lab, "status": det, "reason": rsn,
                 "state3": merge3(el, det), "verdict": verdicts.get(el),
                 "evidence_docs": ev_docs(el), "evidence_media": ev_media(el)}
                for el, lab, det, rsn in base]
    ok = sum(1 for e in elements if e["status"] == "ok")
    good = sum(1 for e in elements if e["state3"] == "good")
    fail = sum(1 for e in elements if e["state3"] == "fail")
    return {"elements": elements, "auto_completion": round(ok / 5 * 100),
            "good": good, "gap": 5 - good - fail, "fail": fail}


@app.post("/cases/{case_id}/evaluation/verdict")
def evaluation_verdict(case_id: str, body: dict = None,
                       user=Depends(auth.require_roles("auditor", "operator")),
                       db: Session = Depends(get_db)):
    """P2-5: 평가 항목 행별 오디터 판정(good/gap/fail) 저장(WorkflowEvent latest-wins) +
    부적합/보완 시 시정조치(CAR) 자동생성(finding_id='eval:{element}', CorrectiveAction 흐름 재사용)."""
    body = body or {}
    el = str(body.get("element") or "").strip()
    vd = str(body.get("verdict") or "").strip()
    if el not in HPAS_AUTO_ELEMENTS:
        raise HTTPException(422, {"code": "BAD_ELEMENT"})
    if vd not in ("good", "gap", "fail"):
        raise HTTPException(422, {"code": "BAD_VERDICT"})
    c = _get_case(db, case_id, user)
    sm.record_event(db, c, c.status, c.status, "evaluation.verdict", user["role"], user["uid"],
                    {"element": el, "verdict": vd})
    car_id = None
    if vd in ("gap", "fail"):
        fid = "eval:" + el
        ex = (db.query(models.CorrectiveAction)
              .filter(models.CorrectiveAction.case_id == case_id,
                      models.CorrectiveAction.finding_id == fid,
                      models.CorrectiveAction.status.in_(("submitted", "rejected"))).first())
        if ex:
            car_id = ex.id
        else:
            car = models.CorrectiveAction(
                case_id=case_id, finding_id=fid,
                description="[자동생성] 평가 항목 '%s' %s 판정 — 시정조치 필요"
                            % (el, "부적합" if vd == "fail" else "보완"),
                status="submitted", submitted_by=user["uid"])
            db.add(car)
            db.flush()
            car_id = car.id
    # P2 훅: 5요소 최신 판정이 전부 good이면(가드=미해결 major NC 검사) 최종패키지 준비로 자동 전진
    if vd == "good":
        latest = {}
        for ev in (db.query(models.WorkflowEvent)
                   .filter_by(case_id=case_id, action="evaluation.verdict")
                   .order_by(models.WorkflowEvent.created_at.asc()).all()):
            p = ev.payload or {}
            if p.get("element"):
                latest[p["element"]] = p.get("verdict")
        latest[el] = vd
        if all(latest.get(k) == "good" for k in HPAS_AUTO_ELEMENTS):
            _auto_advance(db, c, "final_package_preparation", user, "evaluation.complete.auto")
    db.commit()
    return {"element": el, "verdict": vd, "car_id": car_id}


@app.get("/cases/{case_id}/doc-checklist")
def doc_checklist(case_id: str, lang: str = Query("ko"),
                  user=Depends(auth.get_current_user), db: Session = Depends(get_db)):
    c = _get_case(db, case_id, user)
    from .intake import REQUIRED_DOCS, doc_requirements, requirement_text, exempt_text
    # 서류명·요건·면제사유를 읽는 사람 언어로. 표는 있었는데 늘 한국어를 골랐다.
    DOC_KO = _intake_doc_names(lang)
    _L = lambda k: _CHECKLIST_L10N.get(k, {}).get((lang or "ko").lower(), k)
    # 필수 서류는 신청 경로와 관할·규모에 따라 다르다. 목록에서 빼더라도 행은 남겨
    # '해당 없음'으로 사유와 함께 보여준다 — 조용히 사라지면 심사자가 빠뜨린 것인지
    # 면제인지 구분할 수 없다.
    _rq = doc_requirements(c.pathway, (c.profile_ext or {}).get("country"), c.is_msme)
    _req, _na, _alt = _rq["required"], _rq["not_applicable"], _rq["alt"]
    docs = db.query(models.DocumentAsset).filter_by(case_id=case_id).all()
    by_type = {}
    for d in docs:
        by_type.setdefault(d.doc_type, []).append(
            {"document_id": d.document_id, "filename": d.filename, "review_status": d.review_status})
    # 시스템이 생성한 산출물도 제출물로 인정한다. SJPH 매뉴얼은 이 플랫폼이 만들어 내는
    # 정식 문서(GeneratedDocument)인데, DocumentAsset만 보면 정작 자기가 생성한 매뉴얼을
    # '미제출'로 표시하게 된다. 다만 무엇으로 충족됐는지는 source로 드러낸다(업로드와 구분).
    gen = {}
    for g in (db.query(models.GeneratedDocument).filter_by(case_id=case_id)
              .order_by(models.GeneratedDocument.version.desc()).all()):
        gen.setdefault(g.doc_type, g)
    checklist = []
    for dt in REQUIRED_DOCS:
        files = by_type.get(dt, [])
        non_rejected = [x for x in files if x["review_status"] != "rejected"]
        satisfied = bool(non_rejected)
        source = _L("업로드 파일") if satisfied else None
        g = gen.get(dt)
        if not satisfied and g is not None:
            satisfied = True
            source = "%s v%s · %s" % (_L("생성 문서"), g.version, g.status)
        # 미제출(파일 없음) vs 반려(제출됐으나 전부 반려=내용 부족) 구분
        status = "ok" if satisfied else ("rejected" if files else "missing")
        row = {"doc_type": dt, "doc_type_ko": DOC_KO[dt], "satisfied": satisfied,
               "files": files, "file_count": len(files), "status": status,
               "source": source, "applicable": True,
               "requirement": requirement_text(dt, lang), "required": dt in _req}
        if dt in _na:                       # 이 경로에서 요구되지 않는 서류
            row.update({"applicable": False, "status": "not_applicable",
                        "required": False, "na_reason": exempt_text(_na[dt], lang)})
        checklist.append(row)
    # 필수 외 실제 업로드된 문서 유형(기타·공급사선언·성적서 등)도 포함 — 전체 파일 표출
    for dt, files in by_type.items():
        if dt in REQUIRED_DOCS:
            continue
        checklist.append({"doc_type": dt, "doc_type_ko": DOC_KO.get(dt, dt), "satisfied": True,
                          "files": files, "file_count": len(files), "status": "ok",
                          "requirement": requirement_text(dt, lang), "required": False})
    # 공급사 할랄 인증서는 '원본 서류'가 없어도 인증번호는 확보돼 있을 수 있다.
    # 오디터가 대조할 대상이 몇 건인지 함께 보여야 '자료가 전무하다'는 오해가 안 생긴다.
    _cn = (db.query(models.Material)
           .filter(models.Material.case_id == case_id,
                   models.Material.cert_no.isnot(None), models.Material.cert_no != "").count())
    for _c in checklist:
        if _c["doc_type"] == "halal_certificate" and _cn:
            _c["cert_no_on_file"] = _cn
            _c["note"] = _L(_CERT_NO_NOTE) % _cn
            # 자기선언 경로는 인증번호 자체가 제출물이다(BPJPH가 직접 대조).
            # 무엇으로 갈음했는지 source에 남겨 업로드 충족과 구분한다.
            _a = _alt.get("halal_certificate")
            if _a and not _c["satisfied"] and _a["by"] == "supplier_cert_no":
                _c.update({"satisfied": True, "status": "ok",
                           "source": "공급사 인증번호 %d건 · %s" % (_cn, _a["note"])})
    missing = [{
        "doc_type": c["doc_type"], "doc_type_ko": c["doc_type_ko"], "status": c["status"],
        "file_count": c["file_count"], "requirement": c["requirement"],
        "note": c.get("note"), "cert_no_on_file": c.get("cert_no_on_file"),
        "reason": ("서류가 제출되지 않았습니다 (파일 없음)"
                   if c["status"] == "missing"
                   else "제출됐으나 반려됨 — 내용 보완이 필요합니다"),
    } for c in checklist if c.get("required") and not c["satisfied"]]
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
    # 구제 경로(G1 수정 이전 제출분): consultant_review에 pathway 미확정으로 걸린 케이스는
    # 상태 전이 없이 pathway 라벨만 확정 허용(reguler 한정 — 자기선언 전환은 반려→재제출 경유).
    if c.status == "consultant_review" and c.pathway in ("undetermined", None, ""):
        if body.pathway == "self_declare":
            raise HTTPException(409, {"code": "PATHWAY_LOCKED_USE_RETURN",
                                      "hint": "자기선언 전환은 신청 반려(return-application) 후 재제출로 경로판정을 거치세요."})
        a = sm.assess_pathway(db, c)
        c.risk_category = a["risk_category"]
        c.pathway = "reguler"
        sm.record_event(db, c, c.status, c.status, "pathway.confirm", user["role"], user["uid"],
                        {"pathway": "reguler", "repair": True, "override_reason": body.override_reason})
        db.commit()
        return {"pathway": c.pathway, "next_state": c.status, "assessment": a}
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


# ── P0-3: 사전심사 오디터 회신 루프 — 스키마 무변경, WorkflowEvent(latest-wins)로 저장 ──
# stageBar stage3(=auditor_reviewed)의 근거를 채운다. Phase 2 mock_audit 헬퍼와 동일 패턴.
PREASSESS_SECTIONS = ("documents", "materials", "process")   # 문서·재료·제조 3섹션(고정 키)


def _preassess_review_latest(db, case_id):
    """오디터 3섹션 검토 최신(preassess.review, latest-wins)."""
    e = (db.query(models.WorkflowEvent)
         .filter(models.WorkflowEvent.case_id == case_id,
                 models.WorkflowEvent.action == "preassess.review")
         .order_by(models.WorkflowEvent.created_at.desc()).first())
    if not e:
        return None
    p = e.payload or {}
    return {"sections": p.get("sections", {}), "verdict": p.get("verdict"),
            "note": p.get("note", ""), "at": e.created_at.isoformat() if e.created_at else None}


def _preassess_doc_requests(db, case_id):
    """추가서류 요청 이력(preassess.doc_request, 회차 오름차순)."""
    evs = (db.query(models.WorkflowEvent)
           .filter(models.WorkflowEvent.case_id == case_id,
                   models.WorkflowEvent.action == "preassess.doc_request")
           .order_by(models.WorkflowEvent.created_at.asc()).all())
    return [{"items": (e.payload or {}).get("items", []), "message": (e.payload or {}).get("message", ""),
             "round": (e.payload or {}).get("round"), "actor": e.actor_type,
             "at": e.created_at.isoformat() if e.created_at else None} for e in evs]


def _preassess_resubmit_latest(db, case_id):
    """클라이언트 재제출 최신(preassess.resubmit, latest-wins)."""
    e = (db.query(models.WorkflowEvent)
         .filter(models.WorkflowEvent.case_id == case_id,
                 models.WorkflowEvent.action == "preassess.resubmit")
         .order_by(models.WorkflowEvent.created_at.desc()).first())
    if not e:
        return None
    p = e.payload or {}
    return {"round": p.get("round"), "note": p.get("note", ""),
            "at": e.created_at.isoformat() if e.created_at else None}


@app.post("/cases/{case_id}/preassess/review")
def preassess_review(case_id: str, body: schemas.PreassessReviewReq,
                     user=Depends(auth.require_roles("auditor", "fatwa_liaison", "operator")),
                     db: Session = Depends(get_db)):
    """① 오디터 3섹션(문서·재료·제조) 검토 기록 — auditor_reviewed 근거. latest-wins."""
    c = _get_case(db, case_id, user)
    verdict = (body.verdict or "").lower()
    if verdict not in ("ready", "supplement"):
        raise HTTPException(422, {"code": "INVALID_VERDICT", "allowed": ["ready", "supplement"]})
    sections = body.sections or {}
    bad = set(sections.keys()) - set(PREASSESS_SECTIONS)
    if bad:
        raise HTTPException(422, {"code": "BAD_SECTION", "allowed": list(PREASSESS_SECTIONS),
                                  "got": sorted(bad)})
    sm.record_event(db, c, c.status, c.status, "preassess.review", user["role"], user["uid"],
                    {"sections": sections, "verdict": verdict, "note": (body.note or "").strip()})
    if verdict == "ready":   # P2 훅: 보완 재제출 검토 통과→컨설턴트 검토(supplementation_submitted에서만 발화)
        _auto_advance(db, c, "consultant_review", user, "preassess.review.auto")
    db.commit()
    return {"ok": True, "verdict": verdict}


@app.post("/cases/{case_id}/preassess/doc-request")
def preassess_doc_request(case_id: str, body: schemas.PreassessDocRequestReq,
                          user=Depends(auth.require_roles("auditor", "fatwa_liaison", "operator")),
                          db: Session = Depends(get_db)):
    """② 추가/부족 요청 서류 생성·전송 — round 누적 + 클라이언트 알림."""
    c = _get_case(db, case_id, user)
    items = body.items or []
    if not items:
        raise HTTPException(422, {"code": "ITEMS_REQUIRED"})
    # round = 지금까지 이 케이스의 preassess.doc_request 이벤트 수 + 1
    prior = (db.query(models.WorkflowEvent)
             .filter(models.WorkflowEvent.case_id == case_id,
                     models.WorkflowEvent.action == "preassess.doc_request").count())
    rnd = prior + 1
    msg = (body.message or "").strip()
    sm.record_event(db, c, c.status, c.status, "preassess.doc_request", user["role"], user["uid"],
                    {"items": items, "message": msg, "round": rnd})
    _notify(db, c, "preassess.doc_request", body=msg, role="applicant",
            msg=("preassess.doc_request", {}))
    db.commit()
    return {"ok": True, "round": rnd, "items": len(items)}


@app.post("/cases/{case_id}/preassess/resubmit")
def preassess_resubmit(case_id: str, body: schemas.PreassessResubmitReq,
                       user=Depends(auth.require_roles("applicant", "consultant")),
                       db: Session = Depends(get_db)):
    """③ 클라이언트 재업로드 후 재제출 표시 — 오디터 알림. round=현재 doc_request 회차."""
    c = _get_case(db, case_id, user)
    reqs = _preassess_doc_requests(db, case_id)
    rnd = reqs[-1]["round"] if reqs else None
    note = (body.note or "").strip()
    sm.record_event(db, c, c.status, c.status, "preassess.resubmit", user["role"], user["uid"],
                    {"round": rnd, "note": note})
    _auto_advance(db, c, "supplementation_submitted", user, "preassess.resubmit.auto")   # P2 훅: 보완 재제출→제출 상태
    _notify(db, c, "preassess.resubmit", body=note, role="auditor",
            msg=("preassess.resubmit", {}))
    db.commit()
    return {"ok": True, "round": rnd}


@app.get("/cases/{case_id}/preassess/review")
def preassess_review_get(case_id: str, user=Depends(auth.get_current_user),
                         db: Session = Depends(get_db)):
    """통합 조회 — 오디터·클라이언트 공용(org 격리). 프런트 stageBar·양측 뷰가 이걸로 렌더."""
    _get_case(db, case_id, user)
    review = _preassess_review_latest(db, case_id)
    history = _preassess_doc_requests(db, case_id)
    resubmit = _preassess_resubmit_latest(db, case_id)
    return {"reviewed": review is not None, "review": review,
            "doc_request": (history[-1] if history else None),
            "doc_request_history": history, "resubmit": resubmit}


def _resubmit_doc_compare(db, case_id):
    """P2-7: 사전심사 재제출 — 이전 제출본↔새 제출본 비교(스키마 무변경, 조회 전용).

    경계 = 최신 preassess.doc_request(오디터 보완요청) 시각. 이 시각 이전 업로드는
    '이전 제출본', 이후 업로드는 '새(재)제출본'으로 나눈다. doc_type별 최신본을 비교해
    신규(added)/변경(changed)/동일(unchanged)/재제출(resubmitted·해시미확인)/
    미재제출(missing) 판정. 기존 DocumentAsset·WorkflowEvent만 읽는다."""
    from .intake import DOC_KO
    boundary_ev = (db.query(models.WorkflowEvent)
                   .filter(models.WorkflowEvent.case_id == case_id,
                           models.WorkflowEvent.action == "preassess.doc_request")
                   .order_by(models.WorkflowEvent.created_at.desc()).first())
    if not boundary_ev or not boundary_ev.created_at:
        return None
    boundary = boundary_ev.created_at
    docs = (db.query(models.DocumentAsset).filter_by(case_id=case_id)
            .order_by(models.DocumentAsset.created_at.asc()).all())

    def _slim(d):
        return {"document_id": d.document_id, "filename": d.filename,
                "doc_type": d.doc_type, "doc_type_ko": DOC_KO.get(d.doc_type, d.doc_type),
                "file_hash": d.file_hash, "has_file": bool(d.content_b64),
                "uploaded_by": d.uploaded_by, "uploader_role": d.uploader_role,
                "at": d.created_at.isoformat() if d.created_at else None}
    prev_by, new_by = {}, {}
    for d in docs:
        if not d.created_at:
            continue
        # created_at 오름차순 순회 → doc_type별 최신본이 각 버킷에 남는다(latest-wins)
        (new_by if d.created_at >= boundary else prev_by)[d.doc_type] = _slim(d)
    diff = []
    for dt in sorted(set(prev_by) | set(new_by), key=str):
        p, n = prev_by.get(dt), new_by.get(dt)
        if p and n:
            if p.get("file_hash") and n.get("file_hash"):
                st = "unchanged" if p["file_hash"] == n["file_hash"] else "changed"
            else:
                st = "resubmitted"   # 재업로드됐으나 해시 미기록 → 내용 동일성 미확인
        elif n:
            st = "added"
        else:
            st = "missing"   # 이전엔 있었으나 재제출 안 됨
        diff.append({"doc_type": dt, "doc_type_ko": DOC_KO.get(dt, dt),
                     "status": st, "prev": p, "new": n})
    counts = {"added": 0, "changed": 0, "unchanged": 0, "resubmitted": 0, "missing": 0}
    for x in diff:
        counts[x["status"]] += 1
    return {"boundary_at": boundary.isoformat(),
            "prev_docs": list(prev_by.values()), "new_docs": list(new_by.values()),
            "diff": diff, "counts": counts,
            "has_new": bool(new_by)}


# ---------- A09: 보완·재전송 센터(집약 조회 전용) ----------
# 각 단계(신청 반려 · 사전심사 회신루프 · 현장보고서 재심 · CAR 시정조치)의
# 보완/추가서류 요청을 한 곳에 모아 반환한다. 스키마 무변경 — 기존 WorkflowEvent·
# 테이블 조회만 서버에서 합친다(신규 쓰기·마이그레이션 없음).
@app.get("/cases/{case_id}/resubmit-center")
def resubmit_center(case_id: str, user=Depends(auth.get_current_user),
                    db: Session = Depends(get_db)):
    """보완·재전송 센터 집약 — 출처단계·사유·회차·기한·상태를 정규화한 requests 리스트."""
    c = _get_case(db, case_id, user)
    reqs = []

    # ① 신청서 반려(return-application) — draft_state=returned / return_reason
    app_ret = (db.query(models.WorkflowEvent)
               .filter(models.WorkflowEvent.case_id == case_id,
                       models.WorkflowEvent.action == "application.return")
               .order_by(models.WorkflowEvent.created_at.desc()).first())
    if app_ret:
        returned = (c.draft_state == "returned")
        reqs.append({
            "key": "application", "source": "application", "source_ko": "신청서 반려",
            "reason": c.return_reason or (app_ret.payload or {}).get("reason", ""),
            "items": [], "round": None,
            "status": "pending" if returned else "resolved",
            "due": c.due_date,
            "at": app_ret.created_at.isoformat() if app_ret.created_at else None,
            "action": "application"})

    # ② 사전심사 회신루프(P0-3) — 추가서류 요청 이력 + 재제출 + 오디터 재검토
    pre_hist = _preassess_doc_requests(db, case_id)
    if pre_hist:
        latest = pre_hist[-1]
        review = _preassess_review_latest(db, case_id)
        resub = _preassess_resubmit_latest(db, case_id)
        rnd = latest.get("round")
        if review and review.get("verdict") == "ready":
            status = "resolved"
        elif resub and resub.get("round") == rnd:
            status = "resubmitted"
        else:
            status = "pending"
        items = [(it.get("doc_type_ko") or it.get("doc_type") or "")
                 for it in (latest.get("items") or [])]
        # M13: 이력 컬럼 — 검토자(요청 actor)·대상(요청 서류)·최신 검토 결과
        def _hist_item_names(raw):
            names = []
            for it in (raw or []):
                if isinstance(it, dict):
                    names.append(it.get("doc_type_ko") or it.get("doc_type") or "")
                else:
                    names.append(str(it))
            return [n for n in names if n]
        review_verdict = (review or {}).get("verdict")
        reqs.append({
            "key": "preassess", "source": "preassess", "source_ko": "사전심사 보완",
            "reason": latest.get("message", ""), "items": items, "round": rnd,
            "status": status, "due": c.due_date, "at": latest.get("at"),
            "action": "preassess_resubmit", "resubmit": resub,
            "review_verdict": review_verdict,
            "compare": _resubmit_doc_compare(db, case_id),   # P2-7: 이전↔새 제출본 비교
            "history": [{"round": h.get("round"), "count": len(h.get("items") or []),
                         "actor": h.get("actor"),
                         "items": _hist_item_names(h.get("items")),
                         "at": h.get("at")} for h in pre_hist]})

    # ③ 현장 심사보고서 재심루프(P0-4) — 보완 반려 + 재제출 + 수정확인
    ret = _audit_report_event_latest(db, case_id, "audit_report.return")
    if ret:
        resub = _audit_report_event_latest(db, case_id, "audit_report.resubmit")
        recon = _audit_report_event_latest(db, case_id, "audit_report.reconfirm")
        rnd = ret.get("round")
        if recon and recon.get("decision") == "ok":
            status = "resolved"
        elif resub and (resub.get("round") == rnd
                        or (resub.get("at") or "") >= (ret.get("at") or "")):
            status = "resubmitted"
        else:
            status = "pending"
        reqs.append({
            "key": "audit_report", "source": "audit_report", "source_ko": "현장보고서 재심",
            "reason": ret.get("comment", ""), "items": [], "round": rnd,
            "status": status, "due": c.due_date, "at": ret.get("at"),
            "action": "audit_report_resubmit",
            "resubmit": ({"round": resub.get("round"), "at": resub.get("at")} if resub else None)})

    # ④ CAR 시정조치 — 미해결 지적별 보완 요청(제출→검토→종결)
    findings = db.query(models.AuditFinding).filter_by(case_id=case_id).all()
    cars = (db.query(models.CorrectiveAction).filter_by(case_id=case_id)
            .order_by(models.CorrectiveAction.created_at.desc()).all())
    cars_by_f = {}
    for x in cars:
        cars_by_f.setdefault(x.finding_id, []).append(x)
    for f in findings:
        fcars = cars_by_f.get(f.finding_id, [])
        if f.status == "closed" or any(x.status in ("accepted", "closed") for x in fcars):
            status = "resolved"
        elif any(x.status == "submitted" for x in fcars):
            status = "resubmitted"
        else:
            status = "pending"
        fat = getattr(f, "created_at", None)
        reqs.append({
            "key": "finding:" + f.finding_id, "source": "car", "source_ko": "시정조치(CAR)",
            "reason": f.finding or "", "items": [], "round": len(fcars),
            "status": status, "due": f.due_date, "severity": f.severity,
            "finding_id": f.finding_id, "action": "car",
            "at": fat.isoformat() if fat else None})

    counts = {"pending": 0, "resubmitted": 0, "resolved": 0, "total": len(reqs)}
    for r in reqs:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    return {"case_id": case_id, "company_name": c.company_name,
            "due_date": c.due_date, "requests": reqs, "counts": counts}


# ---------- transition ----------
def _auto_advance(db, c, to_state, user, action):
    """서브플로우 완료 훅 — 조건 충족 시 케이스 상태 자동 전진(전수검사 P2: '서브플로우는 끝나는데
    케이스가 안 넘어감' 7곳 해소). /transition과 동일 규칙(PROTECTED 제외·역할 게이트 admin 우회·
    가드 통과)일 때만 전진하고, 아니면 조용히 건너뜀(수동 '다음 단계' 폴백 유지)."""
    try:
        if to_state in sm.PROTECTED_STATES or not sm.allowed(c.status, to_state):
            return False
        if user["role"] != "admin" and user["role"] not in sm.transition_roles(to_state):
            return False
        ok, _blk = sm.can_transition(db, c, to_state)
        if not ok:
            return False
        frm = c.status
        sm.apply_side_effects(c, to_state)
        c.status = to_state
        obs.inc("glhac_state_transition_total", {"to": to_state})
        sm.record_event(db, c, frm, to_state, action, user["role"], user["uid"], {"auto": True})
        return True
    except Exception as e:
        log.warning("auto_advance(%s→%s) 실패 무시: %s", getattr(c, "status", "?"), to_state, e)
        return False


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
    # 상태 전이에 딸린 알림 — 문구는 카탈로그(NOTIFY_MSG)에서 읽는 사람 언어로 조립된다
    _NOTIFY_ON = {"audit_closed": "audit_closed",
                  "document_pre_audit_requested": "document_requested",
                  "onsite_audit_scheduled": "audit_scheduled"}
    if body.to_state in _NOTIFY_ON:
        ev = _NOTIFY_ON[body.to_state]
        _notify(db, c, ev, msg=(ev, {"company": c.company_name or ""}),
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
    _assert_org_access(db, user, org_id)
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
    """동반자(pendamping) 배정.

    배정 대상이 실제로 있는 사람인지, 역할이 맞는지 확인한다. 종전에는 아무 문자열이나
    받아 저장했다 — 오타난 id 로 배정하면 '배정은 됐는데 아무도 못 보는' 상태가 되고,
    자기선언 경로의 검증 단계에서야 드러난다(오디터 배정 경로는 이미 확인하고 있었다)."""
    _get_case(db, case_id, user)
    uid = (body.pendamping_id or "").strip()
    pu = db.query(models.User).filter(
        or_(models.User.user_id == uid, models.User.username == uid)).first()
    if not pu or pu.role != "pendamping_pph":
        raise HTTPException(404, {"code": "PENDAMPING_NOT_FOUND", "given": uid})
    pa = models.PendampingAssignment(case_id=case_id, pendamping_id=pu.user_id)
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
    advanced = False
    if body.decision == "verified":   # P2 훅: 펜담핑 검증→자기선언 제출(가드 4종 통과 시)
        advanced = _auto_advance(db, c, "self_declaration_submitted", user, "pendamping.verify.auto")
    db.commit()
    return {"decision": body.decision, "switched_to_reguler": switched, "auto_advanced": advanced}


# ---------- 동반자(PPH) 배정 인박스 ----------
@app.get("/pendamping/assignments")
def pendamping_assignments(user=Depends(auth.require_roles("pendamping_pph", "operator", "admin")),
                           db: Session = Depends(get_db)):
    """동반자(PPH) 전용 인박스 — pendamping_assignment(케이스별)에서 로그인 동반자 배정건 집약(스키마 무변경).
    pendamping_pph: 본인(uid) 배정건만 / operator·admin: 전체(모니터링). 케이스별 최신 배정 1건(assignment_id desc)."""
    q = db.query(models.PendampingAssignment)
    if user["role"] == "pendamping_pph":
        q = q.filter(models.PendampingAssignment.pendamping_id == user["uid"])
    rows = q.order_by(models.PendampingAssignment.assignment_id.desc()).all()
    seen, items = set(), []
    for pa in rows:
        if pa.case_id in seen:
            continue
        seen.add(pa.case_id)
        c = db.get(models.CaseApplication, pa.case_id)
        if not c:
            continue
        try:
            auth.check_org(user, c)
        except HTTPException:
            continue   # org 격리 — 타 조직 배정건은 노출하지 않음
        items.append({
            "case_id": c.case_id, "company_name": c.company_name,
            "status": c.status, "pathway": c.pathway,
            "pendamping_id": pa.pendamping_id, "decision": pa.decision,   # None|verified|rejected|rework
            "note": pa.note,
            "verified_at": pa.verified_at.isoformat() if pa.verified_at else None,
        })
    return {"items": items, "count": len(items)}


# ---------- 자기선언서 (Surat Pernyataan Pelaku Usaha · SEHATI) ----------
# SEHATI self-declare 필수 산출물(사업자 자기선언서). 스키마 무변경 — _render_pdf_rich 재사용 +
# GeneratedDocument(doc_type=self_declaration)로 1건 등록. 서명은 내부 무결성 서명(공인 전자서명 아님).
_SD_STATEMENTS = [
    ("당사 제품은 할랄 원재료만을 사용하며 하람(haram)·나지스(najis) 성분을 포함하지 않습니다.",
     "Produk kami hanya menggunakan bahan halal dan tidak mengandung bahan haram/najis."),
    ("제조공정은 간이·저위험(low-risk) 공정이며, 비할랄과의 교차오염 방지를 준수합니다.",
     "Proses produksi bersifat sederhana/berisiko rendah dan menjaga pencegahan kontaminasi dengan bahan non-halal."),
    ("당사는 할랄제품보증시스템(SJPH)을 수립·이행·유지합니다.",
     "Kami menyusun, menerapkan, dan memelihara Sistem Jaminan Produk Halal (SJPH)."),
    ("본 자기선언 및 제출한 모든 정보는 진실하며, 허위가 있을 경우 관련 법령에 따른 책임을 부담합니다.",
     "Seluruh informasi dalam pernyataan ini benar, dan kami bersedia bertanggung jawab secara hukum apabila terdapat ketidakbenaran."),
]


def _self_declaration_guard(c):
    if c.pathway != "self_declare":
        raise HTTPException(400, {"code": "NOT_SELF_DECLARE", "pathway": c.pathway,
                                  "hint": "자기선언서는 self_declare(SEHATI) 경로 케이스에만 발급됩니다."})


def _pendamping_name(db, c):
    """최신 배정 동반자 이름(assignment_id desc, verify와 동일 정렬). 없으면 빈값."""
    pa = (db.query(models.PendampingAssignment).filter_by(case_id=c.case_id)
          .order_by(models.PendampingAssignment.assignment_id.desc()).first())
    if not pa:
        return ""
    u = db.query(models.User).filter_by(user_id=pa.pendamping_id).first()
    return (u.username if u else pa.pendamping_id) or ""


def _self_declaration_blocks(db, c):
    rep = c.responsible_person or (c.profile_ext or {}).get("pic_name") or ""
    pd_name = _pendamping_name(db, c)
    blocks = [
        {"type": "heading", "text": "SURAT PERNYATAAN PELAKU USAHA · 사업자 자기선언서", "level": 1},
        {"type": "para", "text": "SEHATI Self-Declare — 자기선언(간이·저위험) 할랄 경로 · Jalur sertifikasi halal self-declare"},
        {"type": "heading", "text": "사업자 정보 · Data Pelaku Usaha", "level": 2},
        {"type": "kv", "label": "회사명 · Nama Usaha", "value": c.company_name or "-"},
        {"type": "kv", "label": "NIB · Nomor Induk Berusaha", "value": c.nib or "-"},
        {"type": "kv", "label": "주소 · Alamat", "value": c.address or "-"},
        {"type": "kv", "label": "대표/책임자 · Penanggung Jawab", "value": rep or "-"},
        {"type": "heading", "text": "자기선언 내용 · Isi Pernyataan", "level": 2},
        {"type": "para", "text": "본인은 위 사업자의 책임자로서 다음을 자기선언합니다 · "
                                 "Saya, sebagai penanggung jawab usaha di atas, dengan ini menyatakan:"},
    ]
    for i, (ko, idn) in enumerate(_SD_STATEMENTS, 1):
        blocks.append({"type": "para", "text": "%d. %s" % (i, ko)})
        blocks.append({"type": "para", "text": "    %s" % idn})
    blocks.append({"type": "spacer", "h": 10})
    blocks.append({"type": "signature", "slots": [
        {"role": "사업자 대표 · Pelaku Usaha", "name": rep or "", "signed": False},
        {"role": "동반자 · Pendamping (PPH)", "name": pd_name or "", "signed": False},
    ]})
    blocks.append({"type": "spacer", "h": 8})
    # Phase 0 내부서명 고지 — 자기선언서에도 disclaimer 표기(공인 전자서명 아님). Phase 1: PSrE 공인 시 동적 표기.
    blocks.append({"type": "para", "text": "서명 성격 · Sifat tanda tangan : "
                   + _sig_nature_for(db, c.case_id, "self_declaration")})
    blocks.append({"type": "para", "text": _SIG_VALID_MEANING})
    blocks.append({"type": "para", "text": _LEGAL_DISCLAIMER_PDF})
    return blocks


@app.get("/cases/{case_id}/self-declaration/preview")
def self_declaration_preview(case_id: str, user=Depends(auth.get_current_user), db: Session = Depends(get_db)):
    """자기선언서 미리보기(HTML 조각) — self_declare 경로 전용. read-only·org격리."""
    import html
    from fastapi.responses import HTMLResponse
    c = _get_case(db, case_id, user)
    _self_declaration_guard(c)
    rep = c.responsible_person or (c.profile_ext or {}).get("pic_name") or "-"
    pd_name = _pendamping_name(db, c) or "-"

    def e(x):
        return html.escape(str(x if x is not None else "-"))

    info = [("회사명 · Nama Usaha", c.company_name), ("NIB · Nomor Induk Berusaha", c.nib),
            ("주소 · Alamat", c.address), ("대표/책임자 · Penanggung Jawab", rep)]
    info_rows = "".join(
        "<tr><th style='text-align:left;padding:5px 12px;color:#475569;white-space:nowrap'>%s</th>"
        "<td style='padding:5px 12px'>%s</td></tr>" % (e(k), e(v)) for k, v in info)
    stmts = "".join(
        "<li style='margin:6px 0'><div>%s</div><div style='color:#64748b'>%s</div></li>" % (e(ko), e(idn))
        for ko, idn in _SD_STATEMENTS)
    frag = (
        "<div style='font-size:13.5px;line-height:1.7'>"
        "<h2 style='margin:0 0 4px'>SURAT PERNYATAAN PELAKU USAHA · 사업자 자기선언서</h2>"
        "<div style='color:#64748b;margin-bottom:10px'>SEHATI Self-Declare — 자기선언(간이·저위험) 할랄 경로</div>"
        "<table style='border-collapse:collapse;margin-bottom:12px'>%s</table>"
        "<div style='font-weight:600;margin:8px 0'>자기선언 내용 · Isi Pernyataan</div>"
        "<div style='margin-bottom:4px'>본인은 위 사업자의 책임자로서 다음을 자기선언합니다 · "
        "Saya, sebagai penanggung jawab usaha di atas, dengan ini menyatakan:</div>"
        "<ol style='margin:0 0 12px 18px;padding:0'>%s</ol>"
        "<div style='display:flex;gap:24px;margin:14px 0'>"
        "<div>사업자 대표 · Pelaku Usaha<br><b>%s</b></div>"
        "<div>동반자 · Pendamping (PPH)<br><b>%s</b></div></div>"
        "<div style='border-top:1px dashed #cbd5e1;padding-top:8px;color:#64748b;font-size:12px'>"
        "⚠ %s<br>%s<br>%s</div></div>"
        % (info_rows, stmts, e(rep), e(pd_name),
           e(_sig_nature_for(db, c.case_id, "self_declaration")), e(_SIG_VALID_MEANING), e(_LEGAL_DISCLAIMER_PDF)))
    return HTMLResponse(content=frag)


@app.get("/cases/{case_id}/self-declaration.pdf")
def self_declaration_pdf(case_id: str, user=Depends(auth.get_current_user), db: Session = Depends(get_db)):
    """자기선언서 PDF — self_declare 경로 전용. _render_pdf_rich 재사용 + GeneratedDocument 1건 등록(내부서명 고지 포함)."""
    from fastapi.responses import Response
    c = _get_case(db, case_id, user)
    _self_declaration_guard(c)
    blocks = _self_declaration_blocks(db, c)
    # GeneratedDocument 등록(최초 1회) — 반복 다운로드로 버전 폭증하지 않도록 존재 시 재사용.
    exists = db.query(models.GeneratedDocument).filter_by(case_id=case_id, doc_type="self_declaration").first()
    if not exists:
        content = "\n".join("%d. %s / %s" % (i, ko, idn) for i, (ko, idn) in enumerate(_SD_STATEMENTS, 1))
        _save_gendoc(db, c, "self_declaration", content, user, status="draft")
        db.commit()
    pdf = _render_pdf_rich("SURAT PERNYATAAN PELAKU USAHA · 자기선언서", blocks,
                           subtitle=(c.company_name or ""),
                           footer="GL-HAC AI · Self-Declaration " + case_id[:8])
    return Response(content=pdf, media_type="application/pdf",
                    headers={"Content-Disposition": "attachment; filename=self_declaration_%s.pdf" % case_id[:8]})


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
    _assert_org_access(db, user, ei.org_id)   # 배정된 컨설턴트도 검증할 수 있어야 한다
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


# ── P0-4차: M06 규정·법령 관리 — 스키마 무변경, WorkflowEvent(latest-wins)로 저장 ──
# 법령은 case에 종속되지 않는 전역 데이터다. 이 레포엔 case_id 없는 전역 WorkflowEvent 선례가
# 없으므로(모든 이벤트가 case_id 필수·nullable=False), 각 법령을 자체 reg_id("reg_"+uuid)로
# 발급하고 그 reg_id를 WorkflowEvent.case_id에 담아 "법령 단위 이벤트 스트림"을 만든다.
# → 별도 테이블 0개. record_event(해시체인 포함)를 reg_id 스코프로 그대로 재사용.
#   content = 최신 regulation.upserted 이벤트(수정마다 append = 버전 누적).
#   state   = 최신 상태전이 이벤트의 to_status(없으면 draft).
REG_ACTION_PREFIX = "regulation."
REG_STATES = ("draft", "review", "effective", "retired")
# to_state → (허용 이전상태, 기록 action). 발효(effective)는 review에서만.
REG_TRANSITIONS = {
    "review":    {"from": ("draft",),               "action": "regulation.submitted"},
    "effective": {"from": ("review",),              "action": "regulation.approved"},
    "retired":   {"from": ("review", "effective"),  "action": "regulation.retired"},
    "draft":     {"from": ("review",),              "action": "regulation.reopened"},
}
# 영향 매핑 카탈로그(프런트 다중선택 소스). 저장은 payload에 키 문자열 리스트로.
REG_IMPACT_STAGES = [("preassess", "사전심사"), ("mock_audit", "모의심사"),
                     ("onsite", "현장심사"), ("fatwa", "파트와 심의"), ("certificate", "인증서 발급")]
REG_IMPACT_SECTIONS = list(MOCK_EVIDENCE_SECTIONS)   # 증거 섹션 키 재사용(원재료보관·생산영상 등)
REG_CATEGORIES = [("law", "법률"), ("regulation", "시행령·규정"), ("fatwa", "파트와·종교규정"),
                  ("standard", "기술표준"), ("guideline", "지침")]


def _reg_events(db, reg_id, actions=None):
    """해당 법령(reg_id) 이벤트를 시간순으로. actions로 필터 옵션."""
    q = (db.query(models.WorkflowEvent)
         .filter(models.WorkflowEvent.case_id == reg_id,
                 models.WorkflowEvent.action.like(REG_ACTION_PREFIX + "%")))
    if actions:
        q = q.filter(models.WorkflowEvent.action.in_(actions))
    return q.order_by(models.WorkflowEvent.created_at.asc(),
                      models.WorkflowEvent.event_id.asc()).all()


def _reg_ids(db):
    """등록된 모든 법령 reg_id(중복 제거)."""
    rows = (db.query(models.WorkflowEvent.case_id)
            .filter(models.WorkflowEvent.action.like(REG_ACTION_PREFIX + "%"))
            .distinct().all())
    return [r[0] for r in rows]


def _reg_content(db, reg_id, evs=None):
    """최신 regulation.upserted payload = 현재 내용(latest-wins)."""
    evs = evs if evs is not None else _reg_events(db, reg_id)
    ups = [e for e in evs if e.action == "regulation.upserted"]
    if not ups:
        return None
    last = ups[-1]
    p = last.payload or {}
    return {"title": p.get("title", ""), "reg_number": p.get("reg_number"),
            "effective_date": p.get("effective_date"), "category": p.get("category"),
            "summary": p.get("summary", ""),
            "impact_stages": p.get("impact_stages", []) or [],
            "impact_sections": p.get("impact_sections", []) or [],
            "version": p.get("version", len(ups)), "version_count": len(ups),
            "updated_by": last.actor_id,
            "updated_at": last.created_at.isoformat() if last.created_at else None}


def _reg_state(db, reg_id, evs=None):
    """최신 상태 = 마지막 to_status(없으면 draft)."""
    evs = evs if evs is not None else _reg_events(db, reg_id)
    st = "draft"
    for e in evs:
        if e.to_status:
            st = e.to_status
    return st


def _reg_versions(db, reg_id, evs=None):
    """버전 타임라인 — upserted 이벤트를 시간순으로(누가·언제·무엇)."""
    evs = evs if evs is not None else _reg_events(db, reg_id)
    out = []
    for i, e in enumerate([x for x in evs if x.action == "regulation.upserted"], start=1):
        p = e.payload or {}
        out.append({"version": p.get("version", i), "title": p.get("title", ""),
                    "reg_number": p.get("reg_number"), "effective_date": p.get("effective_date"),
                    "category": p.get("category"), "summary": p.get("summary", ""),
                    "impact_stages": p.get("impact_stages", []) or [],
                    "impact_sections": p.get("impact_sections", []) or [],
                    "actor": e.actor_id,
                    "at": e.created_at.isoformat() if e.created_at else None})
    return out


def _reg_history(db, reg_id, evs=None):
    """전체 변경 이력(내용수정+상태전이) 시간순 — 감사용."""
    evs = evs if evs is not None else _reg_events(db, reg_id)
    KO = {"regulation.upserted": "내용 등록·수정", "regulation.submitted": "검토 상신",
          "regulation.approved": "발효 승인", "regulation.retired": "폐지",
          "regulation.reopened": "초안 회귀"}
    out = []
    for e in evs:
        p = e.payload or {}
        out.append({"action": e.action, "label": KO.get(e.action, e.action),
                    "from_state": e.from_status, "to_state": e.to_status,
                    "reason": p.get("reason", ""), "version": p.get("version"),
                    "actor": e.actor_id, "actor_type": e.actor_type,
                    "at": e.created_at.isoformat() if e.created_at else None})
    return out


def _reg_shim(reg_id):
    """record_event(해시체인)용 최소 case 쉼 — reg_id를 case_id 스코프로 사용(전역 데이터)."""
    import types
    return types.SimpleNamespace(case_id=reg_id, org_id=None)


def _reg_summary_row(db, reg_id):
    evs = _reg_events(db, reg_id)
    content = _reg_content(db, reg_id, evs)
    if content is None:
        return None
    return {"reg_id": reg_id, "state": _reg_state(db, reg_id, evs),
            "title": content["title"], "reg_number": content["reg_number"],
            "category": content["category"], "effective_date": content["effective_date"],
            "impact_stages": content["impact_stages"], "impact_sections": content["impact_sections"],
            "version_count": content["version_count"],
            "updated_at": content["updated_at"], "updated_by": content["updated_by"]}


@app.get("/regulations/meta")
def regulations_meta(user=Depends(auth.require_roles("operator"))):
    """프런트 폼 소스 — 분류·영향 심사단계·영향 증거섹션·상태전이 카탈로그."""
    return {"categories": [{"key": k, "label": v} for k, v in REG_CATEGORIES],
            "impact_stages": [{"key": k, "label": v} for k, v in REG_IMPACT_STAGES],
            "impact_sections": [{"key": k, "label": v} for k, v in REG_IMPACT_SECTIONS],
            "states": list(REG_STATES),
            "transitions": {k: v["from"] for k, v in REG_TRANSITIONS.items()}}


@app.get("/regulations")
def regulations_list(user=Depends(auth.require_roles("operator")), db: Session = Depends(get_db)):
    """법령 목록(각 항목 최신 내용·상태). 최신 수정순 정렬."""
    rows = [r for r in (_reg_summary_row(db, rid) for rid in _reg_ids(db)) if r]
    rows.sort(key=lambda r: r.get("updated_at") or "", reverse=True)
    return {"items": rows, "count": len(rows)}


@app.get("/regulations/{reg_id}")
def regulation_detail(reg_id: str, user=Depends(auth.require_roles("operator")),
                      db: Session = Depends(get_db)):
    """상세 = 현재 내용 + 상태 + 버전 이력 + 전체 변경이력(영향매핑 포함)."""
    evs = _reg_events(db, reg_id)
    content = _reg_content(db, reg_id, evs)
    if content is None:
        raise HTTPException(404, {"code": "REGULATION_NOT_FOUND"})
    return {"reg_id": reg_id, "state": _reg_state(db, reg_id, evs), "content": content,
            "versions": _reg_versions(db, reg_id, evs), "history": _reg_history(db, reg_id, evs)}


@app.get("/regulations/{reg_id}/history")
def regulation_history(reg_id: str, user=Depends(auth.require_roles("operator")),
                       db: Session = Depends(get_db)):
    """변경 이력만(버전 타임라인 + 상태전이 로그)."""
    evs = _reg_events(db, reg_id)
    if not evs:
        raise HTTPException(404, {"code": "REGULATION_NOT_FOUND"})
    return {"reg_id": reg_id, "versions": _reg_versions(db, reg_id, evs),
            "history": _reg_history(db, reg_id, evs)}


def _reg_upsert_payload(body, version):
    return {"title": (body.title or "").strip(), "reg_number": (body.reg_number or None),
            "effective_date": (body.effective_date or None), "category": (body.category or None),
            "summary": (body.summary or ""),
            "impact_stages": list(body.impact_stages or []),
            "impact_sections": list(body.impact_sections or []), "version": version}


@app.post("/regulations")
def regulation_create(body: schemas.RegulationUpsertReq,
                      user=Depends(auth.require_roles("operator")), db: Session = Depends(get_db)):
    """신규 법령 draft 생성 — reg_id 발급, version 1 기록."""
    if not (body.title or "").strip():
        raise HTTPException(422, {"code": "TITLE_REQUIRED"})
    import uuid
    reg_id = "reg_" + uuid.uuid4().hex[:12]
    sm.record_event(db, _reg_shim(reg_id), None, "draft", "regulation.upserted",
                    user["role"], user["uid"], _reg_upsert_payload(body, 1))
    db.commit()
    return {"ok": True, "reg_id": reg_id, "state": "draft", "version": 1}


@app.put("/regulations/{reg_id}")
def regulation_update(reg_id: str, body: schemas.RegulationUpsertReq,
                      user=Depends(auth.require_roles("operator")), db: Session = Depends(get_db)):
    """법령 수정 = 새 버전 누적(상태는 유지). 영향매핑도 같은 payload에 저장."""
    if not (body.title or "").strip():
        raise HTTPException(422, {"code": "TITLE_REQUIRED"})
    evs = _reg_events(db, reg_id)
    if _reg_content(db, reg_id, evs) is None:
        raise HTTPException(404, {"code": "REGULATION_NOT_FOUND"})
    state = _reg_state(db, reg_id, evs)
    ver = sum(1 for e in evs if e.action == "regulation.upserted") + 1
    sm.record_event(db, _reg_shim(reg_id), state, state, "regulation.upserted",
                    user["role"], user["uid"], _reg_upsert_payload(body, ver))
    db.commit()
    return {"ok": True, "reg_id": reg_id, "state": state, "version": ver}


@app.post("/regulations/{reg_id}/transition")
def regulation_transition(reg_id: str, body: schemas.RegulationTransitionReq,
                          user=Depends(auth.require_roles("operator")),
                          db: Session = Depends(get_db)):
    """상태전이 draft→review→effective(발효)·retired. 발효 시 관련 역할에 알림."""
    to_state = (body.to_state or "").strip()
    if to_state not in REG_TRANSITIONS:
        raise HTTPException(422, {"code": "INVALID_STATE", "allowed": list(REG_TRANSITIONS.keys())})
    evs = _reg_events(db, reg_id)
    content = _reg_content(db, reg_id, evs)
    if content is None:
        raise HTTPException(404, {"code": "REGULATION_NOT_FOUND"})
    cur = _reg_state(db, reg_id, evs)
    rule = REG_TRANSITIONS[to_state]
    if cur not in rule["from"]:
        raise HTTPException(409, {"code": "ILLEGAL_TRANSITION", "from": cur, "to": to_state,
                                  "allowed_from": list(rule["from"])})
    reason = (body.reason or "").strip()
    sm.record_event(db, _reg_shim(reg_id), cur, to_state, rule["action"],
                    user["role"], user["uid"], {"reason": reason, "version": content["version"]})
    # 발효 시 운영·감사 역할에 인앱 알림(전용 테이블 없음 — Notification 큐, case=None 전역).
    if to_state == "effective":
        title = "법령 발효 · %s" % (content["title"] or reg_id)
        body_txt = "법령번호 %s · 시행일 %s%s" % (
            content["reg_number"] or "-", content["effective_date"] or "-",
            (" · 사유: " + reason) if reason else "")
        for rid in ("operator", "auditor"):
            _notify(db, None, "regulation.effective", title, body=body_txt, role=rid)
    db.commit()
    return {"ok": True, "reg_id": reg_id, "from": cur, "to": to_state, "action": rule["action"]}


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
def _company_key(c):
    """회사 식별자 — 상호와 사업자번호를 함께 본다. org 하나에 여러 회사가 든 경우를 가른다.

    번호만으로는 부족했다. 새 신청이 org 프로필(사업자번호 포함)을 상속하는 탓에
    서로 다른 회사 셋이 같은 번호를 달고 있었다(실측: 바이오로제트·질경이·천우건설).
    둘 다 같아야 같은 회사로 본다 — 덜 묶이면 공장을 다시 고르면 그만이지만, 잘못 묶이면
    남의 공장이 남의 인증 신청서에 실린다."""
    return ((c.nib or "").strip(), (c.company_name or "").strip())


def _facilities_for_case(db, c):
    """이 신청의 '회사 자산' 공장 — 같은 org라도 다른 회사가 이미 쓰는 공장은 뺀다.

    모델은 org=회사(1:N 공장)를 전제하지만, 실제 데이터에는 org 하나에 여러 회사가
    들어있는 경우가 있다(데모 org). 그러면 케이스 생성 시 org 공장을 전부 상속해
    인도네시아 케이터링 신청서에 한국 공장이 붙는다(CV. CITRA PRATAMA 실측).

    가르는 기준은 '다른 회사가 이미 연결해 쓰는 공장인가'다. org=회사인 정상 데이터에서는
    같은 회사의 지난 신청이 걸릴 뿐이라 아무것도 빠지지 않는다 — 회사 자산 상속은 그대로다.
    """
    rows = (db.query(models.Facility).filter_by(org_id=c.org_id)
            .order_by(models.Facility.created_at).all())
    mine = set(c.facility_ids or [])
    me = _company_key(c)
    taken = set()
    for other in db.query(models.CaseApplication).filter(
            models.CaseApplication.org_id == c.org_id,
            models.CaseApplication.case_id != c.case_id).all():
        if _company_key(other) != me:
            taken.update(other.facility_ids or [])
    return [f for f in rows if f.facility_id in mine or f.facility_id not in taken]


@app.get("/orgs/{org_id}/facilities")
def list_facilities(org_id: str, case_id: str = Query(None),
                    user=Depends(auth.get_current_user), db: Session = Depends(get_db)):
    # 타 조직 조회 차단 — org_id는 URL에서 오므로 검사 없이는 남의 회사 공장이 열린다.
    # 조회는 배정을 인정한다 — 담당 업체의 시설을 못 보면 심사가 안 된다.
    _assert_org_access(db, user, org_id)
    if case_id:   # 케이스 문맥이 있으면 그 회사의 자산만
        rows = _facilities_for_case(db, _get_case(db, case_id, user))
    else:
        rows = (db.query(models.Facility).filter_by(org_id=org_id)
                .order_by(models.Facility.created_at).all())
    return [{"facility_id": f.facility_id, "name": f.name, "address": f.address, "city": f.city,
             "country": f.country, "zip": f.zip, "reg_no": f.reg_no,
             "profile_ext": f.profile_ext or {}, "created_at": str(f.created_at)} for f in rows]


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


@app.patch("/facilities/{facility_id}")
def update_facility(facility_id: str, body: schemas.FacilityUpdateReq,
                    user=Depends(auth.get_current_user), db: Session = Depends(get_db)):
    """공장 상세 편집 — 주소/도시/국가/우편/등록번호/상세를 Facility 테이블에 저장."""
    f = db.get(models.Facility, facility_id)
    if not f:
        raise HTTPException(404, {"code": "FACILITY_NOT_FOUND"})
    for k in ("name", "address", "city", "country", "zip", "reg_no"):
        v = getattr(body, k)
        if v is not None:
            setattr(f, k, v)
    if body.profile_ext is not None:
        f.profile_ext = {**(f.profile_ext or {}), **body.profile_ext}
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


# ===== 동적 메뉴 시스템 (설계서 §3~§5) =====
def _ensure_menu_assign(db):
    """'메뉴 배정 관리'(관리자 통합 배정) — admin 역할만 노출.
    개인화는 '내 메뉴 설정'(_ensure_my_menu, 전체 역할)으로 분리."""
    existing = db.query(models.SysMenu).filter_by(menu_code="MENU_ASSIGN").first()
    if existing:   # admin 외 역할 배정 제거(통합 배정은 관리자 전용) + admin 보장
        db.query(models.SysRoleMenu).filter(
            models.SysRoleMenu.menu_id == existing.menu_id,
            models.SysRoleMenu.role_id != "admin").delete(synchronize_session=False)
        if not db.query(models.SysRoleMenu).filter_by(
                menu_id=existing.menu_id, role_id="admin").first():
            db.add(models.SysRoleMenu(role_id="admin", menu_id=existing.menu_id, sort_order=1))
        db.commit()
        return
    grp = db.query(models.SysMenu).filter_by(menu_code="GRP_ADMIN").first()
    if not grp:
        gid = models.uid()
        grp = models.SysMenu(menu_id=gid, menu_code="GRP_ADMIN", menu_depth=1,
                             menu_type="folder", icon_name="⚙️", default_sort_order=99)
        db.add(grp)
        for lang, name in [("ko", "시스템 관리"), ("en", "Administration"), ("id", "Administrasi")]:
            db.add(models.SysMenuI18n(menu_id=gid, language_code=lang, menu_name=name))
        db.flush()
    mid = models.uid()
    db.add(models.SysMenu(menu_id=mid, menu_code="MENU_ASSIGN", parent_menu_id=grp.menu_id,
                          menu_depth=2, menu_type="screen", route_path="menuAssign",
                          icon_name="🗂", default_sort_order=1, system_admin_yn=True))
    for lang, name in [("ko", "메뉴 배정 관리"), ("en", "Menu Assignment"), ("id", "Penetapan Menu")]:
        db.add(models.SysMenuI18n(menu_id=mid, language_code=lang, menu_name=name))
    db.add(models.SysRoleMenu(role_id="admin", menu_id=mid, sort_order=1))   # 관리자 전용
    db.commit()


def _ensure_consultant_menus(db):
    """영업 화면 2개 — 컨설턴트의 '내 영업'과 운영자의 '컨설턴트 관리'(idempotent).

    컨설턴트는 자기가 유치한 고객과 실적을 보고, 운영자는 컨설턴트를 등록하고 요율을
    정하고 수수료를 정산한다. 서로 보는 것이 달라 화면을 나눈다."""
    SPEC = [
        # (코드, 그룹, 라우트, 아이콘, 정렬, 역할, 이름)
        ("CONSULTANT_SALES", "GRP_3", "consultantSales", "🤝", 7, ("consultant",),
         [("ko", "내 영업·고객"), ("en", "My Sales"), ("id", "Penjualan Saya")]),
        ("CONSULTANT_ADMIN", "GRP_ADMIN", "consultantAdmin", "💼", 7, ("ops", "admin"),
         [("ko", "컨설턴트 관리"), ("en", "Consultants"), ("id", "Konsultan")]),
    ]
    changed = False
    for code, gcode, route, icon, sort, roles, names in SPEC:
        row = db.query(models.SysMenu).filter_by(menu_code=code).first()
        if not row:
            grp = db.query(models.SysMenu).filter_by(menu_code=gcode).first()
            if not grp:
                continue
            mid = models.uid()
            db.add(models.SysMenu(menu_id=mid, menu_code=code, parent_menu_id=grp.menu_id,
                                  menu_depth=2, menu_type="screen", route_path=route,
                                  icon_name=icon, default_sort_order=sort))
            for lang, name in names:
                db.add(models.SysMenuI18n(menu_id=mid, language_code=lang, menu_name=name))
            changed = True
        else:
            mid = row.menu_id
        have = {rm.role_id for rm in
                db.query(models.SysRoleMenu).filter_by(menu_id=mid).all()}
        for r in roles:
            if r not in have:
                db.add(models.SysRoleMenu(role_id=r, menu_id=mid, sort_order=sort))
                changed = True
    if changed:
        db.commit()


def _ensure_my_menu(db):
    """'내 메뉴 설정'(개인화) 사이드바 노출 + 전체 역할 배정(idempotent).
    각 사용자가 자기 좌측 메뉴 구성·순서를 본인 계정에서 개별 관리 → sys_user_menu(본인 id)."""
    _ALL = ("client", "consultant", "auditor", "sharia", "ops", "admin")
    existing = db.query(models.SysMenu).filter_by(menu_code="MY_MENU").first()
    if existing:
        have = {rm.role_id for rm in
                db.query(models.SysRoleMenu).filter_by(menu_id=existing.menu_id).all()}
        added = False
        for role in _ALL:
            if role not in have:
                db.add(models.SysRoleMenu(role_id=role, menu_id=existing.menu_id, sort_order=9))
                added = True
        if added:
            db.commit()
        return
    grp = db.query(models.SysMenu).filter_by(menu_code="GRP_7").first()   # 내 정보 관리
    if not grp:
        return
    mid = models.uid()
    db.add(models.SysMenu(menu_id=mid, menu_code="MY_MENU", parent_menu_id=grp.menu_id,
                          menu_depth=2, menu_type="screen", route_path="myMenu",
                          icon_name="⚙", default_sort_order=9))
    for lang, name in [("ko", "내 메뉴 설정"), ("en", "My Menu"), ("id", "Menu Saya")]:
        db.add(models.SysMenuI18n(menu_id=mid, language_code=lang, menu_name=name))
    for role in _ALL:
        db.add(models.SysRoleMenu(role_id=role, menu_id=mid, sort_order=9))
    db.commit()


def _ensure_approvals_menu(db):
    """'승인함'(2인 승인) 사이드바 — maker/checker 역할(ops=operator, sharia=fatwa_liaison, admin). idempotent."""
    _ROLES = ("ops", "sharia", "admin")   # sys_role_menu는 매핑된 role_id 사용
    existing = db.query(models.SysMenu).filter_by(menu_code="APPROVALS").first()
    if existing:
        have = {rm.role_id for rm in
                db.query(models.SysRoleMenu).filter_by(menu_id=existing.menu_id).all()}
        added = False
        for r in _ROLES:
            if r not in have:
                db.add(models.SysRoleMenu(role_id=r, menu_id=existing.menu_id, sort_order=6))
                added = True
        if added:
            db.commit()
        return
    grp = db.query(models.SysMenu).filter_by(menu_code="GRP_1").first()   # 개요
    if not grp:
        return
    mid = models.uid()
    db.add(models.SysMenu(menu_id=mid, menu_code="APPROVALS", parent_menu_id=grp.menu_id,
                          menu_depth=2, menu_type="screen", route_path="approvals",
                          icon_name="✅", default_sort_order=6))
    for lang, name in [("ko", "승인함"), ("en", "Approvals"), ("id", "Persetujuan")]:
        db.add(models.SysMenuI18n(menu_id=mid, language_code=lang, menu_name=name))
    for r in _ROLES:
        db.add(models.SysRoleMenu(role_id=r, menu_id=mid, sort_order=6))
    db.commit()


def _fix_report_route(db):
    """'보고서'(REPORT) 메뉴가 파트와 화면을 가리키던 것을 바로잡는다. idempotent.

    REPORT는 오디터·컨설턴트에게, FATWA는 샤리아·운영에게 배정돼 있어 역할 구분은
    처음부터 맞았는데 route_path만 fatwa로 남아 있었다(정렬순서도 6으로 겹침).
    그 결과 오디터가 '보고서'를 누르면 파트와 심의 화면으로 튕겼다.

    시드는 이미 시드된 DB를 건드리지 않으므로(위 조기반환) 운영 DB는 여기서 교정한다.
    사람이 의도적으로 다른 화면을 지정한 경우는 건드리지 않는다 — fatwa일 때만 고친다."""
    rep = db.query(models.SysMenu).filter_by(menu_code="REPORT").first()
    if not rep or rep.route_path != "fatwa":
        return
    rep.route_path = "auditReport"
    db.commit()


def _ensure_inquiry_menu(db):
    """'홈페이지 문의함' 사이드바 — glhac.com 문의하기로 들어온 익명 글을 답변하는 화면.

    좌측 메뉴는 DB(sys_menu)에서 온다 — index.html 의 NAV 는 DB 가 비었을 때만 쓰는
    폴백이라, 여기 등록하지 않으면 화면은 있는데 메뉴에 안 뜬다.
    노출 역할은 서버 _BOARD_STAFF 와 같다(클라이언트 제외 — 남의 문의를 보면 안 된다).
    idempotent."""
    _ROLES = ("consultant", "auditor", "sharia", "ops", "admin")
    existing = db.query(models.SysMenu).filter_by(menu_code="INQUIRY").first()
    if existing:
        have = {rm.role_id for rm in
                db.query(models.SysRoleMenu).filter_by(menu_id=existing.menu_id).all()}
        added = False
        for r in _ROLES:
            if r not in have:
                db.add(models.SysRoleMenu(role_id=r, menu_id=existing.menu_id, sort_order=7))
                added = True
        if added:
            db.commit()
        return
    grp = db.query(models.SysMenu).filter_by(menu_code="GRP_5").first()   # 지원
    if not grp:
        grp = db.query(models.SysMenu).filter_by(menu_code="GRP_1").first()   # 없으면 개요로
    if not grp:
        return
    mid = models.uid()
    db.add(models.SysMenu(menu_id=mid, menu_code="INQUIRY", parent_menu_id=grp.menu_id,
                          menu_depth=2, menu_type="screen", route_path="inquiry",
                          icon_name="\U0001f4ee", default_sort_order=7))
    for lang, name in [("ko", "\ud648\ud398\uc774\uc9c0 \ubb38\uc758\ud568"),
                       ("en", "Website Inquiries"), ("id", "Pertanyaan Situs")]:
        db.add(models.SysMenuI18n(menu_id=mid, language_code=lang, menu_name=name))
    for r in _ROLES:
        db.add(models.SysRoleMenu(role_id=r, menu_id=mid, sort_order=7))
    db.commit()


def seed_menus(db):
    """현행 메뉴 구조(menu_seed.json)를 DB에 시드 — idempotent. 설계서 §10 마이그레이션."""
    import json as _json
    if db.query(models.SysMenu).first():
        _ensure_menu_assign(db)   # 이미 시드됨 — 관리 메뉴만 보강(사이드바 노출)
        _ensure_my_menu(db)       # '내 메뉴 설정'(개인화) 보강
        _ensure_approvals_menu(db)  # '승인함'(2인 승인) 보강
        _fix_report_route(db)     # '보고서' 메뉴가 파트와로 가던 라우팅 교정
        _ensure_consultant_menus(db)   # 영업(유치)·컨설턴트 관리 화면
        _ensure_inquiry_menu(db)       # '홈페이지 문의함'(익명 게시판)
        return
    path = os.path.join(os.path.dirname(__file__), "menu_seed.json")
    if not os.path.exists(path):
        return
    data = _json.load(open(path, encoding="utf-8"))
    code2id = {}
    for m in data["menus"]:
        mid = models.uid()
        code2id[m["menu_code"]] = mid
        db.add(models.SysMenu(menu_id=mid, menu_code=m["menu_code"], menu_depth=m["depth"],
                              menu_type=m["type"], route_path=m.get("route"), icon_name=m.get("icon"),
                              default_sort_order=m["sort"]))
        for lang, name in (m.get("i18n") or {}).items():
            db.add(models.SysMenuI18n(menu_id=mid, language_code=lang, menu_name=name))
    db.flush()
    for m in data["menus"]:
        if m.get("parent"):
            row = db.get(models.SysMenu, code2id[m["menu_code"]])
            row.parent_menu_id = code2id.get(m["parent"])
    for rm in data["role_menu"]:
        mid = code2id.get(rm["menu_code"])
        if mid:
            db.add(models.SysRoleMenu(role_id=rm["role"], menu_id=mid, sort_order=rm["sort"]))
    db.commit()
    _ensure_menu_assign(db)   # 신규 시드에도 '메뉴 배정 관리' 사이드바 노출
    _ensure_my_menu(db)       # '내 메뉴 설정'(개인화) 사이드바 노출
    _ensure_approvals_menu(db)  # '승인함'(2인 승인) 사이드바 노출
    _fix_report_route(db)
    _ensure_consultant_menus(db)
    _ensure_inquiry_menu(db)


_BR2ROLE_MENU = {"applicant": "client", "consultant": "consultant", "auditor": "auditor",
                 "fatwa_liaison": "sharia", "operator": "ops", "admin": "admin",
                 "penyelia_halal": "client", "pendamping_pph": "client"}


@app.get("/me/menus")
def my_menus(lang: str = "ko", user=Depends(auth.get_current_user), db: Session = Depends(get_db)):
    """로그인 사용자 동적 메뉴 트리 — 사용자별 배정(있으면) > 역할 기본. 설계서 §4·§5·§3.4."""
    ums = {um.menu_id: um.sort_order for um in
           db.query(models.SysUserMenu).filter_by(user_id=user["uid"], visible_yn=True).all()}
    if ums:                                  # P4: 사용자별 배정 우선
        rms = ums
    else:
        oms = ({om.menu_id: om.sort_order for om in
                db.query(models.SysOrgMenu).filter_by(org_id=user.get("org_id"), visible_yn=True).all()}
               if user.get("org_id") else {})
        if oms:                              # P5: 기관별 배정(역할 override)
            rms = oms
        else:                                # 역할 기본
            role = _BR2ROLE_MENU.get(user["role"], user["role"])
            rms = {rm.menu_id: rm.sort_order for rm in
                   db.query(models.SysRoleMenu).filter_by(role_id=role, visible_yn=True).all()}
            # 흡수 역할(pendamping_pph→client 등)의 전용 메뉴(예: 동반자 워크스페이스)는 원본 역할로 추가 배정.
            if user["role"] != role:
                for rm in db.query(models.SysRoleMenu).filter_by(role_id=user["role"], visible_yn=True).all():
                    rms.setdefault(rm.menu_id, rm.sort_order)
    if not rms:
        return []
    return _menu_tree(db, rms, lang)


def _menu_tree(db, rms, lang):
    """배정 {menu_id: sort} → 2뎁스 트리(그룹+children) + 다국어. 설계서 §5."""
    menus = {m.menu_id: m for m in db.query(models.SysMenu).filter_by(use_yn=True).all()}
    i18n = {x.menu_id: x.menu_name for x in
            db.query(models.SysMenuI18n).filter_by(language_code=lang).all()}
    groups = {}
    for mid in rms:
        m = menus.get(mid)
        if m and m.parent_menu_id:
            groups.setdefault(m.parent_menu_id, []).append(m)
    out = []
    for pid in sorted(groups, key=lambda p: menus[p].default_sort_order if p in menus else 99):
        g = menus.get(pid)
        if not g:
            continue
        children = sorted(groups[pid], key=lambda c: rms.get(c.menu_id, c.default_sort_order))
        out.append({"menuId": g.menu_id, "menuCode": g.menu_code,
                    "menuName": i18n.get(g.menu_id, g.menu_code), "icon": g.icon_name,
                    "sortOrder": g.default_sort_order,
                    "children": [{"menuId": c.menu_id, "menuCode": c.menu_code,
                                  "menuName": i18n.get(c.menu_id, c.menu_code),
                                  "routePath": c.route_path, "icon": c.icon_name,
                                  "sortOrder": rms.get(c.menu_id)} for c in children]})
    return out


def _my_allowed_menus(db, user):
    """내가 개인화할 수 있는 범위 = 내 역할(role) 배정 메뉴. 권한 밖 메뉴 노출 방지."""
    role = _BR2ROLE_MENU.get(user["role"], user["role"])
    return {rm.menu_id: rm.sort_order for rm in
            db.query(models.SysRoleMenu).filter_by(role_id=role, visible_yn=True).all()}


@app.get("/me/menu-config")
def my_menu_config(lang: str = "ko", user=Depends(auth.get_current_user),
                   db: Session = Depends(get_db)):
    """내 메뉴 개인화 데이터 — master(등록된 전체 메뉴 + allowed 플래그) + assigned(내 현재 구성).
    좌측엔 전체 메뉴를 다 보여주고 각 항목에 사용 여부(사용가능/불가) 표시. 저장은 역할 허용 범위로 제한."""
    allowed = set(_my_allowed_menus(db, user))
    menus = db.query(models.SysMenu).filter_by(use_yn=True).all()
    i18n = {x.menu_id: x.menu_name for x in
            db.query(models.SysMenuI18n).filter_by(language_code=lang).all()}
    kids = {}
    for m in menus:
        if m.parent_menu_id:
            kids.setdefault(m.parent_menu_id, []).append(m)
    master = []
    for r in sorted([m for m in menus if not m.parent_menu_id], key=lambda x: x.default_sort_order):
        ch = [{"menuId": c.menu_id, "menuCode": c.menu_code,
               "menuName": i18n.get(c.menu_id, c.menu_code), "routePath": c.route_path,
               "icon": c.icon_name, "allowed": c.menu_id in allowed}   # 내 역할 사용가능 여부
              for c in sorted(kids.get(r.menu_id, []), key=lambda x: x.default_sort_order)]
        if not ch:
            continue
        master.append({"menuId": r.menu_id, "menuCode": r.menu_code,
                       "menuName": i18n.get(r.menu_id, r.menu_code), "icon": r.icon_name,
                       "children": ch})
    ums = {um.menu_id: um.sort_order for um in
           db.query(models.SysUserMenu).filter_by(user_id=user["uid"], visible_yn=True).all()}
    assigned = _menu_tree(db, ums if ums else _my_allowed_menus(db, user), lang)  # 미설정이면 역할 기본
    return {"master": master, "assigned": assigned}


@app.put("/me/menu-config")
def save_my_menu_config(body: schemas.MenuAssignReq, user=Depends(auth.get_current_user),
                        db: Session = Depends(get_db)):
    """내 메뉴 개인화 저장 — 내 역할 허용 메뉴로 범위 제한(권한 상승 방지). sys_user_menu(본인 id)."""
    allowed = set(_my_allowed_menus(db, user))
    for g in body.menus:                       # 역할 허용 밖 항목 제거
        g["children"] = [c for c in (g.get("children") or []) if c.get("menuId") in allowed]
    menus = {m.menu_id: m for m in db.query(models.SysMenu).all()}
    role = _BR2ROLE_MENU.get(user["role"], user["role"])
    rows = _save_assign_rows(db, menus, body, role)   # 빈그룹·중복·중지 검증(§8)
    db.query(models.SysUserMenu).filter_by(user_id=user["uid"]).delete()
    for mid, so in rows:
        db.add(models.SysUserMenu(user_id=user["uid"], menu_id=mid, sort_order=so))
    db.commit()
    return {"saved": len(rows)}


@app.post("/me/menu-config/reset")
def reset_my_menu_config(user=Depends(auth.get_current_user), db: Session = Depends(get_db)):
    """내 메뉴를 역할 기본값으로 초기화 — 내 sys_user_menu 삭제(역할 기본으로 폴백)."""
    n = db.query(models.SysUserMenu).filter_by(user_id=user["uid"]).delete()
    db.commit()
    return {"reset": n}


@app.get("/admin/menus")
def admin_menus(lang: str = "ko", user=Depends(auth.require_roles("admin")),
                db: Session = Depends(get_db)):
    """전체 메뉴 마스터 트리(배정화면 좌측). 설계서 §6.1."""
    menus = db.query(models.SysMenu).all()
    i18n = {x.menu_id: x.menu_name for x in
            db.query(models.SysMenuI18n).filter_by(language_code=lang).all()}

    def node(m):
        return {"menuId": m.menu_id, "menuCode": m.menu_code,
                "menuName": i18n.get(m.menu_id, m.menu_code), "menuType": m.menu_type,
                "routePath": m.route_path, "icon": m.icon_name, "useYn": m.use_yn,
                "requiredYn": m.required_yn, "systemAdminYn": m.system_admin_yn,
                "sortOrder": m.default_sort_order}
    kids = {}
    for m in menus:
        if m.parent_menu_id:
            kids.setdefault(m.parent_menu_id, []).append(m)
    out = []
    for r in sorted([m for m in menus if not m.parent_menu_id], key=lambda x: x.default_sort_order):
        n = node(r)
        n["children"] = [node(c) for c in sorted(kids.get(r.menu_id, []), key=lambda x: x.default_sort_order)]
        out.append(n)
    return out


@app.get("/admin/assign/role/{role_id}/menus")
def get_role_assign(role_id: str, lang: str = "ko", user=Depends(auth.require_roles("admin")),
                    db: Session = Depends(get_db)):
    """역할 배정 메뉴 트리(배정화면 우측). 설계서 §6.1."""
    rms = {rm.menu_id: rm.sort_order for rm in
           db.query(models.SysRoleMenu).filter_by(role_id=role_id).all()}
    return _menu_tree(db, rms, lang)


@app.put("/admin/assign/role/{role_id}/menus")
def put_role_assign(role_id: str, body: schemas.MenuAssignReq,
                    user=Depends(auth.require_roles("admin")), db: Session = Depends(get_db)):
    """역할 배정 저장 + 검증(설계서 §8): 부모자동·빈그룹제외·중복금지·중지금지·관리자메뉴제한·순서재정렬."""
    menus = {m.menu_id: m for m in db.query(models.SysMenu).all()}
    seen = set()
    rows = []
    for gi, g in enumerate(body.menus, 1):
        gm = menus.get(g.get("menuId"))
        if not gm:
            continue
        valid = []
        for c in (g.get("children") or []):
            cm = menus.get(c.get("menuId"))
            if not cm or cm.menu_id in seen:      # §8-3 중복 금지
                continue
            if not cm.use_yn:                      # §8-6 사용중지 메뉴 배정 금지
                continue
            if cm.system_admin_yn and role_id not in ("admin", "ops"):  # §8-5 관리자 메뉴 제한
                continue
            seen.add(cm.menu_id)                   # 즉시 추가 → 그룹 내·간 중복 모두 차단
            valid.append(cm)
        if not valid:                              # §8-2 빈 1뎁스 제외
            continue
        if gm.menu_id not in seen:                 # §8-1 부모 자동 추가
            seen.add(gm.menu_id)
            rows.append((gm.menu_id, gi))
        for ci, cm in enumerate(valid, 1):
            rows.append((cm.menu_id, ci))          # §8-7 순서 자동 재정렬
    db.query(models.SysRoleMenu).filter_by(role_id=role_id).delete()
    for mid, so in rows:
        db.add(models.SysRoleMenu(role_id=role_id, menu_id=mid, sort_order=so))
    db.commit()
    return {"role_id": role_id, "saved": len(rows)}


def _save_assign_rows(db, menus, body, target_role):
    """배정 검증 공통(설계서 §8) → [(menu_id, sort_order)]. target_role은 관리자메뉴 제한 판정용."""
    seen = set()
    rows = []
    for gi, g in enumerate(body.menus, 1):
        gm = menus.get(g.get("menuId"))
        if not gm:
            continue
        valid = []
        for c in (g.get("children") or []):
            cm = menus.get(c.get("menuId"))
            if not cm or cm.menu_id in seen or not cm.use_yn:
                continue
            if cm.system_admin_yn and target_role not in ("admin", "ops"):
                continue
            seen.add(cm.menu_id)
            valid.append(cm)
        if not valid:
            continue
        if gm.menu_id not in seen:
            seen.add(gm.menu_id)
            rows.append((gm.menu_id, gi))
        for ci, cm in enumerate(valid, 1):
            rows.append((cm.menu_id, ci))
    return rows


@app.get("/admin/assign/user/{user_id}/menus")
def get_user_assign(user_id: str, lang: str = "ko", user=Depends(auth.require_roles("admin")),
                    db: Session = Depends(get_db)):
    """사용자별 배정 메뉴 트리 — 설계서 §3.4·§6.3. 빈 배정이면 [](reset로 역할 복사)."""
    ums = {um.menu_id: um.sort_order for um in
           db.query(models.SysUserMenu).filter_by(user_id=user_id).all()}
    return _menu_tree(db, ums, lang)


@app.put("/admin/assign/user/{user_id}/menus")
def put_user_assign(user_id: str, body: schemas.MenuAssignReq,
                    user=Depends(auth.require_roles("admin")), db: Session = Depends(get_db)):
    """사용자별 배정 저장 — 설계서 §3.4·§8. 관리자메뉴 제한은 대상 사용자 역할 기준."""
    menus = {m.menu_id: m for m in db.query(models.SysMenu).all()}
    u = db.get(models.User, user_id)
    urole = _BR2ROLE_MENU.get(u.role, u.role) if u else ""
    rows = _save_assign_rows(db, menus, body, urole)
    db.query(models.SysUserMenu).filter_by(user_id=user_id).delete()
    for mid, so in rows:
        db.add(models.SysUserMenu(user_id=user_id, menu_id=mid, sort_order=so))
    db.commit()
    return {"user_id": user_id, "saved": len(rows)}


@app.post("/admin/assign/user/{user_id}/reset-to-role")
def reset_user_to_role(user_id: str, user=Depends(auth.require_roles("admin")),
                       db: Session = Depends(get_db)):
    """사용자 배정을 역할 기본값으로 재설정(복사) — 설계서 §6.3."""
    u = db.get(models.User, user_id)
    if not u:
        raise HTTPException(404, {"code": "USER_NOT_FOUND"})
    role = _BR2ROLE_MENU.get(u.role, u.role)
    rms = db.query(models.SysRoleMenu).filter_by(role_id=role).all()
    db.query(models.SysUserMenu).filter_by(user_id=user_id).delete()
    for rm in rms:
        db.add(models.SysUserMenu(user_id=user_id, menu_id=rm.menu_id, sort_order=rm.sort_order))
    db.commit()
    return {"user_id": user_id, "copied": len(rms), "role": role}


# ===== P5: 기관별 배정 =====
@app.get("/admin/assign/org/{org_id}/menus")
def get_org_assign(org_id: str, lang: str = "ko", user=Depends(auth.require_roles("admin")),
                   db: Session = Depends(get_db)):
    """기관별 배정 메뉴 트리 — 설계서 §3.5."""
    oms = {om.menu_id: om.sort_order for om in
           db.query(models.SysOrgMenu).filter_by(org_id=org_id).all()}
    return _menu_tree(db, oms, lang)


@app.put("/admin/assign/org/{org_id}/menus")
def put_org_assign(org_id: str, body: schemas.MenuAssignReq,
                   user=Depends(auth.require_roles("admin")), db: Session = Depends(get_db)):
    """기관별 배정 저장 — 설계서 §3.5·§8. 기관은 다양한 역할 포함 → 관리자메뉴 허용."""
    menus = {m.menu_id: m for m in db.query(models.SysMenu).all()}
    rows = _save_assign_rows(db, menus, body, "admin")
    db.query(models.SysOrgMenu).filter_by(org_id=org_id).delete()
    for mid, so in rows:
        db.add(models.SysOrgMenu(org_id=org_id, menu_id=mid, sort_order=so))
    db.commit()
    return {"org_id": org_id, "saved": len(rows)}


# ===== P6: 기능 권한 (메뉴 노출과 분리) =====
_PERM_FIELDS = ["view", "create", "update", "delete", "submit", "approve", "sign", "download"]


@app.get("/admin/assign/{atype}/{target_id}/permissions")
def get_permissions(atype: str, target_id: str, user=Depends(auth.require_roles("admin")),
                    db: Session = Depends(get_db)):
    """메뉴 기능 권한 조회(atype=role|org|user) — 설계서 §3.6·§9."""
    out = {}
    for p in db.query(models.SysMenuPermission).filter_by(
            assignment_type=atype.upper(), target_id=target_id).all():
        out[p.menu_id] = {f: bool(getattr(p, "can_" + f)) for f in _PERM_FIELDS}
    return out


@app.put("/admin/assign/{atype}/{target_id}/permissions")
def put_permissions(atype: str, target_id: str, body: schemas.MenuPermissionReq,
                    user=Depends(auth.require_roles("admin")), db: Session = Depends(get_db)):
    """메뉴 기능 권한 저장 — 설계서 §3.6·§9. {menu_id: {view,create,...}}."""
    db.query(models.SysMenuPermission).filter_by(
        assignment_type=atype.upper(), target_id=target_id).delete()
    for mid, perm in (body.permissions or {}).items():
        kw = {"can_" + f: bool((perm or {}).get(f, False)) for f in _PERM_FIELDS}
        db.add(models.SysMenuPermission(assignment_type=atype.upper(), target_id=target_id,
                                        menu_id=mid, **kw))
    db.commit()
    return {"saved": len(body.permissions or {})}


_static = os.path.join(os.path.dirname(__file__), "static")
app.mount("/ui", NoCacheStaticFiles(directory=_static, html=True), name="ui")