# Azure deployment runbook (Phase 5)

This is the one-time, manual cutover that lifts the nightly screener to Azure. The
**code** is already cloud-ready; this runbook provisions the infrastructure, grants
the managed identity, runs the first screen as a go/no-go gate, and wires CD.

> **Clean cutover.** Historical rows in your local `local.db` (signals, trades, paper
> trades) are **not migrated** — Azure SQL starts empty and Alembic builds the schema
> on first run. If you want to keep forward-testing locally, keep running locally; the
> cloud store is a fresh start.

## Architecture in one paragraph

One container image (`Dockerfile`) is run as five scheduled **Azure Container Apps
Jobs**. The image ENTRYPOINT is a US-Eastern gate (`swing_screener.ops.eastern_gate`)
that a UTC cron triggers; the gate decides at Eastern wall-time whether to `exec` the
real CLI. Signals/trades live in **Azure SQL** (Alembic owns the schema); chart PNGs
live in a **private Blob container**; secrets live in **Key Vault**. Everything authes
with **one user-assigned managed identity (UAMI)** — there are **no stored credentials
anywhere** (no SQL logins, no storage keys, no service-principal secrets). The
**dashboard stays local**, pointed at Azure SQL via `az login`. CD auto-builds and
repoints the jobs on merge to `main` via GitHub→Azure **OIDC**.

## Prerequisites

- An Azure subscription and `az` CLI (`az login`).
- Your own Entra (Azure AD) **user object id** (`az ad signed-in-user show --query id -o tsv`)
  — you become the SQL AAD admin and a Blob reader for the local dashboard.
- The real secret value: `ANTHROPIC_API_KEY` (and the digest recipient). Passed to the deploy
  as `@secure()` params and stored only in Key Vault — **never commit them**. **Email needs no
  secret**: it sends via Azure Communication Services authenticated by the managed identity.

## Step 1 — Provision (Bicep)

The IaC is under [`infra/`](../infra/): `main.bicep` (subscription-scoped, creates the
resource group) wires modules for the registry, the UAMI, the Container Apps environment
(+ a cost-capped Log Analytics workspace), storage, Key Vault, Azure SQL, and the five
jobs. Validate then deploy:

```bash
az bicep build --file infra/main.bicep            # compile check (no subscription needed)
az deployment sub create \
  --location <region> \
  --template-file infra/main.bicep \
  --parameters infra/main.bicepparam \
  --parameters anthropicApiKey=<...> digestTo=<you@example.com> \
               sqlAadAdminLogin=<your-upn> sqlAadAdminObjectId=<your-object-id> \
               sqlAllowedIps='["<your-home-ip>"]' \
  --what-if                                         # review first, then re-run without --what-if
  # Email sends via Azure Communication Services + managed identity (no secret).
  # On re-deploys add seedSecrets=false so an already-set ANTHROPIC_API_KEY isn't overwritten.
```

- **Azure SQL defaults to the Basic DTU tier** (~$5/mo flat, no cold start). A
  `useServerless=true` param switches it to GP serverless (auto-pause) — only worth it if
  the DB genuinely idles >16h/day; the hourly intraday job keeps it warm, so Basic is
  usually cheaper.
- **SQL firewall** is restricted to the IPs you pass in `sqlAllowedIps` — start with your
  home IP (for the dashboard). A Consumption Container Apps environment doesn't expose a
  stable egress IP as a deploy output, so add the env's outbound IP(s) in a follow-up once
  known: `az sql server firewall-rule create -g <rg> -s <server> -n ca-egress --start-ip-address <ip> --end-ip-address <ip>`. The broad "allow all Azure IPs" rule is deliberately
  **not** used. (Entra-only auth still gates access; this just minimizes the network
  surface. A private endpoint is the stronger Phase-6 option.)
- The Log Analytics workspace has a **daily ingestion cap** and 30-day retention so the
  ~9×/day jobs don't run up a logging bill.

## Step 2 — Build + push the image

```bash
az acr build --registry <acr-name> --image swing-screener:bootstrap .
```

