# Model adapters

This page describes step E3. It covers the interface that connects Schola to a model, and the first adapter, the API adapter.
It says what the code does, what it refuses, and what it does not prove.

The code is `model_adapter.py` and `api_adapter.py`.
`model_adapter.py` holds the interface, the token budget, the key wrapper, and the context guard.
`api_adapter.py` holds the API adapter.
`types/model-adapter.d.ts` types the error payload and nothing else. A request and a result get types once a route sends them.
The tests are `tests/test_model_adapter.py` and `tests/test_api_adapter.py`.
The code uses only the Python standard library.
It needs Python 3.10 or later. The two test files passed on 3.10, 3.11, 3.12, and 3.14, all on macOS. Python 3.9 fails some of them, so it is not supported.
The tests use the explicit range list for address checks and do not depend on the `ipaddress` tables of one Python version.
Nothing calls these adapters yet. No route, screen, or agent loop uses them in this step.
Step E4 added the local adapter, a subclass of the API adapter. `docs/local-adapter.md` describes it.

## Terms

- A model is a program that writes text from text it receives. Schola does not run one in this step.
- A model adapter is the part of Schola that talks to one model source. Every adapter has the same single call, `complete`.
- A message is one piece of text with a role. The role names the writer: `system` (the instructions), `user`, or `assistant` (an earlier reply).
- A token is a small piece of text that a model counts. A word is often one or two tokens. Providers charge and limit by tokens.
- A token cap is the most tokens one session can use. The token budget is the counter that enforces it.
- A hold is the part of the budget that a running call keeps for itself.
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
The result also says how many tokens the budget counted (`tokens_charged`). The flag `usage_reported` is true for a provider that gave usage numbers. The flag `redacted` is true for a reply from which Schola removed the key. A `notice` explains both in words.
The reply text is output from a model. Treat it as untrusted.

`complete` is the same for every adapter. A new adapter writes only the part that talks to its source.
The shared part runs these steps in this order:

1. It checks that the request is a request.
2. It runs the context guard (see below).
3. It takes a hold in the token budget. If the hold does not fit under the cap, it raises `token_cap_reached`. At this point Schola has sent nothing. It made no lookup and no connection.
4. It sends the call.
5. It checks the reply and counts the tokens. If the reply repeats the key, it removes the key from the text.

The sizes are limited, so one request stays small.
A request holds 1 to 100 messages. A message holds at most 100000 characters. All messages together hold at most 400000.
`max_output_tokens` is a whole number from 1 to 200000.
These limits check the interface. They are not a safety policy.

## Errors

An error is a `ModelAdapterError`. It has a `code`, a fixed sentence as its message, `retryable`, and `tokens_charged`.
If `retryable` is true, the same call can work later. The code sets the default.
`tokens_charged` is what the budget counted for the failed call (see "What a failed call costs").

- `auth_failed`: the provider refused the key (HTTP 401 or 403). Not retryable.
- `rate_limited`: too many calls, or the plan's quota is used up (HTTP 429). Retryable. A used-up quota can stay used for a long time, so wait before you retry.
- `timeout`: the provider did not answer in time, or it answered with HTTP 408 or 504. Retryable.
- `endpoint_unreachable`: Schola failed to reach the provider. Retryable. These cases belong here:
  - Schola failed to connect, or the host name did not resolve.
  - The connection broke.
  - The provider had a server error (HTTP 500 to 599, except 501 and 505).
  - A TLS fault that a new try can fix, such as a connection that closes in the middle of the handshake.
  - A failed certificate check or a wrong TLS setting also lands here. Those two are not retryable.
- `response_too_large`: the reply is bigger than the cap. Not retryable.
- `bad_response`: the reply is not the one JSON object that Schola expects. This code also covers a status that Schola does not expect, such as HTTP 501 and 505. Not retryable.
- `token_cap_reached`: the session's token cap leaves too little room for the call. Schola sent nothing. Not retryable.
- `unsafe_endpoint`: the URL or the address breaks a rule (see below), or the endpoint tried to redirect. Not retryable.
- `invalid_request`: something the caller sent is not valid. One example is a message with a key or a path in it. Another is a provider answer of HTTP 400 to 499 that no other code covers. Not retryable.

