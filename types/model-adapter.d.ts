/** The model adapter interface (step E3). The code is `model_adapter.py` and `api_adapter.py`; `docs/model-adapters.md` explains it. */

/** The closed set of error codes. A message never holds a key, a URL, or text from a provider. */
type ModelAdapterErrorCode =
  | 'auth_failed'
  | 'rate_limited'
  | 'timeout'
  | 'endpoint_unreachable'
  | 'response_too_large'
  | 'bad_response'
  | 'token_cap_reached'
  | 'unsafe_endpoint'
  | 'invalid_request';

/** What `ModelAdapterError.payload()` gives. `retryable` is true when the same call may work later. */
interface ModelAdapterErrorPayload {
  error: string;
  code: ModelAdapterErrorCode;
  retryable: boolean;
}

type ModelMessageRole = 'system' | 'user' | 'assistant';

interface ModelMessage {
  role: ModelMessageRole;
  text: string;
}

/** The input of `complete`. The token budget is a separate object that the engine owns for the whole session. */
interface ModelCompletionRequest {
  /** 1 to 100 messages, at most 100000 characters each and 400000 together. */
  messages: ModelMessage[];
  /** A whole number from 1 to 200000. */
  maxOutputTokens: number;
}

interface ModelUsage {
  inputTokens: number;
  outputTokens: number;
}

/** `end`: the model finished. `length`: it hit the output limit. `content_filter`: the provider cut it. `other`: anything else. */
type ModelStopReason = 'end' | 'length' | 'content_filter' | 'other';

/**
 * The output of `complete`. `text` is untrusted model output.
 * When the provider sent no usable usage, `usage` is null, `usageReported` is false, `tokensCharged` is `maxOutputTokens`,
 * and `notice` says so.
 */
interface ModelCompletionResult {
  text: string;
  usage: ModelUsage | null;
  stopReason: ModelStopReason;
  usageReported: boolean;
  tokensCharged: number;
  notice: string | null;
}

/** The per-session cap on tokens. The cap is required. Schola has no default yet (an open decision). */
interface ModelTokenBudget {
  readonly cap: number;
  readonly spent: number;
  readonly remaining: number;
  readonly exhausted: boolean;
}
