{{- /*
generators.secretFromFiles turns a marked configMapGenerator output into a Secret.
Emitted for every component; a target that matches nothing is a no-op.

configMapGenerator keeps file content as plaintext under data:, which is what
postBuild substitution needs, because envsubst reads the built YAML and a
secretGenerator would have base64-encoded the ${VAR} out of its reach. There is no
option to stop a secretGenerator encoding: Secret.data is []byte and generators have
to handle binary files. So generate a ConfigMap and move the content to stringData,
where it is still plaintext and the API server does the encoding at apply time.

A component opts in per generator entry:

    configMapGenerator:
    - name: mosquitto-config
      namespace: mosquitto
      files:
      - mosquitto.conf
      options:
        disableNameSuffixHash: true
        annotations:
          generators.example.com/as-secret: "true"

disableNameSuffixHash is required. kustomize rewrites references to a hashed name
only where it believes a ConfigMap is referenced, and the workload refers to this as
a Secret, so a hashed name would leave that reference dangling. Content changes
therefore do not roll the pod by themselves; rely on reloader.stakater.com/auto.

The move is by path, not by value, so nothing here has to know the content.
*/ -}}
{{- define "generators.secretFromFiles" -}}
- target:
    kind: ConfigMap
    annotationSelector: generators.example.com/as-secret=true
  patch: |
    - op: replace
      path: /kind
      value: Secret
    - op: move
      from: /data
      path: /stringData
{{- end }}
