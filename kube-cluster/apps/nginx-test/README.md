# nginx-test

A throwaway `nginx:alpine` Pod — a scratch workload for poking at the cluster.
No Service/Ingress, so kube-vnet leaves it isolated (nothing reaches it). Drop the
whole component when you don't need it.

- `resources.yaml` — the `nginx-test` namespace + the pod.
