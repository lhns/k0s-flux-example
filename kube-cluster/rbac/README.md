# rbac

Cluster-wide authorization that belongs to no single app.

`oidc/` maps Authelia groups onto Kubernetes groups. Headlamp was the first consumer,
but the binding is not Headlamp's: it applies to every client presenting an Authelia
id_token, `kubectl` included. Keeping it under `apps/headlamp` would have implied a
scope it does not have.

Who the issuer is, and how claims become Kubernetes usernames and groups, is the
API server's side of this — see `../k0s-files/authentication-config.yaml`.
