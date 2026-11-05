"""The model adapter interface: one call, typed errors, a token budget, and a key wrapper.

An adapter connects the engine to one model source. Every adapter has one public call:

    adapter.complete(request) -> CompletionResult

`request` is a `CompletionRequest`: messages (a role and a text each), `max_output_tokens`,
and the session `TokenBudget`. The result holds the text, the token usage, and the stop
reason. `docs/model-adapters.md` explains the terms and the limits.

Rules
-----
* `complete` is the same for every adapter. A subclass writes only `_send`, which does the
  network work and returns a `RawCompletion`. This module holds the rules that must not differ
  between adapters: the request checks, the context guard, the token budget, the handling of a
  missing usage field, and the removal of the key from the reply.
* The token budget is checked before any network work. A spent budget gives `token_cap_reached`
  and nothing is sent.
* A reply with no usage numbers is never counted as free. The request's `max_output_tokens`
  is counted as spent, and the result says so (`usage_reported` and `notice`).
* An error carries a stable `code` and a `retryable` flag. A message is a fixed sentence. It
  never holds a key, a URL, or text from a provider.
* The API key lives in an `ApiKey`. Its text form is always `[redacted]`. This module does not
  store a key anywhere.

Only the Python standard library is used.
"""
import dataclasses
import ntpath
import os
import posixpath
import re
import threading

ERROR_CODES = ('auth_failed', 'rate_limited', 'timeout', 'endpoint_unreachable', 'response_too_large',
               'bad_response', 'token_cap_reached', 'unsafe_endpoint', 'invalid_request')
# A call that may work if tried again later. The others need a change first.
_RETRYABLE_CODES = ('rate_limited', 'timeout', 'endpoint_unreachable')

ROLES = ('system', 'user', 'assistant')
STOP_REASONS = ('end', 'length', 'content_filter', 'other')

# Sanity limits of the interface. They are not policy: they keep one request and one count of tokens bounded.
MAX_MESSAGES = 100
MAX_MESSAGE_CHARS = 100_000
MAX_TOTAL_CHARS = 400_000
MAX_OUTPUT_TOKENS = 200_000
MAX_TOKEN_COUNT = 10 ** 12
MAX_KEY_CHARS = 1024

REDACTED = '[redacted]'
_KEY_CHARACTERS = re.compile(r'[\x21-\x7e]+', re.ASCII)


class ModelAdapterError(Exception):
    """A model adapter problem with a stable code. The message is a fixed sentence and holds no secret."""

    def __init__(self, message, code, retryable=None):
        if code not in ERROR_CODES:
            raise ValueError('Unknown model adapter error code.')
        super().__init__(message)
        self.code = code
        self.retryable = code in _RETRYABLE_CODES if retryable is None else bool(retryable)

    def payload(self):
        return {'error': str(self), 'code': self.code, 'retryable': self.retryable}

    def __reduce__(self):
        return (self.__class__, (str(self), self.code, self.retryable))


def _invalid(message):
    return ModelAdapterError(message, 'invalid_request')


# ----------------------------------------------------------------------
# The API key
# ----------------------------------------------------------------------

class ApiKey:
    """An API key that cannot be printed, formatted, pickled, or written as JSON by accident.

    `str`, `repr`, `format`, and `%` all give `[redacted]`. `pickle` and `json.dumps` refuse.
    The value comes out of one method only, `reveal_for_transport`, which the transport calls
    when it builds a request header. The key is kept in memory and nowhere else.
    Python cannot hide an object from code that runs in the same process. This class stops accidents, not an attacker inside the process.
    """
    __slots__ = ('_ApiKey__value',)

    def __init__(self, value):
        if not isinstance(value, str) or len(value) > MAX_KEY_CHARS or not _KEY_CHARACTERS.fullmatch(value):
            # The message must not echo the value, so nothing from `value` goes in.
            raise _invalid('An API key must be 1 to 1024 visible ASCII characters with no spaces.')
        object.__setattr__(self, '_ApiKey__value', value)

    def reveal_for_transport(self):
        """The key text. Only the code that sends the request calls this."""
        return self.__value

    def __str__(self):
        return REDACTED

    __repr__ = __str__

    def __format__(self, spec):
        return format(REDACTED, spec)

    def __reduce__(self):
        raise TypeError('An API key cannot be pickled.')

    def __reduce_ex__(self, protocol):
        raise TypeError('An API key cannot be pickled.')

    def __copy__(self):
        return self

    def __deepcopy__(self, memo):
        return self

    def __eq__(self, other):
        if not isinstance(other, ApiKey):
            return NotImplemented
        return self.__value == other.__value

    def __setattr__(self, name, value):
        raise AttributeError('An API key cannot be changed.')

    def __delattr__(self, name):
        raise AttributeError('An API key cannot be changed.')


