{{- /*
generators.substituteFrom derives postBuild.substituteFrom from a component's
substitution-secrets/ subdirectory. Takes dict "root" $ "dir" <component dir> "namespace"
<the Kustomization's namespace>.

The directory name is reserved, and its presence is the entire opt-in. Every Secret and
ConfigMap found inside becomes one entry, named from the manifest itself rather than
from a list, so a rename cannot leave the two out of step.

Only objects in that namespace qualify: the component's control namespace (kustomizations.yaml).
Flux resolves substituteFrom in the Kustomization's own namespace, so anything else could never
be found and optional:false would wedge the component. Objects that do not qualify are still
applied, they are just not substituted from. One in flux-system fails the render instead:
dropping it would silently lose its keys.

Map iteration is key-sorted, so the order is stable. Where two sources define the same
key the later entry wins.
*/ -}}
{{- define "generators.substituteFrom" -}}
{{- $out := list }}
{{- range $file, $_ := .root.Files.Glob (printf "%s/substitution-secrets/*.yaml" .dir) }}
{{- range $doc := regexSplit "(?m)^---[[:space:]]*$" ($.root.Files.Get $file) -1 }}
{{- $obj := fromYaml $doc }}
{{- if has (dig "kind" "" $obj) (list "Secret" "ConfigMap") }}
{{- $objNs := dig "metadata" "namespace" "" $obj }}
{{- if eq $objNs $.namespace }}
{{- $out = append $out (dict "kind" $obj.kind "name" $obj.metadata.name) }}
{{- else if eq $objNs "flux-system" }}
{{- fail (printf "%s: %s/%s is in flux-system, but its component lives in %s; move it there" $file $obj.kind $obj.metadata.name $.namespace) }}
{{- end }}
{{- end }}
{{- end }}
{{- end }}
{{- toYaml $out }}
{{- end }}
{{- /*
generators.refExports derives substituteFrom entries from a component's artifacts/
subdirectory. Takes the same dict as above.

One per ImageComposition or ImageBuild whose push.writeRefTo targets that namespace (the
rule above), named as oci-composer names its ConfigMap, which is not configurable:
<lowercased kind>-<namespace>-<name>.
*/ -}}
{{- define "generators.refExports" -}}
{{- $out := list }}
{{- range $file, $_ := .root.Files.Glob (printf "%s/artifacts/*.yaml" .dir) }}
{{- range $doc := regexSplit "(?m)^---[[:space:]]*$" ($.root.Files.Get $file) -1 }}
{{- $obj := fromYaml $doc }}
{{- if has (dig "kind" "" $obj) (list "ImageComposition" "ImageBuild") }}
{{- $to := dig "spec" "push" "writeRefTo" "namespace" "" $obj }}
{{- if eq $to $.namespace }}
{{- $out = append $out (dict "kind" "ConfigMap" "name" (printf "%s-%s-%s" (lower $obj.kind) $obj.metadata.namespace $obj.metadata.name)) }}
{{- else if eq $to "flux-system" }}
{{- fail (printf "%s: %s/%s writes its ref into flux-system, but its component lives in %s; point writeRefTo there" $file $obj.kind $obj.metadata.name $.namespace) }}
{{- end }}
{{- end }}
{{- end }}
{{- end }}
{{- toYaml $out }}
{{- end }}
