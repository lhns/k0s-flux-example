-- Authenticate against lldap instead of Snikket's own account store (README, "Authentication").
-- Loaded via SNIKKET_TWEAK_EXTRA_CONFIG.

-- Re-opening the VirtualHost is required: Snikket sets `authentication` again inside it, and a
-- per-host setting beats a global one, so a bare assignment is ignored without a log line.

VirtualHost "xmpp.example.com"
	-- mod_auth_ldap plus users() and admin sync (artifacts/ldap-module.yaml). Not named "ldap":
	-- that is the module the image already ships.
	authentication = "lldap"

	ldap_server = "lldap.lldap.svc.cluster.local:3890"
	ldap_base   = "ou=people,dc=example,dc=com"

	-- In lldap's `lldap_password_manager` group: search plus the password writes clients make.
	-- In no group it sees only itself, and the symptom is "wrong password".
	ldap_rootdn = "uid=snikket,ou=people,dc=example,dc=com"

	-- ENV_<NAME> is os.getenv: the Secret never lands in a config file.
	ldap_password = ENV_LDAP_BIND_PASSWORD

	-- Verify by binding as the user. "getpasswd" is impossible: lldap keeps nothing recoverable.
	ldap_mode = "bind"

	-- Who may log in. A bare group name works as well as a DN.
	ldap_filter = "(&(uid=$user)(|(memberOf=xmpp)(memberOf=admin)))"

	-- Membership grants prosody:admin at the next login.
	ldap_admin_group = "admin"

	-- PLAIN must stay enabled though Snikket disables it: a bind never holds the key material
	-- SCRAM needs, so with PLAIN off no mechanism is advertised and every login fails. Safe:
	-- c2s_require_encryption is on, and PLAIN is not offered on an unencrypted stream unless the
	-- flag below says so; it is pinned here to keep that guarantee next to the concession.
	disable_sasl_mechanisms = { "OAUTHBEARER" }
	allow_unencrypted_plain_auth = false

	-- Account creation cannot work against a directory Snikket does not own. Left enabled,
	-- an invite is created fine and fails only on redemption. Circles and contact invites stay.
	modules_disabled = { "invites_register", "invites_register_api", "register" }
