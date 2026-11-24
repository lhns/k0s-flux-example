-- Our copy of the image's extensions.lua. Re-diff against the image after a digest bump
-- (README).

local component_domain = os.getenv("COMPONENT_DOMAIN") or "COMPONENT_DOMAIN-unset"

-- Our trunk hosts, keyed lowercase: SIP_HOST and every enabled registration's host. A host
-- label dials through that host's default registration (credentials). See
-- resolve_destination.
local trunks = {}

-- The Matrix bridge's pjsip endpoint and From user; must equal sip.username in the bridge's
-- config.
local bridge_peer = "matrixbridge"

-- loadfile + pcall, never dofile: a malformed table must not take inbound down. nil fails
-- outbound CLOSED.
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

sip_routing = load_routing(os.getenv("SIP_ROUTING_FILE") or "/etc/asterisk/routing.lua")

local function reg_disabled(reg)
	return type(reg) == "table" and reg.disabled == true
end

do
	local host = os.getenv("SIP_HOST") or ""
	if host ~= "" then trunks[host:lower()] = host end
	for _, r in pairs(sip_routing and sip_routing.registrations or {}) do
		if type(r) == "table" and not reg_disabled(r) and type(r.host) == "string"
				and r.host ~= "" then
			trunks[r.host:lower()] = r.host
		end
	end
end

-- Does a routing-table address pattern cover this caller? Anything but "*",
-- "xmpp:*[@domain]", "matrix:*", "matrix:@*:server" matches literally.
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

function routing_line(tbl, name)
	if type(tbl) ~= "table" or type(tbl.lines) ~= "table" then return nil end
	local want = tostring(name or ""):lower()
	if want == "" then return nil end
	for lname, l in pairs(tbl.lines) do
		if type(l) == "table" and tostring(lname):lower() == want then return l, tostring(lname) end
	end
	return nil
end

-- The registration a rule presents: pinned `registration` (emergency only), else the line's
-- `primary`. nil for anything undefined, which refuses: a guessed peer reaches a carrier
-- that answers with a recorded refusal.
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

-- The peer a call LEAVES BY, not the host: registrations share hosts, from_user is pinned
-- per endpoint.
function registration_peer(reg, name)
	if type(reg) == "table" and name ~= nil and tostring(name) ~= "" then
		return tostring(name)
	end
	return nil
end

local function peer_host(tbl, peer)
	for name, r in pairs(tbl.registrations) do
		if type(r) == "table" and tostring(name):lower() == peer
				and tostring(r.host or "") ~= "" then
			return tostring(r.host):lower()
		end
	end
	return peer
end

-- Extension FIRST: PJSIP/<endpoint> alone dials the AOR contact, which on a trunk is the
-- registrar.
function dial_string(tbl, peer, exten)
	return "PJSIP/" .. exten .. "@" .. peer
end

-- The endpoint carries credentials and transport; the URI host must be real, or the
-- endpoint name is sent to as an uncredentialed hostname.
function message_target(tbl, peer, num)
	return "pjsip:" .. peer .. "/sip:" .. num .. "@"
		.. peer_host(tbl, tostring(peer):lower())
end

-- No list means anyone. NARROWING, not permission: an excluded caller falls to the next
-- rule. The gate is line_permits.
function routing_from_match(globs, addr)
	if globs == nil then return true end
	if type(globs) ~= "table" then return false end
	for _, g in ipairs(globs) do
		if routing_glob_match(g, addr) then return true end
	end
	return false
end

-- THE OUTBOUND GATE. Fails closed. Refuses rather than falling through, so revoking someone
-- cannot leak to another line.
function line_permits(tbl, name, addr)
	local l = routing_line(tbl, name)
	if type(l) ~= "table" or type(l.callers) ~= "table" or #l.callers == 0 then return false end
	for _, g in ipairs(l.callers) do
		if routing_glob_match(g, addr) then return true end
	end
	return false
end

-- FIRST MATCH WINS. `from_uri` is nil where there is no caller identity (Matrix bridge,
-- SMS), so a from_glob rule cannot match. A rule naming an undefined line or registration
-- returns nil instead of trying the next rule: a later one would send the call out a peer
-- nobody chose.
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

local function host_default_kind(tbl, reg)
	if reg.entrypoint then return "entrypoint" end
	local host = tostring(reg.host or ""):lower()
	local n = 0
	for _, r in pairs(tbl.registrations) do
		if type(r) == "table" and not reg_disabled(r)
				and tostring(r.host or ""):lower() == host then
			n = n + 1
		end
	end
	if n == 1 then return "sole" end
	return nil
end

