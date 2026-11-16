"""macOS integration tests: WAL SQLite, real local rsync, fake SSH, no network."""
import gzip
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import signal
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest


ROOT = Path(__file__).resolve().parents[1]
PULL = ROOT / "scripts/offsite_backup_pull.sh"
RESTORE = ROOT / "scripts/offsite_backup_restore_check.sh"
NAME_RE = re.compile(r"glhac_v3-[0-9]{8}-[0-9]{6}\.db\.gz\Z")
NOW = datetime.now(ZoneInfo("Asia/Seoul")).replace(microsecond=0)
pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="macOS BSD date/stat required")


@pytest.fixture(autouse=True, scope="module")
def fresh_db_per_module():
    """Override the app-starting conftest fixture: this suite is shell-only."""
    yield


FAKE_TOOL = r'''
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time

tool = Path(sys.argv[0]).name
args = sys.argv[1:]
with open(os.environ["FAKE_EVENTS"], "a") as log:
    log.write(json.dumps({"tool": tool, "args": args}) + "\n")
remote = Path(os.environ["FAKE_REMOTE"])

def sleep_if_requested(kind):
    if os.environ.get("FAKE_SLEEP") == kind:
        # Exercise the KILL fallback and descendant cleanup, not just exit-on-TERM.
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        child = subprocess.Popen(["/bin/sleep", "30"])
        Path(os.environ["FAKE_SLEEP_PIDS"]).write_text(json.dumps([os.getpid(), child.pid]))
        time.sleep(30)

if tool == "ssh":
    # Reject ANY command other than the intended, read-only directory listing.
    command = args[args.index("--") + 1:]
    assert command == [os.environ.get("GLHAC_BACKUP_USER", "rocky") + "@" + os.environ["GLHAC_BACKUP_HOST"],
                       "ls", "-ln", "--time-style=+%s", "--", os.environ.get("GLHAC_BACKUP_REMOTE_DIR", "/var/backups/glhac").rstrip("/") + "/"]
    assert args[args.index("-i") + 1] == os.environ["GLHAC_BACKUP_KEY"]
    assert "BatchMode=yes" in args and "StrictHostKeyChecking=yes" in args
    if os.environ.get("FAKE_FAIL_SSH"):
        sys.exit(255)
    sleep_if_requested("ssh")
    if os.environ.get("FAKE_BREAK_LOG"):
        log_path = Path(os.environ["GLHAC_BACKUP_LOCAL_DIR"]) / "offsite-backup.log"
        if log_path.is_file():
            log_path.unlink()
        log_path.mkdir(exist_ok=True)
    if os.environ.get("FAKE_HOLD"):
        marker = Path(os.environ["FAKE_HOLD"])
        marker.write_text("ready")
        deadline = time.monotonic() + 20
        while not marker.with_suffix(".release").exists():
            if time.monotonic() > deadline:
                sys.exit(98)
            time.sleep(0.02)
    print("total 100")
    # Deliberately descending: the script must select by name, not ls order.
    for path in sorted(remote.iterdir(), reverse=True):
        kind = "l" if path.is_symlink() else "d" if path.is_dir() else "-"
        print(f"{kind}rw-r--r-- 1 0 0 {path.stat().st_size} 1 {path.name}")
elif tool == "rsync":
    source, destination = args[-2:]
    if ":" in source and not source.startswith("/"):
        sleep_if_requested("download")
        assert "--delete" not in args
        transport = Path(args[args.index("-e") + 1])
        assert transport.is_file() and os.access(transport, os.X_OK)
        # Exercise the generated transport's real Bash quoting with FAKE ssh.
        probe = ["/bin/bash", str(transport), "--",
            os.environ.get("GLHAC_BACKUP_USER", "rocky") + "@" + os.environ["GLHAC_BACKUP_HOST"],
            "ls", "-ln", "--time-style=+%s", "--", os.environ.get("GLHAC_BACKUP_REMOTE_DIR", "/var/backups/glhac").rstrip("/") + "/"]
        subprocess.run(probe, check=True, stdout=subprocess.DEVNULL)
        if os.environ.get("FAKE_FAIL_TRANSFER"):
            Path(destination).write_bytes(b"partial")
            sys.exit(12)
        shutil.copyfile(remote / source.rsplit("/", 1)[1], destination)
    else:
        sleep_if_requested("mirror")
        assert not any(a.startswith("--delete") for a in args)
        assert "--ignore-existing" not in args and "-e" not in args
        assert "--checksum" in args
        source, sidecar, destination = map(Path, args[args.index("--") + 1:])
        assert source.is_absolute() and sidecar == Path(str(source) + ".sha256")
        assert source.parent.name in ("daily", "weekly", "monthly")
        assert source.parent.parent == Path(os.environ["GLHAC_BACKUP_LOCAL_DIR"]).resolve()
        assert destination.parent == Path(os.environ["GLHAC_BACKUP_MIRROR_DIR"]).resolve()
        assert destination.name.startswith(".offsite-mirror.")
        # Real local-only rsync: no remote operand or transport can get here.
        # openrsync re-execs its local receiver through PATH; keep that child
        # on the real binary too, after validating every operand is local.
        result = subprocess.run(["/usr/bin/rsync", *args], check=False,
                                env={**os.environ, "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"})
        with open(os.environ["FAKE_EVENTS"], "a") as log:
            log.write(json.dumps({"tool": "local_rsync", "args": args,
                                  "returncode": result.returncode}) + "\n")
        if result.returncode:
            sys.exit(result.returncode)
        failure = os.environ.get("FAKE_FAIL_MIRROR")
        if failure in ("1", source.parent.name):
            # Fail AFTER actual I/O and leave a partial staged file.
            (destination / source.name).write_bytes(b"partial transfer")
            sys.exit(23)
        if os.environ.get("FAKE_CORRUPT_MIRROR") == source.parent.name:
            (destination / source.name).write_bytes(b"undetected I/O corruption")
elif tool == "stat":
    mirror = Path(os.environ["GLHAC_BACKUP_MIRROR_DIR"]).resolve()
    path = Path(args[-1])
    if "%d" in args and (path == mirror or mirror in path.parents):
        stage = os.environ["FAKE_DEVICE_STAGE"]
        changed = ((stage == "mkdir" and any(mirror.glob(".offsite-mirror.*")))
                   or (stage == "copy" and (mirror / "daily").is_dir())
                   or (stage == "prune" and Path(os.environ["FAKE_DEVICE_AFTER_FILE"]).exists()))
        if changed:
            print(mirror.stat().st_dev + 1)
            sys.exit(0)
    sys.exit(subprocess.run(["/usr/bin/stat", *args]).returncode)
elif tool == "osascript":
    # Validate static -e code and a separate argv message, without real alerts.
    assert args[:6] == ["-e", "on run argv", "-e",
                        'display notification (item 1 of argv) with title "GL-HAC 외부 백업"',
                        "-e", "end run"]
    assert len(args) == 7
    with open(os.environ["FAKE_EVENTS"], "a") as log:
        log.write(json.dumps({"tool": "notification", "args": [args[-1]]}) + "\n")
else:
    raise AssertionError("Unexpected external tool: " + tool)
'''


