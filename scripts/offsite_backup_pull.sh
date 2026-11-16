#!/bin/bash
# macOS / Bash 3.2. The server is read-only: ls and an rsync sender only.
set -euo pipefail
umask 077
export LC_ALL=C
shopt -s nullglob

backup_host=${GLHAC_BACKUP_HOST:-}
backup_user=${GLHAC_BACKUP_USER:-rocky}
backup_key=${GLHAC_BACKUP_KEY:-}
remote_dir=${GLHAC_BACKUP_REMOTE_DIR:-/var/backups/glhac}
local_dir=${GLHAC_BACKUP_LOCAL_DIR:-"$HOME/Backups/glhac-offsite"}
mirror_dir=${GLHAC_BACKUP_MIRROR_DIR:-}
max_age=${GLHAC_BACKUP_MAX_AGE_HOURS:-36}
ssh_timeout=${GLHAC_BACKUP_SSH_TIMEOUT_SECONDS:-60}
rsync_timeout=${GLHAC_BACKUP_RSYNC_TIMEOUT_SECONDS:-1800}
notify_enabled=${GLHAC_BACKUP_NOTIFY:-1}
name_re='^glhac_v3-[0-9]{8}-[0-9]{6}\.db\.gz$'
filename=-
file_size=-
case_count=-
work_dir=
transport_dir=
lock_owned=0
error_logged=0
retention_failed=0
checksum_failures=()
log_file=
command_pid=
watchdog_pid=
timed_out=0
mirror_work=
mirror_volume=
mirror_device=

log_event() {
    local level=$1 message=$2 line msg
    # Notification must still be attempted when stdout or the log disk fails.
    if [[ "$notify_enabled" == 1 && ( "$level" == WARN || "$level" == ERROR ) ]]; then
        # Pass text as argv, never as executable AppleScript source.
        msg="$message ($filename)"
        osascript -e 'on run argv' \
            -e 'display notification (item 1 of argv) with title "GL-HAC 외부 백업"' \
            -e 'end run' "$msg" >/dev/null 2>&1 || :
    fi
    # Record errors only AFTER attempting notification, including tier errors
    # that allow safe additions to continue but must make the run fail.
    [[ "$level" != ERROR ]] || error_logged=1
    line="$(date '+%Y-%m-%dT%H:%M:%S%z' || :) result=$level file=$filename bytes=$file_size cases=$case_count message=$message"
    printf '%s\n' "$line" || :
    if [[ -n "$log_file" ]]; then
        { printf '%s\n' "$line" >> "$log_file"; } 2>/dev/null || :
    fi
    return 0
}

fail() {
    log_event ERROR "$1" >&2
    exit 1
}

stop_timed_processes() {
    if [[ -n "$watchdog_pid" ]]; then
        kill -TERM -- "-$watchdog_pid" 2>/dev/null || :
        wait "$watchdog_pid" 2>/dev/null || :
        watchdog_pid=
    fi
    if [[ -n "$command_pid" ]]; then
        kill -TERM -- "-$command_pid" 2>/dev/null || :
        kill -KILL -- "-$command_pid" 2>/dev/null || :
        wait "$command_pid" 2>/dev/null || :
        command_pid=
    fi
}

# macOS has no timeout command. Bash job control gives each background job a
# process group, allowing cleanup to stop rsync AND its SSH descendants.
run_with_timeout() {
    local seconds=$1 status=0 marker=$work_dir/timed-out
    shift
    timed_out=0
    rm -f -- "$marker" || return 1
    set -m
    "$@" &
    command_pid=$!
    (
        trap - EXIT HUP INT TERM
        sleep "$seconds"
        if kill -0 -- "-$command_pid" 2>/dev/null; then
            : > "$marker" || :
            kill -TERM -- "-$command_pid" 2>/dev/null || :
            sleep 1
            kill -KILL -- "-$command_pid" 2>/dev/null || :
        fi
    ) &
    watchdog_pid=$!
    set +m
    wait "$command_pid" 2>/dev/null || status=$?
    if [[ -f "$marker" ]]; then
        timed_out=1
        wait "$watchdog_pid" 2>/dev/null || :
        watchdog_pid=
        status=124
    fi
    stop_timed_processes
    return "$status"
}

