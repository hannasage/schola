/**
 * The model adapter error (step E3). The code is `model_adapter.py`; `docs/model-adapters.md` explains it.
 * Only the error payload has a type here. It is the exact shape of `ModelAdapterError.payload()`, with the same snake_case names.
 * The request and result shapes get types when a route sends them.
 */

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

/**
 * What `ModelAdapterError.payload()` gives. `retryable` is true when the same call may work later.
 * `tokens_charged` is what the session's token budget counted for the failed call.
 */
interface ModelAdapterErrorPayload {
  error: string;
  code: ModelAdapterErrorCode;
  retryable: boolean;
  tokens_charged: number;
}
