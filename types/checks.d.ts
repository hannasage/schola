/** Local check runs. The local server decides what a check does. The browser shows it and asks the student to agree. */
type CheckStatus = 'passed' | 'failed' | 'error' | 'skipped';
type CheckErrorKind = 'invalid_token' | 'consent_required' | 'busy' | 'workspace_required' | 'invalid_workspace' | 'hash_mismatch' | 'not_found'
  | 'too_large' | 'confirmation_required' | 'consent_not_needed' | 'forbidden_origin' | 'network_error' | 'invalid_response' | 'request_failed';

interface CheckRef {
  moduleId: string;
  version: string;
  checkId: string;
}

/**
 * What a run needs. `taskId` names the task the check belongs to. The server needs it to write the check events
 * when the module has more than one task. A module with one task does not need it.
 */
interface CheckRunRef extends CheckRef {
  taskId?: string;
}

/** What would run, as the server describes it. Every field is untrusted text or numbers. */
interface CheckInspection {
  needsConsent: boolean;
  checkHash: string;
  runnerType: string | null;
  description: string | null;
  runtime: string | null;
  source: string | null;
  args: string[];
  timeoutSeconds: number;
  workspace: string;
  /** What Schola sets for a script run. Empty for a check that runs no script. */
  limits?: string[];
  /** What Schola does not limit, such as memory, the network, and the files the student can read. */
  limitsNotApplied?: string[];
}

interface CheckConsentGrant {
  consentId: string;
  moduleId: string;
  version: string;
  checkId: string;
  checkHash: string;
  workspace: string;
}

interface CheckWorkspace {
  path: string | null;
  configured: boolean;
  usable: boolean;
  problem?: string;
}

interface CheckResult {
  status: CheckStatus;
  evidence: Record<string, unknown>;
  log: string[];
}

/**
 * Whether the server wrote the events of a run. `true`: both are in the log. `false`: the store says nothing was written.
 * `'partial'`: the attempt is in the log and the result is not, or a write may have reached the log (`log_uncertain`).
 */
type CheckEventsWritten = boolean | 'partial';

/** The stable codes of `eventsError`. */
type CheckEventsError =
  | 'not_run' | 'task_required' | 'task_unknown' | 'log_full' | 'log_reserved' | 'log_uncertain' | 'log_damaged' | 'log_unavailable'
  | 'duplicate_event_id' | 'clock_unavailable' | 'artifact_too_large' | 'artifact_unavailable' | 'event_invalid' | 'events_failed';

interface CheckRunResponse {
  moduleId: string;
  version: string;
  checkId: string;
  checkHash: string;
  tier: 1 | 2;
  result: CheckResult;
  /** A fault in the event log never changes `result`. This field says what happened to the events. */
  eventsWritten: CheckEventsWritten;
  /** A stable code. Missing when `eventsWritten` is `true`. For `log_uncertain`, read the event log before a retry. */
  eventsError?: CheckEventsError;
}

interface CheckService {
  getWorkspace(): Promise<CheckWorkspace>;
  setWorkspace(path: string): Promise<CheckWorkspace>;
  inspect(ref: CheckRef): Promise<CheckInspection>;
  grantConsent(ref: CheckRef, options: { confirm: boolean; shownHash: string }): Promise<CheckConsentGrant>;
  run(ref: CheckRunRef, consentId?: string): Promise<CheckRunResponse>;
}

interface CheckConsentOutcome {
  consented: boolean;
  consentId?: string;
}
