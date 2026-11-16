"""Build/sign real macOS launchers; execute only embedded, fake backup scripts."""
import hashlib
import os
from pathlib import Path
import plistlib
import re
import shutil
import signal
import subprocess
import sys
import time

import pytest


ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / "scripts/launcher/build_launcher.sh"
PULL = ROOT / "scripts/offsite_backup_pull.sh"
FIXED_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"
ENV = {"PATH": FIXED_PATH}
pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="macOS launcher required")


@pytest.fixture(autouse=True, scope="module")
def require_compiler():
    if shutil.which("cc", path=FIXED_PATH) is None:
        pytest.skip("cc is not available")


def invoke_build(binary, script=None, *, env=None):
    args = ["/bin/bash", str(BUILD), str(binary)]
    if script is not None:
        args.append(str(script))
    return subprocess.run(
        args, env={**ENV, "TMPDIR": str(binary.parent) + "/", **(env or {})}, cwd=binary.parent,
        capture_output=True, text=True, timeout=60,
    )


@pytest.fixture
def build_launcher(tmp_path):
    source_dir = tmp_path / "source 'quoted'"
    source_dir.mkdir()
    directory = tmp_path / "installed launcher 'quoted'"
    directory.mkdir(mode=0o700)

    def build(body):
        script = source_dir / "offsite_backup_pull.sh"
        if isinstance(body, bytes):
            script.write_bytes(body)
        else:
            script.write_text("#!/bin/bash\n" + body + "\n")
        binary = directory / "glhac-offsite-backup"
        result = invoke_build(binary, script)
        assert result.returncode == 0, result.stdout + result.stderr
        return binary, script, result

    return build


def run(binary, *args, env=None, cwd=None):
    return subprocess.run(
        [str(binary), *args], env={**ENV, **(env or {})}, cwd=cwd,
        capture_output=True, text=True, timeout=10,
    )


@pytest.mark.parametrize("exit_code", [0, 3, 42])
def test_embedded_script_and_exit_status(build_launcher, tmp_path, exit_code):
    binary, _, _ = build_launcher(
        f'printf "%s\\n%s\\n내장 스크립트\\n" "$0" "$#"\nexit {exit_code}'
    )
    result = run(binary, cwd=tmp_path)
    assert result.returncode == exit_code, result.stderr
    assert result.stdout == "glhac-offsite-backup\n0\n내장 스크립트\n"


