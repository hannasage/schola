"""The check routes of the local server, kept apart from `server.py`.

Routes (all on the local address). `server.py` applies the Host and Origin
rules first. Every route except the first also needs the session token in the
`X-Schola-Check-Token` header.

    GET  /api/checks/session    the per-launch token
    GET  /api/checks/workspace  the folder checks read, or null
    POST /api/checks/workspace  {path}: set the folder
    POST /api/checks/inspect    {moduleId, version, checkId}: what would run, and its hash
    POST /api/checks/consent    {moduleId, version, checkId, confirm: true, shownHash}: agree to a script
    POST /api/checks/run        {moduleId, version, checkId, consentId?}: run one check

A request body for these routes may be up to 64 KB.

What the token and the consent guarantee, and what they do not
--------------------------------------------------------------
The token is random for each launch and is sent only to a request that passes
the Host and Origin checks. A page on another site cannot read it. Consent is
created only by the consent route. The run route has no field and no header
that grants consent, so a lesson, or a caller that can only reach the run
route, cannot agree for the student.

Neither one can tell the student's click from another program on the same
computer. That program can call `GET /api/checks/session` and then the consent
route with any headers it likes. It already has the student's permissions. So
the guarantee is against hostile lesson content, not against a hostile local
program.
"""
import hmac
import json
import os
import secrets
import threading
from pathlib import Path

import check_consent
import check_runner
import check_script
from lesson_store import LessonStoreError
from local_store import StoreError

BODY_LIMIT = 64 * 1024
TOKEN_HEADER = 'X-Schola-Check-Token'
SETTING_FILE = 'check-workspace.json'
SETTING_VERSION = 1
MAX_PATH_CHARS = 4096
GET_ROUTES = ('/api/checks/session', '/api/checks/workspace')
POST_ROUTES = ('/api/checks/workspace', '/api/checks/inspect', '/api/checks/consent', '/api/checks/run')
_ID_FIELDS = ('moduleId', 'version', 'checkId')
_SESSION_LOCK = threading.Lock()


def is_check_route(path):
    return path in GET_ROUTES or path in POST_ROUTES


class CheckError(StoreError):
    """A check route problem with a stable code."""


def _bad(message, field='request', code='invalid_request', status=400):
    return CheckError(message, code, status, field)


class CheckSession:
    """Everything that lives for one launch of the server: the token, the consents, and the run lock."""

    def __init__(self, registry=None, limits=None, home=None):
        self.token = secrets.token_urlsafe(32)
        self.consents = check_consent.ConsentRegistry()
        self.run_lock = threading.Lock()
        self.registry = registry
        self.limits = limits
        self.home = Path(home) if home is not None else Path.home()

    def adapters(self):
        return self.registry or check_runner.default_registry()


def session_for(server):
    """The session of a server. It is created at the first use, or at start by `server.py`."""
    session = getattr(server, 'check_session', None)
    if session is None:
        with _SESSION_LOCK:
            session = getattr(server, 'check_session', None)
            if session is None:
                session = server.check_session = CheckSession()
    return session


# ---------------------------------------------------------------------------
# Who may call
# ---------------------------------------------------------------------------

def check_fetch_site(headers):
    """A browser that says the request came from another site is refused. Other callers send no such header."""
    site = headers.get('Sec-Fetch-Site')
    if site is not None and site not in ('same-origin', 'none'):
        raise CheckError('Use the local application address.', 'forbidden_origin', 403)


def check_token(session, headers):
    sent = headers.get(TOKEN_HEADER)
    given = sent.encode('utf-8', errors='replace') if isinstance(sent, str) else b''
    expected = session.token.encode('utf-8')
    # compare_digest takes time that depends only on the length of its inputs. The result is combined, not branched on, per part.
    if not (hmac.compare_digest(given, expected) and sent):
        raise CheckError('The check session token is missing or wrong. Reload Schola and try again.', 'invalid_token', 403)


# ---------------------------------------------------------------------------
# The workspace setting
# ---------------------------------------------------------------------------

def _inside(path, folder):
    """True when the real path `path` is `folder` or below it."""
    return path == folder or path.startswith(folder if folder.endswith(os.sep) else folder + os.sep)