def backup_name(stamp):
    return stamp.strftime("glhac_v3-%Y%m%d-%H%M%S.db.gz")


def checksum(data, name):
    return f"{hashlib.sha256(data).hexdigest()}  {name}\n"


@pytest.fixture
def harness(tmp_path):
    class Harness:
        def __init__(self):
            self.remote = tmp_path / "fake remote"
            self.local = tmp_path / "local backups 'quoted'"
            self.bin = tmp_path / "bin"
            self.scratch = tmp_path / "scratch"
            self.events = tmp_path / "events.jsonl"
            self.key = tmp_path / "fake key 'quoted' $(never-run)"
            self.key.write_text("FAKE-KEY-CONTENT-NOT-A-REAL-KEY")
            for path in (self.remote, self.local, self.bin, self.scratch):
                path.mkdir()
            for name in ("ssh", "rsync", "osascript", "scp"):
                self.install_tool(name)
            self.env = {k: v for k, v in os.environ.items()
                        if not k.startswith(("GLHAC_BACKUP_", "FAKE_"))}
            self.env.update({
                "PATH": f"{self.bin}:/usr/bin:/bin:/usr/sbin:/sbin",
                "TZ": "Asia/Seoul", "TMPDIR": str(self.scratch),
                "GLHAC_BACKUP_HOST": "backup.invalid",
                "GLHAC_BACKUP_KEY": str(self.key),
                "GLHAC_BACKUP_LOCAL_DIR": str(self.local),
                "GLHAC_BACKUP_NOTIFY": "0",
                "FAKE_REMOTE": str(self.remote), "FAKE_EVENTS": str(self.events),
            })

        def install_tool(self, name):
            tool = self.bin / name
            tool.write_text(f"#!{sys.executable}\n" + FAKE_TOOL)
            tool.chmod(0o700)

        def data(self, cases=3, broken_integrity=False):
            path = self.scratch / "fixture.db"
            backup = self.scratch / "fixture-backup.db"
            conn = sqlite3.connect(path)
            try:
                assert conn.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
                conn.executescript("DROP TABLE IF EXISTS case_application; DROP TABLE IF EXISTS document_asset;"
                                   "CREATE TABLE case_application(id INTEGER PRIMARY KEY, valid INTEGER);"
                                   "CREATE TABLE document_asset(id INTEGER PRIMARY KEY);")
                conn.executemany("INSERT INTO case_application(valid) VALUES (?)",
                                 [(1,)] * cases)
                conn.executemany("INSERT INTO document_asset DEFAULT VALUES", [()] * 2)
                conn.commit()
                # Match production: .backup from a live WAL database, without
                # including its -wal/-shm sidecars in the compressed backup.
                subprocess.run(["/usr/bin/sqlite3", "-init", "/dev/null", "-batch", "-bail",
                                "fixture.db", ".backup fixture-backup.db"],
                               cwd=self.scratch, check=True, capture_output=True)
            finally:
                conn.close()
            raw = bytearray(backup.read_bytes())
            assert raw[18:20] == b"\x02\x02", "fixture must retain the WAL header"
            if broken_integrity:
                # A valid SQLite header with an inconsistent freelist count:
                # integrity_check reports corruption even if SELECT still works.
                raw[36:40] = (1).to_bytes(4, "big")
            data = gzip.compress(raw, mtime=0)
            path.unlink()
            backup.unlink()
            return data

        def put(self, stamp=NOW, data=None):
            name = backup_name(stamp)
            (self.remote / name).write_bytes(self.data() if data is None else data)
            return name

        def seed(self, tier, name, data, root=None):
            directory = (self.local if root is None else root) / tier
            directory.mkdir(parents=True, exist_ok=True)
            (directory / name).write_bytes(data)
            (directory / (name + ".sha256")).write_text(checksum(data, name))

        def run(self, script=PULL, args=(), **overrides):
            return subprocess.run(["/bin/bash", str(script), *map(str, args)],
                                  env={**self.env, **overrides}, text=True,
                                  capture_output=True, timeout=30)

        def calls(self, tool):
            if not self.events.exists():
                return []
            return [event["args"] for line in self.events.read_text().splitlines()
                    if (event := json.loads(line))["tool"] == tool]

        def names(self, tier):
            return sorted(p.name for p in (self.local / tier).glob("*.db.gz")
                          if NAME_RE.fullmatch(p.name) and p.is_file() and not p.is_symlink())

        def clean(self):
            assert not list(self.local.glob(".offsite-work.*"))
            assert not (self.local / ".offsite-backup.lock").exists()
            assert not list(self.scratch.glob("glhac-restore-check.*"))
            for args in self.calls("rsync"):
                if "-e" in args:
                    assert not Path(args[args.index("-e") + 1]).parent.exists()

    return Harness()


