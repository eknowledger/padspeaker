"""Deployment tests: deploy/install.sh against a fake ssh, and the Batocera service script deploy/padspeaker.

The fake ``ssh`` on PATH never connects anywhere. It records every command it is given, rewrites the Batocera path
``/userdata`` into a sandbox folder and runs the command locally with ``sh`` and a *restricted* PATH (a folder of
symlinks to the few tools the real box has, plus stubs for ``batocera-services``, ``pactl`` and ``parec``). So the real
remote scripts are executed end to end, on a copy of the Batocera layout that holds seeded "bystander" files which must
never change. The service script is also tested on its own with a fake python.
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
BASH = shutil.which("bash") or "/bin/bash"
SERVICE_SRC = REPO / "deploy" / "padspeaker"
SEPARATOR = "=====STUB-COMMAND"

STUB_SSH = r'''#!/bin/bash
# Fake ssh for tests: records the command, runs it in a sandbox. Never reaches a network.
: "${STUB_SANDBOX:?}" "${STUB_LOG:?}" "${STUB_REMOTE_PATH:?}"
while [ $# -gt 0 ]; do
    case $1 in
        -o|-p|-i|-l) shift 2 ;;
        -*) shift ;;
        *) break ;;
    esac
done
host=$1; shift
cmd="$*"
upload=""
if [ "$cmd" = "sh -s" ]; then
    cmd=$(cat)
else
    case $cmd in *"cat >"*) upload=1 ;; esac
fi
{ echo "$STUB_SEPARATOR host=$host"; printf '%s\n' "$cmd"; } >> "$STUB_LOG"
rewritten=$(printf '%s' "$cmd" | sed -e "s#/userdata#$STUB_SANDBOX/userdata#g")
header='PATH="$STUB_REMOTE_PATH"
df() { if [ -n "${STUB_FREE_MB:-}" ]; then printf "Filesystem 1M-blocks Used Available Capacity Mounted\nx 9 9 %s 1%% /\n" "$STUB_FREE_MB"; else command df "$@"; fi; }
sha256sum() { shasum -a 256 "$@"; }
'
if [ -n "$upload" ]; then
    cat > "$STUB_SANDBOX/.upload.tgz"
    cp "$STUB_SANDBOX/.upload.tgz" "$STUB_SANDBOX/captured.tar.gz"
    if [ -n "${STUB_CORRUPT_UPLOAD:-}" ]; then printf 'garbage' >> "$STUB_SANDBOX/.upload.tgz"; fi
    sh -c "$header$rewritten" < "$STUB_SANDBOX/.upload.tgz"
else
    sh -c "$header$rewritten" < /dev/null
fi
'''

STUB_SCP = '#!/bin/bash\necho "scp must not be used" >&2\nexit 97\n'

# python3 on the fake box: libsbc check is answered by STUB_SBC; the daemon itself is faked when STUB_FAKE_DAEMON is set
STUB_PYTHON = r'''#!/bin/bash
case "$*" in
    *libsbc.so.1*)
        if [ "${STUB_SBC:-ok}" = ok ]; then exit 0; fi
        echo "OSError: libsbc.so.1: cannot open shared object file" >&2
        exit 1 ;;
    *"-m padspeaker run"*)
        if [ -n "${STUB_FAKE_DAEMON:-}" ]; then
            echo "FAKE DAEMON ARGS: $*"
            trap 'exit 0' TERM
            while :; do sleep 0.1; done
        fi ;;
esac
exec "@REAL@" "$@"
'''

STUB_PACTL = '#!/bin/sh\n[ "${STUB_PACTL_FAIL:-}" = 1 ] && exit 1\nexit 0\n'
STUB_PAREC = '#!/bin/sh\nexit 0\n'

STUB_BATOCERA_SERVICES = r'''#!/bin/sh
conf="$STUB_SANDBOX/userdata/system/batocera.conf"
svcdir="$STUB_SANDBOX/userdata/system/services"
echo "$*" >> "$STUB_SANDBOX/.batocera-services.log"
cur() { sed -n 's/^system.services=//p' "$conf"; }
put() { awk -v v="$1" 'BEGIN{d=0} /^system.services=/{print "system.services=" v; d=1; next} {print} END{if(!d) print "system.services=" v}' "$conf" > "$conf.new" && mv "$conf.new" "$conf"; }
case $1 in
    enable)  case " $(cur) " in *" $2 "*) ;; *) put "$(cur) $2" ;; esac ;;
    disable) put "$(cur | tr ' ' '\n' | grep -vx "$2" | tr '\n' ' ' | sed 's/ *$//')" ;;
    start|stop|status) sh "$svcdir/$2" "$1" ;;
    restart) sh "$svcdir/$2" stop; sleep 0.2; sh "$svcdir/$2" start ;;
    *) exit 1 ;;
esac
'''

# what the real box has (GNU userland); anything not in this folder is "not installed" for the scripts under test
REMOTE_TOOLS = ("sh", "bash", "awk", "basename", "cat", "chmod", "cmp", "cp", "cut", "date", "dirname", "env", "find",
                "grep", "gzip", "head", "ls", "mkdir", "mv", "ps", "rm", "rmdir", "sed", "sleep", "sort", "tail", "tar",
                "tee", "touch", "tr", "wc", "shasum", "df", "kill", "pgrep", "pkill", "timeout", "readlink", "id", "uname",
                "true", "false", "xargs", "mktemp")


def _stub(path: Path, body: str) -> Path:
    path.write_text(body)
    path.chmod(0o755)
    return path


class Env:
    def __init__(self, tmp: Path, repo: Path):
        self.tmp = tmp
        self.repo = repo
        self.sandbox = tmp / "box"
        self.lbin = tmp / "localbin"
        self.rbin = tmp / "remotebin"
        self.log = tmp / "ssh.log"
        self.userdata = self.sandbox / "userdata"
        self.system = self.userdata / "system"
        self.dest = self.system / "padspeaker"
        self.services = self.system / "services"
        self.logs = self.system / "logs"
        self.conf = self.system / "batocera.conf"
        self.pidfile = self.sandbox / "run" / "padspeaker.pid"
        self.service = self.services / "padspeaker"
        # the install this project had before it was renamed (ds4-bt-speaker); only the migration may touch these
        self.old_dest = self.system / "ds4-bt-speaker"
        self.old_service = self.services / "ds4_bt_speaker"
        self.old_running = self.sandbox / "run" / "old-service-running"
        for d in (self.services, self.logs, self.sandbox / "run", self.lbin, self.rbin):
            d.mkdir(parents=True)
        _stub(self.lbin / "ssh", STUB_SSH)
        _stub(self.lbin / "scp", STUB_SCP)
        for tool in REMOTE_TOOLS:
            found = shutil.which(tool)
            if found:
                (self.rbin / tool).symlink_to(found)
        _stub(self.rbin / "python3", STUB_PYTHON.replace("@REAL@", sys.executable))
        _stub(self.rbin / "pactl", STUB_PACTL)
        _stub(self.rbin / "parec", STUB_PAREC)
        _stub(self.rbin / "batocera-services", STUB_BATOCERA_SERVICES)
        self.log.write_text("")
        self.seed_bystanders()

    # ---- seeding and snapshots
    def seed_bystanders(self):
        """Files that belong to the box or the user. Nothing here may ever change."""
        (self.services / "symlink_manager").write_text("#!/bin/bash\necho hi\n")
        (self.services / "custom_service_handler").write_text("#!/bin/bash\necho handler\n")
        self.conf.write_text("system.hostname=BATOCERA\nsystem.services=symlink_manager custom_service_handler\naudio.volume=80\n")
        for rel in ("roms/switch/game.nsp", "system/configs/emulationstation/es_settings.cfg", "system/custom.sh",
                    "system/logs/es_launch_stdout.log", "system/padspeaker-notes.txt", "system/ds4-bt-speaker-notes.txt", "saves/x.sav"):
            p = self.userdata / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("bystander " + rel)

    def bystanders(self, include_conf=True):
        """Hashes of every file in the sandbox that is not ours (our app, our service script, our log, stub artefacts)."""
        snap = {}
        for p in sorted(self.sandbox.rglob("*")):
            rel = p.relative_to(self.sandbox).as_posix()
            if not p.is_file() or p.is_symlink():
                continue
            if (rel.startswith(("userdata/system/padspeaker/", "userdata/system/ds4-bt-speaker/",
                                "userdata/system/ds4-bt-speaker.old-")) or rel == "userdata/system/services/padspeaker"
                    or rel == "userdata/system/services/ds4_bt_speaker"
                    or rel.startswith("userdata/system/logs/padspeaker.log") or rel.startswith("run/")
                    or rel in (".upload.tgz", "captured.tar.gz", ".batocera-services.log")):
                continue
            if rel == "userdata/system/batocera.conf" and not include_conf:
                continue
            snap[rel] = hashlib.sha256(p.read_bytes()).hexdigest()
        return snap

    OLD_SERVICE_SCRIPT = """#!/bin/sh
