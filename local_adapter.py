"""The local adapter: a model that runs on the student's own machine, behind an endpoint that follows the OpenAI request format.

    LocalAdapter(base_url, model).complete(request)
    LocalAdapter.preset('ollama', model).complete(request)
    LocalAdapter(base_url).list_models()

This is the API adapter (`api_adapter.py`) with three rules added and three things changed. `docs/local-adapter.md` explains them.

Added
-----
* Loopback only. The host must be `127.0.0.1`, `[::1]`, or `localhost`, and every address it resolves to must be loopback.
  Any other host is `unsafe_endpoint`. The pinned address, the redirect rule, the size cap, and the deadline are the API adapter's.
* No key. The adapter never sends an `Authorization` header. A key passed in is `invalid_request`.
* `list_models()` asks the server which models it offers (`GET <base URL>/models`). It uses no token budget.

Changed
-------
* The default read and total times are longer, because a local model can take minutes to answer.
* Nothing listening at the address is `endpoint_unreachable` with one friendly fixed sentence, for a completion and for a list.
* A model name is optional, so a caller can list the models first and choose one after.

The interface, the token budget, the charging rules, and the context guard are the shared ones in `model_adapter.py`.
Only the Python standard library is used.
"""
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

NOT_LISTENING = 'No local model server answered at that address. Start one, then try again.'
_NO_MODEL = 'Choose a model before you ask for a completion.'
_UNSAFE_HOST = 'A local model must be at a loopback address: 127.0.0.1, [::1], or localhost. Use the API adapter for any other host.'
_NAME_FOR_NO_MODEL = 'unset'  # stands in for the model name while the base class checks the rest; it is never sent


def _models(data):
    """The usable model ids of a `data` list, in order, without repeats, at most `MAX_LISTED_MODELS`.

    An entry that is not an object with a text `id` that passes the model name rule is left out: Schola could not use it.
    """
    found, seen = [], set()
    for entry in data:
        name = entry.get('id') if isinstance(entry, dict) else None
        if api_adapter.model_name_ok(name) and name not in seen:
            seen.add(name)
            found.append(name)
            if len(found) == MAX_LISTED_MODELS:
                break
    return tuple(found)


class LocalAdapter(api_adapter.ApiAdapter):
    """Call an OpenAI-format chat endpoint on this machine.

    `model` may be left out. Such an adapter can `list_models()` and cannot `complete`.
    The other options are the API adapter's, and mean the same. `ssl_context` matters only for an `https` loopback URL.
    """

    def __init__(self, base_url, model=None, api_key=None, *, allowed_paths=(), max_tokens_field='max_tokens', connect_timeout=DEFAULT_CONNECT_TIMEOUT,
                 read_timeout=DEFAULT_READ_TIMEOUT, total_timeout=DEFAULT_TOTAL_TIMEOUT, max_response_bytes=api_adapter.MAX_RESPONSE_BYTES,
                 ssl_context=None, resolver=None):
        if api_key is not None:
            # Nothing of the value goes in the message.
            raise ma._invalid('A local model needs no API key, and Schola sends none.')
        if not api_adapter.parse_endpoint(base_url).loopback:
            raise api_adapter._unsafe(_UNSAFE_HOST)
        super().__init__(base_url, _NAME_FOR_NO_MODEL if model is None else model, None, allowed_paths=allowed_paths, max_tokens_field=max_tokens_field,
                         connect_timeout=connect_timeout, read_timeout=read_timeout, total_timeout=total_timeout, max_response_bytes=max_response_bytes,
                         ssl_context=ssl_context, resolver=resolver)
        self._model = model

    @classmethod
    def preset(cls, name, model=None, **options):
        """An adapter for one of the `PRESETS`. Other options (timeouts, `allowed_paths`, and so on) go to the constructor."""
        url = PRESETS.get(name) if isinstance(name, str) else None
        if url is None:
            raise ma._invalid('Unknown local runtime. Use one of: ' + ', '.join(PRESETS) + '.')
        return cls(url, model, **options)

    def complete(self, request):
        if self._model is None:
            raise ma._invalid(_NO_MODEL)
        return super().complete(request)

    def list_models(self):
        """The ids of the models the server offers, as a tuple. It spends no tokens and holds nothing in a budget.

        A server that sends something else than a list of models gives `bad_response`. Ids that Schola could not use are left out.
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
            return _body_error(document['error'], None), None
        data = document.get('data')
        if not isinstance(data, list):
            return _bad(), None
        return None, _models(data)

    def _classify_error(self, caught):
        if isinstance(caught, api_adapter.ConnectFailed):
            return ModelAdapterError(NOT_LISTENING, 'endpoint_unreachable', retryable=True)
        return super()._classify_error(caught)
