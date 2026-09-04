{{- /*
generators.crdPatch is the repo-wide default for chart-shipped CRDs. Takes the policy
string; the caller supplies indentation with nindent.

Helm never touches a chart's crds/ directory and helm-controller defaults upgrades to
Skip, so without this a chart's CRDs freeze at whatever version was installed first.
That failure is silent: new fields simply do not exist, and the API server prunes them
from anything that sets one. Since `crds` exists only per HelmRelease, this is the only
place a repo-wide default can live.

Only charts that ship a crds/ directory are affected, currently traefik and velero.
Charts that template their CRDs upgrade them already and this is a no-op for them.

CreateReplace really does replace, which is safe for the additive changes a CRD bump
usually is. If a chart ever drops a version that still has stored objects the API server
rejects it, so that failure is loud rather than destructive. Override per component with
the crdPolicy annotation. This is a merge patch, so an existing spec.upgrade block keeps
its other fields.
*/ -}}
{{- define "generators.crdPatch" -}}
- target:
    kind: HelmRelease
  patch: |
    apiVersion: helm.toolkit.fluxcd.io/v2
    kind: HelmRelease
    metadata:
      name: unused   # the target selector above chooses; this name is not matched against
    spec:
      upgrade:
        crds: {{ . }}
{{- end }}
