# PER-46 evidence — deploy 01: the relay on the NixOS LXC

What each file here is, and what it measured.

| File | What it is |
|---|---|
| `runbook.md` | The eight steps a machine with `nix` and access to the box runs, each naming the criterion it closes. **NOT YET RUN** — see its opening. |
| `criterion-2-ci.txt` | The relay's own `nix flake check` in CI: the module evaluated and its throwaway system built (`✅ checks.x86_64-linux.pagewire-module-evaluates`). Not criterion 2 as written, but it closes the "nothing has ever evaluated the module" gap. Captured from the run at `db89266`. |
| `criterion-2-base-server-ci.txt` | Criterion 1 **and** criterion 2's build half, on the config repo: its CI evaluates *and builds* the real `base-server-lxc` with the relay inside it — green at `2e95e7f` — after fixing three pre-existing defects. Also records why `nix flake check` alone still does not pass. |
| `criterion-6-shipped-tunables.txt` | Criterion 6's offline half: the shipped `deploy/pagewire.env.example` beside the test that pins it to `config.py`. Generated on this tree. |

There is no generator script here, deliberately: the only artifact this session could
produce is a copy of a committed file plus a captured test run, and both are reproducible
from the repository alone — `cat deploy/pagewire.env.example` and
`python -m pytest tests/test_deploy_config.py -v`. A generator that just re-ran those two
commands would be a third thing to keep in step.

## What is not here, and why

Every criterion except 6's offline half needs something this environment does not have:

- **Criterion 1's lock entry** needs `nix flake lock` to write a `narHash`.
- **Criterion 2** needs `nix flake check` / `nix build` to evaluate the module.
- **Criteria 3, 4, 5's deploy half and 6's live half** need the running box and a real
  event posted over the LAN.

`nix` is absent and the filesystem has ~4.5 GiB free — not enough to install nix and
evaluate a NixOS system — so those are the runbook's steps and not this session's claims.
The one piece of that which has since been closed is the largest one: the module **is** now
evaluated, because the relay's own CI does it (`criterion-2-ci.txt`) — and so is the **config
repo's** real `base-server-lxc` configuration, which its own new CI both evaluates and builds
(`criterion-2-base-server-ci.txt`). What remains untouched is the box itself.
