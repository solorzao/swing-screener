// =============================================================================
// jobs.bicep
// TEN scheduled Container Apps Jobs, all sharing ONE image and ONE UAMI.
//
// Each job keeps the image ENTRYPOINT (the US-Eastern gate,
// `python -m swing_screener.ops.eastern_gate`) and sets container `args` to the
// real CLI the gate should exec. The gate reads RUN_IF_ET_HOUR /
// RUN_IF_LAST_BUSINESS_DAY (env, per job) to decide at ET wall time whether to
// run -- the UTC cron deliberately fires on BOTH EST and EDT and the gate picks
// the right ET hour.
//
// DST double-fire is SAFE: the workloads are idempotent (the app guarantees
// this). weekly-digest and monthly-digest both fire `30 20,21 * * 5` on a
// month-end Friday and the intraday-exit may overlap, but the EmailLog unique
// constraint makes the concurrent exit-alert insert atomic, so no duplicate
// email is sent.
//
// AUTH: identity.type:'UserAssigned' (the one UAMI). registries[].identity and
// secrets[].identity both reference the UAMI resource ID -- no registry creds,
// no stored secrets. KV secrets are pulled via secrets[].keyVaultUrl.
//
// apiVersion verified against current Azure docs (2026-06): 2026-01-01 is the
// latest non-preview Microsoft.App/jobs version.
// =============================================================================

@description('Azure region for the jobs.')
param location string

@description('Container Apps managed environment resource ID.')
param environmentId string

@description('Resource ID of the UAMI (auth for ACR pull, KV secrets, identity block).')
param uamiId string

@description('Client ID of the UAMI -> AZURE_CLIENT_ID and the mssql User Id.')
param uamiClientId string

@description('ACR login server, e.g. myacr.azurecr.io.')
param acrLoginServer string

@description('Image repository name within the registry.')
param imageRepository string = 'swing-screener'

// Default 'latest' -- CD pushes :latest alongside every immutable :<sha> tag. A
// re-provision that omits imageTag must land on the current code, not roll all
// ten jobs back to the months-old bootstrap image until the next main push
// (same template-default-must-match-prod lesson as deepAnalysisEnabled below).
@description('Image tag to run.')
param imageTag string = 'latest'

// --- Plain (non-secret) environment ---
@description('mssql SQLAlchemy URL for SWING_DB_URL (Entra MSI auth, no password).')
param swingDbUrl string

@description('Blob account URL -> SWING_BLOB_ACCOUNT_URL.')
param blobAccountUrl string

@description('Blob container -> SWING_BLOB_CONTAINER.')
param blobContainer string = 'charts'

@description('ACS endpoint -> SWING_ACS_ENDPOINT (managed-identity email send).')
param acsEndpoint string

@description('ACS verified MailFrom address -> SWING_ACS_SENDER.')
param acsSender string

@description('Key Vault URL (e.g. https://vault.vault.azure.net/) -> KEY_VAULT_URL and the base for secret reference URIs.')
param keyVaultUrl string

// Exactly the two secrets Key Vault ships (keyvault.bicep's secretNames output):
// email sends via ACS with the managed identity, so there is no Gmail credential.
@description('Key Vault secret NAMES (not values), bundled: { anthropic, digestTo }.')
param secretNames object

@description('Replica timeout (seconds) for the screen job.')
param screenTimeoutSeconds int = 3600

@description('''Account equity in dollars for R-based sizing (SWING_ACCOUNT_EQUITY).
Empty = sizing unconfigured: digests and order intents render R-multiples, never a
guessed dollar (settings.resolve_risk_unit). Set to the real funded amount --
1R = equity x SWING_RISK_PCT, conviction-scaled per pick. A string
so "" can mean unset (bicep has no null string param).''')
param accountEquity string = ''

@description('''Risk per trade as a fraction of equity (SWING_RISK_PCT; settings
default 0.01 when absent). 1R = equity x this. Empty = absent (code default).''')
param riskPct string = ''

