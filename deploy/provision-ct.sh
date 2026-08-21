#!/usr/bin/env bash
# Create the memhub CT on the Proxmox host. Run once, from a workstation.
#
#   deploy/provision-ct.sh
#
# Idempotent: every step checks first, so a re-run after a failure continues
# instead of starting over. It never touches the stores — deploy.sh installs
# releases, and this script only builds the box they run on.
#
# Rollback for the whole script: `pct destroy 106` on the host. Nothing depends
# on the container until deploy.sh has run.
set -euo pipefail

# shellcheck source=deploy/lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
# shellcheck source=deploy/pins.env
source "$(dirname "${BASH_SOURCE[0]}")/pins.env"

BIND_SRC="${BIND_SRC:-/srv/memhub}"
TEMPLATE="${TEMPLATE:-local:vztmpl/debian-12-standard_12.0-1_amd64.tar.zst}"
GATEWAY="${GATEWAY:-10.0.0.1}"
RESTIC_PATHS="${RESTIC_PATHS:-/etc/restic-backup/paths}"

# uid 1000 inside an unprivileged CT is uid 101000 on the host. The bind source
# has to be owned by the mapped uid, or the service cannot write its own store.
HOST_UID="101000"

main() {
    require ssh scp
    pve_run true || die "cannot reach $PVE_HOST over SSH."

    create_bind_source
    create_ct
    ensure_nesting
    start_ct
    install_packages
    create_service_user
    write_env_file
    init_store
    add_to_restic

    log "CT $VMID provisioned. Next: deploy/deploy.sh"
}

create_bind_source() {
    log "bind source $BIND_SRC on the host"
    pve_run mkdir -p "$BIND_SRC"
    pve_run chown -R "$HOST_UID:$HOST_UID" "$BIND_SRC"
}

