"""The hub route of the local server, kept apart from `server.py`.

    GET /api/hub?tzOffsetMinutes=N    the numbers behind the hub, read only
    GET /api/hub?timeZone=NAME        the same, with the student's day set by an IANA zone such as America/New_York

`server.py` applies the Host and Origin rules first. The route then needs the session token in the
`X-Schola-Check-Token` header and refuses a browser request that says it came from another site. These are
the rules of the event routes. The route has no body, writes nothing, and keeps nothing between requests.

`tzOffsetMinutes` is the student's offset east of UTC in whole minutes, from -840 to 840. The default is 0.
`timeZone` is an IANA zone name. Each event then gets the offset the zone had at that moment. Send one of the two, not both.
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
    """The offset from the query, 0 when there is none. It is a plain whole number, sent once."""
    values = (query or {}).get('tzOffsetMinutes')
    if values is None:
        return 0
    message = f'Send tzOffsetMinutes as a whole number from {hub_data.MIN_OFFSET} to {hub_data.MAX_OFFSET}.'
    if len(values) != 1 or not _OFFSET.fullmatch(values[0]):
        raise _bad(message, 'tzOffsetMinutes')
    value = int(values[0])
    if not hub_data.MIN_OFFSET <= value <= hub_data.MAX_OFFSET:
        raise _bad(message, 'tzOffsetMinutes')
    return value


def parse_zone(query):
    """The time zone from the query, or None. A name that does not match the strict pattern or is not in the zone database is refused."""
    values = (query or {}).get('timeZone')
    if values is None:
        return None
    message = 'Send timeZone as an IANA zone name such as America/New_York.'
    if len(values) != 1:
        raise _bad(message, 'timeZone')
    try:
        return hub_data.load_zone(values[0])
    except ValueError:
        raise _bad(message, 'timeZone') from None


def parse_query(query):
    """(offset, zone) from the query. Only `tzOffsetMinutes` or `timeZone` is allowed, and not both."""
    query = query or {}
    if set(query) - {'tzOffsetMinutes', 'timeZone'}:
        raise _bad('Send only tzOffsetMinutes or timeZone.', 'query')
    if 'tzOffsetMinutes' in query and 'timeZone' in query:
        raise _bad('Send tzOffsetMinutes or timeZone, not both.', 'query')
    return parse_offset(query), parse_zone(query)


def handle_get(server, lessons, events, path, headers, query=None):
    """Answer GET /api/hub (the only path in GET_ROUTES). The caller is checked first, then the query, and only then is a store read."""
    event_routes.check_caller(server, headers)
    offset, zone = parse_query(query)
    return 200, hub_data.build_hub(events, lessons, _utc_now(), offset, zone)
