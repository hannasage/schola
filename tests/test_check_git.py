"""The git-commit-exists check, against repositories that try to run programs.

Each hostile repository is built at test time in a temporary folder. A marker
script writes a file when git runs it. Every hostile test also runs a control:
a git command that is known to run the program. The control proves that the
repository really is hostile, so an empty marker folder means something.
"""
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import check_git
import check_runner
from check_runner import Limits, run

GIT = shutil.which('git')
NEEDS_GIT = unittest.skipUnless(GIT, 'git is not installed')
CLEAN_ENV = {'PATH': '/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin', 'LC_ALL': 'C', 'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': os.devnull,
             'GIT_AUTHOR_NAME': 'Test', 'GIT_AUTHOR_EMAIL': 'test@example.com', 'GIT_COMMITTER_NAME': 'Test', 'GIT_COMMITTER_EMAIL': 'test@example.com',
             'GIT_TERMINAL_PROMPT': '0'}
HOOKS = ['applypatch-msg', 'pre-applypatch', 'post-applypatch', 'pre-commit', 'pre-merge-commit', 'prepare-commit-msg', 'commit-msg', 'post-commit',
         'pre-rebase', 'post-checkout', 'post-merge', 'pre-push', 'pre-receive', 'update', 'post-receive', 'post-update', 'reference-transaction',
         'push-to-checkout', 'pre-auto-gc', 'post-rewrite', 'post-index-change', 'fsmonitor-watchman', 'proc-receive']


def automated(runner):
    return {'id': 'c1', 'kind': 'automated', 'description': 'A check.', 'runner': runner}


def git(repo, *args, env=None, input=None, check=True):
    process = subprocess.run([GIT, *args], cwd=repo, env=env or CLEAN_ENV, capture_output=True, input=input, timeout=30)
    if check and process.returncode != 0:
        raise AssertionError(f'git {args} failed: {process.stderr.decode(errors="replace")}')
    return process


@NEEDS_GIT
class GitCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(os.path.realpath(self.temp.name))
        self.work = self.base / 'work'
        self.work.mkdir()
        self.markers = self.base / 'markers'
        self.markers.mkdir()
        self.outside = self.base / 'outside'
        self.outside.mkdir()

    # -- building repositories ------------------------------------------------

    def init(self, folder=None, *extra):
        folder = folder or self.work
        folder.mkdir(exist_ok=True)
        git(folder, 'init', '-q', '-b', 'main', *extra)
        return folder

    def commit(self, message, folder=None, name='f.txt', content=None):
        folder = folder or self.work
        (folder / name).write_text(content if content is not None else f'{message}\n{time.time_ns()}\n')
        git(folder, 'add', name)
        git(folder, 'commit', '-q', '-m', message)

    def repo_with(self, *messages):
        self.init()
        for message in messages:
            self.commit(message)
        return self.work

    def check(self, runner_fields=None, limits=None, workspace=None):
        runner = {'type': 'git-commit-exists', **(runner_fields or {})}
        return run(automated(runner), workspace or self.work, limits)

    # -- hostile helpers -----------------------------------------------------

    def script(self, name):
        path = self.base / f'{name}.sh'
        path.write_text(f'#!/bin/sh\necho "$0 $@" >> "{self.markers}/{name}"\nexit 0\n')
        path.chmod(0o755)
        return str(path)

    def fired(self, clear=True):
        found = sorted(p.name for p in self.markers.iterdir())
        if clear:
            for p in self.markers.iterdir():
                p.unlink()
        return found

    def hostile_config(self, repo=None):
        repo = repo or self.work
        hooks = self.base / 'hooks'
        hooks.mkdir(exist_ok=True)
        for name in HOOKS:
            hook = hooks / name
            hook.write_text(f'#!/bin/sh\necho x >> "{self.markers}/hook-{name}"\nexit 0\n')
            hook.chmod(0o755)
            local = repo / '.git' / 'hooks' / name
            local.parent.mkdir(exist_ok=True)
            local.write_text(f'#!/bin/sh\necho x >> "{self.markers}/localhook-{name}"\nexit 0\n')
            local.chmod(0o755)
        (self.base / 'extra.cfg').write_text(f'[core]\n\tfsmonitor = {self.script("include-fsmonitor")}\n\teditor = {self.script("include-editor")}\n')
        (repo / '.gitattributes').write_text('*.txt diff=evil filter=evil\n')
        config = repo / '.git' / 'config'
        config.write_text(config.read_text() + f'''
[core]
	fsmonitor = {self.script("fsmonitor")}
	hooksPath = {hooks}
	sshCommand = {self.script("ssh")}
	pager = {self.script("pager")}
	editor = {self.script("editor")}
	askPass = {self.script("askpass")}
	alternateRefsCommand = {self.script("altrefs")}
	attributesFile = {self.base / "attributes"}
[diff]
	external = {self.script("diffext")}
[diff "evil"]
	textconv = {self.script("textconv")}
[credential]
	helper = !{self.script("credential")}
[gpg]
	program = {self.script("gpg")}
[uploadpack]
	packObjectsHook = {self.script("packhook")}
[include]
	path = {self.base / "extra.cfg"}
''')

    def controls(self, repo=None):
        """Commands known to run the hostile programs. They prove the fixture bites."""
        repo = repo or self.work
        (repo / 'f.txt').write_text('dirty\n')
        git(repo, 'status', '--porcelain', check=False)
        fired = self.fired()
        # The [include] file sets core.fsmonitor after the repository's own line, so it is the one that wins.
        self.assertTrue(any('fsmonitor' in name for name in fired), f'git status should run core.fsmonitor in this repository: {fired}')
        git(repo, 'diff', check=False)
        self.assertIn('diffext', self.fired())
        git(repo, 'log', '-p', check=False)
        self.assertIn('textconv', self.fired())
        git(repo, 'commit', '--allow-empty', '-qm', 'control', check=False)
        fired = self.fired()
        self.assertTrue(any(name.startswith('hook-') for name in fired), fired)
        git(repo, 'ls-remote', 'ssh://example.invalid/x', check=False)
        self.assertIn('ssh', self.fired())
        git(repo, 'checkout', '-q', '--', 'f.txt', check=False)
        self.fired()


