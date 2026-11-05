"""The `git-commit-exists` check: does the student's repository hold a commit?

Git can run programs that a repository's own config names: a file system
monitor, hooks, a pager, an external diff, an editor, an SSH command, and more.
A repository in the student's workspace is not trusted. So this check:

* never uses a shell. Git runs from an argument list, with `cwd` set to the
  workspace.
* gives git a clean environment. No `GIT_*` variable of the server's reaches it.
  System and user config are switched off.
* names the git folder itself with `--git-dir`, after checking it. The `.git`
  entry must be a real folder, or a file that points at a folder inside the
  workspace. A link, or a file that points outside, is refused. Git never walks
  up to a parent repository.
* passes a fixed list of `-c` overrides that switch off every setting known to
  name a program (`FIXED_CONFIG`).
* runs only three read commands: `rev-parse`, `rev-list`, and `cat-file`. None
  of them reads the index or the working tree, and none of them runs hooks.
* limits each run in time and output, and kills git and its children on either.
* checks that the current user owns the repository. When `--git-dir` is given,
  git skips its own ownership check, so this module repeats it. It never passes
  `safe.directory`, so it never widens trust.

`docs/RELEASE.md` lists what was proved by test and what was not.

Only the Python standard library is used.
"""
import os
import re
import shutil
import stat
import subprocess
import tempfile
import threading

import check_runner
import check_workspace
from check_runner import DescriptorError, result, take_fields
from check_workspace import WorkspaceError

# Each pair becomes `-c key=value`. An empty value clears a setting that names a program.
FIXED_CONFIG = (
    ('core.fsmonitor', 'false'),
    ('core.hooksPath', os.devnull),
    ('core.sshCommand', ''),
    ('core.pager', 'cat'),
    ('core.editor', ''),
    ('core.askPass', ''),
    ('core.alternateRefsCommand', ''),
    ('core.attributesFile', os.devnull),
    ('core.excludesFile', os.devnull),
    ('diff.external', ''),
    ('credential.helper', ''),
    ('gpg.program', 'false'),
    ('uploadpack.packObjectsHook', ''),
    ('protocol.allow', 'never'),
    ('protocol.ext.allow', 'never'),
    ('gc.auto', '0'),
    ('maintenance.auto', 'false'),
)
SAFE_PATH = '/usr/bin:/bin'
PRIVATE_PREFIX = 'schola-git-'
MAX_GITFILE_BYTES = 4096
MAX_STDERR_BYTES = 4096
_SHA = re.compile(r'^[0-9a-f]{40}(?:[0-9a-f]{24})?$')
_BATCH_HEADER = re.compile(rb'^([0-9a-f]{40}(?:[0-9a-f]{24})?) (\w+) (\d+)$')

# Entries of the git folder that must be plain files or plain folders, never links, pipes, or sockets.
_FILES = ('HEAD', 'config', 'packed-refs', 'shallow', 'description')
_FOLDERS = ('objects', 'refs', 'info')
_REFUSED = ('commondir', 'objects/info/alternates', 'objects/info/http-alternates')


class GitProblem(Exception):
    """A result the check reports. `status` is `failed`, `error`, or `skipped`."""

    def __init__(self, status, reason, message):
        super().__init__(message)
        self.status, self.reason, self.message = status, reason, message

    def as_result(self, **evidence):
        return result(self.status, {'reason': self.reason, **evidence}, [self.message])


def find_git():
    """The path of the git program, or None."""
    return shutil.which('git')


def _unsafe(message='This folder has a .git entry that Schola will not use. It is a link, points outside the workspace, or is not a normal git folder.'):
    return GitProblem('error', 'unsafe_git_dir', message)


# ---------------------------------------------------------------------------
# Finding and checking the git folder
# ---------------------------------------------------------------------------

def _owned(info):
    return not hasattr(os, 'geteuid') or info.st_uid == os.geteuid()


def locate_git_dir(root):
    """The real path of the git folder of the workspace `root`, or a GitProblem."""
    entry = os.path.join(root, '.git')
    try:
        info = os.lstat(entry)
    except FileNotFoundError:
        raise GitProblem('failed', 'no_repository', 'This folder is not a git repository. Run git init here, then make a commit.') from None
    except OSError:
        raise _unsafe() from None
    if stat.S_ISLNK(info.st_mode):
        raise _unsafe()
    if stat.S_ISDIR(info.st_mode):
        gitdir = entry
    elif stat.S_ISREG(info.st_mode):
        gitdir = _follow_gitfile(root)
    else:
        raise _unsafe()
    return _validate_git_dir(root, gitdir)