@pytest.mark.parametrize("kind", ["missing", "modified", "symlink", "directory", "fifo"])
def test_source_and_adjacent_files_cannot_change_execution(build_launcher, tmp_path, kind):
    binary, source, _ = build_launcher('printf "embedded only\\n"')
    malicious = '#!/bin/bash\nprintf "tampered\\n"\nexit 99\n'
    source.write_text(malicious)
    changed = run(binary)
    assert changed.returncode == 0, changed.stderr
    assert changed.stdout == "embedded only\n"
    source.unlink()
    adjacent = binary.with_name("offsite_backup_pull.sh")
    decoy = tmp_path / adjacent.name
    decoy.write_text(malicious)
    if kind == "modified":
        adjacent.write_text(malicious)
        adjacent.chmod(0o666)
    elif kind == "symlink":
        adjacent.symlink_to(decoy)
    elif kind == "directory":
        adjacent.mkdir()
    elif kind == "fifo":
        os.mkfifo(adjacent, 0o600)
    result = run(binary, cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    assert result.stdout == "embedded only\n"


def test_launcher_symlink_uses_embedded_script(build_launcher, tmp_path):
    binary, source, _ = build_launcher('printf "embedded via alias\\n"')
    source.unlink()
    alias = tmp_path / "launcher alias"
    alias.symlink_to(binary)
    result = run(alias, cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    assert result.stdout == "embedded via alias\n"


def test_only_allowed_environment_is_passed_verbatim(build_launcher):
    allowed = {
        "HOME": "/fake home 'quoted'",
        "TZ": "Asia/Seoul",
        "LANG": "C",
        "LC_ALL": "C",
        "USER": "test-user",
        "LOGNAME": "test-logname",
        "TMPDIR": "/fake tmp/",
        "GLHAC_BACKUP_MIRROR_DIR": "/Volumes/fake mirror 'quoted'",
        "GLHAC_BACKUP_VALUE": "unchanged ; $(never-run) & = value\nnext line\n",
        "GLHAC_BACKUP_EMPTY": "",
    }
    excluded = {
        "LAUNCHER_TEST_VALUE": "secret",
        "HOME_EXTRA": "secret",
        "GLHAC_BACKUP": "secret",
        "GLHAC_BACKUPX_VALUE": "secret",
        "LC_CTYPE": "C",
        "DYLD_LIBRARY_PATH": "/fake dyld",
        "DYLD_INSERT_LIBRARIES": "/nonexistent/glhac-test.dylib",
        "LD_LIBRARY_PATH": "/fake ld",
        "LD_PRELOAD": "/nonexistent/glhac-test.so",
    }
    body = "\n".join(f'''printf '%s\\0' "${{{name}-unset}}"''' for name in (*allowed, *excluded))
    binary, _, _ = build_launcher(body)
    result = run(binary, env={**allowed, **excluded})
    assert result.returncode == 0, result.stderr
    assert result.stdout.split("\0") == [*allowed.values(), *(["unset"] * len(excluded)), ""]


def test_bash_startup_and_function_injection_is_blocked(build_launcher, tmp_path):
    marker = tmp_path / "injection ran"
    startup = tmp_path / "injected startup.sh"
    startup.write_text('printf injected > "$GLHAC_BACKUP_INJECTED"\n')
    binary, _, _ = build_launcher(r'''
printf '%s\n%s\n' "${BASH_ENV-unset}" "${ENV-unset}"
if declare -F x >/dev/null; then x; exit 90; fi
[[ $- != *x* ]] || exit 91
if shopt -q failglob; then exit 92; fi
printf 'clean\n'
''')
    result = run(binary, env={
        "BASH_ENV": str(startup),
        "ENV": str(startup),
        "SHELLOPTS": "xtrace",
        "BASHOPTS": "failglob",
        "BASH_FUNC_x%%": '() { printf injected > "$GLHAC_BACKUP_INJECTED"; }',
        "BASH_FUNC_x()": '() { printf injected > "$GLHAC_BACKUP_INJECTED"; }',
        "GLHAC_BACKUP_INJECTED": str(marker),
    })
    assert result.returncode == 0, result.stderr
    assert result.stdout == "unset\nunset\nclean\n"
    assert not marker.exists()
    assert not result.stderr


def test_path_is_fixed_before_command_lookup(build_launcher, tmp_path):
    fake_tool = tmp_path / "basename"
    fake_tool.write_text('#!/bin/bash\nprintf "injected\\n"\n')
    fake_tool.chmod(0o700)
    binary, _, _ = build_launcher('printf "%s\\n" "$PATH"\nbasename /test/value')
    result = run(binary, env={"PATH": str(tmp_path)})
    assert result.returncode == 0, result.stderr
    assert result.stdout == FIXED_PATH + "\nvalue\n"


@pytest.mark.parametrize("args", [("unexpected",), ("--help",), ("--version",), ("one", "two")])
def test_arguments_are_rejected(build_launcher, args):
    binary, _, _ = build_launcher('printf "must not run\\n"')
    result = run(binary, *args)
    assert result.returncode == 2
    assert "Usage:" in result.stderr
    assert not result.stdout


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGINT, signal.SIGHUP])
def test_signal_is_forwarded_and_child_is_waited_for(build_launcher, tmp_path, signum):
    ready = tmp_path / "ready"
    signal_name = signal.Signals(signum).name.removeprefix("SIG")
    binary, _, _ = build_launcher(f'''
trap 'printf "forwarded {signal_name}\\n"; exit 43' {signal_name}
printf '%s %s\\n' "$$" "$PPID" > "$GLHAC_BACKUP_READY"
while :; do read -r line || :; done
''')
    process = subprocess.Popen(
        [str(binary)], env={**ENV, "GLHAC_BACKUP_READY": str(ready)},
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 5
        while not ready.exists() or not ready.read_text().endswith("\n"):
            assert process.poll() is None, "launcher exited before the child was ready"
            assert time.monotonic() < deadline, "child did not become ready"
            time.sleep(0.01)
        child_pid, parent_pid = map(int, ready.read_text().split())
        assert child_pid != process.pid
        assert parent_pid == process.pid  # The signed launcher was not exec'd away.
        process.send_signal(signum)  # Only the launcher receives the test signal.
        stdout, stderr = process.communicate(timeout=5)
        assert process.returncode == 43, stderr
        assert stdout == f"forwarded {signal_name}\n"
        with pytest.raises(ProcessLookupError):
            os.kill(child_pid, 0)
    finally:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.communicate(timeout=5)


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGHUP, signal.SIGKILL])
def test_child_signal_status(build_launcher, signum):
    binary, _, _ = build_launcher(f'kill -{int(signum)} "$$"')
    result = run(binary)
    assert result.returncode == 128 + signum, result.stderr