A message never holds the key, the URL, the address, the error text of the operating system, or any text from the provider.
Schola never reads the body of a provider error.
Schola raises an error after the code that caught the problem ends. So the error chains no other error. A chained error can hold such text.
Schola turns an unexpected failure inside an adapter into `bad_response`. If the original had a `retryable` flag, the error keeps it.
Schola writes only the type of the original and the file, line, and function of each frame to the logger `schola.model_adapter`. It drops the message. The message can hold a key or a path. The logger prints nothing until the application adds a handler.
An out-of-memory failure is not turned into an error. It goes through.

## The token budget

`TokenBudget(cap)` takes the cap as a required argument. It has no default value. The default cap is an open decision for the maintainer.

- Before a call sends anything, it takes a hold. The hold is an estimate of the input plus `max_output_tokens`. The estimate is the number of input characters divided by 3, rounded up.
- The hold must fit in what is left: the cap, minus the tokens spent, minus the holds of the calls that are still running. If it does not fit, the call fails with `token_cap_reached`.
- `remaining` and `exhausted` count the holds of running calls. While a call runs, `remaining` is smaller by its hold.
- A cap that is small next to `max_output_tokens` refuses every call. Pick a `max_output_tokens` that is well below the cap.
- After a call, the budget swaps the hold for the tokens the response reports: input plus output.
- Schola raises that charge to a floor. The floor is the input estimate divided by 4, plus the number of reply characters divided by 8. A provider that under-counts then costs at least this much. The `notice` says so.
- No charge goes above twice the hold. Schola puts a line in the `notice` for each cap.
- If the response has no usable usage numbers, the budget counts the whole hold as spent. The result says so: `usage_reported` is false, `usage` is empty, and `notice` explains.
- These cases count as no usable usage: a field that is missing, a negative number, a number that is not whole, and only one of the two counts.
- An input count of zero is also unusable, with text or without. The provider read the input, so zero cannot be right.
- Schola ignores `total_tokens`. The two counts decide.
- The budget takes a charge that is a plain whole number of 0 or more. A subclass or an error that hands over anything else is charged the whole hold.

One call can still pass the cap. The estimate can be too low, and the provider counts its own way. The overshoot is at most the hold, because no charge goes above twice the hold. The next call is then refused.
Twenty calls that run at the same time cannot take more holds than the cap allows. So the total overshoot is the sum of those differences, not the sum of the whole calls.
The estimate is rough. It suits Latin text. Text in Chinese or Japanese costs about one token a character, so the estimate runs low there and the overshoot grows.

## What a failed call costs

A reply that arrived but cannot be used was still billed. A request that was sent and never answered was probably billed too. The budget counts them.
A charge for a failure is the whole hold unless the text says otherwise. The whole hold is the input estimate plus `max_output_tokens`.

- A 200 reply whose body cannot be used: the budget counts the `usage` the body carries. If `usage` cannot be read, the budget counts the whole hold. These cases belong here:
  - a null `content` with no refusal
  - an empty `choices` list
  - a wrong shape or a broken JSON body
  - a reply that is too large
  - a reply with the wrong type, or with compression
  - a length header that cannot be read
