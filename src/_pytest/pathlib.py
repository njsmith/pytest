
import os
import errno
import atexit
import operator
import six
import sys
from functools import reduce
import uuid
from six.moves import map
import itertools
import shutil
from os.path import expanduser, expandvars, isabs, sep
from posixpath import sep as posix_sep
import fnmatch
import stat

from .compat import PY36


if PY36:
    from pathlib import Path, PurePath
else:
    from pathlib2 import Path, PurePath

__all__ = ["Path", "PurePath"]


LOCK_TIMEOUT = 60 * 60 * 3

get_lock_path = operator.methodcaller("joinpath", ".lock")


def ensure_reset_dir(path):
    """
    ensures the given path is a empty directory
    """
    if path.exists():
        rmtree(path, force=True)
    path.mkdir()


def _shutil_rmtree_remove_writable(func, fspath, _):
    "Clear the readonly bit and reattempt the removal"
    os.chmod(fspath, stat.S_IWRITE)
    func(fspath)


def rmtree(path, force=False):
    if force:
        # ignore_errors leaves dead folders around
        # python needs a rm -rf as a followup
        # the trick with _shutil_rmtree_remove_writable is unreliable
        shutil.rmtree(str(path), ignore_errors=True)
    else:
        shutil.rmtree(str(path))


def find_prefixed(root, prefix):
    """finds all elements in root that begin with the prefix, case insensitive"""
    l_prefix = prefix.lower()
    for x in root.iterdir():
        if x.name.lower().startswith(l_prefix):
            yield x


def extract_suffixes(iter, prefix):
    """
    :param iter: iterator over path names
    :param prefix: expected prefix of the path names
    :returns: the parts of the paths following the prefix
    """
    p_len = len(prefix)
    for p in iter:
        yield p.name[p_len:]


def find_suffixes(root, prefix):
    """combines find_prefixes and extract_suffixes
    """
    return extract_suffixes(find_prefixed(root, prefix), prefix)


def parse_num(maybe_num):
    """parses number path suffixes, returns -1 on error"""
    try:
        return int(maybe_num)
    except ValueError:
        return -1


if six.PY2:

    def _max(iterable, default):
        """needed due to python2.7 lacking the default argument for max"""
        return reduce(max, iterable, default)


else:
    _max = max


def make_numbered_dir(root, prefix):
    """create a directory with a increased number as suffix for the given prefix"""
    for i in range(10):
        # try up to 10 times to create the folder
        max_existing = _max(map(parse_num, find_suffixes(root, prefix)), default=-1)
        new_number = max_existing + 1
        new_path = root.joinpath("{}{}".format(prefix, new_number))
        try:
            new_path.mkdir()
        except Exception:
            pass
        else:
            return new_path
    else:
        raise EnvironmentError(
            "could not create numbered dir with prefix "
            "{prefix} in {root} after 10 tries".format(prefix=prefix, root=root)
        )


