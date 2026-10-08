# github-runner

Self-hosted GitHub Actions runners. **Nothing in this namespace is privileged.**
The ARC scale sets start a fresh pod per job ([below](#arc-scale-sets)); `k0s-flux`,
this repo's Lint and Renovate, has Docker from a workspace of its own, `apps/remote-docker-ci`
([below](#k0s-flux-lint-and-renovate)).

The Docker daemon lives in the workspace, reached over an SSH-in-WebSocket tunnel. The
remote-docker client runs inside the runner container and rewrites every bind mount into an
NFS-backed volume served from the runner's own filesystem, so a `container:` job's workspace and
`/actions-runner/externals` resolve without any path here matching the daemon's filesystem.

## Why the client is in the image, not a sidecar

The client serves **its own** filesystem over NFS. A sidecar is a different filesystem, so
`/actions-runner/externals` — the Node runtime the runner injects into job containers — would be
invisible to it and `container:` jobs would fail to start. `artifacts/arc-runner-k0s-flux.yaml` therefore puts
the binary in the image, with a wrapper entrypoint that starts the session before the runner.

## Why the daemon is privileged, and why that is not fixable here

The workspace pod is privileged. Both ways out were tested on this cluster (k8s 1.36.3,
containerd 2.3.3, kernel 6.12) and **both fail**:

- **User namespaces** (`hostUsers: false`). The userns part works — a probe came up with
  `uid_map: 0 3607822336 65536` — but dockerd never starts:
  `can't create /sys/fs/cgroup/cgroup.subtree_control: Permission denied`. cgroup v2 is not
  delegated to the mapped user; that is a runtime gap, not something a pod spec fixes.
- **Rootless** (`docker:dind-rootless`, `privileged: false`): `rootlesskit` cannot create the
  network namespace without `CAP_SYS_ADMIN`.

They cannot be combined either — rootlesskit must create its own user mappings, which it cannot do
inside a userns. Do not spend another evening on this without new information.

What this arrangement *does* buy is that the privilege is in **one** pod for the whole cluster
rather than a sidecar per CI stack, and that these runner pods have none of it.

## Running the same workflow here and on GitHub

`runs-on: ubuntu-latest` is a **VM image** (`actions/runner-images`) with tens of GB of
preinstalled toolchain; the runner images here carry only a subset ([Runner images](#runner-images)).
Do not widen them to match: a half-match passes here and fails on a hosted runner, and the reason
lives in this cluster instead of the repo. Put the toolchain in the workflow, or in a `container:`
image, which resolves identically on both.

Two things still differ, and no image choice fixes them:

- **`nproc` and `free` report the node**, not the pod -- `/proc` is not namespaced. `make -j$(nproc)`
  launches 20 compilers against the pod's memory limit and gets OOMKilled. Pin `-j` explicitly.
- **Resource ceilings differ by where the job runs.** Directly in the runner pod it is that pod's
  `limits.memory`; under `container:` it is the workspace pod, which sets no limit at all.

## Security

The perimeter is **who can trigger a workflow**. A job runs arbitrary code, and it runs on the
shared workspace daemon under this runner's account.

- **Do not register a runner on a public repository.** A fork PR would run attacker-controlled
  code.
- Prefer fine-grained PATs scoped to the specific repositories over classic `repo`-scope tokens.
- ADR 0019 is explicit that per-account daemons are **separation, not isolation** — every one is
  still privileged, so a determined account can break out and reach another. It prevents the
  failure that actually happens (`docker ps` showing someone else's work), not a targeted escape.
- `networkpolicy.yaml` governs the **runner agent**, not job containers. Those run on the
  workspace daemon and inherit *that* pod's network reach.

## Secret

`github-runner-k0s-flux-ssh` (`secret-k0s-flux-ssh.yaml`): `id_ed25519-0`, the one remote-docker
key every `k0s-flux` pod uses (`id_ed25519-1` is unused); the public half is in
`apps/remote-docker-ci/release.yaml`.

`arc-k0s-flux` (`secret-arc-k0s-flux.yaml`): `github_token`, the key ARC reads for `k0s-flux`: a
fine-grained PAT for `example/k0s-flux` only, with **Administration: read and write**, what minting a
runner registration token requires. A GitHub App would need three keys in one Secret, which
`secret-from-clipboard.py` cannot write. Created from the repo root:

```sh
python3 scripts/secret-from-clipboard.py kube-cluster/apps/github-runner/secret-arc-k0s-flux.yaml arc-k0s-flux github-runner github_token
```

## Verifying

```sh
# the daemon is reachable, and it is the workspace's (from a job of the k0s-flux scale set)
docker info | head

# the tunnel and the NFS rewrite, in one command
docker run --rm -v /tmp:/w alpine:3 ls /w
```

A workflow with `container:` **and** `services:` is the case worth testing first.

## ARC scale sets

`scale-sets.yaml` holds the runner scale sets of
[actions-runner-controller](../arc-controller/README.md) `k0s-flux` (Lint and Renovate). Each is a HelmRelease of the `gha-runner-scale-set` chart.

- **A pod per job.** The listener in `arc-system` long-polls GitHub for the scale set's jobs. The
  controller then creates one runner pod per job, with a just-in-time registration, and deletes
  the pod when the job ends. Nothing a job installs or leaves in `/tmp` reaches the next one.
- **Scaling.** `minRunners` pods wait idle; the scale set adds one per queued job up to
  `maxRunners`. A pod that does not fit on a node stays Pending and its job waits.

  | scale set | min | max | request | limit |
  | --- | --- | --- | --- | --- |
  | `k0s-flux` | 1 | 4 | 500m, 1Gi | 4Gi |

- **`runs-on`** is the scale set's name, alone: `runs-on: k0s-flux`. GitHub
  documents only that form (or `scaleSetLabels`), so do not add `self-hosted`.
- **Security.** The pods run as root but unprivileged,
  `allowPrivilegeEscalation: false`, RuntimeDefault seccomp, the `github-runner` ServiceAccount
  without a token, no hostPath. `github-runner-egress` confines a job to DNS and the public
  internet; only `k0s-flux` has a vnet label, for its Docker workspace. The job holds only its
  single-use registration; the PAT stays
  with the controller and the listener. Root is what the workflows expect (`apt-get`); the
  mounted `arc-runner-sudoers` lets root `sudo` too.
- **Images.** `k0s-flux` runs the base plus the remote-docker client:
  [Runner images](#runner-images).
- **Removing one.** The chart's objects carry ARC finalizers, so remove a scale set while the
  controller is still running, never together with it.

### k0s-flux: Lint and Renovate

`example/k0s-flux`. Lint and Renovate are both `runs-on: k0s-flux`. Lint does not need Docker but
accepts depending on `remote-docker-ci` ([ADR 0050](../../../docs/adr/0050-arc-scale-sets-for-per-job-runners.md)):
if it is down, dispatch with `runner: github-hosted`. The warm pod holds one slot.

- **Docker through ephemeral clients.** Renovate's action runs containers. Every pod of the
  scale set starts a remote-docker client in its runner container (the client serves the pod's
  own filesystem over NFS, so not a sidecar), against `apps/remote-docker-ci`. remote-docker
  identifies a client by its key, but that account is **ephemeral**
  ([ADR 0050](https://github.com/lhns/remote-docker/blob/main/docs/adr/0050-ephemeral-clients.md)):
  all pods present the one key and each client process still gets its own tunnel port, exports and
  volumes, removed 2 minutes after it ends, with its containers. The daemon, images and cache stay
  shared per replica. Clients older than 0.9.0 are refused.
- **The image** is `artifacts/arc-runner-k0s-flux.yaml`: the base plus the client at
  `/usr/local/bin/remote-docker`, also `docker`. The container's command is
  `with-remote-docker /home/runner/run.sh`: the wrapper installs the key from the mounted Secret
  (`github-runner-k0s-flux-ssh`, `id_ed25519-0` at `/secrets/id_ed25519`) into
  `REMOTE_DOCKER_STATE_DIR` as 0600, starts the session, then execs the runner. No `remote create`: the `REMOTE_DOCKER_*` env alone
  configures the client.
  `REMOTE_DOCKER_ENDPOINT` and `DOCKER_HOST` point at the socket in that emptyDir, so a runner
  with no home or an arbitrary uid still finds it.
- **Two instances.** The Service pins each runner pod to one of `remote-docker-ci`'s two replicas;
  see [its README](../remote-docker-ci/README.md). Each pod holds one of a replica's slots.
- **Egress.** Pods carry `kube-vnet/net.remote-docker-ci.remote-docker-ci: egress`, so they reach
  `:2280` there and nothing else private. Job containers are confined by that workspace's own
  policy.
- **No tools in the image** beyond the base and the client; the workflows install what they use,
  identically on hosted runners.
- **Dispatching** Lint or Renovate with `runner: github-hosted` runs it on `ubuntu-latest` instead,
  for when the cluster is down; that costs Actions minutes.
- **Image cache.** The workspace's graph persists across runs. `renovate.yml` pins Renovate to an
  exact version and drops superseded tags first. Renovate's own repository cache
  (`RENOVATE_REPOSITORY_CACHE`) does not survive a pod, so each run starts cold on it.
- **Residual risk.** The pod is unprivileged, but `DOCKER_HOST` reaches a privileged daemon, so a
  job can break out to the workspace's node. Inside its own pod, a root job can read the
  remote-docker key. The perimeter is who can push to `example/k0s-flux`.

### Runner images

`artifacts/arc-runner-base.yaml` is the `arc-runner-base` ImageBuild, the base. `arc-runner-k0s-flux` builds a second stage of the same Dockerfile
(`spec.target`): the base plus the remote-docker client, infrastructure for the Docker-backed
`k0s-flux`, not a tool layer. oci-composer cannot layer one ImageBuild on another: a
`FROM` must be a digest in git, while a build's digest is only exported at runtime (`writeRefTo`),
and the `artifacts/` Kustomization has no substitution. It also refuses a Dockerfile from a
ConfigMap (`--require-pinned-sources`). So both ImageBuilds carry the same inline Dockerfile,
stages of one recipe, and `scripts/check-arc-dockerfile.py` (validate.sh) fails when they differ:
edit one, copy it to the other.

- **The rule: the base holds only the tools `ubuntu-latest` also preinstalls**
  ([toolset-2404.json](https://github.com/actions/runner-images/blob/main/images/ubuntu/toolsets/toolset-2404.json)),
  so a workflow runs unchanged on both and installs only what ubuntu-latest lacks. Infrastructure
  the hosted runner has no equivalent of (remote-docker) goes in the derived stage, never the base.
- **In the base:** git, git-lfs, curl, wget, jq, zip/unzip, bzip2, build-essential, python3 with pip,
  venv and pipx, yamllint, php 8.3, Node 22, `gh`, yq, kubectl, kustomize, helm and the docker CLI
  with buildx and compose (no daemon). Versions may be newer than ubuntu-latest's, and Renovate keeps
  them current; the `sudo` and `apt-get` behaviour comes from running as root with `arc-runner-sudoers`.
  Not included: tools ubuntu-latest lacks; a workflow installs those itself.
- **Adding a tool:** only if ubuntu-latest has it. Add a download under a
  `# renovate:` comment with its `url=` and `digest=sha256:` lines (copy one in the Dockerfile, in
  both files);
  `check-fetch-digests.py --fix` rewrites the digest. Apt packages are not version-pinned; the
  digest of the `FROM` is what freezes them until the next rebuild.

### Adding a scale set

1. A Secret with key `github_token`, as in [Secret](#secret).
2. A HelmRelease in `scale-sets.yaml`, copied from `arc-k0s-flux`, with its own
   `runnerScaleSetName`, `githubConfigUrl` and `githubConfigSecret`.
3. `runs-on: <runnerScaleSetName>` in the repository's workflows.

A scale set in another namespace needs a second controller: `arc-controller` watches only this one.
