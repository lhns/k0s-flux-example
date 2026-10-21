# zensical

`zensical/zensical` — a static site generator (Material-for-MkDocs successor) serving the blog at
**blog.example.com**. `zensical.example.com`, the apex `example.com`, and
`blog.kube.example.com` all redirect to the canonical host.

## Shape (source persistent, build ephemeral)
Split by lifecycle:
- **Config** — `zensical.toml` lives in this repo (git) and is mounted as a hashed **ConfigMap** at
  `/docs/zensical.toml` (edits roll the pod).
- **Source** — the markdown `docs/` + theme `overrides/` live in a small **CephFS RWX**
  PVC (`zensical-source`) and mounted **read-only** at `/docs/docs` and `/docs/overrides`.
- **Build/cache** — `/docs` itself is an **emptyDir** (ephemeral, per pod). zensical builds `site/` +
  `.cache/` there on every start; they're never persisted (regenerated from the source). This makes
  start-first rollouts trivially safe — each pod builds into its own emptyDir, no shared writer.
- zensical runs as **root**, so it writes the emptyDir fine and reads the read-only source (which is
  world-readable).
- Public, no auth. `kube-vnet`: `net.traefik.traefik: ingress` only (it serves; no outbound deps).

To edit the blog: update the source in the `zensical-source` PVC (or change `zensical.toml` here in
git); the pod picks it up on restart.