# ----------------------------------------------------------------------
# The token budget
# ----------------------------------------------------------------------

class TokenBudget:
    """A cap on the tokens one session may spend. The cap is required. Schola has no default (an open decision).

    A call spends the tokens its response reports. Before a call sends anything, the budget must have room:
    spent tokens plus the tokens that calls in flight may still use must be below the cap. A call can
    overshoot the cap by what it uses, because its input size is not known before it runs.
    The next call is then refused with `token_cap_reached`.
    """

    def __init__(self, cap):
        if isinstance(cap, bool) or not isinstance(cap, int) or cap < 1:
            raise _invalid('The token cap must be a whole number of 1 or more.')
        self._cap = cap
        self._spent = 0
        self._reserved = 0
        self._lock = threading.Lock()

    @property
    def cap(self):
        return self._cap

    # The reads below take no lock. Reading one whole number is a single step, and a reader never needs two numbers that agree.

    @property
    def spent(self):
        return self._spent

    @property
    def remaining(self):
        return max(0, self._cap - self._spent)

    @property
    def exhausted(self):
        return self._spent >= self._cap

    def __repr__(self):
        return f'TokenBudget(cap={self._cap}, spent={self.spent})'

    def _begin(self, reserve):
        """Hold `reserve` tokens for one call. Refuse when the budget is already spent or fully held."""
        with self._lock:
            if self._spent + self._reserved >= self._cap:
                raise ModelAdapterError('The token cap for this session is reached. Schola sent nothing.', 'token_cap_reached')
            self._reserved += reserve

    def _abort(self, reserve):
        with self._lock:
            self._reserved -= reserve

    def _finish(self, reserve, charge):
        with self._lock:
            self._reserved -= reserve
            self._spent += charge


# ----------------------------------------------------------------------
# Requests and results
# ----------------------------------------------------------------------

def _encodable(text):
    try:
        text.encode('utf-8')
    except UnicodeError:
        return False
    return True


@dataclasses.dataclass(frozen=True)
class Message:
    role: str
    text: str

    def __post_init__(self):
        if self.role not in ROLES:
            raise _invalid('A message role must be system, user, or assistant.')
        if not isinstance(self.text, str) or not self.text:
            raise _invalid('A message needs text.')
        if len(self.text) > MAX_MESSAGE_CHARS:
            raise _invalid(f'A message may hold at most {MAX_MESSAGE_CHARS} characters.')
        if not _encodable(self.text):
            raise _invalid('A message must be valid Unicode text.')


@dataclasses.dataclass(frozen=True)
class Usage:
    input_tokens: int
    output_tokens: int

    def __post_init__(self):
        for value in (self.input_tokens, self.output_tokens):
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= MAX_TOKEN_COUNT:
                raise ModelAdapterError('The model reported token usage that is not a count.', 'bad_response')

    @property
    def total(self):
        return self.input_tokens + self.output_tokens


@dataclasses.dataclass(frozen=True)
class CompletionRequest:
    messages: tuple
    max_output_tokens: int
    budget: TokenBudget

    def __post_init__(self):
        messages = self.messages
        if not isinstance(messages, (list, tuple)) or not 1 <= len(messages) <= MAX_MESSAGES:
            raise _invalid(f'Send 1 to {MAX_MESSAGES} messages.')
        if not all(isinstance(message, Message) for message in messages):
            raise _invalid('Every message must be a Message.')
        if sum(len(message.text) for message in messages) > MAX_TOTAL_CHARS:
            raise _invalid(f'The messages may hold at most {MAX_TOTAL_CHARS} characters together.')
        tokens = self.max_output_tokens
        if isinstance(tokens, bool) or not isinstance(tokens, int) or not 1 <= tokens <= MAX_OUTPUT_TOKENS:
            raise _invalid(f'max_output_tokens must be a whole number from 1 to {MAX_OUTPUT_TOKENS}.')
        if not isinstance(self.budget, TokenBudget):
            raise _invalid('A request needs a TokenBudget.')
        object.__setattr__(self, 'messages', tuple(messages))


@dataclasses.dataclass(frozen=True)
class RawCompletion:
    """What a transport returns: the reply text, the usage when the provider gave it (else None), and the stop reason."""
    text: str
    usage: object
    stop_reason: str


