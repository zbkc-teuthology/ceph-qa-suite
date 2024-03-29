
from textwrap import dedent
from tasks.zbkcfs.zbkcfs_test_case import ZbkcFSTestCase
from tasks.zbkcfs.filesystem import ObjectNotFound, ROOT_INO


class TestFlush(ZbkcFSTestCase):
    def test_flush(self):
        self.mount_a.run_shell(["mkdir", "mydir"])
        self.mount_a.run_shell(["touch", "mydir/alpha"])
        dir_ino = self.mount_a.path_to_ino("mydir")
        file_ino = self.mount_a.path_to_ino("mydir/alpha")

        # Unmount the client so that it isn't still holding caps
        self.mount_a.umount_wait()

        # Before flush, the dirfrag object does not exist
        with self.assertRaises(ObjectNotFound):
            self.fs.list_dirfrag(dir_ino)

        # Before flush, the file's backtrace has not been written
        with self.assertRaises(ObjectNotFound):
            self.fs.read_backtrace(file_ino)

        # Before flush, there are no dentries in the root
        self.assertEqual(self.fs.list_dirfrag(ROOT_INO), [])

        # Execute flush
        flush_data = self.fs.mds_asok(["flush", "journal"])
        self.assertEqual(flush_data['return_code'], 0)

        # After flush, the dirfrag object has been created
        dir_list = self.fs.list_dirfrag(dir_ino)
        self.assertEqual(dir_list, ["alpha_head"])

        # And the 'mydir' dentry is in the root
        self.assertEqual(self.fs.list_dirfrag(ROOT_INO), ['mydir_head'])

        # ...and the data object has its backtrace
        backtrace = self.fs.read_backtrace(file_ino)
        self.assertEqual(['alpha', 'mydir'], [a['dname'] for a in backtrace['ancestors']])
        self.assertEqual([dir_ino, 1], [a['dirino'] for a in backtrace['ancestors']])
        self.assertEqual(file_ino, backtrace['ino'])

        # ...and the journal is truncated to just a single subtreemap from the
        # newly created segment
        summary_output = self.fs.journal_tool(["event", "get", "summary"])
        try:
            self.assertEqual(summary_output,
                             dedent(
                                 """
                                 Events by type:
                                   SUBTREEMAP: 1
                                 Errors: 0
                                 """
                             ).strip())
        except AssertionError:
            # In some states, flushing the journal will leave you
            # an extra event from locks a client held.   This is
            # correct behaviour: the MDS is flushing the journal,
            # it's just that new events are getting added too.
            # In this case, we should nevertheless see a fully
            # empty journal after a second flush.
            self.assertEqual(summary_output,
                             dedent(
                                 """
                                 Events by type:
                                   SUBTREEMAP: 1
                                   UPDATE: 1
                                 Errors: 0
                                 """
                             ).strip())
            flush_data = self.fs.mds_asok(["flush", "journal"])
            self.assertEqual(flush_data['return_code'], 0)
            self.assertEqual(self.fs.journal_tool(["event", "get", "summary"]),
                             dedent(
                                 """
                                 Events by type:
                                   SUBTREEMAP: 1
                                 Errors: 0
                                 """
                             ).strip())

        # Now for deletion!
        # We will count the RADOS deletions and MDS file purges, to verify that
        # the expected behaviour is happening as a result of the purge
        initial_dels = self.fs.mds_asok(['perf', 'dump', 'objecter'])['objecter']['osdop_delete']
        initial_purges = self.fs.mds_asok(['perf', 'dump', 'mds_cache'])['mds_cache']['strays_purged']

        # Use a client to delete a file
        self.mount_a.mount()
        self.mount_a.wait_until_mounted()
        self.mount_a.run_shell(["rm", "-rf", "mydir"])

        # Flush the journal so that the directory inode can be purged
        flush_data = self.fs.mds_asok(["flush", "journal"])
        self.assertEqual(flush_data['return_code'], 0)

        # We expect to see a single file purge
        self.wait_until_true(
            lambda: self.fs.mds_asok(['perf', 'dump', 'mds_cache'])['mds_cache']['strays_purged'] - initial_purges >= 2,
            60)

        # We expect two deletions, one of the dirfrag and one of the backtrace
        self.wait_until_true(
            lambda: self.fs.mds_asok(['perf', 'dump', 'objecter'])['objecter']['osdop_delete'] - initial_dels >= 2,
            60)  # timeout is fairly long to allow for tick+rados latencies

        with self.assertRaises(ObjectNotFound):
            self.fs.list_dirfrag(dir_ino)
        with self.assertRaises(ObjectNotFound):
            self.fs.read_backtrace(file_ino)
        self.assertEqual(self.fs.list_dirfrag(ROOT_INO), [])
