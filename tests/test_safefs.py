import os
import tempfile
import unittest
from pathlib import Path

from dashboard import safefs
from dashboard.safefs import UnsafePath

ME, MY_GID = os.getuid(), os.getgid()
STRANGER = ME + 4242        # a uid that owns nothing here; passing it as "the user" makes every file look foreign


class HomeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = self.root / "home"
        self.home.mkdir(mode=0o750)
        os.chmod(self.home, 0o750)
        self.ssh = self.home / ".ssh"
        self.ssh.mkdir(mode=0o700)
        self.keys = self.ssh / "authorized_keys"
        self.keys.write_bytes(b"ssh-ed25519 AAAA test\n")
        os.chmod(self.keys, 0o600)

    def read(self, rel=".ssh/authorized_keys", uid=ME, home=None):
        return safefs.read_in_home(str(home or self.home), uid, rel)


class ReadTest(HomeTest):
    def test_reads_a_normal_file(self):
        self.assertEqual(self.read(), b"ssh-ed25519 AAAA test\n")

    def test_missing_file_or_folder_is_none(self):
        self.assertIsNone(self.read(".ssh/nothing"))
        self.assertIsNone(self.read(".nothing/authorized_keys"))

    def test_symlinked_folder_is_refused(self):
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir(mode=0o700)
        (elsewhere / "authorized_keys").write_bytes(b"x")
        self.ssh.rename(self.home / ".ssh-real")
        os.symlink(elsewhere, self.ssh)
        with self.assertRaisesRegex(UnsafePath, "symbolic link"):
            self.read()

    def test_symlinked_file_is_refused(self):
        secret = self.root / "secret"
        secret.write_bytes(b"root only")
        self.keys.unlink()
        os.symlink(secret, self.keys)
        with self.assertRaisesRegex(UnsafePath, "symbolic link"):
            self.read()

    def test_hard_linked_file_is_refused(self):
        os.link(self.keys, self.root / "other-name")
        with self.assertRaisesRegex(UnsafePath, "hard link"):
            self.read()

    def test_fifo_is_refused_without_blocking(self):
        self.keys.unlink()
        os.mkfifo(self.keys, 0o600)
        with self.assertRaisesRegex(UnsafePath, "not a regular file"):
            self.read()

    def test_a_folder_in_place_of_the_file_is_refused(self):
        self.keys.unlink()
        self.keys.mkdir()
        with self.assertRaisesRegex(UnsafePath, "not a regular file"):
            self.read()

    def test_file_or_folder_writable_by_others_is_refused(self):
        os.chmod(self.keys, 0o660)
        with self.assertRaisesRegex(UnsafePath, "written to by other"):
            self.read()
        os.chmod(self.keys, 0o600)
        os.chmod(self.ssh, 0o770)
        with self.assertRaisesRegex(UnsafePath, "written to by other"):
            self.read()
        os.chmod(self.ssh, 0o700)
        os.chmod(self.home, 0o777)
        with self.assertRaisesRegex(UnsafePath, "home folder can be written"):
            self.read()

    def test_files_of_another_user_are_refused(self):
        with self.assertRaisesRegex(UnsafePath, "owned by another user"):
            self.read(uid=STRANGER)

    def test_home_that_is_a_symlink_is_refused(self):
        link = self.root / "homelink"
        os.symlink(self.home, link)
        with self.assertRaisesRegex(UnsafePath, "home folder is a symbolic link"):
            self.read(home=link)

    def test_oversized_file_is_refused(self):
        self.keys.write_bytes(b"x" * 100)
        with self.assertRaisesRegex(UnsafePath, "larger than"):
            safefs.read_in_home(str(self.home), ME, ".ssh/authorized_keys", max_size=50)

    def test_bad_relative_paths_are_refused(self):
        for rel in ("", "/etc/shadow", "../x", ".ssh/../.ssh/authorized_keys", ".ssh//authorized_keys", "a\0b", ".", None):
            with self.assertRaises(UnsafePath, msg=repr(rel)):
                safefs.read_in_home(str(self.home), ME, rel)
        with self.assertRaises(UnsafePath):
            safefs.read_in_home("relative/home", ME, ".ssh/authorized_keys")

    def test_no_file_descriptor_leaks(self):
        before = len(os.listdir("/proc/self/fd"))
        for _ in range(20):
            self.read()
            self.read(".ssh/nothing")
            with self.assertRaises(UnsafePath):
                self.read(uid=STRANGER)
        self.assertEqual(len(os.listdir("/proc/self/fd")), before)


