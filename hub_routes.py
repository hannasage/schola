"""The hub route of the local server, kept apart from `server.py`.

    GET /api/hub?tzOffsetMinutes=N    the numbers behind the hub, read only

`server.py` applies the Host and Origin rules first. The route then needs the session token in the
`X-Schola-Check-Token` header and refuses a browser request that says it came from another site. These are
the rules of the event routes. The route has no body, writes nothing, and keeps nothing between requests.

`tzOffsetMinutes` is the student's offset east of UTC in whole minutes, from -840 to 840. The default is 0.
The answer is built by `hub_data` (docs/hub-data.md) and typed in `types/hub.d.ts`.
"""
import datetime
import re

import event_routes
import hub_data
from local_store import StoreError

GET_ROUTES = ('/api/hub',)
_OFFSET = re.compile(r'0|-?[1-9][0-9]{0,2}', re.ASCII)


def _utc_now():
    """The clock of the route. Tests replace it. `hub_data` never reads a clock."""
    return datetime.datetime.now(datetime.timezone.utc)


def _bad(message, field):
    return StoreError(message, 'invalid_request', 400, field)


def parse_offset(query):
    """The offset from the query. Only `tzOffsetMinutes` is allowed, once, as a plain whole number."""
    query = query or {}
    if set(query) - {'tzOffsetMinutes'}:
        raise _bad('Send only tzOffsetMinutes.', 'query')
    values = query.get('tzOffsetMinutes')
    if values is None:
        return 0
    message = f'Send tzOffsetMinutes as a whole number from {hub_data.MIN_OFFSET} to {hub_data.MAX_OFFSET}.'
    if len(values) != 1 or not _OFFSET.fullmatch(values[0]):
        raise _bad(message, 'tzOffsetMinutes')
    value = int(values[0])
    if not hub_data.MIN_OFFSET <= value <= hub_data.MAX_OFFSET:
        raise _bad(message, 'tzOffsetMinutes')
    return value


def handle_get(server, lessons, events, path, headers, query=None):
    """Answer GET /api/hub (the only path in GET_ROUTES). The caller is checked first, then the query, and only then is a store read."""
    event_routes.check_caller(server, headers)
    offset = parse_offset(query)
    return 200, hub_data.build_hub(events, lessons, _utc_now(), offset)
