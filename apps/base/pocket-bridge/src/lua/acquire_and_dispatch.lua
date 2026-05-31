-- Plan §7.6 — atomic lease+state read with embed_pending recognition.
-- KEYS[1] = pocket:state:<recording_id>
-- KEYS[2] = pocket:lock:<recording_id>
-- ARGV[1] = lease_ttl_seconds
-- ARGV[2] = owner_uuid (uuid4 from bridge)
-- Returns: {action, current_state, owner_uuid_if_taken_else_empty}
local state = redis.call('GET', KEYS[1])
if state == false then state = 'none' end
if state == 'complete'      then return {'dedup',         state, ''} end
if state == 'embed_pending' then return {'embed_pending', state, ''} end
local lock_ok = redis.call('SET', KEYS[2], ARGV[2], 'NX', 'EX', ARGV[1])
if not lock_ok then return {'in_progress', state, ''} end
if state == 'none' then return {'start', state, ARGV[2]} end
return {'resume', state, ARGV[2]}
