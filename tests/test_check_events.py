"""The check events: `attempt_submitted` and `check_result`, written when a check runs.

The tests use the real event log, the real check runner, and the real lesson store on temporary folders.
Every event that a test reads from the log is checked again with `learning_event.validate_event`.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import sys
import tempfile
import threading
import time
import unittest
import uuid
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import check_consent
import check_events
import check_runner
import learning_event
from event_log import EventLog, EventLogError
from lesson_store import LessonStore

MODULE_FIXTURE = ROOT / 'tests' / 'fixtures' / 'contracts' / 'valid' / 'lesson-module-minimal.json'
EMPTY_SHA = hashlib.sha256(b'').hexdigest()
UUID = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')
ATTEMPT = re.compile(r'^[A-Za-z0-9_-]{1,64}$')
STAMP = re.compile(r'^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$')
SOURCE = 'print("hello from a script")\n'
SESSION = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'


def manifest_digest(files):
    """The digest that the contract of these events defines, written out again so the test does not trust the code."""
    lines = b''
    for path, content in sorted(files.items(), key=lambda item: item[0].encode('utf-8')):
        lines += path.encode('utf-8') + b'\0' + hashlib.sha256(content).hexdigest().encode('ascii') + b'\0' + str(len(content)).encode('ascii') + b'\n'
    return hashlib.sha256(lines).hexdigest()


def module_document(tasks=None):
    document = json.loads(MODULE_FIXTURE.read_text())
    document['schemaVersion'] = '1.1'
    if tasks is not None:
        template = document['tasks'][0]
        document['tasks'] = [dict(template, id=name) for name in tasks]
    document['checks'] = [
        {'id': 'self', 'kind': 'self-reported', 'description': 'The student says it is done.'},
        {'id': 'has-readme', 'kind': 'automated', 'description': 'A README exists.', 'runner': {'type': 'file-exists', 'path': 'README.md'}},
        {'id': 'has-todo', 'kind': 'automated', 'description': 'A TODO exists.', 'runner': {'type': 'text-match', 'path': 'README.md', 'text': 'TODO'}},
        {'id': 'bad-pattern', 'kind': 'automated', 'description': 'A broken pattern.', 'runner': {'type': 'text-match', 'path': 'README.md', 'pattern': '('}},
        {'id': 'valid-json', 'kind': 'automated', 'description': 'JSON is valid.', 'runner': {'type': 'json-valid', 'path': 'data.json'}},
        {'id': 'a-dir', 'kind': 'automated', 'description': 'A folder exists.', 'runner': {'type': 'file-exists', 'path': 'src', 'kind': 'directory'}},
        {'id': 'script-a', 'kind': 'automated', 'description': 'A script check.', 'runner': {'type': 'script'}, 'runtime': 'python3', 'source': SOURCE},
        {'id': 'git', 'kind': 'automated', 'description': 'A commit exists.', 'runner': {'type': 'git-commit-exists'}},
    ]
    return document


class EventsCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(os.path.realpath(self.temp.name))
        self.data = base / 'data'
        self.data.mkdir()
        self.work = base / 'home' / 'work'
        self.work.mkdir(parents=True)
        (self.work / 'README.md').write_text('hello TODO\n')
        (self.work / 'data.json').write_text('{"a": 1}')
        (self.work / 'src').mkdir()
        (self.work / 'src' / 'app.py').write_text('print("hi")\n')
        self.lessons = LessonStore(self.data)
        self.import_module(module_document())
        self.log = EventLog(self.data)

    def import_module(self, document):
        self.lessons.import_files([('m.json', json.dumps(document))])
        self.document = self.lessons.get_module(document['id'], document['version'])
        return self.document

    def check(self, check_id):
        return next(item for item in self.document['checks'] if item['id'] == check_id)

    def record(self, check_id, *, task_id=None, log=None, consent=None, registry=None, limits=None, sources=None, session_id=SESSION):
        kwargs = {'sources': sources} if sources is not None else {}
        return check_events.run_and_record(
            self.check(check_id), self.work, log=log or self.log, session_id=session_id, module=self.document, task_id=task_id,
            consent=consent, registry=registry, limits=limits, **kwargs)

    def script_consent(self):
        return {'checkHash': check_consent.check_hash(self.check('script-a'))}

    def events(self):
        report = self.log.read()
        self.assertTrue(report['ok'], report)
        found = [item['event'] for item in report['events']]
        for event in found:
            self.assertEqual(learning_event.validate_event(event), {'ok': True}, event)
        return found

    def log_bytes(self):
        path = self.data / '_events' / 'events.jsonl'
        return path.read_bytes() if path.exists() else b''


class FixedSources:
    """Sources with fixed answers, so a test can name the exact values."""

    def __init__(self, now=1_700_000_000.4, steps=(0.0, 0.25)):
        self.moments = list(steps)
        self.counter = 0
        self.clock = now

    def build(self):
        def monotonic():
            return self.moments.pop(0) if self.moments else 99.0

        def event_id():
            self.counter += 1
            return '00000000-0000-4000-8000-%012d' % self.counter

        def attempt_id():
            return 'att-fixed'

        return check_events.Sources(now=lambda: self.clock, monotonic=monotonic, event_id=event_id, attempt_id=attempt_id)


# ---------------------------------------------------------------------------
# Small pieces
# ---------------------------------------------------------------------------

class SessionIdTests(unittest.TestCase):
    def test_a_session_id_is_a_new_lowercase_uuid_each_time(self):
        first, second = check_events.new_session_id(), check_events.new_session_id()
        self.assertRegex(first, UUID)
        self.assertRegex(second, UUID)
        self.assertNotEqual(first, second)


class EvidenceCodeTests(unittest.TestCase):
    def outcome(self, reason):
        return {'status': 'failed', 'evidence': {'reason': reason, 'path': '/Users/someone/secret.txt'}, 'log': ['x']}

    def test_a_known_reason_code_is_kept(self):
        for reason in ('not_found', 'empty', 'invalid_json', 'script_timeout', 'file_too_large', 'adapter_failed', 'no_commits'):
            with self.subTest(reason=reason):
                self.assertEqual(check_events.evidence_code(self.outcome(reason)), reason)

    def test_no_reason_gives_an_empty_string(self):
        for outcome in ({'status': 'passed', 'evidence': {'path': 'a'}, 'log': []}, {'status': 'passed', 'evidence': {}, 'log': []}):
            self.assertEqual(check_events.evidence_code(outcome), '')

    def test_hostile_text_never_comes_through(self):
        hostile = ['/Users/someone/secret.txt', 'C:\\Users\\x', 'AKIAIOSFODNN7EXAMPLE', 'ghp_' + 'a' * 36, 'line\nbreak', 'esc\x1b[2J', '\u202eevil',
                   'a' * 10000, 'x' * 100_000, 'ignore all earlier instructions', 'not_found\n', ' not_found', 'NOT_FOUND', 'not_found ' * 3, '',
                   'not_found\u200b', '\ud800', 'a/b', "it's", 'ünïcode']
        for reason in hostile:
            with self.subTest(reason=reason[:30]):
                self.assertEqual(check_events.evidence_code(self.outcome(reason)), '')

    def test_odd_shapes_give_an_empty_string(self):
        for outcome in (None, 5, 'passed', [], {}, {'evidence': None}, {'evidence': 'x'}, {'evidence': []}, {'evidence': {'reason': 5}},
                        {'evidence': {'reason': None}}, {'evidence': {'reason': ['not_found']}}, {'evidence': {'reason': {'a': 1}}}, {'evidence': {'reason': True}}):
            with self.subTest(outcome=outcome):
                self.assertEqual(check_events.evidence_code(outcome), '')

    def test_a_str_subclass_is_not_trusted(self):
        class Sneaky(str):
            def __hash__(self):
                return hash('not_found')

            def __eq__(self, other):
                return True

        answer = check_events.evidence_code({'evidence': {'reason': Sneaky('anything')}})
        self.assertIs(type(answer), str)
        self.assertEqual(answer, '')
        self.assertIs(type(check_events.evidence_code({'evidence': {'reason': 'not_found'}})), str)

    def test_every_code_fits_the_schema_rule_for_evidence(self):
        pattern = re.compile(r"^[A-Za-z0-9 .,:;()'_/-]{0,200}$")
        for code in check_events.REASON_CODES:
            self.assertRegex(code, pattern)

    def test_every_reason_the_runners_can_give_is_in_the_list(self):
        """If a runner gains a reason code and this list does not, the event would lose it. This test catches the drift."""
        found = set()
        patterns = (r"'reason': '([a-z_]+)'", r"Problem\('[a-z]+', '([a-z_]+)'", r"_failure\('([a-z_]+)'", r"return 0, '([a-z_]+)'",
                    r"\('(?:failed|error)', '([a-z_]+)', ", r"check_workspace\.[A-Z_]+: '([a-z_]+)'", r"result\('[a-z]+', \{'reason': '([a-z_]+)'")
        for name in ('check_runner.py', 'check_script.py', 'check_git.py', 'check_workspace.py'):
            text = (ROOT / name).read_text()
            for pattern in patterns:
                found.update(re.findall(pattern, text))
        found -= {'passed', 'failed', 'error', 'skipped'}
        self.assertGreater(len(found), 25, found)
        self.assertEqual(sorted(found - check_events.REASON_CODES), [])


# ---------------------------------------------------------------------------
# The digest of the work
# ---------------------------------------------------------------------------

class ArtifactTests(EventsCase):
    def artifact(self, check_id, **kwargs):
        return check_events.artifact_for(self.check(check_id), self.work, kwargs.get('limits') or check_runner.Limits())

    def test_a_file_check_hashes_one_file_by_the_manifest_rule(self):
        artifact = self.artifact('has-readme')
        self.assertEqual(artifact.digest, manifest_digest({'README.md': b'hello TODO\n'}))
        self.assertEqual(artifact.size, len(b'hello TODO\n'))

    def test_text_match_and_json_valid_use_their_own_path(self):
        self.assertEqual(self.artifact('has-todo'), self.artifact('has-readme'))
        artifact = self.artifact('valid-json')
        self.assertEqual((artifact.digest, artifact.size), (manifest_digest({'data.json': b'{"a": 1}'}), 8))

    def test_the_digest_changes_with_the_content_and_with_the_name(self):
        before = self.artifact('has-readme')
        (self.work / 'README.md').write_text('hello TODO!\n')
        after = self.artifact('has-readme')
        self.assertNotEqual(before.digest, after.digest)
        self.assertEqual(after.size, before.size + 1)
        renamed = {'id': 'x', 'kind': 'automated', 'description': 'd', 'runner': {'type': 'file-exists', 'path': 'src/app.py'}}
        (self.work / 'copy.py').write_text('print("hi")\n')
        other = {'id': 'y', 'kind': 'automated', 'description': 'd', 'runner': {'type': 'file-exists', 'path': 'copy.py'}}
        self.assertNotEqual(check_events.artifact_for(renamed, self.work, check_runner.Limits()).digest,
                            check_events.artifact_for(other, self.work, check_runner.Limits()).digest)

    def test_a_missing_file_is_the_digest_of_an_empty_manifest(self):
        (self.work / 'README.md').unlink()
        artifact = self.artifact('has-readme')
        self.assertEqual((artifact.digest, artifact.size), (EMPTY_SHA, 0))

    def test_a_folder_holds_no_file_so_it_is_the_empty_manifest(self):
        artifact = self.artifact('a-dir')
        self.assertEqual((artifact.digest, artifact.size), (EMPTY_SHA, 0))

    def test_a_git_check_and_an_unknown_runner_examine_no_files(self):
        self.assertEqual(self.artifact('git'), check_events.Artifact(EMPTY_SHA, 0))
        unknown = {'id': 'u', 'kind': 'automated', 'description': 'd', 'runner': {'type': 'other-thing', 'path': 'README.md'}}
        self.assertEqual(check_events.artifact_for(unknown, self.work, check_runner.Limits()), check_events.Artifact(EMPTY_SHA, 0))

    def test_a_self_reported_or_odd_check_has_the_empty_manifest(self):
        for check in (self.check('self'), None, 5, {}, {'runner': 5}, {'kind': 'automated', 'runner': {'type': 'file-exists'}},
                      {'kind': 'automated', 'runner': {'type': 'file-exists', 'path': 5}}):
            with self.subTest(check=check):
                self.assertEqual(check_events.artifact_for(check, self.work, check_runner.Limits()), check_events.Artifact(EMPTY_SHA, 0))

    def test_a_link_that_leaves_the_workspace_examines_nothing(self):
        outside = self.work.parent / 'outside.txt'
        outside.write_text('outside secret')
        (self.work / 'leak.txt').symlink_to(outside)
        check = {'id': 'x', 'kind': 'automated', 'description': 'd', 'runner': {'type': 'file-exists', 'path': 'leak.txt'}}
        self.assertEqual(check_events.artifact_for(check, self.work, check_runner.Limits()), check_events.Artifact(EMPTY_SHA, 0))

    def test_a_link_inside_the_workspace_is_read_under_the_runner_path(self):
        (self.work / 'alias.md').symlink_to(self.work / 'README.md')
        check = {'id': 'x', 'kind': 'automated', 'description': 'd', 'runner': {'type': 'file-exists', 'path': 'alias.md'}}
        self.assertEqual(check_events.artifact_for(check, self.work, check_runner.Limits()).digest, manifest_digest({'alias.md': b'hello TODO\n'}))

    def test_a_hostile_path_in_the_check_examines_nothing(self):
        for path in ('../outside', '/etc/passwd', 'a\x00b', 'a' * 100_000, '..', '.', '', 'a\\b', 'C:\\x'):
            with self.subTest(path=path[:20]):
                check = {'id': 'x', 'kind': 'automated', 'description': 'd', 'runner': {'type': 'file-exists', 'path': path}}
                self.assertEqual(check_events.artifact_for(check, self.work, check_runner.Limits()), check_events.Artifact(EMPTY_SHA, 0))

    def test_a_missing_workspace_examines_nothing(self):
        self.assertEqual(check_events.artifact_for(self.check('has-readme'), self.work / 'nope', check_runner.Limits()), check_events.Artifact(EMPTY_SHA, 0))
        self.assertEqual(check_events.artifact_for(self.check('has-readme'), None, check_runner.Limits()), check_events.Artifact(EMPTY_SHA, 0))

    def test_a_file_over_the_read_limit_of_the_runner_examines_nothing_for_text_match(self):
        (self.work / 'README.md').write_bytes(b'x' * 50)
        limits = check_runner.Limits(max_file_bytes=10)
        self.assertEqual(self.artifact('has-todo', limits=limits), check_events.Artifact(EMPTY_SHA, 0))

    def test_a_file_over_the_cap_for_a_work_digest_is_reported_not_guessed(self):
        (self.work / 'README.md').write_bytes(b'x' * 50)
        with mock.patch.object(check_events, 'MAX_ARTIFACT_BYTES', 10):
            with self.assertRaises(check_events.ArtifactError) as raised:
                self.artifact('has-readme')
        self.assertEqual(raised.exception.code, 'artifact_too_large')

    def test_several_small_files_over_the_cap_together_are_reported(self):
        """Each file is under the cap. Their sum is not."""
        with mock.patch.object(check_events, 'MAX_ARTIFACT_BYTES', 15):
            with self.assertRaises(check_events.ArtifactError) as raised:
                self.artifact('script-a')
        self.assertEqual(raised.exception.code, 'artifact_too_large')
        with mock.patch.object(check_events, 'MAX_ARTIFACT_BYTES', 31):
            self.assertEqual(self.artifact('script-a').size, 31)

    def test_a_path_with_extra_slashes_and_dots_is_hashed_under_its_plain_form(self):
        plain = {'id': 'x', 'kind': 'automated', 'description': 'd', 'runner': {'type': 'file-exists', 'path': 'src/app.py'}}
        for path in ('./src//app.py', 'src/./app.py', 'src/app.py/'):
            odd = {'id': 'y', 'kind': 'automated', 'description': 'd', 'runner': {'type': 'file-exists', 'path': path}}
            with self.subTest(path=path):
                self.assertEqual(check_events.artifact_for(odd, self.work, check_runner.Limits()), check_events.artifact_for(plain, self.work, check_runner.Limits()))
        self.assertEqual(check_events.artifact_for(plain, self.work, check_runner.Limits()).digest, manifest_digest({'src/app.py': b'print("hi")\n'}))

    def test_file_exists_hashes_a_file_that_is_over_the_read_limit_of_the_other_runners(self):
        (self.work / 'README.md').write_bytes(b'x' * 50)
        limits = check_runner.Limits(max_file_bytes=10)
        artifact = self.artifact('has-readme', limits=limits)
        self.assertEqual((artifact.digest, artifact.size), (manifest_digest({'README.md': b'x' * 50}), 50))

    def test_a_file_that_grows_past_the_limit_while_it_is_read(self):
        big = self.work / 'big.bin'
        big.write_bytes(b'x' * 50)

        def open_big(*args, **kwargs):
            return os.open(big, os.O_RDONLY)

        with mock.patch.object(check_events.check_workspace, 'open_inside', open_big):
            with mock.patch.object(check_events, 'MAX_ARTIFACT_BYTES', 10):
                with self.assertRaises(check_events.ArtifactError) as raised:
                    self.artifact('has-readme')
            self.assertEqual(raised.exception.code, 'artifact_too_large')
            limits = check_runner.Limits(max_file_bytes=10)
            self.assertEqual(self.artifact('has-todo', limits=limits), check_events.Artifact(EMPTY_SHA, 0))

    def test_a_read_error_while_hashing_is_reported_and_the_descriptor_is_closed(self):
        def open_descriptors():
            return len(os.listdir('/dev/fd'))

        before = open_descriptors()
        with mock.patch.object(check_events.os, 'read', side_effect=OSError(5, 'I/O error')):
            with self.assertRaises(check_events.ArtifactError) as raised:
                self.artifact('has-readme')
        self.assertEqual(raised.exception.code, 'artifact_unavailable')
        self.assertEqual(open_descriptors(), before)

    def test_a_script_check_hashes_the_files_the_runner_copies(self):
        (self.work / '.git').mkdir()
        (self.work / '.git' / 'HEAD').write_text('ref')
        (self.work / 'node_modules').mkdir()
        (self.work / 'node_modules' / 'x.js').write_text('x')
        (self.work / 'dir with space').mkdir()
        (self.work / 'dir with space' / 'caf\u00e9.txt').write_text('caf\u00e9')
        artifact = self.artifact('script-a')
        expected = {'README.md': b'hello TODO\n', 'data.json': b'{"a": 1}', 'src/app.py': b'print("hi")\n', 'dir with space/caf\u00e9.txt': 'caf\u00e9'.encode('utf-8')}
        self.assertEqual(artifact.digest, manifest_digest(expected))
        self.assertEqual(artifact.size, sum(len(content) for content in expected.values()))

    def test_the_script_itself_is_not_part_of_the_work(self):
        names_before = sorted(path.name for path in self.work.iterdir())
        first = self.artifact('script-a')
        self.assertEqual(sorted(path.name for path in self.work.iterdir()), names_before)
        self.assertEqual(self.artifact('script-a'), first)

    def test_a_script_check_leaves_no_scratch_folder_behind(self):
        before = {path.name for path in Path(tempfile.gettempdir()).glob('schola-script-*')}
        for _ in range(3):
            self.artifact('script-a')
        after = {path.name for path in Path(tempfile.gettempdir()).glob('schola-script-*')}
        self.assertEqual(after - before, set())

    def test_a_script_check_in_an_empty_workspace_is_the_empty_manifest(self):
        empty = self.work.parent / 'empty'
        empty.mkdir()
        artifact = check_events.artifact_for(self.check('script-a'), empty, check_runner.Limits())
        self.assertEqual(artifact, check_events.Artifact(EMPTY_SHA, 0))

    def test_a_workspace_the_runner_refuses_to_copy_examines_nothing(self):
        outside = self.work.parent / 'outside.txt'
        outside.write_text('outside')
        (self.work / 'leak').symlink_to(outside)
        self.assertEqual(self.artifact('script-a'), check_events.Artifact(EMPTY_SHA, 0))

    def test_the_copy_limits_of_the_runner_apply(self):
        limits = check_runner.Limits(script_copy_files=1)
        self.assertEqual(self.artifact('script-a', limits=limits), check_events.Artifact(EMPTY_SHA, 0))
        limits = check_runner.Limits(script_copy_bytes=5)
        self.assertEqual(self.artifact('script-a', limits=limits), check_events.Artifact(EMPTY_SHA, 0))

    def test_a_copy_bigger_than_the_event_cap_is_reported(self):
        with mock.patch.object(check_events, 'MAX_ARTIFACT_BYTES', 10):
            with self.assertRaises(check_events.ArtifactError) as raised:
                self.artifact('script-a')
        self.assertEqual(raised.exception.code, 'artifact_too_large')

    def test_a_scratch_folder_that_cannot_be_made_is_reported(self):
        with mock.patch.object(check_events.tempfile, 'mkdtemp', side_effect=OSError('no space')):
            with self.assertRaises(check_events.ArtifactError) as raised:
                self.artifact('script-a')
        self.assertEqual(raised.exception.code, 'artifact_unavailable')

    def test_many_small_files_take_linear_time(self):
        for index in range(1500):
            (self.work / f'f{index}.txt').write_text(str(index))
        started = time.monotonic()
        artifact = self.artifact('script-a')
        self.assertLess(time.monotonic() - started, 20)
        self.assertGreater(artifact.size, 1500)

    def test_the_manifest_does_not_depend_on_the_order_the_entries_arrive_in(self):
        entries = [(name, hashlib.sha256(name.encode()).hexdigest(), len(name)) for name in ('b.txt', 'a/z.txt', 'a.txt', 'B.txt', 'caf\u00e9', 'a/b.txt')]
        expected = check_events.digest_entries(entries)
        for order in (list(reversed(entries)), entries[2:] + entries[:2], sorted(entries, key=lambda item: item[2])):
            self.assertEqual(check_events.digest_entries(order), expected)
        by_bytes = sorted(entries, key=lambda item: item[0].encode('utf-8'))
        lines = b''.join(name.encode('utf-8') + b'\0' + sha.encode() + b'\0' + str(size).encode() + b'\n' for name, sha, size in by_bytes)
        self.assertEqual(expected, check_events.Artifact(hashlib.sha256(lines).hexdigest(), sum(item[2] for item in entries)))

    def test_no_entries_is_the_empty_manifest(self):
        self.assertEqual(check_events.digest_entries([]), check_events.Artifact(EMPTY_SHA, 0))


# ---------------------------------------------------------------------------
# Building and writing the two events
# ---------------------------------------------------------------------------

class WriteTests(EventsCase):
    def test_a_passing_run_writes_an_attempt_then_a_result(self):
        outcome, report = self.record('has-readme')
        self.assertEqual(outcome['status'], 'passed')
        self.assertEqual(report, check_events.EventsReport(True, None))
        attempt, result = self.events()
        self.assertEqual((attempt['type'], result['type']), ('attempt_submitted', 'check_result'))

    def test_the_envelope_of_both_events(self):
        self.record('has-readme', sources=FixedSources().build())
        attempt, result = self.events()
        for event in (attempt, result):
            self.assertEqual(set(event), {'schemaVersion', 'eventId', 'time', 'sessionId', 'moduleId', 'moduleVersion', 'taskId', 'source', 'skills', 'type', 'data'})
            self.assertEqual(event['schemaVersion'], '1.0')
            self.assertEqual(event['source'], 'app')
            self.assertEqual(event['skills'], [])
            self.assertEqual(event['sessionId'], SESSION)
            self.assertEqual((event['moduleId'], event['moduleVersion']), ('hello-module', '1.0.0'))
            self.assertEqual(event['taskId'], 'task-1')
            self.assertEqual(event['time'], '2023-11-14T22:13:20Z')
        self.assertEqual((attempt['eventId'], result['eventId']), ('00000000-0000-4000-8000-000000000001', '00000000-0000-4000-8000-000000000002'))

    def test_the_data_of_both_events(self):
        self.record('has-readme', sources=FixedSources().build())
        attempt, result = self.events()
        self.assertEqual(attempt['data'], {'attemptId': 'att-fixed', 'artifactDigest': manifest_digest({'README.md': b'hello TODO\n'}), 'artifactBytes': 11})
        self.assertEqual(result['data'], {'checkId': 'has-readme', 'attemptId': 'att-fixed', 'status': 'passed', 'durationMs': 250, 'evidence': '', 'runnerType': 'file-exists'})

    def test_real_ids_and_times_have_the_right_shape(self):
        self.record('has-readme')
        attempt, result = self.events()
        self.assertRegex(attempt['eventId'], UUID)
        self.assertRegex(result['eventId'], UUID)
        self.assertNotEqual(attempt['eventId'], result['eventId'])
        self.assertRegex(attempt['time'], STAMP)
        self.assertRegex(result['time'], STAMP)
        self.assertRegex(attempt['data']['attemptId'], ATTEMPT)
        self.assertEqual(attempt['data']['attemptId'], result['data']['attemptId'])
        self.assertLessEqual(len(attempt['data']['attemptId']), 64)

    def test_the_time_is_utc_whatever_the_local_zone_is(self):
        if not hasattr(time, 'tzset'):
            self.skipTest('this system cannot change its time zone')
        previous = os.environ.get('TZ')
        os.environ['TZ'] = 'Asia/Tokyo'
        time.tzset()
        try:
            self.record('has-readme', sources=FixedSources().build())
        finally:
            if previous is None:
                del os.environ['TZ']
            else:
                os.environ['TZ'] = previous
            time.tzset()
        self.assertEqual({event['time'] for event in self.events()}, {'2023-11-14T22:13:20Z'})

    def test_the_time_of_each_event_is_read_when_it_is_built(self):
        moments = iter([1_700_000_000.0, 1_700_000_005.9])
        sources = check_events.Sources(now=lambda: next(moments), monotonic=FixedSources().build().monotonic)
        self.record('has-readme', sources=sources)
        attempt, result = self.events()
        self.assertEqual((attempt['time'], result['time']), ('2023-11-14T22:13:20Z', '2023-11-14T22:13:25Z'))

    def test_each_status_is_written_as_the_runner_reported_it(self):
        (self.work / 'README.md').write_text('no match here\n')
        cases = {'has-readme': 'passed', 'has-todo': 'failed', 'bad-pattern': 'error'}
        for check_id, status in cases.items():
            with self.subTest(check=check_id):
                outcome, report = self.record(check_id)
                self.assertEqual((outcome['status'], report.written), (status, True))
                self.assertEqual(self.events()[-1]['data']['status'], status)

    def test_a_failed_run_still_has_an_attempt_and_a_result(self):
        (self.work / 'README.md').unlink()
        outcome, report = self.record('has-readme')
        self.assertEqual((outcome['status'], report.written), ('failed', True))
        attempt, result = self.events()
        self.assertEqual((attempt['data']['artifactDigest'], attempt['data']['artifactBytes']), (EMPTY_SHA, 0))
        self.assertEqual((result['data']['status'], result['data']['evidence']), ('failed', 'not_found'))

    def test_an_error_result_writes_both_events_with_its_reason_code(self):
        outcome, report = self.record('bad-pattern')
        self.assertEqual((outcome['status'], report.written), ('error', True))
        attempt, result = self.events()
        self.assertEqual(attempt['type'], 'attempt_submitted')
        self.assertEqual((result['data']['status'], result['data']['evidence']), ('error', 'invalid_pattern'))

    def test_a_skipped_result_from_a_started_runner_writes_both_events(self):
        class Skipper:
            types = ('skip-check',)

            def run(self, check, workspace, limits):
                return {'status': 'skipped', 'evidence': {'reason': 'runtime_unavailable'}, 'log': []}

        registry = check_runner.Registry()
        registry.register(Skipper())
        self.document['checks'].append({'id': 'skip', 'kind': 'automated', 'description': 'd', 'runner': {'type': 'skip-check'}})
        outcome, report = self.record('skip', registry=registry)
        self.assertEqual((outcome['status'], report.written), ('skipped', True))
        attempt, result = self.events()
        self.assertEqual((attempt['type'], result['data']['status'], result['data']['evidence'], result['data']['runnerType']), ('attempt_submitted', 'skipped', 'runtime_unavailable', 'skip-check'))

    def test_a_script_check_writes_events_with_the_workspace_digest(self):
        outcome, report = self.record('script-a', consent=self.script_consent())
        self.assertEqual((outcome['status'], report.written), ('passed', True))
        attempt, result = self.events()
        self.assertEqual(attempt['data']['artifactDigest'], self.artifact_digest('script-a'))
        self.assertEqual(result['data']['runnerType'], 'script')

    def artifact_digest(self, check_id):
        return check_events.artifact_for(self.check(check_id), self.work, check_runner.Limits()).digest

    def test_a_script_that_changes_nothing_gives_the_same_digest_each_run(self):
        self.record('script-a', consent=self.script_consent())
        self.record('script-a', consent=self.script_consent())
        digests = {event['data']['artifactDigest'] for event in self.events() if event['type'] == 'attempt_submitted'}
        self.assertEqual(len(digests), 1)

    def test_the_duration_is_whole_milliseconds_and_never_negative_or_over_the_cap(self):
        for steps, expected in (((0.0, 0.0), 0), ((5.0, 3.0), 0), ((0.0, 0.0019), 1), ((0.0, 100000.0), 3_600_000)):
            with self.subTest(steps=steps):
                self.log = EventLog(Path(tempfile.mkdtemp(dir=self.temp.name)))
                self.record('has-readme', sources=FixedSources(steps=steps).build())
                self.assertEqual(self.events()[-1]['data']['durationMs'], expected)

    def test_the_run_is_timed_around_the_runner_only(self):
        moments = [0.0]

        def monotonic():
            return moments[0]

        def slow_runner(*args, **kwargs):
            moments[0] = 2.0
            return check_runner.RunTrace(check_runner.result('passed'), True)

        sources = check_events.Sources(monotonic=monotonic)
        with mock.patch.object(check_runner, 'run_traced', slow_runner):
            self.record('has-readme', sources=sources)
        self.assertEqual(self.events()[-1]['data']['durationMs'], 2000)

    def test_a_second_run_is_a_new_attempt_with_new_ids(self):
        self.record('has-readme')
        self.record('has-readme')
        events = self.events()
        self.assertEqual(len(events), 4)
        self.assertEqual(len({event['eventId'] for event in events}), 4)
        attempts = [event['data']['attemptId'] for event in events if event['type'] == 'attempt_submitted']
        self.assertEqual(len(set(attempts)), 2)
        results = [event['data']['attemptId'] for event in events if event['type'] == 'check_result']
        self.assertEqual(attempts, results)

    def test_the_session_id_is_the_one_that_was_passed(self):
        other = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb'
        self.record('has-readme', session_id=other)
        self.assertEqual({event['sessionId'] for event in self.events()}, {other})

    def test_the_workspace_path_and_the_content_never_reach_the_log(self):
        (self.work / 'my-secret-name.txt').write_text('TOPSECRET-CONTENT')
        self.document['checks'].append({'id': 'named', 'kind': 'automated', 'description': 'd', 'runner': {'type': 'text-match', 'path': 'my-secret-name.txt', 'text': 'TOPSECRET'}})
        self.record('named')
        self.record('script-a', consent=self.script_consent())
        raw = self.log_bytes()
        for needle in (b'my-secret-name', b'TOPSECRET', str(self.work).encode(), str(self.work.parent).encode(), b'README', b'app.py', b'hello from a script', b'/Users'):
            self.assertNotIn(needle, raw)

    def test_hostile_evidence_from_a_runner_never_reaches_the_log(self):
        hostile = {'reason': '/Users/someone/.ssh/id_rsa\nAKIAIOSFODNN7EXAMPLE', 'path': 'a' * 10000, 'output': 'ghp_' + 'z' * 36 + '\x1b[2J\u202e'}

        class Leaky:
            types = ('leaky-check',)

            def run(self, check, workspace, limits):
                return {'status': 'failed', 'evidence': dict(hostile), 'log': ['/Users/someone/' + 'q' * 5000]}

        registry = check_runner.Registry()
        registry.register(Leaky())
        self.document['checks'].append({'id': 'leaky', 'kind': 'automated', 'description': 'd', 'runner': {'type': 'leaky-check'}})
        outcome, report = self.record('leaky', registry=registry)
        self.assertEqual(report.written, True)
        self.assertEqual(self.events()[-1]['data']['evidence'], '')
        raw = self.log_bytes()
        for needle in (b'/Users', b'AKIA', b'ghp_', b'\x1b', b'a' * 50, b'qqqq', b'.ssh'):
            self.assertNotIn(needle, raw)

    def test_a_huge_reason_code_costs_no_more_than_a_short_one(self):
        class Huge:
            types = ('huge-check',)

            def run(self, check, workspace, limits):
                return {'status': 'failed', 'evidence': {'reason': 'a' * 100_000}, 'log': []}

        registry = check_runner.Registry()
        registry.register(Huge())
        self.document['checks'].append({'id': 'huge', 'kind': 'automated', 'description': 'd', 'runner': {'type': 'huge-check'}})
        started = time.monotonic()
        self.record('huge', registry=registry)
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(self.events()[-1]['data']['evidence'], '')

    def test_a_log_with_an_earlier_bad_line_still_takes_the_events_and_keeps_the_bad_line(self):
        folder = self.data / '_events'
        folder.mkdir(mode=0o700)
        (folder / 'events.jsonl').write_bytes(b'this is not json\n')
        outcome, report = self.record('has-readme')
        self.assertEqual(report.written, True)
        self.assertTrue(self.log_bytes().startswith(b'this is not json\n'))
        self.assertEqual(self.log.read()['total'], 3)

    def test_two_runs_at_once_write_four_valid_events(self):
        errors = []

        def work():
            try:
                self.record('has-readme', log=EventLog(self.data))
            except Exception as error:  # noqa: BLE001
                errors.append(error)

        threads = [threading.Thread(target=work) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        events = self.events()
        self.assertEqual(len(events), 4)
        self.assertEqual(len({event['eventId'] for event in events}), 4)

    def test_the_result_is_the_one_the_runner_gave(self):
        outcome, _ = self.record('has-readme')
        self.assertEqual(outcome, check_runner.run(self.check('has-readme'), self.work))


class NoRunNoEventsTests(EventsCase):
    def assertNothingWritten(self, outcome_report, code):
        outcome, report = outcome_report
        self.assertEqual(report, check_events.EventsReport(False, code))
        self.assertFalse((self.data / '_events').exists())
        return outcome

    def test_a_self_reported_check_writes_nothing_and_says_so(self):
        outcome = self.assertNothingWritten(self.record('self'), 'not_run')
        self.assertEqual(outcome['status'], 'skipped')

    def test_a_check_with_no_adapter_writes_nothing(self):
        self.document['checks'].append({'id': 'odd', 'kind': 'automated', 'description': 'd', 'runner': {'type': 'quantum-check'}})
        outcome = self.assertNothingWritten(self.record('odd'), 'not_run')
        self.assertEqual(outcome['evidence']['reason'], 'unknown_runner_type')

    def test_a_workspace_that_is_not_a_folder_writes_nothing(self):
        outcome, report = check_events.run_and_record(self.check('has-readme'), self.work / 'nope', log=self.log, session_id=SESSION, module=self.document)
        self.assertEqual((outcome['evidence']['reason'], report), ('workspace_unavailable', check_events.EventsReport(False, 'not_run')))
        self.assertFalse((self.data / '_events').exists())

    def test_a_bad_check_shape_writes_nothing(self):
        for check in (None, {}, {'kind': 'automated'}):
            with self.subTest(check=check):
                outcome, report = check_events.run_and_record(check, self.work, log=self.log, session_id=SESSION, module=self.document)
                self.assertEqual(report, check_events.EventsReport(False, 'not_run'))
        self.assertFalse((self.data / '_events').exists())

    def test_the_result_is_returned_in_each_case(self):
        outcome, _ = self.record('self')
        self.assertEqual(outcome['evidence']['reason'], 'self_reported')


class TaskTests(EventsCase):
    def test_a_module_with_one_task_uses_it(self):
        self.record('has-readme')
        self.assertEqual({event['taskId'] for event in self.events()}, {'task-1'})

    def test_a_named_task_of_the_module_is_used(self):
        self.import_module(dict(module_document(tasks=['task-a', 'task-b']), version='1.1.0'))
        self.record('has-readme', task_id='task-b')
        self.assertEqual({event['taskId'] for event in self.events()}, {'task-b'})

    def test_with_several_tasks_and_no_choice_nothing_is_written_and_the_reason_is_named(self):
        self.import_module(dict(module_document(tasks=['task-a', 'task-b']), version='1.1.0'))
        outcome, report = self.record('has-readme')
        self.assertEqual(outcome['status'], 'passed')
        self.assertEqual(report, check_events.EventsReport(False, 'task_required'))
        self.assertFalse((self.data / '_events').exists())

    def test_a_task_that_is_not_in_the_module_is_refused_and_nothing_is_written(self):
        outcome, report = self.record('has-readme', task_id='no-such-task')
        self.assertEqual(outcome['status'], 'passed')
        self.assertEqual(report, check_events.EventsReport(False, 'task_unknown'))
        self.assertFalse((self.data / '_events').exists())

    def test_a_task_id_is_never_made_up(self):
        for task in ('', 'Task 1', 5, ['task-1'], 'task-1\n', 'TASK-1', 'x' * 100_000):
            with self.subTest(task=str(task)[:20]):
                self.assertEqual(self.record('has-readme', task_id=task)[1], check_events.EventsReport(False, 'task_unknown'))
        self.assertFalse((self.data / '_events').exists())

    def test_the_task_helper_gives_the_same_answers(self):
        document = {'tasks': [{'id': 'a'}, {'id': 'b'}]}
        self.assertEqual(check_events.resolve_task(document, 'b'), ('b', None))
        self.assertEqual(check_events.resolve_task(document, None), (None, 'task_required'))
        self.assertEqual(check_events.resolve_task(document, 'c'), (None, 'task_unknown'))
        self.assertEqual(check_events.resolve_task({'tasks': [{'id': 'a'}]}, None), ('a', None))
        self.assertEqual(check_events.resolve_task({'tasks': []}, None), (None, 'task_required'))
        self.assertEqual(check_events.resolve_task({}, None), (None, 'task_required'))
        self.assertEqual(check_events.resolve_task({'tasks': 'x'}, None), (None, 'task_required'))
        self.assertEqual(check_events.resolve_task({'tasks': [5, {'id': 5}, {'id': 'a'}]}, None), ('a', None))


class FailureTests(EventsCase):
    """A log that cannot be written never fails or hides the check result. The report names the code."""

    def assertResultKept(self, check_id='has-readme', **kwargs):
        outcome, report = self.record(check_id, **kwargs)
        self.assertEqual(outcome['status'], 'passed')
        return report

    def fill_log(self, free):
        """Make the log leave `free` bytes before the 5 MB cap."""
        folder = self.data / '_events'
        folder.mkdir(mode=0o700, exist_ok=True)
        size = learning_event.MAX_BYTES - free
        (folder / 'events.jsonl').write_bytes(b'x' * (size - 1) + b'\n')
        os.chmod(folder / 'events.jsonl', 0o600)

    def test_a_full_log_is_log_full_and_the_result_is_kept(self):
        self.fill_log(10)
        report = self.assertResultKept()
        self.assertEqual(report, check_events.EventsReport(False, 'log_full'))
        self.assertEqual(self.log_bytes().count(b'attempt_submitted'), 0)

    def test_a_log_that_is_nearly_full_still_takes_app_events(self):
        """The 80 percent reserve is for the page. The app may use the rest."""
        folder = self.data / '_events'
        folder.mkdir(mode=0o700)
        (folder / 'events.jsonl').write_bytes(b'x' * (event_reserve() + 1000) + b'\n')
        self.assertEqual(self.assertResultKept().written, True)

    def test_a_log_with_room_for_one_event_gives_partial(self):
        probe = tempfile.mkdtemp(dir=self.temp.name)
        self.log = EventLog(Path(probe))
        self.record('has-readme', sources=FixedSources().build())
        lines = (Path(probe) / '_events' / 'events.jsonl').read_bytes().splitlines(keepends=True)
        self.assertEqual(len(lines), 2)
        shutil.rmtree(probe)
        self.log = EventLog(self.data)
        self.fill_log(len(lines[0]) + len(lines[1]) - 1)
        report = self.assertResultKept(sources=FixedSources().build())
        self.assertEqual(report, check_events.EventsReport('partial', 'log_full'))
        raw = self.log_bytes()
        self.assertEqual(raw.count(b'"attempt_submitted"'), 1)
        self.assertEqual(raw.count(b'"check_result"'), 0)

    @unittest.skipIf(hasattr(os, 'geteuid') and os.geteuid() == 0, 'root can read any file')
    def test_a_log_that_cannot_be_read_is_log_unavailable(self):
        self.log.append(self.sample_event())
        path = self.data / '_events' / 'events.jsonl'
        os.chmod(path, 0)
        self.addCleanup(os.chmod, path, 0o600)
        self.assertEqual(self.assertResultKept(log=EventLog(self.data)), check_events.EventsReport(False, 'log_unavailable'))

    @unittest.skipIf(hasattr(os, 'geteuid') and os.geteuid() == 0, 'root can write anywhere')
    def test_a_folder_that_cannot_be_written_is_log_unavailable(self):
        os.chmod(self.data, 0o500)
        self.addCleanup(os.chmod, self.data, 0o700)
        self.assertEqual(self.assertResultKept(), check_events.EventsReport(False, 'log_unavailable'))

    @unittest.skipIf(hasattr(os, 'geteuid') and os.geteuid() == 0, 'root can write anywhere')
    def test_an_events_folder_that_is_read_only_is_log_unavailable(self):
        self.log.append(self.sample_event())
        folder = self.data / '_events'
        os.chmod(folder, 0o500)
        self.addCleanup(os.chmod, folder, 0o700)
        os.chmod(folder / 'events.jsonl', 0o400)
        self.assertEqual(self.assertResultKept(log=EventLog(self.data)), check_events.EventsReport(False, 'log_unavailable'))

    def test_a_log_that_is_a_folder_is_log_damaged(self):
        (self.data / '_events').mkdir(mode=0o700)
        (self.data / '_events' / 'events.jsonl').mkdir()
        self.assertEqual(self.assertResultKept(), check_events.EventsReport(False, 'log_damaged'))

    def test_an_events_folder_that_is_a_file_is_log_damaged(self):
        (self.data / '_events').write_text('not a folder')
        self.assertEqual(self.assertResultKept(), check_events.EventsReport(False, 'log_damaged'))
        self.assertEqual((self.data / '_events').read_text(), 'not a folder')

    def test_a_log_that_is_a_link_is_log_unavailable(self):
        folder = self.data / '_events'
        folder.mkdir(mode=0o700)
        target = self.data / 'elsewhere.jsonl'
        target.write_text('')
        (folder / 'events.jsonl').symlink_to(target)
        self.assertEqual(self.assertResultKept(), check_events.EventsReport(False, 'log_unavailable'))
        self.assertEqual(target.read_text(), '')

    def test_a_lock_that_stays_busy_is_log_unavailable(self):
        log = EventLog(self.data, lock_timeout=0.05)
        with mock.patch.object(log, '_take', side_effect=EventLogError('busy', 'lock_unavailable', 503, retryable=True)):
            self.assertEqual(self.assertResultKept(log=log), check_events.EventsReport(False, 'log_unavailable'))

    def test_every_store_code_maps_to_a_stable_code(self):
        table = {'log_full': 'log_full', 'log_reserved': 'log_full', 'log_corrupt': 'log_damaged', 'log_unreadable': 'log_unavailable',
                 'lock_unavailable': 'log_unavailable', 'write_failed': 'log_unavailable', 'fsync_failed': 'log_unavailable',
                 'unsafe_path': 'log_unavailable', 'invalid_event': 'event_invalid', 'event_too_large': 'event_invalid',
                 'unsupported_version': 'event_invalid', 'duplicate_event_id': 'events_failed', 'something_new': 'events_failed', None: 'events_failed'}
        for store_code, ours in table.items():
            with self.subTest(store=store_code):
                self.assertEqual(check_events.error_code(EventLogError('m', store_code)), ours)
        self.assertEqual(check_events.error_code(RuntimeError('boom')), 'events_failed')
        self.assertEqual(set(table.values()) <= check_events.ERROR_CODES, True)

    def test_a_store_that_fails_after_the_first_event_gives_partial(self):
        calls = []

        class Flaky:
            def append(self, event):
                calls.append(event['type'])
                if len(calls) == 2:
                    raise EventLogError('x', 'write_failed')
                return {'appended': True}

        report = self.assertResultKept(log=Flaky())
        self.assertEqual((calls, report), (['attempt_submitted', 'check_result'], check_events.EventsReport('partial', 'log_unavailable')))

    def test_a_store_that_raises_something_unexpected_is_events_failed(self):
        class Broken:
            def append(self, event):
                raise RuntimeError('/Users/someone/secret exploded')

        report = self.assertResultKept(log=Broken())
        self.assertEqual(report, check_events.EventsReport(False, 'events_failed'))

    def test_an_unexpected_fault_after_the_first_event_is_partial(self):
        calls = []

        class Broken:
            def append(self, event):
                calls.append(1)
                if len(calls) == 2:
                    raise ValueError('x')

        self.assertEqual(self.assertResultKept(log=Broken()), check_events.EventsReport('partial', 'events_failed'))

    def test_the_report_never_carries_the_text_of_an_error(self):
        class Broken:
            def append(self, event):
                raise EventLogError('/Users/someone/secret path', 'write_failed', details={'path': '/Users/x'})

        report = self.assertResultKept(log=Broken())
        self.assertNotIn('Users', json.dumps(report.fields()))

    def test_an_event_that_fails_the_contract_is_refused_before_any_write(self):
        bad = dict(self.document, id='Bad Module!')
        outcome, report = check_events.run_and_record(self.check('has-readme'), self.work, log=self.log, session_id=SESSION, module=bad)
        self.assertEqual(report, check_events.EventsReport(False, 'event_invalid'))
        self.assertFalse((self.data / '_events').exists())

    def test_a_result_that_fails_the_contract_stops_both_events(self):
        """The attempt is valid and the result is not. Neither one is written."""
        trace = check_runner.RunTrace({'status': 'maybe', 'evidence': {}, 'log': []}, True)
        with mock.patch.object(check_runner, 'run_traced', return_value=trace):
            outcome, report = self.record('has-readme')
        self.assertEqual(outcome['status'], 'maybe')
        self.assertEqual(report, check_events.EventsReport(False, 'event_invalid'))
        self.assertFalse((self.data / '_events').exists())

    def test_a_bad_session_id_is_refused_before_any_write(self):
        for session in ('', 'not-a-uuid', SESSION.upper(), None, 5):
            with self.subTest(session=session):
                self.assertEqual(self.record('has-readme', session_id=session)[1], check_events.EventsReport(False, 'event_invalid'))
        self.assertFalse((self.data / '_events').exists())

    def test_an_artifact_fault_is_reported_and_writes_nothing(self):
        with mock.patch.object(check_events, 'artifact_for', side_effect=check_events.ArtifactError('artifact_unavailable')):
            self.assertEqual(self.assertResultKept(), check_events.EventsReport(False, 'artifact_unavailable'))
        with mock.patch.object(check_events, 'artifact_for', side_effect=RuntimeError('boom')):
            self.assertEqual(self.assertResultKept(), check_events.EventsReport(False, 'events_failed'))
        self.assertFalse((self.data / '_events').exists())

    def test_a_clock_that_fails_is_reported_and_the_result_is_kept(self):
        def broken():
            raise OverflowError('bad time')

        sources = check_events.Sources(now=lambda: 1.0, monotonic=FixedSources().build().monotonic)
        sources2 = check_events.Sources(now=broken)
        outcome, report = self.record('has-readme', sources=sources2)
        self.assertEqual(outcome['status'], 'passed')
        self.assertEqual((report.written, report.error), (False, 'events_failed'))
        outcome, report = self.record('has-readme', sources=check_events.Sources(now=lambda: 1e30, monotonic=sources.monotonic))
        self.assertEqual((outcome['status'], report.written, report.error), ('passed', False, 'events_failed'))

    def test_a_keyboard_interrupt_is_not_swallowed(self):
        class Stop:
            def append(self, event):
                raise KeyboardInterrupt

        with self.assertRaises(KeyboardInterrupt):
            self.record('has-readme', log=Stop())

    def sample_event(self):
        return {'schemaVersion': '1.0', 'eventId': str(uuid.uuid4()), 'time': '2026-01-01T00:00:00Z', 'sessionId': SESSION, 'moduleId': 'hello-module',
                'moduleVersion': '1.0.0', 'taskId': 'task-1', 'source': 'student', 'skills': [], 'type': 'task_opened', 'data': {}}


def event_reserve():
    import event_log
    return event_log.RESERVE_BYTES


class ReportTests(unittest.TestCase):
    def test_the_fields_of_each_report(self):
        self.assertEqual(check_events.EventsReport(True, None).fields(), {'eventsWritten': True})
        self.assertEqual(check_events.EventsReport(False, 'log_full').fields(), {'eventsWritten': False, 'eventsError': 'log_full'})
        self.assertEqual(check_events.EventsReport('partial', 'log_full').fields(), {'eventsWritten': 'partial', 'eventsError': 'log_full'})

    def test_a_report_is_frozen(self):
        report = check_events.EventsReport(True, None)
        with self.assertRaises(Exception):
            report.written = False

    def test_the_fields_are_exact_types(self):
        self.assertIs(check_events.EventsReport(True, None).fields()['eventsWritten'], True)
        self.assertIs(check_events.EventsReport(False, 'x').fields()['eventsWritten'], False)

    def test_every_error_code_is_a_short_lowercase_word(self):
        for code in check_events.ERROR_CODES:
            self.assertRegex(code, r'^[a-z_]{1,40}$')


class SourcesTests(unittest.TestCase):
    def test_the_defaults_make_valid_ids(self):
        sources = check_events.Sources()
        self.assertRegex(sources.event_id(), UUID)
        self.assertRegex(sources.attempt_id(), ATTEMPT)
        self.assertNotEqual(sources.attempt_id(), sources.attempt_id())
        self.assertNotEqual(sources.event_id(), sources.event_id())

    def test_sources_are_frozen(self):
        with self.assertRaises(Exception):
            check_events.Sources().now = None


if __name__ == '__main__':
    unittest.main()
