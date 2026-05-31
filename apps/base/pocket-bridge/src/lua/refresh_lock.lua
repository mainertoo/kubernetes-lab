-- Plan §7.6 — extend lease TTL iff caller owns the lock.
-- KEYS[1] = pocket:lock:<recording_id>
-- ARGV[1] = owner_uuid
-- ARGV[2] = new_ttl_seconds
-- Returns: 1 if refreshed, 0 if ownership lost.
if redis.call('GET', KEYS[1]) == ARGV[1] then
  redis.call('EXPIRE', KEYS[1], ARGV[2])
  return 1
end
return 0
