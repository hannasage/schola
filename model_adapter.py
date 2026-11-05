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
* The token budget is checked before any network work. A call holds an estimate of its input plus its
  `max_output_tokens`. If the hold does not fit under the cap, the call gets `token_cap_reached` and nothing is sent.
* A reply with no usable usage numbers (missing, malformed, or zero next to text) is never counted as free.
  The request's `max_output_tokens` is counted as spent, and the result says so (`usage_reported` and `notice`).
* An error can carry `tokens_charged`. A reply that arrived but cannot be used was still billed, so the budget counts it.
* An error carries a stable `code` and a `retryable` flag. A message is a fixed sentence. It
  never holds a key, a URL, or text from a provider.
* The API key lives in an `ApiKey`. Its text form is always `[redacted]`. This module does not
  store a key anywhere.

Only the Python standard library is used.
"""
import dataclasses
import logging
import ntpath
import os
import posixpath
import re
import threading
import traceback
import unicodedata

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
MAX_PATH_CHARS = 4096

REDACTED = '[redacted]'
_KEY_CHARACTERS = re.compile(r'[\x21-\x7e]+', re.ASCII)


class ModelAdapterError(Exception):
    """A model adapter problem with a stable code. The message is a fixed sentence and holds no secret."""

    def __init__(self, message, code, retryable=None, tokens_charged=0):
        if code not in ERROR_CODES:
            raise ValueError('Unknown model adapter error code.')
        if isinstance(tokens_charged, bool) or not isinstance(tokens_charged, int) or tokens_charged < 0:
            raise ValueError('tokens_charged must be a whole number of 0 or more.')
        super().__init__(message)
        self.code = code
        self.retryable = code in _RETRYABLE_CODES if retryable is None else bool(retryable)
        # What the budget counts for this failure. A reply that arrived but cannot be used was still billed.
        self.tokens_charged = tokens_charged

    def payload(self):
        return {'error': str(self), 'code': self.code, 'retryable': self.retryable, 'tokens_charged': self.tokens_charged}

    def __reduce__(self):
        return (self.__class__, (str(self), self.code, self.retryable, self.tokens_charged))


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
        if getattr(self, '_ApiKey__value', None) is not None:
            raise _invalid('An API key cannot be set a second time.')
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

class _Hold:
    """The tokens one call holds in a budget while it runs."""
    __slots__ = ('amount', 'active')

    def __init__(self):
        self.amount, self.active = 0, False


class TokenBudget:
    """A cap on the tokens one session may spend. The cap is required. Schola has no default (an open decision).

    Before a call sends anything, the budget must have room for the whole call: the tokens already spent, plus the
    tokens that calls in flight hold, plus this call's hold, must fit in the cap. A call holds an estimate of its
    input (about 3 characters a token) plus its `max_output_tokens`. Otherwise the call is refused with `token_cap_reached`.
    When the call ends, the budget swaps the hold for the tokens the provider reported. A call can still pass the cap
    if the provider counts more than the estimate. The next call is then refused.
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

    @property
    def spent(self):
        with self._lock:
            return self._spent

    @property
    def remaining(self):
        """What a new call can still hold: the cap minus the tokens spent and the tokens held by calls in flight."""
        with self._lock:
            return max(0, self._cap - self._spent - self._reserved)

    @property
    def exhausted(self):
        return self.remaining == 0

    def __repr__(self):
        return f'TokenBudget(cap={self._cap}, spent={self.spent})'

    def _begin(self, reserve, hold):
        """Hold `reserve` tokens for one call. Refuse when they do not fit."""
        with self._lock:
            if reserve > self._cap - self._spent - self._reserved:
                raise ModelAdapterError('The token cap for this session leaves too little room for this call. Schola sent nothing.', 'token_cap_reached')
            self._reserved += reserve
            hold.amount, hold.active = reserve, True

    def _abort(self, hold):
        """Free a hold and charge nothing. Does nothing for a hold that is already settled."""
        with self._lock:
            if hold.active:
                self._reserved -= hold.amount
                hold.active = False

    def _finish(self, hold, charge):
        """Free a hold and charge `charge` tokens. Does nothing for a hold that is already settled."""
        with self._lock:
            if hold.active:
                self._reserved -= hold.amount
                self._spent += charge
                hold.active = False


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
    `redacted` is true when Schola removed the API key from the reply text.
    """
    text: str
    usage: object
    stop_reason: str
    usage_reported: bool
    tokens_charged: int
    notice: object = None
    redacted: bool = False


# ----------------------------------------------------------------------
# The context guard
# ----------------------------------------------------------------------

# The guard reads two views of each message: the text with invisible characters removed and compatibility forms folded
# (NFKC), and the same text with all white space removed. The second view finds a key that was split by spaces or lines.
_INVISIBLE = {ord(char): None for char in '\u00ad\u034f\u061c\u180e\u200b\u200c\u200d\u200e\u200f\u202a\u202b\u202c\u202d\u202e'
              '\u2060\u2061\u2062\u2063\u2064\u2066\u2067\u2068\u2069\ufeff'}
_WHITESPACE = re.compile(r'\s+')

# Shapes of secrets that are common and easy to spot. This is a short list of patterns, not a scanner. It misses what it
# does not know, and it can refuse text that only looks like a key. Every pattern here must scan in linear time.
_B = r'(?<![A-Za-z0-9])'
# Each shape comes with a short lowercase word that must be in the text before the pattern runs. This keeps the scan fast on text
# that has nothing like a key in it.
_COMPACT_SHAPES = tuple((re.compile(pattern), word) for pattern, word in (
    (_B + r'[sS][kK]-[A-Za-z0-9_-]{16,}', 'sk-'),
    (_B + r'[sr]k_(?:live|test)_[A-Za-z0-9]{16,}', 'k_'),
    (_B + r'(?:AKIA|ASIA)[0-9A-Z]{16}', 'a'),
    (_B + r'gh[pousr]_[A-Za-z0-9]{30,}', 'gh'),
    (_B + r'github_pat_[A-Za-z0-9_]{20,}', 'github_pat_'),
    (_B + r'xox[abprs]-[A-Za-z0-9-]{10,}', 'xox'),
    (_B + r'AIza[0-9A-Za-z_-]{35}', 'aiza'),
    (_B + r'hf_[A-Za-z0-9]{30,}', 'hf_'),
    (_B + r'gsk_[A-Za-z0-9]{20,}', 'gsk_'),
    (_B + r'npm_[A-Za-z0-9]{30,}', 'npm_'),
    (_B + r'glpat-[A-Za-z0-9_-]{20,}', 'glpat-'),
    (_B + r'SG\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}', 'sg.'),
    (_B + r'ya29\.[A-Za-z0-9_-]{20,}', 'ya29.'),
    (_B + r'whsec_[A-Za-z0-9]{20,}', 'whsec_'),
    (_B + r'sb_secret_[A-Za-z0-9_-]{16,}', 'sb_secret_'),
))
_SPACED_SHAPES = tuple((re.compile(pattern, flags), word) for pattern, flags, word in (
    (r'-----BEGIN [A-Z ]*PRIVATE KEY[A-Z ]*-----', 0, '-----begin'),
    (r'\bbearer\s+[A-Za-z0-9._~+/=-]{20,}', re.IGNORECASE, 'bearer'),
    (r'\bauthorization["\']?\s*[:=]\s*["\']?basic\s+[A-Za-z0-9+/]{12,}', re.IGNORECASE, 'authorization'),
    (r'(?<![A-Za-z0-9+.-])[A-Za-z][A-Za-z0-9+.-]*://[^\s/@:]+:[^\s/@]+@', 0, '://'),
))
# NAME = value, where the name ends in api key, secret, token, or password. The value needs a digit, and the digit
# test is made in code: a look-ahead for it in the pattern makes the scan quadratic.
_KEYWORD = re.compile(r'(?:api[_-]?key|secret(?:[_-]?access)?(?:[_-]?key)?|token|passwd|password)'
                      r'["\']?\s*[:=]\s*["\']?(?P<value>[A-Za-z0-9._~+/=-]{16,})', re.IGNORECASE)
_KEYWORD_WORDS = ('api', 'secret', 'token', 'passwd', 'password')
_JWT_RUN = re.compile(r'[A-Za-z0-9_.-]+')

# Paths. A path starts at the start of the text or after white space, a quote, a bracket, or one of < > : = , ; | & !
# (or after "-" that follows white space). A "/" inside a word ("and/or", "km/h", "1/2") starts nothing.
_STOP = r'''\s'"`<>|*?,;()\[\]{}'''
_SET = r'''\s'"`(\[{<>:=,;|&!'''
_START = r'(?:^|(?<=[' + _SET + r'])|(?<=\s-))'
_FILE_URL = re.compile(r'(?<![A-Za-z0-9+.-])file://[^' + _STOP + r']*', re.IGNORECASE)
_URL = re.compile(r'''(?<![A-Za-z0-9+.-])[A-Za-z][A-Za-z0-9+.-]*://[^\s'"`<>]*''')
_POSIX_PATH = re.compile(_START + r'(?P<slashes>/+)(?P<rest>[^' + _STOP + r'/][^' + _STOP + r']*)')
_HOME_PATH = re.compile(_START + r'~[\w.-]*/[^' + _STOP + r']*')
_DRIVE_PATH = re.compile(r'(?<!\w)[A-Za-z]:[\\/][^' + _STOP + r']+')
_UNC_PATH = re.compile(r'(?<![\\\w])\\\\[^' + _STOP + r'\\]+\\[^' + _STOP + r']*')
_ENV_PATH = re.compile(r'(?<![\w$])\$\{?[A-Za-z_][A-Za-z0-9_]*\}?[\\/]|%[A-Za-z_][A-Za-z0-9_]*%[\\/]')
_CLOSING_TAG = re.compile(r'</[A-Za-z][A-Za-z0-9-]*\s*>')
_DRIVE_TAIL = re.compile(r'(?:^|(?<=\W))[A-Za-z]:$')
_TRAVERSAL = re.compile(r'(?<![\w.])\.\.(?![\w.])')
_WINDOWS_ROOT = re.compile(r'[A-Za-z]:[\\/]')
_DRIVE_ONLY = re.compile(r'[a-z]:\\?')
_AFTER_ROOT = ''' \t\r\n\'"`)]},;:!?'''


def _is_windows(path):
    return bool(_WINDOWS_ROOT.match(path)) or path.startswith('\\\\')


def _normal(path):
    """(cleaned path, separator). Windows paths ignore case and accept both slashes."""
    if _is_windows(path):
        return ntpath.normcase(ntpath.normpath(path)), '\\'
    return posixpath.normpath(path), '/'


def _roots(allowed_paths):
    """Check and normalize the allow-list: (cleaned path, separator, has a space). A root must be an absolute path.

    A bare string is a mistake, not a list of one. The root of everything ("/" or a drive such as "C:\\") is refused.
    """
    if isinstance(allowed_paths, (str, bytes)) or not isinstance(allowed_paths, (list, tuple, set, frozenset)):
        raise _invalid('Send allowed_paths as a list of absolute paths.')
    roots = []
    for root in allowed_paths:
        text = os.fspath(root) if isinstance(root, (str, os.PathLike)) else None
        if not isinstance(text, str) or not (text.startswith('/') or _is_windows(text)):
            raise _invalid('An allowed path must be an absolute path.')
        path, separator = _normal(text)
        if not path.strip('/') or _DRIVE_ONLY.fullmatch(path):
            raise _invalid('An allowed path cannot be the root of everything. Name a folder.')
        roots.append((path, separator, bool(_WHITESPACE.search(path))))
    return tuple(roots)


def _inside(candidate, roots):
    if len(candidate) > MAX_PATH_CHARS:
        return False  # no real path is this long, and cleaning one this long is slow
    path, _ = _normal(candidate)
    for root, separator, _ in roots:
        if path == root or path.startswith(root.rstrip(separator) + separator):
            return True
    return False


def _spaced_root_at(text, start, roots):
    """True when the text at `start` begins with an allowed root that holds a space, and the root ends there."""
    for root, separator, spaced in roots:
        if not spaced:
            continue
        piece = text[start:start + len(root)]
        if separator == '\\':
            piece = piece.replace('/', '\\').lower()
        if piece != root:
            continue
        follow = text[start + len(root):start + len(root) + 2]
        if follow == '' or follow[0] in '/\\' or follow[0] in _AFTER_ROOT or (follow[0] == '.' and (len(follow) == 1 or follow[1] in _AFTER_ROOT)):
            return True
    return False


def _file_url_path(url):
    """The path part of a `file://` URL. It drops the host. A URL with no path gives '/', which no root allows."""
    rest = url[len('file://'):]
    return rest if rest.startswith('/') else ('/' + rest.partition('/')[2] if '/' in rest else '/')


def _path_problem(text, roots):
    """True when the text has a `..` segment, an environment path, or an absolute path outside the roots."""
    stripped = _CLOSING_TAG.sub(' ', _URL.sub(' ', text))
    if ('..' in stripped and _TRAVERSAL.search(stripped)) or (('$' in stripped or '%' in stripped) and _ENV_PATH.search(stripped)):
        return True
    for url in _FILE_URL.findall(text):
        if not _inside(_file_url_path(url), roots):
            return True
    for pattern, mark in ((_POSIX_PATH, '/'), (_HOME_PATH, '~'), (_DRIVE_PATH, ':'), (_UNC_PATH, '\\\\')):
        if mark not in stripped:
            continue
        for match in pattern.finditer(stripped):
            if pattern is _POSIX_PATH and (len(match.group('slashes')) > 1 and '/' not in match.group('rest')
                                           or _DRIVE_TAIL.search(stripped, max(0, match.start() - 2), match.start())):
                continue  # "//word" is a comment marker, not a path. "C:/dir" is a drive path, and the drive pattern reads it.
            if not _inside(match.group(0).rstrip('.:!'), roots) and not _spaced_root_at(stripped, match.start(), roots):
                return True
    return False


def _has_jwt(text):
    for run in _JWT_RUN.findall(text):
        if run.count('.') < 2:
            continue
        parts = run.split('.')
        for index in range(len(parts) - 2):
            if parts[index].startswith('eyJ') and len(parts[index]) >= 11 and parts[index + 1].startswith('eyJ') \
                    and len(parts[index + 1]) >= 11 and len(parts[index + 2]) >= 8:
                return True
    return False


def _has_assignment(text):
    return any(any('0' <= char <= '9' for char in match.group('value')) for match in _KEYWORD.finditer(text))


def _looks_like_secret(spaced, compact):
    # A key that was split by spaces or lines shows in the compact view. A key that sits after a word shows in the spaced
    # view, because joining the words would hide the start of the key. A key split after a word is not found.
    lowered = compact.lower()
    return (any(word in lowered and (shape.search(compact) or shape.search(spaced)) for shape, word in _COMPACT_SHAPES)
            or any(word in lowered and shape.search(spaced) for shape, word in _SPACED_SHAPES)
            or ('eyJ' in spaced and _has_jwt(spaced)) or (any(word in lowered for word in _KEYWORD_WORDS) and _has_assignment(spaced)))


def context_guard(messages, allowed_paths=(), secrets=()):
    """Refuse a message that carries a key-shaped string, a held key, or a path outside the allow-list.

    This is a small guard on what reaches a model. It does not choose what goes into a message. That choice
    is work for the policy and agent steps (E5 and E6).
    It looks at text only, after it folds compatibility forms (NFKC) and removes invisible characters. It finds
    absolute paths (POSIX, home, Windows, UNC, `file:` URLs, and environment variables such as `$HOME/`) and `..`
    segments. It does not follow links and it does not check relative paths. It can refuse text that only looks
    like a key or a path. The error names the message number and never repeats the text.
    The whole scan is linear in the length of the text, and the text is capped at `MAX_TOTAL_CHARS`.
    """
    roots = _roots(allowed_paths)
    held = []
    for secret in secrets:
        if not isinstance(secret, ApiKey):
            raise _invalid('Secrets must be ApiKey values.')
        held.append(secret.reveal_for_transport())
    if not isinstance(messages, (list, tuple)) or len(messages) > MAX_MESSAGES:
        raise _invalid(f'Send at most {MAX_MESSAGES} messages.')
    total, compacts = 0, []
    for number, message in enumerate(messages, 1):
        if not isinstance(message, Message):
            raise _invalid('Every message must be a Message.')
        total += len(message.text)
        if total > MAX_TOTAL_CHARS:
            raise _invalid(f'The messages are too long to check. Send at most {MAX_TOTAL_CHARS} characters.')
        spaced = unicodedata.normalize('NFKC', message.text).translate(_INVISIBLE)
        compact = _WHITESPACE.sub('', spaced)
        if any(value in compact for value in held) or _looks_like_secret(spaced, compact):
            raise _invalid(f'Message {number} holds text that looks like a secret key. Schola sent nothing.')
        if _path_problem(spaced, roots):
            raise _invalid(f'Message {number} names a file path outside the allowed folders. Schola sent nothing.')
        compacts.append(compact)
    joined = ''.join(compacts)
    if any(value in joined for value in held):
        raise _invalid('The messages together hold a secret key. Schola sent nothing.')


# ----------------------------------------------------------------------
# The adapter
# ----------------------------------------------------------------------

_LOG = logging.getLogger('schola.model_adapter')
_LOG.addHandler(logging.NullHandler())  # silent until the application adds a handler


class _NoTransport(NotImplementedError):
    """The base class has no `_send`. This is a programming error and it is not turned into a provider error."""


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
        raise _NoTransport

    def _scrub(self, text):
        """The text without the values of the held keys."""
        for secret in self._secrets:
            text = text.replace(secret.reveal_for_transport(), REDACTED)
        return text

    def _unexpected(self, error):
        """Log an exception that nobody planned for, with its traceback and without any held key. Return the error to raise."""
        detail = self._scrub(''.join(traceback.format_exception(type(error), error, error.__traceback__)))
        _LOG.error('A model adapter failed in an unexpected way:\n%s', detail)
        return ModelAdapterError('The model adapter failed in an unexpected way.', 'bad_response', retryable=bool(getattr(error, 'retryable', False)))

    def complete(self, request):
        if not isinstance(request, CompletionRequest):
            raise _invalid('Send a CompletionRequest.')
        context_guard(request.messages, self._allowed_paths, self._secrets)
        budget = request.budget
        # Reserve the input (about 3 characters a token, rounded up) and the longest reply the request allows.
        reserve = -(-sum(len(message.text) for message in request.messages) // 3) + request.max_output_tokens
        hold = _Hold()
        try:
            budget._begin(reserve, hold)  # raises token_cap_reached before any network work
            failure = result = None
            try:
                result = self._result(request, self._send(request))
            except ModelAdapterError as error:
                failure = error
            except (MemoryError, _NoTransport):
                raise
            except Exception as error:
                # Raised below, outside this block, so no traceback holds the original error or anything it printed.
                failure = self._unexpected(error)
            if failure is not None:
                budget._finish(hold, failure.tokens_charged)
                raise failure
            budget._finish(hold, result.tokens_charged)
            return result
        finally:
            budget._abort(hold)  # does nothing once the hold is settled; frees it after an interrupt

    def _result(self, request, raw):
        if not isinstance(raw, RawCompletion) or not isinstance(raw.text, str) or raw.stop_reason not in STOP_REASONS \
                or not (raw.usage is None or isinstance(raw.usage, Usage)):
            raise ModelAdapterError('The model adapter gave a reply of the wrong kind.', 'bad_response')
        text = self._scrub(raw.text)
        redacted = text != raw.text
        usage = raw.usage
        if usage is not None and usage.total == 0 and raw.text:
            usage = None  # zero tokens next to a reply with text cannot be true
        notices = []
        if usage is None:
            charged = request.max_output_tokens
            notices.append(f'The provider reported no usage that Schola can use. Schola counted the request maximum of {charged} tokens as spent.')
        else:
            charged = usage.total
        if redacted:
            notices.append('Schola redacted the API key from the reply text.')
        return CompletionResult(text, usage, raw.stop_reason, usage is not None, charged, ' '.join(notices) or None, redacted)
