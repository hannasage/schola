/** The local event log and reflections. The server writes and checks them; the page only sends student events and shows the report. */
type LearningEventSource = 'student' | 'app' | 'agent';

/** One event as the contract defines it (contracts/learning-events.md). `data` depends on `type`. */
interface LearningEventRecord {
  schemaVersion: string;
  eventId: string;
  time: string;
  sessionId: string;
  moduleId: string;
  moduleVersion: string;
  taskId: string | null;
  source: LearningEventSource;
  skills: string[];
  type: string;
  data: Record<string, unknown>;
}

/** The body of POST /api/events. The page may send `source: 'student'` only. */
interface EventAppendRequest {
  event: LearningEventRecord & { source: 'student' };
}

interface EventAppendResult {
  appended: true;
  eventId: string;
  /** The line number of the new event. */
  line: number;
  bytes: number;
  /** True when the log ended in a cut line and the server first added a newline. */
  newlineAdded: boolean;
}

/** One entry of the per-line report. A valid line has no reason. */
interface EventLogLine {
  line: number;
  ok: boolean;
  reason?: 'invalid' | 'unsupported_version' | 'too_large' | 'truncated_tail';
  errors?: Array<{ pointer: string; message: string }>;
}

/**
 * The answer of GET /api/events. A missing log is `exists: false` with empty lists.
 * `lines` and `events` hold one window: `limit` lines after `offset` lines. `events` holds the valid lines of the window only.
 * `ok` and `truncatedTail` describe the whole log, so `ok: false` means some line, in or out of the window, is damaged.
 */
interface EventLogReport {
  ok: boolean;
  exists: boolean;
  lines: EventLogLine[];
  events: Array<{ line: number; event: LearningEventRecord }>;
  truncatedTail: boolean;
  /** The number of lines in the whole log. */
  total: number;
  offset: number;
  limit: number | null;
  /** True when lines follow the window. */
  more: boolean;
}

interface EventDeleteRequest {
  confirm: true;
}

/** The answer of DELETE /api/events. */
interface EventDeleteResult {
  deleted: true;
  logRemoved: boolean;
  reflectionsRemoved: number;
}

interface EventReflectionSaved {
  reflectionId: string;
  charCount: number;
}

interface EventReflection extends EventReflectionSaved {
  text: string;
}

type EventErrorCode =
  | 'invalid_request' | 'invalid_json' | 'payload_too_large' | 'unsupported_media_type' | 'request_timeout'
  | 'invalid_token' | 'forbidden_origin' | 'source_forbidden' | 'type_forbidden' | 'not_found' | 'storage_unavailable'
  | 'invalid_event' | 'unsupported_version' | 'event_too_large' | 'duplicate_event_id' | 'log_full' | 'log_reserved'
  | 'lock_unavailable' | 'write_failed' | 'fsync_failed' | 'delete_failed'
  | 'log_missing' | 'log_unreadable' | 'log_corrupt' | 'unsafe_path' | 'confirmation_required'
  | 'invalid_reflection' | 'invalid_reflection_id' | 'reflection_not_found' | 'reflection_unreadable' | 'reflection_corrupt'
  | 'reflections_full' | 'reflection_mismatch'
  | 'internal_error';

/** The body of every error answer. `details` lists validator findings for `invalid_event`, or write facts such as `partialLine` and `maybeWritten`. */
interface EventErrorBody {
  error: string;
  code: EventErrorCode;
  retryable: boolean;
  field?: string;
  details?: Array<{ pointer: string; message: string }>
    | { partialLine?: boolean; maybeWritten?: boolean; removed?: boolean; logRemoved?: boolean; reflectionsRemoved?: number; reflectionId?: string; charCount?: number };
}
