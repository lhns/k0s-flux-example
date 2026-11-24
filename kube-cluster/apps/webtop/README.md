# webtop

Per-user NixOS desktops in the browser at `desktop.example.com`, started on demand by
[kube-webtop-workspaces](https://github.com/lhns/kube-webtop-workspaces) (`release.yaml`, chart
`desktops`).

- **`runtimeclass.yaml`**: `RuntimeClass webtop-systemd` (systemd as PID 1, ADR 0067).

## Per-user desktops

- **Access**: `desktop.example.com` -> Traefik `authelia` middleware (two-factor, `webtop` or `admin`
  group, `apps/authelia/configuration.yml`) -> `desktops-gateway`. The gateway trusts `Remote-User`
  and `Remote-Groups` from whoever reaches it; the chart's NetworkPolicy, which admits only
  Traefik, is the gate. `trustedProxyCIDRs` (the pod CIDR) is no second check, since Traefik and
  the desktops share that range. A user in `webtop` opens their own desktop; `admin` may open any.
- **Authorization**: only the gateway's group check, on every request (no LDAP, kube-webtop-workspaces
  ADR 0017). A user removed at the provider is refused from the next request, but a running desktop
  stays up: `kubectl -n webtop exec deploy/desktops-controller -- /kube-webtop-workspaces desktops
  stop|remove <user>` (`remove` keeps the PVC).
- **State**: a per-user RBD PVC (`desktop-<user>`, 50Gi, `ceph-rbd`) mounted at `/persist`. The
  controller creates it and **never deletes it**, so stopping or deleting a desktop keeps the data;
  removing a PVC and its PV is a deliberate manual step (see the repo `AGENTS.md`, pv-reaper).
- **Runtime**: `RuntimeClass webtop-systemd` (ADR 0067); desktop egress is internet only (plus DNS and
  the controller API): `10/8`, `172.16/12`, `192.168/16`, `100.64/10` and `169.254/16` are excluded,
  which covers the pod, service and node ranges.
- **Metrics**: controller and gateway `:9090`, scraped by the otel `cluster` collector.
  Dashboard "Webtop Desktops" (folder Cluster): `grafana.yaml`, `dashboards/webtop-desktops.json`.
- **Time zone and language**: `desktop.defaults` in `release.yaml` is the site default
  (Berlin, German). A user changes their own in `~/.config/home-manager/home.nix`, then runs
  `home-manager switch` and signs out and in again:
  ```nix
  webtop.timeZone = "Europe/London";
  webtop.locale = "en_GB.UTF-8";      # LANG; one of the image's locales
  webtop.formats.LC_TIME = "sv_SE.UTF-8";  # optional, per category: ISO dates, 24 h
  ```
  Formats can also be set in System Settings > Region & Language, which wins over both. Details:
  webtop-nix README, "Time zone and locale".

## System module

`artifacts/desktop.yaml` builds the desktop image the chart runs (`desktop.image: ${WEBTOP_DESKTOP_IMAGE}`):
kube-webtop-workspaces' desktop image with the site's NixOS module (`system.nix`, inline) built into its
system, by webtop-nix's `image/system/Dockerfile` from the image's builder (`<tag>-builder`; webtop-nix
README, "System module", ADR 0020). The result is the stock image's layers plus one; `{ }` publishes the
stock image unchanged.

- **A change** to `system.nix` or to either `FROM` is a new build and a new digest, which the desktops take
  at their next start like any image update. Nothing builds on a desktop.
- **Both `FROM`s come from one kube-webtop-workspaces build**, the one `release.yaml` pins for the chart and
  controller: bump all of them together (by hand; Renovate proposes releases only). The installer fails the
  build on a mismatched image and builder.
- **Build pods** run in `webtop`: no Pod Security level is enforced there, and the chart's NetworkPolicies
  select only its own pods, so the build reaches cache.nixos.org and ghcr.io.
- **Registry space**: the first build pushes the whole desktop image (about 2 GB compressed) into
  oci-composer's registry, and each new desktop version its changed layers (all of them on a nixpkgs
  bump); `push.history: 3` keeps that bounded.

### Decisions

- Hostname `desktop.example.com`; `webtop.example.com` and `webtop.kube.example.com` are gone with the old
  shared webtop.
- Namespace `webtop`: kube-vnet isolates pod by pod, so a second namespace adds nothing.
- Pods set a memory request/limit and no CPU limit: a CPU limit throttles an interactive or bursty
  workload instead of protecting neighbours (other components cite this).
- Admins must not open other users' desktops via `/u/<user>/` until per-user origins exist: all
  desktops share one origin (`desktop.example.com`), so a desktop's web content could act as the admin.
- Desktops run the chart's seccomp profile (RuntimeDefault plus `pivot_root`, from the pinned
  webtop-nix), installed by the Security Profiles Operator (ADR 0069), not RuntimeDefault:
  bubblewrap (`buildFHSEnv` apps such as jetbrains-toolbox) and containers in the desktop need
  `pivot_root`.
- Chart and controller are dev builds (`0.0.0-dev.<sha>`) pinned by digest until the project cuts a
  release (ADR 0009); Renovate never automerges them.
- Site-wide system settings are a NixOS module built into the desktop image here (`artifacts/desktop.yaml`),
  not a fork of the image or a rebuild on each desktop: the stock image stays kube-webtop-workspaces',
  and a change costs one in-cluster build. Its build pods run in `webtop`: a separate namespace would
  add nothing (see "System module").
