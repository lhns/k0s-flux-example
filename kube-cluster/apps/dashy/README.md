# dashy

The [Dashy](https://dashy.to) start page (`lissy93/dashy`) at
**`dashy.kube.example.com`**, behind Authelia SSO. Ported from a Docker Swarm stack.

- `conf.yml` — the dashboard config (managed as code). Rendered into a hashed
  `ConfigMap` by kustomize (`configMapGenerator`) and mounted **read-only** at
  `/app/user-data/conf.yml`; editing this file rolls the pod. Dashy's own OIDC
  (`appConfig.auth`, pointing at `https://auth.example.com`) drives the per-item group
  visibility.
- `resources.yaml` — the `dashy` namespace, Deployment, Service, Ingress.

The Ingress adds `authelia-authelia@kubernetescrd`, so `dashy` is allowlisted in
Traefik's `providers.kubernetesIngress.crossProviderNamespaces`
(`../../infra/traefik/release.yaml`).

> Config-as-code: the in-UI editor is off (`allowConfigEdit: false`) — change the
> dashboard by editing `conf.yml` here. The image is `:latest`; pin it when
> convenient.
