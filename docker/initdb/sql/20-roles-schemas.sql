-- Roles (least privilege)
--   cabler_owner : owns all objects, runs migrations. Not used by the application.
--   cabler_app   : read/write on data schemas, cannot change structure.
--   cabler_ro    : read only (analysts, web app, QGIS, ArcGIS).
-- Passwords are read from the container environment so they never appear in this file.
\getenv dbname POSTGRES_DB
\getenv owner_pw CABLER_OWNER_PASSWORD
\getenv app_pw CABLER_APP_PASSWORD
\getenv ro_pw CABLER_RO_PASSWORD

CREATE ROLE cabler_owner LOGIN PASSWORD :'owner_pw';
CREATE ROLE cabler_app   LOGIN PASSWORD :'app_pw';
CREATE ROLE cabler_ro    LOGIN PASSWORD :'ro_pw';

REVOKE ALL ON DATABASE :"dbname" FROM PUBLIC;
GRANT CONNECT ON DATABASE :"dbname" TO cabler_owner, cabler_app, cabler_ro;

-- Data flows raw -> staging -> core -> results. meta holds bookkeeping (migrations, lineage).
CREATE SCHEMA raw     AUTHORIZATION cabler_owner;
CREATE SCHEMA staging AUTHORIZATION cabler_owner;
CREATE SCHEMA core    AUTHORIZATION cabler_owner;
CREATE SCHEMA results AUTHORIZATION cabler_owner;
CREATE SCHEMA meta    AUTHORIZATION cabler_owner;

REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public, raw, staging, core, results, meta
    TO cabler_owner, cabler_app, cabler_ro;
GRANT CREATE ON SCHEMA public TO cabler_owner;

-- Objects created later by cabler_owner automatically get the right privileges.
ALTER DEFAULT PRIVILEGES FOR ROLE cabler_owner IN SCHEMA raw, staging, core, results, meta
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO cabler_app;
ALTER DEFAULT PRIVILEGES FOR ROLE cabler_owner IN SCHEMA raw, staging, core, results, meta
    GRANT USAGE, SELECT ON SEQUENCES TO cabler_app;
ALTER DEFAULT PRIVILEGES FOR ROLE cabler_owner IN SCHEMA raw, staging, core, results, meta
    GRANT SELECT ON TABLES TO cabler_ro;
ALTER DEFAULT PRIVILEGES FOR ROLE cabler_owner IN SCHEMA raw, staging, core, results, meta
    GRANT SELECT ON SEQUENCES TO cabler_ro;
