#!/usr/bin/env bash
# 사용: bash deploy_prune_remote.sh ROOT CURRENT_TS [KEEP]
# ROOT는 운영에서 /, 테스트에서는 임시 디렉터리. KEEP 미지정 시 환경변수 또는 5.
# 최신 KEEP개와 CURRENT_TS, app/home의 mtime 최신도 보존하므로 KEEP개보다 많을 수 있다.
set -euo pipefail

ROOT="${1:?사용: deploy_prune_remote.sh ROOT CURRENT_TS [KEEP]}"
CURRENT_TS="${2-}"
KEEP="${3-${GLHAC_DEPLOY_KEEP-5}}"
if [[ ! "$KEEP" =~ ^([1-9]|[1-4][0-9]|50)$ ]]; then
  echo "  경고: GLHAC_DEPLOY_KEEP은 1~50 정수여야 합니다 — 백업 정리 생략" >&2
  exit 0
fi
if [[ ! "$CURRENT_TS" =~ ^[0-9]{14}$ ]]; then
  echo "  경고: 이번 배포 TS가 14자리 숫자가 아닙니다 — 백업 정리 생략" >&2
  exit 0
fi

# Python 표준 라이브러리로 공백·개행이 있는 이름도 분리 없이 처리한다.
if ! python3 - "$ROOT" "$CURRENT_TS" "$KEEP" <<'PY'
import os
from pathlib import Path
import re
import stat
import sys

root = Path(sys.argv[1])
current_ts = sys.argv[2]
keep = int(sys.argv[3])
groups = (
    ("DB deploy", "var/lib/glhac", r"glhac_v3\.db\.bak-deploy-([0-9]{14})"),
    ("DB predeploy", "var/lib/glhac", r"glhac_v3\.db\.bak-predeploy-[A-Za-z0-9._]+-([0-9]{14})"),
    ("app", "opt", r"glhac-app-bak-([0-9]{14})\.tgz"),
    ("home", "opt", r"glhac-home-bak-([0-9]{14})\.tgz"),
)


def warn(message):
    print("  경고: " + message, file=sys.stderr)


def check_directory(directory):
    # resolve()는 링크를 숨긴다. ROOT 위까지 원래 경로를 상위부터 검사한다.
    absolute = directory.absolute()
    for component in (*reversed(absolute.parents), absolute):
        try:
            mode = component.lstat().st_mode
        except FileNotFoundError:
            return
        if stat.S_ISLNK(mode):
            raise OSError("심볼릭 링크 경로 {} — 백업 정리 생략".format(repr(str(component))))


def backups(directory, pattern):
    found = []
    try:
        check_directory(directory)
        with os.scandir(directory) as entries:
            for entry in entries:
                match = pattern.fullmatch(entry.name)
                if match:
                    info = entry.stat(follow_symlinks=False)
                    if stat.S_ISREG(info.st_mode):
                        found.append((match.group(1), entry.name, info.st_mtime_ns))
    except FileNotFoundError:
        return []
    return sorted(found, reverse=True)


for label, relative_dir, expression in groups:
    directory = root / relative_dir
    pattern = re.compile(expression)
    deleted = 0
    try:
        candidates = backups(directory, pattern)
    except OSError as error:
        warn("{} 목록 조회 실패: {}".format(label, error))
        print("  {}: 삭제 0개 · 남음 확인 불가".format(label))
        continue

    newest_mtime = None
    if label in ("app", "home") and candidates:
        # rollback의 ls -t 첫 후보를 보호하며, mtime 동률이면 모두 보존한다.
        newest_mtime = max(mtime for _, _, mtime in candidates)

    for timestamp, name, mtime in candidates[keep:]:
        if timestamp == current_ts or mtime == newest_mtime:
            continue
        try:
            check_directory(directory)
        except OSError as error:
            warn("{} 경로 확인 실패: {}".format(label, error))
            break
        path = directory / name
        try:
            # 목록을 읽은 뒤 다른 타입으로 바뀌었으면 삭제하지 않는다.
            if stat.S_ISREG(path.lstat().st_mode):
                path.unlink()
                deleted += 1
        except FileNotFoundError:
            continue
        except OSError as error:
            warn("{} 삭제 실패: {}".format(repr(str(path)), error))

    try:
        remaining = "{}개".format(len(backups(directory, pattern)))
    except OSError as error:
        warn("{} 남은 개수 확인 실패: {}".format(label, error))
        remaining = "확인 불가"
    print("  {}: 삭제 {}개 · 남음 {}".format(label, deleted, remaining))
PY
then
  echo "  경고: 배포 백업 정리 실행에 실패했습니다 — 배포 성공은 유지합니다" >&2
fi
