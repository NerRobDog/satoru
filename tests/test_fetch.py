"""Getting a pack onto the disk, and being sure it is the pack.

Three things have to hold. A download that does not match its hash must leave
nothing a later run could mistake for a good file. An archive is data from the
internet, so a member named ../../../etc/something is an attack and not a file.
And a pack already in the cache is not downloaded twice — 133 MB is not a thing
to fetch again because the launcher restarted.
"""
import hashlib
import os
import shutil
import tarfile
import tempfile
import unittest

from support import satoru


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def make_tarball(directory, name="pack.tar.gz", members=(("setup.sh", "#!/bin/sh\n"),)):
    path = os.path.join(directory, name)
    stage = tempfile.mkdtemp()
    try:
        for rel, body in members:
            full = os.path.join(stage, rel)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "w") as fh:
                fh.write(body)
        with tarfile.open(path, "w:gz") as tf:
            tf.add(stage, arcname="pack")
    finally:
        shutil.rmtree(stage, ignore_errors=True)
    return path


class Verify(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_a_matching_hash_passes(self):
        p = make_tarball(self.dir)
        self.assertTrue(satoru.verify_sha256(p, sha256_of(p)))

    def test_a_wrong_hash_fails(self):
        p = make_tarball(self.dir)
        self.assertFalse(satoru.verify_sha256(p, "0" * 64))

    def test_case_does_not_matter(self):
        p = make_tarball(self.dir)
        self.assertTrue(satoru.verify_sha256(p, sha256_of(p).upper()))


class Fetch(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.cache = os.path.join(self.dir, "cache")
        self.origin = make_tarball(self.dir, "origin.tar.gz")
        self.sha = sha256_of(self.origin)
        self.source = {"kind": "release", "url": "file://" + self.origin,
                       "sha256": self.sha, "version": "v0.1"}

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_downloads_and_returns_the_file(self):
        got = satoru.fetch_pack(self.source, self.cache)
        self.assertTrue(os.path.isfile(got))
        self.assertEqual(sha256_of(got), self.sha)

    def test_a_hash_mismatch_leaves_nothing_behind(self):
        bad = dict(self.source, sha256="0" * 64)
        self.assertRaises(satoru.PackError, satoru.fetch_pack, bad, self.cache)
        leftovers = os.listdir(self.cache) if os.path.isdir(self.cache) else []
        self.assertEqual(leftovers, [],
                         "a failed download must not leave a file a later run would trust")

    def test_a_cached_pack_is_not_downloaded_again(self):
        first = satoru.fetch_pack(self.source, self.cache)
        os.remove(self.origin)  # the network is now gone
        second = satoru.fetch_pack(self.source, self.cache)
        self.assertEqual(first, second)

    def test_a_cached_file_that_no_longer_matches_is_refetched(self):
        cached = satoru.fetch_pack(self.source, self.cache)
        with open(cached, "wb") as fh:
            fh.write(b"corrupted")
        again = satoru.fetch_pack(self.source, self.cache)
        self.assertEqual(sha256_of(again), self.sha)


class Unpack(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _stage(self):
        """A pack directory to build archives out of, with one file in it."""
        stage = os.path.join(self.dir, "stage")
        if not os.path.isdir(stage):
            os.makedirs(stage)
            with open(os.path.join(stage, "setup.sh"), "w") as fh:
                fh.write("#!/bin/sh\n")
        return stage

    def test_unpacks_into_the_destination(self):
        p = make_tarball(self.dir)
        dest = os.path.join(self.dir, "out")
        root = satoru.unpack(p, dest)
        self.assertTrue(os.path.isfile(os.path.join(root, "setup.sh")))

    def test_a_member_that_climbs_out_is_refused(self):
        path = os.path.join(self.dir, "evil.tar.gz")
        victim = os.path.join(self.dir, "victim.txt")
        with tarfile.open(path, "w:gz") as tf:
            info = tarfile.TarInfo("../victim.txt")
            data = b"owned"
            info.size = len(data)
            import io
            tf.addfile(info, io.BytesIO(data))
        self.assertRaises(satoru.PackError, satoru.unpack, path,
                          os.path.join(self.dir, "out"))
        self.assertFalse(os.path.exists(victim), "the archive escaped its destination")

    def test_an_absolute_member_is_refused(self):
        path = os.path.join(self.dir, "abs.tar.gz")
        with tarfile.open(path, "w:gz") as tf:
            info = tarfile.TarInfo("/tmp/satoru-should-never-exist")
            info.size = 0
            import io
            tf.addfile(info, io.BytesIO(b""))
        self.assertRaises(satoru.PackError, satoru.unpack, path,
                          os.path.join(self.dir, "out"))

    def test_a_symlink_pointing_out_is_refused(self):
        path = os.path.join(self.dir, "link.tar.gz")
        with tarfile.open(path, "w:gz") as tf:
            info = tarfile.TarInfo("pack/escape")
            info.type = tarfile.SYMTYPE
            info.linkname = "/etc/passwd"
            tf.addfile(info)
        self.assertRaises(satoru.PackError, satoru.unpack, path,
                          os.path.join(self.dir, "out"))

    def test_apple_double_files_do_not_hide_the_packs_root(self):
        """A tarball built on macOS carries `._name` siblings whenever the files
        had extended attributes, and our own releases are built on macOS. Counting
        those as a second top-level entry left every pack command running one
        directory too high, where `bash setup.sh` is `No such file or directory`.
        """
        import io
        path = os.path.join(self.dir, "appledouble.tar.gz")
        with tarfile.open(path, "w:gz") as tf:
            tf.add(self._stage(), arcname="pack")
            for junk in ("._pack", ".DS_Store"):
                info = tarfile.TarInfo(junk)
                data = b"\x00\x05\x16\x07"
                info.size = len(data)
                tf.addfile(info, io.BytesIO(data))
        root = satoru.unpack(path, os.path.join(self.dir, "out"))
        self.assertTrue(os.path.isfile(os.path.join(root, "setup.sh")),
                        "the pack root is the directory, not what sits beside it")

    def test_two_real_directories_are_left_alone(self):
        # Only macOS metadata is ignored; a genuinely multi-rooted archive still
        # unpacks to the destination itself.
        path = os.path.join(self.dir, "two.tar.gz")
        stage = self._stage()
        with tarfile.open(path, "w:gz") as tf:
            tf.add(stage, arcname="one")
            tf.add(stage, arcname="two")
        dest = os.path.join(self.dir, "out2")
        self.assertEqual(satoru.unpack(path, dest), dest)

    def test_unpacking_twice_replaces_rather_than_merges(self):
        dest = os.path.join(self.dir, "out")
        first = satoru.unpack(make_tarball(self.dir, "a.tar.gz"), dest)
        with open(os.path.join(first, "stale.txt"), "w") as fh:
            fh.write("left over from an older pack")
        second = satoru.unpack(make_tarball(self.dir, "b.tar.gz"), dest)
        self.assertFalse(os.path.exists(os.path.join(second, "stale.txt")))


if __name__ == "__main__":
    unittest.main()