def test_real_signature_has_runtime_and_no_entitlements(build_launcher):
    binary, _, _ = build_launcher("exit 0")
    result = subprocess.run(
        ["/usr/bin/codesign", "-dv", str(binary)], capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, result.stderr
    flags = re.search(r"flags=(0x[\da-fA-F]+)\(([^)]*)\)", result.stderr)
    assert flags is not None, result.stderr
    assert int(flags[1], 16) & 0x10000
    assert "runtime" in flags[2].split(",")
    assert "Identifier=com.glhac.offsite-backup.launcher" in result.stderr
    entitlements = subprocess.run(
        ["/usr/bin/codesign", "-d", "--entitlements", ":-", str(binary)],
        capture_output=True, timeout=10,
    )
    assert entitlements.returncode == 0, entitlements.stderr
    assert not entitlements.stdout.strip() or plistlib.loads(entitlements.stdout) == {}
    verified = subprocess.run(
        ["/usr/bin/codesign", "--verify", "--strict", str(binary)],
        capture_output=True, text=True, timeout=10,
    )
    assert verified.returncode == 0, verified.stderr


def test_build_rejects_signature_without_runtime(tmp_path):
    tools_dir = tmp_path / "build tools"
    tools_dir.mkdir()
    codesign = tools_dir / "codesign"
    # Produce a genuine ad-hoc signature without runtime, then inspect it with
    # the real codesign. The build must reject it before publishing output.
    codesign.write_text('''#!/bin/bash
if [[ $1 == --force ]]; then
    exec /usr/bin/codesign --force --sign - --identifier com.glhac.offsite-backup.launcher "${!#}"
fi
exec /usr/bin/codesign "$@"
''')
    codesign.chmod(0o700)
    script = tmp_path / "fake.sh"
    script.write_text("exit 0\n")
    binary = tmp_path / "launcher"
    result = invoke_build(binary, script, env={"PATH": str(tools_dir) + ":" + FIXED_PATH})
    assert result.returncode != 0
    assert "Hardened Runtime" in result.stderr
    assert not binary.exists()
    assert not binary.with_suffix(".sha256").exists()


@pytest.mark.parametrize(
    "content", [b"", b"exit 0\0\nexit 99\n", b"#" * 65537], ids=["empty", "nul", "too-large"],
)
def test_build_rejects_unusable_script_bytes(tmp_path, content):
    script = tmp_path / "fake.sh"
    script.write_bytes(content)
    binary = tmp_path / "launcher"
    result = invoke_build(binary, script)
    assert result.returncode != 0
    expected = "NUL" if b"\0" in content else "65536"
    assert expected in result.stderr
    assert not binary.exists()
    assert not binary.with_suffix(".sha256").exists()


@pytest.mark.parametrize("size", [37, 65536])
def test_script_bytes_and_sha256_are_preserved(build_launcher, size):
    content = b"printf 'exact bytes\\n'\n#"
    content += b"x" * (size - len(content))  # No final newline: hash these exact bytes.
    binary, _, result = build_launcher(content)
    digest = hashlib.sha256(content).hexdigest()
    assert Path(str(binary) + ".sha256").read_text() == digest + "\n"
    assert f"{size} bytes" in result.stdout
    assert digest in result.stdout
    assert content + b"\0" in binary.read_bytes()
    executed = run(binary)
    assert executed.returncode == 0, executed.stderr
    assert executed.stdout == "exact bytes\n"


def test_script_plus_environment_must_fit_arg_max(build_launcher):
    body = b"printf 'must not run\\n'\n#"
    binary, _, _ = build_launcher(body + b"x" * (65536 - len(body)))
    # The launcher itself fits, but adding the embedded script to bash's argv
    # would exceed ARG_MAX. Keep a margin for the launcher's own argv/pointers.
    budget = os.sysconf("SC_ARG_MAX") - 32768
    env = {}
    while budget > 16384:
        key = f"GLHAC_BACKUP_PAD_{len(env)}"
        env[key] = "x" * 16384
        budget -= len(key) + 1 + 16384 + 1
    result = run(binary, env=env)
    assert result.returncode == 2
    assert "ARG_MAX" in result.stderr
    assert not result.stdout


def test_default_build_embeds_real_script_without_running_it(tmp_path):
    binary = tmp_path / "default launcher"
    result = invoke_build(binary)
    assert result.returncode == 0, result.stdout + result.stderr
    content = PULL.read_bytes()
    assert content + b"\0" in binary.read_bytes()
    assert Path(str(binary) + ".sha256").read_text() == hashlib.sha256(content).hexdigest() + "\n"


@pytest.mark.parametrize("args", [[], [""], ["output", ""], ["one", "two", "three"]])
def test_build_usage(args, tmp_path):
    result = subprocess.run(
        ["/bin/bash", str(BUILD), *args], cwd=tmp_path, env=ENV,
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 2
    assert "Usage:" in result.stderr
