"""The API adapter: one model endpoint that follows the OpenAI chat-completions request format.

    ApiAdapter(base_url, model, api_key).complete(request)

The caller gives the base URL (for example `https://api.openai.com/v1`), the model name, and an
`ApiKey`. The adapter posts to `<base URL>/chat/completions`. The interface, the token budget, and the
key wrapper are in `model_adapter.py`. `docs/model-adapters.md` explains the terms and the limits.

Endpoint rules
--------------
* `https` only. `http` is allowed for a loopback host only: `127.0.0.1`, `::1`, or `localhost`.
* No credentials in the URL, no fragment, no query, no other scheme, no ambiguous numeric host.
* The host is looked up on every call. For a host that is not loopback, every address must be public
  (not private, loopback, link-local, multicast, reserved, or unspecified, and no address that carries
  one of those inside an IPv4-mapped, 6to4, or NAT64 address). For a loopback host, every address must be
  loopback. One bad address refuses the host.
* The adapter connects to the address it checked, not to the name. A second lookup cannot send it
  somewhere else, so DNS rebinding between the check and the connection is closed. The name stays in
  the `Host` header and in the TLS check.
* A redirect is never followed. The key goes to the configured host only.
* The environment's proxy settings are ignored: `http.client` does not read them.

Limits on every call
--------------------
* A connect timeout, a read timeout for each wait, and a total time for the whole call. One monotonic deadline covers the
  name lookup, each connection try, the TLS handshake, the send, and the reply. The total must be at least the connect time.
* The reply body is read in pieces and refused once it passes the cap (1 MB by default).
* The reply must be one JSON object of the expected shape. Anything else is `bad_response`.
* A 200 reply that cannot be used was billed: the error carries the usage of the body, or the whole hold (the input estimate plus
  `max_output_tokens`) when the usage is unreadable. A body that breaks in the middle, and a timeout after the request was sent,
  carry the whole hold. Other failures carry nothing.

An error message is a fixed sentence. It never holds the key, the URL, the address, the operating system's
error text, or any text from the provider. Errors are raised after the handler that caught the problem has
ended, so they carry no chained error.

Only the Python standard library is used.
"""
import http.client
import ipaddress
import json
import re
import socket
import ssl
import threading
import time
from urllib.parse import urlsplit

import model_adapter as ma
from model_adapter import ModelAdapterError, RawCompletion, Usage

LOOPBACK_HOSTS = ('127.0.0.1', '::1', 'localhost')
DEFAULT_CONNECT_TIMEOUT = 10.0
DEFAULT_READ_TIMEOUT = 60.0
DEFAULT_TOTAL_TIMEOUT = 120.0
MAX_TIMEOUT = 600.0
MAX_RESPONSE_BYTES = 1_000_000
MAX_RESPONSE_BYTES_LIMIT = 10_000_000
MAX_URL_CHARS = 2048
MAX_MODEL_CHARS = 200
TOKEN_FIELDS = ('max_tokens', 'max_completion_tokens')
_CHUNK = 8192
_USER_AGENT = 'schola-api-adapter/1'

