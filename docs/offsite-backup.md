# 맥미니 외부 DB 백업

맥미니가 매일 04:30 서버의 최신 `glhac_v3-YYYYmmdd-HHMMSS.db.gz`를 가져온다. 서버의 기존 03:00 `glhac-backup.timer`와 14일 보관은 그대로 사용하며, 서버 설치·설정 변경은 없다. 서버 파일은 root 소유 0644이므로 `rocky`로 읽는다. 원격 목록은 SSH의 `ls -ln --time-style=+%s`만 실행하며, 다운로드는 기존 서버의 rsync sender를 사용한다. 서버의 GNU ls 시간 형식을 숫자 한 필드로 고정해 언어 설정에 영향을 받지 않는다.

## 설정

macOS 기본 Bash 3.2, ssh, rsync, gzip, `/usr/bin/sqlite3`, shasum, osascript와 기본 파일 도구를 사용한다. Homebrew/Python은 실행에 필요 없다.

| 환경변수 | 기본값 / 의미 |
|---|---|
| `GLHAC_BACKUP_HOST` | 필수. IPv4 또는 DNS 호스트명 |
| `GLHAC_BACKUP_USER` | `rocky` |
| `GLHAC_BACKUP_KEY` | 필수. 읽을 수 있는 SSH 키 파일의 절대경로 |
| `GLHAC_BACKUP_REMOTE_DIR` | `/var/backups/glhac` |
| `GLHAC_BACKUP_LOCAL_DIR` | `$HOME/Backups/glhac-offsite` |
| `GLHAC_BACKUP_MIRROR_DIR` | 기본 비활성. 예: `/Volumes/MGTEC/GLHAC_Backups` |
| `GLHAC_BACKUP_MAX_AGE_HOURS` | `36`, 양의 정수 |
| `GLHAC_BACKUP_SSH_TIMEOUT_SECONDS` | `60`, 원격 목록 조회 전체 실행 제한(초), 1~999999 |
| `GLHAC_BACKUP_RSYNC_TIMEOUT_SECONDS` | `1800`, 다운로드·미러의 각 rsync 호출 전체 실행 제한(초), 1~999999 |
| `GLHAC_BACKUP_NOTIFY` | `1`: 경고·실패 알림, `0`: 알림 끄기 |

경로에는 절대경로를 쓴다. 로컬·키 경로의 공백과 따옴표를 지원한다. 원격 경로는 `/`, 영문/숫자, `_`, `-`, `.`만 허용하며 `..`, `.` 경로 성분과 중복 `/`를 거부한다. SSH가 원격 인자를 셸로 해석하므로 이 검사 후 고정 `ls` 인자에 전달한다. 호스트와 사용자도 허용 문자로 검증한다.

키 인증과 검증된 서버 호스트 키의 `~/.ssh/known_hosts` 등록은 설치 전에 준비되어 있어야 한다. 대화형 인증을 허용하지 않으며 `StrictHostKeyChecking=yes`를 사용한다. `ssh -F /dev/null`로 개인 SSH 설정을 읽지 않으므로 Host 별칭, ProxyCommand 등에는 의존하지 않는다. 키 내용은 읽어 출력하거나 로그에 기록하지 않는다.

연결 제한·keepalive·rsync의 I/O 제한과 별개로 Bash 3.2 백그라운드 감시가 각 명령의 전체 실행 시간을 제한한다. 제한을 넘으면 명령과 자식 프로세스(전송용 SSH 포함)에 TERM을 보내고, 1초 뒤 남은 프로세스를 KILL로 종료한다. 시간 초과는 ERROR·알림과 종료코드 1로 처리한다. macOS에 없는 `timeout` 명령이나 추가 패키지에 의존하지 않는다. 이 제한은 각 SSH/rsync 호출에 적용되며 SQLite 검사 등 로컬 처리나 전체 실행 횟수의 합을 제한하는 값은 아니다.