@dataclasses.dataclass(frozen=True)
class CompletionResult:
    """The text is untrusted model output. `usage` is None when the provider reported none.

    `tokens_charged` is what the budget counted. When `usage_reported` is false it is `max_output_tokens`, and `notice` says so.
    """
    text: str
    usage: object
    stop_reason: str
    usage_reported: bool
    tokens_charged: int
    notice: object = None


# ----------------------------------------------------------------------
# The context guard
# ----------------------------------------------------------------------

# Shapes of secrets that are common and easy to spot. This is a short list of patterns, not a scanner. It misses what it
# does not know, and it can refuse text that only looks like a key.
_B = r'(?<![A-Za-z0-9])'
_KEY_PATTERNS = (
    _B + r'sk-[A-Za-z0-9_-]{16,}',
    _B + r'[sr]k_(?:live|test)_[A-Za-z0-9]{16,}',
    _B + r'(?:AKIA|ASIA)[0-9A-Z]{16}',
    _B + r'gh[pousr]_[A-Za-z0-9]{30,}',
    _B + r'github_pat_[A-Za-z0-9_]{20,}',
    _B + r'xox[abprs]-[A-Za-z0-9-]{10,}',
    _B + r'AIza[0-9A-Za-z_-]{35}',
    _B + r'hf_[A-Za-z0-9]{30,}',
    _B + r'gsk_[A-Za-z0-9]{20,}',
    r'-----BEGIN [A-Z ]*PRIVATE KEY-----',
    _B + r'eyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}',
    r'\bbearer\s+[A-Za-z0-9._~+/=-]{20,}',
    r'\b(?:api[_-]?key|secret|token|password|passwd)\b\s*[:=]\s*[\'"]?(?=[A-Za-z0-9._~+/=-]*[0-9])[A-Za-z0-9._~+/=-]{16,}',
)
# The prefixes are case sensitive. Only the words "bearer", "api key", "secret", "token", and "password" ignore case.
_KEY_SHAPES = tuple(re.compile(pattern, re.ASCII | (re.IGNORECASE if pattern.startswith(('\\bbearer', '\\b(?:api')) else 0))
                    for pattern in _KEY_PATTERNS)

_STOP = r'''\s'"`<>|*?,;()\[\]{}'''
_FILE_URL = re.compile(r'file://[^' + _STOP + r']*', re.ASCII | re.IGNORECASE)
_URL = re.compile(r'''[A-Za-z][A-Za-z0-9+.-]*://[^\s'"`<>]*''', re.ASCII)
_POSIX_PATH = re.compile(r'(?<![\w/.:~\\)<>-])/(?!/)[^' + _STOP + r']+', re.ASCII)
_HOME_PATH = re.compile(r'(?<![\w/.~\\)<>-])~[\w.-]*/[^' + _STOP + r']*', re.ASCII)
_DRIVE_PATH = re.compile(r'(?<!\w)[A-Za-z]:[\\/][^' + _STOP + r']+', re.ASCII)
_UNC_PATH = re.compile(r'(?<![\\\w])\\\\[^' + _STOP + r'\\]+\\[^' + _STOP + r']*', re.ASCII)
_TRAVERSAL = re.compile(r'(?<![\w.])\.\.(?=[\\/])|(?<=[\\/])\.\.(?=[\\/' + _STOP + r']|$)', re.ASCII)
_WINDOWS_ROOT = re.compile(r'[A-Za-z]:[\\/]', re.ASCII)


def _is_windows(path):
    return bool(_WINDOWS_ROOT.match(path)) or path.startswith('\\\\')


def _normal(path):
    """(cleaned path, separator). Windows paths ignore case and accept both slashes."""
    if _is_windows(path):
        return ntpath.normcase(ntpath.normpath(path)), '\\'
    return posixpath.normpath(path), '/'


def _roots(allowed_paths):
    """Check and normalize the allow-list. A root must be an absolute path. A bare string is a mistake, not a list of one."""
    if isinstance(allowed_paths, (str, bytes)) or not isinstance(allowed_paths, (list, tuple, set, frozenset)):
        raise _invalid('Send allowed_paths as a list of absolute paths.')
    roots = []
    for root in allowed_paths:
        text = os.fspath(root) if isinstance(root, (str, os.PathLike)) else None
        if not isinstance(text, str) or not (text.startswith('/') or _is_windows(text)):
            raise _invalid('An allowed path must be an absolute path.')
        roots.append(_normal(text))
    return tuple(roots)


def _inside(candidate, roots):
    path, _ = _normal(candidate)
    for root, separator in roots:
        if path == root or path.startswith(root.rstrip(separator) + separator):
            return True
    return False


