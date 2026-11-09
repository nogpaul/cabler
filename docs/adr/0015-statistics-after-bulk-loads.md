# ADR-0015: Fresh statistics after every bulk load

Status: accepted · Date: 2026-11-09

## Context
The first NRW run (203,182 OpenStreetMap power objects) spent 59 minutes in the apply stage; the
same statement takes 19 seconds once the tables have been analysed, and Hamburg (about 60 times
fewer objects) had taken 0.6 seconds. The tables had just been loaded, PostgreSQL had no statistics about
them, assumed that the filter on the snapshot's version selects very few rows, and chose a plan
that is quadratic in the number of rows. The validation stage showed the same effect (more than
four minutes, then 2.3 seconds after an analyse). Autovacuum would have analysed the tables, but
up to a minute after the next stage had already been planned.

## Decision
- The stage that creates a large set of rows runs `ANALYZE` on the table it filled, inside its own
  transaction: the load stage on `raw.osm_object` or `raw.feature`, the diff stage on
  `raw.osm_change` or `raw.feature_change`, the apply stage on its staging table.
- The application role receives the `MAINTAIN` privilege (PostgreSQL 17) on the tables of `raw` and
  `staging`, including tables created later. It gets no other new right and still owns nothing.
- A test checks that these tables are never left in the never-analysed state.

## Consequences
- Each stage pays a few seconds to sample the table it has just filled, and every later stage gets
  a plan based on real row counts.
- A new source type must analyse the tables its loader and applier fill; the test of the existing
  sources shows how.
- The privilege is a maintenance right on data the role may already read and change.
