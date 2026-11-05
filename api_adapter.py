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
* A connect timeout, a read timeout for each wait, and a total time for the whole call.
* The reply body is read in pieces and refused once it passes the cap (1 MB by default).
* The reply must be one JSON object of the expected shape. Anything else is `bad_response`.

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
                                      or address.is_reserved or address.is_unspecified)


class _PinnedConnection(http.client.HTTPConnection):
    """A connection that goes to an address the adapter already checked, and keeps the host name for the Host header and TLS."""

    def __init__(self, host, port, addresses, context, connect_timeout, read_timeout):
        super().__init__(host, port, timeout=connect_timeout)
        self._addresses, self._context, self._read_timeout = addresses, context, read_timeout

    def connect(self):
        last = None
        for address in self._addresses:
            try:
                sock = socket.create_connection((address, self.port), self.timeout)
            except OSError as error:
                last = error
                continue
            break
        else:
            raise last
        if self._context is not None:
            try:
                sock = self._context.wrap_socket(sock, server_hostname=self.host)
            except BaseException:
                sock.close()
                raise
        sock.settimeout(self._read_timeout)
        self.sock = sock


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


def _usage(document):
    """Usage from the reply, or None when it is missing or not two counts. The caller then counts the request maximum."""
    usage = document.get('usage')
    if not isinstance(usage, dict):
        return None
    counts = (usage.get('prompt_tokens'), usage.get('completion_tokens'))
    if any(isinstance(count, bool) or not isinstance(count, int) or not 0 <= count <= ma.MAX_TOKEN_COUNT for count in counts):
        return None
    return Usage(*counts)


def _media(header):
    """(media type, charset) of a Content-Type header. The charset is None when it is not given."""
    pieces = [piece.strip() for piece in header.split(';')]
    charset = None
    for piece in pieces[1:]:
        name, _, value = piece.partition('=')
        if name.strip().lower() == 'charset':
            charset = value.strip().strip('"').lower()
    return pieces[0].lower(), charset


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
    if 500 <= status < 600:
        return ModelAdapterError(f'The provider had a server error (HTTP {status}).', 'endpoint_unreachable')
    if 400 <= status < 500:
        return ModelAdapterError(f'The provider refused the request (HTTP {status}). Check the base URL and the model name.', 'invalid_request')
    return ModelAdapterError(f'The provider answered with an unexpected status (HTTP {status}).', 'bad_response')


def _timeout():
    return ModelAdapterError('The provider did not answer in time.', 'timeout')


def _too_large():
    return ModelAdapterError('The provider reply is larger than Schola allows.', 'response_too_large')


def _bad(message='The provider reply was not in the expected format.'):
    return ModelAdapterError(message, 'bad_response')


def _classify(error):
    """Turn a network or protocol exception into a typed error with a fixed sentence. Nothing of `error` goes in the sentence."""
    if isinstance(error, ssl.SSLCertVerificationError):
        return ModelAdapterError('The TLS certificate of the endpoint could not be verified.', 'endpoint_unreachable', retryable=False)
    if isinstance(error, ssl.SSLError):
        return ModelAdapterError('Schola could not make a secure (TLS) connection to the endpoint.', 'endpoint_unreachable', retryable=False)
    if isinstance(error, TimeoutError):
        return _timeout()
    if isinstance(error, OSError):
        return ModelAdapterError('Schola could not reach the endpoint.', 'endpoint_unreachable')
    if isinstance(error, (http.client.HTTPException, ValueError)):
        return _bad()
    return _bad('The model adapter failed in an unexpected way.')


