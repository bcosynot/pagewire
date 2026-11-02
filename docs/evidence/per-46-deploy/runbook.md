# PER-46 runbook — deploying the relay to the NixOS LXC

Each step says **which acceptance criterion it closes** and is marked **NOT YET RUN**,
unless it is marked **DONE** with the evidence beside it.

**Nothing in this runbook has been executed against the box.** The build environment that
produced this branch has no `nix` (and ~4.5 GiB free, too little to install it and evaluate
a NixOS system), so every criterion that needs evaluation or a running service is parked
here rather than claimed. What *was* done is in the repo: the flake, the module, the
shipped tunables file, and the tests that pin the last of those to the code.

Reaching the box is not a blocker and never was: the `~/.ssh/base_server_lxc` key the
config repo's AGENTS.md names does not exist and is not needed. The relay's target is
**CT 99999 on the Proxmox host `pve`** (`nixos`, `26.11.20260802`, `192.168.0.112`), and
the already-present forced-`pct` key reaches it. Two wrinkles found on 2026-11-02:

- Plain `ssh pve …` now fails with `Bad owner or permissions on
  /etc/ssh/ssh_config.d/20-systemd-ssh-proxy.conf` — a system-wide include whose file is
  owned by an unmapped uid. Pass the profile config explicitly to skip the include:
  `ssh -F ~/.ssh/config pve …`.
- `pct exec` on NixOS has no PATH, so run a shell and set it first:
  `ssh -F ~/.ssh/config pve exec 99999 -- /bin/sh < script.sh` with
  `export PATH=/run/current-system/sw/bin:/run/wrappers/bin:$PATH` as the script's first line.

---

## What is already in the repository (the agent half)

- `flake.nix` — `packages.<system>.default` (the app, `buildPythonApplication`, with the
  console scripts the units start) and `nixosModules.default` (the module below).
- `nixos/pagewire.nix` — the module: the `pagewire.service` (uvicorn on loopback), the
  `pagewire-sweep` oneshot + timer (derive, then deliver), the plain-HTTP nginx listener
  on `lanPort`, the user/group, the tmpfiles rules and the firewall port.
- `deploy/pagewire.env.example` — every tunable and both tokens, referenced BY PATH only.
- `src/pagewire/sessions.py` gained a `main` (the derive CLI the sweep runs); `app.py`
  gained `serve`; `pyproject.toml` gained `[project.scripts]`.
- `tests/test_deploy_config.py` and `tests/test_cli_entrypoints.py` pin the above.

`test_deploy_config.py` derives the set of `PAGEWIRE_*` names `config.py` reads straight
out of its source and fails if `deploy/pagewire.env.example` does not name every one of
them — that is criterion 6's offline half, and it is what keeps the two from drifting.

---

## 1. Publish the relay repo — DONE (owner-approved), re-check the lock here

Closes: the precondition for criteria 1 and 2.

The box has no GitHub credentials (`/root/.netrc` absent; `nix.conf` carries no
`access-tokens`), and every flake input it already builds from — `prodlog`, `jhop`,
`ouch`, `transistor` — is a public repo. `pagewire` was private, so the box could not
have fetched it. Vivek chose to make it public, matching that pattern; history was
scanned first for secrets and came back clean (the only `token`-shaped hits are the fake
`rw-evidence-token` / `hc-evidence-token` fixtures in the delivery evidence).

Watch for: `gh repo view bcosynot/pagewire --json visibility` → `PUBLIC` before the box
is asked to fetch it.

## 2. Wire the input into the config repo — NOT YET RUN

Closes: **criterion 1**.

`bcosynot/base-server` (private; branch `main` is the default, `unit` is what a local
checkout was on) gains, beside the existing app inputs:

```nix
pagewire.url = "github:bcosynot/pagewire";
```

and the module import in the LXC configuration:

```nix
imports = [ inputs.pagewire.nixosModules.default ];
services.pagewire = {
  enable = true;
  environmentFile = "/var/app/data/pagewire/pagewire.env";
};
```

Then let nix write the lock entry — this is the part that needs `nix`:

```sh
nix flake lock            # pins pagewire to a revision in flake.lock
```

Watch for: a `pagewire` node in `flake.lock` with a `rev` and `narHash`. **An input by
URL with no lock entry is not criterion 1**; `locked` must be present, not just `url`.

Paste the input line and its lock node into `criterion-1-input-and-lock.txt`.

## 3. Check and build the configuration — NOT YET RUN

Closes: **criterion 2**.

From a machine that has nix (a dev box, or the LXC itself):

```sh
nix flake check
nix build .#nixosConfigurations.base-server-lxc.config.system.build.toplevel
```

Watch for: both exiting 0. A module that references an option that does not exist, or a
package that will not build, fails here and not on the box. This step is the one this
environment could not run, so it is the likeliest place for a first-try error in
`nixos/pagewire.nix` — read the eval error rather than assuming.

