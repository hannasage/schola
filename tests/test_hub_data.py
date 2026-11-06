"""The hub rollups: pure functions over events and modules, and the thin reader of the two stores."""
import contextlib
import datetime
import inspect
import io
import json
import os
from pathlib import Path
import random
import re
import sys
import tempfile
import time
import unittest
from unittest import mock
import uuid
import zoneinfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import event_log
import hub_data
from event_log import EventLog, EventLogError
from hub_data import CheckInfo, LogFacts, ModuleInfo, TaskInfo
from lesson_store import LessonStore

UTC = datetime.timezone.utc
NOW = datetime.datetime(2026, 11, 5, 12, 0, 0, tzinfo=UTC)  # a Thursday
S1 = '11111111-1111-4111-8111-111111111111'
S2 = '22222222-2222-4222-8222-222222222222'
S3 = '33333333-3333-4333-8333-333333333333'
APP_TYPES = ('attempt_submitted', 'check_result', 'task_finished', 'module_completed')
LOG_OK = LogFacts(exists=True, ok=True, events=0, damaged_lines=0, truncated_tail=False)
NEW_YORK = zoneinfo.ZoneInfo('America/New_York')
FIXTURE = ROOT / 'tests' / 'fixtures' / 'contracts' / 'valid' / 'lesson-module-minimal.json'


def event(kind, time_text='2026-11-05T09:00:00Z', module='intro', task='t1', source=None, session=S1, skills=(), **data):
    """One contract-shaped event. Fields the type needs get defaults; `data` overrides them."""
    defaults = {
        'check_result': {'checkId': 'c1', 'attemptId': 'a1', 'status': 'passed', 'durationMs': 5, 'evidence': '', 'runnerType': None},
        'hint_requested': {'level': 'nudge', 'attemptId': None, 'origin': 'student'},
        'task_finished': {'result': 'passed'},
        'module_completed': {'checksPassed': 1, 'tasksTotal': 3},
        'task_opened': {},
        'mode_changed': {'from': 'coach', 'to': 'pair'},
        'attempt_submitted': {'attemptId': 'a1', 'artifactDigest': '0' * 64, 'artifactBytes': 4},
    }[kind]
    if kind == 'module_completed':
        task = None
    return {'schemaVersion': '1.0', 'eventId': str(uuid.uuid4()), 'time': time_text, 'sessionId': session, 'moduleId': module,
            'moduleVersion': '1.0.0', 'taskId': task, 'source': source or ('app' if kind in APP_TYPES else 'student'),
            'skills': list(skills), 'type': kind, 'data': dict(defaults, **data)}


def check(status='passed', time_text='2026-11-05T09:00:00Z', check_id='c1', **kw):
    return event('check_result', time_text, status=status, checkId=check_id, **kw)


def hint(level, task='t1', **kw):
    attempt = None if level in ('nudge', 'question') else 'a1'
    return event('hint_requested', task=task, level=level, attemptId=attempt, **kw)


def module(module_id='intro', version='1.0.0', title='Intro', tasks=('t1', 't2', 't3'), checks=('c1', 'c2')):
    return ModuleInfo(module_id, version, title, tuple(TaskInfo(t, f'Do {t}') for t in tasks),
                      tuple(CheckInfo(c, f'Check {c}') for c in checks))


def hub(events=(), modules=(), now=NOW, tz=0, log=LOG_OK, zone=None, skipped=None):
    return hub_data.compute_hub(list(events), log, list(modules), now, tz, zone, skipped)


def log_block(**changes):
    """The `log` object of a response, with the defaults of a clean log."""
    block = {'exists': True, 'ok': True, 'fault': None, 'events': 0, 'damagedLines': 0, 'truncatedTail': False,
             'skippedEvents': 0, 'futureEvents': 0, 'unmatchedEvents': 0}
    block.update(changes)
    return block


def iso(stamp, minutes=0):
    """An aware datetime as RFC 3339 text written with a fixed offset of `minutes` (Z when 0)."""
    local = stamp.astimezone(datetime.timezone(datetime.timedelta(minutes=minutes)))
    zone = 'Z' if minutes == 0 else f"{'+' if minutes > 0 else '-'}{abs(minutes) // 60:02d}:{abs(minutes) % 60:02d}"
    return local.strftime('%Y-%m-%dT%H:%M:%S') + zone


def local_day(text, tz):
    stamp = datetime.datetime.fromisoformat(text.replace('Z', '+00:00')).astimezone(UTC)
    return (stamp + datetime.timedelta(minutes=tz)).date()


def flat_days(result):
    """The day counts of the whole window in order, with None for days after today."""
    return [day for week in result['participation']['weeks'] for day in week['days']]


def day_count(result, date):
    """The session count of one date, read through the week starts."""
    for week in result['participation']['weeks']:
        start = datetime.date.fromisoformat(week['start'])
        offset = (date - start).days
        if 0 <= offset < 7:
            return week['days'][offset]
    return 'outside'


class ShapeTests(unittest.TestCase):
    def test_an_empty_hub_has_the_decided_closed_shape(self):
        result = hub()
        self.assertEqual(list(result), ['schemaVersion', 'generatedAt', 'log', 'focus', 'participation', 'checkpoints', 'skills', 'skillsTotal', 'checks', 'checksTotal', 'library'])
        self.assertEqual(result['schemaVersion'], '1.0')
        self.assertEqual(result['generatedAt'], '2026-11-05T12:00:00Z')
        self.assertEqual(result['log'], log_block(exists=True, ok=True, events=0, damagedLines=0, truncatedTail=False))
        self.assertIsNone(result['focus'])
        self.assertEqual(result['checkpoints'], {'done': 0, 'total': 0})
        self.assertEqual((result['skills'], result['checks'], result['library']), ([], [], []))
        self.assertEqual((result['skillsTotal'], result['checksTotal']), (0, 0))

    def test_the_response_is_plain_json(self):
        result = hub([event('task_opened')], [module()])
        self.assertEqual(json.loads(json.dumps(result, allow_nan=False)), result)

    def test_every_key_in_a_full_response_is_in_the_decided_set(self):
        events = [event('task_opened'), check('failed', skills=['loops']), check('passed', skills=['loops']), hint('question'),
                  event('task_finished')]
        result = hub(events, [module(), module('second')])
        self.assertEqual(list(result['log']), list(log_block()))
        self.assertEqual(set(result['focus']), {'moduleId', 'version', 'title', 'tasksTotal', 'tasksDone', 'tasksSkipped', 'nextTask'})
        self.assertEqual(set(result['focus']['nextTask']), {'taskId', 'text'})
        self.assertEqual(set(result['participation']), {'windowWeeks', 'weeks', 'totals'})
        self.assertEqual(set(result['participation']['weeks'][0]), {'start', 'days', 'sessions', 'activeDays'})
        self.assertEqual(set(result['participation']['totals']), {'sessions', 'activeDays', 'days'})
        self.assertEqual(set(result['skills'][0]), {'skillId', 'passed', 'tried', 'window'})
        self.assertEqual(set(result['checks'][0]), {'moduleId', 'checkId', 'title', 'retriesBeforeFirstPass', 'open', 'hintLevelReached'})
        self.assertEqual(set(result['library'][0]), {'moduleId', 'version', 'title', 'status', 'tasksTotal', 'tasksDone', 'tasksSkipped'})

    def test_no_key_names_a_streak_a_rank_a_total_time_or_a_feeling(self):
        banned = re.compile(r'streak|leader|rank|score|badge|minutes|duration|time|total_?time|guess|frustrat|emotion|mood', re.I)
        events = [check('failed', skills=['loops']), hint('question'), event('task_finished'), event('module_completed')]
        result = hub(events, [module(), module('second')])
        names = set()

        def walk(value):
            if isinstance(value, dict):
                for key, item in value.items():
                    names.add(key)
                    walk(item)
            elif isinstance(value, list):
                for item in value:
                    walk(item)
        walk(result)
        self.assertEqual([name for name in names if banned.search(name)], [])

    def test_the_pure_module_never_reads_a_clock(self):
        source = inspect.getsource(hub_data)
        for forbidden in ('.now(', '.utcnow(', 'time.time(', 'time.monotonic(', 'import time', 'today('):
            self.assertNotIn(forbidden, source)

    def test_a_naive_now_is_refused(self):
        with self.assertRaises(ValueError):
            hub(now=datetime.datetime(2026, 11, 5, 12, 0, 0))

    def test_now_in_another_zone_is_converted_to_utc(self):
        zone = datetime.timezone(datetime.timedelta(hours=-5))
        result = hub(now=datetime.datetime(2026, 11, 5, 23, 30, 0, tzinfo=zone))  # 04:30 UTC on the 6th
        self.assertEqual(result['generatedAt'], '2026-11-06T04:30:00Z')

    def test_generated_at_is_utc_even_when_the_zone_changes_the_year_and_month(self):
        zone = datetime.timezone(datetime.timedelta(hours=14))
        result = hub(now=datetime.datetime(2027, 1, 1, 1, 5, 9, tzinfo=zone))  # 11:05:09 UTC on New Year's Eve
        self.assertEqual(result['generatedAt'], '2026-12-31T11:05:09Z')
        result = hub(now=datetime.datetime(2026, 3, 1, 0, 30, 0, tzinfo=datetime.timezone(datetime.timedelta(hours=-9))))
        self.assertEqual(result['generatedAt'], '2026-03-01T09:30:00Z')

    def test_the_offset_must_be_a_whole_number_in_range(self):
        for bad in (841, -841, 1000, 0.0, 1.5, '0', None, True, False):
            with self.assertRaises(ValueError, msg=repr(bad)):
                hub(tz=bad)
        for good in (-840, 840, 0, 330):
            hub(tz=good)