class WriteTest(HomeTest):
    def dir_fd(self):
        fd = safefs.open_in_home(str(self.home), ME, ".ssh", directory=True)
        self.addCleanup(os.close, fd)
        return fd

    def test_creates_and_replaces_with_the_given_mode(self):
        fd = self.dir_fd()
        safefs.atomic_write(fd, "authorized_keys", b"new\n", ME, MY_GID, 0o600)
        self.assertEqual(self.keys.read_bytes(), b"new\n")
        self.assertEqual(os.stat(self.keys).st_mode & 0o777, 0o600)
        safefs.atomic_write(fd, "fresh", b"x", ME, MY_GID, 0o640)
        self.assertEqual(os.stat(self.ssh / "fresh").st_mode & 0o777, 0o640)
        self.assertEqual(sorted(p.name for p in self.ssh.iterdir()), ["authorized_keys", "fresh"])      # no temp files left

    def test_replaces_a_symlink_instead_of_writing_through_it(self):
        target = self.root / "victim"
        target.write_bytes(b"precious")
        self.keys.unlink()
        os.symlink(target, self.keys)
        safefs.atomic_write(self.dir_fd(), "authorized_keys", b"new\n", ME, MY_GID, 0o600)
        self.assertEqual(target.read_bytes(), b"precious")
        self.assertFalse(self.keys.is_symlink())
        self.assertEqual(self.keys.read_bytes(), b"new\n")

    def test_breaks_a_hard_link_instead_of_writing_through_it(self):
        other = self.root / "hardlinked"
        os.link(self.keys, other)
        safefs.atomic_write(self.dir_fd(), "authorized_keys", b"new\n", ME, MY_GID, 0o600)
        self.assertEqual(other.read_bytes(), b"ssh-ed25519 AAAA test\n")
        self.assertEqual(self.keys.read_bytes(), b"new\n")

    def test_a_failed_write_leaves_the_old_file_and_no_temp_file(self):
        fd = self.dir_fd()
        with self.assertRaises(PermissionError):                       # a normal user may not give files away
            safefs.atomic_write(fd, "authorized_keys", b"new\n", STRANGER, MY_GID, 0o600)
        self.assertEqual(self.keys.read_bytes(), b"ssh-ed25519 AAAA test\n")
        self.assertEqual([p.name for p in self.ssh.iterdir()], ["authorized_keys"])

    def test_refuses_a_folder_and_bad_names(self):
        fd = self.dir_fd()
        (self.ssh / "sub").mkdir()
        with self.assertRaisesRegex(UnsafePath, "is a folder"):
            safefs.atomic_write(fd, "sub", b"x", ME, MY_GID, 0o600)
        for name in ("", ".", "..", "a/b", "a\0b"):
            with self.assertRaises(UnsafePath, msg=repr(name)):
                safefs.atomic_write(fd, name, b"x", ME, MY_GID, 0o600)

    def test_ensure_dir_creates_with_mode_and_reuses_safely(self):
        parent = os.open(self.home, os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(os.close, parent)
        fd = safefs.ensure_dir(parent, "newdir", ME, MY_GID, 0o700)
        os.close(fd)
        self.assertEqual(os.stat(self.home / "newdir").st_mode & 0o777, 0o700)
        os.close(safefs.ensure_dir(parent, "newdir", ME, MY_GID, 0o700))        # already there: fine
        os.symlink(self.root, self.home / "linked")
        with self.assertRaisesRegex(UnsafePath, "symbolic link"):
            safefs.ensure_dir(parent, "linked", ME, MY_GID)
        with self.assertRaises(UnsafePath):
            safefs.ensure_dir(parent, "a/b", ME, MY_GID)
        os.chmod(self.home / "newdir", 0o777)
        with self.assertRaisesRegex(UnsafePath, "written to by other"):
            safefs.ensure_dir(parent, "newdir", ME, MY_GID)


class BackupTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "backups"
        self.src = Path(self.tmp.name) / "app.conf"
        self.src.write_text("one\n")
        self.t = 1_700_000_000.0

    def make(self, **kw):
        return safefs.backup(str(self.src), self.root, now=lambda: self.t, **kw)

    def test_copies_the_file_privately_under_a_utc_name(self):
        path = self.make()
        self.assertEqual(path.read_text(), "one\n")
        self.assertEqual(path.name, "20231114T221320Z")
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(path.parent).st_mode & 0o777, 0o700)

    def test_two_backups_in_one_second_do_not_overwrite(self):
        first = self.make()
        self.src.write_text("two\n")
        second = self.make()
        self.assertNotEqual(first, second)
        self.assertEqual(first.read_text(), "one\n")
        self.assertEqual(second.read_text(), "two\n")

    def test_only_the_newest_are_kept(self):
        for i in range(7):
            self.t += 1
            self.src.write_text(f"v{i}\n")
            self.make(keep=3)
        listed = safefs.list_backups(str(self.src), self.root)
        self.assertEqual(len(listed), 3)
        self.assertEqual([Path(b["path"]).read_text() for b in listed], ["v6\n", "v5\n", "v4\n"])

    def test_different_paths_never_share_a_folder(self):
        a, b = Path(self.tmp.name) / "a_b", Path(self.tmp.name) / "a" / "b"
        b.parent.mkdir()
        a.write_text("A")
        b.write_text("B")
        self.assertNotEqual(safefs._backup_folder(str(a), self.root), safefs._backup_folder(str(b), self.root))
        self.assertNotEqual(safefs._backup_folder("/x/y%2Fz", self.root), safefs._backup_folder("/x/y/z", self.root))

    def test_list_for_a_file_without_backups_is_empty(self):
        self.assertEqual(safefs.list_backups(str(self.src), self.root), [])