cleanup() {
    local status=$?
    trap - EXIT
    set +e
    stop_timed_processes
    # Do not remove a path on a replacement volume after an unplug/remount.
    if [[ -n "$mirror_work" && "$(stat -L -f %d "$mirror_volume" 2>/dev/null)" == "$mirror_device" && "$(stat -L -f %d "$mirror_work" 2>/dev/null)" == "$mirror_device" ]]; then
        rm -rf -- "$mirror_work"
    fi
    [[ -z "$work_dir" ]] || rm -rf -- "$work_dir"
    [[ -z "$transport_dir" ]] || rm -rf -- "$transport_dir"
    if [[ "$lock_owned" == 1 ]]; then
        rm -f -- "$local_dir/.offsite-backup.lock/pid"
        rmdir -- "$local_dir/.offsite-backup.lock"
    fi
    if [[ "$status" != 0 ]]; then
        if [[ "$error_logged" == 0 ]]; then
            log_event ERROR '백업 처리 실패 또는 실행 중단' >&2
        fi
        exit 1
    fi
}
trap cleanup EXIT
trap 'exit 1' HUP INT TERM

[[ "$local_dir" == /* && "$local_dir" != / && "$local_dir" != *$'\n'* && "$local_dir" != *$'\r'* ]] || fail 'LOCAL_DIR은 루트 이외의 절대경로여야 합니다'
mkdir -p -- "$local_dir"
local_dir=$(cd "$local_dir" && pwd -P)
[[ "$local_dir" != / ]] || fail 'LOCAL_DIR에 루트 디렉터리를 사용할 수 없습니다'
[[ ! -L "$local_dir/offsite-backup.log" ]] || fail '로그 파일 심볼릭 링크는 사용할 수 없습니다'
log_file=$local_dir/offsite-backup.log
[[ ! -e "$log_file" || -f "$log_file" ]] || fail '로그 파일 경로가 일반 파일이 아닙니다'
touch "$log_file" || fail '로그 파일을 준비할 수 없습니다'

# ssh concatenates remote argv into a shell command. Allow only literal safe
# tokens BEFORE passing argv; never interpolate arbitrary input in a command.
[[ "$backup_host" =~ ^[A-Za-z0-9][A-Za-z0-9.-]*$ ]] || fail 'HOST에 IPv4 주소 또는 호스트명이 필요합니다'
[[ "$backup_user" =~ ^[A-Za-z_][A-Za-z0-9_-]*$ ]] || fail 'USER 형식이 잘못되었습니다'
[[ "$backup_key" == /* && -f "$backup_key" && -r "$backup_key" ]] || fail 'KEY에 읽을 수 있는 키 파일의 절대경로가 필요합니다'
[[ "$remote_dir" =~ ^/[A-Za-z0-9_./-]+$ ]] || fail 'REMOTE_DIR에 허용되지 않은 문자가 있습니다'
remote_dir=${remote_dir%/}
case "$remote_dir/" in
    /|*//*|*/../*|*/./*) fail 'REMOTE_DIR에 정규화된 절대경로가 필요합니다' ;;
esac
[[ "$max_age" =~ ^[1-9][0-9]{0,5}$ ]] || fail 'MAX_AGE_HOURS는 1~999999 정수여야 합니다'
[[ "$ssh_timeout" =~ ^[1-9][0-9]{0,5}$ ]] || fail 'SSH_TIMEOUT_SECONDS는 1~999999 정수여야 합니다'
[[ "$rsync_timeout" =~ ^[1-9][0-9]{0,5}$ ]] || fail 'RSYNC_TIMEOUT_SECONDS는 1~999999 정수여야 합니다'
[[ "$notify_enabled" == 0 || "$notify_enabled" == 1 ]] || fail 'NOTIFY는 0 또는 1이어야 합니다'
[[ -z "$mirror_dir" || ( "$mirror_dir" == /* && "$mirror_dir" != *$'\n'* && "$mirror_dir" != *$'\r'* ) ]] || fail 'MIRROR_DIR은 절대경로여야 합니다'

if ! mkdir -- "$local_dir/.offsite-backup.lock" 2>/dev/null; then
    fail '다른 백업이 실행 중이거나 이전 실행의 락이 남아 있습니다'
fi
lock_owned=1
printf '%s\n' "$$" > "$local_dir/.offsite-backup.lock/pid"
for tier in daily weekly monthly; do
    [[ ! -L "$local_dir/$tier" ]] || fail '보관 디렉터리 심볼릭 링크는 사용할 수 없습니다'
    mkdir -p -- "$local_dir/$tier"
done
work_dir=$(mktemp -d "$local_dir/.offsite-work.XXXXXX")

# BSD date can silently normalize impossible dates (e.g. February 30).
# Round-trip the timestamp as well as checking the filename pattern.
parse_name() {
    local stamp roundtrip
    [[ "$1" =~ $name_re ]] || return 1
    stamp=${1#glhac_v3-}
    stamp=${stamp%.db.gz}
    date_parts=$(date -j -f '%Y%m%d-%H%M%S' "$stamp" '+%Y%m%d-%H%M%S %s %w' 2>/dev/null) || return 1
    roundtrip=${date_parts%% *}
    [[ "$roundtrip" == "$stamp" ]]
}

verify_checksum() {
    local path=$1 name=${1##*/} actual stored
    [[ -f "$path" && ! -L "$path" && -f "$path.sha256" && ! -L "$path.sha256" ]] || return 1
    actual=$(shasum -a 256 < "$path") || return 1
    stored=$(cat "$path.sha256") || return 1
    [[ "$stored" == "${actual%% *}  $name" ]]
}

