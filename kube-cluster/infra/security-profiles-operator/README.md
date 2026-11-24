# security-profiles-operator

The [Security Profiles Operator](https://github.com/kubernetes-sigs/security-profiles-operator)
(SPO): a `SeccompProfile` object becomes `<kubelet root>/seccomp/operator/<name>.json` on every node,
also on nodes added later, for pods to use as a `Localhost` profile. Seccomp only; the chart's
SELinux, AppArmor and recording features stay off.

Used by `apps/webtop`: its chart ships the desktops' profile (ADR 0069).

- `kubeletDir: /var/lib/k0s/kubelet`: k0s's kubelet root.
- Needs cert-manager (`dependsOn: infra-cert-manager`): the operator requests its webhook and metrics
  certificates there.
- The daemon (`spod`) runs privileged on every node.
- Helm never upgrades CRDs, but the generator does (`crdPolicy` default `CreateReplace`), so a chart
  bump brings its CRDs.

A container whose `Localhost` profile is not yet on its node does not start; the kubelet retries
until the daemon has written it. `kubectl get seccompprofiles` shows each profile's status
(`Installed` once it is on every node).
