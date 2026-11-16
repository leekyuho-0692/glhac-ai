"""배포 백업 보관: 임시 파일과 정적 흐름 검사만 사용하며 운영에는 접속하지 않는다."""
import os
from pathlib import Path
import re
import subprocess

import pytest


REPO = Path(__file__).resolve().parents[1]
DEPLOY = REPO / "scripts/deploy.sh"
PRUNE = REPO / "scripts/deploy_prune_remote.sh"
CURRENT_TS = "20261231000000"
KINDS = ("DB deploy", "DB predeploy", "app", "home")


@pytest.fixture
def remote_root(tmp_path):
    root = tmp_path / "remote root"
    (root / "var/lib/glhac").mkdir(parents=True)
    (root / "opt").mkdir()
    return root


def backup_path(root, kind, timestamp, tag="sha"):
    if kind == "DB deploy":
        return root / "var/lib/glhac" / f"glhac_v3.db.bak-deploy-{timestamp}"
    if kind == "DB predeploy":
        return root / "var/lib/glhac" / f"glhac_v3.db.bak-predeploy-{tag}-{timestamp}"
    return root / "opt" / f"glhac-{kind}-bak-{timestamp}.tgz"


def populate(root, count):
    paths = {kind: [] for kind in KINDS}
    for index in range(count):
        for kind in KINDS:
            # predeploy 태그 사전순과 모든 종류의 mtime을 TS 순서의 반대로 만든다.
            path = backup_path(root, kind, f"20261116{index:06d}", f"{count - index:08x}")
            path.write_text(f"backup {index}")
            os.utime(path, (10_000 - index, 10_000 - index))
            paths[kind].append(path)
    return paths


def run_prune(root, keep=None, *, current_ts=CURRENT_TS, env_keep=None, extra_env=None):
    env = os.environ.copy()
    env.pop("GLHAC_DEPLOY_KEEP", None)
    if env_keep is not None:
        env["GLHAC_DEPLOY_KEEP"] = env_keep
    env.update(extra_env or {})
    command = ["bash", str(PRUNE), str(root), current_ts]
    if keep is not None:
        command.append(keep)
    result = subprocess.run(command, env=env, text=True, capture_output=True, timeout=15)
    assert result.returncode == 0, result.stderr
    return result


def counts(result):
    return {
        label: (int(deleted), int(remaining))
        for label, deleted, remaining in re.findall(
            r"(DB deploy|DB predeploy|app|home): 삭제 (\d+)개 · 남음 (\d+)개",
            result.stdout,
        )
    }


@pytest.mark.parametrize("keep", [1, 2, 5, 50])
def test_keeps_latest_timestamps_and_archive_mtime_per_kind(remote_root, keep):
    paths = populate(remote_root, keep + 3)
    result = run_prune(remote_root, str(keep))
    for kind, group in paths.items():
        expected = group[-keep:]
        if kind in ("app", "home"):
            expected = group[:1] + expected
        assert [path for path in group if path.exists()] == expected
    assert counts(result) == {"DB deploy": (3, keep), "DB predeploy": (3, keep),
                              "app": (2, keep + 1), "home": (2, keep + 1)}
    assert not result.stderr
    # 다시 실행해도 보관 집합과 개수는 바뀌지 않는다.
    assert counts(run_prune(remote_root, str(keep))) == {
        "DB deploy": (0, keep), "DB predeploy": (0, keep),
        "app": (0, keep + 1), "home": (0, keep + 1),
    }


@pytest.mark.parametrize("env_keep, expected", [(None, 5), ("2", 2)])
def test_default_and_environment_keep(remote_root, env_keep, expected):
    paths = populate(remote_root, 7)
    result = run_prune(remote_root, env_keep=env_keep)
    for kind, group in paths.items():
        expected_paths = group[-expected:]
        if kind in ("app", "home"):
            expected_paths = group[:1] + expected_paths
        assert [path for path in group if path.exists()] == expected_paths
    assert counts(result) == {
        "DB deploy": (7 - expected, expected), "DB predeploy": (7 - expected, expected),
        "app": (6 - expected, expected + 1), "home": (6 - expected, expected + 1),
    }


