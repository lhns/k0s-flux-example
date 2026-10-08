# Architecture decision records

One record per decision about the cluster or the repo's structure. An app gets one only where it
deviates from a cluster-wide rule or sets a precedent others follow; ordinary app setup belongs in
the component's README.

All records were reconstructed in October 2026, from the commit and PR history and the owner's
working conversations (cited as "conversation <date>"), and are numbered by decision date: the
deciding commit, merge or conversation. The owner filled in the reasons the history did not show.
Where neither shows why, a record says "not recorded" rather than guessing.

## Index

| # | Title | Status | Date | In one line |
|---|---|---|---|---|
| [0001](0001-k0s-and-flux-replace-docker-swarm.md) | k0s and Flux replace Docker Swarm | Accepted | 2026-07-03 | k0sctl makes the cluster, Flux owns everything in it; the Swarm is the read-only rollback |
| [0002](0002-first-generators-artifactgenerator-and-resourceset.md) | First generators: ArtifactGenerator, then ResourceSet | Superseded by 0004 | 2026-07-04 | early per-component generators that still needed an input list |
| [0003](0003-sops-with-age-one-key-per-cluster.md) | SOPS with age, one key per cluster | Accepted | 2026-07-04 | Flux decrypts; only the cluster holds its key; secrets arrive by clipboard script |
| [0004](0004-one-kustomization-per-component-from-a-root-chart.md) | One Kustomization per component, from a repo-root chart | Accepted, amended | 2026-07-07 | the directory tree is the list; a component is its own failure domain |
| [0005](0005-groups-infra-apps-rbac.md) | Three groups: `infra`, `apps`, `rbac` | Accepted | 2026-07-07 | `infra` is what the cluster needs to function; a one-app operator is an app |
| [0006](0006-push-webhook-drives-reconciliation.md) | A push webhook drives reconciliation | Accepted | 2026-07-07 | push applies in seconds; sources poll `1h`, reconcilers `10m` |
| [0007](0007-ceph-csi-storage-classes-by-access-pattern.md) | Ceph through ceph-csi; class by access pattern | Accepted, amended by 0035 | 2026-07-07 | `ceph-rbd` for databases, `cephfs` for files, `cephfs-appdata` for bulk |
| [0008](0008-pv-reaper-split-reclaim.md) | pv-reaper split reclaim | Accepted | 2026-07-07 | deleting the PV destroys the volume, even with `Retain` |
| [0009](0009-own-code-in-own-repos.md) | Own code in its own repos | Accepted | 2026-07-07 | operators, apps and forks publish releases; this repo pins them |
| [0010](0010-ingress-traefik-behind-one-vip.md) | Ingress: one Traefik behind one VIP | Accepted | 2026-07-08 | wildcard certificate by DNS-01, TCP entrypoints, trusted outer proxy |
| [0011](0011-generator-settings-are-annotations.md) | Generator settings are annotations | Accepted | 2026-07-08 | `flux-component-generator/` keys on the component's `kustomization.yaml` |
| [0012](0012-prune-off-around-risky-changes.md) | Prune off around risky changes | Accepted | 2026-07-08 | `pruneDisabled`, one PR per step, `check-migration.py` between |
| [0013](0013-kube-vnet-default-deny-ingress.md) | kube-vnet: default-deny ingress | Accepted | 2026-07-08 | ingress only by explicit vnet membership, even within a namespace |
| [0014](0014-one-shared-cloudnative-pg-cluster.md) | One shared CloudNativePG cluster | Accepted | 2026-07-08 | a role and database per app; three instances on RBD, synchronous replication |
| [0015](0015-self-hosted-renovate-automerge-policy.md) | Self-hosted Renovate; automerge policy | Accepted | 2026-07-09 | patch and digests automerge on a green `lint` status; the rest is reviewed |
| [0016](0016-validate-sh-one-offline-gate.md) | `validate.sh`: one offline gate | Accepted | 2026-07-09 | strict schemas, golden tests and a check script per invariant |
| [0017](0017-backups-stay-in-ceph.md) | Backups stay in Ceph | Accepted | 2026-07-09 | Velero CSI snapshots, barman for Postgres; no off-site copy yet |
| [0018](0018-k0s-upgrades-through-autopilot.md) | k0s upgrades through Autopilot | Accepted | 2026-07-10 | the version in `k0sctl.yaml` names a Job that recreates the Plan |
| [0019](0019-flux-manifests-from-gen-flux-never-bootstrap.md) | Flux manifests from `gen-flux.sh`, never `flux bootstrap` | Accepted | 2026-07-10 | source-watcher included; controllers tuned from measurement |
| [0020](0020-swarm-data-mounted-in-place.md) | Swarm data mounted in place (exception) | Accepted | 2026-07-11 | static PVs: not reaped, not snapshotted, never pruned |
| [0021](0021-cross-namespace-copies-are-reflected.md) | Cross-namespace copies are reflected | Accepted | 2026-07-16 | one source per shared Secret or Middleware; copies in git only where nothing else reaches |
| [0022](0022-sso-authelia-forwardauth-reflected.md) | SSO: Authelia forwardAuth, reflected | Accepted | 2026-07-19 | Middlewares copied into each namespace; Authelia is also the API server's OIDC issuer |
| [0023](0023-etcd-on-local-disk.md) | etcd on a local disk | Accepted | 2026-07-20 | never on Ceph-backed storage; VM-level, outside the repo |
| [0024](0024-safety-boundaries.md) | Safety boundaries | Accepted | 2026-07-23 | hypervisors and Ceph read-only, Swarm untouched, secrets unseen, denials escalated |
| [0025](0025-manifest-rules-namespaces-and-config-rollout.md) | Explicit namespaces; config changes reach the pod | Accepted | 2026-07-25 | no `namespace:` transformer; generated ConfigMaps; Reloader for Secrets |
| [0026](0026-cpu-baseline-ivybridge.md) | The CPU baseline is IvyBridge | Accepted | 2026-07-26 | x86-64-v2 with AVX; v3-only images are rebuilt, never downgraded |
| [0027](0027-everything-pinned-and-checked.md) | Everything pinned and checked | Accepted | 2026-07-27 | tags, digests, source revisions and download digests, all Renovate-tracked |
| [0028](0028-workload-defaults-requests-limits-drains.md) | Workload defaults | Accepted | 2026-07-29 | a request everywhere, memory limits, no CPU limits |
| [0029](0029-availability-single-replicas-and-ha-surge.md) | Availability: one replica and ha-surge | Accepted | 2026-07-29 | drains covered make-before-break; replication only where state needs it |
| [0030](0030-telemetry-through-kafka.md) | Telemetry through Kafka | Accepted | 2026-07-30 | one gateway into Kafka; the consumer retries, exporters keep no queue |
| [0031](0031-beyond-ingress-isolation.md) | Beyond ingress isolation | Accepted | 2026-07-31 | egress policies for untrusted jobs, Multus macvlan, a VPN gateway |
| [0032](0032-files-come-from-images-never-fetched.md) | Files come from images, never fetched | Accepted | 2026-08-05 | image volumes, or kube-oci-composer; Spegel gets its own config root |
| [0033](0033-spec-hash-tags-for-composed-images.md) | Spec-hash tags for composed images | Superseded by 0042 | 2026-08-05 | tags derived from the build spec, rewritten into consumers |
| [0034](0034-chart-crds-upgraded-by-default.md) | Chart CRDs upgraded by default | Accepted | 2026-08-09 | `crds: CreateReplace` on every HelmRelease, overridable |
| [0035](0035-a-csi-driver-per-failure-domain.md) | A CSI driver per failure domain | Accepted | 2026-08-17 | the HDD filesystem has its own driver, so its hangs stay its own |
| [0036](0036-public-template-from-make-public.md) | Public template from `make-public.py` | Accepted | 2026-08-24 | a verified, sanitised orphan branch; this repo stays private |
| [0037](0037-comments-record-traps.md) | Comments record traps | Accepted | 2026-09-03 | one to three lines, no narratives; agent rules in `AGENTS.md` |
| [0038](0038-plaintext-config-substituted-secrets.md) | Plaintext configs, substituted secrets | Accepted | 2026-09-04 | `substitution-secrets/`, `as-secret`, strict substitution |
| [0039](0039-one-generator-release-per-component.md) | One generator release per component | Accepted | 2026-09-17 | the 1 MiB release cap; size checked in the lint |
| [0040](0040-filtered-gitrepository-per-component.md) | Filtered GitRepository per component | Superseded by 0047 | 2026-09-17 | `spec.ignore` clones as the per-component chart source |
| [0041](0041-change-workflow.md) | Change workflow | Accepted | 2026-09-17 | plan and verify; PRs at deploy boundaries; merge only on green |
| [0042](0042-composed-images-as-exported-digest-refs.md) | Composed images as exported digest refs | Accepted | 2026-09-23 | `artifacts/` exports the digest; the component substitutes it |
| [0043](0043-ci-on-self-hosted-runners.md) | CI on self-hosted runners | Accepted | 2026-09-24 | in-cluster, ephemeral, confined by a remote-docker workspace of their own |
| [0044](0044-control-namespaces-opt-in.md) | Control namespaces, opt-in | Superseded by 0046 | 2026-10-03 | the switch used to migrate one component at a time |
| [0045](0045-namespaces-dir-never-pruned.md) | `namespaces/`, never pruned | Accepted | 2026-10-03 | the Namespace has its own Kustomization; removing a component leaves it |
| [0046](0046-control-namespaces-always-on.md) | Control namespaces always on | Accepted | 2026-10-04 | each component's Kustomizations and their sources in `gen-<name>` |
| [0047](0047-externalartifacts-per-component.md) | ExternalArtifacts per component and root | Accepted | 2026-10-04 | content-hash revisions: a commit reconciles only what it changed |
| [0048](0048-several-clusters-one-repo.md) | Several clusters in one repo | Accepted | 2026-10-04 | a directory and age key per cluster, `shared/` bases, versions in overlays |
| [0049](0049-victoriametrics-hot-cold-and-dedup-downsampling.md) | VictoriaMetrics hot/cold and dedup (exception) | Accepted | 2026-10-04 | one store over two volumes; a 5-year tier downsampled by dedup |
| [0050](0050-arc-scale-sets-for-per-job-runners.md) | ARC scale sets for per-job runners | Accepted | 2026-10-05 | a pod per job; Docker through ephemeral remote-docker clients |
| [0051](0051-notesnook-mongodb-held-at-8.md) | Notesnook's MongoDB stays on 8 (exception) | Accepted | 2026-10-05 | its C# drivers do not support MongoDB 9; Renovate's PR is held |
| [0052](0052-flux-alerts-go-to-matrix.md) | Flux alerts go to Matrix | Accepted | 2026-10-08 | error events post to an unencrypted room; generator renders the Alert |
| [0053](0053-grafana-dashboards-provisioned-from-git.md) | Grafana dashboards provisioned from git | Accepted | 2026-10-08 | file provider, read-only in the UI; edit the JSON in git |
| [0054](0054-descheduler-respreads-replicas.md) | The descheduler re-spreads replicas | Accepted | 2026-10-08 | spread-only plugins every 15 min; PVC, system-critical, DaemonSet pods and kube-system protected |

## Adding a record

Write one when a change decides something about the cluster or the repo's structure, or when an
app deliberately departs from a rule here.

1. Copy the shape of an existing record: `NNNN-short-title.md`, the next number.
2. Header: `Status` (Proposed, Accepted, Superseded by NNNN, or Accepted with "amended by"), `Date`,
   and a one-paragraph `Current answer`.
3. Then Context (what forced it), Decision, Consequences (including traps), Alternatives where
   there were any, and References: commit SHAs and PR numbers.
4. A later decision that replaces one gets its own record; mark the old one superseded and link
   both ways. A change that only refines one amends it in place, with the date in its header.
5. Add the row above, in the same PR as the change. Keep it to the decision (AGENTS.md,
   "Decisions"), and link to READMEs rather than repeating them.
