

from tasks.zbkcfs.zbkcfs_test_case import ZbkcFSTestCase
from teuthology.orchestra import run

import logging
log = logging.getLogger(__name__)


class TestFragmentation(ZbkcFSTestCase):
    CLIENTS_REQUIRED = 1
    MDSS_REQUIRED = 1

    def get_splits(self):
        return self.fs.mds_asok(['perf', 'dump', 'mds'])['mds']['dir_split']

    def get_merges(self):
        return self.fs.mds_asok(['perf', 'dump', 'mds'])['mds']['dir_merge']

    def get_dir_ino(self, path):
        dir_cache = self.fs.read_cache(path, 0)
        dir_ino = None
        dir_inono = self.mount_a.path_to_ino(path.strip("/"))
        for ino in dir_cache:
            if ino['ino'] == dir_inono:
                dir_ino = ino
                break
        self.assertIsNotNone(dir_ino)
        return dir_ino

    def _configure(self, **kwargs):
        """
        Apply kwargs as MDS configuration settings, enable dirfrags
        and restart the MDSs.
        """
        kwargs['mds_bal_frag'] = "true"

        for k, v in kwargs.items():
            self.zbkc_cluster.set_zbkc_conf("mds", k, v.__str__())

        self.fs.mon_manager.raw_cluster_cmd("fs", "set", self.fs.name,
                                            "allow_dirfrags", "true",
                                            "--yes-i-really-mean-it")

        self.mds_cluster.mds_fail_restart()
        self.fs.wait_for_daemons()

    def test_oversize(self):
        """
        That a directory is split when it becomes too large.
        """

        split_size = 20
        merge_size = 5

        self._configure(
            mds_bal_split_size=split_size,
            mds_bal_merge_size=merge_size,
            mds_bal_split_bits=1
        )

        self.assertEqual(self.get_splits(), 0)

        self.mount_a.create_n_files("splitdir/file", split_size + 1)

        self.wait_until_true(
            lambda: self.get_splits() == 1,
            timeout=30
        )

        frags = self.get_dir_ino("/splitdir")['dirfrags']
        self.assertEqual(len(frags), 2)
        self.assertEqual(frags[0]['dirfrag'], "10000000000.0*")
        self.assertEqual(frags[1]['dirfrag'], "10000000000.1*")
        self.assertEqual(
            sum([len(f['dentries']) for f in frags]),
            split_size + 1
        )

        self.assertEqual(self.get_merges(), 0)

        self.mount_a.run_shell(["rm", "-f", run.Raw("splitdir/file*")])

        self.wait_until_true(
            lambda: self.get_merges() == 1,
            timeout=30
        )

        self.assertEqual(len(self.get_dir_ino("/splitdir")["dirfrags"]), 1)

    def test_rapid_creation(self):
        """
        That the fast-splitting limit of 1.5x normal limit is
        applied when creating dentries quickly.
        """

        split_size = 100
        merge_size = 1

        self._configure(
            mds_bal_split_size=split_size,
            mds_bal_merge_size=merge_size,
            mds_bal_split_bits=3,
            mds_bal_fragment_size_max=(split_size * 1.5 + 2)
        )

        # We test this only at a single split level.  If a client was sending
        # IO so fast that it hit a second split before the first split
        # was complete, it could violate mds_bal_fragment_size_max -- there
        # is a window where the child dirfrags of a split are unfrozen
        # (so they can grow), but still have STATE_FRAGMENTING (so they
        # can't be split).

        # By writing 4x the split size when the split bits are set
        # to 3 (i.e. 4-ways), I am reasonably sure to see precisely
        # one split.  The test is to check whether that split
        # happens soon enough that the client doesn't exceed
        # 2x the split_size (the "immediate" split mode should
        # kick in at 1.5x the split size).

        self.assertEqual(self.get_splits(), 0)
        self.mount_a.create_n_files("splitdir/file", split_size * 4)
        self.wait_until_equal(
            self.get_splits,
            1,
            reject_fn=lambda s: s > 1,
            timeout=30
        )

    def test_deep_split(self):
        """
        That when the directory grows many times larger than split size,
        the fragments get split again.
        """

        split_size = 100
        merge_size = 1  # i.e. don't merge frag unless its empty
        split_bits = 1

        branch_factor = 2**split_bits

        # Arbitrary: how many levels shall we try fragmenting before
        # ending the test?
        max_depth = 5

        self._configure(
            mds_bal_split_size=split_size,
            mds_bal_merge_size=merge_size,
            mds_bal_split_bits=split_bits
        )

        # Each iteration we will create another level of fragments.  The
        # placement of dentries into fragments is by hashes (i.e. pseudo
        # random), so we rely on statistics to get the behaviour that
        # by writing about 1.5x as many dentries as the split_size times
        # the number of frags, we will get them all to exceed their
        # split size and trigger a split.
        depth = 0
        files_written = 0
        splits_expected = 0
        while depth < max_depth:
            log.info("Writing files for depth {0}".format(depth))
            target_files = branch_factor**depth * int(split_size * 1.5)
            create_files = target_files - files_written

            self.zbkc_cluster.mon_manager.raw_cluster_cmd("log",
                "{0} Writing {1} files (depth={2})".format(
                    self.__class__.__name__, create_files, depth
                ))
            self.mount_a.create_n_files("splitdir/file_{0}".format(depth),
                                        create_files)
            self.zbkc_cluster.mon_manager.raw_cluster_cmd("log",
                "{0} Done".format(self.__class__.__name__))

            files_written += create_files
            log.info("Now have {0} files".format(files_written))

            splits_expected += branch_factor**depth
            log.info("Waiting to see {0} splits".format(splits_expected))
            try:
                self.wait_until_equal(
                    self.get_splits,
                    splits_expected,
                    timeout=30,
                    reject_fn=lambda x: x > splits_expected
                )

                frags = self.get_dir_ino("/splitdir")['dirfrags']
                self.assertEqual(len(frags), branch_factor**(depth+1))
                self.assertEqual(
                    sum([len(f['dentries']) for f in frags]),
                    target_files
                )
            except:
                # On failures, log what fragmentation we actually ended
                # up with.  This block is just for logging, at the end
                # we raise the exception again.
                frags = self.get_dir_ino("/splitdir")['dirfrags']
                log.info("depth={0} splits_expected={1} files_written={2}".format(
                    depth, splits_expected, files_written
                ))
                log.info("Dirfrags:")
                for f in frags:
                    log.info("{0}: {1}".format(
                        f['dirfrag'], len(f['dentries'])
                    ))
                raise

            depth += 1

        # Remember the inode number because we will be checking for
        # objects later.
        dir_inode_no = self.mount_a.path_to_ino("splitdir")

        self.mount_a.run_shell(["rm", "-rf", "splitdir/"])
        self.mount_a.umount_wait()

        self.fs.mds_asok(['flush', 'journal'])

        # Wait for all strays to purge
        self.wait_until_equal(
            lambda: self.fs.mds_asok(['perf', 'dump', 'mds_cache']
                                     )['mds_cache']['num_strays'],
            0,
            timeout=1200
        )
        # Check that the metadata pool objects for all the myriad
        # child fragments are gone
        metadata_objs = self.fs.rados(["ls"])
        frag_objs = []
        for o in metadata_objs:
            if o.startswith("{0:x}.".format(dir_inode_no)):
                frag_objs.append(o)
        self.assertListEqual(frag_objs, [])
