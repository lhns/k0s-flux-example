# 0006 — A push webhook drives reconciliation; intervals are the fallback

- Status: Accepted
- Date: 2026-07-07, amended 2026-07-08, 2026-09-22 and 2026-10-04
- Current answer: a GitHub push webhook (`infra/flux-webhook`, a Flux Receiver) reconciles the
  `flux-system` GitRepository at once, so a push applies in seconds. Intervals only correct
  drift: sources poll every `1h`, Kustomizations and HelmReleases reconcile every `10m`.

## Context

Short intervals were the first way to make pushes apply quickly (`1m` on the generated
Kustomizations), and they make every controller poll constantly for no change.

## Decision

- A Receiver for GitHub pushes, behind Traefik with an HMAC token.
- Interval policy (2026-07-08): sources `1h`, because charts and images are pinned exactly and a
  poll re-fetches the same artifact; reconcilers `10m`, to undo manual drift. Shorten a source
  only for a floating tag.
- **Per-component sources refreshed on push** (2026-09-22, PR #395): selected by label, so a
  component's files and its generated Kustomization spec land together rather than on a poll.
- **Receiver names `flux-system` only** (2026-10-04): with ExternalArtifacts
  ([0047](0047-externalartifacts-per-component.md)) every generated source derives from it.

## Consequences

- A missed webhook delivery leaves the cluster up to an hour behind;
  `flux reconcile source git flux-system` closes it.
- A cluster without the Receiver (the test cluster today) polls hourly.

## References

- `14e7a393`, `43255719`, `048426ba`, PR #395, PR #609.
- README: [Reconciliation intervals](../../README.md#reconciliation-intervals).
