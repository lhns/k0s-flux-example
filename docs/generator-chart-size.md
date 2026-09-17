# The generator chart and the 1 MiB release limit

On 2026-09-17 the `generators` HelmRelease stopped applying:

    Secret "sh.helm.release.v1.generators.v997" is invalid:
    data: Too long: may not be more than 1048576 bytes

Every generated Kustomization froze at the previous content. Nothing was corrupted and
nothing alerted; the first visible symptom was an unrelated image pin failing to deploy.

## Why

Helm stores each release as a Secret whose `data.release` is `gzip(JSON(release))`, and the
release **embeds the whole chart, file by file**. The repo root is the chart, so the chart is
the repo.

Measured on v996, the last release that applied:

| part | bytes |
|---|---|
| `chart` | 2,326,706 |
| `manifest` — the rendered Kustomizations, i.e. the actual output | 119,310 |
| stored after gzip | **1,039,492** |
| Kubernetes limit | 1,048,576 |

99.1% full, with 9 KB of headroom. It carried 28x more than it read.

The limit is **apiserver validation**, not etcd — `kubectl create secret --dry-run=server` with
1.2 MB fails the same way, before etcd is involved. `MaxSecretSize` is a hardcoded constant;
no flag raises it, and ConfigMap has the same one. etcd's own `--max-request-bytes` (1.5 MiB
default) is never reached.

## What the chart actually reads

    templates/**, Chart.yaml
    **/kustomization.yaml                 component discovery
    **/substitution-secrets/*.yaml        postBuild.substituteFrom
    kube-cluster/k0sctl.yaml              the k0s version
    <component>/*.yaml                    _artifacts.tpl, top level only

That last line is **97.1% of the payload**. `_artifacts.tpl` finds `ImageComposition` and
`ImageBuild` *by kind*, so it must parse every top-level yaml of every component to find the 8
that exist across 5 components. Discovery alone needs 54 KB; artifact scanning costs 1,288 KB.

## Options, measured

`.helmignore` cannot express an include-list: it has **no negation**. `!kustomization.yaml`
after `*.yaml` keeps nothing. Flux has no `ignore` field on HelmRelease or HelmChart either —
the API rejects `spec.chart.spec.ignore` outright.

| approach | largest release |
|---|---|
| today | 99.1% |
| `.helmignore`, exclusions only | 73.8% |
| chart rooted per group (chart files inside `apps/`, `infra/`) | 58.0% |
| **GitRepository per scope, `spec.ignore`, chart stays at root** | **9.8% (per component)** |
| + artifacts moved to `<component>/artifacts/*.yaml` | ~4% |

A chart cannot read outside its own root — verified: a chart in `sub/` sees nothing in
`../outside/`, with or without `../`. So the chart must sit at the repo root, which means one
`.helmignore` shared by every release. Splitting the HelmRelease by group therefore changes
nothing on its own: all four would package the identical chart.

## What works: a filtered GitRepository per scope

`GitRepository.spec.ignore` uses `.sourceignore` (gitignore format) and **does** support
negation. A second GitRepository against the same URL and branch, filtered to one scope, feeds
the chart; the unfiltered `flux-system` GitRepository keeps serving every Kustomization.

Proven end to end against `apps/authelia`, the largest component:

    GitRepository artifact    741,102 -> 52,015 bytes
    Helm release Secret     1,039,492 -> 102,816 bytes   (9.8% of the limit)
    HelmRelease                              Ready=True
    rendered                 the correct Kustomization, correct dependsOn

```yaml
apiVersion: source.toolkit.fluxcd.io/v1
kind: GitRepository
metadata: {name: gen-app-authelia, namespace: flux-system}
spec:
  interval: 30m
  url: https://github.com/example/k0s-flux.git
  ref: {branch: main}
  secretRef: {name: flux-system}
  # Exclude everything, then re-include by path. sourceignore does NOT prune excluded
  # directories the way git does, so the usual gitignore idiom for re-including directories
  # is unnecessary -- and adding it re-admits everything (measured: 1,675,003 bytes, larger
  # than unfiltered). `*` covers .git on its own.
  ignore: |
    *
    !/Chart.yaml
    !/templates/**
    !/kube-cluster/apps/authelia/*.yaml
    !/kube-cluster/apps/authelia/substitution-secrets/*.yaml
---
apiVersion: helm.toolkit.fluxcd.io/v2
kind: HelmRelease
metadata: {name: gen-app-authelia, namespace: flux-system}
spec:
  interval: 30m
  # Same as the root: this only emits a Kustomization, whose own Ready condition and
  # interval decide the component. Waiting would fail the release on a slow component and
  # then retry only on backoff.
  install: {disableWait: true}
  upgrade: {disableWait: true}
  chart:
    spec:
      chart: "."
      # The chart version never changes, so without this a new file in a component
      # would not re-render.
      reconcileStrategy: Revision
      sourceRef: {kind: GitRepository, name: gen-app-authelia, namespace: flux-system}
  values:
    groups:
      - {base: kube-cluster/apps, prefix: app}
```

The per-scope releases need no "which component am I" parameter: `kustomizations.yaml` globs
`%s/*/kustomization.yaml` and the artifact contains only one component, so the glob finds only
it. The `ignore` does the filtering; the values are identical across every scope.

The root release sets `generate`, which the generated releases do not — that is what stops
the recursion. Same guard pattern `k0s-upgrade.yaml` uses with `{{- with .Values.k0sUpgrade }}`.
Every generator is a GitRepository + HelmRelease sharing a name: `generators` at the root,
`gen-<prefix>-<component>` below it. See `templates/generate.yaml`.

## Hazards found while proving it

- **A generated Kustomization is real and has `prune: true`.** The PoC's `pocapp-authelia`
  applied authelia's resources alongside the live `app-authelia`; two Kustomizations owned the
  same objects. Deleting it would have pruned authelia out of the cluster. Set `prune: false`
  and `suspend: true` before removing one. Any migration has the same overlap.
- **The gitignore idiom for re-including directories re-admits everything here.** sourceignore
  does not prune excluded directories, so it is unnecessary; measured, it made the artifact
  larger than unfiltered.

## Not yet proven

- `reconcileStrategy: Revision` re-rendering when a component is **added** to a filtered scope.
- Cross-scope `dependsOn` (`app-matrix` -> `infra-postgres`) while several GitRepositories
  reconcile independently and briefly disagree.
- Load of one GitRepository per component: 88 clones on their own intervals.

## Worth adding either way

Nothing warned. The release sat at 99.1% for an unknown period and failed silently. A check
that packages the chart and fails above ~70% of 1 MiB would give months of notice, and runs
offline.