@description('''The execution adapter (SWING_EXECUTION_MODE): off | manual | paper |
live. Empty = absent -> code default "off" (fail-safe: the screener never arms by
accident; unknown values also coerce to off). "paper" runs the PaperAdapter --
simulated fills into the curated account="paper" intent book, no broker, fenced out
of research aggregates -- the North-Star Mid-term order-flow proving leg.''')
param executionMode string = ''

// --- Broker arming (the two locks that live in env; the third is the autonomy gate in
// the database). BOTH default '' = ABSENT, which is the intended prod state today: no
// broker configured, real money disallowed. That is the same
// template-default-must-match-prod lesson as deepAnalysisEnabled below, pointed the
// other way -- deep analysis is ON in prod so its default is '1'; arming is OFF in prod
// so these defaults are ''. A bicep redeploy must never be able to arm real money as a
// side effect, and it must never silently DISARM a deliberately armed job either: the
// day these are set in prod, they get set in main.bicepparam in the same act (the
// arming ceremony, docs/runbooks/arming-alpaca-live.md).
@description('''Broker adapter for the LIVE path (SWING_BROKER): "alpaca" is the only
impl. Empty = absent -> no broker configured, so even execution_mode=live falls back to
the NoOp adapter and submits nothing (fail-safe). This is lock #1 of three; setting it
alone arms NOTHING.''')
param broker string = ''

@description('''The explicit, LOUD real-money flag (SWING_BROKER_ALLOW_REAL_MONEY):
"1"/"true"/"yes"/"on" allows real-money orders; empty = absent -> false, and any
unrecognised value is also false. Lock #2 of three (mode==live AND this AND a ready
autonomy gate -- settings.can_arm_real_money). A real-money Alpaca host WITHOUT this is
still refused.''')
param allowRealMoney string = ''

// The execution SCOPE ceiling. Not a lock -- it does not gate arming -- but it belongs to
// the same ceremony because it decides WHICH strategy real money is put behind, and the
// default is the permissive one: absent = ALL play types dispatch, continuation included.
// The 2026-07-18 audit's finding is that continuation has no confirmed edge, so arming
// without setting this puts money behind a strategy the evidence does not support. It
// lives here (rather than only in the CLI ceremony) because path A of the arming runbook
// -- uncomment the params, re-provision -- must be able to express the scope at all;
// without this param a drift-safe arming is structurally allow-all.
@description('''The play types the agent may execute (SWING_EXECUTE_PLAY_TYPES), comma
separated: "reversal" | "continuation" | "reversal,continuation". Empty = absent ->
allow-ALL (today's behaviour, and the PERMISSIVE default -- set it deliberately in the
arming ceremony). Garbage parses fail-CLOSED to the empty set: nothing dispatches, with a
loud warning -- but a PARTLY valid list keeps its valid members (the unknown ones are
dropped with a warning); only an all-garbage list collapses to allow-none. Task 22's
cockpit subtraction sits BENEATH this ceiling and can only narrow it, never widen it.''')
param executePlayTypes string = ''

@description('''The three hard caps the adapter's submit() clamp enforces per
account-day (empty = that cap absent -> unbounded, the code's None sentinel).
maxDailyNotional is DOLLARS of recorded order notional; maxDailyLoss is an R
THRESHOLD (new orders blocked once the day's summed realized R <= -this);
maxConcurrent is open positions. Also the Safety screen's caps mandate: live
arming requires all three set.''')
param maxDailyNotional string = ''
param maxDailyLoss string = ''
param maxConcurrent string = ''

// 1800s (30 min) comfortably covers deep (Opus + web-search) analysis of the
// top-N picks (~1 min/pick) plus the email build.
@description('Replica timeout (seconds) for digest/alert jobs.')
param digestTimeoutSeconds int = 1800

// --- Deep-analysis (Opus web-search analyst) knobs. Default ON: the insight engine is
// the qualitative learning loop (analyst calls recorded + scored -> calibration -> the
// autonomy gate). The template default MUST match the intended prod state -- it was
// flipped on out-of-band in 2026-06 while this default stayed '0', so any bicep
// redeploy would have silently disarmed the analyst (2026-07-01 audit). Set '0' to
// force the deterministic narrator. All values are strings (Container Apps env vars).
@description('Master switch for deep analysis: "1"/"true" on, anything else off.')
param deepAnalysisEnabled string = '1'

