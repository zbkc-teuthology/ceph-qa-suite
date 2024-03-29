

import json
import logging
import os
from textwrap import dedent
import time
from teuthology.orchestra.run import CommandFailedError
from tasks.zbkcfs.fuse_mount import FuseMount
from tasks.zbkcfs.zbkcfs_test_case import ZbkcFSTestCase


log = logging.getLogger(__name__)


class FullnessTestCase(ZbkcFSTestCase):
    CLIENTS_REQUIRED = 2

    # Subclasses define whether they're filling whole cluster or just data pool
    data_only = False

    # Subclasses define how many bytes should be written to achieve fullness
    pool_capacity = None
    fill_mb = None

    # Subclasses define what fullness means to them
    def is_full(self):
        raise NotImplementedError()

    def setUp(self):
        ZbkcFSTestCase.setUp(self)

        if not isinstance(self.mount_a, FuseMount):
            self.skipTest("FUSE needed: ENOSPC handling in kclient is tracker #17204")

        # These tests just use a single active MDS throughout, so remember its ID
        # for use in mds_asok calls
        self.active_mds_id = self.fs.get_active_names()[0]

        # Capture the initial OSD map epoch for later use
        self.initial_osd_epoch = json.loads(
            self.fs.mon_manager.raw_cluster_cmd("osd", "dump", "--format=json").strip()
        )['epoch']

        # Check the initial barrier epoch on the MDS: this should be
        # set to the latest map at MDS startup.  We do this check in
        # setUp to get in there before subclasses might touch things
        # in their own setUp functions.
        self.assertGreaterEqual(self.fs.mds_asok(["status"], mds_id=self.active_mds_id)['osdmap_epoch_barrier'],
                                self.initial_osd_epoch)

    def test_barrier(self):
        """
        That when an OSD epoch barrier is set on an MDS, subsequently
        issued capabilities cause clients to update their OSD map to that
        epoch.
        """

        # Sync up clients with initial MDS OSD map barrier
        self.mount_a.open_no_data("foo")
        self.mount_b.open_no_data("bar")

        # Grab mounts' initial OSD epochs: later we will check that
        # it hasn't advanced beyond this point.
        mount_a_initial_epoch = self.mount_a.get_osd_epoch()[0]
        mount_b_initial_epoch = self.mount_b.get_osd_epoch()[0]

        # Freshly mounted at start of test, should be up to date with OSD map
        self.assertGreaterEqual(mount_a_initial_epoch, self.initial_osd_epoch)
        self.assertGreaterEqual(mount_b_initial_epoch, self.initial_osd_epoch)

        # Set and unset a flag to cause OSD epoch to increment
        self.fs.mon_manager.raw_cluster_cmd("osd", "set", "pause")
        self.fs.mon_manager.raw_cluster_cmd("osd", "unset", "pause")

        out = self.fs.mon_manager.raw_cluster_cmd("osd", "dump", "--format=json").strip()
        new_epoch = json.loads(out)['epoch']
        self.assertNotEqual(self.initial_osd_epoch, new_epoch)

        # Do a metadata operation on clients, witness that they end up with
        # the old OSD map from startup time (nothing has prompted client
        # to update its map)
        self.mount_a.open_no_data("alpha")
        self.mount_b.open_no_data("bravo1")

        # Sleep long enough that if the OSD map was propagating it would
        # have done so (this is arbitrary because we are 'waiting' for something
        # to *not* happen).
        time.sleep(30)

        mount_a_epoch, mount_a_barrier = self.mount_a.get_osd_epoch()
        self.assertEqual(mount_a_epoch, mount_a_initial_epoch)
        mount_b_epoch, mount_b_barrier = self.mount_b.get_osd_epoch()
        self.assertEqual(mount_b_epoch, mount_b_initial_epoch)

        # Set a barrier on the MDS
        self.fs.mds_asok(["osdmap", "barrier", new_epoch.__str__()], mds_id=self.active_mds_id)

        # Do an operation on client B, witness that it ends up with
        # the latest OSD map from the barrier.  This shouldn't generate any
        # cap revokes to A because B was already the last one to touch
        # a file in root.
        self.mount_b.run_shell(["touch", "bravo2"])
        self.mount_b.open_no_data("bravo2")

        # Some time passes here because the metadata part of the operation
        # completes immediately, while the resulting OSD map update happens
        # asynchronously (it's an Objecter::_maybe_request_map) as a result
        # of seeing the new epoch barrier.
        self.wait_until_equal(
            lambda: self.mount_b.get_osd_epoch(),
            (new_epoch, new_epoch),
            30,
            lambda x: x[0] > new_epoch or x[1] > new_epoch)

        # ...and none of this should have affected the oblivious mount a,
        # because it wasn't doing any data or metadata IO
        mount_a_epoch, mount_a_barrier = self.mount_a.get_osd_epoch()
        self.assertEqual(mount_a_epoch, mount_a_initial_epoch)

    def _data_pool_name(self):
        data_pool_names = self.fs.get_data_pool_names()
        if len(data_pool_names) > 1:
            raise RuntimeError("This test can't handle multiple data pools")
        else:
            return data_pool_names[0]

    def _test_full(self, easy_case):
        """
        - That a client trying to write data to a file is prevented
        from doing so with an -EFULL result
        - That they are also prevented from creating new files by the MDS.
        - That they may delete another file to get the system healthy again

        :param easy_case: if true, delete a successfully written file to
                          free up space.  else, delete the file that experienced
                          the failed write.
        """

        osd_mon_report_interval_max = int(self.fs.get_config("osd_mon_report_interval_max", service_type='osd'))

        log.info("Writing {0}MB should fill this cluster".format(self.fill_mb))

        # Fill up the cluster.  This dd may or may not fail, as it depends on
        # how soon the cluster recognises its own fullness
        self.mount_a.write_n_mb("large_file_a", self.fill_mb / 2)
        try:
            self.mount_a.write_n_mb("large_file_b", self.fill_mb / 2)
        except CommandFailedError:
            log.info("Writing file B failed (full status happened already)")
            assert self.is_full()
        else:
            log.info("Writing file B succeeded (full status will happen soon)")
            self.wait_until_true(lambda: self.is_full(),
                                 timeout=osd_mon_report_interval_max * 5)

        # Attempting to write more data should give me ENOSPC
        with self.assertRaises(CommandFailedError) as ar:
            self.mount_a.write_n_mb("large_file_b", 50, seek=self.fill_mb / 2)
        self.assertEqual(ar.exception.exitstatus, 1)  # dd returns 1 on "No space"

        # Wait for the MDS to see the latest OSD map so that it will reliably
        # be applying the policy of rejecting non-deletion metadata operations
        # while in the full state.
        osd_epoch = json.loads(self.fs.mon_manager.raw_cluster_cmd("osd", "dump", "--format=json-pretty"))['epoch']
        self.wait_until_true(
            lambda: self.fs.mds_asok(['status'], mds_id=self.active_mds_id)['osdmap_epoch'] >= osd_epoch,
            timeout=10)

        if not self.data_only:
            with self.assertRaises(CommandFailedError):
                self.mount_a.write_n_mb("small_file_1", 0)

        # Clear out some space
        if easy_case:
            self.mount_a.run_shell(['rm', '-f', 'large_file_a'])
            self.mount_a.run_shell(['rm', '-f', 'large_file_b'])
        else:
            # In the hard case it is the file that filled the system.
            # Before the new #7317 (ENOSPC, epoch barrier) changes, this
            # would fail because the last objects written would be
            # stuck in the client cache as objecter operations.
            self.mount_a.run_shell(['rm', '-f', 'large_file_b'])
            self.mount_a.run_shell(['rm', '-f', 'large_file_a'])

        # Here we are waiting for two things to happen:
        # * The MDS to purge the stray folder and execute object deletions
        #  * The OSDs to inform the mon that they are no longer full
        self.wait_until_true(lambda: not self.is_full(),
                             timeout=osd_mon_report_interval_max * 5)

        # Wait for the MDS to see the latest OSD map so that it will reliably
        # be applying the free space policy
        osd_epoch = json.loads(self.fs.mon_manager.raw_cluster_cmd("osd", "dump", "--format=json-pretty"))['epoch']
        self.wait_until_true(
            lambda: self.fs.mds_asok(['status'], mds_id=self.active_mds_id)['osdmap_epoch'] >= osd_epoch,
            timeout=10)

        # Now I should be able to write again
        self.mount_a.write_n_mb("large_file", 50, seek=0)

        # Ensure that the MDS keeps its OSD epoch barrier across a restart

    def test_full_different_file(self):
        self._test_full(True)

    def test_full_same_file(self):
        self._test_full(False)

    def _remote_write_test(self, template):
        """
        Run some remote python in a way that's useful for
        testing free space behaviour (see test_* methods using this)
        """
        file_path = os.path.join(self.mount_a.mountpoint, "full_test_file")

        # Enough to trip the full flag
        osd_mon_report_interval_max = int(self.fs.get_config("osd_mon_report_interval_max", service_type='osd'))
        mon_tick_interval = int(self.fs.get_config("mon_tick_interval", service_type="mon"))

        # Sufficient data to cause RADOS cluster to go 'full'
        log.info("pool capacity {0}, {1}MB should be enough to fill it".format(self.pool_capacity, self.fill_mb))

        # Long enough for RADOS cluster to notice it is full and set flag on mons
        # (report_interval for mon to learn PG stats, tick interval for it to update OSD map,
        #  factor of 1.5 for I/O + network latency in committing OSD map and distributing it
        #  to the OSDs)
        full_wait = (osd_mon_report_interval_max + mon_tick_interval) * 1.5

        # Configs for this test should bring this setting down in order to
        # run reasonably quickly
        if osd_mon_report_interval_max > 10:
            log.warn("This test may run rather slowly unless you decrease"
                     "osd_mon_report_interval_max (5 is a good setting)!")

        self.mount_a.run_python(template.format(
            fill_mb=self.fill_mb,
            file_path=file_path,
            full_wait=full_wait
        ))

    def test_full_fclose(self):
        # A remote script which opens a file handle, fills up the filesystem, and then
        # checks that ENOSPC errors on buffered writes appear correctly as errors in fsync
        remote_script = dedent("""
            import time
            import datetime
            import subprocess
            import os

            # Write some buffered data through before going full, all should be well
            print "writing some data through which we expect to succeed"
            bytes = 0
            f = os.open("{file_path}", os.O_WRONLY | os.O_CREAT)
            bytes += os.write(f, 'a' * 4096)
            os.fsync(f)
            print "fsync'ed data successfully, will now attempt to fill fs"

            # Okay, now we're going to fill up the filesystem, and then keep
            # writing until we see an error from fsync.  As long as we're doing
            # buffered IO, the error should always only appear from fsync and not
            # from write
            full = False

            for n in range(0, {fill_mb}):
                bytes += os.write(f, 'x' * 1024 * 1024)
                print "wrote bytes via buffered write, may repeat"
            print "done writing bytes"

            # OK, now we should sneak in under the full condition
            # due to the time it takes the OSDs to report to the
            # mons, and get a successful fsync on our full-making data
            os.fsync(f)
            print "successfully fsync'ed prior to getting full state reported"

            # Now wait for the full flag to get set so that our
            # next flush IO will fail
            time.sleep(30)

            # A buffered IO, should succeed
            print "starting buffered write we expect to succeed"
            os.write(f, 'x' * 4096)
            print "wrote, now waiting 30s and then doing a close we expect to fail"

            # Wait long enough for a background flush that should fail
            time.sleep(30)

            # ...and check that the failed background flush is reflected in fclose
            try:
                os.close(f)
            except OSError:
                print "close() returned an error as expected"
            else:
                raise RuntimeError("close() failed to raise error")

            os.unlink("{file_path}")
            """)
        self._remote_write_test(remote_script)

    def test_full_fsync(self):
        """
        That when the full flag is encountered during asynchronous
        flushes, such that an fwrite() succeeds but an fsync/fclose()
        should return the ENOSPC error.
        """

        # A remote script which opens a file handle, fills up the filesystem, and then
        # checks that ENOSPC errors on buffered writes appear correctly as errors in fsync
        remote_script = dedent("""
            import time
            import datetime
            import subprocess
            import os

            # Write some buffered data through before going full, all should be well
            print "writing some data through which we expect to succeed"
            bytes = 0
            f = os.open("{file_path}", os.O_WRONLY | os.O_CREAT)
            bytes += os.write(f, 'a' * 4096)
            os.fsync(f)
            print "fsync'ed data successfully, will now attempt to fill fs"

            # Okay, now we're going to fill up the filesystem, and then keep
            # writing until we see an error from fsync.  As long as we're doing
            # buffered IO, the error should always only appear from fsync and not
            # from write
            full = False

            for n in range(0, {fill_mb} + 1):
                try:
                    bytes += os.write(f, 'x' * 1024 * 1024)
                    print "wrote bytes via buffered write, moving on to fsync"
                except OSError as e:
                    print "Unexpected error %s from write() instead of fsync()" % e
                    raise

                try:
                    os.fsync(f)
                    print "fsync'ed successfully"
                except OSError as e:
                    print "Reached fullness after %.2f MB" % (bytes / (1024.0 * 1024.0))
                    full = True
                    break
                else:
                    print "Not full yet after %.2f MB" % (bytes / (1024.0 * 1024.0))

                if n > {fill_mb} * 0.8:
                    # Be cautious in the last region where we expect to hit
                    # the full condition, so that we don't overshoot too dramatically
                    print "sleeping a bit as we've exceeded 80% of our expected full ratio"
                    time.sleep({full_wait})

            if not full:
                raise RuntimeError("Failed to reach fullness after writing %d bytes" % bytes)

            # The error sticks to the inode until we dispose of it
            try:
                os.close(f)
            except OSError:
                print "Saw error from close() as expected"
            else:
                raise RuntimeError("Did not see expected error from close()")

            os.unlink("{file_path}")
            """)

        self._remote_write_test(remote_script)


