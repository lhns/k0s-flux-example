# Authenticating Snikket against lldap

**Status: implemented.** Snikket authenticates against lldap. The files are `ldap.cfg.lua`
(prosody config), `mod_auth_lldap.lua` (the forked auth provider) and `ldap-secret.yaml`
(the bind password), wired up in `kustomization.yaml` and `resources.yaml`.

lldap is the source of truth: create a user there, add them to `xmpp` or `admin`, and they log
in to XMPP with their directory password — no invite link, no second password.

This document is the reasoning behind each piece, and the traps found on the way. Everything
below was verified against the running image; keep it in sync if the config changes.

## How it works

`mod_auth_ldap` treats LDAP as the user database rather than as a password oracle for local
accounts:

```lua
function provider.user_exists(username)
    return not not get_user(username);   -- queries LDAP, not a local store
end
```

There is no account-creation step to build. Prosody creates a user's data (roster, archives)
lazily on first use, keyed by JID, so "the user exists once they are in lldap" is the module's
normal behaviour.

`ldap_mode = "bind"` authenticates by binding to lldap as the user's own DN with the password
they supplied. Snikket's server never stores it.

Everything needed ships in the image. Verified against the running container:

| fact | source |
| --- | --- |
| `mod_auth_ldap` is present | `/usr/lib/prosody/modules/mod_auth_ldap.lua` |
| Extension point: `Include (ENV_SNIKKET_TWEAK_EXTRA_CONFIG or "/snikket/prosody/*.cfg.lua")` | generated config line 443 |
| Prosody resolves `ENV_<NAME>` to `os.getenv()` | `configmanager.lua:216` |
| `plugin_paths = { "/etc/prosody/modules" }` already set | generated config line 35 |

The third means the bind password comes straight from a Secret as an env var — no rendering
step, no secret in a ConfigMap. The fourth means a custom module needs no config change to be
found.

## Config

### The VirtualHost must be re-opened

Snikket sets `authentication` twice — globally at line 256, and again **inside** the VirtualHost
at line 331. Its `Include` is the last line of the file, after two `Component` blocks. Prosody's
config is sectional, so settings in the included file belong to whatever section preceded them:
a bare `authentication = "ldap"` lands in the `share.` component and is ignored, with no error
and no log line.

Re-opening the VirtualHost fixes it, and is legal because `env.VirtualHost`
(`configmanager.lua:276`) has no duplicate check — it errors only when the name clashes with a
`Component`. Later settings overwrite earlier ones.

**Every per-host option below depends on this.** If the override stops applying (an upstream
reordering, say), authentication reverts to `internal_hashed` and LDAP users cannot log in —
it fails closed, nothing is lost or exposed, and the fix is a config revert.

### The file

`ldap.cfg.lua` in this directory, mounted from a ConfigMap at `/etc/snikket-extra/`, with
`SNIKKET_TWEAK_EXTRA_CONFIG=/etc/snikket-extra/*.cfg.lua`. The shape:

```lua
VirtualHost "xmpp.example.com"
    authentication = "lldap"        -- the fork, see "The portal's user list" below

    ldap_server   = "lldap.lldap.svc.cluster.local:3890"
    ldap_base     = "ou=people,dc=example,dc=com"
    ldap_rootdn   = "uid=snikket,ou=people,dc=example,dc=com"
    ldap_password = ENV_LDAP_BIND_PASSWORD
    ldap_mode     = "bind"

    -- Who may log in. memberOf is a first-class field in lldap and OR is supported,
    -- so several groups can be listed; a bare group name works as well as a full DN.
    ldap_filter   = "(&(uid=$user)(|(memberOf=xmpp)(memberOf=admin)))"

    -- Read by the fork: membership grants prosody:admin on login.
    ldap_admin_group = "admin"

    -- Required: see "SASL" below. Without this nobody can authenticate at all.
    disable_sasl_mechanisms      = { "OAUTHBEARER" }
    allow_unencrypted_plain_auth = false

    -- The account-creating paths cannot work against LDAP; see "Invites" below.
    modules_disabled = { "invites_register", "invites_register_api", "register" }
```

No static `admins` list: the module derives admin from the `admin` group instead, so the
directory stays the single source of truth. The file itself carries the same reasoning inline —
this section is the summary, `ldap.cfg.lua` is what runs.

`ldap_tls` is deliberately unset — lldap serves plain LDAP on 3890, which is how Authelia
connects to it too (`start_tls: false` in `apps/authelia/configuration.yml`).

### Kubernetes wiring

- ConfigMap with the above, mounted at `/etc/snikket-extra/` on the `server` container
- `LDAP_BIND_PASSWORD` from a SOPS Secret via `secretKeyRef`
- Pod label `kube-vnet/net.lldap.lldap: egress`
- `snikket` added to `allowedNamespaces.names` in `apps/lldap/vnet.yaml`

