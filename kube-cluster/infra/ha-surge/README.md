# ha-surge

[ha-surge](https://github.com/lhns/kube-ha-surge) — make-before-break HA for
opted-in **single-replica Deployments** during node drains (manual `kubectl
drain`, k0s upgrades, autoscaler reclaim), without permanently running a second
replica.

A `ValidatingWebhook` on `pods/eviction` holds an eviction with a **429** (which
drains retry on natively) and **detaches the doomed pod from its ReplicaSet** (by
removing its `pod-template-hash` label). The ReplicaSet then creates its own real
replacement, while the detached pod keeps its app labels — so the Service keeps
routing to it and it keeps serving — until that replacement is Ready; then the
eviction is allowed and a controller deletes the detached pod. `spec.replicas` is
never touched (Flux doesn't fight it) and no pod is ever created (no
admission-policy interaction). Fail-open by construction (`failurePolicy: Ignore`
+ a timeout), so it can never wedge a drain.

- `release.yaml` — `OCIRepository` (pinned chart tag) + `HelmRelease`. Deployed
  from the published OCI chart `oci://ghcr.io/lhns/charts/ha-surge`.
- The eviction webhook is **apiserver-dialed**, so kube-vnet auto-allows it — no
  `vnet.yaml`. The webhook serving cert is issued by **cert-manager**
  (`dependsOn: infra-cert-manager`).

## Coverage

**Cluster-wide by default.** `release.yaml` sets `defaultEnabled: true`, so every eligible
workload is covered with **no label or annotation anywhere**. `kube-system` and
`ha-surge-system` are always excluded.

Opt an individual namespace or Deployment *out* with `ha-surge.lhns.de/enabled: "false"`.
The same key set to `"true"` is only meaningful when `defaultEnabled` is off.

Refused automatically (eviction passes straight through, no cover):

- an **`RWO` volume** — the replacement cannot co-mount a single-writer disk;
- **`strategy: Recreate`** — the ReplicaSet will not run two pods at once, so
  make-before-break is impossible by construction;
- non-Deployment owners, and Deployments already running ≥2 replicas;
- a Service selecting on `pod-template-hash`, since the detached pod loses that label.

Don't add a blocking `minAvailable` PDB to a covered workload — it would 429 the drain before
the webhook is reached.

**Cover is per-Deployment, not per-app.** An app split across several Deployments is only as
covered as its least-covered part: guacamole's web tier is covered while `guacd` is not, and a
dropped `guacd` takes live sessions with it regardless.

**Fail-open includes a detach timeout.** Under a busy drain the log shows
`detach timeout, failing open` — the eviction proceeds uncovered rather than stalling the drain.
Seen for bitwarden, mealie, headlamp, reflector and standardnotes-web on a three-node cluster
draining one node, where the replacement had nowhere roomy to land.