def validate_workspace(path, data_dir, home):
    """The real path of a folder that is safe to use as a workspace, or a CheckError."""
    problem = 'Enter the full path of an existing folder.'
    if (not isinstance(path, str) or not path or len(path) > MAX_PATH_CHARS or not os.path.isabs(path)
            or any(ord(ch) < 32 or ord(ch) == 127 for ch in path)):
        raise _bad(problem, 'path', 'invalid_workspace')
    try:
        real = os.path.realpath(path, strict=True)
        if not os.path.isdir(real) or not os.access(real, os.R_OK | os.X_OK):
            raise _bad('That path is not a folder Schola can read.', 'path', 'invalid_workspace')
    except (OSError, ValueError):
        raise _bad(problem, 'path', 'invalid_workspace') from None
    home_real = os.path.realpath(home)
    data_real = os.path.realpath(data_dir)
    if os.path.dirname(real) == real:
        raise _bad('Choose a project folder, not the top of the disk.', 'path', 'invalid_workspace')
    if _inside(home_real, real):
        raise _bad('Choose a folder inside your home folder, not your home folder or a folder that holds it.', 'path', 'invalid_workspace')
    if not _inside(real, home_real):
        raise _bad('Choose a folder inside your home folder. Schola does not read folders outside it.', 'path', 'invalid_workspace')
    if any(part.startswith('.') for part in os.path.relpath(real, home_real).split(os.sep)):
        raise _bad('Choose a folder that is not hidden. A folder whose name starts with a dot is not allowed.', 'path', 'invalid_workspace')
    if _inside(real, data_real) or _inside(data_real, real):
        raise _bad('Choose a folder that does not hold Schola\'s own data, and is not inside it.', 'path', 'invalid_workspace')
    return real


def _setting_path(lessons):
    return lessons._safe(lessons.root / SETTING_FILE)


def saved_workspace(lessons, session):
    """{path, configured, usable, problem?}. Reads the saved setting and checks that it is still safe."""
    path = _setting_path(lessons)
    if not path.is_file():
        return {'path': None, 'configured': False, 'usable': False}
    saved = lessons._read_json(path)
    value = saved.get('path') if isinstance(saved, dict) and saved.get('version') == SETTING_VERSION else None
    if not isinstance(value, str):
        raise LessonStoreError('The saved workspace setting is damaged. The file was preserved.', 'corrupt_file', 500)
    try:
        return {'path': validate_workspace(value, lessons.data, session.home), 'configured': True, 'usable': True}
    except CheckError as error:
        return {'path': value, 'configured': True, 'usable': False, 'problem': str(error)}


def _usable_workspace(lessons, session):
    found = saved_workspace(lessons, session)
    if not found['usable']:
        message = found.get('problem') or 'Choose the folder where you do your work before you run a check.'
        raise CheckError(message, 'workspace_required', 409, 'path')
    return found['path']


def set_workspace(lessons, session, payload):
    if not isinstance(payload, dict) or set(payload) != {'path'}:
        raise _bad('Send {path}.', 'request')
    real = validate_workspace(payload['path'], lessons.data, session.home)
    data = (json.dumps({'version': SETTING_VERSION, 'path': real}, indent=2, sort_keys=True) + '\n').encode('utf-8')
    lessons._atomic(_setting_path(lessons), data)
    return {'path': real, 'configured': True, 'usable': True}


# ---------------------------------------------------------------------------
# Finding a check
# ---------------------------------------------------------------------------

def _reference(payload, extra=()):
    allowed = set(_ID_FIELDS) | set(extra)
    if not isinstance(payload, dict) or not set(_ID_FIELDS) <= set(payload) or set(payload) - allowed:
        raise _bad(f'Send {{{", ".join(_ID_FIELDS + tuple(extra))}}}.', 'request')
    if any(not isinstance(payload[name], str) for name in _ID_FIELDS):
        raise _bad('The module, version, and check must be text.', 'request')
    return payload['moduleId'], payload['version'], payload['checkId']


def _find_check(lessons, module_id, version, check_id):
    document = lessons.get_module(module_id, version)
    for check in document['checks']:
        if check['id'] == check_id:
            return check
    raise CheckError('That check is not in that module version.', 'not_found', 404, 'checkId')


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

