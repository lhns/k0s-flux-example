# 0028 — Workload defaults: a request everywhere, memory limits, no CPU limits

- Status: Accepted; drains are [0029](0029-availability-single-replicas-and-ha-surge.md)
- Date: 2026-07-29, amended 2026-09-02 and 2026-09-03
- Current answer: every namespace has a LimitRange with a `defaultRequest` (128Mi / 25m) and no
  default limit. Workloads set memory limits; CPU limits are not set (the Flux controllers are
  the sized exception). Single-replica Deployments survive node drains through ha-surge
  ([0029](0029-availability-single-replicas-and-ha-surge.md)).

## Context

BestEffort pods count as zero for the scheduler, so requests can sum far below real memory use,
and some software sizes its cache or heap to the node rather than to a limit. A node can then run
out of memory while looking half empty.

## Decision

- **No CPU limits** (convention by 2026-07-29): a CFS quota throttles latency-sensitive
  components into probe failures or spurious failovers, and throttling something in the
  pod-creation path shows up as slow pod starts everywhere. Requests keep the scheduling
  guarantees.
- **Memory limits** (2026-09-02/03) on everything that had none, which also shrinks software
  that sizes itself to visible memory.
- **A `defaultRequest` per namespace** (2026-09-03): pods that set none are scheduled as if they use
  nothing. No `default` limit, because a blanket ceiling on unsized workloads would OOM-kill them.
  Since 2026-10-03 the LimitRange lives in the component's `namespaces/`
  ([0045](0045-namespaces-dir-never-pruned.md)).
- **Requests follow steady-state use**: software that sizes its cache from the limit (as
  VictoriaMetrics does) needs a request near its real use, not just a higher limit, or it is the
  first eviction target (2026-09-20).

## Consequences

- A pod without a memory limit is a review finding, not a lint failure.

## References

- `2740ce04`, `5e920115` (no CPU limit on Multus), `c418745b`, `8b576c4d`, `99a499ee`,
  `09978ba5`, PR #363, PR #582.
- Conversation 2026-07-29 (CPU limits), 2026-09-02 (memory), 2026-09-20 (requests).
