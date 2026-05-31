-- Plan §7.6 — monotonic state advance, UUID-fenced.
-- Strict: requires lock ownership match AND current state in allowed_prior.
-- KEYS[1] = pocket:state:<recording_id>
-- KEYS[2] = pocket:lock:<recording_id>
-- ARGV[1] = owner_uuid
-- ARGV[2] = new_state
-- ARGV[3] = ttl_seconds (state key TTL after SET)
-- ARGV[4..N] = allowed prior states
-- Returns: {1, new_state} on success; {0, reason, current_state} on rejection
--   reasons: 'ownership_lost' | 'non_monotonic'
if redis.call('GET', KEYS[2]) ~= ARGV[1] then
  return {0, 'ownership_lost', redis.call('GET', KEYS[1]) or 'none'}
end
local current = redis.call('GET', KEYS[1])
if current == false then current = 'none' end
local allowed = false
for i = 4, #ARGV do
  if ARGV[i] == current then allowed = true; break end
end
if not allowed then
  return {0, 'non_monotonic', current}
end
redis.call('SET', KEYS[1], ARGV[2], 'EX', ARGV[3])
return {1, ARGV[2]}
