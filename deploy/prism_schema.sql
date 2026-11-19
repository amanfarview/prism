/* Prism's own corner of the shared Azure SQL database (farview.database.windows.net / jarvis).
   Same pattern as orbitapp and jarvisapp: its own login, sees only its own schemas, nothing else.
   prismapp OWNS prism (live), prism_staging (staging) and prism_test (deploy/azure_sql.py test), so it can create
   and fill tables there, and has no rights at all on jarvis, orbit, flo, portfolioiq or dbo.

   Run in SSMS (or Azure Data Studio) as the server admin. Pick a long random password (a password manager's
   32 characters, letters and digits only is simplest); it gets typed once more, hidden, into
   `sudo /opt/prism-venv/bin/python /var/www/prism/deploy/azure_sql.py login` on the server. Never email it or paste it in chat.

   ---------------------------------------------------------------------------------------------------------
   PART 1: in master (right-click master > New Query) */

CREATE LOGIN prismapp WITH PASSWORD = '<long random password>';
GO

/* ---------------------------------------------------------------------------------------------------------
   PART 2: in jarvis. Right-click the jarvis database > New Query (the SSMS dropdown silently stays on master).
   Check the line below says jarvis before running the rest. */

SELECT DB_NAME() AS must_say_jarvis;
GO
CREATE USER prismapp FOR LOGIN prismapp WITH DEFAULT_SCHEMA = prism;
GO
CREATE SCHEMA prism AUTHORIZATION prismapp;           -- live tables
GO
CREATE SCHEMA prism_staging AUTHORIZATION prismapp;   -- staging copy (/prism-staging)
GO
CREATE SCHEMA prism_test AUTHORIZATION prismapp;      -- azure_sql.py test creates and drops tables here only
GO
GRANT CREATE TABLE TO prismapp;                       -- needed to create tables, but only inside schemas it owns
GO

/* Check: should list prism, prism_staging and prism_test, all owned by prismapp, and nothing else for prismapp. */
SELECT s.name AS schema_name, p.name AS owner
FROM sys.schemas s JOIN sys.database_principals p ON p.principal_id = s.principal_id
WHERE p.name = 'prismapp';
GO
