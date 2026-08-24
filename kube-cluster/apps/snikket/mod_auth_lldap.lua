-- A verbatim fork of prosody's mod_auth_ldap with two additions, both marked
-- "LHNS:" below:
--
--   1. provider.users(), which the stock module lacks -- so the portal's user
--      list iterates nil and returns HTTP 500.
--   2. an authentication-success hook mapping an lldap group onto a prosody
--      role, so admin follows the directory instead of a static list.
--
-- One module rather than two, so both share the connection, options and filter.
--
-- To update: re-copy the stock module out of the image and re-apply the two
-- LHNS blocks. Do not hand-merge the rest.

-- mod_auth_ldap

local new_sasl = require "prosody.util.sasl".new;
local lualdap = require "lualdap";

local function ldap_filter_escape(s)
	return (s:gsub("[*()\\%z]", function(c) return ("\\%02x"):format(c:byte()) end));
end

-- Config options
local ldap_server = module:get_option_string("ldap_server", "localhost");
local ldap_rootdn = module:get_option_string("ldap_rootdn", "");
local ldap_password = module:get_option_string("ldap_password", "");
local ldap_tls = module:get_option_boolean("ldap_tls");
local ldap_scope = module:get_option_enum("ldap_scope", "subtree", "base", "onelevel");
local ldap_filter = module:get_option_string("ldap_filter", "(uid=$user)"):gsub("%%s", "$user", 1);
local ldap_base = assert(module:get_option_string("ldap_base"), "ldap_base is a required option for ldap");
local ldap_mode = module:get_option_enum("ldap_mode", "bind", "getpasswd");
local ldap_admins = module:get_option_string("ldap_admin_filter",
	module:get_option_string("ldap_admins")); -- COMPAT with mistake in documentation
local host = ldap_filter_escape(module:get_option_string("realm", module.host));

if ldap_admins then
	module:log("error", "The 'ldap_admin_filter' option has been deprecated, "..
	           "and will be ignored. Equivalent functionality may be added in "..
	           "the future if there is demand."
	);
end

-- Initiate connection
local ld = nil;
module.unload = function() if ld then pcall(ld, ld.close); end end

function ldap_do_once(method, ...)
	if ld == nil then
		local err;
		ld, err = lualdap.open_simple(ldap_server, ldap_rootdn, ldap_password, ldap_tls);
		if not ld then return nil, err, "reconnect"; end
	end

	-- luacheck: ignore 411/success
	local success, iterator, invariant, initial = pcall(ld[method], ld, ...);
	if not success then ld = nil; return nil, iterator, "search"; end

	local success, dn, attr = pcall(iterator, invariant, initial);
	if not success then ld = nil; return success, dn, "iter"; end

	return dn, attr, "return";
end

function ldap_do(method, retry_count, ...)
	local dn, attr, where;
	for _=1,1+retry_count do
		dn, attr, where = ldap_do_once(method, ...);
		if dn or not(attr) then break; end -- nothing or something found
		module:log("warn", "LDAP: %s %s (in %s)", tostring(dn), tostring(attr), where);
		-- otherwise retry
	end
	if not dn and attr then
		module:log("error", "LDAP: %s", tostring(attr));
	end
	return dn, attr;
end

function get_user(username)
	module:log("debug", "get_user(%q)", username);
	return ldap_do("search", 2, {
		base = ldap_base;
		scope = ldap_scope;
		sizelimit = 1;
		filter = ldap_filter:gsub("%$(%a+)", {
			user = ldap_filter_escape(username);
			host = host;
		});
	});
end

local provider = {};

function provider.create_user(username, password) -- luacheck: ignore 212
	return nil, "Account creation not available with LDAP.";
end

function provider.user_exists(username)
	return not not get_user(username);
end

function provider.set_password(username, password)
	local dn, attr = get_user(username);
	if not dn then return nil, attr end
	if attr.userPassword == password then return true end
	return ldap_do("modify", 2, dn, { '=', userPassword = password });
end

