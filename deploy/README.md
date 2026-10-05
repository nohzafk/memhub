# deploy

Provisioning and releases for the memhub container. This file is the order of
operations and the things that are easy to get wrong.

Everything runs **from a workstation**, over SSH to the Proxmox host. Nothing here
needs to run inside the container by hand.

```sh
deploy/provision-ct.sh    # once: create the container, install uv, create the store
deploy/deploy.sh          # every release: code, units, health check
deploy/smoke.sh           # the end-to-end round trip over the network
```

Every host, address and id is an environment variable with a placeholder default —
`PVE_HOST`, `VMID`, `CT_IP`, `GATEWAY`, `BIND_SRC`, `MEMHUB_URL`,
`MEMHUB_TOKEN_FILE`. Set them; do not edit the defaults.

## What is pinned, and where

`pins.env` holds the versions memhub downloads — today only uv. An upgrade is an
edit there plus a `deploy.sh` run, and no other file names a version. `deploy.sh`
verifies each download's sha256 and refuses to install a file that does not match,
so a moved tag or a truncated transfer fails loudly instead of producing a server
that answers plausible nonsense.

## Layout in the container

| Path | Holds | Backed up |
|---|---|---|
| `/opt/memhub` | the release: `memhub/`, `vendor/`, `.venv` | no — redeployable |
| `/var/lib/memhub/optmem` | **the store** | yes — this is the only irreplaceable data |
| `/var/lib/memhub/daily` | the retired day log, no longer served | yes — kept as an archive |
| `/etc/memhub/token` | the bearer token | no |

## Things that will bite

- **The container needs `nesting=1`.** `ProtectSystem`, `PrivateTmp` and
  `ReadWritePaths` require a private mount namespace, and an unprivileged
  container cannot create one without it. The unit dies instantly with
  `226/NAMESPACE` ("Failed to set up mount namespacing"). `provision-ct.sh` sets
  the feature at creation and repairs a container that lacks it; the flag applies
  at boot, so an existing container is rebooted.
- **The smoke test writes, permanently.** It appends a memory dated today, and
  `memo import` refuses older dates afterwards — so a smoke run blocks a seeding
  import of any dump containing older dates until the store is re-created.
- **`memo init` runs exactly once**, in `provision-ct.sh`. Creating the store is
  creating the identity, which is why `deploy.sh` never touches either store and
  the API does not serve `init` at all.
- **The `ME` patch is applied after every push**, because a release overwrites
  `vendor/memo.py` with the repo's unpatched copy. `deploy.sh` verifies the patch
  landed and dies if upstream changed that line.
- **The unit keeps `/` read-only** (`ProtectSystem=strict`) with `/var/lib/memhub`
  as the single writable path. That is why `memhub.service` runs
  `.venv/bin/uvicorn` directly instead of `uv run`: `uv run` wants the network and
  a writable `/opt/memhub` at boot.

## Backups

The stores are the only thing here that cannot be rebuilt. Back up
`/var/lib/memhub` — everything else is a redeploy or a download away. Verify a
restore before trusting it: a backup that has never been restored is a hypothesis.

## Token rotation

1. `openssl rand -hex 32`
2. Put the new value where each machine reads it from, and install it there.
3. `deploy/deploy.sh` — it pushes the new value and restarts the service.

The token is written through a file at every hop, never as a command-line
argument: an argv is visible in `ps` to every user on the host.
