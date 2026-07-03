# GL-HAC AI — Dual-Pathway Backend (M1 MVP)

설계: `GL_HAC_AI_프로덕션_보강설계서_워크플로우Depth_v2_2026-06-30.md` Part 24 구현체.
스택: **Python 3.11 · FastAPI · SQLite** + 로컬 AI(**Ollama qwen2.5 / PaddleOCR**, 설계 24.16).

## 구성
- `app/models.py` — 24.9 스키마(case.pathway, penyelia/pendamping/lph, external_identity, ontology)
- `app/state_machine.py` — 24.2.6 전이표 + 24.11 가드 + 해시체인 감사(B.5)
- `app/screening.py` + `app/ontology_seed.py` — 24.13 임계원재료 스크리닝
- `app/ai_local.py` — 로컬 AI 어댑터(Ollama/PaddleOCR, 24.16) — 추론은 M3
- `app/main.py` — 24.14 API

## 실행
```bash
python3.11 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn app.main:app --reload --port 8800
# OpenAPI: http://localhost:8800/docs
```

## 테스트
```bash
.venv/bin/python tests/test_smoke.py        # 또는 .venv/bin/pytest -q
```

## DB 마이그레이션 (Alembic · §6.1)
개발은 SQLite 기본(startup의 create_all로 자동 생성). 프로덕션은 **Alembic + PostgreSQL** 권장.

```bash
# DB 지정(SQLite 기본, Postgres는 아래)
export GLHAC_DB_URL="postgresql+psycopg2://glhac:glhac@localhost:5432/glhac"   # 또는 sqlite:///./glhac.db

# 신규 DB: 전체 스키마 적용
.venv/bin/alembic upgrade head

# 기존 DB(이미 create_all로 생성됨): 기준선만 표시
.venv/bin/alembic stamp 0001_baseline

# 스키마 변경 후 마이그레이션 자동생성 → 검토 → 적용
.venv/bin/alembic revision --autogenerate -m "설명"
.venv/bin/alembic upgrade head
```
- `alembic/env.py` 는 앱과 동일한 `GLHAC_DB_URL`·`Base.metadata` 사용(SQLite/Postgres 양립, SQLite는 batch 모드).
- Postgres 사용 시 `requirements.txt`의 `psycopg2-binary` 주석 해제. `docker compose --profile pg up` 로 Postgres 기동.
- 앱 startup의 create_all은 idempotent — Alembic과 공존(테이블 있으면 no-op).

## 핵심 흐름 (자기선언/정규 이중경로)
```
onboarding → … → pathway_determination
  ├─ self_declare: SD eligible → sjph_lite → pendamping 검증 → 제출 → ketetapan → 인증서
  └─ reguler: consultant_review → 사전심사 → LPH → 현장심사 → fatwa → 인증서
```
- AI는 제안만(경로판정), 전이는 사람/시스템 액션, **SIHALAL 식별자 미검증 시 제출 차단**.
- 모든 전이는 `workflow_event`(append-only + 해시체인)에 기록.

## 로컬 AI
- Ollama: `brew services start ollama && ollama pull qwen2.5:7b` → `GET /ai/health`
- PaddleOCR: Python 3.11 venv (3.14 미지원)