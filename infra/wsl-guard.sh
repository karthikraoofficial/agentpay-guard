# shellcheck shell=bash
# Sourced by the scripts that run on the operator's machine (#203):
#   source "$(dirname "$0")/wsl-guard.sh"
#   wsl_guard "oci uv" "$@"     # the tools it needs, then the script's own arguments
#
# On Windows, `bash` in PowerShell is WSL's (C:\WINDOWS\system32\bash.exe), not Git Bash.
# WSL has its own PATH and home, so the tools installed for Git Bash (the OCI CLI, uv,
# ~/.oci, ~/.ssh/agentpay-oci) aren't there, and a "not installed" message would be wrong.
# So under WSL, when a tool is missing, stop and say how to run the script from Git Bash.
# With every tool there, or outside WSL, it does nothing: the script's own checks apply.

# wsl_guard "TOOL..." [ARG...] exits 2 under WSL if any TOOL isn't on PATH.
wsl_guard() {
  [[ -n "${WSL_DISTRO_NAME:-}" ]] || grep -qi microsoft /proc/version 2>/dev/null || return 0
  local tool missing=() tools=$1
  shift
  for tool in $tools; do command -v "$tool" > /dev/null 2>&1 || missing+=("$tool"); done
  (( ${#missing[@]} )) || return 0
  {
    echo "You're in WSL's bash, which can't see your Windows tools (missing: ${missing[*]})."
    echo "PowerShell's \"bash\" is WSL. Run this from Git Bash instead. In PowerShell:"
    printf '  & "C:\\Program Files\\Git\\bin\\bash.exe" %s\n' "$0${*:+ $*}"
  } >&2
  exit 2
}