class WindowTests(unittest.TestCase):
    def test_eight_weeks_starting_on_monday_and_ending_this_week(self):
        weeks = hub()['participation']['weeks']
        self.assertEqual(len(weeks), 8)
        self.assertEqual(weeks[0]['start'], '2026-09-14')
        self.assertEqual(weeks[-1]['start'], '2026-11-02')
        for week in weeks:
            self.assertEqual(datetime.date.fromisoformat(week['start']).weekday(), 0)
            self.assertEqual(len(week['days']), 7)
        self.assertEqual(hub()['participation']['windowWeeks'], 8)

    def test_days_after_today_are_null_and_days_up_to_today_are_numbers(self):
        days = hub()['participation']['weeks'][-1]['days']
        self.assertEqual(days, [0, 0, 0, 0, None, None, None])  # Monday to Thursday, then the rest of the week
        self.assertEqual(flat_days(hub()).count(None), 3)

    def test_totals_days_counts_days_up_to_and_including_today(self):
        self.assertEqual(hub()['participation']['totals'], {'sessions': 0, 'activeDays': 0, 'days': 53})
        monday = datetime.datetime(2026, 11, 2, 8, 0, 0, tzinfo=UTC)
        sunday = datetime.datetime(2026, 11, 8, 8, 0, 0, tzinfo=UTC)
        self.assertEqual(hub(now=monday)['participation']['totals']['days'], 50)
        self.assertEqual(hub(now=sunday)['participation']['totals']['days'], 56)
        self.assertNotIn(None, flat_days(hub(now=sunday)))

    def test_an_event_on_the_first_day_of_the_window_counts_and_the_day_before_does_not(self):
        inside = event('task_opened', '2026-09-14T00:00:00Z')
        outside = event('task_opened', '2026-09-13T23:59:59Z', session=S2)
        result = hub([inside, outside])
        self.assertEqual(result['participation']['weeks'][0]['days'][0], 1)
        self.assertEqual(result['participation']['totals'], {'sessions': 1, 'activeDays': 1, 'days': 53})

    def test_the_first_second_after_today_is_not_counted(self):
        result = hub([event('task_opened', '2026-11-05T23:59:59Z'), event('task_opened', '2026-11-06T00:00:00Z', session=S2)])
        self.assertEqual(day_count(result, datetime.date(2026, 11, 5)), 1)
        self.assertEqual(result['participation']['totals']['sessions'], 1)

    def test_the_window_crosses_a_year_end_and_a_leap_day(self):
        for now in (datetime.datetime(2027, 1, 5, 12, tzinfo=UTC), datetime.datetime(2028, 3, 1, 12, tzinfo=UTC),
                    datetime.datetime(2028, 2, 29, 23, 59, tzinfo=UTC), datetime.datetime(2027, 3, 1, 0, 0, tzinfo=UTC)):
            weeks = hub(now=now)['participation']['weeks']
            this_monday = now.date() - datetime.timedelta(days=now.weekday())
            self.assertEqual([w['start'] for w in weeks], [(this_monday - datetime.timedelta(days=7 * (7 - i))).isoformat() for i in range(8)])

    def test_a_century_leap_day_is_real_and_other_century_days_are_not(self):
        now = datetime.datetime(2000, 3, 2, 12, tzinfo=UTC)
        result = hub([event('task_opened', '2000-02-29T10:00:00Z')], now=now)
        self.assertTrue(result['log']['ok'])
        self.assertEqual(day_count(result, datetime.date(2000, 2, 29)), 1)

    def test_a_leap_day_event_lands_on_the_leap_day(self):
        now = datetime.datetime(2028, 3, 2, 12, tzinfo=UTC)
        result = hub([event('task_opened', '2028-02-29T10:00:00Z')], now=now)
        self.assertEqual(day_count(result, datetime.date(2028, 2, 29)), 1)
        self.assertEqual(day_count(result, datetime.date(2028, 3, 1)), 0)


class DayConversionTests(unittest.TestCase):
    def test_the_local_day_follows_the_offset(self):
        late = event('task_opened', '2026-11-04T23:30:00Z')
        self.assertEqual(day_count(hub([late], tz=0), datetime.date(2026, 11, 4)), 1)
        self.assertEqual(day_count(hub([late], tz=60), datetime.date(2026, 11, 4)), 0)
        self.assertEqual(day_count(hub([late], tz=60), datetime.date(2026, 11, 5)), 1)
        self.assertEqual(day_count(hub([late], tz=-60), datetime.date(2026, 11, 4)), 1)
        self.assertEqual(day_count(hub([late], tz=29), datetime.date(2026, 11, 4)), 1)
        self.assertEqual(day_count(hub([late], tz=30), datetime.date(2026, 11, 5)), 1)

    def test_an_offset_in_the_time_text_is_honoured(self):
        # 01:00 at +14:00 is 11:00 UTC the day before.
        result = hub([event('task_opened', '2026-11-05T01:00:00+14:00')])
        self.assertEqual(day_count(result, datetime.date(2026, 11, 4)), 1)
        result = hub([event('task_opened', '2026-11-04T20:00:00-05:00')])  # 01:00 UTC on the 5th
        self.assertEqual(day_count(result, datetime.date(2026, 11, 5)), 1)

    def test_a_fraction_of_a_second_is_read_and_ignored(self):
        result = hub([event('task_opened', '2026-11-04T23:59:59.999999999Z')])
        self.assertEqual(day_count(result, datetime.date(2026, 11, 4)), 1)

    def test_plus_840_around_midnight(self):
        # With +840 the student's day is 14 hours ahead. 12:00 UTC on the 5th is 02:00 on the 6th, a Friday.
        before = event('task_opened', '2026-11-05T09:59:59Z')
        after = event('task_opened', '2026-11-05T10:00:00Z', session=S2)
        result = hub([before, after], tz=840)
        self.assertEqual(day_count(result, datetime.date(2026, 11, 5)), 1)
        self.assertEqual(day_count(result, datetime.date(2026, 11, 6)), 1)
        self.assertEqual(result['participation']['weeks'][-1]['days'], [0, 0, 0, 1, 1, None, None])
        self.assertEqual(result['participation']['totals']['days'], 54)

    def test_minus_840_around_midnight(self):
        # With -840 the student's day is 14 hours behind. 12:00 UTC on the 5th is 22:00 on the 4th, a Wednesday.
        before = event('task_opened', '2026-11-05T13:59:59Z')
        after = event('task_opened', '2026-11-05T14:00:00Z', session=S2)
        result = hub([before, after], tz=-840)
        self.assertEqual(result['participation']['weeks'][-1]['days'], [0, 0, 1, None, None, None, None])
        self.assertEqual(result['participation']['totals'], {'sessions': 1, 'activeDays': 1, 'days': 52})

    def test_the_first_and_last_instants_of_the_year_range_do_not_fail(self):
        edge = [event('task_opened', '0000-01-01T00:00:00+23:59'), event('task_opened', '9999-12-31T23:59:59-23:59', session=S2)]
        result = hub(edge, tz=840)
        self.assertEqual(result['participation']['totals']['sessions'], 0)  # one is long ago, one is in the future
        self.assertTrue(result['log']['ok'])

    def test_a_differential_check_against_the_datetime_module(self):
        rng = random.Random(20261105)
        for _ in range(60):
            now = datetime.datetime(2026, 1, 1, tzinfo=UTC) + datetime.timedelta(minutes=rng.randrange(0, 60 * 24 * 900))
            tz = rng.choice([-840, -300, -1, 0, 1, 90, 330, 840, rng.randrange(-840, 841)])
            today = (now + datetime.timedelta(minutes=tz)).date()
            start = today - datetime.timedelta(days=today.weekday() + 49)
            events, expected = [], {}
            for index in range(40):
                stamp = now + datetime.timedelta(minutes=rng.randrange(-60 * 24 * 80, 60 * 24 * 3))
                text = iso(stamp, rng.choice([0, 0, 330, -480, 60, -30, 840, -840, 545]))
                session = [S1, S2, S3][rng.randrange(3)]
                source = rng.choice(['student', 'app', 'agent'])
                events.append(event('task_opened', text, source=source, session=session))
                day = local_day(text, tz)
                if source != 'agent' and start <= day <= today:
                    expected.setdefault(day, set()).add(session)
            result = hub(events, now=now, tz=tz)
            for offset in range(56):
                day = start + datetime.timedelta(days=offset)
                want = None if day > today else len(expected.get(day, ()))
                self.assertEqual(flat_days(result)[offset], want, (now, tz, day))
            weeks = result['participation']['weeks']
            self.assertEqual(weeks[-1]['start'], (today - datetime.timedelta(days=today.weekday())).isoformat())
            self.assertEqual(result['participation']['totals']['days'], (today - start).days + 1)


class CountedEventTests(unittest.TestCase):
    def test_student_and_app_events_count_and_agent_events_do_not(self):
        events = [event('task_opened', source='student'), check(source='app', session=S2), event('mode_changed', source='agent', session=S3)]
        self.assertEqual(hub(events)['participation']['totals'], {'sessions': 2, 'activeDays': 1, 'days': 53})

    def test_a_log_with_only_agent_events_shows_no_activity_anywhere(self):
        events = [event('task_opened', source='agent'), hint('full-answer', source='agent'), event('mode_changed', source='agent'),
                  event('task_opened', source='agent', session=S2)]
        result = hub(events, [module()])
        self.assertEqual(result['participation']['totals'], {'sessions': 0, 'activeDays': 0, 'days': 53})
        self.assertEqual((result['skills'], result['checks']), ([], []))
        self.assertEqual(result['focus']['moduleId'], 'intro')
        self.assertEqual(result['focus']['tasksDone'], 0)

    def test_an_agent_event_does_not_move_the_focus_or_a_check(self):
        events = [event('task_opened', module='intro'), event('task_opened', module='second', source='agent')]
        self.assertEqual(hub(events, [module(), module('second')])['focus']['moduleId'], 'intro')
        agent_check = check('failed', source='agent')
        self.assertEqual(hub([agent_check], [module()])['checks'], [])

    def test_an_agent_hint_does_not_raise_the_hint_level(self):
        events = [check('failed'), hint('full-answer', source='agent')]
        self.assertIsNone(hub(events, [module()])['checks'][0]['hintLevelReached'])

    def test_an_agent_check_result_does_not_feed_a_skill(self):
        self.assertEqual(hub([check('passed', skills=['loops'], source='agent')])['skills'], [])

    def test_a_future_event_counts_in_nothing(self):
        future = [check('passed', '2026-11-06T00:00:01Z', skills=['loops']), event('task_finished', '2026-11-07T10:00:00Z'),
                  event('module_completed', '2026-11-08T10:00:00Z'), hint('full-answer', task='t1')]
        future[3]['time'] = '2026-11-09T10:00:00Z'
        result = hub(future, [module()])
        self.assertEqual(result['participation']['totals']['sessions'], 0)
        self.assertEqual(result['skills'], [])
        self.assertEqual(result['checks'], [])
        self.assertEqual(result['checkpoints']['done'], 0)
        self.assertEqual(result['focus']['tasksDone'], 0)
        self.assertEqual(result['library'][0]['status'], 'focus')

    def test_a_future_event_does_not_make_a_module_the_focus(self):
        events = [event('task_opened', module='intro'), event('task_opened', '2027-01-01T00:00:00Z', module='second')]
        self.assertEqual(hub(events, [module(), module('second')])['focus']['moduleId'], 'intro')

    def test_an_event_from_before_the_window_still_counts_for_the_rest(self):
        events = [event('task_finished', '2025-01-01T10:00:00Z'), check('passed', '2025-01-01T09:00:00Z', skills=['loops'])]
        result = hub(events, [module()])
        self.assertEqual(result['participation']['totals']['sessions'], 0)
        self.assertEqual(result['focus']['tasksDone'], 1)
        self.assertEqual(result['skills'], [{'skillId': 'loops', 'passed': 1, 'tried': 1, 'window': 5}])

    def test_events_out_of_time_order_land_on_their_own_days(self):
        events = [event('task_opened', '2026-11-05T09:00:00Z', session=S1), event('task_opened', '2026-11-03T09:00:00Z', session=S2),
                  event('task_opened', '2026-11-04T09:00:00Z', session=S3), event('task_opened', '2026-11-03T08:00:00Z', session=S1)]
        days = hub(events)['participation']['weeks'][-1]['days']
        self.assertEqual(days, [0, 2, 1, 1, None, None, None])

    def test_an_event_on_an_unknown_module_still_counts_as_activity(self):
        result = hub([event('task_opened', module='gone')], [module()])
        self.assertEqual(result['participation']['totals']['sessions'], 1)
        self.assertEqual(result['focus']['moduleId'], 'intro')


