"""The local adapter: a model that runs on the student's own machine, behind an endpoint that follows the OpenAI request format.

    LocalAdapter(base_url, model).complete(request)
    LocalAdapter.preset('ollama', model).complete(request)
    LocalAdapter(base_url).list_models()

This is the API adapter (`api_adapter.py`) with three rules added and five things changed. `docs/local-adapter.md` explains them.

Added
-----
* Loopback only. The host must be `127.0.0.1`, `[::1]`, or `localhost`, and every address it resolves to must be loopback.
  Any other host is `unsafe_endpoint`. The pinned address, the redirect rule, the size cap, and the deadline are the API adapter's.
* No key. The adapter never sends an `Authorization` header. A key passed in is `invalid_request`.
* `list_models()` asks the server which models it offers (`GET <base URL>/models`). It uses no token budget.

Changed
-------
* The default read and total times are longer, because a local model can take minutes to answer.
* Nothing listening at the address (the connection was refused) is `endpoint_unreachable` with one friendly fixed sentence, for a completion and
  for a list. Any other trouble keeps the general sentence, and a timeout stays a timeout.
* `localhost` is not looked up. It means the two fixed addresses `127.0.0.1` and `::1`.
* A status that reads wrong for a keyless local server (401, 403, and 404 on the model list) has a sentence of its own.
* A model name is optional, so a caller can list the models first and choose one after.

The interface, the token budget, the charging rules, and the context guard are the shared ones in `model_adapter.py`.
Only the Python standard library is used.
"""
import dataclasses
import errno
import types

import api_adapter
from api_adapter import ModelAdapterError, _bad, _body_error, _load
import model_adapter as ma

# The default ports of three local runtimes, as far as is known. Nobody checked them against a real runtime here.
PRESETS = types.MappingProxyType({
    'ollama': 'http://127.0.0.1:11434/v1',
    'lmstudio': 'http://127.0.0.1:1234/v1',
    'llamacpp': 'http://127.0.0.1:8080/v1',
})

# A local model can need time to load and to write. A non-streaming call sends nothing until the whole reply is ready,
# so the read time has to cover the whole reply. Still bounded: the total is the largest time the API adapter allows.
DEFAULT_CONNECT_TIMEOUT = 5.0
DEFAULT_READ_TIMEOUT = 300.0
DEFAULT_TOTAL_TIMEOUT = 600.0
# The model list is a short answer. Its read time and total time never go above this, whatever the call limits are.
LIST_SECONDS = 30.0
MAX_LISTED_MODELS = 500

# What `localhost` means. It is never looked up, so a hosts file or a resolver cannot move it. Each address is still checked on every call.
LOCALHOST_ADDRESSES = ('127.0.0.1', '::1')

NOT_LISTENING = 'No local model server answered at that address. Start one, then try again.'
UNSAFE_URL = ('A local model must be at 127.0.0.1, [::1], or localhost (letter case does not matter), with no credentials, query, or fragment. '
              'Use the API adapter for any other host.')
NOT_THIS_COMPUTER = 'localhost resolved to an address that is not this computer.'
NO_MODEL_LIST = 'This server does not offer a model list. Type the model name instead.'
WANTS_A_KEY = 'The local server refused the request (401 or 403). It may want a key, which this adapter does not send, or an access rule may apply.'
NO_USABLE_ID = 'The server listed models, but Schola can use none of the names. Type the model name instead.'
_NO_MODEL = 'Choose a model before you ask for a completion.'
_GENERAL = 'Schola could not reach the endpoint.'
_NAME_FOR_NO_MODEL = 'unset'  # stands in for the model name while the base class checks the rest; it is never sent


@dataclasses.dataclass(frozen=True)
class ModelList:
    """What `list_models` found. `skipped` counts the entries left out (a name Schola cannot use, an entry of the wrong kind, a repeat).
    `truncated` is true when the server listed more usable ids than `MAX_LISTED_MODELS`."""
    ids: tuple
    skipped: int
    truncated: bool


def _usable_id(name):
    """True for a model id Schola can use as a label. An id is untrusted text: it passes the model name rule and does not look like a path or an option."""
    if not api_adapter.model_name_ok(name):
        return False
    # A single letter and a colon is the shape of a drive (`C:/x` or `C:foo`). A longer name before the colon (`llama3:8b`) is a model and a tag.
    if name[0] in '/~-' or any(part in ('.', '..') for part in name.split('/')) or (len(name) > 1 and name[0].isalpha() and name[1] == ':'):
        return False
    return True


def _models(data):
    """The `ModelList` of a `data` list: the usable ids in order, without repeats, at most `MAX_LISTED_MODELS`."""
    found, seen, skipped, truncated = [], set(), 0, False
    for entry in data:
        name = entry.get('id') if isinstance(entry, dict) else None
        if not _usable_id(name) or name in seen:
            skipped += 1
        elif len(found) == MAX_LISTED_MODELS:
            truncated = True  # one more usable id than the list holds; the loop goes on to count the rest of the unusable entries
        else:
            seen.add(name)
            found.append(name)
    return ModelList(tuple(found), skipped, truncated)


