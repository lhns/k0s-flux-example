-- Our copy of the image's /etc/asterisk/extensions.lua. It deliberately omits the comment
-- "-- outbound calls", so the entrypoint's sed that would hardcode a single caller there is a
-- no-op -- and a sed that matches nothing exits 0, so the gate in resolve_destination is all
-- there is. COMPONENT_DOMAIN and the routing table come from the environment, not from that
-- sed. Re-diff against the image after a digest bump; see the README.

-- Fall back to something obviously broken rather than concatenating nil into every call.
local component_domain = os.getenv("COMPONENT_DOMAIN") or "COMPONENT_DOMAIN-unset"

-- Our own trunk hosts, keyed lowercase. A destination whose label is one of these is
-- dialled through that peer, so it carries the registration credentials. The label after
-- "#" is now either one of these or a LINE name; see resolve_destination.
local trunks = {}
for name in (os.getenv("SIP_TRUNKS") or os.getenv("SIP_HOST") or ""):gmatch("[^,%s]+") do
	trunks[name:lower()] = name
end

-- The Matrix bridge's chan_sip peer. It is the name of the [matrixbridge] section, the
-- From user part of everything the bridge sends, and -- because chan_sip resolves a peer
-- before DNS -- the host we address a MESSAGE back to. All three must agree with
-- sip.username in the bridge's own config.
local bridge_peer = os.getenv("SIP_BRIDGE_PEER") or "matrixbridge"

-- loadfile + pcall, never dofile: a malformed table must not raise in THIS chunk and take
-- inbound down too. nil fails outbound CLOSED -- no peer, every caller refused.
function load_routing(path)
	local chunk = path and loadfile(path)
	if not chunk then return nil end
	local ok, tbl = pcall(chunk)
	if not ok or type(tbl) ~= "table"
			or type(tbl.registrations) ~= "table" or type(tbl.lines) ~= "table"
			or type(tbl.outbound) ~= "table" then
		return nil
	end
	return tbl
end

-- Global, and the path is overridable, so a test can point it at the chart's rendered
-- output instead of needing a file at the Asterisk location.
sip_routing = load_routing(os.getenv("SIP_ROUTING_FILE") or "/etc/asterisk/routing.lua")

-- Does an address pattern from the routing table cover this caller? "*" is anyone,
-- "xmpp:*"/"matrix:*" anyone on that transport, "xmpp:*@domain" and "matrix:@*:server" any
-- localpart there; anything else has to match literally.
function routing_glob_match(glob, addr)
	if type(glob) ~= "string" or type(addr) ~= "string" then return false end
	glob, addr = glob:lower(), addr:lower()
	if glob == "*" then return true end
	if glob == "xmpp:*" then return addr:sub(1, 5) == "xmpp:" end
	if glob == "matrix:*" then return addr:sub(1, 7) == "matrix:" end
	local domain = glob:match("^xmpp:%*@(.+)$")
	if domain then return addr:match("^xmpp:[^@]+@(.+)$") == domain end
	local server = glob:match("^matrix:@%*:(.+)$")
	if server then return addr:match("^matrix:@[^:]+:(.+)$") == server end
	return glob == addr
end

-- A line by name, case-folded because it arrives as the label a client typed after "#".
-- Returns the line entry and its canonical name, or nil.
function routing_line(tbl, name)
	if type(tbl) ~= "table" or type(tbl.lines) ~= "table" then return nil end
	local want = tostring(name or ""):lower()
	if want == "" then return nil end
	for lname, l in pairs(tbl.lines) do
		if type(l) == "table" and tostring(lname):lower() == want then return l, tostring(lname) end
	end
	return nil
end

-- The registration an outbound rule presents: the pinned `registration` (emergency only),
-- otherwise the line's `primary`. Returns the entry and its name, or nil for anything the
-- table does not define -- which refuses the call, because guessing a peer is how one
-- reaches a carrier that answers with a recorded refusal rather than an error.
function routing_registration(tbl, rule)
	if type(tbl) ~= "table" or type(tbl.registrations) ~= "table"
			or type(rule) ~= "table" then
		return nil
	end
	local name = rule.registration
	if name == nil then
		local l = routing_line(tbl, rule.line)
		if type(l) ~= "table" then return nil end
		name = l.primary
	end
	local reg = tbl.registrations[tostring(name or "")]
	if type(reg) ~= "table" or type(reg.host) ~= "string" or reg.host == "" then return nil end
	return reg, tostring(name)
end

-- The peer an outbound call LEAVES BY, which is not the host once several registrations share
-- one: fromuser is pinned per peer, so each presentable DID has its own. See the README.
function registration_peer(reg)
	if type(reg) ~= "table" then return nil end
	if type(reg.peer) == "string" and reg.peer ~= "" then return reg.peer end
	if type(reg.host) == "string" and reg.host ~= "" then return reg.host end
	return nil
end

-- The host a peer name stands for. Which of several peers at one address chan_sip attributes
-- an inbound INVITE to is not predictable, so they must all resolve alike. Unrecognised names
-- are returned unchanged, so a trunk peer keeps meaning itself.
local function peer_host(tbl, peer)
	for _, r in pairs(tbl.registrations) do
		if type(r) == "table" and tostring(r.peer or ""):lower() == peer
				and tostring(r.host or "") ~= "" then
			return tostring(r.host):lower()
		end
	end
	return peer
end

-- Does a rule's from_glob list cover this caller? No list is no narrowing, so anyone.
-- NARROWING, not permission: a caller this excludes falls through to the next rule. The gate
-- is line_permits.
function routing_from_match(globs, addr)
	if globs == nil then return true end
	if type(globs) ~= "table" then return false end
	for _, g in ipairs(globs) do
		if routing_glob_match(g, addr) then return true end
	end
	return false
end

-- May this caller present this line? THE OUTBOUND GATE. Fails closed: an unknown line, a
-- table with no callers list, an empty one, or no caller at all all refuse. Refuses rather
-- than falling through, so revoking someone is one edit and cannot leak to another line.
function line_permits(tbl, name, addr)
	local l = routing_line(tbl, name)
	if type(l) ~= "table" or type(l.callers) ~= "table" or #l.callers == 0 then return false end
	for _, g in ipairs(l.callers) do
		if routing_glob_match(g, addr) then return true end
	end
	return false