파일명에는 시간대 정보가 없다. 서버와 맥미니가 한국 시간이라는 전제로 예제는 `TZ=Asia/Seoul`을 지정한다. 수동 실행도 같은 시간대를 사용한다. 다른 시간대를 쓰는 서버라면 이를 맞춘다. launchd의 04:30 스케줄은 **맥의 시스템 시간대**를 따른다.

## 설치: 사용자 LaunchAgent

아래 명령은 설치자가 맥미니에서 실행할 절차다. 예제 파일만 생성한 상태에서는 작업이 등록되거나 서버에 접속하지 않는다.

1. 보관 디렉터리를 먼저 만든다. launchd는 스크립트 시작 전에 stdout/stderr 파일을 열기 때문에 경로가 미리 있어야 한다.

   ```bash
   mkdir -p "$HOME/Backups/glhac-offsite" "$HOME/Library/LaunchAgents"
   chmod 700 "$HOME/Backups/glhac-offsite"
   cp /Users/dany/glhac-wt-residual/scripts/launchd/com.glhac.offsite-backup.plist.example \
     "$HOME/Library/LaunchAgents/com.glhac.offsite-backup.plist"
   ```

2. 복사한 plist의 `<HOST>`, `<KEY_PATH>`, `<REPO_PATH>`, `<LOCAL_DIR>`를 실제 값으로 바꾼다. `<REPO_PATH>` 예: `/Users/dany/glhac-wt-residual`, `<LOCAL_DIR>` 예: `/Users/dany/Backups/glhac-offsite`. XML 텍스트에서는 자리표시자가 `&lt;HOST&gt;`처럼 보인다. `~`, `$HOME`, `$LOCAL_DIR`는 plist에서 확장되지 않는다. 경로에 `&`가 있으면 `&amp;`로 쓴다. 키 권한은 0600을 권장한다.

   미러를 쓰려면 `EnvironmentVariables` 딕셔너리에 다음을 추가한다.

   ```xml
   <key>GLHAC_BACKUP_MIRROR_DIR</key>
   <string>/Volumes/MGTEC/GLHAC_Backups</string>
   ```

3. 문법을 확인하고 등록한다.

   ```bash
   plutil -lint "$HOME/Library/LaunchAgents/com.glhac.offsite-backup.plist"
   launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/com.glhac.offsite-backup.plist"
   launchctl print "gui/$(id -u)/com.glhac.offsite-backup"
   ```

사용자 로그인 세션에서 실행된다. `StartCalendarInterval`은 잠자기 중 놓친 실행을 깨어난 뒤 한 번 실행한다. 전원이 꺼져 있거나 로그아웃된 동안의 모든 일자를 소급 실행하는 기능은 아니다. 즉시 수동 점검할 때는 다음을 실행한다. 이 명령은 실제 서버에서 백업을 가져온다.

```bash
launchctl kickstart "gui/$(id -u)/com.glhac.offsite-backup"
tail -20 "$HOME/Backups/glhac-offsite/offsite-backup.log"
```

## 검증과 보관

