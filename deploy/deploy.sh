#!/usr/bin/env bash
# Install a memhub release into the container. Idempotent — run it for every
# release, and after any change to pins.env.
#
#   deploy/deploy.sh                 # full deploy, gate G1 enforced first
#   deploy/deploy.sh --skip-checks   # skip pytest/ruff (CI already ran them)
#
# Order matters: code, then the things it needs, then the units, then a health
# check that fails loudly. Nothing here touches either store — the stores are
# created once by provision-ct.sh and only ever written through the API.
set -euo pipefail

# shellcheck source=deploy/lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
# shellcheck source=deploy/pins.env
source "$(dirname "${BASH_SOURCE[0]}")/pins.env"

# sops-nix renders the shared token here on both workstations.
TOKEN_FILE="${MEMHUB_TOKEN_FILE:-/run/secrets/memhub-token}"
SKIP_CHECKS="no"

main() {
    [[ "${1:-}" == "--skip-checks" ]] && SKIP_CHECKS="yes"

    require ssh scp
    pve_run true || die "cannot reach $PVE_HOST over SSH."
    ct_exists || die "CT $VMID does not exist. Run deploy/provision-ct.sh first."

    run_gate_g1
    push_code
    patch_memo_scope
    sync_venv
    install_token
    install_units
    check_health

    log "deployed. Smoke test: deploy/smoke.sh"
}

# Gate G1 blocks a deploy. Enforce it here rather than trusting
# whoever is at the keyboard to remember.
run_gate_g1() {
    if [[ "$SKIP_CHECKS" == "yes" ]]; then
        warn "skipping gate G1 at the operator's request"
        return
    fi
    log "gate G1: pytest and ruff"
    require uv
    ( cd "$(repo_root)/server" \
        && uv run pytest -q \
        && uv run ruff check . ../tools ../client \
        && uv run ruff format --check . ../tools ../client ) \
        || die "gate G1 failed. Fix it before deploying."
}

push_code() {
    log "pushing server/ to $APP_DIR"
    local tar="/tmp/memhub-release.$$.tgz"
    tar czf "$tar" -C "$(repo_root)/server" \
        --exclude='.venv' --exclude='__pycache__' --exclude='.pytest_cache' \
        --exclude='.ruff_cache' --exclude='tests' .
    ct_sh "mkdir -p $APP_DIR"
    ct_push "$tar" /tmp/memhub-release.tgz 0600
    ct_sh "tar xzf /tmp/memhub-release.tgz -C $APP_DIR && rm -f /tmp/memhub-release.tgz"
    ct_sh "chown -R root:root $APP_DIR"
    rm -f "$tar"
}

# Every continuation command the server-side memo prints — `memo -g wake 2 296`,
# `memo -g nap 16-31 "..."` — has to route back to the shared store from any cwd
# on any machine. Upstream memo names itself by its own path, which would print
# a path that exists only inside the container.
patch_memo_scope() {
    local memo="$APP_DIR/vendor/memo.py"
    if ct_sh "grep -qxF 'ME = \"memo -g\"' $memo"; then
        log "memo.py already scoped to 'memo -g'"
        return
    fi
    log "patching vendor/memo.py: ME = \"memo -g\""
    ct_sh "sed -i 's|^ME = pretty(__file__)$|ME = \"memo -g\"|' $memo"
    ct_sh "grep -qxF 'ME = \"memo -g\"' $memo" \
        || die "the ME patch did not apply — upstream memo.py changed that line."
}

sync_venv() {
    log "uv sync --frozen"
    ct_sh "cd $APP_DIR && /usr/local/bin/uv sync --frozen --no-dev"
    ct_sh "test -x $APP_DIR/.venv/bin/uvicorn" || die "uv sync produced no uvicorn."
}

install_token() {
    [[ -r "$TOKEN_FILE" ]] || die "no token at $TOKEN_FILE.
       sops-nix renders it on both workstations; set MEMHUB_TOKEN_FILE to
       override. Generate one with: openssl rand -hex 32"
    local token
    token="$(tr -d '[:space:]' < "$TOKEN_FILE")"
    [[ -n "$token" ]] || die "$TOKEN_FILE is empty."

    log "writing /etc/memhub/token"
    # Through a file, never on a command line: an argv is visible in `ps` to
    # everyone on the host for as long as the call runs.
    local stage="/tmp/memhub-token.$$"
    # $stage is a local variable naming the remote path, so expanding it here is
    # what we want. The token itself goes over stdin, never into the command.
    # shellcheck disable=SC2029
    printf '%s\n' "$token" | ssh "${SSH_OPTS[@]}" "$PVE_HOST" "cat > $stage && chmod 0600 $stage"
    pve_run pct push "$VMID" "$stage" /etc/memhub/token --perms 0640
    pve_run rm -f "$stage"
    ct_sh "chown root:$SERVICE_USER /etc/memhub/token"
}

install_units() {
    log "installing systemd units"
    ct_push "$(dirname "${BASH_SOURCE[0]}")/memhub.service" \
        /etc/systemd/system/memhub.service 0644
    ct_sh "systemctl daemon-reload"
    ct_sh "systemctl enable memhub.service >/dev/null"
    # Restart, not reload: a release changes the code it runs.
    ct_sh "systemctl restart memhub.service"
}

check_health() {
    log "waiting for http://$CT_IP:8900/health"
    local waited=0 body
    while (( waited < 120 )); do
        body="$(curl -fsS --max-time 5 "http://$CT_IP:8900/health" 2>/dev/null || true)"
        if [[ -n "$body" ]] && health_ok "$body"; then
            printf '    %s\n' "$body"
            log "healthy"
            return
        fi
        sleep 2
        waited=$(( waited + 2 ))
    done
    warn "last /health body: ${body:-<no answer>}"
    ct_sh "systemctl --no-pager --lines=20 status memhub.service" || true
    die "the service did not come up healthy within ${waited}s."
}

# `ok` means the store is there. There is nothing else to be healthy about.
health_ok() {
    python3 - "$1" <<'PY'
import json, sys
try:
    body = json.loads(sys.argv[1])
except ValueError:
    sys.exit(1)
sys.exit(0 if body.get("ok") else 1)
PY
}

verify_sha() {
    sha_matches "$1" "$2" || die "$1 failed its checksum. Expected $2.
       Refusing to install it — check pins.env against the upstream release."
}

sha_matches() {
    local got
    got="$(ct_sh "sha256sum '$1'" | awk '{print $1}')"
    [[ "$got" == "$2" ]]
}

main "$@"
