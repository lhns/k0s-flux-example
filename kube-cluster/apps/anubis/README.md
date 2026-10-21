# anubis

[Anubis](https://github.com/TecharoHQ/anubis) proof-of-work challenge against scrapers, shared by
all public apps through one Traefik forwardAuth `Middleware` (`anubis`, reflected into every
namespace). Decision: [ADR 0055](../../../docs/adr/0055-anubis-forwardauth-for-public-apps.md).

- Two replicas behind a PDB; Valkey (no persistence) holds in-flight challenges only.
- The challenge page is `https://anubis.example.com`. A solved client gets a cookie for `example.com`, so
  one challenge covers every protected host. The cookie is **not bound to the client IP**
  (`-jwt-restriction-header` is empty): a changing IP would otherwise clear it and break SPAs'
  background calls. A stolen cookie therefore works from any IP until it expires.
- Metrics (`anubis_*`, per-rule policy hits) on :9090, scraped by the otel `cluster` collector.
- Policy: `botPolicies.yaml` imports Anubis's default rules (`(data)/meta/default-config.yaml`),
  a ConfigMap, so an edit rolls the pods.

## Decisions

- **Chat link previews pass.** Preview bots run no JavaScript, so a challenged link previews as
  the Anubis page. Telegram and Google's user-triggered fetchers (Keep, Messages) use Anubis's
  verified rules. Discord, WhatsApp, iMessage (`facebookexternalhit`), Slack, X and Synapse are
  allowed by user agent only, as they publish no address ranges Anubis can check: a scraper
  claiming one of those user agents is not challenged.

## Protect an app

1. Add `- name: anubis` to the route's `middlewares`.
2. Nothing else for a host under `example.com`: `REDIRECT_DOMAINS` is `*.example.com`. A host on another
   domain must be added there, or the challenge refuses to send the client back.

Keep git, API and runner paths working by serving them from a second route on the same host with
a higher `priority` and no `anubis` middleware (see `apps/spliit/routing.yaml`: it bypasses the
SPA's `/trpc`, `/auth` and asset paths, leaving only the HTML shell challenged).

## Dashboard

`grafana.yaml` holds the Anubis `GrafanaDashboard` (`/d/anubis`, folder *Cluster*): the
forwardAuth decisions and the challenge flow.

## Alerts

`grafana.yaml`: rule group `app-anubis`, folder *Cluster*. Added before Anubis had metrics history, so not backtested.

| Rule | Fires when | For | Severity |
| --- | --- | --- | --- |
| `anubis-metrics-stalled` | no `anubis_policy_results` sample for 10m (also on NoData); every forwardAuth route depends on it | 5m | critical |
| `anubis-rule-spike` | a rule's DENY count, or the challenges issued, over 15m is above 200 and 10x its 1-day 15m average; ignores DENY rules under 2h old | 5m | warning |
| `anubis-solutions-rejected` | over 10 failed validations in 1h and over 50% of submitted solutions rejected | 30m | warning |

## Traps

- `status_codes.DENY` must be 403. Anubis defaults it to 200, which forwardAuth treats as a pass,
  so denied scrapers would get through.
- The signing key (`secret.yaml`, `ED25519_PRIVATE_KEY_HEX_FILE`) must be the same on every
  replica and stable across restarts. Otherwise every client is challenged again, and a cookie
  issued by one replica fails on the other.
- The empty `-jwt-restriction-header=` must stay an `args` entry: `flagenv` skips empty env vars,
  so `JWT_RESTRICTION_HEADER=""` would leave the `X-Real-IP` default in force.
- `TARGET` is a single space: that selects forwardAuth mode.
- Anubis serves `/healthz` on the metrics port (9090), not on `BIND`.
- The `nginx-test` route (`nginx-test.example.com`) is the canary. Browser user agents get a 307 to
  the challenge; the default `curl` user agent passes.
