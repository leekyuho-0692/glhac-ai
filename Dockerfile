# GL-HAC AI v3 — 프로덕션 이미지
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    GLHAC_UPLOAD_DIR=/data/uploads

WORKDIR /app

# 의존성 먼저(레이어 캐시)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# 업로드 샌드박스 + SQLite 파일 볼륨
RUN mkdir -p /data/uploads
VOLUME ["/data"]

EXPOSE 8000

# 컨테이너 헬스체크 — /health 는 DB 체크 포함(실패 시 503)
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health').status==200 else 1)"

# 단일 워커 권장(인프로세스 알림워커·SQLite 단일라이터). 멀티워커는 Postgres + 외부 cron 드레인 후.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
