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
-- rules exercised below are the live ones.

local APP, ROUTING = ...
if not APP or APP == "" or not ROUTING or ROUTING == "" then
	print("usage: lua5.1 dialplan_test.lua <component dir> <rendered routing.lua>")
	os.exit(1)
end

local env = {
	SIP_HOST = "sip.example.net",
	SIP_TRUNKS = "sip.example.net,voip.example.org",
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
	"015112345678", nil, "sip.example.net", "015112345678", "routed")

-- 1b. short all-digit string stays on the internal trunk, not PSTN.
case("short extension", "5550",
	"5550", nil, "voip.example.org", "5550", "routed")

-- 2. roster contact on the PSTN trunk. This is the regression: without the suffix strip
--    the label reads "sip.example.net@sip.example.net", matches nothing, and is dialled as DNS.
case("roster contact, pstn trunk", "+4915112345678#sip.example.net",
	"+4915112345678@sip.example.net", "sip.example.net", "sip.example.net", "+4915112345678", "trunk")

-- 3. roster contact on the other trunk: that trunk, not the default.
case("roster contact, other trunk", "5550#voip.example.org",
	"5550@voip.example.org", "voip.example.org", "voip.example.org", "5550", "trunk")

-- 4. A LINE label -- the form every inbound caller is now rendered as, so it is also the
--    form dialled back. It resolves through the line's primary registration to that
--    registration's PEER, which carries the credentials and pins the presented number.
--    office's peer is named after the registration rather than after the host: three DIDs
--    share sip.example.net and fromuser is pinned per peer, so a peer named after the host could
--    present only one of them. eventpbx's primary has a hand-written peer, so there it is
--    still the host.
case("line label", "+4915112345678#office",
	"+4915112345678@office", "office", "pstn-02012345671", "+4915112345678", "line")
case("line label, other line", "+4915112345678#eventpbx",
	"+4915112345678@eventpbx", "eventpbx", "voip.example.org", "+4915112345678", "line")
-- A client that upper-cased the label still reaches the line; line names are lowercase.
case("line label case-insensitive", "+4915112345678#OFFICE",
	"+4915112345678@OFFICE", "OFFICE", "pstn-02012345671", "+4915112345678", "line")

-- 5. Neither a trunk of ours nor a line: FAILS CLOSED. This used to fall through to
--    SIP/<user>@<label>, a dial with no credentials that resolved the label as DNS -- which
--    a carrier may answer with a recorded refusal, i.e. a connected call rather than an
--    error. `kind` is what the jingle handler refuses on.
case("unknown label fails closed", "alice#pbx.example.net",
	"alice@pbx.example.net", "pbx.example.net", nil, "alice", "unknown")
-- The same, for a label that merely looks like one of ours.
case("near-miss trunk label fails closed", "+4915112345678#sip.example.net.evil.example",
	"+4915112345678@sip.example.net.evil.example", "sip.example.net.evil.example", nil,
	"+4915112345678", "unknown")

-- Case differences in the appended host must still strip.
case("autohost case-insensitive", "+4915112345678#SIP.EXAMPLE.NET",
	"+4915112345678@SIP.EXAMPLE.NET", "SIP.EXAMPLE.NET", "sip.example.net", "+4915112345678", "trunk")

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
	"sip.example.net", "home", true)
routed("112", "112", "admin@xmpp.example.com", "sip.example.net", "home", true)

-- Anchoring. Unanchored, "11[02]" would match inside 0112345 and hand an ordinary number an
-- emergency waiver. Same trunk either way, so `emergency` is the assertion that can fail.
routed("0112345 is not an emergency number", "0112345", "admin@xmpp.example.com",
	"sip.example.net", "home", false)
routed("1102 is not an emergency number", "1102", "admin@xmpp.example.com",
	"voip.example.org", "eventpbx", false)

-- Ordering again, at the other boundary: five digits still match the short rule, six fall
-- past it to the catch-all.
routed("five digits", "12345", "admin@xmpp.example.com", "voip.example.org", "eventpbx", false)
routed("six digits", "123456", "admin@xmpp.example.com", "sip.example.net", "home", false)

-- The per-person rule: same destination, different caller, different line -- and a PEER of its
-- own, which is the whole point. Without one the call would leave by the host-named peer and
-- present the household number whatever the table said.
routed("office caller presents office", "015112345678", "office@xmpp.example.com",
	"pstn-02012345671", "office", false)
