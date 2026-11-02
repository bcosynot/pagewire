# PER-46 evidence — deploy 01: the relay on the NixOS LXC

What each file here is, and what it measured.

| File | What it is |
|---|---|
| `runbook.md` | The eight steps a machine with `nix` and access to the box runs, each naming the criterion it closes. **NOT YET RUN** — see its opening. |
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
The module in `nixos/pagewire.nix` has therefore never been evaluated by anything, which is
the single largest caveat on this branch.
