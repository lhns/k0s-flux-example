# flux-alerts

Flux error events go to a Matrix room, posted by `@flux:example.com`.

- `provider.yaml`: notification-controller `Provider` `matrix`; the room ID is `channel`.
- `secret.yaml`: SOPS `flux-matrix`, key `token`, a MAS compatibility token for `@flux:example.com`.
- The `flux-errors` Alert is rendered by the generator, not here: `templates/alerts.yaml`,
  enabled by `alerts` in `kube-cluster/flux-system/generators.yaml`. ADR 0052.

The room must stay unencrypted: Flux cannot do E2EE.

Rotate the token in the `mas` pod, piped straight into sops on the jumphost, never displayed:
`mas-cli -c /config/config.yaml manage issue-compatibility-token flux FLUXNOTIFY`.
