# admission-policies

Cluster-wide `ValidatingAdmissionPolicy` and `MutatingAdmissionPolicy` objects. Nothing namespaced, hence no `namespaces/`.

- **`deny-dollar-in-image-refs`** rejects Pods and the pod templates of Deployments,
  StatefulSets, DaemonSets, ReplicaSets, Jobs and CronJobs when a container, initContainer or
  ephemeralContainer `image`, or an image volume `reference`, contains `$`. That is an
  unsubstituted Flux `${VAR}`: strict substitution only applies once a Kustomization has
  `postBuild` (README, "Substitution caveats"). The dry-run fails, so the old pods keep
  running. On UPDATE only a `$` ref that the old object did not already have is denied, so
  existing objects (old ReplicaSets during a rollout) stay updatable. `failurePolicy: Fail`, so an evaluation error also denies.

- **`probe-timeout-default`** (`probe-timeouts.yaml`, ADR 0057) raises every probe
  `timeoutSeconds` of 1 to 5 on Pod CREATE, in containers and initContainers. Defaulting runs
  before mutating admission, so an unset timeout already reads 1: an explicit 1 is raised too;
  set 2 or more to keep a short timeout. Only Pods are mutated, so Deployments and charts keep
  their own value and Flux sees no drift; the effective value is on the Pod
  (`kubectl get pod -o yaml`). Existing Pods change on their next restart. `failurePolicy:
  Ignore`, so a CEL error admits the Pod unchanged.