Both halves of the kube-vnet wiring are required; the label alone does nothing.
`apps/authelia/resources.yaml` is the reference consumer.

## SASL: PLAIN has to be enabled

Snikket disables the only mechanism bind-mode LDAP can offer (generated config line 258):

```lua
disable_sasl_mechanisms = { "PLAIN", "OAUTHBEARER" }
```

Prosody advertises a mechanism only when the auth provider supplies a matching backend
(`util/sasl.lua`, `registerMechanism`):

```lua
registerMechanism("PLAIN",        {"plain", "plain_test"}, ...)
registerMechanism("SCRAM-"..hash, {"plain", "scram_"..hash}, ...)
```

`mod_auth_ldap` in bind mode supplies only `plain_test`. SCRAM requires the *server* to compute
the expected proof from stored key material; an LDAP bind is a yes/no oracle, which is exactly
the PLAIN model. Bind mode therefore cannot offer SCRAM, and leaving PLAIN disabled means no
usable mechanism is advertised and **every login fails**.

Alternatives, all dead ends:

| alternative | why it fails |
| --- | --- |
| `ldap_mode = "getpasswd"` | supplies the `plain` backend and would restore SCRAM, but reads `userPassword` back; lldap stores nothing recoverable (OPAQUE) |
| `mod_auth_ldap2` (also in the image) | same `get_password` dependency |
| OAUTHBEARER / SASL2 (XEP-0493) | no OAuth auth module in the image; client support is thin |
| Syncing hashes into Prosody's internal store | needs to read passwords out of lldap — same impossibility |

This is not a defect in either project. Disabling PLAIN is correct for Snikket's default
`internal_hashed` backend, which does support SCRAM; lldap not storing recoverable passwords is
better than directories that do. The incompatibility is inherent to delegating verification to a
remote bind.

Enabling PLAIN is safe in this configuration, and the config above scopes it to the XMPP host
only:

- `c2s_require_encryption = true` (line 223) — clients cannot connect without TLS
- Prosody classes PLAIN as insecure unless `allow_unencrypted_plain_auth` is set
  (`mod_saslauth.lua:27-28`), so it is not advertised on an unencrypted stream
- `tls_profile = "modern"` (line 172)

`allow_unencrypted_plain_auth = false` is the default; it is set explicitly so the safety
property sits next to the concession and cannot regress silently.

Net change: the server sees the password at login instead of never. Nothing becomes sniffable,
and s2s and the web keep Snikket's original settings.

## Password changes

These keep working, with the right lldap group. `mod_auth_ldap.set_password` issues:

```lua
ldap_do("modify", 2, dn, { '=', userPassword = password })
```

an LDAP Modify/Replace on `userPassword` — which is exactly, and only, what lldap accepts
(`crates/ldap/src/modify.rs`):

```rust
.eq_ignore_ascii_case("userpassword") || change.operation != LdapModifyType::Replace
    → UnwillingToPerform
if !credentials.can_change_password(&user_id, user_is_admin) → denied
```

So the bind account needs lldap's built-in **`lldap_password_manager`** (lldap's own tests cover
this path: `setup_bound_password_manager_handler`). That group also grants search, so it
replaces `lldap_strict_readonly` rather than accompanying it.

The trade-off is a real choice:

| bind account group | effect |
| --- | --- |
| `lldap_strict_readonly` | passwords change only in lldap's own UI |
| `lldap_password_manager` | the XMPP client's password dialog works; the bind account can rewrite any user's password |

## Invites

Invite *creation* still succeeds under LDAP; only redemption fails, when `create_user` returns
`"Account creation not available with LDAP."` That produces a link which looks valid and fails
in the recipient's hands, so the account-creating paths are disabled instead.

Snikket already sets part of this:

```lua
registration_invite_only = true                          -- line 177
allow_contact_invites = false                            -- line 194
deny_user_invites_by_roles = { "prosody:restricted" }    -- line 197
```

The rest is the `modules_disabled` line in the config above:

- `invites_register` — redeeming an invite into a new account
- `invites_register_api` — the portal endpoint that mints account invites
- `register` — in-band registration

`invites`, `invites_groups` and `invites_default_group` stay: Snikket's circles and contact
invites build on them and they do not create accounts. Effect on the portal: the invite button
fails at the API rather than producing a dead link.

## The portal's user list, and admin roles

Both need one small custom module. `mod_http_admin_api` backs the portal, and has two relevant
call sites:

- `delete_user` (`:574`) does `local ok, err = usermanager.delete_user(...)` and returns the
  error — a clean "not implemented", correct for a directory Prosody does not own. **No fix
  needed.**
- `users()` (`:420`, also `:830`) does `for username in usermanager.users(module.host) do`.
  Unimplemented provider methods fall through `provider_mt = { __index = new_null_provider() }`
  to a dummy returning `nil, "method not implemented"`, so the loop becomes `for username in
  nil` — a Lua error, i.e. HTTP 500. Neither `mod_auth_ldap` nor `mod_auth_ldap2` implements it.