def test_receive_sha256_and_skip(harness):
    h = harness
    name = h.put()
    data = (h.remote / name).read_bytes()
    for result in (h.run(), h.run()):
        assert result.returncode == 0, result.stderr
    assert h.names("daily") == [name]
    assert (h.local / "daily" / name).read_bytes() == data
    assert (h.local / "daily" / (name + ".sha256")).read_text() == checksum(data, name)
    assert len(h.calls("rsync")) == 1
    assert len(h.calls("ssh")) == 3  # two listings plus fake transport quoting probe
    log = (h.local / "offsite-backup.log").read_text()
    assert "result=OK" in log and "result=SKIP" in log
    assert f"file={name} bytes={len(data)} cases=3" in log
    assert "FAKE-KEY-CONTENT" not in log and str(h.key) not in log
    h.clean()


@pytest.mark.parametrize("kind", ["gzip", "not_sqlite", "integrity", "empty"])
def test_invalid_backup_never_enters_daily(harness, kind):
    h = harness
    data = {"gzip": lambda: b"broken gzip",
            "not_sqlite": lambda: gzip.compress(b"not a SQLite database"),
            "integrity": lambda: h.data(broken_integrity=True),
            "empty": lambda: h.data(cases=0)}[kind]()
    h.put(data=data)
    result = h.run(GLHAC_BACKUP_NOTIFY="1")
    assert result.returncode == 1
    assert "result=ERROR" in result.stderr
    assert not any(h.names(tier) for tier in ("daily", "weekly", "monthly"))
    assert h.calls("osascript")
    h.clean()


def test_stale_backup_warns_and_is_received(harness):
    h = harness
    name = h.put(NOW - timedelta(hours=48))
    result = h.run(GLHAC_BACKUP_NOTIFY="1", GLHAC_BACKUP_MAX_AGE_HOURS="36")
    assert result.returncode == 0, result.stderr
    assert "서버 백업 정지" in result.stdout
    assert h.names("daily") == [name]
    assert h.calls("osascript")
    h.clean()


def test_invalid_names_dates_and_symlinks_are_ignored(harness):
    h = harness
    name = h.put()
    for bad in ("unrelated.db.gz", "glhac_v3-99999999-999999.db.gz",
                "glhac_v3-20260230-030000.db.gz", "glhac_v3-20260101-250000.db.gz",
                "glhac_v3-29990101-030000.db.gz;touch PWNED", "glhac_v3-29990101-030000.db.gz extra"):
        (h.remote / bad).write_bytes(b"junk")
    (h.remote / "glhac_v3-29990102-030000.db.gz").symlink_to(h.remote / name)
    (h.remote / "glhac_v3-29990103-030000.db.gz").mkdir()
    h.put(NOW - timedelta(days=1))
    result = h.run(GLHAC_BACKUP_REMOTE_DIR="/srv/glhac-backups/archive")
    assert result.returncode == 0, result.stderr
    assert h.names("daily") == [name]
    h.clean()


def test_no_valid_remote_backup_fails_without_transfer(harness):
    h = harness
    (h.remote / "glhac_v3-20261301-030000.db.gz").write_bytes(b"junk")
    result = h.run()
    assert result.returncode == 1 and "유효한 서버 백업" in result.stderr
    assert not h.calls("rsync") and not h.names("daily")
    h.clean()


@pytest.mark.parametrize("eligible_age", [None, 0, 48])
def test_future_backup_is_excluded_without_masking_stale_warning(harness, eligible_age):
    h = harness
    future = h.put(NOW + timedelta(hours=2))
    eligible = h.put(NOW - timedelta(hours=eligible_age)) if eligible_age is not None else None
    result = h.run(GLHAC_BACKUP_NOTIFY="1")
    assert result.returncode == (1 if eligible is None else 0), result.stderr
    assert "미래 시각 백업 제외" in result.stdout and future in result.stdout
    assert h.names("daily") == ([eligible] if eligible else [])
    assert ("서버 백업 정지" in result.stdout) == (eligible_age == 48)
    assert h.calls("osascript")
    if eligible is None:
        assert not h.calls("rsync")
    h.clean()


def test_future_backup_within_one_hour_is_allowed(harness):
    h = harness
    name = h.put(NOW + timedelta(minutes=30))
    result = h.run()
    assert result.returncode == 0, result.stderr
    assert h.names("daily") == [name]
    assert "미래 시각" not in result.stdout
    h.clean()


@pytest.mark.parametrize("damage", ["contents", "checksum", "missing_checksum"])
def test_corrupt_previous_daily_preserves_new_daily_and_all_archives(harness, damage):
    h = harness
    data = h.data()
    # Find an older month's first Sunday, eligible for BOTH archive tiers.
    first = (NOW.replace(day=1) - timedelta(days=1)).replace(day=1)
    old_stamp = first + timedelta(days=(6 - first.weekday()) % 7)
    old = backup_name(old_stamp)
    for tier in ("daily", "weekly", "monthly"):
        h.seed(tier, old, data)
    # A full preflight must stop even changes due to a good, earlier candidate.
    earlier = backup_name(old_stamp - timedelta(days=1))
    h.seed("daily", earlier, data)
    for offset in range(20):
        h.seed("daily", backup_name(old_stamp - timedelta(days=offset + 2)), data)
    for offset in range(10):
        h.seed("weekly", backup_name(old_stamp - timedelta(weeks=offset + 1)), data)
    for offset in range(14):
        h.seed("monthly", backup_name(old_stamp - timedelta(days=40 * (offset + 1))), data)
    mirror = h.local.parent / "mirror"
    mirror.mkdir()
    for tier in ("daily", "weekly", "monthly"):
        directory = mirror / tier
        directory.mkdir()
        (directory / old).write_bytes(data)
        (directory / (old + ".sha256")).write_text(checksum(data, old))
    path = h.local / "daily" / old
    if damage == "contents":
        path.write_bytes(b"corrupt previous daily")
    elif damage == "checksum":
        Path(str(path) + ".sha256").write_text("0" * 64 + f"  {old}\n")
    else:
        Path(str(path) + ".sha256").unlink()
    before_daily = h.names("daily")
    before = {p: p.read_bytes() for root in (h.local / "weekly", h.local / "monthly", mirror)
              for p in root.rglob("*") if p.is_file()}
    new = h.put(data=data)
    result = h.run(GLHAC_BACKUP_MIRROR_DIR=str(mirror), GLHAC_BACKUP_NOTIFY="1")
    assert result.returncode == 1 and "이전 daily SHA-256" in result.stderr
    assert old in result.stderr and "result=ERROR" in (h.local / "offsite-backup.log").read_text()
    assert h.names("daily") == sorted(before_daily + [new])
    assert (h.local / "daily" / new).read_bytes() == data
    assert (h.local / "daily" / (new + ".sha256")).read_text() == checksum(data, new)
    after = {p: p.read_bytes() for root in (h.local / "weekly", h.local / "monthly", mirror)
             for p in root.rglob("*") if p.is_file()}
    assert after == before
    assert not h.calls("local_rsync") and len(h.calls("osascript")) == 1
    h.clean()


