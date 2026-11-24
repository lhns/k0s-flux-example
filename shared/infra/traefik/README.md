# traefik (shared)

Only what every cluster's Traefik needs the same: `vnet.yaml`, the `traefik` vnet. Backends join
it (ingress) to receive from Traefik; Traefik pods are egress members. The chart, its values and
the TLS are per cluster, in `<cluster>/infra/traefik/`, which lists this base in `resources`
and in `flux-component-generator/bases`.
