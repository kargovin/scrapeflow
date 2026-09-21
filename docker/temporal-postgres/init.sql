-- Runs once, on first boot of an empty temporal_postgres_data volume (postgres image's
-- /docker-entrypoint-initdb.d hook). POSTGRES_DB already created `temporal` — the main
-- store; this adds the visibility store. Which name is which is decided by the Temporal
-- server's config (DBNAME / VISIBILITY_DBNAME), not by anything in here.
CREATE DATABASE temporal_visibility OWNER temporal;
