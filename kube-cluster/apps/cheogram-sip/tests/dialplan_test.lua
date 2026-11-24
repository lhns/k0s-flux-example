-- Unit tests for the pure parts of ../extensions.lua, run under lua5.1 by run.sh
-- (Asterisk's Lua). os.getenv is replaced before the chunk loads (trunks and sip_routing
-- are built at load). Numbers are placeholders, never real subscribers.
--
-- $1 is the component dir, $2 the routing table as the chart RENDERS it (a fixture would
-- drift). run.sh runs it on the live table and with every `disabled` entry enabled.

local APP, ROUTING = ...
if not APP or APP == "" or not ROUTING or ROUTING == "" then
	print("usage: lua5.1 dialplan_test.lua <component dir> <rendered routing.lua>")
	os.exit(1)
end

local env = {
	SIP_HOST = "sip.example.net",
	SIP_ROUTING_FILE = ROUTING,
	SIP_SHORT_MAXLEN = "5",
	SIP_E164_COUNTRY = "49",
	COMPONENT_DOMAIN = "sip.xmpp.example.org",
	SIP_USER = "02012345678",
}
os.getenv = function(k) return env[k] end

app = { log = function() end }

dofile(APP .. "/extensions.lua")

local routing = sip_routing
if not routing then
	print("FAIL: the rendered routing table at " .. ROUTING .. " did not load")
	os.exit(1)
end

local HOME = routing.lines.home.primary
local OFFICE = routing.lines.office.primary
local EVENT = routing.lines.eventpbx.primary
local OFFICE2
for name, r in pairs(routing.registrations) do
	if r.line == "office" and name ~= OFFICE then OFFICE2 = name end
end
local OFFICE_NO = routing.registrations[OFFICE].contact_user
local OFFICE2_NO = routing.registrations[OFFICE2].contact_user
local HOME_NO = routing.registrations[HOME].contact_user
local OFFICE_DID = routing.registrations[OFFICE].did
local EVENT_NO = routing.registrations[EVENT].contact_user

-- eventpbx is disabled in values.yaml; its cases follow the table's flag. ev(on, off).
local ev_reg = routing.registrations[EVENT]
local EV_ON = ev_reg ~= nil and not ev_reg.disabled
local function ev(on, off)
	if EV_ON then return on end
	return off
end

local failures = 0
local function check(name, got, want)
	if got ~= want then
		failures = failures + 1
		print(string.format("FAIL %s: got [%s] want [%s]", name, tostring(got), tostring(want)))
	else
		print("ok   " .. name)
	end
end

local function component_to(localpart)
	if not localpart:find("@") then localpart = localpart .. "@" .. env.SIP_HOST end
	return local_to_sip_uri(localpart)
end

local permitted = "xmpp:someone@xmpp.example.com"

local function case(name, localpart, want_to, want_label, want_peer, want_exten, want_kind)
	local to, label, peer, exten, _, kind = resolve_destination(component_to(localpart), permitted)
	check(name .. " to", to, want_to)
	check(name .. " label", label, want_label)
	check(name .. " peer", peer, want_peer)
	check(name .. " exten", exten, want_exten)
	check(name .. " kind", kind, want_kind)
end

case("bare number", "015112345678",
	"015112345678", nil, HOME, "015112345678", "routed")

case("short extension", "5550",
	"5550", nil, ev(EVENT), "5550", ev("routed", "disabled"))

-- A trunk label names a HOST: it resolves through that host's default registration
-- (credentials).
case("roster contact, pstn trunk", "+4915112345678#sip.example.net",
	"+4915112345678@sip.example.net", "sip.example.net", HOME, "+4915112345678", "trunk")

case("roster contact, other trunk", "5550#voip.example.org",
	"5550@voip.example.org", "voip.example.org", ev(EVENT), "5550",
	ev("trunk", "unknown"))

-- Peers are named after their registration, not the host: DIDs share a host and the number
-- is pinned per section.
case("line label", "+4915112345678#office",
	"+4915112345678@office", "office", OFFICE, "+4915112345678", "line")
case("line label, other line", "+4915112345678#eventpbx",
	"+4915112345678@eventpbx", "eventpbx", ev(EVENT), "+4915112345678",
	ev("line", "disabled"))
case("line label case-insensitive", "+4915112345678#OFFICE",
	"+4915112345678@OFFICE", "OFFICE", OFFICE, "+4915112345678", "line")

-- Neither trunk nor line FAILS CLOSED: a DNS dial may get a recorded refusal that looks
-- like a connected call.
case("unknown label fails closed", "alice#pbx.example.net",
	"alice@pbx.example.net", "pbx.example.net", nil, "alice", "unknown")
case("near-miss trunk label fails closed", "+4915112345678#sip.example.net.evil.example",
	"+4915112345678@sip.example.net.evil.example", "sip.example.net.evil.example", nil,
	"+4915112345678", "unknown")

case("autohost case-insensitive", "+4915112345678#SIP.EXAMPLE.NET",
	"+4915112345678@SIP.EXAMPLE.NET", "SIP.EXAMPLE.NET", HOME, "+4915112345678", "trunk")

-- emergency is the only way past a line's callers list: assert it on every case.
local function routed(name, localpart, from, want_peer, want_line, want_emergency)
	local _, _, peer, _, rule, _, line = resolve_destination(component_to(localpart),
		from and ("xmpp:" .. from) or permitted)
	check(name .. " peer", peer, want_peer)
	check(name .. " line", line, want_line)
	check(name .. " emergency", rule ~= nil and rule.emergency == true, want_emergency)
end

-- 110 also matches the short rule: reaching PSTN is first-match-wins, so this rule sits
-- first.
routed("110 from a stranger", "110", "nobody@elsewhere.example",
	HOME, "home", true)
routed("112", "112", "admin@xmpp.example.com", HOME, "home", true)

routed("0112345 is not an emergency number", "0112345", "admin@xmpp.example.com",
	HOME, "home", false)
routed("1102 is not an emergency number", "1102", "admin@xmpp.example.com",
	ev(EVENT), "eventpbx", false)

routed("five digits", "12345", "admin@xmpp.example.com", ev(EVENT), "eventpbx", false)
routed("six digits", "123456", "admin@xmpp.example.com", HOME, "home", false)

routed("office caller presents office", "015112345678", "office@xmpp.example.com",
	OFFICE, "office", false)
routed("anyone else still gets the household line", "015112345678", "admin@xmpp.example.com",
	HOME, "home", false)
routed("an excluded caller falls past the from_glob rule", "015112345678", nil,
	HOME, "home", false)

