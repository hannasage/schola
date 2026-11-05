# The local adapter

This page describes step E4. It covers the local adapter: the part of Schola that talks to a model that runs on the student's own computer.
It says what the code does, what it refuses, and what it does not prove.
Read `docs/model-adapters.md` first. This adapter builds on the API adapter described there. It follows the same interface, token budget, charging rules, and context guard.

The code is `local_adapter.py`. It uses `api_adapter.py` and `model_adapter.py`.
The tests are `tests/test_local_adapter.py` and `tests/test_api_adapter.py`.
The code uses only the Python standard library. It needs Python 3.10 or later.
The three test modules (`test_local_adapter`, `test_api_adapter`, and `test_model_adapter`) passed on Python 3.10, 3.12, 3.13, and 3.14, on macOS.
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
found = chooser.list_models()                               # a ModelList
found.ids, found.skipped, found.truncated                   # a tuple of ids, a count, and a flag
```

`LocalAdapter(base_url, model=None, api_key=None, *, ...)` takes these options from the API adapter: `allowed_paths`, `max_tokens_field`, `connect_timeout`, `read_timeout`, `total_timeout`, `max_response_bytes`, and `ssl_context`.
It has no `resolver` option, because it never looks up a name (see "Loopback only").
The call and the result are the same as in `docs/model-adapters.md`. The adapter posts to `<base URL>/chat/completions`.
`LocalAdapter` is a subclass of `ApiAdapter`. A check for the API adapter's type is also true for a local adapter.

## What this adapter adds

### Loopback only

The host must be one of three spellings: `127.0.0.1`, `[::1]`, or `localhost`. Letter case does not matter.
The constructor refuses any other host at once. The error code is `unsafe_endpoint`.
That covers every public host and every private address. It also covers other loopback addresses, such as `127.0.0.2`.
It covers other spellings of the same address too: `127.1`, `[::ffff:127.0.0.1]`, and `localhost.` (with a final dot).
Schola refuses those on purpose. It accepts only the three spellings, so there is no list of look-alikes to keep up to date.
The API adapter accepts remote hosts. For a remote provider, use the API adapter.
The message for a refused URL is about a local server. It does not suggest `https`.

Any port and any path prefix on those hosts are accepted. The student picks them, and a runtime can listen on any port.

The address rules still run at every call.

- `localhost` is not looked up. It means two fixed addresses: `127.0.0.1` and `::1`. A hosts file or a resolver cannot move it.
  So the thread that a lookup can leave behind (see the limits in `docs/model-adapters.md`) does not apply to this adapter.
- The other two spellings are already numbers, so they need no lookup either.
- Schola checks each address on every call. Each one must be loopback. One bad address refuses the call with the message "localhost resolved to an address that is not this computer."
  With the fixed addresses this check cannot fail. It stays as a guard against a later change.
- Schola never follows a redirect.
- Schola ignores the proxy settings of the environment.

Loopback does not mean trusted. Any program on the same computer can listen on a port, and on a shared computer another user can too.
A program that listens on a preset port before the runtime does receives the messages that Schola sends to that port. Schola does not check who listens.
The context guard still decides what the messages can hold. The reply is output from a program that Schola did not check. Treat it as untrusted text.

`https` on a loopback host works the way it does in the API adapter. The certificate must pass the check.
A runtime that uses a self-made certificate fails the check. The caller can pass an `ssl_context` that trusts that one certificate.
The local adapter adds nothing for this, and no test covers it for this adapter. The TLS tests of the API adapter cover the shared code. Local runtimes normally use plain `http`.

### No key

The adapter never sends an `Authorization` header and holds no key.
If the caller passes an `api_key`, the adapter refuses with `invalid_request`. The message does not repeat the value.
A local server that asks for a key is not supported. Such a server answers HTTP 401 or 403. The adapter reports `auth_failed` with a fixed sentence.
The sentence names the status (401 or 403). It says that a key or an access rule can be the cause, and that this adapter sends no key.

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
It returns a `ModelList`. This is a frozen record with three fields:

- `ids`: a tuple of model ids, in the order the server gave them.
- `skipped`: how many entries Schola left out.
- `truncated`: true for a server that listed more usable ids than Schola keeps.

The method takes no argument. It uses no token budget, holds nothing, and never charges anything. A failed list has `tokens_charged` of 0.
It is a separate method from `complete`. The two can run at the same time.

Schola leaves an entry out, and counts it in `skipped`, in these cases:

- The entry is not an object with a text `id`.
- The id fails the model name rule. The rule is the API adapter's: 1 to 200 characters from letters, digits, and `. _ : / @ + # ~ -`.
- The id repeats an earlier id.
- The id looks like a path or an option. This is the rule for an untrusted label (see below).

