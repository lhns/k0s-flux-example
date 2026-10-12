-- Unit tests for the pure parts of ../extensions.lua: the JID <-> SIP URI mapping, inbound
-- line attribution, and outbound destination resolution. Run with lua5.1 by run.sh next to
-- this file -- the dialplan runs under Asterisk's Lua 5.1, so test it there too.
--
-- The dialplan reads its configuration from the environment and Lua cannot set it, so
-- os.getenv is replaced before the chunk is loaded; `trunks` and `sip_routing` are both
-- built at load time. Numbers here are placeholders, never real subscribers.
--
-- $1 is the component directory, which run.sh is itself handed; $2 the routing table as
-- the routing/ chart RENDERS it. A fixture would drift from the chart, and the outbound
-- rules exercised below are the live ones. run.sh runs it twice: on the live table, and on
-- the one rendered with every `disabled` entry enabled.

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
	-- The [matrixbridge] peer and our own number on the trunk, both read only by the
	-- message path. Placeholders, as everywhere here.
	SIP_BRIDGE_PEER = "matrixbridge",
	SIP_USER = "02012345678",
}
os.getenv = function(k) return env[k] end

-- pbx_lua supplies `app`; nothing here dials, but normalize_e164 logs through it.
app = { log = function() end }

dofile(APP .. "/extensions.lua")

-- Held so the fail-closed cases can blank `sip_routing` and put it back. Asserted, because
-- a table that failed to load makes EVERY outbound case below exercise the fail-closed
-- path instead of the rules, and they would all still pass.
local routing = sip_routing
if not routing then
	print("FAIL: the rendered routing table at " .. ROUTING .. " did not load")
	os.exit(1)
end

-- eventpbx is `disabled` in values.yaml. Its cases follow the table's own flag, so both
-- runs assert their state and re-enabling it needs no edit here. ev(on, off).
local ev_reg = routing.registrations["eventpbx-5552"]
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

-- What the component hands the dialplan: the localpart with "@SIP_HOST" appended when it
-- carries no host, then "#" mapped back to "@" by local_to_sip_uri.
local function component_to(localpart)
	if not localpart:find("@") then localpart = localpart .. "@" .. env.SIP_HOST end
	return local_to_sip_uri(localpart)
end

-- A caller the live table permits on every line. resolve_destination gates as well as
-- routes, so a case with no caller asserts the gate rather than the routing.
local permitted = "xmpp:someone@xmpp.example.com"

local function case(name, localpart, want_to, want_label, want_peer, want_exten, want_kind)
	local to, label, peer, exten, _, kind = resolve_destination(component_to(localpart), permitted)
	check(name .. " to", to, want_to)
	check(name .. " label", label, want_label)
	check(name .. " peer", peer, want_peer)
	check(name .. " exten", exten, want_exten)
	check(name .. " kind", kind, want_kind)
end

-- 1. bare number: the appended host expresses no intent, so it is stripped and the routing
--    table decides.
case("bare number", "015112345678",
	"015112345678", nil, "pstn-02012345678", "015112345678", "routed")

-- 1b. short all-digit string stays on the internal trunk, not PSTN. With that line disabled
--     it is REFUSED -- never passed on to the catch-all's PSTN trunk.
case("short extension", "5550",
	"5550", nil, ev("eventpbx-5552"), "5550", ev("routed", "disabled"))

-- 2. roster contact on the PSTN trunk. Without the suffix strip the label reads
--    "sip.example.net@sip.example.net", matches nothing, and is dialled as DNS. A trunk label names a
--    HOST, and no section is named after one: it resolves through that host's DEFAULT
--    registration, so the dial carries credentials and pins a number.
case("roster contact, pstn trunk", "+4915112345678#sip.example.net",
	"+4915112345678@sip.example.net", "sip.example.net", "pstn-02012345678", "+4915112345678", "trunk")

-- 3. roster contact on the other trunk: that trunk, not the default. A host with no enabled
--    registration is no trunk of ours, so the label is refused.
case("roster contact, other trunk", "5550#voip.example.org",
	"5550@voip.example.org", "voip.example.org", ev("eventpbx-5552"), "5550",
	ev("trunk", "unknown"))

-- 4. A LINE label -- the form every inbound caller is rendered as, so it is also the
--    form dialled back. It resolves through the line's primary registration to that
--    registration's PEER, which carries the credentials and pins the presented number.
--    Every peer is named after its registration, never after the host: three DIDs share
--    sip.example.net and the presented number is pinned per section.
case("line label", "+4915112345678#office",
	"+4915112345678@office", "office", "pstn-02012345671", "+4915112345678", "line")
case("line label, other line", "+4915112345678#eventpbx",
	"+4915112345678@eventpbx", "eventpbx", ev("eventpbx-5552"), "+4915112345678",
	ev("line", "disabled"))
-- A client that upper-cased the label still reaches the line; line names are lowercase.
case("line label case-insensitive", "+4915112345678#OFFICE",
	"+4915112345678@OFFICE", "OFFICE", "pstn-02012345671", "+4915112345678", "line")

-- 5. Neither a trunk of ours nor a line: FAILS CLOSED, rather than dialling the label as a
--    DNS name with no credentials -- which a carrier may answer with a recorded refusal,
--    i.e. a connected call rather than an error. `kind` is what the jingle handler refuses on.
case("unknown label fails closed", "alice#pbx.example.net",
	"alice@pbx.example.net", "pbx.example.net", nil, "alice", "unknown")
-- The same, for a label that merely looks like one of ours.
case("near-miss trunk label fails closed", "+4915112345678#sip.example.net.evil.example",
	"+4915112345678@sip.example.net.evil.example", "sip.example.net.evil.example", nil,
	"+4915112345678", "unknown")

-- Case differences in the appended host must still strip.
case("autohost case-insensitive", "+4915112345678#SIP.EXAMPLE.NET",
	"+4915112345678@SIP.EXAMPLE.NET", "SIP.EXAMPLE.NET", "pstn-02012345678", "+4915112345678", "trunk")

-- The LIVE routing rules, as the chart renders them. `emergency` is the only thing that
-- lets a caller outside a line's callers list reach a trunk, so it is asserted on every case,
-- not just the emergency ones: a rule that grew it by accident is the failure that matters.
-- The waiver itself lives in the jingle handler, which needs a channel and cannot be
-- reached from here; the rule the handler reads is what is checked.
local function routed(name, localpart, from, want_peer, want_line, want_emergency)
	local _, _, peer, _, rule, _, line = resolve_destination(component_to(localpart),
		from and ("xmpp:" .. from) or permitted)
	check(name .. " peer", peer, want_peer)
	check(name .. " line", line, want_line)
	check(name .. " emergency", rule ~= nil and rule.emergency == true, want_emergency)
end

-- Emergency, from an account the allow-list does not cover. 110 is three digits, so the
-- short-extension rule matches it too: reaching the PSTN trunk rather than the internal one
-- IS first-match-wins, and is the whole reason the rule sits first. The rule pins a
-- REGISTRATION, so the line is that registration's rather than the caller's.
routed("110 from a stranger", "110", "nobody@elsewhere.example",
	"pstn-02012345678", "home", true)
