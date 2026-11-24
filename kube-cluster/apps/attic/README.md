# attic

[Attic](https://github.com/zhaofengli/attic) Nix binary cache for the per-user desktops
(`apps/webtop`). One cache, `desktops`, served in-cluster at
`http://attic.attic.svc.cluster.local/desktops`.

**Not running yet.** The component is suspended until its secrets exist; see [Enabling](#enabling).
Merging it creates only the `attic` namespace and its control namespace.

## Decisions

- **Desktops pull, they never push.** Every desktop trusts the cache's signing key, so whatever
  is pushed runs on every user's desktop. Pushes come from an admin token (below) or CI. Giving
  desktops push, even per user, would be a deliberate decision to share that trust between all
  desktop users; it is not enabled.
- **Public cache.** Nix sends no token to a substituter. Who can read is bounded by the network:
  the vnet admits only `webtop`, and the desktops' own egress policy needs a rule for it.
- **In-cluster only, no IngressRoute** (litellm precedent). The only readers are in the cluster,
  and admin goes through a port-forward. Authelia forward-auth would break the `attic` CLI, and a
  token-only route would put an upload API on the internet for no reader that needs it.
- **Plain HTTP inside the cluster.** Paths are verified against the cache's signature, not the
  transport.
- **Shared CNPG cluster**, the default for a new app's database; atticd runs its migrations on start.
- **Chunks in S3 on RGW**, bucket `attic` with its own RGW user, so a quota can cap the cache
  without touching other buckets.
- **Retention 3 months since last access** (`default-retention-period`); GC runs every 12 hours.
- **Image `latest@sha256`.** Upstream publishes branch and commit tags only; Renovate bumps the digest.

## Shape

- `resources.yaml`: atticd in monolithic mode (API, migrations, GC), one replica, `Recreate`.
  Service on port 80: `allowed-hosts` matches the Host header exactly, port included.
- `server.toml`: plain config. Database URL, token key and S3 keys come from env.
- `database.yaml`: role and database `attic` in the shared cluster.
- `vnet.yaml`: `webtop` may reach it.

## Enabling

From the repo root. Steps 2-4 only write files; the cluster changes with step 5's merge.

**1. RGW user and bucket** (a Ceph change; on a node with `radosgw-admin`):

```sh
radosgw-admin user create --uid=attic --display-name="attic binary cache"
radosgw-admin quota set --quota-scope=user --uid=attic --max-size=200G   # size to taste
radosgw-admin quota enable --quota-scope=user --uid=attic
# with the attic user's keys:
aws --endpoint-url https://s3.example.com s3 mb s3://attic
```

**2. S3 keys**, one Secret per file. Copy each key, then:

```sh
python3 scripts/secret-from-clipboard.py kube-cluster/apps/attic/secret-s3-access-key.yaml attic-s3-access-key attic AWS_ACCESS_KEY_ID
python3 scripts/secret-from-clipboard.py kube-cluster/apps/attic/secret-s3-secret-key.yaml attic-s3-secret-key attic AWS_SECRET_ACCESS_KEY
```

**3. Token signing key** (RSA PKCS#1, base64). It goes straight to the clipboard (`clip` on
Windows, `wl-copy`/`xclip` elsewhere):

```sh
openssl genrsa -traditional 4096 | base64 -w0 | clip
python3 scripts/secret-from-clipboard.py kube-cluster/apps/attic/secret-token.yaml attic-token attic ATTIC_SERVER_TOKEN_RS256_SECRET_BASE64
```

**4. Database role secret.** CNPG wants `username` and `password` in one `basic-auth` Secret with
the Reflector annotations, which the clipboard script cannot write. This generates the password and
encrypts it without printing it. Hex, because it is spliced into the database URL unescaped:

```sh
f=kube-cluster/apps/attic/db-role-secret.yaml; cfg=$(mktemp)
{ cat <<'EOF'
apiVersion: v1
kind: Secret
metadata:
  name: attic-db
  namespace: postgres
  annotations:
    reflector.v1.k8s.emberstack.com/reflection-allowed: "true"
    reflector.v1.k8s.emberstack.com/reflection-allowed-namespaces: attic
    reflector.v1.k8s.emberstack.com/reflection-auto-enabled: "true"
    reflector.v1.k8s.emberstack.com/reflection-auto-namespaces: attic
type: kubernetes.io/basic-auth
stringData:
  username: attic
EOF
  echo "  password: $(openssl rand -hex 32)"
} | sops --config "$cfg" encrypt --input-type yaml --output-type yaml \
    --age age1exampleexampleexampleexampleexampleexampleexampleexamplexxxxx \
    --encrypted-regex '^(data|stringData)$' --filename-override db-role-secret.yaml > "$f"
rm -f "$cfg"; grep -c 'ENC\[' "$f"   # 2
```

**5. Enable.** Add `db-role-secret.yaml`, `secret-token.yaml`, `secret-s3-access-key.yaml` and
`secret-s3-secret-key.yaml` to `resources:` in `kustomization.yaml`, delete the `suspend`
annotation, and merge. Then `kubectl -n attic logs deploy/attic` should show it listening.

**6. Admin token and cache.** The token prints once; keep it in the password manager.

```sh
kubectl -n attic exec deploy/attic -- /bin/atticadm -f /config/server.toml make-token \
  --sub admin --validity 1y --pull desktops --push desktops --delete desktops \
  --create-cache desktops --configure-cache desktops --configure-cache-retention desktops --destroy-cache desktops
```

With `kubectl -n attic port-forward svc/attic 8080:80` running (on the jumphost, with
`ssh -L 8080:localhost:8080` from a machine that has Nix):

```sh
nix shell nixpkgs#attic-client
attic login local http://localhost:8080 <token>
attic cache create desktops --public
attic cache info desktops    # "Public Key: desktops:..."
```

**7. Point the desktops at it** in `apps/webtop/release.yaml`, once the chart there is a build that
has `desktop.nix` (lhns/kube-webtop-workspaces, "extra Nix substituters"). This opens one hole in
the desktops' internet-only egress, to this pod only:

```yaml
    desktop:
      podLabels:
        kube-vnet/net.attic.attic: egress
      nix:
        extraSubstituters: [http://attic.attic.svc.cluster.local/desktops]
        extraTrustedPublicKeys: ["desktops:..."]   # from `attic cache info`
    networkPolicy:
      desktopEgress:
        extraRules:
          - to:
              - namespaceSelector: {matchLabels: {kubernetes.io/metadata.name: attic}}
                podSelector: {matchLabels: {app.kubernetes.io/name: attic}}
            ports: [{port: 8080, protocol: TCP}]
```

Desktops pick it up on their next start.

## Pushing

With the admin token, from an admin's desktop once step 7 is in. Not through the port-forward: `push`
switches to the server's `api-endpoint`, the in-cluster name.

```sh
attic login local http://attic.attic.svc.cluster.local <token>
attic push desktops ./result
```

A CI runner pushing from its own namespace needs that namespace in `vnet.yaml`, the pod label
`kube-vnet/net.attic.attic: egress` and a token limited
to `--push desktops`.