def _fit(default, total):
    """`default`, or `total` when that is less. A total that is not a valid number leaves the default, and the base class refuses the total."""
    return min(default, total) if api_adapter._number(total, 0, api_adapter.MAX_TIMEOUT) else default


class LocalAdapter(api_adapter.ApiAdapter):
    """Call an OpenAI-format chat endpoint on this machine.

    `model` may be left out. Such an adapter can `list_models()` and cannot `complete`.
    The other options are the API adapter's, and mean the same. `ssl_context` matters only for an `https` loopback URL.
    """

    def __init__(self, base_url, model=None, api_key=None, *, allowed_paths=(), max_tokens_field='max_tokens', connect_timeout=None,
                 read_timeout=None, total_timeout=None, max_response_bytes=api_adapter.MAX_RESPONSE_BYTES, ssl_context=None):
        if api_key is not None:
            # Nothing of the value goes in the message.
            raise ma._invalid('A local model needs no API key, and Schola sends none.')
        failure = endpoint = None
        try:
            endpoint = api_adapter.parse_endpoint(base_url)
        except ModelAdapterError as caught:
            failure = caught
        if failure is not None:
            raise api_adapter._unsafe(UNSAFE_URL) if failure.code == 'unsafe_endpoint' else failure
        if not endpoint.loopback:
            raise api_adapter._unsafe(UNSAFE_URL)
        # A default that does not fit under a total the caller set is lowered to fit. A value the caller set is never changed.
        if total_timeout is None:
            total_timeout = DEFAULT_TOTAL_TIMEOUT
        if connect_timeout is None:
            connect_timeout = _fit(DEFAULT_CONNECT_TIMEOUT, total_timeout)
        if read_timeout is None:
            read_timeout = _fit(DEFAULT_READ_TIMEOUT, total_timeout)
        super().__init__(base_url, _NAME_FOR_NO_MODEL if model is None else model, None, allowed_paths=allowed_paths, max_tokens_field=max_tokens_field,
                         connect_timeout=connect_timeout, read_timeout=read_timeout, total_timeout=total_timeout, max_response_bytes=max_response_bytes,
                         ssl_context=ssl_context)
        self._model = model

    @classmethod
    def preset(cls, name, model=None, **options):
        """An adapter for one of the `PRESETS`. Other options (timeouts, `allowed_paths`, and so on) go to the constructor."""
        url = PRESETS.get(name) if isinstance(name, str) else None
        if url is None:
            raise ma._invalid('Unknown local runtime. Use one of: ' + ', '.join(PRESETS) + '.')
        return cls(url, model, **options)

    def complete(self, request):
        if not isinstance(request, ma.CompletionRequest):
            raise ma._invalid('Send a CompletionRequest.')
        if self._model is None:
            raise ma._invalid(_NO_MODEL)
        return super().complete(request)

    def _body(self, request):
        if self._model is None:
            raise ma._invalid(_NO_MODEL)  # a second guard, where the model name goes into the request
        return super()._body(request)

    def list_models(self):
        """The models the server offers, as a `ModelList`. It spends no tokens and holds nothing in a budget.

        A server that sends something else than a list of models gives `bad_response`, and so does a server that lists models and
        no usable name. Entries that Schola cannot use are left out and counted in `skipped`.
        """
        limits = (min(self._read_timeout, LIST_SECONDS), min(self._total_timeout, LIST_SECONDS))
        error, found = self._roundtrip('GET', '/models', None, 0, self._parse_models, limits)
        if error is not None:
            raise error  # raised here, outside any handler, so it chains nothing
        return found

    def _parse_models(self, body, hold):
        document = _load(body)
        if document is None:
            return _bad(), None
        if document.get('error') is not None:
            return _body_error(document['error'], None, lambda status: self._status_error(status, '/models')), None
        data = document.get('data')
        if not isinstance(data, list):
            return _bad(), None
        found = _models(data)
        if data and not found.ids:
            return _bad(NO_USABLE_ID), None  # a list of only unusable names must not look like "no models"
        return None, found

    def _lookup(self, wait=None):
        if self._endpoint.host == 'localhost':
            return list(LOCALHOST_ADDRESSES)
        return super()._lookup(wait)

    def _addresses(self, wait=None):
        failure = found = None
        try:
            found = super()._addresses(wait)
        except ModelAdapterError as caught:
            failure = caught
        if failure is not None:
            raise api_adapter._unsafe(NOT_THIS_COMPUTER) if failure.code == 'unsafe_endpoint' else failure
        return found

    def _status_error(self, status, path):
        if status in (401, 403):
            return ModelAdapterError(WANTS_A_KEY, 'auth_failed')
        if status == 404 and path == '/models':
            return ModelAdapterError(NO_MODEL_LIST, 'invalid_request')
        return super()._status_error(status, path)

    def _classify_error(self, caught):
        if isinstance(caught, api_adapter.ConnectFailed):
            if caught.refused:
                return ModelAdapterError(NOT_LISTENING, 'endpoint_unreachable', retryable=True)
            # A refusal says nothing is listening. Other trouble does not say that. A denied connect will not work on a second try.
            return ModelAdapterError(_GENERAL, 'endpoint_unreachable', retryable=caught.errno not in (errno.EACCES, errno.EPERM))
        return super()._classify_error(caught)
