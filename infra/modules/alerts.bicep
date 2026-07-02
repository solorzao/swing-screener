// =============================================================================
// alerts.bicep
// Minimal ops alerting. The 2026-07-01 audit found ZERO alert rules / action
// groups anywhere in infra/ -- with replicaRetryLimit:1 a job fails twice and
// then gives up silently, so a dead pipeline was only discovered by noticing a
// MISSING email. These two scheduled-query rules turn that into a push:
//
//   1. job-failed      -- any Container Apps Job execution failed.
//   2. screen-missing  -- the evening screen did not log its completion marker
//                         (SCREEN_RUN_COMPLETE, src/swing_screener/pipeline/run.py)
//                         on a weekday.
//
// Both query the EXISTING Log Analytics workspace: the managed environment ships
// logs with destination 'log-analytics' (env.bicep), so telemetry lands in the
// CUSTOM tables ContainerAppSystemLogs_CL / ContainerAppConsoleLogs_CL (NOT the
// dedicated ContainerAppSystemLogs/ContainerAppConsoleLogs tables, which only
// exist with the azure-monitor destination).
//
// COST: <=5 query evaluations/hour against the 1 GB/day-capped workspace --
// noise-level. Keep evaluationFrequency at PT15M or slower.
//
// BOOTSTRAP CAVEAT: on a FRESH workspace the _CL tables do not exist until the
// first job execution logs something, so both rules would fail deploy-time query
// validation -- skipQueryValidation:true keeps provisioning order-independent.
// Until the tables exist the rules evaluate with errors (visible on the rule
// blade) and cannot fire; the runbook's mandatory manual first screen (Step 4)
// creates the tables.
//
// apiVersions verified against Bicep types: Microsoft.Insights/actionGroups ->
// 2023-01-01 (stable); Microsoft.Insights/scheduledQueryRules -> 2021-08-01
// (stable).
// =============================================================================

@description('Azure region for the scheduled-query rules (must be the Log Analytics workspace region). The action group itself is global.')
param location string

@description('Short env/name prefix used to build resource names (matches main.bicep).')
param namePrefix string

@description('Deterministic unique suffix from main.bicep.')
param resourceToken string

@description('Resource ID of the existing Log Analytics workspace the jobs log to.')
param workspaceId string

@description('Email address that receives ops alerts. Deliberately NOT @secure(): it is stored readable in the action group resource by design -- an ops routing address, not a credential (digestTo stays secret because it is seeded into Key Vault).')
param alertEmail string

@description('Tags applied to the action group and alert rules.')
param tags object = {}

// -----------------------------------------------------------------------------
// KQL 1: failed job execution.
//
// ASSUMPTION (verify against the live workspace after the first real failure):
// job failure telemetry appears in ContainerAppSystemLogs_CL with a Reason_s /
// Type_s field and/or a "... terminated with exit code 'N'" log line. The exact
// column set and Reason vocabulary of the _CL table are not contractually
// documented, so this query is deliberately BROAD (false positives over false
// negatives for an ops alert) and hedges column names with column_ifexists():
//   - Reason_s values seen for failed job executions/replicas
//   - failure phrasing in Log_s (backoff exhausted, image pull failures)
//   - a non-zero exit code line
// No job-name filter: this environment runs ONLY jobs (no container apps), so
// every failure row in the table is ours. The eastern_gate exits 0 on a
// non-matching cron firing, so gated skips can never trip this.
// -----------------------------------------------------------------------------
var jobFailedQuery = '''
ContainerAppSystemLogs_CL
| extend JobName = strcat(column_ifexists('JobName_s', ''), column_ifexists('ContainerJobName_s', ''))
| extend Reason = tostring(column_ifexists('Reason_s', ''))
| where Reason in~ ('BackoffLimitExceeded', 'JobFailed', 'ScheduledJobFailed')
    or Log_s has_any ('job execution failed', 'backoff limit exceeded', 'ErrImagePull', 'ImagePullBackOff')
    or Log_s matches regex @"terminated with exit code:? '?[1-9]"
| project TimeGenerated, JobName, Reason, Log_s
'''