sip_routing = nil
routed("no routing table", "015112345678", "admin@xmpp.example.com", nil, nil, false)
routed("no routing table, emergency", "110", "admin@xmpp.example.com", nil, nil, false)
case("no routing table, line label", "+4915112345678#office",
	"+4915112345678@office", "office", nil, "+4915112345678", "unknown")
sip_routing = routing

local glob_tbl = {
	registrations = {
		["reg-a"] = {host = "trunk-a", line = "a"},
		["reg-b"] = {host = "trunk-b", line = "b"},
	},
	lines = {
		a = {primary = "reg-a", callers = {"xmpp:*@xmpp.example.org"}},
		b = {primary = "reg-b", callers = {"xmpp:*@xmpp.example.org"}},
	},
	outbound = {
		{to_pattern = "^(.*)$", line = "a", from_glob = {"xmpp:bob@xmpp.example.org"}},
		{to_pattern = "^(.*)$", line = "b"},
	},
}
local function narrowed(name, from, want)
	local _, reg = routing_match(glob_tbl, "5551234", from)
	check(name, reg and reg.host, want)
end
narrowed("from_glob picks the narrowed rule", "xmpp:bob@xmpp.example.org", "trunk-a")
narrowed("another caller falls to the next rule", "xmpp:ann@xmpp.example.org", "trunk-b")
narrowed("unidentified caller falls to the next rule", nil, "trunk-b")

-- A rule naming an undefined line/registration must NOT fall through to a later rule.
check("undefined line fails closed",
	routing_match({registrations = {}, lines = {},
		outbound = {{to_pattern = "^(.*)$", line = "gone"}}}, "5551234", nil),
	nil)
check("undefined pinned registration fails closed",
	routing_match({registrations = {}, lines = {a = {primary = "reg-a"}},
		outbound = {{to_pattern = "^(.*)$", registration = "gone", emergency = true}}},
		"5551234", nil),
	nil)
check("line with a dangling primary fails closed",
	routing_match({registrations = {}, lines = {a = {primary = "reg-a"}},
		outbound = {{to_pattern = "^(.*)$", line = "a"}}}, "5551234", nil),
	nil)

-- A DISABLED primary refuses; no fall-through to the next rule's trunk.
local off_tbl = {
	registrations = {
		["reg-a"] = {host = "trunk-a", line = "a", disabled = true},
		["reg-b"] = {host = "trunk-b", line = "b"},
	},
	lines = {
		a = {primary = "reg-a", callers = {"*"}},
		b = {primary = "reg-b", callers = {"*"}},
	},
	outbound = {
		{to_pattern = "^(%d%d?%d?%d?%d?)$", line = "a"},
		{to_pattern = "^(.*)$", line = "b"},
	},
}
sip_routing = off_tbl
local function off(name, to, want_peer, want_kind)
	local _, _, peer, _, _, kind = resolve_destination(to, permitted)
	check(name .. " peer", peer, want_peer)
	check(name .. " kind", kind, want_kind)
end
off("a short extension on a disabled line is refused", "5550", nil, "disabled")
off("so is its line label", "015112345678@a", nil, "disabled")
off("other lines are untouched", "015112345678", "reg-b", "routed")
check("the refusal names the line", refusal_reason("disabled", "a"),
	"line 'a' is disabled in the routing table")
local off_peer, _, _, off_cause, off_reason = matrix_outbound_route("5550", "sip-5550", permitted)
check("the matrix path refuses it", off_peer, nil)
check("with 503", off_cause, 34)
check("and says why", tostring(off_reason):find("is disabled", 1, true) ~= nil, true)
local sms_off, _, sms_off_reason = sms_outbound_route("5550", permitted)
check("the sms path refuses it", sms_off, nil)
check("and says why", tostring(sms_off_reason):find("is disabled", 1, true) ~= nil, true)
sip_routing = routing
check("a disabled registration attributes nothing by endpoint",
	(inbound_line(off_tbl, nil, "reg-a")), nil)
check("nor as the sole registration on its host", (inbound_line(off_tbl, nil, "trunk-a")), nil)

local function globbed(name, glob, addr, want)
	check(name, routing_glob_match(glob, addr), want)
end
globbed("* is anyone", "*", "xmpp:a@b.example.org", true)
globbed("transport wildcard", "xmpp:*", "xmpp:a@b.example.org", true)
globbed("transport wildcard is per transport", "xmpp:*", "matrix:@a:b.example.org", false)
globbed("domain wildcard", "xmpp:*@xmpp.example.org", "xmpp:bob@xmpp.example.org", true)
globbed("domain wildcard is per domain", "xmpp:*@xmpp.example.org", "xmpp:bob@other.example", false)
globbed("matrix server wildcard", "matrix:@*:example.org", "matrix:@bob:example.org", true)
globbed("literal is case-insensitive", "xmpp:bob@xmpp.example.org", "xmpp:BOB@XMPP.EXAMPLE.ORG", true)
globbed("no caller matches nothing", "xmpp:*@xmpp.example.org", nil, false)

-- Inbound identity: the dialled contact is the one this registration gave the carrier, so it
-- names one registration and therefore one line. When the carrier stops echoing it, the
-- host's default registration answers and per-DID inbound degrades to one line.
local function attributed(name, extension, peername, want_line)
	check(name, (inbound_line(routing, extension, peername)), want_line)
end
attributed("contact picks the office line", OFFICE_NO, "sip.example.net", "office")
attributed("the other office DID, same line", OFFICE2_NO, "sip.example.net", "office")
attributed("entrypoint contact picks the household line", HOME_NO, "sip.example.net", "home")
attributed("unknown contact falls back to the entrypoint's line", "someone@example.net",
	"sip.example.net", "home")
attributed("the household endpoint is the household line", nil, HOME, "home")
attributed("an office endpoint is the office line", nil, OFFICE, "office")
attributed("the other office endpoint too", nil, OFFICE2, "office")
attributed("the eventpbx endpoint is its own line", nil, EVENT, ev("eventpbx"))
attributed("no contact at all falls back the same way", nil, "sip.example.net", "home")
attributed("sole registration on a host", nil, "voip.example.org", ev("eventpbx"))
attributed("unknown peer has no line", HOME_NO, "pbx.example.net", nil)
-- The endpoint OUTRANKS the dialled contact (the Request-URI is whatever the carrier sent).
attributed("the endpoint outranks a contact naming another DID", OFFICE2_NO,
	OFFICE, "office")
attributed("including one naming the household DID", HOME_NO,
	OFFICE, "office")
