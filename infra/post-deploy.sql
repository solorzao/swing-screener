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

-- 2) Your own Entra user, so the LOCAL dashboard (ActiveDirectoryDefault) can
--    READ candidates. Read-only is enough; the dashboard never writes.
CREATE USER [<your-entra-upn>] FROM EXTERNAL PROVIDER;
ALTER ROLE db_datareader ADD MEMBER [<your-entra-upn>];
GO

-- Entra role propagation can take a few minutes before the new users can connect.
