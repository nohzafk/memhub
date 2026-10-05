#!/usr/bin/env bash
# Pull a read-only copy of the shared store to this machine.
#
#   memhub-mirror              # refresh the mirror
#   memhub-mirror --verify     # check the current mirror and say what it holds
#
# Why a mirror and not a second writer: OptMem is position-is-identity, so two
# stores that both accept writes are two identities that can never be merged
#. A mirror is a *copy* — nothing writes to it, so it cannot fork. That
# is the whole reason it is safe, and the reason it must stay read-only.
#
# What it is for:
#   * a second copy of the memories, independent of the server's own backups
#   * offline reads:  MEMORY_DIR=<mirror>/optmem memo -l wake
#   * disaster recovery: promote it (see below)
#
# If the server is unreachable and a fact must not wait, write it to the local store
# with `memo -l note "..."` and promote it later with tools/promote_local.py. Do
# NOT write into the mirror: that is exactly the fork this design avoids.
#
# Promoting a mirror to be the store, if the server is gone for good:
#   1. Copy <mirror>/optmem into the new server's data dir.
#   2. chown them to the service user and make them writable again, then start it.
#   3. Every memory keeps its id, so TREE/ and META.jsonl stay valid. That is
#      what a byte copy buys, and what a filtered copy could not.
set -euo pipefail

MEMHUB_URL="${MEMHUB_URL:-http://127.0.0.1:8900}"
MEMHUB_TOKEN_FILE="${MEMHUB_TOKEN_FILE:-$HOME/.config/memhub/token}"
MIRROR_DIR="${MEMHUB_MIRROR_DIR:-$HOME/.agents/memory/mirror}"

LOG_REC=320

die() {
    printf 'memhub-mirror: %s\n' "$*" >&2
    exit 1
}

# A mirror is only worth having if it is whole. Every check answers one question:
# would this still work if it were promoted to be the store?
verify_tree() {
    local dir="$1" log="$1/optmem/LOG.txt" size records

    [[ -f "$log" ]] || { echo "no optmem/LOG.txt"; return 1; }
    # GNU stat on Linux and under the Nix wrapper; BSD stat when the script is
    # run by hand on a workstation whose PATH has no coreutils.
    size="$(stat -c %s "$log" 2>/dev/null || stat -f%z "$log")"
    if (( size % LOG_REC != 0 )); then
        echo "LOG.txt is $size bytes, not a multiple of $LOG_REC — truncated"
        return 1
    fi
    records=$(( size / LOG_REC ))
    [[ -f "$dir/optmem/config" ]] || { echo "no optmem/config"; return 1; }

    # META.jsonl is optional — a store older than scope has none — but if it is
    # there it must parse, or the annotations it feeds would be wrong.
    if [[ -f "$dir/optmem/META.jsonl" ]] && ! python3 -c '
import json, sys
for line in open(sys.argv[1], encoding="utf-8"):
    if line.strip():
        json.loads(line)
' "$dir/optmem/META.jsonl" 2>/dev/null; then
        echo "META.jsonl is not valid JSONL"
        return 1
    fi

    echo "$records memories"
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
