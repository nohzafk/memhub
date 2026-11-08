#!/usr/bin/env bash
# Pull a read-only copy of the shared store to this machine.
#
#   memhub-mirror              # refresh the mirror
#   memhub-mirror --verify     # check the current mirror and say what it holds
#
# Why a mirror and not a second writer: the shared store has one writer, the
# hub, which hands out ids that are never reused. Two stores that both accept
# writes would hand out the same ids to different rules. A mirror is a *copy* —
# nothing writes to it, so it cannot fork. That is the whole reason it is safe,
# and the reason it must stay read-only.
#
# What it is for:
#   * a second copy of the rules and their git history, independent of the
#     server's own backups
#   * offline reads:  MEMORY_DIR=<mirror>/rules memo -l wake
#   * disaster recovery: promote it (see below)
#
# A shared write made while the server is unreachable waits in the client's
# outbox and is sent on the next shared call. Do NOT write into the mirror:
# that is exactly the fork this design avoids.
#
# Promoting a mirror to be the store, if the server is gone for good:
#   1. Copy <mirror>/rules into the new server's data dir, as <data>/rules.
#   2. chown it to the service user and make it writable again, then start it.
#   3. Every rule keeps its id, NEXT keeps the next one, and META.jsonl and
#      EPOCH stay valid. That is what a byte copy buys, and what a filtered
#      copy could not.
set -euo pipefail

MEMHUB_URL="${MEMHUB_URL:-http://127.0.0.1:8900}"
MEMHUB_TOKEN_FILE="${MEMHUB_TOKEN_FILE:-$HOME/.config/memhub/token}"
MIRROR_DIR="${MEMHUB_MIRROR_DIR:-$HOME/.agents/memory/mirror}"

# A RULES.txt line is blank, a `//` comment, or a rule. Same grammar as
# rules.py, which refuses to read a store holding any other line.
RULE_LINE='^[[:space:]]*$|^//|^#[0-9]+ [0-9]{4}-[0-9]{2}-[0-9]{2} .+$'

die() {
    printf 'memhub-mirror: %s\n' "$*" >&2
    exit 1
}

# A mirror is only worth having if it is whole. Every check answers one question:
# would this still work if it were promoted to be the store?
verify_tree() {
    local dir="$1/rules" bad rules next

    [[ -f "$dir/RULES.txt" ]] || { echo "no rules/RULES.txt"; return 1; }
    [[ -f "$dir/NEXT" ]] || { echo "no rules/NEXT"; return 1; }
    [[ -d "$dir/.git" ]] || { echo "no rules/.git: the history is missing"; return 1; }

    # NEXT is the id the next write gets; rules.py refuses a store whose NEXT
    # is not a positive integer, so a promoted mirror holding one would too.
    next="$(< "$dir/NEXT")"
    [[ "$next" =~ ^[[:space:]]*0*[1-9][0-9]*[[:space:]]*$ ]] || { echo "rules/NEXT does not hold a positive id: '$next'"; return 1; }

    # Bytes, not characters: a rule's UTF-8 text must not trip the locale.
    bad="$(LC_ALL=C grep -nEv "$RULE_LINE" "$dir/RULES.txt" | head -n 1 || true)"
    if [[ -n "$bad" ]]; then
        echo "RULES.txt line ${bad%%:*} is not a rule: ${bad#*:}"
        return 1
    fi
    rules="$(LC_ALL=C grep -c '^#' "$dir/RULES.txt" || true)"

    # META.jsonl is optional — a store with no scoped write has none — but if
    # it is there it must parse, or the annotations it feeds would be wrong.
    if [[ -f "$dir/META.jsonl" ]] && ! python3 -c '
import json, sys
for line in open(sys.argv[1], encoding="utf-8"):
    if line.strip():
        json.loads(line)
' "$dir/META.jsonl" 2>/dev/null; then
        echo "META.jsonl is not valid JSONL"
        return 1
    fi

    echo "$rules rules"
}

if [[ "${1:-}" == "--verify" ]]; then
    if ! summary="$(verify_tree "$MIRROR_DIR")"; then
        die "the mirror at $MIRROR_DIR is not usable: $summary"
    fi
    printf 'memhub-mirror: %s at %s\n' "$summary" "$MIRROR_DIR"
    exit 0
fi

[[ -r "$MEMHUB_TOKEN_FILE" ]] || die "no token at $MEMHUB_TOKEN_FILE"
token="$(tr -d '[:space:]' < "$MEMHUB_TOKEN_FILE")"

mkdir -p "$(dirname "$MIRROR_DIR")"
staging="$(mktemp -d "$(dirname "$MIRROR_DIR")/.mirror-XXXXXX")"
trap 'chmod -R u+w "$staging" 2>/dev/null || true; rm -rf "$staging"' EXIT

# Fetch and unpack into staging. Nothing touches the existing mirror until the
# new copy has been checked: a half-downloaded store must never replace a whole
# one, which is what makes an unverified backup worse than none at all.
curl -fsS --connect-timeout 5 --max-time 300 \
    -H "Authorization: Bearer $token" \
    "$MEMHUB_URL/export" | tar xz -C "$staging" \
    || die "could not fetch the store from $MEMHUB_URL"

if ! summary="$(verify_tree "$staging")"; then
    die "the downloaded store is not usable, keeping the old mirror: $summary"
fi

if [[ -d "$MIRROR_DIR" ]]; then
    chmod -R u+w "$MIRROR_DIR"
    rm -rf "$MIRROR_DIR.previous"
    mv "$MIRROR_DIR" "$MIRROR_DIR.previous"
fi
mv "$staging" "$MIRROR_DIR"
trap - EXIT
# Read-only, because the one rule of a mirror is that nothing writes to it.
chmod -R a-w "$MIRROR_DIR"

printf 'memhub-mirror: %s -> %s\n' "$summary" "$MIRROR_DIR"
