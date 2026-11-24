# flux-alerts

Flux problems go to a Matrix room. Two paths (ADR 0052, 0065):

- Error events, posted by `@flux:example.com`, only for the components Grafana alerting depends on
  (`alerts.components`) and the flux-system sources. Grafana cannot report their failure.
- Everything else through Grafana `flux-not-ready`: one message when it breaks, a repeat every
  24h while it stays broken, and a resolved message.

- `provider.yaml`: notification-controller `Provider` `matrix`; the room ID is `channel`.
- `secret.yaml`: SOPS `flux-matrix`, key `token`, a MAS compatibility token for `@flux:example.com`.
- The `flux-errors` Alert is rendered by the generator, not here: `templates/alerts.yaml`,
  enabled by `alerts` in `kube-cluster/flux-system/generators.yaml`; `alerts.components` lists
  the components it covers, and the render fails on a name that is not generated. ADR 0052.

The room must stay unencrypted: Flux cannot do E2EE.

Rotate the token in the `mas` pod, piped straight into sops on the jumphost, never displayed:
`mas-cli manage issue-compatibility-token flux FLUXNOTIFY` (`MAS_CONFIG` supplies the config).

## Alerts

`grafana.yaml`: rule group `infra-flux-alerts`, folder *Cluster*; both rules repeat every 24h.

| Rule | Fires when | For | Severity |
| --- | --- | --- | --- |
| `flux-never-reconciling` | no reconcile of an unsuspended object in 25h | 1h | info |
| `flux-not-ready` | `gotk_resource_info` `ready` is `False` or `Unknown`, not suspended, reason not `DependencyNotReady` | 15m | warning |

Objects without conditions (OCI HelmRepositories) have no `ready` label and never match.

Backtest: `gotk_resource_info` exists only since 2026-10-26, so `flux-not-ready` was run over the
few minutes available. Only `infra-velero` matched, once, as `Unknown`/`Progressing`, which `for: 15m`
absorbs. `flux-never-reconciling` over 7 days: only two suspended Kustomizations,
which the suspend exclusion now drops.
