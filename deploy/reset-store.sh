#!/usr/bin/env bash
# Wipe the shared store and create it again. First step of a re-seed.
#
#   deploy/reset-store.sh --yes
#
# This DESTROYS every memory and every day file on the server. It exists because
# OptMem is append-only: a memory cannot be deleted, only compressed, so the only
# way to remove one is to start the identity over.
#
# A re-seed needs exactly this. The smoke test appends a memory dated today, and
# `memo import` refuses a line dated before the store's newest memory — so
# seeding a store that has been smoke-tested fails on its first line.
#
# It does NOT touch the archives on any Mac.
set -euo pipefail

# shellcheck source=deploy/lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

main() {
    [[ "${1:-}" == "--yes" ]] || die "this destroys every shared memory. Re-run with --yes if that is what you want."

    require ssh
    pve_run true || die "cannot reach $PVE_HOST over SSH."
    ct_exists || die "CT $VMID does not exist."

    log "what is there now"
    curl -fsS --max-time 5 "http://$CT_IP:8900/health" || true
    echo

    log "stopping memhub (the store must not move under a live writer)"
    ct_sh "systemctl stop memhub.service"

    log "removing the store and the day files"
    ct_sh "rm -rf $DATA_DIR/optmem"
    # daily/ holds one file per day, plain markdown. Remove the dated logs and
    # leave anything a human put there.
    ct_sh "rm -f $DATA_DIR/daily/[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9].md"

    log "creating a fresh store"
    ct_push "$(repo_root)/server/vendor/memo.py" /tmp/memo.py 0755
    ct_sh "runuser -u $SERVICE_USER -- env MEMORY_DIR=$DATA_DIR/optmem python3 /tmp/memo.py init >/dev/null"
    ct_sh "rm -f /tmp/memo.py"
    ct_sh "sed -i 's|Edit with \`[^\`]*\`|Edit with \`memo -g config NAME=VALUE\`|' \\
        $DATA_DIR/optmem/config"
    ct_sh "runuser -u $SERVICE_USER -- mkdir -p $DATA_DIR/daily"

    log "starting memhub"
    ct_sh "systemctl start memhub.service"

    local i=0
    while (( i < 30 )); do
        if curl -fsS --max-time 5 "http://$CT_IP:8900/health" 2>/dev/null | grep -q '"ok":true'; then
            log "the store is empty and serving:"
            printf '    %s\n' "$(curl -fsS "http://$CT_IP:8900/health")"
            return
        fi
        sleep 2
        i=$(( i + 2 ))
    done
    die "memhub did not come back after ${i}s."
}

main "$@"