class BasicTests(GitCase):
    def test_a_repository_with_commits_passes(self):
        self.repo_with('first', 'second', 'third')
        result = self.check()
        self.assertEqual(result['status'], 'passed')
        self.assertEqual(result['evidence']['commits'], 3)
        self.assertRegex(result['evidence']['head'], r'^[0-9a-f]{7}$')

    def test_min_commits(self):
        self.repo_with('a', 'b', 'c')
        self.assertEqual(self.check({'minCommits': 3})['status'], 'passed')
        result = self.check({'minCommits': 4})
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['evidence']['commits'], 3)
        self.assertEqual(result['evidence']['required'], 4)

    def test_message_contains(self):
        self.repo_with('Add the parser', 'Fix a bug in the lexer', 'Add tests for the parser')
        self.assertEqual(self.check({'messageContains': 'parser'})['status'], 'passed')
        self.assertEqual(self.check({'messageContains': 'parser', 'minCommits': 2})['status'], 'passed')
        result = self.check({'messageContains': 'parser', 'minCommits': 3})
        self.assertEqual((result['status'], result['evidence']['matching']), ('failed', 2))
        self.assertEqual(self.check({'messageContains': 'nothing like this'})['status'], 'failed')
        self.assertEqual(self.check({'messageContains': 'ADD'})['status'], 'failed')
        self.assertEqual(self.check({'messageContains': 'ADD', 'ignoreCase': True, 'minCommits': 2})['status'], 'passed')

    def test_the_message_is_a_literal_and_the_body_counts(self):
        self.init()
        self.commit('subject line\n\nThe body has a.c and (x+) and $(touch pwned) in it')
        for text in ('a.c', '(x+)', '$(touch pwned)', 'subject line', 'body has'):
            self.assertEqual(self.check({'messageContains': text})['status'], 'passed', text)
        self.assertEqual(self.check({'messageContains': 'a+c'})['status'], 'failed')
        self.assertFalse((self.work / 'pwned').exists())

    def test_unicode_emoji_and_shell_characters_in_messages(self):
        self.init()
        self.commit('café \U0001f600 `touch x` "quoted" \'single\' ; echo hi | cat > y')
        self.assertEqual(self.check({'messageContains': '\U0001f600'})['status'], 'passed')
        self.assertEqual(self.check({'messageContains': 'café'})['status'], 'passed')
        self.assertEqual(self.check({'messageContains': '`touch x`'})['status'], 'passed')
        self.assertEqual(self.check({'messageContains': '; echo hi | cat > y'})['status'], 'passed')
        self.assertEqual(sorted(p.name for p in self.work.iterdir() if not p.name.startswith('.')), ['f.txt'])

    def test_a_message_that_looks_like_git_output_does_not_confuse_the_parser(self):
        self.init()
        self.commit('x\n\ncommit 0000000000000000000000000000000000000000 commit 99\n\nfake commit text')
        result = self.check({'messageContains': 'fake commit text', 'minCommits': 1})
        self.assertEqual(result['status'], 'passed')
        self.assertEqual(self.check({'messageContains': 'x', 'minCommits': 2})['status'], 'failed')

    def test_an_empty_repository_fails_and_a_folder_without_one_fails(self):
        self.init()
        result = self.check()
        self.assertEqual((result['status'], result['evidence']['reason']), ('failed', 'no_commits'))
        other = self.base / 'plain'
        other.mkdir()
        result = self.check(workspace=other)
        self.assertEqual((result['status'], result['evidence']['reason']), ('failed', 'no_repository'))

    def test_a_detached_head_and_other_branches_count_from_head(self):
        self.repo_with('one', 'two')
        git(self.work, 'checkout', '-q', '--detach', 'HEAD~1')
        self.assertEqual(self.check()['evidence']['commits'], 1)

    def test_a_sha256_repository_works(self):
        process = subprocess.run([GIT, 'init', '-q', '--object-format=sha256', str(self.work)], capture_output=True, env=CLEAN_ENV)
        if process.returncode != 0:
            self.skipTest('this git cannot make a sha256 repository')
        self.commit('sha256 commit')
        self.assertEqual(self.check({'messageContains': 'sha256'})['status'], 'passed')

    def test_descriptor_mistakes_are_errors(self):
        self.repo_with('a')
        for fields in ({'messageContains': ''}, {'messageContains': 5}, {'messageContains': 'x' * 501}, {'minCommits': 0}, {'minCommits': 1001},
                       {'minCommits': True}, {'minCommits': '2'}, {'extra': 'x'}, {'ignoreCase': 'yes'}, {'branch': 'main'}):
            with self.subTest(fields=fields):
                result = self.check(fields)
                self.assertEqual((result['status'], result['evidence']['reason']), ('error', 'invalid_descriptor'))

    def test_a_huge_history_is_counted_and_a_huge_message_is_capped(self):
        self.init()
        git(self.work, 'commit', '-q', '--allow-empty', '-m', 'one')
        git(self.work, 'commit', '-q', '--allow-empty', '-F', '-', input=b'y' * 3_000_000)
        result = self.check({'messageContains': 'one'}, Limits(git_output_bytes=100_000))
        self.assertEqual((result['status'], result['evidence']['reason']), ('error', 'output_too_large'))
        self.assertEqual(self.check({'minCommits': 2})['status'], 'passed')

    def test_only_the_newest_commits_are_scanned_for_a_message(self):
        self.init()
        git(self.work, 'commit', '-q', '--allow-empty', '-m', 'oldest marker text')
        for index in range(6):
            git(self.work, 'commit', '-q', '--allow-empty', '-m', f'filler {index}')
        result = self.check({'messageContains': 'marker text'}, Limits(git_max_scan=3))
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['evidence']['scanned'], 3)
        self.assertEqual(self.check({'messageContains': 'marker text'})['status'], 'passed')

    def test_git_missing_is_skipped_for_self_report(self):
        self.repo_with('a')
        with mock.patch.object(check_git.shutil, 'which', return_value=None):
            result = self.check()
        self.assertEqual((result['status'], result['evidence']['reason']), ('skipped', 'git_unavailable'))
        self.assertIn('self-report', ' '.join(result['log']).lower())

    def test_the_run_changes_nothing_in_the_workspace(self):
        self.repo_with('a', 'b')
        before = subprocess.run(['find', str(self.work), '-type', 'f', '-exec', 'shasum', '{}', '+'], capture_output=True, text=True).stdout
        self.check({'messageContains': 'a'})
        self.check()
        after = subprocess.run(['find', str(self.work), '-type', 'f', '-exec', 'shasum', '{}', '+'], capture_output=True, text=True).stdout
        self.assertEqual(sorted(before.splitlines()), sorted(after.splitlines()))
        self.assertFalse((self.work / '.git' / 'index.lock').exists())

    def test_the_evidence_never_holds_a_message_or_a_path(self):
        self.repo_with('very secret message text')
        text = json.dumps(self.check({'messageContains': 'secret'}))
        self.assertNotIn('very secret message text', text)
        self.assertNotIn(str(self.work), text)


