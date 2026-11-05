# Model adapters

This page describes step E3. It covers the interface that connects Schola to a model, and the first adapter, the API adapter.
It says what the code does, what it refuses, and what it does not prove.

The code is `model_adapter.py` and `api_adapter.py`.
`model_adapter.py` holds the interface, the token budget, the key wrapper, and the context guard.
`api_adapter.py` holds the API adapter.
The types for other code are in `types/model-adapter.d.ts`.
The tests are `tests/test_model_adapter.py` and `tests/test_api_adapter.py`.
The code uses only the Python standard library.
Nothing calls these adapters yet. No route, screen, or agent loop uses them in this step.

## Terms

- A model is a program that writes text from text it receives. Schola does not run one in this step.
- A model adapter is the part of Schola that talks to one model source. Every adapter has the same single call, `complete`.
- A message is one piece of text with a role. The role names the writer: `system` (the instructions), `user`, or `assistant` (an earlier reply).
- A token is a small piece of text that a model counts. A word is often one or two tokens. Providers charge and limit by tokens.
- A token cap is the most tokens one session can use. The token budget is the counter that enforces it.
- A session is one sitting of a student in a module.
- An API key is a secret string from a provider. It proves who pays for the calls.
- An endpoint is a web address that accepts model calls. The base URL is the part of the address before `/chat/completions`.
- The OpenAI request format is the way of asking for a reply that the OpenAI chat-completions service uses. Many other services and local programs copy it.
- A loopback address leads back to the same computer: `127.0.0.1`, `::1`, or the name `localhost`.
- A redirect is an answer that says "ask a different address instead".
- SSRF (server-side request forgery) is a trick. It makes a program call an address it must not call, such as a private network or a cloud metadata service.
- DNS is the system that turns a host name into numeric addresses. DNS rebinding is a trick. A name gives a safe address first and a bad address later.

## The call

```text
adapter.complete(request) -> result
```

A request holds three things: the messages, `max_output_tokens` (the most tokens the reply can use), and the session's `TokenBudget`.
A result holds the reply text, the token usage (`input_tokens` and `output_tokens`), and a stop reason.
The stop reason is `end` (the model finished), `length` (it reached the limit), `content_filter` (the provider cut it), or `other`.
The reply text is output from a model. Treat it as untrusted.

`complete` is the same for every adapter. A new adapter writes only the part that talks to its source.
The shared part runs these steps in this order:

1. It checks that the request is a request.
2. It runs the context guard (see below).
3. It checks the token budget. If the budget is spent, it raises `token_cap_reached`. At this point Schola has sent nothing. It made no lookup and no connection.
4. It sends the call.
5. It checks the reply and counts the tokens. If the reply repeats the key, it removes the key from the text.

The sizes are limited, so one request stays small.
A request holds 1 to 100 messages. A message holds at most 100000 characters. All messages together hold at most 400000.
`max_output_tokens` is a whole number from 1 to 200000.
These limits check the interface. They are not a safety policy.

## Errors

An error is a `ModelAdapterError`. It has a `code`, a fixed sentence as its message, and `retryable`.
If `retryable` is true, the same call can work later. The code sets the default.

- `auth_failed`: the provider refused the key (HTTP 401 or 403). Not retryable.
- `rate_limited`: too many calls, or the plan's quota is used up (HTTP 429). Retryable. A used-up quota can stay used for a long time, so wait before you retry.
- `timeout`: the provider did not answer in time, or it answered with HTTP 408 or 504. Retryable.
- `endpoint_unreachable`: Schola failed to connect, the host name did not resolve, the connection broke, or the provider had a server error (HTTP 500 to 599). Retryable. A TLS certificate that fails the check also gives this code. That case is not retryable.
- `response_too_large`: the reply is bigger than the cap. Not retryable.
- `bad_response`: the reply is not the one JSON object that Schola expects, or the provider sent a status that Schola does not expect. Not retryable.
- `token_cap_reached`: the session's token cap is reached. Schola sent nothing. Not retryable.
- `unsafe_endpoint`: the URL or the address breaks a rule (see below), or the endpoint tried to redirect. Not retryable.
- `invalid_request`: something the caller sent is not valid. One example is a message with a key or a path in it. Another is a provider answer of HTTP 400 to 499 that no other code covers. Not retryable.

A message never holds the key, the URL, the address, the error text of the operating system, or any text from the provider.
Schola never reads the body of a provider error.
Schola raises an error after the code that caught the problem ends. So the error chains no other error. A chained error can hold such text.
Schola turns an unexpected failure inside an adapter into `bad_response`. The cost: a programming mistake in an adapter shows as a plain `bad_response`. A test of the adapter sees the real cause.