@description('Model id for the analysis call.')
param analysisModel string = 'claude-opus-4-8'

// Extended thinking bills as OUTPUT tokens at the opus $25/MTok rate, making the
// thinking budget the digest's dominant output cost -- 'medium' halves that term vs
// 'high' (2026-07-17 cost plan). Template default MUST match the intended prod state,
// and 'medium' IS the intended prod state as of that plan.
@description('Reasoning effort -> extended-thinking budget: none/low/medium/high.')
param analysisReasoning string = 'medium'

@description('How many top picks per digest get the deep treatment.')
param deepAnalysisTopN string = '5'

@description('Which digest kinds get deep analysis (comma list).')
param deepAnalysisKinds string = 'daily,weekly,monthly'

@description('Max web searches per deep-analysis call (cost cap).')
param analysisMaxSearches string = '4'

// The per-RUN dollar ceiling pairs with the ON-by-default master switch above: once a
// digest run's accumulated deep-analysis spend reaches it, remaining picks fall back to
// the deterministic narrator. Deep-on with NO ceiling (the app treats missing/garbage
// as None = unbounded) must never be a template default.
@description('Per-run deep-analysis spend ceiling in USD (SWING_DEEP_ANALYSIS_MAX_USD).')
param deepAnalysisMaxUsd string = '2.50'

// The weekly Market Weather LLM read: ONE deep call per Sunday run. The code-level
// switch (StrategyConfig.market_report_enabled) is ON and market_run ANDs this env
// gate with it. ON by default -- the template default MUST match the intended prod
// state. Set '0' to force the deterministic facts read (no LLM bill); no per-run
// dollar ceiling needed because the job makes at most one call.
@description('Enable the weekly Market Weather LLM analysis (SWING_MARKET_REPORT).')
param marketReportEnabled string = '1'

// Journal v2 coaches (Personal Trade Coach + System Behavior Auditor). Enabled by
// default (turned on 2026-07-13), like deepAnalysisEnabled -- so a re-provision keeps
// them on rather than silently resetting to off. Safe because: the Coach only spends
// when Oliver closes a manual trade, the Auditor skips the LLM on a dead week ($0-gate),
// and each has a REAL USD ceiling below (never empty -> empty would mean unbounded).
// Set '' to disable. The code default (settings.py) stays off when the env is unset.
@description('Enable the Coach LLM (SWING_COACH_ENABLED; empty = off).')
param coachEnabled string = '1'

@description('Per-run Coach spend ceiling in USD (SWING_COACH_MAX_USD).')
param coachMaxUsd string = '1.00'

@description('Enable the Auditor LLM (SWING_AUDIT_ENABLED; empty = off).')
param auditEnabled string = '1'

@description('Per-run Auditor spend ceiling in USD (SWING_AUDIT_MAX_USD).')
param auditMaxUsd string = '1.00'

@description('Tags applied to the jobs.')
param tags object = {}

var imageRef = '${acrLoginServer}/${imageRepository}:${imageTag}'

// Unversioned KV reference URIs: ${vaultUri}secrets/<name>. keyVaultUrl already
// ends in '/', so no extra separator. Unversioned = auto-rotate to latest.
var anthropicKvUri = '${keyVaultUrl}secrets/${secretNames.anthropic}'
var digestToKvUri = '${keyVaultUrl}secrets/${secretNames.digestTo}'

// Container App secret definitions. Each resolves its value from Key Vault at
// runtime using the UAMI -- the template carries no secret material.
var secretDefs = [
  {
    name: 'anthropic-api-key'
    keyVaultUrl: anthropicKvUri
    identity: uamiId
  }
  {
    name: 'digest-to'
    keyVaultUrl: digestToKvUri
    identity: uamiId
  }
]