class SessionTests(unittest.TestCase):
    def test_many_events_of_one_session_on_one_day_are_one_session(self):
        events = [event('task_opened', f'2026-11-05T0{h}:00:00Z') for h in range(1, 6)]
        self.assertEqual(hub(events)['participation']['totals'], {'sessions': 1, 'activeDays': 1, 'days': 53})

    def test_two_sessions_on_one_day_are_two_sessions_and_one_active_day(self):
        events = [event('task_opened', session=S1), event('task_opened', session=S2)]
        week = hub(events)['participation']['weeks'][-1]
        self.assertEqual((week['days'][3], week['sessions'], week['activeDays']), (2, 2, 1))

    def test_one_session_on_two_days_counts_once_on_each_day(self):
        events = [event('task_opened', '2026-11-03T23:00:00Z'), event('task_opened', '2026-11-04T01:00:00Z')]
        result = hub(events)
        week = result['participation']['weeks'][-1]
        self.assertEqual(week['days'], [0, 1, 1, 0, None, None, None])
        self.assertEqual((week['sessions'], week['activeDays']), (2, 2))
        self.assertEqual(result['participation']['totals'], {'sessions': 2, 'activeDays': 2, 'days': 53})

    def test_the_same_session_across_two_weeks_counts_in_both_weeks(self):
        events = [event('task_opened', '2026-11-01T23:00:00Z'), event('task_opened', '2026-11-02T01:00:00Z')]
        weeks = hub(events)['participation']['weeks']
        self.assertEqual((weeks[-2]['sessions'], weeks[-1]['sessions']), (1, 1))

    def test_a_session_that_crosses_local_midnight_depends_on_the_offset(self):
        events = [event('task_opened', '2026-11-03T23:30:00Z'), event('task_opened', '2026-11-04T00:30:00Z')]
        self.assertEqual(hub(events, tz=0)['participation']['totals']['sessions'], 2)
        self.assertEqual(hub(events, tz=-31)['participation']['totals']['sessions'], 1)
        self.assertEqual(hub(events, tz=-30)['participation']['totals']['sessions'], 2)

    def test_weekly_sessions_and_active_days_add_up_to_the_totals(self):
        events = [event('task_opened', f'2026-10-{day:02d}T10:00:00Z', session=S1 if day % 2 else S2) for day in range(1, 30)]
        result = hub(events)
        weeks = result['participation']['weeks']
        self.assertEqual(sum(w['sessions'] for w in weeks), result['participation']['totals']['sessions'])
        self.assertEqual(sum(w['activeDays'] for w in weeks), result['participation']['totals']['activeDays'])
        for week in weeks:
            self.assertEqual(week['sessions'], sum(d for d in week['days'] if d is not None))
            self.assertEqual(week['activeDays'], sum(1 for d in week['days'] if d))


class CheckpointTests(unittest.TestCase):
    def test_done_counts_distinct_pinned_modules_with_a_completion(self):
        events = [event('module_completed', module='intro'), event('module_completed', module='intro'), event('module_completed', module='second')]
        self.assertEqual(hub(events, [module(), module('second'), module('third')])['checkpoints'], {'done': 2, 'total': 3})

    def test_a_completion_of_a_module_that_is_not_pinned_does_not_count(self):
        result = hub([event('module_completed', module='gone')], [module()])
        self.assertEqual(result['checkpoints'], {'done': 0, 'total': 1})

    def test_a_completion_by_an_agent_does_not_count(self):
        result = hub([event('module_completed', source='agent')], [module()])
        self.assertEqual(result['checkpoints'], {'done': 0, 'total': 1})
        self.assertEqual(result['library'][0]['status'], 'focus')


class SkillTests(unittest.TestCase):
    def test_a_skill_reports_passed_and_tried(self):
        events = [check('failed', skills=['loops']), check('passed', skills=['loops']), check('passed', skills=['loops', 'lists'])]
        self.assertEqual(hub(events)['skills'], [{'skillId': 'lists', 'passed': 1, 'tried': 1, 'window': 5},
                                                 {'skillId': 'loops', 'passed': 2, 'tried': 3, 'window': 5}])

    def test_only_the_latest_five_results_count(self):
        events = [check('passed', skills=['loops']) for _ in range(4)] + [check('failed', skills=['loops']) for _ in range(5)]
        self.assertEqual(hub(events)['skills'], [{'skillId': 'loops', 'passed': 0, 'tried': 5, 'window': 5}])
        events = [check('failed', skills=['loops']) for _ in range(5)] + [check('passed', skills=['loops']) for _ in range(4)]
        self.assertEqual(hub(events)['skills'][0]['passed'], 4)
        self.assertEqual(hub(events)['skills'][0]['tried'], 5)

    def test_exactly_five_results_all_count_and_six_drops_the_oldest(self):
        five = [check('passed', skills=['loops']) for _ in range(5)]
        self.assertEqual(hub(five)['skills'][0]['passed'], 5)
        six = [check('failed', skills=['loops'])] + five
        self.assertEqual((hub(six)['skills'][0]['passed'], hub(six)['skills'][0]['tried']), (5, 5))

    def test_log_order_decides_what_is_latest_not_the_time_text(self):
        old_but_last = check('passed', '2026-10-01T10:00:00Z', skills=['loops'])
        events = [check('failed', '2026-11-04T10:00:00Z', skills=['loops']) for _ in range(5)] + [old_but_last]
        result = hub(events)['skills'][0]
        self.assertEqual((result['passed'], result['tried']), (1, 5))

    def test_failed_and_error_are_tried_and_not_passed(self):
        for status in ('failed', 'error'):
            self.assertEqual(hub([check(status, skills=['loops'])])['skills'], [{'skillId': 'loops', 'passed': 0, 'tried': 1, 'window': 5}], status)

    def test_a_self_reported_or_skipped_result_is_not_skill_evidence(self):
        for status in ('self-reported', 'skipped'):
            result = hub([check(status, skills=['loops'])])
            self.assertEqual((result['skills'], result['skillsTotal']), ([], 0), status)

    def test_a_self_reported_or_skipped_result_does_not_push_a_real_result_out_of_the_window(self):
        for status in ('skipped', 'self-reported'):
            events = [check('passed', skills=['loops'])] + [check(status, skills=['loops']) for _ in range(9)]
            self.assertEqual(hub(events)['skills'], [{'skillId': 'loops', 'passed': 1, 'tried': 1, 'window': 5}], status)

    def test_only_the_hundred_most_recently_tried_skills_are_kept_and_the_total_is_given(self):
        events = [check('passed', skills=[f's{n:03d}']) for n in range(120)]
        result = hub(events)
        self.assertEqual(result['skillsTotal'], 120)
        self.assertEqual([item['skillId'] for item in result['skills']], [f's{n:03d}' for n in range(20, 120)])

    def test_a_later_result_makes_an_old_skill_recent_again(self):
        events = [check('passed', skills=[f's{n:03d}']) for n in range(101)] + [check('failed', skills=['s000'])]
        result = hub(events)
        ids = [item['skillId'] for item in result['skills']]
        self.assertIn('s000', ids)
        self.assertNotIn('s001', ids)
        self.assertEqual((result['skillsTotal'], len(ids)), (101, 100))

    def test_a_skipped_result_does_not_make_a_skill_recent(self):
        events = [check('passed', skills=[f's{n:03d}']) for n in range(100)] + [check('skipped', skills=['s000']), check('passed', skills=['extra'])]
        ids = [item['skillId'] for item in hub(events)['skills']]
        self.assertNotIn('s000', ids)
        self.assertIn('extra', ids)

    def test_under_the_limit_the_total_is_the_length(self):
        result = hub([check('passed', skills=['a-skill']), check('failed', skills=['b-skill'])])
        self.assertEqual((result['skillsTotal'], len(result['skills'])), (2, 2))

    def test_skills_are_tracked_apart(self):
        events = [check('passed', skills=['a-skill']) for _ in range(6)] + [check('failed', skills=['b-skill'])]
        self.assertEqual(hub(events)['skills'], [{'skillId': 'a-skill', 'passed': 5, 'tried': 5, 'window': 5},
                                                 {'skillId': 'b-skill', 'passed': 0, 'tried': 1, 'window': 5}])

    def test_a_result_with_no_skills_makes_no_skill(self):
        self.assertEqual(hub([check('passed')])['skills'], [])

    def test_only_check_results_feed_skills(self):
        events = [event('task_opened', skills=['loops']), hint('nudge', skills=['loops']), event('task_finished', skills=['loops'])]
        self.assertEqual(hub(events)['skills'], [])

    def test_skills_are_sorted_by_id(self):
        events = [check('passed', skills=[name]) for name in ('zeta', 'alpha', 'mid')]
        self.assertEqual([s['skillId'] for s in hub(events)['skills']], ['alpha', 'mid', 'zeta'])

    def test_a_repeated_skill_in_one_event_counts_once(self):
        self.assertEqual(hub([check('passed', skills=['loops', 'loops'])])['skills'][0]['tried'], 1)


