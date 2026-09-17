-- External components for the XMPP to SIP gateway (apps/cheogram-sip).
-- Loaded via SNIKKET_TWEAK_EXTRA_CONFIG, the same mechanism as ldap.cfg.lua.
--
-- component_interfaces MUST stay above the Component lines. A Component opens a
-- section that runs to the end of the file, so a global set after one is silently
-- ignored: no error, no log line, and the gateway simply never connects.

-- Prosody binds the external-component port to 127.0.0.1 by default, which no other
-- pod can reach. Widened so apps/cheogram-sip can connect. XEP-0114 is plaintext and
-- authenticates with the secrets below, so the port is deliberately absent from every
-- Service, IngressRoute and VIP: kube-vnet (vnet.yaml) is what keeps it reachable only
-- from the gateway.
component_interfaces = { "0.0.0.0" }

-- Asterisk's res_xmpp sends some stanzas with no `from` attribute. XEP-0114 requires
-- components to stamp it, and prosody enforces that by killing the stream with
-- <invalid-from/>. The gateway then reconnects, works for one call, and dies again, so
-- the symptom is "the first call arrives, later ones cannot be established".
--
-- This is global: prosody reads it in mod_component, so it relaxes `from` validation
-- for EVERY component. Acceptable only because both components here are ours, are
-- reachable solely from the gateway pod (vnet.yaml) with a secret, and have s2s
-- disabled. Remove it if a component ever comes from somewhere less trusted.
validate_from_addresses = false

-- User facing. Dial +49XXXXXXXXX@sip.xmpp.example.com for the phone network, or
-- them\40their-domain@sip.xmpp.example.com for a federated SIP address.
--
-- Not in DNS and not in snikket-tls. Components are not TLS endpoints, so prosody
-- logging "no certificate for sip.xmpp.example.com" is expected. Do not fix it by adding
-- a SAN: that means a new cert, a DNS-01 issuance and another restart of this pod.
Component "sip.xmpp.example.com"
	-- ENV_<NAME> is read from the environment (configmanager.lua:216), so the secret
	-- stays in the Secret and never lands in a config file.
	component_secret = ENV_SIP_COMPONENT_SECRET
	-- Without this, any federated user who learned the name could place calls on the
	-- trunk. Outbound is additionally restricted per line by the gateway's routing table
	-- (lines.<name>.callers), but that is a second line, not the first.
	modules_disabled = { "s2s" }

-- Asterisk's own component connection (chan_xmpp). Internal to the gateway.
Component "asterisk.xmpp.example.com"
	component_secret = ENV_ASTERISK_COMPONENT_SECRET
	modules_disabled = { "s2s" }
