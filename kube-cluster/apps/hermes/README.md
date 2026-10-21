# hermes

[Hermes Agent](https://github.com/NousResearch/hermes-agent): the owner's agent in Matrix
(`@hermes:example.com`, end-to-end encrypted). Paperclip handles delegated work. The only IngressRoute
is the dashboard (`hermes.example.com`); the API server (`hermes:8642`, ClusterIP) has no client today. Paperclip's agent runs on a separate `hermes-worker` instance ("Worker").

## Decisions

- **Front door, not project office.** Hermes is the one agent the owner talks to (Matrix, E2EE,
  one allowed user). Delegated multi-step work, budgets and human-only approvals stay in
  Paperclip (`apps/paperclip`).
- **No cluster API access.** No ServiceAccount token; `kubectl`, `flux`, `sops -d` and force-push
  are on a deny list that holds even with approvals off; egress is the internet only
  (`networkpolicy.yaml`). Cluster changes reach the repo as PRs the owner reviews.
- **Subscription only through the official binary.** The Max subscription is used solely by the
  `claude` CLI logged in via Anthropic's own flow (allowed when a platform hosts Claude Code).
  Hermes' own model calls use the API key; `auth.adopt_external_logins: false`, its provider
  OAuth is never used.
- **Claude direct.** The native Anthropic API keeps caching, thinking and betas; other models go
  through `apps/litellm`.
- **Knowledge is the Paperclip wiki**, mounted read-only (static PV on its subvolume). Moving to
  another agent later is cheap: knowledge in the wiki, tasks in Paperclip, code in git.
- The Claude login lives on `hermes-data`; losing that volume means logging in again.

## Layout

| Path | Where it lands |
|---|---|
| `managed/config.yaml` | ConfigMap `hermes-managed` at `/etc/hermes-managed` (`HERMES_MANAGED_DIR`). Pinned for every profile: provider, `auth.adopt_external_logins: false`, backend, `approvals.deny`. |
| `profiles/owner/` | ConfigMap, `config.yaml` and `SOUL.md` copied by the `seed-config` init container to `/opt/data` on every start. |
| `profiles/worker/config.yaml` | The worker's root config, seeded the same way. |
| `resources.yaml` | Owner Deployment, Service, `hermes-data` (RBD 10Gi, `/opt/data`), wiki PV/PVC (`/wiki`), the Paperclip image volume for the task-bridge skill. |
| `worker.yaml` | Worker Deployment, Service, `hermes-worker-data` (RBD 5Gi). |
| `vnet.yaml`, `networkpolicy.yaml` | Paperclip may call the worker's :8642 (pod label `kube-vnet/net.hermes`). Owner egress: internet, Traefik's VIP and Paperclip :3100 (`kube-vnet/net.paperclip.paperclip`). Worker egress: internet and Paperclip :3100. |
| `secret-*.yaml` | One Secret per key; see "Credentials". |

Config precedence: managed scope overlays the profile file, which overlays Hermes' defaults. Only
keys read through `load_config` work in managed scope; `approvals.mode` and the model stay in the
profile so a second profile can differ.

### The owner profile is the root profile

`hermes gateway run` serves the root `/opt/data` as the default profile, and secrets arrive as
container env, which only that launch profile reads (a named profile reads its own `.env`). So the
owner profile is the root profile: `profiles/owner/` is copied to `/opt/data/config.yaml`, and
`hermes` without `-p` is the owner. Config is copied, not mounted, because Hermes rewrites
`config.yaml` (hook migration, `hermes config set`) and a ConfigMap mount, subPath included, is
read-only. A runtime change is lost on the next start; make it in git.

Not honoured / not set: `MATRIX_HOME_ROOM` and `MATRIX_ALLOWED_ROOMS` are unset on purpose (DMs
are exempt from the room allowlist). Set them once a home room exists, for cron delivery and to
restrict group rooms.

### Add a profile or an instance

- Profile: `profiles/<name>/config.yaml`, a generator entry, another copy in the init container to
  `/opt/data/profiles/<name>/config.yaml`, and its own `.env` secrets (a named profile does not read
  the container env). Served at `/p/<name>/v1/...` with its own `API_SERVER_KEY`.
- Instance (other identity or trust level): a second Deployment file in this app, like
  `apps/github-runner`.

## Credentials

Every key is a SOPS Secret here, created with `scripts/secret-from-clipboard.py` (never shown) or
piped into sops. The file must exist before it is listed in `kustomization.yaml`.

| File / Secret (`KEY`) | What it is | Used by | Created from / rotate |
|---|---|---|---|
| `secret-anthropic.yaml` / `hermes-anthropic` (`ANTHROPIC_API_KEY`) | Anthropic API key (Console, billed per token) | owner + worker, Hermes' own model calls | owner's Console key via the clipboard script |
| `secret-matrix.yaml` / `hermes-matrix` (`MATRIX_ACCESS_TOKEN`) | MAS compatibility token of `@hermes:example.com`, device `HERMESBOT` | owner | "Rotate the Matrix token" |
| `secret-matrix-recovery.yaml` / `hermes-matrix-recovery` (`MATRIX_RECOVERY_KEY`) | cross-signing recovery key of the bot | owner | "Recovery key" |
| `secret-owner-api.yaml` / `hermes-owner-api` (`API_SERVER_KEY`) | owner's API server key (no client uses it today) | owner | generated, piped into sops |
| `secret-worker-api.yaml` / `hermes-worker-api` (`API_SERVER_KEY`) | shared secret Paperclip → worker | worker; the **same value** is the `apiKey` of Paperclip agent "Hermes" | owner generates it; change both together |
| `secret-worker-paperclip.yaml` / `hermes-worker-paperclip` (`PAPERCLIP_API_KEY`) | standard key of Paperclip agent "Hermes": runs post comments and set status | worker | `POST /api/agents/<hermes>/keys` (or the agent's API Keys panel) |
| `secret-openai-voice.yaml` / `hermes-openai-voice` (`VOICE_TOOLS_OPENAI_KEY`) | OpenAI key for Whisper: transcribes voice messages (`stt` in the owner config) | owner only | owner's OpenAI key via the clipboard script |
| `secret-paperclip-bridge.yaml` / `hermes-paperclip-bridge` (`PAPERCLIP_BRIDGE_API_KEY`) | `task_bridge` key of agent "Hermes", limited to parent DEMO-2 "From Hermes", assignee CEO only | owner, task-bridge skill | `POST /api/agents/<hermes>/keys` with `scope.kind: task_bridge` ("Task bridge") |

Not in git: the claude CLI's subscription login (`/opt/data/claude`, "claude-code skill"). Paperclip
agent "Hermes" points at `http://127.0.0.1:8642`, its `hermes-forward` sidecar (`apps/paperclip`).

## Matrix

- Only `MATRIX_ALLOWED_USERS` (the owner) may talk to it; other senders are ignored. Bridge puppets
  and bridge bots are ignored by `MATRIX_IGNORE_USER_PATTERNS` (built from
  `apps/matrix/registrations`; add a prefix there when a bridge is added).
- In a group room it answers only when mentioned (`MATRIX_REQUIRE_MENTION`); DMs always.
- **Approvals** (`approvals.mode: smart`): a regex flags risky commands; a Haiku call approves,
  denies or escalates; escalations arrive as a card with reactions: ✅ once, 🌀 this session, ♾️
  always, ❌ deny. Unanswered cards deny after 300 s. `approvals.deny` (`kubectl`, `flux`, forced
  push, `sops -d`) blocks regardless of mode. ♾️ writes `command_allowlist` to `config.yaml`,
  which the next start resets.

### Recovery key

`secret-matrix-recovery.yaml` holds the cross-signing recovery key (`MATRIX_RECOVERY_KEY`); every
start verifies device `HERMESBOT` with it. It came from one bootstrap run with
`MATRIX_RECOVERY_KEY_OUTPUT_FILE`, piped from the pod into sops. A lost crypto store needs a new
device (new compat token) but keeps this key.

Verify the device in Element once: `HERMESBOT`.

### Rotate the Matrix token

In the `mas` pod, piped straight into sops, never displayed:
`mas-cli manage issue-compatibility-token hermes HERMESBOT` (`MAS_CONFIG` supplies the config).

## Wiki

`/wiki` (`OBSIDIAN_VAULT_PATH`) is the Paperclip LLM Wiki, read-only at the source: static PV
`hermes-wiki` on the `paperclip-wiki` subvolume path, `csi.readOnly: true`. Hermes cannot write it;
new knowledge goes in as a Paperclip ticket (see "Wiki and tickets"). Deleting this PV is safe (static, ADR 0020);
never delete `paperclip-wiki`'s dynamic PV. If the subvolume is ever recreated, update `rootPath`.

## Paperclip wiring

### Worker

Paperclip's "Hermes" agent runs on `hermes-worker`, a second Deployment, not as a profile of the
owner pod. Profiles share one `/opt/data`, so a prompt injected through a ticket could read the
owner's chats, memory, Matrix crypto store and claude login. The worker has none of that: its own
volume (`hermes-worker-data`), no Matrix variables, no claude CLI, no dashboard, egress to the
internet and the Paperclip API only, and the read-only `/wiki`. Paperclip expects the agent to post
its comment and set the issue status itself (the adapter passes no credentials), so the worker holds
agent Hermes's standard key (`secret-worker-paperclip.yaml`, `PAPERCLIP_API_KEY`) and
`PAPERCLIP_API_URL`; without them a run finishes the work but leaves the issue blocked. Its API server (`hermes-worker:8642`,
only Paperclip may reach it) uses its own `API_SERVER_KEY`; without the Secret the server refuses
to start and the pod stays down.

`approvals.mode: "off"` plus the managed `approvals.deny` list. Nobody can answer an approval card
for a Paperclip run, and `smart` escalated the run's own `curl` to the Paperclip API, parking runs
until they timed out. The boundary is isolation: what the pod cannot reach, a hijacked run cannot
read or send. Accepted risk: it holds the Anthropic API key (stripped from its shell) and agent
Hermes's Paperclip key, and has internet egress, so a prompt injection could send that key out;
locking egress to the Anthropic API and Paperclip is the next step if that matters.

Owner steps:

1. Generate a key into the clipboard (`openssl rand -hex 32 | clip`, or `pbcopy`/`xclip`) and store it:
   `python3 scripts/secret-from-clipboard.py kube-cluster/apps/hermes/secret-worker-api.yaml hermes-worker-api hermes API_SERVER_KEY`.
   Add `secret-worker-api.yaml` to `resources:` in `kustomization.yaml` (a listed file must exist,
   so only now) and merge. Keep the key in the clipboard for step 2.
2. In Paperclip create the agent "Hermes": adapter `hermes_gateway`, `apiBaseUrl`
   `http://127.0.0.1:8642` (Paperclip's `hermes-forward` sidecar; the adapter refuses plain HTTP to
   any non-loopback host and its UI has no override), `apiKey` the key, `sessionKeyStrategy: issue`. Use the Test button (`/health`).
3. Create a standard API key for agent "Hermes" (agent page, API Keys; or in the browser console
   `POST /api/agents/<hermes-id>/keys` with `{"name":"hermes-worker"}`), copy it, store it with
   `python3 scripts/secret-from-clipboard.py kube-cluster/apps/hermes/secret-worker-paperclip.yaml hermes-worker-paperclip hermes PAPERCLIP_API_KEY`,
   list the file in `kustomization.yaml` and merge. Without it runs do the work but cannot post the
   result or set the status, and Paperclip parks the issue as blocked.

### Task bridge (owner to Paperclip tickets)

The owner instance creates, comments on and lists tickets with the `paperclip-task-bridge` skill,
using `PAPERCLIP_API_URL=http://paperclip.paperclip.svc:3100/api` and `PAPERCLIP_BRIDGE_API_KEY`
(Secret `hermes-paperclip-bridge`, optional). The skill is not vendored: `artifacts/task-bridge.yaml` (oci-composer `ImageComposition`) copies
`/app/packages/adapters/hermes/skills/paperclip-task-bridge` out of the pinned Paperclip image (the same
reference as `apps/paperclip`; Renovate bumps both) into a few-KB image, mounted as `task-bridge` at
`/etc/hermes-skills/paperclip-task-bridge`. A node move therefore pulls kilobytes, not the 2.6 GB server
image, which took longer to unpack than containerd's CreateContainer timeout. `skills.external_dirs:
[/etc/hermes-skills]` in the owner config makes Hermes find it, so the skill matches the server version.
`hermes skills list` should show it; if the path moves in a release, fix `subpath` in the composition.

The key is a `task_bridge` key: it creates tickets only under one parent issue and assigns them only
to the allowed agents. Scope it to a parent issue "From Hermes" and `allowedAssigneeAgentIds` = the
lead agent, so nothing Hermes creates lands back on Hermes. The skill defaults to assigning to the
authenticated agent, so ask Hermes to pass `--assignee-agent-id <lead>`.

1. In Paperclip create the parent issue "From Hermes"; note its id, the Hermes agent id and the
   lead agent id (the key hangs off any agent; use the Hermes agent).
2. In the browser console on the Paperclip board (signed in as the board user) run:
   ```js
   await (await fetch(`/api/agents/<hermesAgentId>/keys`, {method: "POST",
     headers: {"Content-Type": "application/json"},
     body: JSON.stringify({name: "Hermes task bridge",
       scope: {kind: "task_bridge", parentIssueId: "<id>", allowedAssigneeAgentIds: ["<lead>"]}})})).json()
   ```
   The token appears once in the response. Copy only the token, then
   `python3 scripts/secret-from-clipboard.py kube-cluster/apps/hermes/secret-paperclip-bridge.yaml hermes-paperclip-bridge hermes PAPERCLIP_BRIDGE_API_KEY`,
   add the file to `kustomization.yaml`, merge. The pod restarts.
3. From Matrix: "list my Paperclip tasks", then "create a ticket for the lead agent: ...".

### Wiki and tickets

`/wiki` is read-only in both pods and the owner's `SOUL.md` says so: new knowledge goes in as a
ticket for the wiki maintainer. There is no write inbox. The LLM Wiki plugin lists and reads `.md`
files in any `raw/` subdirectory as sources, but ingests per source only (its `ingest-source`
action and the maintainer's `wiki-ingest` skill); nothing sweeps files that appear on disk. A dropped
file would be visible but not distilled. Revisit if the plugin gains a sweep.

### Approvals poll

Not possible with the bridge key: `GET /companies/:id/approvals` needs the `company_scope:read`
action, which `task_bridge` keys are denied. A cron listing pending approvals would need a board or
full agent key in the pod. Approvals stay in the Paperclip UI. Not built.

## Dashboard

`https://hermes.example.com`: sessions and transcripts, a Chat tab (the TUI over WebSocket), cron,
config, skills and logs. A second container, `dashboard`, in the same pod runs
`hermes dashboard` on :9119 against the shared `/opt/data` (same state.db; the pod shares its PID
namespace, which the dashboard needs to see the gateway).

Login is two layers, because a non-loopback bind forces Hermes' own login and Hermes has no
trusted-header mode:

1. Anubis, then Authelia forward-auth (`routing.yaml`; admin group, two-factor via the
   `*.example.com` rule).
2. Hermes redirects to its own OIDC login against Authelia (public client `hermes-dashboard`, PKCE
   S256, callback `https://hermes.example.com/auth/callback`, admin group only). With the Authelia
   session from step 1 this is a redirect round trip with no prompt.

Settings: `HERMES_DASHBOARD_PUBLIC_URL` and `HERMES_DASHBOARD_OIDC_{ISSUER,CLIENT_ID,SCOPES}` on the
container; `dashboard.trusted_proxies` in the owner profile (Traefik's pod network). The container
resolves `auth.example.com` to Traefik's VIP via `hostAliases`, like `matrix.example.com`. Access tokens are
short-lived; expiry just repeats step 2.

Chat-tab sessions run without `/wiki` and the claude-code skill (only `/opt/data` and the API key are
mounted). Not supported: Hermes Desktop's Remote Gateway, which cannot pass forward-auth.

## claude-code skill

Short coding tasks from Matrix run `claude -p` with `--allowedTools` and `--max-turns`. Long work
becomes a Paperclip ticket. The CLI is built from npm by the `claude-cli` `ImageBuild` (`artifacts/claude-cli.yaml`,
oci-composer) and mounted read-only at `/opt/claude-cli`, first on `PATH`. Renovate bumps the
pinned version; the CLI's own updater is off. Nothing is installed at pod start.

The only manual step is the one-time login, stored in `CLAUDE_CONFIG_DIR=/opt/data/claude`:

```
ssh -t admin@10.20.150.84 kubectl -n hermes exec -it deploy/hermes -c hermes -- claude auth login
```

**Subscription rule**: the Max login is used only by the official `claude` binary, signed in
through Anthropic's own flow. Never `hermes auth add anthropic` with it, never set
`CLAUDE_CODE_OAUTH_TOKEN` or `ANTHROPIC_TOKEN` for Hermes' own calls (those use the API key), and
keep `auth.adopt_external_logins: false`. Git push credentials are not provided: the skill works
locally; changes reach repos by PR.

## Continue a Matrix conversation in a terminal

```
kubectl exec -it deploy/hermes -n hermes -- hermes sessions list
kubectl exec -it deploy/hermes -n hermes -- hermes --resume <id>
```

## Upgrading

The image normally runs under s6 as root. Here it runs without it (non-root, `shareProcessNamespace`
makes the entrypoint skip s6, `HERMES_GATEWAY_NO_SUPERVISE=1`), so **root-only migrations in the
image's init are skipped**: read the release notes before bumping the tag. On every bump also:

- re-read `/etc/hermes/agent-browser-executable-path` in the image and update
  `AGENT_BROWSER_EXECUTABLE_PATH` (the Chromium revision is in the path);
- check `kubectl logs` for `[stage2]` warnings.
