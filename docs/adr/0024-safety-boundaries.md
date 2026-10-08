# 0024 — Safety boundaries: outside systems read-only, the Swarm untouched, secrets unseen

- Status: Accepted
- Date: 2026-07-23, amended 2026-07-25, 2026-08-27, 2026-09-07, 2026-09-18, 2026-09-24 and
  2026-09-26
- Current answer: work on this repo and cluster, by a person or an agent, changes the cluster
  through git. Systems outside it are read-only unless the owner allows the specific action: the
  Proxmox hosts and Ceph nodes may be inspected, never changed destructively, and any config
  change there is asked for first; the Swarm and its folders are never touched; the public edge
  is the owner's. Secrets are never read, printed or decrypted. A permission the tooling denies is
  escalated to the owner, never worked around.

## Context

Much of the work here is done with an AI agent that has shell access to the jumphost, and from
there to the hypervisors and the Ceph cluster. Those systems hold every VM and all data, and are
not reconciled from git, so a mistake there has no revert.

## Decision

- **Hypervisors read-only** (2026-07-23): inspect, never change destructively. Network tests run
  from the jumphost, never a Proxmox host (2026-07-24).
- **Ceph** (2026-07-25): no destructive operations, and ask before changing any config. A capacity
  investigation is read-only and deletes nothing (2026-08-27). A cleanup the owner approves runs in
  growing batches, each re-checked against Ceph state, stopping at the first oddity (2026-09-26).
- **The Swarm is never touched** (2026-07-25): migration leftovers are cleaned up on the cluster
  side only ([0001](0001-k0s-and-flux-replace-docker-swarm.md)).
- **Tests do not run on prod** when a local or remote Docker can run them (2026-09-07). A test
  container that could reach a real external service runs without a network (2026-09-18).
- **Secrets** (2026-07-21 onward, written down 2026-09-24): values never enter the conversation;
  they are piped into `sops` on the host that holds the key, or handed over through
  `secret-from-clipboard.py` ([0003](0003-sops-with-age-one-key-per-cluster.md)). An exposed
  credential is reported, and rotated only when the owner asks.
- **Denied means ask** (2026-09-18): a denied merge, push or command is not retried or routed
  around; the owner runs it or grants it. Changes to agent instructions and tool settings are the
  owner's.

## Consequences

- None of this is enforced by the repo; the agent rules are in [AGENTS.md](../../AGENTS.md), the
  permission system enforces some of them.
- Fixes outside git (Ceph flags, hypervisor settings, the edge, router forwards) are carried out
  or approved by the owner, and the repo records what was found.

## References

- Conversation 2026-07-23 to 2026-07-25 (hypervisors, Ceph, Swarm), 2026-08-27 (read-only
  audit), 2026-09-07 (no tests on prod), 2026-09-14 and 2026-09-16 (secrets), 2026-09-18
  (denials, offline tests), 2026-09-26 (staged cleanup).
- `d9854ef0` (AGENTS.md "Receiving secrets").