routed("anyone else still gets the household line", "015112345678", "admin@xmpp.example.com",
	"sip.example.net", "home", false)
-- A caller the narrowed rule excludes falls past it to the catch-all. Refusal is the LINE's
-- job, asserted separately; narrowing never refuses.
routed("an excluded caller falls past the from_glob rule", "015112345678", nil,
	"sip.example.net", "home", false)

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
attributed("contact picks the office line", "02012345671@xmpp.example.com", "sip.example.net", "office")
attributed("the other office DID, same line", "02012345672@xmpp.example.com", "sip.example.net", "office")
-- The entrypoint's own registration is not rendered into registrations.tsv -- the image
-- builds that register line itself -- but it IS in the table, so it resolves like any other.
attributed("entrypoint contact picks the household line", "admin@xmpp.example.com", "sip.example.net", "home")
-- And it is the fallback for the same host when the carrier stops echoing our contact, so
-- per-DID inbound degrades to what it was before lines rather than to nothing.
attributed("unknown contact falls back to the entrypoint's line", "someone@example.net",
	"sip.example.net", "home")
attributed("no contact at all falls back the same way", nil, "sip.example.net", "home")
-- A host with exactly one registration needs no entrypoint entry to be unambiguous.
attributed("sole registration on a host", nil, "voip.example.org", "eventpbx")
attributed("unknown peer has no line", "admin@xmpp.example.com", "pbx.example.net", nil)
-- Several peers now share one address, and which of them chan_sip attributes an
-- unauthenticated inbound INVITE to is not predictable. A registration's own peer name
-- therefore has to resolve to the same host as the host-named peer, or an inbound call that
-- landed on the "wrong" one would lose its line -- on the household number as well.
attributed("a per-DID peer resolves like its host", "02012345672@xmpp.example.com",
	"pstn-02012345671", "office")
attributed("and still falls back to the host's entrypoint line", "someone@example.net",
	"pstn-02012345671", "home")
attributed("no peer has no line", "admin@xmpp.example.com", nil, nil)

-- HOW it was attributed, which is what the handler NOTICEs on. A contact match and a
-- fallback are otherwise indistinguishable: the carrier quietly ceasing to echo our Contact
-- collapses every DID on a host onto one line and logs nothing, which is how it would be
-- found as "all calls ring the wrong line" rather than as a line in the log.
local function attribution(name, extension, peername, want)
	local _, _, _, how = inbound_line(routing, extension, peername)
	check(name, how, want)
end
attribution("a contact match says so", "02012345671@xmpp.example.com", "sip.example.net", "contact")
attribution("the entrypoint fallback is marked", "someone@example.net", "sip.example.net", "entrypoint")
-- A host with one registration answers everything, so the fallback is invisible there too.
attribution("the sole-registration fallback is marked", "someone@example.net",
	"voip.example.org", "sole")
-- Asking for the default outright -- the message path, which has no contact -- is a
-- fallback like any other, and the handler NOTICEs only what carried a contact.
attribution("no contact is the fallback too", nil, "sip.example.net", "entrypoint")
attribution("an unknown peer attributes nothing", "admin@xmpp.example.com", "pbx.example.net", nil)

-- What a caller is rendered as. The LINE occupies the slot the trunk host used to, so the
-- XMPP form is "+49...#office" -- which is also the form resolve_destination resolves above,
-- and the two must stay symmetric or people get a contact they cannot call back.
check("from uri carries the line",
	call_from_uri("<sip:+4915112345678@10.20.2.3>", "sip.example.net", "office"),
	"<sip:+4915112345678@office>")
check("from uri as a JID localpart",
	sip_uri_to_local("+4915112345678@office"), "+4915112345678#office")
check("and back", local_to_sip_uri("+4915112345678#office"), "+4915112345678@office")
-- No line: the trunk host, which is what this did before lines. Degraded identity, but the
-- call still connects and the label still dials back.
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
-- Whatever it produces has to survive the kick command's character class, or an answered
-- Matrix call could enter a conference that can never be ended.
-- The class admits "+" for exactly this; it is not a shell metacharacter.
check("line conference is kickable",
	matrix_kick_command(matrix_conference_name("<sip:+4915112345678@office>", "office")),
	"asterisk -rx 'confbridge kick sip-office-+4915112345678 all'")

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
	"sip.example.net", "+4915112345678", "sip-4915112345678", nil)

