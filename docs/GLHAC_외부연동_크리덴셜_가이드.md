# GL-HAC AI v3 — 외부연동 크리덴셜 가이드

발송·결제 채널의 **어댑터는 모두 구현 완료**되어 있으며, 운영에서 필요한 것은
**크리덴셜(환경변수) 설정**뿐입니다. 크리덴셜 미설정 시 해당 채널은
`no_credentials` 스텁으로 동작(로그만 남기고 발송/결제 실패 처리)하므로 안전합니다.

- 모든 값은 `.env`(커밋 금지)에 넣습니다. 예시는 `.env.example` 참조.
- **설정 상태 확인**: 관리자 토큰으로 `GET /admin/notify-channels` → 각 채널의
  `configured`/`status`(connected|unset)를 **값 노출 없이** 반환합니다.
- 프로덕션 반영: `.env` 갱신 후 서비스 재시작(launchd: `com.glhac.app` unload/load).

---

## 1. SMS / WhatsApp — Twilio

| 항목 | 값 |
|------|-----|
| 발급 | https://console.twilio.com → Account SID / Auth Token, 발신번호 구매 |
| 환경변수 | `GLHAC_TWILIO_SID`, `GLHAC_TWILIO_TOKEN`, `GLHAC_TWILIO_SMS_FROM`, `GLHAC_TWILIO_WA_FROM` |
| 활성 조건 | SID+TOKEN+해당 FROM 모두 설정 |
| 구현 | `app/notify.py:_twilio_send` (SMS/WhatsApp 공용, `whatsapp:` 접두 자동) |

WhatsApp은 Twilio에서 WhatsApp Sender 승인이 별도로 필요합니다(샌드박스는 테스트용).

## 2. KakaoTalk 알림톡 — 비즈메시지 대행사(Solapi·Aligo·NHN 등)

| 항목 | 값 |
|------|-----|
| 발급 | 대행사 가입 → 발신프로필 등록 → 알림톡 템플릿 심사 승인 |
| 환경변수 | `GLHAC_KAKAO_API_URL`, `GLHAC_KAKAO_API_KEY`, `GLHAC_KAKAO_SENDER_KEY`(선택), `GLHAC_KAKAO_TEMPLATE_CODE`(선택) |
| 활성 조건 | API_URL+API_KEY 둘 다 설정 |
| 구현 | `app/notify.py:_send_kakao` — Bearer 인증 HTTP POST, `{to,text,senderKey,templateCode}` |

대행사별 요청 포맷이 다르면 `_send_kakao`의 `json=` 페이로드를 대행사 스펙에 맞춰 조정합니다.

## 3. Email — SMTP

| 항목 | 값 |
|------|-----|
| 환경변수 | `GLHAC_SMTP_HOST`, `GLHAC_SMTP_PORT`(기본 587), `GLHAC_SMTP_USER`, `GLHAC_SMTP_PASS`, `GLHAC_SMTP_FROM` |
| 활성 조건 | HOST+FROM 설정(USER/PASS 없으면 비인증 릴레이) |
| 구현 | `app/notify.py:_send_email` — STARTTLS 시도, 제목 `[GL-HAC] ...` |

## 4. Webhook — 외부 시스템 이벤트 수신

| 항목 | 값 |
|------|-----|
| 환경변수 | `GLHAC_WEBHOOK_URL`(전역), `GLHAC_WEBHOOK_SECRET`(HMAC-SHA256 서명) |
| 구현 | `app/notify.py:_send_webhook` — POST + `X-Signature` HMAC 헤더 |

케이스별 webhook URL은 별도 등록 가능(전역 URL은 fallback).

## 5. 결제 PG — Midtrans / Xendit

| 항목 | 값 |
|------|-----|
| 콜백 수신 | `POST /pg/webhook/{provider}` — provider ∈ {midtrans, xendit, manual} |
| 환경변수 | `GLHAC_PG_WEBHOOK_SECRET`(공용 fallback), `GLHAC_PG_SECRET_MIDTRANS`, `GLHAC_PG_SECRET_XENDIT` |
| 동작 | 서명검증 → idempotency(중복 콜백 무시) → 인보이스 상태 자동반영(paid 등) |
| 구현 | `app/main.py:pg_webhook` (7323~), `_PG_PROVIDERS` |

PG 대시보드에서 콜백/알림 URL을 `https://<도메인>/pg/webhook/midtrans` 형태로 등록하고,
서명 시크릿을 provider별 환경변수에 넣습니다. 실 PG의 서명 알고리즘(Midtrans는 SHA512
`order_id+status_code+gross_amount+serverkey` 등)에 맞춰 `pg_webhook`의 검증부를 provider별로 확장합니다.

## 6. SiHALAL 신원검증 API (외부 정부 시스템)

현재 신원검증은 내부 상태(`external_identity.verification_status`) 기반이며,
SiHALAL 공식 API 연동은 **BPJPH 계약·API 키 발급 후** 어댑터를 추가하는 지점입니다(미연동).

---

## 설정 상태 확인 예시

```bash
# 관리자 토큰 발급 후
curl -s https://<도메인>/admin/notify-channels -H "Authorization: Bearer <ADMIN_TOKEN>"
# → {"channels": {"sms": {"configured": true, "status": "connected"}, "kakao": {...}, ...}}
```

프런트 알림봇 바에도 채널별 연결됨/미설정 배지가 이 엔드포인트 기준으로 표시됩니다.