def _follow_gitfile(root):
    try:
        data = check_workspace.read_file(root, '.git', MAX_GITFILE_BYTES)
    except WorkspaceError:
        raise _unsafe() from None
    try:
        text = data.decode('utf-8')
    except UnicodeError:
        raise _unsafe() from None
    first = text.split('\n', 1)[0].rstrip('\r')
    if not first.startswith('gitdir: ') or '\x00' in text:
        raise _unsafe()
    target = first[len('gitdir: '):].strip()
    if not target or any(ord(ch) < 32 for ch in target):
        raise _unsafe()
    try:
        real = os.path.realpath(os.path.join(root, target), strict=True)
    except (OSError, ValueError):
        raise _unsafe() from None
    if not check_workspace.is_inside(root, real) or real == root:
        raise _unsafe()
    return real


def _validate_git_dir(root, gitdir):
    """The git folder, as a real path: a real folder inside the workspace, owned by this user, with plain entries."""
    try:
        real = os.path.realpath(gitdir)
        if real == root or not check_workspace.is_inside(root, real):
            raise _unsafe()
        info = os.lstat(real)
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise _unsafe()
        owner_ok = _owned(info) and _owned(os.lstat(root))
    except OSError:
        raise _unsafe() from None
    if not owner_ok:
        raise GitProblem('error', 'untrusted_repository',
                         'Another user owns this repository. Git would not trust it, and neither does Schola. Make a repository of your own in your folder.')
    for name in _REFUSED:
        if os.path.lexists(os.path.join(real, *name.split('/'))):
            raise _unsafe('This repository borrows objects or settings from another folder. Schola will not read it. Make a repository of your own.')
    for name in _FILES:
        _require(real, name, stat.S_ISREG, required=name == 'HEAD')
    for name in _FOLDERS:
        _require(real, name, stat.S_ISDIR, required=name != 'info')
    check_config(real)
    return real


MAX_CONFIG_BYTES = 262144
# The settings a repository config may hold: plain data that names no program and pulls in no other file.
# Section -> allowed keys (lower case). `None` allows any key. A section name ending in `+` also allows a subsection.
_ALLOWED_CONFIG = {
    'core': {'repositoryformatversion', 'filemode', 'bare', 'logallrefupdates', 'ignorecase', 'precomposeunicode', 'symlinks', 'autocrlf',
             'eol', 'safecrlf', 'quotepath', 'trustctime', 'checkstat', 'sharedrepository', 'ignorestat', 'hidedotfiles', 'protecthfs',
             'protectntfs', 'whitespace', 'untrackedcache', 'commentchar', 'abbrev', 'compression', 'loosecompression', 'longpaths',
             'warnambiguousrefs', 'bigfilethreshold'},
    'user': {'name', 'email', 'signingkey', 'useconfigonly'},
    'init': {'defaultbranch'},
    'pull': {'rebase', 'ff'},
    'push': {'default', 'followtags', 'autosetupremote'},
    'fetch': {'prune', 'prunetags'},
    'status': {'showuntrackedfiles', 'short', 'branch'},
    'merge': {'ff', 'conflictstyle'},
    'rebase': {'autostash', 'autosquash'},
    'log': {'date', 'abbrevcommit'},
    'diff': {'renames', 'algorithm', 'colormoved', 'mnemonicprefix'},
    'help': {'autocorrect'},
    'color': None,
    'advice': None,
    'extensions': {'objectformat'},
    'branch': {'remote', 'merge', 'rebase', 'description', 'pushremote', 'autosetupmerge', 'autosetuprebase'},
    'remote': {'url', 'fetch', 'pushurl', 'tagopt', 'push', 'prune'},
    'url': {'insteadof', 'pushinsteadof'},
}
_SUBSECTIONS = frozenset({'branch', 'remote', 'url', 'color'})
_HEADER = re.compile(r'^\[\s*([A-Za-z0-9.-]+)(?:\s+"((?:[^"\\]|\\.)*)")?\s*\](.*)$')
_KEY = re.compile(r'^([A-Za-z][A-Za-z0-9-]*)\s*(?:=.*)?$')


