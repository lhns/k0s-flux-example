# msmtpd

Outbound SMTP relay (`crazymax/msmtpd`) in its own namespace. In-cluster senders speak plaintext SMTP
to `msmtpd:2500`; msmtpd relays to **Gmail** over STARTTLS. This keeps the Gmail app-password in one
Secret (`msmtpd-secrets`) so app configs — Authelia today, others later — carry **no SMTP secret**.

## Shape
- Deployment `crazymax/msmtpd` listening on `:2500`. Upstream Gmail settings are inline env; only
  `SMTP_PASSWORD` (the Gmail app password) is a SOPS secret (`secret.yaml`).
- **kube-vnet**: a `msmtpd` VirtualNetwork (`vnet.yaml`) that sender namespaces join (Authelia adds
  `kube-vnet/net.msmtpd.msmtpd: egress`). Egress to Gmail is external, unrestricted. Nothing is exposed
  externally.
- No persistence.

## The sender address rule

**Every app that sends through this relay must use `admin+<app>@example.com`.** Not
`<app>@example.com`, however much nicer that would read.

The relay's upstream is a Gmail account, and Gmail only lets you send as an address it *owns* —
the account itself, or an address verified under *Send mail as*. Anything else gets its `From:`
header rewritten to the account, so a vanity domain does not fail, it just quietly stops being
attributable. Plus-addressing is owned by definition, which is why `+<app>` survives and gives one
readable sender per service in the mailbox.

Current senders: `+auth` (authelia), `+gitea`, `+forgejo`.

Two traps worth knowing before you "verify" a change here:

- **A successful SMTP session does not mean the From survived.** The envelope sender (`MAIL FROM`)
  and the `From:` header are checked separately, and only the envelope produces an exit code.
  `msmtp -f auth@example.com … && echo ok` prints `ok` for an address Gmail will happily rewrite.
- **`SMTP_AUTH` must be `on`.** msmtp defaults to `auth off` and the image only writes an `auth`
  line when that variable is set — supplying `SMTP_USER` and `SMTP_PASSWORD` does *not* imply it.
  Without it every message is rejected with `530 5.7.0 Authentication Required`, which msmtp
  reports as `envelope from address <x> not accepted by the server`. That wording points straight
  at the sender address and will send you hunting for an alias problem that does not exist; the
  giveaway is `auth=off` in msmtpd's own log line. This was the state of the relay until
  2026-08-06, during which **nothing sent at all**.

If a real `@example.com` sender is ever wanted, the fix is not here — it is either verifying the alias
in the Gmail account's *Send mail as*, or moving the relay's upstream off Gmail to a provider that
is authoritative for `example.com`.