class TestQuotaFull(FullnessTestCase):
    """
    Test per-pool fullness, which indicates quota limits exceeded
    """
    pool_capacity = 1024 * 1024 * 32   # arbitrary low-ish limit
    fill_mb = pool_capacity / (1024 * 1024)

    # We are only testing quota handling on the data pool, not the metadata
    # pool.
    data_only = True

    def setUp(self):
        super(TestQuotaFull, self).setUp()

        pool_name = self.fs.get_data_pool_name()
        self.fs.mon_manager.raw_cluster_cmd("osd", "pool", "set-quota", pool_name,
                                            "max_bytes", "{0}".format(self.pool_capacity))

    def is_full(self):
        return self.fs.is_pool_full(self.fs.get_data_pool_name())


class TestClusterFull(FullnessTestCase):
    """
    Test cluster-wide fullness, which indicates that an OSD has become too full
    """
    pool_capacity = None
    REQUIRE_MEMSTORE = True

    def setUp(self):
        super(TestClusterFull, self).setUp()

        if self.pool_capacity is None:
            # This is a hack to overcome weird fluctuations in the reported
            # `max_avail` attribute of pools that sometimes occurs in between
            # tests (reason as yet unclear, but this dodges the issue)
            TestClusterFull.pool_capacity = self.fs.get_pool_df(self._data_pool_name())['max_avail']
            mon_osd_full_ratio = float(self.fs.get_config("mon_osd_full_ratio"))
            TestClusterFull.fill_mb = int(1.05 * mon_osd_full_ratio * (self.pool_capacity / (1024.0 * 1024.0)))

    def is_full(self):
        return self.fs.is_full()

# Hide the parent class so that unittest.loader doesn't try to run it.
del globals()['FullnessTestCase']
