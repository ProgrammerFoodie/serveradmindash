"""The only code that reads or writes files in places other users control (their home folders).

The dashboard runs as root, and a user can put anything in their own home folder: a symlink from
`~/.ssh` to `/etc`, a hard link from `authorized_keys` to `/etc/shadow`, a FIFO that blocks forever.
Reading or rewriting such a file as root would hand that user root's reach. So everything here:

* walks the path one component at a time with O_NOFOLLOW, relative to a directory file descriptor,
  so a symlink anywhere on the way is refused and a rename during the walk cannot redirect it;
* accepts only regular files owned by that user (or root) with a single hard link, and folders
  that nobody else can write to (the same rule as sshd's StrictModes);
* writes by creating a temporary file next to the target and renaming it over, never through the
  existing file, so a link in the target's place is replaced rather than written through.

Backups of root-owned config files live here too, because they share the careful-file-handling job.
"""

import errno
import os
import secrets
import stat
import time
from pathlib import Path

MAX_READ = 256 * 1024
BACKUP_KEEP = 20

_DIR = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


class UnsafePath(Exception):
    """The path is not something it is safe to touch as root. The message says why, for the person who sees it."""


def _parts(relpath: str) -> list[str]:
    if not isinstance(relpath, str) or not relpath or "\0" in relpath or relpath.startswith("/"):
        raise UnsafePath("not a relative path")
    parts = relpath.split("/")
    if any(p in ("", ".", "..") for p in parts):
        raise UnsafePath("path contains empty, '.' or '..' parts")
    return parts


def _owned(st: os.stat_result, uid: int) -> bool:
    return st.st_uid in (uid, 0)


def _check_dir(fd: int, uid: int, what: str) -> None:
    st = os.fstat(fd)
    if not _owned(st, uid):
        raise UnsafePath(f"{what} is owned by another user")
    if st.st_mode & 0o022:
        raise UnsafePath(f"{what} can be written to by other users")


def _open_dir_at(parent_fd: int, name: str, what: str) -> int:
    try:
        return os.open(name, _DIR, dir_fd=parent_fd)
    except OSError as e:
        if e.errno in (errno.ELOOP, errno.ENOTDIR):
            raise UnsafePath(f"{what} is a symbolic link or not a folder") from None
        raise


def open_in_home(home: str, uid: int, relpath: str, *, directory: bool = False, max_size: int = MAX_READ) -> int:
    """Open `relpath` inside `home` for reading and return the file descriptor (the caller closes it).

    Raises FileNotFoundError if something on the way is missing, UnsafePath if anything is not safe.
    With directory=True the last component is opened as a folder, for use with atomic_write().
    """
    parts = _parts(relpath)
    if not isinstance(home, str) or not home.startswith("/") or "\0" in home:
        raise UnsafePath("the home folder is not an absolute path")
    try:
        fd = os.open(home, _DIR)
    except OSError as e:
        if e.errno in (errno.ELOOP, errno.ENOTDIR):
            raise UnsafePath("the home folder is a symbolic link or not a folder") from None
        raise
    try:
        _check_dir(fd, uid, "the home folder")
        for i, name in enumerate(parts):
            last = i == len(parts) - 1
            if not last or directory:
                nxt = _open_dir_at(fd, name, f"'{name}'")
                os.close(fd)
                fd = nxt
                _check_dir(fd, uid, f"'{name}'")
                continue
            try:
                nxt = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=fd)
            except OSError as e:
                if e.errno == errno.ELOOP:
                    raise UnsafePath(f"'{name}' is a symbolic link") from None
                raise
            os.close(fd)
            fd = nxt
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                raise UnsafePath(f"'{name}' is not a regular file")
            if not _owned(st, uid):
                raise UnsafePath(f"'{name}' is owned by another user")
            if st.st_nlink != 1:
                raise UnsafePath(f"'{name}' has more than one hard link")
            if st.st_mode & 0o022:
                raise UnsafePath(f"'{name}' can be written to by other users")
            if st.st_size > max_size:
                raise UnsafePath(f"'{name}' is larger than {max_size} bytes")
        return fd
    except BaseException:
        os.close(fd)
        raise


