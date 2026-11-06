"""The numbers behind the hub, computed from the event log and the lesson store.

The hub is the home screen. This module answers one question: what does the student's own record say?
It holds two layers.

* `compute_hub` is pure. It takes events, facts about the log, the pinned modules, and a clock value, and
  it returns the response as plain data. It reads no file and no clock, so a test can give it any day.
* `build_hub` is the one thin function that reads the two stores. It reads the event log once.

The response is closed and versioned (`SCHEMA_VERSION`). It has no field for streaks, ranks, totals of time,
or any label about the student. The page that draws it (docs/hub-data.md) says how each number is made.

Only the Python standard library is used.
"""
import collections
import datetime
import re
import sys
import zoneinfo

from event_log import EventLogError
from lesson_store import LessonStoreError

SCHEMA_VERSION = '1.0'
WINDOW_WEEKS = 8
SKILL_WINDOW = 5
CHECK_LIMIT = 20
MIN_OFFSET = -840
MAX_OFFSET = 840
# The maxima of the lesson contract, so a valid module is never cut: a title is at most 200 characters, a task prompt
# at most 5000, and a check description at most 2000. The hub shows the description of a check as its title.
TITLE_CAP = 200
TASK_TEXT_CAP = 5000
CHECK_TEXT_CAP = 2000
SKILL_LIMIT = 100
HINT_LADDER = ('nudge', 'question', 'partial-example', 'full-answer')
CHECK_STATUSES = frozenset({'passed', 'failed', 'error', 'skipped', 'self-reported'})
# A check is passed by the app (`passed`) or by the student's own report (`self-reported`). A report is not skill evidence.
PASS_STATUSES = frozenset({'passed', 'self-reported'})
SKILL_STATUSES = frozenset({'passed', 'failed', 'error'})
TASK_RESULTS = frozenset({'passed', 'abandoned', 'skipped'})
SKIP_NOTES = 5  # how many skipped events the reader names on stderr, one line each
SOURCES = frozenset({'student', 'app', 'agent'})
# Only these two sources count as the student's activity. The agent's own events never do.
COUNTED_SOURCES = frozenset({'student', 'app'})
# Log faults that mean "this log cannot be read". Any other fault, such as a busy lock, is a real error.
UNREADABLE_CODES = frozenset({'log_corrupt', 'log_unreadable', 'unsafe_path'})

_LEVEL = {name: number for number, name in enumerate(HINT_LADDER)}
_TIME = re.compile(r'([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})(?:\.[0-9]{1,9})?(Z|[+-][0-9]{2}:[0-9]{2})', re.ASCII)
_ZONE_NAME = re.compile(r'[A-Za-z][A-Za-z0-9_+-]{0,31}(?:/[A-Za-z0-9_+-]{1,32}){0,2}', re.ASCII)
_EPOCH = datetime.datetime(1970, 1, 1, tzinfo=datetime.timezone.utc)
_MINUTES_PER_DAY = 1440
_WEEK_DAYS = 7


class TaskInfo(collections.namedtuple('TaskInfo', 'task_id text')):
    """One task of a pinned module: its id and its prompt."""
    __slots__ = ()


class CheckInfo(collections.namedtuple('CheckInfo', 'check_id title')):
    """One check of a pinned module: its id and the text the hub shows for it."""
    __slots__ = ()


class ModuleInfo(collections.namedtuple('ModuleInfo', 'module_id version title tasks checks')):
    """What the hub needs from one pinned module. `tasks` and `checks` are tuples in module order."""
    __slots__ = ()


class LogFacts(collections.namedtuple('LogFacts', 'exists ok events damaged_lines truncated_tail fault', defaults=(None,))):
    """What the log reader found. `events` counts the valid events, `damaged_lines` the lines that are not valid events.

    `fault` is the code of the log store when the log could not be read at all, else None.
    """
    __slots__ = ()


_Row = collections.namedtuple('_Row', 'seq day session module task kind skills status check level result')


