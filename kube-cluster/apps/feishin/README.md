# feishin

[Feishin](https://github.com/jeffvli/feishin) — a self-hosted web music client —
at **`feishin.example.com`**, ported from a Docker Swarm stack.

- `resources.yaml` — Deployment, Service, Ingress.

Stateless: no volumes, no config file. It's a browser client for an upstream
media server, locked (`SERVER_LOCK`) to the external Jellyfin at
`https://jellyfin-pg.example.com` (`SERVER_*` env) — that address is the *backend*, not
feishin's own host, so it is not feishin's own host.

The pod joins the `traefik` vnet (ingress) to be served. No Authelia — the
`router.middlewares` label was commented out in the Swarm stack, so feishin is
public (matching the original).

`image: ghcr.io/jeffvli/feishin:latest`, digest-pinned — upstream cuts no release tags.
