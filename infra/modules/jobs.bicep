// =============================================================================
// jobs.bicep
// FIVE scheduled Container Apps Jobs, all sharing ONE image and ONE UAMI.
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

@description('Image tag to run.')
param imageTag string = 'bootstrap'

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

@description('Key Vault secret NAMES (not values), bundled: { anthropic, gmailAddress, gmailPassword, digestTo }.')
param secretNames object

@description('Replica timeout (seconds) for the screen job.')
param screenTimeoutSeconds int = 3600

// 1800s (30 min) comfortably covers deep (Opus + web-search) analysis of the
// top-N picks (~1 min/pick) plus the email build.
@description('Replica timeout (seconds) for digest/alert jobs.')
param digestTimeoutSeconds int = 1800

// --- Deep-analysis (Opus web-search analyst) knobs. Default OFF / cheap; flip
// SWING_DEEP_ANALYSIS to "1" (and ensure web search is enabled in the Claude
// Console) to turn it on. All values are strings (Container Apps env vars).
@description('Master switch for deep analysis: "1"/"true" on, anything else off.')
param deepAnalysisEnabled string = '0'

@description('Model id for the analysis call.')
param analysisModel string = 'claude-opus-4-8'

@description('Reasoning effort -> extended-thinking budget: none/low/medium/high.')
param analysisReasoning string = 'high'

@description('How many top picks per digest get the deep treatment.')
param deepAnalysisTopN string = '5'

@description('Which digest kinds get deep analysis (comma list).')
param deepAnalysisKinds string = 'daily,weekly,monthly'

@description('Max web searches per deep-analysis call (cost cap).')
param analysisMaxSearches string = '4'

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
var commonEnv = [
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
  // Deep-analysis knobs (only the digest jobs act on them; default OFF). They are
  // harmless on the screen/exit jobs, which never read them.
  {
    name: 'SWING_DEEP_ANALYSIS'
    value: deepAnalysisEnabled
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
]

// Per-job spec: cron + container args + gate env + timeout. The five jobs
// differ ONLY in these. UTC crons fire on both EST and EDT; the gate selects ET.
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

@description('Names of the five scheduled jobs.')
output jobNames array = [for (spec, i) in jobSpecs: jobs[i].name]