@pytest.mark.parametrize("stage", ["initial", "during_failure", "during_warning"])
def test_log_write_failure_does_not_block_notification(harness, stage):
    h = harness
    if stage == "initial":
        (h.local / "offsite-backup.log").mkdir()
        overrides = {}
    else:
        h.put(NOW - timedelta(hours=48) if stage == "during_warning" else NOW,
              data=h.data() if stage == "during_warning" else b"broken gzip")
        overrides = {"FAKE_BREAK_LOG": "1"}
    result = h.run(GLHAC_BACKUP_NOTIFY="1", **overrides)
    assert result.returncode == (0 if stage == "during_warning" else 1)
    assert len(h.calls("osascript")) == 1
    assert (h.local / "offsite-backup.log").is_dir()
    if stage == "during_warning":
        assert "result=WARN" in result.stdout
    else:
        assert "result=ERROR" in result.stderr
    if stage == "initial":
        assert "로그 파일" in result.stderr and not h.calls("ssh")
    h.clean()


def test_unwritable_tmpdir_does_not_block_notification(harness):
    h = harness
    unwritable = h.scratch / "unwritable tmp"
    unwritable.mkdir(mode=0o500)
    try:
        assert not os.access(unwritable, os.W_OK)
        # Fail before any transport or here-string; only the error path runs.
        result = h.run(TMPDIR=str(unwritable), GLHAC_BACKUP_HOST="", GLHAC_BACKUP_NOTIFY="1")
        assert result.returncode == 1 and "result=ERROR" in result.stderr
        assert not h.calls("ssh") and not h.calls("rsync")
        calls = h.calls("osascript")
        assert len(calls) == 1
        assert calls[0][:6] == ["-e", "on run argv", "-e",
                               'display notification (item 1 of argv) with title "GL-HAC 외부 백업"',
                               "-e", "end run"]
        assert h.calls("notification") == [["HOST에 IPv4 주소 또는 호스트명이 필요합니다 (-)"]]
    finally:
        unwritable.chmod(0o700)
    h.clean()


@pytest.mark.parametrize("kind", ["ssh", "download", "mirror"])
def test_command_timeout_notifies_and_kills_descendants(harness, kind):
    h = harness
    name = h.put()
    mirror = h.local.parent / "mirror"
    mirror.mkdir()
    pid_file = h.scratch / "sleep-pids.json"
    start = time.monotonic()
    result = h.run(GLHAC_BACKUP_NOTIFY="1", GLHAC_BACKUP_MIRROR_DIR=str(mirror),
                   GLHAC_BACKUP_SSH_TIMEOUT_SECONDS="1",
                   GLHAC_BACKUP_RSYNC_TIMEOUT_SECONDS="1",
                   FAKE_SLEEP=kind, FAKE_SLEEP_PIDS=str(pid_file))
    assert time.monotonic() - start < 10
    assert result.returncode == 1 and "시간 제한 초과" in result.stderr
    assert len(h.calls("osascript")) == 1
    assert h.names("daily") == ([name] if kind == "mirror" else [])
    pids = json.loads(pid_file.read_text())
    try:
        for pid in pids:
            with pytest.raises(ProcessLookupError):
                os.kill(pid, 0)
    finally:
        # A regression must not leave a sleeping helper behind after the test.
        for pid in pids:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    assert not list(mirror.glob(".offsite-mirror.*"))
    h.clean()