class CheckRowTests(unittest.TestCase):
    def test_retries_before_the_first_pass(self):
        events = [check('failed'), check('error'), check('passed'), check('failed'), check('failed')]
        row = hub(events, [module()])['checks'][0]
        self.assertEqual((row['moduleId'], row['checkId'], row['title'], row['retriesBeforeFirstPass'], row['open']),
                         ('intro', 'c1', 'Check c1', 2, False))

    def test_a_pass_on_the_first_try_has_no_retries(self):
        row = hub([check('passed')], [module()])['checks'][0]
        self.assertEqual((row['retriesBeforeFirstPass'], row['open']), (0, False))

    def test_a_check_never_passed_is_open_with_the_count_so_far(self):
        row = hub([check('failed'), check('error')], [module()])['checks'][0]
        self.assertEqual((row['retriesBeforeFirstPass'], row['open']), (2, True))

    def test_a_self_reported_result_counts_as_a_pass_for_the_check(self):
        row = hub([check('failed'), check('self-reported'), check('failed')], [module()])['checks'][0]
        self.assertEqual((row['retriesBeforeFirstPass'], row['open']), (1, False))

    def test_a_self_reported_result_on_the_first_try_is_a_pass_with_no_retries(self):
        row = hub([check('self-reported')], [module()])['checks'][0]
        self.assertEqual((row['retriesBeforeFirstPass'], row['open']), (0, False))

    def test_a_skipped_result_is_neither_a_retry_nor_a_pass(self):
        row = hub([check('failed'), check('skipped'), check('skipped'), check('passed')], [module()])['checks'][0]
        self.assertEqual((row['retriesBeforeFirstPass'], row['open']), (1, False))
        self.assertEqual(hub([check('skipped'), check('skipped')], [module()])['checks'], [])
        row = hub([check('failed'), check('skipped')], [module()])['checks'][0]
        self.assertEqual((row['retriesBeforeFirstPass'], row['open']), (1, True))

    def test_a_skipped_result_does_not_make_a_check_recent(self):
        events = [check('failed', check_id='c1'), check('failed', check_id='c2'), check('skipped', check_id='c1')]
        self.assertEqual([r['checkId'] for r in hub(events, [module()])['checks']], ['c2', 'c1'])

    def test_checks_are_tracked_apart_by_module_and_id(self):
        events = [check('failed', check_id='c1'), check('passed', check_id='c2'), check('failed', module='second', check_id='c1')]
        rows = {(r['moduleId'], r['checkId']): r for r in hub(events, [module(), module('second')])['checks']}
        self.assertEqual(rows[('intro', 'c1')]['open'], True)
        self.assertEqual(rows[('intro', 'c2')]['open'], False)
        self.assertEqual(rows[('second', 'c1')]['retriesBeforeFirstPass'], 1)

    def test_a_check_with_no_results_is_not_listed(self):
        self.assertEqual(hub([event('task_opened')], [module()])['checks'], [])

    def test_a_check_the_pinned_module_does_not_have_is_ignored(self):
        self.assertEqual(hub([check('failed', check_id='nope')], [module()])['checks'], [])

    def test_a_check_of_an_unpinned_module_is_ignored(self):
        self.assertEqual(hub([check('failed', module='gone')], [module()])['checks'], [])

    def test_hint_level_is_the_highest_level_asked_on_the_same_task(self):
        events = [check('failed', task='t1'), hint('nudge', task='t1'), hint('partial-example', task='t1'), hint('question', task='t1')]
        self.assertEqual(hub(events, [module()])['checks'][0]['hintLevelReached'], 'partial-example')
        for level in ('nudge', 'question', 'partial-example', 'full-answer'):
            events = [check('failed', task='t1'), hint(level, task='t1')]
            self.assertEqual(hub(events, [module()])['checks'][0]['hintLevelReached'], level)

    def test_a_higher_level_wins_whatever_the_order_asked(self):
        for asked, reached in ((['full-answer', 'partial-example'], 'full-answer'), (['question', 'full-answer', 'partial-example'], 'full-answer'),
                               (['partial-example', 'question', 'nudge'], 'partial-example'), (['question', 'nudge'], 'question')):
            events = [check('failed', task='t1')] + [hint(level, task='t1') for level in asked]
            self.assertEqual(hub(events, [module()])['checks'][0]['hintLevelReached'], reached, asked)

    def test_the_ladder_is_the_one_in_the_lesson_contract(self):
        schema = json.loads((ROOT / 'contracts' / 'lesson-common.v1.schema.json').read_text(encoding='utf-8'))
        self.assertEqual(list(hub_data.HINT_LADDER), schema['$defs']['hintLevel']['enum'])

    def test_a_check_first_seen_late_is_ordered_by_its_latest_result(self):
        events = [check('failed', check_id='c1'), check('failed', check_id='c2'), check('failed', check_id='c3'), check('failed', check_id='c2')]
        rows = hub(events, [module(checks=['c1', 'c2', 'c3'])])['checks']
        self.assertEqual([r['checkId'] for r in rows], ['c2', 'c3', 'c1'])

    def test_hint_level_is_null_with_no_hint(self):
        self.assertIsNone(hub([check('failed')], [module()])['checks'][0]['hintLevelReached'])

    def test_a_hint_on_another_task_or_module_does_not_count(self):
        events = [check('failed', task='t1'), hint('full-answer', task='t2'), hint('full-answer', task='t1', module='second')]
        self.assertIsNone(hub(events, [module(), module('second')])['checks'][0]['hintLevelReached'])

    def test_a_hint_before_the_check_result_counts_when_it_comes_before_the_pass(self):
        events = [hint('question', task='t1'), check('failed', task='t1')]
        self.assertEqual(hub(events, [module()])['checks'][0]['hintLevelReached'], 'question')
        events = [hint('question', task='t1'), check('passed', task='t1')]
        self.assertEqual(hub(events, [module()])['checks'][0]['hintLevelReached'], 'question')

    def test_a_hint_after_the_first_pass_does_not_count(self):
        events = [hint('nudge', task='t1'), check('passed', task='t1'), hint('full-answer', task='t1')]
        row = hub(events, [module()])['checks'][0]
        self.assertEqual(row['hintLevelReached'], 'nudge')
        events = [check('passed', task='t1'), hint('full-answer', task='t1')]
        self.assertIsNone(hub(events, [module()])['checks'][0]['hintLevelReached'])

    def test_a_check_not_passed_yet_counts_every_hint_so_far(self):
        events = [check('failed', task='t1'), hint('partial-example', task='t1'), check('failed', task='t1'), hint('full-answer', task='t1')]
        self.assertEqual(hub(events, [module()])['checks'][0]['hintLevelReached'], 'full-answer')

    def test_two_checks_on_one_task_share_the_hints_before_their_own_pass(self):
        events = [hint('nudge', task='t1'), check('passed', task='t1', check_id='c1'), hint('full-answer', task='t1'),
                  check('passed', task='t1', check_id='c2')]
        rows = {r['checkId']: r for r in hub(events, [module()])['checks']}
        self.assertEqual((rows['c1']['hintLevelReached'], rows['c2']['hintLevelReached']), ('nudge', 'full-answer'))
        events = [hint('nudge', task='t1'), check('passed', task='t1', check_id='c1'), hint('full-answer', task='t1'),
                  check('failed', task='t1', check_id='c2')]
        rows = {r['checkId']: r for r in hub(events, [module()])['checks']}
        self.assertEqual((rows['c1']['hintLevelReached'], rows['c2']['hintLevelReached']), ('nudge', 'full-answer'))

    def test_a_self_reported_pass_also_ends_the_hint_window(self):
        events = [hint('question', task='t1'), check('self-reported', task='t1'), hint('full-answer', task='t1')]
        self.assertEqual(hub(events, [module()])['checks'][0]['hintLevelReached'], 'question')

    def test_a_pass_on_one_task_counts_the_hints_on_the_other_task_the_check_was_run_for(self):
        events = [check('failed', task='t1'), hint('full-answer', task='t1'), check('passed', task='t2')]
        self.assertEqual(hub(events, [module()])['checks'][0]['hintLevelReached'], 'full-answer')

    def test_the_hint_window_takes_the_tasks_seen_up_to_the_pass(self):
        events = [check('failed', task='t1'), hint('full-answer', task='t2'), check('passed', task='t2')]
        self.assertEqual(hub(events, [module()])['checks'][0]['hintLevelReached'], 'full-answer')
        events = [check('failed', task='t1'), check('passed', task='t1'), hint('full-answer', task='t2'), check('failed', task='t2')]
        self.assertIsNone(hub(events, [module()])['checks'][0]['hintLevelReached'])

    def test_a_check_run_for_two_tasks_takes_the_highest_hint_of_either(self):
        events = [check('failed', task='t1'), check('failed', task='t2'), hint('question', task='t1'), hint('full-answer', task='t2')]
        self.assertEqual(hub(events, [module()])['checks'][0]['hintLevelReached'], 'full-answer')
        events = [check('failed', task='t1'), check('failed', task='t2'), hint('question', task='t1'), hint('partial-example', task='t2')]
        self.assertEqual(hub(events, [module()])['checks'][0]['hintLevelReached'], 'partial-example')

    def test_only_the_twenty_most_recently_active_checks_are_kept_newest_first(self):
        ids = [f'c{n:02d}' for n in range(30)]
        events = [check('failed', check_id=name) for name in ids]
        rows = hub(events, [module(checks=ids)])['checks']
        self.assertEqual(len(rows), 20)
        self.assertEqual([r['checkId'] for r in rows], ids[::-1][:20])
        self.assertEqual(hub(events, [module(checks=ids)])['checksTotal'], 30)

    def test_over_the_limit_open_checks_are_kept_first_then_the_most_recent(self):
        ids = [f'c{n:02d}' for n in range(25)]
        events = [check('failed', check_id=name) for name in ids[:5]] + [check('passed', check_id=name) for name in ids[5:]]
        result = hub(events, [module(checks=ids)])
        shown = [r['checkId'] for r in result['checks']]
        self.assertEqual(result['checksTotal'], 25)
        self.assertEqual(sorted(shown), ids[:5] + ids[10:])  # the 5 open checks and the 15 newest passed ones
        self.assertEqual(shown, ids[10:][::-1] + ids[:5][::-1])  # newest first

    def test_many_open_checks_are_cut_to_the_most_recent_open_ones(self):
        ids = [f'c{n:02d}' for n in range(25)]
        events = [check('failed', check_id=name) for name in ids] + [check('passed', check_id='c24')]
        result = hub(events, [module(checks=ids)])
        shown = [r['checkId'] for r in result['checks']]
        self.assertEqual(len(shown), 20)
        self.assertNotIn('c24', shown)  # it passed, so it ranks after every open check
        self.assertEqual(sorted(shown), ids[4:24])

    def test_under_the_limit_the_total_is_the_length(self):
        result = hub([check('failed', check_id='c1'), check('passed', check_id='c2')], [module()])
        self.assertEqual((result['checksTotal'], len(result['checks'])), (2, 2))
        self.assertEqual(hub([], [module()])['checksTotal'], 0)

    def test_a_later_result_makes_an_old_check_recent_again(self):
        ids = [f'c{n:02d}' for n in range(21)]
        events = [check('failed', check_id=name) for name in ids] + [check('failed', check_id='c00')]
        rows = hub(events, [module(checks=ids)])['checks']
        self.assertEqual(rows[0]['checkId'], 'c00')
        self.assertNotIn('c01', [r['checkId'] for r in rows])

    def test_a_result_after_the_first_pass_makes_the_check_recent_but_adds_no_retry(self):
        events = [check('passed', check_id='c1'), check('failed', check_id='c2'), check('failed', check_id='c1')]
        rows = hub(events, [module()])['checks']
        self.assertEqual([r['checkId'] for r in rows], ['c1', 'c2'])
        self.assertEqual(rows[0]['retriesBeforeFirstPass'], 0)

    def test_the_check_title_comes_from_the_module(self):
        info = ModuleInfo('intro', '1.0.0', 'Intro', (TaskInfo('t1', 'x'),), (CheckInfo('c1', 'Make the chart'),))
        self.assertEqual(hub([check('failed')], [info])['checks'][0]['title'], 'Make the chart')


