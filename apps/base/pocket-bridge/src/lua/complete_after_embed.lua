-- Plan §7.6 (v8 D17 / F8-001, revised v11 P10-005) — embed_pending → complete.
-- NO lock required (poller doesn't hold the lease that ingest used).
-- Monotonic-only: rejects any state other than embed_pending → complete.
-- Also DELs the persistent stalled-marker on success (P10-005).
-- KEYS[1] = pocket:state:<recording_id>
-- KEYS[2] = pocket:embed_stalled:<recording_id>
-- ARGV[1] = ttl_seconds (complete state TTL)
-- Returns: {1, 'completed'} | {0, 'already_complete'} | {0, 'not_embed_pending', current_state}
local current = redis.call('GET', KEYS[1])
if current == 'embed_pending' then
  redis.call('SET', KEYS[1], 'complete', 'EX', ARGV[1])
  redis.call('DEL', KEYS[2])
  return {1, 'completed'}
elseif current == 'complete' then
  redis.call('DEL', KEYS[2])
  return {0, 'already_complete'}
else
  return {0, 'not_embed_pending', current or 'none'}
end