def create_cleanup_lock(p):
    """crates a lock to prevent premature folder cleanup"""
    lock_path = get_lock_path(p)
    try:
        fd = os.open(str(lock_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except OSError as e:
        if e.errno == errno.EEXIST:
            six.raise_from(
                EnvironmentError("cannot create lockfile in {path}".format(path=p)), e
            )
        else:
            raise
    else:
        pid = os.getpid()
        spid = str(pid)
        if not isinstance(spid, six.binary_type):
            spid = spid.encode("ascii")
        os.write(fd, spid)
        os.close(fd)
        if not lock_path.is_file():
            raise EnvironmentError("lock path got renamed after sucessfull creation")
        return lock_path


def register_cleanup_lock_removal(lock_path, register=atexit.register):
    """registers a cleanup function for removing a lock, by default on atexit"""
    pid = os.getpid()

    def cleanup_on_exit(lock_path=lock_path, original_pid=pid):
        current_pid = os.getpid()
        if current_pid != original_pid:
            # fork
            return
        try:
            lock_path.unlink()
        except (OSError, IOError):
            pass

    return register(cleanup_on_exit)


def delete_a_numbered_dir(path):
    """removes a numbered directory"""
    create_cleanup_lock(path)
    parent = path.parent

    garbage = parent.joinpath("garbage-{}".format(uuid.uuid4()))
    path.rename(garbage)
    rmtree(garbage, force=True)


def ensure_deletable(path, consider_lock_dead_if_created_before):
    """checks if a lock exists and breaks it if its considered dead"""
    if path.is_symlink():
        return False
    lock = get_lock_path(path)
    if not lock.exists():
        return True
    try:
        lock_time = lock.stat().st_mtime
    except Exception:
        return False
    else:
        if lock_time < consider_lock_dead_if_created_before:
            lock.unlink()
            return True
        else:
            return False


def try_cleanup(path, consider_lock_dead_if_created_before):
    """tries to cleanup a folder if we can ensure its deletable"""
    if ensure_deletable(path, consider_lock_dead_if_created_before):
        delete_a_numbered_dir(path)


def cleanup_candidates(root, prefix, keep):
    """lists candidates for numbered directories to be removed - follows py.path"""
    max_existing = _max(map(parse_num, find_suffixes(root, prefix)), default=-1)
    max_delete = max_existing - keep
    paths = find_prefixed(root, prefix)
    paths, paths2 = itertools.tee(paths)
    numbers = map(parse_num, extract_suffixes(paths2, prefix))
    for path, number in zip(paths, numbers):
        if number <= max_delete:
            yield path


def cleanup_numbered_dir(root, prefix, keep, consider_lock_dead_if_created_before):
    """cleanup for lock driven numbered directories"""
    for path in cleanup_candidates(root, prefix, keep):
        try_cleanup(path, consider_lock_dead_if_created_before)
    for path in root.glob("garbage-*"):
        try_cleanup(path, consider_lock_dead_if_created_before)


def make_numbered_dir_with_cleanup(root, prefix, keep, lock_timeout):
    """creates a numbered dir with a cleanup lock and removes old ones"""
    e = None
    for i in range(10):
        try:
            p = make_numbered_dir(root, prefix)
            lock_path = create_cleanup_lock(p)
            register_cleanup_lock_removal(lock_path)
        except Exception as e:
            pass
        else:
            consider_lock_dead_if_created_before = p.stat().st_mtime - lock_timeout
            cleanup_numbered_dir(
                root=root,
                prefix=prefix,
                keep=keep,
                consider_lock_dead_if_created_before=consider_lock_dead_if_created_before,
            )
            return p
    assert e is not None
    raise e


def resolve_from_str(input, root):
    assert not isinstance(input, Path), "would break on py2"
    root = Path(root)
    input = expanduser(input)
    input = expandvars(input)
    if isabs(input):
        return Path(input)
    else:
        return root.joinpath(input)


def fnmatch_ex(pattern, path):
    """FNMatcher port from py.path.common which works with PurePath() instances.

    The difference between this algorithm and PurePath.match() is that the latter matches "**" glob expressions
    for each part of the path, while this algorithm uses the whole path instead.

    For example:
        "tests/foo/bar/doc/test_foo.py" matches pattern "tests/**/doc/test*.py" with this algorithm, but not with
        PurePath.match().

    This algorithm was ported to keep backward-compatibility with existing settings which assume paths match according
    this logic.

    References:
    * https://bugs.python.org/issue29249
    * https://bugs.python.org/issue34731
    """
    path = PurePath(path)
    iswin32 = sys.platform.startswith("win")

    if iswin32 and sep not in pattern and posix_sep in pattern:
        # Running on Windows, the pattern has no Windows path separators,
        # and the pattern has one or more Posix path separators. Replace
        # the Posix path separators with the Windows path separator.
        pattern = pattern.replace(posix_sep, sep)

    if sep not in pattern:
        name = path.name
    else:
        name = six.text_type(path)
    return fnmatch.fnmatch(name, pattern)