ssh_command=(ssh -F /dev/null -i "$backup_key"
    -o BatchMode=yes -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes
    -o ConnectTimeout=15 -o ConnectionAttempts=1
    -o ServerAliveInterval=15 -o ServerAliveCountMax=3)
# Rocky's GNU ls uses one numeric mtime field, independent of locale/TIME_STYLE.
if ! run_with_timeout "$ssh_timeout" "${ssh_command[@]}" -- "$backup_user@$backup_host" ls -ln --time-style=+%s -- "$remote_dir/" > "$work_dir/listing" 2> "$work_dir/ssh.err"; then
    [[ "$timed_out" == 0 ]] || fail "원격 목록 조회 시간 제한 초과 (${ssh_timeout}초)"
    fail '원격 목록 조회 실패'
fi

latest=
latest_size=
latest_epoch=
now=$(date +%s)
while read -r mode links owner group size modified candidate; do
    [[ "$mode" == -* && "$size" =~ ^[1-9][0-9]{0,17}$ ]] || continue
    parse_name "$candidate" || continue
    read -r stamp epoch weekday <<< "$date_parts"
    if (( epoch > now + 3600 )); then
        log_event WARN "미래 시각 백업 제외 (현재보다 1시간 초과): $candidate"
        continue
    fi
    if [[ -z "$latest" || "$candidate" > "$latest" ]]; then
        latest=$candidate
        latest_size=$size
        latest_epoch=$epoch
    fi
done < "$work_dir/listing"
[[ -n "$latest" ]] || fail '유효한 서버 백업 파일이 없습니다'
filename=$latest
file_size=$latest_size
if (( now - latest_epoch > max_age * 3600 )); then
    log_event WARN '서버 백업 정지: 최신 파일이 MAX_AGE_HOURS보다 오래되었습니다'
fi

destination=$local_dir/daily/$filename
[[ ! -L "$destination" && ! -L "$destination.sha256" && ! -d "$destination" && ! -d "$destination.sha256" ]] || fail '백업 파일 경로가 안전하지 않습니다'
# Do not replace or silently supply a checksum for an uncertain existing file.
if [[ -f "$destination" ]]; then
    verify_checksum "$destination" || fail "기존 daily SHA-256 검증 실패: $filename; 기존 파일 보존, 정리 중단"
fi
downloaded=1
if [[ -f "$destination" && "$(stat -f %z "$destination")" == "$file_size" ]]; then
    downloaded=0
    compressed=$destination
else
    # rsync parses -e itself, so keep its executable path free of spaces and
    # quote SSH argv with Bash %q inside a private, temporary transport script.
    # This supports spaces/quotes in key paths without eval or shell injection.
    transport_dir=$(mktemp -d /tmp/glhac-offsite-ssh.XXXXXX)
    {
        printf '#!/bin/bash\nset -euo pipefail\nexec'
        printf ' %q' "${ssh_command[@]}"
        printf ' "$@"\n'
    } > "$transport_dir/ssh"
    chmod 700 "$transport_dir/ssh"
    compressed=$work_dir/download.db.gz
    if ! run_with_timeout "$rsync_timeout" rsync -t --timeout=120 -e "$transport_dir/ssh" -- "$backup_user@$backup_host:$remote_dir/$filename" "$compressed" > "$work_dir/rsync.out" 2> "$work_dir/rsync.err"; then
        [[ "$timed_out" == 0 ]] || fail "백업 다운로드 시간 제한 초과 (${rsync_timeout}초)"
        fail '백업 다운로드 실패'
    fi
