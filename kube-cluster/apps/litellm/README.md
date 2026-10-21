# litellm

[LiteLLM](https://docs.litellm.ai/) proxy, the in-cluster OpenAI-compatible gateway for
**non-Claude and self-hosted models**. It ships with an empty model list: add providers as they
are wanted.

## Decisions

- **Claude does not go through it.** Tools talk to the Anthropic API directly, because prompt
  caching, extended thinking and beta headers do not survive translation to the OpenAI format.
- **Everything else does**: provider keys are Secrets of the gateway only; a new provider is a
  Secret plus a `model_list` entry, not a change in every client.
- **One virtual key per consumer**, so spend is attributable and a key can be revoked alone; the
  master key is for administration only.
- **In-cluster only** (kube-vnet, no IngressRoute); admin UI by port-forward.
- **Official image pinned by digest**, never pip: the PyPI releases were compromised in March 2026.
- Single replica: an outage affects non-Claude models only.

## Shape

- **Image:** `ghcr.io/berriai/litellm-non_root`, runs as `nobody`, Prisma engines baked in, pinned
  by tag and digest. The PyPI releases were compromised in
  March 2026, so the image is the only supported install; never `pip install litellm`.
- **DB:** role and database `litellm` in the shared CNPG cluster (virtual keys, spend, stored
  models). Migrations run on start.
- **Secrets:** `litellm-master` holds `LITELLM_MASTER_KEY` (admin API key) and `LITELLM_SALT_KEY`.
  The salt key encrypts provider credentials stored in the DB: **never change it** once models are
  added through the UI/API, or they become unreadable.
- **Telemetry:** `LITELLM_TELEMETRY_DISABLED=true` (the veto of the opt-in telemetry in newer
  releases) and `LITELLM_LOCAL_MODEL_COST_MAP=True` (no price map fetched from the internet).
- **Network:** ClusterIP `litellm:4000`, no IngressRoute. Namespaces `hermes`, `paperclip` and
  `matrix` may reach it (`vnet.yaml`); a client pod carries `kube-vnet/net.litellm.litellm: egress`.
- **Memory:** limit 2Gi, below upstream's 4Gi production floor, which assumes prompts stored in
  spend logs (off here). Raise it if the pod is OOMKilled.

## Admin UI

Not exposed. Port-forward and log in as `admin` with the master key:

```sh
kubectl -n litellm port-forward svc/litellm 4000:4000   # then http://localhost:4000/ui
```

## Add a provider

Copy the provider's key, then create a Secret (one per provider, env var name as the key):

```sh
python3 scripts/secret-from-clipboard.py kube-cluster/apps/litellm/secret-<provider>.yaml litellm-<provider> litellm <ENV_NAME>
```

Add `secret-<provider>.yaml` to `kustomization.yaml` and an `envFrom` entry to the Deployment:

```yaml
- secretRef: {name: litellm-<provider>}
```

Then add the model to `model_list` in `config.yaml` with `api_key: os.environ/<ENV_NAME>`; the
file has commented examples for an OpenAI-compatible provider, a self-hosted Ollama/vLLM endpoint
and OpenRouter. A self-hosted endpoint in the cluster needs its own vnet to allow litellm in
(egress from litellm is open).

## Virtual keys, one per consumer

Give each client its own key so spend and access are separable and one can be revoked. Create it
in the UI (Virtual Keys), or with the API through a port-forward:

```sh
curl -s localhost:4000/key/generate -H "Authorization: Bearer $MASTER_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"key_alias": "hermes", "models": ["local-llama"], "max_budget": 10}'
```

Store the returned key as a SOPS Secret in the consumer's namespace with
`scripts/secret-from-clipboard.py`; never use the master key in a client.

## Pointing a consumer at it

OpenAI-compatible: base URL `http://litellm.litellm.svc:4000/v1`, API key = the consumer's
virtual key, model = a `model_name` from `config.yaml`.
