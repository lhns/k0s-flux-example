#!/bin/sh
# Moves Synapse stream sequences forward to stream_positions. Never backwards, never deletes.
# GUARD_DRY_RUN=1 only reports. Connection comes from the PG* environment.
# Pairs are sequence:stream_name from synapse's id_generators; events_backfill_stream_seq is
# left out (stored negative, so Synapse's own check cannot trip on it).
set -eu

PAIRS="account_data_sequence:account_data
cache_invalidation_stream_seq:caches
device_inbox_sequence:to_device
device_lists_sequence:device_lists_stream
e2e_cross_signing_keys_sequence:e2e_cross_signing_keys
events_stream_seq:events
presence_stream_sequence:presence_stream
profile_updates_sequence:profile_updates
pushers_sequence:pushers
push_rules_stream_sequence:push_rules_stream
quarantined_media_id_seq:quarantined_media
receipts_sequence:receipts
sticky_events_sequence:sticky_events
thread_subscriptions_sequence:thread_subscriptions
un_partial_stated_event_stream_sequence:un_partial_stated_event_stream
un_partial_stated_room_stream_sequence:un_partial_stated_room_stream"

VALUES=$(echo "$PAIRS" | sed "s/^\([^:]*\):\(.*\)$/('\1','\2')/" | paste -sd, -)

# pg_sequences.last_value is NULL until the first nextval; sequences that do not exist yet
# (newer Synapse streams) drop out of the join.
rows=$(psql -X -v ON_ERROR_STOP=1 -At -F ' ' -c "
  SELECT m.seq, COALESCE(s.last_value, 0), p.mx
  FROM (VALUES $VALUES) AS m(seq, stream)
  JOIN pg_sequences s ON s.schemaname = 'public' AND s.sequencename = m.seq
  JOIN (SELECT stream_name, max(stream_id) AS mx FROM stream_positions GROUP BY 1) p
    ON p.stream_name = m.stream
  WHERE COALESCE(s.last_value, 0) < p.mx
  ORDER BY 1")

[ -n "$rows" ] || { echo "sequence-guard: all sequences at or ahead of stream_positions"; exit 0; }

echo "$rows" | while read -r seq old new; do
  echo "WARN sequence-guard: $seq $old -> $new"
  [ "${GUARD_DRY_RUN:-}" = 1 ] || psql -X -v ON_ERROR_STOP=1 -At -c "SELECT setval('public.$seq', $new)" >/dev/null
done
