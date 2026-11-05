"""Read files inside a student's workspace folder without ever leaving it.

Every check that touches the student's files goes through this module. The path
comes from lesson content, so it is untrusted. The rules are:

* The path is relative. It holds no NUL byte, no backslash, no drive letter, no
  `..` part, and no part that is too long. It is not too long overall.
* The path is resolved with `realpath` first. If the result is outside the
  workspace, the path is refused. A symbolic link that stays inside the
  workspace is allowed. A link that leaves it is never followed.
* The resolved path is then opened one part at a time, from a folder
  descriptor, with `O_NOFOLLOW`. A link that appears after the first check
  makes the open fail. The check and the open cannot be split by an attacker.
* Before the last part is opened, it is looked at without following links. A
  device, a named pipe, a socket, or anything else that is not a regular file
  or a folder is refused without being opened.
* The open file is checked again with `fstat`. It must be a regular file below
  the size limit. Where the operating system can name the path of an open
  file, that path must be inside the workspace too.
* Reads stop at the size limit, even if the file grows while it is read.

Only the Python standard library is used.
"""
import errno
import os
import stat
import sys

# Error codes. `not_found` is an ordinary result for a check. The others mean the descriptor or the
# workspace is unsafe or unusable, and a check reports them as an error.
INVALID_PATH = 'invalid_path'
OUTSIDE = 'outside_workspace'
NOT_FOUND = 'not_found'
NOT_REGULAR = 'not_regular'
WRONG_KIND = 'wrong_kind'
TOO_LARGE = 'file_too_large'
UNREADABLE = 'unreadable'
NO_WORKSPACE = 'workspace_unavailable'

DEFAULT_MAX_FILE_BYTES = 1_000_000
DEFAULT_MAX_PATH_BYTES = 1024
DEFAULT_MAX_COMPONENT_BYTES = 255
DEFAULT_MAX_DEPTH = 32

_O_CLOEXEC = getattr(os, 'O_CLOEXEC', 0)
_O_NOFOLLOW = getattr(os, 'O_NOFOLLOW', 0)
_O_NONBLOCK = getattr(os, 'O_NONBLOCK', 0)
_O_DIRECTORY = getattr(os, 'O_DIRECTORY', 0)
_HAS_DIR_FD = os.open in os.supports_dir_fd and os.stat in os.supports_dir_fd and bool(_O_NOFOLLOW)

_MESSAGES = {
    INVALID_PATH: 'The path in this check is not a safe relative path.',
    OUTSIDE: 'The path in this check leaves the workspace folder. Schola did not open it.',
    NOT_FOUND: 'No file or folder exists at that path.',
    NOT_REGULAR: 'The path is not an ordinary file or folder. Schola did not open it.',
    WRONG_KIND: 'The path is a file where a folder was expected, or a folder where a file was expected.',
    TOO_LARGE: 'The file is larger than the size Schola reads for a check.',
    UNREADABLE: 'Schola could not read that path.',
    NO_WORKSPACE: 'The workspace folder is not set, or it cannot be used.',
}


class WorkspaceError(Exception):
    """A path cannot be used. `code` is one of the codes above. The message holds no path."""

    def __init__(self, code, message=None):
        super().__init__(message or _MESSAGES[code])
        self.code = code


def split_relative(path, max_path_bytes=DEFAULT_MAX_PATH_BYTES, max_component_bytes=DEFAULT_MAX_COMPONENT_BYTES, max_depth=DEFAULT_MAX_DEPTH):
    """Turn a relative path into a list of parts, or raise `WorkspaceError`. Touches no file."""
    if not isinstance(path, str) or not path:
        raise WorkspaceError(INVALID_PATH)
    if '\x00' in path or '\\' in path or any(ord(ch) < 32 or ord(ch) == 127 for ch in path):
        raise WorkspaceError(INVALID_PATH)
    try:
        encoded = path.encode('utf-8')
    except UnicodeError:
        raise WorkspaceError(INVALID_PATH) from None
    if len(encoded) > max_path_bytes:
        raise WorkspaceError(INVALID_PATH)
    if path.startswith('/') or (len(path) >= 2 and path[1] == ':' and path[0].isalpha()):
        raise WorkspaceError(INVALID_PATH)
    parts = [part for part in path.split('/') if part not in ('', '.')]
    if not parts or len(parts) > max_depth:
        raise WorkspaceError(INVALID_PATH)
    for part in parts:
        if part == '..' or len(part.encode('utf-8')) > max_component_bytes:
            raise WorkspaceError(INVALID_PATH)
    return parts