// Env shared by every job. Secret values arrive via secretRef; the rest plain.
// Sizing/execution env, present only when actually configured -- an empty value
// must stay ABSENT so settings reads its honest code default (unconfigured
// sizing -> R-multiples; absent mode -> fail-safe "off"), never a parse of ''.
var equityEnv = accountEquity == ''
  ? []
  : [
      {
        name: 'SWING_ACCOUNT_EQUITY'
        value: accountEquity
      }
    ]
var riskPctEnv = riskPct == ''
  ? []
  : [
      {
        name: 'SWING_RISK_PCT'
        value: riskPct
      }
    ]
var executionModeEnv = executionMode == ''
  ? []
  : [
      {
        name: 'SWING_EXECUTION_MODE'
        value: executionMode
      }
    ]
// Same absent-not-empty rule as executionMode: SWING_BROKER='' would still be "set" on
// the container, and settings reads it as an empty broker id -- behaviourally identical
// to absent, so this is hygiene rather than a behaviour change. Omitting the var keeps
// `az containerapp job show` an honest record of what is configured.
var brokerEnv = broker == ''
  ? []
  : [
      {
        name: 'SWING_BROKER'
        value: broker
      }
    ]
var allowRealMoneyEnv = allowRealMoney == ''
  ? []
  : [
      {
        name: 'SWING_BROKER_ALLOW_REAL_MONEY'
        value: allowRealMoney
      }
    ]
// Same absent-not-empty hygiene as broker/executionMode: settings treats a BLANK
// SWING_EXECUTE_PLAY_TYPES exactly like an absent one (both -> None = unscoped), so
// omitting the var is behaviour-neutral -- it just keeps `az containerapp job show` an
// honest record of what is actually configured.
var executePlayTypesEnv = executePlayTypes == ''
  ? []
  : [
      {
        name: 'SWING_EXECUTE_PLAY_TYPES'
        value: executePlayTypes
      }
    ]
var capsEnv = concat(
  maxDailyNotional == '' ? [] : [{ name: 'SWING_MAX_DAILY_NOTIONAL', value: maxDailyNotional }],
  maxDailyLoss == '' ? [] : [{ name: 'SWING_MAX_DAILY_LOSS', value: maxDailyLoss }],
  maxConcurrent == '' ? [] : [{ name: 'SWING_MAX_CONCURRENT', value: maxConcurrent }]
)

var commonEnv = concat(equityEnv, riskPctEnv, executionModeEnv, brokerEnv, allowRealMoneyEnv, executePlayTypesEnv, capsEnv, [
  {
    name: 'ANTHROPIC_API_KEY'
    secretRef: 'anthropic-api-key'
  }
  {
    name: 'DIGEST_TO'
    secretRef: 'digest-to'
  }
  // Email is sent via Azure Communication Services using the managed identity --
  // NO email password is stored anywhere.
  {
    name: 'SWING_ACS_ENDPOINT'
    value: acsEndpoint
  }
  {
    name: 'SWING_ACS_SENDER'
    value: acsSender
  }
  {
    name: 'SWING_DB_URL'
    value: swingDbUrl
  }
  {
    name: 'SWING_BLOB_ACCOUNT_URL'
    value: blobAccountUrl
  }
  {
    name: 'SWING_BLOB_CONTAINER'
    value: blobContainer
  }
  {
    name: 'SWING_CHART_DIR'
    // /tmp is world-writable; the non-root user can't create /data. Charts are
    // uploaded to Blob immediately, so this local staging dir is throwaway.
    value: '/tmp/charts'
  }
  {
    name: 'KEY_VAULT_URL'
    value: keyVaultUrl
  }
  {
    // So DefaultAzureCredential picks THIS user-assigned identity.
    name: 'AZURE_CLIENT_ID'
    value: uamiClientId
  }
  // Deep-analysis knobs (only the digest jobs act on them; default ON, bounded by the
  // per-run spend ceiling). They are harmless on the screen/exit jobs, which never
  // read them.
  {
    name: 'SWING_DEEP_ANALYSIS'
    value: deepAnalysisEnabled
  }
  {
    name: 'SWING_DEEP_ANALYSIS_MAX_USD'
    value: deepAnalysisMaxUsd
  }
  {
    name: 'SWING_ANALYSIS_MODEL'
    value: analysisModel
  }
  {
    name: 'SWING_ANALYSIS_REASONING'
    value: analysisReasoning
  }
  {
    name: 'SWING_DEEP_ANALYSIS_TOP_N'
    value: deepAnalysisTopN
  }
  {
    name: 'SWING_DEEP_ANALYSIS_KINDS'
    value: deepAnalysisKinds
  }
  {
    name: 'SWING_ANALYSIS_MAX_SEARCHES'
    value: analysisMaxSearches
  }
  // The market-weather job's LLM gate; harmless on every other job (never read).
  {
    name: 'SWING_MARKET_REPORT'
    value: marketReportEnabled
  }
  // Journal v2 coaches. On commonEnv so the coach/audit jobs read them; the other
  // jobs ignore them harmlessly (like the deep-analysis knobs on the screen jobs).
  {
    name: 'SWING_COACH_ENABLED'
    value: coachEnabled
  }
  {
    name: 'SWING_COACH_MAX_USD'
    value: coachMaxUsd
  }
  {
    name: 'SWING_AUDIT_ENABLED'
    value: auditEnabled
  }
  {
    name: 'SWING_AUDIT_MAX_USD'
    value: auditMaxUsd
  }
])