class ApiAdapter(ma.ModelAdapter):
    """Call an OpenAI-format chat endpoint.

    `max_tokens_field` is the name of the output limit in the request: `max_tokens` (the common one) or `max_completion_tokens`.
    `ssl_context` must check certificates and host names. `resolver` replaces `socket.getaddrinfo`; tests use it.
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
        if not isinstance(model, str) or len(model) > MAX_MODEL_CHARS or not _MODEL.fullmatch(model):
            raise ma._invalid('The model name must be 1 to 200 characters from letters, digits, and . _ : / @ + # ~ -')
        if max_tokens_field not in TOKEN_FIELDS:
            raise ma._invalid('max_tokens_field must be max_tokens or max_completion_tokens.')
        if not all(_number(value, 0, MAX_TIMEOUT) for value in (connect_timeout, read_timeout, total_timeout)):
            raise ma._invalid(f'Each timeout must be a number of seconds above 0 and at most {MAX_TIMEOUT:g}.')
        if isinstance(max_response_bytes, bool) or not isinstance(max_response_bytes, int) or not 1 <= max_response_bytes <= MAX_RESPONSE_BYTES_LIMIT:
            raise ma._invalid(f'max_response_bytes must be a whole number from 1 to {MAX_RESPONSE_BYTES_LIMIT}.')
        if ssl_context is None:
            ssl_context = ssl.create_default_context()
        elif not isinstance(ssl_context, ssl.SSLContext) or ssl_context.verify_mode != ssl.CERT_REQUIRED or not ssl_context.check_hostname:
            raise ma._invalid('The TLS context must check certificates and host names.')
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

    def _lookup(self):
        """Resolve the host with a time limit. A lookup that outlasts the connect timeout is abandoned on its own thread."""
        outcome = {}
        host, port, resolver = self._endpoint.host, self._endpoint.port, self._resolver

        def work():
            try:
                outcome['found'] = resolver(host, port, type=socket.SOCK_STREAM)
            except BaseException:  # nothing of the error is kept
                outcome['failed'] = True

        thread = threading.Thread(target=work, daemon=True)
        thread.start()
        thread.join(self._connect_timeout)
        if thread.is_alive():
            raise _timeout()
        if 'found' not in outcome:
            raise ModelAdapterError('Schola could not find the endpoint host.', 'endpoint_unreachable')
        return _first_fields(outcome['found'])  # None for an answer of the wrong shape; the caller treats it like an empty list

    def _addresses(self):
        """The addresses to connect to, checked. Every address the host resolves to must pass, or the call is refused."""
        host = self._endpoint.host
        listed = [host] if _ip(host) is not None else self._lookup()
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

    def _headers(self):
        headers = {'Content-Type': 'application/json', 'Accept': 'application/json',
                   'User-Agent': _USER_AGENT, 'Connection': 'close'}
        if self._api_key is not None:
            headers['Authorization'] = 'Bearer ' + self._api_key.reveal_for_transport()
        return headers

    def _send(self, request):
        error, raw = self._exchange(request)
        if error is not None:
            raise error  # raised here, outside any handler, so it chains nothing
        return raw

    def _exchange(self, request):
        """Do the call. Return (error, reply). This never raises for a network or protocol problem."""
        expired = threading.Event()
        connection = timer = None
        error = raw = None
        try:
            addresses = self._addresses()
            endpoint = self._endpoint
            connection = _PinnedConnection(endpoint.host, endpoint.port, addresses, self._ssl_context if endpoint.scheme == 'https' else None,
                                           self._connect_timeout, self._read_timeout)

            def expire():
                expired.set()
                sock = connection.sock
                if sock is not None:
                    try:
                        sock.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass

            timer = threading.Timer(self._total_timeout, expire)
            timer.daemon = True
            timer.start()
            connection.request('POST', endpoint.path + '/chat/completions', self._body(request), self._headers())
            error, raw = self._read(connection.getresponse())
        except ModelAdapterError as caught:
            error = caught
        except Exception as caught:
            error = _classify(caught)
        finally:
            if timer is not None:
                timer.cancel()
            if connection is not None:
                connection.close()
        if expired.is_set():
            return _timeout(), None
        return error, raw

    def _read(self, response):
        failure = _status_error(response.status)
        if failure is not None:
            return failure, None  # the body of an error reply is never read
        encoding = (response.getheader('Content-Encoding') or 'identity').strip().lower()
        media, charset = _media(response.getheader('Content-Type') or '')
        if encoding != 'identity' or not (media == 'application/json' or media.endswith('+json')) or charset not in (None, 'utf-8'):
            return _bad(), None
        declared = response.getheader('Content-Length')
        declared = int(declared) if declared is not None and declared.strip().isdigit() else None
        if declared is not None and declared > self._max_bytes:
            return _too_large(), None
        pieces, total = [], 0
        while True:
            piece = response.read(_CHUNK)
            if not piece:
                break
            total += len(piece)
            if total > self._max_bytes:
                return _too_large(), None
            pieces.append(piece)
        if declared is not None and total != declared:
            return _bad('The provider reply ended early.'), None
        return self._parse(b''.join(pieces))

    def _parse(self, body):
        try:
            document = json.loads(body.decode('utf-8'), parse_int=_parse_int, parse_constant=_reject_constant)
            choice = document['choices'][0]
            text = choice['message']['content']
            finish = choice.get('finish_reason')
            if not isinstance(text, str):
                return _bad(), None
            text.encode('utf-8')
        except (ValueError, TypeError, KeyError, IndexError, RecursionError):
            return _bad(), None
        return None, RawCompletion(text, _usage(document), _STOP_REASONS.get(finish, 'other') if isinstance(finish, str) else 'other')