class _Malformed(Exception):
    """An event does not have the fields the contract promises. `reason` names the field and holds no event content."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


# ----------------------------------------------------------------------
# Days. All the date work is whole-number arithmetic, so the extremes of the calendar cannot overflow.
# ----------------------------------------------------------------------

def _days_from_civil(year, month, day):
    """Days since 1970-01-01 of a date in the proleptic Gregorian calendar."""
    if month <= 2:
        year -= 1
    era = year // 400
    year_of_era = year - era * 400
    day_of_year = (153 * (month + (-3 if month > 2 else 9)) + 2) // 5 + day - 1
    day_of_era = year_of_era * 365 + year_of_era // 4 - year_of_era // 100 + day_of_year
    return era * 146097 + day_of_era - 719468


def _civil_from_days(days):
    days += 719468
    era = days // 146097
    day_of_era = days - era * 146097
    year_of_era = (day_of_era - day_of_era // 1460 + day_of_era // 36524 - day_of_era // 146096) // 365
    day_of_year = day_of_era - (365 * year_of_era + year_of_era // 4 - year_of_era // 100)
    shifted = (5 * day_of_year + 2) // 153
    day = day_of_year - (153 * shifted + 2) // 5 + 1
    month = shifted + (3 if shifted < 10 else -9)
    year = year_of_era + era * 400 + (1 if month <= 2 else 0)
    return year, month, day


def _days_in_month(year, month):
    if month == 2:
        return 29 if year % 4 == 0 and (year % 100 != 0 or year % 400 == 0) else 28
    return 30 if month in (4, 6, 9, 11) else 31


def _weekday(days):
    """0 for Monday to 6 for Sunday. 1970-01-01 was a Thursday."""
    return (days + 3) % 7


def _date_text(days):
    year, month, day = _civil_from_days(days)
    return f'{year:04d}-{month:02d}-{day:02d}'


def _utc_minutes(text):
    """Minutes since 1970-01-01T00:00Z of an RFC 3339 time, or None when the text is not a real time."""
    if type(text) is not str:
        return None
    found = _TIME.fullmatch(text)
    if found is None:
        return None
    year, month, day, hour, minute, second = (int(part) for part in found.groups()[:6])
    if not (1 <= month <= 12 and 1 <= day <= _days_in_month(year, month) and hour <= 23 and minute <= 59 and second <= 59):
        return None
    zone = found.group(7)
    offset = 0
    if zone != 'Z':
        zone_hours, zone_minutes = int(zone[1:3]), int(zone[4:6])
        if zone_hours > 23 or zone_minutes > 59:
            return None
        offset = (zone_hours * 60 + zone_minutes) * (-1 if zone[0] == '-' else 1)
    return _days_from_civil(year, month, day) * _MINUTES_PER_DAY + hour * 60 + minute - offset


def _clock_minutes(now):
    """The injected clock as minutes since the epoch (UTC), and its text for `generatedAt`."""
    if not isinstance(now, datetime.datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError('now must be a datetime with a time zone.')
    utc = now.astimezone(datetime.timezone.utc)
    minutes = _days_from_civil(utc.year, utc.month, utc.day) * _MINUTES_PER_DAY + utc.hour * 60 + utc.minute
    text = f'{utc.year:04d}-{utc.month:02d}-{utc.day:02d}T{utc.hour:02d}:{utc.minute:02d}:{utc.second:02d}Z'
    return minutes, text


def check_offset(value):
    """A time zone offset in whole minutes east of UTC, from -840 to 840. A bool is not a number here."""
    if type(value) is not int or not MIN_OFFSET <= value <= MAX_OFFSET:
        raise ValueError(f'The offset must be a whole number of minutes from {MIN_OFFSET} to {MAX_OFFSET}.')
    return value


def load_zone(name):
    """The time zone of an IANA name such as `America/New_York`. A bad or unknown name is a ValueError.

    The name must match a strict pattern before it is looked up, so it can never name a path.
    """
    if type(name) is not str or _ZONE_NAME.fullmatch(name) is None:
        raise ValueError('The time zone must be an IANA name such as America/New_York.')
    try:
        return zoneinfo.ZoneInfo(name)
    except (KeyError, ValueError, OSError):  # not found, not a zone file, or a folder
        raise ValueError('The time zone must be an IANA name such as America/New_York.') from None


def _fixed_day(offset):
    return lambda minutes: (minutes + offset) // _MINUTES_PER_DAY


def _zone_day(zone):
    """A function from UTC minutes to the local day number, using the offset the zone had at that moment."""
    def day_of(minutes):
        try:
            local = (_EPOCH + datetime.timedelta(minutes=minutes)).astimezone(zone)
        except (OverflowError, ValueError, OSError):
            # The instant is outside the years 1 to 9999 once placed in the zone. It is far from the window,
            # so the UTC day is as good as any: a zone is at most a day away.
            return minutes // _MINUTES_PER_DAY
        return _days_from_civil(local.year, local.month, local.day)
    return day_of


# ----------------------------------------------------------------------
# Reading the events
# ----------------------------------------------------------------------

def _text(value, name):
    if type(value) is not str or not value:
        raise _Malformed(f'bad {name}')
    return value


def _row(seq, item, day_of):
    """One counted event as a row, or None for an event of the agent. Raises _Malformed if a field the hub reads is wrong."""
    if type(item) is not dict:
        raise _Malformed('the event is not an object')
    source = item.get('source')
    if type(source) is not str or source not in SOURCES:
        raise _Malformed('bad source')
    if source not in COUNTED_SOURCES:
        return None
    stamp = _utc_minutes(item.get('time'))
    if stamp is None:
        raise _Malformed('bad time')
    session = _text(item.get('sessionId'), 'sessionId')
    module = _text(item.get('moduleId'), 'moduleId')
    task = item.get('taskId')
    if task is not None:
        _text(task, 'taskId')
    kind = _text(item.get('type'), 'type')
    skills = item.get('skills')
    if type(skills) is not list or any(type(skill) is not str for skill in skills):
        raise _Malformed('bad skills')
    data = item.get('data')
    if type(data) is not dict:
        raise _Malformed('bad data')
    status = check = level = result = None
    if kind == 'check_result':
        status, check = data.get('status'), data.get('checkId')
        if type(status) is not str or status not in CHECK_STATUSES:
            raise _Malformed('bad data.status')
        if task is None:
            raise _Malformed('missing taskId')
        _text(check, 'data.checkId')
    elif kind == 'hint_requested':
        level = data.get('level')
        if type(level) is not str or level not in _LEVEL:
            raise _Malformed('bad data.level')
        if task is None:
            raise _Malformed('missing taskId')
    elif kind == 'task_finished':
        result = data.get('result')
        if type(result) is not str or result not in TASK_RESULTS:
            raise _Malformed('bad data.result')
        if task is None:
            raise _Malformed('missing taskId')
    return _Row(seq, day_of(stamp), session, module, task, kind, tuple(dict.fromkeys(skills)), status, check, level, result)


def _rows(events, day_of, on_skip):
    """The counted events as rows, in log order, and the number of events skipped because a field was wrong."""
    rows, skipped = [], 0
    for seq, item in enumerate(events):
        try:
            row = _row(seq, item, day_of)
        except _Malformed as bad:
            skipped += 1
            if on_skip is not None:
                on_skip(seq, bad.reason)
            continue
        if row is not None:
            rows.append(row)
    return rows, skipped


def _cap(text, limit):
    if type(text) is not str:
        raise ValueError('Module text must be a string.')
    return text[:limit]


def _check_modules(modules):
    seen = set()
    for info in modules:
        if not isinstance(info, ModuleInfo) or type(info.module_id) is not str or type(info.version) is not str:
            raise ValueError('modules must hold ModuleInfo values.')
        if info.module_id in seen:
            raise ValueError('A module appears twice.')
        seen.add(info.module_id)
    return list(modules)


# ----------------------------------------------------------------------
# The rollup
# ----------------------------------------------------------------------

def compute_hub(events, log, modules, now, tz_offset_minutes=0, time_zone=None, on_skip=None):
    """The hub response as plain data.

    `events` are the valid events of the log as dicts, in log order. `log` is a `LogFacts`. `modules` are the
    pinned modules in the order the store lists them. `now` is a datetime with a time zone: the caller's clock.
    The student's day is set by `tz_offset_minutes` (a fixed offset east of UTC) or by `time_zone` (a tzinfo such
    as a ZoneInfo, which gives each event the offset it had at that moment), never both.
    `on_skip(seq, reason)` is called for each event that is left out because a field is wrong; `seq` is the
    position in `events`. Nothing here reads a file or a clock.
    """
    offset = check_offset(tz_offset_minutes)
    if not isinstance(log, LogFacts):
        raise ValueError('log must be a LogFacts value.')
    if time_zone is None:
        day_of = _fixed_day(offset)
    elif not isinstance(time_zone, datetime.tzinfo):
        raise ValueError('time_zone must be a tzinfo value.')
    elif offset != 0:
        raise ValueError('Give an offset or a time zone, not both.')
    else:
        day_of = _zone_day(time_zone)
    now_minutes, generated_at = _clock_minutes(now)
    modules = _check_modules(modules)
    today = day_of(now_minutes)
    rows, skipped = _rows(events, day_of, on_skip)
    future = sum(1 for row in rows if row.day > today)
    rows = [row for row in rows if row.day <= today]  # an event after today counts in nothing but futureEvents

    pinned = {info.module_id: info for info in modules}
    check_ids = {info.module_id: frozenset(check.check_id for check in info.checks) for info in modules}
    window_start = today - _weekday(today) - _WEEK_DAYS * (WINDOW_WEEKS - 1)
    sessions_by_day = {}
    completed, results, last_seq = set(), {}, {}
    hints, check_state, skill_state = {}, {}, {}
    unmatched = 0
    for row in rows:
        sessions_by_day.setdefault(row.day, set()).add(row.session)
        if row.kind == 'check_result' and row.status in SKILL_STATUSES:
            for skill in row.skills:
                entry = skill_state.setdefault(skill, [collections.deque(maxlen=SKILL_WINDOW), 0])
                entry[0].append(row.status == 'passed')
                entry[1] = row.seq
        module = row.module
        if module not in pinned:
            continue
        last_seq[module] = row.seq
        if row.kind == 'module_completed':
            completed.add(module)
        elif row.kind == 'task_finished':
            results.setdefault(module, {})[row.task] = row.result
        elif row.kind == 'hint_requested':
            key = (module, row.task)
            hints[key] = max(hints.get(key, -1), _LEVEL[row.level])
        elif row.kind == 'check_result':
            if row.check not in check_ids[module]:
                unmatched += 1
            elif row.status != 'skipped':
                state = check_state.setdefault((module, row.check), {'retries': 0, 'passed': False, 'last': 0, 'tasks': set(), 'level': -1})
                state['last'] = row.seq
                state['tasks'].add(row.task)
                if not state['passed']:
                    if row.status in PASS_STATUSES:
                        state['passed'] = True
                        # The hints that came before the pass, on the tasks this check has been run for.
                        state['level'] = max((hints.get((module, task), -1) for task in state['tasks']), default=-1)
                    else:
                        state['retries'] += 1

    focus_id = _choose_focus(modules, completed, last_seq)
    recent_skills = sorted(skill_state.items(), key=lambda item: item[1][1], reverse=True)[:SKILL_LIMIT]
    return {
        'schemaVersion': SCHEMA_VERSION,
        'generatedAt': generated_at,
        'log': {'exists': log.exists, 'ok': log.ok, 'fault': log.fault, 'events': log.events, 'damagedLines': log.damaged_lines,
                'truncatedTail': log.truncated_tail, 'skippedEvents': skipped, 'futureEvents': future, 'unmatchedEvents': unmatched},
        'focus': _focus(pinned.get(focus_id), results),
        'participation': _participation(sessions_by_day, window_start, today),
        'checkpoints': {'done': len(completed), 'total': len(modules)},
        'skills': [{'skillId': skill, 'passed': sum(entry[0]), 'tried': len(entry[0]), 'window': SKILL_WINDOW}
                   for skill, entry in sorted(recent_skills)],
        'skillsTotal': len(skill_state),
        'checks': _checks(check_state, pinned, hints),
        'checksTotal': len(check_state),
        'library': [_library_entry(info, completed, focus_id, results) for info in modules],
    }


def _choose_focus(modules, completed, last_seq):
    """The open module with the latest event, else the first open module, else None."""
    open_modules = [info.module_id for info in modules if info.module_id not in completed]
    if not open_modules:
        return None
    touched = [module for module in open_modules if module in last_seq]
    return max(touched, key=last_seq.__getitem__) if touched else open_modules[0]


def _task_counts(info, results):
    """(passed, skipped) task counts of a module. The latest result of a task wins. A result for a task the module lacks counts nowhere."""
    found = results.get(info.module_id, {})
    mine = {task.task_id for task in info.tasks}
    done = sum(1 for task_id in mine if found.get(task_id) == 'passed')
    skipped = sum(1 for task_id in mine if found.get(task_id) in ('abandoned', 'skipped'))
    return done, skipped


def _focus(info, results):
    if info is None:
        return None
    found = results.get(info.module_id, {})
    upcoming = next((task for task in info.tasks if task.task_id not in found), None)  # any finish moves past a task
    done, skipped = _task_counts(info, results)
    return {
        'moduleId': info.module_id, 'version': info.version, 'title': _cap(info.title, TITLE_CAP),
        'tasksTotal': len(info.tasks), 'tasksDone': done, 'tasksSkipped': skipped,
        'nextTask': None if upcoming is None else {'taskId': upcoming.task_id, 'text': _cap(upcoming.text, TASK_TEXT_CAP)},
    }


def _library_entry(info, completed, focus_id, results):
    done, skipped = _task_counts(info, results)
    return {'moduleId': info.module_id, 'version': info.version, 'title': _cap(info.title, TITLE_CAP),
            'status': 'done' if info.module_id in completed else 'focus' if info.module_id == focus_id else 'next',
            'tasksTotal': len(info.tasks), 'tasksDone': done, 'tasksSkipped': skipped}


def _participation(sessions_by_day, window_start, today):
    weeks = []
    for week in range(WINDOW_WEEKS):
        first = window_start + _WEEK_DAYS * week
        days = [len(sessions_by_day.get(day, ())) if day <= today else None for day in range(first, first + _WEEK_DAYS)]
        counted = [count for count in days if count is not None]
        weeks.append({'start': _date_text(first), 'days': days, 'sessions': sum(counted), 'activeDays': sum(1 for count in counted if count)})
    return {
        'windowWeeks': WINDOW_WEEKS,
        'weeks': weeks,
        'totals': {'sessions': sum(week['sessions'] for week in weeks), 'activeDays': sum(week['activeDays'] for week in weeks),
                   'days': today - window_start + 1},
    }


def _checks(check_state, pinned, hints):
    """Up to CHECK_LIMIT checks: the open ones first, then the most recent. They are listed newest first."""
    chosen = sorted(check_state.items(), key=lambda item: (item[1]['passed'], -item[1]['last']))[:CHECK_LIMIT]
    chosen.sort(key=lambda item: item[1]['last'], reverse=True)
    rows = []
    for (module, check_id), state in chosen:
        title = next(check.title for check in pinned[module].checks if check.check_id == check_id)
        if state['passed']:
            reached = state['level']
        else:  # not passed yet: every hint so far counts
            reached = max((hints.get((module, task), -1) for task in state['tasks']), default=-1)
        rows.append({'moduleId': module, 'checkId': check_id, 'title': _cap(title, CHECK_TEXT_CAP),
                     'retriesBeforeFirstPass': state['retries'], 'open': not state['passed'],
                     'hintLevelReached': HINT_LADDER[reached] if reached >= 0 else None})
    return rows


# ----------------------------------------------------------------------
# The thin reader
# ----------------------------------------------------------------------

def _warn(message):
    """One line on stderr. A closed or broken stderr must never break the hub."""
    try:
        print(message, file=sys.stderr)
    except (OSError, ValueError):
        pass


def _read_log(events):
    """The log facts, the valid events, and the line number of each. One `read`.

    A log that cannot be read is a finding, not an error: `log.ok` is false and `log.fault` holds the code
    of the log store. The class and the code go to stderr, so the fault is not silent.
    """
    try:
        report = events.read()
    except EventLogError as error:
        if error.code in UNREADABLE_CODES:
            _warn(f'hub: the event log cannot be read ({type(error).__name__} {error.code}). The hub shows log.ok false.')
            return LogFacts(True, False, 0, 0, False, error.code), [], []
        raise
    if not report['exists']:
        return LogFacts(False, True, 0, 0, False), [], []
    valid = [item['event'] for item in report['events']]
    lines = [item['line'] for item in report['events']]
    damaged = sum(1 for line in report['lines'] if line['ok'] is not True and line.get('reason') != 'truncated_tail')
    return LogFacts(True, report['ok'] is True, len(valid), damaged, report['truncatedTail'] is True), valid, lines


def _corrupt():
    return LessonStoreError('A saved lesson file has an unexpected shape. The file was preserved.', 'corrupt_file', 500)


def _module_info(document, module_id, version):
    if type(document) is not dict or type(document.get('title')) is not str:
        raise _corrupt()
    tasks, checks = document.get('tasks'), document.get('checks')
    if type(tasks) is not list or type(checks) is not list:
        raise _corrupt()
    if any(type(task) is not dict or type(task.get('id')) is not str or type(task.get('prompt')) is not str for task in tasks):
        raise _corrupt()
    if any(type(check) is not dict or type(check.get('id')) is not str or type(check.get('description')) is not str for check in checks):
        raise _corrupt()
    return ModuleInfo(module_id, version, document['title'],
                      tuple(TaskInfo(task['id'], task['prompt']) for task in tasks),
                      tuple(CheckInfo(check['id'], check['description']) for check in checks))


def _read_modules(lessons):
    """The pinned modules in the order the store lists them."""
    pins = lessons.pins()['pins']
    if not pins:
        return []
    found = []
    for module_id in lessons.module_ids():
        version = pins.get(module_id)
        if version is not None:
            found.append(_module_info(lessons.get_module(module_id, version), module_id, version))
    return found


def build_hub(events, lessons, now, tz_offset_minutes=0, time_zone=None):
    """Read the event log once and the lesson store, and return the hub response. Writes no file.

    `time_zone` is a tzinfo from `load_zone`, or None. A skipped event is named on stderr by its line number.
    """
    offset = check_offset(tz_offset_minutes)
    log, valid, lines = _read_log(events)
    skipped = [0]

    def note(seq, reason):
        skipped[0] += 1
        if skipped[0] <= SKIP_NOTES:
            _warn(f'hub: skipped the event on line {lines[seq]}: {reason}')

    result = compute_hub(valid, log, _read_modules(lessons), now, offset, time_zone, note)
    if skipped[0] > SKIP_NOTES:
        _warn(f'hub: skipped {skipped[0]} events in all. The first {SKIP_NOTES} are named above.')
    return result