class FocusTests(unittest.TestCase):
    def test_with_no_pinned_module_focus_is_null_and_the_library_is_empty(self):
        result = hub([event('task_opened')])
        self.assertEqual((result['focus'], result['library'], result['checkpoints']['total']), (None, [], 0))

    def test_with_no_events_the_first_pinned_module_is_the_focus(self):
        result = hub([], [module('alpha', title='Alpha'), module('beta')])
        self.assertEqual(result['focus'], {'moduleId': 'alpha', 'version': '1.0.0', 'title': 'Alpha', 'tasksTotal': 3, 'tasksDone': 0, 'tasksSkipped': 0,
                                           'nextTask': {'taskId': 't1', 'text': 'Do t1'}})

    def test_the_module_with_the_latest_event_is_the_focus(self):
        events = [event('task_opened', module='beta'), event('task_opened', module='alpha')]
        self.assertEqual(hub(events, [module('alpha'), module('beta')])['focus']['moduleId'], 'alpha')
        events.reverse()
        self.assertEqual(hub(events, [module('alpha'), module('beta')])['focus']['moduleId'], 'beta')

    def test_latest_means_last_in_the_log_not_the_biggest_time(self):
        events = [event('task_opened', '2026-11-04T10:00:00Z', module='beta'), event('task_opened', '2026-11-01T10:00:00Z', module='alpha')]
        self.assertEqual(hub(events, [module('alpha'), module('beta')])['focus']['moduleId'], 'alpha')

    def test_a_completed_module_is_never_the_focus(self):
        events = [event('task_opened', module='beta'), event('task_opened', module='alpha'), event('module_completed', module='alpha')]
        result = hub(events, [module('alpha'), module('beta')])
        self.assertEqual(result['focus']['moduleId'], 'beta')
        self.assertEqual([m['status'] for m in result['library']], ['done', 'focus'])

    def test_when_the_module_with_events_is_complete_the_next_untouched_module_is_the_focus(self):
        events = [event('module_completed', module='alpha')]
        result = hub(events, [module('alpha'), module('beta'), module('gamma')])
        self.assertEqual(result['focus']['moduleId'], 'beta')

    def test_when_every_module_is_complete_there_is_no_focus(self):
        events = [event('module_completed', module='alpha'), event('module_completed', module='beta')]
        result = hub(events, [module('alpha'), module('beta')])
        self.assertIsNone(result['focus'])
        self.assertEqual([m['status'] for m in result['library']], ['done', 'done'])
        self.assertEqual(result['checkpoints'], {'done': 2, 'total': 2})

    def test_events_on_unpinned_modules_do_not_pick_the_focus(self):
        events = [event('task_opened', module='gone')]
        self.assertEqual(hub(events, [module('alpha'), module('beta')])['focus']['moduleId'], 'alpha')

    def test_tasks_done_counts_distinct_finished_tasks_of_the_module(self):
        events = [event('task_finished', task='t1'), event('task_finished', task='t1'), event('task_finished', task='t3'),
                  event('task_finished', task='t2', module='other'), event('task_finished', task='ghost')]
        focus = hub(events, [module(), module('other')])['focus']
        self.assertEqual((focus['tasksDone'], focus['tasksTotal']), (2, 3))

    def test_only_a_passed_task_counts_as_done_and_the_others_count_as_skipped(self):
        events = [event('task_finished', task='t1', result='abandoned'), event('task_finished', task='t2', result='skipped'),
                  event('task_finished', task='t3', result='passed')]
        focus = hub(events, [module()])['focus']
        self.assertEqual((focus['tasksDone'], focus['tasksSkipped'], focus['tasksTotal']), (1, 2, 3))
        self.assertEqual(hub(events, [module()])['library'][0]['tasksSkipped'], 2)

    def test_next_task_moves_past_any_finished_task(self):
        events = [event('task_finished', task='t1', result='abandoned'), event('task_finished', task='t2', result='skipped')]
        self.assertEqual(hub(events, [module()])['focus']['nextTask']['taskId'], 't3')
        events = [event('task_finished', task=name, result='skipped') for name in ('t1', 't2', 't3')]
        focus = hub(events, [module()])['focus']
        self.assertEqual((focus['nextTask'], focus['tasksDone'], focus['tasksSkipped']), (None, 0, 3))

    def test_the_latest_result_of_a_task_wins(self):
        events = [event('task_finished', task='t1', result='abandoned'), event('task_finished', task='t1', result='passed')]
        focus = hub(events, [module()])['focus']
        self.assertEqual((focus['tasksDone'], focus['tasksSkipped']), (1, 0))
        events.reverse()
        focus = hub(events, [module()])['focus']
        self.assertEqual((focus['tasksDone'], focus['tasksSkipped']), (0, 1))

    def test_a_finish_for_a_task_the_module_does_not_have_counts_nowhere(self):
        events = [event('task_finished', task='ghost', result='abandoned'), event('task_finished', task='ghost-2')]
        focus = hub(events, [module()])['focus']
        self.assertEqual((focus['tasksDone'], focus['tasksSkipped']), (0, 0))

    def test_the_next_task_is_the_first_in_module_order_without_a_finish(self):
        events = [event('task_finished', task='t1'), event('task_finished', task='t3')]
        self.assertEqual(hub(events, [module()])['focus']['nextTask'], {'taskId': 't2', 'text': 'Do t2'})

    def test_next_task_follows_module_order_not_event_order(self):
        events = [event('task_finished', task='t2'), event('task_finished', task='t1')]
        self.assertEqual(hub(events, [module()])['focus']['nextTask']['taskId'], 't3')

    def test_next_task_is_null_when_every_task_is_finished(self):
        events = [event('task_finished', task=name) for name in ('t1', 't2', 't3')]
        focus = hub(events, [module()])['focus']
        self.assertEqual((focus['nextTask'], focus['tasksDone']), (None, 3))

    def test_a_module_with_no_tasks(self):
        result = hub([event('task_opened', task='t1')], [module(tasks=())])
        self.assertEqual((result['focus']['tasksTotal'], result['focus']['tasksDone'], result['focus']['nextTask']), (0, 0, None))
        self.assertEqual((result['library'][0]['tasksTotal'], result['library'][0]['tasksDone'], result['library'][0]['tasksSkipped']), (0, 0, 0))

    def test_a_task_finished_by_an_agent_does_not_count(self):
        self.assertEqual(hub([event('task_finished', task='t1', source='agent')], [module()])['focus']['tasksDone'], 0)


class LibraryTests(unittest.TestCase):
    def test_the_library_keeps_the_order_it_is_given_with_a_status_each(self):
        events = [event('module_completed', module='b'), event('task_finished', task='t1', module='c'), event('task_opened', module='c')]
        result = hub(events, [module('a'), module('b'), module('c'), module('d')])
        self.assertEqual([(m['moduleId'], m['status']) for m in result['library']], [('a', 'next'), ('b', 'done'), ('c', 'focus'), ('d', 'next')])
        self.assertEqual([m['tasksDone'] for m in result['library']], [0, 0, 1, 0])
        self.assertEqual(result['library'][0], {'moduleId': 'a', 'version': '1.0.0', 'title': 'Intro', 'status': 'next', 'tasksTotal': 3, 'tasksDone': 0, 'tasksSkipped': 0})

    def test_fifty_modules(self):
        modules = [module(f'm{n:02d}') for n in range(50)]
        events = [event('module_completed', module=f'm{n:02d}') for n in range(0, 50, 2)] + [event('task_opened', module='m07')]
        result = hub(events, modules)
        self.assertEqual(len(result['library']), 50)
        self.assertEqual(result['checkpoints'], {'done': 25, 'total': 50})
        self.assertEqual(result['focus']['moduleId'], 'm07')
        statuses = [m['status'] for m in result['library']]
        self.assertEqual((statuses.count('done'), statuses.count('focus'), statuses.count('next')), (25, 1, 24))

    def test_duplicate_module_ids_in_the_input_are_refused(self):
        with self.assertRaises(ValueError):
            hub([], [module('a'), module('a')])


class TextTests(unittest.TestCase):
    def test_the_caps_are_the_maxima_of_the_lesson_contract(self):
        common = json.loads((ROOT / 'contracts' / 'lesson-common.v1.schema.json').read_text(encoding='utf-8'))['$defs']
        defs = json.loads((ROOT / 'contracts' / 'lesson-module.v1.schema.json').read_text(encoding='utf-8'))['$defs']
        self.assertEqual(hub_data.TITLE_CAP, common['title']['maxLength'])
        self.assertEqual(hub_data.TASK_TEXT_CAP, defs['task']['properties']['prompt']['maxLength'])
        self.assertEqual(hub_data.CHECK_TEXT_CAP, defs['check']['properties']['description']['maxLength'])

    def test_text_at_the_contract_maximum_is_never_cut(self):
        info = ModuleInfo('intro', '1.0.0', 'T' * 200, (TaskInfo('t1', 'P' * 5000),), (CheckInfo('c1', 'D' * 2000),))
        result = hub([check('failed', task='t1')], [info])
        self.assertEqual(result['focus']['title'], 'T' * 200)
        self.assertEqual(result['library'][0]['title'], 'T' * 200)
        self.assertEqual(result['focus']['nextTask']['text'], 'P' * 5000)
        self.assertEqual(result['checks'][0]['title'], 'D' * 2000)

    def test_text_over_the_contract_maximum_is_cut_to_it(self):
        info = ModuleInfo('intro', '1.0.0', 'T' * 5000, (TaskInfo('t1', 'P' * 9000),), (CheckInfo('c1', 'D' * 3000),))
        result = hub([check('failed', task='t1')], [info])
        self.assertEqual(len(result['focus']['title']), 200)
        self.assertEqual(len(result['library'][0]['title']), 200)
        self.assertEqual(len(result['focus']['nextTask']['text']), 5000)
        self.assertEqual(len(result['checks'][0]['title']), 2000)

    def test_control_and_bidirectional_characters_pass_through_as_data(self):
        nasty = 'a\x00b\x1b[31m‮⁦<script>alert(1)</script>\ud800'
        info = ModuleInfo('intro', '1.0.0', nasty, (TaskInfo('t1', nasty),), (CheckInfo('c1', nasty),))
        result = hub([check('failed', task='t1')], [info])
        self.assertEqual(result['focus']['title'], nasty)
        self.assertEqual(result['focus']['nextTask']['text'], nasty)
        self.assertEqual(result['checks'][0]['title'], nasty)
        self.assertEqual(json.loads(json.dumps(result))['focus']['title'], nasty)  # the JSON text escapes it all

    def test_the_response_holds_no_event_content(self):
        secret = event('check_result', evidence='SECRET-EVIDENCE')
        text = json.dumps(hub([secret, event('task_opened')], [module()]))
        for forbidden in ('SECRET-EVIDENCE', secret['eventId'], S1, 'attemptId', 'artifact', 'evidence', 'sessionId', 'eventId'):
            self.assertNotIn(forbidden, text)