end

-- FIRST MATCH WINS over the table's outbound rules. `dest` is the destination with any
-- label already stripped; `from_uri` is the caller as "xmpp:user@domain", or nil on the
-- paths that carry no caller identity (the Matrix bridge, and SMS), where a rule narrowed
-- by from_glob cannot match and the next rule decides instead.
--
-- Returns the rule, its registration and that registration's name, or nil. to_pattern
-- arrives anchored from the renderer, so a rule cannot forget to. Derived only from its
-- arguments, so tests/ can exercise it.
--
-- A rule whose line or registration the table does not define returns nil rather than
-- trying the next rule: the table is inconsistent, and a later rule would send the call
-- out a peer nobody chose.
function routing_match(tbl, dest, from_uri)
	if type(tbl) ~= "table" or type(tbl.outbound) ~= "table" then return nil end
	for _, r in ipairs(tbl.outbound) do
		if type(r.to_pattern) == "string" and tostring(dest):match(r.to_pattern)
				and routing_from_match(r.from_glob, from_uri) then
			local reg, regname = routing_registration(tbl, r)
			if not reg then return nil end
			return r, reg, regname
		end
	end
	return nil
end

-- Which registration an INBOUND call or message arrived on, and therefore which line.
--
-- `extension` is the register contact the carrier dialled back, which is the only per-DID
-- discriminator chan_sip offers -- and only while the carrier echoes our registered Contact
-- in the Request-URI. That is observed behaviour, not a standard. When it stops matching,
-- this falls back to the host's own default registration (the `entrypoint` one, or the sole
-- one there) and per-DID inbound silently collapses to that one line, which is exactly the
-- behaviour that predates lines. Pass extension = nil to ask for that default outright: the
-- message path has a host but no contact.
--
-- The third return says HOW: "contact" is a real per-DID match, "entrypoint" and "sole" are
-- the fallback. The two are indistinguishable downstream, so the collapse above is invisible
-- without it; the call site NOTICEs anything but "contact".
function inbound_registration(tbl, extension, peername)
	if type(tbl) ~= "table" or type(tbl.registrations) ~= "table" then return nil end
	local ext = tostring(extension or ""):lower()
	local peer = tostring(peername or ""):lower()
	if peer == "" then return nil end
	peer = peer_host(tbl, peer)
	local entry, entryname, sole, solename, several
	for name, r in pairs(tbl.registrations) do
		if type(r) == "table" and tostring(r.host or ""):lower() == peer then
			if ext ~= "" and tostring(r.contact_user or ""):lower() == ext then
				return r, tostring(name), "contact"
			end
			if r.entrypoint then entry, entryname = r, tostring(name) end
			if sole then several = true else sole, solename = r, tostring(name) end
		end
	end
	if entry then return entry, entryname, "entrypoint" end
	if sole and not several then return sole, solename, "sole" end
	return nil
end

-- The line name for an inbound call or message, plus the registration it was attributed to
-- and how it was attributed.
function inbound_line(tbl, extension, peername)
	local reg, name, how = inbound_registration(tbl, extension, peername)
	if type(reg) == "table" and type(reg.line) == "string" and reg.line ~= "" then
		return reg.line, reg, name, how
	end
	return nil
end

-- Normalise a caller's number to E.164 so the same human always maps to the same JID and
-- the same Matrix portal room. Carriers are inconsistent: +49..., 0049..., 0..., and bare
-- numbers all name the same person, and downstream nothing reconciles them.
--
-- Returns the number unchanged whenever it cannot be normalised CONFIDENTLY. Guessing is
-- worse than not trying: a wrong prefix silently dials a different, valid number.
--
-- Order is load-bearing: "00" must be tested before "0", or 0049... becomes +4949...
local function normalize_e164_raw(num)
	local cc = os.getenv("SIP_E164_COUNTRY")
	if not num or num == "" or not cc or cc == "" then return num end
	if num:match("[^0-9+]") then return num end          -- not a number at all
	if num:sub(1, 1) == "+" then return num end          -- already E.164

	-- Short strings are internal extensions and emergency numbers, never E.164. Guarding
	-- on length is required: a formatter will happily turn 110 into +49110.
	local short = tonumber(os.getenv("SIP_SHORT_MAXLEN") or "5") or 5
	if #num <= short then return num end

	if num:sub(1, 2) == "00" then return "+" .. num:sub(3) end
	if num:sub(1, 1) == "0" then return "+" .. cc .. num:sub(2) end

	-- Bare number, no trunk prefix. Two cases look identical here and need opposite
	-- treatment: a national number with the trunk 0 omitted just needs the country code,
	-- while a subscriber number whose area code is genuinely absent needs the local area
	-- code prepended. Nothing can tell them apart, so only act when SIP_E164_AREA says
	-- which this carrier sends. Unset means leave it alone.
	local area = os.getenv("SIP_E164_AREA")
	if area and area ~= "" then return "+" .. cc .. area .. num end
	return num
end

-- Every rewrite is logged, on all call sites: whether SIP_E164_AREA can be set safely is a
-- question only a week of these lines can answer.
local function normalize_e164(num)
	local out = normalize_e164_raw(num)
	if out ~= num then
		app.log("NOTICE", "normalize_e164: " .. num .. " -> " .. out)
	end
	return out
end

function textBase10Decode(digits)
	if digits:sub(0, 2) == "99" then
		result = ""
		for i = 3,digits:len(),3
		do
			result = result .. string.char(tonumber(digits:sub(i, i+2)))
		end
		return result
	else
		result = ""
		for i = 1,digits:len(),2
		do
			result = result .. string.char(tonumber(digits:sub(i, i+1)) + 30)
		end
		return result
	end
end

