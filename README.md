# k0s-flux

GitOps for a k0s Kubernetes cluster, managed by Flux.

- `kube-cluster/k0sctl.yaml` — cluster definition (k0s + kube-router CNI + control
  plane), applied with `k0sctl` (not Flux).
- `kube-cluster/flux-system/` — Flux bootstrap (controllers, `GitRepository`, root
  `Kustomization`) plus the generator `HelmRelease`.
- `kube-cluster/infra/`, `kube-cluster/apps/` — one directory per component. The
  root generator chart (`Chart.yaml` + `templates/`) emits one Flux `Kustomization`
  per directory (`infra-<name>` / `app-<name>`).

## Namespacing (no top-level `namespace:` transformer)

Every namespaced object declares its own namespace **explicitly**:

- hand-written resources → `metadata.namespace`
- generated objects (`configMapGenerator` / `secretGenerator`) → a `namespace:` field
  **on the generator entry** (a generated object with no namespace fails to apply with
  `namespace not specified`).

**Do not** put a top-level `namespace:` in a `kustomization.yaml`. That transformer
*force-overrides* the namespace on every object — including ones deliberately pinned
elsewhere (e.g. a CNPG `Database`/`DatabaseRole` + its reflected secret living in
`postgres`) — silently breaking multi-namespace apps. Keeping namespaces explicit means
one rule covers single- and multi-namespace apps alike, and any file states its own
namespace without cross-referencing the kustomization.

Reference: `authelia`, `zensical` (generator with `namespace:`); `matrix`, `stalwart`
(multi-ns, explicit `metadata.namespace` throughout).

## Getting files into a workload

Workloads routinely need files their upstream image does not ship — plugins, extensions,
config bundles. **Never fetch them at pod start.** A git clone or a download in an
initContainer makes startup depend on the network, pins versions by ref rather than by
content, and usually leaves the result on a data PVC, turning a cache into state.

In order of preference:

**1. The content is already a published image → mount it directly.** No composition, no
artifact, no build. Image volumes do this natively, and `subPath` handles layout when the
files sit in a subdirectory of the image:

```yaml
volumes:
  - name: app-view-plugin
    image:
      reference: ghcr.io/lhns/headlamp-app-view:0.3.0
volumeMounts:
  - name: app-view-plugin
    mountPath: /headlamp/user-plugins
    subPath: plugins
```

Image volumes are always mounted **read-only** — that is structural, not something you opt
into. Reference: `apps/headlamp`.

**2. The content is not an image → compose one.** git repositories (including `subpath`
selection out of a monorepo), jars fetched by URL, ConfigMaps: something has to turn these
into OCI content, and that something is [`infra/oci-composer`](kube-cluster/infra/oci-composer/README.md).
An app declares `ImageComposition` objects among its own resources and the root generator
wires them to their consumers by spec-hash, so the artifact and the pod template can never
drift. Reference: `apps/freshrss`.

The line between the two is **whether the inputs are already images** — not how complex the
result is. Wrapping an existing image in a composition only re-wraps it.

**Renovate caveat, easy to miss:** an image volume spells the reference as a *mapping*
(`image: {reference: repo:tag}`), which Renovate's built-in `kubernetes` manager does not
see — it looks for `image: <string>`. Moving a reference out of a container spec into an
image volume therefore stops the auto-bump silently. `renovate.json` carries an
image-volume custom manager for this, anchored on an explicit `# renovate:` comment so it
cannot match the composed placeholders the generator rewrites (those must never be bumped).

## Reconciliation intervals

Reconciliation is **push-driven**: a GitHub webhook → notification-controller
`Receiver` reconciles our `GitRepository` on every push, so changes apply in
seconds. The `interval` fields are therefore **fallbacks / drift-correction
cadence**, not the primary trigger.

| Kind | Interval | Why |
| --- | --- | --- |
| **Sources** — `GitRepository`, `HelmRepository`, `OCIRepository`, and a HelmRelease's `chart.spec` | **`1h`** | Our git source is reconciled in real time by the webhook, so its poll is only a safety net. Upstream Helm/OCI sources have **no** webhook, but we **pin exact versions**, so polling just re-fetches the same artifact — frequency is irrelevant. (If you ever track a mutable/floating tag, shorten that one source so the new digest gets picked up.) |
| **Reconcilers** — `Kustomization`, `HelmRelease` | **`10m`** | They re-apply desired state and self-heal manual drift. Git changes reach them fast via webhook → GitRepository → Kustomization; `10m` is the drift-correction fallback. |

Rule of thumb when adding a resource: **source → `1h`, Kustomization/HelmRelease
→ `10m`.** Generated Kustomizations already default to `10m`.

## Chart CRDs are upgraded (`crds: CreateReplace` by default)

Helm never touches a chart's `crds/` directory on upgrade, and helm-controller's
default for `spec.upgrade.crds` is **`Skip`**. Left alone, that means a chart's
CRDs freeze at whatever version was *first installed* — permanently, and with no
error anywhere. New optional fields simply do not exist, so the API server prunes
them from any resource that sets one.

Traefik is how we found out: its CRDs sat at the **v3.6** schema generation under
a **v3.7.9** proxy from the `41.1.0` → `41.1.1` bump onward, missing e.g.
`encodedCharacters`.

There is no controller-level flag — `crds` exists only per `HelmRelease` — so the
root generator emits it into every generated `Kustomization` as a patch targeting
`kind: HelmRelease`. It is a *merge* patch, so an existing `spec.upgrade` block
keeps its other fields.

Only charts that ship a `crds/` dir are affected at all (**traefik**, **velero**).
Charts that template their CRDs (cert-manager, cloudnative-pg, metallb) already
upgrade them, and this is a no-op there.

`CreateReplace` is a **replace**. For additive schema changes — what a CRD bump
almost always is — that is safe. If a chart ever drops a CRD version that still
has stored objects, the API server rejects it, so the failure is loud rather than
destructive. Override per component on its own `kustomization.yaml`:

```yaml
metadata:
  annotations:
    crdPolicy: Skip        # Skip | Create | CreateReplace (default)
```

## Moving a component between groups (non-destructive migration)

The generator names each Flux `Kustomization` after its **directory**
(`infra-<name>` / `app-<name>`). So moving a component's folder — e.g.
`apps/postgres/` → `infra/postgres/` — is seen by Flux as *delete the old
Kustomization, create a new one*. Deleting a Kustomization **prunes everything it
manages**, so the resources are destroyed and then recreated under the new name.

For **stateless** components that's harmless. For anything **stateful** (a
database, or any PVC-backed workload) it means data loss: the PVCs are deleted
(their `Retain` PVs are left orphaned as `Released`) and the workload
re-bootstraps empty — nothing is copied.

To move a stateful component safely, **adopt-in-place** instead: tell the old
Kustomization to release its objects rather than delete them, so the new one
takes ownership of the exact same resources.

1. Add this annotation to every resource in the component (e.g. via the
   component's `kustomization.yaml` `commonAnnotations:`), commit, and let it
   reconcile so the annotation lands on the live objects:

   ```yaml
   kustomize.toolkit.fluxcd.io/prune: disabled
   ```
2. `git mv` the directory to the new group and push. The old Kustomization is
   pruned but, because the objects are marked `prune: disabled`, they are **kept**;
   the new Kustomization adopts them in place — no delete, no recreate, no orphaned
   PVs.
3. Remove the annotation again (so normal pruning resumes) and push.

> Rule: never `git mv` a stateful component between `apps/` and `infra/` without
> the `prune: disabled` dance first — verify the workload never restarts.
