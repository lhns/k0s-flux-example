# seerr

Migrated from the Swarm `arr` stack, where it ran as the `seerr` service against the
stack's own Postgres.

## Why it is not in the `arr` namespace

`arr` routes every pod through the VPN gateway by default — the namespace label is the
switch. seerr has no reason to be tunnelled: it talks to TMDB, to the *arr APIs and to
jellyfin, and it is reached by people from outside. Keeping it in its own namespace is a
stronger guarantee than an opt-out label on the pod, which is one edit away from being
forgotten.

## Storage and database

`/config` is the Swarm `${DATA_PATH}/jellyseerr/data` tree, mounted in place from CephFS
`appdata:/docker/arr/jellyseerr` with a `subPath` — the same bytes, not a copy.

The `jellyseerr` database moved into the shared CNPG cluster with its own `seerr` login
role. The Swarm deployment used the `postgres` superuser and a password shared across the
whole stack; that does not carry over.

## Auth

No `authelia` middleware, unlike everything in `arr`. seerr manages its own accounts and
is meant to be usable by people without SSO logins. `jellyseerr.example.com` 301s to
`seerr.example.com`.