def workspace_root(workspace):
    """The real path of the workspace folder. Raises `WorkspaceError` if it is not a folder."""
    if not isinstance(workspace, (str, os.PathLike)):
        raise WorkspaceError(NO_WORKSPACE)
    try:
        text = os.fspath(workspace)
        if not isinstance(text, str) or not text or '\x00' in text:
            raise WorkspaceError(NO_WORKSPACE)
        root = os.path.realpath(text)
        if not stat.S_ISDIR(os.stat(root).st_mode):
            raise WorkspaceError(NO_WORKSPACE)
    except (OSError, ValueError):
        raise WorkspaceError(NO_WORKSPACE) from None
    return root


def is_inside(root, candidate):
    """True when the real path `candidate` is `root` or below it. Both must already be real paths."""
    if candidate == root:
        return True
    prefix = root if root.endswith(os.sep) else root + os.sep
    return candidate.startswith(prefix)


def _fd_path(fd):
    """The path the operating system reports for an open descriptor, or None when it cannot say."""
    try:
        if sys.platform == 'darwin':
            import fcntl
            raw = fcntl.fcntl(fd, fcntl.F_GETPATH, b'\0' * 1024)
            return os.fsdecode(raw.split(b'\0', 1)[0])
        if sys.platform.startswith('linux'):
            return os.readlink(f'/proc/self/fd/{fd}')
    except (OSError, ImportError, AttributeError, ValueError):
        return None
    return None


def _same_file(a, b):
    return a.st_dev == b.st_dev and a.st_ino == b.st_ino


def _descends_from(path, root_stat):
    """True when `path` or one of its parents is the workspace folder. Compares files, so letter case cannot fool it."""
    current = path
    for _ in range(4096):
        try:
            if _same_file(os.stat(current), root_stat):
                return True
        except OSError:
            return False
        parent = os.path.dirname(current)
        if parent == current:
            return False
        current = parent
    return False


def _map_open_error(error):
    if error.errno in (errno.ENOENT, errno.ENOTDIR):
        return WorkspaceError(NOT_FOUND)
    if error.errno in (errno.ELOOP, errno.EMLINK):
        # O_NOFOLLOW found a link after the path was resolved. Treat it as an escape attempt.
        return WorkspaceError(OUTSIDE)
    if error.errno in (errno.ENXIO, errno.EOPNOTSUPP, errno.ENODEV):
        return WorkspaceError(NOT_REGULAR)
    if error.errno == errno.ENAMETOOLONG:
        return WorkspaceError(INVALID_PATH)
    return WorkspaceError(UNREADABLE)


def _resolve(root, parts):
    """The real parts of `parts` below `root`, or raise. A link that leaves the workspace is refused here."""
    target = os.path.realpath(os.path.join(root, *parts))
    if not is_inside(root, target):
        raise WorkspaceError(OUTSIDE)
    relative = os.path.relpath(target, root)
    return [] if relative == '.' else relative.split(os.sep)


def open_inside(workspace, path, want='file', max_file_bytes=DEFAULT_MAX_FILE_BYTES, **path_limits):
    """Open a path inside the workspace for reading and return the file descriptor. The caller closes it.

    `want` is `file`, `directory`, or `any` (a file or a folder). Anything else is refused.
    Raises `WorkspaceError`. The error never holds a path.
    """
    parts = split_relative(path, **path_limits)
    root = workspace_root(workspace)
    real_parts = _resolve(root, parts)
    if not _HAS_DIR_FD:
        return _open_without_dir_fd(root, real_parts, want, max_file_bytes)
    root_stat = os.stat(root)
    current = None
    try:
        current = os.open(root, os.O_RDONLY | _O_DIRECTORY | _O_CLOEXEC)
        if not _same_file(os.fstat(current), root_stat):
            raise WorkspaceError(OUTSIDE)
        for part in real_parts[:-1]:
            try:
                following = os.open(part, os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW | _O_CLOEXEC, dir_fd=current)
            except OSError as error:
                if error.errno in (errno.ENOTDIR, errno.ELOOP):
                    # Either a plain file used as a folder, or a link swapped in after the path check.
                    try:
                        swapped = stat.S_ISLNK(os.stat(part, dir_fd=current, follow_symlinks=False).st_mode)
                    except OSError:
                        swapped = False
                    raise WorkspaceError(OUTSIDE if swapped else NOT_FOUND) from None
                raise _map_open_error(error) from None
            os.close(current)
            current = following
        if not real_parts:
            leaf, current = current, None
            return _checked(leaf, root_stat, want, max_file_bytes)
        name = real_parts[-1]
        try:
            before = os.stat(name, dir_fd=current, follow_symlinks=False)
        except OSError as error:
            raise _map_open_error(error) from None
        if stat.S_ISLNK(before.st_mode):
            raise WorkspaceError(OUTSIDE)
        if not (stat.S_ISREG(before.st_mode) or stat.S_ISDIR(before.st_mode)):
            raise WorkspaceError(NOT_REGULAR)
        flags = os.O_RDONLY | _O_NOFOLLOW | _O_NONBLOCK | _O_CLOEXEC | (_O_DIRECTORY if stat.S_ISDIR(before.st_mode) else 0)
        try:
            leaf = os.open(name, flags, dir_fd=current)
        except OSError as error:
            raise _map_open_error(error) from None
        try:
            if not _same_file(os.fstat(leaf), before):
                raise WorkspaceError(OUTSIDE)
            return _checked(leaf, root_stat, want, max_file_bytes)
        except BaseException:
            _close_quietly(leaf)
            raise
    finally:
        if current is not None:
            _close_quietly(current)


