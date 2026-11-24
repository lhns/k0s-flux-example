# 0067 — Workers use the systemd cgroup driver, with a writable-cgroup runtime for systemd pods

- Status: Accepted
- Date: 2026-10-28, amended 2026-10-28
- Current answer: containerd's default runtime on every worker uses the systemd cgroup driver, and
  the kubelet follows it through the CRI. A second handler, `runc-cgrw`, adds `cgroup_writable`;
  the RuntimeClass `webtop-systemd` selects it for pods that run systemd as PID 1. Every other pod
  keeps a read-only cgroup.

## Context

A pod that runs systemd as PID 1 in a user namespace needs a cgroup of its own that it can write.
runc delegates a cgroup and chowns it to the container's root user only with the systemd driver;
containerd's `cgroup_writable` changes only the mount mode, not the ownership. With the cgroupfs
driver the cgroup is root-owned and unusable to a user-namespaced root.

## Decision

- **`SystemdCgroup = true`** on the default `runc` runtime and on `runc-cgrw`, in
  `k0s-files/runc-cgroups.toml`, uploaded by k0sctl like `registries.toml`
  ([0032](0032-files-come-from-images-never-fetched.md)). The kubelet reads the driver from the
  CRI runtime, so the workerProfile sets no `cgroupDriver`.
- **`runc-cgrw`** sets `cgroup_writable = true`; the default runtime does not, so the cgroup stays
  read-only for everything else.
- **A RuntimeClass** selecting `runc-cgrw` lives with the workload that needs it, like
  `RuntimeClass nvidia` with its device plugin; the first is `webtop-systemd` in `apps/webtop`.

## Consequences

- Changing the driver needs a drain and a reboot per worker, one at a time. Uploading the file
  restarts nothing, and a drain alone leaves DaemonSet pods in their old cgroups.
- A worker with the file switches at its next k0s restart, kubelet and containerd together; a plain
  restart without drain and reboot leaves running pods in cgroups of the old driver.
- Amendment (2026-10-28): uploading the file does restart k0s. `k0sctl apply` compares size and
  mtime of each uploaded file, and a difference drains and restarts that node. This file is still
  rolled out per node with drain + reboot, not by `k0sctl apply` alone.
- A pod's own limits still apply to the cgroup subtree it is delegated.
- A pod with `runtimeClassName: webtop-systemd` cannot start on a worker that lacks the handler.
