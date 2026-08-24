# webtop

A full Linux desktop in a browser tab (`lscr.io/linuxserver/webtop`, XFCE on Ubuntu). Public at
`webtop.example.com`, gated by **Authelia forwardAuth**; `webtop.kube.example.com` redirects here.

Deployed alongside [`../kasmvnc`](../kasmvnc/README.md) to compare the two remote-desktop stacks
against each other — see that README for what to test. **One of the two gets deleted afterwards.**

## Shape
- **Selkies, not KasmVNC.** LinuxServer rebased every webtop image off KasmVNC in June 2025. What
  runs here is **pixelflux** (LinuxServer's own MPL-2.0 capture and encode pipeline, damage-driven
  so it encodes nothing while the screen is static), **pcmflux** for Opus audio, and **Selkies**
  (MPL-2.0, upstream) for the session, WebSocket transport and browser client. Xwayland covers
  legacy X11 apps.
- **KDE, because XFCE cannot do Wayland.** LinuxServer's Wayland stack is **KDE Plasma Wayland**
  for full desktops and **Labwc** for single-app containers; XFCE has no Wayland support, so the
  `ubuntu-xfce` tag runs plain `Xvfb`. This was deployed as XFCE first, and the running container
  proved it: `/usr/bin/Xvfb :1 -screen 0 15360x8640x24`, `WAYLAND_DISPLAY` empty,
  `XDG_SESSION_TYPE` empty, and selkies logging `[x11] Configuring Output`. An earlier version of
  this file claimed a Smithay compositor with Xwayland — that was wrong for that image, and wrong
  about the compositor besides. `PIXELFLUX_WAYLAND=false` forces the X11 path if it is ever needed
  for comparison.
