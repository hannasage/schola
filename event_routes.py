"""The event log and reflection routes of the local server, kept apart from `server.py`.

Routes (all on the local address). `server.py` applies the Host and Origin rules
first. Every route also needs the session token in the `X-Schola-Check-Token`
header and refuses a browser request that says it came from another site. The
token is the one `check_routes` makes. A page reads it from `GET /api/checks/session`.

    POST   /api/events              {event}: add one event. Only `source: "student"` is accepted
    GET    /api/events              a window of the per-line report: ?limit= (1 to 5000, default 500) and ?offset=
    GET    /api/events/export       the exact bytes of the log, with the headers X-Schola-Log-Ok and X-Schola-Truncated-Tail
    DELETE /api/events              {confirm: true}: delete the log and every reflection
    POST   /api/reflections         {text}: save a reflection, 1 to 10000 characters
    GET    /api/reflections/<id>    one reflection, by its lowercase UUID

A request body for these routes may be up to 256 KB.

Who may write which event
-------------------------
These routes are open to the page, so they take events with `source: "student"`
only. Any other source, or none, is refused. An event with the source `app` or
`agent` can come only through `event_log.EventLog.append` inside the server process.
An `agent_offer` event is refused from the page with the code `type_forbidden`, even with the source `student`.
A page also cannot fill the log: once the log is 80% full the page gets `log_reserved`. This keeps a web client
from forging a `check_result` or any other event that the app alone writes.
The contract does not tell a real student event from a made-up one, so a page
can still submit a student event of any type that the contract allows for a student.

The token does not separate the page script from the app. Any script that runs on the
app origin can read the token, so it can also call DELETE.
"""
import re
import threading

from event_log import EventLog, EventLogError
from check_routes import check_fetch_site, check_token, session_for

BODY_LIMIT = 256 * 1024
GET_ROUTES = ('/api/events', '/api/events/export')
POST_ROUTES = ('/api/events', '/api/reflections')
DELETE_ROUTES = ('/api/events',)
REFLECTION_PREFIX = '/api/reflections/'
# The one source the page may claim. The in-process API writes the others.
_PAGE_SOURCE = 'student'
# The one type the page may never write, whatever its source. Only the agent writes an `agent_offer`.
_AGENT_TYPE = 'agent_offer'
DEFAULT_LIMIT = 500
MAX_LIMIT = 5000
_LOG_LOCK = threading.Lock()
_NUMBER = re.compile(r'[0-9]{1,9}', re.ASCII)


def log_for(server):
    """The one event log of a server. It is made at the first use and kept, so its checks of the file are kept too."""
    log = getattr(server, 'event_log', None)
    if log is None:
        with _LOG_LOCK:
            log = getattr(server, 'event_log', None)
            if log is None:
                log = server.event_log = EventLog(server.data_dir)
    return log


def fault_message(method, path):
    """What to tell the caller when something unexpected broke. It says what the caller can know about the write."""
    if method == 'POST' and path == '/api/events':
        return 'Schola hit an unexpected problem. The event may or may not be saved. Read your event log before you try again.'
    if method == 'POST':
        return 'Schola hit an unexpected problem. The reflection may or may not be saved. Check your reflections before you try again.'
    if method == 'DELETE':
        return 'Schola hit an unexpected problem. Some files may or may not be deleted. Read your event log before you try again.'
    return 'Schola hit an unexpected problem and could not finish. Your event log was not changed.'


def is_reflection_path(path):
    """True for `/api/reflections/<anything>`. The store decides whether the rest is a valid id."""
    return path.startswith(REFLECTION_PREFIX)


def _bad(message, field='request'):
    return EventLogError(message, 'invalid_request', 400, field)


def _only(payload, keys, shape):
    if not isinstance(payload, dict) or set(payload) - set(keys):
        raise _bad(f'Send {shape}.')


def check_caller(server, headers):
    """Refuse a request from another site or without the session token."""
    check_fetch_site(headers)
    check_token(session_for(server), headers)


def _number(query, name, default, low, high):
    values = query.get(name)
    if values is None:
        return default
    if len(values) != 1 or not _NUMBER.fullmatch(values[0]) or not low <= int(values[0]) <= high:
        raise _bad(f'Send {name} as a whole number from {low} to {high}.', name)
    return int(values[0])


def _window(query):
    query = query or {}
    if set(query) - {'limit', 'offset'}:
        raise _bad('Send only limit and offset.', 'query')
    return _number(query, 'limit', DEFAULT_LIMIT, 1, MAX_LIMIT), _number(query, 'offset', 0, 0, 999_999_999)


def handle_get(server, events, path, headers, query=None):
    check_caller(server, headers)
    if path == '/api/events':
        limit, offset = _window(query)
        return 200, events.read(limit=limit, offset=offset)
    if path == '/api/events/export':
        exported = events.export_checked()
        return 200, exported['data'], 'application/x-ndjson', {'X-Schola-Log-Ok': 'true' if exported['ok'] else 'false',
                                                               'X-Schola-Truncated-Tail': 'true' if exported['truncatedTail'] else 'false'}
    if is_reflection_path(path):
        return 200, events.get_reflection(path[len(REFLECTION_PREFIX):])
    return None


def handle_post(server, events, path, payload, headers):
    check_caller(server, headers)
    if path == '/api/events':
        _only(payload, ('event',), '{event}')
        if 'event' not in payload or not isinstance(payload['event'], dict):
            raise _bad('Send {event} with one event object.', 'event')
        event = payload['event']
        if event.get('source') != _PAGE_SOURCE:
            raise EventLogError('The page can add events from the student only. The app and the agent write their own events.',
                                'source_forbidden', 403, field='event.source')
        if event.get('type') == _AGENT_TYPE:
            raise EventLogError('The page cannot add an agent_offer event. The agent writes its own offers.',
                                'type_forbidden', 403, field='event.type')
        return 201, events.append(event, from_page=True)
    if path == '/api/reflections':
        _only(payload, ('text',), '{text}')
        if 'text' not in payload:
            raise _bad('Send {text}.', 'text')
        return 201, events.save_reflection(payload['text'])
    return None


def handle_delete(server, events, path, payload, headers):
    check_caller(server, headers)
    if path == '/api/events':
        _only(payload, ('confirm',), '{confirm: true}')
        return 200, events.delete(payload.get('confirm', False))
    return None