attributed("no peer has no line", HOME_NO, nil, nil)

-- HOW it was attributed. A fallback looks like a correct match downstream and logs nothing, so
-- the carrier quietly dropping our Contact would show up as "all calls ring the wrong line".
-- Only the host's default endpoint carries a type=identify, so an endpoint match elsewhere
-- cannot have been the source-address fallback; the default endpoint's own match could.
local function attribution(name, extension, peername, want)
	local _, _, _, how = inbound_line(routing, extension, peername)
	check(name, how, want)
end
attribution("a contact match says so", OFFICE_NO, "sip.example.net", "contact")
attribution("the entrypoint fallback is marked", "someone@example.net", "sip.example.net", "entrypoint")
attribution("an endpoint match is a match", nil, OFFICE, "endpoint")
attribution("even against a contact naming another DID", HOME_NO,
	OFFICE, "endpoint")
attribution("both agreeing is still a contact match", OFFICE_NO,
	OFFICE, "contact")
attribution("the default endpoint may always be the source-address fallback", nil,
	HOME, "entrypoint")
attribution("and says so even on its own contact only when the contact matches",
	HOME_NO, HOME, "contact")
attribution("the sole-registration fallback is marked", "someone@example.net",
	"voip.example.org", ev("sole"))
-- Asking for the default outright (the message path has a host but no contact) is a fallback.
attribution("no contact is the fallback too", nil, "sip.example.net", "entrypoint")
attribution("an unknown peer attributes nothing", HOME_NO, "pbx.example.net", nil)

-- The LINE fills the slot after "#"; keep it symmetric with resolve_destination or contacts
-- cannot be called back.
check("from uri carries the line",
	call_from_uri("<sip:+4915112345678@10.20.176.78>", "sip.example.net", "office"),
	"<sip:+4915112345678@office>")
check("from uri as a JID localpart",
	sip_uri_to_local("+4915112345678@office"), "+4915112345678#office")
check("and back", local_to_sip_uri("+4915112345678#office"), "+4915112345678@office")
check("from uri without a line falls back to the trunk",
	call_from_uri("<sip:+4915112345678@10.20.176.78>", "sip.example.net", nil),
	"<sip:+4915112345678@sip.example.net>")
check("from uri with neither is left alone",
	call_from_uri("<sip:+4915112345678@10.20.176.78>", "pbx.example.net", nil),
	"<sip:+4915112345678@10.20.176.78>")

check("conference name carries the line",
	matrix_conference_name("<sip:+4915112345678@office>", "office"),
	"sip-office-+4915112345678")
-- Split on ^(.*)-([0-9]+)$ so line names may contain hyphens; "-" not "_" (mautrix escapes
-- underscore).
local conf_line, conf_digits = ("office-main-+4915112345678"):match("^(.*)-(%+?[0-9]+)$")
check("portal id splits on the trailing digits", conf_line, "office-main")
check("portal id digits keep the plus", conf_digits, "+4915112345678")
check("conference name needs a line",
	matrix_conference_name("<sip:+4915112345678@office>", nil), nil)
check("conference name needs e164",
	matrix_conference_name("<sip:5551@eventpbx>", "eventpbx"), nil)
-- Must survive the kick's character class, or a Matrix call joins a conference that cannot
-- be ended.
check("line conference is kickable",
	matrix_kick_conference(matrix_conference_name("<sip:+4915112345678@office>", "office")),
	"sip-office-+4915112345678")

local matrix_caller = "matrix:@admin:example.com"

local function route(name, exten, conf, want_peer, want_num, want_conf, want_cause)
	local peer, num, gotconf, cause = matrix_outbound_route(exten, conf, matrix_caller)
	check(name .. " peer", peer, want_peer)
	check(name .. " num", num, want_num)
	check(name .. " conf", gotconf, want_conf)
	check(name .. " cause", cause, want_cause)
end

check("no caller is refused", (matrix_outbound_route("+4915112345678", "sip-4915112345678", nil)), nil)
check("an unlisted matrix caller is refused",
	(matrix_outbound_route("+4915112345678", "sip-4915112345678", "matrix:@a:example.net")), nil)

route("e164 to default trunk", "+4915112345678", "sip-4915112345678",
	HOME, "+4915112345678", "sip-4915112345678", nil)

route("request uri host ignored", "+4915112345678@cheogram-sip-internal.example.svc",
	"sip-4915112345678", HOME, "+4915112345678", "sip-4915112345678", nil)

route("short extension keeps the short trunk", "5550", "sip-5550",
	ev(EVENT), ev("5550"), ev("sip-5550"), ev(nil, 34))

route("00 prefix normalised", "004915112345678", "sip-4915112345678",
	HOME, "+4915112345678", "sip-4915112345678", nil)

route("not a number", "alice@pbx.example.net", "sip-4915112345678", nil, nil, nil, 1)
route("no conference header", "+4915112345678", nil, nil, nil, nil, 1)
route("empty conference header", "+4915112345678", "", nil, nil, nil, 1)
route("conference name with a comma", "+4915112345678", "sip-49151,app,Echo",
	nil, nil, nil, 1)

route("conference header trimmed", "+4915112345678", "  sip-4915112345678 ",
	HOME, "+4915112345678", "sip-4915112345678", nil)

sip_routing = nil
route("no routing table", "+4915112345678", "sip-4915112345678", nil, nil, nil, 34)
sip_routing = routing

route("office room uses the office line", "+4915555550101", "sip-office-+4915555550101",
	OFFICE, "+4915555550101", "sip-office-+4915555550101", nil)
route("home room uses the home line", "+4915555550101", "sip-home-+4915555550101",
	HOME, "+4915555550101", "sip-home-+4915555550101", nil)
route("emergency from an office room", "112", "sip-office-+4915555550101",
	HOME, "112", "sip-office-+4915555550101", nil)
route("legacy conference is routed by number", "+4915555550101", "sip-4915555550101",
	HOME, "+4915555550101", "sip-4915555550101", nil)
route("unknown room line is refused", "+4915555550101", "sip-nope-+4915555550101",
	nil, nil, nil, 34)
route("room on a disabled line", "+4915555550101", "sip-eventpbx-+4915555550101",
	ev(EVENT), ev("+4915555550101"), ev("sip-eventpbx-+4915555550101"), ev(nil, 34))
local gate_tbl = {registrations = routing.registrations, outbound = routing.outbound,
	lines = {home = routing.lines.home,
		office = {primary = routing.lines.office.primary, callers = {"xmpp:*@xmpp.example.com"}}}}
