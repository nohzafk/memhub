#!/usr/bin/env bash
# Wipe the shared rules store and create it again: a new identity.
#
#   deploy/reset-store.sh --yes
#
# This DESTROYS every rule on the server, its history and its scope rows. A
# single rule is retired with `memo -g drop <id>`; this is for starting over,
# e.g. before committing a migrated RULES.txt as the store's first content.
#
# It does NOT touch the retired optmem/ and daily/ archives, nor any Mac.
set -euo pipefail

# shellcheck source=deploy/lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

main() {
    [[ "${1:-}" == "--yes" ]] || die "this destroys every shared rule. Re-run with --yes if that is what you want."

    require ssh
    pve_run true || die "cannot reach $PVE_HOST over SSH."
    ct_exists || die "CT $VMID does not exist."

    log "what is there now"
    curl -fsS --max-time 5 "http://$CT_IP:8900/health" || true
    echo

    log "stopping memhub (the store must not move under a live writer)"
    ct_sh "systemctl stop memhub.service"

    log "removing the store"
    ct_sh "rm -rf $DATA_DIR/rules"

    log "creating a fresh store"
    ct_push "$(repo_root)/server/memhub/rules.py" /tmp/rules.py 0755
    ct_sh "runuser -u $SERVICE_USER -- env MEMORY_DIR=$DATA_DIR/rules python3 /tmp/rules.py init >/dev/null"
    ct_sh "rm -f /tmp/rules.py"

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
