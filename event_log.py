"""Store learning events in one local append-only log.

Everything lives under one folder, `_events/`, inside the data folder. A curriculum
id cannot start with an underscore, so the folder never collides with a curriculum
folder, and older builds ignore it.

    _events/
      events.jsonl         the log, one event per line (the format is in contracts/learning-events.md)
      .events.lock         the lock file; it holds no data and is never deleted

Rules
-----
* Every event is checked by `learning_event`, the validator of the contract. This
  module holds no copy of the rules. An invalid event writes nothing.
* One writer at a time. A write holds an exclusive `flock` on `.events.lock`. A reader
  holds a shared one, so it never sees a line that a writer has half written.
  The lock works between threads and between processes of one computer. It does not
  stop a program that ignores the lock and edits the file.
* A write is one `write` call in append mode, then `fsync`. A new log also flushes
  its folder. A failed step is an error, never a success.
* The caps of the contract are enforced on write: 4096 bytes per line, 5 MB per file,
  50000 lines, and a unique event id. A write that would pass a cap is refused with
  `log_full`. The store never rotates the log and never deletes a line.
* A log that does not end in a newline (a cut line from a crash or a short write)
  gets one newline before the next event. Earlier bytes are never rewritten.
* A read reports damage and never repairs it. `delete` is the only way the store
  removes the log, and only with `confirm=True`.
* Reflection text is kept in separate files, one for each reflection, apart from the log.

Only the Python standard library is used. macOS and Linux are supported. Windows has no
`fcntl` and every write there fails with `lock_unavailable`.
"""
import contextlib
import errno
import json
import os
from pathlib import Path
import stat
import time

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None

import learning_event as le
from lesson_store import _fsync_dir
from local_store import StoreError

EVENTS_DIRECTORY = '_events'
LOG_FILE = 'events.jsonl'
LOCK_FILE = '.events.lock'
LOCK_TIMEOUT_SECONDS = 2.0
_LOCK_POLL_SECONDS = 0.005

_REFUSALS = {'invalid': ('invalid_event', 422), 'unsupported_version': ('unsupported_version', 409), 'too_large': ('event_too_large', 413)}


class EventLogError(StoreError):
    """An event log problem with a stable code. `details` holds optional findings."""

    def __init__(self, message, code, status=500, field=None, details=None, retryable=False):
        super().__init__(message, code, status, field, retryable)
        self.details = details

    def payload(self):
        result = super().payload()
        if self.details is not None:
            result['details'] = self.details
        return result


def _errno_name(error):
    """The error code of an OSError, for a message. A path or a file name never goes in a message."""
    return errno.errorcode.get(error.errno, 'unknown') if error.errno else 'unknown'


class _State:
    """What one pass over the log file found, tied to the file it came from."""

    def __init__(self, signature, size, lines, ends_clean, ids):
        self.signature = signature  # (size, modified time, inode); None when the file does not exist
        self.size = size
        self.lines = lines
        self.ends_clean = ends_clean  # true when the file is empty or ends in a newline
        self.ids = ids  # every event id on a line that parses, valid or not

    @property
    def missing(self):
        return self.signature is None


def _signature(info):
    return (info.st_size, info.st_mtime_ns, info.st_ino)


def _scan(data, signature):
    """Count lines and collect event ids from the bytes of a log. Lines that do not parse add no id.

    A line that does not parse is damage. `read` reports it, so skipping it here hides nothing.
    """
    pieces = data.split(b'\n')
    ends_clean = not data or data.endswith(b'\n')
    lines = len(pieces) - 1 if ends_clean else len(pieces)
    ids = set()
    for piece in pieces:
        # Like the validators, do not parse a line over the limit. An id there counts for nothing.
        if not piece or len(piece) > le.MAX_LINE_BYTES:
            continue
        try:
            document = json.loads(piece.decode('utf-8'))
        except (UnicodeError, ValueError, RecursionError):
            continue
        if isinstance(document, dict) and isinstance(document.get('eventId'), str):
            ids.add(document['eventId'])
    return _State(signature, len(data), lines, ends_clean, ids)