def read_in_home(home: str, uid: int, relpath: str, max_size: int = MAX_READ) -> bytes | None:
    """The contents of a file inside a home folder, or None if it (or a folder on the way) does not exist."""
    try:
        fd = open_in_home(home, uid, relpath, max_size=max_size)
    except FileNotFoundError:
        return None
    try:
        chunks = []
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)
    finally:
        os.close(fd)


def ensure_dir(parent_fd: int, name: str, uid: int, gid: int, mode: int = 0o700) -> int:
    """Open the folder `name` under `parent_fd`, creating it (owned by uid:gid, with `mode`) if it is missing."""
    if "/" in name:
        raise UnsafePath("not a single folder name")
    _parts(name)
    created = False
    try:
        os.mkdir(name, 0o700, dir_fd=parent_fd)
        created = True
    except FileExistsError:
        pass
    fd = _open_dir_at(parent_fd, name, f"'{name}'")
    try:
        if created:
            os.fchown(fd, uid, gid)
            os.fchmod(fd, mode)
        else:
            _check_dir(fd, uid, f"'{name}'")
        return fd
    except BaseException:
        os.close(fd)
        raise


def atomic_write(dir_fd: int, name: str, data: bytes, uid: int, gid: int, mode: int) -> None:
    """Replace (or create) the file `name` in the folder `dir_fd` with `data`, owned by uid:gid, with `mode`.

    The data goes to a temporary file in the same folder first and is renamed over the target, so a reader
    sees the old or the new file, never a mix, and a symlink or hard link at `name` is replaced, not followed.
    """
    if not name or "/" in name or name in (".", "..") or "\0" in name:
        raise UnsafePath("not a plain file name")
    try:
        existing = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        if stat.S_ISDIR(existing.st_mode):
            raise UnsafePath(f"'{name}' is a folder")
    except FileNotFoundError:
        pass
    tmp = f".{name}.{secrets.token_hex(6)}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=dir_fd)
    try:
        try:
            os.fchown(fd, uid, gid)
            os.fchmod(fd, mode)
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view):]
            os.fsync(fd)
        finally:
            os.close(fd)
        os.rename(tmp, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
    except BaseException:
        try:
            os.unlink(tmp, dir_fd=dir_fd)
        except FileNotFoundError:
            pass
        raise
    os.fsync(dir_fd)


# ---- backups of config files -------------------------------------------------------------------------

def _backup_folder(path: str, root: Path) -> Path:
    # "/" and "%" are escaped so that two different paths can never share a folder.
    return Path(root) / path.strip("/").replace("%", "%25").replace("/", "%2F")


def backup(path: str, root: Path, keep: int = BACKUP_KEEP, now=time.time) -> Path:
    """Copy the file at `path` into `root` (mode 600) under a UTC timestamp and keep only the newest `keep` copies."""
    with open(path, "rb") as f:
        data = f.read()
    folder = _backup_folder(path, root)
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(folder, 0o700)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now()))
    target, n = folder / stamp, 0
    while True:
        try:
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            break
        except FileExistsError:
            n += 1
            target = folder / f"{stamp}-{n}"
    with os.fdopen(fd, "wb") as out:
        out.write(data)
        out.flush()
        os.fsync(out.fileno())
    for old in sorted(p for p in folder.iterdir() if p.is_file())[:-keep] if keep > 0 else []:
        old.unlink()
    return target


def list_backups(path: str, root: Path) -> list[dict]:
    """Backups of `path`, newest first: [{"name", "path", "size"}]."""
    folder = _backup_folder(path, root)
    if not folder.is_dir():
        return []
    return [{"name": p.name, "path": str(p), "size": p.stat().st_size} for p in sorted((p for p in folder.iterdir() if p.is_file()), reverse=True)]
