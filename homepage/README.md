# GL-HAC 홈페이지 (glhac.com)

랜딩 페이지(`index.html`) + 익명 문의 게시판(`board/index.html`). 앱(glhac.co.kr)과
별개의 정적 사이트다. Vite 빌드 산출물(`assets/`)과 인트로 미디어(`intro/`)를 포함한다.

## 배포

nginx 정적 서빙. 서버 경로 `/var/www/glhac-home` (소유 nginx).

```bash
KEY=$HOME/Downloads/SSH_KeyPair-260908134815.pem; HOST=rocky@1.201.116.174
# 이 디렉터리(homepage/) 전체를 배포
rsync -az --exclude 'README.md' --exclude 'tests' --exclude '*.py' \
  -e "ssh -i $KEY -o StrictHostKeyChecking=no" ./ "$HOST":/tmp/glhac-home/
ssh -i "$KEY" -o StrictHostKeyChecking=no "$HOST" \
  'sudo cp -r /var/www/glhac-home /var/www/glhac-home.bak-$(date +%s); \
   sudo rsync -a /tmp/glhac-home/ /var/www/glhac-home/; \
   sudo chown -R nginx:nginx /var/www/glhac-home'
```

재기동 불필요(정적). `nginx` conf: `/etc/nginx/conf.d/glhac-com.conf`.

## 컨설턴트 귀속(?ref) 규칙 — **현재 URL의 ?ref 만**

QR/추천 링크(`glhac.com/?ref=<컨설턴트코드>`)로 들어온 경우에만 귀속이 흐른다.
저장분(sessionStorage/localStorage) 되살림은 하지 않는다 — `?ref` 없이 그냥 방문해
인증신청·문의를 눌렀는데 옛 코드가 붙으면 엉뚱한 컨설턴트로 귀속되기 때문이다.

- `index.html` `urlRef()`: 인증신청(앱) 링크와 문의(`./board/`) 링크에 **현재 URL의
  `?ref` 만** 부착. `ref()`(sessionStorage)는 쓰지 않는다.
- `board/index.html` `REF`: **현재 URL `?ref` 만** 읽는다(sessionStorage 폴백 제거).
- 앱 측(`glhac-ai/app/static/index.html` `glhacRef()`)도 URL 전용 — 세 경로 일관.

흐름:
```
QR → glhac.com/?ref=CODE → 인증신청/문의 링크에 ?ref 유지 → 귀속 O
그냥 방문 glhac.com/     → 링크에 ?ref 없음               → 귀속 X
```

앱 루트 리다이렉트(`/` → `/ui/`)는 쿼리스트링을 보존한다(`glhac-ai/app/main.py`
`_root_redirect`) — 안 그러면 `glhac.co.kr/?ref=CODE`에서 ref가 유실된다.