## The token budget

`TokenBudget(cap)` takes the cap as a required argument. It has no default value. The default cap is an open decision for the maintainer.

- Before a call sends anything, the budget needs room. The tokens already spent, plus the `max_output_tokens` of calls still running, must be below the cap. If not, the call fails with `token_cap_reached`.
- After a call, the budget adds the tokens the response reports: input plus output.
- A call that fails adds nothing and frees what it held.
- If the response has no usable usage numbers, the budget counts `max_output_tokens` as spent. The result says so: `usage_reported` is false, `usage` is empty, and `notice` explains. A usage that is present but wrong is treated the same way. Examples are a negative number, a number that is not whole, and only one of the two counts. Schola never counts a reply as free.

One call can pass the cap. Schola does not know the input size until the provider answers, and Schola does not count tokens itself. The next call is then refused.
Calls that run at the same time each hold their `max_output_tokens`. If two calls run and their holds reach the cap, a third call is refused.
A call can fail after the provider already charged for it. A timeout after the request left is one example. That call adds nothing here. The budget then counts too few tokens.

## The API key

The key lives in an `ApiKey`. Its text form is always `[redacted]`. This holds for `str`, `repr`, `format`, f-strings, `%` formatting, logging, and printing a list or a dictionary that holds the key.
`pickle` refuses to save it. `json.dumps` refuses to write it. If the caller passes `default=str`, `json.dumps` writes `"[redacted]"`.
`copy` returns the same object. If two keys hold the same value, they are equal. A key is not hashable.
The value comes out through one method, `reveal_for_transport()`. Only the code that builds the request header calls it.
A key is 1 to 1024 visible ASCII characters with no spaces. This rule also keeps a stray line break out of the request header.

The key stays in memory. Step E3 builds no storage for keys. Where keys live on each platform is an open decision.
Until that decision is made, the code that creates an adapter must get the key from somewhere itself. That code must not write the key to the event log, a log line, or an error.

What the wrapper does not do: Python cannot hide an object from code in the same process. Code that reads memory, uses the debugger, or walks the object can reach the value.
Some error reporters record the local variables of every stack frame. Such a tool records the key text in the frame that builds the header. Do not turn that on in a build that uses real keys.

If a provider echoes the key, the adapter removes it from the reply text. The context guard refuses a message that holds the key.

## The context guard

The model must not see keys or files that it must not see. `context_guard(messages, allowed_paths, secrets)` is a small check. The shared `complete` runs it on every request.
It raises `invalid_request` for a message that does one of these things:

- It holds a key that the adapter holds (exact match).
- It holds text with the shape of a common secret. These shapes count:
  - `sk-` keys, Stripe keys, AWS access keys, and GitHub tokens.
  - Slack tokens, Google keys, Hugging Face tokens, and Groq keys.
  - A PEM private key header and a JSON web token.
  - `Bearer` and then a long token.
  - `api_key`, `secret`, `token`, or `password`, then `=` or `:`, then a long value with a digit in it.
- It names an absolute file path that is not inside one of the `allowed_paths`. The path forms are POSIX, `~`, a Windows drive, `\\server\share`, and a `file:` URL.
- It holds a `..` path segment.

The error names the message number. It never repeats the text.
`allowed_paths` is a list of absolute folders. The default is the empty list, so by default the guard refuses every absolute path.
A path is inside a root in two cases: it equals the root, or it sits under the root. Schola first cleans the path (it removes `.` and doubled separators). Schola does not follow links.

The limits of the guard:

- It is a short list of patterns. It misses keys in formats it does not know. It misses keys that are split, encoded, or spelled out. A hex digest and an id pass, on purpose.
- It can refuse harmless text that looks like a key or a path. Teaching text about HTML tags, regular expressions, or `password = "..."` examples can trip it.
- It does not check relative paths such as `src/main.py`. It checks only for `..` segments.
- It does not decide what belongs in a message. Steps E5 and E6 choose what the model sees. That work covers rollups of events, a short window of recent events, fetching a raw event by id, and marking student text as data. The guard stops two kinds of mistakes at the last moment.

## The API adapter

```python
adapter = ApiAdapter(base_url, model, api_key)   # api_key is an ApiKey
result = adapter.complete(request)
```

The caller gives the base URL (for example `https://api.openai.com/v1`), the model name, and an `ApiKey`.
The adapter sends `POST <base URL>/chat/completions` with a JSON body. The body holds the model, the messages (`role` and `content`), the output limit, and `"stream": false`.
By default the output limit goes out as `max_tokens`. Some providers want `max_completion_tokens`. For those, pass `max_tokens_field='max_completion_tokens'`.
The key goes in the `Authorization: Bearer` header. A loopback endpoint can run with `api_key=None`. Then Schola sends no header.

