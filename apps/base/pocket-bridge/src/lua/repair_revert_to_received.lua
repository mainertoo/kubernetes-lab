-- Plan §7.6 (v10 F9-002) — ONLY retrograde transition in the system.
-- Used exclusively by §7.3a R5 to drop a stuck embed_pending back to received
-- so the repair flow can re-ingest. CAS-guarded against concurrent advancement.
-- KEYS[1] = pocket:state:<recording_id>
-- Returns: {1, 'reverted'} | {0, 'not_embed_pending', current_state}
local current = redis.call('GET', KEYS[1])
if current == 'embed_pending' then
  redis.call('SET', KEYS[1], 'received')
  return {1, 'reverted'}
else
  return {0, 'not_embed_pending', current or 'none'}
end