sip_routing = gate_tbl
route("room line not permitting the caller", "+4915555550101", "sip-office-+4915555550101",
	nil, nil, nil, 34)
route("emergency ignores the room line's callers", "112", "sip-office-+4915555550101",
	HOME, "112", "sip-office-+4915555550101", nil)
sip_routing = routing

check("cause BUSY", matrix_outbound_cause("BUSY"), 17)
check("cause CONGESTION", matrix_outbound_cause("CONGESTION"), 34)
check("cause HANGUP", matrix_outbound_cause("HANGUP"), 19)
check("cause FAILED", matrix_outbound_cause("FAILED"), 34)
check("cause UNKNOWN", matrix_outbound_cause("UNKNOWN"), 34)
check("cause nil", matrix_outbound_cause(nil), 34)

check("kick conference", matrix_kick_conference("sip-4915112345678"), "sip-4915112345678")
check("kick no conference", matrix_kick_conference(nil), nil)
check("kick empty conference", matrix_kick_conference(""), nil)
check("kick skips endmarked", matrix_kick_conference("sip-4915112345678", "ENDMARKED"), nil)
check("kick skips kicked", matrix_kick_conference("sip-4915112345678", "KICKED"), nil)
check("kick after a plain hangup", matrix_kick_conference("sip-4915112345678", "HANGUP"),
	"sip-4915112345678")
check("kick rejects a comma", matrix_kick_conference("sip-1,participants"), nil)
check("kick rejects a quote", matrix_kick_conference("sip-1' ; rm -rf /"), nil)
check("kick rejects a space", matrix_kick_conference("sip-1 all"), nil)
check("kick rejects a semicolon", matrix_kick_conference("sip-1;id"), nil)
check("kick rejects a dollar", matrix_kick_conference("sip-$(id)"), nil)
local _, _, routed = matrix_outbound_route("+4915112345678", "sip-4915112345678", matrix_caller)
check("routed conference is kickable", matrix_kick_conference(routed), "sip-4915112345678")

check("sip_uri_to_local", sip_uri_to_local("alice@pbx.example.net"), "alice#pbx.example.net")
check("local_to_sip_uri", local_to_sip_uri("alice#pbx.example.net"), "alice@pbx.example.net")

local function inbound(name, header, want_uri, want_digits)
	local uri, digits = sms_inbound_from(header)
	check(name .. " uri", uri, want_uri)
	check(name .. " digits", digits, want_digits)
end

-- The host is REWRITTEN to the LINE, else one human is "+49...#<sbc-ip>" for an SMS and
-- "+49...#home" for a call.
inbound("plain e164", "<sip:+4915112345678@10.20.176.78>",
	"+4915112345678@home", "4915112345678")
inbound("display name and params",
	'"Someone" <sip:+4915112345678@10.20.176.78;user=phone>;tag=abc123',
	"+4915112345678@home", "4915112345678")
inbound("no angle brackets", "sip:+4915112345678@10.20.176.78;tag=abc123",
	"+4915112345678@home", "4915112345678")
inbound("uppercase scheme", "<SIP:+4915112345678@10.20.176.78>",
	"+4915112345678@home", "4915112345678")
inbound("00 prefix", "<sip:004915112345678@10.20.176.78>",
	"+4915112345678@home", "4915112345678")
inbound("national 0 prefix", "<sip:015112345678@10.20.176.78>",
	"+4915112345678@home", "4915112345678")
inbound("known trunk names its line", "<sip:5551@voip.example.org>",
	ev("5551@eventpbx", "5551@home"), nil)
inbound("alphanumeric sender", "<sip:Telekom@10.20.176.78>", "Telekom@home", nil)
inbound("short code", "<sip:22122@10.20.176.78>", "22122@home", nil)

local saved_host = env.SIP_HOST
env.SIP_HOST = nil
inbound("nothing to attribute it to", "<sip:+4915112345678@10.20.176.78>",
	"+4915112345678@10.20.176.78", nil)
env.SIP_HOST = saved_host

-- THE invariant: a call and an SMS from one number yield the same key (both via
-- matrix_conference_name).
local function same_portal(name, header)
	local _, digits, line = sms_inbound_from(header)
	local call_line = inbound_line(routing, nil, env.SIP_HOST)
	local conf = matrix_conference_name(call_from_uri(header, env.SIP_HOST, call_line), call_line)
	check(name .. " line", line, call_line)
	check(name .. " portal", conf, digits and ("sip-" .. line .. "-+" .. digits))
end
same_portal("e164", "<sip:+4915112345678@10.20.176.78>")
same_portal("00 prefix", "<sip:004915112345678@10.20.176.78>")
same_portal("national 0 prefix", "<sip:015112345678@10.20.176.78>")
same_portal("display name and params",
	'"Someone" <sip:+4915112345678@10.20.176.78;user=phone>;tag=abc123')
same_portal("alphanumeric sender", "<sip:Telekom@10.20.176.78>")
same_portal("short code", "<sip:22122@10.20.176.78>")

-- Whether a carrier echoes our Contact on a MESSAGE is untested; non-matches must land
-- where they did.
local function sms_line(name, extension, want_uri, want_line, want_how)
	local uri, _, line, _, how = sms_inbound_from("<sip:+4915112345678@10.20.176.78>", extension)
	check(name .. " uri", uri, want_uri)
	check(name .. " line", line, want_line)
	check(name .. " how", how, want_how)
end
sms_line("contact picks the office line", OFFICE_NO,
	"+4915112345678@office", "office", "contact")
sms_line("the other office DID, same line", OFFICE2_NO,
	"+4915112345678@office", "office", "contact")
sms_line("the entrypoint contact picks the household line", HOME_NO,
	"+4915112345678@home", "home", "contact")
sms_line("an unmatched contact falls back as before", "someone@example.net",
	"+4915112345678@home", "home", "entrypoint")
sms_line("no contact falls back as before", nil, "+4915112345678@home", "home", "entrypoint")
sms_line("an empty contact falls back as before", "", "+4915112345678@home", "home", "entrypoint")

local function sms_matches_call(name, extension)
	local _, _, sms = sms_inbound_from("<sip:+4915112345678@10.20.176.78>", extension)
	check(name, sms, (inbound_line(routing, extension, env.SIP_HOST)))
end
sms_matches_call("office DID", OFFICE_NO)
sms_matches_call("entrypoint DID", HOME_NO)
sms_matches_call("unmatched contact", "someone@example.net")
sms_matches_call("no contact", nil)