class DamageTests(unittest.TestCase):
    def test_log_facts_are_reported_as_given(self):
        log = LogFacts(exists=True, ok=False, events=7, damaged_lines=2, truncated_tail=True)
        self.assertEqual(hub(log=log)['log'], log_block(exists=True, ok=False, events=7, damagedLines=2, truncatedTail=True))

    def test_a_missing_log_gives_zeros(self):
        log = LogFacts(exists=False, ok=True, events=0, damaged_lines=0, truncated_tail=False)
        result = hub([], [module()], log=log)
        self.assertEqual(result['log'], log_block(exists=False, ok=True, events=0, damagedLines=0, truncatedTail=False))
        self.assertEqual(result['participation']['totals']['sessions'], 0)

    def test_a_damaged_log_still_gives_the_lesson_side_and_the_valid_events(self):
        log = LogFacts(exists=True, ok=False, events=2, damaged_lines=1, truncated_tail=False)
        result = hub([event('task_finished', task='t1')], [module()], log=log)
        self.assertFalse(result['log']['ok'])
        self.assertEqual(result['focus']['tasksDone'], 1)
        self.assertEqual(result['checkpoints']['total'], 1)
        self.assertEqual(len(result['library']), 1)

    def test_a_malformed_event_is_skipped_alone_and_counted(self):
        good = [event('task_finished', task='t1'), check('failed', skills=['loops']), event('module_completed', module='second')]
        plain = hub(good, [module(), module('second')])
        for broken in self.malformed_events():
            seen = []
            result = hub(good + [broken], [module(), module('second')], skipped=lambda seq, reason: seen.append((seq, reason)))
            self.assertEqual(result['log']['skippedEvents'], 1, broken)
            self.assertTrue(result['log']['ok'], broken)
            self.assertEqual([seq for seq, _ in seen], [3], broken)
            self.assertTrue(seen[0][1] and isinstance(seen[0][1], str))
            expected = dict(plain, log=dict(plain['log'], skippedEvents=1))
            self.assertEqual(result, expected, broken)

    def test_a_skipped_event_is_in_no_rollup(self):
        broken = event('task_finished', task='t2')
        broken['sessionId'] = None
        result = hub([event('task_finished', task='t1'), broken], [module()])
        self.assertEqual(result['focus']['tasksDone'], 1)
        self.assertEqual(result['log']['skippedEvents'], 1)
        self.assertEqual(result['participation']['totals']['sessions'], 1)

    def test_a_malformed_event_does_not_change_the_counts_the_log_reported(self):
        log = LogFacts(exists=True, ok=True, events=9, damaged_lines=0, truncated_tail=False)
        result = hub([None], log=log)
        self.assertEqual(result['log'], log_block(exists=True, ok=True, events=9, damagedLines=0, truncatedTail=False, skippedEvents=1))

    def test_the_skip_reasons_name_the_field_and_never_hold_event_content(self):
        seen = []
        secret = event('task_opened')
        secret['moduleId'] = ['SECRET-VALUE']
        hub([secret], skipped=lambda seq, reason: seen.append(reason))
        self.assertEqual(len(seen), 1)
        self.assertNotIn('SECRET', seen[0])
        self.assertIn('moduleId', seen[0])

    def test_an_error_in_the_skip_sink_is_not_swallowed(self):
        def explode(seq, reason):
            raise RuntimeError('sink')
        with self.assertRaises(RuntimeError):
            hub([None], skipped=explode)

    def test_future_events_are_counted_apart_from_the_rest(self):
        events = [event('task_opened', '2026-11-06T00:00:00Z'), event('task_opened', '2027-01-01T00:00:00Z'), event('task_opened'),
                  event('task_opened', '2030-01-01T00:00:00Z', source='agent')]
        result = hub(events)
        self.assertEqual(result['log']['futureEvents'], 2)  # an agent event is never counted, so it is not a future event either
        self.assertEqual(result['participation']['totals']['sessions'], 1)

    def test_a_future_event_is_judged_in_the_local_day(self):
        late = event('task_opened', '2026-11-05T23:30:00Z')
        self.assertEqual(hub([late], tz=0)['log']['futureEvents'], 0)
        self.assertEqual(hub([late], tz=840)['log']['futureEvents'], 0)  # today is the 6th for +840
        self.assertEqual(hub([late], tz=-840)['log']['futureEvents'], 1)  # today is the 4th for -840

    def test_check_results_the_pinned_module_does_not_have_are_counted_as_unmatched(self):
        events = [check('failed', check_id='nope'), check('passed', check_id='c1'), check('skipped', check_id='nope'),
                  check('failed', check_id='nope', module='gone'), check('failed', check_id='nope', source='agent'),
                  check('failed', '2027-01-01T00:00:00Z', check_id='nope')]
        result = hub(events, [module()])
        self.assertEqual(result['log']['unmatchedEvents'], 2)
        self.assertEqual([r['checkId'] for r in result['checks']], ['c1'])

    def test_the_fault_of_the_log_is_passed_through(self):
        log = LogFacts(True, False, 0, 0, False, 'log_corrupt')
        self.assertEqual(hub(log=log)['log'], log_block(ok=False, fault='log_corrupt'))
        self.assertIsNone(hub()['log']['fault'])

    @staticmethod
    def malformed_events():
        def with_field(base, **changes):
            document = base() if base is not event else event('task_opened')
            document.update(changes)
            return document
        out = [None, 'text', 5, [], {}]
        out.append(with_field(event, time='not a time'))
        out.append(with_field(event, time='2026-02-30T10:00:00Z'))
        out.append(with_field(event, time='2026-11-05T10:00:60Z'))
        out.append(with_field(event, time='2100-02-29T10:00:00Z'))
        out.append(with_field(event, time='1900-02-29T10:00:00Z'))
        for short_month in ('02-30', '04-31', '06-31', '09-31', '11-31'):
            out.append(with_field(event, time=f'2026-{short_month}T10:00:00Z'))
        out.append(with_field(event, time='2026-13-01T10:00:00Z'))
        out.append(with_field(event, time='2026-00-10T10:00:00Z'))
        out.append(with_field(event, time='2026-11-00T10:00:00Z'))
        out.append(with_field(event, time='2026-11-05T24:00:00Z'))
        out.append(with_field(event, time='2026-11-05T10:60:00Z'))
        out.append(with_field(event, time='2026-11-05T10:00:00+24:00'))
        out.append(with_field(event, time='2026-11-05T10:00:00+10:60'))
        out.append(with_field(event, time='2026-11-05T10:00:00Z\n'))
        out.append(with_field(event, time='2026-11-05 10:00:00Z'))
        out.append(with_field(event, time='2026-11-05T10:00:00'))
        out.append(with_field(event, time=20261105))
        out.append(with_field(event, time=None))
        out.append(with_field(event, sessionId=None))
        out.append(with_field(event, sessionId=5))
        out.append(with_field(event, sessionId=''))
        out.append(with_field(event, moduleId=None))
        out.append(with_field(event, moduleId=['x']))
        out.append(with_field(event, taskId=5))
        out.append(with_field(event, source='robot'))
        out.append(with_field(event, source=None))
        out.append(with_field(event, type=None))
        out.append(with_field(event, type=['task_opened']))
        out.append(with_field(event, skills='loops'))
        out.append(with_field(event, skills=[5]))
        out.append(with_field(event, skills=None))
        out.append(with_field(event, data=None))
        out.append(with_field(event, data=[]))
        bad_check = check()
        bad_check['data'] = dict(bad_check['data'], status='maybe')
        out.append(bad_check)
        bad_check = check()
        bad_check['data'] = dict(bad_check['data'], checkId=7)
        out.append(bad_check)
        bad_check = check()
        del bad_check['data']['status']
        out.append(bad_check)
        bad_check = check()
        bad_check['taskId'] = None
        out.append(bad_check)
        bad_check = check()
        bad_check['data'] = dict(bad_check['data'], checkId='')
        out.append(bad_check)
        bad_hint = hint('nudge')
        bad_hint['data'] = dict(bad_hint['data'], level='shout')
        out.append(bad_hint)
        bad_hint = hint('nudge')
        bad_hint['taskId'] = None
        out.append(bad_hint)
        bad_finish = event('task_finished')
        bad_finish['taskId'] = None
        out.append(bad_finish)
        out.append(with_field(lambda: event('task_finished'), taskId=3))
        bad_finish = event('task_finished')
        bad_finish['data'] = {'result': 'done'}
        out.append(bad_finish)
        bad_finish = event('task_finished')
        bad_finish['data'] = {}
        out.append(bad_finish)
        return out

    def test_an_agent_event_is_only_checked_for_the_fields_the_hub_reads(self):
        agent = event('task_opened', source='agent')
        agent['data'] = 'ignored'
        self.assertEqual(hub([agent])['log']['skippedEvents'], 0)

    def test_bool_is_not_a_number_and_a_subclass_of_str_is_not_a_string(self):
        class Name(str):
            pass
        weird = event('task_opened')
        weird['moduleId'] = Name('intro')
        self.assertEqual(hub([weird])['log']['skippedEvents'], 1)