@pytest.mark.parametrize("current_index", [0, 1, 4])
def test_current_timestamp_is_protected_even_outside_latest_n(remote_root, current_index):
    paths = populate(remote_root, 5)
    result = run_prune(remote_root, "2", current_ts=f"20261116{current_index:06d}")
    expected_counts = {}
    for kind, group in paths.items():
        expected = set(group[-2:]) | {group[current_index]}
        if kind in ("app", "home"):
            expected.add(group[0])
        assert {path for path in group if path.exists()} == expected
        expected_counts[kind] = (5 - len(expected), len(expected))
    assert counts(result) == expected_counts


@pytest.mark.parametrize("kind", ["app", "home"])
def test_old_archive_with_future_mtime_preserves_rollback_candidate(remote_root, kind):
    group = populate(remote_root, 6)[kind]
    future_ns = 4_102_444_800_000_000_000  # 2100-01-01
    os.utime(group[1], ns=(future_ns, future_ns))
    rollback_order = subprocess.run(["ls", "-t", *map(str, group)], check=True,
                                    text=True, capture_output=True, timeout=15)
    assert Path(rollback_order.stdout.splitlines()[0]) == group[1]

    # 최신 이름 2개, 이번 TS, mtime 최신 후보가 서로 다른 파일이다.
    result = run_prune(remote_root, "2", current_ts="20261116000002")
    expected = {group[1], group[2], group[4], group[5]}
    assert {path for path in group if path.exists()} == expected
    assert counts(result)[kind] == (2, 4)
    assert not result.stderr
    repeated = run_prune(remote_root, "2", current_ts="20261116000002")
    assert counts(repeated)[kind] == (0, 4)
    assert {path for path in group if path.exists()} == expected


@pytest.mark.parametrize("kind", ["app", "home"])
def test_archive_mtime_ties_preserve_all_rollback_candidates(remote_root, kind):
    group = populate(remote_root, 4)[kind]
    future_ns = 4_102_444_800_000_000_000
    for path in group[:2]:
        os.utime(path, ns=(future_ns, future_ns))
    result = run_prune(remote_root, "1")
    assert {path for path in group if path.exists()} == {group[0], group[1], group[3]}
    assert counts(result)[kind] == (1, 3)


def test_all_predeploy_names_with_current_timestamp_are_protected(remote_root):
    populate(remote_root, 4)
    extra = backup_path(remote_root, "DB predeploy", "20261116000000", "another_sha.v1")
    extra.write_text("protected")
    result = run_prune(remote_root, "1", current_ts="20261116000000")
    assert extra.read_text() == "protected"
    assert counts(result)["DB predeploy"] == (2, 3)