-- The "+" in the From keeps the bridge from dropping the message while answering 200 OK.
local to, from = sms_bridge_message("+4915112345678@home", "4915112345678")
check("bridge to", to,
	"pjsip:matrixbridge/sip:+492012345678@matrix-sip-bridge.matrix.svc.cluster.local")
local did_to = sms_bridge_message("+4915112345678@office", "4915112345678",
	{did = OFFICE_DID})
check("bridge to uses the registration's did", did_to,
	"pjsip:matrixbridge/sip:" .. OFFICE_DID .. "@matrix-sip-bridge.matrix.svc.cluster.local")
check("bridge from", from, "sip:+4915112345678@home")
check("bridge skipped without digits", sms_bridge_message("Telekom@sip.example.net", nil), nil)

-- Outbound needs BOTH the bridge as sender AND a numeric destination: From alone is
-- spoofable by a carrier.
check("dispatch bridge + number", sms_is_outbound("<sip:matrixbridge@10.20.176.78>", "+4915112345678"), true)
check("dispatch bridge + jid", sms_is_outbound("<sip:matrixbridge@10.20.176.78>", "admin@xmpp.example.com"), false)
check("dispatch carrier + number", sms_is_outbound("<sip:+4915112345678@10.20.176.78>", "+492012345678"), false)
check("dispatch carrier + jid", sms_is_outbound("<sip:+4915112345678@10.20.176.78>", "admin@xmpp.example.com"), false)
check("dispatch bridge with display name",
	sms_is_outbound('"bridge" <sip:matrixbridge@10.20.176.78>;tag=x', "+4915112345678"), true)

local function sms_route(name, to_uri, want_peer, want_num)
	local peer, num, reason = sms_outbound_route(to_uri, matrix_caller)
	check(name .. " peer", peer, want_peer)
	check(name .. " num", num, want_num)
	check(name .. " reason", reason == nil, want_peer ~= nil)
end
sms_route("e164 to default trunk", "+4915112345678", HOME, "+4915112345678")
sms_route("request uri host ignored", "sip:+4915112345678@cheogram-sip-internal.example.svc:5060",
	HOME, "+4915112345678")
sms_route("short extension keeps the short trunk", "5550", ev(EVENT), ev("5550"))
sms_route("00 prefix normalised", "004915112345678", HOME, "+4915112345678")
check("sms with no caller is refused", (sms_outbound_route("+4915112345678", nil)), nil)
check("sms from an unlisted caller is refused",
	(sms_outbound_route("+4915112345678", "matrix:@someone:example.net")), nil)
sms_route("not a number", "alice@pbx.example.net", nil, nil)
sms_route("empty destination", "", nil, nil)
sip_routing = nil
sms_route("no routing table", "+4915112345678", nil, nil)
sip_routing = routing

local function xmpp_route(name, to_uri, from, want_peer, want_num)
	local peer, num = sms_outbound_route(local_to_sip_uri(to_uri), from)
	check(name .. " peer", peer, want_peer)
	check(name .. " num", num, want_num)
end
xmpp_route("xmpp sender reaches the trunk", "+4915112345678", permitted,
	HOME, "+4915112345678")
xmpp_route("an unlisted xmpp domain is refused", "+4915112345678",
	"xmpp:someone@elsewhere.example", nil, nil)
xmpp_route("an unidentified xmpp sender is refused", "+4915112345678", nil, nil, nil)
xmpp_route("a #label resolves through the table", "+4915112345678#home", permitted,
	HOME, "+4915112345678")

local plan_from = "<sip:+4915112345678@home>"
local plan_conf, plan_caller = matrix_conference_name(plan_from, "home")

local function legs(name, stage, want_legs, want_matrix)
	local got, got_matrix = inbound_legs(stage, plan_from, plan_conf, plan_caller)
	check(name, got, want_legs)
	check(name .. " (matrix)", got_matrix, want_matrix)
end

local xmpp_leg = "Motif/jingle-endpoint/" .. make_jid("admin@xmpp.example.com", plan_from)
local function hex(s) return (s:gsub(".", function(c) return string.format("%02x", c:byte()) end)) end
local function person_leg(mxid) return "Local/" .. hex(mxid) .. "@matrix-leg/n" end
local matrix_leg = person_leg("@admin:example.com")
check("hex of an mxid", hex("@a:b"), "40613a62")

legs("xmpp only", {ring = {"xmpp:admin@xmpp.example.com"}}, xmpp_leg, false)
legs("xmpp and matrix ring together", {ring = {"xmpp:admin@xmpp.example.com", "matrix:@admin:example.com"}},
	xmpp_leg .. "&" .. matrix_leg, true)
legs("several matrix targets are several legs",
	{ring = {"matrix:@admin:example.com", "matrix:@other:example.com"}},
	matrix_leg .. "&" .. person_leg("@other:example.com"), true)
legs("the same person twice is one leg, case-insensitively",
	{ring = {"matrix:@admin:example.com", "matrix:@ADMIN:example.com"}}, matrix_leg, true)
legs("a bare matrix: is one default leg", {ring = {"matrix:"}}, "Local/default@matrix-leg/n", true)
legs("a bare matrix: and a person are two legs", {ring = {"matrix:", "matrix:@admin:example.com"}},
	"Local/default@matrix-leg/n&" .. matrix_leg, true)
legs("a matrix pattern is no leg", {ring = {"matrix:@*:example.com"}}, nil, false)
legs("two xmpp targets are two legs", {ring = {"xmpp:a@example.net", "xmpp:b@example.net"}},
	"Motif/jingle-endpoint/" .. make_jid("a@example.net", plan_from) .. "&"
	.. "Motif/jingle-endpoint/" .. make_jid("b@example.net", plan_from), false)
legs("an unparseable address contributes nothing", {ring = {"tel:+4915112345678"}}, nil, false)
legs("a stage with no ring", {}, nil, false)
legs("not a stage", nil, nil, false)

local no_conf = inbound_legs({ring = {"xmpp:admin@xmpp.example.com", "matrix:@admin:example.com"}},
	plan_from, nil, nil)
check("no conference means no matrix leg", no_conf, xmpp_leg)
-- The stage rings nobody, like an unanswered ring; the handler NOTICEs on nil.
check("a matrix-only stage without a conference rings nobody",
	(inbound_legs({ring = {"matrix:@admin:example.com"}}, plan_from, nil, nil)), nil)

local vars, dialled_calls, sent, hung_up = {}, {}, {}, 0
local function chan_var(name)
	return {
		get = function() return vars[name] end,
		set = function(_, v) vars[name] = v end,
	}