An id is an untrusted label. The server chose it. A later step can show it to the student or put it in a request.
Use an id as a display label and as a model name in a request. Never use it as a path.
So Schola also leaves out an id that:

- starts with `/`, `~`, or `-`
- has a segment that is `.` or `..` (`./x`, `a/./b`, `../x`, `a/../b`, or `a/..`)
- starts with one letter and a colon, such as `C:/models/x` or `C:foo`

Schola tells a drive from a model and a tag by the length of the part before the first colon. One letter is the only shape of a drive. So `C:foo` and `c:x` are left out, and `llama3:8b` and `qwen2.5:7b` pass.
A model that really has a one-letter name before the colon is left out too.
Ids such as `hf.co/user/model` and `a..b` pass. A dot inside a segment is fine. The check reads the id only. It does not open any path.

The list holds at most 500 ids. When the server lists more usable ids, `truncated` is true. Schola drops the rest.

If the reply is not one JSON object with a `data` list, the call fails with `bad_response`. An `error` object in a 200 reply fails like it does for a completion.
A server that lists nothing (`data` is empty) gives an empty `ids` with `skipped` of 0. That is a valid answer.
A server that lists models but no usable name fails with `bad_response` and this sentence:

> The server listed models, but Schola can use none of the names. Type the model name instead.

This keeps a list of only unusable names from looking like "no models".

The llama.cpp server reports the file path of each model as its id. A path often has a slash at the start, and it can hold a space.
Schola leaves out both kinds. A student of that runtime then sees `skipped` above 0, or the error above, and has to type the model name.
Even a typed name must pass the model name rule, so a name with a space is not usable. Whether to relax this rule is a decision for the maintainer.

The list has the same limits as a completion: the reply body cap (1 MB by default), no redirect, a fixed error sentence, and one deadline for the whole call.
The list is a short answer. Its read time and its total time never go above 30 seconds. This holds for long call limits too.
A shorter call limit also shortens the list.

A status that reads wrong for a local server has its own sentence. A 404 on `/models` gives `invalid_request` with this sentence:

> This server does not offer a model list. Type the model name instead.

An adapter made without a model can list models and cannot complete. `complete` raises `invalid_request` before it touches the budget.
The model name is checked in two places: at the start of `complete`, and where the request body is built.
This lets a caller list the models first and choose one after.

### Time limits

A local model can take minutes to load and to write a reply. A call that asks for a whole reply (not a stream) gets nothing back until the model finishes. So the read time has to cover the whole reply.
The defaults are longer than the API adapter's. They are still bounded, and the same checks apply.

- Connect time: 5 seconds. A runtime on the same computer answers a connect at once or refuses it.
- Read time: 300 seconds.
- Total time for the whole call: 600 seconds. This is the largest value that the API adapter allows.

The caller can set all three, up to 600 seconds. The total must be at least the connect time and at least the read time.
A caller can set only the total, for example `total_timeout=30`. Then Schola lowers the connect and read defaults to fit under it (5 and 30 seconds).
A value that the caller sets is never lowered. If the caller sets a read time above the total, the adapter is refused.
The numbers are guesses. Nobody measured a real runtime. A large model on a slow computer can need more than 300 seconds for a long reply. Then the call fails with `timeout`.
`max_output_tokens` and the token budget work exactly as in `docs/model-adapters.md`.

### Nothing is listening

If every connect try is refused, the call fails with `endpoint_unreachable` and this fixed sentence:

> No local model server answered at that address. Start one, then try again.

`retryable` is true. The sentence holds no address and no port. It applies to `list_models` and to `complete`. A failed connect sent nothing, so it charges nothing.

The rule has these parts:

