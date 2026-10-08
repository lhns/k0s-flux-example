# arc-controller

GitHub's [actions-runner-controller](https://github.com/actions/actions-runner-controller) in
runner scale set mode: the `gha-runner-scale-set-controller` chart. The scale sets themselves,
their pods and their credentials are in [`apps/github-runner`](../github-runner/README.md).

- **One namespace.** `flags.watchSingleNamespace: github-runner` gives the controller a Role
  there rather than a ClusterRole over every namespace's pods and Secrets. A scale set elsewhere
  needs a second controller.
- **Listeners run here**, one pod per scale set (`<scale set>-*-listener`). Each long-polls the
  GitHub Actions service with the scale set's credential, which the controller copies into this
  namespace, and patches the EphemeralRunnerSet when jobs arrive.
- **Network.** The controller and the listeners talk to the API server, and the listeners to
  GitHub. The cluster baseline restricts ingress only and nothing dials either (no webhooks),
  so there is no egress policy here.
- **Metrics** (`gha_*`) are on, on :8080 of the controller and of every listener. The otel `cluster`
  collector scrapes them by annotation; `vnet.yaml` admits it, and `arc-system` is listed in
  `infra/opentelemetry-monitoring`. The scale sets annotate their listeners through
  `listenerTemplate.metadata`. In VictoriaMetrics the metric's own `namespace` label becomes
  `exported_namespace`, and the scale set is `actions_github_com_scale_set_name` (a pod label).
  `listenerMetrics` stays unset, which keeps the `job_workflow_*` labels off. Dashboard:
  "GitHub Runners" in Grafana ([ADR 0053](../../../docs/adr/0053-grafana-dashboards-provisioned-from-git.md)).
- **CRDs** come from the chart's `crds/` and are upgraded by the generator's default
  `crdPolicy: CreateReplace`.

## Upgrading

The controller and `gha-runner-scale-set` must be the same version; `renovate.json` groups the
two charts into one PR, never automerged. Read the release notes first: some ARC upgrades have
required removing the scale sets before the controller.