`az acr build` builds **server-side** in ACR — more reliable for the `msodbcsql18` layer
than a local `docker build` (Microsoft rotates the `packages.microsoft.com` signing key
periodically, which breaks a local build of that layer).

## Step 3 — One-time SQL grants (`infra/post-deploy.sql`)

Bicep can't create the *contained database users* for the managed identity, so run this
once as the Entra admin (via `sqlcmd -G` or the portal Query editor) against the
screener database. See [`infra/post-deploy.sql`](../infra/post-deploy.sql):

```sql
CREATE USER [<uami-name>] FROM EXTERNAL PROVIDER;
ALTER ROLE db_datareader ADD MEMBER [<uami-name>];
ALTER ROLE db_datawriter ADD MEMBER [<uami-name>];
ALTER ROLE db_ddladmin  ADD MEMBER [<uami-name>];   -- Alembic issues CREATE/ALTER TABLE
-- your own Entra user, so the LOCAL dashboard can read:
CREATE USER [<your-entra-upn>] FROM EXTERNAL PROVIDER;
ALTER ROLE db_datareader ADD MEMBER [<your-entra-upn>];
```

Then grant your human identity **Storage Blob Data Reader** so the local dashboard can
download charts:

```bash
az role assignment create --assignee <your-object-id> \
  --role "Storage Blob Data Reader" --scope <storage-account-resource-id>
```

Wait a few minutes for Entra propagation.

## Step 4 — First cloud screen = the yfinance go/no-go gate

> **Existential risk, do not skip.** From Azure datacenter IPs, yfinance is often
> rate-limited or blocked, which can make a cloud screen produce **zero signals**. A local
> dry-run will *not* surface this — it only appears from an Azure IP.

Run the evening screen manually (override the gate hour so it doesn't wait for cron):

```bash
az containerapp job start --name evening-screen -g <rg> \
  --env-vars RUN_IF_ET_HOUR=$(TZ=America/New_York date +%H)
az containerapp job execution list --name evening-screen -g <rg> -o table
```

Verify in the logs (Log Analytics `ContainerAppConsoleLogs_CL`): Alembic created the six
tables, signals persisted, chart PNGs landed in the private `charts` container, **and read
`n_failed` vs the universe size**. If the failure rate is materially high (say >25%), **stop
and fix before wiring the cadence live**: route yfinance through an outbound proxy/NAT with
a non-Azure egress IP, or swap the data source behind the existing `data.fetch` seam. The
fetch layer already retries with jittered backoff, but that alone won't beat a hard block.

Then smoke the digest + exit paths:

```bash
az containerapp job start --name daily-digest  -g <rg> --env-vars RUN_IF_ET_HOUR=$(TZ=America/New_York date +%H)
az containerapp job start --name intraday-exit -g <rg> --env-vars RUN_IF_ET_HOUR=$(TZ=America/New_York date +%H)  # against a seeded open real trade
```

## Step 5 — Point the local dashboard at Azure SQL

The dashboard **stays local**. Connection strings come in two forms — note the
**clientId, not objectId** gotcha for the job:

| Context | `SWING_DB_URL` auth |
|---|---|
| Jobs (in Azure) | `...;Authentication=ActiveDirectoryMSI;User Id=<UAMI clientId>` (Bicep sets this) |
| Local dashboard | `...;Authentication=ActiveDirectoryDefault` (uses your `az login`) |

```bash
az login
$env:SWING_DB_URL = "mssql+pyodbc://<server>.database.windows.net/swing?driver=ODBC+Driver+18+for+SQL+Server&Authentication=ActiveDirectoryDefault"
$env:SWING_BLOB_ACCOUNT_URL = "https://<storage>.blob.core.windows.net"
$env:SWING_BLOB_CONTAINER = "charts"
streamlit run src/swing_screener/dashboard/app.py
```

The dashboard reads candidates from Azure SQL and downloads charts from Blob (as Storage
Blob Data Reader). It needs **ODBC Driver 18** locally (`msodbcsql18`).

## Step 6 — Wire CD (GitHub → Azure via OIDC)

[`.github/workflows/cd.yml`](../.github/workflows/cd.yml) runs the full test suite then,
only if green, builds the image and repoints the jobs. It authenticates with **OIDC
federated credentials — no stored client secret**. One-time setup:

1. Create a deploy identity (app registration or a UAMI) and add a **federated
   credential** with:
   - **subject** exactly `repo:solorzao/swing-screener:ref:refs/heads/main`
   - **audience** `api://AzureADTokenExchange`
   - **issuer** `https://token.actions.githubusercontent.com`

   A wrong subject **or** audience/issuer fails the token exchange silently — the
   audience/issuer are the more common silent failures.
2. Grant it **least privilege only**: `AcrPush` on the ACR, plus a role that permits
   `containerapp job update` on the resource group. Re-verify the exact job-management
   role name/id with `az role definition list --query "[?contains(roleName,'Container Apps')]"`;
   do **not** grant Contributor.
3. Add repo **secrets** `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID`
   (identifiers, not credentials — OIDC mints short-lived tokens at run time) and repo
   **variables** `AZURE_RESOURCE_GROUP`, `ACR_NAME`.

`job update --image` does not start a run; the new image is used on the next scheduled
(or manual) execution. Rollback = re-run the update with an older `:<sha>` tag.

## Schedules (UTC cron + Python ET gate)

ACA Jobs cron is **UTC-only**, so each job's UTC cron fires a *superset* of times (covering
both EST and EDT) and the Python gate picks the right Eastern hour. "Last business day"
(monthly) is likewise solved in Python, not cron.

