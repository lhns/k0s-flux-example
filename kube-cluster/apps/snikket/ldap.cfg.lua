-- Authenticate against lldap instead of Snikket's own account store.
-- Loaded via SNIKKET_TWEAK_EXTRA_CONFIG. Full reasoning: ldap-integration.md.
--
-- The VirtualHost line is required, not decoration. Prosody's config is
-- sectional, and Snikket's Include of this file sits after two Component
-- blocks -- so bare assignments would land in the `share.` component and be
-- ignored, with no error and no log line.

VirtualHost "xmpp.example.com"
	-- The fork in this directory: mod_auth_ldap plus users() and admin sync.
	-- Named "lldap" and not "ldap" because "ldap" would name the file
	-- mod_auth_ldap.lua, colliding with the one the image already ships.
	authentication = "lldap"

	ldap_server = "lldap.lldap.svc.cluster.local:3890"
	ldap_base   = "ou=people,dc=lhns,dc=de"

	-- Must be in lldap's `lldap_password_manager` group: that grants directory
	-- search and the password writes in-band changes need. In no group, lldap
	-- shows the account only itself, and the symptom is "wrong password" rather
	-- than a permissions error.
	ldap_rootdn = "uid=snikket,ou=people,dc=lhns,dc=de"

	-- ENV_<NAME> is read from the environment (configmanager.lua:216), so the
	-- secret comes from a Kubernetes Secret and never lands in a config file.
	ldap_password = ENV_LDAP_BIND_PASSWORD

	-- Verify by binding as the user, so no password is stored here. "getpasswd"
	-- is impossible against lldap, which keeps nothing recoverable.
	ldap_mode = "bind"

	-- Who may log in. lldap treats memberOf as a real field and supports OR, and
	-- a bare group name works as well as a DN (as in apps/mosquitto).
	ldap_filter = "(&(uid=$user)(|(memberOf=xmpp)(memberOf=admin)))"

	-- Read by the fork: membership grants prosody:admin on login.
	ldap_admin_group = "admin"

	-- PLAIN must stay enabled, though Snikket disables it globally. Bind-mode
	-- LDAP verifies by binding, so it never holds the key material SCRAM needs
	-- and PLAIN is the only mechanism it can offer -- disable it and the server
	-- advertises nothing usable, so every login fails.
	--
	-- Safe because c2s_require_encryption is on and prosody will not offer PLAIN
	-- on an unencrypted stream unless the flag below says so. Pinned rather than
	-- left to its default, to keep the guarantee next to the concession.
	disable_sasl_mechanisms = { "OAUTHBEARER" }
	allow_unencrypted_plain_auth = false

	-- Account creation cannot work against a directory Snikket does not own. Left
	-- enabled, creating an invite still SUCCEEDS and only redemption fails, which
	-- hands someone a link that looks valid and dies on use.
	--
	-- `invites`, `invites_groups` and `invites_default_group` stay: circles and
	-- contact invites build on them and create no accounts.
	modules_disabled = { "invites_register", "invites_register_api", "register" }