def check_config(gitdir):
    """Refuse a repository whose config holds anything outside the allow-list. Reads the file as text and runs nothing."""
    pack = os.path.join(gitdir, 'objects', 'pack')
    try:
        if any(entry.endswith('.promisor') for entry in os.listdir(pack)):
            raise _unsafe(_CONFIG_MESSAGE)
    except FileNotFoundError:
        pass
    except OSError:
        raise _unsafe() from None
    path = os.path.join(gitdir, 'config')
    try:
        info = os.lstat(path)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_CONFIG_BYTES:
            raise _unsafe(_CONFIG_MESSAGE)
        with open(path, 'rb') as handle:
            text = handle.read(MAX_CONFIG_BYTES + 1).decode('utf-8', errors='replace')
    except FileNotFoundError:
        text = ''
    except OSError:
        raise _unsafe(_CONFIG_MESSAGE) from None
    section, continued = None, False
    for raw in text.split('\n'):
        line = raw.strip()
        was_continued, continued = continued, line.endswith('\\') and not line.startswith(('#', ';'))
        if was_continued or not line or line[0] in '#;':
            continue
        while line:
            header = _HEADER.match(line)
            if header:
                name, sub, line = header.group(1).lower(), header.group(2), header.group(3).strip()
                if '.' in name:
                    name, _, sub = name.partition('.')
                section = (name, sub)
                if name not in _ALLOWED_CONFIG or (sub is not None and name not in _SUBSECTIONS):
                    raise _unsafe(_CONFIG_MESSAGE)
                if line[:1] in ('#', ';'):
                    line = ''
                continue
            key = _KEY.match(line)
            if not key or section is None:
                raise _unsafe(_CONFIG_MESSAGE)
            allowed = _ALLOWED_CONFIG[section[0]]
            name = key.group(1).lower()
            if allowed is not None and name not in allowed:
                raise _unsafe(_CONFIG_MESSAGE)
            if section[0] == 'remote' and name in ('url', 'pushurl') and '::' in line.split('=', 1)[-1]:
                raise _unsafe(_CONFIG_MESSAGE)
            break


_CONFIG_MESSAGE = ('This repository has a setting that Schola does not read, such as a program, a partial clone, or an included file. '
                   'Schola will not run git on it. Make a repository of your own with git init.')


def _require(gitdir, name, kind, required):
    path = os.path.join(gitdir, name)
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        if required:
            raise _unsafe() from None
        return
    except OSError:
        raise _unsafe() from None
    if stat.S_ISLNK(info.st_mode) or not kind(info.st_mode):
        raise _unsafe()


# ---------------------------------------------------------------------------
# Running git
# ---------------------------------------------------------------------------

def clean_environment(private):
    """The only variables git gets. Nothing is copied from the server's own environment."""
    return {
        'PATH': SAFE_PATH, 'HOME': private, 'XDG_CONFIG_HOME': private, 'LANG': 'C', 'LC_ALL': 'C',
        'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_CONFIG_SYSTEM': os.devnull,
        'GIT_TERMINAL_PROMPT': '0', 'GIT_OPTIONAL_LOCKS': '0', 'GIT_PAGER': 'cat', 'GIT_ATTR_NOSYSTEM': '1', 'GIT_NO_REPLACE_OBJECTS': '1',
        'GIT_NO_LAZY_FETCH': '1', 'GIT_ALLOW_PROTOCOL': 'none',
    }


def build_command(git, gitdir, verb_and_args):
    command = [git, '--no-pager', '--no-replace-objects', '--git-dir', gitdir]
    for key, value in FIXED_CONFIG:
        command += ['-c', f'{key}={value}']
    return command + list(verb_and_args)


def _drain(stream, keep, box):
    """Read a stream to its end. Keep only the first `keep` bytes, so a flood costs no memory."""
    kept = bytearray()
    try:
        while True:
            chunk = stream.read(65536)
            if not chunk:
                break
            if len(kept) < keep:
                kept += chunk[:keep - len(kept)]
    except (OSError, ValueError):
        pass
    finally:
        try:
            stream.close()
        except OSError:
            pass
    box.append(bytes(kept))