- 원격 목록 중 정확한 파일명 패턴과 실제 달력 날짜가 맞는 일반 파일을 선택한다. 현재보다 1시간 넘게 미래인 파일은 파일명을 포함한 WARN·알림을 남기고 후보에서 제외한다. 남은 후보 중 파일명 시각이 가장 최신인 파일 하나를 받는다. 36시간을 넘으면 `서버 백업 정지` 경고 후 계속 수신하며, 후보가 하나도 없으면 ERROR·알림과 종료코드 1로 처리한다.
- 같은 이름·크기의 로컬 파일은 전송을 생략하지만 gzip, SQLite, 케이스 수, 기존 SHA-256을 다시 확인한다. 새 파일은 같은 로컬 파일시스템의 임시 파일로 받는다.
- `gzip -t`, 임시 DB의 `PRAGMA integrity_check` 결과 `ok`, `case_application` 1행 이상을 모두 통과한 뒤 `daily/`로 원자적으로 이동한다. 운영 DB의 WAL 모드에서 `.backup`한 파일은 헤더도 WAL일 수 있어, 압축 해제한 임시 복사본을 `-readonly` 없이 열고 필요한 WAL/SHM 파일을 만들 수 있게 한다. 압축된 원본은 검사 중 변경하지 않는다. 이어 `.db.gz.sha256`을 원자적으로 저장한다. 두 파일 전체가 하나의 트랜잭션은 아니다. 현재 선택된 이름의 기존 daily도 수신·교체 전에 SHA-256을 확인하며, 체크섬이 없거나 맞지 않으면 기존 파일을 보존하고 ERROR·알림과 종료코드 1로 중단한다. 체크섬을 자동으로 다시 만들어 오류를 숨기지 않는다.
- `daily/` 최신 14개, `weekly/` 일요일자 복사본 최신 8개, `monthly/` 월별 첫 확보 파일 최신 12개를 유지한다. 월초 실행을 놓쳤다면 그 달에 확보한 가장 이른 파일을 월별본으로 삼는다. 일요일 백업을 한 번도 받지 못한 주는 weekly가 생기지 않는다. 신규 daily 저장 후, 이전 daily 전체의 SHA-256을 실제 재계산해 각 `.sha256`과 비교한 다음 주간·월간 보관을 시작한다. 불일치·체크섬 누락·읽기 실패가 하나라도 있으면 ERROR·알림과 종료코드 1로 중단한다. 이 경우 신규 daily는 남고, 주간·월간 보관본 교체와 모든 보관 정리·미러는 실행하지 않는다.
- 로컬과 미러의 **각 tier 전체**를 보관 결정 전에 검증한다. 정리·월별 대표본 선택에 참여하는 모든 기존 파일의 SHA-256을 실제 재계산하며, 하나라도 불일치·체크섬 누락·읽기 실패가 있으면 해당 tier의 삭제를 전부 건너뛴다. 정상본만 개수에 세어 정리하지 않으며 보관 개수를 초과해도 그대로 둔다. 오류 파일 이름을 ERROR 로그·알림에 남기고 최종 종료코드는 1이다. 손상 파일과 체크섬은 삭제하거나 덮어쓰지 않는다. weekly/monthly 또는 미러의 오류가 있어도 충돌하지 않는 정상 신규 파일은 추가할 수 있다.
- 월별 대표본은 검증에 통과한 파일 중 가장 이른 파일로 선택한다. 손상 월별본은 대표본으로 취급하지 않으므로 다른 이름의 정상본을 새로 보관할 수 있다. monthly에 오류가 하나라도 있으면 다른 달을 포함한 월별 중복 정리와 12개 제한 정리를 모두 중단한다. 같은 이름의 손상본이 있으면 그대로 보존하며 자동 교체하지 않는다.
- 정리는 수정 시각이 아닌 파일명 순서로 한다. 정확한 백업 패턴의 일반 파일과 대응 `.sha256`만 삭제하며, 다른 파일과 심볼릭 링크는 정리 대상에서 제외한다.
- `/Volumes/...` 미러는 볼륨과 `/Volumes`의 장치 번호가 다른지 확인해 실제 마운트를 판별한 뒤 하위 경로를 만든다. 디렉터리 생성·파일 복사・교체·각 보관 정리 직전에 최초 장치 번호와 일치하는지 다시 확인한다. 장치 변경·분리를 발견하면 WARN·알림 후 미러를 중단하고 검증된 로컬 백업은 성공으로 보관한다. 그 외 미러 경로는 미리 만들어 둔다. 미러가 없으면 경고만 남기며 성공 처리한다.
- 미러의 `daily/weekly/monthly/`는 복사 전에 미러에만 남은 이력을 포함해 전체 SHA-256을 검증한다. 내용과 체크섬이 같은 파일은 복사를 생략한다. 새 파일이나 검증된 기존 파일과 내용이 달라진 파일은 같은 미러 볼륨의 `.offsite-mirror.*` 임시 디렉터리에 rsync하고 SHA-256을 확인한 뒤 rename으로 교체한다. 손상·체크섬 누락 파일은 교체 대상에서 제외한다. `--delete`나 `--ignore-existing`은 사용하지 않는다. 데이터·체크섬 한 쌍의 교체는 하나의 트랜잭션이 아니며, 중단으로 불일치가 남으면 다음 실행에서 ERROR로 보고하고 보존한다.
- **모든 tier의 복사가 성공한 뒤에만**, 오류가 없는 tier를 다시 검증하고 미러 자체의 파일명 날짜 순서로 daily 14개·weekly 8개·monthly 12개를 유지한다. 로컬에 없다는 이유로 미러 파일을 삭제하지 않으므로, 로컬 보관소가 초기화되어도 미러 이력은 각 보관 개수 안에서 남는다. 기존본 검증에 실패한 tier는 삭제하지 않는다. 미러 루트, 다른 파일, 중첩 디렉터리는 보존한다. 전송·복사본 체크섬 검증 실패 시 모든 미러 보관 정리를 중단하고 종료코드 1로 끝내며, 검증된 로컬 백업과 아직 교체하지 않은 기존 미러 파일은 남는다.

