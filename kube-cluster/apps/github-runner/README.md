# github-runner

Self-hosted GitHub Actions runners. **Nothing in this namespace is privileged.**

The Docker daemon lives in `apps/remote-docker`, reached over an SSH-in-WebSocket tunnel. The
remote-docker client runs inside each runner container and rewrites every bind mount into an
NFS-backed volume served from the runner's own filesystem, so a `container:` job's workspace and
`/actions-runner/externals` resolve without any path here matching the daemon's filesystem.

## Shape

One Deployment per runner, one workspace **account** per runner. GitHub has no user-account runner
scope — only repository, organization and enterprise — so a personal account registers per
**repository**, and several repos mean several runners.

With the workspace on `perUserDind: true`, each account gets its own dockerd. The two runners
therefore share neither an image cache nor visibility of each other's containers, which is the
point: one of these repositories is not ours.

## Why the client is in the image, not a sidecar

The client serves **its own** filesystem over NFS. A sidecar is a different filesystem, so
`/actions-runner/externals` — the Node runtime the runner injects into job containers — would be
invisible to it and `container:` jobs would fail to start. `images.yaml` therefore builds
`myoung34/github-runner` with the binary added and a wrapper entrypoint that starts the session
before deferring to the image's own `/entrypoint.sh`.

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
preinstalled toolchain. This runner is the agent on a plain Ubuntu base, so a workflow written
against a hosted runner will find most of that missing. **Do not fix that by installing packages
into `images.yaml`.** Matching that image is not achievable, and a half-match is worse than none:
the workflow passes here and fails on a hosted runner, and the reason lives in this cluster instead
of in the repo.

For a job that must run unchanged in both places, name the environment in the workflow:

```yaml
jobs:
  build:
    runs-on: [self-hosted]   # or ubuntu-latest -- the job is identical either way
    container: gcc:14
```

`container:` resolves to the same image on hosted and self-hosted runners, so the toolchain lives
in the repo rather than in this cluster, and changing it needs no infra change. On hosted runners
it uses their daemon; here it uses the workspace, which is what the bind-mount rewrite above exists
to make work.

Two things that still differ, and no image choice fixes them:

- **`nproc` and `free` report the node**, not the pod -- `/proc` is not namespaced. `make -j$(nproc)`
  launches 20 compilers against the pod's 8Gi limit and gets OOMKilled. Pin `-j` explicitly.
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

## Adding a runner

Copy a Deployment, give it a unique `RUNNER_NAME`, `REMOTE_DOCKER_USER`, `REPO_URL` and secret
keys; add the account's public key to `authorizedKeys` in `apps/remote-docker/release.yaml`, and
its private key to this namespace's secret.

## Secret

`github-runner`, SOPS/age-encrypted, created from the repo root (sops reads `.sops.yaml` from the
CWD). Keys: `ACCESS_TOKEN_A`, `ACCESS_TOKEN_B` (the PATs) and `runner-a-id_ed25519`,
`runner-b-id_ed25519` (the clients' SSH private keys). The matching **public** keys are declared
in plain YAML in the workspace's chart values — they are public, and enrolling should be a
reviewable diff rather than a sops round-trip.

## Verifying

```sh
# the daemon is reachable, and it is the workspace's
kubectl exec -n github-runner deploy/runner-a -- docker info | head

# the tunnel and the NFS rewrite, in one command
kubectl exec -n github-runner deploy/runner-a -- docker run --rm -v /tmp:/w alpine:3 ls /w
```

Each runner should show **online** under its repo's Settings → Actions → Runners. The case worth
testing first is a workflow with `container:` **and** `services:` — that is what the previous
dind-sidecar design could not do, and the reason for this architecture.
