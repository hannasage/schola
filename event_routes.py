"""The event log and reflection routes of the local server, kept apart from `server.py`.

Routes (all on the local address). `server.py` applies the Host and Origin rules
first. Every route also needs the session token in the `X-Schola-Check-Token`
header and refuses a browser request that says it came from another site. The
token is the one `check_routes` makes. A page reads it from `GET /api/checks/session`.

    POST   /api/events              {event}: add one event. Only `source: "student"` is accepted
    GET    /api/events              the per-line report of the log
    GET    /api/events/export       the exact bytes of the log
    DELETE /api/events              {confirm: true}: delete the log file
    POST   /api/reflections         {text}: save a reflection, 1 to 10000 characters
    GET    /api/reflections/<id>    one reflection, by its lowercase UUID

A request body for these routes may be up to 256 KB.

Who may write which event
-------------------------
These routes are open to the page, so they take events with `source: "student"`
only. An event with the source `app` or `agent` can come only through
`event_log.EventLog.append` inside the server process. This keeps a web client
from forging a `check_result` or any other event that the app alone writes.
The contract does not tell a real student event from a made-up one, so a page
can still submit a student event of any type that the contract allows for a student.
"""
from event_log import EventLogError
from check_routes import check_fetch_site, check_token, session_for

BODY_LIMIT = 256 * 1024
GET_ROUTES = ('/api/events', '/api/events/export')
POST_ROUTES = ('/api/events', '/api/reflections')
DELETE_ROUTES = ('/api/events',)
REFLECTION_PREFIX = '/api/reflections/'
# Sources that the page may not claim. The in-process API writes them.
_NOT_FOR_THE_PAGE = ('app', 'agent')


def is_reflection_path(path):
    """True for `/api/reflections/<anything>`. The store decides whether the rest is a valid id."""
    return path.startswith(REFLECTION_PREFIX)


def _bad(message, field='request'):
    return EventLogError(message, 'invalid_request', 400, field)


def _only(payload, keys, shape):
    if not isinstance(payload, dict) or set(payload) - set(keys):
        raise _bad(f'Send {shape}.')


def _check_caller(server, headers):
    check_fetch_site(headers)
    check_token(session_for(server), headers)


def handle_get(server, events, path, headers):
    _check_caller(server, headers)
    if path == '/api/events':
        return 200, events.read()
    if path == '/api/events/export':
        return 200, events.export(), 'application/x-ndjson'
    if is_reflection_path(path):
        return 200, events.get_reflection(path[len(REFLECTION_PREFIX):])
    return None


def handle_post(server, events, path, payload, headers):
    _check_caller(server, headers)
    if path == '/api/events':
        _only(payload, ('event',), '{event}')
        if 'event' not in payload or not isinstance(payload['event'], dict):
            raise _bad('Send {event} with one event object.', 'event')
        event = payload['event']
        if event.get('source') in _NOT_FOR_THE_PAGE:
            raise EventLogError('The page can add events from the student only. The app and the agent write their own events.',
                                'source_forbidden', 403, field='event.source')
        return 201, events.append(event)
    if path == '/api/reflections':
        _only(payload, ('text',), '{text}')
        if 'text' not in payload:
            raise _bad('Send {text}.', 'text')
        return 201, events.save_reflection(payload['text'])
    return None


def handle_delete(server, events, path, payload, headers):
    _check_caller(server, headers)
    if path == '/api/events':
        _only(payload, ('confirm',), '{confirm: true}')
        return 200, events.delete(payload.get('confirm', False))
    return None