-- The request URI host is the gateway's own Service name and says nothing about routing.
route("request uri host ignored", "+4915112345678@cheogram-sip-internal.example.svc",
	"sip-4915112345678", "sip.example.net", "+4915112345678", "sip-4915112345678", nil)

-- A short all-digit destination keeps going to the internal trunk, exactly as the XMPP
-- path routes it: normalize_e164 leaves it alone and resolve_destination decides.
route("short extension keeps the short trunk", "5550", "sip-5550",
	"voip.example.org", "5550", "sip-5550", nil)

-- Not E.164 yet: normalised before the trunk is chosen, so it cannot route differently
-- from the same number written with a plus.
route("00 prefix normalised", "004915112345678", "sip-4915112345678",
	"sip.example.net", "+4915112345678", "sip-4915112345678", nil)

-- Fails closed. 1 -> 404, so the bridge sees the same answer as before this path existed.
route("not a number", "alice@pbx.example.net", "sip-4915112345678", nil, nil, nil, 1)
route("no conference header", "+4915112345678", nil, nil, nil, nil, 1)
route("empty conference header", "+4915112345678", "", nil, nil, nil, 1)
-- A comma would be read by Originate() as a further argument.
route("conference name with a comma", "+4915112345678", "sip-49151,app,Echo",
	nil, nil, nil, 1)

-- Whitespace around a header value is the transport's, not the name's.
route("conference header trimmed", "+4915112345678", "  sip-4915112345678 ",
	"sip.example.net", "+4915112345678", "sip-4915112345678", nil)

-- Routing table missing or malformed: 34 -> 503, rather than dialling anonymously, which a
-- carrier may answer with a recorded refusal -- a connected call rather than an error.
sip_routing = nil
route("no routing table", "+4915112345678", "sip-4915112345678", nil, nil, nil, 34)
sip_routing = routing

-- Originate's statuses, mapped to the causes chan_sip turns into SIP responses.
check("cause BUSY", matrix_outbound_cause("BUSY"), 17)
check("cause CONGESTION", matrix_outbound_cause("CONGESTION"), 34)
check("cause HANGUP", matrix_outbound_cause("HANGUP"), 19)
check("cause FAILED", matrix_outbound_cause("FAILED"), 34)
-- Anything unknown, and a variable that was never set, still hang the leg up.
check("cause UNKNOWN", matrix_outbound_cause("UNKNOWN"), 34)
check("cause nil", matrix_outbound_cause(nil), 34)

-- Ending a conference when a leg hangs up. This is the whole of the fix for "the SIP
-- caller hangs up and the Element call stays open": end_marked cannot do it, so the
-- hangup extension runs this command instead.
check("kick command", matrix_kick_command("sip-4915112345678"),
	"asterisk -rx 'confbridge kick sip-4915112345678 all'")
-- A leg that never joined a conference must produce no command at all, because every
-- ordinary inbound call and every SIP MESSAGE passes through the same handler.
check("kick no conference", matrix_kick_command(nil), nil)
check("kick empty conference", matrix_kick_command(""), nil)
-- The name is pasted into a shell command line, so anything outside the class the
-- routing already enforces fails closed rather than being quoted and hoped about.
check("kick rejects a quote", matrix_kick_command("sip-1' ; rm -rf /"), nil)
check("kick rejects a space", matrix_kick_command("sip-1 all"), nil)
check("kick rejects a semicolon", matrix_kick_command("sip-1;id"), nil)
check("kick rejects a dollar", matrix_kick_command("sip-$(id)"), nil)
-- Whatever matrix_outbound_route accepts as a conference name must survive to here, or
-- an outbound call could be placed into a conference that can never be ended.
local _, _, routed = matrix_outbound_route("+4915112345678", "sip-4915112345678", matrix_caller)
check("routed conference is kickable", matrix_kick_command(routed),
	"asterisk -rx 'confbridge kick sip-4915112345678 all'")

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
	"5551@eventpbx", nil)
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
sms_line("contact picks the office line", "02012345671@xmpp.example.com",
	"+4915112345678@office", "office", "contact")
sms_line("the other office DID, same line", "02012345672@xmpp.example.com",
	"+4915112345678@office", "office", "contact")
