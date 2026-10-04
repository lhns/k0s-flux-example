{{- /*
generators.controlNamespace is the namespace a generated Kustomization lives in: gen-<component>.
Takes its name. A -namespaces, -artifacts or -substitution-secrets name resolves through its
component; generate.yaml refuses component names that end like that. A dependsOn on anything
the generator does not emit, such as k0s-upgrade in flux-system, therefore cannot resolve.
*/ -}}
{{- define "generators.controlNamespace" -}}
gen-{{ regexReplaceAll "-(namespaces|artifacts|substitution-secrets)$" . "" }}
{{- end }}