# stand-in for the service script of the pre-rename release: it only keeps a marker file
marker={marker}
case "$1" in
    start) touch "$marker"; echo started ;;
    stop) {stop} ;;
    status) if [ -f "$marker" ]; then echo started; exit 0; fi; echo stopped; exit 3 ;;
esac
"""

    def seed_old_install(self, running=True, enabled=True, config='{"volume_cap": 42}', stubborn=False):
        """An install made under the old name: folder, config, service script, log files, entry in system.services."""
        files = {"ds4speaker/__init__.py": "__version__ = '0.0.9'\n", "ds4speaker/__main__.py": "# old\n",
                 "ds4speaker.previous/__init__.py": "# older\n", "tools/box_selftest.py": "# old tool\n",
                 "service/ds4_bt_speaker": "#!/bin/sh\n", "VERSION": "0.0.9 (git abc) built long ago\n",
                 "MANIFEST.sha256": "00  VERSION\n", "notes-by-the-owner.txt": "mine\n"}
        if config is not None:
            files["config.json"] = config
        for rel, text in files.items():
            path = self.old_dest / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        stop = ":" if stubborn else 'rm -f "$marker"; echo stopped'
        _stub(self.old_service, self.OLD_SERVICE_SCRIPT.format(marker=self.old_running, stop=stop))
        (self.logs / "ds4-bt-speaker.log").write_text("old log\n")
        (self.logs / "ds4-bt-speaker.log.1").write_text("older log\n")
        if running:
            self.old_running.write_text("")
        if enabled:
            self.conf.write_text(self.conf.read_text().replace("custom_service_handler", "custom_service_handler ds4_bt_speaker"))

    def backups(self):
        return sorted(self.system.glob("ds4-bt-speaker.old-*"))

    def tree(self, root):
        """{relative path: sha256 or None for a folder} of everything below root (root itself included as '.')."""
        out = {".": None}
        for p in sorted(Path(root).rglob("*")):
            out[p.relative_to(root).as_posix()] = None if p.is_dir() else hashlib.sha256(p.read_bytes()).hexdigest()
        return out

    def seed_user_data(self):
        """User data inside the app folder (config) and the log; they survive an uninstall without --purge."""
        (self.dest).mkdir(parents=True, exist_ok=True)
        (self.dest / "config.json").write_text('{"volume_cap": 60}')
        (self.logs / "padspeaker.log").write_text("old log\n")
        (self.logs / "padspeaker.log.1").write_text("older log\n")
        (self.logs / "padspeaker.log.console").write_text("console\n")
        return self.user_snapshot()

    def user_snapshot(self):
        out = {}
        for p in (self.dest / "config.json", self.logs / "padspeaker.log", self.logs / "padspeaker.log.1"):
            out[p.name] = p.read_text() if p.exists() else None
        return out

    # ---- running things
    @property
    def install_sh(self) -> Path:
        return self.repo / "deploy" / "install.sh"

    def env(self, **extra):
        e = dict(os.environ)
        e.update(PATH=f"{self.lbin}:{e['PATH']}", STUB_SANDBOX=str(self.sandbox), STUB_LOG=str(self.log),
                 STUB_SEPARATOR=SEPARATOR, STUB_REMOTE_PATH=str(self.rbin), TMPDIR=str(self.tmp),
                 PADSPEAKER_HOME=str(self.dest), PADSPEAKER_LOG=str(self.logs / "padspeaker.log"), PADSPEAKER_PIDFILE=str(self.pidfile),
                 PADSPEAKER_START_TICKS="2", PADSPEAKER_STOP_TICKS="12")
        for k in ("PADSPEAKER_HOST", "PADSPEAKER_PYTHON", "PADSPEAKER_CONFIG", "STUB_FREE_MB", "STUB_SBC", "STUB_FAKE_DAEMON"):
            e.pop(k, None)
        e.update(extra)
        return e

    def run(self, *args, **extra):
        return subprocess.run([BASH, str(self.install_sh), *args], capture_output=True, text=True, timeout=180,
                              env=self.env(**extra), cwd=str(self.tmp))

    def commands(self):
        """[(host, script text)] of every ssh call the stub saw."""
        out = []
        for chunk in self.log.read_text().split(SEPARATOR)[1:]:
            head, _, body = chunk.partition("\n")
            out.append((head.strip().replace("host=", "", 1), body))
        return out

    def daemon_pid(self):
        try:
            return int(self.pidfile.read_text().strip())
        except (OSError, ValueError):
            return None

    def kill_daemon(self):
        pid = self.daemon_pid()
        if pid:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass


def _copy_repo(dst: Path) -> Path:
    """A private copy of the parts install.sh packages, so tests can add junk without touching the real tree.

    While the package is still being written, a missing __main__.py / __version__ is filled in *in the copy only*."""
    for part in ("padspeaker", "tools", "deploy"):
        shutil.copytree(REPO / part, dst / part, ignore=shutil.ignore_patterns(".DS_Store", "__pycache__"), symlinks=False)
    init = dst / "padspeaker" / "__init__.py"
    if "__version__" not in init.read_text():
        init.write_text(init.read_text() + '\n__version__ = "0.0.0-test"\n')
    main = dst / "padspeaker" / "__main__.py"
    if not main.exists():
        main.write_text("import sys\nfrom . import __version__\nif '--version' in sys.argv:\n    print('padspeaker', __version__)\n")
    return dst


@pytest.fixture
def box(tmp_path):
    env = Env(tmp_path, _copy_repo(tmp_path / "repo"))
    yield env
    env.kill_daemon()


def wait_dead(pid, seconds=5.0):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        out = subprocess.run(["ps", "-p", str(pid), "-o", "stat="], capture_output=True, text=True).stdout.strip()
        if not out or out.startswith("Z"):
            return True
        time.sleep(0.1)
    return False


def installed(box, *args, **extra):
    r = box.run("alice@box.local", *args, **extra)
    assert r.returncode == 0, r.stdout + r.stderr
    return r


# ---------------------------------------------------------------- syntax and file content

def test_scripts_pass_syntax_check_and_are_executable():
    for rel in ("deploy/install.sh", "deploy/padspeaker"):
        r = subprocess.run([BASH, "-n", str(REPO / rel)], capture_output=True, text=True)
        assert r.returncode == 0, f"{rel}: {r.stderr}"
        assert (REPO / rel).stat().st_mode & stat.S_IXUSR, f"{rel} must be executable"
    r = subprocess.run(["sh", "-n", str(SERVICE_SRC)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    dash = shutil.which("dash")
    if dash:
        assert subprocess.run([dash, "-n", str(SERVICE_SRC)], capture_output=True).returncode == 0


def test_install_script_avoids_bash4_features():
    text = (REPO / "deploy/install.sh").read_text()
    code = "\n".join(l for l in text.splitlines() if not l.strip().startswith("#"))
    for bad in ("declare -A", "mapfile", "readarray", "coproc", "${!", ",,}", "^^}", "&>>", "|&"):
        assert bad not in code, f"macOS bash 3.2 does not support {bad!r}"


def test_service_script_is_plain_posix_sh():
    """batocera-services runs it with `bash script start`, boot runs it directly: it must work under both, and dash."""
    lines = SERVICE_SRC.read_text().splitlines()
    assert lines[0] == "#!/bin/sh"
    code = "\n".join(l for l in lines if not l.strip().startswith("#"))
    for bad in ("[[", "declare ", "mapfile", "source ", "<(", "function ", "${!", "&>", "|&", "$'"):
        assert bad not in code, f"bashism {bad!r}"


def test_service_name_is_valid_for_batocera():
    assert re.match(r"^[_[:alpha:]][_[:alpha:][:digit:]]*$".replace("[:alpha:]", "A-Za-z").replace("[:digit:]", "0-9"),
                    SERVICE_SRC.name)


def test_remote_scripts_have_valid_posix_sh_syntax(box):
    r = box.run("--dry-run", "--enable")
    assert r.returncode == 0, r.stderr
    scripts = []
    cur = None
    for line in r.stdout.splitlines():
        if line.startswith("[dry-run] would run this"):
            cur = []
            scripts.append(cur)
        elif line.startswith("==>"):
            cur = None
        elif cur is not None and line.startswith("    | "):
            cur.append(line[6:])
    assert len(scripts) >= 5
    prelude = scripts[0][:scripts[0].index("set -eu")]
    for body in scripts:
        text = "\n".join((prelude if body is not scripts[0] else []) + body) + "\n"
        r = subprocess.run(["sh", "-n"], input=text, capture_output=True, text=True)
        assert r.returncode == 0, r.stderr + text
    r = box.run("--dry-run", "--uninstall", "--purge")
    assert r.returncode == 0, r.stderr


# ---------------------------------------------------------------- the service script on its own

FAKE_PYTHON = '''#!/bin/bash
echo "ARGS: $*"
echo "XDG_RUNTIME_DIR=$XDG_RUNTIME_DIR"
echo "PYTHONPATH=$PYTHONPATH"
echo "CWD=$(pwd)"
{trap}
while :; do sleep 0.1; done
'''


class Svc:
    def __init__(self, tmp: Path, shell="sh"):
        self.tmp = tmp
        self.shell = shell
        self.home = tmp / "app"
        (self.home / "padspeaker").mkdir(parents=True)
        self.log = tmp / "logs" / "padspeaker.log"
        self.pidfile = tmp / "run" / "padspeaker.pid"
        self.python = self.make_python("trap 'exit 0' TERM")

    def make_python(self, trap="trap 'exit 0' TERM", name="fakepython", body=None):
        return _stub(self.tmp / name, body or FAKE_PYTHON.replace("{trap}", trap))

    def run(self, action, python=None, **extra):
        env = dict(os.environ)
        env.pop("XDG_RUNTIME_DIR", None)
        env.update(PADSPEAKER_HOME=str(self.home), PADSPEAKER_LOG=str(self.log), PADSPEAKER_PIDFILE=str(self.pidfile),
                   PADSPEAKER_PYTHON=str(python or self.python), PADSPEAKER_START_TICKS="2", PADSPEAKER_STOP_TICKS="8")
        env.pop("PADSPEAKER_CONFIG", None)
        env.update(extra)
        return subprocess.run([self.shell, str(SERVICE_SRC), *([action] if action else [])], capture_output=True,
                              text=True, env=env, timeout=60)

    def pid(self):
        try:
            return int(self.pidfile.read_text().strip())
        except (OSError, ValueError):
            return None

    def alive(self, pid):
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        # a zombie counts as dead
        r = subprocess.run(["ps", "-p", str(pid), "-o", "stat="], capture_output=True, text=True)
        return bool(r.stdout.strip()) and not r.stdout.strip().startswith("Z")

    def text(self):
        """What the daemon printed (its console output is kept in LOG.console)."""
        console = Path(str(self.log) + ".console")
        return console.read_text() if console.exists() else ""


@pytest.fixture(params=["sh", "bash"])
def svc(tmp_path, request):
    s = Svc(tmp_path, request.param)
    yield s
    pid = s.pid()
    if pid:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass


def test_service_start_launches_the_daemon_and_stop_ends_it(svc):
    r = svc.run("start")
    assert r.returncode == 0, r.stdout + r.stderr
    pid = svc.pid()
    assert pid and svc.alive(pid)
    text = svc.text()
    assert f"ARGS: -m padspeaker run --log-file {svc.log}" in text
    assert "XDG_RUNTIME_DIR=/var/run" in text
    assert re.search(rf"PYTHONPATH={re.escape(str(svc.home))}(:|\n)", text)
    assert f"CWD={svc.home}" in text or f"CWD={os.path.realpath(svc.home)}" in text
    r = svc.run("status")
    assert r.returncode == 0 and "started" in r.stdout
    r = svc.run("stop")
    assert r.returncode == 0, r.stdout + r.stderr
    assert not svc.alive(pid) and not svc.pidfile.exists()
    r = svc.run("status")
    assert r.returncode == 3 and "stopped" in r.stdout


def test_service_start_is_idempotent(svc):
    assert svc.run("start").returncode == 0
    pid = svc.pid()
    r = svc.run("start")
    assert r.returncode == 0 and "already running" in r.stdout
    assert svc.pid() == pid and svc.alive(pid)
    assert svc.text().count("ARGS:") == 1                       # no second daemon was launched


def test_service_restart_gives_a_new_process(svc):
    svc.run("start")
    old = svc.pid()
    r = svc.run("restart")
    assert r.returncode == 0, r.stdout + r.stderr
    new = svc.pid()
    assert new and new != old and svc.alive(new) and not svc.alive(old)


def test_service_stop_when_not_running_is_harmless(svc):
    r = svc.run("stop")
    assert r.returncode == 0, r.stdout + r.stderr
    assert svc.run("status").returncode == 3


def test_service_ignores_a_stale_pid_file(svc):
    svc.pidfile.parent.mkdir(parents=True)
    svc.pidfile.write_text("999999\n")
    assert svc.run("status").returncode == 3
    r = svc.run("start")
    assert r.returncode == 0, r.stdout + r.stderr
    assert svc.pid() != 999999 and svc.alive(svc.pid())


def test_service_never_kills_a_foreign_process_with_a_reused_pid(svc):
    foreign = subprocess.Popen(["sleep", "30"])
    try:
        svc.pidfile.parent.mkdir(parents=True)
        svc.pidfile.write_text(f"{foreign.pid}\n")
        assert svc.run("status").returncode == 3              # not ours
        assert svc.run("stop").returncode == 0
        assert foreign.poll() is None                        # still alive
        assert not svc.pidfile.exists()
        svc.pidfile.write_text(f"{foreign.pid}\n")
        r = svc.run("start")
        assert r.returncode == 0 and svc.pid() != foreign.pid and foreign.poll() is None
    finally:
        foreign.kill()
        foreign.wait()


def test_service_start_reports_a_daemon_that_dies_at_once(svc):
    bad = svc.make_python(name="badpython", body='#!/bin/bash\necho "Traceback: boom"\nexit 3\n')
    r = svc.run("start", python=bad)
    assert r.returncode != 0
    assert "did not stay up" in r.stdout + r.stderr and "boom" in r.stdout + r.stderr   # the console tail is shown
    assert str(svc.log) in r.stdout + r.stderr
    assert not svc.pidfile.exists()


def test_service_reports_a_missing_install(svc):
    shutil.rmtree(svc.home / "padspeaker")
    r = svc.run("start")
    assert r.returncode == 1 and "not installed" in r.stdout + r.stderr


def test_service_status_works_without_an_install(svc):
    shutil.rmtree(svc.home / "padspeaker")
    assert svc.run("status").returncode == 3


def test_service_passes_the_config_file_only_when_it_exists(svc):
    svc.run("start")
    assert "--config" not in svc.text()
    svc.run("stop")
    (svc.home / "config.json").write_text("{}")
    svc.run("start")
    assert f"--config {svc.home}/config.json" in svc.text()


def test_service_usage_error(svc):
    r = svc.run("bogus")
    assert r.returncode == 1 and "Usage" in r.stdout + r.stderr
    assert svc.run(None).returncode == 1


def test_service_stop_escalates_to_kill(svc):
    stubborn = svc.make_python(trap="trap '' TERM", name="stubborn")
    assert svc.run("start", python=stubborn).returncode == 0
    pid = svc.pid()
    t0 = time.monotonic()
    r = svc.run("stop", python=stubborn)
    assert r.returncode == 0, r.stdout + r.stderr
    assert not svc.alive(pid) and time.monotonic() - t0 < 15


def test_service_rotates_big_logs(svc):
    svc.log.parent.mkdir(parents=True)
    svc.log.write_text("x" * 1_200_000)
    (svc.log.parent / "padspeaker.log.1").write_text("one")
    (svc.log.parent / "padspeaker.log.2").write_text("two")
    (svc.log.parent / "padspeaker.log.3").write_text("three-is-dropped")
    assert svc.run("start").returncode == 0
    d = svc.log.parent
    assert (d / "padspeaker.log.1").read_text() == "x" * 1_200_000
    assert (d / "padspeaker.log.2").read_text() == "one"
    assert (d / "padspeaker.log.3").read_text() == "two"
    assert not (d / "padspeaker.log.4").exists()
    assert svc.log.stat().st_size < 10_000 and "service: starting" in svc.log.read_text()


def test_service_does_not_rotate_small_logs(svc):
    svc.log.parent.mkdir(parents=True)
    svc.log.write_text("keep me\n")
    svc.run("start")
    assert svc.log.read_text().startswith("keep me") and not (svc.log.parent / "padspeaker.log.1").exists()


def test_service_start_returns_quickly_and_does_not_hold_the_caller_pipes(svc):
    """The boot script runs `start &` with its output on the boot log: the daemon must not keep that pipe open."""
    t0 = time.monotonic()
    r = svc.run("start")                                      # capture_output waits for EOF on the pipes
    assert r.returncode == 0 and time.monotonic() - t0 < 10


# ---------------------------------------------------------------- install.sh: dry run

def test_dry_run_install_sends_nothing(box):
    r = box.run("--dry-run", "alice@box.local")
    assert r.returncode == 0, r.stderr
    assert box.commands() == []                                  # the fake ssh was never called
    assert "DRY RUN" in r.stdout and "nothing was changed" in r.stdout
    assert "alice@box.local" in r.stdout
    assert "padspeaker/__init__.py" in r.stdout and "sha256:" in r.stdout
    assert "service/padspeaker" in r.stdout
    assert not box.dest.exists() and not box.service.exists()


def test_dry_run_uninstall_sends_nothing(box):
    installed(box)
    before = box.log.read_text()
    snap = box.seed_user_data()
    r = box.run("--uninstall", "--dry-run")
    assert r.returncode == 0, r.stderr
    assert box.log.read_text() == before
    assert (box.dest / "padspeaker").is_dir() and box.service.is_file() and box.user_snapshot() == snap


def test_dry_run_with_enable_does_not_enable(box):
    r = box.run("--dry-run", "--enable")
    assert r.returncode == 0, r.stderr
    assert "batocera-services enable padspeaker" in r.stdout
    assert box.commands() == [] and not (box.sandbox / ".batocera-services.log").exists()


# ---------------------------------------------------------------- install.sh: the real flow against the fake box

def test_install_end_to_end(box):
    before = box.bystanders()
    r = installed(box)
    out = r.stdout
    assert "free space on " in out and "tarball sha256 matches" in out
    assert "VERIFY-OK" in out
    assert box.commands()[0][0] == "alice@box.local"
    d = box.dest
    assert (d / "padspeaker" / "__init__.py").is_file() and (d / "tools" / "ds4_tone_poc.py").is_file()
    assert (d / "VERSION").read_text().strip() and (d / "MANIFEST.sha256").is_file()
    # the service script: installed, executable, identical to the packaged one, and a copy kept in the app folder
    assert box.service.read_bytes() == SERVICE_SRC.read_bytes()
    assert os.access(box.service, os.X_OK)
    assert (d / "service" / "padspeaker").read_bytes() == SERVICE_SRC.read_bytes()
    # nothing else on the box changed, nothing temporary is left behind
    assert box.bystanders() == before
    assert [p.name for p in d.iterdir() if p.name.startswith(".")] == []
    assert [p.name for p in box.services.iterdir() if p.name.startswith(".")] == []
    assert not (d / "padspeaker.previous").exists()
    assert (box.logs).is_dir()


def test_install_does_not_enable_or_start_and_prints_the_enable_command(box):
    r = installed(box)
    assert "NOT enabled" in r.stdout
    assert "ssh alice@box.local 'batocera-services enable padspeaker && batocera-services start padspeaker'" in r.stdout
    assert "padspeaker" not in box.conf.read_text()
    assert not (box.sandbox / ".batocera-services.log").exists()
    assert box.daemon_pid() is None
    assert "box_selftest.py" in r.stdout                           # tells how to try it first


def test_install_with_enable_enables_and_starts(box):
    before = box.bystanders(include_conf=False)
    r = installed(box, "--enable", STUB_FAKE_DAEMON="1")
    assert "enabled" in r.stdout
    conf = box.conf.read_text()
    assert "system.services=symlink_manager custom_service_handler padspeaker" in conf
    assert "audio.volume=80" in conf and "system.hostname=BATOCERA" in conf
    log = (box.sandbox / ".batocera-services.log").read_text()
    assert "enable padspeaker" in log
    pid = box.daemon_pid()
    assert pid is not None
    os.kill(pid, 0)                                               # alive
    assert "FAKE DAEMON ARGS: -m padspeaker run --log-file" in (box.logs / "padspeaker.log.console").read_text()
    assert box.bystanders(include_conf=False) == before


def test_update_keeps_one_previous_version_and_restarts_a_running_service(box):
    installed(box, "--enable", STUB_FAKE_DAEMON="1")
    first = box.daemon_pid()
    (box.dest / "padspeaker" / "marker_v1.txt").write_text("v1")
    (box.dest / "tools" / "marker_v1.txt").write_text("v1")
    seed = box.seed_user_data()
    before = box.bystanders()
    r = installed(box, STUB_FAKE_DAEMON="1")
    assert (box.dest / "padspeaker.previous" / "marker_v1.txt").read_text() == "v1"
    assert (box.dest / "tools.previous" / "marker_v1.txt").exists()
    assert not (box.dest / "padspeaker" / "marker_v1.txt").exists()
    assert (box.dest / "padspeaker" / "__init__.py").is_file()
    now = box.user_snapshot()
    assert now["config.json"] == seed["config.json"] and now["padspeaker.log.1"] == seed["padspeaker.log.1"]
    assert now["padspeaker.log"].startswith(seed["padspeaker.log"])      # the restarted daemon appended to it
    assert box.bystanders() == before
    assert "restarted" in r.stdout
    second = box.daemon_pid()
    assert second and second != first
    os.kill(second, 0)
    assert "padspeaker" in box.conf.read_text()                # still enabled, once
    assert box.conf.read_text().count("padspeaker") == 1
    installed(box, STUB_FAKE_DAEMON="1")                            # a third install drops the first generation
    assert not (box.dest / "padspeaker.previous" / "marker_v1.txt").exists()
    assert (box.dest / "padspeaker.previous" / "__init__.py").is_file()


def test_every_remote_command_stays_inside_allowed_folders(box):
    installed(box, "--enable", STUB_FAKE_DAEMON="1")
    box.run("--uninstall", "--purge", "--yes", "alice@box.local")
    allowed = ("/userdata/system/padspeaker", "/userdata/system/services", "/userdata/system/logs", "/dev/null",
               "/var/run", "/userdata/system/batocera.conf",       # read by the migration step to see what was enabled
               "/userdata/system/ds4-bt-speaker")                  # the pre-rename folder, only touched by the migration
    seen = 0
    for host, script in box.commands():
        for line in script.splitlines():
            code = line.strip()
            if code.startswith("#"):
                continue
            for m in re.finditer(r"""(?:(?<=\s)|(?<=\s")|(?<=\s')|(?<==)|(?<==")|(?<==')|(?<=\())(/[A-Za-z0-9_.\-/]*)""", code):
                path = m.group(1)
                if path == "/":
                    continue
                seen += 1
                assert path.startswith(allowed) or path in ("/userdata", "/userdata/system"), f"{path} in: {code}"
    assert seen > 10


def test_refuses_when_less_than_50_mb_free(box):
    r = box.run(STUB_FREE_MB="49")
    assert r.returncode != 0
    assert "only 49 MB free" in r.stdout + r.stderr
    assert not box.dest.exists() and not box.service.exists()   # nothing was created, nothing was uploaded
    assert len(box.commands()) == 1
    assert not (box.sandbox / "captured.tar.gz").exists()


def test_exactly_50_mb_free_is_enough(box):
    r = box.run(STUB_FREE_MB="50")
    assert r.returncode == 0, r.stdout + r.stderr


def test_min_free_cannot_be_lowered_but_can_be_raised(box):
    assert box.run("--min-free-mb", "10", "--dry-run").returncode != 0
    r = box.run("--min-free-mb", "1000", STUB_FREE_MB="500")
    assert r.returncode != 0 and "only 500 MB free" in r.stdout + r.stderr


def assert_untouched_refusal(box, r, *needles):
    before = getattr(box, "_before", None)
    assert r.returncode != 0
    text = r.stdout + r.stderr
    for n in needles:
        assert n in text, (n, text)
    assert not box.dest.exists() and not box.service.exists()
    assert len(box.commands()) == 1                                # only the read-only check ran
    assert not (box.sandbox / "captured.tar.gz").exists()
    if before is not None:
        assert box.bystanders() == before


def test_refuses_without_python3_and_says_how_to_fix_it(box):
    (box.rbin / "python3").unlink()
    box._before = box.bystanders()
    r = box.run()
    assert_untouched_refusal(box, r, "python3", "FAIL")


def test_refuses_without_libsbc_and_says_how_to_fix_it(box):
    box._before = box.bystanders()
    r = box.run(STUB_SBC="missing")
    assert_untouched_refusal(box, r, "libsbc.so.1", "apt install libsbc1", "pacman -S sbc")


def test_refuses_without_pactl_or_parec(box):
    (box.rbin / "pactl").unlink()
    r = box.run()
    assert_untouched_refusal(box, r, "pactl", "pulseaudio-utils")
    (box.rbin / "parec").unlink()
    box.log.write_text("")
    r = box.run()
    assert_untouched_refusal(box, r, "parec")


def test_lists_every_missing_requirement_at_once(box):
    (box.rbin / "pactl").unlink()
    (box.rbin / "parec").unlink()
    r = box.run(STUB_SBC="missing", STUB_FREE_MB="10")
    assert_untouched_refusal(box, r, "libsbc.so.1", "pactl", "parec", "only 10 MB free")


def test_unreachable_audio_server_is_only_a_warning(box):
    r = box.run(STUB_PACTL_FAIL="1")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "WARN" in r.stdout and "audio server" in r.stdout


def test_enable_needs_batocera_services_and_checks_before_touching(box):
    (box.rbin / "batocera-services").unlink()
    r = box.run("--enable")
    assert_untouched_refusal(box, r, "batocera-services")


def test_damaged_upload_is_detected_and_old_install_survives(box):
    installed(box)
    (box.dest / "padspeaker" / "still_here.txt").write_text("old")
    snap = box.seed_user_data()
    before = box.bystanders()
    r = box.run(STUB_CORRUPT_UPLOAD="1")
    assert r.returncode != 0
    assert "damaged" in r.stdout + r.stderr
    assert (box.dest / "padspeaker" / "still_here.txt").read_text() == "old"
    assert not (box.dest / "padspeaker.previous").exists()
    assert box.user_snapshot() == snap and box.bystanders() == before
    assert [p.name for p in box.dest.iterdir() if p.name.startswith(".staging")] == []     # the failed upload was cleaned up


def test_broken_python_file_is_caught_before_the_swap(box):
    installed(box)
    (box.dest / "padspeaker" / "still_here.txt").write_text("old")
    (box.repo / "padspeaker" / "broken.py").write_text("def x(:\n")
    r = box.run()
    assert r.returncode != 0 and "do not compile" in r.stdout + r.stderr
    assert (box.dest / "padspeaker" / "still_here.txt").exists()


def test_package_that_cannot_run_is_caught_before_the_swap(box):
    installed(box)
    (box.dest / "padspeaker" / "still_here.txt").write_text("old")
    (box.repo / "padspeaker" / "__main__.py").write_text("raise SystemExit(7)\n")
    r = box.run()
    assert r.returncode != 0 and "--version" in r.stdout + r.stderr
    assert (box.dest / "padspeaker" / "still_here.txt").exists()


def test_scp_is_never_needed(box):
    installed(box)                                               # the fake scp exits 97 if it is ever called


def test_host_argument_and_validation(box):
    installed(box)
    assert {h for h, _ in box.commands()} == {"alice@box.local"}
    r = box.run("--dry-run", "--host", "pi@10.0.0.7")
    assert "pi@10.0.0.7" in r.stdout
    r = box.run("--dry-run", PADSPEAKER_HOST="bob@elsewhere")
    assert "bob@elsewhere" in r.stdout
    r = box.run("--dry-run")
    assert "root@batocera.local" in r.stdout
    for bad in ("-oProxyCommand=evil", "a b", "host;rm", "$(x)"):
        r = box.run("--dry-run", bad)
        assert r.returncode != 0, bad
    assert box.run("--bogus").returncode != 0


# ---------------------------------------------------------------- the tarball

def test_tarball_content(box):
    repo = box.repo
    (repo / "padspeaker" / "__pycache__").mkdir(exist_ok=True)
    (repo / "padspeaker" / "__pycache__" / "x.cpython-311.pyc").write_bytes(b"\0")
    (repo / "padspeaker" / "stale.pyc").write_bytes(b"\0")
    (repo / "padspeaker" / ".DS_Store").write_bytes(b"\0")
    (repo / "padspeaker" / "._resource_fork").write_bytes(b"\0")
    (repo / "padspeaker" / "old.py.orig").write_text("x")
    (repo / "tools" / "run.log").write_text("x")
    (repo / "padspeaker" / "link.py").symlink_to("/etc/passwd")
    for junk_dir in ("tests", "docs", ".venv", ".git"):
        (repo / junk_dir).mkdir()
        (repo / junk_dir / "junk.py").write_text("x")
    installed(box)
    with tarfile.open(box.sandbox / "captured.tar.gz") as tf:
        members = tf.getmembers()
        names = [m.name for m in members]
        bad = re.compile(r"__pycache__|\.pyc$|\.DS_Store|(^|/)\._|\.orig$|\.log$|(^|/)(tests|docs|\.venv|\.git)(/|$)|link\.py|junk")
        assert [n for n in names if bad.search(n)] == []
        for needed in ("padspeaker/__init__.py", "padspeaker/protocol.py", "tools/ds4_tone_poc.py", "tools/box_selftest.py",
                       "service/padspeaker", "MANIFEST.sha256", "VERSION"):
            assert needed in names, needed
        assert all(m.isfile() or m.isdir() for m in members)           # no symlinks, devices ...
        assert all(m.uid == 0 and m.gid == 0 for m in members)
        assert all(not n.startswith("/") and ".." not in n.split("/") for n in names)
        assert set(n.split("/")[0] for n in names) == {"padspeaker", "tools", "service", "MANIFEST.sha256", "VERSION"}
        manifest = tf.extractfile("MANIFEST.sha256").read().decode().splitlines()
        listed = {}
        for line in manifest:
            digest, _, path = line.partition("  ")
            listed[path] = digest
            assert hashlib.sha256(tf.extractfile(path).read()).hexdigest() == digest, path
        files = {n for n, m in zip(names, members) if m.isfile() and n != "MANIFEST.sha256"}
        assert set(listed) == files                                    # the manifest covers exactly the packaged files
        assert next(m for m in members if m.name == "service/padspeaker").mode & 0o111
        version = tf.extractfile("VERSION").read().decode()
        assert version.startswith("0.0.0-test") or re.match(r"\d+\.\d+", version), version


def test_real_repo_tarball_has_no_junk_in_dry_run_listing(box):
    """The listing printed by --dry-run against the real working tree (with whatever caches it has)."""
    r = subprocess.run([BASH, str(REPO / "deploy/install.sh"), "--dry-run"], capture_output=True, text=True, timeout=120,
                       env=box.env())
    assert r.returncode == 0, r.stderr
    listing = r.stdout.split("contents (")[1].split("==>")[0]
    for junk in ("__pycache__", ".pyc", ".DS_Store", "tests/", "docs/", ".venv", "config.json"):
        assert junk not in listing, junk
    assert "padspeaker/protocol.py" in listing and "tools/ds4_tone_poc.py" in listing and "tools/box_selftest.py" in listing and "service/padspeaker" in listing


# ---------------------------------------------------------------- migration from the old name (ds4-bt-speaker)

def _old_name_leftovers(box):
    """Everything under the sandbox whose name still says ds4-bt-speaker / ds4_bt_speaker (path -> is a file)."""
    return sorted(p.relative_to(box.sandbox).as_posix() for p in box.sandbox.rglob("*")
                  if "ds4-bt-speaker" in p.name or "ds4_bt_speaker" in p.name)


def test_old_install_is_migrated_to_the_new_name(box):
    box.seed_old_install(running=True, enabled=True)
    old_tree = box.tree(box.old_dest)
    before = box.bystanders(include_conf=False)
    r = installed(box, STUB_FAKE_DAEMON="1")
    out = r.stdout
    assert "found an install under the old name" in out and "MIGRATE-OK" in out
    # the old service was stopped, disabled, and its script removed
    assert not box.old_running.exists() and not box.old_service.exists()
    log = (box.sandbox / ".batocera-services.log").read_text().splitlines()
    assert "disable ds4_bt_speaker" in log
    # the whole old folder was kept, byte for byte, next to the old place; the old place is gone
    assert not box.old_dest.exists()
    backups = box.backups()
    assert len(backups) == 1 and re.fullmatch(r"ds4-bt-speaker\.old-\d{8}-\d{6}-\d+", backups[0].name)
    assert box.tree(backups[0]) == old_tree
    assert str(backups[0]).replace(str(box.sandbox), "") in out
    # the config reached the new place
    assert (box.dest / "config.json").read_text() == '{"volume_cap": 42}'
    assert (box.dest / "padspeaker" / "__init__.py").is_file()
    # the old service was enabled: the new one is enabled (once) and running, the old entry is gone
    conf = box.conf.read_text()
    assert "system.services=symlink_manager custom_service_handler padspeaker" in conf
    assert "ds4_bt_speaker" not in conf and "audio.volume=80" in conf and "system.hostname=BATOCERA" in conf
    assert "enable padspeaker" in log and "will be enabled and started" in out and "The service is enabled" in out
    os.kill(box.daemon_pid(), 0)
    # everything else, the old log files and a file that merely looks like ours included, is untouched
    assert box.bystanders(include_conf=False) == before
    assert (box.logs / "ds4-bt-speaker.log").read_text() == "old log\n"
    assert (box.system / "ds4-bt-speaker-notes.txt").read_text() == "bystander system/ds4-bt-speaker-notes.txt"
    assert "left the old log files" in out


def test_migration_without_an_enabled_old_service_leaves_the_new_one_off(box):
    box.seed_old_install(running=False, enabled=False)
    conf_before = box.conf.read_text()
    r = installed(box)
    assert "MIGRATE-OK" in r.stdout and not box.old_service.exists() and not box.old_dest.exists()
    assert len(box.backups()) == 1
    assert box.conf.read_text() == conf_before                         # batocera.conf was not written at all
    assert not (box.sandbox / ".batocera-services.log").exists()       # no enable, no disable
    assert "NOT enabled" in r.stdout and box.daemon_pid() is None


def test_migration_never_overwrites_an_existing_new_config(box):
    installed(box)
    (box.dest / "config.json").write_text('{"volume_cap": 7}')
    box.seed_old_install(running=False, enabled=False, config='{"volume_cap": 99}')
    r = installed(box)
    assert (box.dest / "config.json").read_text() == '{"volume_cap": 7}'
    assert "kept the existing" in r.stdout
    assert (box.backups()[0] / "config.json").read_text() == '{"volume_cap": 99}'      # still in the backup


def test_migration_of_an_old_folder_without_a_config_or_a_service(box):
    box.seed_old_install(running=False, enabled=False, config=None)
    box.old_service.unlink()
    r = installed(box)
    assert "MIGRATE-OK" in r.stdout and not (box.dest / "config.json").exists()
    assert len(box.backups()) == 1 and not box.old_dest.exists()


def test_migration_of_a_leftover_old_service_script_without_a_folder(box):
    box.seed_old_install(running=True, enabled=True)
    shutil.rmtree(box.old_dest)
    r = installed(box, STUB_FAKE_DAEMON="1")
    assert "MIGRATE-OK" in r.stdout and not box.old_service.exists() and not box.old_running.exists()
    assert box.backups() == []
    assert "ds4_bt_speaker" not in box.conf.read_text()


def test_migration_stops_before_touching_anything_when_the_old_service_will_not_stop(box):
    box.seed_old_install(running=True, enabled=True, stubborn=True)
    old_tree = box.tree(box.old_dest)
    conf_before = box.conf.read_text()
    r = box.run("alice@box.local")
    assert r.returncode != 0 and "still running" in r.stdout + r.stderr
    assert box.tree(box.old_dest) == old_tree and box.old_service.exists() and box.backups() == []
    assert box.conf.read_text() == conf_before and not (box.dest / "config.json").exists()
    assert not (box.sandbox / ".batocera-services.log").exists()


def test_no_old_install_means_nothing_to_migrate(box):
    before = box.bystanders()
    r = installed(box)
    assert "nothing to migrate" in r.stdout and "MIGRATE-OK" not in r.stdout
    assert "old name" not in r.stdout.split("Migrate an install")[0]                # the check did not announce one
    assert _old_name_leftovers(box) == ["userdata/system/ds4-bt-speaker-notes.txt"]  # only the seeded bystander
    assert box.bystanders() == before
    assert not (box.sandbox / ".batocera-services.log").exists()
    assert "NOT enabled" in r.stdout


def test_migration_twice_changes_nothing_the_second_time(box):
    box.seed_old_install(running=True, enabled=True)
    installed(box, STUB_FAKE_DAEMON="1")
    backup = box.backups()[0]
    backup_tree = box.tree(backup)
    conf, config = box.conf.read_text(), (box.dest / "config.json").read_text()
    system_entries = sorted(p.name for p in box.system.iterdir())
    service_entries = sorted(p.name for p in box.services.iterdir())
    bystanders = box.bystanders()
    (box.sandbox / ".batocera-services.log").unlink()
    r = installed(box, STUB_FAKE_DAEMON="1")
    assert "nothing to migrate" in r.stdout and "MIGRATE-OK" not in r.stdout
    assert box.backups() == [backup] and box.tree(backup) == backup_tree
    assert box.conf.read_text() == conf and (box.dest / "config.json").read_text() == config
    assert sorted(p.name for p in box.system.iterdir()) == system_entries
    assert sorted(p.name for p in box.services.iterdir()) == service_entries
    assert box.bystanders() == bystanders
    service_log = box.sandbox / ".batocera-services.log"
    assert not service_log.exists() or "disable" not in service_log.read_text()       # nothing was disabled again
    assert box.conf.read_text().count("padspeaker") == 1


def test_dry_run_with_an_old_install_changes_nothing(box):
    box.seed_old_install(running=True, enabled=True)
    snapshot = box.tree(box.sandbox)
    r = box.run("--dry-run", "alice@box.local")
    assert r.returncode == 0, r.stderr
    assert box.commands() == []                                          # not a single ssh call
    assert box.tree(box.sandbox) == snapshot                             # not a single file or folder changed
    assert box.old_running.exists()                                      # the old service is still running
    assert "DRY RUN" in r.stdout and "script_migrate" not in r.stdout
    assert "Migrate an install under the old name" in r.stdout and "MIGRATE-OK" in r.stdout    # the plan is shown
    assert "$OLD_DEST.old-$TS" in r.stdout


def test_uninstall_leaves_an_old_install_alone_and_says_so(box):
    box.seed_old_install(running=True, enabled=True)
    snapshot = box.tree(box.old_dest)
    conf = box.conf.read_text()
    r = box.run("--uninstall", "alice@box.local")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "NOTE: an install under the old name" in r.stdout
    assert box.tree(box.old_dest) == snapshot and box.old_service.exists() and box.old_running.exists()
    assert box.conf.read_text() == conf
    assert box.run("--uninstall", "--dry-run").returncode == 0


def test_a_backup_of_the_old_folder_survives_uninstall_and_purge(box):
    box.seed_old_install(running=False, enabled=False)
    installed(box)
    backup = box.backups()[0]
    snapshot = box.tree(backup)
    assert box.run("--uninstall", "--purge", "--yes").returncode == 0
    assert box.tree(backup) == snapshot


def test_no_old_environment_variable_names_are_left():
    for rel in ("deploy/install.sh", "deploy/padspeaker", "tests/test_deploy.py"):
        text = _text(rel)
        for old in ("DS4_HOME", "DS4_LOG", "DS4_PIDFILE", "DS4_CONFIG", "DS4_PYTHON", "DS4_HOST", "DS4_START_TICKS",
                    "DS4_STOP_TICKS", "DS4SPEAKER"):
            if rel != "tests/test_deploy.py":
                assert old not in text, (rel, old)
    assert "PADSPEAKER_HOME" in _text("deploy/padspeaker") and "PADSPEAKER_HOST" in _text("deploy/install.sh")


# ---------------------------------------------------------------- uninstall

def test_uninstall_removes_app_and_service_but_keeps_config_and_logs(box):
    installed(box, "--enable", STUB_FAKE_DAEMON="1")
    pid = box.daemon_pid()
    seed = box.seed_user_data()
    (box.dest / "my-notes.txt").write_text("not ours")
    before = box.bystanders(include_conf=False)
    r = box.run("--uninstall", "alice@box.local")
    assert r.returncode == 0, r.stdout + r.stderr
    d = box.dest
    for gone in ("padspeaker", "tools", "service", "padspeaker.previous", "tools.previous", "service.previous", "VERSION",
                 "MANIFEST.sha256"):
        assert not (d / gone).exists(), gone
    assert not box.service.exists()
    assert box.user_snapshot() == seed
    assert (d / "my-notes.txt").read_text() == "not ours"
    assert "kept your data" in r.stdout
    assert box.bystanders(include_conf=False) == before
    conf = box.conf.read_text()
    assert "padspeaker" not in conf and "symlink_manager custom_service_handler" in conf and "audio.volume=80" in conf
    assert "disable padspeaker" in (box.sandbox / ".batocera-services.log").read_text()
    assert wait_dead(pid)                                          # the daemon was stopped
    assert not box.pidfile.exists()


def test_uninstall_of_a_clean_install_leaves_no_folder_behind(box):
    installed(box)
    r = box.run("--uninstall")
    assert r.returncode == 0, r.stdout + r.stderr
    assert not box.dest.exists()
    assert not box.service.exists()


def test_uninstall_when_nothing_is_installed_is_harmless(box):
    before = box.bystanders()
    r = box.run("--uninstall")
    assert r.returncode == 0, r.stdout + r.stderr
    assert box.bystanders() == before


def test_purge_needs_yes_and_then_deletes_everything_of_ours(box):
    installed(box)
    box.seed_user_data()
    before = box.bystanders()
    r = box.run("--uninstall", "--purge")
    assert r.returncode != 0 and "--yes" in r.stderr
    assert (box.dest / "config.json").is_file()
    assert box.run("--purge").returncode != 0                          # --purge alone makes no sense
    r = box.run("--uninstall", "--purge", "--yes")
    assert r.returncode == 0, r.stdout + r.stderr
    assert not box.dest.exists() and not box.service.exists()
    assert not list(box.logs.glob("padspeaker.log*"))
    assert box.bystanders() == before                                  # incl. system/padspeaker-notes.txt and other logs


def test_printed_uninstall_script_protects_user_data_without_purge(box):
    r = box.run("--uninstall", "--dry-run")
    in_purge_branch = False
    for line in r.stdout.splitlines():
        code = line[6:].strip() if line.startswith("    | ") else ""
        if code == 'if [ "$PURGE" = 1 ]; then':
            in_purge_branch = True
        elif code == "else":
            in_purge_branch = False
        if code.startswith(("rm ", "rm -")) and not in_purge_branch:
            for name in ("config.json", "logs", "padspeaker.log", "$DEST\""):
                assert name not in code, code
    assert re.search(r'\[ "\$PURGE" = 1 \]', r.stdout)


# ---------------------------------------------------------------- packaging and documentation stay consistent with the code

DOC_FILES = ("README.md", "CONTRIBUTING.md", "docs/ARCHITECTURE.md", "docs/RELEASING.md", "docs/PROTOCOL.md", "docs/BATOCERA.md", "docs/LINUX.md", "COMPATIBILITY.md",
             "SECURITY.md", "CHANGELOG.md", ".github/PULL_REQUEST_TEMPLATE.md")
CREDIT_URLS = (
    "https://sensepost.com/blog/2020/dual-pod-shock-emotional-abuse-of-a-dualshock",
    "https://www.psdevwiki.com/ps4/DS4-BT",
    "https://github.com/snipem/DS4Dongle",
    "https://controllers.fandom.com/wiki/Sony_DualShock_4/Data_Structures",
)


def _text(rel):
    return (REPO / rel).read_text()


def test_pyproject_describes_the_package():
    text = _text("pyproject.toml")
    try:
        import tomllib
    except ImportError:                                  # Python 3.9 and 3.10: look at the text instead
        assert 'name = "padspeaker"' in text
        assert 'requires-python = ">=3.9"' in text
        assert 'padspeaker = "padspeaker.cli:main"' in text
        assert 'authors = [{ name = "Ahmed Elmalt" }]' in text
        assert "dependencies = []" in text
        assert 'version = { attr = "padspeaker.__version__" }' in text
        return
    data = tomllib.loads(text)
    project = data["project"]
    assert project["name"] == "padspeaker"
    assert project["requires-python"] == ">=3.9"
    assert project["dependencies"] == []                 # no runtime dependencies
    assert "version" in project["dynamic"]
    assert data["tool"]["setuptools"]["dynamic"]["version"] == {"attr": "padspeaker.__version__"}
    assert project["scripts"] == {"padspeaker": "padspeaker.cli:main"}
    assert data["tool"]["setuptools"]["packages"] == ["padspeaker"]
    assert project["license"] == "MIT"
    assert project["authors"] == [{"name": "Ahmed Elmalt"}]
    base = "https://github.com/eknowledger/padspeaker"
    assert project["urls"] == {"Homepage": base, "Documentation": base + "#readme", "Issues": base + "/issues",
                               "Changelog": base + "/blob/main/CHANGELOG.md"}
    classifiers = "\n".join(project["classifiers"])
    for minor in range(9, 14):
        assert f"Programming Language :: Python :: 3.{minor}" in classifiers
    assert "Operating System :: POSIX :: Linux" in classifiers


def test_console_script_target_exists():
    import importlib
    cli = importlib.import_module("padspeaker.cli")
    assert callable(cli.main)
    assert isinstance(importlib.import_module("padspeaker").__version__, str)


def test_license_is_mit_with_the_owners_name():
    text = _text("LICENSE")
    assert text.startswith("MIT License")
    assert re.search(r"^Copyright \(c\) 2026 Ahmed Elmalt$", text, re.M)
    assert "Permission is hereby granted, free of charge" in text


def test_every_config_key_is_documented_in_the_readme():
    from padspeaker.daemon import DaemonConfig
    readme = _text("README.md")
    for key in DaemonConfig._KEYS:
        assert f"`{key}`" in readme, f"config key {key} is not documented in README.md"


def test_readme_covers_what_users_need():
    readme = _text("README.md")
    for needed in ("Quick start: Batocera", "Quick start: any Linux", "System Settings > Audio Output", "## Volume",
                   "## Configuration", "## Troubleshooting", "**No sound.**", "**The pad's light went off", "missing from the menu",
                   "**PipeWire not ready at boot.**", "Pairing modes", "classic (PlayStation) mode", "## Limitations",
                   "DualSense", "Wi-Fi", "latency", "Latency", "## Credits", "python3 -m padspeaker run", "[MIT](LICENSE)",
                   "deploy/install.sh --dry-run", "--enable", "--uninstall", "python -m pytest"):
        assert needed in readme, needed
    for url in CREDIT_URLS:
        assert url in readme, url


def test_protocol_doc_matches_the_code():
    from padspeaker import protocol as p
    doc = _text("docs/PROTOCOL.md")
    for url in CREDIT_URLS:
        assert url in doc, url
    for needed in ("462", "78", "`0x17`", "`0x11`", "112", "`9c 75 19`", "32 000 Hz", "bitpool 25", "16 blocks", "8 subbands",
                   "`0xA2`", "`0x4F`", "`0x02` speaker, `0x24` headset", "How it was verified", "tools/ds4_tone_poc.py"):
        assert needed in doc, needed
    assert (p.AUDIO_REPORT_LEN, p.STATE_REPORT_LEN, p.SBC_FRAME_BYTES, p.MAX_SPEAKER_VOLUME) == (462, 78, 112, 0x4F)
    # the worked examples in the text are produced by the code
    assert p.build_volume_report(0x40).hex() in doc
    frame = bytes([0x9C, 0x75, 0x19]) + bytes(109)
    audio = p.build_audio_report(0, [frame] * 4)
    assert audio[:8].hex() in doc
    assert " ".join("%02x" % b for b in audio[-4:]) in doc


def test_batocera_doc_matches_the_installer():
    doc = _text("docs/BATOCERA.md")
    script = _text("deploy/install.sh")
    for needed in ("batocera-services enable padspeaker", "deploy/install.sh --enable", "deploy/install.sh --uninstall",
                   "--purge --yes", "--dry-run", "/userdata/system/padspeaker", "/userdata/system/services/padspeaker",
                   "/userdata/system/logs/padspeaker.log", "XDG_RUNTIME_DIR=/var/run", "Start > System Settings > Audio Output",
                   "What is NOT modified", "padspeaker.previous", "box_selftest.py", "50 MB"):
        assert needed in doc, needed
    for constant in ("DEST=/userdata/system/padspeaker", "SERVICE_NAME=padspeaker",
                     "LOG_FILE=/userdata/system/logs/padspeaker.log", "MIN_FREE_MB_FLOOR=50"):
        assert constant in script, constant


def test_markdown_links_resolve():
    link = re.compile(r"\]\(([^)\s]+)\)")
    checked = 0
    for rel in DOC_FILES:
        base = (REPO / rel).parent
        for target in link.findall(_text(rel)):
            if target.startswith(("http://", "https://", "mailto:", "#")):
                continue
            path = target.split("#", 1)[0]
            assert (base / path).exists(), f"{rel}: broken link {target}"
            checked += 1
    assert checked >= 5


def test_ci_workflow_runs_pytest_on_supported_pythons():
    workflows = REPO / ".github" / "workflows"
    path = next(p for p in (workflows / "tests.yml", workflows / "test.yml") if p.exists())
    text = path.read_text()
    for version in ("3.9", "3.13"):                      # both ends of the supported range
        assert f'"{version}"' in text, version
    assert "pytest" in text and "actions/checkout" in text and "actions/setup-python" in text
    assert "secrets." not in text


def test_files_written_for_python_39_parse_as_python_39():
    """The code runs on 3.9 on the target; syntax newer than that must not creep into the scripts and tools."""
    import ast
    for rel in ("tools/box_selftest.py", "tests/test_deploy.py", "tests/test_box_tools.py"):
        ast.parse(_text(rel), filename=rel, feature_version=(3, 9))


# ---------------------------------------------------------------- open-source readiness: documents, templates, examples

def _tracked_text_files():
    skip = {".venv", ".git", "__pycache__", ".pytest_cache", "build", "dist"}
    for path in sorted(REPO.rglob("*")):
        if not path.is_file() or skip & set(path.relative_to(REPO).parts) or path.suffix in (".png", ".pyc"):
            continue
        if path.name.endswith(".egg-info"):
            continue
        yield path


def test_no_personal_data_in_the_repository():
    """Sample data must be neutral. The patterns are assembled from pieces so this file does not match itself."""
    forbidden = {
        "a real pad address prefix": "41" + ":" + "42" + ":",
        "a real pad address tail": "9e" + "b5",
        "another real pad tail": "20" + "d4",
        "a real TV name": "HI" + "SENSE",
        "a real host name": "Ym" + "ir",
        "a real address of the box": "192.168.1." + "74",
        "a user name": "ael" + "malt",
    }
    hits = []
    for path in _tracked_text_files():
        text = path.read_text(errors="replace")
        for what, needle in forbidden.items():
            if needle.lower() in text.lower():
                hits.append(f"{path.relative_to(REPO)}: {what}")
    assert hits == []


def test_every_mac_address_in_the_repository_is_an_obvious_example():
    mac = re.compile(r"(?<![0-9A-Za-z:])([0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5})(?![0-9A-Za-z:])")
    allowed_prefixes = ("aa:bb:cc:", "aa:aa:aa:", "00:00:00:", "11:22:33:", "ff:00:00:", "aa:00:00:", "xx")
    odd = []
    for path in _tracked_text_files():
        for found in mac.findall(path.read_text(errors="replace")):
            if not found.lower().startswith(allowed_prefixes):
                odd.append(f"{path.relative_to(REPO)}: {found}")
    assert odd == []


def test_no_owner_placeholders_are_left():
    """Everything the owner had to fill in before publishing is filled in."""
    where = set()
    for path in _tracked_text_files():
        if path.name == "test_deploy.py":
            continue
        text = path.read_text(errors="replace")
        if "TODO (owner)" in text or "TODO-owner" in text:
            where.add(path.relative_to(REPO).as_posix())
    assert where == set(), where
    assert "## [0.1.0] - 2026-10-19" in _text("CHANGELOG.md")


def test_readme_carries_the_affiliation_disclaimer():
    readme = _text("README.md")
    assert "Not affiliated with Sony" in readme
    assert "Sony Interactive Entertainment" in readme
    assert "trademarks of their owners" in readme and "public research" in readme


def test_readme_documents_what_the_new_docs_promise():
    readme = _text("README.md")
    for needed in ("Combined outputs", "`tv_label`", "`keep_lightbar`", "`disable_sniff`", "sync_batocera_audio_device",
                   "audio.device", "At boot, at shutdown", "Two pads: which one drives the menu?", "`extra_pad_ids`",
                   "--extra-pad-id", "docs/LINUX.md", "COMPATIBILITY.md", "SECURITY.md", "CHANGELOG.md"):
        assert needed in readme, needed


def test_changelog_lists_every_config_key():
    from padspeaker import __version__
    from padspeaker.daemon import DaemonConfig
    text = _text("CHANGELOG.md")
    assert f"[{__version__}]" in text
    for key in DaemonConfig._KEYS:
        assert f"`{key}`" in text, key


def test_security_doc_names_every_switch_and_the_writes():
    text = _text("SECURITY.md")
    for key in ("disable_sniff", "disconnect_on_hid_loss", "sync_batocera_audio_device", "combine_pads", "combine_tv_pad",
                "combine_tv_all", "only_macs", "ignore_macs", "extra_pad_ids", "status_file", "lock_file"):
        assert key in text, key
    for needed in ("`0x17`", "`0x11`", "runs as root", "Reporting a problem", "hcitool lp", "bluetoothctl disconnect",
                   "batocera-settings-set", "without a shell", "/dev/hidrawN"):
        assert needed.lower() in text.lower(), needed


def test_compatibility_doc_has_its_lists_and_the_report_recipe():
    text = _text("COMPATIBILITY.md")
    for heading in ("## Verified", "## Not verified", "## Not supported", "## Report your pad"):
        assert heading in text, heading
    for needed in ("054C:09CC", "Batocera v41", "PipeWire 1.2", "6.11", "DualSense", "USB", "headset", "microphone",
                   "padspeaker list --json", "padspeaker check", "padspeaker tone", "aa:bb:cc:xx:xx:xx"):
        assert needed in text, needed


def test_linux_doc_covers_its_topics_and_marks_examples_untested():
    text = _text("docs/LINUX.md")
    for needed in ("PipeWire", "PulseAudio", "libsbc", "BlueZ", "Python 3.9", "pip install .", "python3 -m build",
                   "systemctl --user", "How the runtime directory is found", "GNOME", "KDE", "wpctl", "pactl set-default-sink",
                   "Without hcitool", "untested", "70-padspeaker.rules", "deploy/systemd/user/padspeaker.service",
                   "deploy/systemd/padspeaker.service", "loginctl enable-linger"):
        assert needed.lower() in text.lower(), needed


def test_files_that_the_docs_point_at_exist():
    ref = re.compile(r"`((?:deploy|docs|tools|tests|\.github)/[A-Za-z0-9_./-]+)`")
    missing = []
    for rel in DOC_FILES:
        for target in ref.findall(_text(rel)):
            if target.endswith("/"):
                continue
            if not (REPO / target).exists():
                missing.append(f"{rel}: {target}")
    assert missing == []


def _unit_sections(rel):
    import configparser
    parser = configparser.ConfigParser(strict=False, interpolation=None, comment_prefixes=("#", ";"), delimiters=("=",))
    parser.optionxform = str
    parser.read_string(_text(rel))
    return parser


def test_systemd_system_unit_example():
    unit = _unit_sections("deploy/systemd/padspeaker.service")
    assert set(unit.sections()) == {"Unit", "Service", "Install"}
    service = unit["Service"]
    assert service["ExecStart"].endswith("padspeaker run")
    assert service["Restart"] == "on-failure" and service["Type"] == "simple"
    assert service["User"]                                                    # a system unit runs as a named user
    assert re.fullmatch(r"/run/user/\d+", service["Environment"].split("=", 1)[1])      # %U would be root's id here
    assert "%U" not in "\n".join(line for line in _text("deploy/systemd/padspeaker.service").splitlines()
                                  if not line.lstrip().startswith("#"))
    assert unit["Install"]["WantedBy"] == "multi-user.target"
    assert "bluetooth.target" in unit["Unit"]["After"]


def test_systemd_user_unit_example():
    unit = _unit_sections("deploy/systemd/user/padspeaker.service")
    assert set(unit.sections()) == {"Unit", "Service", "Install"}
    assert unit["Service"]["ExecStart"].endswith("padspeaker run")
    assert "User" not in unit["Service"]
    assert unit["Install"]["WantedBy"] == "default.target"
    assert "pipewire-pulse.service" in unit["Unit"]["After"]


def test_unit_files_call_the_console_script_that_pyproject_defines():
    pyproject = _text("pyproject.toml")
    assert re.search(r'^padspeaker = "padspeaker\.cli:main"$', pyproject, re.M)
    for rel in ("deploy/systemd/padspeaker.service", "deploy/systemd/user/padspeaker.service"):
        assert "padspeaker run" in _text(rel)


def test_udev_rule_example_is_scoped_and_marked_untested():
    text = _text("deploy/udev/70-padspeaker.rules")
    assert "UNTESTED" in text
    rules = [l for l in text.splitlines() if l.strip() and not l.lstrip().startswith("#")]
    assert len(rules) == 1
    rule = rules[0]
    assert rule.startswith('SUBSYSTEM=="hidraw"')
    assert 'KERNELS=="0005:054C:*"' in rule                  # Bluetooth bus and Sony vendor only, not every hidraw node
    assert 'GROUP="padspeaker"' in rule and 'MODE="0660"' in rule
    assert "MODE=\"0666\"" not in text and "0664" not in text            # never world-writable
    assert "#SUBSYSTEM==\"hidraw\", KERNELS==\"0005:054C:*\", TAG+=\"uaccess\"" in text


def test_github_templates():
    bug = _text(".github/ISSUE_TEMPLATE/bug_report.yml")
    assert bug.startswith("name:") and "\nbody:\n" in bug
    for field in ("padspeaker --version", "padspeaker list --json", "padspeaker check",
                  "padspeaker tone", "uname -r", "bluetoothctl --version", "hcitool", "VVVV:PPPP", "xx"):
        assert field in bug, field
    assert len(re.findall(r"^\s+- type: ", bug, re.M)) >= 8
    assert "id: " in bug and "required: true" in bug
    pr = _text(".github/PULL_REQUEST_TEMPLATE.md")
    for needed in ("pytest", "Docs updated", "0x11", "0x17", "Python 3.9", "No personal data"):
        assert needed in pr, needed


def test_gitignore_and_packaging_hygiene():
    ignore = _text(".gitignore").splitlines()
    for entry in (".venv/", "__pycache__/", "*.pyc", ".pytest_cache/", "*.egg-info/", "dist/", "build/", ".DS_Store"):
        assert entry in ignore, entry
    assert (REPO / "LICENSE").is_file() and (REPO / "MANIFEST.in").is_file()
    assert not (REPO / "dist").exists() and not (REPO / "build").exists()
    assert not list(REPO.glob("*.egg-info"))
    pyproject = _text("pyproject.toml")
    assert "[project.urls]" in pyproject and "TODO" not in pyproject               # the real repository address
    assert re.search(r'^license = "MIT"$', pyproject, re.M)


def test_the_proof_of_concept_is_marked_historical():
    head = _text("tools/ds4_tone_poc.py")[:600]
    assert "HISTORICAL PROOF OF CONCEPT" in head and "padspeaker tone" in head
