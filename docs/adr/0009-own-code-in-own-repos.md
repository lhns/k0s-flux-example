# 0009 — Own code lives in its own repos; this repo pins releases

- Status: Accepted
- Date: 2026-07-07, amended 2026-07-29, 2026-08-18, 2026-09-14, 2026-09-18 and 2026-09-30
- Current answer: what the owner writes for the cluster lives in a repository of its own with
  its own CI, and publishes an image or OCI chart: the operators kube-vnet, kube-pv-reaper,
  kube-traefik-reflector, ha-surge and kube-oci-composer, the Headlamp app-view plugin,
  remote-docker, bespoke apps such as matrix-sip-bridge, and the forks and rebuilds he maintains
  (Asterisk, mautrix bridges, JetKVM, ceph-csi for x86-64-v2). This repo pins a released version
  and sets the values specific to this cluster; it carries no source code of them.

## Context

pv-reaper started as a script in a ConfigMap and became kube-pv-reaper, with its own image, chart
and releases. Every later controller followed that shape.

## Decision

- **Own repo, published artifacts**: a commit publishes a `sha-` build, a version tag a release;
  this repo pins one and Renovate bumps it ([0015](0015-self-hosted-renovate-automerge-policy.md)).
  A dev build may be tried here, but what stays deployed is a release (kube-vnet, 2026-07-20).
- **Generic tools, cluster values here** (2026-07-29): a tool does not hardcode this cluster.
  Knowledge of kube-vnet's runtime labels stays out of ha-surge and is a flag set from this repo.
  ha-surge's always-excluded namespaces, by contrast, are safety rails in its own chart, changed
  only by a release.
- **Maintained on their own** (2026-08-18): oci-composer has its own agent; a defect found here
  goes to its repo as a bug report, not as a patch from this repo.
- **Bespoke apps too** (2026-09-14): matrix-sip-bridge got its own repo and CI image, configured
  by YAML like the other bridges, so this repo stays configuration only.
- **Patches become forks** (2026-09-18, 2026-09-30): a small patch for an upstream PR may be
  applied at build time; anything bigger (a database migration, UI changes) is a real fork with
  feature branches on upstream and a build branch. Asterisk's patches, base64-embedded in an
  `ImageBuild`, became a fork with its own CI. Fork work opens no upstream PR unless asked and
  carries no personal data.

## Consequences

- A fix reaches the cluster only after a release in its own repo.
- A fork's moving branch tag needs a never-automerge rule.
- Renovate tracks each artifact like any upstream one ([0027](0027-everything-pinned-and-checked.md)).

## References

- `93f0fd24` (pv-reaper chart), `abe628c6` (traefik-reflector), `00e16011`, `88c632d2`
  (ha-surge), `d1131caa` (Headlamp plugin), `10f50483` (remote-docker), `3371bf42`
  (matrix-sip-bridge), `c94f94c9` (Asterisk fork), `2915be5d`, `72d2908b` (JetKVM patch, then
  forks), `df3b4ffa` (ceph-csi build).
- Conversation 2026-07-07 (pv-reaper), 2026-07-20 (dev builds), 2026-07-29 (ha-surge values),
  2026-08-18 (oci-composer's agent), 2026-09-14, 2026-09-18 and 2026-09-30 (bespoke apps, forks).