// -----------------------------------------------------------------------------
// KQL 2: ABSENCE of evening-screen success.
//
// run_screen ends with a stable marker line containing the literal token
// SCREEN_RUN_COMPLETE (see pipeline/run.py -- reword only together). The token
// is unique to the evening screen, so no job-name column filter is needed
// (sidestepping the _CL column-name uncertainty entirely).
//
// WEEKEND HANDLING (the "lookback that tolerates weekends" option): instead of
// a 26-32h sliding window -- which would false-alarm every Saturday evening,
// ~26h after Friday's healthy 16:15 ET run -- the check is anchored to ET
// midnight and armed only 19:00-23:59 ET Mon-Fri: the job completes by ~18:15 ET
// worst case (16:15 ET start + 1h timeout + one retry), so by 19:00 ET the
// marker MUST exist "today (ET)". Tradeoff: a missed weekday screen alerts the
// SAME evening (first hourly evaluation after 19:00 ET) and weekends never
// evaluate, but a job that somehow ran and only THEN died after logging the
// marker still counts as success -- acceptable, the marker is the last
// statement of the run. DST is handled by the IANA tz conversion, matching the
// eastern_gate's clock.
//
// Shape: the query RETURNS A ROW when the marker is missing inside the armed
// window (alert fires on Count > 0), which is what lets weekends/pre-deadline
// hours evaluate to "no rows = healthy" instead of tripping a LessThan rule.
// -----------------------------------------------------------------------------
var screenMissingQuery = '''
let et_now = datetime_utc_to_local(now(), 'America/New_York');
let in_check_window = dayofweek(et_now) between (1d .. 5d) and hourofday(et_now) >= 19;
let et_midnight_utc = datetime_local_to_utc(startofday(et_now), 'America/New_York');
let markers_today = toscalar(
    ContainerAppConsoleLogs_CL
    | where TimeGenerated >= et_midnight_utc
    | where Log_s contains 'SCREEN_RUN_COMPLETE'
    | count);
range i from 1 to 1 step 1
| where in_check_window and markers_today == 0
| project TimeGenerated = now(), Detail = 'evening-screen logged no SCREEN_RUN_COMPLETE today (ET)'
'''

// --- Action group: plain email to the operator ---
resource actionGroup 'Microsoft.Insights/actionGroups@2023-01-01' = {
  name: '${namePrefix}-ag-ops-${resourceToken}'
  location: 'global' // action groups are a global service
  tags: tags
  properties: {
    // 12-char limit; shows up in SMS/email headers.
    groupShortName: take('${namePrefix}ops', 12)
    enabled: true
    emailReceivers: [
      {
        name: 'ops-email'
        emailAddress: alertEmail
        useCommonAlertSchema: true
      }
    ]
  }
}

// --- Alert 1: any job execution failed ---
resource jobFailedAlert 'Microsoft.Insights/scheduledQueryRules@2021-08-01' = {
  name: '${namePrefix}-alert-job-failed-${resourceToken}'
  location: location
  tags: tags
  kind: 'LogAlert'
  properties: {
    displayName: 'swing-screener: a Container Apps Job execution failed'
    description: 'A scheduled job (evening-screen, digests, intraday-exit, on-demand-analysis, market-weather) logged failure telemetry in ContainerAppSystemLogs_CL. With replicaRetryLimit:1 the platform will NOT retry further -- investigate the execution logs.'
    severity: 1
    enabled: true
    evaluationFrequency: 'PT15M'
    // 30m window on a 15m cadence: each event is seen by 2 evaluations, so
    // ingestion latency of up to ~15m cannot slip an event between windows.
    windowSize: 'PT30M'
    scopes: [
      workspaceId
    ]
    criteria: {
      allOf: [
        {
          query: jobFailedQuery
          timeAggregation: 'Count'
          operator: 'GreaterThan'
          threshold: 0
          failingPeriods: {
            numberOfEvaluationPeriods: 1
            minFailingPeriodsToAlert: 1
          }
        }
      ]
    }
    // Stateful: one fired notification while the event is in-window, then
    // auto-resolve -- no per-evaluation re-fire spam.
    autoMitigate: true
    skipQueryValidation: true // _CL tables don't exist until the first job logs
    actions: {
      actionGroups: [
        actionGroup.id
      ]
    }
  }
}

// --- Alert 2: evening screen produced no completion marker (weekday) ---
resource screenMissingAlert 'Microsoft.Insights/scheduledQueryRules@2021-08-01' = {
  name: '${namePrefix}-alert-screen-missing-${resourceToken}'
  location: location
  tags: tags
  kind: 'LogAlert'
  properties: {
    displayName: 'swing-screener: evening screen missing (no SCREEN_RUN_COMPLETE today ET)'
    description: 'The evening screen did not log its SCREEN_RUN_COMPLETE marker by 19:00 ET on a weekday -- the nightly pipeline (signals, shadow book, charts, next morning digest) did not complete. A silent zero-signal screen is the existential failure mode; check the evening-screen job executions and yfinance egress.'
    severity: 1
    enabled: true
    evaluationFrequency: 'PT1H'
    // The query anchors its own lookback to ET midnight (<=24h); P2D comfortably
    // contains that regardless of the evaluation's UTC offset.
    windowSize: 'P2D'
    scopes: [
      workspaceId
    ]
    criteria: {
      allOf: [
        {
          query: screenMissingQuery
          timeAggregation: 'Count'
          operator: 'GreaterThan'
          threshold: 0
          failingPeriods: {
            numberOfEvaluationPeriods: 1
            minFailingPeriodsToAlert: 1
          }
        }
      ]
    }
    // Stateful: fires once per missed day (~19:xx ET), auto-resolves after the
    // armed window ends (~00:xx ET) or on the next successful run.
    autoMitigate: true
    skipQueryValidation: true // _CL tables don't exist until the first job logs
    actions: {
      actionGroups: [
        actionGroup.id
      ]
    }
  }
}

@description('Action group resource ID (for any future alert rules).')
output actionGroupId string = actionGroup.id