-- LHNS: list accounts. Reuses ldap_filter with $user as "*", so the list is
-- exactly the set that can log in and cannot drift from it. No paging, which is
-- fine at this size.
function provider.users()
	if ld == nil then
		local err;
		ld, err = lualdap.open_simple(ldap_server, ldap_rootdn, ldap_password, ldap_tls);
		if not ld then
			module:log("error", "LDAP: cannot connect to list users: %s", tostring(err));
			return function () end;
		end
	end
	local ok, iter, invariant, initial = pcall(ld.search, ld, {
		base = ldap_base;
		scope = ldap_scope;
		filter = ldap_filter:gsub("%$(%a+)", {
			user = "*";
			host = host;
		});
		attrs = { "uid" };
	});
	if not ok then
		module:log("error", "LDAP: user listing failed: %s", tostring(iter));
		ld = nil;
		return function () end;
	end
	return function ()
		local dn, attr = iter(invariant, initial);
		if not dn then return nil; end
		return attr and attr.uid;
	end
end

if ldap_mode == "getpasswd" then
	function provider.get_password(username)
		local dn, attr = get_user(username);
		if dn and attr then
			return attr.userPassword;
		end
	end

	function provider.test_password(username, password)
		return provider.get_password(username) == password;
	end

	function provider.get_sasl_handler()
		return new_sasl(module.host, {
			plain = function(sasl, username) -- luacheck: ignore 212/sasl
				local password = provider.get_password(username);
				if not password then return "", nil; end
				return password, true;
			end
		});
	end
elseif ldap_mode == "bind" then
	local function test_password(userdn, password)
		local ok, err = lualdap.open_simple(ldap_server, userdn, password, ldap_tls);
		if not ok then
			module:log("debug", "ldap open_simple error: %s", err);
		end
		return not not ok;
	end

	function provider.test_password(username, password)
		local dn = get_user(username);
		if not dn then return end
		return test_password(dn, password)
	end

	function provider.get_sasl_handler()
		return new_sasl(module.host, {
			plain_test = function(sasl, username, password) -- luacheck: ignore 212/sasl
				return provider.test_password(username, password), true;
			end
		});
	end
else
	module:log("error", "Unsupported ldap_mode %s", tostring(ldap_mode));
end


-- LHNS: keep prosody's admin role in step with an lldap group.
--
-- Runs on every successful authentication, so a group change in lldap takes
-- effect on the user's next login. Roles are stored locally by JID, so this
-- converges that store toward the directory.
--
-- It manages the admin bit and nothing else. Roles assigned deliberately
-- (prosody:restricted, prosody:operator) are left alone -- reading them as "not
-- admin" and demoting would undo a decision made elsewhere.
--
-- Demotion goes to the role a plain account has anyway, not prosody:member:
-- moving everyone to member on login would be an unrelated privilege change.
local ldap_admin_group = module:get_option_string("ldap_admin_group");
local ldap_admin_default_role = module:get_option_string("ldap_admin_default_role", "prosody:registered");
if ldap_admin_group then
	local usermanager = require "prosody.core.usermanager";

	-- Roles this hook is allowed to move a user off. Anything else is left as is.
	local managed = {
		["prosody:admin"] = true,
		["prosody:member"] = true,
		["prosody:registered"] = true,
		["prosody:guest"] = true,
	};

	local function is_in_admin_group(username)
		local dn = ldap_do("search", 2, {
			base = ldap_base;
			scope = ldap_scope;
			sizelimit = 1;
			filter = ("(&(uid=%s)(memberOf=%s))"):format(
				ldap_filter_escape(username), ldap_filter_escape(ldap_admin_group));
			attrs = { "uid" };
		});
		return not not dn;
	end

	module:hook("authentication-success", function (event)
		local session = event.session;
		local username = session and session.username;
		if not username then return; end

		local current = usermanager.get_user_role(username, module.host);
		local current_name = current and current.name;
		if current_name and not managed[current_name] then return; end

		local want = is_in_admin_group(username) and "prosody:admin" or nil;
		if not want then
			-- Only act on a non-member if they are currently admin. Someone who is
			-- neither in the group nor an admin needs no change, and rewriting their
			-- role on every login would churn the role store for nothing.
			if current_name ~= "prosody:admin" then return; end
			want = ldap_admin_default_role;
		end
		if current_name == want then return; end

		local ok, err = usermanager.set_user_role(username, module.host, want);
		if ok then
			module:log("info", "role for %s: %s -> %s (from lldap group %q)",
				username, tostring(current_name), want, ldap_admin_group);
		else
			module:log("error", "could not set role %s for %s: %s", want, username, tostring(err));
		end
	end);
end

module:provides("auth", provider);
