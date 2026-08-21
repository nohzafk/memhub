# shellcheck shell=bash
# Shared helpers for provision-ct.sh and deploy.sh. Sourced, never run.

PVE_HOST="${PVE_HOST:-root@pve.example}"
VMID="${VMID:-106}"
CT_IP="${CT_IP:-10.0.0.2}"
APP_DIR="${APP_DIR:-/opt/memhub}"
DATA_DIR="${DATA_DIR:-/var/lib/memhub}"
SERVICE_USER="${SERVICE_USER:-memhub}"

repo_root() {
    cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd
}

log() {
    printf '\033[1;34m==>\033[0m %s\n' "$*"
}

warn() {
    printf '\033[1;33m warning:\033[0m %s\n' "$*" >&2
}

die() {
    printf '\033[1;31m error:\033[0m %s\n' "$*" >&2
    exit 1
}

require() {
    local tool
    for tool in "$@"; do
        command -v "$tool" >/dev/null || die "$tool is not on PATH."
    done
}

# Run a command on the Proxmox host. BatchMode keeps a missing key from opening
# a password prompt inside a script; ConnectTimeout turns "wrong network" into a
# ten-second error instead of a two-minute TCP wait.
# LogLevel=ERROR silences OpenSSH 10's post-quantum advisory, which prints on
# every call and buries the output that matters. Real errors still print.
SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=10 -o LogLevel=ERROR)

pve_run() {
    # Client-side expansion is the contract: callers pass already-resolved
    # arguments (a VMID, a path from pins.env), not a template for the host to
    # interpret. Use ct_sh when a remote shell has to do the expanding.
    # shellcheck disable=SC2029
    ssh "${SSH_OPTS[@]}" "$PVE_HOST" "$@"
}

# Run a command inside the container, via the host. `--` keeps pct from eating flags.
ct_run() {
    pve_run pct exec "$VMID" -- "$@"
}

# Run a shell snippet inside the container. Use for pipes, redirects and heredocs,
# which `pct exec` alone cannot express.
#
# The snippet travels base64-encoded, which looks like overkill until you count
# the parsers between here and there: this shell, then ssh (which joins its
# arguments and lets the REMOTE shell re-split them), then `pct exec`, then the
# shell inside the container. `printf %q` survives the first three for a
# single-line snippet, but a multi-line one becomes $'...\n...' and `pct exec`
# hands bash an empty -c argument:
#
#   bash: -c: option requires an argument
#
# Base64 is [A-Za-z0-9+/=] only — no spaces, quotes or newlines for any layer to
# interpret — so a heredoc arrives exactly as written. The inner `bash -s` reads
# the script from stdin, so a snippet must not itself read stdin.
ct_sh() {
    local encoded
    encoded="$(printf '%s' "$1" | base64 | tr -d '\n')"
    pve_run pct exec "$VMID" -- bash -lc \
        "$(printf '%q' "echo $encoded | base64 -d | bash -s")"
}

# Copy a local file into the container. `pct push` reads from the Proxmox host's
# filesystem, so the file goes to the host first.
ct_push() {
    local src="$1" dst="$2" mode="${3:-0644}" stage
    stage="/tmp/memhub-push.$$"
    scp -q "${SSH_OPTS[@]}" "$src" "$PVE_HOST:$stage"
    pve_run pct push "$VMID" "$stage" "$dst" --perms "$mode"
    pve_run rm -f "$stage"
}

ct_exists() {
    pve_run pct status "$VMID" >/dev/null 2>&1
}