if __name__ == "__main__":
    unittest.main()


class WritePrivateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def test_writes_an_owner_only_file_and_replaces_an_existing_one(self):
        target = self.dir / "state.json"
        safefs.write_private(target, b"one")
        safefs.write_private(target, b"two")
        self.assertEqual((target.read_bytes(), oct(target.stat().st_mode & 0o777)), (b"two", "0o600"))
        self.assertEqual([p.name for p in self.dir.iterdir()], ["state.json"])         # no temporary file is left behind

    def test_a_symlink_planted_at_the_target_or_a_guessable_temp_name_is_never_written_through(self):
        victim = self.dir / "victim"
        victim.write_text("precious")
        target = self.dir / "state.json"
        os.symlink(victim, target)
        os.symlink(victim, self.dir / "state.json.tmp")                 # the old, guessable temporary name
        safefs.write_private(target, b"new")
        self.assertEqual(victim.read_text(), "precious")
        self.assertFalse(target.is_symlink())
        self.assertEqual(target.read_bytes(), b"new")

    def test_a_failed_write_cleans_up(self):
        with self.assertRaises(OSError):
            safefs.write_private(self.dir / "missing-folder" / "x", b"data")
        self.assertEqual(list(self.dir.iterdir()), [])


class PrivateFilesDoNotFollowSymlinksTest(unittest.TestCase):
    def test_audit_and_history_refuse_a_symlinked_file(self):
        from dashboard.audit import Audit
        from dashboard.history import History
        with tempfile.TemporaryDirectory() as tmp:
            victim = Path(tmp) / "victim"
            victim.write_text("precious")
            os.symlink(victim, Path(tmp) / "audit.jsonl")
            Audit(Path(tmp) / "audit.jsonl").record("u", "1.2.3.4", "x", "y", True, "done")      # logged and swallowed, never written through
            self.assertEqual(victim.read_text(), "precious")
            os.symlink(victim, Path(tmp) / "history.db")
            with self.assertRaises(OSError):
                History(Path(tmp) / "history.db")
            self.assertEqual(victim.read_text(), "precious")