| Job | cron (UTC) | runs | gate |
|---|---|---|---|
| evening-screen | `15 20,21 * * 1-5` | `pipeline.run` (~16:15 ET) | `RUN_IF_ET_HOUR=16` |
| daily-digest | `0 12,13 * * 1-5` | `notify.run --kind daily` (~08:00 ET) | `RUN_IF_ET_HOUR=8` |
| intraday-exit | `0 13-21 * * 1-5` | `notify.run --kind exit` (hourly) | `RUN_IF_ET_HOUR=9..16` |
| weekly-digest | `30 20,21 * * 5` | `notify.run --kind weekly` (Fri after close) | `RUN_IF_ET_HOUR=16` |
| monthly-digest | `30 20,21 * * 1-5` | `notify.run --kind monthly` (last session) | `RUN_IF_ET_HOUR=16` + `RUN_IF_LAST_BUSINESS_DAY=1` |
| on-demand-analysis | `*/15 * * * *` | `notify.ondemand` (drains the request queue) | none — runs every firing |

On a DST-transition day a UTC cron pair can fire **twice** (or zero times) for the intended
ET hour. This is safe because the workloads are **idempotent**: the screen delete+reinserts
per `run_date` and `advance_open` guards on `last_advanced == today`; digests/exit alerts
dedupe via the `EmailLog` unique constraint. (Pinned by `test_run_double_fire_*`.) The
**on-demand-analysis** worker is ungated (it polls the `analysis_requests` queue every 15 min);
it dedupes emails via `EmailLog` (`kind="ondemand"`), claims rows atomically, and requeues
stale `running` rows so a crashed retry recovers. Like the others, the **new job is created by
re-running the provisioning** (`az deployment sub create`, Step 1) — CD only updates images;
the `analysis_requests` table is created by `alembic upgrade head` on the job's first run.

## Operational notes

- **Cold resume:** Azure SQL (serverless option) auto-pauses; the first request of the day
  fails with error **40613** while it wakes (~1 min). `run_screen` retries the startup
  `alembic upgrade head` with backoff, so the evening screen survives it.
- **Chart TTL:** a lifecycle rule deletes chart blobs after ~90 days, but `Signal.chart_path`
  rows persist longer — old charts 404 and the PDF/dashboard degrade to a chartless section
  (handled in code; not an error).
- **Secrets** are injected into the jobs as env vars from Key Vault by the UAMI; the code
  logs secret **names only, never values**.

## Cost

~**$10–12/month** on the default path: ACR Basic (~$5) + Azure SQL Basic DTU (~$5) +
Container Apps Jobs (≈$0 within the free grant) + Storage/Key Vault (pennies) + a capped
Log Analytics workspace. CD is ~$0 (OIDC + free Actions minutes at this volume).