class CommandShapeTests(GitCase):
    def spy(self, **runner):
        seen = []
        real = check_git.subprocess.Popen

        def watcher(command, *args, **kwargs):
            seen.append((command, kwargs))
            return real(command, *args, **kwargs)

        with mock.patch.object(check_git.subprocess, 'Popen', watcher):
            self.check(runner)
        return seen

    def test_git_runs_as_an_argument_list_with_no_shell_and_a_clean_environment(self):
        self.repo_with('a')
        with mock.patch.dict(os.environ, {'GIT_DIR': '/elsewhere', 'GIT_WORK_TREE': '/elsewhere', 'GIT_SSH_COMMAND': 'evil', 'GIT_EXTERNAL_DIFF': 'evil',
                                            'GIT_CONFIG_COUNT': '1', 'GIT_CONFIG_KEY_0': 'core.fsmonitor', 'GIT_CONFIG_VALUE_0': 'evil', 'GIT_PAGER': 'evil',
                                            'GIT_EDITOR': 'evil', 'GIT_ASKPASS': 'evil', 'SSH_ASKPASS': 'evil', 'GIT_EXEC_PATH': '/elsewhere', 'SECRET_TOKEN': 'x',
                                            'GIT_TRACE': '1', 'PAGER': 'evil', 'EDITOR': 'evil'}):
            seen = self.spy(messageContains='a')
        self.assertGreaterEqual(len(seen), 3)
        for command, kwargs in seen:
            self.assertIsInstance(command, list)
            self.assertIs(kwargs.get('shell', False), False)
            self.assertEqual(kwargs['cwd'], str(self.work))
            self.assertTrue(kwargs['start_new_session'])
            env = kwargs['env']
            self.assertEqual(env['GIT_CONFIG_NOSYSTEM'], '1')
            self.assertEqual(env['GIT_CONFIG_GLOBAL'], os.devnull)
            self.assertEqual(env['GIT_TERMINAL_PROMPT'], '0')
            self.assertEqual(env['GIT_OPTIONAL_LOCKS'], '0')
            self.assertEqual(env['GIT_PAGER'], 'cat')
            allowed = {'PATH', 'HOME', 'XDG_CONFIG_HOME', 'LANG', 'LC_ALL', 'GIT_CONFIG_NOSYSTEM', 'GIT_CONFIG_GLOBAL', 'GIT_CONFIG_SYSTEM',
                       'GIT_TERMINAL_PROMPT', 'GIT_OPTIONAL_LOCKS', 'GIT_PAGER', 'GIT_ATTR_NOSYSTEM', 'GIT_NO_REPLACE_OBJECTS',
                       'GIT_NO_LAZY_FETCH', 'GIT_ALLOW_PROTOCOL'}
            self.assertEqual(env['GIT_NO_LAZY_FETCH'], '1')
            self.assertEqual(env['GIT_ALLOW_PROTOCOL'], 'none')
            self.assertLessEqual(set(env), allowed)
            self.assertNotIn(str(Path.home()), json.dumps(env))

    def test_only_read_commands_run_with_the_fixed_overrides(self):
        self.repo_with('a')
        seen = self.spy(messageContains='a')
        verbs = []
        for command, _ in seen:
            self.assertIn('--no-pager', command)
            self.assertEqual(command[command.index('--git-dir') + 1], str(self.work / '.git'))
            for key, value in check_git.FIXED_CONFIG:
                self.assertIn(f'{key}={value}', command)
            after = command[command.index('--git-dir') + 2:]
            verbs.append(next(item for item in after if not item.startswith('-') and '=' not in item))
        self.assertLessEqual(set(verbs), {'rev-parse', 'rev-list', 'cat-file', 'show-ref'})
        # A diff driver (textconv) is named by .gitattributes, so no fixed override can switch it off. The defence is
        # to never ask git for a patch, a filtered blob, or a checkout.
        for command, _ in seen:
            self.assertFalse({'-p', '--patch', '--textconv', '--filters', '--ext-diff', '--show-signature', '--follow'} & set(command))
        self.assertEqual(self.check()['status'], 'passed')

    def test_the_fixed_overrides_cover_every_program_a_config_can_name(self):
        keys = {key for key, _ in check_git.FIXED_CONFIG}
        for key in ('core.fsmonitor', 'core.hooksPath', 'core.sshCommand', 'core.pager', 'core.editor', 'core.askPass', 'core.alternateRefsCommand',
                    'core.attributesFile', 'diff.external', 'credential.helper', 'gpg.program', 'uploadpack.packObjectsHook', 'gc.auto'):
            self.assertIn(key, keys)
        values = dict(check_git.FIXED_CONFIG)
        self.assertEqual(values['core.fsmonitor'], 'false')
        self.assertEqual(values['core.hooksPath'], os.devnull)
        self.assertEqual(values['core.pager'], 'cat')
        self.assertEqual(values['core.sshCommand'], '')
        self.assertEqual(values['diff.external'], '')

    def test_no_safe_directory_override_is_ever_passed(self):
        self.repo_with('a')
        for command, kwargs in self.spy():
            self.assertFalse(any('safe.directory' in item for item in command))
            self.assertNotIn('GIT_CONFIG_COUNT', kwargs['env'])


