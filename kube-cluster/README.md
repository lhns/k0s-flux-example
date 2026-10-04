# Per-component tests

`bash scripts/validate.sh` runs the repo-wide checks over every component (see the root
[README](../README.md#validation-and-tests)) and, through `scripts/run-tests.sh`, the opt-in
tests below. Nothing component-specific belongs in the repo-wide checks.

A component that needs a check of its own — a generated file
to diff, a config language with a compiler, an invariant no schema expresses — puts it in

    kube-cluster/{apps,infra,rbac}/<component>/tests/run.sh

`scripts/run-tests.sh` discovers those by glob (the same shape as the generator chart's
`<base>/*/kustomization.yaml`, so a new group is picked up for free), runs each from the
repo root with the component directory as `$1`, and exits non-zero if any failed. **Most
components have none, and that is the normal case** — absence is silent, never a warning.

## Contract

- **No cluster access.** These run on a PR: no `kubectl`, no ssh, no network unless the
  test documents why.
- **Deterministic.** No wall-clock and no network, so a rerun of the same tree is the same
  result.
- **A missing tool is a FAILURE.** `ALLOW_SKIP=1` downgrades it to a reported skip for a
  workstation; CI sets `ALLOW_SKIP=0` so it can never skip. A check that quietly does not
  run reports green, which is worse than not having it. The opt-out is per component, not
  per check: one missing tool skips that component's whole `run.sh`.
- **Every skip lands in the final summary**, with its reason, after all the output — not
  as one `skipped:` line mid-scroll.

## Tools manifest

Anything the test needs beyond a POSIX shell goes in `<component>/tests/tools`, one
executable per line, optionally followed by the apt package that provides it:

    lua5.1
    luac5.1 lua5.1
    helm -

The runner checks the executables before running the test, so a missing one is a named
FAIL or SKIP instead of a `command not found`. `.github/workflows/lint.yml` installs the
packages from these manifests, so a new component test needs no edit there — unless apt
has no package for it, which is what `-` says: the workflow must then install it in its own
step, and an entry lacking the `-` fails that step for the whole repo.