def test_nonpatterns_links_directories_and_other_backup_locations_are_untouched(remote_root):
    paths = populate(remote_root, 3)
    dbdir = remote_root / "var/lib/glhac"
    untouched = []
    for name in (
        "glhac_v3.db", "glhac_v3.db-wal", "glhac_v3.db-shm",
        "glhac_v3.db.bak-prereprocess-sha-20200101000000",
        "glhac_v3.db.bak-deploy-2020010100000",
        "glhac_v3.db.bak-deploy-202001010000000",
        "glhac_v3.db.bak-deploy-20200101000000.extra",
        "glhac_v3.db.bak-deploy-20200101000000\n",
        "glhac_v3.db.bak-deploy-２０２００１０１００００００",
        "glhac_v3.db.bak-predeploy-sha-2020010100000",
        "glhac_v3.db.bak-predeploy-sha-202001010000000",
        "glhac_v3.db.bak-predeploy-sha-20200101000000.extra",
        "glhac_v3.db.bak-predeploy-sha20200101000000",
        "other-glhac_v3.db.bak-deploy-20200101000000",
    ):
        untouched.append(dbdir / name)
    for kind in ("app", "home"):
        for suffix in ("2020010100000.tgz", "202001010000000.tgz", "20200101000000.tgz.extra",
                       "20200101000000.tgz\n", "20200101000000.zip"):
            untouched.append(remote_root / "opt" / f"glhac-{kind}-bak-{suffix}")
    daily = remote_root / "var/backups/glhac"
    daily.mkdir(parents=True)
    untouched.append(daily / "glhac_v3.db.bak-deploy-20200101000000")
    nested = dbdir / "nested"
    nested.mkdir()
    untouched.append(nested / "glhac_v3.db.bak-deploy-20200101000000")
    for path in untouched:
        path.write_text("untouched")

    links, directories, fifos = [], [], []
    for kind in KINDS:
        # 신규 TS인 링크도 보관 수에 포함되면 안 된다.
        for timestamp, target in (("20270101000000", untouched[0]),
                                  ("20270102000000", remote_root / "missing")):
            link = backup_path(remote_root, kind, timestamp)
            link.symlink_to(target)
            links.append((link, target))
        directory = backup_path(remote_root, kind, "20270103000000")
        directory.mkdir()
        (directory / "keep.txt").write_text("untouched")
        directories.append(directory)
        fifo = backup_path(remote_root, kind, "20270104000000")
        os.mkfifo(fifo)
        fifos.append(fifo)

    result = run_prune(remote_root, "1")
    assert counts(result) == {"DB deploy": (2, 1), "DB predeploy": (2, 1),
                              "app": (1, 2), "home": (1, 2)}
    for kind, group in paths.items():
        expected = group[-1:] if kind.startswith("DB") else [group[0], group[-1]]
        assert [path for path in group if path.exists()] == expected
    assert all(path.read_text() == "untouched" for path in untouched)
    assert all(link.is_symlink() and os.readlink(link) == str(target) for link, target in links)
    assert all((directory / "keep.txt").read_text() == "untouched" for directory in directories)
    assert all(path.is_fifo() for path in fifos)


def test_predeploy_requires_allowed_tag_and_timestamp_separator(remote_root):
    dbdir = remote_root / "var/lib/glhac"
    valid_names = (
        "glhac_v3.db.bak-predeploy-Z_tag.9-20261116000000",
        "glhac_v3.db.bak-predeploy-z-20261116000001",
        "glhac_v3.db.bak-predeploy-B_1.0-20261116000002",
        "glhac_v3.db.bak-predeploy-0-20261116000003",
    )
    untouched_names = (
        "glhac_v3.db.bak-predeploy-20200101000000",
        "glhac_v3.db.bak-predeploy--20200101000000",
        "glhac_v3.db.bak-predeploy-z-tag-20990101000000-20261116000001",
        "glhac_v3.db.bak-predeploy-space and\nnewline-20261116000002",
        "glhac_v3.db.bak-predeploy-quote'-$()-*-20261116000003",
        "glhac_v3.db.bak-predeploy-태그-20200101000000",
    )
    for name in valid_names + untouched_names:
        (dbdir / name).write_text("backup")
    result = run_prune(remote_root, "2")
    assert {path.name for path in dbdir.iterdir()} == set(valid_names[-2:] + untouched_names)
    assert counts(result)["DB predeploy"] == (2, 2)
    assert all((dbdir / name).read_text() == "backup" for name in untouched_names)