`offsite-backup.log`는 시각·결과(`OK/SKIP/WARN/ERROR`)·파일·바이트 수·케이스 수를 한 줄로 기록한다. 검증 전이거나 파일을 선택하지 못했다면 해당 값은 `-`다. 경고만 있는 실행에는 `WARN`과 최종 `OK/SKIP`가 함께 남는다. 성공·경고는 종료코드 0, 실패·중복 실행은 1이며, tier 검증 오류가 있으면 최종 `OK/SKIP`를 남기지 않는다. WARN/ERROR 알림을 먼저 시도하고 stdout/stderr·로그 파일 쓰기 실패를 별도로 격리한다. 고정 AppleScript를 `osascript -e` 인자로, 메시지를 별도 argv로 전달해 here-document나 알림용 임시 파일 없이 호출하므로 쓰기 불가 `TMPDIR`에서도 알림을 시도한다. 오류 기록 여부도 알림 시도 후에 설정한다. 최초 로그 파일 준비 실패도 ERROR·알림을 시도한 뒤 종료코드 1로 끝낸다. 알림 전송 자체의 실패는 백업 결과를 바꾸지 않는다. stdout/stderr는 별도 로그 파일이며, 로그 자동 순환은 하지 않는다.

임시 DB와 WAL/SHM, 전송 파일, 임시 SSH 래퍼는 정상 종료·오류·HUP/INT/TERM 시 trap으로 제거한다. 실행 중인 전송·감시 프로세스도 종료한다. 미러 임시 파일은 최초 장치임을 확인할 수 있을 때만 제거한다. 동시 실행은 `.offsite-backup.lock/`의 원자적 mkdir로 차단한다. SIGKILL·정전처럼 trap이 실행될 수 없는 중단 뒤에는 `pid`와 실제 프로세스를 확인하고, 실행 중인 백업이 없을 때만 남은 락·`.offsite-work.*`·`/tmp/glhac-offsite-ssh.*`를 수동 정리한다. 분리했던 미러를 다시 연결했을 때 남은 `.offsite-mirror.*`도 같은 방식으로 정리한다. 락을 시간만 보고 자동 탈취하지 않는다.

## 복구 리허설과 실제 복구

기본 경로의 최신 daily 또는 지정한 weekly/monthly/미러 파일을 점검한다. 환경변수는 수동 실행 셸에 별도로 지정해야 하며 LaunchAgent 설정에서 자동 상속되지 않는다.