class EventLog:
    def __init__(self, data_dir, lock_timeout=LOCK_TIMEOUT_SECONDS):
        self.data = Path(data_dir).absolute()
        self.folder = self.data / EVENTS_DIRECTORY
        self.path = self.folder / LOG_FILE
        self.lock_timeout = lock_timeout
        self._state = None  # the last scan of the file, reused while the file is unchanged

    # ------------------------------------------------------------------
    # Paths and low level file work
    # ------------------------------------------------------------------

    def _safe(self, path):
        path = Path(path)
        current = self.data
        for part in path.relative_to(self.data).parts:
            current = current / part
            if current.is_symlink():
                raise EventLogError('Data paths cannot be symbolic links.', 'unsafe_path')
        return path

    def _make_folder(self):
        """Create the data folder and `_events/`, and flush the entry of each new folder."""
        self._safe(self.folder)
        try:
            self.data.mkdir(parents=True, exist_ok=True)
            os.mkdir(self.folder, 0o700)
        except FileExistsError:
            return
        except OSError as error:
            raise EventLogError(f'Schola could not create the event folder ({_errno_name(error)}). Nothing was written.',
                                'write_failed', details={'partialLine': False, 'maybeWritten': False}) from error
        try:
            _fsync_dir(self.data)
        except OSError as error:
            raise EventLogError(f'Schola could not flush the event folder to disk ({_errno_name(error)}). No event was written.',
                                'fsync_failed', details={'partialLine': False, 'maybeWritten': False}) from error

    @contextlib.contextmanager
    def _locked(self, exclusive, create=True):
        """Hold the log lock. A reader passes `create=False` and goes on unlocked when no lock file exists, because no writer ever ran."""
        descriptor = None
        if fcntl is None:
            raise EventLogError('This system cannot lock the event log. Nothing was changed.', 'lock_unavailable', 503)
        try:
            flags = os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0) | (os.O_CREAT if create else 0)
            descriptor = os.open(self.folder / LOCK_FILE, flags, 0o600)
        except FileNotFoundError:
            if create:
                raise EventLogError('Schola could not open the event log lock. Nothing was changed.', 'lock_unavailable', 503, retryable=True) from None
        except OSError as error:
            raise EventLogError(f'Schola could not open the event log lock ({_errno_name(error)}). Nothing was changed.',
                                'lock_unavailable', 503, retryable=True) from error
        try:
            if descriptor is not None:
                self._take(descriptor, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
            yield
        finally:
            if descriptor is not None:
                os.close(descriptor)  # closing the descriptor releases the lock

    def _take(self, descriptor, mode):
        deadline = time.monotonic() + self.lock_timeout
        while True:
            try:
                fcntl.flock(descriptor, mode | fcntl.LOCK_NB)
                return
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise EventLogError('Another program is using the event log. Try again in a moment. Nothing was changed.',
                                        'lock_unavailable', 503, retryable=True) from None
                time.sleep(_LOCK_POLL_SECONDS)
            except OSError as error:
                raise EventLogError(f'Schola could not lock the event log ({_errno_name(error)}). Nothing was changed.',
                                    'lock_unavailable', 503, retryable=True) from error

    def _stat_log(self):
        """The stat of the log file, or None when there is none. Anything but a regular file is `log_corrupt`."""
        self._safe(self.path)
        try:
            info = os.lstat(self.path)
        except FileNotFoundError:
            return None
        except OSError as error:
            raise EventLogError(f'The event log cannot be read ({_errno_name(error)}).', 'log_unreadable') from error
        if not stat.S_ISREG(info.st_mode):
            raise EventLogError('The event log is not a regular file. Schola left it as it is.', 'log_corrupt')
        return info

    def _read_log(self, oversize_code):
        """Read the whole log. Returns (bytes, stat), or None when there is no log. A file over the size limit raises `oversize_code`."""
        self._safe(self.path)
        try:
            descriptor = os.open(self.path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
        except FileNotFoundError:
            return None
        except OSError as error:
            if error.errno == errno.ELOOP:
                raise EventLogError('Data paths cannot be symbolic links.', 'unsafe_path') from error
            raise EventLogError(f'The event log cannot be read ({_errno_name(error)}).', 'log_unreadable') from error
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode):
                raise EventLogError('The event log is not a regular file. Schola left it as it is.', 'log_corrupt')
            if info.st_size > le.MAX_BYTES:
                raise EventLogError('The event log is over its 5 MB limit. Schola left it as it is.', oversize_code, 409 if oversize_code == 'log_full' else 500)
            with os.fdopen(descriptor, 'rb') as handle:
                descriptor = None  # the file object owns it now
                data = handle.read(le.MAX_BYTES + 1)
        except OSError as error:
            raise EventLogError(f'The event log cannot be read ({_errno_name(error)}).', 'log_unreadable') from error
        finally:
            if descriptor is not None:
                os.close(descriptor)
        if len(data) > le.MAX_BYTES:
            raise EventLogError('The event log is over its 5 MB limit. Schola left it as it is.', oversize_code, 409 if oversize_code == 'log_full' else 500)
        return data, info

    def _load_state(self):
        """The scan of the log as it is now. The cached scan is used only when the file is unchanged. Call with the lock held."""
        info = self._stat_log()
        if info is None:
            self._state = None
            return _State(None, 0, 0, True, set())
        if self._state is not None and self._state.signature == _signature(info):
            return self._state
        self._state = None
        found = self._read_log('log_full')
        if found is None:
            return _State(None, 0, 0, True, set())
        data, read_info = found
        self._state = _scan(data, _signature(read_info))
        return self._state

    # ------------------------------------------------------------------
    # Append
    # ------------------------------------------------------------------

    @staticmethod
    def _line_for(event):
        """The bytes of one line for an event, with the newline, after the validator has accepted it. Returns (bytes, event id)."""
        try:
            text = json.dumps(event, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
        except (TypeError, ValueError, RecursionError):
            raise EventLogError('An event must be a JSON object. Nothing was written.', 'invalid_event', 422) from None
        result = le.validate_event_text(text)
        if not result['ok']:
            code, status = _REFUSALS[result['reason']]
            raise EventLogError('The event does not follow the learning event contract. Nothing was written.', code, status, details=result['errors'])
        try:
            line = text.encode('utf-8')
        except UnicodeError:
            raise EventLogError('An event must be UTF-8 text. Nothing was written.', 'invalid_event', 422) from None
        # The validator already refuses a longer line. This keeps the file rule true if the validator ever changes.
        if len(line) > le.MAX_LINE_BYTES:
            raise EventLogError('The event is over the 4096 byte line limit. Nothing was written.', 'event_too_large', 413)
        return line + b'\n', json.loads(text)['eventId']

    def append(self, event):
        """Check one event and add it to the log as one line. Returns `{appended, eventId, line, bytes, newlineAdded}`.

        Raises `EventLogError`. After `write_failed` or `fsync_failed` the error `details` say whether a
        partial last line may be in the file. The next append treats such a line as a truncated tail.
        """
        line, event_id = self._line_for(event)
        self._safe(self.path)
        self._make_folder()
        with self._locked(exclusive=True):
            return self._append_locked(event_id, line)

    def _append_locked(self, event_id, line):
        state = self._load_state()
        if event_id in state.ids:
            raise EventLogError('An event with this id is already in the log. Nothing was written.', 'duplicate_event_id', 409, field='eventId')
        newline = b'' if state.ends_clean else b'\n'
        data = newline + line
        if state.lines + 1 > le.MAX_LINES or state.size + len(data) > le.MAX_BYTES:
            raise EventLogError('The event log is full. Schola does not rotate or delete it. Nothing was written.', 'log_full', 409)
        try:
            descriptor = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        except OSError as error:
            raise EventLogError(f'Schola could not open the event log for writing ({_errno_name(error)}). Nothing was written.',
                                'write_failed', details={'partialLine': False, 'maybeWritten': False}) from error
        self._state = None  # from here the file may change, so the next call scans it again
        try:
            try:
                written = os.write(descriptor, data)
            except OSError as error:
                raise EventLogError(f'Schola could not write the event ({_errno_name(error)}). Nothing was written.',
                                    'write_failed', details={'partialLine': False, 'maybeWritten': False}) from error
            if written != len(data):
                partial = written > 0
                raise EventLogError('The write stopped early. The log may end in a partial line. The next event starts on a new line.' if partial
                                    else 'The write stopped early. Nothing was written.',
                                    'write_failed', details={'partialLine': partial, 'maybeWritten': partial})
            try:
                os.fsync(descriptor)
            except OSError as error:
                raise EventLogError(f'Schola could not flush the event to disk ({_errno_name(error)}). The event may be in the log. Read the log before you retry.',
                                    'fsync_failed', details={'partialLine': False, 'maybeWritten': True}) from error
            info = os.fstat(descriptor)
        except BaseException:
            with contextlib.suppress(OSError):
                os.close(descriptor)  # the first error is the one to report
            raise
        try:
            os.close(descriptor)
        except OSError as error:
            raise EventLogError(f'Schola could not close the event log ({_errno_name(error)}). The event may be in the log. Read the log before you retry.',
                                'fsync_failed', details={'partialLine': False, 'maybeWritten': True}) from error
        if state.missing:
            try:
                _fsync_dir(self.folder)
            except OSError as error:
                raise EventLogError(f'Schola could not flush the new log to disk ({_errno_name(error)}). The event may be in the log. Read the log before you retry.',
                                    'fsync_failed', details={'partialLine': False, 'maybeWritten': True}) from error
        state.ids.add(event_id)
        self._state = _State(_signature(info), info.st_size, state.lines + 1, True, state.ids)
        return {'appended': True, 'eventId': event_id, 'line': state.lines + 1, 'bytes': len(line), 'newlineAdded': bool(newline)}

    # ------------------------------------------------------------------
    # Read, export, delete
    # ------------------------------------------------------------------

    def _read_locked(self):
        self._safe(self.path)
        if not self.folder.is_dir():
            return None
        with self._locked(exclusive=False, create=False):
            return self._read_log('log_corrupt')

    def read(self):
        """The per-line report of the log: `{ok, exists, lines, events, truncatedTail}`. Reads only; repairs nothing.

        `lines` is the validator report, with a reason and error pointers for each damaged line. `events`
        holds the valid events with their line numbers. A missing log reads as empty with `exists` false.
        """
        found = self._read_locked()
        if found is None:
            return {'ok': True, 'exists': False, 'lines': [], 'events': [], 'truncatedTail': False}
        data = found[0]
        report = le.validate_log_text(data)
        if 'reason' in report:
            # The validator refuses a log it cannot split into lines. The store only writes within the limits, so another program made this one.
            code = 'log_unreadable' if report['reason'] == 'unreadable' else 'log_corrupt'
            raise EventLogError('The event log is over its limits or cannot be read. Schola left it as it is.', code)
        pieces = data.split(b'\n')
        events = [{'line': item['line'], 'event': json.loads(pieces[item['line'] - 1])} for item in report['lines'] if item['ok']]
        return {'ok': report['ok'], 'exists': True, 'lines': report['lines'], 'events': events, 'truncatedTail': report['truncatedTail']}

    def export(self):
        """The exact bytes of the log, damage and all. Raises `log_missing` when there is no log."""
        found = self._read_locked()
        if found is None:
            raise EventLogError('There is no event log.', 'log_missing', 404)
        return found[0]

    def delete(self, confirm=False):
        """Remove the log file. Only `confirm=True` does it. Returns `{deleted: True}` or raises. The lock file and reflections stay."""
        if confirm is not True:
            raise EventLogError('Confirm before the event log is deleted. This cannot be undone.', 'confirmation_required', 400, field='confirm')
        self._safe(self.path)
        if not self.folder.is_dir():
            raise EventLogError('There is no event log.', 'log_missing', 404)
        with self._locked(exclusive=True):
            if self._stat_log() is None:
                raise EventLogError('There is no event log.', 'log_missing', 404)
            self._state = None
            try:
                os.unlink(self.path)
            except OSError as error:
                raise EventLogError(f'Schola could not delete the event log ({_errno_name(error)}). The log is unchanged.', 'delete_failed') from error
            try:
                _fsync_dir(self.folder)
            except OSError as error:
                raise EventLogError(f'The event log was removed, but Schola could not flush the folder to disk ({_errno_name(error)}).',
                                    'fsync_failed', details={'removed': True}) from error
        return {'deleted': True}