- A refusal (the system's "connection refused" or "connection aborted") on any of the addresses gives the sentence. For `localhost` that means `127.0.0.1` or `::1`.
- A timeout on any address gives `timeout`, whatever the other address said. A busy server is not a missing one.
- Any other connect trouble gives the general sentence of the API adapter. Examples are too many open files, an unreachable network, and an address that is not usable. The code is `endpoint_unreachable`, and it is retryable.
- A connect that the system denies (a permission error) gives the same general sentence. Here `retryable` is false. A second try gives the same result.
- A connection that breaks after it was made, a TLS fault, and an HTTP 5xx keep the sentences of the API adapter.

## What a call costs

The charging rules are the ones in `docs/model-adapters.md`. This adapter adds none.

- The hold is the input estimate plus `max_output_tokens`. If it does not fit under the cap, the call gets `token_cap_reached` and Schola sends nothing.
- Local servers often leave out the usage numbers. A reply with no usable usage is charged the whole hold. The result says so (`usage_reported` is false).
- A reply that arrives but cannot be used is charged as the API adapter charges it. A timeout after the request was sent is charged the whole hold.
- A local model costs no money, but the token cap still limits how much a session uses.

## What changed in the shared code

These changes are in `api_adapter.py`. Each has a test in `tests/test_api_adapter.py`. The API adapter keeps its wording and its charges.

- A fix for a bug in the step E3 code. A server that answers with a correct `Content-Length` and `Connection: close` closed the socket as soon as the body was read. Python 3.12 and later close it at once.
  The reader then asked the closed socket for its next wait and got "bad file descriptor". Schola reported `endpoint_unreachable` and charged the whole hold for a good reply.
  The adapter itself sends `Connection: close`, and many servers repeat it, so real runtimes were likely to hit this on Python 3.12 and later.
  The old tests missed it because the fake server never sent a `Connection` header in its replies. The reader now stops at a closed response and never touches a closed socket.
  A body read to its declared length counts as whole. A later socket failure does not change that.
  The new tests send `Connection: close` with a length, with chunks, and with neither. They do this for the chat call and for the list.
  They also run the reply-shape, status, and charge tests again with the header on every reply.
- `_roundtrip` does one call for any method and path. The chat call (`_exchange`) now uses it. A call with no body sends no `Content-Type`. A call can carry its own read and total limits, and a call that spends no tokens passes a hold of 0.
- The connect time and the name lookup time are cut to the total time of the call. For the chat call this changes nothing, because the total was always at least the connect time.
- `_classify_error` is a hook for the words of a network error. `_status_error` is a hook for the words of an HTTP status. A subclass can override either. The defaults are the old behavior.
- `ConnectFailed` is an error for a connect that failed on every address for a reason other than time. It keeps the number of the failure (`errno`) and says whether an address refused (`refused`).
  It is an `OSError`, so the API adapter treats it as before. A timeout on any address is raised as a timeout.
- `model_name_ok` is the model name rule as one function. The constructor and the model list both use it. `_load` is the shared reader of a JSON reply.

## Limits and what is not checked

- No real runtime was called (see the first section). The preset ports, the request fields, the usage fields, and the `/models` shape are not checked against Ollama, LM Studio, and the llama.cpp server.
- A runtime can answer `/models` in another shape. Then `list_models` fails with `bad_response` or gives a shorter list.
- The name rule keeps out path ids, which the llama.cpp server uses (see "The model list").
- Streaming is not supported. The adapter always asks for a whole reply.
- The default timeouts are guesses.
- Another program on a preset port gets the messages. Schola cannot tell it from a runtime.
- Windows and Linux are untested. The tests ran on macOS. The continuous integration run on Linux checks the test files there.
- `https` on loopback with a self-made certificate has no special support and no test for this adapter.
- The adapter does not retry. `retryable` is information for the caller, and the caller needs its own limit on attempts.
  A server can take a request and reset the connection before any reply byte. That costs nothing in the budget. So a retry loop does not end by itself (see `docs/model-adapters.md`).
- The adapter does not start a runtime and does not download a model.

## Not built in this step

- Starting, stopping, or installing a runtime, and downloading a model.
- Any route, screen, or wiring in `server.py`.
- Choosing a model for the student. That belongs to the screens and the policy step.
- The offer rate limit, the policy evaluator, and the agent loop (steps E5 and E6).
- Key storage and the default token cap. The decisions are still open.