def _shown(check, workspace, digest, limits=None):
    source = check.get('source')
    timeout = check.get('timeoutSeconds', check_consent.DEFAULT_TIMEOUT_SECONDS)
    if isinstance(timeout, float) and timeout.is_integer():
        timeout = int(timeout)
    planned, missing = ([], [])
    if check_consent.needs_consent(check) and isinstance(timeout, int) and not isinstance(timeout, bool):
        planned, missing = check_script.describe_limits(limits or check_runner.Limits(), timeout)
    return {
        'needsConsent': check_consent.needs_consent(check),
        'checkHash': digest,
        'runnerType': (check.get('runner') or {}).get('type'),
        'description': check.get('description'),
        'runtime': check.get('runtime'),
        'source': source if isinstance(source, str) else None,
        'args': list(check.get('args', [])),
        'timeoutSeconds': timeout,
        'workspace': workspace,
        'limits': planned,
        'limitsNotApplied': missing,
    }


def handle_get(server, lessons, path, headers):
    session = session_for(server)
    check_fetch_site(headers)
    if path == '/api/checks/session':
        return 200, {'token': session.token}
    check_token(session, headers)
    if path == '/api/checks/workspace':
        return 200, saved_workspace(lessons, session)
    return None


def handle_post(server, lessons, path, payload, headers):
    session = session_for(server)
    check_fetch_site(headers)
    check_token(session, headers)
    if path == '/api/checks/workspace':
        return 200, set_workspace(lessons, session, payload)
    if path == '/api/checks/inspect':
        module_id, version, check_id = _reference(payload)
        check = _find_check(lessons, module_id, version, check_id)
        workspace = _usable_workspace(lessons, session)
        return 200, _shown(check, workspace, check_consent.check_hash(check), session.limits)
    if path == '/api/checks/consent':
        return 200, _grant(session, lessons, payload, headers)
    if path == '/api/checks/run':
        return 200, _run(session, lessons, payload)
    return None


def _grant(session, lessons, payload, headers):
    module_id, version, check_id = _reference(payload, ('confirm', 'shownHash'))
    if 'confirm' not in payload or 'shownHash' not in payload:
        raise _bad('Send {moduleId, version, checkId, confirm, shownHash}.', 'request')
    # A browser always sends Origin on a POST, and server.py has already checked its value. A caller with no Origin is
    # not the Schola page. A local program can still fake the header, so this only stops careless callers.
    if not headers.get('Origin'):
        raise CheckError('Agree to a script from the Schola page.', 'forbidden_origin', 403)
    if payload['confirm'] is not True:
        raise CheckError('Confirm that you have read the script and agree to run it.', 'confirmation_required', 400, 'confirm')
    check = _find_check(lessons, module_id, version, check_id)
    if not check_consent.needs_consent(check):
        raise CheckError('This check runs no script. It needs no consent.', 'consent_not_needed', 400)
    workspace = _usable_workspace(lessons, session)
    digest = check_consent.check_hash(check)
    shown = payload['shownHash']
    if not isinstance(shown, str) or not hmac.compare_digest(shown.encode('utf-8', errors='replace'), digest.encode('ascii')):
        raise CheckError('The script changed since it was shown to you. Review it again.', 'hash_mismatch', 409, 'shownHash')
    return session.consents.grant(module_id, version, check_id, digest, workspace)


def _run(session, lessons, payload):
    module_id, version, check_id = _reference(payload, ('consentId',))
    consent_id = payload.get('consentId')
    if 'consentId' in payload and not isinstance(consent_id, str):
        raise _bad('The consent id must be text.', 'consentId')
    check = _find_check(lessons, module_id, version, check_id)
    workspace = _usable_workspace(lessons, session)
    digest = check_consent.check_hash(check)
    tier, consent = 1, None
    if check_consent.needs_consent(check):
        tier = 2
        consent = session.consents.find(module_id, version, check_id, digest, workspace, consent_id)
        if consent is None:
            raise CheckError('Review this script and agree before it runs.', 'consent_required', 403)
    if not session.run_lock.acquire(blocking=False):
        raise CheckError('Another check is running. Wait for it to finish.', 'busy', 429, retryable=True)
    try:
        outcome = check_runner.run(check, workspace, session.limits, session.adapters(), consent=consent)
    finally:
        session.run_lock.release()
    return {'moduleId': module_id, 'version': version, 'checkId': check_id, 'checkHash': digest, 'tier': tier, 'result': outcome}
