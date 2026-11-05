"""Consent for checks that run a creator's script.

A script check runs code that a lesson author wrote. The student must see what
runs and agree first. This module holds the pieces that make that agreement
exact:

* `check_hash` is the SHA-256 of everything that would run: the runner
  descriptor, the runtime, the arguments, the time limit, and the script bytes.
  The description and the ids are not part of it, because they do not run.
* A consent record ties one student agreement to the module, the version, the
  check, that exact hash, and the real path of the workspace. Change any of
  them and the consent no longer applies.
* Records live in memory only. They end when the server stops.

What this does not do: it cannot tell the student's click from another program
on the same computer that can call the same local routes. It guards against
hostile lesson content. It is consent, not isolation.

Only the Python standard library is used.
"""
import hashlib
import json
import secrets
import threading
from collections import OrderedDict

# Runner types that run code written by a lesson author. Each one needs consent.
TIER2_TYPES = frozenset({'script'})
DEFAULT_TIMEOUT_SECONDS = 30
MAX_RECORDS = 200


def needs_consent(check):
    """True for a check that carries a script to run. A version 1.0 script runner has no script, so it needs no consent."""
    runner = check.get('runner') if isinstance(check, dict) else None
    return (isinstance(check, dict) and check.get('kind') == 'automated' and isinstance(runner, dict)
            and runner.get('type') in TIER2_TYPES and ('runtime' in check or 'source' in check))


def source_bytes(check):
    """The script, as the bytes that would be written to disk. Empty when the check has none."""
    source = check.get('source') if isinstance(check, dict) else None
    return source.encode('utf-8', errors='surrogatepass') if isinstance(source, str) else b''


def canonical_descriptor(check):
    """The parts of a check that run, as stable JSON bytes."""
    timeout = check.get('timeoutSeconds', DEFAULT_TIMEOUT_SECONDS)
    if isinstance(timeout, float) and timeout.is_integer():
        timeout = int(timeout)  # 30.0 and 30 are the same time limit
    parts = {'runner': check.get('runner'), 'args': check.get('args', []), 'timeoutSeconds': timeout}
    if 'runtime' in check:
        parts['runtime'] = check['runtime']
    return json.dumps(parts, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode('ascii')


def check_hash(check):
    """The hex SHA-256 of the canonical descriptor, a newline, and the script bytes."""
    return hashlib.sha256(canonical_descriptor(check) + b'\n' + source_bytes(check)).hexdigest()


class ConsentRegistry:
    """The agreements of this server session. Thread safe, bounded, and never written to disk."""

    def __init__(self, limit=MAX_RECORDS):
        self._limit = limit
        self._lock = threading.Lock()
        self._records = OrderedDict()

    def grant(self, module_id, version, check_id, digest, workspace):
        binding = (module_id, version, check_id, digest, workspace)
        with self._lock:
            for record in self._records.values():
                if record['binding'] == binding:
                    return self._public(record)
            consent_id = secrets.token_urlsafe(18)
            self._records[consent_id] = {'consentId': consent_id, 'binding': binding}
            while len(self._records) > self._limit:
                self._records.popitem(last=False)
            return self._public(self._records[consent_id])

    def find(self, module_id, version, check_id, digest, workspace, consent_id=None):
        """The consent with this id, if it was given for exactly this binding. The id is required."""
        if not isinstance(consent_id, str) or not consent_id:
            return None
        binding = (module_id, version, check_id, digest, workspace)
        given = consent_id.encode('utf-8', errors='replace')
        with self._lock:
            found = None
            for record in self._records.values():
                # Compare against every record, so the time does not say which id was close.
                if secrets.compare_digest(given, record['consentId'].encode('utf-8')) and record['binding'] == binding:
                    found = record
            return self._public(found) if found is not None else None

    def records(self):
        with self._lock:
            return [self._public(record) for record in self._records.values()]

    def clear(self):
        with self._lock:
            self._records.clear()

    @staticmethod
    def _public(record):
        module_id, version, check_id, digest, workspace = record['binding']
        return {'consentId': record['consentId'], 'moduleId': module_id, 'version': version, 'checkId': check_id,
                'checkHash': digest, 'workspace': workspace}