// Per-job spec: cron + container args + gate env + timeout. The jobs differ
// ONLY in these. UTC crons fire on both EST and EDT; the gate selects ET.
var jobSpecs = [
  {
    name: 'evening-screen'
    cron: '15 20,21 * * 1-5'
    args: [
      '-m'
      'swing_screener.pipeline.run'
    ]
    gateEnv: [
      {
        name: 'RUN_IF_ET_HOUR'
        value: '16'
      }
    ]
    timeout: screenTimeoutSeconds
  }
  {
    name: 'daily-digest'
    cron: '0 12,13 * * 1-5'
    args: [
      '-m'
      'swing_screener.notify.run'
      '--kind'
      'daily'
    ]
    gateEnv: [
      {
        name: 'RUN_IF_ET_HOUR'
        value: '8'
      }
    ]
    timeout: digestTimeoutSeconds
  }
  {
    // On-demand single-ticker deep-analysis worker. Drains the analysis_requests
    // queue -- unlike the digest/screen jobs it is NOT gated on an Eastern hour: an
    // empty gateEnv leaves RUN_IF_ET_HOUR unset, and the eastern_gate passes straight
    // through to exec the worker on every firing. Same image/UAMI/secrets/env as
    // daily-digest (it emails + calls Anthropic + reads/writes Azure SQL + uploads to
    // Blob); only the cron + entrypoint args differ. HOURLY, not */15: the queue has
    // never held a row (2026-07 audit) -- ~2,880 empty container starts/month bought
    // nothing; widen back if/when the feature gets a real user.
    name: 'on-demand-analysis'
    cron: '0 * * * *'
    args: [
      '-m'
      'swing_screener.notify.ondemand'
    ]
    gateEnv: []
    timeout: digestTimeoutSeconds
  }
  {
    name: 'intraday-exit'
    cron: '0 13-21 * * 1-5'
    args: [
      '-m'
      'swing_screener.notify.run'
      '--kind'
      'exit'
    ]
    gateEnv: [
      {
        name: 'RUN_IF_ET_HOUR'
        value: '9,10,11,12,13,14,15,16'
      }
    ]
    timeout: digestTimeoutSeconds
  }
  {
    name: 'weekly-digest'
    cron: '30 20,21 * * 5'
    args: [
      '-m'
      'swing_screener.notify.run'
      '--kind'
      'weekly'
    ]
    gateEnv: [
      {
        name: 'RUN_IF_ET_HOUR'
        value: '16'
      }
    ]
    timeout: digestTimeoutSeconds
  }
  {
    name: 'monthly-digest'
    cron: '30 20,21 * * 1-5'
    args: [
      '-m'
      'swing_screener.notify.run'
      '--kind'
      'monthly'
    ]
    gateEnv: [
      {
        name: 'RUN_IF_ET_HOUR'
        value: '16'
      }
      {
        name: 'RUN_IF_LAST_BUSINESS_DAY'
        value: '1'
      }
    ]
    timeout: digestTimeoutSeconds
  }
  {
    // Weekly macro "Market Weather" report -- a market-broad regime/risk read (SPY MTF Heiken-Ashi
    // + VIX term structure, HY credit, rotation, breadth, recession odds), NOT a stock pick. Runs
    // Sunday ~09:00 ET off the completed weekly candle (a calm weekend macro review). The Opus
    // analyst is ON by default (StrategyConfig.market_report_enabled AND the SWING_MARKET_REPORT
    // env gate above); the recipient comes from DIGEST_TO and email is sent via ACS, same as the
    // digests. The ET gate makes the Sunday UTC cron pair fire exactly once, so no DST double-send.
    name: 'market-weather'
    cron: '0 13,14 * * 0'
    args: [
      '-m'
      'swing_screener.notify.market_run'
    ]
    gateEnv: [
      {
        name: 'RUN_IF_ET_HOUR'
        value: '9'
      }
    ]
    timeout: digestTimeoutSeconds
  }
  {
    // Journal v2 -- Personal Trade Coach worker. UN-gated (empty gateEnv) so it runs
    // every firing: it drains the on-close draft queue (backfilling review narratives)
    // AND refreshes the weekly Weaknesses Profile. Default-OFF coach still drains the
    // queue to deterministic-template narratives; the LLM path needs SWING_COACH_ENABLED.
    name: 'journal-coach'
    cron: '0 * * * *'
    args: [
      '-m'
      'swing_screener.journal.coach_run'
    ]
    gateEnv: []
    timeout: digestTimeoutSeconds
  }
  {
    // Journal v2 -- System Behavior Auditor weekly sweep. ET-gated to 16:00 Saturday
    // (the UTC pair fires once, no DST double-run), mirroring the reflection cadence.
    name: 'journal-audit-weekly'
    cron: '30 20,21 * * 6'
    args: [
      '-m'
      'swing_screener.journal.audit_run'
      'weekly'
    ]
    gateEnv: [
      {
        name: 'RUN_IF_ET_HOUR'
        value: '16'
      }
    ]
    timeout: digestTimeoutSeconds
  }
  {
    // Journal v2 -- Auditor daily breach scan (caps exceeded, disarms). ET-gated to
    // 16:00 on weekdays -- "flags on the next run after the breach" (design cadence).
    name: 'journal-audit-breach'
    cron: '30 20,21 * * 1-5'
    args: [
      '-m'
      'swing_screener.journal.audit_run'
      'breach'
    ]
    gateEnv: [
      {
        name: 'RUN_IF_ET_HOUR'
        value: '16'
      }
    ]
    timeout: digestTimeoutSeconds
  }
]

resource jobs 'Microsoft.App/jobs@2026-01-01' = [
  for spec in jobSpecs: {
    name: spec.name
    location: location
    tags: tags
    identity: {
      type: 'UserAssigned'
      userAssignedIdentities: {
        '${uamiId}': {}
      }
    }
    properties: {
      environmentId: environmentId
      configuration: {
        triggerType: 'Schedule'
        replicaRetryLimit: 1
        replicaTimeout: spec.timeout
        scheduleTriggerConfig: {
          cronExpression: spec.cron
          parallelism: 1
          replicaCompletionCount: 1
        }
        registries: [
          {
            server: acrLoginServer
            identity: uamiId
          }
        ]
        secrets: secretDefs
      }
      template: {
        containers: [
          {
            name: spec.name
            image: imageRef
            // ENTRYPOINT (the gate) is kept; args are the CLI the gate execs.
            args: spec.args
            // Common app env + this job's gate env.
            env: concat(commonEnv, spec.gateEnv)
            resources: {
              cpu: json('0.5')
              memory: '1Gi'
            }
          }
        ]
      }
    }
  }
]

@description('Names of the scheduled jobs.')
output jobNames array = [for (spec, i) in jobSpecs: jobs[i].name]
