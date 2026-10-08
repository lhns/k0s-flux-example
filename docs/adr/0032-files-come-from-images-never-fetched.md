# 0032 — Files a workload needs come from images, never fetched at pod start

- Status: Accepted
- Date: 2026-08-05, amended 2026-08-24 and 2026-10-07
- Current answer: plugins, extensions and config bundles are never downloaded or cloned when a
  pod starts. If the content is already an image, mount it as an image volume (read-only, `subPath`
  for layout). If it is not (git paths, jars by URL, Debian packages), compose an OCI artifact
  with kube-oci-composer from the component's `artifacts/` and mount that. The composer's
  registry is pulled by each node's containerd through a host-level drop-in, in a containerd
  config root that Spegel does not own.

## Context

Fetching plugins or config in an init container on every start adds a network dependency at
startup, pins versions by ref rather than by verified content, writes a cache onto the data PVC
where it becomes state, and needs ownership fixed by `chown -R`.

## Decision

- **Image volumes** for content that is already an image; the line is whether the inputs are
  images, not how complex the result is. The rule is repo-wide, so it is in the root README
  rather than in oci-composer's own docs.
- **kube-oci-composer** ([0009](0009-own-code-in-own-repos.md)) assembles the rest from
  content-addressed inputs and pushes to its own registry (2026-07-31). Flux builds nothing, and
  baking a plugin into a custom image would tie it to the upstream image's release cadence.
  Workloads name a digest, never a mutable tag, and Renovate, already trusted, closes the update
  loop rather than Flux's image automation with write access to git.
- **Pulls are done by the kubelet**, so neither cluster DNS nor kube-vnet applies and a public
  hostname would hairpin through the edge. The registry is addressed by a name that resolves
  nowhere (`oci-composer.internal`), and `k0s-files/` tells each node's containerd where it is.
- **Spegel gets its own config root** (2026-08-24): Spegel, the peer-to-peer image mirror, clears
  its registry config directory on every start, so hand-written config sits in a separate root
  listed first. CRI pulls use `use_local_image_pull`, since the default transfer service cannot
  parse a list of roots.
- **Spegel as a fallback** (2026-10-07): Spegel indexes and serves `oci-composer.internal:30500`
  images, but its `_default` never applies to a registry with its own directory. The hand-written
  `hosts.toml` therefore lists the three Spegel ports (30020) before the NodePorts, so the single
  registry pod moving does not block nodes whose peers already hold the digest.

## Consequences

- An image-volume reference needs a `# renovate:` comment, or Renovate stops bumping it.
- Changing `k0s-files/` takes a `k0sctl apply` and a worker restart; k0sctl does not remove a
  dropped upload.
- How a consumer names a composed image changed: [0033](0033-spec-hash-tags-for-composed-images.md),
  then [0042](0042-composed-images-as-exported-digest-refs.md).

## References

- `8155b29c`, `44b04abd`, `98bad1ad`, `6019aa1c`, `9366cce9`, `dad2cb79`.
- Conversation 2026-07-31 to 2026-08-03 (composer scope, digests).
- README: [Getting files into a workload](../../README.md#getting-files-into-a-workload);
  [infra/oci-composer](../../kube-cluster/infra/oci-composer/README.md),
  [infra/spegel](../../kube-cluster/infra/spegel/README.md).
