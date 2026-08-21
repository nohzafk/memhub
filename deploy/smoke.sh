#!/usr/bin/env bash
# End-to-end check over the LAN — gate G2. Run from a workstation.
#
#   deploy/smoke.sh
#   MEMHUB_URL=http://memhub.example:8900 deploy/smoke.sh
#
# WARNING — this writes. OptMem is append-only by design, so the memory this
# records cannot be deleted, only compressed. It is dated today, and `memo
# import` refuses a line dated before the last memory in the store. So a smoke
# run BLOCKS a seeding import of any dump containing older dates until the
# store is re-created (`rm -rf optmem && memo init` in the container).
# Smoke first, seed second, and re-init in between.
set -euo pipefail

# shellcheck source=deploy/lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

MEMHUB_URL="${MEMHUB_URL:-http://$CT_IP:8900}"
TOKEN_FILE="${MEMHUB_TOKEN_FILE:-/run/secrets/memhub-token}"
MARKER="memhub smoke test $(date +%Y-%m-%dT%H:%M:%S)"

failures=0

main() {
    require curl python3
    [[ -r "$TOKEN_FILE" ]] || die "no token at $TOKEN_FILE (set MEMHUB_TOKEN_FILE)."
    TOKEN="$(tr -d '[:space:]' < "$TOKEN_FILE")"

    check_health
    check_memo_note
    check_memo_wake
    check_daylog_note
    check_daylog_grep
    check_daylog_read

    echo
    if (( failures )); then
        die "$failures check(s) failed — gate G2 is not met."
    fi
    log "gate G2 met against $MEMHUB_URL"
    warn "this run appended a memory dated $(date +%F). Re-create the store
       before any seeding import, or 'memo import' will refuse the older dates."
}

# --- plumbing ---------------------------------------------------------------

# One /run call. Prints the tool's stdout; fails the check on a non-zero exit.
run_tool() {
    local tool="$1"; shift
    local body
    body="$(python3 - "$tool" "$@" <<'PY'
import json, sys
print(json.dumps({"tool": sys.argv[1], "argv": sys.argv[2:]}))
PY
)"
    curl -fsS --max-time 30 \
        -H "Authorization: Bearer $TOKEN" -H 'content-type: application/json' \
        -d "$body" "$MEMHUB_URL/run"
}

# One field of a JSON body, as text. A JSON null prints as the empty string,
# not as Python's "None": `index_error` is null on every healthy write, and a
# non-empty value is what the caller reads as failure.
field() {
    # The program comes in as -c, not as a heredoc: a heredoc would BE stdin,
    # and stdin is where the JSON body arrives.
    python3 -c '
import json, sys
value = json.load(sys.stdin).get(sys.argv[1])
print("" if value is None else value)
' "$1"
}

ok() {
    printf '  \033[1;32mok\033[0m   %s\n' "$1"
}

bad() {
    printf '  \033[1;31mFAIL\033[0m %s\n' "$1" >&2
    failures=$((failures + 1))
}

# --- the checks -------------------------------------------------------------

check_health() {
    log "/health"
    local body
    body="$(curl -fsS --max-time 5 "$MEMHUB_URL/health")" || { bad "unreachable"; return; }
    printf '    %s\n' "$body"
    if [[ "$(printf '%s' "$body" | field ok)" == "True" ]]; then
        ok "store"
    else
        bad "store"
    fi

}

check_memo_note() {
    log "memo -g note"
    local out
    out="$(run_tool memo note "$MARKER")" || { bad "note failed"; return; }
    if [[ "$(printf '%s' "$out" | field exit_code)" == "0" ]]; then
        ok "recorded"
    else
        bad "recorded"
    fi
}

check_memo_wake() {
    log "memo -g wake"
    local out
    out="$(run_tool memo wake)" || { bad "wake failed"; return; }
    if printf '%s' "$out" | field stdout | grep -qF "$MARKER"; then
        ok "wake shows the new memory"
    else
        bad "wake does not show it"
    fi
}

check_daylog_note() {
    log "daylog -g note"
    local out
    out="$(run_tool daylog note "$MARKER")" || { bad "daylog note failed"; return; }
    if [[ "$(printf '%s' "$out" | field exit_code)" == "0" ]]; then
        ok "recorded"
    else
        bad "recorded"
    fi
}

# grep and read are the retrieval half, added to the canonical daylog in
# the Nix config repo and re-vendored here. Both are real assertions now: an
# earlier revision only warned, because the vendored copy was the bash one that
# predated the verbs. A warning was right while the gap was known and expected,
# and is wrong now — it would let a deploy that silently shipped the old file
# pass gate G2.
check_daylog_grep() {
    log "daylog -g grep"
    local out code
    out="$(run_tool daylog grep "smoke test")" || { bad "request failed"; return; }
    code="$(printf '%s' "$out" | field exit_code)"
    if [[ "$code" != "0" ]]; then
        bad "daylog grep exits $code — is the vendored daylog.py the current one?"
        return
    fi
    if printf '%s' "$out" | field stdout | grep -qF "$MARKER"; then
        ok "grep finds the entry"
    else
        bad "grep found nothing"
    fi
}

# `read` with no argument means today, which is the day check_daylog_note just
# wrote to. This is the verb that makes a shared day readable from a workstation at all:
# it returns content, where `path` returns a filename on the server that the caller
# cannot open.
check_daylog_read() {
    log "daylog -g read"
    local out code
    out="$(run_tool daylog read)" || { bad "request failed"; return; }
    code="$(printf '%s' "$out" | field exit_code)"
    if [[ "$code" != "0" ]]; then
        bad "daylog read exits $code — is the vendored daylog.py the current one?"
        return
    fi
    if printf '%s' "$out" | field stdout | grep -qF "$MARKER"; then
        ok "read returns today in full"
    else
        bad "read does not show the entry just written"
    fi
}

main "$@"
