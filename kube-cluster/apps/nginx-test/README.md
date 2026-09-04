# nginx-test

A throwaway `nginx:alpine` Pod — a scratch workload for poking at the cluster.
A Service but no Ingress: reachable in-cluster, not from outside. Drop the
whole component when you don't need it.

It doubles as the canary for `postBuild.substituteFrom`: `probe.yaml` is a plaintext
Secret holding `${NGINX_TEST_VALUE}`. If it ever reads back as that literal string
instead of a value, substitution ordering is broken cluster-wide.

- `resources.yaml` — the `nginx-test` namespace + the pod.
- `probe.yaml` — the substitution canary.
- `substitution-secrets/` — the value it substitutes.
