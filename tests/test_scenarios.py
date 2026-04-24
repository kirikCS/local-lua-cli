"""Test scenarios for ICEQ agent evaluation.

Each scenario is a dict with:
- id:             short identifier (A1, B2, C3, ...)
- category:       "fix", "multifile", "octapi"
- description:    task prompt for the agent
- setup_files:    {path: content} — files to create before running
- expected_files: list of files that should exist (and pass lua54) after
- max_iterations: max iterations allowed for this scenario
"""

# ---------------------------------------------------------------------------
# Fixture content (embedded for benchmark portability)
# ---------------------------------------------------------------------------

_BUGGY_VALIDATOR = """\
-- Email validator module

-- Bug 1: global variable instead of local
validator = {}

function validator.is_valid_email(email)
    -- Bug 2: no nil check — email:match crashes if email is nil
    -- Bug 3: wrong pattern — the dot before [%a] is not escaped (should be %.)
    local pattern = "^[%w._%-]+@[%w.-]+.[%a]+$"
    return email:match(pattern) ~= nil
end

function validator.validate_user(user)
    local errors = {}
    if not user.name or #user.name == 0 then
        errors[#errors + 1] = "name is required"
    end
    if not validator.is_valid_email(user.email) then
        errors[#errors + 1] = "invalid email"
    end
    return #errors == 0, errors
end

-- Test
local ok, errs = validator.validate_user({name = "Alice", email = "alice@example.com"})
print("Valid user: " .. tostring(ok))

local ok2, errs2 = validator.validate_user({name = "", email = "bad"})
print("Invalid user: " .. tostring(ok2) .. " errors: " .. #errs2)
"""

_NO_ERROR_HANDLING = """\
-- Config parser: reads key=value lines from string input
-- No error handling at all — crashes on bad input

local function trim(s)
    return s:match("^%s*(.-)%s*$")
end

local function parse_config(input)
    local config = {}
    for line in input:gmatch("[^\\n]+") do
        line = trim(line)
        if #line > 0 and not line:match("^#") then
            local key, value = line:match("^(%w+)%s*=%s*(.+)$")
            config[key] = trim(value)
        end
    end
    return config
end

local function get_number(config, key)
    return tonumber(config[key])
end

local function get_bool(config, key)
    local v = config[key]
    return v == "true" or v == "1" or v == "yes"
end

-- Test with valid input
local input = "host=localhost\\nport=8080\\ndebug=true"
local config = parse_config(input)
print("Host: " .. config.host)
print("Port: " .. get_number(config, "port"))
print("Debug: " .. tostring(get_bool(config, "debug")))
"""

_ADD_FEATURE = """\
-- Simple rate limiter (single IP only)

local limiter = {}
local requests = {}
local max_requests = 10
local window = 60

local function cleanup()
    local now = os.time()
    local cutoff = now - window
    local new = {}
    for _, ts in ipairs(requests) do
        if ts > cutoff then
            new[#new + 1] = ts
        end
    end
    requests = new
end

function limiter.check()
    cleanup()
    if #requests >= max_requests then
        return false, 0
    end
    requests[#requests + 1] = os.time()
    return true, max_requests - #requests
end

-- Test
for i = 1, 12 do
    local ok, remaining = limiter.check()
    print(string.format("Request %d: %s (remaining: %d)", i, tostring(ok), remaining))
end

return limiter
"""

_UTILS = """\
local M = {}
function M.trim(s) return s:match("^%s*(.-)%s*$") end
function M.split(s, sep)
    local t = {}
    for part in s:gmatch("[^" .. sep .. "]+") do t[#t+1] = part end
    return t
end
function M.starts_with(s, prefix) return s:sub(1, #prefix) == prefix end
return M
"""


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------

SCENARIOS = [
    # =======================================================================
    # Category A — Fix existing code (patch_file)
    # =======================================================================
    {
        "id": "A1",
        "category": "fix",
        "description": "Fix all bugs in buggy_validator.lua",
        "setup_files": {
            "buggy_validator.lua": _BUGGY_VALIDATOR,
        },
        "expected_files": ["buggy_validator.lua"],
        "max_iterations": 5,
    },
    {
        "id": "A2",
        "category": "fix",
        "description": (
            "Add proper error handling to no_error_handling.lua using pcall. "
            "Wrap all dangerous operations so the script never crashes on bad input."
        ),
        "setup_files": {
            "no_error_handling.lua": _NO_ERROR_HANDLING,
        },
        "expected_files": ["no_error_handling.lua"],
        "max_iterations": 5,
    },
    {
        "id": "A3",
        "category": "fix",
        "description": (
            "Read add_feature.lua and add support for per-IP rate limiting "
            "(track each IP separately). The check function should accept an IP "
            "string as argument."
        ),
        "setup_files": {
            "add_feature.lua": _ADD_FEATURE,
        },
        "expected_files": ["add_feature.lua"],
        "max_iterations": 5,
    },

    # =======================================================================
    # Category B — Multifile (read + write + patch)
    # =======================================================================
    {
        "id": "B1",
        "category": "multifile",
        "description": (
            "Read tests/fixtures/utils.lua, then write tests/test_utils.lua that "
            "requires utils and tests all exported functions with assert and print."
        ),
        "setup_files": {
            "tests/fixtures/utils.lua": _UTILS,
        },
        "expected_files": ["tests/fixtures/utils.lua", "tests/test_utils.lua"],
        "max_iterations": 7,
    },
    {
        "id": "B2",
        "category": "multifile",
        "description": (
            "Create src/config.lua that parses key=value config strings into tables. "
            "Then create src/app.lua that requires config and parses "
            "'host=localhost\\nport=8080\\ndebug=true' and prints each value."
        ),
        "setup_files": {},
        "expected_files": ["src/config.lua", "src/app.lua"],
        "max_iterations": 7,
    },
    {
        "id": "B3",
        "category": "multifile",
        "description": (
            "Create a logging system: src/logger.lua with log_info(msg), "
            "log_error(msg) that format with timestamp. Then create src/server.lua "
            "that requires logger and simulates handling 3 HTTP requests with logging."
        ),
        "setup_files": {},
        "expected_files": ["src/logger.lua", "src/server.lua"],
        "max_iterations": 7,
    },

    # =======================================================================
    # Category C — Octapi business cases
    # =======================================================================
    {
        "id": "C1",
        "category": "octapi",
        "description": (
            "Write a Lua script that validates an incoming API request: "
            "check required fields (name, email, age), validate email format, "
            "validate age is a number between 0-150, return structured response "
            "{ok=bool, errors=table}. Include a test that prints results for "
            "valid and invalid inputs."
        ),
        "setup_files": {},
        "expected_files": ["solution.lua"],
        "max_iterations": 5,
    },
    {
        "id": "C2",
        "category": "octapi",
        "description": (
            "Write a Lua script that transforms JSON data: input has fields "
            "{first_name, last_name, birth_date, address={city, zip}}. "
            "Transform to {full_name, age, location}. Use dkjson for JSON "
            "encode/decode. Print the result as JSON."
        ),
        "setup_files": {},
        "expected_files": ["solution.lua"],
        "max_iterations": 5,
    },
    {
        "id": "C3",
        "category": "octapi",
        "description": (
            "Write a Lua rate limiter: track requests per IP using a table. "
            "Allow max 10 requests per 60 seconds per IP. Return "
            "{allowed=bool, remaining=number, reset_at=number}. "
            "Include a test that simulates requests from two different IPs."
        ),
        "setup_files": {},
        "expected_files": ["solution.lua"],
        "max_iterations": 5,
    },
]
