{{- /*
generators.substituteFrom derives postBuild.substituteFrom from a component's
substitution-secrets/ subdirectory. Takes dict "root" $ "dir" <component dir>.

The directory name is reserved, and its presence is the entire opt-in. Every Secret and
ConfigMap found inside becomes one entry, named from the manifest itself rather than
from a list, so a rename cannot leave the two out of step.

Only flux-system objects qualify. Flux resolves substituteFrom in the Kustomization's
own namespace, so anything else could never be found and optional:false would wedge the
component. Objects that do not qualify are still applied, they are just not substituted
from.

Map iteration is key-sorted, so the order is stable. Where two sources define the same
key the later entry wins.
*/ -}}
{{- define "generators.substituteFrom" -}}
{{- $out := list }}
{{- range $file, $_ := .root.Files.Glob (printf "%s/substitution-secrets/*.yaml" .dir) }}
{{- range $doc := regexSplit "(?m)^---[[:space:]]*$" ($.root.Files.Get $file) -1 }}
{{- $obj := fromYaml $doc }}
{{- if and (has (dig "kind" "" $obj) (list "Secret" "ConfigMap")) (eq (dig "metadata" "namespace" "" $obj) "flux-system") }}
{{- $out = append $out (dict "kind" $obj.kind "name" $obj.metadata.name) }}
{{- end }}
{{- end }}
{{- end }}
{{- toYaml $out }}
{{- end }}