### Endpoint rules

If a URL breaks a rule, the adapter refuses it when the adapter is made. The error code is `unsafe_endpoint`.

- The scheme is `https`. The scheme `http` is allowed only for `127.0.0.1`, `::1`, and `localhost`.
- The URL has no credentials, no fragment, no query, and no other scheme.
- The host is a plain ASCII name or an IP address. A host that ends in a number (such as `127.1` or `2130706433`) or in `0x` is refused. Some systems turn those into an address.
- The path is made of segments. A segment holds letters, digits, and `. _ ~ -`. The path has no `.` or `..` segment, no doubled slash, and no percent code.
- A URL has at most 2048 characters.

### Address rules (SSRF)

At every call, the adapter looks up the host and checks every address it gets.

- For a host that is not loopback, each address must be public. These are refused: private (`10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`, `fc00::/7`), loopback, link-local, shared address space (`100.64.0.0/10`), multicast, reserved, and unspecified. Link-local includes the cloud metadata address `169.254.169.254`. Some IPv6 addresses carry an IPv4 address inside (IPv4-mapped, 6to4, and NAT64). Schola checks the carried IPv4 address.
- For a loopback host, each address must be loopback. If a hosts file points `localhost` at another machine, Schola refuses it.
- If one address in the list is bad, Schola refuses the host.
- The adapter connects to the address it checked, not to the name. It sends the name in the `Host` header and uses it for the TLS check. A second lookup cannot move the connection. So DNS rebinding between the check and the connection does not work against this adapter.
- If a lookup takes longer than the connect timeout, the call stops with `timeout`.
- The adapter ignores the proxy settings of the environment.

### Redirects

The adapter never follows a redirect. Any answer in the 300 range gives `unsafe_endpoint`, and Schola sends nothing anywhere else. The key goes to the configured host only.

### Time and size limits

- The connect timeout is 10 seconds. The wait for each read is 60 seconds. The whole call takes at most 120 seconds. The caller can set all three, up to 600 seconds. The total limit also stops a server that sends one byte at a time.
- Schola reads the reply in pieces. It refuses the reply with `response_too_large` once the reply passes 1 MB (1,000,000 bytes). If the declared length is already bigger, it refuses at once. The caller can set the cap up to 10 MB.
- The reply must have the type `application/json` (a `+json` type is accepted), no compression, and a `utf-8` charset or none.
- The reply must be one JSON object with `choices[0].message.content` as text. Schola refuses numbers of more than 18 digits, `NaN`, and invalid text. Anything else gives `bad_response`. Schola uses only the first choice.
- The `finish_reason` values `stop`, `length`, and `content_filter` map to `end`, `length`, and `content_filter`. Every other value, and a missing value, maps to `other`.

### TLS

TLS is the encryption that `https` uses. For `https`, the adapter uses a context that checks certificates and host names. It refuses a context that does not check them. A failed check gives `endpoint_unreachable` and is not retryable.
The operating system and Python's default context decide which certificate authorities are trusted.

## What is not verified

- No real provider was called. The tests use a fake endpoint on a loopback port. Nobody checked whether a real provider accepts this request. That covers OpenAI, Azure OpenAI, Mistral, Groq, and others. Providers differ in the name of the token limit field, in the usage fields, and in extra request fields. The adapter refuses Azure style URLs that need a query string (`?api-version=`).
- Streaming is not supported. The adapter always asks for a whole reply.
- The HTTPS path is tested against a loopback server with a certificate made at test time. It needs the `openssl` program. The tests skip without it. A public certificate chain is not tested.
- The address rules rest on Python's `ipaddress` module. Its tables of special addresses change between Python versions.
- DNS lookups use the system resolver. Nothing cancels a lookup that hangs. The call stops waiting, and the lookup finishes on its own thread.
- The adapter does not retry. `retryable` is information for the caller.
- The adapter does not read a provider's `Retry-After` header.
- The token count comes from the provider. Schola cannot check the numbers.
- Windows is untested.

## Not built in this step

- The command line bridge. Nobody checked whether provider terms allow it.
- The local adapter (step E4). This adapter already accepts a loopback endpoint with no key. Nobody checked which local runtimes work.
- Key storage. Where keys live on each platform is an open decision.
- The default token cap. The cap is required until that decision is made.
- The offer rate limit, the policy evaluator, and the agent loop (steps E5 and E6).
- Any route, screen, or wiring in `server.py`.
