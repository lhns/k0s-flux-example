# dashy

The [Dashy](https://dashy.to) start page (`lissy93/dashy`) at
**`dashy.example.com`**, behind Authelia SSO. Ported from a Docker Swarm stack.

- `conf.yml` — the dashboard config (managed as code). Rendered into a hashed
  `ConfigMap` by kustomize (`configMapGenerator`) and mounted **read-only** at
  `/app/user-data/conf.yml`; editing this file rolls the pod. Dashy's own OIDC
  (`appConfig.auth`, pointing at `https://auth.example.com`) drives the per-item group
  visibility.
- `resources.yaml` — Deployment, Service, Ingress.

The Ingress uses the reflected `dashy-authelia@kubernetescrd` middleware.

> Config-as-code: the in-UI editor is off (`allowConfigEdit: false`) — change the
> dashboard by editing `conf.yml` here.