class LinearTimeTests(unittest.TestCase):
    def test_a_hundred_thousand_events_in_memory_roll_up_in_seconds(self):
        base = event('check_result', skills=['loops'])
        events = []
        for index in range(100_000):
            copy = dict(base, eventId=str(index), time=f'2026-10-{1 + index % 28:02d}T10:00:00Z', sessionId=S1 if (index // 28) % 2 else S2)
            events.append(copy)
        started = time.perf_counter()
        result = hub(events, [module()])
        elapsed = time.perf_counter() - started
        self.assertLess(elapsed, 10)
        self.assertEqual(result['skills'][0]['tried'], 5)
        self.assertEqual(result['participation']['totals']['sessions'], 56)  # 28 days times 2 sessions

    @staticmethod
    def lines_run(count):
        """The number of lines of hub_data.py that run for a log of `count` events. It does not depend on the machine."""
        names = [f'c{i}' for i in range(20)]
        info = module(checks=names)
        events = [event('check_result', f'2026-10-{1 + i % 28:02d}T10:00:00Z', check_id=names[i % 20], skills=[f's{i % 50}'], session=str(uuid.UUID(int=i % 300)))
                  for i in range(count)]
        counter = [0]
        source = hub_data.__file__

        def tracer(frame, kind, argument):
            if frame.f_code.co_filename == source:
                if kind == 'line':
                    counter[0] += 1
                return tracer
            return None
        sys.settrace(tracer)
        try:
            hub(events, [info])
        finally:
            sys.settrace(None)
        return counter[0]

    def test_work_grows_in_step_with_the_log(self):
        small, large = self.lines_run(1000), self.lines_run(8000)
        # 8 times the events. Linear work gives about 8 times the lines (a fixed part of the work does not grow). Quadratic work gives about 64.
        self.assertGreater(large, small * 5)
        self.assertLess(large, small * 9)

    def test_cpu_time_grows_in_step_with_the_log_with_a_wide_floor(self):
        names = [f'c{i}' for i in range(20)]
        info = module(checks=names)

        def run(count):
            events = [event('check_result', f'2026-10-{1 + i % 28:02d}T10:00:00Z', check_id=names[i % 20]) for i in range(count)]
            started = time.process_time()  # CPU time of this process, so other programs on the machine add little noise
            hub(events, [info])
            return time.process_time() - started
        run(2000)  # warm up
        small, large = min(run(10000) for _ in range(3)), min(run(80000) for _ in range(3))
        self.assertLess(large, max(small, 0.05) * 40)  # 8 times the events, quadratic work would take about 64 times


class ZoneTests(unittest.TestCase):
    """A named time zone converts each event with the offset it had at that moment."""

    def test_a_fall_back_day_follows_the_zone_and_not_one_offset(self):
        # New York leaves daylight time on 2026-11-01 at 06:00 UTC.
        edt = event('task_opened', '2026-11-01T04:30:00Z')  # 00:30 on the 1st, offset -4
        est = event('task_opened', '2026-11-02T04:30:00Z', session=S2)  # 23:30 on the 1st, offset -5
        result = hub([edt, est], zone=NEW_YORK)
        self.assertEqual(day_count(result, datetime.date(2026, 10, 31)), 0)
        self.assertEqual(day_count(result, datetime.date(2026, 11, 1)), 2)
        self.assertEqual(day_count(result, datetime.date(2026, 11, 2)), 0)
        fixed = hub([edt, est], tz=-300)  # the winter offset puts the first event on the 31st
        self.assertEqual((day_count(fixed, datetime.date(2026, 10, 31)), day_count(fixed, datetime.date(2026, 11, 1))), (1, 1))

    def test_the_repeated_hour_of_a_fall_back_night_stays_on_one_day(self):
        both = [event('task_opened', '2026-11-01T05:30:00Z'), event('task_opened', '2026-11-01T06:30:00Z', session=S2)]  # 01:30 twice
        self.assertEqual(day_count(hub(both, zone=NEW_YORK), datetime.date(2026, 11, 1)), 2)

    def test_a_spring_forward_day_follows_the_zone_and_not_one_offset(self):
        # New York enters daylight time on 2026-03-08 at 07:00 UTC.
        now = datetime.datetime(2026, 3, 12, 12, tzinfo=UTC)
        events = [event('task_opened', '2026-03-08T04:59:00Z', session=S1),  # 23:59 on the 7th, offset -5
                  event('task_opened', '2026-03-08T05:00:00Z', session=S2),  # 00:00 on the 8th
                  event('task_opened', '2026-03-09T04:30:00Z', session=S3)]  # 00:30 on the 9th, offset -4
        result = hub(events, now=now, zone=NEW_YORK)
        self.assertEqual([day_count(result, datetime.date(2026, 3, day)) for day in (7, 8, 9)], [1, 1, 1])
        fixed = hub(events, now=now, tz=-300)  # the winter offset puts the last event on the 8th
        self.assertEqual([day_count(fixed, datetime.date(2026, 3, day)) for day in (7, 8, 9)], [1, 2, 0])

    def test_an_event_time_with_its_own_offset_is_converted_to_the_zone(self):
        events = [event('task_opened', '2026-11-01T00:30:00-04:00', session=S1), event('task_opened', '2026-11-01T23:30:00-05:00', session=S2),
                  event('task_opened', '2026-11-02T05:30:00+01:00', session=S3)]
        result = hub(events, zone=NEW_YORK)
        self.assertEqual(day_count(result, datetime.date(2026, 11, 1)), 3)

    def test_today_follows_the_zone(self):
        now = datetime.datetime(2026, 11, 2, 4, 30, tzinfo=UTC)  # 23:30 on Sunday the 1st in New York
        result = hub(now=now, zone=NEW_YORK)
        self.assertEqual(result['participation']['weeks'][-1]['start'], '2026-10-26')
        self.assertEqual(result['participation']['weeks'][-1]['days'], [0, 0, 0, 0, 0, 0, 0])
        self.assertEqual(result['participation']['totals']['days'], 56)
        self.assertEqual(hub(now=now)['participation']['weeks'][-1]['start'], '2026-11-02')
        self.assertEqual(result['generatedAt'], '2026-11-02T04:30:00Z')

    def test_a_zone_without_daylight_time_matches_the_same_fixed_offset(self):
        rng = random.Random(7)
        india = zoneinfo.ZoneInfo('Asia/Kolkata')
        events = [event('task_opened', iso(NOW + datetime.timedelta(minutes=rng.randrange(-60 * 24 * 60, 60 * 24 * 2)), 0), session=[S1, S2, S3][rng.randrange(3)])
                  for _ in range(200)]
        self.assertEqual(hub(events, zone=india), hub(events, tz=330))

    def test_a_differential_check_against_zoneinfo(self):
        rng = random.Random(20261106)
        for _ in range(40):
            now = datetime.datetime(2025, 1, 1, tzinfo=UTC) + datetime.timedelta(minutes=rng.randrange(0, 60 * 24 * 900))
            today = now.astimezone(NEW_YORK).date()
            start = today - datetime.timedelta(days=today.weekday() + 49)
            events, expected = [], {}
            for _ in range(60):
                stamp = now + datetime.timedelta(minutes=rng.randrange(-60 * 24 * 80, 60 * 24 * 3))
                text = iso(stamp, rng.choice([0, 0, -240, -300, 330, 60]))
                session = [S1, S2, S3][rng.randrange(3)]
                events.append(event('task_opened', text, session=session))
                day = stamp.astimezone(NEW_YORK).date()
                if start <= day <= today:
                    expected.setdefault(day, set()).add(session)
            result = hub(events, now=now, zone=NEW_YORK)
            for offset in range(56):
                day = start + datetime.timedelta(days=offset)
                want = None if day > today else len(expected.get(day, ()))
                self.assertEqual(flat_days(result)[offset], want, (now, day))
            self.assertEqual(result['participation']['totals']['days'], (today - start).days + 1)

    def test_the_first_and_last_instants_of_the_calendar_do_not_fail_with_a_zone(self):
        edge = [event('task_opened', '0001-01-01T00:00:00Z'), event('task_opened', '9999-12-31T23:59:59Z', session=S2),
                event('task_opened', '0001-01-01T00:00:00+23:59', session=S3)]
        result = hub(edge, zone=NEW_YORK)
        self.assertEqual(result['log']['skippedEvents'], 0)
        self.assertEqual(result['log']['futureEvents'], 1)
        self.assertEqual(result['participation']['totals']['sessions'], 0)

    def test_the_last_instant_of_the_calendar_in_a_zone_ahead_of_utc_is_still_the_future(self):
        tokyo = zoneinfo.ZoneInfo('Asia/Tokyo')  # 9999-12-31T23:59:59Z is in the year 10000 there, which no date can hold
        result = hub([event('task_opened', '9999-12-31T23:59:59Z')], zone=tokyo)
        self.assertEqual((result['log']['futureEvents'], result['log']['skippedEvents']), (1, 0))
        result = hub([event('task_opened', '0001-01-01T00:00:00Z')], zone=tokyo)  # fits in the zone: Tokyo is ahead
        self.assertEqual(result['log']['futureEvents'], 0)

    def test_an_offset_and_a_zone_together_are_refused(self):
        with self.assertRaises(ValueError):
            hub(tz=60, zone=NEW_YORK)
        hub(tz=0, zone=NEW_YORK)

    def test_the_zone_must_be_a_tzinfo(self):
        for bad in ('America/New_York', 5, object()):
            with self.assertRaises(ValueError, msg=repr(bad)):
                hub(zone=bad)


class LoadZoneTests(unittest.TestCase):
    def test_valid_names_load(self):
        for name in ('America/New_York', 'UTC', 'Asia/Kolkata', 'Etc/GMT+5', 'America/Argentina/Buenos_Aires', 'Europe/London'):
            self.assertIsInstance(hub_data.load_zone(name), datetime.tzinfo, name)

    def test_the_loaded_zone_is_the_zoneinfo_of_that_name(self):
        self.assertEqual(hub_data.load_zone('America/New_York').key, 'America/New_York')

    def test_bad_names_are_a_value_error(self):
        bad = ('', ' ', 'x' * 100, '../etc/passwd', '/etc/passwd', 'America/', '/America', 'America//New_York', 'America/New York', 'Nope/Nowhere',
               'a/b/c/d', 'America', 'America/New_York\n', 'America/..', '.', '..', './UTC', 'UTC/', 'Amerika/New_York', 'america/new_york\x00',
               '\u202eUTC', 'America/New_York ', 5, None, ['UTC'], b'UTC', 'zone.tab', 'tzdata.zi')
        for name in bad:
            with self.assertRaises(ValueError, msg=repr(name)):
                hub_data.load_zone(name)


class ZoneNamePatternTests(unittest.TestCase):
    """The pattern alone must refuse a bad name, whatever the zone database would say."""

    def test_a_name_that_does_not_match_the_pattern_never_reaches_the_zone_database(self):
        names = ('', '..', '.', 'zone.tab', './UTC', 'America/../UTC', 'a/b/c/d', 'UTC\n', 'America/New_York\n', '/UTC', 'UTC/', 'America//New_York',
                 'America/New York', 'x' * 33, 'A/' + 'x' * 33, '\u202eUTC', '1UTC', '_UTC', '-UTC', 'UTC\x00')
        with mock.patch.object(zoneinfo, 'ZoneInfo') as database:
            for name in names:
                with self.assertRaises(ValueError, msg=repr(name)):
                    hub_data.load_zone(name)
        database.assert_not_called()

    def test_a_name_that_matches_the_pattern_is_looked_up_as_it_is(self):
        with mock.patch.object(zoneinfo, 'ZoneInfo', return_value='zone') as database:
            for name in ('UTC', 'America/New_York', 'Etc/GMT+5', 'America/Argentina/Buenos_Aires', 'x' * 32, 'A/' + 'x' * 32):
                self.assertEqual(hub_data.load_zone(name), 'zone')
        self.assertEqual(database.call_count, 6)


class ReaderTests(unittest.TestCase):
    """The thin reader, on real stores in a temporary folder."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data = Path(self.temp.name)
        self.log = EventLog(self.data)
        self.lessons = LessonStore(self.data)

    def read(self, tz=0, now=NOW, zone=None):
        """Build the hub. What the reader wrote to stderr is kept in self.stderr."""
        captured = io.StringIO()
        with contextlib.redirect_stderr(captured):
            result = hub_data.build_hub(self.log, self.lessons, now, tz, zone)
        self.stderr = captured.getvalue()
        return result

    def write_log(self, events, tail=b'', raw_lines=()):
        folder = self.data / '_events'
        folder.mkdir(mode=0o700, exist_ok=True)
        body = b''.join(json.dumps(e, separators=(',', ':')).encode() + b'\n' for e in events) + b''.join(raw_lines) + tail
        (folder / 'events.jsonl').write_bytes(body)
        os.chmod(folder / 'events.jsonl', 0o600)

    def import_module(self, module_id='intro', version='1.0.0', tasks=2, checks=2, title='Intro'):
        document = json.loads(FIXTURE.read_text())
        base = document['tasks'][0]
        document.update(id=module_id, version=version, title=title)
        document['tasks'] = [dict(base, id=f'task-{n}', prompt=f'Prompt {n}') for n in range(1, tasks + 1)]
        document['checks'] = [{'id': f'check-{n}', 'kind': 'self-reported', 'description': f'Check number {n}'} for n in range(1, checks + 1)]
        self.lessons.import_files([(f'{module_id}-{version}.json', (json.dumps(document) + '\n').encode())])
        return document

    def test_no_stores_at_all(self):
        before = sorted(self.data.rglob('*'))
        result = self.read()
        self.assertEqual(result['log'], log_block(exists=False, ok=True, events=0, damagedLines=0, truncatedTail=False))
        self.assertEqual((result['focus'], result['library'], result['checkpoints']), (None, [], {'done': 0, 'total': 0}))
        self.assertEqual(sorted(self.data.rglob('*')), before)  # reading creates nothing

    def test_modules_and_events_roll_up(self):
        self.import_module('alpha', title='Alpha')
        self.import_module('beta', title='Beta')
        events = [event('task_finished', module='beta', task='task-1'), event('task_opened', module='beta', task='task-2'),
                  event('check_result', module='beta', task='task-2', status='failed', checkId='check-1')]
        self.write_log(events)
        result = self.read()
        self.assertEqual(result['log'], log_block(exists=True, ok=True, events=3, damagedLines=0, truncatedTail=False))
        self.assertEqual(result['focus'], {'moduleId': 'beta', 'version': '1.0.0', 'title': 'Beta', 'tasksTotal': 2, 'tasksDone': 1, 'tasksSkipped': 0,
                                           'nextTask': {'taskId': 'task-2', 'text': 'Prompt 2'}})
        self.assertEqual([(m['moduleId'], m['status']) for m in result['library']], [('alpha', 'next'), ('beta', 'focus')])
        self.assertEqual(result['checks'][0]['title'], 'Check number 1')
        self.assertEqual(result['checkpoints'], {'done': 0, 'total': 2})

    def test_the_pinned_version_is_the_one_shown_and_a_newer_unpinned_one_is_not(self):
        self.import_module('alpha', version='1.0.0', title='Old')
        self.import_module('alpha', version='1.1.0', title='New')
        result = self.read()
        self.assertEqual((result['library'][0]['version'], result['library'][0]['title']), ('1.0.0', 'Old'))

    def test_a_module_with_stored_versions_but_no_pin_is_ignored(self):
        self.import_module('alpha')
        self.import_module('beta')
        pins = self.data / '_lessons' / 'pins.json'
        document = json.loads(pins.read_text())
        del document['pins']['beta']
        pins.write_text(json.dumps(document))
        result = self.read()
        self.assertEqual([m['moduleId'] for m in result['library']], ['alpha'])
        self.assertEqual(result['checkpoints']['total'], 1)

    def test_a_damaged_line_is_a_finding_and_not_an_error(self):
        self.import_module('alpha')
        good = event('task_finished', module='alpha', task='task-1')
        self.write_log([good], raw_lines=[b'{"not": "an event"}\n', b'garbage\n'])
        result = self.read()
        self.assertEqual(result['log'], log_block(exists=True, ok=False, events=1, damagedLines=2, truncatedTail=False))
        self.assertEqual(result['focus']['tasksDone'], 1)
        self.assertEqual(result['participation']['totals']['sessions'], 1)

    def test_a_truncated_tail_is_reported_and_is_not_counted_as_a_damaged_line(self):
        self.import_module('alpha')
        self.write_log([event('task_opened', module='alpha', task='task-1')], tail=b'{"schemaVersion":"1.0","eventId":"x')
        result = self.read()
        self.assertEqual(result['log'], log_block(exists=True, ok=False, events=1, damagedLines=0, truncatedTail=True))
        self.assertEqual(result['participation']['totals']['sessions'], 1)

    def test_a_log_over_its_limits_is_unreadable_not_an_error(self):
        self.import_module('alpha')
        folder = self.data / '_events'
        folder.mkdir(mode=0o700)
        (folder / 'events.jsonl').write_bytes(b'\n' * 100_000)
        os.chmod(folder / 'events.jsonl', 0o600)
        result = self.read()
        self.assertEqual(result['log'], log_block(exists=True, ok=False, fault='log_corrupt', events=0, damagedLines=0, truncatedTail=False))
        self.assertEqual(result['focus']['moduleId'], 'alpha')
        self.assertEqual(result['checkpoints'], {'done': 0, 'total': 1})
        self.assertIn('EventLogError', self.stderr)
        self.assertIn('log_corrupt', self.stderr)

    def test_an_unsafe_log_path_is_unreadable_not_an_error(self):
        self.import_module('alpha')
        folder = self.data / '_events'
        folder.mkdir(mode=0o700)
        (self.data / 'elsewhere.jsonl').write_bytes(b'')
        (folder / 'events.jsonl').symlink_to(self.data / 'elsewhere.jsonl')
        result = self.read()
        self.assertEqual((result['log']['ok'], result['log']['fault']), (False, 'unsafe_path'))
        self.assertEqual(result['library'][0]['moduleId'], 'alpha')
        self.assertIn('unsafe_path', self.stderr)

    def test_a_folder_that_is_a_symbolic_link_is_an_unsafe_path_with_a_fault(self):
        self.import_module('alpha')
        (self.data / 'elsewhere').mkdir(mode=0o700)
        (self.data / '_events').symlink_to(self.data / 'elsewhere')
        result = self.read()
        self.assertEqual((result['log']['ok'], result['log']['fault']), (False, 'unsafe_path'))

    def test_a_file_that_cannot_be_read_is_a_fault(self):
        self.import_module('alpha')
        broken = EventLogError('The event log cannot be read (EIO).', 'log_unreadable')
        with mock.patch.object(EventLog, 'read', side_effect=broken):
            result = self.read()
        self.assertEqual((result['log']['ok'], result['log']['fault'], result['log']['exists']), (False, 'log_unreadable', True))
        self.assertIn('log_unreadable', self.stderr)
        self.assertNotIn(str(self.data), self.stderr)

    def test_a_healthy_log_has_no_fault_and_writes_nothing_to_stderr(self):
        self.import_module('alpha')
        self.write_log([event('task_opened', module='alpha')])
        result = self.read()
        self.assertEqual(result['log'], log_block(events=1))
        self.assertEqual(self.stderr, '')

    def test_a_damaged_but_readable_log_has_no_fault(self):
        self.write_log([event('task_opened')], raw_lines=[b'garbage\n'])
        self.assertIsNone(self.read()['log']['fault'])

    def test_a_malformed_event_in_a_valid_report_is_skipped_and_named_on_stderr(self):
        good, broken = event('task_opened'), event('task_opened')
        broken['sessionId'] = None
        report = {'ok': True, 'exists': True, 'lines': [{'line': 1, 'ok': True}, {'line': 2, 'ok': True}],
                  'events': [{'line': 1, 'event': good}, {'line': 2, 'event': broken}], 'truncatedTail': False,
                  'total': 2, 'offset': 0, 'limit': None, 'more': False}
        with mock.patch.object(EventLog, 'read', return_value=report):
            result = self.read()
        self.assertEqual(result['log'], log_block(events=2, skippedEvents=1))
        self.assertEqual(result['participation']['totals']['sessions'], 1)
        self.assertIn('line 2', self.stderr)
        self.assertIn('sessionId', self.stderr)

    def test_a_flood_of_skipped_events_writes_a_few_lines_and_a_total(self):
        broken = []
        for index in range(40):
            item = event('task_opened')
            item['time'] = 'nonsense'
            broken.append({'line': index + 1, 'event': item})
        report = {'ok': True, 'exists': True, 'lines': [{'line': n + 1, 'ok': True} for n in range(40)], 'events': broken,
                  'truncatedTail': False, 'total': 40, 'offset': 0, 'limit': None, 'more': False}
        with mock.patch.object(EventLog, 'read', return_value=report):
            result = self.read()
        self.assertEqual(result['log']['skippedEvents'], 40)
        self.assertLessEqual(len(self.stderr.splitlines()), 8)
        self.assertIn('40', self.stderr.splitlines()[-1])

    def test_a_time_zone_name_is_used_by_the_reader(self):
        self.write_log([event('task_opened', '2026-11-01T04:30:00Z')])
        result = self.read(zone=NEW_YORK)
        self.assertEqual(day_count(result, datetime.date(2026, 11, 1)), 1)
        self.assertEqual(day_count(self.read(tz=-300), datetime.date(2026, 10, 31)), 1)

    def test_a_busy_lock_is_not_hidden_as_damage(self):
        busy = EventLogError('Another program is using the event log.', 'lock_unavailable', 503, retryable=True)
        with mock.patch.object(EventLog, 'read', side_effect=busy):
            with self.assertRaises(EventLogError) as caught:
                self.read()
        self.assertEqual(caught.exception.code, 'lock_unavailable')

    def test_the_log_is_read_once_per_call(self):
        self.import_module('alpha')
        self.write_log([event('task_opened', module='alpha')])
        with mock.patch.object(EventLog, 'read', autospec=True, side_effect=EventLog.read) as spy:
            self.read()
        self.assertEqual(spy.call_count, 1)
        self.assertEqual(spy.call_args.kwargs.get('limit'), None)

    def test_a_log_with_only_agent_events(self):
        self.import_module('alpha')
        self.write_log([event('task_opened', module='alpha', source='agent'), event('mode_changed', source='agent')])
        result = self.read()
        self.assertEqual(result['participation']['totals']['sessions'], 0)
        self.assertEqual(result['log']['events'], 2)
        self.assertEqual(result['focus']['moduleId'], 'alpha')

    def test_the_reader_does_not_change_either_store(self):
        self.import_module('alpha')
        self.write_log([event('task_opened', module='alpha')])
        def contents():
            # A read of the log makes its lock file when it is missing (docs/event-log.md). That empty file is not data.
            return {str(p): p.read_bytes() for p in sorted(self.data.rglob('*')) if p.is_file() and p.name != '.events.lock'}
        snapshot = contents()
        self.read()
        self.assertEqual(contents(), snapshot)

    def test_a_module_the_store_returns_in_the_wrong_shape_fails_closed(self):
        self.import_module('alpha')
        with mock.patch.object(LessonStore, 'get_module', return_value={'title': 'x', 'version': '1.0.0', 'tasks': 'nope', 'checks': []}):
            with self.assertRaises(Exception) as caught:
                self.read()
        self.assertEqual(getattr(caught.exception, 'code', None), 'corrupt_file')

    def test_fifty_modules_from_the_store(self):
        for number in range(50):
            self.import_module(f'mod-{number:02d}', tasks=1, checks=1)
        result = self.read()
        self.assertEqual(len(result['library']), 50)
        self.assertEqual(result['library'][0]['moduleId'], 'mod-00')

    def test_a_log_near_the_byte_cap_is_read_and_rolled_up(self):
        self.import_module('alpha')
        events, size, index = [], 0, 0
        while True:
            stamp = f'2026-10-{1 + index % 28:02d}T{index % 24:02d}:00:00Z'
            item = event('check_result', stamp, module='alpha', task='task-1', status='failed', checkId='check-1', skills=['loops'],
                         session=str(uuid.UUID(int=index % 400)))
            item['eventId'] = str(uuid.UUID(int=index + 1000))
            line = json.dumps(item, separators=(',', ':')).encode() + b'\n'
            if size + len(line) > event_log.le.MAX_BYTES - 4096:
                break
            events.append(item)
            size += len(line)
            index += 1
        self.write_log(events)
        started = time.perf_counter()
        result = self.read()
        elapsed = time.perf_counter() - started
        self.assertTrue(result['log']['ok'])
        self.assertEqual(result['log']['events'], len(events))
        self.assertEqual(result['checks'][0]['retriesBeforeFirstPass'], len(events))
        self.assertEqual(result['skills'], [{'skillId': 'loops', 'passed': 0, 'tried': 5, 'window': 5}])
        self.assertLess(elapsed, 120)
        print(f'\nnear-cap log: {len(events)} events, {size} bytes, first read {elapsed:.2f} s', file=sys.stderr)


if __name__ == '__main__':
    unittest.main()