- A 200 reply whose body breaks in the middle counts the whole hold. This covers a reset, a cut chunk, a chunk line that is too long, and a body that ends early. Schola counts it like a body that ends before its declared length.
- A 200 reply that holds an `error` object counts as an error from the provider. The budget counts nothing. The one exception is a reply that carries `usage`. Then the budget counts that.
- A timeout after the request was fully sent: the budget counts the whole hold.
- An interrupt (such as Ctrl-C), or an out-of-memory failure, after the send began: the budget counts the whole hold, because the request can have gone out. Before the send began, it counts nothing.
- A refusal from the provider, with no text, is a reply, not an error. It comes back with the stop reason `content_filter` and its usage.
- A call to a tool, with no text, is a reply too. It comes back with the stop reason `other` and its usage.
- Everything else counts nothing. That covers these cases:
  - a connect failure, a name lookup failure, or a TLS failure
  - a timeout before the request was sent, or while it was still going out
  - a redirect
  - any HTTP status other than 200 (401, 402, 403, 429, 4xx, 5xx)
  - a connection that breaks before any reply came back. This case can hide a bill that Schola does not see.

A failure that counts nothing can repeat without end. A 429, a 5xx, a connect failure, and a reset all count nothing. A caller that retries on a retryable error needs its own limit on attempts. Only billed failures fill the budget and end a retry loop with `token_cap_reached`.

## The API key

The key lives in an `ApiKey`. Its text form is always `[redacted]`. This holds for `str`, `repr`, `format`, f-strings, `%` formatting, logging, and printing a list or a dictionary that holds the key.
`pickle` refuses to save it. `json.dumps` refuses to write it. If the caller passes `default=str`, `json.dumps` writes `"[redacted]"`.
`copy` returns the same object. If two keys hold the same value, they are equal. A key is not hashable.
A key cannot change after it is made. A second call to `__init__` raises an error.
The value comes out through one method, `reveal_for_transport()`. Only the code that builds the request header calls it.
A key is 1 to 1024 visible ASCII characters with no spaces. This rule also keeps a stray line break out of the request header.

The key stays in memory. Step E3 builds no storage for keys. Where keys live on each platform is an open decision.
Until that decision is made, the code that creates an adapter must get the key from somewhere itself. That code must not write the key to the event log, a log line, or an error.

What the wrapper does not do: Python cannot hide an object from code in the same process. Code that reads memory, uses the debugger, or walks the object can reach the value.
Some error reporters record the local variables of every stack frame. Such a tool records the key text in the frame that builds the header. Do not turn that on in a build that uses real keys. Such a tool also records a base URL that the caller typed, with any password in it.

If a provider echoes the key, the adapter removes it from the reply text and sets `redacted` and the `notice`. The context guard refuses a message that holds the key.

## The context guard

The model must not see keys or files that it must not see. `context_guard(messages, allowed_paths, secrets)` is a small check. The shared `complete` runs it on every request.
It first folds the text to a plain form (Unicode NFKC). It scans three views of the text.

- The key view has the invisible characters deleted. Three Unicode classes count:
  - format marks (Cf)
  - control codes that are not white space (Cc)
  - combining marks (Mn and Me)
  - the blank fillers U+115F, U+1160, U+3164, U+FFA0, and U+2800
  - variation selectors and tag characters (they fall in the classes above)
- The compact view is the key view with all spaces and line breaks removed. A key cut by spaces or lines still shows. Schola also joins the compact views of all messages and looks for key shapes in the joined text. A key split over two messages shows.
- The path view turns each invisible character into a space instead. So `open\u200b/etc/passwd` reads as `open /etc/passwd`.

The key view and the path view differ on purpose. Deleting a character can glue a word onto a path.
It raises `invalid_request` for a message that does one of these things:

