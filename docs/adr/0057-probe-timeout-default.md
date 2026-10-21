# 0057 — Probe timeouts default to 5s, set by a MutatingAdmissionPolicy

- Status: Accepted
- Date: 2026-10-20
- Current answer: a cluster-wide `MutatingAdmissionPolicy` raises every probe `timeoutSeconds` of 1
  to 5 on Pod CREATE, so third-party charts need no per-chart probe values; 2 or more is kept.
  It fails open.

## Context
The kubelet defaults `timeoutSeconds` to 1, and the default is not configurable. With 1s, HTTP
checks that touch a database and exec probes time out now and then without the workload being
unhealthy, which fills the event stream with probe failures. Our own manifests now set their
timeouts, but third-party charts mostly do not expose them.

## Decision
A cluster-wide `MutatingAdmissionPolicy` (`infra/admission-policies/probe-timeouts.yaml`)
raises every probe timeout of 1 to 5 on Pod CREATE. Defaulting runs before admission, so unset
and an explicit 1 look the same and are both raised; 2 or more is kept.

## Consequences
- Charts need no per-chart probe values; the effective timeout is on the Pod, not in git.
- A deliberate 1s timeout is not possible; use 2.
- Fail-open (`failurePolicy: Ignore`): a policy error never blocks Pod creation.
- Detection speed is unchanged: it is `periodSeconds × failureThreshold`.

## References

- PR #951, `57d79301`, [`infra/admission-policies`](../../kube-cluster/infra/admission-policies/README.md).
