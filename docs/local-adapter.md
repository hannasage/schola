# The local adapter

This page describes step E4. It covers the local adapter: the part of Schola that talks to a model that runs on the student's own computer.
It says what the code does, what it refuses, and what it does not prove.
Read `docs/model-adapters.md` first. This adapter builds on the API adapter described there, and it follows the same interface, token budget, charging rules, and context guard.

The code is `local_adapter.py`. It uses `api_adapter.py` and `model_adapter.py`.
The tests are `tests/test_local_adapter.py` and `tests/test_api_adapter.py`.
The code uses only the Python standard library. It needs Python 3.10 or later.
The test files passed on Python 3.10, 3.12, and 3.14, on macOS.
Nothing calls this adapter yet. No route, screen, or agent loop uses it in this step.

## Read this first: no real runtime was tested

No real local runtime was started or called, and no model was downloaded. The tests use a fake model server that runs inside the test, on a loopback port.
Nobody checked whether Ollama, LM Studio, or the llama.cpp server accepts the request that this adapter sends. Nobody checked the shape of their replies either.
The three preset ports below are the default ports of those runtimes as far as is known. They are not checked here.
A runtime can differ in the name of the output limit field. It can differ in the usage fields and in the `/models` answer. It can also differ in how it treats a model that is not loaded yet.

## Terms

- A local model is a model that runs on the student's own computer, not on a provider's servers.
- A runtime is the program that loads a local model and answers requests. Ollama, LM Studio, and the llama.cpp server are runtimes.
- A loopback address leads back to the same computer: `127.0.0.1`, `::1`, or the name `localhost`.
- A port is a number that picks one program among the programs that listen on one computer. A runtime listens on a default port.
- A preset is a stored base URL for one runtime's default port.
- The model list is the answer to the question "which models do you offer?" The server sends a list of model ids.
- A model id is the name that a request uses to pick one model.

## Use

```python
adapter = LocalAdapter.preset('ollama', 'llama3.2:latest')    # or LocalAdapter('http://127.0.0.1:11434/v1', 'llama3.2:latest')
result = adapter.complete(request)                          # the same call as every adapter

chooser = LocalAdapter.preset('ollama')                     # no model yet
names = chooser.list_models()                               # a tuple of model ids
```

`LocalAdapter(base_url, model=None, api_key=None, *, ...)` takes the same options as the API adapter: `allowed_paths`, `max_tokens_field`, `connect_timeout`, `read_timeout`, `total_timeout`, `max_response_bytes`, `ssl_context`, and `resolver`.
The call and the result are the same as in `docs/model-adapters.md`. The adapter posts to `<base URL>/chat/completions`.
`LocalAdapter` is a subclass of `ApiAdapter`. A check for the API adapter's type is also true for a local adapter.

## What this adapter adds

### Loopback only

If the host is not `127.0.0.1`, `[::1]`, or `localhost`, the adapter refuses it when the adapter is made. The error code is `unsafe_endpoint`.
This covers every public host and every private address too. Names that only look like a loopback host are refused: `127.0.0.2`, `localhost.example.com`, `127.1`, and `localhost.` (with a final dot).
The API adapter accepts remote hosts. For a remote provider, use the API adapter.

The address rules of the API adapter still run at every call.

- Schola looks up the host and checks every address. Each one must be loopback. One bad address refuses the host. If a hosts file points `localhost` at another machine, Schola refuses it.
- Schola connects to the address it checked, not to the name. A second lookup cannot send the call somewhere else.
- Schola never follows a redirect.
- Schola ignores the proxy settings of the environment.

Loopback does not mean trusted. Any program on the same computer can listen on a port, and on a shared computer another user can too.
Schola does not check who listens. It sends the messages to whatever answers on that port.
The context guard still decides what the messages can hold. The reply is output from a program that Schola did not check. Treat it as untrusted text.

`https` on a loopback host works the way it does in the API adapter. The certificate must pass the check.
A runtime that uses a self-made certificate fails the check. The caller can pass an `ssl_context` that trusts that one certificate.
The local adapter adds nothing for this, and no test covers it for this adapter. The TLS tests of the API adapter cover the shared code. Local runtimes normally use plain `http`.

### No key

The adapter never sends an `Authorization` header and holds no key.
If the caller passes an `api_key`, the adapter refuses with `invalid_request`. The message does not repeat the value.
A local server that asks for a key is not supported. Such a server answers HTTP 401 or 403, and the adapter reports `auth_failed`. That message talks about a key. Treat it as "this server wants a key".

### Presets

`LocalAdapter.preset(name, model=None, **options)` returns an adapter for a stored base URL. The other options go to the constructor.
The presets are plain data in `local_adapter.PRESETS`. The dictionary is read-only.

- `ollama`: `http://127.0.0.1:11434/v1`
- `lmstudio`: `http://127.0.0.1:1234/v1`
- `llamacpp`: `http://127.0.0.1:8080/v1`

These ports are not checked here (see the first section). A student can change the port in a runtime. Then the preset does not fit, and the caller passes the base URL itself.
An unknown name gives `invalid_request`. The name is case sensitive. The message lists the known names and does not repeat what the caller sent.

### The model list

