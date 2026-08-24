# traefik-reflector

[kube-traefik-reflector](https://github.com/lhns/kube-traefik-reflector) — a controller
that mirrors Traefik "policy" CRDs (`Middleware`, `MiddlewareTCP`, `TLSOption`,
`ServersTransport`, `ServersTransportTCP`) across namespaces based on a per-resource
annotation. It lets routers reference those objects **locally**, so Traefik's global
`allowCrossNamespace` / `crossProviderNamespaces` can eventually be turned off.

- `release.yaml` — Flux `OCIRepository` + `HelmRelease` off the chart at
  `oci://ghcr.io/lhns/charts/kube-traefik-reflector`.

Pinned to chart/image **`0.1.0`**.

## Using it

Annotate a Traefik policy object with the namespaces allowed to use it:

```yaml
metadata:
  annotations:
    traefik-reflector/namespaces: "duckdb,dashy"        # list, or "*"
    # traefik-reflector/namespace-selector: "team=web"  # or a namespace-label selector
```

A managed copy appears in each target namespace; routers there reference it locally
(e.g. `authelia@kubernetescrd`). Secrets are **not** copied — pair with the emberstack
`reflector` for those (a warning Event is emitted if a referenced Secret is missing).

`dependsOn: infra-traefik` (needs the Traefik CRDs). Egress-only; no kube-vnet rule.
