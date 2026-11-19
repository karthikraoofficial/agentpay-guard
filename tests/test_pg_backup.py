"""The hosted demo's nightly backups (#200, ADR-0023): `infra/pg-backup.sh`.

The script runs with stand-in `docker`, `curl` and `crontab` commands first on PATH, which
log their arguments and play the parts each test needs, so these need bash but no Docker
and no Oracle account. The restore check (`pg-backup.sh check`) needs a real stack and is
run by hand (see the ticket).
"""

import os
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

SCRIPT = Path("infra/pg-backup.sh").resolve().as_posix()  # bash on Windows reads "\\" as an escape
PAR = "https://objectstorage.ap-hyderabad-1.oraclecloud.com/p/SECRET-TOKEN-x9/n/ns/b/agentpay-guard-backups/o/"

# By full path: on Windows, subprocess looks in System32 (WSL's bash) before PATH.
BASH = shutil.which("bash")

pytestmark = pytest.mark.skipif(BASH is None, reason="needs bash")

STUBS = {
    "docker": """#!/usr/bin/env bash
echo "docker $*" >> "$STUB_LOG"
case "$*" in
  *pg_dump*)
    if [[ -n "${STUB_DUMP_FAILS:-}" ]]; then printf 'PGDMP-half'; exit 1; fi
    printf 'PGDMP-archive' ;;
  *"pg_restore -l"*) cat > /dev/null ;;
esac
""",
    "curl": """#!/usr/bin/env bash
echo "curl $*" >> "$STUB_LOG"
[[ -z "${STUB_CURL_FAILS:-}" ]] || exit 22
""",
    "crontab": """#!/usr/bin/env bash
if [[ "${1:-}" == "-l" ]]; then cat "$STUB_CRONTAB" 2>/dev/null || exit 1
else cat > "$STUB_CRONTAB"; fi
""",
}


class Vm:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.backups = root / "backups"
        self.env_file = root / ".env"
        self.log = root / "stub.log"
        self.crontab = root / "crontab"
        bin_dir = root / "bin"
        bin_dir.mkdir()
        for name, body in STUBS.items():
            stub = bin_dir / name
            stub.write_text(body, encoding="utf-8", newline="\n")
            stub.chmod(0o755)
        self.path = f"{bin_dir}{os.pathsep}{os.environ['PATH']}"
        self.env_file.write_text("AGENTPAY_ENV=production\n", encoding="utf-8")

    def set(self, key: str, value: str) -> None:
        with self.env_file.open("a", encoding="utf-8") as env:
            env.write(f"{key}='{value}'\n")

    def run(self, command: str, **extra: str) -> subprocess.CompletedProcess[str]:
        env = {
            **os.environ,
            "PATH": self.path,
            "STUB_LOG": self.log.as_posix(),
            "STUB_CRONTAB": self.crontab.as_posix(),
            "AGENTPAY_BACKUP_DIR": self.backups.as_posix(),
            "AGENTPAY_BACKUP_ENV_FILE": self.env_file.as_posix(),
            **extra,
        }
        assert BASH is not None
        return subprocess.run(  # noqa: S603 - our script, our arguments
            [BASH, SCRIPT, command],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )

    def dumps(self) -> list[str]:
        return sorted(p.name for p in self.backups.iterdir()) if self.backups.exists() else []

    def calls(self, tool: str) -> list[str]:
        lines = self.log.read_text(encoding="utf-8").splitlines() if self.log.exists() else []
        return [line for line in lines if line.startswith(f"{tool} ")]


@pytest.fixture
def vm(tmp_path: Path) -> Vm:
    return Vm(tmp_path)


def test_a_nightly_dump_is_kept_under_its_final_name_and_uploaded(vm: Vm) -> None:
    vm.set("AGENTPAY_BACKUP_PAR_URL", PAR)

    done = vm.run("dump")

    assert done.returncode == 0, done.stderr
    [name] = [n for n in vm.dumps() if n.endswith(".dump")]
    assert name.startswith("agentpay-") and not any(n.endswith(".partial") for n in vm.dumps())
    assert (vm.backups / name).read_text(encoding="utf-8") == "PGDMP-archive"
    [upload] = vm.calls("curl")
    assert f"{PAR}{name}" in upload
    assert name in upload.split(f"{PAR}{name}")[0]  # the file sent is the dump itself


def test_only_the_newest_14_dumps_are_kept(vm: Vm) -> None:
    vm.backups.mkdir()
    old = [f"agentpay-202601{day:02d}T023000Z.dump" for day in range(1, 17)]
    for name in old:
        (vm.backups / name).write_text("PGDMP-old", encoding="utf-8")

    done = vm.run("dump")

    assert done.returncode == 0, done.stderr
    kept = [n for n in vm.dumps() if n.endswith(".dump")]
    assert len(kept) == 14
    assert set(old[:3]).isdisjoint(kept)  # 16 old + tonight's: the 3 oldest go


def test_a_failed_dump_leaves_nothing_under_a_dump_name(vm: Vm) -> None:
    vm.set("AGENTPAY_BACKUP_PAR_URL", PAR)

    done = vm.run("dump", STUB_DUMP_FAILS="1")

    assert done.returncode != 0
    assert [n for n in vm.dumps() if n.startswith("agentpay-")] == []
    assert vm.calls("curl") == []


def test_without_an_upload_link_the_dump_stays_on_the_vm(vm: Vm) -> None:
    done = vm.run("dump")

    assert done.returncode == 0, done.stderr
    assert len([n for n in vm.dumps() if n.endswith(".dump")]) == 1
    assert vm.calls("curl") == []
    assert "no off-VM copy" in done.stdout


def test_a_failed_upload_keeps_the_dump_and_fails(vm: Vm) -> None:
    vm.set("AGENTPAY_BACKUP_PAR_URL", PAR)

    done = vm.run("dump", STUB_CURL_FAILS="1")

    assert done.returncode != 0
    assert len([n for n in vm.dumps() if n.endswith(".dump")]) == 1
    assert "upload failed" in done.stdout + done.stderr


def test_the_upload_link_is_never_printed(vm: Vm) -> None:
    vm.set("AGENTPAY_BACKUP_PAR_URL", PAR)

    for extra in ({}, {"STUB_CURL_FAILS": "1"}):
        done = vm.run("dump", **extra)
        assert "SECRET-TOKEN" not in done.stdout + done.stderr


def test_a_link_about_to_expire_is_warned_about(vm: Vm) -> None:
    vm.set("AGENTPAY_BACKUP_PAR_URL", PAR)
    soon = (datetime.now(UTC) + timedelta(days=10)).date().isoformat()
    vm.set("AGENTPAY_BACKUP_PAR_EXPIRES", soon)

    done = vm.run("dump")

    assert done.returncode == 0, done.stderr
    assert f"expires on {soon}" in done.stdout


def test_installing_the_cron_jobs_twice_leaves_one_of_each(vm: Vm) -> None:
    vm.crontab.write_text("0 1 * * * echo someone-elses-job\n", encoding="utf-8")

    for _ in range(2):
        done = vm.run("install-cron")
        assert done.returncode == 0, done.stderr

    lines = vm.crontab.read_text(encoding="utf-8").splitlines()
    assert "0 1 * * * echo someone-elses-job" in lines
    ours = [line for line in lines if line.endswith("# agentpay-backup")]
    assert len(ours) == 2
    assert ours[0].startswith("30 2 * * * ") and "pg-backup.sh dump" in ours[0]
    assert ours[1].startswith("0 3 * * 0 ") and "pg-backup.sh check" in ours[1]