@pytest.mark.parametrize("relative_dir, skipped_kinds", [
    ("var", KINDS[:2]),
    ("var/lib", KINDS[:2]),
    ("var/lib/glhac", KINDS[:2]),
    ("opt", KINDS[2:]),
])
@pytest.mark.parametrize("dangling", [False, True])
def test_symlinked_directory_or_parent_skips_affected_groups(
        remote_root, tmp_path, relative_dir, skipped_kinds, dangling):
    paths = populate(remote_root, 4)
    directory = remote_root / relative_dir
    outside = tmp_path / "outside backups"
    directory.rename(outside)
    directory.symlink_to(tmp_path / "missing" if dangling else outside, target_is_directory=True)

    result = run_prune(remote_root, "1")
    expected_counts = {}
    for kind, group in paths.items():
        if kind in skipped_kinds:
            for index, path in enumerate(group):
                assert (outside / path.relative_to(directory)).read_text() == f"backup {index}"
            assert f"{kind}: 삭제 0개 · 남음 확인 불가" in result.stdout
        else:
            expected = {group[-1]} if kind.startswith("DB") else {group[0], group[-1]}
            assert {path for path in group if path.exists()} == expected
            expected_counts[kind] = (4 - len(expected), len(expected))
    assert counts(result) == expected_counts
    assert directory.is_symlink()
    assert result.stderr.count("경고:") == len(skipped_kinds)
    assert result.stderr.count("심볼릭 링크") == len(skipped_kinds)
    assert str(directory) in result.stderr and "정리 생략" in result.stderr


@pytest.mark.parametrize("ancestor", [False, True])
@pytest.mark.parametrize("dangling", [False, True])
def test_symlinked_root_or_ancestor_skips_every_group(remote_root, tmp_path, ancestor, dangling):
    paths = populate(remote_root, 4)
    link = tmp_path / "linked root or parent"
    target = tmp_path if ancestor else remote_root
    link.symlink_to(tmp_path / "missing" if dangling else target, target_is_directory=True)
    root_argument = link / remote_root.name if ancestor else link

    result = run_prune(root_argument, "1")
    for group in paths.values():
        for index, path in enumerate(group):
            assert path.read_text() == f"backup {index}"
    assert not counts(result)
    assert link.is_symlink()
    assert result.stderr.count("경고:") == len(KINDS)
    assert result.stderr.count("심볼릭 링크") == len(KINDS)
    assert str(link) in result.stderr and "정리 생략" in result.stderr


@pytest.mark.parametrize("keep", ["", "0", "51", "-1", "+5", "01", "1.0", "abc", " 5", "5 ",
                                 "5\n", "1\n2", "9" * 100, "1; exit 42", "$(exit 42)"])
def test_invalid_keep_warns_and_skips_every_group(remote_root, keep):
    paths = populate(remote_root, 7)
    result = run_prune(remote_root, env_keep=keep)
    assert "경고:" in result.stderr and "GLHAC_DEPLOY_KEEP" in result.stderr
    assert all(path.is_file() for group in paths.values() for path in group)
    assert not counts(result)


@pytest.mark.parametrize("current_ts", ["", "20261116", "20261116000000x"])
def test_invalid_current_timestamp_skips_cleanup(remote_root, current_ts):
    paths = populate(remote_root, 3)
    result = run_prune(remote_root, "1", current_ts=current_ts)
    assert "경고:" in result.stderr
    assert all(path.is_file() for group in paths.values() for path in group)


@pytest.mark.parametrize("missing", [False, True])
def test_empty_or_missing_directories(remote_root, missing):
    if missing:
        (remote_root / "var/lib/glhac").rmdir()
        (remote_root / "opt").rmdir()
    result = run_prune(remote_root)
    assert counts(result) == {kind: (0, 0) for kind in KINDS}
    assert not result.stderr


@pytest.mark.skipif(os.geteuid() == 0, reason="root는 디렉터리 쓰기 권한 제한을 우회한다")
def test_delete_failure_warns_and_reports_actual_remaining(remote_root):
    populate(remote_root, 3)
    dbdir = remote_root / "var/lib/glhac"
    dbdir.chmod(0o500)
    try:
        result = run_prune(remote_root, "1")
    finally:
        dbdir.chmod(0o700)
    assert "경고:" in result.stderr and "삭제 실패" in result.stderr
    assert counts(result) == {"DB deploy": (0, 3), "DB predeploy": (0, 3),
                              "app": (1, 2), "home": (1, 2)}