- It holds a key that the adapter holds (exact match). Schola also looks for the key in all messages joined together.
- It holds text with the shape of a common secret. A private key header can have up to 40 capital letters or spaces before and after `PRIVATE KEY`. These shapes count:
  - `sk-` keys (also in capitals), Stripe keys, AWS access keys, and GitHub tokens.
  - Slack tokens, Google keys, Hugging Face tokens, Groq keys, and npm tokens.
  - GitLab tokens (`glpat-`) and SendGrid keys (`SG.`).
  - Google OAuth tokens (`ya29.`), Stripe webhook secrets (`whsec_`), and Supabase keys (`sb_secret_`).
  - A private key header (RSA, EC, PGP, and others) and a JSON web token.
  - `Bearer` and then a long token, and `Authorization: Basic` and then a long value.
  - A URL with a user name and password in it, such as `postgres://user:pass@host/db`.
  - A name, then `=` or `:`, then a value. The name ends in `api_key`, `apikey`, `secret`, `secret_access_key`, `token`, `passwd`, or `password`. The value has 16 or more characters and holds a digit. Quotes can sit around the name and the value.
  - These all match: `AWS_SECRET_ACCESS_KEY`, `DB_PASSWORD`, `client_secret`, and `{"apiKey": "..."}`.
- It names an absolute file path that is not inside one of the `allowed_paths`.
- It holds a `..` path segment, or a bare `..` word such as `cd ..`.
- It names a path through an environment variable, such as `$HOME/.ssh/id_rsa` or `%USERPROFILE%\notes`.

These path forms count:

- POSIX paths: `/etc/passwd`, `//etc/passwd`, `///etc/passwd`, and `//server/share/x`.
- Home paths that start with `~`, Windows drive paths, and `\\server\share`.
- A `file:` URL, with `//` or with one slash.

A path starts at the start of the text. It also starts after any character that is not part of a word or of a path. Letters, digits, `.`, `_`, `-`, `/`, and `\` are part of a word or a path. A `-` that follows white space can also start a path, as in `x -/etc/passwd`. A slash inside a word starts nothing. So `and/or`, `km/h`, `1/2`, and `и/или` pass.
A path ends at white space, a quote, a bracket, or one of `< > | * ? , ;`. It also ends at `:`, `=`, `&`, and `!`. A second path glued on with one of those marks is checked on its own. So `/allowed/proj/bin:/etc/passwd` and `--volume /allowed/proj:/etc/shadow` are refused. A line suffix such as `a.py:12:5` is fine. A URL ends at `,`, `;`, and `)` too, so `http://x.com,/etc/passwd` is refused.
`//word` alone is a comment marker, not a path. `https://host/path` is a URL, not a path. A closing tag such as `</div>` is not a path.
The error names the message number. It never repeats the text.

