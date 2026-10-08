# reloader

[Stakater Reloader](https://github.com/stakater/Reloader) — watches ConfigMaps and
Secrets and triggers a **rolling restart** of workloads that reference them, so
config/secret changes actually reach running pods. (Kubernetes never updates
env-var config in a running pod, and volume-mounted config only changes on disk —
the app still has to re-read it.)

**Opt in per workload** (default is off):

```yaml
metadata:
  annotations:
    # restart on ANY referenced ConfigMap/Secret change:
    reloader.stakater.com/auto: "true"
    # or target specific ones:
    # configmap.reloader.stakater.com/reload: "my-config"
    # secret.reloader.stakater.com/reload: "my-secret"
```

A shared base: each cluster deploys it from `<cluster>/infra/reloader/`, which holds the
Namespace (`namespaces/`) and the chart version (`version.yaml`, patched onto the base).

- `release.yaml` — HelmRepository `stakater` + HelmRelease `reloader` without a version
  (watches all namespaces; RBAC + ServiceAccount created by the chart; `default` strategy =
  rolling restart).

No kube-vnet rule needed: Reloader only watches + patches via the API server
(egress); nothing connects to it.
