# 0033 — Composed images are tagged by a hash of their spec

- Status: Superseded by [0042](0042-composed-images-as-exported-digest-refs.md)
- Date: 2026-08-05, amended 2026-08-09
- Current answer: no longer used. The generator hashed each `ImageComposition`'s build spec into
  a tag and rewrote both the composition's publish tag and its consumers' image references with
  it, so the two could not drift and no substitution was needed.

## Context

A consumer must name exactly the artifact its composition builds. Substitution was avoided
deliberately: at the time an undefined `${VAR}` became an empty string, and the first consumer
had literal `${…}` that had to survive.

## Decision (as it was)

- The root generator found compositions by kind in each component's YAML, hashed the
  build-determining spec, and emitted one kustomize `images:` entry per composition plus a patch
  setting its publish tag (2026-08-05, first through a per-app `configurations:` file, then
  generator patches so apps carried no kustomize config).
- **Sources in the hash** (2026-08-09): a `sourceRef` names its input rather than containing it,
  so a pin bump in the referenced GitRepository did not move the tag and `publish.immutable`
  refused the rebuild. The referenced sources' specs were folded into the hash.

## Why it was superseded

- The tag moved in the same commit as the source pin, and kustomize applies both. If the
  composition reconciled before its GitRepository fetched, it built old content under the new
  tag, and the immutable tag then refused the correct build forever (PR #352).
- Finding compositions by kind meant packaging every component's YAML into the generator chart,
  most of the release payload ([0039](0039-one-generator-release-per-component.md)).
- A component's files and its generated Kustomization could disagree for a while, which changed
  what a published tag meant (PR #395).

## References

- `8155b29c`, `7794fc85`, `ee2a3eb1`, `3d27bc71`, PR #352, PR #393.