-- Which registration an INBOUND call or message arrived on.
--
-- `extension` is the register contact the carrier dialled back: the only per-DID
-- discriminator, and only while it echoes our Contact (observed, not a standard). When it
-- stops matching, this falls back to the host's default registration and per-DID inbound
-- silently collapses to one line. nil asks for that default.
--
-- The third return says HOW: "contact"/"endpoint" are real matches; "entrypoint"/"sole" are
-- the otherwise invisible fallback the call site NOTICEs.
function inbound_registration(tbl, extension, peername)
	if type(tbl) ~= "table" or type(tbl.registrations) ~= "table" then return nil end
	local ext = tostring(extension or ""):lower()
	local peer = tostring(peername or ""):lower()
	if peer == "" then return nil end

	-- One endpoint per registration names the DID and is tried first, but is not a
	-- "contact" match: the host's default endpoint also carries the type=identify, so it
	-- may be the source-address fallback. A MESSAGE has no endpoint.
	for name, r in pairs(tbl.registrations) do
		if type(r) == "table" and not reg_disabled(r)
				and tostring(name):lower() == peer then
			if ext ~= "" and tostring(r.contact_user or ""):lower() == ext then
				return r, tostring(name), "contact"
			end
			return r, tostring(name), host_default_kind(tbl, r) or "endpoint"
		end
	end

	peer = peer_host(tbl, peer)
	local entry, entryname, sole, solename, several
	for name, r in pairs(tbl.registrations) do
		if type(r) == "table" and not reg_disabled(r)
				and tostring(r.host or ""):lower() == peer then
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

function inbound_line(tbl, extension, peername)
	local reg, name, how = inbound_registration(tbl, extension, peername)
	if type(reg) == "table" and type(reg.line) == "string" and reg.line ~= "" then
		return reg.line, reg, name, how
	end
	return nil
end

-- Normalise to E.164 so one human maps to one JID and portal room. Returns the number
-- unchanged unless CONFIDENT: a wrong prefix dials a different valid number. "00" must be
-- tested before "0".
local function normalize_e164_raw(num)
	local cc = os.getenv("SIP_E164_COUNTRY")
	if not num or num == "" or not cc or cc == "" then return num end
	if num:match("[^0-9+]") then return num end          -- not a number at all
	if num:sub(1, 1) == "+" then return num end          -- already E.164

	-- Short strings are extensions and emergency numbers (110 would become +49110).
	local short = tonumber(os.getenv("SIP_SHORT_MAXLEN") or "5") or 5
	if #num <= short then return num end

	if num:sub(1, 2) == "00" then return "+" .. num:sub(3) end
	if num:sub(1, 1) == "0" then return "+" .. cc .. num:sub(2) end

	-- Bare number: a missing trunk 0 needs the country code, a missing area code needs the
	-- area too, and they look identical. Act only if SIP_E164_AREA says which.
	local area = os.getenv("SIP_E164_AREA")
	if area and area ~= "" then return "+" .. cc .. area .. num end
	return num
end

local function normalize_e164(num)
	local out = normalize_e164_raw(num)
	if out ~= num then
		app.log("NOTICE", "normalize_e164: " .. num .. " -> " .. out)
	end
	return out
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

-- "@" in a JID localpart becomes "#", not XEP-0106's escape (README); local_to_sip_uri is
-- the inverse, change one and calling back breaks.
function sip_uri_to_local(uri)
	return (uri:gsub("@", "#"))
end

function local_to_sip_uri(s)
	return (s:gsub("#", "@"))
end

