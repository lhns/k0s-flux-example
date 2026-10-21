# headlamp

[Headlamp](https://headlamp.dev) — a Kubernetes dashboard, at
`headlamp.kube.example.com` (traefik ingress, wildcard TLS).

- `release.yaml` — HelmRepository `headlamp` + HelmRelease `headlamp`.
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

## Break-glass login

Normal logins are OIDC through Authelia. When Authelia is down, use a short-lived token for the
ServiceAccount `headlamp-breakglass` (`breakglass.yaml`, bound to cluster-admin):

```sh
ssh admin@10.20.150.84 'kubectl -n headlamp create token headlamp-breakglass --duration=1h'
```

Open `headlamp.kube.example.com`, choose **Use A Token** instead of **Sign In**, and paste it. Headlamp
offers both whenever OIDC is configured and auto-redirect is off (the default). The token expires
on its own; nothing stores it.

## Decisions

- **Headlamp's own ServiceAccount has no binding.** Every call is made with the logged-in user's
  token (OIDC, or the break-glass token above). The SA token stays mounted only because in-cluster
  mode builds its config from it (`rest.InClusterConfig()` reads the token and `ca.crt` files);
  unbound, it grants discovery only.
- **The break-glass account is a separate ServiceAccount** that no pod mounts, so its rights exist
  only in a token someone deliberately mints on the jumphost.
