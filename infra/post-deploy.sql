-- post-deploy.sql -- one-time, run by the Entra (AAD) admin against the `swing`
-- database AFTER `az deployment sub create` (Bicep cannot create contained DB
-- users for a managed identity). Connect with Entra auth, e.g.:
--   sqlcmd -S <server>.database.windows.net -d swing -G -U <your-upn>
-- or the Azure Portal Query editor (AAD login). Replace the placeholders below.
--
-- Auth is Entra-only (azureADOnlyAuthentication=true) -- there are NO SQL logins
-- or passwords. These statements just map existing Entra principals to DB roles.

-- 1) The job's user-assigned managed identity (UAMI). Use the UAMI's NAME
--    (its display name in Entra), which is the `uamiName` Bicep created,
--    e.g. swing-uami-<token>.
CREATE USER [<uami-name>] FROM EXTERNAL PROVIDER;
ALTER ROLE db_datareader ADD MEMBER [<uami-name>];
ALTER ROLE db_datawriter ADD MEMBER [<uami-name>];
ALTER ROLE db_ddladmin  ADD MEMBER [<uami-name>];   -- Alembic issues CREATE/ALTER TABLE
GO

-- 2) Your own Entra user -- the LOCAL cockpit's principal (ActiveDirectoryDefault).
--    The cockpit is NOT read-only: its actions WRITE -- DISARM events
--    (disarm_events), manual trade closes (trades UPDATE + exit_events insert),
--    manual journal entries/notes/tags (journal_reviews, journal_notes, tags,
--    trade_tags), coach tag-confirms/edits (journal_reviews), and audit ACKs
--    (system_audits UPDATE). db_datawriter covers that whole (growing) DML
--    surface; the schema stays Alembic-owned via the UAMI -- deliberately NO
--    db_ddladmin here.
--    (Deployed before 2026-07? Re-run just the db_datawriter line to upgrade.)
CREATE USER [<your-entra-upn>] FROM EXTERNAL PROVIDER;
ALTER ROLE db_datareader ADD MEMBER [<your-entra-upn>];
ALTER ROLE db_datawriter ADD MEMBER [<your-entra-upn>];
GO

-- 3) The GitHub deploy identity (the cd.yml/reflect.yml OIDC app registration or UAMI),
--    so the weekly REFLECTION workflow can READ the forward book (paper_trades etc.).
--    Read-only: reflection writes edge/ files in the repo, never the database. Use the
--    identity's Entra DISPLAY NAME. The same identity also needs firewall-rule write on
--    the SQL SERVER resource (e.g. "SQL Server Contributor" scoped to the server) so
--    reflect.yml can open/close its ephemeral per-run firewall rule.
CREATE USER [<deploy-identity-name>] FROM EXTERNAL PROVIDER;
ALTER ROLE db_datareader ADD MEMBER [<deploy-identity-name>];
GO

-- Entra role propagation can take a few minutes before the new users can connect.
