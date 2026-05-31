-- Plan §7.6 — DEL the lock iff caller owns it.
-- KEYS[1] = pocket:lock:<recording_id>
-- ARGV[1] = owner_uuid
-- Returns: 1 if released, 0 if ownership lost (lock left untouched).
if redis.call('GET', KEYS[1]) == ARGV[1] then
  redis.call('DEL', KEYS[1])
  return 1
end
return 0
