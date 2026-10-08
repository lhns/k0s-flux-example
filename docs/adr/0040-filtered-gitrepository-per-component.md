# 0040 — A filtered GitRepository per component feeds its generator release

- Status: Superseded by [0047](0047-externalartifacts-per-component.md)
- Date: 2026-09-17, amended 2026-09-22 and 2026-10-02
- Current answer: no longer used. Each `gen-<group>-<name>` release read its chart from a second
  GitRepository on the same repo and branch, whose `spec.ignore` excluded everything and
  re-included `Chart.yaml`, `templates/` and that component's generator inputs.

## Context

The chart must sit at the repo root and `.helmignore` cannot include by pattern
([0039](0039-one-generator-release-per-component.md)). `GitRepository.spec.ignore` uses gitignore
syntax with negation, so a filtered clone could shrink the chart to what one component needs.

## Decision (as it was)

- `*` then `!` re-includes by path; the gitignore idiom for re-including directories was
  unnecessary and re-admitted everything.
- `reconcileStrategy: Revision`, since the chart version never changes.
- **Amended 2026-09-22:** the 30-minute poll let a component's files land before its generated
  Kustomization changed, so the push Receiver refreshed these sources too (PR #395).
- **Amended 2026-10-02:** each was copied from one `generate.sourceFile`, so credentials and
  options were set once (PR #532).

## Why it was superseded

- The Kustomizations still read the unfiltered `flux-system` source, so every commit
  reconciled every component, slowly at default concurrency.
- Files and generated spec came from different sources at different times, and skew between them
  could break a rollout.

ExternalArtifacts derive every per-component artifact from the one `flux-system` checkout, with
a content-hash revision ([0047](0047-externalartifacts-per-component.md)).

## References

- `c88aa67e`, `d38741d9`, PR #395, PR #532, PR #603.
- [docs/generator-chart-size.md](../generator-chart-size.md).
