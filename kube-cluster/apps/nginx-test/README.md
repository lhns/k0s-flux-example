# nginx-test

A single-replica `nginx:alpine` Deployment that carries three canaries. Reachable in-cluster, and
at `nginx-test.example.com` behind the `anubis` middleware, as the canary for
[apps/anubis](../anubis/README.md). It is also the kube-ha-surge drain test (the container sleeps
30s before nginx starts, so a break-before-make gap shows as an outage; see `resources.yaml`).

The third is the canary for `postBuild.substituteFrom`: `probe/probe.conf` is a plaintext
`configMapGenerator` source holding `${NGINX_TEST_VALUE}`, patched to a Secret. If it ever reads back as that literal string
instead of a value, substitution ordering is broken cluster-wide.

- `resources.yaml` — the Deployment and Service.
- `routing.yaml` — the Anubis-protected test route.
- `probe/probe.conf` — the substitution canary.
- `substitution-secrets/` — the value it substitutes.