def _file_url_path(url):
    """The path part of a `file://` URL. It drops the host. A URL with no path gives '/', which no root allows unless the root is '/'."""
    rest = url[len('file://'):]
    return rest if rest.startswith('/') else ('/' + rest.partition('/')[2] if '/' in rest else '/')


def _paths_in(text):
    for url in _FILE_URL.findall(text):
        yield _file_url_path(url)
    for pattern in (_POSIX_PATH, _HOME_PATH, _DRIVE_PATH, _UNC_PATH):
        for match in pattern.finditer(text):
            yield match.group(0).rstrip('.:!')


def _has_traversal(text):
    return bool(_TRAVERSAL.search(_URL.sub(' ', text)))


def context_guard(messages, allowed_paths=(), secrets=()):
    """Refuse a message that carries a key-shaped string, a held key, or a path outside the allow-list.

    This is a small guard on what reaches a model. It does not choose what goes into a message. That choice
    is work for the policy and agent steps (E5 and E6).
    The guard looks at text only. It finds absolute paths (POSIX, home, Windows, UNC, and `file:` URLs) and `..`
    segments. It does not follow links and it does not check relative paths. It can refuse text that only looks like a
    key or a path. The error names the message number and never repeats the text.
    """
    roots = _roots(allowed_paths)
    held = []
    for secret in secrets:
        if not isinstance(secret, ApiKey):
            raise _invalid('Secrets must be ApiKey values.')
        held.append(secret.reveal_for_transport())
    for number, message in enumerate(messages, 1):
        if not isinstance(message, Message):
            raise _invalid('Every message must be a Message.')
        text = message.text
        if any(value in text for value in held) or any(shape.search(text) for shape in _KEY_SHAPES):
            raise _invalid(f'Message {number} holds text that looks like a secret key. Schola sent nothing.')
        if _has_traversal(text) or any(not _inside(path, roots) for path in _paths_in(text)):
            raise _invalid(f'Message {number} names a file path outside the allowed folders. Schola sent nothing.')


# ----------------------------------------------------------------------
# The adapter
# ----------------------------------------------------------------------

class ModelAdapter:
    """The interface. A subclass writes `_send(request) -> RawCompletion` and raises `ModelAdapterError` for a problem.

    `allowed_paths` is the list of folders whose paths may appear in messages (default: none).
    `secrets` is a list of `ApiKey` values that must never appear in a message or in the reply.
    """

    def __init__(self, allowed_paths=(), secrets=()):
        _roots(allowed_paths)
        if not isinstance(secrets, (list, tuple)) or not all(isinstance(secret, ApiKey) for secret in secrets):
            raise _invalid('Secrets must be a list of ApiKey values.')
        self._allowed_paths = tuple(allowed_paths)
        self._secrets = tuple(secrets)

    def _send(self, request):
        raise NotImplementedError

    def complete(self, request):
        if not isinstance(request, CompletionRequest):
            raise _invalid('Send a CompletionRequest.')
        context_guard(request.messages, self._allowed_paths, self._secrets)
        budget, reserved = request.budget, request.max_output_tokens
        budget._begin(reserved)  # raises token_cap_reached before any network work
        failure = result = None
        try:
            result = self._result(request, self._send(request))
        except ModelAdapterError as error:
            failure = error
        except NotImplementedError:  # a subclass with no transport is a programming error, not a provider problem
            budget._abort(reserved)
            raise
        except Exception:
            # Raised below, outside this block, so no traceback holds the original error or anything it printed.
            failure = ModelAdapterError('The model adapter failed in an unexpected way.', 'bad_response')
        except BaseException:
            budget._abort(reserved)
            raise
        if failure is not None:
            budget._abort(reserved)
            raise failure
        budget._finish(reserved, result.tokens_charged)
        return result

    def _result(self, request, raw):
        if not isinstance(raw, RawCompletion) or not isinstance(raw.text, str) or raw.stop_reason not in STOP_REASONS \
                or not (raw.usage is None or isinstance(raw.usage, Usage)):
            raise ModelAdapterError('The model adapter gave a reply of the wrong kind.', 'bad_response')
        text = raw.text
        for secret in self._secrets:
            text = text.replace(secret.reveal_for_transport(), REDACTED)
        if raw.usage is None:
            charged = request.max_output_tokens
            notice = (f'The provider reported no usage. Schola counted the request maximum of {charged} tokens as spent.')
            return CompletionResult(text, None, raw.stop_reason, False, charged, notice)
        return CompletionResult(text, raw.usage, raw.stop_reason, True, raw.usage.total, None)
