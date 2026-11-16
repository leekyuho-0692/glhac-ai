#!/bin/bash
set -euo pipefail

if [[ $# -lt 1 || $# -gt 2 || -z "${1:-}" || ( $# -eq 2 && -z "$2" ) ]]; then
    printf 'Usage: %s <output-path> [source-script]\n' "$0" >&2
    exit 2
fi

source_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
source_script=${2:-"$source_dir/../offsite_backup_pull.sh"}
out=$1
case "$out" in
    /*) ;;
    *) out=$PWD/$out ;;
esac

if [[ ! -f "$source_script" || ! -r "$source_script" ]]; then
    printf '백업 스크립트를 읽을 수 없습니다: %s\n' "$source_script" >&2
    exit 2
fi

umask 077
build_dir=$(mktemp -d "${TMPDIR:-/tmp}/glhac-launcher.XXXXXX")
trap 'rm -rf -- "$build_dir"' EXIT
trap 'exit 1' HUP INT TERM
# Hash and embed the same private snapshot, even if the source later changes.
cp -- "$source_script" "$build_dir/script.sh"
script_size=$(wc -c < "$build_dir/script.sh")
if (( script_size == 0 || script_size > 65536 )); then
    printf '스크립트 크기는 1~65536 bytes여야 합니다: %s\n' "$script_size" >&2
    exit 2
fi

{
    printf 'static const unsigned char glhac_offsite_script[] = {\n'
    LC_ALL=C od -An -v -t u1 "$build_dir/script.sh" | LC_ALL=C awk '
        {
            for (i = 1; i <= NF; i++) {
                if ($i == 0) invalid = 1
                printf "0x%02x,", $i
            }
            printf "\n"
        }
        END {
            if (invalid) {
                print "스크립트에 NUL 바이트가 있습니다" > "/dev/stderr"
                exit 2
            }
            print "0x00\n};"
        }
    '
} > "$build_dir/glhac_offsite_script.h"
script_sha256=$(shasum -a 256 < "$build_dir/script.sh")
script_sha256=${script_sha256%% *}

printf '%s\n' '경고: 런처를 재빌드하면 TCC 전체 디스크 접근 권한을 다시 부여해야 합니다.' >&2
TMPDIR="$build_dir" cc -O2 -Wall -Wextra -I "$build_dir" \
    -o "$build_dir/launcher" "$source_dir/glhac_offsite_launcher.c"
codesign --force --sign - --options runtime \
    --identifier com.glhac.offsite-backup.launcher "$build_dir/launcher"
signature=$(codesign -dv "$build_dir/launcher" 2>&1)
printf '%s\n' "$signature"
# Ad-hoc signatures can report 0x10002(adhoc,runtime); require the runtime bit
# and its named flag, not an exact flags string that rejects this valid form.
signature_flags=$(printf '%s\n' "$signature" | sed -nE \
    's/^CodeDirectory .* flags=(0x[[:xdigit:]]+)\(([^,()]+,)*runtime(,[^,()]+)*\).*/\1/p')
if [[ -z "$signature_flags" ]] || (( (signature_flags & 0x10000) == 0 )); then
    printf '%s\n' 'Hardened Runtime 서명 플래그가 없습니다; 빌드를 중단합니다.' >&2
    exit 1
fi
codesign --verify --strict "$build_dir/launcher"
# Only publish a successfully signed build. No entitlements are added.
install -m 700 "$build_dir/launcher" "$out"
printf '%s\n' "$script_sha256" > "$out.sha256"
printf '빌드 및 Hardened Runtime 애드혹 서명 완료: %s\n' "$out"
printf '내장 스크립트: %s bytes, SHA-256: %s\n' "$((script_size))" "$script_sha256"
printf '스크립트 SHA-256 파일: %s.sha256\n' "$out"