class HostileRepositoryTests(GitCase):
    def setUp(self):
        super().setUp()
        # These tests prove the second layer: even if a hostile config got past the allow-list, the commands run nothing.
        patcher = mock.patch.object(check_git, 'check_config', lambda gitdir: None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_hostile_config_runs_nothing_for_our_commands(self):
        self.repo_with('first', 'second')
        self.hostile_config()
        self.controls()
        self.assertEqual(self.fired(), [])
        git(self.work, 'checkout', '-q', '--', '.', check=False)
        self.fired()
        result = self.check({'messageContains': 'first', 'minCommits': 1})
        self.assertEqual(result['status'], 'passed', result)
        self.assertEqual(self.check({'minCommits': 2})['status'], 'passed')
        self.assertEqual(self.fired(), [], 'git ran a program named in the repository')

    def test_a_hostile_config_with_a_dirty_tree_and_attributes_runs_nothing(self):
        self.repo_with('first')
        self.hostile_config()
        (self.work / 'f.txt').write_text('changed\n')
        (self.work / 'new.txt').write_text('untracked\n')
        self.controls()
        self.assertEqual(self.fired(), [])
        self.check({'messageContains': 'first'})
        self.assertEqual(self.fired(), [])

    def test_the_overrides_alone_are_not_what_keeps_the_commands_quiet(self):
        """Recorded finding. These three read commands run none of the programs even with no overrides.
        The overrides stay as a second layer in case a future git version reads more."""
        self.repo_with('first')
        self.hostile_config()
        gd = str(self.work / '.git')
        for args in (['rev-parse', '--verify', '-q', 'HEAD^{commit}'], ['rev-list', '--count', 'HEAD']):
            git(self.work, '--no-pager', '--git-dir', gd, *args, check=False)
        sha = git(self.work, 'rev-parse', 'HEAD').stdout
        git(self.work, '--no-pager', '--git-dir', gd, 'cat-file', '--batch', input=sha, check=False)
        self.assertEqual(self.fired(), [])

    def test_a_pager_in_the_config_is_not_started_even_on_a_terminal(self):
        self.repo_with('first')
        self.hostile_config()
        import pty
        master, slave = os.openpty()
        process = subprocess.Popen([GIT, 'log', '-1'], cwd=self.work, env=dict(CLEAN_ENV, TERM='xterm'), stdout=slave, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
        os.close(slave)
        process.wait(20)
        os.close(master)
        self.assertEqual(self.fired(), ['pager'], 'control: git log on a terminal should start the pager')
        del pty
        self.check({'messageContains': 'first'})
        self.assertEqual(self.fired(), [])

    def test_a_signed_looking_commit_does_not_start_gpg(self):
        self.init()
        (self.work / 'f.txt').write_text('x')
        git(self.work, 'add', 'f.txt')
        git(self.work, 'commit', '-q', '-m', 'signed', '--no-gpg-sign')
        head = git(self.work, 'cat-file', 'commit', 'HEAD').stdout
        body = head.replace(b'\n\nsigned', b'\ngpgsig -----BEGIN PGP SIGNATURE-----\n \n -----END PGP SIGNATURE-----\n\nsigned', 1)
        sha = git(self.work, 'hash-object', '-t', 'commit', '-w', '--stdin', input=body).stdout.strip().decode()
        git(self.work, 'update-ref', 'refs/heads/main', sha)
        self.hostile_config()
        git(self.work, 'log', '-1', '--show-signature', check=False)
        self.assertIn('gpg', self.fired(), 'control: --show-signature should run gpg.program')
        self.assertEqual(self.check({'messageContains': 'signed'})['status'], 'passed')
        self.assertEqual(self.fired(), [])

    def test_inherited_git_variables_do_not_reach_git(self):
        self.repo_with('first')
        evil = self.script('envhook')
        env = {'GIT_EXTERNAL_DIFF': evil, 'GIT_SSH_COMMAND': evil, 'GIT_PAGER': evil, 'GIT_EDITOR': evil, 'GIT_ASKPASS': evil, 'PAGER': evil,
               'GIT_CONFIG_COUNT': '2', 'GIT_CONFIG_KEY_0': 'core.fsmonitor', 'GIT_CONFIG_VALUE_0': evil, 'GIT_CONFIG_KEY_1': 'core.hooksPath',
               'GIT_CONFIG_VALUE_1': str(self.base), 'GIT_DIR': str(self.outside), 'GIT_WORK_TREE': str(self.outside), 'GIT_INDEX_FILE': str(self.outside / 'idx'),
               'GIT_OBJECT_DIRECTORY': str(self.outside), 'GIT_ALTERNATE_OBJECT_DIRECTORIES': str(self.outside), 'GIT_NAMESPACE': 'x', 'GIT_CEILING_DIRECTORIES': '/'}
        with mock.patch.dict(os.environ, env):
            result = self.check({'messageContains': 'first'})
        self.assertEqual(result['status'], 'passed', result)
        self.assertEqual(self.fired(), [])

    def test_a_gitfile_pointing_outside_is_rejected(self):
        outside_repo = self.init(self.outside / 'repo')
        self.commit('outside commit', outside_repo)
        self.hostile_config(outside_repo)
        shutil.rmtree(self.work)
        self.work.mkdir()
        for target in (str(outside_repo / '.git'), '../outside/repo/.git', '../../../../../../' + str(outside_repo / '.git').lstrip('/')):
            (self.work / '.git').write_text(f'gitdir: {target}\n')
            result = self.check({'messageContains': 'outside commit'})
            self.assertEqual((result['status'], result['evidence']['reason']), ('error', 'unsafe_git_dir'), target)
            self.assertEqual(self.fired(), [])
            self.assertNotIn('outside commit', json.dumps(result))

    def test_a_gitfile_that_points_inside_the_workspace_is_allowed(self):
        self.repo_with('inside')
        os.rename(self.work / '.git', self.work / 'realgit')
        (self.work / '.git').write_text('gitdir: realgit\n')
        self.assertEqual(self.check({'messageContains': 'inside'})['status'], 'passed')

    def test_bad_gitfiles_are_errors(self):
        for text in ('', 'gitdir:', 'gitdir: \n', 'not a gitfile', 'gitdir: a\x00b', 'gitdir: ' + 'x' * 5000, 'gitdir: missing-folder', 'GITDIR: x', '\n\ngitdir: x'):
            (self.work / '.git').write_text(text)
            with self.subTest(text=text[:30]):
                result = self.check()
                self.assertEqual(result['status'], 'error')
                self.assertEqual(result['evidence']['reason'], 'unsafe_git_dir')
            (self.work / '.git').unlink()

    def test_a_gitfile_that_points_at_a_symlink_to_outside_is_rejected(self):
        outside_repo = self.init(self.outside / 'repo')
        self.commit('outside commit', outside_repo)
        os.symlink(outside_repo / '.git', self.work / 'linked')
        (self.work / '.git').write_text('gitdir: linked\n')
        result = self.check({'messageContains': 'outside commit'})
        self.assertEqual((result['status'], result['evidence']['reason']), ('error', 'unsafe_git_dir'))

    def test_a_git_entry_that_is_a_symlink_is_rejected_even_inside(self):
        outside_repo = self.init(self.outside / 'repo')
        self.commit('outside commit', outside_repo)
        self.hostile_config(outside_repo)
        os.symlink(outside_repo / '.git', self.work / '.git')
        result = self.check({'messageContains': 'outside commit'})
        self.assertEqual((result['status'], result['evidence']['reason']), ('error', 'unsafe_git_dir'))
        self.assertEqual(self.fired(), [])
        os.unlink(self.work / '.git')
        inside = self.work / 'real'
        inside.mkdir()
        os.symlink('real', self.work / '.git')
        self.assertEqual(self.check()['evidence']['reason'], 'unsafe_git_dir')

    def test_a_workspace_inside_a_bigger_repository_does_not_find_it(self):
        parent = self.init(self.base / 'parent')
        self.commit('parent commit', parent)
        self.hostile_config(parent)
        inner = parent / 'inner'
        inner.mkdir()
        result = self.check(workspace=inner)
        self.assertEqual((result['status'], result['evidence']['reason']), ('failed', 'no_repository'))
        self.assertEqual(self.fired(), [])

    def test_git_entries_that_are_links_or_pipes_are_rejected(self):
        for name, make in (
            ('objects', lambda p: (shutil.rmtree(p), os.symlink(self.outside, p))),
            ('refs', lambda p: (shutil.rmtree(p), os.symlink(self.outside, p))),
            ('HEAD', lambda p: (p.unlink(), os.symlink(self.outside / 'HEAD', p))),
            ('config', lambda p: (p.unlink(), os.mkfifo(p))),
            ('HEAD', lambda p: (p.unlink(), os.mkfifo(p))),
            ('commondir', lambda p: p.write_text('../../../outside\n')),
            ('commondir', lambda p: p.write_text('.\n')),
        ):
            shutil.rmtree(self.work, ignore_errors=True)
            self.work.mkdir()
            self.repo_with('a')
            target = self.work / '.git' / name
            make(target)
            signal.alarm(20)
            try:
                started = time.monotonic()
                result = self.check()
                elapsed = time.monotonic() - started
            finally:
                signal.alarm(0)
            with self.subTest(name=name, kind=stat.filemode(os.lstat(target).st_mode)):
                self.assertEqual((result['status'], result['evidence']['reason']), ('error', 'unsafe_git_dir'))
                self.assertLess(elapsed, 5)

    def test_a_repository_that_borrows_objects_from_another_folder_is_rejected(self):
        self.repo_with('a')
        info = self.work / '.git' / 'objects' / 'info'
        info.mkdir(exist_ok=True)
        (info / 'alternates').write_text(str(self.outside / 'objects') + '\n')
        result = self.check()
        self.assertEqual((result['status'], result['evidence']['reason']), ('error', 'unsafe_git_dir'))

    def test_a_repository_owned_by_someone_else_is_not_trusted(self):
        self.repo_with('a')
        real_stat = os.lstat

        class Other:
            def __init__(self, info):
                self.info = info

            def __getattr__(self, name):
                return os.geteuid() + 1 if name == 'st_uid' else getattr(self.info, name)

        with mock.patch.object(check_git.os, 'lstat', lambda path, *a, **k: Other(real_stat(path, *a, **k))):
            result = self.check()
        self.assertEqual((result['status'], result['evidence']['reason']), ('error', 'untrusted_repository'))

    def test_git_itself_refusing_a_repository_is_an_error_not_a_pass(self):
        self.repo_with('a')
        config = self.work / '.git' / 'config'
        config.write_text(config.read_text() + '\n[extensions]\n\tfutureThing = true\n[core]\n\trepositoryformatversion = 1\n')
        result = self.check()
        self.assertEqual(result['status'], 'error')
        self.assertEqual(result['evidence']['reason'], 'git_failed')
        self.assertNotIn(str(self.work), json.dumps(result))

    def test_a_corrupt_repository_is_an_error_not_a_pass(self):
        self.repo_with('a')
        (self.work / '.git' / 'HEAD').write_text('garbage\n')
        result = self.check()
        self.assertIn(result['status'], ('error', 'failed'))
        self.assertNotEqual(result['status'], 'passed')


class ConfigAllowListTests(GitCase):
    """The repository config may hold only keys from a short list of plain-data settings."""

    def with_config(self, text):
        self.repo_with('first')
        config = self.work / '.git' / 'config'
        config.write_text(config.read_text() + '\n' + text)

    def test_a_plain_config_is_accepted(self):
        self.with_config('[user]\n\tname = A Student\n\temail = a@example.com\n[remote "origin"]\n\turl = https://example.com/x.git\n\tfetch = +refs/heads/*:refs/remotes/origin/*\n'
                         '[branch "main"]\n\tremote = origin\n\tmerge = refs/heads/main\n[pull]\n\trebase = false\n[color "diff"]\n\tnew = green\n# a comment\n; another\n')
        self.assertEqual(self.check()['status'], 'passed')

    def test_settings_that_name_a_program_or_pull_in_more_config_are_refused(self):
        cases = {
            'fsmonitor': '[core]\n\tfsmonitor = /bin/true\n', 'hooks': '[core]\n\thooksPath = /tmp\n', 'ssh': '[core]\n\tsshCommand = /bin/true\n',
            'pager': '[core]\n\tpager = /bin/true\n', 'editor': '[core]\n\teditor = /bin/true\n', 'worktree': '[core]\n\tworktree = /tmp\n',
            'diff external': '[diff]\n\texternal = /bin/true\n', 'diff driver': '[diff "x"]\n\ttextconv = /bin/true\n', 'filter': '[filter "x"]\n\tclean = /bin/true\n',
            'credential': '[credential]\n\thelper = !true\n', 'gpg': '[gpg]\n\tprogram = /bin/true\n', 'include': '[include]\n\tpath = /tmp/x\n',
            'includeIf': '[includeIf "gitdir:/"]\n\tpath = /tmp/x\n', 'extension': '[extensions]\n\tworktreeConfig = true\n',
            'partial clone': '[extensions]\n\tpartialClone = origin\n', 'promisor': '[remote "origin"]\n\tpromisor = true\n',
            'promisor mixed case': '[remote "origin"]\n\tProMiSor = true\n', 'partial filter': '[remote "origin"]\n\tpartialclonefilter = blob:none\n',
            'ext url': '[remote "origin"]\n\turl = ext::sh -c true\n', 'upload pack': '[remote "origin"]\n\tuploadpack = /bin/true\n',
            'same line': '[core] fsmonitor = /bin/true\n', 'unknown section': '[mystery]\n\tkey = value\n', 'alias': '[alias]\n\tco = !true\n',
            'uploadpack hook': '[uploadpack]\n\tpackObjectsHook = /bin/true\n', 'garbage line': '[core]\n\t= = =\n',
        }
        for label, text in cases.items():
            shutil.rmtree(self.work, ignore_errors=True)
            self.work.mkdir()
            self.with_config(text)
            with self.subTest(label=label):
                result = self.check()
                self.assertEqual((result['status'], result['evidence']['reason']), ('error', 'unsafe_git_dir'))
                self.assertEqual(self.fired(), [])

    def test_a_huge_config_is_refused(self):
        self.with_config('# ' + 'x' * 300000 + '\n')
        self.assertEqual(self.check()['evidence']['reason'], 'unsafe_git_dir')

    def test_a_promisor_pack_marker_is_refused(self):
        self.repo_with('first')
        pack = self.work / '.git' / 'objects' / 'pack'
        pack.mkdir(exist_ok=True)
        (pack / 'pack-1234.promisor').write_text('')
        self.assertEqual(self.check()['evidence']['reason'], 'unsafe_git_dir')


class PromisorTests(GitCase):
    """A partial clone fetches a missing object by running a transport. A hostile repository names a program there."""

    def build(self):
        self.repo_with('first', 'second', 'third')
        # Remove the oldest commit object, so git needs it and would ask the promisor remote for it.
        oldest = git(self.work, 'rev-list', '--max-parents=0', 'HEAD').stdout.decode().strip()
        wanted = git(self.work, 'rev-list', '--objects', 'HEAD', f'^{oldest}').stdout
        pack_dir = self.work / '.git' / 'objects' / 'pack'
        pack_dir.mkdir(exist_ok=True)
        # A pack of everything except the oldest commit, marked as promised by a remote. Then the oldest commit is gone.
        names = git(self.work, 'pack-objects', str(pack_dir / 'pack'), input=wanted).stdout.decode().strip()
        (pack_dir / f'pack-{names}.promisor').write_text('')
        git(self.work, 'prune-packed')
        for path in (self.work / '.git' / 'objects').glob(f'{oldest[:2]}/{oldest[2:]}'):
            path.unlink()
        self.assertTrue(list(pack_dir.glob('*.pack')), 'the fixture needs a pack')
        trap = self.script('transport')
        remote = self.init(self.outside / 'remote')
        self.commit('remote commit', remote)
        config = self.work / '.git' / 'config'
        config.write_text(config.read_text().replace('repositoryformatversion = 0', 'repositoryformatversion = 1') +
                          f'[extensions]\n\tpartialClone = origin\n[remote "origin"]\n\turl = file://{remote}\n\tuploadpack = {trap}\n\tpromisor = true\n\tpartialclonefilter = blob:none\n')
        return oldest

    def test_a_promisor_repository_is_refused_before_git_runs_and_nothing_is_started(self):
        self.build()
        result = self.check()
        self.assertEqual((result['status'], result['evidence']['reason']), ('error', 'unsafe_git_dir'))
        self.assertEqual(self.fired(), [])
        result = self.check({'messageContains': 'first'})
        self.assertEqual(result['evidence']['reason'], 'unsafe_git_dir')
        self.assertEqual(self.fired(), [])

    def test_the_environment_blocks_every_transport_even_if_the_config_check_is_skipped(self):
        oldest = self.build()
        # Control: with the protections off, git does start the transport program for the missing commit.
        git(self.work, '--no-pager', 'rev-list', '--count', 'HEAD', check=False, env=CLEAN_ENV)
        controlled = self.fired()
        if 'transport' not in controlled:
            self.skipTest(f'this git did not try to fetch the missing commit {oldest[:7]}, so the control cannot show the risk')
        with mock.patch.object(check_git, 'check_config', lambda gitdir: None):
            result = self.check()
            self.check({'messageContains': 'first'})
        self.assertEqual(self.fired(), [])
        self.assertNotEqual(result['status'], 'passed')


class RepositoryStateTests(GitCase):
    def fake_git(self, script):
        path = self.base / 'fake-git'
        path.write_text('#!/bin/sh\n' + script)
        path.chmod(0o755)
        return str(path)

    def verb_script(self, **answers):
        """A fake git. `answers` maps a verb to shell text that runs for it. Other verbs exit 2."""
        body = 'verb=""\nfor a in "$@"; do case "$a" in rev-parse|rev-list|cat-file|show-ref) verb=$a;; esac; done\ncase "$verb" in\n'
        for verb, text in answers.items():
            body += f'  {verb}) {text} ;;\n'
        return self.fake_git(body + '  *) exit 2 ;;\nesac\n')

    def run_with(self, fake, **fields):
        self.repo_with('a')
        with mock.patch.object(check_git, 'find_git', return_value=fake):
            return self.check(fields)

    HEAD = 'echo ' + 'a' * 40

    def test_a_missing_head_object_is_an_error_not_no_commits(self):
        self.repo_with('first', 'second')
        head = git(self.work, 'rev-parse', 'HEAD').stdout.decode().strip()
        git(self.work, 'repack', '-a', '-d', '-q')
        git(self.work, 'prune-packed')
        for pack in (self.work / '.git' / 'objects' / 'pack').glob('*.pack'):
            pack.unlink()
        for index in (self.work / '.git' / 'objects' / 'pack').glob('*.idx'):
            index.unlink()
        result = self.check()
        self.assertEqual(result['status'], 'error', (head, result))
        self.assertNotEqual(result['evidence'].get('reason'), 'no_commits')

    def test_a_detached_head_that_names_a_missing_commit_is_an_error(self):
        self.repo_with('first')
        (self.work / '.git' / 'HEAD').write_text('b' * 40 + '\n')
        result = self.check()
        self.assertEqual(result['status'], 'error')

    def test_a_new_repository_with_no_refs_is_still_no_commits(self):
        self.init()
        result = self.check()
        self.assertEqual((result['status'], result['evidence']['reason']), ('failed', 'no_commits'))

    def test_an_unborn_branch_beside_other_branches_is_an_error_that_names_the_cause(self):
        self.repo_with('first')
        git(self.work, 'branch', 'other')
        git(self.work, 'symbolic-ref', 'HEAD', 'refs/heads/unborn')
        result = self.check()
        self.assertEqual((result['status'], result['evidence']['reason']), ('error', 'git_failed'))

    def test_a_rev_list_count_with_an_error_exit_is_not_a_pass(self):
        fake = self.verb_script(**{'rev-parse': self.HEAD, 'rev-list': 'echo 99; exit 1'})
        result = self.run_with(fake, minCommits=1)
        self.assertEqual((result['status'], result['evidence']['reason']), ('error', 'git_failed'))
        self.assertNotIn('commits', result['evidence'])

    def test_a_rev_list_count_that_is_not_a_number_is_not_a_pass(self):
        fake = self.verb_script(**{'rev-parse': self.HEAD, 'rev-list': 'echo lots'})
        self.assertEqual(self.run_with(fake)['status'], 'error')

    def test_a_cat_file_error_exit_is_not_a_pass(self):
        batch = 'cat >/dev/null; printf "%s commit 20\\ntree x\\n\\nmatch\\n\\n" ' + 'a' * 40 + '; exit 1'
        fake = self.verb_script(**{'rev-parse': self.HEAD, 'rev-list': self.HEAD, 'cat-file': batch})
        result = self.run_with(fake, messageContains='match')
        self.assertEqual((result['status'], result['evidence']['reason']), ('error', 'git_failed'))

    def test_a_cat_file_answer_that_cannot_be_parsed_is_not_a_pass(self):
        fake = self.verb_script(**{'rev-parse': self.HEAD, 'rev-list': self.HEAD, 'cat-file': 'cat >/dev/null; echo garbage'})
        result = self.run_with(fake, messageContains='match')
        self.assertEqual((result['status'], result['evidence']['reason']), ('error', 'git_failed'))

    def test_a_git_that_cannot_start_is_never_a_pass(self):
        self.repo_with('a')
        with mock.patch.object(check_git.subprocess, 'Popen', side_effect=OSError('no')):
            result = self.check()
        self.assertNotEqual(result['status'], 'passed')
        self.assertEqual(result['status'], 'skipped')

    def test_a_scan_that_hit_the_cap_says_so(self):
        self.init()
        for index in range(4):
            git(self.work, 'commit', '-q', '--allow-empty', '-m', f'c{index}')
        result = self.check({'messageContains': 'c0'}, Limits(git_max_scan=3))
        self.assertIn('newest 3', ' '.join(result['log']))
        result = self.check({'messageContains': 'c3'}, Limits(git_max_scan=10))
        self.assertNotIn('newest', ' '.join(result['log']))

    def test_a_scratch_folder_that_cannot_be_removed_is_noted_in_the_log(self):
        self.repo_with('a')
        with mock.patch.object(check_git.shutil, 'rmtree', lambda *a, **k: None):
            result = self.check()
        self.assertEqual(result['status'], 'passed')
        self.assertIn('could not remove', ' '.join(result['log']).lower())
        for name in os.listdir(tempfile.gettempdir()):
            if name.startswith(check_git.PRIVATE_PREFIX):
                shutil.rmtree(Path(tempfile.gettempdir()) / name, ignore_errors=True)


class LimitTests(GitCase):
    def fake_git(self, body):
        path = self.base / 'fake-git'
        path.write_text('#!/bin/sh\n' + body)
        path.chmod(0o755)
        return str(path)

    def test_a_git_that_hangs_is_killed_with_its_children(self):
        self.repo_with('a')
        child_pid = self.base / 'child.pid'
        fake = self.fake_git(f'sleep 60 &\necho $! > "{child_pid}"\nsleep 60\n')
        started = time.monotonic()
        with mock.patch.object(check_git, 'find_git', return_value=fake):
            result = self.check(limits=Limits(git_timeout=0.7))
        self.assertLess(time.monotonic() - started, 6)
        self.assertEqual((result['status'], result['evidence']['reason']), ('error', 'git_timeout'))
        pid = int(child_pid.read_text())
        deadline = time.monotonic() + 3
        alive = True
        while time.monotonic() < deadline and alive:
            try:
                os.kill(pid, 0)
                time.sleep(0.05)
            except ProcessLookupError:
                alive = False
        self.assertFalse(alive, 'the child of git is still running')

    def test_a_kill_that_fails_is_an_error_and_does_not_hang(self):
        self.repo_with('a')
        pid_file = self.base / 'git.pid'
        fake = self.fake_git(f'echo $$ > "{pid_file}"\nsleep 60\n')
        started = time.monotonic()
        try:
            with mock.patch.object(check_git, 'find_git', return_value=fake), mock.patch.object(check_runner, 'kill_group', return_value=False):
                result = self.check(limits=Limits(git_timeout=0.4, kill_wait_seconds=0.4))
            self.assertLess(time.monotonic() - started, 8)
            self.assertEqual((result['status'], result['evidence']['reason']), ('error', 'kill_failed'))
        finally:
            if pid_file.exists():
                try:
                    os.killpg(int(pid_file.read_text()), signal.SIGKILL)
                except ProcessLookupError:
                    pass

    def test_git_output_over_the_cap_is_killed(self):
        self.repo_with('a')
        fake = self.fake_git('yes xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx\n')
        started = time.monotonic()
        with mock.patch.object(check_git, 'find_git', return_value=fake):
            result = self.check(limits=Limits(git_output_bytes=50_000))
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual((result['status'], result['evidence']['reason']), ('error', 'output_too_large'))

    def test_git_that_writes_a_flood_of_errors_does_not_fill_memory(self):
        self.repo_with('a')
        fake = self.fake_git('yes error-text 1>&2 &\nPID=$!\nsleep 0.3\nkill $PID\nexit 3\n')
        with mock.patch.object(check_git, 'find_git', return_value=fake):
            result = self.check()
        self.assertEqual((result['status'], result['evidence']['reason']), ('error', 'git_failed'))
        self.assertLess(len(json.dumps(result)), 5000)

    def test_the_private_folder_is_removed_after_every_run(self):
        self.repo_with('a')
        before = set(os.listdir(tempfile.gettempdir()))
        for _ in range(3):
            self.check({'messageContains': 'a'})
        left = [name for name in set(os.listdir(tempfile.gettempdir())) - before if name.startswith('schola-git-')]
        self.assertEqual(left, [])

    def test_the_private_folder_is_never_removed_outside_the_temp_root(self):
        self.repo_with('a')
        victim = self.outside / 'victim'
        victim.mkdir()
        (victim / 'keep.txt').write_text('keep')
        with mock.patch.object(check_git.tempfile, 'mkdtemp', return_value=str(victim)):
            self.check()
        self.assertTrue((victim / 'keep.txt').exists())


if __name__ == '__main__':
    unittest.main()
