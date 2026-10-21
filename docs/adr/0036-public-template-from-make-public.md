# 0036 — The public version is a sanitised orphan branch, never this repo

- Status: Accepted
- Date: 2026-08-24, amended 2026-09-04, 2026-09-16 and 2026-10-21
- Current answer: this repository stays private. `scripts/make-public.py` builds a sanitised
  `public-template` branch from `HEAD`, sharing no ancestry with `main`, to be pushed to a
  separate, empty repo. It removes and rewrites what `scripts/public.config.yaml` lists, then
  proves the result: `kustomize build` of every component, gitleaks, and a denylist of every
  value it removed.

## Context

The public repo is meant as a sanitised demo of the setup for others. The history
contains values that must never be published, and publishing a repo publishes every branch and
all of its history, not just the one meant.

## Decision

- **An orphan branch, pushed by URL** (2026-08-24): no commit of `main` is reachable from it, and
  it goes to the public repo by URL rather than through a configured remote, so a later
  `git push --all` cannot publish `main`.
- A pure git and filesystem transform over `git archive`: no cluster, no SSH, `main` untouched.
- Credentials are found by key name or by shape at runtime and held in memory only, so the
  config holds no secret values; the config deletes itself from the output.
- Verification is part of the build.
- **Publish history accumulates** (2026-09-04): each publish commits on top of the previous snapshot
  instead of replacing it. Any miss is then permanent in public history, so a failed verification is
  a hard stop, and every new kind of identifier is matched by shape where it can be, not by value.
- Later rounds: redact infrastructure identifiers and third-party repos, scoped substitutions
  (2026-09-16).
- **LAN addresses come from a random table** (2026-10-21): `ip_map` in the config maps each real
  /24 and host to a fake one drawn once and committed, instead of keeping the real octets. The
  build fails on any home-LAN address without an entry. Hostnames and filesystem names of the
  storage backend are literal substitutions like other identifiers.

## Alternatives considered

- Making this repo public: its history contains values that must never be published.

## Consequences

- Every new kind of identifier (a host, a phone number, a partner's repo) needs a config entry,
  or the build's denylist does not know to look for it.
- `TODO.md` is deleted from the public output, so the README does not link to it.
- PRs that change exposed content run `make-public.py --no-commit` as a check.

## References

- `9db01488`, `03b0c97c`, `f71cb16d`, `9ff7335f`, `02bb99f9`, PR #527, PR #539.
- Conversation 2026-08-24 (orphan branch), 2026-09-03 (history), 2026-10-05 (why publish).