sms_line("the entrypoint contact picks the household line", "admin@xmpp.example.com",
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
sms_matches_call("office DID", "02012345671@xmpp.example.com")
sms_matches_call("entrypoint DID", "admin@xmpp.example.com")
sms_matches_call("unmatched contact", "someone@example.net")
sms_matches_call("no contact", nil)

-- What the inbound handler addresses the bridge as. The host is the PEER name, not a
-- hostname; the "+" in the From is what keeps the bridge from dropping the message while
-- answering 200 OK.
local to, from = sms_bridge_message("+4915112345678@home", "4915112345678")
check("bridge to", to, "sip:+492012345678@matrixbridge")
-- With a registration it is that DID rather than SIP_USER, which is the honest value once a
-- host carries more than one of our numbers.
local did_to = sms_bridge_message("+4915112345678@office", "4915112345678",
	{did = "+492012345671"})
check("bridge to uses the registration's did", did_to, "sip:+492012345671@matrixbridge")
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
sms_route("e164 to default trunk", "+4915112345678", "sip.example.net", "+4915112345678")
-- The request URI host is the gateway's own Service name and says nothing about routing.
sms_route("request uri host ignored", "sip:+4915112345678@cheogram-sip-internal.example.svc:5060",
	"sip.example.net", "+4915112345678")
sms_route("short extension keeps the short trunk", "5550", "voip.example.org", "5550")
sms_route("00 prefix normalised", "004915112345678", "sip.example.net", "+4915112345678")
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

-- An XMPP user's message takes this same route now; it used to reach the trunk at a bare
-- SIP URI, gated by nothing. `permitted` is a listed caller, so the gate is what differs.
local function xmpp_route(name, to_uri, from, want_peer, want_num)
	local peer, num = sms_outbound_route(local_to_sip_uri(to_uri), from)
	check(name .. " peer", peer, want_peer)
	check(name .. " num", num, want_num)
end
xmpp_route("xmpp sender reaches the trunk", "+4915112345678", permitted,
	"sip.example.net", "+4915112345678")
xmpp_route("an unlisted xmpp domain is refused", "+4915112345678",
	"xmpp:someone@elsewhere.example", nil, nil)
xmpp_route("an unidentified xmpp sender is refused", "+4915112345678", nil, nil, nil)
-- The #label form: the label is dropped with the host part, so the table picks the line
-- rather than "home" being dialled as a SIP host, which is what the old handler did.
xmpp_route("a #label resolves through the table", "+4915112345678#home", permitted,
	"sip.example.net", "+4915112345678")

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
local matrix_leg = "SIP/matrixbridge/" .. plan_caller

legs("xmpp only", {ring = {"xmpp:admin@xmpp.example.com"}}, xmpp_leg, false)
legs("xmpp and matrix ring together", {ring = {"xmpp:admin@xmpp.example.com", "matrix:@admin:example.com"}},
	xmpp_leg .. "&" .. matrix_leg, true)
-- Every matrix: entry names the same conference, so a second leg is one room rung twice.
legs("several matrix targets collapse to one leg",
	{ring = {"matrix:@admin:example.com", "matrix:@other:example.com"}}, matrix_leg, true)
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
-- own peer name, which is what chan_sip matches the request on.
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
	"sip.example.net", "routed")
gated("an unlisted caller is forbidden", "015112345678", "xmpp:someone@example.net",
	nil, "forbidden")
gated("an unidentified caller is forbidden", "015112345678", nil, nil, "forbidden")
-- A line label resolves a line, so it is gated exactly like a routed number.
gated("a line label is gated too", component_to("015112345678#office"),
	"xmpp:someone@example.net", nil, "forbidden")
gated("and allowed for a listed caller", component_to("015112345678#office"),
	"xmpp:someone@xmpp.example.com", "pstn-02012345671", "line")
-- A raw trunk label predates lines and still sits in rosters, so it is gated by the line of
-- that host's default registration rather than refused: an old contact keeps working, and it
-- is still checked.
gated("a trunk label is gated by its default line", component_to("015112345678#sip.example.net"),
	"xmpp:someone@xmpp.example.com", "sip.example.net", "trunk")
gated("and refused for an unlisted caller", component_to("015112345678#sip.example.net"),
	"xmpp:someone@example.net", nil, "forbidden")
-- The emergency rule pins a registration and carries no line: reachable by anyone, which is
-- the entire point of it.
gated("110 from an unlisted caller", "110", "xmpp:someone@example.net",
	"sip.example.net", "routed")
gated("112 from an unidentified caller", "112", nil, "sip.example.net", "routed")

if failures > 0 then
	print(failures .. " failure(s)")
	os.exit(1)
end
print("lua dialplan tests OK")
