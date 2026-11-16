#!/bin/bash
# Local rehearsal only: never opens the application DB or connects to a server.
set -euo pipefail
umask 077
export LC_ALL=C
shopt -s nullglob

work_dir=
cleanup() {
    local status=$?
    trap - EXIT
    [[ -z "$work_dir" ]] || rm -rf -- "$work_dir"
    if [[ "$status" != 0 ]]; then exit 1; fi
}
trap cleanup EXIT
trap 'exit 1' HUP INT TERM
fail() { printf '복구 점검 실패: %s\n' "$1" >&2; exit 1; }

[[ $# -le 1 ]] || fail '사용법: offsite_backup_restore_check.sh [백업.db.gz]'
backup=${1:-}
if [[ -z "$backup" ]]; then
    local_dir=${GLHAC_BACKUP_LOCAL_DIR:-"$HOME/Backups/glhac-offsite"}
    name_re='^glhac_v3-[0-9]{8}-[0-9]{6}\.db\.gz$'
    for path in "$local_dir/daily"/glhac_v3-*.db.gz; do
        name=${path##*/}
        [[ -f "$path" && ! -L "$path" && "$name" =~ $name_re ]] || continue
        stamp=${name#glhac_v3-}
        stamp=${stamp%.db.gz}
        parsed=$(date -j -f '%Y%m%d-%H%M%S' "$stamp" '+%Y%m%d-%H%M%S' 2>/dev/null) || continue
        [[ "$parsed" == "$stamp" ]] || continue
        backup=$path
    done
fi
[[ -n "$backup" && -f "$backup" && ! -L "$backup" ]] || fail '점검할 로컬 백업이 없습니다'
[[ "$backup" == /* ]] || backup=$PWD/$backup
if [[ -f "$backup.sha256" ]]; then
    digest_line=$(shasum -a 256 < "$backup")
    digest=${digest_line%% *}
    [[ "$(cat "$backup.sha256")" == "$digest  ${backup##*/}" ]] || fail 'SHA-256 불일치'
fi
work_dir=$(mktemp -d "${TMPDIR:-/tmp}/glhac-restore-check.XXXXXX")
gzip -t "$backup" 2>/dev/null || fail 'gzip 검증 실패'
gzip -dc "$backup" > "$work_dir/check.db" 2>/dev/null || fail '압축 해제 실패'
# WAL-header backups need writable sidecars; only the temporary copy is opened.
sqlite_command=(/usr/bin/sqlite3 -init /dev/null -batch -bail -noheader "$work_dir/check.db")
integrity=$("${sqlite_command[@]}" 'PRAGMA integrity_check;' 2> "$work_dir/sqlite.err") || fail 'SQLite integrity_check 실패'
[[ "$integrity" == ok ]] || fail 'SQLite integrity_check 결과가 ok가 아닙니다'
cases=$("${sqlite_command[@]}" 'SELECT COUNT(*) FROM case_application;' 2> "$work_dir/sqlite.err") || fail 'case_application 조회 실패'
documents=$("${sqlite_command[@]}" 'SELECT COUNT(*) FROM document_asset;' 2> "$work_dir/sqlite.err") || fail 'document_asset 조회 실패'
printf 'backup=%s\nintegrity_check=ok\ncase_application=%s\ndocument_asset=%s\n' "$backup" "$cases" "$documents"