function jid_escape(s)
	-- TODO: the class for escaping backslash is overbroad at the moment
	return s
		:gsub("\\([2345][0267face0c])", "\\5c%1")
		:gsub(" ", "\\20")
		:gsub("\"", "\\22")
		:gsub("&", "\\26")
		:gsub("'", "\\27")
		:gsub("/", "\\2f")
		:gsub(":", "\\3a")
		:gsub("<", "\\3c")
		:gsub(">", "\\3e")
		:gsub("@", "\\40")
end

function jid_unescape(s)
	return s
		:gsub("\\20", " ")
		:gsub("\\22", "\"")
		:gsub("\\26", "&")
		:gsub("\\27", "'")
		:gsub("\\2f", "/")
		:gsub("\\3a", ":")
		:gsub("\\3c", "<")
		:gsub("\\3e", ">")
		:gsub("\\40", "@")
		:gsub("\\5c", "\\")
end

-- A SIP address has to live in a JID localpart, which cannot contain "@". XEP-0106 would
-- escape it to  , which is correct but unreadable. "#" is legal in a localpart and is
-- one of the few characters RFC 3261 does NOT allow unescaped in a SIP user part, so the
-- split back to user@host stays unambiguous.
--
-- Cost: "#" starts a fragment in URI syntax, so a JID rendered into an xmpp: URI needs it
-- percent-encoded. Clients that build such links must handle that.
function sip_uri_to_local(uri)
	return (uri:gsub("@", "#"))
end

-- The inverse, for a destination dialled back from a client.
function local_to_sip_uri(s)
	return (s:gsub("#", "@"))
end