def test_retention_by_filename_and_sunday_monthly_archives(harness):
    h = harness
    data = h.data()
    sunday = NOW - timedelta(days=(NOW.weekday() + 1) % 7)
    for offset in range(1, 21):
        name = backup_name(sunday - timedelta(days=offset))
        h.seed("daily", name, data)
        # Reverse mtimes: retention must still follow filename dates.
        os.utime(h.local / "daily" / name, (offset * 10000, offset * 10000))
    for offset in range(3, 14):
        h.seed("weekly", backup_name(sunday - timedelta(weeks=offset)), data)
    for offset in range(1, 15):
        month_number = sunday.year * 12 + sunday.month - 1 - offset
        stamp = sunday.replace(year=month_number // 12, month=month_number % 12 + 1, day=2)
        h.seed("monthly", backup_name(stamp), data)
    for tier in ("daily", "weekly", "monthly"):
        (h.local / tier / "keep.txt").write_text("keep")
        (h.local / tier / "glhac_v3-malformed.db.gz").write_text("keep")
        (h.local / tier / "glhac_v3-19000101-030000.db.gz").symlink_to(h.key)
    newest = h.put(sunday, data)
    all_daily = sorted(h.names("daily") + [newest])
    expected_weekly = sorted(set(h.names("weekly") + [n for n in all_daily
        if datetime.strptime(n, "glhac_v3-%Y%m%d-%H%M%S.db.gz").weekday() == 6]))[-8:]
    earliest = {}
    for name in sorted(h.names("monthly") + all_daily):
        earliest.setdefault(name[9:15], name)
    result = h.run()
    assert result.returncode == 0, result.stderr
    assert h.names("daily") == all_daily[-14:]
    assert h.names("weekly") == expected_weekly
    assert h.names("monthly") == sorted(earliest.values())[-12:]
    for tier, count in (("daily", 14), ("weekly", 8), ("monthly", 12)):
        assert len(h.names(tier)) == count
        for name in h.names(tier):
            assert (h.local / tier / (name + ".sha256")).read_text() == checksum(data, name)
        assert (h.local / tier / "keep.txt").read_text() == "keep"
        assert (h.local / tier / "glhac_v3-malformed.db.gz").read_text() == "keep"
        assert (h.local / tier / "glhac_v3-19000101-030000.db.gz").is_symlink()
    for removed in all_daily[:-14]:
        assert not (h.local / "daily" / (removed + ".sha256")).exists()
    h.clean()


def test_monthly_pins_first_received_even_if_first_day_was_missed(harness):
    h = harness
    previous_month = NOW.replace(day=1) - timedelta(days=1)
    first = h.put(previous_month.replace(day=3))
    assert h.run().returncode == 0
    second = h.put(previous_month.replace(day=6))
    result = h.run()
    assert result.returncode == 0, result.stderr
    assert h.names("daily") == [first, second]
    assert (h.local / "daily" / second).read_bytes() == (h.remote / second).read_bytes()
    assert len(h.calls("rsync")) == 2 and h.calls("rsync")[-1][-2].endswith("/" + second)
    assert h.names("monthly") == [first]
    h.clean()


@pytest.mark.parametrize("store", ["local", "mirror"])
@pytest.mark.parametrize("tier,keep", [("daily", 14), ("weekly", 8), ("monthly", 12)])
@pytest.mark.parametrize("damage", ["contents", "checksum", "missing_checksum"])
def test_any_uncertain_file_blocks_all_deletions_in_its_tier(harness, store, tier, keep, damage):
    h = harness
    data = h.data()
    sunday = NOW - timedelta(days=(NOW.weekday() + 1) % 7)
    root = h.local if store == "local" else h.local.parent / "mirror"
    # More GOOD files than the limit: counting only verified files is unsafe too.
    for offset in range(1, keep + 2):
        h.seed(tier, backup_name(sunday - timedelta(days=40 * offset)), data, root=root)
    bad = backup_name(sunday - timedelta(days=1))
    h.seed(tier, bad, data, root=root)
    directory = root / tier
    sidecar = directory / (bad + ".sha256")
    if damage == "contents":
        (directory / bad).write_bytes(b"CORRUPT-CONTENTS-MUST-NOT-BE-LOGGED")
    elif damage == "checksum":
        sidecar.write_text("0" * 64 + f"  {bad}\n")
    else:
        sidecar.unlink()
    before = {p: p.read_bytes() for p in directory.iterdir()}
    new = h.put(sunday, data)
    overrides = {"GLHAC_BACKUP_MIRROR_DIR": str(root)} if store == "mirror" else {}
    result = h.run(GLHAC_BACKUP_NOTIFY="1", **overrides)
    assert result.returncode == 1, (result.stdout, result.stderr)
    assert "result=ERROR" in result.stderr and bad in result.stderr and tier in result.stderr
    assert all(p.read_bytes() == contents for p, contents in before.items())
    assert (directory / new).read_bytes() == data
    if damage == "missing_checksum":
        assert not sidecar.exists()
    log = (h.local / "offsite-backup.log").read_text()
    assert "result=ERROR" in log and bad in log
    assert "CORRUPT-CONTENTS-MUST-NOT-BE-LOGGED" not in log
    assert "result=OK" not in log and "result=SKIP" not in log
    assert h.calls("notification")
    assert not list(root.glob(".offsite-mirror.*"))
    h.clean()


@pytest.mark.parametrize("damage", ["contents", "missing_checksum"])
def test_corrupt_monthly_cannot_pin_representative_or_allow_any_monthly_deletion(harness, damage):
    h = harness
    data = h.data()
    previous_month = NOW.replace(day=1) - timedelta(days=1)
    bad = backup_name(previous_month.replace(day=1))
    later = backup_name(previous_month.replace(day=9))
    for name in (bad, later):
        h.seed("monthly", name, data)
    bad_path = h.local / "monthly" / bad
    if damage == "contents":
        bad_path.write_bytes(b"corrupt old monthly")
    else:
        Path(str(bad_path) + ".sha256").unlink()
    before = {p: p.read_bytes() for p in bad_path.parent.iterdir()}
    first = h.put(previous_month.replace(day=3), data)
    result = h.run(GLHAC_BACKUP_NOTIFY="1")
    assert result.returncode == 1 and bad in result.stderr
    assert (h.local / "monthly" / first).read_bytes() == data
    assert h.names("monthly") == [bad, first, later]
    assert all(p.read_bytes() == contents for p, contents in before.items())
    # The next run must select the healthy 3rd; the corrupt 1st cannot pin it.
    second = h.put(previous_month.replace(day=6), data)
    result = h.run(GLHAC_BACKUP_NOTIFY="1")
    assert result.returncode == 1 and "result=ERROR" in result.stderr
    assert h.names("daily") == [first, second] and len(h.calls("rsync")) == 2
    assert h.names("monthly") == [bad, first, later]
    assert (h.local / "monthly" / (first + ".sha256")).read_text() == checksum(data, first)
    assert all(p.read_bytes() == contents for p, contents in before.items())
    if damage == "missing_checksum":
        assert not Path(str(bad_path) + ".sha256").exists()
    assert h.calls("notification")
    h.clean()


@pytest.mark.parametrize("store,tier", [("local", "weekly"), ("local", "monthly"),
                                      ("mirror", "daily"), ("mirror", "weekly"), ("mirror", "monthly")])
def test_corrupt_existing_target_is_preserved_instead_of_overwritten(harness, store, tier):
    h = harness
    data = h.data()
    sunday = NOW - timedelta(days=(NOW.weekday() + 1) % 7)
    name = h.put(sunday, data)
    root = h.local if store == "local" else h.local.parent / "mirror"
    h.seed(tier, name, data, root=root)
    target = root / tier / name
    target.write_bytes(b"corrupt existing target")
    sidecar = Path(str(target) + ".sha256")
    before = (target.read_bytes(), sidecar.read_bytes(), target.stat().st_ino)
    overrides = {"GLHAC_BACKUP_MIRROR_DIR": str(root)} if store == "mirror" else {}
    result = h.run(GLHAC_BACKUP_NOTIFY="1", **overrides)
    assert result.returncode == 1 and name in result.stderr
    assert before == (target.read_bytes(), sidecar.read_bytes(), target.stat().st_ino)
    assert (h.local / "daily" / name).read_bytes() == data
    assert h.calls("notification")
    h.clean()


def test_mirror_daily_keeps_old_good_file_despite_thirteen_newer_corrupt_files(harness):
    h = harness
    data = h.data()
    mirror = h.local.parent / "mirror"
    old = backup_name(NOW - timedelta(days=100))
    h.seed("daily", old, data, root=mirror)
    bad_names = []
    for offset in range(1, 14):
        bad = backup_name(NOW - timedelta(days=offset))
        h.seed("daily", bad, data, root=mirror)
        (mirror / "daily" / bad).write_bytes(b"corrupt history")
        bad_names.append(bad)
    before = {p: p.read_bytes() for p in (mirror / "daily").iterdir()}
    new = h.put(data=data)
    result = h.run(GLHAC_BACKUP_MIRROR_DIR=str(mirror), GLHAC_BACKUP_NOTIFY="1")
    assert result.returncode == 1 and "result=ERROR" in result.stderr
    assert all(name in result.stderr for name in bad_names)
    assert (mirror / "daily" / old).read_bytes() == data
    assert (mirror / "daily" / new).read_bytes() == data
    assert len(list((mirror / "daily").glob("*.db.gz"))) == 15
    assert all(p.read_bytes() == contents for p, contents in before.items())
    assert len(h.calls("notification")) == 13
    h.clean()


@pytest.mark.parametrize("on_volume", [False, True])
def test_missing_mirror_warns_without_creating_it(harness, on_volume):
    h = harness
    name = h.put()
    mirror = Path("/Volumes/GLHAC-test-unmounted-" + h.local.parent.name) / "Backups" if on_volume else h.local.parent / "missing mirror"
    assert not mirror.exists()
    result = h.run(GLHAC_BACKUP_MIRROR_DIR=str(mirror), GLHAC_BACKUP_NOTIFY="1")
    assert result.returncode == 0, result.stderr
    assert "result=WARN" in result.stdout and "미러" in result.stdout
    assert h.names("daily") == [name] and not mirror.exists()
    assert h.calls("osascript")
    h.clean()


def test_real_rsync_preserves_mirror_only_history_and_unmanaged_files(harness):
    h = harness
    name = h.put()
    mirror = h.local.parent / "mirror disk"
    mirror.mkdir()
    (mirror / "keep-root.txt").write_text("keep")
    stale = "glhac_v3-20000101-030000.db.gz"
    for tier in ("daily", "weekly", "monthly"):
        directory = mirror / tier
        directory.mkdir()
        for other in ("keep.txt", "glhac_v3-INVALID.db.gz"):
            (directory / other).write_text("keep")
        (directory / stale).write_bytes(b"old")
        (directory / (stale + ".sha256")).write_text(checksum(b"old", stale))
        nested = directory / "glhac_v3-20000102-030000.db.gz"
        nested.mkdir()
        (nested / "keep.txt").write_text("keep")
    result = h.run(GLHAC_BACKUP_MIRROR_DIR=str(mirror))
    assert result.returncode == 0, result.stderr
    assert (mirror / "keep-root.txt").read_text() == "keep"
    assert (mirror / "daily" / name).read_bytes() == (h.remote / name).read_bytes()
    for tier in ("daily", "weekly", "monthly"):
        directory = mirror / tier
        assert (directory / stale).read_bytes() == b"old"
        assert (directory / (stale + ".sha256")).read_text() == checksum(b"old", stale)
        assert (directory / "keep.txt").read_text() == "keep"
        assert (directory / "glhac_v3-INVALID.db.gz").read_text() == "keep"
        assert (directory / "glhac_v3-20000102-030000.db.gz" / "keep.txt").read_text() == "keep"
        for backup in h.names(tier):
            assert (directory / backup).read_bytes() == (h.local / tier / backup).read_bytes()
            assert (directory / (backup + ".sha256")).read_bytes() == (h.local / tier / (backup + ".sha256")).read_bytes()
    assert h.calls("local_rsync")
    assert not list(mirror.glob(".offsite-mirror.*"))
    h.clean()


def test_real_rsync_mirror_prunes_by_its_own_filename_history(harness):
    h = harness
    data = h.data()
    sunday = NOW - timedelta(days=(NOW.weekday() + 1) % 7)
    name = h.put(sunday, data)
    mirror = h.local.parent / "mirror"
    mirror.mkdir()
    expected = {}
    for tier, keep in (("daily", 14), ("weekly", 8), ("monthly", 12)):
        directory = mirror / tier
        directory.mkdir()
        names = []
        for offset in range(1, keep + 5):
            old = backup_name(sunday - timedelta(days=40 * offset))
            names.append(old)
            (directory / old).write_bytes(data)
            (directory / (old + ".sha256")).write_text(checksum(data, old))
            os.utime(directory / old, (offset * 10000, offset * 10000))
        expected[tier] = sorted(names + [name])[-keep:]
    result = h.run(GLHAC_BACKUP_MIRROR_DIR=str(mirror))
    assert result.returncode == 0, result.stderr
    assert h.calls("local_rsync")
    for tier, retained in expected.items():
        directory = mirror / tier
        assert sorted(p.name for p in directory.glob("*.db.gz")) == retained
        assert sorted(p.name for p in directory.glob("*.sha256")) == [n + ".sha256" for n in retained]
        assert h.names(tier) == [name]  # fresh local store must not erase history
    h.clean()


@pytest.mark.parametrize("failure", [None, "transfer", "checksum"])
def test_real_rsync_mirror_replaces_changed_content_only_after_validation(harness, failure):
    h = harness
    data = h.data()
    name = h.put(data=data)
    h.seed("daily", name, data)
    mirror = h.local.parent / "mirror"
    directory = mirror / "daily"
    directory.mkdir(parents=True)
    target = directory / name
    # Same size and mtime still require a content comparison, not --ignore-existing.
    old_data = bytes([data[0] ^ 1]) + data[1:]
    target.write_bytes(old_data)
    source_stat = (h.local / "daily" / name).stat()
    os.utime(target, ns=(source_stat.st_atime_ns, source_stat.st_mtime_ns))
    sidecar = directory / (name + ".sha256")
    sidecar.write_text(checksum(old_data, name))
    original_inode = target.stat().st_ino
    overrides = {}
    if failure == "transfer":
        overrides["FAKE_FAIL_MIRROR"] = "daily"
    elif failure == "checksum":
        overrides["FAKE_CORRUPT_MIRROR"] = "daily"
    result = h.run(GLHAC_BACKUP_MIRROR_DIR=str(mirror), **overrides)
    assert result.returncode == (1 if failure else 0), result.stderr
    assert h.calls("local_rsync")
    assert target.read_bytes() == (old_data if failure else data)
    assert sidecar.read_text() == checksum(old_data if failure else data, name)
    assert (target.stat().st_ino == original_inode) == bool(failure)
    assert not list(mirror.glob(".offsite-mirror.*"))
    if failure == "checksum":
        assert "미러 복사본 SHA-256" in result.stderr
    if failure is None:
        before = len(h.calls("local_rsync"))
        second = h.run(GLHAC_BACKUP_MIRROR_DIR=str(mirror))
        assert second.returncode == 0, second.stderr
        assert len(h.calls("local_rsync")) == before
    h.clean()


def test_late_mirror_transfer_failure_prevents_pruning_every_tier(harness):
    h = harness
    data = h.data()
    sunday = NOW - timedelta(days=(NOW.weekday() + 1) % 7)
    name = h.put(sunday, data)
    mirror = h.local.parent / "mirror"
    mirror.mkdir()
    for tier, count in (("daily", 16), ("weekly", 10), ("monthly", 14)):
        directory = mirror / tier
        directory.mkdir()
        for offset in range(1, count + 1):
            old = backup_name(sunday - timedelta(days=40 * offset))
            (directory / old).write_bytes(data)
            (directory / (old + ".sha256")).write_text(checksum(data, old))
    before = {p: p.read_bytes() for p in mirror.rglob("*") if p.is_file()}
    result = h.run(GLHAC_BACKUP_MIRROR_DIR=str(mirror), FAKE_FAIL_MIRROR="monthly")
    assert result.returncode == 1 and "미러 복사 실패" in result.stderr
    assert len(h.calls("local_rsync")) == 3  # daily/weekly succeeded before monthly I/O failed
    assert all(p.read_bytes() == data for p, data in before.items())
    assert (mirror / "daily" / name).exists() and (mirror / "weekly" / name).exists()
    assert not (mirror / "monthly" / name).exists()
    assert not list(mirror.glob(".offsite-mirror.*"))
    assert h.names("daily") == [name]
    h.clean()


@pytest.mark.parametrize("stage", ["mkdir", "copy", "prune"])
def test_mirror_device_change_stops_before_each_mutating_stage(harness, stage):
    h = harness
    name = h.put()
    mirror = h.local.parent / "mirror"
    mirror.mkdir()
    before = {}
    if stage == "prune":
        directory = mirror / "daily"
        directory.mkdir()
        for offset in range(1, 17):
            old = backup_name(NOW - timedelta(days=40 * offset))
            (directory / old).write_bytes(b"keep old history")
            (directory / (old + ".sha256")).write_text(checksum(b"keep old history", old))
        before = {p: p.read_bytes() for p in directory.iterdir()}
    h.install_tool("stat")
    result = h.run(GLHAC_BACKUP_MIRROR_DIR=str(mirror), GLHAC_BACKUP_NOTIFY="1",
                   FAKE_DEVICE_STAGE=stage,
                   FAKE_DEVICE_AFTER_FILE=str(mirror / "monthly" / (name + ".sha256")))
    assert result.returncode == 0, result.stderr
    assert "result=WARN" in result.stdout and "미러 장치 변경" in result.stdout
    assert h.names("daily") == [name] and h.calls("osascript")
    if stage == "mkdir":
        assert not (mirror / "daily").exists()
    if stage in ("mkdir", "copy"):
        assert not h.calls("local_rsync")
        assert not (mirror / "daily" / name).exists()
    else:
        assert h.calls("local_rsync") and (mirror / "monthly" / name).exists()
        assert all(p.read_bytes() == data for p, data in before.items())
    h.clean()


@pytest.mark.parametrize("overlap", ["same", "alias", "tier_symlink"])
def test_mirror_cannot_overwrite_local_backups_through_aliases(harness, overlap):
    h = harness
    name = h.put()
    mirror = h.local.parent / "mirror"
    if overlap == "same":
        mirror = h.local
    elif overlap == "alias":
        mirror.symlink_to(h.local, target_is_directory=True)
    else:
        mirror.mkdir()
        (mirror / "daily").symlink_to(h.local / "daily", target_is_directory=True)
    result = h.run(GLHAC_BACKUP_MIRROR_DIR=str(mirror))
    assert result.returncode == 1
    assert h.names("daily") == [name]
    assert all("-e" in call for call in h.calls("rsync"))  # no mirror operation
    h.clean()


@pytest.mark.parametrize("setting,value", [
    ("GLHAC_BACKUP_HOST", "host;touch BAD"), ("GLHAC_BACKUP_USER", "-oProxyCommand=bad"),
    ("GLHAC_BACKUP_REMOTE_DIR", "/tmp/$(touch BAD)"),
    ("GLHAC_BACKUP_REMOTE_DIR", "/tmp/../backups"),
    ("GLHAC_BACKUP_MAX_AGE_HOURS", "0"), ("GLHAC_BACKUP_KEY", ""),
    ("GLHAC_BACKUP_SSH_TIMEOUT_SECONDS", "0"),
    ("GLHAC_BACKUP_SSH_TIMEOUT_SECONDS", "1;exit"),
    ("GLHAC_BACKUP_RSYNC_TIMEOUT_SECONDS", "-1"),
    ("GLHAC_BACKUP_RSYNC_TIMEOUT_SECONDS", "1.5"),
    ("GLHAC_BACKUP_HOST", ""),
])
def test_invalid_configuration_never_reaches_ssh(harness, setting, value):
    result = harness.run(**{setting: value})
    assert result.returncode == 1
    assert not harness.calls("ssh") and not harness.calls("rsync")
    harness.clean()


@pytest.mark.parametrize("failure", ["FAKE_FAIL_SSH", "FAKE_FAIL_TRANSFER", "FAKE_FAIL_MIRROR"])
def test_transport_failures_and_cleanup(harness, failure):
    h = harness
    name = h.put()
    mirror = h.local.parent / "mirror"
    mirror.mkdir()
    result = h.run(**{failure: "1", "GLHAC_BACKUP_MIRROR_DIR": str(mirror)})
    assert result.returncode == 1
    assert h.names("daily") == ([name] if failure == "FAKE_FAIL_MIRROR" else [])
    h.clean()


@pytest.mark.parametrize("damage", ["gzip", "checksum", "missing_checksum", "size"])
def test_skipped_download_is_still_verified(harness, damage):
    h = harness
    name = h.put()
    assert h.run().returncode == 0
    path = h.local / "daily" / name
    if damage == "gzip":
        data = bytearray(path.read_bytes())
        data[-1] ^= 1
        path.write_bytes(data)  # unchanged size
    elif damage == "checksum":
        Path(str(path) + ".sha256").write_text("0" * 64 + f"  {name}\n")
    elif damage == "missing_checksum":
        Path(str(path) + ".sha256").unlink()
    else:
        path.write_bytes(b"corrupt different-size current daily")
    before = path.read_bytes()
    result = h.run()
    assert result.returncode == 1
    assert len(h.calls("rsync")) == 1
    assert path.read_bytes() == before
    if damage == "missing_checksum":
        assert not Path(str(path) + ".sha256").exists()
    h.clean()


@pytest.mark.parametrize("terminate", [False, True])
def test_lock_excludes_concurrent_run_and_signal_cleans_up(harness, terminate):
    h = harness
    h.put()
    marker = h.scratch / "hold"
    process = subprocess.Popen(["/bin/bash", str(PULL)], env={**h.env, "FAKE_HOLD": str(marker)},
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        deadline = time.monotonic() + 10
        while not marker.exists():
            assert process.poll() is None, process.communicate()
            assert time.monotonic() < deadline
            time.sleep(0.02)
        pid_file = h.local / ".offsite-backup.lock" / "pid"
        assert pid_file.read_text().strip() == str(process.pid)
        second = h.run()
        assert second.returncode == 1 and "락" in second.stderr
        assert pid_file.exists() and len(h.calls("ssh")) == 1
        if terminate:
            process.send_signal(signal.SIGTERM)
        marker.with_suffix(".release").touch()
        stdout, stderr = process.communicate(timeout=20)
        assert process.returncode == (1 if terminate else 0), (stdout, stderr)
        h.clean()
    finally:
        marker.with_suffix(".release").touch()
        if process.poll() is None:
            process.terminate()
            process.communicate(timeout=20)


def test_restore_explicit_and_latest_leave_originals_unchanged(harness):
    h = harness
    name = h.put()
    assert h.run().returncode == 0
    backup = h.local / "daily" / name
    before = (backup.read_bytes(), backup.stat().st_mtime_ns)
    for args in ((), (backup,)):
        result = h.run(RESTORE, args)
        assert result.returncode == 0, result.stderr
        assert "integrity_check=ok\ncase_application=3\ndocument_asset=2" in result.stdout
        assert str(backup) in result.stdout
        assert before == (backup.read_bytes(), backup.stat().st_mtime_ns)
    assert len(h.calls("rsync")) == 1
    h.clean()


def test_restore_integrity_failure_removes_temporary_database(harness):
    h = harness
    backup = h.remote / h.put(data=h.data(broken_integrity=True))
    before = backup.read_bytes()
    result = h.run(RESTORE, (backup,))
    assert result.returncode == 1 and "integrity_check" in result.stderr
    assert backup.read_bytes() == before
    assert not h.calls("ssh")
    h.clean()


def test_launchd_example_schedule_and_absolute_paths():
    with (ROOT / "scripts/launchd/com.glhac.offsite-backup.plist.example").open("rb") as stream:
        config = plistlib.load(stream)
    assert config["Label"] == "com.glhac.offsite-backup"
    assert config["StartCalendarInterval"] == {"Hour": 4, "Minute": 30}
    assert config["ProgramArguments"] == ["/bin/bash", "<REPO_PATH>/scripts/offsite_backup_pull.sh"]
    assert config["EnvironmentVariables"]["GLHAC_BACKUP_HOST"] == "<HOST>"
    assert config["EnvironmentVariables"]["GLHAC_BACKUP_KEY"] == "<KEY_PATH>"
    for key in ("StandardOutPath", "StandardErrorPath"):
        assert config[key].startswith("<LOCAL_DIR>/")