Admin rights have no built-in LDAP mapping either: `ldap_admin_filter` is deprecated and ignored
(the module logs an error if set), and `mod_auth_ldap2`'s `provider.is_admin` uses the legacy
`is_admin` API Prosody is deprecating (`usermanager.lua:354`). Either use the static `admins`
list in the config above, or sync roles on login in the same module.

The module here does the latter, hooking `authentication-success`. Both directions are verified:
adding a user to `admin` promotes them to `prosody:admin` on their next login, and removing them
puts them back on `prosody:registered` on the login after that. Each writes a log line:

```
role for xmpp-test-user: prosody:admin -> prosody:registered (from lldap group "admin")
```

**Revocation is therefore not immediate, and that is the one thing to know about this design.**
A group change does nothing until the account next authenticates, so someone removed from `admin`
stays admin until then, and a client that is already connected keeps the session it has. Taking
someone out of `xmpp` has the same shape: it stops the next login, not the current connection.

To cut access off now rather than eventually, remove the group *and* end their sessions
(`prosodyctl shell user clients <jid>` shows them). Making it immediate would mean polling lldap
or watching it for changes, which is a different design and buys little here — the directory is
small and admin changes are rare.

### Shipping the module

Copy `/usr/lib/prosody/modules/mod_auth_ldap.lua`, rename it, add `users()`, and mount it into
`/etc/prosody/modules/` — already in `plugin_paths`, so nothing else changes.

**Use a `subPath` mount.** That directory holds 68 symlinks into `prosody-modules`; a
whole-directory ConfigMap mount would shadow every one of them and break the server. `subPath`
mounts do not receive ConfigMap updates, so changes need a pod roll — Reloader already does that.

```lua
function provider.users()
    if ld == nil then
        local err; ld, err = lualdap.open_simple(ldap_server, ldap_rootdn, ldap_password, ldap_tls);
        if not ld then module:log("error", "LDAP: %s", tostring(err)); return function () end; end
    end
    local ok, iter, invariant, initial = pcall(ld.search, ld, {
        base = ldap_base;
        scope = ldap_scope;
        filter = ldap_filter:gsub("%$(%a+)", { user = "*", host = host });
        attrs = { "uid" };
    });
    if not ok then ld = nil; return function () end; end
    return function ()
        local dn, attr = iter(invariant, initial);
        if not dn then return nil; end
        return attr and attr.uid;
    end
end
```

Reusing `ldap_filter` means the portal lists exactly the users who can log in. Limits: one
shared connection (the module already reconnects via `retry_count`), and no paging.

For group-driven admin, hook `authentication-success` (fired by `mod_saslauth.lua:67`), check
the user's `memberOf`, and call `usermanager.set_user_role(user, host, "prosody:admin")`
(`usermanager.lua:244`). Roles persist in `mod_authz_internal`, so the internal role store
converges toward LDAP on each login.

## Existing accounts and stored data

Nothing is migrated or destroyed.

The internal password hash is neither read nor written once `authentication = "lldap"` — the
provider is replaced outright. It stays on disk, unused. That is why rollback is clean.

`authentication` and `storage` are independent settings; Snikket selects storage separately
(line 260, `SNIKKET_TWEAK_STORAGE`, default internal files). Prosody keys rosters, archives and
everything else by `username@host`, so if the lldap uid matches the existing localpart, the same
data serves the same JID.

## lldap setup

None of this is declarative — this repo does not declare lldap objects
(see `apps/forgejo/README.md` for the same constraint).

1. Bind account `uid=snikket,ou=people,dc=example,dc=com`, in `lldap_password_manager` (or
   `lldap_strict_readonly`; see *Password changes*).
2. Groups `xmpp` and `admin` under `ou=groups`, with the relevant members.
3. Use the same username as the existing JID localpart so roster and archives carry over.

**Troubleshooting note:** with the bind account in neither group, lldap lets a user see only
themselves, every lookup for another user returns nothing, and Prosody reports an authentication
failure. The symptom is "wrong password" when the cause is missing read permission.

## Verification

Check the effective value first — the failure in *Config* is silent:

```sh
kubectl -n snikket exec deploy/snikket -c server -- \
  prosodyctl shell config get xmpp.example.com authentication      # must be "lldap"
```

Then:

- a member of `xmpp` or `admin` logs in with their lldap password
- a valid lldap user in **neither** group is rejected — proves the filter gates rather than
  decorates
- the portal's user list renders
- a password change from a client succeeds, and the new password works in lldap's UI
- existing roster and history are intact for the pre-existing JID

## Rollback

`git revert` the commit that introduced this. Snikket reverts to `internal_hashed` on the next
roll, and the pre-existing accounts work immediately — their hashes were never read or written
while LDAP auth was active, so nothing needs restoring.