fi
[[ -f "$compressed" && "$(stat -f %z "$compressed")" == "$file_size" ]] || fail '백업 파일 크기 불일치'
gzip -t "$compressed" 2> "$work_dir/gzip.err" || fail 'gzip 검증 실패'
gzip -dc "$compressed" > "$work_dir/check.db" 2> "$work_dir/gzip.err" || fail '압축 해제 실패'
# WAL-header .backup files may need temporary -wal/-shm files even for reads.
# Only this disposable, decompressed copy is opened in normal mode.
sqlite_command=(/usr/bin/sqlite3 -init /dev/null -batch -bail -noheader "$work_dir/check.db")
integrity=$("${sqlite_command[@]}" 'PRAGMA integrity_check;' 2> "$work_dir/sqlite.err") || fail 'SQLite integrity_check 실패'
[[ "$integrity" == ok ]] || fail 'SQLite integrity_check 결과가 ok가 아닙니다'
case_count=$("${sqlite_command[@]}" 'SELECT COUNT(*) FROM case_application;' 2> "$work_dir/sqlite.err") || fail 'case_application 조회 실패'
[[ "$case_count" =~ ^[0-9]+$ && "$case_count" -ge 1 ]] || fail 'case_application 행 수가 1 미만입니다'
rm -f -- "$work_dir/check.db"

digest_line=$(shasum -a 256 < "$compressed")
digest=${digest_line%% *}
checksum="$digest  $filename"
if [[ "$downloaded" == 0 && -f "$destination.sha256" ]]; then
    [[ "$(cat "$destination.sha256")" == "$checksum" ]] || fail '기존 백업 SHA-256 불일치'
fi
printf '%s\n' "$checksum" > "$work_dir/checksum"
if [[ "$downloaded" == 1 ]]; then
    mv -f -- "$compressed" "$destination"
fi
mv -f -- "$work_dir/checksum" "$destination.sha256"

file_has_checksum_failure() {
    local path
    # Bash 3.2 treats an empty array expansion as unset under set -u.
    [[ "${#checksum_failures[@]}" -gt 0 ]] || return 1
    for path in "${checksum_failures[@]}"; do
        [[ "$path" != "$1" ]] || return 0
    done
    return 1
}

tier_has_checksum_failure() {
    local path
    [[ "${#checksum_failures[@]}" -gt 0 ]] || return 1
    for path in "${checksum_failures[@]}"; do
        [[ "${path%/*}" != "$1" ]] || return 0
    done
    return 1
}

