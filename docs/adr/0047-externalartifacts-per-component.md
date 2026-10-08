# 0047 — ExternalArtifacts: one content-addressed source per Kustomization, and for the root chart

- Status: Accepted; supersedes [0040](0040-filtered-gitrepository-per-component.md)
- Date: 2026-10-04
- Current answer: for each component the root release emits an ArtifactGenerator (Flux's
  source-watcher) over the one `flux-system` GitRepository. It packs one ExternalArtifact for the
  component's generator chart and one per Kustomization (the component, and its `namespaces/`,
  `artifacts/` and `substitution-secrets/` where present). Each artifact's revision is a hash of
  its content, so a commit reconciles only what it changed. The root chart is an ExternalArtifact
  too.

## Context

Every Kustomization read the same `flux-system` source, so every commit made all of them
reconcile; at default concurrency that is slow, and a change looks stuck. The generator
releases read separately filtered clones ([0040](0040-filtered-gitrepository-per-component.md)),
which lagged the files. ArtifactGenerators had been tried and dropped in July
([0002](0002-first-generators-artifactgenerator-and-resourceset.md)), when per-unit hashing
bought nothing at this scale.

## Decision

- **source-watcher installed again**, with the `ExternalArtifact` gates on kustomize- and
  helm-controller ([0019](0019-flux-manifests-from-gen-flux-never-bootstrap.md)). It is an extra
  component, not core Flux.
- **Not `readyExpr`**: it would stop dependants waiting, but every Kustomization would still wake
  on every commit; content-hashed artifacts stop the waking itself. Raising controller
  concurrency came first and stays ([0019](0019-flux-manifests-from-gen-flux-never-bootstrap.md)).
- **Migrated behind a switch** with prune off ([0012](0012-prune-off-around-risky-changes.md)),
  after simulating every artifact with a re-implementation of source-watcher's copy rules and
  checking it builds identically to the repo.
- **Copy rules are exact repo paths**: an unmatched path or glob fails the artifact, and an
  exclude without `/` matches a base name at any depth. Helm refuses `Chart.yaml` at an archive's
  root, hence `generators/`.
- **The root release moved last** (PR #604-#608), checked against the live release and upgraded
  in place. The switch, the `gen-*` GitRepositories and `generate.sourceFile` are gone.

## Consequences

- A push reconciles only the Kustomizations it changed and one Helm release, not all of them.
- **An artifact missing a file builds fewer objects, and prune deletes the rest.** A change to
  what `templates/generate.yaml` copies is a risky change.
- A secret change also changes the chart artifact, so it costs one Helm upgrade with identical
  output.
- Siblings do not wait for the same commit, so the Kustomizations retry every minute, and
  substitution Secrets are watched so a Secret-only change still reaches its consumer.
- A component cannot read outside its artifact; `../` into `shared/` needs the `bases` annotation
  ([0048](0048-several-clusters-one-repo.md)), and `check-artifacts.py` catches a missing one.
- `flux bootstrap` without source-watcher would stop every component.

## References

- PR #577, #581, #583, #585-#599, #603, #604, #606-#609.
- Conversation 2026-10-03 and 2026-10-04 (`readyExpr`, measurements).
- README: [ExternalArtifacts](../../README.md#externalartifacts).
