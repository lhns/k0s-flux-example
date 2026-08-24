# headlamp

[Headlamp](https://headlamp.dev) — a Kubernetes dashboard, at
`headlamp.kube.example.com` (traefik ingress, wildcard TLS).

- `release.yaml` — HelmRepository `headlamp` + HelmRelease `headlamp` `0.44.0`.
  Plugins are managed declaratively via `pluginsManager`: **helm**, **flux**, and
  **cert-manager**. The `kube-vnet/net.traefik.traefik: ingress` pod label lets
  traefik reach it.
- **`headlamp-app-view`**, our own plugin, isn't on ArtifactHub so it can't ride the
  `pluginsManager`. It's mounted straight from `ghcr.io/lhns/headlamp-app-view` as an
  **image volume**, into Headlamp's separate *user*-plugins dir — not the shared
  `/headlamp/plugins`, which the pluginsManager reconciles to its own config and would
  prune anything unlisted from.

  This replaced an initContainer that copied the plugin out of that same image into an
  emptyDir. The kubelet already has the image mounted, so the copy bought nothing.
  `subPath: plugins` handles the layout (the plugin sits at `/plugins/headlamp-app-view`
  in the image); image volumes mount read-only regardless, verified on-cluster.

  **No oci-composer here, deliberately.** The artifact is already a published image, so
  composing would only re-wrap it. The composer earns its place when the inputs are *not*
  images — git repos, jars, ConfigMaps. See `apps/freshrss`.

  Renovate tracks the reference through the `image-volume` custom manager in
  `renovate.json`; the built-in kubernetes manager only sees `image: <string>`, and an
  image volume spells it as a mapping.