_URL_CHARACTERS = re.compile(r'[\x21-\x7e]+', re.ASCII)
_LABEL = re.compile(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', re.ASCII)
_NUMBERLIKE = re.compile(r'[0-9]+|0[xX][0-9a-fA-F]*', re.ASCII)
_PATH = re.compile(r'(?:/[A-Za-z0-9._~-]+)*/?', re.ASCII)
_MODEL = re.compile(r'[A-Za-z0-9._:/@+#~-]+', re.ASCII)
_NAT64 = ipaddress.ip_network('64:ff9b::/96')
_STOP_REASONS = {'stop': 'end', 'length': 'length', 'content_filter': 'content_filter'}

_UNSAFE_URL = 'The endpoint URL is not allowed. Use https, or http to a loopback host, with no credentials, query, or fragment.'


def _unsafe(message=_UNSAFE_URL):
    return ModelAdapterError(message, 'unsafe_endpoint')


class _Endpoint:
    """A checked base URL. It holds the pieces the transport needs and nothing the caller typed in full."""

    def __init__(self, scheme, host, port, path, loopback):
        self.scheme, self.host, self.port, self.path, self.loopback = scheme, host, port, path, loopback


def _ip(text):
    """An address object, or None when `text` is not an IP address."""
    try:
        return ipaddress.ip_address(text)
    except (ValueError, TypeError):
        return None


def _split(url):
    """(parts, host, port) of a URL, or (None, None, None) when it cannot be read."""
    try:
        parts = urlsplit(url)
        return parts, parts.hostname, parts.port
    except ValueError:
        return None, None, None


def _valid_host(host):
    if len(host) > 253:
        return False
    if _ip(host) is not None:
        return True
    labels = host.split('.')
    if not all(_LABEL.fullmatch(label) for label in labels):
        return False
    # "127.1", "2130706433", and "0x7f.1" are numbers that some systems turn into an address. A real name never ends in one.
    return not _NUMBERLIKE.fullmatch(labels[-1])


def parse_endpoint(base_url):
    """Check a base URL and return its parts. A URL that breaks a rule is `unsafe_endpoint`. The URL never goes in a message."""
    if not isinstance(base_url, str) or not base_url:
        raise ma._invalid('Send the endpoint as a URL.')
    if len(base_url) > MAX_URL_CHARS or not _URL_CHARACTERS.fullmatch(base_url) or '#' in base_url or '?' in base_url:
        raise _unsafe()
    parts, host, port = _split(base_url)
    if parts is None:
        raise _unsafe()
    scheme = parts.scheme.lower()
    if scheme not in ('http', 'https') or '@' in parts.netloc or not host or '%' in parts.netloc or not _valid_host(host):
        raise _unsafe()
    if port is not None and not 1 <= port <= 65535:
        raise _unsafe()
    loopback = host in LOOPBACK_HOSTS
    if scheme == 'http' and not loopback:
        raise _unsafe('http is allowed only for a loopback host (127.0.0.1, ::1, or localhost). Use https.')
    if not _PATH.fullmatch(parts.path) or any(segment in ('.', '..') for segment in parts.path.split('/')):
        raise _unsafe()
    return _Endpoint(scheme, host, port or (443 if scheme == 'https' else 80), parts.path.rstrip('/'), loopback)


def _embedded(address):
    """The IPv4 address hidden inside an IPv6 address (mapped, 6to4, or NAT64), or None."""
    if address.version != 6:
        return None
    if address.ipv4_mapped is not None:
        return address.ipv4_mapped
    if address.sixtofour is not None:
        return address.sixtofour
    if address in _NAT64:
        return ipaddress.IPv4Address(int(address) & 0xFFFFFFFF)
    return None


# Ranges that are refused whatever the running Python says about them. Its tables differ between versions.
_BLOCKED = tuple(ipaddress.ip_network(text) for text in ('192.88.99.0/24', '100.64.0.0/10', '198.18.0.0/15', 'fec0::/10', '2001:db8::/32', '64:ff9b:1::/48'))


def address_allowed(address, loopback_host):
    """True when `address` is fit for the host kind: only loopback for a loopback host, only public otherwise."""
    if getattr(address, 'scope_id', None):
        return False
    hidden = _embedded(address)
    if hidden is not None:
        return not loopback_host and address_allowed(hidden, False)
    if loopback_host:
        return address.is_loopback
    return address.is_global and not (address.is_private or address.is_loopback or address.is_link_local or address.is_multicast
                                      or address.is_reserved or address.is_unspecified or getattr(address, 'is_site_local', False)
                                      or any(address in network for network in _BLOCKED))


def _tls_ok(context):
    """True when a TLS context checks certificates and host names and allows TLS 1.2 or later only."""
    return (isinstance(context, ssl.SSLContext) and context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname
            and context.minimum_version >= ssl.TLSVersion.TLSv1_2
            and (context.maximum_version == ssl.TLSVersion.MAXIMUM_SUPPORTED or context.maximum_version >= ssl.TLSVersion.TLSv1_2))


def _tls_acceptable(context):
    """True when a context checks certificates and host names and its highest TLS version is not below 1.2 (or below its own lowest).

    This is tested before anything is changed, so a context that is refused is left as it was.
    """
    if not isinstance(context, ssl.SSLContext) or context.verify_mode != ssl.CERT_REQUIRED or not context.check_hostname:
        return False
    top = context.maximum_version
    return top == ssl.TLSVersion.MAXIMUM_SUPPORTED or top >= max(context.minimum_version, ssl.TLSVersion.TLSv1_2)


def _raise_tls_floor(context):
    """Raise the lowest TLS version of a context to 1.2 when it is lower.

    Some systems report a plain context with no floor at all (`MINIMUM_SUPPORTED`). Refusing such a context would turn a
    normal caller away, so the change goes on the caller's context. A context that cannot take the change is left as it is,
    and `_tls_ok` then refuses it.
    """
    if context.minimum_version < ssl.TLSVersion.TLSv1_2:
        try:
            context.minimum_version = ssl.TLSVersion.TLSv1_2
        except ValueError:
            pass


class ConnectFailed(OSError):
    """No address connected, and none timed out. It is an `OSError`, so it is `endpoint_unreachable` unless a subclass of the adapter words it better.

    `errno` is the number of the failure. When any address refused the connection it is the number of that refusal, and `refused` is true.
    """

    def __init__(self, number, message, refused):
        super().__init__(number, message)
        self.refused = refused


class _PinnedConnection(http.client.HTTPConnection):
    """A connection that goes to an address the adapter already checked, and keeps the host name for the Host header and TLS.

    Every wait is cut by `deadline`, a `time.monotonic()` value: each connection try, and the TLS handshake.
    """

    def __init__(self, host, port, addresses, context, connect_timeout, read_timeout, deadline):
        super().__init__(host, port, timeout=connect_timeout)
        self._addresses, self._context, self._read_timeout, self._deadline = addresses, context, read_timeout, deadline
        self._live = None

    def _left(self):
        left = self._deadline - time.monotonic()
        if left <= 0:
            raise TimeoutError('the call ran out of time')
        return left

    def _remaining(self):
        return min(self.timeout, self._left())

    def arm(self):
        """Set the time limit of the next send or read: the read time, or what is left of the total if that is less.

        A shutdown from the timer thread does not reliably wake a blocked send or TLS read on every system, so every
        wait carries its own limit as well.
        """
        wait = min(self._read_timeout, self._left())  # raises when the deadline has passed
        sock = self.live()
        if sock is not None and getattr(sock, 'fileno', lambda: 0)() != -1:  # `http.client` closes the socket once it has read a whole body
            sock.settimeout(wait)

    def live(self):
        """The socket in use. `http.client` drops `self.sock` as soon as a reply says "connection: close", but the reply still reads from it."""
        return self._live if self._live is not None else self.sock

    def send(self, data):
        if self.sock is None and self.auto_open:
            self.connect()
        self.arm()
        super().send(data)

    def getresponse(self):
        self.arm()
        return super().getresponse()

    def connect(self):
        if self._context is not None and not _tls_ok(self._context):
            raise ModelAdapterError('The TLS settings no longer check certificates and host names. Schola sent nothing.', 'unsafe_endpoint')
        last = refusal = timeout = None
        for address in self._addresses:
            wait = self._remaining()
            try:
                sock = socket.create_connection((address, self.port), wait)
            except OSError as error:
                last = error
                if isinstance(error, TimeoutError):
                    timeout = error
                elif isinstance(error, (ConnectionRefusedError, ConnectionAbortedError)) and refusal is None:
                    refusal = error
                continue
            break
        else:
            if timeout is not None:
                raise timeout  # an address that timed out may be a server that is busy; the others do not change that
            failure = refusal or last
            raise ConnectFailed(failure.errno, 'no connection', refusal is not None)
        self.sock = self._live = sock  # set at once, so the timer can close a socket that is still in the handshake
        if self._context is not None:
            # A failure here leaves the raw socket in `self.sock`, and `close()` of the connection closes it.
            sock.settimeout(self._remaining())
            sock = self._context.wrap_socket(sock, server_hostname=self.host)
            self.sock = self._live = sock
        self.arm()


def model_name_ok(model):
    """True when `model` is a model name Schola accepts: 1 to 200 characters from letters, digits, and . _ : / @ + # ~ -"""
    return isinstance(model, str) and len(model) <= MAX_MODEL_CHARS and bool(_MODEL.fullmatch(model))


def _first_fields(items):
    """The address text of each `getaddrinfo` entry, or None when the answer is not in that shape."""
    try:
        return [item[4][0] for item in items]
    except (TypeError, IndexError, KeyError):
        return None


def _number(value, low, high):
    return not isinstance(value, bool) and isinstance(value, (int, float)) and low < value <= high


def _parse_int(text):
    if len(text) > 18:
        raise ValueError('number too long')
    return int(text)


def _reject_constant(text):
    raise ValueError('not JSON')


def _load(body):
    """The JSON object in a reply body, or None when the body is not one object (bad text, a number of over 18 digits, NaN, too deep)."""
    try:
        document = json.loads(body.decode('utf-8'), parse_int=_parse_int, parse_constant=_reject_constant)
    except (ValueError, RecursionError):
        return None
    return document if isinstance(document, dict) else None


def _usage(document):
    """Usage from the reply, or None when it is missing or not two counts. The caller then counts the request maximum."""
    usage = document.get('usage')
    if not isinstance(usage, dict):
        return None
    counts = (usage.get('prompt_tokens'), usage.get('completion_tokens'))
    if any(isinstance(count, bool) or not isinstance(count, int) or not 0 <= count <= ma.MAX_TOKEN_COUNT for count in counts):
        return None
    if counts[0] == 0:
        return None  # the provider read the input, so a count of zero for it cannot be right
    return Usage(*counts)


def _length(text):
    """The value of a Content-Length header, or None when it is not plain ASCII digits that `int` can read."""
    text = text.strip()
    if not (text.isascii() and text.isdecimal()):
        return None
    try:
        return int(text)
    except ValueError:  # a number of thousands of digits
        return None


def _media(header):
    """(media type, charset) of a Content-Type header. The charset is None when it is not given."""
    pieces = [piece.strip() for piece in header.split(';')]
    charset = None
    for piece in pieces[1:]:
        name, _, value = piece.partition('=')
        if name.strip().lower() == 'charset':
            charset = value.strip().strip('"').lower()
    return pieces[0].lower(), charset


def _json_media(media):
    return media == 'application/json' or (media.startswith('application/') and media.endswith('+json'))


def _status_error(status):
    """The error for an HTTP status that is not 200, or None for 200."""
    if status == 200:
        return None
    if 300 <= status < 400:
        return _unsafe('The endpoint tried to redirect the request. Schola never follows a redirect.')
    if status in (401, 403):
        return ModelAdapterError('The provider refused the API key. Check the key and what it may do.', 'auth_failed')
    if status == 429:
        return ModelAdapterError('The provider says there were too many requests or the quota is used. Try again later.', 'rate_limited')
    if status in (408, 504):
        return ModelAdapterError(f'The provider timed out (HTTP {status}).', 'timeout')
    if status in (501, 505):
        return ModelAdapterError(f'The provider does not support this request (HTTP {status}).', 'bad_response')
    if 500 <= status < 600:
        return ModelAdapterError(f'The provider had a server error (HTTP {status}).', 'endpoint_unreachable')
    if status == 402:
        return ModelAdapterError('The provider asks for payment or says the quota is used up (HTTP 402). Check billing and the plan.', 'invalid_request')
    if status in (400, 422):
        return ModelAdapterError(f'The provider refused the request (HTTP {status}). Check the model name and the output limit field (max_tokens_field).',
                                 'invalid_request')
    if 400 <= status < 500:
        return ModelAdapterError(f'The provider refused the request (HTTP {status}). Check the base URL and the model name.', 'invalid_request')
    return ModelAdapterError(f'The provider answered with an unexpected status (HTTP {status}).', 'bad_response')


def _timeout(tokens_charged=0):
    return ModelAdapterError('The provider did not answer in time.', 'timeout', tokens_charged=tokens_charged)


def _too_large():
    return ModelAdapterError('The provider reply is larger than Schola allows.', 'response_too_large')


def _bad(message='The provider reply was not in the expected format.'):
    return ModelAdapterError(message, 'bad_response')


def _billed(error, usage, unreadable):
    """Set what the budget counts for a 200 reply that cannot be used: the usage it carries, else `unreadable`."""
    error.tokens_charged = usage.total if usage is not None else unreadable
    return error


def _body_error(value, usage, status_error=None):
    """The error for a 200 reply that holds an `error` object. A number in `code` or `status` goes through the status table."""
    number = None
    if isinstance(value, dict):
        for key in ('code', 'status'):
            candidate = value.get(key)
            if isinstance(candidate, str) and len(candidate) == 3 and candidate.isascii() and candidate.isdecimal():
                candidate = int(candidate)
            if isinstance(candidate, int) and 400 <= candidate <= 599:  # a bool is 0 or 1, so it never gets here
                number = candidate
                break
    mapped = (status_error or _status_error)(number) if number is not None else None
    if mapped is not None:
        # Same code and retry advice as the status table, but the sentence says where the provider put it.
        mapped = ModelAdapterError(f'The provider reported this in the body of a 200 reply: {mapped}', mapped.code)  # the retry advice comes from the code
    return _billed(mapped or _bad('The provider reply holds an error.'), usage, 0)


_RETRY_WORTHY_TLS = (ssl.SSLEOFError, ssl.SSLZeroReturnError, ssl.SSLWantReadError, ssl.SSLWantWriteError)


def _classify(error):
    """Turn a network or protocol exception into a typed error with a fixed sentence, or None if it is not one we know.

    Nothing of `error` goes in the sentence.
    """
    if isinstance(error, TimeoutError):
        return _timeout()
    if isinstance(error, ssl.SSLCertVerificationError):
        return ModelAdapterError('The TLS certificate of the endpoint could not be verified.', 'endpoint_unreachable', retryable=False)
    if isinstance(error, ssl.SSLError):
        return ModelAdapterError('Schola could not make a secure (TLS) connection to the endpoint.', 'endpoint_unreachable',
                                 retryable=isinstance(error, _RETRY_WORTHY_TLS))
    if isinstance(error, OSError):
        return ModelAdapterError('Schola could not reach the endpoint.', 'endpoint_unreachable')
    if isinstance(error, (http.client.HTTPException, ValueError)):
        return _bad()
    return None


class ApiAdapter(ma.ModelAdapter):
    """Call an OpenAI-format chat endpoint.

    `max_tokens_field` is the name of the output limit in the request: `max_tokens` (the common one) or `max_completion_tokens`.
    `ssl_context` must check certificates and host names and must not cap TLS below 1.2. Schola raises its lowest version to TLS 1.2
    (on the context you pass in). `resolver` replaces
    `socket.getaddrinfo`; tests use it. The total time must be at least the connect time.
    A loopback endpoint may run without a key (`api_key=None`).
    """

    def __init__(self, base_url, model, api_key, *, allowed_paths=(), max_tokens_field='max_tokens', connect_timeout=DEFAULT_CONNECT_TIMEOUT,
                 read_timeout=DEFAULT_READ_TIMEOUT, total_timeout=DEFAULT_TOTAL_TIMEOUT, max_response_bytes=MAX_RESPONSE_BYTES,
                 ssl_context=None, resolver=None):
        endpoint = parse_endpoint(base_url)
        if api_key is None:
            if not endpoint.loopback:
                raise ma._invalid('Send an API key.')
        elif not isinstance(api_key, ma.ApiKey):
            raise ma._invalid('Send the API key as an ApiKey.')
        if not model_name_ok(model):
            raise ma._invalid('The model name must be 1 to 200 characters from letters, digits, and . _ : / @ + # ~ -')
        if max_tokens_field not in TOKEN_FIELDS:
            raise ma._invalid('max_tokens_field must be max_tokens or max_completion_tokens.')
        if not all(_number(value, 0, MAX_TIMEOUT) for value in (connect_timeout, read_timeout, total_timeout)):
            raise ma._invalid(f'Each timeout must be a number of seconds above 0 and at most {MAX_TIMEOUT:g}.')
        if total_timeout < connect_timeout or total_timeout < read_timeout:
            raise ma._invalid('The total time must be at least the connect time and at least the read time.')
        if isinstance(max_response_bytes, bool) or not isinstance(max_response_bytes, int) or not 1 <= max_response_bytes <= MAX_RESPONSE_BYTES_LIMIT:
            raise ma._invalid(f'max_response_bytes must be a whole number from 1 to {MAX_RESPONSE_BYTES_LIMIT}.')
        if ssl_context is None:
            ssl_context = ssl.create_default_context()
        if not _tls_acceptable(ssl_context):
            raise ma._invalid('The TLS context must check certificates and host names, and it must not cap TLS below 1.2. '
                              'Schola raises its lowest version to TLS 1.2 for you.')
        _raise_tls_floor(ssl_context)
        if not _tls_ok(ssl_context):
            raise ma._invalid('The TLS context must check certificates and host names, and it must not cap TLS below 1.2. '
                              'Schola raises its lowest version to TLS 1.2 for you.')
        if resolver is not None and not callable(resolver):
            raise ma._invalid('The resolver must be a function.')
        super().__init__(allowed_paths=allowed_paths, secrets=() if api_key is None else (api_key,))
        self._endpoint = endpoint
        self._model = model
        self._api_key = api_key
        self._token_field = max_tokens_field
        self._connect_timeout, self._read_timeout, self._total_timeout = connect_timeout, read_timeout, total_timeout
        self._max_bytes = max_response_bytes
        self._ssl_context = ssl_context
        self._resolver = resolver or socket.getaddrinfo

    # ------------------------------------------------------------------
    # The address
    # ------------------------------------------------------------------

    def _lookup(self, wait=None):
        """Resolve the host within the connect time (or `wait`). The lookup is the first step, and the connect time is never above the total.

        A lookup that outlasts the connect time is abandoned on its own thread.
        """
        outcome = {}
        host, port, resolver = self._endpoint.host, self._endpoint.port, self._resolver

        def work():
            try:
                outcome['found'] = resolver(host, port, type=socket.SOCK_STREAM)
            except BaseException:  # nothing of the error is kept
                outcome['failed'] = True

        thread = threading.Thread(target=work, daemon=True)
        thread.start()
        thread.join(self._connect_timeout if wait is None else wait)
        if thread.is_alive():
            raise _timeout()
        if 'found' not in outcome:
            raise ModelAdapterError('Schola could not find the endpoint host.', 'endpoint_unreachable')
        found = _first_fields(outcome['found'])
        if found is None:
            raise _bad('The name lookup gave an answer that Schola cannot read.')
        return found

    def _addresses(self, wait=None):
        """The addresses to connect to, checked. Every address the host resolves to must pass, or the call is refused."""
        host = self._endpoint.host
        listed = [host] if _ip(host) is not None else self._lookup(wait)
        if not listed:
            raise ModelAdapterError('Schola could not find the endpoint host.', 'endpoint_unreachable')
        checked = []
        for text in listed:
            address = _ip(text)
            if address is None or not address_allowed(address, self._endpoint.loopback):
                raise _unsafe('The endpoint host resolved to an address that is not allowed (private, loopback, link-local, or reserved).')
            if text not in checked:
                checked.append(text)
        return checked

    # ------------------------------------------------------------------
    # The call
    # ------------------------------------------------------------------

    def _body(self, request):
        document = {'model': self._model, 'messages': [{'role': message.role, 'content': message.text} for message in request.messages],
                    self._token_field: request.max_output_tokens, 'stream': False}
        return json.dumps(document, ensure_ascii=True, separators=(',', ':')).encode('ascii')

    def _headers(self, has_body=True):
        headers = {'Accept': 'application/json', 'User-Agent': _USER_AGENT, 'Connection': 'close'}
        if has_body:
            headers['Content-Type'] = 'application/json'
        if self._api_key is not None:
            headers['Authorization'] = 'Bearer ' + self._api_key.reveal_for_transport()
        return headers

    def _send(self, request):
        error, raw = self._exchange(request)
        if error is not None:
            raise error  # raised here, outside any handler, so it chains nothing
        return raw

    def _status_error(self, status, path):
        """The error for an HTTP status that is not 200 (or None for 200), for a call to `path`. A subclass can word one status better."""
        return _status_error(status)

    def _classify_error(self, caught):
        """The typed error for a network or protocol exception, or None. A subclass can word one case better; it must not leak."""
        return _classify(caught)

    def _exchange(self, request):
        """The chat call: a POST of the request. Return (error, reply)."""
        return self._roundtrip('POST', '/chat/completions', lambda: self._body(request), request.hold_tokens, self._parse)

    def _roundtrip(self, method, path, body, hold, parse, limits=None):
        """Do one call. Return (error, reply). This never raises for a network or protocol problem.

        `body` is a function that gives the request body, or None for a call with no body. `parse(bytes, hold)` turns a 200 reply
        into (error, reply). `hold` is what a billed failure charges: a call that spends no tokens passes 0.
        `limits` is (read time, total time), or None for the adapter's own. The connect time never goes above the total.

        One monotonic deadline covers the name lookup, each connection try, the TLS handshake, the send, and the reply.
        A request that was sent and then timed out was probably billed, so that timeout charges the whole hold.
        """
        read_timeout, total_timeout = (self._read_timeout, self._total_timeout) if limits is None else limits
        connect_timeout = min(self._connect_timeout, total_timeout)
        deadline = time.monotonic() + total_timeout
        expired = threading.Event()
        connection = timer = None
        error = raw = None
        sent = waited = False

        def expire():
            expired.set()
            sock = connection.live() if connection is not None else None
            if sock is not None:
                for action in (lambda: sock.shutdown(socket.SHUT_RDWR), sock.close):
                    try:
                        action()
                    except OSError:
                        pass

        try:
            timer = threading.Timer(total_timeout, expire)
            timer.daemon = True
            timer.start()
            addresses = self._addresses(connect_timeout)
            endpoint = self._endpoint
            connection = _PinnedConnection(endpoint.host, endpoint.port, addresses, self._ssl_context if endpoint.scheme == 'https' else None,
                                           connect_timeout, read_timeout, deadline)
            connection.request(method, endpoint.path + path, None if body is None else body(), self._headers(body is not None))
            sent = True
            error, raw = self._read(connection, connection.getresponse(), hold, parse, path)
        except MemoryError:
            raise
        except ModelAdapterError as caught:
            error = caught
        except Exception as caught:
            error = self._classify_error(caught)
            if error is None:
                error = self._unexpected(caught)
            elif isinstance(caught, TimeoutError):
                waited = True
        finally:
            if timer is not None:
                timer.cancel()
            if connection is not None:
                connection.close()
        if error is None and raw is not None:
            return None, raw  # a reply that was read in full is a reply, whatever the clock says
        if expired.is_set() or waited:
            return _timeout(hold if sent else 0), None
        return error, raw

    def _read(self, connection, response, hold, parse, path):
        failure = self._status_error(response.status, path)
        if failure is not None:
            return failure, None  # the body of an error reply is never read; nothing is charged
        # From here the provider answered 200, so the call was billed. A reply that cannot be read is charged the whole hold.
        encoding = (response.getheader('Content-Encoding') or 'identity').strip().lower()
        media, charset = _media(response.getheader('Content-Type') or '')
        if encoding != 'identity' or not _json_media(media) or charset not in (None, 'utf-8'):
            return _billed(_bad(), None, hold), None
        declared = response.getheader('Content-Length')
        if declared is not None:
            declared = _length(declared)
            if declared is None:
                return _billed(_bad('The provider sent a length that cannot be read.'), None, hold), None
            if declared > self._max_bytes:
                return _billed(_too_large(), None, hold), None
        pieces, total = [], 0
        try:
            while not response.isclosed():  # a closed response has nothing more to read, and its socket is gone
                connection.arm()
                piece = response.read1(_CHUNK)
                if not piece:
                    break
                total += len(piece)
                if total > self._max_bytes:
                    return _billed(_too_large(), None, hold), None
                pieces.append(piece)
        except MemoryError:
            raise
        except Exception as caught:
            # The body broke in the middle of a reply the provider billed: a reset, a cut chunk, an over-long line, a timeout.
            # A body that was read to its declared length is whole, whatever the socket says afterwards.
            if declared is None or total != declared:
                return _billed(_classify(caught) or _bad(), None, hold), None
        if declared is not None and total != declared:
            return _billed(_bad('The provider reply ended early.'), None, hold), None
        return parse(b''.join(pieces), hold)

    def _parse(self, body, hold):
        document = _load(body)
        if document is None:
            return _billed(_bad(), None, hold), None
        usage = _usage(document)
        if document.get('error') is not None:
            return _body_error(document['error'], usage, lambda status: self._status_error(status, '/chat/completions')), None
        choices = document.get('choices')
        if not isinstance(choices, list):
            return _billed(_bad(), usage, hold), None
        if not choices:
            return _billed(_bad('The provider reply holds no choices.'), usage, hold), None
        choice = choices[0]
        message = choice.get('message') if isinstance(choice, dict) else None
        if not isinstance(message, dict):
            return _billed(_bad(), usage, hold), None
        finish = choice.get('finish_reason')
        text = message.get('content')
        if text is None:
            # No text. A refusal or a cut by the provider's filter is a reply that says so; so is a call to a tool.
            if message.get('refusal') or finish == 'content_filter':
                return None, RawCompletion('', usage, 'content_filter')
            if message.get('tool_calls') or finish in ('tool_calls', 'function_call'):
                return None, RawCompletion('', usage, 'other')
        if not isinstance(text, str) or not ma._encodable(text):
            return _billed(_bad(), usage, hold), None
        return None, RawCompletion(text, usage, _STOP_REASONS.get(finish, 'other') if isinstance(finish, str) else 'other')
