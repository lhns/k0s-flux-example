# Operations

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
back to `2/2` in about a minute.

**Do not uncordon a node that is still `NotReady`.** Check it has rejoined and its CSI node
plugins are Running first, or pods land on a node that cannot mount their volumes:

```sh
kubectl get node <node>
kubectl get pods -A -o wide | grep -E "<node>.*(nodeplugin|Crash|Error)"
```

On nodes with CephFS volumes, read the
[kernel caveat](../kube-cluster/infra/ceph-csi-cephfs/README.md#kernel-caveat-unmounting-cephfs-can-panic-the-node)
first: a drain unmounts them, and an unmount can panic the node.