routed("112", "112", "admin@xmpp.example.com", "pstn-02012345678", "home", true)

-- Anchoring. Unanchored, "11[02]" would match inside 0112345 and hand an ordinary number an
-- emergency waiver. Same trunk either way, so `emergency` is the assertion that can fail.
routed("0112345 is not an emergency number", "0112345", "admin@xmpp.example.com",
	"pstn-02012345678", "home", false)
routed("1102 is not an emergency number", "1102", "admin@xmpp.example.com",
	ev("eventpbx-5552"), "eventpbx", false)

-- Ordering again, at the other boundary: five digits still match the short rule, six fall
-- past it to the catch-all.
routed("five digits", "12345", "admin@xmpp.example.com", ev("eventpbx-5552"), "eventpbx", false)
routed("six digits", "123456", "admin@xmpp.example.com", "pstn-02012345678", "home", false)

-- The per-person rule: same destination, different caller, different line -- and a PEER of its
-- own, which is the whole point. Without one the call would leave by the host-named peer and
-- present the household number whatever the table said.
routed("office caller presents office", "015112345678", "office@xmpp.example.com",
	"pstn-02012345671", "office", false)
routed("anyone else still gets the household line", "015112345678", "admin@xmpp.example.com",
	"pstn-02012345678", "home", false)
-- A caller the narrowed rule excludes falls past it to the catch-all. Refusal is the LINE's
-- job, asserted separately; narrowing never refuses.
routed("an excluded caller falls past the from_glob rule", "015112345678", nil,
	"pstn-02012345678", "home", false)

-- Fail closed. Without a table there is no rule and no peer, so the caller is refused --
-- including for 110, which must not be special-cased into a dial with no credentials. A
-- line label has nothing to resolve either, which is the one case that could have widened.
sip_routing = nil
routed("no routing table", "015112345678", "admin@xmpp.example.com", nil, nil, false)
routed("no routing table, emergency", "110", "admin@xmpp.example.com", nil, nil, false)
case("no routing table, line label", "+4915112345678#office",
	"+4915112345678@office", "office", nil, "+4915112345678", "unknown")
sip_routing = routing

-- from_glob against a synthetic table, so the forms the live rule does not use are covered.
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
-- A caller with no identity matches no narrowed rule, so the next one decides. It is then
-- refused by the LINE's callers list, not here: narrowing and permission are separate.
narrowed("unidentified caller falls to the next rule", nil, "trunk-b")

-- A rule naming a line or a registration the table does not define must NOT fall through to
-- a later rule: a later rule would send the call out a peer nobody chose.
check("undefined line fails closed",
	routing_match({registrations = {}, lines = {},
		outbound = {{to_pattern = "^(.*)$", line = "gone"}}}, "5551234", nil),
	nil)
check("undefined pinned registration fails closed",
	routing_match({registrations = {}, lines = {a = {primary = "reg-a"}},
		outbound = {{to_pattern = "^(.*)$", registration = "gone", emergency = true}}},
		"5551234", nil),
	nil)
-- A line whose primary is missing is the same failure one level down.
check("line with a dangling primary fails closed",
	routing_match({registrations = {}, lines = {a = {primary = "reg-a"}},
		outbound = {{to_pattern = "^(.*)$", line = "a"}}}, "5551234", nil),
	nil)

