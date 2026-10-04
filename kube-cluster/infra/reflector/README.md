# reflector

[emberstack/kubernetes-reflector](https://github.com/emberstack/kubernetes-reflector)
— a small controller that **mirrors Secrets and ConfigMaps across namespaces**,
driven by annotations. It only touches objects that opt in; by default it copies
nothing.

Use it when the same **credential we own** is needed in two namespaces and we don't
want to duplicate the SOPS file. Keep one source secret in git, annotate it, and
Reflector auto-creates + keeps the copy in sync (and deletes it if the source goes
away).

## Auto (push) mode — annotate the source

```yaml
metadata:
  annotations:
    reflector.v1.k8s.emberstack.com/reflection-allowed: "true"
    reflector.v1.k8s.emberstack.com/reflection-allowed-namespaces: "grafana"
    reflector.v1.k8s.emberstack.com/reflection-auto-enabled: "true"
    reflector.v1.k8s.emberstack.com/reflection-auto-namespaces: "grafana"
```

Keep `reflection-allowed-namespaces` tight (name the exact namespaces) — it's the
allowlist for who may receive the secret.

**First user:** `apps/grafana` — the `grafana-db` credential lives once in the
`postgres` namespace (for the CNPG `DatabaseRole`) and is reflected into `grafana`
(for `GF_DATABASE_PASSWORD`). Apps that consume a reflected secret should
`dependsOn: infra-reflector`.

Only handles **Secrets/ConfigMaps** — not CRDs (so not Traefik middlewares).
Egress-only (talks to the API server); no kube-vnet rule needed.

## sops-age in control namespaces

Every component decrypts with a `sops-age` in its control namespace `gen-<name>`.
`flux-system/sops-age` exists only in the cluster (bootstrap), so it is annotated by hand,
**once per cluster**:

```sh
kubectl -n flux-system annotate secret sops-age \
  reflector.v1.k8s.emberstack.com/reflection-allowed=true \
  reflector.v1.k8s.emberstack.com/reflection-allowed-namespaces-selector=flux-component-generator/control-namespace=true \
  reflector.v1.k8s.emberstack.com/reflection-auto-enabled=true \
  reflector.v1.k8s.emberstack.com/reflection-auto-namespaces-selector=flux-component-generator/control-namespace=true
```

The label selectors need Reflector 10.0.65 or later. Set no `reflection-allowed-namespaces`
next to them: either one matching admits a namespace, and both empty admits every namespace.
The label therefore hands out the cluster's decryption key; only the generator sets it, which
`scripts/check-substitution.py` enforces for the repo. Removing the label, or the annotations,
deletes the copies, and every moved component then fails to decrypt.