`allowed_paths` is a list of absolute folders. The default is the empty list, so by default the guard refuses every absolute path.
The root of everything is refused as an allowed path. That means `/`, a drive such as `C:\`, and a bare `\\`. Name a folder.
A path is inside a root in two cases: it equals the root, or it sits under the root. Schola first cleans the path (it removes `.` and doubled separators). Schola does not follow links. A path of more than 4096 characters is refused.
A root can hold a space, such as `/Users/x/Sage Advice/y`. A path that starts with that root passes. The match is exact, and for a Windows root it ignores case.

The scan takes time in proportion to the length of the text. The guard refuses more than 400000 characters in all.

The limits of the guard:

- It is a short list of patterns. It misses keys in formats it does not know. It misses keys that are encoded, or spelled out. A hex digest and an id pass, on purpose.
- Schola finds a split key only at the start of the text or after a non-letter. A key split after a word is missed.
- It can refuse harmless text that looks like a key or a path. Teaching text about regular expressions, shell variables, or `password = "..."` examples can trip it. A slash after any mark that is not part of a word starts a path, so `a:/b` and `(/x)` are refused.
- Look-alike slashes such as U+2215 and U+2044 are not read as slashes. A root-relative Windows path such as `\etc\passwd` is not checked.
- It does not check relative paths such as `src/main.py`. It checks only for `..` segments.
- It does not decode percent codes. `%2e%2e/` passes.
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
- Any port is allowed. That is safe for a URL that the student does not type. A student who types the URL can aim the adapter at any public port. The address rules below still apply.

### Address rules (SSRF)

At every call, the adapter looks up the host and checks every address it gets.

- For a host that is not loopback, each address must be public. These kinds are refused:
  - private addresses (`10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`, `fc00::/7`)
  - loopback, link-local, multicast, reserved, and unspecified addresses
  - shared address space (`100.64.0.0/10`)
  - site-local addresses (`fec0::/10`)
  - the old 6to4 relay range (`192.88.99.0/24`)
- A fixed list of ranges backs this up. A change in a Python version does not open a range.
- Link-local includes the cloud metadata address `169.254.169.254`.
- Some IPv6 addresses carry an IPv4 address inside (IPv4-mapped, 6to4, and NAT64). Schola checks the carried IPv4 address.
- The Azure wire address `168.63.129.16` is public as far as these rules go, so the adapter allows it.
- For a loopback host, each address must be loopback. If a hosts file points `localhost` at another machine, Schola refuses it.
- If one address in the list is bad, Schola refuses the host.
- The adapter connects to the address it checked, not to the name. It sends the name in the `Host` header and uses it for the TLS check. A second lookup cannot move the connection. So DNS rebinding between the check and the connection does not work against this adapter.
- If the lookup gives an answer that is not a list of addresses, the call fails with `bad_response`. If the host does not exist, the call fails with `endpoint_unreachable`.
- The adapter ignores the proxy settings of the environment.

### Redirects

The adapter never follows a redirect. Any answer in the 300 range gives `unsafe_endpoint`, and Schola sends nothing anywhere else. The key goes to the configured host only.

### Time and size limits

- Three limits apply. The connect time is 10 seconds by default. The wait for each read is 60 seconds. The whole call takes at most 120 seconds. The caller can set all three, up to 600 seconds. The total must be at least the connect time and at least the read time.
- One clock covers the whole call: the name lookup, each try to connect, the TLS handshake, the send, and the reply. Each connect try gets the smaller of the connect time and what is left of the total. When the total is gone, Schola stops trying other addresses.
- Every send and every read of the body carries its own limit. The limit is the read time or what is left of the total, whichever is smaller. A timer also shuts the socket at the end of the total. A shutdown from another thread does not reliably wake a blocked send or TLS read on every system. So the limits on the socket are the main bound, and the timer is the backup.
- A server that never reads the request, or that goes silent after the TLS handshake, ends the call at the total. A server that sends the body one byte at a time ends it at the total too.
- One case is weaker. A server can send the header lines of the reply one byte at a time. Each wait there has the read time, so only the timer stops this. The timer works on the systems where the tests ran, but Schola cannot promise it for every system.
- The bound is the total plus a small delay for thread scheduling. The tests allow 0.6 seconds of delay. A name lookup that hangs is the exception. Schola stops waiting at the deadline, but the lookup keeps its own daemon thread until the system gives up. Each hung lookup leaves one such thread.
- Schola reads the reply in pieces. It refuses the reply with `response_too_large` once the reply passes 1 MB (1,000,000 bytes). If the declared length is already bigger, it refuses at once. The caller can set the cap up to 10 MB.
- The reply must have the type `application/json` or an `application/` type that ends in `+json`, no compression, and a `utf-8` charset or none. A `Content-Length` must be plain ASCII digits. Anything else is billed like an unreadable body.
- The reply must be one JSON object with `choices[0].message.content` as text. Schola refuses numbers of more than 18 digits, `NaN`, and invalid text. Anything else gives `bad_response`. Schola uses only the first choice. An empty `choices` list has its own message.
- A null `content` can still be a reply.
  - With a `refusal` or the `finish_reason` `content_filter`, the call returns an empty text and the stop reason `content_filter`.
  - With `tool_calls` or the `finish_reason` `tool_calls` or `function_call`, the call returns an empty text and the stop reason `other`.
- If a 200 reply has a top-level `error` object, Schola reads a number from its `code` or `status`. A number from 400 to 599 goes through the status table below, so a 429 is `rate_limited` and retryable. If there is no such number, the call fails with `bad_response`.
- The `finish_reason` values `stop`, `length`, and `content_filter` map to `end`, `length`, and `content_filter`. Every other value, and a missing value, maps to `other`. A reply with no text and the reason `length` comes back as a success with an empty text.

The status table:

- 401 and 403: `auth_failed`.
- 429: `rate_limited`.
- 408 and 504: `timeout`.
- 501 and 505: `bad_response`.
- Other 5xx: `endpoint_unreachable`.
- 402: `invalid_request`, with a sentence about billing and the quota.
- 400 and 422: `invalid_request`, with a sentence that names the output limit field (`max_tokens_field`).
- Other 4xx: `invalid_request`.
- Any other status that is not 200: `bad_response`. Any 3xx is `unsafe_endpoint`.

### TLS

TLS is the encryption that `https` uses. For `https`, the adapter uses a context that checks certificates and host names and allows TLS 1.2 or later only.

- With no `ssl_context`, the adapter builds the default context. A lowest version below TLS 1.2 goes up to TLS 1.2.
- You can pass your own context. The adapter refuses a context that skips the certificate check or the host name check. It also refuses a context whose highest version is below TLS 1.2, or below its own lowest version. The adapter makes these checks first. A refused context stays as it was.
- Some systems report a plain context with no lowest version at all (`MINIMUM_SUPPORTED`). The adapter does not refuse such a context. It raises the lowest version to TLS 1.2 on the context you passed in. Your context changes.
- To set it yourself: `context = ssl.create_default_context(); context.minimum_version = ssl.TLSVersion.TLSv1_2`.
- A lowest version above TLS 1.2 stays as it is.
- A context that two adapters share changes for both. Another thread can also change a context between the check and the connect. The connect-time check below catches the change, but only the checks it makes.

The adapter checks the context again at connect time. A change made after the adapter was built does not open a hole. A failed check at that point gives `unsafe_endpoint`. A failed certificate check on the server gives `endpoint_unreachable` and is not retryable.
The operating system and Python's default context decide which certificate authorities are trusted.

## Limits and what is not verified

- No real provider was called. The tests use a fake endpoint on a loopback port. Nobody checked whether a real provider accepts this request. That covers OpenAI, Azure OpenAI, Mistral, Groq, and others. Providers differ in the name of the token limit field, in the usage fields, and in extra request fields. The adapter refuses Azure style URLs that need a query string (`?api-version=`).
- Streaming is not supported. The adapter always asks for a whole reply.
- The reply text is raw model output. A caller that prints it to a terminal must remove terminal escape sequences first.
- The HTTPS path is tested against a loopback server with a certificate made at test time. It needs the `openssl` program. The tests skip without it. A public certificate chain is not tested.
- The address rules rest on Python's `ipaddress` module and on a fixed list of ranges. Its tables of special addresses change between Python versions.
- DNS lookups use the system resolver. Nothing cancels a lookup that hangs. The call stops waiting at the deadline.
- The adapter does not retry. `retryable` is information for the caller, and the caller needs its own limit on attempts.
- A name lookup that outlasts the connect time leaves one daemon thread until the system gives up.
- A 200 reply with `"error": false` or `"error": {}` next to valid choices is refused, because any `error` value that is not null counts as an error.
- A whole number of more than 18 digits anywhere in a reply gets the whole reply refused.
- The adapter does not read a provider's `Retry-After` header.
- The token count comes from the provider. Schola cannot check the numbers, and the input estimate is rough.
- A connection that breaks before any reply byte arrives counts as nothing in the budget. The provider can still bill for such a call. Schola cannot see it.
- Windows and Linux are untested. The tests ran on macOS, with Python 3.10, 3.11, 3.12, and 3.14.

## Not built in this step

- The command line bridge. Nobody checked whether provider terms allow it.
- Key storage. Where keys live on each platform is an open decision.
- The default token cap. The cap is required until that decision is made.
- The offer rate limit, the policy evaluator, and the agent loop (steps E5 and E6).
- Any route, screen, or wiring in `server.py`.
