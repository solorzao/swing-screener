// =============================================================================
// main.bicepparam -- sample parameters for the Phase 5 deployment.
//
// SECRETS: the @secure() params below are left as EMPTY placeholders. Real
// values are passed at deploy time and MUST NEVER be committed, e.g.:
//
//   az deployment sub create \
//     --location westus2 \
//     --template-file infra/main.bicep \
//     --parameters infra/main.bicepparam \
//     --parameters anthropicApiKey=$ANTHROPIC_API_KEY digestTo=$DIGEST_TO
//
// Email sends via Azure Communication Services using the managed identity, so
// there is NO Gmail password to store. (Or use a Key Vault reference / CI secret
// store. Do not paste secrets here.)
// =============================================================================

using './main.bicep'

param location = 'eastus'
param namePrefix = 'swing'
param imageTag = 'bootstrap'

// --- Azure SQL AAD admin (replace placeholders with real values) ---
// e.g. an Entra security group that owns the database.
param sqlAadAdminLogin = 'REPLACE-WITH-AAD-ADMIN-GROUP-OR-UPN'
param sqlAadAdminObjectId = '00000000-0000-0000-0000-000000000000'
param sqlAadAdminPrincipalType = 'Group'

// --- SQL firewall allow-list ---
// Fill in post-deploy with the Container Apps env outbound IP(s) and your home
// IP. Empty here means NO inbound is permitted until you add IPs (safe default).
// Example: ['203.0.113.10', '198.51.100.7']
param sqlAllowedIps = []

// --- DB tier: Basic DTU by default; flip to true for GP_S_Gen5 serverless ---
param useServerless = false

// --- Log Analytics cost caps ---
param logDailyQuotaGb = 1
param logRetentionInDays = 30

// --- Secrets: leave empty here; pass real values at deploy time only ---
param anthropicApiKey = ''
param digestTo = ''

// --- Ops alerts recipient: not a secret, but pass the real address at deploy
// time like digestTo (an empty address fails the action group deployment
// LOUDLY -- alerting must never be silently absent). ---
param alertEmail = ''

// --- Sizing: the funded account's equity in dollars (2026-07-17 decision --
// a $1,000 starter account). 1R = equity x riskPct, conviction-scaled per
// pick; picks whose per-share risk exceeds the budget size to 0 shares
// (honest: they don't fit this account). Empty = R-multiples only. ---
param accountEquity = '1000'

// --- Risk per trade: 10% of equity (2026-07-17 decision -- deliberately hot
// for the $1,000 starter: 1R = $100, so nearly every pick sizes to real
// shares; a -3R day is -30% of the account. Revisit as the account grows). ---
param riskPct = '0.10'

// --- Execution: PAPER (2026-07-17 decision) -- the PaperAdapter opens
// simulated fills in the curated account="paper" intent book (no broker, no
// dollars, fenced out of research stats). This is the North-Star Mid-term
// order-flow proving leg: intents -> sizing -> caps -> fills, end to end.
// Live stays behind the three locks + caps mandate; this param never
// arms it. ---
param executionMode = 'paper'
