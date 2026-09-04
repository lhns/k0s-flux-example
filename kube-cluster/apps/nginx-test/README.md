# nginx-test

A throwaway `nginx:alpine` Pod — a scratch workload for poking at the cluster.
A Service but no Ingress: reachable in-cluster, not from outside. Drop the
whole component when you don't need it.

- `resources.yaml` — the `nginx-test` namespace + the pod.