def run_git(git, gitdir, workspace, private, args, limits, input_bytes=None):
    """Run one git read command. Returns (return code, stdout bytes, stderr bytes)."""
    stdin = subprocess.DEVNULL
    stdin_handle = None
    if input_bytes is not None:
        path = os.path.join(private, 'input')
        with open(path, 'wb') as handle:
            handle.write(input_bytes)
        stdin = stdin_handle = open(path, 'rb')
    try:
        process = subprocess.Popen(build_command(git, gitdir, args), cwd=workspace, env=clean_environment(private), shell=False,
                                   stdin=stdin, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True, close_fds=True)
    except OSError:
        raise GitProblem('skipped', 'git_unavailable', 'Schola cannot start git. Report this check yourself (self-report).') from None
    finally:
        if stdin_handle is not None:
            stdin_handle.close()
    errors = []
    collector = check_runner.Collector(process, limits.git_output_bytes, on_overflow=lambda: check_runner.kill_group(process))
    out_reader = threading.Thread(target=collector.run, daemon=True)
    err_reader = threading.Thread(target=_drain, args=(process.stderr, MAX_STDERR_BYTES, errors), daemon=True)
    out_reader.start()
    err_reader.start()
    timed_out = False
    try:
        try:
            process.wait(timeout=limits.git_timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            killed = check_runner.kill_group(process)
            try:
                process.wait(timeout=limits.kill_wait_seconds)
            except subprocess.TimeoutExpired:
                killed = False
            if not killed:
                raise GitProblem('error', 'kill_failed', 'Git took too long and Schola could not stop it. Close Schola and open it again.') from None
        # Anything git left running in its group goes now.
        check_runner.kill_group(process)
        out_reader.join(2)
        err_reader.join(2)
    finally:
        collector.stop()
        if process.poll() is None:
            check_runner.kill_group(process)
            try:
                process.wait(timeout=limits.kill_wait_seconds)
            except subprocess.TimeoutExpired:
                pass
    data, total, overflow = collector.snapshot()
    if timed_out:
        raise GitProblem('error', 'git_timeout', 'Git took too long, so Schola stopped it.')
    if overflow:
        raise GitProblem('error', 'output_too_large', 'Git printed more than Schola reads for a check.')
    return process.returncode, data, errors[0] if errors else b''


def _remove_private(path):
    """Remove the private folder, but only a folder this module made directly in the temp root.

    Returns True when nothing is left behind that Schola made. Returns False when the folder is still there.
    """
    try:
        real = os.path.realpath(path)
        root = os.path.realpath(tempfile.gettempdir())
        if os.path.dirname(real) != root or not os.path.basename(real).startswith(PRIVATE_PREFIX) or os.path.islink(path):
            return True
        shutil.rmtree(real, ignore_errors=True)
        return not os.path.lexists(real)
    except OSError:
        return False


def _parse_batch(data, wanted):
    """The messages of the commits in `cat-file --batch` output. A malformed answer raises ValueError."""
    messages, offset = [], 0
    while offset < len(data) and len(messages) < wanted:
        end = data.find(b'\n', offset)
        if end < 0:
            raise ValueError('header')
        header = data[offset:end]
        offset = end + 1
        if header.endswith(b' missing'):
            continue
        match = _BATCH_HEADER.match(header)
        if not match:
            raise ValueError('header')
        size = int(match.group(3))
        body = data[offset:offset + size]
        if len(body) != size:
            raise ValueError('size')
        offset += size + 1
        if match.group(2) == b'commit':
            _, _, message = body.partition(b'\n\n')
            messages.append(message.decode('utf-8', errors='replace'))
    return messages


# ---------------------------------------------------------------------------
# The adapter
# ---------------------------------------------------------------------------

class GitAdapter:
    """`git-commit-exists`: `minCommits`, `messageContains`, and `ignoreCase`."""
    types = ('git-commit-exists',)

    def run(self, check, workspace, limits):
        try:
            fields = take_fields(check['runner'], {'messageContains': ('str', False), 'minCommits': ('int', False), 'ignoreCase': ('bool', False)})
            needle = fields.get('messageContains')
            if needle is not None and not 1 <= len(needle) <= 500:
                raise DescriptorError('The field messageContains must hold 1 to 500 characters.')
            minimum = fields.get('minCommits', 1)
            if not 1 <= minimum <= limits.max_min_count:
                raise DescriptorError(f'The field minCommits must be between 1 and {limits.max_min_count}.')
        except DescriptorError as error:
            return check_runner.bad_descriptor(error)
        git = find_git()
        if not git:
            return result('skipped', {'reason': 'git_unavailable'}, ['Git is not installed, so Schola cannot run this check. Report the result yourself (self-report).'])
        private = tempfile.mkdtemp(prefix=PRIVATE_PREFIX)
        try:
            gitdir = locate_git_dir(workspace)
            outcome = self._check(git, gitdir, workspace, private, limits, needle, minimum, fields.get('ignoreCase', False))
        except GitProblem as problem:
            outcome = problem.as_result()
        finally:
            clean = _remove_private(private)
        if not clean:
            outcome['log'].append('Schola could not remove its temporary git folder. It is safe to delete a folder named schola-git-* in your temp folder.')
        return outcome

    def _check(self, git, gitdir, workspace, private, limits, needle, minimum, ignore_case):
        code, out, err = run_git(git, gitdir, workspace, private, ['rev-parse', '--verify', '--quiet', 'HEAD^{commit}'], limits)
        head = out.decode('ascii', errors='replace').strip()
        if code == 1 and not head:
            return self._no_head(git, gitdir, workspace, private, limits, minimum)
        if code != 0 or not _SHA.match(head):
            return self._git_failed(code, err)
        evidence = {'head': head[:7], 'required': minimum}
        if needle is None:
            code, out, err = run_git(git, gitdir, workspace, private, ['rev-list', '--count', 'HEAD'], limits)
            text = out.decode('ascii', errors='replace').strip()
            if code != 0 or not text.isdigit():
                return self._git_failed(code, err)
            commits = int(text)
            evidence['commits'] = commits
            if commits >= minimum:
                return result('passed', evidence, [f'Found {commits} commits.'])
            return result('failed', evidence, ['There are not enough commits yet.'])
        code, out, err = run_git(git, gitdir, workspace, private, ['rev-list', f'--max-count={limits.git_max_scan}', 'HEAD'], limits)
        shas = [item for item in out.decode('ascii', errors='replace').split('\n') if item]
        if code != 0 or not shas or not all(_SHA.match(item) for item in shas):
            return self._git_failed(code, err)
        code, out, err = run_git(git, gitdir, workspace, private, ['cat-file', '--batch'], limits, ('\n'.join(shas) + '\n').encode('ascii'))
        if code != 0:
            return self._git_failed(code, err)
        try:
            messages = _parse_batch(out, len(shas))
        except ValueError:
            return result('error', {'reason': 'git_failed'}, ['Git gave an answer Schola could not read.'])
        wanted = needle.casefold() if ignore_case else needle
        matching = sum(1 for message in messages if wanted in (message.casefold() if ignore_case else message))
        evidence.update({'matching': matching, 'scanned': len(messages)})
        note = [f'Schola read only the newest {limits.git_max_scan} commits.'] if len(messages) >= limits.git_max_scan else []
        if matching >= minimum:
            return result('passed', evidence, [f'Found {matching} matching commits.'] + note)
        return result('failed', evidence, ['Not enough commit messages contain that text.'] + note)

    def _no_head(self, git, gitdir, workspace, private, limits, minimum):
        """HEAD does not name a commit. That is 'no commits yet' only for a repository with no refs and a HEAD that names a branch."""
        code, out, err = run_git(git, gitdir, workspace, private, ['show-ref'], limits)
        try:
            with open(os.path.join(gitdir, 'HEAD'), 'rb') as handle:
                head_text = handle.read(512).decode('utf-8', errors='replace').strip()
        except OSError:
            return self._git_failed(None, b'')
        if code == 1 and not out.strip() and head_text.startswith('ref: refs/'):
            return result('failed', {'reason': 'no_commits', 'commits': 0, 'required': minimum}, ['This repository has no commits yet. Make a commit first.'])
        return result('error', {'reason': 'git_failed', 'exitCode': code},
                      ['HEAD does not point at a commit, but the repository holds other refs or a damaged HEAD. Schola cannot tell whether you made commits.'])

    @staticmethod
    def _git_failed(code, err):
        if b'dubious ownership' in err:
            return result('error', {'reason': 'untrusted_repository'}, ['Git does not trust this repository because another user owns it.'])
        return result('error', {'reason': 'git_failed', 'exitCode': code}, ['Git could not read this repository.'])