`list_models()` sends `GET <base URL>/models`. It expects the OpenAI format: `{"data": [{"id": "..."}]}`.
It returns a tuple of ids, in the order the server gave them.

- It takes no argument. It uses no token budget, holds nothing, and never charges anything. A failed list has `tokens_charged` of 0.
- It is a separate method from `complete`. The two can run at the same time.
- An id that the model name rule refuses is left out, because the adapter cannot use it. The rule is the API adapter's: 1 to 200 characters from letters, digits, and `. _ : / @ + # ~ -`.
  A name with a space, such as a file path with a space in it, is left out for this reason.
- An entry that is not an object with a text `id` is left out too. A repeated id is listed once.
- The list holds at most 500 ids. Schola drops the rest without a message.
- If the reply is not one JSON object with a `data` list, the call fails with `bad_response`. An `error` object in a 200 reply fails like it does for a completion.
- A server that offers no usable model gives an empty tuple. That is a valid answer.

The list has the same limits as a completion: the reply body cap (1 MB by default), no redirect, a fixed error sentence, and one deadline for the whole call.
The list is a short answer. Its read time and its total time never go above 30 seconds. This holds even when the call limits are longer.
A shorter call limit also shortens the list.

An adapter made without a model can list models and cannot complete. `complete` raises `invalid_request` before it touches the budget.
This lets a caller list the models first and choose one after.

### Time limits

A local model can take minutes to load and to write a reply. A call that asks for a whole reply (not a stream) gets nothing back until the model finishes. So the read time has to cover the whole reply.
The defaults are longer than the API adapter's. They are still bounded, and the same checks apply.

- Connect time: 5 seconds. A runtime on the same computer answers a connect at once or refuses it.
- Read time: 300 seconds.
- Total time for the whole call: 600 seconds. This is the largest value that the API adapter allows.

The caller can set all three, up to 600 seconds. The total must be at least the connect time and at least the read time.
The numbers are guesses. Nobody measured a real runtime. A large model on a slow computer can need more than 300 seconds for a long reply. Then the call fails with `timeout`.
`max_output_tokens` and the token budget work exactly as in `docs/model-adapters.md`.

### Nothing is listening

If no program listens at the address, the call fails with `endpoint_unreachable` and this fixed sentence:

> No local model server answered at that address. Start one, then try again.

`retryable` is true. The sentence holds no address and no port. It applies to `list_models` and to `complete`.
It applies to a connect that fails on every address for a reason other than time.
These cases keep the sentences of the API adapter: a name that does not resolve, a connection that breaks after it was made, a TLS fault, and an HTTP 5xx.
A failed connect sent nothing, so it charges nothing.

## What a call costs

The charging rules are the ones in `docs/model-adapters.md`. This adapter adds none.

- The hold is the input estimate plus `max_output_tokens`. If it does not fit under the cap, the call gets `token_cap_reached` and Schola sends nothing.
- Local servers often leave out the usage numbers. A reply with no usable usage is charged the whole hold. The result says so (`usage_reported` is false).
- A reply that arrives but cannot be used is charged as the API adapter charges it. A timeout after the request was sent is charged the whole hold.
- A local model costs no money, but the token cap still limits how much a session uses.

## What changed in the shared code

Four small changes in `api_adapter.py` let the local adapter reuse the network code instead of copying it. Each has a test in `tests/test_api_adapter.py` (class `TransportSeamTests`).
The API adapter behaves as before.

- `_roundtrip` does one call for any method and path. The chat call (`_exchange`) now uses it. A call with no body sends no `Content-Type`. A call can carry its own read and total limits, and a call that spends no tokens passes a hold of 0.
- `_classify_error` is a hook. A subclass can word one network error better. The default is the old behavior.
- `ConnectFailed` is an error for a connect that failed on every address for a reason other than time. It is an `OSError`, so the API adapter treats it as before.
- `model_name_ok` is the model name rule as one function. The constructor and the model list both use it.
- The connect time and the name lookup time are now cut to the total time of the call. For the chat call this changes nothing, because the total was always at least the connect time.

## Limits and what is not checked

- No real runtime was called (see the first section). The preset ports, the request fields, the usage fields, and the `/models` shape are not checked against Ollama, LM Studio, and the llama.cpp server.
- A runtime can answer `/models` in another shape. Then `list_models` fails with `bad_response` or gives a shorter list.
- Streaming is not supported. The adapter always asks for a whole reply.
- The default timeouts are guesses.
- Another program on a preset port gets the messages. Schola cannot tell it from a runtime.
- Windows and Linux are untested. The tests ran on macOS. The continuous integration run on Linux checks the test files there.
- `https` on loopback with a self-made certificate has no special support and no test for this adapter.
- The adapter does not retry. `retryable` is information for the caller, and the caller needs its own limit on attempts.
- The adapter does not start a runtime and does not download a model.

## Not built in this step

- Starting, stopping, or installing a runtime, and downloading a model.
- Any route, screen, or wiring in `server.py`.
- Choosing a model for the student. That belongs to the screens and the policy step.
- The offer rate limit, the policy evaluator, and the agent loop (steps E5 and E6).
- Key storage and the default token cap. The decisions are still open.