def test_python_failure_is_only_a_warning(remote_root, tmp_path):
    paths = populate(remote_root, 3)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    python = bindir / "python3"
    python.write_text("#!/bin/bash\nexit 73\n")
    python.chmod(0o700)
    result = run_prune(remote_root, "1", extra_env={"PATH": f"{bindir}:{os.environ['PATH']}"})
    assert "경고:" in result.stderr
    assert all(path.is_file() for group in paths.values() for path in group)


def cleanup_block():
    source = DEPLOY.read_text()
    return source[source.index("# ── 배포 백업 정리"):].rsplit('\nsay "배포 완료', 1)[0]


@pytest.mark.parametrize("keep", [None, "1", "50", "", "51", "1; exit 42"])
@pytest.mark.parametrize("ssh_status", [0, 255])
def test_deploy_cleanup_heredoc_and_nonfatal_ssh_without_network(tmp_path, keep, ssh_status):
    # deploy.sh 전체를 실행하지 않는다. 정리 블록의 SSH만 로컬 기록기로 대체한다.
    fake_ssh = tmp_path / "record-ssh.sh"
    fake_ssh.write_text('printf "%s\\n" "$@" > "$SSH_ARGS"\n'
                        'cat > "$SSH_INPUT"\nexit "$SSH_STATUS"\n')
    args_file, input_file = tmp_path / "args", tmp_path / "input"
    env = {**os.environ, "SSH_ARGS": str(args_file), "SSH_INPUT": str(input_file),
           "SSH_STATUS": str(ssh_status)}
    env.pop("GLHAC_DEPLOY_KEEP", None)
    if keep is not None:
        env["GLHAC_DEPLOY_KEEP"] = keep
    driver = ('set -euo pipefail\nAPP_SRC="$1"\nSSH=(bash "$2")\nTS="$3"\n'
              'say() { printf "  %s\\n" "$*"; }\n' + cleanup_block() + '\necho continued\n')
    result = subprocess.run(["bash", "-c", driver, "test-cleanup", str(REPO),
                             str(fake_ssh), CURRENT_TS], env=env, text=True,
                            capture_output=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert "continued" in result.stdout
    if keep in ("", "51", "1; exit 42"):
        assert not args_file.exists() and not input_file.exists()
        assert "경고:" in result.stderr
    else:
        assert args_file.read_text().splitlines() == ["sudo", "bash", "-s", "--", "/",
                                                     CURRENT_TS, keep or "5"]
        assert input_file.read_text() == "set -euo pipefail\n" + PRUNE.read_text()
        assert ("경고:" in result.stderr) == (ssh_status != 0)


def test_cleanup_is_after_success_and_absent_from_failure_rollback_and_dry_run():
    source = DEPLOY.read_text()
    assert "set -euo pipefail" in source.split('KEY=', 1)[0]
    apply = source.split('say "적용 후 확인"\n', 1)[1].split("\nEOS\n", 1)[0]
    assert apply.startswith('"${SSH[@]}" bash -se <<EOS\nset -uo pipefail\n')
    assert r'if [ "\$code" = "200" ]; then ok=1;' in apply
    assert r'[ "\$code" = "200" ] || exit 1' in apply  # home-only 경로
    failure = apply.split(r'if [ "\$ok" != 1 ]; then', 1)[1].split("\n  fi", 1)[0]
    assert "sudo tar xzf /opt/glhac-app-bak-$TS.tgz" in failure
    assert "exit 1" in failure
    assert "deploy_prune_remote" not in apply
    rollback = source.split('if [ "$ROLLBACK" = 1 ]; then', 1)[1].split("# ── 사전 점검", 1)[0]
    assert "exit 0" in rollback and "deploy_prune_remote" not in rollback
    dry = source.split('if [ "$DRY" = 1 ]; then', 1)[1].split("\n", 1)[0]
    assert "exit 0" in dry and "deploy_prune_remote" not in dry
    assert source.index("# ── 배포 백업 정리") > source.index("# ── 바깥에서 실제로 열리는지")
    assert source.count('sudo bash -s -- / "$TS" "$DEPLOY_KEEP" <<EOS') == 1
    assert 'if ! "${SSH[@]}" sudo bash -s' in cleanup_block()
