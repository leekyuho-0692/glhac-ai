# GL-HAC AI v3 — PostgreSQL 전환 가이드 (Alembic)

데모/개발은 **SQLite**(기본)로 그대로 두고, 프로덕션에서 **PostgreSQL**로 전환하는 절차입니다.
DB 엔진은 `GLHAC_DB_URL` 하나로 전환되며(`app/db.py`), 스키마는 **Alembic 마이그레이션**으로 관리합니다.

## 구성

- `alembic.ini` — 기본 URL은 SQLite. 실제 URL은 **환경변수 `GLHAC_DB_URL` 우선**(`alembic/env.py`가 오버라이드).
- `alembic/env.py` — `app.db.Base` + `app.models` 전체를 autogenerate 대상으로 연결. EncryptedType은 `sa.String`으로 렌더(마이그레이션이 앱 코드에 비의존).
- `alembic/versions/*.py` — baseline(현행 전체 스키마 52개 테이블) 포함.

## A. 신규 PostgreSQL에 배포 (권장)

```bash
# 1) PostgreSQL 준비 (예: 로컬/클라우드)
createdb glhac    # 또는 관리형 PG 인스턴스

# 2) 드라이버 설치
pip install "psycopg2-binary>=2.9"    # requirements.txt에 포함됨

# 3) 접속 URL 지정
export GLHAC_DB_URL="postgresql+psycopg2://user:pw@host:5432/glhac"

# 4) 전체 스키마 생성 (baseline → head)
alembic upgrade head

# 5) 앱 기동 (동일 GLHAC_DB_URL)
uvicorn app.main:app --host 0.0.0.0 --port 8800
```

## B. 기존 SQLite DB를 Alembic 추적으로 편입

기존 DB는 `create_all` + 경량 `_migrate`로 이미 최신 스키마입니다. 재생성 없이 baseline 리비전만 스탬프:

```bash
export GLHAC_DB_URL="sqlite:///./glhac.db"   # 기존 파일
alembic stamp head    # 스키마 변경 없이 '이 DB는 head까지 적용됨'으로 표시
```

이후 스키마 변경은 `alembic revision --autogenerate -m "설명"` → `alembic upgrade head`로 관리합니다.

## C. SQLite → PostgreSQL 데이터 이관 (선택)

스키마는 A로 생성하고, 데이터만 옮길 때:

- 소규모: 앱 레벨 덤프/로드 스크립트(테이블별 SELECT→INSERT) 또는 `pgloader sqlite:///glhac.db postgresql://...`
- 이관 후 시퀀스/제약 검증, `alembic current`가 head인지 확인.

## 주의 / PG 특화 포인트

- **JSON 컬럼**: SQLAlchemy `JSON`은 PG에서 `JSON` 타입으로 매핑(필요 시 JSONB로 승격 가능).
- **FK 강제**: SQLite는 PRAGMA로 활성화(`app/db.py`), PG는 기본 강제. Alembic 스키마에 FK가 실제 반영됨.
- **동시성**: PG는 다중 라이터 지원 → 멀티워커/멀티컨테이너 배포 가능(SQLite 단일 라이터 제약 해소).
- **create_all 공존**: 앱 시작 시 `create_all`이 여전히 idempotent로 실행되지만, PG에서도 기존 테이블은 건드리지 않습니다. 순수 Alembic 운영을 원하면 `main.py`의 `create_all`/`_migrate` 호출을 제거하고 배포 파이프라인에서 `alembic upgrade head`만 수행하세요.

## 검증 (완료됨)

- 빈 DB에 `alembic upgrade head` → 52개 테이블 전부 생성 확인(모델 정의와 일치).
- 최신 컬럼(annual_revenue·outlet_count) 및 approval_request(2인 승인) 포함.
- EncryptedType → VARCHAR 렌더(앱 코드 비의존).
