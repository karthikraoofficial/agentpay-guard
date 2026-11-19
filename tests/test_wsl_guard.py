"""Operator scripts under WSL (#203): PowerShell's `bash` is WSL's, which can't see the tools
installed for Git Bash. Each script run on the operator's machine stops first with the way
out, and only when a tool it needs is missing.

The scripts run with WSL_DISTRO_NAME set (as WSL sets it) and a PATH holding only bash's own
directory, so no `oci` and no `uv`.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

# By full path: on Windows, subprocess looks in System32 (WSL's bash) before PATH.
BASH = shutil.which("bash")

pytestmark = pytest.mark.skipif(BASH is None, reason="needs bash")

OPERATOR_SCRIPTS = [
    ["infra/oci-create-vm.sh"],
    ["infra/oci-create-backup-bucket.sh", ".env.production"],
    ["infra/provision-wizard.sh"],
]


def run(args: list[str], *, wsl: bool) -> subprocess.CompletedProcess[str]:
    assert BASH is not None
    env = {key: value for key, value in os.environ.items() if key != "WSL_DISTRO_NAME"}
    env["PATH"] = str(Path(BASH).parent)
    if wsl:
        env["WSL_DISTRO_NAME"] = "Ubuntu"
    return subprocess.run(  # noqa: S603 - our scripts, our arguments
        [BASH, *args],
        capture_output=True,
        text=True,
        env=env,
        stdin=subprocess.DEVNULL,
        timeout=60,
        check=False,
    )


@pytest.mark.parametrize("args", OPERATOR_SCRIPTS, ids=lambda a: Path(a[0]).name)
def test_under_wsl_without_its_tools_a_script_says_to_use_git_bash(args: list[str]) -> None:
    prod = Path(".env.production")
    before = prod.stat().st_mtime_ns if prod.exists() else None

    done = run(args, wsl=True)

    assert done.returncode == 2
    assert done.stdout == ""  # it stops before doing anything
    assert "WSL" in done.stderr and "Git Bash" in done.stderr
    assert f'& "C:\\Program Files\\Git\\bin\\bash.exe" {" ".join(args)}' in done.stderr
    assert "oci" in done.stderr  # names what's missing
    assert (prod.stat().st_mtime_ns if prod.exists() else None) == before


def test_outside_wsl_a_missing_oci_cli_still_gets_the_install_hint() -> None:
    done = run(["infra/oci-create-backup-bucket.sh", ".env.production"], wsl=False)

    assert done.returncode == 2
    assert "Install the OCI CLI first" in done.stderr
    assert "Git Bash" not in done.stderr