-- Pick a peer for an outbound destination, and split it into user part and label.
-- Returns to, label, peer, exten, rule, kind, line -- all derived, nothing read from the
-- channel, so tests/ can exercise it.
--
-- The component is handed SIP_HOST as its PSTN gateway and appends "@SIP_HOST" to any
-- destination whose localpart carries no host of its own. A roster contact's "user#label"
-- localpart has none as far as the component can tell, so it arrives here as
-- "user@label@SIP_HOST": the suffix must be stripped BEFORE the label is derived, or the
-- label is "label@SIP_HOST", matches nothing, and chan_sip resolves it as a DNS name.
--
-- A label equal to SIP_HOST therefore expresses no intent and is gone after the strip,
-- which is what keeps short extensions off the PSTN trunk.
--
-- `kind` is the fail-closed part: "trunk", "line" or "routed" reached a peer;
-- "unknown", "unrouted" and "forbidden" refuse. There is no fall-through to an
-- uncredentialed dial, which a carrier can answer with a recorded refusal.
--
-- `rule` is nil for every explicit label, so `emergency` is only ever set on a routed one.
function resolve_destination(to, from_uri)
	local autohost = os.getenv("SIP_HOST") or ""
	if autohost ~= "" then
		local suffix = "@" .. autohost
		if to:lower():sub(-#suffix) == suffix:lower() then
			to = to:sub(1, #to - #suffix)
		end
	end
	local label = to:match("@(.+)$")
	local peer, rule, kind, line
	if label then
		peer = trunks[label:lower()]
		if peer then
			-- A raw trunk label predates lines and still sits in rosters. Gate it by the
			-- line of that host's default registration, so an old contact keeps working and
			-- is still checked. No unambiguous default means no line, which the gate refuses.
			kind = "trunk"
			line = inbound_line(sip_routing, nil, peer)
		else
			local l, lname = routing_line(sip_routing, label)
			if l then
				kind, line = "line", lname
				local reg = routing_registration(sip_routing, {line = lname})
				peer = registration_peer(reg)
				if not peer then kind = "unrouted" end
			else
				kind = "unknown"
			end
		end
	else
		local r, reg = routing_match(sip_routing, to, from_uri)
		if r then
			rule, peer, kind = r, registration_peer(reg), "routed"
			line = r.line or reg.line
		else
			kind = "unrouted"
		end
	end

	-- THE GATE, on the line the destination resolved to, so a labelled destination is
	-- checked as well as a routed one. An emergency rule pins a registration and carries no
	-- line, which is what lets 110/112 out from an account permitted nothing else. A trunk
	-- label has no line to check, so it is refused outright rather than assumed.
	if not (rule and rule.emergency) then
		if peer and not line_permits(sip_routing, line, from_uri) then
			peer, kind = nil, "forbidden"
		end
	end
	-- label was captured from the FIRST "@", so this is the part before it either way.
	local exten = (label and to:sub(1, #to - #label - 1) or to)
	return to, label, peer, exten, rule, kind, line
end

-- Hangup causes for what Originate reports back, picked for the SIP response chan_sip
-- derives from each: 17 -> 486, 34 -> 503, 19 -> 480, 1 -> 404. The bridge fails the call
-- on any non-2xx alike, so these are for whoever reads the trace, not for its logic.
local originate_cause = {
	BUSY = 17,
	CONGESTION = 34,
	HANGUP = 19,
	RINGING = 19,
	FAILED = 34,
}

function matrix_outbound_cause(status)
	return originate_cause[status or ""] or 34
end

-- The CLI command that ends a matrix conference, or nil plus a reason.
--
-- ConfBridge's end_marked only fires when the LAST MARKED user leaves, and livekit-sip
-- is the only marked user: it ends the caller when livekit-sip goes, and does nothing at
-- all in the other direction. A caller hanging up therefore leaves livekit-sip sitting in
-- the conference, its LiveKit participant alive, and the Matrix call open until the
-- membership expires hours later. Marking both legs does not help -- the marked count
-- never reaches zero while one of them is there -- and end_marked_any arrived in Asterisk
-- 16.19, after the 16.2 this image ships. So the conference is ended explicitly.
--
-- Pure, so tests/ exercises it. The name is re-checked here even
-- though every caller already validated it, because it is pasted into a shell command.
function matrix_kick_command(conf)
	conf = tostring(conf or "")
	if conf == "" then return nil, "no conference on this channel" end
	if not conf:match("^[%w%-_+]+$") then
		return nil, "unusable conference name: " .. conf
	end
	return "asterisk -rx 'confbridge kick " .. conf .. " all'"
end

-- Hangup handler for every leg that enters a matrix conference. MATRIX_CONF is set on the
-- channel before it joins and cleared once the leg has handed the conference on, so an
-- unset one means there is nothing to end.
--
-- Goes through the CLI socket in this container because that is the only way to reach a
-- conference from a channel that is already gone: Asterisk 16 has no dialplan application
-- that kicks one.
local function matrix_end_conference()
	local conf = tostring(channel.MATRIX_CONF:get() or "")
	local cmd, reason = matrix_kick_command(conf)
	if not cmd then
		-- Every ordinary call passes through here too, so only a name we could not use
		-- is worth a line.
		if conf ~= "" then app.log("NOTICE", "matrix hangup: " .. reason) end
		return
	end
	app.log("NOTICE", "matrix hangup: ending conference " .. conf)
	app.system(cmd)
end

-- Asterisk routes the special extensions h (hangup), i (invalid) and t (timeout) through
-- the same `_.` pattern as a real destination. Naming them explicitly is not only about
-- log noise: in a ConfBridge context `_.` otherwise puts the hung-up channel into a
-- conference literally named "h".
local function matrix_ignore() end

-- Decide where an INVITE from the [matrixbridge] peer goes. Derived only from its
-- arguments, so tests/ exercises it.
-- Returns peer, number, conference, or nil for all three plus a hangup cause and a
-- reason to log.
--
-- Fails CLOSED on anything that is not a plain number: this context is the one place a
-- Matrix user reaches the trunks, and an arbitrary SIP URI must not pass through it.
--
-- The conference name is held to a strict character class because it is passed to
-- Originate() as an argument, where a comma or caret would be read as further arguments.
function matrix_outbound_route(request_exten, conference, caller)
	local num = tostring(request_exten or ""):gsub("@.*$", "")
	-- The bridge sends E.164 with the plus, so this is normally a no-op. Applied anyway
	-- so a destination that ever arrives as 0049... routes like every other path, and
	-- because it leaves short strings alone, which is what lets the routing table still
	-- see an internal extension as one instead of a PSTN number.
	num = normalize_e164(num)
	if not num:match("^%+?[0-9]+$") then
		return nil, nil, nil, 1, "destination is not a number: " .. tostring(request_exten)
	end
	conference = tostring(conference or ""):gsub("^%s+", ""):gsub("%s+$", "")
	if not conference:match("^[%w%-_+]+$") then
		return nil, nil, nil, 1, "missing or unusable conference header"
	end
	-- The same resolver the XMPP path uses, so the two cannot drift on which trunk a
	-- number takes. The caller is the Matrix user the bridge names on the INVITE; without
	-- one, a rule narrowed by from_glob cannot match and the next rule decides.
	local _, _, peer = resolve_destination(num, caller)
	if not peer or peer == "" then
		return nil, nil, nil, 34, "no peer for " .. num
	end
	return peer, num, conference
end

function make_jid(extension, from_header)
	return (
		jid_escape(extension)
		.. "@" .. component_domain .. "/"
		.. jid_escape(sip_uri_to_local(from_header:gsub("^[^<]*<sip:", ""):gsub(">.*$", "")))
	):gsub("\\", "\\\\")
end

-- "sip-<line>-<caller in E.164, plus included>". Calls and messages both derive it here,
-- which is the only thing keeping one human out of two portals on one line. nil without a
-- line or without +E.164: the bridge declines a key it cannot use, so inventing one would
-- fail silently. See the README for the portal-ID shape.
function matrix_conference_name(from, line)
	local digits = tostring(from or ""):match("^<sip:%+([0-9]+)@")
	if not digits then return nil end
	line = tostring(line or "")
	if line == "" then return nil end
	return "sip-" .. line .. "-+" .. digits, digits
end

-- The caller behind a request from the Matrix bridge, as "matrix:@user:server".
--
-- The bridge is matched by address and never challenged, so this is a CLAIM, trusted only
-- as far as that peer is. The scheme is therefore built here and never taken from the wire:
-- anything not shaped like an MXID returns nil, so a value of "xmpp:someone@..." cannot
-- borrow another transport's identity.
function matrix_caller_uri(raw)
	local mxid = tostring(raw or ""):gsub("^%s+", ""):gsub("%s+$", "")
	if not mxid:match("^@[^:@%s]+:[^:@%s]+$") then return nil end
	return "matrix:" .. mxid:lower()
end

-- The display name of a From header, unquoted. The bridge carries the sending Matrix user
-- there because the user part is the peer name and not ours to use; sms_strip_uri discards
-- it, so this has to run before that.
function sms_from_display(header)
	local name = tostring(header or ""):match("^%s*(.-)%s*<")
	if not name then return nil end
	name = name:gsub('^"(.*)"$', "%1")
	if name == "" then return nil end
	return name
end

-- A line's call plan. No line, or a table rendered before inbound_route, falls back to the
-- pre-call-plan behaviour -- ring the dialled contact -- rather than to ringing nobody.
-- The second return marks that fallback: it rings a different set for a different time, and
-- nothing else downstream can tell the two apart.
function inbound_stages(tbl, line, extension)
	local l = routing_line(tbl, line)
	if type(l) == "table" and type(l.inbound_route) == "table" and #l.inbound_route > 0 then
		return l.inbound_route, false
	end
	-- No localpart: matrix: targets are advisory everywhere, since the bridge rings a portal
	-- room and routes on the conference name alone.
	return { { ring = { "xmpp:" .. tostring(extension or ""), "matrix:" }, timeout = 300 } }, true
end

-- One stage as a Dial() string, plus whether it includes the Matrix leg. Every matrix: target
-- collapses to at most ONE leg: they all name the same conference, so a second is the same
-- room rung twice. Without a conference it contributes none -- the bridge would decline it.
function inbound_legs(stage, from, conf, caller)
	if type(stage) ~= "table" or type(stage.ring) ~= "table" then return nil, false end
	local legs, wants_matrix = {}, false
	for _, addr in ipairs(stage.ring) do
		addr = tostring(addr or "")
		local jid = addr:match("^xmpp:(.+)$")
		if jid then
			legs[#legs + 1] = "Motif/jingle-endpoint/" .. make_jid(jid, from)
		elseif addr:match("^matrix:") then
			wants_matrix = true
		end
	end
	local with_matrix = wants_matrix and conf ~= nil and caller ~= nil
	if with_matrix then
		legs[#legs + 1] = "SIP/" .. bridge_peer .. "/" .. caller
	end
	if #legs == 0 then return nil, false end
	return table.concat(legs, "&"), with_matrix
end

-- Rewrite an inbound call's From to <sip:user@line>, the rewrite the two comments at its
-- call site describe. The line occupies the slot the trunk host used to, so the caller
-- appears as "+49...#office" and dialling that back resolves through resolve_destination.
-- Pure, so tests/ can hold it against the message path's sms_inbound_from: the two must
-- agree, or one human is two portal rooms.
--
-- No line (the carrier stopped echoing our contact and the peer has no default) falls back
-- to the trunk host, which is what this did before lines: degraded identity, but the call
-- still connects and the label still dials back.
function call_from_uri(from_header, peername, line)
	local label = (type(line) == "string" and line ~= "" and line)
		or (peername and trunks[tostring(peername):lower()])
	if not label then return from_header end
	local uri = tostring(from_header):gsub("^[^<]*<sip:", ""):gsub(">.*$", "")
	return "<sip:" .. normalize_e164(uri:gsub("@.*$", "")) .. "@" .. label .. ">"
end

-- Reduce a From/To header or a request extension to a bare SIP URI: display name, angle
-- brackets, scheme and any ";param" tail removed. The tail matters here and did not on the
-- call path: a carrier that sends ";user=phone" would otherwise become part of the host.
local function sms_strip_uri(s)
	s = tostring(s or "")
	s = s:gsub("^[^<]*<", ""):gsub(">.*$", "")
	s = s:gsub("^%s+", ""):gsub("%s+$", "")
	s = s:gsub("^[sS][iI][pP][sS]?:", "")
	s = s:gsub("[;%?].*$", "")
	return s
end

-- An inbound MESSAGE's sender, as user@line plus its E.164 digits, the line it arrived on,
-- the registration it was attributed to and how. digits is nil for an alphanumeric sender
-- ID, a short code, or anything normalize_e164 declined.
--
-- The host is rewritten to the LINE, never left as the carrier sent it: the call path does
-- the same from CHANNEL("peername"), which does not exist on a Message channel, so without
-- this one human is +49...#home for a call and +49...#<sbc-ip> for an SMS.
--
-- `extension` is the dialled contact, the same per-DID discriminator the call path passes.
-- Whether a carrier echoes our Contact on a MESSAGE is untested -- one that does not falls
-- back to the host's default registration, which is what this did before.
function sms_inbound_from(from_header, extension)
	local uri = sms_strip_uri(from_header)
	local user, host = uri:match("^([^@]+)@(.+)$")
	if not user then user, host = uri, nil end
	user = normalize_e164(user)
	-- SIP_HOST, not an outbound lookup: this says where a message CAME FROM, and an inbound
	-- carrier MESSAGE arrives on a trunk we register to, whatever host it names itself.
	local peer = (host and trunks[host:lower()]) or os.getenv("SIP_HOST") or host
	local line, reg, _, how = inbound_line(sip_routing, extension, peer)
	local label = line or peer
	if label and label ~= "" then uri = user .. "@" .. label else uri = user end
	local _, digits = matrix_conference_name("<sip:" .. uri .. ">", line)
	return uri, digits, line, reg, how
end

-- Where an inbound message goes on the bridge leg, or nil to leave the bridge out.
--
-- The host is the PEER NAME: chan_sip resolves a peer before DNS, which is what picks up
-- its TCP transport and port. The "+" in `from` is load-bearing -- the bridge rejects a
-- number without one and then drops the message WHILE ANSWERING 200 OK, invisible on both
-- sides. The To is informational to the bridge; our own DID is the honest value.
function sms_bridge_message(from_uri, digits, reg)
	if not digits or digits == "" then return nil end
	local did = type(reg) == "table" and tostring(reg.did or "") or ""
	if did == "" then did = normalize_e164(os.getenv("SIP_USER") or "") end
	if did == "" then did = bridge_peer end
	return "sip:" .. did .. "@" .. bridge_peer, "sip:" .. from_uri
end

-- True when a MESSAGE is the bridge asking us to send an SMS, rather than the carrier
-- delivering one. BOTH tests are required: on the From alone a carrier-injected
-- "From: sip:matrixbridge@..." would be an outbound-SMS primitive on our trunk, billed to
-- us. The trunk's own messages fail both -- their From is a subscriber number and their
-- extension is the JID in the register line's quoted contact.
function sms_is_outbound(from_header, extension)
	local user = sms_strip_uri(from_header):match("^([^@]+)@") or sms_strip_uri(from_header)
	if user:lower() ~= bridge_peer:lower() then return false end
	return tostring(extension or ""):match("^%+?[0-9]+$") ~= nil
end

-- Trunk for an outbound SMS: a sibling of matrix_outbound_route without the conference.
-- Fails CLOSED on anything that is not a plain number, for the same reason -- this is the
-- one path where a Matrix user reaches the trunks.
function sms_outbound_route(to_uri, caller)
	local num = normalize_e164(sms_strip_uri(to_uri):gsub("@.*$", ""))
	if not num:match("^%+?[0-9]+$") then
		return nil, nil, "destination is not a number: " .. tostring(to_uri)
	end
	local _, _, peer = resolve_destination(num, caller)
	if not peer or peer == "" then
		return nil, nil, "no peer for " .. num
	end
	return peer, num
end

-- Deliver an inbound message to XMPP and, when the sender normalised, to Matrix. Shared by
-- messages-in and public so the two cannot name the same human differently.
--
-- XMPP goes FIRST on purpose: the bridge's handler can block for 32s, and a hung bridge
-- must not delay the phone. MESSAGE_SEND_STATUS is ONE channel variable overwritten by
-- each send, so capturing it between them is required, not tidiness. Does not hang up --
-- app.hangup() unwinds through a Lua error and nothing after it would run.
local function sms_deliver_inbound(extension, from_header)
	local from_uri, digits, line, reg, how = sms_inbound_from(from_header, extension)
	app.MessageSend("xmpp:" .. make_jid(extension, "<sip:" .. from_uri .. ">"), "xmpp:asterisk")
	local xmpp_status = tostring(channel.MESSAGE_SEND_STATUS:get() or "")
	local bridge_status = "skipped"
	local to, from = sms_bridge_message(from_uri, digits, reg)
	if to then
		app.MessageSend(to, from)
		bridge_status = tostring(channel.MESSAGE_SEND_STATUS:get() or "")
	end
	-- chan_sip answers 202 before any of this runs and the bridge answers 200 for a message
	-- it drops, so these lines are the entire record that a message existed. Do not trim.
	app.log("NOTICE", "sms inbound from=[" .. tostring(from_uri) .. "] exten=["
		.. tostring(extension) .. "] digits=[" .. tostring(digits)
		.. "] line=[" .. tostring(line) .. "] how=[" .. tostring(how)
		.. "] xmpp=[" .. xmpp_status .. "] bridge=[" .. bridge_status .. "]")
end

-- A Matrix user's reply, on its way to the trunk. Correct, and dead on the PSTN trunk: PSTN
-- answers outbound MESSAGE with 501 Sip Gw only. MESSAGE_SEND_STATUS does not carry that
-- refusal -- chan_sip answered the sender before this ran -- so the NOTICE below is the only
-- place it shows.
--
-- No `from` argument: chan_sip then applies
-- the peer's fromuser/fromdomain, already pinned to our own number on both trunks, which is
-- how the call path gets its caller ID too. Passing one here would present the Matrix user.
local function sms_deliver_outbound(extension, from_header)
	-- The sender rides the From display name: the user part is the bridge's peer name, which
	-- is what chan_sip matches the request on, so it cannot carry anything else.
	local caller = matrix_caller_uri(sms_from_display(from_header))
	local peer, num, reason = sms_outbound_route(extension, caller)
	if not peer then
		app.log("NOTICE", "sms outbound rejected: " .. tostring(reason))
		return
	end
	app.MessageSend("sip:" .. num .. "@" .. peer)
	app.log("NOTICE", "sms outbound num=[" .. tostring(num) .. "] peer=[" .. tostring(peer)
		.. "] caller=[" .. tostring(caller)
		.. "] status=[" .. tostring(channel.MESSAGE_SEND_STATUS:get() or "") .. "]")
end

extensions = {
	public = {
		["i"] = function(context, extension)
			app.goto("default", "i", 1)
		end;

		["h"] = function(context, extension)
			-- app.goto unwinds through a Lua error, so it stays outside the pcall or
			-- the pcall swallows the goto along with anything else.
			pcall(matrix_end_conference)
			app.goto("default", "h", 1)
		end;

		["t"] = matrix_ignore;

		["_."] = function(context, extension)
			local from = channel.SIP_HEADER("From"):get()
			channel.original_extension = extension
			-- The extension BEFORE the decoding below rewrites it: it is the contact this
			-- registration gave the carrier, and therefore which of our DIDs was dialled.
			local dialled_contact = extension

			if not extension:match("[^0-9]") then
				from = from:gsub("^[^<]*<sip:", ""):gsub(">.*$", "")
				extension = textBase10Decode(extension)
				if from:match("^%+?[0-9]*@") then
					if not extension:match("@cheogram%.com$") then
						extension = jid_escape(extension) .. "@cheogram.com"
					end
					if from:byte(1) ~= 43 then
						from = "+" .. from
					end
				end
				from = "<sip:" .. from .. ">"
			end

			if not extension:find("%.") then
				app.log("NOTICE", "Call from '' (" .. channel.CHANNEL("peerip"):get() .. ":0) to extension '" .. extension .. "' rejected because extension not found in context 'public'.")
				app.goto("i", 1)
				return
			end

			-- Name the LINE the call came in on, instead of whatever host the carrier put in
			-- From (today an SBC IP address). Two reasons, and neither is cosmetic: it says which
			-- of our numbers was dialled, and it makes the caller JID dialable back through the
			-- credentialed peer rather than anonymously to a bare address, which is the path a
			-- provider can answer with a recorded refusal. Also survives them renumbering.
			--
			-- Not a reverse DNS lookup: a PTR is whatever the carrier chose to publish, need not
			-- route anywhere, and would put a resolver in the call path.
			local peername = channel.CHANNEL("peername"):get()
			local line, _, _, how = inbound_line(sip_routing, dialled_contact, peername)
			from = call_from_uri(from, peername, line)

			-- The carrier echoing our Contact is observed behaviour, not a standard. Once it
			-- stops, every call on this host is attributed to its default line and looks exactly
			-- like a correct match. Silent while the contact still matches.
			if how ~= "contact" then
				app.log("NOTICE", "inbound fallback: contact [" .. tostring(dialled_contact)
					.. "] matched no registration on peer [" .. tostring(peername)
					.. "], attributed to line [" .. tostring(line)
					.. "] by [" .. tostring(how) .. "]")
			end

			if channel.CHANNEL("channeltype"):get() == "Message" then
				-- Unreachable while sip.conf routes every out-of-call MESSAGE to
				-- messages-in, and deliberately not a second copy of that path: the two
				-- identities drifted apart exactly once already.
				sms_deliver_inbound(extension, channel.MESSAGE("from"):get())
				app.hangup()
			else
				-- The conference name is the only thing the bridge routes on; it declines one
				-- it does not recognise, so a mismatch fails closed and silently.
				local conf, caller = matrix_conference_name(from, line)

				-- Ring time is the SUM of the stages: Asterisk times a whole Dial, never one
				-- target. The bridge's calls.ring_timeout must stay above that sum.
				local stages, default_plan = inbound_stages(sip_routing, line, extension)
				-- A line with no plan rings the dialled contact for 300s instead of its own
				-- targets. Only for a line that exists: no line at all is the NOTICE above.
				if default_plan and line then
					app.log("NOTICE", "inbound: line [" .. tostring(line)
						.. "] has no call plan, ringing the dialled contact")
				end

				for i, stage in ipairs(stages) do
					if stage.record then
						-- No voicemail in this image yet, so this ends the call.
						app.log("NOTICE", "inbound: no answer on line [" .. tostring(line)
							.. "], ending the call")
						return
					end

					local legs, with_matrix = inbound_legs(stage, from, conf, caller)
					if legs then
						if with_matrix then
							-- Next Dial ONLY, so it is re-issued per stage; hoisted above the
							-- loop it is lost from stage 2 on. The Motif legs ignore it.
							app.SIPAddHeader("X-Conference: " .. conf)
						end
						app.dial(legs, stage.timeout or 30,
							with_matrix and "rU(matrix-answered)" or "r")

						if with_matrix and channel.MATRIX_ANSWERED:get() == "1" then
							-- A channel inside ConfBridge() cannot also Dial(), so the order is
							-- forced. Named before joining so the h extension can end the
							-- conference: end_marked cannot, see matrix_kick_command.
							channel.MATRIX_CONF:set(conf)
							app.confbridge(conf, "default_bridge", "matrix_caller")
							return
						end
						-- Without this a finished call rings the next stage.
						if channel.DIALSTATUS:get() == "ANSWER" then return end
					else
						-- Every target dropped: the stage takes no time and rings nobody, which is
						-- indistinguishable from one that rang and went unanswered.
						app.log("NOTICE", "inbound: stage " .. i .. " on line ["
							.. tostring(line) .. "] rings nobody")
					end
				end
			end
		end;
	};

	-- Runs on whichever leg answered the Dial above, via its U() option.
	--
	-- GOSUB_RESULT=CONTINUE hangs that leg up and lets the CALLER continue at the next
	-- priority instead of being bridged to it. The bridge's leg is a control leg with no
	-- media, so without this an answered Matrix call is a silent dead end. Dial() has
	-- already hung the XMPP legs up with ANSWERED_ELSEWHERE by the time this runs.
	--
	-- The whole body is inside pcall deliberately. This gosub also runs when the XMPP leg
	-- answers, and app_dial hangs the answered leg UP if the gosub fails -- so a Lua error
	-- here would drop ordinary inbound calls to the household number. Swallowing it costs
	-- at worst a silent Matrix call; letting it out costs the phone.
	["matrix-answered"] = {
		["s"] = function(context, extension)
			pcall(function()
				if channel.CHANNEL("peername"):get() ~= "matrixbridge" then return end
				-- On the caller's channel, not this leg's: this leg is about to be hung up.
				channel.MASTER_CHANNEL("MATRIX_ANSWERED"):set("1")
				channel.GOSUB_RESULT:set("CONTINUE")
			end)
		end;
	};

	-- A Matrix user placing an outbound call: the bridge INVITEs the number here, with the
	-- conference name in X-Conference. Reaching this context at all requires the
	-- [matrixbridge] peer, which is the whole authorisation check.
	--
	-- Originate, not Dial: the far end has to end up in the CONFERENCE, not bridged to
	-- this control leg, which carries no media. It blocks until the call is answered or
	-- fails, which is what makes ORIGINATE_STATUS -- and therefore a real SIP status back
	-- to the bridge -- available at all.
	--
	-- The 200 is deliberately withheld until the far end answers: the bridge only asks
	-- livekit-sip to join the conference AFTER its INVITE is answered, so answering early
	-- would trade the one failure signal there is for a second of earlier audio.
	matrixbridge = {
		["_."] = function(context, extension)
			-- pcall around the decision only. Hangup() unwinds through a Lua error, so
			-- the app calls must stay outside it or the unwind is swallowed.
			local ok, peer, num, conf, cause, reason = pcall(matrix_outbound_route,
				extension, channel.SIP_HEADER("X-Conference"):get(),
				matrix_caller_uri(channel.SIP_HEADER("X-Matrix-Caller"):get()))
			if not ok then
				app.log("NOTICE", "matrix outbound: " .. tostring(peer))
				app.hangup("34")
				return
			end
			-- NOTICE because the dialplan is otherwise silent and a wrong trunk choice is
			-- only visible after the call has already failed.
			app.log("NOTICE", "matrix outbound exten=[" .. tostring(extension)
				.. "] num=[" .. tostring(num) .. "] peer=[" .. tostring(peer)
				.. "] conf=[" .. tostring(conf) .. "] reject=[" .. tostring(reason) .. "]")
			if not peer then
				app.hangup(tostring(cause))
				return
			end
			-- 180 keeps the bridge's INVITE transaction alive while the phone rings.
			app.ringing()
			-- Originate() does not watch this channel, so a CANCEL from the bridge while
			-- the phone rings cannot stop it: the callee still answers, and lands alone
			-- in the conference. Naming the conference before it blocks is what lets the
			-- h extension clear that out.
			channel.MATRIX_CONF:set(conf)
			app.originate("SIP/" .. peer .. "/" .. num, "exten", "matrix-outbound", conf, "1", "45")
			local status = channel.ORIGINATE_STATUS:get()
			app.log("NOTICE", "matrix outbound ORIGINATE_STATUS=[" .. tostring(status) .. "]")
			-- pbx_lua does not abort a handler on hangup, it only offers this, so the
			-- CANCEL has to be tested for explicitly. Returning with MATRIX_CONF still
			-- set is what asks the h extension to end the conference.
			if type(check_hangup) == "function" and check_hangup() then
				app.log("NOTICE", "matrix outbound: caller gone while ringing, ending " .. conf)
				return
			end
			if status ~= "SUCCESS" then
				channel.MATRIX_CONF:set("")
				app.hangup(tostring(matrix_outbound_cause(status)))
				return
			end
			-- Answer, then return: returning hangs this leg up, which is what the bridge
			-- expects. The conference outlives it, so stop claiming it: clearing only
			-- after Answer() succeeded means a leg that could not answer still ends it.
			app.answer()
			channel.MATRIX_CONF:set("")
		end;

		["h"] = function(context, extension)
			pcall(matrix_end_conference)
		end;

		["i"] = matrix_ignore;
		["t"] = matrix_ignore;
	};

	-- Where the leg Originate() placed above lands, with the conference name as its
	-- extension -- the same shape as the livekit context, and reachable only from that
	-- Originate. matrix_caller (end_marked) is what makes a Matrix hangup drop this leg;
	-- the marked profile here would invert that and the call would never end.
	["matrix-outbound"] = {
		["_."] = function(context, extension)
			-- As on the inbound path: end_marked drops this leg when livekit-sip goes,
			-- and the h extension covers the opposite direction, which it cannot.
			channel.MATRIX_CONF:set(extension)
			app.confbridge(extension, "default_bridge", "matrix_caller")
		end;

		["h"] = function(context, extension)
			pcall(matrix_end_conference)
		end;

		["i"] = matrix_ignore;
		["t"] = matrix_ignore;
	};

	-- Every out-of-call SIP MESSAGE lands here -- both trunks and the bridge -- because
	-- sip.conf sets outofcall_message_context globally. So this context is a DISPATCHER,
	-- not the inbound path: routing the trunks back into `public` instead would depend on
	-- chan_sip's peer-vs-global precedence, which no CLI exposes.
	["messages-in"] = {
		["_."] = function(context, extension)
			local from = channel.MESSAGE("from"):get()
			if sms_is_outbound(from, extension) then
				sms_deliver_outbound(extension, from)
			else
				sms_deliver_inbound(extension, from)
			end
			-- Last: it unwinds the handler through a Lua error.
			app.hangup()
		end;

		-- `_.` otherwise swallows these, same as every other context here.
		["h"] = matrix_ignore;
		["i"] = matrix_ignore;
		["t"] = matrix_ignore;
	};

	-- livekit-sip dials in here to join a caller already parked in a conference. It sends
	-- the bridge name as the extension, so there is no mapping to do and nothing to look
	-- up: whatever it asks for is what it gets. Reaching this context at all requires the
	-- [livekit] peer's credentials.
	livekit = {
		["_."] = function(context, extension)
			app.answer()
			-- MARKED user, paired with end_marked on the caller in confbridge-matrix.conf:
			-- the bridge ends a call by dropping livekit-sip out of the LiveKit room, and
			-- that pairing is the only thing that then hangs the caller up.
			app.confbridge(extension, "default_bridge", "matrix_marked")
		end;

		-- Nothing to end here: this leg IS the marked user, so end_marked drops the
		-- caller by itself when it goes.
		["h"] = matrix_ignore;
		["i"] = matrix_ignore;
		["t"] = matrix_ignore;
	};

	jingle = {
		["jingle-endpoint"] = function(context, extension)
			local jid = channel.CALLERID("name"):get()
			local from = jid_unescape(jid:sub(0, jid:find("@") - 1))
			local to = local_to_sip_uri(jid_unescape(jid:sub(jid:find("/") + 1)))
			-- resolve_destination gates as well as routes: kind == "forbidden" is this
			-- caller refused the line, and it returns no peer, so the refusal below is the
			-- same fall-through as an unroutable number.
			local label, peer, exten, rule, kind, line
			to, label, peer, exten, rule, kind, line = resolve_destination(to, "xmpp:" .. from)

			channel.CALLERID("all"):set(from .. "<" .. from .. ">")

			-- Every reachable destination goes out a PEER, which carries the registration
			-- credentials: either the one its label named, or the one the routing table
			-- picked for a label-less number. Upstream's SIP/<user>@<host> fallback is gone
			-- -- see resolve_destination's `kind`; restoring foreign SIP dialling means a
			-- deliberate allow list, not a fall-through.
			--
			-- NOTICE so it reaches the file log and stdout; the dialplan is otherwise silent
			-- and a wrong peer is only visible after the call has already failed.
			app.log("NOTICE", "outbound to=[" .. tostring(to) .. "] label=[" .. tostring(label)
				.. "] kind=[" .. tostring(kind)
				.. "] exten=[" .. tostring(exten)
				.. "] peer=[" .. tostring(peer)
				.. "] rule=[" .. tostring(rule and rule.to_pattern)
				.. "] line=[" .. tostring(line)
				.. "] emergency=[" .. tostring(rule and rule.emergency or false) .. "]")
			if peer and peer ~= "" then
				app.dial("SIP/" .. peer .. "/" .. exten:gsub("\\", "\\\\"):gsub("&", ""))
				return
			end

			app.log("NOTICE", "Cannot dial '" .. to .. "': "
				.. (kind == "forbidden"
					and "caller is not in the callers list of line '" .. tostring(line) .. "'"
				or kind == "unknown"
					and "'" .. tostring(label) .. "' is neither one of our trunks nor a line"
					or "no routing rule matched, or the table is missing or malformed"))
		end;
	};

	-- An XMPP user's message on its way to the trunk. Mirrors sms_deliver_outbound: the
	-- routing table gates and picks the peer, and no `from` is passed so chan_sip applies
	-- the peer's fromuser. Sending at a bare SIP URI here reached the trunk ungated.
	--
	-- A #label is dropped with the rest of the host part, so the table decides the line.
	xmpp = {
		["s"] = function(context, extension)
			local jid = channel.MESSAGE("from"):get():sub(6)
			local from = jid_unescape(jid:sub(0, jid:find("@") - 1))
			local to = local_to_sip_uri(jid_unescape(jid:sub(jid:find("/") + 1)))
			local caller = "xmpp:" .. from
			local peer, num, reason = sms_outbound_route(to, caller)
			if not peer then
				app.log("NOTICE", "xmpp outbound rejected: " .. tostring(reason)
					.. " caller=[" .. tostring(caller) .. "]")
				return
			end
			app.MessageSend("sip:" .. num .. "@" .. peer)
			app.log("NOTICE", "xmpp outbound num=[" .. tostring(num) .. "] peer=[" .. tostring(peer)
				.. "] caller=[" .. tostring(caller)
				.. "] status=[" .. tostring(channel.MESSAGE_SEND_STATUS:get() or "") .. "]")
		end;
	};
}
