# 0069 — Seccomp profiles come from the Security Profiles Operator

- Status: Accepted
- Date: 2026-11-12
- Current answer: `infra/security-profiles-operator` installs `SeccompProfile` objects as files on
  every node. A component that needs its own profile ships it as a `SeccompProfile` and uses it as
  `Localhost: operator/<name>.json`. The first one is the webtop desktops' profile.

## Context

Pods run under containerd's RuntimeDefault seccomp profile. Some workloads need a syscall it
refuses, while dropping the filter (`Unconfined`) gives up all of it. A `Localhost` profile must be
a file on every node a pod can schedule on. A file placed by k0sctl lives outside Flux, and changing
it restarts k0s on every node.

## Decision

- **The Security Profiles Operator** (`infra/security-profiles-operator`, seccomp only) puts
  profiles on the nodes. Its kubelet directory is k0s's, `/var/lib/k0s/kubelet`.
- **The component that needs a profile ships it**, as a cluster-scoped `SeccompProfile` in its own
  manifests or chart, next to the workload that uses it, and depends on
  `infra-security-profiles-operator`.
- A profile stays as close to RuntimeDefault as the workload allows: RuntimeDefault plus the
  syscalls it needs.

## Consequences

- Profiles change through Flux like any other object, and no node restarts. A node added later
  gets every profile from the daemon.
- A container whose profile is not yet on its node does not start; the kubelet retries, so it never
  runs without its profile.
- The operator needs cert-manager, and its daemon runs privileged on every node.
- First use: the webtop desktops (`apps/webtop`), whose chart ships webtop-nix's `seccomp/desktop.json`
  (RuntimeDefault plus `pivot_root`, for bubblewrap and containers in the desktop).