verify_tier() {
    local directory=$1 label=$2 path invalid=0
    # Scan the WHOLE tier before any retention decision. Filtering bad files
    # out of the count would still allow good history to be deleted.
    for path in "$directory"/glhac_v3-*.db.gz; do
        [[ -f "$path" && ! -L "$path" ]] || continue
        parse_name "${path##*/}" || continue
        if ! verify_checksum "$path"; then
            checksum_failures[${#checksum_failures[@]}]=$path
            retention_failed=1
            invalid=1
            log_event ERROR "$label SHA-256 검증 실패: ${path##*/}; 손상 파일 보존, 해당 tier 정리 중단" >&2
        fi
    done
    return "$invalid"
}

# Preflight ALL previous daily files before changing any archive or pruning.
# A failure preserves the newly received daily and every existing archive.
verify_tier "$local_dir/daily" '이전 daily' || exit 1
verify_tier "$local_dir/weekly" '이전 weekly' || :
verify_tier "$local_dir/monthly" '이전 monthly' || :

copy_archive() {
    local source=$1 target=$2
    [[ ! -L "$target" && ! -L "$target.sha256" && ! -d "$target" && ! -d "$target.sha256" ]] || fail '보관 파일 경로가 안전하지 않습니다'
    file_has_checksum_failure "$target" && return 0
    if [[ -f "$target" && -f "$target.sha256" ]] && cmp -s "$source.sha256" "$target.sha256" && cmp -s "$source" "$target"; then
        return
    fi
    cp -- "$source" "$work_dir/archive.db.gz"
    cp -- "$source.sha256" "$work_dir/archive.sha256"
    mv -f -- "$work_dir/archive.db.gz" "$target"
    mv -f -- "$work_dir/archive.sha256" "$target.sha256"
}

# Archive before pruning daily. A missed 1st uses the earliest successfully
# received file for that month. Repeated runs never replace it with a later day.
for source in "$local_dir/daily"/glhac_v3-*.db.gz; do
    name=${source##*/}
    [[ -f "$source" && ! -L "$source" && -f "$source.sha256" && ! -L "$source.sha256" ]] || continue
    parse_name "$name" || continue
    read -r stamp epoch weekday <<< "$date_parts"
    if [[ "$weekday" == 0 ]]; then
        copy_archive "$source" "$local_dir/weekly/$name"
    fi
    month_prefix=${name:0:15}
    first_in_month=$name
    for existing in "$local_dir/monthly/$month_prefix"*.db.gz; do
        old_name=${existing##*/}
        [[ -f "$existing" && ! -L "$existing" ]] || continue
        parse_name "$old_name" || continue
        file_has_checksum_failure "$existing" && continue
        if [[ "$old_name" < "$first_in_month" ]]; then
            first_in_month=$old_name
        fi
    done
    if [[ "$first_in_month" == "$name" ]]; then
        copy_archive "$source" "$local_dir/monthly/$name"
    fi
    # A bad file in ANY month blocks every deletion in the monthly tier.
    tier_has_checksum_failure "$local_dir/monthly" && continue
    for existing in "$local_dir/monthly/$month_prefix"*.db.gz; do
        old_name=${existing##*/}
        [[ -f "$existing" && ! -L "$existing" ]] || continue
        parse_name "$old_name" || continue
        if [[ "$old_name" != "$first_in_month" ]]; then
            rm -f -- "$existing" "$existing.sha256"
        fi
    done
done

prune() {
    local directory=$1 keep=$2 label=$3 path name count=0 index
    local files=()
    tier_has_checksum_failure "$directory" && return 0
    verify_tier "$directory" "$label" || return 0
    # LC_ALL=C glob order is filename/timestamp order, independent of mtime.
    for path in "$directory"/glhac_v3-*.db.gz; do
        name=${path##*/}
        [[ -f "$path" && ! -L "$path" ]] || continue
        parse_name "$name" || continue
        files[count]=$path
        count=$((count + 1))
    done
    for ((index=0; index<count-keep; index++)); do
        rm -f -- "${files[index]}" "${files[index]}.sha256"
    done
}
prune "$local_dir/daily" 14 '로컬 daily'
prune "$local_dir/weekly" 8 '로컬 weekly'
prune "$local_dir/monthly" 12 '로컬 monthly'

mirror_device_matches() {
    local path=$1 current
    current=$(stat -L -f %d "$mirror_volume" 2>/dev/null) || current=
    if [[ ! -d "$mirror_volume" || -L "$mirror_volume" || "$current" != "$mirror_device" ]]; then
        log_event WARN '미러 장치 변경 또는 분리: 미러 중단, 로컬 백업만 보관합니다'
        return 1
    fi
    # Before mkdir, check the closest existing ancestor as well. This also
    # catches a nested mount or a symlink leading to another filesystem.
    while [[ ! -e "$path" && ! -L "$path" ]]; do
        path=${path%/*}
        [[ -n "$path" ]] || path=/
    done
    current=$(stat -L -f %d "$path" 2>/dev/null) || current=
    if [[ "$current" != "$mirror_device" ]]; then
        log_event WARN '미러 장치 변경 또는 분리: 미러 중단, 로컬 백업만 보관합니다'
        return 1
    fi
}

mirror_backups() {
    local volume remaining device parent_device physical tier source name target copy_status keep
    [[ -n "$mirror_dir" ]] || return 0
    mirror_dir=${mirror_dir%/}
    if [[ "$mirror_dir" == /Volumes/* ]]; then
        remaining=${mirror_dir#/Volumes/}
        volume=/Volumes/${remaining%%/*}
        # An unmounted volume can leave an ordinary directory behind. A
        # filesystem boundary is required before creating anything beneath it.
        device=$(stat -L -f %d "$volume" 2>/dev/null) || device=
        parent_device=$(stat -L -f %d /Volumes 2>/dev/null) || parent_device=
        if [[ ! -d "$volume" || -L "$volume" || -z "$device" || "$device" == "$parent_device" ]]; then
            log_event WARN '미러 볼륨 미마운트: 로컬 백업만 보관합니다'
            return 0
        fi
    elif [[ ! -d "$mirror_dir" ]]; then
        log_event WARN '미러 디렉터리 없음: 로컬 백업만 보관합니다'
        return 0
    else
        volume=$(cd "$mirror_dir" && pwd -P) || fail '미러 경로 확인 실패'
        device=$(stat -L -f %d "$volume") || fail '미러 장치 확인 실패'
    fi
    mirror_volume=$volume
    mirror_device=$device
    mirror_device_matches "$mirror_dir" || return 0
    mkdir -p -- "$mirror_dir" || fail '미러 디렉터리 생성 실패: 로컬 백업은 보관되었습니다'
    physical=$(cd "$mirror_dir" && pwd -P) || fail '미러 경로 확인 실패'
    [[ "$physical" != / ]] || fail 'MIRROR_DIR에 루트 디렉터리를 사용할 수 없습니다'
    case "$physical/" in "$local_dir/"*) fail '미러와 로컬 보관 경로가 겹칩니다' ;; esac
    case "$local_dir/" in "$physical/"*) fail '미러와 로컬 보관 경로가 겹칩니다' ;; esac
    mirror_device_matches "$physical" || return 0
    mirror_work=$(mktemp -d "$physical/.offsite-mirror.XXXXXX") || fail '미러 임시 디렉터리 생성 실패'
    for tier in daily weekly monthly; do
        [[ ! -L "$physical/$tier" ]] || fail '미러 하위 디렉터리 심볼릭 링크는 사용할 수 없습니다'
        mirror_device_matches "$physical/$tier" || return 0
        mkdir -p -- "$physical/$tier" || fail '미러 보관 디렉터리 생성 실패'
        verify_tier "$physical/$tier" "미러 $tier" || :
        mirror_device_matches "$physical/$tier" || return 0
        for source in "$local_dir/$tier"/glhac_v3-*.db.gz; do
            name=${source##*/}
            [[ -f "$source" && ! -L "$source" ]] || continue
            parse_name "$name" || continue
            file_has_checksum_failure "$source" && continue
            verify_checksum "$source" || fail "미러 원본 SHA-256 검증 실패: $tier/$name"
            target=$physical/$tier/$name
            mirror_device_matches "$physical/$tier" || return 0
            [[ ! -L "$target" && ! -L "$target.sha256" && ! -d "$target" && ! -d "$target.sha256" ]] || fail '미러 파일 경로가 안전하지 않습니다'
            file_has_checksum_failure "$target" && continue
            if [[ -f "$target" && -f "$target.sha256" ]] && cmp -s "$source" "$target" && cmp -s "$source.sha256" "$target.sha256"; then
                continue
            fi
            # Copy to the same volume under a private temporary name. Even an
            # interrupted overwrite leaves the old mirror file intact.
            mirror_device_matches "$mirror_work" || return 0
            copy_status=0
            run_with_timeout "$rsync_timeout" rsync -t --checksum --timeout=120 -- "$source" "$source.sha256" "$mirror_work/" > "$work_dir/mirror.out" 2> "$work_dir/mirror.err" || copy_status=$?
            mirror_device_matches "$mirror_work" || return 0
            if [[ "$copy_status" != 0 ]]; then
                [[ "$timed_out" == 0 ]] || fail "미러 복사 시간 제한 초과 (${rsync_timeout}초): 로컬 백업은 보관되었습니다"
                fail '미러 복사 실패: 기존 미러 정리 중단, 로컬 백업은 보관되었습니다'
            fi
            verify_checksum "$mirror_work/$name" || fail "미러 복사본 SHA-256 검증 실패: $tier/$name"
            mirror_device_matches "$physical/$tier" || return 0
            mv -f -- "$mirror_work/$name" "$target"
            mv -f -- "$mirror_work/$name.sha256" "$target.sha256"
        done
    done
    # All tiers must finish copying before ANY deletion. Mirror-only history
    # participates in the same filename-based retention, never rsync --delete.
    for tier in daily weekly monthly; do
        case "$tier" in daily) keep=14 ;; weekly) keep=8 ;; monthly) keep=12 ;; esac
        [[ ! -L "$physical/$tier" ]] || fail '미러 하위 디렉터리 심볼릭 링크는 사용할 수 없습니다'
        mirror_device_matches "$physical/$tier" || return 0
        prune "$physical/$tier" "$keep" "미러 $tier"
    done
}
mirror_backups
[[ "$retention_failed" == 0 ]] || exit 1
if [[ "$downloaded" == 1 ]]; then
    log_event OK '다운로드·검증·보관 완료'
else
    log_event SKIP '같은 이름·크기: 다운로드 생략, 재검증·보관 완료'
fi