-- Pick a peer for an outbound destination; returns to, label, peer, exten, rule, kind, line
-- (testable).
--
-- The component appends "@SIP_HOST" to destinations without a host, so "user#label" arrives
-- as "user@label@SIP_HOST": strip the suffix BEFORE deriving the label. A label equal to
-- SIP_HOST then expresses no intent, which keeps short extensions off the PSTN trunk.
--
-- `kind` is the fail-closed part: "trunk", "line", "routed" reached a peer; the rest
-- refuse. Never fall through to an uncredentialed dial, nor (disabled primary) to the next
-- rule's trunk.
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
		local host = trunks[label:lower()]
		if host then
			kind = "trunk"
			local reg, rname
			line, reg, rname = inbound_line(sip_routing, nil, host)
			peer = registration_peer(reg, rname)
			if not peer then kind = "unrouted" end
		else
			local l, lname = routing_line(sip_routing, label)
			if l then
				kind, line = "line", lname
				local reg, rname = routing_registration(sip_routing, {line = lname})
				if reg_disabled(reg) then
					kind = "disabled"
				else
					peer = registration_peer(reg, rname)
					if not peer then kind = "unrouted" end
				end
			else
				kind = "unknown"
			end
		end
	else
		local r, reg, rname = routing_match(sip_routing, to, from_uri)
		if r then
			rule, line = r, r.line or reg.line
			if reg_disabled(reg) then
				kind = "disabled"
			else
				peer, kind = registration_peer(reg, rname), "routed"
			end
		else
			kind = "unrouted"
		end
	end

	-- THE GATE, on the resolved line. An emergency rule pins a registration and has no
	-- line, so 110/112 leave from an account permitted nothing else. A trunk label has no
	-- line and is refused.
	if not (rule and rule.emergency) then
		if peer and not line_permits(sip_routing, line, from_uri) then
			peer, kind = nil, "forbidden"
		end
	end
	local exten = (label and to:sub(1, #to - #label - 1) or to)
	return to, label, peer, exten, rule, kind, line
end

function refusal_reason(kind, line, label)
	if kind == "forbidden" then
		return "caller is not in the callers list of line '" .. tostring(line) .. "'"
	elseif kind == "unknown" then
		return "'" .. tostring(label) .. "' is neither one of our trunks nor a line"
	elseif kind == "disabled" then
		return "line '" .. tostring(line) .. "' is disabled in the routing table"
	end
	return "no routing rule matched, or the table is missing or malformed"
end

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

-- The matrix conference this leg must end, or nil plus a log reason. end_marked only ends
-- the caller when livekit-sip leaves; a caller hanging up leaves livekit-sip in the room,
-- so that direction is ended from h. The name is re-checked: pbx_lua joins arguments with
-- commas.
function matrix_kick_conference(conf, result)
	conf = tostring(conf or "")
	if conf == "" or result == "ENDMARKED" or result == "KICKED" then return nil end
	if not conf:match("^[%w%-_+]+$") then
		return nil, "unusable conference name: " .. conf
	end
	return conf
end

-- Hangup handler for every matrix-conference leg; MATRIX_CONF unset means nothing to end.
-- ConfKick() takes the conference by name: this channel is already hung up and the control
-- leg was never in it.
local function matrix_end_conference()
	local conf, reason = matrix_kick_conference(channel.MATRIX_CONF:get(),
		channel.CONFBRIDGE_RESULT:get())
	if not conf then
		if reason then app.log("NOTICE", "matrix hangup: " .. reason) end
		return
	end
	app.log("NOTICE", "matrix hangup: ending conference " .. conf)
	app.confkick(conf, "all")
end

-- h, i, t go through `_.` like real destinations; in a ConfBridge context `_.` would put
-- the hung-up channel into a conference named "h".
local function matrix_ignore() end

-- Where an INVITE from the [matrixbridge] peer goes. Fails CLOSED on anything but a plain
-- number: this is where a Matrix user reaches the trunks. The conference name is
-- class-restricted because Originate() reads a comma or caret as further arguments.
function matrix_outbound_route(request_exten, conference, caller)
	local num = tostring(request_exten or ""):gsub("@.*$", "")
	num = normalize_e164(num)
	if not num:match("^%+?[0-9]+$") then
		return nil, nil, nil, 1, "destination is not a number: " .. tostring(request_exten)
	end
	conference = tostring(conference or ""):gsub("^%s+", ""):gsub("%s+$", "")
	if not conference:match("^[%w%-_+]+$") then
		return nil, nil, nil, 1, "missing or unusable conference header"
	end
	-- The room's line is in "sip-<line>-+<digits>"; lines may contain "-", so the capture
	-- is greedy to the last "-+". Emergency numbers keep their pinned registration. An
	-- unconfigured line refuses.
	local dest, room_line = num, conference:match("^sip%-(.+)%-%+[0-9]+$")
	if room_line then
		local r = routing_match(sip_routing, num, caller)
		if not (r and r.emergency) then dest = num .. "@" .. room_line end
	end
	local _, label, peer, _, _, kind, line = resolve_destination(dest, caller)
	if not peer or peer == "" then
		return nil, nil, nil, 34, "no peer for " .. num .. ": " .. refusal_reason(kind, line, label)
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

-- "sip-<line>-<E.164 with plus>", derived here for calls and messages alike so one human
-- stays in one portal. nil without a line or +E.164: the bridge declines other keys, so
-- inventing one fails silently.
function matrix_conference_name(from, line)
	local digits = tostring(from or ""):match("^<sip:%+([0-9]+)@")
	if not digits then return nil end
	line = tostring(line or "")
	if line == "" then return nil end
	return "sip-" .. line .. "-+" .. digits, digits
end

-- The caller behind a Matrix bridge request. The bridge is matched by address and never
-- challenged, so this is a CLAIM: the scheme is built here, never taken from the wire, and
-- non-MXIDs return nil.
function matrix_caller_uri(raw)
	local mxid = tostring(raw or ""):gsub("^%s+", ""):gsub("%s+$", "")
	if not mxid:match("^@[^:@%s]+:[^:@%s]+$") then return nil end
	return "matrix:" .. mxid:lower()
end

-- The From display name carries the sending Matrix user; run before sms_strip_uri, which
-- discards it.
function sms_from_display(header)
	local name = tostring(header or ""):match("^%s*(.-)%s*<")
	if not name then return nil end
	name = name:gsub('^"(.*)"$', "%1")
	if name == "" then return nil end
	return name
end

function inbound_stages(tbl, line, extension)
	local l = routing_line(tbl, line)
	if type(l) == "table" and type(l.inbound_route) == "table" and #l.inbound_route > 0 then
		return l.inbound_route, false
	end
	return { { ring = { "xmpp:" .. tostring(extension or ""), "matrix:" }, timeout = 300 } }, true
end

local function hex_encode(s)
	return (s:gsub(".", function(c) return string.format("%02x", c:byte()) end))
end

local function hex_decode(s)
	if s == "" or #s % 2 ~= 0 or s:find("%X") then return nil end
	return (s:gsub("%x%x", function(h) return string.char(tonumber(h, 16)) end))
end

-- Extension of the Local leg with no recipient. Not hex, so no MXID can encode to it.
local default_leg = "default"

local function matrix_target_mxid(addr)
	local mxid = tostring(addr or ""):match("^matrix:(.+)$")
	if not mxid or mxid:find("*", 1, true) then return nil end
	local uri = matrix_caller_uri(mxid)
	return uri and uri:sub(#"matrix:" + 1)
end

local function stage_matrix_recipients(stage)
	local out, seen = {}, {}
	if type(stage) ~= "table" or type(stage.ring) ~= "table" then return out end
	for _, addr in ipairs(stage.ring) do
		addr = tostring(addr or "")
		local who
		if addr == "matrix:" then who = false else who = matrix_target_mxid(addr) end
		if who ~= nil and not seen[tostring(who)] then
			seen[tostring(who)] = true
			out[#out + 1] = who
		end
	end
	return out
end

-- One stage as a Dial() string plus whether it has a Matrix leg: one Local leg per distinct
-- matrix: target, one recipientless leg for a bare "matrix:". Without a conference they add
-- none.
--
-- A Local leg, not PJSIP/: the recipient header can only be added by a pre-dial handler, so
-- the Dial() that creates the PJSIP channel must be inside matrix-leg.
function inbound_legs(stage, from, conf, caller)
	if type(stage) ~= "table" or type(stage.ring) ~= "table" then return nil, false end
	local legs, with_matrix = {}, false
	local matrix_done = {}
	for _, addr in ipairs(stage.ring) do
		addr = tostring(addr or "")
		local jid = addr:match("^xmpp:(.+)$")
		if jid then
			legs[#legs + 1] = "Motif/jingle-endpoint/" .. make_jid(jid, from)
		elseif conf ~= nil and caller ~= nil and addr:match("^matrix:") then
			local ext
			if addr == "matrix:" then
				ext = default_leg
			else
				local mxid = matrix_target_mxid(addr)
				ext = mxid and hex_encode(mxid)
			end
			if ext and not matrix_done[ext] then
				matrix_done[ext] = true
				legs[#legs + 1] = "Local/" .. ext .. "@matrix-leg/n"
				with_matrix = true
			end
		end
	end
	if #legs == 0 then return nil, false end
	return table.concat(legs, "&"), with_matrix
end

-- Rewrite an inbound call's From to <sip:user@line> so dialling back resolves. Must agree
-- with sms_inbound_from or one human is two portal rooms. No line falls back to the trunk
-- host.
function call_from_uri(from_header, peername, line)
	local label = (type(line) == "string" and line ~= "" and line)
		or (peername and trunks[tostring(peername):lower()])
	if not label then return from_header end
	local uri = tostring(from_header):gsub("^[^<]*<sip:", ""):gsub(">.*$", "")
	return "<sip:" .. normalize_e164(uri:gsub("@.*$", "")) .. "@" .. label .. ">"
end

-- Reduce a header or extension to a bare SIP URI. The ";param" tail must go: it would
-- become part of the host.
local function sms_strip_uri(s)
	s = tostring(s or "")
	s = s:gsub("^[^<]*<", ""):gsub(">.*$", "")
	s = s:gsub("^%s+", ""):gsub("%s+$", "")
	s = s:gsub("^[sS][iI][pP][sS]?:", "")
	s = s:gsub("[;%?].*$", "")
	return s
end

-- An inbound MESSAGE's sender: user@line, E.164 digits (nil if alphanumeric, short or
-- unnormalised), line, registration, how.
--
-- The host becomes the LINE as on the call path (CHANNEL("endpoint"), absent on a Message
-- channel); otherwise one human is +49...#home for a call and +49...#<sbc-ip> for an SMS.
-- Whether a carrier echoes our Contact on a MESSAGE is untested.
function sms_inbound_from(from_header, extension)
	local uri = sms_strip_uri(from_header)
	local user, host = uri:match("^([^@]+)@(.+)$")
	if not user then user, host = uri, nil end
	user = normalize_e164(user)
	-- SIP_HOST, not an outbound lookup: this says where a message CAME FROM, always a trunk
	-- we register to.
	local peer = (host and trunks[host:lower()]) or os.getenv("SIP_HOST") or host
	local line, reg, _, how = inbound_line(sip_routing, extension, peer)
	local label = line or peer
	if label and label ~= "" then uri = user .. "@" .. label else uri = user end
	local _, digits = matrix_conference_name("<sip:" .. uri .. ">", line)
	return uri, digits, line, reg, how
end

-- Where an inbound message goes on the bridge leg, or nil. The "+" in `from` is
-- load-bearing: the bridge drops a number without one while ANSWERING 200 OK, invisible on
-- both sides. The URI host must be the Service (bridge_host).
function sms_bridge_message(from_uri, digits, reg)
	if not digits or digits == "" then return nil end
	local bridge_host = sip_routing and sip_routing.bridge_host
	if type(bridge_host) ~= "string" or bridge_host == "" then return nil end
	local did = type(reg) == "table" and tostring(reg.did or "") or ""
	if did == "" then did = normalize_e164(os.getenv("SIP_USER") or "") end
	if did == "" then did = bridge_peer end
	return "pjsip:" .. bridge_peer .. "/sip:" .. did .. "@" .. bridge_host,
		"sip:" .. from_uri
end

-- Who a line's inbound message goes to on the bridge: its call plan's MXIDs, so a text
-- lands in the call's room. A bare "matrix:" or no Matrix target is the default recipient,
-- `false`.
function sms_matrix_recipients(tbl, line)
	local out, seen, default = {}, {}, false
	local l = routing_line(tbl, line)
	for _, stage in ipairs(type(l) == "table" and type(l.inbound_route) == "table"
			and l.inbound_route or {}) do
		for _, who in ipairs(stage_matrix_recipients(stage)) do
			if who == false then
				default = true
			elseif not seen[who] then
				seen[who] = true
				out[#out + 1] = who
			end
		end
	end
	if default or #out == 0 then out[#out + 1] = false end
	return out
end

-- True when a MESSAGE is the bridge asking for an SMS. BOTH tests are required: From alone
-- would let a carrier-injected "From: sip:matrixbridge@..." send SMS on our trunk, billed
-- to us.
function sms_is_outbound(from_header, extension)
	local user = sms_strip_uri(from_header):match("^([^@]+)@") or sms_strip_uri(from_header)
	if user:lower() ~= bridge_peer:lower() then return false end
	return tostring(extension or ""):match("^%+?[0-9]+$") ~= nil
end

function sms_outbound_route(to_uri, caller)
	local num = normalize_e164(sms_strip_uri(to_uri):gsub("@.*$", ""))
	if not num:match("^%+?[0-9]+$") then
		return nil, nil, "destination is not a number: " .. tostring(to_uri)
	end
	local _, label, peer, _, _, kind, line = resolve_destination(num, caller)
	if not peer or peer == "" then
		return nil, nil, "no peer for " .. num .. ": " .. refusal_reason(kind, line, label)
	end
	return peer, num
end

function sms_xmpp_targets(tbl, line, extension)
	local stages, default_plan = inbound_stages(tbl, line, extension)
	if default_plan then return { tostring(extension or "") } end
	local out, seen = {}, {}
	for _, stage in ipairs(stages) do
		for _, addr in ipairs(type(stage) == "table" and type(stage.ring) == "table"
				and stage.ring or {}) do
			local jid = tostring(addr or ""):match("^xmpp:(.+)$")
			if jid and not seen[jid:lower()] then
				seen[jid:lower()] = true
				out[#out + 1] = jid
			end
		end
	end
	return out
end

-- Deliver an inbound message to the line's XMPP and Matrix recipients. XMPP goes FIRST: the
-- bridge handler can block 32s and must not delay the phone. MESSAGE_SEND_STATUS is one
-- variable overwritten per send, so capture it between them. No hangup here (it unwinds via
-- a Lua error).
local function sms_deliver_inbound(extension, from_header)
	local from_uri, digits, line, reg, how = sms_inbound_from(from_header, extension)
	local xmpp_statuses = {}
	for _, jid in ipairs(sms_xmpp_targets(sip_routing, line, extension)) do
		app.MessageSend("xmpp:" .. make_jid(jid, "<sip:" .. from_uri .. ">"), "xmpp:asterisk")
		xmpp_statuses[#xmpp_statuses + 1] = jid .. "="
			.. tostring(channel.MESSAGE_SEND_STATUS:get() or "")
	end
	local xmpp_status = #xmpp_statuses > 0 and table.concat(xmpp_statuses, ",") or "none"
	local bridge_statuses = {}
	local to, from = sms_bridge_message(from_uri, digits, reg)
	if to then
		-- One MESSAGE per recipient: the bridge reads the header to pick the login and
		-- room. MESSAGE_DATA persists across sends, so set it every time; empty removes it.
		for _, who in ipairs(sms_matrix_recipients(sip_routing, line)) do
			channel.MESSAGE_DATA("X-Matrix-Recipient"):set(who or "")
			app.MessageSend(to, from)
			bridge_statuses[#bridge_statuses + 1] = (who or "default") .. "="
				.. tostring(channel.MESSAGE_SEND_STATUS:get() or "")
		end
	end
	local bridge_status = #bridge_statuses > 0 and table.concat(bridge_statuses, ",")
		or "skipped"
	-- Asterisk answers 202 first and the bridge answers 200 for dropped messages, so this
	-- line is the only record. Do not trim.
	app.log("NOTICE", "sms inbound from=[" .. tostring(from_uri) .. "] exten=["
		.. tostring(extension) .. "] digits=[" .. tostring(digits)
		.. "] line=[" .. tostring(line) .. "] how=[" .. tostring(how)
		.. "] xmpp=[" .. xmpp_status .. "] bridge=[" .. bridge_status .. "]")
end

-- A Matrix user's reply to the trunk. Dead on the PSTN trunk: PSTN answers outbound MESSAGE
-- with 501, which MESSAGE_SEND_STATUS misses, so the NOTICE is the only place it shows.
--
-- No `from` argument: the endpoint's from_user/from_domain apply, as for calls. Passing one
-- would present the Matrix user.
local function sms_deliver_outbound(extension, from_header)
	local caller = matrix_caller_uri(sms_from_display(from_header))
	local peer, num, reason = sms_outbound_route(extension, caller)
	if not peer then
		app.log("NOTICE", "sms outbound rejected: " .. tostring(reason))
		return
	end
	app.MessageSend(message_target(sip_routing, peer, num))
	app.log("NOTICE", "sms outbound num=[" .. tostring(num) .. "] peer=[" .. tostring(peer)
		.. "] caller=[" .. tostring(caller)
		.. "] status=[" .. tostring(channel.MESSAGE_SEND_STATUS:get() or "") .. "]")
end

local function inbound_peername()
	return channel.CHANNEL("endpoint"):get()
end

local function inbound_from_header()
	return "<sip:" .. tostring(channel.CALLERID("num"):get() or "")
		.. "@" .. tostring(channel.CHANNEL("endpoint"):get() or "") .. ">"
end

function is_matrix_leg(name)
	return tostring(name or ""):match("^Local/[^@]+@matrix%-leg%-") ~= nil
end

-- Body of matrix-leg. A bad extension hangs up: the stage loses this one leg and rings the
-- rest.
function matrix_leg(context, extension)
	local who = nil
	if extension ~= default_leg then
		who = hex_decode(extension)
		who = who and matrix_target_mxid("matrix:" .. who)
		if not who then
			app.log("NOTICE", "matrix-leg: bad recipient extension [" .. tostring(extension) .. "]")
			app.hangup()
			return
		end
	end
	local caller = channel.MATRIX_CALLER_PENDING:get()
	if not caller or caller == "" then
		app.log("NOTICE", "matrix-leg: no caller to dial the bridge with")
		app.hangup()
		return
	end
	channel["__MATRIX_RECIPIENT"]:set(who or "")
	app.dial("PJSIP/" .. caller .. "@" .. bridge_peer, "", "b(add-leg-headers^s^1)")
end

extensions = {
	public = {
		-- No `default` context to hand off to, so the special extensions end the call here.
		["i"] = function(context, extension)
			app.hangup()
		end;

		["h"] = function(context, extension)
			pcall(matrix_end_conference)
		end;

		["t"] = matrix_ignore;

		["_."] = function(context, extension)
			local from = inbound_from_header()
			channel.original_extension = extension
			local dialled_contact = extension

			-- Name the LINE, not the carrier's From host: that makes the caller JID
			-- dialable back through the credentialed peer (README).
			local peername = inbound_peername()
			local line, _, _, how = inbound_line(sip_routing, dialled_contact, peername)
			from = call_from_uri(from, peername, line)

			-- Once the carrier stops echoing our Contact, calls look exactly like correct
			-- default-line matches, so log anything but a match.
			if how ~= "contact" and how ~= "endpoint" then
				app.log("NOTICE", "inbound fallback: contact [" .. tostring(dialled_contact)
					.. "] matched no registration on peer [" .. tostring(peername)
					.. "], attributed to line [" .. tostring(line)
					.. "] by [" .. tostring(how) .. "]")
			end

			if channel.CHANNEL("channeltype"):get() == "Message" then
				-- Unreachable while every endpoint sets message_context (-> messages-in).
				-- Deliberately not a second copy of that path: the identities drifted once.
				sms_deliver_inbound(extension, channel.MESSAGE("from"):get())
				app.hangup()
			else
				local conf, caller = matrix_conference_name(from, line)

				-- Ring time is the SUM of the stages (Asterisk times a whole Dial); the
				-- bridge's calls.ring_timeout must exceed it.
				local stages, default_plan = inbound_stages(sip_routing, line, extension)
				if default_plan and line then
					app.log("NOTICE", "inbound: line [" .. tostring(line)
						.. "] has no call plan, ringing the dialled contact")
				end

				for i, stage in ipairs(stages) do
					if stage.record then
						app.log("NOTICE", "inbound: no answer on line [" .. tostring(line)
							.. "], ending the call")
						return
					end

					local legs, with_matrix = inbound_legs(stage, from, conf, caller)
					if legs then
						if with_matrix then
							-- Read by matrix-leg and add-leg-headers; "__" inherits it down
							-- the legs. Set per stage: hoisted above the loop it is lost
							-- from stage 2.
							channel["__MATRIX_CONF_PENDING"]:set(conf)
							channel["__MATRIX_CALLER_PENDING"]:set(caller)
						end
						app.dial(legs, stage.timeout or 30,
							with_matrix and "rU(matrix-answered)" or "r")

						if with_matrix and channel.MATRIX_ANSWERED:get() == "1" then
							-- A channel in ConfBridge() cannot Dial(), so the order is
							-- forced. Named before joining so h can end the conference.
							channel.MATRIX_CONF:set(conf)
							app.confbridge(conf, "default_bridge", "matrix_caller")
							return
						end
						if channel.DIALSTATUS:get() == "ANSWER" then return end
					else
						app.log("NOTICE", "inbound: stage " .. i .. " on line ["
							.. tostring(line) .. "] rings nobody")
					end
				end
			end
		end;
	};

	-- Runs on the answering leg via Dial's U(). GOSUB_RESULT=CONTINUE hangs that leg up and
	-- lets the CALLER continue instead of bridging: the bridge's leg is a media-less
	-- control leg, so otherwise an answered Matrix call is a silent dead end.
	--
	-- The body is in pcall: app_dial hangs the answered leg up if the gosub fails, and this
	-- also runs for XMPP legs, so an error would drop ordinary calls.
	["matrix-answered"] = {
		["s"] = function(context, extension)
			pcall(function()
				if not is_matrix_leg(channel.CHANNEL("name"):get()) then return end
				-- On the caller's channel, not this leg's: this leg is about to be hung up.
				channel.MASTER_CHANNEL("MATRIX_ANSWERED"):set("1")
				channel.GOSUB_RESULT:set("CONTINUE")
			end)
		end;
	};

	["matrix-leg"] = {
		["_[0-9a-f]."] = matrix_leg;
		["default"] = matrix_leg;
		["h"] = matrix_ignore;
		["i"] = matrix_ignore;
		["t"] = matrix_ignore;
	};

	-- Runs on the PJSIP channel matrix-leg dials, pre-dial: PJSIP_HEADER(add) writes the
	-- callee only from a pre-dial handler, elsewhere the CALLER's channel. In pcall like
	-- matrix-answered.
	["add-leg-headers"] = {
		["s"] = function(context, extension)
			pcall(function()
				if channel.CHANNEL("endpoint"):get() ~= bridge_peer then return end
				local conf = channel.MATRIX_CONF_PENDING:get()
				if conf and conf ~= "" then
					channel.PJSIP_HEADER("add", "X-Conference"):set(conf)
				end
				local who = channel.MATRIX_RECIPIENT:get()
				if who and who ~= "" then
					channel.PJSIP_HEADER("add", "X-Matrix-Recipient"):set(who)
				end
			end)
		end;
	};

	-- A Matrix user's outbound call; reaching this context requires the [matrixbridge]
	-- peer, which is the whole authorisation check.
	--
	-- Originate, not Dial: the far end must land in the CONFERENCE, not bridged to this
	-- media-less leg. It blocks until answered, which gives ORIGINATE_STATUS. The 200 is
	-- withheld until then: the bridge joins livekit-sip only after its INVITE is answered.
	matrixbridge = {
		["_."] = function(context, extension)
			-- pcall the decision only: Hangup() unwinds through a Lua error, so the app
			-- calls stay outside it.
			local ok, peer, num, conf, cause, reason = pcall(matrix_outbound_route,
				-- PJSIP_HEADER, not SIP_HEADER: the latter reads nothing on chan_pjsip.
				extension, channel.PJSIP_HEADER("read", "X-Conference"):get(),
				matrix_caller_uri(channel.PJSIP_HEADER("read", "X-Matrix-Caller"):get()))
			if not ok then
				app.log("NOTICE", "matrix outbound: " .. tostring(peer))
				app.hangup("34")
				return
			end
			app.log("NOTICE", "matrix outbound exten=[" .. tostring(extension)
				.. "] num=[" .. tostring(num) .. "] peer=[" .. tostring(peer)
				.. "] conf=[" .. tostring(conf) .. "] reject=[" .. tostring(reason) .. "]")
			if not peer then
				app.hangup(tostring(cause))
				return
			end
			app.ringing()
			-- Originate() does not watch this channel: a CANCEL while the phone rings
			-- cannot stop it and the callee lands alone in the conference. Naming the
			-- conference first lets h clear it.
			channel.MATRIX_CONF:set(conf)
			app.originate(dial_string(sip_routing, peer, num), "exten", "matrix-outbound",
				conf, "1", "45")
			local status = channel.ORIGINATE_STATUS:get()
			app.log("NOTICE", "matrix outbound ORIGINATE_STATUS=[" .. tostring(status) .. "]")
			-- pbx_lua does not abort on hangup, so test for the CANCEL. Returning with
			-- MATRIX_CONF set asks h to end the conference.
			if type(check_hangup) == "function" and check_hangup() then
				app.log("NOTICE", "matrix outbound: caller gone while ringing, ending " .. conf)
				return
			end
			if status ~= "SUCCESS" then
				channel.MATRIX_CONF:set("")
				app.hangup(tostring(matrix_outbound_cause(status)))
				return
			end
			-- Returning hangs this leg up, as the bridge expects. Clear MATRIX_CONF only
			-- after Answer() succeeded, so a leg that could not answer still ends the
			-- conference.
			app.answer()
			channel.MATRIX_CONF:set("")
		end;

		["h"] = function(context, extension)
			pcall(matrix_end_conference)
		end;

		["i"] = matrix_ignore;
		["t"] = matrix_ignore;
	};

	-- Where the Originate() leg lands; reachable only from there. matrix_caller, never the
	-- marked profile.
	["matrix-outbound"] = {
		["_."] = function(context, extension)
			channel.MATRIX_CONF:set(extension)
			app.confbridge(extension, "default_bridge", "matrix_caller")
		end;

		["h"] = function(context, extension)
			pcall(matrix_end_conference)
		end;

		["i"] = matrix_ignore;
		["t"] = matrix_ignore;
	};

	-- Every out-of-call SIP MESSAGE lands here via message_context: a DISPATCHER. Unset, a
	-- trunk MESSAGE would go into `public`, whose Message branch is dead.
	["messages-in"] = {
		["_."] = function(context, extension)
			local from = channel.MESSAGE("from"):get()
			if sms_is_outbound(from, extension) then
				sms_deliver_outbound(extension, from)
			else
				sms_deliver_inbound(extension, from)
			end
			app.hangup()
		end;

		["h"] = matrix_ignore;
		["i"] = matrix_ignore;
		["t"] = matrix_ignore;
	};

	-- livekit-sip joins a parked caller's conference with the bridge name as extension;
	-- whatever it asks for is what it gets. Reaching this context requires the [livekit]
	-- peer's credentials.
	livekit = {
		["_."] = function(context, extension)
			app.answer()
			-- The MARKED user: the bridge ends a call by dropping livekit-sip from the
			-- room, and confbridge.conf's pairing hangs the caller up.
			app.confbridge(extension, "default_bridge", "matrix_marked")
		end;

		["h"] = matrix_ignore;
		["i"] = matrix_ignore;
		["t"] = matrix_ignore;
	};

	jingle = {
		["jingle-endpoint"] = function(context, extension)
			local jid = channel.CALLERID("name"):get()
			local from = jid_unescape(jid:sub(0, jid:find("@") - 1))
			local to = local_to_sip_uri(jid_unescape(jid:sub(jid:find("/") + 1)))
			local label, peer, exten, rule, kind, line
			to, label, peer, exten, rule, kind, line = resolve_destination(to, "xmpp:" .. from)

			channel.CALLERID("all"):set(from .. "<" .. from .. ">")

			app.log("NOTICE", "outbound to=[" .. tostring(to) .. "] label=[" .. tostring(label)
				.. "] kind=[" .. tostring(kind)
				.. "] exten=[" .. tostring(exten)
				.. "] peer=[" .. tostring(peer)
				.. "] rule=[" .. tostring(rule and rule.to_pattern)
				.. "] line=[" .. tostring(line)
				.. "] emergency=[" .. tostring(rule and rule.emergency or false) .. "]")
			if peer and peer ~= "" then
				app.dial(dial_string(sip_routing, peer,
					exten:gsub("\\", "\\\\"):gsub("&", "")))
				return
			end

			app.log("NOTICE", "Cannot dial '" .. to .. "': " .. refusal_reason(kind, line, label))
		end;
	};

	-- An XMPP user's message to the trunk; mirrors sms_deliver_outbound. A #label is
	-- dropped with the host part, so the table decides the line.
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
			app.MessageSend(message_target(sip_routing, peer, num))
			app.log("NOTICE", "xmpp outbound num=[" .. tostring(num) .. "] peer=[" .. tostring(peer)
				.. "] caller=[" .. tostring(caller)
				.. "] status=[" .. tostring(channel.MESSAGE_SEND_STATUS:get() or "") .. "]")
		end;
	};
}
