#!/usr/bin/env bash
# End-to-end check over the LAN — gate G2. Run from a workstation.
#
#   deploy/smoke.sh
#   MEMHUB_URL=http://memhub.example:8900 deploy/smoke.sh
#
# WARNING — this writes to the real store. It notes a marker rule, edits it,
# and drops it again: the wake ends up as it was, but the marker's id is used
# up for good (ids are never reused) and its three commits stay in the store's
# history. If a check fails before the drop, the marker stays in the wake;
# retire it with `memo -g drop <id>` (the id is printed).
set -euo pipefail

# shellcheck source=deploy/lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

MEMHUB_URL="${MEMHUB_URL:-http://$CT_IP:8900}"
TOKEN_FILE="${MEMHUB_TOKEN_FILE:-/run/secrets/memhub-token}"
MARKER="memhub smoke test $(date +%Y-%m-%dT%H:%M:%S)"
MARKER_ID=""

failures=0

main() {
    require curl python3
    [[ -r "$TOKEN_FILE" ]] || die "no token at $TOKEN_FILE (set MEMHUB_TOKEN_FILE)."
    TOKEN="$(tr -d '[:space:]' < "$TOKEN_FILE")"

    check_health
    check_memo_note
    check_memo_edit
    check_memo_recall
    check_memo_wake "$MARKER edited" present
    check_memo_drop
    check_memo_wake "$MARKER" absent

    echo
    if (( failures )); then
        die "$failures check(s) failed — gate G2 is not met."
    fi
    log "gate G2 met against $MEMHUB_URL"
    warn "this run used up rule id #$MARKER_ID; its commits stay in the store's history."
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
    local body count
    body="$(curl -fsS --max-time 5 "$MEMHUB_URL/health")" || { bad "unreachable"; return; }
    printf '    %s\n' "$body"
    if [[ "$(printf '%s' "$body" | field ok)" == "True" ]]; then
        ok "store"
    else
        bad "store"
    fi
    count="$(printf '%s' "$body" | field rule_count)"
    if [[ "$count" =~ ^[0-9]+$ ]]; then
        ok "rule_count $count"
    else
        bad "rule_count missing"
    fi
}

check_memo_note() {
    log "memo -g note"
    local out stdout
    out="$(run_tool memo note "$MARKER")" || { bad "note failed"; return; }
    stdout="$(printf '%s' "$out" | field stdout)"
    if [[ "$(printf '%s' "$out" | field exit_code)" == "0" \
        && "$stdout" =~ ^Saved\ as\ \#([0-9]+)\.$ ]]; then
        MARKER_ID="${BASH_REMATCH[1]}"
        ok "recorded as #$MARKER_ID"
    else
        bad "recorded: $stdout"
    fi
}

check_memo_edit() {
    log "memo -g edit"
    [[ -n "$MARKER_ID" ]] || { bad "no marker to edit"; return; }
    local out
    out="$(run_tool memo edit "$MARKER_ID" "$MARKER edited")" || { bad "edit failed"; return; }
    if [[ "$(printf '%s' "$out" | field exit_code)" == "0" ]]; then
        ok "edited #$MARKER_ID"
    else
        bad "edit: $(printf '%s' "$out" | field stderr)"
    fi
}

check_memo_recall() {
    log "memo -g recall"
    local out
    out="$(run_tool memo recall "$MARKER")" || { bad "recall failed"; return; }
    if printf '%s' "$out" | field stdout | grep -qF "#$MARKER_ID $(date +%F) $MARKER edited"; then
        ok "recall finds the edited marker"
    else
        bad "recall does not find it"
    fi
}

# The wake is one page: the whole file, grouped, with its size footer.
check_memo_wake() {
    local text="$1" want="$2" out doc
    log "memo -g wake ($want)"
    out="$(run_tool memo wake)" || { bad "wake failed"; return; }
    doc="$(printf '%s' "$out" | field stdout)"
    if ! printf '%s\n' "$doc" | grep -qE '^[0-9]+ rules, [0-9]+ bytes\.$'; then
        bad "wake has no size footer"
    fi
    local seen=absent
    printf '%s' "$doc" | grep -qF "$text" && seen=present
    if [[ "$seen" == "$want" ]]; then
        ok "wake: the marker is $want"
    else
        bad "wake: the marker is $seen, expected $want"
    fi
}

check_memo_drop() {
    log "memo -g drop"
    [[ -n "$MARKER_ID" ]] || { bad "no marker to drop"; return; }
    local out
    out="$(run_tool memo drop "$MARKER_ID" "smoke test marker")" || { bad "drop failed"; return; }
    if [[ "$(printf '%s' "$out" | field exit_code)" == "0" ]]; then
        ok "dropped #$MARKER_ID"
    else
        bad "drop: $(printf '%s' "$out" | field stderr)"
    fi
}

main "$@"