- **Storage**: `/config` (the `abc` user's home) on a dedicated **ceph-rbd** RWO volume, 20Gi.
  Browser profiles are SQLite, which puts this in the repo's "embedded SQLite → block" rule rather
  than the grafana/freshrss cephfs case. `strategy: Recreate` — two desktops would fight over the
  same dotfiles and profile locks. Costs the RBD Multi-Attach stall on reschedule and no ha-surge
  drain cover, same as uptime-kuma.
- **Auth**: `CUSTOM_USER`/`PASSWORD` are deliberately unset, so the container has no login of its
  own and **Authelia forwardAuth is the sole gate** — the same call uptime-kuma makes. No SOPS
  secret, so no reloader annotation.
- **No `securityContext`, no `fsGroup`**: s6-overlay starts as root, chowns `/config` to
  `PUID:PGID`, then drops to `abc`. Same shape as freshrss. An `fsGroup` would fight the
  entrypoint for ownership.
- **`/dev/shm` is a 1Gi tmpfs `emptyDir`.** Not optional — the default 64Mi makes Chromium and
  Firefox crash on startup, and upstream document `shm_size: 1gb` for every desktop image.
- **Memory request/limit**, which is unusual in this repo. A browser grows until something stops
  it, and this pod should not be what evicts its neighbours. Memory only — a CPU limit would
  throttle an interactive desktop, which is worse than the bursty CPU it costs while encoding.
- **Version**: pinned by **digest**. LinuxServer publish no version tags for webtop, only rolling
  ones like `ubuntu-xfce`, so a digest is the honest equivalent of the repo's "pin a real tag so
  Renovate tracks bumps" rule. Renovate raises digest-update PRs against it.
- **kube-vnet**: only `net.traefik.traefik: ingress`. Egress is unrestricted, but every in-cluster
  target denies ingress by default, so reaching one means adding its egress label here **on
  purpose** (e.g. `kube-vnet/net.postgres.postgres: egress`). Starting closed is the point.

## Security

**This is a root-capable shell on the cluster network behind a single auth gate.** The desktop has
a terminal with passwordless sudo, and the pod can egress anywhere. Authelia (two-factor, group
restricted) is the only thing in front of it. That is a reasonable trade for an admin jump box and
a bad one for a machine you hand to someone else.

If it ever becomes browser-only, upstream offers `DISABLE_SUDO`, `DISABLE_TERMINALS` and
`HARDEN_DESKTOP`.

## Notes
- **Requires WebCodecs in the browser, with no fallback.** Both client paths need it: H.264 uses
  `VideoDecoder`, and the JPEG-stripe mode uses `ImageDecoder`. The bundle is explicit —
  `if (typeof ImageDecoder > "u") { console.warn("ImageDecoder API not supported. Cannot decode
  JPEG stripes."); return }` — and `createImageBitmap` is used only for the mouse cursor. So
  changing `SELKIES_ENCODER` cannot work around a browser without WebCodecs.

  Fine on Chrome/Edge 94+, **Firefox 130+ desktop** (both APIs shipped, no about:config needed) and
  Safari 26+. **Broken on Firefox Android**, where `VideoDecoder` is still undefined — and there is
  nothing to configure server-side. [`../kasmvnc`](../kasmvnc/README.md) works there, since its
  client decodes WebP/JPEG with no WebCodecs dependency at all. That is currently the strongest
  argument for keeping both.
- Selkies needs **AVX2** (Haswell, 2013+) for H.264. Verified present on the nodes despite
  `model name: QEMU Virtual CPU version 2.5+`, so it is not the cause of any rendering problem —
  check before blaming it. Selkies still chose `Encoder: CPU | Mode: JPEG` on the XFCE image;
  `SELKIES_ENCODER` selects between x264 and JPEG if that needs forcing.
- There is **no GPU passthrough**, so pixelflux's zero-copy NVENC/VAAPI path — Selkies' headline
  advantage — is not in play. Both stacks fall back to CPU encoding here.
- Upstream Selkies states it is **short of maintainers**. Partly mitigated by LinuxServer owning
  the pixel path (pixelflux, pcmflux) themselves, which is the part most likely to need work.
- **Installing apps: use the Selkies control panel (proot-apps), not `apt` and not Discover.**
  There are three routes and only one of them persists:

  | route | installs to | survives restart |
  |---|---|---|
  | **proot-apps** (Selkies web control panel) | `$HOME/proot-apps`, `$HOME/.local/{bin,share}` → `/config` | **yes** |
  | KDE Discover → applications | `/usr` (PackageKit/apt) | no |
  | KDE Discover → themes, widgets, Plasma add-ons | `~/.local/share` → `/config` | yes |
  | `apt install` in a terminal | `/usr` | no |

  **proot-apps is the intended mechanism** and is what the control panel's "install" opens a
  terminal for. It unpacks OCI images into the home directory and runs them under `proot`
  (userspace, no root, no user namespaces), so everything lands on the persisted volume.
  `/proot-apps` in the image root holds only the tooling (`proot`, `jq`, `ncat`); the copy that
  matters is `/config/.local/bin/proot-apps`. Limited to LinuxServer's curated catalogue rather
  than arbitrary apt packages.

  **KDE Discover is the trap**: `packagekitd` *is* installed (`/usr/libexec/packagekitd`), so an
  application install genuinely succeeds, writes to `/usr`, and is gone on the next restart.

  Note `command -v proot-apps` returns nothing in a non-login shell — `/config/.local/bin` is not
  on `PATH` there. That is not evidence of absence.
- kasmvnc has **no equivalent**, which is a real difference for the evaluation: on that side an
  installed app genuinely does not survive.
- **The XFCE image crash-looped**, which is what produced the black screen: `/config` accumulated
  239MB of core dumps from `xfce4-session` and `xfdesktop`, timestamped exactly across the window
  someone was trying to use it. `xfdesktop` is what draws the desktop, so a black screen is the
  expected symptom. If a black screen ever returns, look for `/config/core.*` first — it names the
  process that died.
- **`/config` carried over from the XFCE deployment**, so it still holds XFCE dotfiles and those
  core dumps. Harmless so far, but if KDE behaves oddly, a stale `.config` is the first suspect;
  the clean test is an empty volume.