def _close_quietly(fd):
    try:
        os.close(fd)
    except OSError:
        pass


def _checked(fd, root_stat, want, max_file_bytes):
    """Check an open descriptor. Returns it on success and closes it on failure."""
    try:
        info = os.fstat(fd)
        is_dir = stat.S_ISDIR(info.st_mode)
        if not (stat.S_ISREG(info.st_mode) or is_dir):
            raise WorkspaceError(NOT_REGULAR)
        if want not in ('file', 'directory', 'any'):
            raise WorkspaceError(INVALID_PATH)
        if (want == 'file' and is_dir) or (want == 'directory' and not is_dir):
            raise WorkspaceError(WRONG_KIND)
        if not is_dir and info.st_size > max_file_bytes:
            raise WorkspaceError(TOO_LARGE)
        reported = _fd_path(fd)
        if reported is not None and not _descends_from(reported, root_stat):
            raise WorkspaceError(OUTSIDE)
        return fd
    except BaseException:
        _close_quietly(fd)
        raise


def _open_without_dir_fd(root, real_parts, want, max_file_bytes):
    """A weaker path for systems with no folder descriptors. The resolved path is checked, then opened."""
    full = os.path.join(root, *real_parts)
    try:
        before = os.lstat(full)
    except OSError as error:
        raise _map_open_error(error) from None
    if stat.S_ISLNK(before.st_mode):
        raise WorkspaceError(OUTSIDE)
    if not (stat.S_ISREG(before.st_mode) or stat.S_ISDIR(before.st_mode)):
        raise WorkspaceError(NOT_REGULAR)
    flags = os.O_RDONLY | _O_NOFOLLOW | _O_NONBLOCK | _O_CLOEXEC
    try:
        fd = os.open(full, flags)
    except OSError as error:
        raise _map_open_error(error) from None
    if not _same_file(os.fstat(fd), before):
        _close_quietly(fd)
        raise WorkspaceError(OUTSIDE)
    return _checked(fd, os.stat(root), want, max_file_bytes)


def read_file(workspace, path, max_file_bytes=DEFAULT_MAX_FILE_BYTES, **path_limits):
    """The bytes of one regular file inside the workspace. Never more than `max_file_bytes`."""
    fd = open_inside(workspace, path, 'file', max_file_bytes, **path_limits)
    try:
        chunks, total = [], 0
        while True:
            try:
                chunk = os.read(fd, min(65536, max_file_bytes + 1 - total))
            except BlockingIOError:
                raise WorkspaceError(UNREADABLE) from None
            except OSError:
                raise WorkspaceError(UNREADABLE) from None
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > max_file_bytes:
                raise WorkspaceError(TOO_LARGE)
        return b''.join(chunks)
    finally:
        _close_quietly(fd)


def exists_inside(workspace, path, want='file', max_file_bytes=DEFAULT_MAX_FILE_BYTES, **path_limits):
    """(found, size). `found` is False when nothing is there. Any unsafe path raises `WorkspaceError`."""
    try:
        fd = open_inside(workspace, path, want, max_file_bytes, **path_limits)
    except WorkspaceError as error:
        if error.code == NOT_FOUND:
            return False, 0
        raise
    try:
        info = os.fstat(fd)
        return True, 0 if stat.S_ISDIR(info.st_mode) else info.st_size
    finally:
        _close_quietly(fd)
