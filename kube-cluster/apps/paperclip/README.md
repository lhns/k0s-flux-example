# paperclip

[Paperclip](https://github.com/paperclipai/paperclip), an orchestrator for teams of AI agents,
at **`paperclip.example.com`**. One pod from the upstream image; the agent CLIs (`claude`, `codex`,
`opencode`, `gemini`) run inside it as child processes of the server.

## Access

Two logins in a row: Anubis, then Authelia forward-auth (the `*.example.com` catch-all rule:
`group:admin`, two-factor, same as `pve.example.com`, so there is no rule of its own), then
Paperclip's own email/password login (better-auth).

Paperclip has no OIDC/OAuth sign-in and no generic trusted-header login, so Authelia cannot
log you in to it. Its trusted-header path is for Paperclip Cloud only and needs a tenant token.

Runs `authenticated` + `private`. `private` is what allows the browser first-admin claim;
Authelia is what keeps the host off the open internet.

### First admin

1. Open the site, pass Authelia, sign up with email and password.
2. The access screen offers to claim the instance: claim it. The first claim wins, and only
   people Authelia lets through can get there.
3. Sign-up is then disabled (`PAPERCLIP_AUTH_DISABLE_SIGN_UP`). It blocks invited users too, so
   unset it briefly to let another person create an account.

## Shape

- **DB:** role and database `paperclip` in the shared CNPG cluster, via `postgres-rw` (the
  pooler would need `DATABASE_PREPARED_STATEMENTS=false`). Migrations run on boot.
- **Storage:** `/paperclip` on `ceph-rbd`. Agent workspaces with git checkouts and
  `node_modules` live there, so it grows; RBD can be expanded.
- **Wiki:** `/paperclip/wiki` is its own CephFS volume (`paperclip-wiki`, RWX), the folder to
  give the LLM Wiki plugin. Paperclip writes `wiki/`; Hermes mounts it read-only and asks for new
  knowledge through tickets, because writes from outside bypass the plugin's index (`apps/hermes`, "Wiki").
- **Hermes:** the owner instance may call the API in-cluster (task bridge; `vnet.yaml`,
  `PAPERCLIP_ALLOWED_HOSTNAMES`). Paperclip runs agents on the `hermes-worker` instance through the
  `hermes-forward` sidecar at `http://127.0.0.1:8642` (the adapter allows plain HTTP only to
  loopback). See `apps/hermes`.
- **Egress** (`networkpolicy.yaml`, ADR 0031): DNS, Postgres, the Hermes worker on :8642, and the
  public internet; nothing else private (LAN, pod/service networks, CGNAT, link-local). An agent
  that needs another in-cluster or LAN target needs a rule here first.
- **Singleton:** `Recreate`, so ha-surge does not cover it. A drain takes it down for a restart.
- **Agents call the API on `127.0.0.1:3100`** (`PAPERCLIP_API_URL`). With only
  `PAPERCLIP_PUBLIC_URL` set they would call the public URL and be stopped by forward-auth.
- **Secrets:** `BETTER_AUTH_SECRET` and `PAPERCLIP_SECRETS_MASTER_KEY` in `secret.yaml`. The
  master key encrypts the secrets Paperclip stores in Postgres. It is in git and not left to be
  generated on the PVC, so the PVC and the database can be restored on their own.

## Provider keys

`ANTHROPIC_API_KEY` is read from the optional Secret `paperclip-anthropic`. To add it, copy the
key, then:

```sh
python3 scripts/secret-from-clipboard.py kube-cluster/apps/paperclip/secret-anthropic.yaml paperclip-anthropic paperclip ANTHROPIC_API_KEY
```

Add the file to `kustomization.yaml`; Reloader restarts the pod. Another provider
(`OPENAI_API_KEY`, `GEMINI_API_KEY`) is its own Secret and its own optional `envFrom` entry.
Or store keys in Paperclip's own secret store in the UI.

## Paths forward-auth blocks

None are needed today. These would need a route without the middlewares if one of the features
is used: `/api/chat-webhooks/*`, `/api/voice-webhooks/*`, `/api/plugins/*/webhooks/*`,
`/api/routine-triggers/public/*`, tool OAuth callbacks under `/api/tools/oauth/`, and
`/api/runner/v1/connect/*` for remote runners.