end
channel = setmetatable({}, {__index = function(_, k)
	local name = k:gsub("^__", "")
	return setmetatable(chan_var(name), {__call = function(_, first, ...)
		local sub = {...}
		return chan_var(name .. "(" .. tostring(first) .. (sub[1] and "," .. sub[1] or "") .. ")")
	end})
end})
app = {
	log = function() end,
	dial = function(dest, timeout, opts) dialled_calls[#dialled_calls + 1] = {dest, timeout, opts} end,
	MessageSend = function(to, from)
		local who = vars["MESSAGE_DATA(X-Matrix-Recipient)"]
		sent[#sent + 1] = {to = to, from = from, who = who ~= "" and who or nil}
	end,
	hangup = function() hung_up = hung_up + 1 end,
}
-- Handlers wrap bodies in pcall; report what it swallows so bugs are not hidden.
local real_pcall = pcall
pcall = function(f, ...)
	local ok, err = real_pcall(f, ...)
	if not ok then
		failures = failures + 1
		print("FAIL swallowed error: " .. tostring(err))
	end
	return ok, err
end
local function reset_pbx() vars, dialled_calls, sent, hung_up = {}, {}, {}, 0 end

reset_pbx()
vars.MATRIX_CALLER_PENDING = plan_caller
extensions["matrix-leg"]["_[0-9a-f]."]("matrix-leg", hex("@user:example.com"))
check("matrix-leg dials the bridge", dialled_calls[1] and dialled_calls[1][1],
	"PJSIP/" .. plan_caller .. "@matrixbridge")
check("matrix-leg uses a pre-dial handler", dialled_calls[1] and dialled_calls[1][3],
	"b(add-leg-headers^s^1)")
check("matrix-leg names the recipient", vars.MATRIX_RECIPIENT, "@user:example.com")

reset_pbx()
vars.MATRIX_CALLER_PENDING = plan_caller
extensions["matrix-leg"]["default"]("matrix-leg", "default")
check("the default leg dials the bridge too", dialled_calls[1] and dialled_calls[1][1],
	"PJSIP/" .. plan_caller .. "@matrixbridge")
check("the default leg names no recipient", vars.MATRIX_RECIPIENT, "")

reset_pbx()
vars.MATRIX_CALLER_PENDING = plan_caller
extensions["matrix-leg"]["_[0-9a-f]."]("matrix-leg", "zz")
check("a bad extension dials nothing", #dialled_calls, 0)
check("and hangs up", hung_up, 1)

local function with_headers(endpoint, conf, who)
	reset_pbx()
	vars["CHANNEL(endpoint)"] = endpoint
	vars.MATRIX_CONF_PENDING = conf
	vars.MATRIX_RECIPIENT = who
	extensions["add-leg-headers"]["s"]("add-leg-headers", "s")
	return vars["PJSIP_HEADER(add,X-Conference)"], vars["PJSIP_HEADER(add,X-Matrix-Recipient)"]
end
local c1, r1 = with_headers("matrixbridge", plan_conf, "@user:example.com")
check("the conference header is set", c1, plan_conf)
check("the recipient header is set", r1, "@user:example.com")
local c2, r2 = with_headers("matrixbridge", plan_conf, "")
check("the default leg still has the conference", c2, plan_conf)
check("and no recipient header", r2, nil)
local c3, r3 = with_headers("other", plan_conf, "@user:example.com")
check("no headers on a channel that is not the bridge", (c3 or "") .. (r3 or ""), "")

local function answered(name)
	reset_pbx()
	vars["CHANNEL(name)"] = name
	extensions["matrix-answered"]["s"]("matrix-answered", "s")
	return vars["MASTER_CHANNEL(MATRIX_ANSWERED)"], vars.GOSUB_RESULT
end
local a1, g1 = answered("Local/" .. hex("@user:example.com") .. "@matrix-leg-00000012;1")
check("a matrix-leg answer marks the call", a1, "1")
check("and ends that leg", g1, "CONTINUE")
local a2 = answered("Local/default@matrix-leg-00000013;1")
check("so does the default leg", a2, "1")
local a3, g3 = answered("Motif/jingle-endpoint-00000014")
check("an xmpp answer is left alone", a3, nil)
check("and keeps its bridge to the caller", g3, nil)
check("another Local context is not a matrix leg", (answered("Local/x@other-00000015;1")), nil)

local function recipients(line) return sms_matrix_recipients(routing, line) end
local function names(list)
	local out = {}
	for i, w in ipairs(list) do out[i] = w or "default" end
	return table.concat(out, ",")
end
check("home texts only the person its plan rings", names(recipients("home")), "@user:example.com")
check("office texts its owner", names(recipients("office")), "@admin:example.com")
check("a plan with literal targets lists each once, in order", names(sms_matrix_recipients(
	{lines = {l = {inbound_route = {{ring = {"matrix:@b:x.de", "xmpp:a@b"}},
		{ring = {"matrix:@a:x.de", "matrix:@B:x.de"}}}}}}, "l")), "@b:x.de,@a:x.de")
check("a bare matrix: is the default recipient", names(sms_matrix_recipients(
	{lines = {l = {inbound_route = {{ring = {"matrix:@a:x.de", "matrix:"}}}}}}, "l")),
	"@a:x.de,default")
check("a plan with no Matrix target still reaches the bridge's default",
	names(sms_matrix_recipients({lines = {l = {inbound_route = {{ring = {"xmpp:a@b"}}}}}}, "l")),
	"default")
check("an unknown line reaches the default", names(recipients("nosuchline")), "default")

local function deliver_on(tbl, line_name)
	local saved = sip_routing
	sip_routing = tbl
	reset_pbx()
	local reg, regname
	for n, r in pairs(routing.registrations) do
		if r.line == line_name and not r.disabled and not reg then reg, regname = r, n end
	end
	local out = {xmpp = {}, bridge = {}}
	if reg then
		vars["CHANNEL(endpoint)"] = regname
		vars["MESSAGE(from)"] = "<sip:+4915112345678@sip.example.net>"
		extensions["messages-in"]["_."]("messages-in", reg.contact_user)
		for _, m in ipairs(sent) do
			if m.to:find("^xmpp:") then
				out.xmpp[#out.xmpp + 1] = m.to
			elseif m.to:find("^pjsip:matrixbridge/") then
				out.bridge[#out.bridge + 1] = m.who or "default"
			end
		end
	end
	sip_routing = saved
	return reg ~= nil, out
end
local function line_with(name, stages)
	return {bridge_host = routing.bridge_host, registrations = routing.registrations,
		outbound = routing.outbound,
		lines = {[name] = {primary = routing.lines[name].primary,
			callers = routing.lines[name].callers, inbound_route = stages}}}
end

local ok_home, two = deliver_on(
	line_with("home", {{ring = {"matrix:@a:example.com", "matrix:@b:example.com"}}}), "home")
check("home registration found", ok_home, true)
check("one bridge MESSAGE per recipient, each naming them", table.concat(two.bridge, ","),
	"@a:example.com,@b:example.com")
check("no xmpp target in the plan means no xmpp send", #two.xmpp, 0)

local _, mixed = deliver_on(line_with("home", {{ring = {"matrix:@a:example.com", "matrix:"}}}), "home")
check("a literal and the default recipient", table.concat(mixed.bridge, ","), "@a:example.com,default")

local _, live_home = deliver_on(routing, "home")
check("home texts no xmpp", #live_home.xmpp, 0)
check("home texts only user", table.concat(live_home.bridge, ","), "@user:example.com")
local _, live_office = deliver_on(routing, "office")
check("office texts one xmpp target", #live_office.xmpp, 1)
check("and it is admin", live_office.xmpp[1] ~= nil and live_office.xmpp[1]:find("admin", 1, true) ~= nil, true)
check("office texts lhns on matrix", table.concat(live_office.bridge, ","), "@admin:example.com")

check("xmpp targets are distinct", table.concat(sms_xmpp_targets(
	{lines = {l = {inbound_route = {{ring = {"xmpp:a@b.de", "xmpp:A@b.de", "matrix:@x:y.de"}},
		{ring = {"xmpp:c@b.de"}}}}}}, "l", "dialled@x"), ","), "a@b.de,c@b.de")
check("no plan keeps the dialled contact", table.concat(
	sms_xmpp_targets(routing, "nosuchline", "dialled@x"), ","), "dialled@x")

local home_stages = inbound_stages(routing, "home", "admin@xmpp.example.com")
check("home has its rendered plan", #home_stages >= 1, true)
check("home stage 1 rings", type(home_stages[1].ring), "table")
local fallback = inbound_stages(routing, nil, "someone@example.net")
check("no line falls back to one stage", #fallback, 1)
check("the fallback rings the dialled contact", fallback[1].ring[1], "xmpp:someone@example.net")
check("and Matrix alongside it", fallback[1].ring[2], "matrix:")
check("at the timeout this had before the call plan", fallback[1].timeout, 300)
local unknown = inbound_stages(routing, "nosuchline", "someone@example.net")
check("an unknown line falls back too", #unknown, 1)

local function plan_is_default(name, line, want)
	local _, default_plan = inbound_stages(routing, line, "someone@example.net")
	check(name, default_plan, want)
end
plan_is_default("a rendered plan is not the fallback", "home", false)
plan_is_default("an unknown line is", "nosuchline", true)
plan_is_default("no line at all is", nil, true)
check("a line with no inbound_route falls back", select(2, inbound_stages(
	{registrations = {}, lines = {a = {primary = "x", callers = {"*"}}}, outbound = {}},
	"a", "someone@example.net")), true)

-- THE MATRIX CALLER is a claim: the scheme is built here so another transport's identity
-- cannot be borrowed.
local function caller(name, raw, want)
	check(name, matrix_caller_uri(raw), want)
end
caller("an mxid becomes a matrix uri", "@admin:example.com", "matrix:@admin:example.com")
caller("case folded like every other address", "@ADMIN:EXAMPLE.COM", "matrix:@admin:example.com")
caller("surrounding space is trimmed", "  @admin:example.com  ", "matrix:@admin:example.com")
caller("no header at all", nil, nil)
caller("empty", "", nil)
caller("cannot claim to be an xmpp caller", "xmpp:office@xmpp.example.com", nil)
caller("cannot pre-supply the scheme either", "matrix:@admin:example.com", nil)
caller("missing the leading at", "lhns:example.com", nil)
caller("no server part", "@lhns", nil)
caller("a second at", "@a@b:example.com", nil)
caller("embedded space", "@a b:example.com", nil)

local function display(name, header, want)
	check(name, sms_from_display(header), want)
end
display("quoted display name", '"@admin:example.com" <sip:matrixbridge@example.net>', "@admin:example.com")
display("unquoted", '@admin:example.com <sip:matrixbridge@example.net>', "@admin:example.com")
display("none", "<sip:matrixbridge@example.net>", nil)
display("no angle brackets at all", "sip:matrixbridge@example.net", nil)

check("a message From yields a matrix caller",
	matrix_caller_uri(sms_from_display('"@admin:example.com" <sip:matrixbridge@example.net>')),
	"matrix:@admin:example.com")
check("a bridge-only From yields none",
	matrix_caller_uri(sms_from_display("<sip:matrixbridge@example.net>")), nil)

-- THE GATE refuses rather than falling through, so revoking someone cannot leak them onto
-- another line.
local function permits(name, line, addr, want)
	check(name, line_permits(routing, line, addr), want)
end
permits("a listed domain", "home", "xmpp:someone@xmpp.example.com", true)
permits("matrix is listed too", "home", "matrix:@admin:example.com", true)
permits("another domain is refused", "home", "xmpp:someone@example.net", false)
permits("a substring of a listed domain is refused", "home", "xmpp:a@xmpp.example.com.example.net", false)
permits("case folded", "home", "XMPP:SOMEONE@XMPP.EXAMPLE.COM", true)
permits("no caller at all", "home", nil, false)
permits("empty caller", "home", "", false)
permits("an unknown line", "nosuchline", "xmpp:someone@xmpp.example.com", false)

local gate_tbl = {registrations = {}, lines = {
	none = {primary = "x"},
	empty = {primary = "x", callers = {}},
	open = {primary = "x", callers = {"*"}},
}, outbound = {}}
check("a line with no callers list", line_permits(gate_tbl, "none", "xmpp:a@b.example"), false)
check("a line with an empty callers list", line_permits(gate_tbl, "empty", "xmpp:a@b.example"), false)
check("a star still needs an identified caller", line_permits(gate_tbl, "open", nil), false)
check("a star admits an identified one", line_permits(gate_tbl, "open", "xmpp:a@b.example"), true)

check("no from_glob narrows nothing", routing_from_match(nil, "xmpp:a@b.example"), true)
check("first entry matches", routing_from_match({"xmpp:a@b.example", "xmpp:c@d.example"},
	"xmpp:a@b.example"), true)
check("second entry matches", routing_from_match({"xmpp:a@b.example", "xmpp:c@d.example"},
	"xmpp:c@d.example"), true)
check("no entry matches", routing_from_match({"xmpp:a@b.example"}, "xmpp:c@d.example"), false)
check("an empty list matches nobody", routing_from_match({}, "xmpp:a@b.example"), false)

local function gated(name, to, from, want_peer, want_kind)
	local _, _, peer, _, _, kind = resolve_destination(to, from)
	check(name .. " peer", peer, want_peer)
	check(name .. " kind", kind, want_kind)
end
gated("a permitted caller is routed", "015112345678", "xmpp:someone@xmpp.example.com",
	HOME, "routed")
gated("an unlisted caller is forbidden", "015112345678", "xmpp:someone@example.net",
	nil, "forbidden")
gated("an unidentified caller is forbidden", "015112345678", nil, nil, "forbidden")
gated("a line label is gated too", component_to("015112345678#office"),
	"xmpp:someone@example.net", nil, "forbidden")
gated("and allowed for a listed caller", component_to("015112345678#office"),
	"xmpp:someone@xmpp.example.com", OFFICE, "line")
gated("a trunk label is gated by its default line", component_to("015112345678#sip.example.net"),
	"xmpp:someone@xmpp.example.com", HOME, "trunk")
gated("and refused for an unlisted caller", component_to("015112345678#sip.example.net"),
	"xmpp:someone@example.net", nil, "forbidden")
-- The emergency rule pins a registration and has no line: reachable by anyone.
gated("110 from an unlisted caller", "110", "xmpp:someone@example.net",
	HOME, "routed")
gated("112 from an unidentified caller", "112", nil, HOME, "routed")

-- EXTENSION FIRST: PJSIP/<endpoint> alone dials the registrar.
check("dial string", dial_string(routing, EVENT, "5550"),
	"PJSIP/5550@" .. EVENT)
check("and on the PSTN trunk", dial_string(routing, OFFICE, "+4915112345678"),
	"PJSIP/+4915112345678@" .. OFFICE)

-- The endpoint carries credentials and transport; an endpoint name as the host after the
-- slash resolves nowhere.
check("message target names the endpoint",
	message_target(routing, EVENT, "5550"),
	"pjsip:" .. EVENT .. "/sip:5550@voip.example.org")
check("and on the PSTN trunk", message_target(routing, OFFICE, "+4915112345678"),
	"pjsip:" .. OFFICE .. "/sip:+4915112345678@sip.example.net")

local function dialled(name, localpart, from, want)
	local _, _, peer, exten = resolve_destination(component_to(localpart),
		from and ("xmpp:" .. from) or permitted)
	check(name, peer and dial_string(routing, peer, exten) or nil, want)
end
dialled("a short extension goes out pjsip", "5550", nil, ev("PJSIP/5550@" .. EVENT))
dialled("an ordinary number goes out the household registration", "015112345678", nil,
	"PJSIP/015112345678@" .. HOME)
dialled("110 leaves by the registration the rule pins", "110", "someone@example.net",
	"PJSIP/110@" .. HOME)
dialled("112 too", "112", nil, "PJSIP/112@" .. HOME)
dialled("a refused caller dials nothing", "5550", "someone@example.net", nil)

-- pjsip cannot carry a JID in the Contact user part (the registration would not load), so
-- the contact is the number.
attribution("the pjsip contact matches per-DID", EVENT_NO, "voip.example.org", ev("contact"))
attribution("and by endpoint on the same host", EVENT_NO, EVENT, ev("contact"))

-- EVERY DID, both directions, off the live table: a DID without a line or a renamed
-- endpoint rings the wrong household.
for name, reg in pairs(routing.registrations) do
	if reg.disabled then
		check("inbound: disabled " .. name .. " attributes nothing",
			(inbound_line(routing, reg.contact_user, name)), nil)
		if routing.lines[reg.line].primary == name then
			local _, _, peer, _, _, kind = resolve_destination(
				component_to("+4915112345678#" .. reg.line), permitted)
			check("outbound: #" .. reg.line .. " is refused", peer, nil)
			check("outbound: #" .. reg.line .. " is disabled", kind, "disabled")
		end
	else
		check("inbound: " .. name .. " endpoint is its line",
			(inbound_line(routing, nil, name)), reg.line)
		check("inbound: " .. name .. " contact is its line",
			(inbound_line(routing, reg.contact_user, name)), reg.line)
		check("inbound: " .. name .. " contact is a per-DID match",
			select(4, inbound_line(routing, reg.contact_user, name)), "contact")
		check("inbound: " .. name .. " contact is a legal sip user part",
			reg.contact_user:find("@") == nil, true)
		local _, _, peer, _, _, kind, line = resolve_destination(
			component_to("+4915112345678#" .. reg.line), permitted)
		check("outbound: #" .. reg.line .. " is a line", kind, "line")
		check("outbound: #" .. reg.line .. " resolves to its own line", line, reg.line)
		check("outbound: #" .. reg.line .. " leaves by its primary",
			peer, routing.lines[reg.line].primary)
		check("outbound: " .. name .. " dials its own endpoint",
			dial_string(routing, name, "123"), "PJSIP/123@" .. name)
	end
end

-- THE EMERGENCY WAIVER: the rule pins a PSTN registration whose line REFUSES the caller who
-- gets through.
local emergency_rule
for _, r in ipairs(routing.outbound) do
	if r.emergency then emergency_rule = emergency_rule or r end
end
local er = emergency_rule or {}
local emergency_reg = routing.registrations[er.registration or ""] or {}
check("an emergency rule exists", emergency_rule ~= nil, true)
check("it pins a registration rather than a line", er.registration ~= nil, true)
check("it carries no line", er.line, nil)
check("the pinned registration exists", routing.registrations[er.registration or ""] ~= nil, true)
check("and is on the PSTN trunk", emergency_reg.host, env.SIP_HOST)
local stranger = "xmpp:nobody@elsewhere.example"
check("its line refuses the stranger", line_permits(routing, emergency_reg.line, stranger), false)
for _, n in ipairs({"110", "112"}) do
	local _, _, peer, exten, rule, kind = resolve_destination(component_to(n), stranger)
	check(n .. " reaches the pinned registration", peer, er.registration)
	check(n .. " is not refused", kind, "routed")
	check(n .. " keeps the waiver", rule ~= nil and rule.emergency == true, true)
	check(n .. " is dialled as itself", exten, n)
end
check("the same caller is refused an ordinary number",
	select(6, resolve_destination(component_to("015112345678"), stranger)), "forbidden")

if failures > 0 then
	print(failures .. " failure(s)")
	os.exit(1)
end
print("lua dialplan tests OK")