```bash
TZ=Asia/Seoul /bin/bash scripts/offsite_backup_restore_check.sh
GLHAC_BACKUP_LOCAL_DIR='/다른/보관경로' TZ=Asia/Seoul \
  /bin/bash scripts/offsite_backup_restore_check.sh
/bin/bash scripts/offsite_backup_restore_check.sh \
  "$HOME/Backups/glhac-offsite/weekly/glhac_v3-YYYYmmdd-HHMMSS.db.gz"
```

리허설은 체크섬이 있으면 검증한 뒤 임시 DB에서 `integrity_check=ok`, `case_application`, `document_asset` 행 수를 출력한다. 수신 검사와 동일하게 임시 DB를 일반 모드로 열어 WAL 헤더 백업을 지원한다. 원본은 바꾸지 않고 임시 DB와 WAL/SHM은 종료 시 삭제한다. 실제 SQLite 무결성 및 행 수를 점검하므로, 월 1회 미러 파일로 실행하여 보관 매체까지 확인한다.

실제 운영 복구는 별도 작업으로 [운영 배포 가이드의 데이터 백업과 복구](운영_배포_가이드.md#6-데이터-백업과-복구)를 따른다. 검증한 외부 백업을 복구 입력으로 사용하고, 서비스 정지 → 현재 DB 별도 보존 → 압축 해제본 무결성 확인 → DB 교체 및 오래된 WAL/SHM 정리 → 권한 확인 → 서비스 기동·조회 확인 순서로 진행한다. 이 스크립트는 서버에 복구본을 쓰거나 서비스를 제어하지 않는다. DB 외 업로드 파일과 암호화 키는 이 백업 범위에 포함되지 않으므로 기존 보관 정책을 함께 유지한다.

## 되돌리기

```bash
launchctl bootout "gui/$(id -u)" "$HOME/Library/LaunchAgents/com.glhac.offsite-backup.plist"
rm "$HOME/Library/LaunchAgents/com.glhac.offsite-backup.plist"
```

등록 해제는 보관된 로컬·미러 백업을 삭제하지 않는다. 서버의 기존 백업 타이머에도 영향이 없다. plist 설정을 바꾼 경우에도 `bootout` 후 `bootstrap`으로 다시 등록한다.

## 개발 검증

macOS에서 실행한다. SSH와 원격 다운로드, osascript는 가짜 도구로 대체하며 실제 네트워크와 알림을 사용하지 않는다. 미러는 래퍼가 인자를 검사한 뒤 **실제 `/usr/bin/rsync`를 로컬 경로 사이에서만 실행**한다. SQLite fixture는 `PRAGMA journal_mode=WAL` 후 `.backup`한 파일을 압축하고 WAL 헤더를 확인한다. 로컬·미러의 모든 tier에서 손상·체크섬 누락 시 전체 삭제 중단, 미러 daily의 오래된 정상본 1개와 최신 손상본 13개 보존, 손상 월별본의 대표본 제외, 로그·TMPDIR 쓰기 실패 시 알림 시도를 회귀 검증한다. 월별 수신 테스트는 지난달 3일·6일을 사용하고 두 번째 수신도 확인한다. 미러 이력·보관 개수·교체와 부분 전송 오류, 미래 파일명, SSH/rsync 전체 시간 제한과 자식 프로세스 종료, 단계별 장치 변경도 확인한다. `--noconftest`로 공통 conftest의 import와 DB 초기화·종료 정리를 막는다. SQLite 검사·gzip·체크섬·BSD date는 실제 기본 도구로 확인하며 장치 변경 테스트에서만 stat 결과를 대체한다.

```bash
GLHAC_DEV=1 GLHAC_NOTIFY_WORKER=0 GLHAC_RATE_LIMIT_PER_MIN=0 \
  .venv/bin/python -m pytest -q -p no:cacheprovider --noconftest tests/test_offsite_backup.py
bash -n scripts/offsite_backup_pull.sh
bash -n scripts/offsite_backup_restore_check.sh
plutil -lint scripts/launchd/com.glhac.offsite-backup.plist.example
```