**Partly covered by CI now.** `.github/workflows/nix.yml` in the relay repo runs
`nix flake check` on every push and PR, and the flake's `checks` output evaluates the
module inside a throwaway system — so a module error shows up on the PR rather than here.
That covers the first command **in the relay's own flake**. It does **not** cover this
step: the config repo's flake is a different flake, and
`nix build .#nixosConfigurations.base-server-lxc…` — the thing criterion 2 actually names
— lives in `base-server`, which has no CI at all.

Paste both transcripts into `criterion-2-flake-check.txt`.

## 4. Install the tunables file on the box — NOT YET RUN

Closes: the precondition for criteria 3, 4, 5 and 6's live half.

The file holds the device token and both service tokens, so it is installed out of band
and never in the flake:

```sh
# on the box, via: ssh -F ~/.ssh/config pve exec 99999 -- /bin/sh < script.sh
install -d -m 750 -o pagewire -g pagewire /var/app/data/pagewire
install -m 600 -o pagewire -g pagewire /dev/null /var/app/data/pagewire/pagewire.env
# then fill it from deploy/pagewire.env.example, replacing the REPLACE_… placeholders
```

Watch for: `PAGEWIRE_DB` in the file agreeing with `services.pagewire.stateDir`, and the
file's mode being `600`. A world-readable token file fails criterion 5.

## 5. Switch the box and see the service come up — NOT YET RUN

Closes: **criterion 3**.

The box runs an older revision than the current `flake.nix`, so it needs the redeploy
before the module exists at all:

```sh
nixos-rebuild switch --flake /root/base-server-lxc#base-server-lxc
systemctl status pagewire.service
systemctl list-timers pagewire-sweep.timer
```

Watch for: `pagewire.service` `active (running)`, and the timer scheduled. Then prove it
comes back on its own:

```sh
systemctl reboot
# after it is back:
systemctl status pagewire.service
```

Paste both `systemctl status` outputs into `criterion-3-service-and-reboot.txt`.

## 6. Post a real event over plain HTTP from another machine — NOT YET RUN

Closes: **criterion 4**.

From a machine on the LAN — **no TLS, no VPN** — with the reader's own token:

```sh
curl -sS -o /dev/null -w '%{http_code}\n' \
  -X POST http://192.168.0.112:8799/events/session \
  -H "Authorization: Bearer $PAGEWIRE_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"v":"1","event":"reader.session","id":"runbook-1","ts":0,"book":"/x.epub","document":"00000000000000000000000000000000","start_time":0,"end_time":60,"duration_seconds":60,"start_progress_bp":0,"end_progress_bp":100,"progress_scale":10000}'
```

Watch for: a 2xx, and `journalctl -u pagewire.service` showing the event accepted. A
redirect to HTTPS, or a refused connection, means the plain-HTTP listener is not the one
answering — check `services.nginx.virtualHosts."pagewire-lan"` actually bound `lanPort`.
A 401 means the token on the card and `PAGEWIRE_TOKEN` disagree.

Paste the curl transcript with the URL and status code into `criterion-4-curl.txt`.

## 7. Read the diff back and check the token file's permissions — NOT YET RUN

Closes: **criterion 5**.

```sh
git -C /root/projects/base-server diff flake.nix     # on the box / in the config repo
ls -l /var/app/data/pagewire/pagewire.env
```

Watch for: the flake diff touching `inputs`, `imports` and `services.pagewire` **only** —
no `PAGEWIRE_*TOKEN` or device token value anywhere in it — and the env file being `-rw-------`
owned by `pagewire`. The relay's own repo never carries a secret either: `git grep -n
'TOKEN=' deploy/` must show only the `REPLACE…` placeholders.

Paste the diff and the `ls -l` into `criterion-5-no-secret.txt`.

## 8. Show each tunable coming from the file — NOT YET RUN

Closes: **criterion 6's live half**; the offline half is `criterion-6-shipped-tunables.txt`.

Change one tunable in `pagewire.env` on the box, restart the service, and show the relay
reading the new value with no code edit:

```sh
sed -i 's/^PAGEWIRE_STITCH_WINDOW_SECONDS=.*/PAGEWIRE_STITCH_WINDOW_SECONDS=900/' \
  /var/app/data/pagewire/pagewire.env
systemctl restart pagewire.service
```

Watch for: the new value in effect (e.g. via the deploy log or a dry run) and none of the
seven criterion-6 tunables appearing anywhere in the flake or the module's code.

---

## What remains unverifiable even after all eight steps

Criterion 4 proves the LAN listener answers; it does not prove the **device** reaches it,
because the device's web client only runs in File Transfer / Calibre Wireless mode and
its sleep-entry drain is a ten-second window on real hardware. That end-to-end check is
PER-47, deliberately, not this ticket.

`services.pagewire.package` defaults to this flake's own package. If the config repo pins
`pagewire` to a revision whose `nixosModules.default` and package disagree, the module and
the code can drift across a lock bump; the lock is the single place that keeps them
together.
