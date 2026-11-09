-- The pipeline refreshes the planner statistics (ANALYZE) of a table right after it has loaded or
-- compared a large snapshot. Without fresh statistics PostgreSQL guesses that a just-loaded table
-- is small and may choose a plan that takes an hour on 200,000 rows (NRW, first run) instead of
-- seconds. ANALYZE needs the MAINTAIN privilege (PostgreSQL 17) on a table the role does not own.

GRANT MAINTAIN ON ALL TABLES IN SCHEMA raw, staging TO cabler_app;
ALTER DEFAULT PRIVILEGES FOR ROLE cabler_owner IN SCHEMA raw, staging
    GRANT MAINTAIN ON TABLES TO cabler_app;