create_ct() {
    if ct_exists; then
        log "CT $VMID exists; leaving it alone"
        return
    fi
    pve_run test -f "/var/lib/vz/template/cache/$(basename "${TEMPLATE#*:vztmpl/}")" ||
        warn "template $TEMPLATE may be missing; download it with:
       ssh $PVE_HOST pveam download local $(basename "${TEMPLATE#*:vztmpl/}")"

    log "creating CT $VMID at $CT_IP"
    pve_run pct create "$VMID" "$TEMPLATE" \
        --hostname memhub --unprivileged 1 --onboot 1 \
        --features nesting=1 \
        --cores 2 --memory 1536 --swap 512 \
        --rootfs local-lvm:8 \
        --net0 "name=eth0,bridge=vmbr0,firewall=1,gw=$GATEWAY,ip=$CT_IP/24,type=veth" \
        --mp0 "$BIND_SRC,mp=$DATA_DIR"
}

# systemd's ProtectSystem, PrivateTmp and ReadWritePaths all need a private
# mount namespace, which an unprivileged CT cannot create unless nesting is
# allowed. Without it both units die instantly with 226/NAMESPACE:
#
#   Failed to set up mount namespacing: /run/systemd/unit-root/proc: Permission denied
#
# A fresh CT gets the flag at creation; this step is for one that already exists.
ensure_nesting() {
    if pve_run pct config "$VMID" | grep -q "^features:.*nesting=1"; then
        return
    fi
    log "enabling nesting=1 (systemd sandboxing needs it)"
    pve_run pct set "$VMID" --features nesting=1
    if [[ "$(pve_run pct status "$VMID")" == *running* ]]; then
        log "rebooting CT $VMID so nesting takes effect"
        pve_run pct reboot "$VMID"
    fi
}


start_ct() {
    if [[ "$(pve_run pct status "$VMID")" != *running* ]]; then
        log "starting CT $VMID"
        pve_run pct start "$VMID"
    fi
    # The container reports running before its network is up, and every step
    # below needs DNS. Wait for it rather than failing on the first apt call.
    local waited=0
    while (( waited < 60 )); do
        if ct_sh 'getent hosts deb.debian.org >/dev/null'; then
            return
        fi
        sleep 2
        waited=$(( waited + 2 ))
    done
    die "CT $VMID still has no working DNS after ${waited}s."
}

install_packages() {
    if ct_run test -x /usr/bin/rg && ct_run test -x /usr/local/bin/uv; then
        log "packages and uv already installed"
        return
    fi
    log "installing python3, curl, ripgrep, uv $UV_VERSION"
    # LC_ALL=C because the image has no generated locales, and apt's warnings
    # about it drown the output that matters.
    ct_sh 'LC_ALL=C DEBIAN_FRONTEND=noninteractive apt-get update -qq'
    ct_sh 'LC_ALL=C DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
        python3 curl ca-certificates ripgrep rsync tar'
    # uv is not packaged for Debian 12. The installer is pinned by version, and
    # placed system-wide so the service user and root see the same binary.
    ct_sh "curl -LsSf https://astral.sh/uv/$UV_VERSION/install.sh \
        | env UV_INSTALL_DIR=/usr/local/bin INSTALLER_NO_MODIFY_PATH=1 sh"
    ct_run /usr/local/bin/uv --version
}

create_service_user() {
    if ct_sh "id -u $SERVICE_USER >/dev/null 2>&1"; then
        log "user $SERVICE_USER exists"
    else
        log "creating user $SERVICE_USER (uid 1000)"
        ct_sh "useradd --uid 1000 --create-home --shell /usr/sbin/nologin $SERVICE_USER"
    fi
    ct_sh "mkdir -p $DATA_DIR && chown $SERVICE_USER:$SERVICE_USER $DATA_DIR"
    ct_sh "mkdir -p /etc/memhub && chmod 0755 /etc/memhub"
}

write_env_file() {
    log "/etc/memhub/env"
    ct_sh "cat > /etc/memhub/env <<'ENV'
MEMHUB_DATA=$DATA_DIR
MEMHUB_TOKEN_FILE=/etc/memhub/token
# Projects whose memories are machine-bound by default. Whether a
# project is machine-bound is a property of the project, identical on every
# machine, so the list lives here once instead of in each client's environment
# where three copies could disagree about one repo.
MEMHUB_HOST_PROJECTS=the second config repo:the Nix config repo
# Nothing may write bytecode into a read-only /opt (ProtectSystem=strict).
PYTHONDONTWRITEBYTECODE=1
ENV"
}

init_store() {
    # Creating the store IS creating the identity, so it happens once, here,
    # deliberately — never from the API, which does not serve `memo init` — and never
    # from deploy.sh, which runs on every release.
    if ct_run test -f "$DATA_DIR/optmem/LOG.txt"; then
        log "OptMem store exists at $DATA_DIR/optmem"
    else
        log "creating the OptMem store"
        ct_push "$(repo_root)/server/vendor/memo.py" /tmp/memo.py 0755
        ct_sh "runuser -u $SERVICE_USER -- env MEMORY_DIR=$DATA_DIR/optmem python3 /tmp/memo.py init"
        ct_sh "rm -f /tmp/memo.py"
        # memo names itself by its own path, so init writes "/tmp/memo.py config
        # NAME=VALUE" into the store's config header — a path that is already
        # gone. `config` is the one file in the store meant to be edited by hand,
        # and the client reaches this store as `memo -g`.
        ct_sh "sed -i 's|Edit with \`[^\`]*\`|Edit with \`memo -g config NAME=VALUE\`|' \
            $DATA_DIR/optmem/config"
    fi
    ct_sh "runuser -u $SERVICE_USER -- mkdir -p $DATA_DIR/daily"
}

add_to_restic() {
    # vzdump skips bind mounts, so restic is the only backup path for the data
    # The include list lives on the host, not in the container.
    if pve_run test -f "$RESTIC_PATHS"; then
        if pve_run grep -qxF "$BIND_SRC" "$RESTIC_PATHS"; then
            log "restic already covers $BIND_SRC"
        else
            log "adding $BIND_SRC to $RESTIC_PATHS"
            ct_backup_note
            pve_run "printf '%s\n' '$BIND_SRC' >> '$RESTIC_PATHS'"
        fi
    else
        warn "$RESTIC_PATHS not found on the host — add $BIND_SRC to the restic
       include list by hand, or the stores are not backed up."
    fi
}

ct_backup_note() {
    pve_run cp -n "$RESTIC_PATHS" "$RESTIC_PATHS.pre-memhub" || true
}

main "$@"