-- A DISABLED primary switches its line off, whatever values.yaml says today: the rule that
-- selects it REFUSES, and must not fall through to the next rule's trunk.
local off_tbl = {
	registrations = {
		["reg-a"] = {host = "trunk-a", peer = "reg-a", line = "a", disabled = true},
		["reg-b"] = {host = "trunk-b", peer = "reg-b", line = "b"},
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

-- The glob forms the schema allows, including the one that must not be treated as a
-- wildcard by accident.
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

-- INBOUND IDENTITY. The dialled DID, not the trunk, decides who a caller appears as: the
-- inbound extension is the contact that registration gave the carrier, so it names one
-- registration and therefore one line. Contacts and lines are the LIVE ones, from the
-- rendered table.
local function attributed(name, extension, peername, want_line)
	check(name, (inbound_line(routing, extension, peername)), want_line)
end
attributed("contact picks the office line", "02012345671", "sip.example.net", "office")
attributed("the other office DID, same line", "02012345672", "sip.example.net", "office")
attributed("entrypoint contact picks the household line", "02012345678", "sip.example.net", "home")
-- And it is the fallback for the same host when the carrier stops echoing our contact, so
-- per-DID inbound degrades to one line rather than to nothing.
attributed("unknown contact falls back to the entrypoint's line", "someone@example.net",
	"sip.example.net", "home")
-- EVERY DID, inbound, by the endpoint pjsip matched the call to -- which is the attribution
-- that does not depend on the carrier echoing anything in the Request-URI.
attributed("the household endpoint is the household line", nil, "pstn-02012345678", "home")
attributed("an office endpoint is the office line", nil, "pstn-02012345671", "office")
attributed("the other office endpoint too", nil, "pstn-02012345672", "office")
attributed("the eventpbx endpoint is its own line", nil, "eventpbx-5552", ev("eventpbx"))
attributed("no contact at all falls back the same way", nil, "sip.example.net", "home")
-- A host with exactly one registration needs no entrypoint entry to be unambiguous.
attributed("sole registration on a host", nil, "voip.example.org", ev("eventpbx"))
attributed("unknown peer has no line", "02012345678", "pbx.example.net", nil)
-- The endpoint OUTRANKS the dialled contact, and must: pjsip matched the call to one
-- registration, while the Request-URI is whatever the carrier chose to send.
attributed("the endpoint outranks a contact naming another DID", "02012345672",
	"pstn-02012345671", "office")
attributed("including one naming the household DID", "02012345678",
	"pstn-02012345671", "office")
attributed("no peer has no line", "02012345678", nil, nil)

-- HOW it was attributed, which is what the handler NOTICEs on. A contact match and a
-- fallback are otherwise indistinguishable: the carrier quietly ceasing to echo our Contact
-- collapses every DID on a host onto one line and logs nothing, which is how it would be
-- found as "all calls ring the wrong line" rather than as a line in the log.
local function attribution(name, extension, peername, want)
	local _, _, _, how = inbound_line(routing, extension, peername)
	check(name, how, want)
end
attribution("a contact match says so", "02012345671", "sip.example.net", "contact")
attribution("the entrypoint fallback is marked", "someone@example.net", "sip.example.net", "entrypoint")
-- The pjsip half. An endpoint match cannot have been the fallback -- only the host's default
-- endpoint carries a type=identify -- so it is not NOTICEd; the default endpoint's own match
-- could have been, so it still reports the fallback it may be.
attribution("an endpoint match is a match", nil, "pstn-02012345671", "endpoint")
attribution("even against a contact naming another DID", "02012345678",
	"pstn-02012345671", "endpoint")
attribution("both agreeing is still a contact match", "02012345671",
	"pstn-02012345671", "contact")
attribution("the default endpoint may always be the source-address fallback", nil,
	"pstn-02012345678", "entrypoint")
attribution("and says so even on its own contact only when the contact matches",
	"02012345678", "pstn-02012345678", "contact")
-- A host with one registration answers everything, so the fallback is invisible there too.
attribution("the sole-registration fallback is marked", "someone@example.net",
	"voip.example.org", ev("sole"))
-- Asking for the default outright -- the message path, which has no contact -- is a
-- fallback like any other, and the handler NOTICEs only what carried a contact.
attribution("no contact is the fallback too", nil, "sip.example.net", "entrypoint")
attribution("an unknown peer attributes nothing", "02012345678", "pbx.example.net", nil)

-- What a caller is rendered as. The LINE occupies the slot after "#", so the XMPP form is
-- "+49...#office" -- which is also the form resolve_destination resolves above, and the two
-- must stay symmetric or people get a contact they cannot call back.
check("from uri carries the line",
	call_from_uri("<sip:+4915112345678@10.20.2.3>", "sip.example.net", "office"),
	"<sip:+4915112345678@office>")
check("from uri as a JID localpart",
	sip_uri_to_local("+4915112345678@office"), "+4915112345678#office")
check("and back", local_to_sip_uri("+4915112345678#office"), "+4915112345678@office")
-- No line: the trunk host. Degraded identity, but the call still connects and the label
-- still dials back.
check("from uri without a line falls back to the trunk",
	call_from_uri("<sip:+4915112345678@10.20.2.3>", "sip.example.net", nil),
	"<sip:+4915112345678@sip.example.net>")
check("from uri with neither is left alone",
	call_from_uri("<sip:+4915112345678@10.20.2.3>", "pbx.example.net", nil),
	"<sip:+4915112345678@10.20.2.3>")

-- The Matrix key. "sip-" plus the portal ID, and the portal ID is "<line>-<digits>" so the
-- same human on two lines is two portals -- one line's numbers are only meaningful there.
check("conference name carries the line",
	matrix_conference_name("<sip:+4915112345678@office>", "office"),
	"sip-office-+4915112345678")
-- Split back with ^(.*)-([0-9]+)$, anchored on the trailing digit run, so a line name may
-- contain hyphens. That is why "-" and not "_": mautrix escapes a literal underscore.
local conf_line, conf_digits = ("office-main-+4915112345678"):match("^(.*)-(%+?[0-9]+)$")
check("portal id splits on the trailing digits", conf_line, "office-main")
check("portal id digits keep the plus", conf_digits, "+4915112345678")
-- Fails closed without a line, and for anything that did not normalise: both paths then
-- leave the bridge out rather than inventing a key it would decline.
check("conference name needs a line",
	matrix_conference_name("<sip:+4915112345678@office>", nil), nil)
check("conference name needs e164",
	matrix_conference_name("<sip:5551@eventpbx>", "eventpbx"), nil)
-- Whatever it produces has to survive the kick's character class, or an answered
-- Matrix call could enter a conference that can never be ended.
-- The class admits "+" for exactly this; it does not separate application arguments.
check("line conference is kickable",
	matrix_kick_conference(matrix_conference_name("<sip:+4915112345678@office>", "office")),
	"sip-office-+4915112345678")

-- What the Matrix bridge sends: E.164 with the plus in the request URI, and the
-- conference name in X-Conference. Numbers are placeholders.
-- A permitted Matrix caller, since the line's callers list now gates every outbound path.
local matrix_caller = "matrix:@admin:example.com"

local function route(name, exten, conf, want_peer, want_num, want_conf, want_cause)
	local peer, num, gotconf, cause = matrix_outbound_route(exten, conf, matrix_caller)
	check(name .. " peer", peer, want_peer)
	check(name .. " num", num, want_num)
	check(name .. " conf", gotconf, want_conf)
	check(name .. " cause", cause, want_cause)
end

-- The gate applies to the Matrix call path: an INVITE whose caller header is missing or
-- names nobody permitted reaches no trunk at all.
check("no caller is refused", (matrix_outbound_route("+4915112345678", "sip-4915112345678", nil)), nil)
check("an unlisted matrix caller is refused",
	(matrix_outbound_route("+4915112345678", "sip-4915112345678", "matrix:@a:example.net")), nil)

-- The ordinary case: a bare E.164 carries no host, so the default trunk applies and the
-- number is passed through untouched.
route("e164 to default trunk", "+4915112345678", "sip-4915112345678",
	"pstn-02012345678", "+4915112345678", "sip-4915112345678", nil)

-- The request URI host is the gateway's own Service name and says nothing about routing.
route("request uri host ignored", "+4915112345678@cheogram-sip-internal.example.svc",
	"sip-4915112345678", "pstn-02012345678", "+4915112345678", "sip-4915112345678", nil)

-- A short all-digit destination keeps going to the internal trunk, exactly as the XMPP
-- path routes it: normalize_e164 leaves it alone and resolve_destination decides.
route("short extension keeps the short trunk", "5550", "sip-5550",
	ev("eventpbx-5552"), ev("5550"), ev("sip-5550"), ev(nil, 34))

-- Not E.164 yet: normalised before the trunk is chosen, so it cannot route differently
-- from the same number written with a plus.
route("00 prefix normalised", "004915112345678", "sip-4915112345678",
	"pstn-02012345678", "+4915112345678", "sip-4915112345678", nil)

-- Fails closed. 1 -> 404, so the bridge sees the same answer as before this path existed.
route("not a number", "alice@pbx.example.net", "sip-4915112345678", nil, nil, nil, 1)
route("no conference header", "+4915112345678", nil, nil, nil, nil, 1)
route("empty conference header", "+4915112345678", "", nil, nil, nil, 1)
-- A comma would be read by Originate() as a further argument.
route("conference name with a comma", "+4915112345678", "sip-49151,app,Echo",
	nil, nil, nil, 1)

-- Whitespace around a header value is the transport's, not the name's.
route("conference header trimmed", "+4915112345678", "  sip-4915112345678 ",
	"pstn-02012345678", "+4915112345678", "sip-4915112345678", nil)

-- Routing table missing or malformed: 34 -> 503, rather than dialling anonymously, which a
-- carrier may answer with a recorded refusal -- a connected call rather than an error.
sip_routing = nil
route("no routing table", "+4915112345678", "sip-4915112345678", nil, nil, nil, 34)
sip_routing = routing

-- The room's line (the middle of "sip-<line>-+<digits>") picks the trunk, not the number.
route("office room uses the office line", "+4915176995276", "sip-office-+4915176995276",
	"pstn-02012345671", "+4915176995276", "sip-office-+4915176995276", nil)
route("home room uses the home line", "+4915176995276", "sip-home-+4915176995276",
	"pstn-02012345678", "+4915176995276", "sip-home-+4915176995276", nil)
-- Emergency keeps its pinned registration, whatever the room's line.
route("emergency from an office room", "112", "sip-office-+4915176995276",
	"pstn-02012345678", "112", "sip-office-+4915176995276", nil)
-- No line (a legacy conference) is routed by the number, as before.
route("legacy conference is routed by number", "+4915176995276", "sip-4915176995276",
	"pstn-02012345678", "+4915176995276", "sip-4915176995276", nil)
-- A line that is not configured fails closed, like an unknown label.
route("unknown room line is refused", "+4915176995276", "sip-nope-+4915176995276",
	nil, nil, nil, 34)
-- eventpbx is disabled in the live table: refused, not rerouted to another trunk.
route("room on a disabled line", "+4915176995276", "sip-eventpbx-+4915176995276",
	ev("eventpbx-5552"), ev("+4915176995276"), ev("sip-eventpbx-+4915176995276"), ev(nil, 34))
-- The line's callers gate applies, not just the number's rule.
local gate_tbl = {registrations = routing.registrations, outbound = routing.outbound,
	lines = {home = routing.lines.home,
		office = {primary = routing.lines.office.primary, callers = {"xmpp:*@xmpp.example.com"}}}}
sip_routing = gate_tbl
route("room line not permitting the caller", "+4915176995276", "sip-office-+4915176995276",
	nil, nil, nil, 34)
route("emergency ignores the room line's callers", "112", "sip-office-+4915176995276",
	"pstn-02012345678", "112", "sip-office-+4915176995276", nil)
sip_routing = routing

-- Originate's statuses, mapped to the causes the stack turns into SIP responses.
check("cause BUSY", matrix_outbound_cause("BUSY"), 17)
check("cause CONGESTION", matrix_outbound_cause("CONGESTION"), 34)
check("cause HANGUP", matrix_outbound_cause("HANGUP"), 19)
check("cause FAILED", matrix_outbound_cause("FAILED"), 34)
-- Anything unknown, and a variable that was never set, still hang the leg up.
check("cause UNKNOWN", matrix_outbound_cause("UNKNOWN"), 34)
check("cause nil", matrix_outbound_cause(nil), 34)

-- Ending a conference when a leg hangs up. This is the whole of the fix for "the SIP
-- caller hangs up and the Element call stays open": end_marked cannot do it, so the
-- hangup extension kicks the conference instead.
check("kick conference", matrix_kick_conference("sip-4915112345678"), "sip-4915112345678")
-- A leg that never joined a conference must produce no kick at all, because every
-- ordinary inbound call and every SIP MESSAGE passes through the same handler.
check("kick no conference", matrix_kick_conference(nil), nil)
check("kick empty conference", matrix_kick_conference(""), nil)
-- The conference already ended this leg, so there is nothing left to kick. ENDMARKED is
-- the normal Matrix-side hangup, and skipping it is what keeps a WARNING off every call.
check("kick skips endmarked", matrix_kick_conference("sip-4915112345678", "ENDMARKED"), nil)
check("kick skips kicked", matrix_kick_conference("sip-4915112345678", "KICKED"), nil)
check("kick after a plain hangup", matrix_kick_conference("sip-4915112345678", "HANGUP"),
	"sip-4915112345678")
-- The name becomes an application argument, so anything outside the class the routing
-- already enforces fails closed rather than being quoted and hoped about.
check("kick rejects a comma", matrix_kick_conference("sip-1,participants"), nil)
check("kick rejects a quote", matrix_kick_conference("sip-1' ; rm -rf /"), nil)
check("kick rejects a space", matrix_kick_conference("sip-1 all"), nil)
check("kick rejects a semicolon", matrix_kick_conference("sip-1;id"), nil)
check("kick rejects a dollar", matrix_kick_conference("sip-$(id)"), nil)
-- Whatever matrix_outbound_route accepts as a conference name must survive to here, or
-- an outbound call could be placed into a conference that can never be ended.
local _, _, routed = matrix_outbound_route("+4915112345678", "sip-4915112345678", matrix_caller)
check("routed conference is kickable", matrix_kick_conference(routed), "sip-4915112345678")

-- The JID localpart mapping both ways.
check("sip_uri_to_local", sip_uri_to_local("alice@pbx.example.net"), "alice#pbx.example.net")
check("local_to_sip_uri", local_to_sip_uri("alice#pbx.example.net"), "alice@pbx.example.net")

-- SIP MESSAGE. The From shapes below are defensive: no real inbound MESSAGE had been
-- observed when this was written, so display name, ";user=phone" params, a bare URI with
-- no angle brackets and every spelling of the number are all exercised.
local function inbound(name, header, want_uri, want_digits)
	local uri, digits = sms_inbound_from(header)
	check(name .. " uri", uri, want_uri)
	check(name .. " digits", digits, want_digits)
end

-- The ordinary carrier shape: E.164 sender, the SBC's own address as the host. The host is
-- REWRITTEN to the LINE -- keeping it would give one human "+49...#<sbc-ip>" for an SMS and
-- "+49...#home" for a call. There is no contact to key on here, so the line is the host's
-- default registration: SMS cannot tell two DIDs on one host apart at all.
inbound("plain e164", "<sip:+4915112345678@10.20.2.3>",
	"+4915112345678@home", "4915112345678")
inbound("display name and params",
	'"Someone" <sip:+4915112345678@10.20.2.3;user=phone>;tag=abc123',
	"+4915112345678@home", "4915112345678")
-- No angle brackets, so the ";tag" belongs to the header and must still come off.
inbound("no angle brackets", "sip:+4915112345678@10.20.2.3;tag=abc123",
	"+4915112345678@home", "4915112345678")
inbound("uppercase scheme", "<SIP:+4915112345678@10.20.2.3>",
	"+4915112345678@home", "4915112345678")
-- 00 and national forms collapse to the same identity, which is the whole point: otherwise
-- the portal key diverges from the call path's conference name.
inbound("00 prefix", "<sip:004915112345678@10.20.2.3>",
	"+4915112345678@home", "4915112345678")
inbound("national 0 prefix", "<sip:015112345678@10.20.2.3>",
	"+4915112345678@home", "4915112345678")
-- A host that is already one of ours names its own line, or the other trunk would be
-- mislabelled as the household one.
inbound("known trunk names its line", "<sip:5551@voip.example.org>",
	ev("5551@eventpbx", "5551@home"), nil)
-- Neither of these normalises, so digits is nil and the bridge leg is skipped rather than
-- keyed on something the bridge would decline.
inbound("alphanumeric sender", "<sip:Telekom@10.20.2.3>", "Telekom@home", nil)
inbound("short code", "<sip:22122@10.20.2.3>", "22122@home", nil)

-- Nothing to attribute it to: the carrier's host is the only label left, and keeping it
-- beats emitting "user@". SIP_HOST, not an outbound lookup -- this says where a message came
-- FROM, which is a trunk we register to, not where an outbound one would go. No line means
-- no portal key either, so the bridge is left out rather than handed a keyless message.
local saved_host = env.SIP_HOST
env.SIP_HOST = nil
inbound("nothing to attribute it to", "<sip:+4915112345678@10.20.2.3>",
	"+4915112345678@10.20.2.3", nil)
env.SIP_HOST = saved_host

-- THE invariant. A call and an SMS from the same number must produce the same key: the call
-- path's conference name, and "sip-" plus the digits the message path hands the bridge.
-- Both derive it from matrix_conference_name, so this holds mechanically rather than by
-- two functions happening to agree today.
local function same_portal(name, header)
	local _, digits, line = sms_inbound_from(header)
	local call_line = inbound_line(routing, nil, env.SIP_HOST)
	local conf = matrix_conference_name(call_from_uri(header, env.SIP_HOST, call_line), call_line)
	check(name .. " line", line, call_line)
	-- The conference carries the number's leading +; sms_inbound_from hands back bare
	-- digits, because sms_bridge_message addresses the bridge by the registration's did.
	check(name .. " portal", conf, digits and ("sip-" .. line .. "-+" .. digits))
end
same_portal("e164", "<sip:+4915112345678@10.20.2.3>")
same_portal("00 prefix", "<sip:004915112345678@10.20.2.3>")
same_portal("national 0 prefix", "<sip:015112345678@10.20.2.3>")
same_portal("display name and params",
	'"Someone" <sip:+4915112345678@10.20.2.3;user=phone>;tag=abc123')
-- Neither path may build a key for a sender that did not normalise.
same_portal("alphanumeric sender", "<sip:Telekom@10.20.2.3>")
same_portal("short code", "<sip:22122@10.20.2.3>")

-- PER-DID SMS. The dialled contact is the same discriminator the call path uses, so an SMS
-- to an office DID keys the office line instead of collapsing onto the host's entrypoint.
-- Whether a carrier echoes our Contact on a MESSAGE is untested, so every case that does
-- not match must still land exactly where it did before.
local function sms_line(name, extension, want_uri, want_line, want_how)
	local uri, _, line, _, how = sms_inbound_from("<sip:+4915112345678@10.20.2.3>", extension)
	check(name .. " uri", uri, want_uri)
	check(name .. " line", line, want_line)
	check(name .. " how", how, want_how)
end
sms_line("contact picks the office line", "02012345671",
	"+4915112345678@office", "office", "contact")
sms_line("the other office DID, same line", "02012345672",
	"+4915112345678@office", "office", "contact")
sms_line("the entrypoint contact picks the household line", "02012345678",
	"+4915112345678@home", "home", "contact")
-- The two degradations, both of which must be today's behaviour exactly: a carrier that
-- sends something else, and one that sends nothing this can key on.
sms_line("an unmatched contact falls back as before", "someone@example.net",
	"+4915112345678@home", "home", "entrypoint")
sms_line("no contact falls back as before", nil, "+4915112345678@home", "home", "entrypoint")
sms_line("an empty contact falls back as before", "", "+4915112345678@home", "home", "entrypoint")

-- And a call and an SMS carrying the SAME dialled contact must name the same line, or one
-- human is one portal room for a call and another for a text.
local function sms_matches_call(name, extension)
	local _, _, sms = sms_inbound_from("<sip:+4915112345678@10.20.2.3>", extension)
	check(name, sms, (inbound_line(routing, extension, env.SIP_HOST)))
end
sms_matches_call("office DID", "02012345671")
sms_matches_call("entrypoint DID", "02012345678")
sms_matches_call("unmatched contact", "someone@example.net")
sms_matches_call("no contact", nil)

-- What the inbound handler addresses the bridge as. The endpoint carries the transport and
-- the Request-URI after it carries the address; the "+" in the From is what keeps the bridge
-- from dropping the message while answering 200 OK.
local to, from = sms_bridge_message("+4915112345678@home", "4915112345678")
check("bridge to", to,
	"pjsip:matrixbridge/sip:+492012345678@matrix-sip-bridge.matrix.svc.cluster.local")
-- With a registration it is that DID rather than SIP_USER, which is the honest value once a
-- host carries more than one of our numbers.
local did_to = sms_bridge_message("+4915112345678@office", "4915112345678",
	{did = "+492012345671"})
check("bridge to uses the registration's did", did_to,
	"pjsip:matrixbridge/sip:+492012345671@matrix-sip-bridge.matrix.svc.cluster.local")
check("bridge from", from, "sip:+4915112345678@home")
-- No digits means no bridge leg, which is what keeps SMS and calls in one portal.
check("bridge skipped without digits", sms_bridge_message("Telekom@sip.example.net", nil), nil)

-- The dispatcher, all four combinations. Outbound needs BOTH the bridge as sender AND a
-- numeric destination: on the From alone a carrier-injected "From: sip:matrixbridge@..."
-- would be an outbound-SMS primitive on our trunk.
check("dispatch bridge + number", sms_is_outbound("<sip:matrixbridge@10.20.2.3>", "+4915112345678"), true)
check("dispatch bridge + jid", sms_is_outbound("<sip:matrixbridge@10.20.2.3>", "admin@xmpp.example.com"), false)
check("dispatch carrier + number", sms_is_outbound("<sip:+4915112345678@10.20.2.3>", "+492012345678"), false)
check("dispatch carrier + jid", sms_is_outbound("<sip:+4915112345678@10.20.2.3>", "admin@xmpp.example.com"), false)
-- The bridge sends no display name today; a From that grows one must still be recognised.
check("dispatch bridge with display name",
	sms_is_outbound('"bridge" <sip:matrixbridge@10.20.2.3>;tag=x', "+4915112345678"), true)

-- Outbound trunk selection, the same rules as the call path.
local function sms_route(name, to_uri, want_peer, want_num)
	local peer, num, reason = sms_outbound_route(to_uri, matrix_caller)
	check(name .. " peer", peer, want_peer)
	check(name .. " num", num, want_num)
	check(name .. " reason", reason == nil, want_peer ~= nil)
end
sms_route("e164 to default trunk", "+4915112345678", "pstn-02012345678", "+4915112345678")
-- The request URI host is the gateway's own Service name and says nothing about routing.
sms_route("request uri host ignored", "sip:+4915112345678@cheogram-sip-internal.example.svc:5060",
	"pstn-02012345678", "+4915112345678")
sms_route("short extension keeps the short trunk", "5550", ev("eventpbx-5552"), ev("5550"))
sms_route("00 prefix normalised", "004915112345678", "pstn-02012345678", "+4915112345678")
-- The gate applies here too: a bridge that stopped naming its sender reaches no trunk.
check("sms with no caller is refused", (sms_outbound_route("+4915112345678", nil)), nil)
check("sms from an unlisted caller is refused",
	(sms_outbound_route("+4915112345678", "matrix:@someone:example.net")), nil)
-- Fails closed, the same rule as the call path: this is where a Matrix user reaches the trunk.
sms_route("not a number", "alice@pbx.example.net", nil, nil)
sms_route("empty destination", "", nil, nil)
-- The same fail-closed path as the call leg: no table, no trunk, no send.
sip_routing = nil
sms_route("no routing table", "+4915112345678", nil, nil)
sip_routing = routing

-- An XMPP user's message takes this same route, so it is gated the same way. `permitted` is
-- a listed caller, so the gate is what differs between these cases.
local function xmpp_route(name, to_uri, from, want_peer, want_num)
	local peer, num = sms_outbound_route(local_to_sip_uri(to_uri), from)
	check(name .. " peer", peer, want_peer)
	check(name .. " num", num, want_num)
end
xmpp_route("xmpp sender reaches the trunk", "+4915112345678", permitted,
	"pstn-02012345678", "+4915112345678")
xmpp_route("an unlisted xmpp domain is refused", "+4915112345678",
	"xmpp:someone@elsewhere.example", nil, nil)
xmpp_route("an unidentified xmpp sender is refused", "+4915112345678", nil, nil, nil)
-- The #label form: the label is dropped with the host part, so the table picks the line
-- rather than "home" being dialled as a SIP host.
xmpp_route("a #label resolves through the table", "+4915112345678#home", permitted,
	"pstn-02012345678", "+4915112345678")

-- THE CALL PLAN. A stage's ring targets become ONE Dial() string; stages are what make
-- ringing sequential.
local plan_from = "<sip:+4915112345678@home>"
local plan_conf, plan_caller = matrix_conference_name(plan_from, "home")

local function legs(name, stage, want_legs, want_matrix)
	local got, got_matrix = inbound_legs(stage, plan_from, plan_conf, plan_caller)
	check(name, got, want_legs)
	check(name .. " (matrix)", got_matrix, want_matrix)
end

local xmpp_leg = "Motif/jingle-endpoint/" .. make_jid("admin@xmpp.example.com", plan_from)
-- Each person is a Local leg named by the hex of their lowercase MXID.
local function hex(s) return (s:gsub(".", function(c) return string.format("%02x", c:byte()) end)) end
local function person_leg(mxid) return "Local/" .. hex(mxid) .. "@matrix-leg/n" end
local matrix_leg = person_leg("@admin:example.com")
check("hex of an mxid", hex("@a:b"), "40613a62")

legs("xmpp only", {ring = {"xmpp:admin@xmpp.example.com"}}, xmpp_leg, false)
legs("xmpp and matrix ring together", {ring = {"xmpp:admin@xmpp.example.com", "matrix:@admin:example.com"}},
	xmpp_leg .. "&" .. matrix_leg, true)
-- Each person gets their own leg, so the call lands in their own bridge room.
legs("several matrix targets are several legs",
	{ring = {"matrix:@admin:example.com", "matrix:@other:example.com"}},
	matrix_leg .. "&" .. person_leg("@other:example.com"), true)
legs("the same person twice is one leg, case-insensitively",
	{ring = {"matrix:@admin:example.com", "matrix:@LHNS:example.com"}}, matrix_leg, true)
-- A bare matrix: is the bridge's default recipient: a leg with no MXID in it.
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

-- The bridge declines an INVITE naming a conference it does not know, so a Matrix leg
-- without one would ring nobody and look like it rang.
local no_conf = inbound_legs({ring = {"xmpp:admin@xmpp.example.com", "matrix:@admin:example.com"}},
	plan_from, nil, nil)
check("no conference means no matrix leg", no_conf, xmpp_leg)
-- With nothing else in the stage that leaves no leg at all: the stage takes no time and
-- rings nobody, which looks exactly like one that rang and went unanswered. The handler
-- NOTICEs on this nil.
check("a matrix-only stage without a conference rings nobody",
	(inbound_legs({ring = {"matrix:@admin:example.com"}}, plan_from, nil, nil)), nil)

-- A fake pbx_lua: variables by name, and a record of what was dialled and sent.
local vars, dialled_calls, sent, hung_up = {}, {}, {}, 0
local function chan_var(name)
	return {
		get = function() return vars[name] end,
		set = function(_, v) vars[name] = v end,
	}
end
channel = setmetatable({}, {__index = function(_, k)
	-- "__NAME" is the inheritable spelling of the same variable.
	local name = k:gsub("^__", "")
	return setmetatable(chan_var(name), {__call = function(_, first, ...)
		-- Function-style variables: CHANNEL(name), PJSIP_HEADER(add, X-Foo), MESSAGE_DATA(X-Foo).
		local sub = {...}
		return chan_var(name .. "(" .. tostring(first) .. (sub[1] and "," .. sub[1] or "") .. ")")
	end})
end})
app = {
	log = function() end,
	dial = function(dest, timeout, opts) dialled_calls[#dialled_calls + 1] = {dest, timeout, opts} end,
	MessageSend = function(to, from)
		local who = vars["MESSAGE_DATA(X-Matrix-Recipient)"]
		-- Emptying a message variable removes it.
		sent[#sent + 1] = {to = to, from = from, who = who ~= "" and who or nil}
	end,
	hangup = function() hung_up = hung_up + 1 end,
}
-- The handlers wrap their bodies in pcall so an error cannot drop a call, which would also
-- hide a bug from these tests; report what it swallows.
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

-- matrix-leg: decode the hex, hand the recipient to the pre-dial handler, dial the bridge.
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

-- add-leg-headers runs on the PJSIP channel.
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

-- matrix-answered recognises the answering Local leg by name, and nothing else.
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

-- SMS: one MESSAGE per person the line rings, each naming them; xmpp as before.
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

-- End to end through the delivery path.
local function deliver_on(tbl, line_name)
	local saved = sip_routing
	sip_routing = tbl
	reset_pbx()
	local reg
	for _, r in pairs(routing.registrations) do
		if r.line == line_name and not r.disabled then reg = reg or r end
	end
	local out = {xmpp = {}, bridge = {}}
	if reg then
		vars["CHANNEL(endpoint)"] = reg.peer
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
	return {registrations = routing.registrations, outbound = routing.outbound,
		lines = {[name] = {primary = routing.lines[name].primary,
			callers = routing.lines[name].callers, inbound_route = stages}}}
end

local ok_home, two = deliver_on(
	line_with("home", {{ring = {"matrix:@a:example.com", "matrix:@b:example.com"}}}), "home")
check("home registration found", ok_home, true)
check("one bridge MESSAGE per recipient, each naming them", table.concat(two.bridge, ","),
	"@a:example.com,@b:example.com")
check("no xmpp target in the plan means no xmpp send", #two.xmpp, 0)

-- MESSAGE_DATA persists across sends: the default send must not carry the previous header.
local _, mixed = deliver_on(line_with("home", {{ring = {"matrix:@a:example.com", "matrix:"}}}), "home")
check("a literal and the default recipient", table.concat(mixed.bridge, ","), "@a:example.com,default")

-- The LIVE table: texts follow the call plan.
local _, live_home = deliver_on(routing, "home")
check("home texts no xmpp", #live_home.xmpp, 0)
check("home texts only user", table.concat(live_home.bridge, ","), "@user:example.com")
local _, live_office = deliver_on(routing, "office")
check("office texts one xmpp target", #live_office.xmpp, 1)
check("and it is lhns", live_office.xmpp[1] ~= nil and live_office.xmpp[1]:find("lhns", 1, true) ~= nil, true)
check("office texts lhns on matrix", table.concat(live_office.bridge, ","), "@admin:example.com")

-- xmpp targets: distinct, and the dialled contact for a line with no plan.
check("xmpp targets are distinct", table.concat(sms_xmpp_targets(
	{lines = {l = {inbound_route = {{ring = {"xmpp:a@b.de", "xmpp:A@b.de", "matrix:@x:y.de"}},
		{ring = {"xmpp:c@b.de"}}}}}}, "l", "dialled@x"), ","), "a@b.de,c@b.de")
check("no plan keeps the dialled contact", table.concat(
	sms_xmpp_targets(routing, "nosuchline", "dialled@x"), ","), "dialled@x")

-- The stages themselves, from the LIVE rendered table.
local home_stages = inbound_stages(routing, "home", "admin@xmpp.example.com")
check("home has its rendered plan", #home_stages >= 1, true)
check("home stage 1 rings", type(home_stages[1].ring), "table")
-- The fallback is the pre-call-plan behaviour: a call on no line still rings the contact it
-- was dialled at.
local fallback = inbound_stages(routing, nil, "someone@example.net")
check("no line falls back to one stage", #fallback, 1)
check("the fallback rings the dialled contact", fallback[1].ring[1], "xmpp:someone@example.net")
check("and Matrix alongside it", fallback[1].ring[2], "matrix:")
check("at the timeout this had before the call plan", fallback[1].timeout, 300)
local unknown = inbound_stages(routing, "nosuchline", "someone@example.net")
check("an unknown line falls back too", #unknown, 1)

-- And the fallback says it is one: it rings a different target for 300s instead of the
-- line's own plan, and nothing else downstream can tell the two apart.
local function plan_is_default(name, line, want)
	local _, default_plan = inbound_stages(routing, line, "someone@example.net")
	check(name, default_plan, want)
end
plan_is_default("a rendered plan is not the fallback", "home", false)
plan_is_default("an unknown line is", "nosuchline", true)
plan_is_default("no line at all is", nil, true)
-- A table rendered before inbound_route existed: the line resolves, the plan does not.
check("a line with no inbound_route falls back", select(2, inbound_stages(
	{registrations = {}, lines = {a = {primary = "x", callers = {"*"}}}, outbound = {}},
	"a", "someone@example.net")), true)

-- THE MATRIX CALLER. The bridge is matched by address and never challenged, so what it
-- says is a claim; the scheme is built here so a value naming another transport cannot
-- borrow its identity.
local function caller(name, raw, want)
	check(name, matrix_caller_uri(raw), want)
end
caller("an mxid becomes a matrix uri", "@admin:example.com", "matrix:@admin:example.com")
caller("case folded like every other address", "@ADMIN:EXAMPLE.COM", "matrix:@admin:example.com")
caller("surrounding space is trimmed", "  @admin:example.com  ", "matrix:@admin:example.com")
caller("no header at all", nil, nil)
caller("empty", "", nil)
-- The whole point of building the scheme rather than reading it.
caller("cannot claim to be an xmpp caller", "xmpp:office@xmpp.example.com", nil)
caller("cannot pre-supply the scheme either", "matrix:@admin:example.com", nil)
caller("missing the leading at", "lhns:example.com", nil)
caller("no server part", "@lhns", nil)
caller("a second at", "@a@b:example.com", nil)
caller("embedded space", "@a b:example.com", nil)

-- The display name is where an outbound MESSAGE carries it: the user part is the bridge's
-- own peer name, which is what identify_by=username matches the request on.
local function display(name, header, want)
	check(name, sms_from_display(header), want)
end
display("quoted display name", '"@admin:example.com" <sip:matrixbridge@example.net>', "@admin:example.com")
display("unquoted", '@admin:example.com <sip:matrixbridge@example.net>', "@admin:example.com")
display("none", "<sip:matrixbridge@example.net>", nil)
display("no angle brackets at all", "sip:matrixbridge@example.net", nil)

-- And the two compose the way the dialplan uses them.
check("a message From yields a matrix caller",
	matrix_caller_uri(sms_from_display('"@admin:example.com" <sip:matrixbridge@example.net>')),
	"matrix:@admin:example.com")
check("a bridge-only From yields none",
	matrix_caller_uri(sms_from_display("<sip:matrixbridge@example.net>")), nil)

-- THE GATE. line_permits is the whole of outbound permission: it refuses rather than
-- falling through, so revoking someone cannot leak them onto another line.
local function permits(name, line, addr, want)
	check(name, line_permits(routing, line, addr), want)
end
permits("a listed domain", "home", "xmpp:someone@xmpp.example.com", true)
permits("matrix is listed too", "home", "matrix:@admin:example.com", true)
permits("another domain is refused", "home", "xmpp:someone@example.net", false)
permits("a substring of a listed domain is refused", "home", "xmpp:a@xmpp.example.com.example.net", false)
permits("case folded", "home", "XMPP:SOMEONE@XMPP.EXAMPLE.COM", true)
-- Every path must name its caller now; an unidentified one is nobody.
permits("no caller at all", "home", nil, false)
permits("empty caller", "home", "", false)
permits("an unknown line", "nosuchline", "xmpp:someone@xmpp.example.com", false)

-- A line whose callers list is missing or empty refuses everyone rather than anyone.
local gate_tbl = {registrations = {}, lines = {
	none = {primary = "x"},
	empty = {primary = "x", callers = {}},
	open = {primary = "x", callers = {"*"}},
}, outbound = {}}
check("a line with no callers list", line_permits(gate_tbl, "none", "xmpp:a@b.example"), false)
check("a line with an empty callers list", line_permits(gate_tbl, "empty", "xmpp:a@b.example"), false)
check("a star still needs an identified caller", line_permits(gate_tbl, "open", nil), false)
check("a star admits an identified one", line_permits(gate_tbl, "open", "xmpp:a@b.example"), true)

-- from_glob NARROWS and falls through; it is a list now, and any entry matching is a match.
check("no from_glob narrows nothing", routing_from_match(nil, "xmpp:a@b.example"), true)
check("first entry matches", routing_from_match({"xmpp:a@b.example", "xmpp:c@d.example"},
	"xmpp:a@b.example"), true)
check("second entry matches", routing_from_match({"xmpp:a@b.example", "xmpp:c@d.example"},
	"xmpp:c@d.example"), true)
check("no entry matches", routing_from_match({"xmpp:a@b.example"}, "xmpp:c@d.example"), false)
check("an empty list matches nobody", routing_from_match({}, "xmpp:a@b.example"), false)

-- And the gate as resolve_destination reports it, which is what the handler branches on.
local function gated(name, to, from, want_peer, want_kind)
	local _, _, peer, _, _, kind = resolve_destination(to, from)
	check(name .. " peer", peer, want_peer)
	check(name .. " kind", kind, want_kind)
end
gated("a permitted caller is routed", "015112345678", "xmpp:someone@xmpp.example.com",
	"pstn-02012345678", "routed")
gated("an unlisted caller is forbidden", "015112345678", "xmpp:someone@example.net",
	nil, "forbidden")
gated("an unidentified caller is forbidden", "015112345678", nil, nil, "forbidden")
-- A line label resolves a line, so it is gated exactly like a routed number.
gated("a line label is gated too", component_to("015112345678#office"),
	"xmpp:someone@example.net", nil, "forbidden")
gated("and allowed for a listed caller", component_to("015112345678#office"),
	"xmpp:someone@xmpp.example.com", "pstn-02012345671", "line")
-- A trunk-host label is gated by the line of that host's default registration rather than
-- refused outright: it reaches a credentialed peer, and is still checked.
gated("a trunk label is gated by its default line", component_to("015112345678#sip.example.net"),
	"xmpp:someone@xmpp.example.com", "pstn-02012345678", "trunk")
gated("and refused for an unlisted caller", component_to("015112345678#sip.example.net"),
	"xmpp:someone@example.net", nil, "forbidden")
-- The emergency rule pins a registration and carries no line: reachable by anyone, which is
-- the entire point of it.
gated("110 from an unlisted caller", "110", "xmpp:someone@example.net",
	"pstn-02012345678", "routed")
gated("112 from an unidentified caller", "112", nil, "pstn-02012345678", "routed")

-- The EXTENSION COMES FIRST. PJSIP/<endpoint> with no extension dials the AOR's contact,
-- which on a trunk is the registrar itself, so getting this backwards is a call to the
-- carrier rather than to the callee.
check("dial string", dial_string(routing, "eventpbx-5552", "5550"),
	"PJSIP/5550@eventpbx-5552")
check("and on the PSTN trunk", dial_string(routing, "pstn-02012345671", "+4915112345678"),
	"PJSIP/+4915112345678@pstn-02012345671")

-- MessageSend: the endpoint before the slash carries the credentials and the transport, and
-- the URI after it is only the address. An endpoint name in the host there resolves nowhere,
-- and a bare URI would leave uncredentialed -- the ungated-trunk shape this exists to prevent.
check("message target names the endpoint",
	message_target(routing, "eventpbx-5552", "5550"),
	"pjsip:eventpbx-5552/sip:5550@voip.example.org")
-- The host after the slash is the REGISTRAR, not the endpoint name: it is what goes in the
-- Request-URI, and an endpoint name there resolves nowhere.
check("and on the PSTN trunk", message_target(routing, "pstn-02012345671", "+4915112345678"),
	"pjsip:pstn-02012345671/sip:+4915112345678@sip.example.net")

-- End to end: the rule, the gate, and the technology the call actually leaves by.
local function dialled(name, localpart, from, want)
	local _, _, peer, exten = resolve_destination(component_to(localpart),
		from and ("xmpp:" .. from) or permitted)
	check(name, peer and dial_string(routing, peer, exten) or nil, want)
end
dialled("a short extension goes out pjsip", "5550", nil, ev("PJSIP/5550@eventpbx-5552"))
dialled("an ordinary number goes out the household registration", "015112345678", nil,
	"PJSIP/015112345678@pstn-02012345678")
dialled("110 leaves by the registration the rule pins", "110", "someone@example.net",
	"PJSIP/110@pstn-02012345678")
dialled("112 too", "112", nil, "PJSIP/112@pstn-02012345678")
dialled("a refused caller dials nothing", "5550", "someone@example.net", nil)

-- pjsip cannot carry a JID in the Contact user part -- it parses the URI it builds and the
-- registration object would not load -- so the eventpbx contact is its own number, and it
-- is a per-DID match like any other rather than the host's fallback.
attribution("the pjsip contact matches per-DID", "5552", "voip.example.org", ev("contact"))
attribution("and by endpoint on the same host", "5552", "eventpbx-5552", ev("contact"))

-- EVERY DID, both directions, straight off the live table rather than case by case: a DID
-- added without a line, or an endpoint renamed out of step with the entry that names it, is
-- a call that rings the wrong household or none.
for name, reg in pairs(routing.registrations) do
	if reg.disabled then
		-- No pjsip object, so nothing arrives on it; and a line it is primary of is off.
		check("inbound: disabled " .. name .. " attributes nothing",
			(inbound_line(routing, reg.contact_user, reg.peer)), nil)
		if routing.lines[reg.line].primary == name then
			local _, _, peer, _, _, kind = resolve_destination(
				component_to("+4915112345678#" .. reg.line), permitted)
			check("outbound: #" .. reg.line .. " is refused", peer, nil)
			check("outbound: #" .. reg.line .. " is disabled", kind, "disabled")
		end
	else
		-- INBOUND: pjsip matched the call to this registration's endpoint, so the endpoint name
		-- alone must name the line. This is the attribution that does not depend on the carrier
		-- echoing anything.
		check("inbound: " .. name .. " endpoint is its line",
			(inbound_line(routing, nil, reg.peer)), reg.line)
		-- And with the contact the carrier would echo, which must agree.
		check("inbound: " .. name .. " contact is its line",
			(inbound_line(routing, reg.contact_user, reg.peer)), reg.line)
		check("inbound: " .. name .. " contact is a per-DID match",
			select(4, inbound_line(routing, reg.contact_user, reg.peer)), "contact")
		-- No "@" in a contact: pjsip parses the Contact URI it builds, so one there makes the
		-- whole registration object fail to load and this DID never registers at all.
		check("inbound: " .. name .. " contact is a legal sip user part",
			reg.contact_user:find("@") == nil, true)
		-- OUTBOUND: the line label a caller on this DID is rendered as resolves back to its own
		-- line, and leaves by its PRIMARY -- which is what pins the presented number.
		local _, _, peer, _, _, kind, line = resolve_destination(
			component_to("+4915112345678#" .. reg.line), permitted)
		check("outbound: #" .. reg.line .. " is a line", kind, "line")
		check("outbound: #" .. reg.line .. " resolves to its own line", line, reg.line)
		check("outbound: #" .. reg.line .. " leaves by its primary",
			peer, routing.lines[reg.line].primary)
		-- Whatever it leaves by is dialled through its own endpoint, never its host.
		check("outbound: " .. name .. " dials its own endpoint",
			dial_string(routing, reg.peer, "123"), "PJSIP/123@" .. reg.peer)
	end
end

-- THE EMERGENCY WAIVER, stated as what it is rather than as one destination that happens to
-- work: the rule pins a registration on the PSTN trunk, and the line that registration
-- belongs to REFUSES the caller who nonetheless gets through.
local emergency_rule
for _, r in ipairs(routing.outbound) do
	if r.emergency then emergency_rule = emergency_rule or r end
end
-- Indexed through a default, so a table that lost the rule REPORTS every assertion below
-- instead of raising on the first and hiding the rest.
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
-- Without the waiver the same call is refused, which is what proves the waiver is doing it
-- and not a permissive callers list somewhere.
check("the same caller is refused an ordinary number",
	select(6, resolve_destination(component_to("015112345678"), stranger)), "forbidden")

if failures > 0 then
	print(failures .. " failure(s)")
	os.exit(1)
end
print("lua dialplan tests OK")
