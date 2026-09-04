# kasmvnc

A full Linux desktop in a browser tab (`kasmweb/ubuntu-noble-desktop`, XFCE on Ubuntu, powered by
**KasmVNC**). Public at `kasmvnc.example.com`, gated by **Authelia forwardAuth**;
`kasmvnc.kube.example.com` redirects here.

Deployed alongside [`../webtop`](../webtop/README.md) to compare the two remote-desktop stacks.
**One of the two gets deleted afterwards.**

## Read this first: standalone kasm images are deliberately degraded

Kasm's own `workspaces-images` README says that when their images run outside the Kasm Workspaces
platform, *"certain functionality, such as audio, uploads, downloads, and microphone pass-through
are only available within the Kasm platform."*

This deployment is standalone, so **expect audio, file upload and file download to be missing.**
That is the product working as designed, not something to debug. Kasm Workspaces itself is not an
option: it is not open source, the free tier caps at 5 concurrent sessions and is licensed for
non-commercial use only, and its agent expects to drive a container runtime that k0s does not
present.

So judge this stack on **rendering feel** — latency, video, scrolling — and not on features it was
never going to have here.

## Shape
- **KasmVNC**, a GPL-2.0 fork of TigerVNC by Kasm Technologies. It *is* the X server, which is why
  the whole stack is one process — and also why it is **X11 only**: you cannot be a Wayland
  compositor by being an X server.
- **Works in browsers without WebCodecs — this is its one clear advantage over webtop.** The client
  bundle shipped here references `webp` 36 times and `createImageBitmap` twice, and **`VideoDecoder`
  / `ImageDecoder` / `WebCodecs` not once**: pixels arrive as WebP/JPEG and go through ordinary
  image decoding. (Upstream KasmVNC can also do WebCodecs H.264/H.265/AV1; that is not in this
  build.)

  webtop is the opposite — Selkies needs WebCodecs for *both* its H.264 and JPEG paths and has no
  fallback. So **kasmvnc is the one that works on Firefox Android**, where `VideoDecoder` is still
  undefined, and no server-side setting can change that for webtop.
- **Storage**: `/home/kasm-user` on a dedicated **ceph-rbd** RWO volume, 20Gi, `strategy: Recreate`
  — same reasoning as webtop (browser profiles are SQLite; single writer by definition).
- **`fsGroup: 1000`**: the image runs as `kasm_user` (uid/gid 1000) and does *not* chown its home
  itself, unlike LinuxServer's s6 entrypoint. This is the grafana/outline pattern — a fixed
  non-root uid on a small volume, so the recursive chown is cheap.
- **Auth**: Authelia forwardAuth is the only login the user sees. The image still **requires**
  `VNC_PW` and will not start without it, so there is a SOPS secret and therefore the reloader
  annotation — but the `proxy` sidecar supplies it, not a human. See the auth bullet below.
- **Traefik talks plain HTTP to the `proxy` sidecar on :8080.** KasmVNC itself serves HTTPS on
  :6901 with a build-time self-signed certificate and offers no plain-HTTP port, which used to
  require a `ServersTransport` with `insecureSkipVerify` — the first and only one in this repo.
  That is gone: the TLS hop now happens inside the pod over loopback, between the sidecar and
  KasmVNC.
- **Two probes, doing different jobs**: the kasm container keeps a TCP probe (its endpoint is
  self-signed HTTPS, where `httpGet` would need `scheme: HTTPS` and still only check a login
  page), while the sidecar has a real `httpGet /` — a 200 there proves proxy, credential and
  KasmVNC together.
- **Version**: pinned to a real tag (`1.17.0`), so Renovate tracks it natively — the one place
  this stack is tidier than webtop, which has only rolling tags.
- **kube-vnet**: only `net.traefik.traefik: ingress`, same as webtop.
- **One gate, not two — via an auth-injecting `proxy` sidecar.** nginx sits in front of KasmVNC,
  adds the `Authorization: Basic` header it insists on, and Traefik talks plain HTTP to that. The
  browser never receives a 401, so it never prompts, and **Authelia is the only login** — the same
  shape as webtop, which is what makes the two comparable in the first place.

  **KasmVNC's own auth cannot simply be turned off.** `-DisableBasicAuth` was tried first and is
  not enough: it covers the *websocket* only, exactly as its help text says. Static pages went
  200, but every `/api/*` call still answered 401 and the browser kept prompting. There is no
  config option to disable it outright.

  **Traefik cannot do the injection itself.** Its `Middleware` CRD types `customRequestHeaders` as
  `map[string]string` — literal values, no secret reference (`basicAuth.secret` exists, but that
  is for *challenging* clients, not injecting an upstream header). Inlining the credential would
  put it in plaintext in git. The sidecar keeps it in the SOPS Secret: the ConfigMap holds a
  template with an `__AUTH__` placeholder, and the real value is built at startup from `$VNC_PW`,
  living only in the Secret and in process memory.

  Safe because kube-vnet allows ingress only from Traefik, and Traefik requires an Authelia
  session; anyone able to reach the pod directly could already read the Secret.

  Two side benefits: the `insecureSkipVerify` **ServersTransport is gone** (the hop to KasmVNC's
  self-signed cert is now inside the pod over loopback), and readiness is a real `httpGet` that
  proves proxy + credential + KasmVNC together, instead of a TCP probe that only proved a port
  was open.

  **The trap this replaced**, worth knowing since it cost real time: with Kasm's basic auth on,
  Authelia's default forward-auth endpoint accepts `CookieSession` *and* `HeaderAuthorization`, so
  it consumed the browser's answer to Kasm's prompt, looked for `kasm_user` in LDAP, and returned
  401 — **no password could ever have worked**. Log signature:

  ```
  msg="Error occurred while attempting to get user details for user: the user was not found" username=kasm_user
  ```

  The fix at the time was an `authelia-session` middleware on a `forward-auth-session` endpoint
  restricted to `CookieSession`. That middleware is still defined and unused; anything deployed
  later with its own basic auth wants it.

## Security

Same as webtop: **a root-capable shell on the cluster network behind a single meaningful auth
gate.** Deliberate for an admin jump box; keep the vnet closed and open targets one at a time.

## One-time setup
1. Point `kasmvnc.example.com` DNS at the Traefik LB `10.20.2.15`. The Authelia `access_control` rule
   ships with this change.
2. Log in through Authelia. **That is the only login** — Kasm's own prompt is disabled.

   Should it ever need to be re-enabled (drop `VNCOPTIONS`), the web account is **`kasm_user`**
   with an underscore — the OS account `kasm-user` with a hyphen is *not* the web login, and
   there is also a read-only `kasm_viewer`. Read the password from the decrypted in-cluster
   Secret:
   ```
   kubectl -n kasmvnc get secret kasmvnc -o jsonpath='{.data.VNC_PW}' | base64 -d
   ```
   or from the encrypted file:
   ```
   ssh admin@10.20.5.15 "sops --decrypt --input-type yaml --output-type yaml /dev/stdin"      < kube-cluster/apps/kasmvnc/secret.yaml
   ```

`VNC_PW` is a 32-character random value, generated and SOPS-encrypted on the jumphost per the
workflow in `docs/migrating-swarm-to-kubernetes.md` §7.1 — piped over stdin, so the plaintext never
touched disk on either machine.

## Notes
- Anything installed with `apt` outside `/home/kasm-user` does not survive a restart. For durable
  tooling, build a custom image on `kasmweb/core-ubuntu-noble`.
- There is no GPU passthrough, so KasmVNC's DRI3 acceleration is not in play — CPU encoding on
  both sides of this comparison.
