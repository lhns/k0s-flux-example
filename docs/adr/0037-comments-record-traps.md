# 0037 — Comments record traps; rules for agents live in AGENTS.md

- Status: Accepted
- Date: 2026-09-03, amended 2026-09-20, 2026-09-24, 2026-10-03 and 2026-10-08
- Current answer: a comment records what the YAML cannot say: a trap, a constraint, "changing
  this breaks X", in one to three lines. No incident narratives, dates or changelogs; git has
  those. Architecture goes in the component's README, repo-wide rules in the root README, and the
  rules an agent must follow in `AGENTS.md` (`CLAUDE.md` includes it). A comment-only edit must
  leave `kustomize build` byte-identical, except in `configMapGenerator` sources, where comments
  are shipped config.

## Context

Much of this repo is written with an AI agent. Comments grew into investigation logs that buried the
one line that mattered, and some went stale and asserted false things. The agent instructions
started as `CLAUDE.md`, holding the PV-deletion trap ([0008](0008-pv-reaper-split-reclaim.md)).

## Decision

- **The comment rule** (2026-09-03), applied in a sweep that compressed or deleted the heavy blocks.
  Comments are concise and do not state the obvious. A comment belongs to the code it sits on;
  rationale owned elsewhere is stated there and linked (2026-09-05). The same goes for commit
  messages (2026-09-17).
- **False statements first** (2026-09-20, PR #368): comments and docs that the config contradicts
  are corrected first, since a comment asserting something untrue gets believed. A warning about an
  upstream bug that is fixed is deleted, not shortened.
- **`AGENTS.md`** (2026-09-24): the instructions moved to the tool-neutral file, which other agents
  read too, with the secret handover rule added ([0003](0003-sops-with-age-one-key-per-cluster.md)).
  `CLAUDE.md` imports it rather than being a symlink, since symlinks are unreliable with git on
  Windows.
- **README examples are generic** (2026-10-03): `example-app`, never a real app, so the rule
  does not read as one app's setup.
- **README restructured** (2026-10-03, PR #539/#540): built around the generator, operations
  moved to `docs/operations.md`, and incident stories dropped.
- **ADRs follow the same discipline** (2026-10-08): they record the decision and its general
  reasons, not incident stories (AGENTS.md, "Decisions").

## Consequences

- Decisions and their history go in `docs/adr/` (this directory), not in comments.
- Upstream currency ("X is the latest release") is not asserted; it decays silently.

## References

- `a4dba12e`, `9059442f`, `0342b3fd`, `228153b3`, PR #368, `d9854ef0`, `28fbf84b`, PR #539,
  #540.
- Conversation 2026-09-03 and 2026-09-05 (comments), 2026-09-24 (AGENTS.md), 2026-10-03
  (generic examples).
- [AGENTS.md](../../AGENTS.md), "Comments".
