{{- /*
generators.artifacts collects the composed OCI artifacts a component declares.
Arg: dict "root" $ "dir" <component dir> "publicHost" <registry host:port>.
Returns a list of {name, kind, tag, repo}; the caller emits both ends from it.

ImageComposition and ImageBuild are found by kind, so any file may hold them and a
component may have several. They differ only in where the input is named, layers[].
sourceRef against context.sourceRef.

The tag is "s" plus a truncated sha256 of the hash input. The caller writes it both into
the component's kustomize `images:` and into the object's own push.tags, so the artifact
and its consumer move together in one commit with nothing observed at runtime. See ADR
0017 in lhns/kube-oci-composer.

The hash input is the spec minus publish, push, interval and suspend. push holds the tag
itself, which would be circular, and neither where an artifact is published nor how often
it is polled changes what it is. `publish` is the pre-0.5.0 name of `push` and stays in
the omit list so that rename moved no existing tag.

A sourceRef names its input rather than containing it, so the pin that decides the
content lives in the referenced object. Hashing the spec alone misses a version bump
entirely, which is the one property a spec hash exists to provide, so the referenced
spec is folded in with interval, timeout and suspend dropped. That wrapping happens only
when there is something to add, so a composition with no sourceRef keeps the tag it
already has.

One gap: an ImageBuild whose Dockerfile is a configMapRef. The hash sees the reference
and not the content, and a generated ConfigMap is not a file here to find, so editing it
rebuilds without moving the tag. Prefer `inline`, or expect push.onConflict to fail the
build loudly.

Map iteration is key-sorted, so this is deterministic.
*/ -}}
{{- define "generators.artifacts" -}}
{{- $root := .root }}
{{- /* Index every object in the directory by kind/name so a sourceRef can resolve. */ -}}
{{- $objects := dict }}
{{- range $file, $_ := .root.Files.Glob (printf "%s/*.yaml" .dir) }}
{{- range $doc := regexSplit "(?m)^---[[:space:]]*$" ($root.Files.Get $file) -1 }}
{{- $obj := fromYaml $doc }}
{{- if and (dig "kind" "" $obj) (dig "metadata" "name" "" $obj) }}
{{- $_ := set $objects (printf "%s/%s" $obj.kind $obj.metadata.name) $obj }}
{{- end }}
{{- end }}
{{- end }}
{{- $out := list }}
{{- range $key, $obj := $objects }}
{{- if has $obj.kind (list "ImageComposition" "ImageBuild") }}
{{- $refs := list }}
{{- if eq $obj.kind "ImageComposition" }}
{{- range $layer := (dig "spec" "layers" (list) $obj) }}
{{- with (dig "sourceRef" (dict) $layer) }}{{- $refs = append $refs . }}{{- end }}
{{- end }}
{{- else }}
{{- with (dig "spec" "context" "sourceRef" (dict) $obj) }}{{- $refs = append $refs . }}{{- end }}
{{- end }}
{{- $sources := dict }}
{{- range $ref := $refs }}
{{- $refKey := printf "%s/%s" (dig "kind" "" $ref) (dig "name" "" $ref) }}
{{- with (get $objects $refKey) }}
{{- $_ := set $sources $refKey (omit (dig "spec" (dict) .) "interval" "timeout" "suspend") }}
{{- end }}
{{- end }}
{{- $hashInput := omit $obj.spec "publish" "push" "interval" "suspend" }}
{{- if $sources }}
{{- $hashInput = dict "spec" $hashInput "sources" $sources }}
{{- end }}
{{- $out = append $out (dict
      "name" $obj.metadata.name
      "kind" $obj.kind
      "tag"  (printf "s%s" ($hashInput | toYaml | sha256sum | trunc 16))
      "repo" (printf "%s/%s/%s" $.publicHost $obj.metadata.namespace $obj.metadata.name)) }}
{{- end }}
{{- end }}
{{- toYaml $out }}
{{- end }}
