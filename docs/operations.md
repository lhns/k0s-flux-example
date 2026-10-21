# Operations

## Checking a deploy landed

`kubectl rollout status` answers the wrong question. It reports on the Deployment that is
in the cluster, and a Kustomization that never applied leaves the old one in place and
healthy — so a reconcile failing on a decryption error, a build error or a dependency
reports "successfully rolled out". The same holds for a change that does not touch the pod
template: nothing rolls, and there is nothing for it to wait on.

The check is the Kustomization's own `Ready` condition:

    flux get kustomizations -A
    kubectl -n <namespace> get kustomization <name> \
      -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}{" "}{.status.conditions[?(@.type=="Ready")].message}{"\n"}'

`<namespace>` is `gen-<name>`. `Ready=True` with the pushed revision in the message is the
only "deployed". Anything else
means git and the cluster have diverged, however healthy the pods look.

`scripts/check-sops-mac.sh` is the pre-push counterpart for the decryption case, which is
the one that cannot be seen locally.

With more than one cluster, all of this is per cluster: its own kubeconfig, and
`scripts/check-sops-mac.sh <cluster>` on a host holding that cluster's key.

## Node maintenance (reboot, shutdown)

`cordon` is **not** enough. It only stops *new* pods landing on the node and evicts nothing.
Use `drain`, which cordons *and* evicts:

```sh
kubectl drain <node> --ignore-daemonsets --delete-emptydir-data
# ... reboot / shut down ...
kubectl uncordon <node>
```

`--ignore-daemonsets` is required because DaemonSet pods are recreated on the same node and can
never be drained. `--delete-emptydir-data` is you accepting the loss of node-local `emptyDir`
scratch; without it the drain aborts on the first such pod and does nothing.

**Draining the node with the CNPG primary needs two eviction attempts, and that is normal.** The
first returns `429 TooManyRequests`, because the `postgres-primary` PDB is `minAvailable: 1` on
`cnpg.io/instanceRole=primary`. CNPG reacts by switching over and demoting the old primary in
place; its role label leaves the selector, and the second eviction is accepted. Expect Postgres
back to `3/3` in about a minute.

**Do not uncordon a node that is still `NotReady`.** Check it has rejoined and its CSI node
plugins are Running first, or pods land on a node that cannot mount their volumes:

```sh
kubectl get node <node>
kubectl get pods -A -o wide | grep -E "<node>.*(nodeplugin|Crash|Error)"
```

On nodes with CephFS volumes, read the
[kernel caveat](../kube-cluster/infra/ceph-csi-cephfs/README.md#kernel-caveat-unmounting-cephfs-can-panic-the-node)
first: a drain unmounts them, and an unmount can panic the node.

## A node is lost: StatefulSet pods stay Terminating

A StatefulSet replaces a pod only once the old one is confirmed gone, so after its node goes
`NotReady` the pod stays `Terminating` until the node returns. That is deliberate: an
unreachable node may still run the pod and write to its RBD volume. Once the node is **known
to be off** (powered down, not just unreachable):

```sh
kubectl taint node <node> node.kubernetes.io/out-of-service=nodeshutdown:NoExecute
# its pods are force-deleted and their volumes detached; they start on another node.
# When the node is back and Ready:
kubectl taint node <node> node.kubernetes.io/out-of-service=nodeshutdown:NoExecute-
```

Never taint, or `kubectl delete pod --force`, while the node might still be running the pod:
that is two writers on one volume.

## Removing a component

Flux never deletes what a component's `namespaces/` holds (`kustomize.toolkit.fluxcd.io/prune:
disabled`, set by the generator). Removing the component directory deletes its workloads and
PVCs but leaves the Namespace. Delete it deliberately once nothing in it is wanted:

```sh
kubectl delete namespace <ns>
```

Namespaces whose owning Kustomization no longer exists:

```sh
kubectl get ns -o json | jq -r '.items[] | .metadata | select(.labels["kustomize.toolkit.fluxcd.io/name"])
  | "\(.labels["kustomize.toolkit.fluxcd.io/namespace"]) \(.labels["kustomize.toolkit.fluxcd.io/name"]) \(.name)"' \
  | while read -r ns ks name; do kubectl -n "$ns" get kustomization "$ks" >/dev/null 2>&1 || echo "$name"; done
```
