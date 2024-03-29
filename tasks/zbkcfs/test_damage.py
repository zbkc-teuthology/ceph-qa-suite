import json
import logging
import errno
import re
from teuthology.contextutil import MaxWhileTries
from teuthology.exceptions import CommandFailedError
from teuthology.orchestra.run import wait
from tasks.zbkcfs.fuse_mount import FuseMount
from tasks.zbkcfs.zbkcfs_test_case import ZbkcFSTestCase, for_teuthology

DAMAGED_ON_START = "damaged_on_start"
DAMAGED_ON_LS = "damaged_on_ls"
CRASHED = "server crashed"
NO_DAMAGE = "no damage"
FAILED_CLIENT = "client failed"
FAILED_SERVER = "server failed"

# An EIO in response to a stat from the client
EIO_ON_LS = "eio"

# An EIO, but nothing in damage table (not ever what we expect)
EIO_NO_DAMAGE = "eio without damage entry"


log = logging.getLogger(__name__)


class TestDamage(ZbkcFSTestCase):
    def _simple_workload_write(self):
        self.mount_a.run_shell(["mkdir", "subdir"])
        self.mount_a.write_n_mb("subdir/sixmegs", 6)
        return self.mount_a.stat("subdir/sixmegs")

    def is_marked_damaged(self, rank):
        mds_map = self.fs.get_mds_map()
        return rank in mds_map['damaged']

    @for_teuthology #459s
    def test_object_deletion(self):
        """
        That the MDS has a clean 'damaged' response to loss of any single metadata object
        """

        self._simple_workload_write()

        # Hmm, actually it would be nice to permute whether the metadata pool
        # state contains sessions or not, but for the moment close this session
        # to avoid waiting through reconnect on every MDS start.
        self.mount_a.umount_wait()
        for mds_name in self.fs.get_active_names():
            self.fs.mds_asok(["flush", "journal"], mds_name)

        self.fs.mds_stop()
        self.fs.mds_fail()

        self.fs.rados(['export', '/tmp/metadata.bin'])

        def is_ignored(obj_id, dentry=None):
            """
            A filter to avoid redundantly mutating many similar objects (e.g.
            stray dirfrags) or similar dentries (e.g. stray dir dentries)
            """
            if re.match("60.\.00000000", obj_id) and obj_id != "600.00000000":
                return True

            if dentry and obj_id == "100.00000000":
                if re.match("stray.+_head", dentry) and dentry != "stray0_head":
                    return True

            return False

        def get_path(obj_id, dentry=None):
            """
            What filesystem path does this object or dentry correspond to?   i.e.
            what should I poke to see EIO after damaging it?
            """

            if obj_id == "1.00000000" and dentry == "subdir_head":
                return "./subdir"
            elif obj_id == "10000000000.00000000" and dentry == "sixmegs_head":
                return "./subdir/sixmegs"

            # None means ls will do an "ls -R" in hope of seeing some errors
            return None

        objects = self.fs.rados(["ls"]).split("\n")
        objects = [o for o in objects if not is_ignored(o)]

        # Find all objects with an OMAP header
        omap_header_objs = []
        for o in objects:
            header = self.fs.rados(["getomapheader", o])
            # The rados CLI wraps the header output in a hex-printed style
            header_bytes = int(re.match("header \((.+) bytes\)", header).group(1))
            if header_bytes > 0:
                omap_header_objs.append(o)

        # Find all OMAP key/vals
        omap_keys = []
        for o in objects:
            keys_str = self.fs.rados(["listomapkeys", o])
            if keys_str:
                for key in keys_str.split("\n"):
                    if not is_ignored(o, key):
                        omap_keys.append((o, key))

        # Find objects that have data in their bodies
        data_objects = []
        for obj_id in objects:
            stat_out = self.fs.rados(["stat", obj_id])
            size = int(re.match(".+, size (.+)$", stat_out).group(1))
            if size > 0:
                data_objects.append(obj_id)

        # Define the various forms of damage we will inflict
        class MetadataMutation(object):
            def __init__(self, obj_id_, desc_, mutate_fn_, expectation_, ls_path=None):
                self.obj_id = obj_id_
                self.desc = desc_
                self.mutate_fn = mutate_fn_
                self.expectation = expectation_
                if ls_path is None:
                    self.ls_path = "."
                else:
                    self.ls_path = ls_path

            def __eq__(self, other):
                return self.desc == other.desc

            def __hash__(self):
                return hash(self.desc)

        junk = "deadbeef" * 10
        mutations = []

        # Removals
        for obj_id in objects:
            if obj_id in [
                # JournalPointers are auto-replaced if missing (same path as upgrade)
                "400.00000000",
                # Missing dirfrags for non-system dirs result in empty directory
                "10000000000.00000000",
            ]:
                expectation = NO_DAMAGE
            else:
                expectation = DAMAGED_ON_START

            log.info("Expectation on rm '{0}' will be '{1}'".format(
                obj_id, expectation
            ))

            mutations.append(MetadataMutation(
                obj_id,
                "Delete {0}".format(obj_id),
                lambda o=obj_id: self.fs.rados(["rm", o]),
                expectation
            ))

        # Blatant corruptions
        mutations.extend([
            MetadataMutation(
                o,
                "Corrupt {0}".format(o),
                lambda o=o: self.fs.rados(["put", o, "-"], stdin_data=junk),
                DAMAGED_ON_START
            ) for o in data_objects
        ])

        # Truncations
        mutations.extend([
            MetadataMutation(
                o,
                "Truncate {0}".format(o),
                lambda o=o: self.fs.rados(["truncate", o, "0"]),
                DAMAGED_ON_START
            ) for o in data_objects
        ])

        # OMAP value corruptions
        for o, k in omap_keys:
            if o.startswith("100."):
                # Anything in rank 0's 'mydir'
                expectation = DAMAGED_ON_START
            else:
                expectation = EIO_ON_LS

            mutations.append(
                MetadataMutation(
                    o,
                    "Corrupt omap key {0}:{1}".format(o, k),
                    lambda o=o,k=k: self.fs.rados(["setomapval", o, k, junk]),
                    expectation,
                    get_path(o, k)
                )
            )

        # OMAP header corruptions
        for obj_id in omap_header_objs:
            if re.match("60.\.00000000", obj_id) \
                    or obj_id in ["1.00000000", "100.00000000", "mds0_sessionmap"]:
                expectation = DAMAGED_ON_START
            else:
                expectation = NO_DAMAGE

            log.info("Expectation on corrupt header '{0}' will be '{1}'".format(
                obj_id, expectation
            ))

            mutations.append(
                MetadataMutation(
                    obj_id,
                    "Corrupt omap header on {0}".format(obj_id),
                    lambda o=obj_id: self.fs.rados(["setomapheader", o, junk]),
                    expectation
                )
            )

        results = {}

        for mutation in mutations:
            log.info("Applying mutation '{0}'".format(mutation.desc))

            # Reset MDS state
            self.mount_a.umount_wait(force=True)
            self.fs.mds_stop()
            self.fs.mds_fail()
            self.fs.mon_manager.raw_cluster_cmd('mds', 'repaired', '0')

            # Reset RADOS pool state
            self.fs.rados(['import', '/tmp/metadata.bin'])

            # Inject the mutation
            mutation.mutate_fn()

            # Try starting the MDS
            self.fs.mds_restart()

            # How long we'll wait between starting a daemon and expecting
            # it to make it through startup, and potentially declare itself
            # damaged to the mon cluster.
            startup_timeout = 60

            if mutation.expectation not in (EIO_ON_LS, DAMAGED_ON_LS, NO_DAMAGE):
                if mutation.expectation == DAMAGED_ON_START:
                    # The MDS may pass through active before making it to damaged
                    try:
                        self.wait_until_true(lambda: self.is_marked_damaged(0), startup_timeout)
                    except RuntimeError:
                        pass

                # Wait for MDS to either come up or go into damaged state
                try:
                    self.wait_until_true(lambda: self.is_marked_damaged(0) or self.fs.are_daemons_healthy(), startup_timeout)
                except RuntimeError:
                    crashed = False
                    # Didn't make it to healthy or damaged, did it crash?
                    for daemon_id, daemon in self.fs.mds_daemons.items():
                        if daemon.proc and daemon.proc.finished:
                            crashed = True
                            log.error("Daemon {0} crashed!".format(daemon_id))
                            daemon.proc = None  # So that subsequent stop() doesn't raise error
                    if not crashed:
                        # Didn't go health, didn't go damaged, didn't crash, so what?
                        raise
                    else:
                        log.info("Result: Mutation '{0}' led to crash".format(mutation.desc))
                        results[mutation] = CRASHED
                        continue
                if self.is_marked_damaged(0):
                    log.info("Result: Mutation '{0}' led to DAMAGED state".format(mutation.desc))
                    results[mutation] = DAMAGED_ON_START
                    continue
                else:
                    log.info("Mutation '{0}' did not prevent MDS startup, attempting ls...".format(mutation.desc))
            else:
                try:
                    self.wait_until_true(self.fs.are_daemons_healthy, 60)
                except RuntimeError:
                    log.info("Result: Mutation '{0}' should have left us healthy, actually not.".format(mutation.desc))
                    if self.is_marked_damaged(0):
                        results[mutation] = DAMAGED_ON_START
                    else:
                        results[mutation] = FAILED_SERVER
                    continue
                log.info("Daemons came up after mutation '{0}', proceeding to ls".format(mutation.desc))

            # MDS is up, should go damaged on ls or client mount
            self.mount_a.mount()
            self.mount_a.wait_until_mounted()
            if mutation.ls_path == ".":
                proc = self.mount_a.run_shell(["ls", "-R", mutation.ls_path], wait=False)
            else:
                proc = self.mount_a.stat(mutation.ls_path, wait=False)

            if mutation.expectation == DAMAGED_ON_LS:
                try:
                    self.wait_until_true(lambda: self.is_marked_damaged(0), 60)
                    log.info("Result: Mutation '{0}' led to DAMAGED state after ls".format(mutation.desc))
                    results[mutation] = DAMAGED_ON_LS
                except RuntimeError:
                    if self.fs.are_daemons_healthy():
                        log.error("Result: Failed to go damaged on mutation '{0}', actually went active".format(
                            mutation.desc))
                        results[mutation] = NO_DAMAGE
                    else:
                        log.error("Result: Failed to go damaged on mutation '{0}'".format(mutation.desc))
                        results[mutation] = FAILED_SERVER

            else:
                try:
                    wait([proc], 20)
                    log.info("Result: Mutation '{0}' did not caused DAMAGED state".format(mutation.desc))
                    results[mutation] = NO_DAMAGE
                except MaxWhileTries:
                    log.info("Result: Failed to complete client IO on mutation '{0}'".format(mutation.desc))
                    results[mutation] = FAILED_CLIENT
                except CommandFailedError as e:
                    if e.exitstatus == errno.EIO:
                        log.info("Result: EIO on client")
                        results[mutation] = EIO_ON_LS
                    else:
                        log.info("Result: unexpected error {0} on client".format(e))
                        results[mutation] = FAILED_CLIENT

            if mutation.expectation == EIO_ON_LS:
                # EIOs mean something handled by DamageTable: assert that it has
                # been populated
                damage = json.loads(
                    self.fs.mon_manager.raw_cluster_cmd(
                        'tell', 'mds.{0}'.format(self.fs.get_active_names()[0]), "damage", "ls", '--format=json-pretty'))
                if len(damage) == 0:
                    results[mutation] = EIO_NO_DAMAGE

        failures = [(mutation, result) for (mutation, result) in results.items() if mutation.expectation != result]
        if failures:
            log.error("{0} mutations had unexpected outcomes:".format(len(failures)))
            for mutation, result in failures:
                log.error("  Expected '{0}' actually '{1}' from '{2}'".format(
                    mutation.expectation, result, mutation.desc
                ))
            raise RuntimeError("{0} mutations had unexpected outcomes".format(len(failures)))
        else:
            log.info("All {0} mutations had expected outcomes".format(len(mutations)))

    def test_damaged_dentry(self):
        # Damage to dentrys is interesting because it leaves the
        # directory's `complete` flag in a subtle state where
        # we have marked the dir complete in order that folks
        # can access it, but in actual fact there is a dentry
        # missing
        self.mount_a.run_shell(["mkdir", "subdir/"])

        self.mount_a.run_shell(["touch", "subdir/file_undamaged"])
        self.mount_a.run_shell(["touch", "subdir/file_to_be_damaged"])

        subdir_ino = self.mount_a.path_to_ino("subdir")

        self.mount_a.umount_wait()
        for mds_name in self.fs.get_active_names():
            self.fs.mds_asok(["flush", "journal"], mds_name)

        self.fs.mds_stop()
        self.fs.mds_fail()

        # Corrupt a dentry
        junk = "deadbeef" * 10
        dirfrag_obj = "{0:x}.00000000".format(subdir_ino)
        self.fs.rados(["setomapval", dirfrag_obj, "file_to_be_damaged_head", junk])

        # Start up and try to list it
        self.fs.mds_restart()
        self.fs.wait_for_daemons()

        self.mount_a.mount()
        self.mount_a.wait_until_mounted()
        dentries = self.mount_a.ls("subdir/")

        # The damaged guy should have disappeared
        self.assertEqual(dentries, ["file_undamaged"])

        # I should get ENOENT if I try and read it normally, because
        # the dir is considered complete
        try:
            self.mount_a.stat("subdir/file_to_be_damaged", wait=True)
        except CommandFailedError as e:
            self.assertEqual(e.exitstatus, errno.ENOENT)
        else:
            raise AssertionError("Expected ENOENT")

        # The fact that there is damaged should have bee recorded
        damage = json.loads(
            self.fs.mon_manager.raw_cluster_cmd(
                'tell', 'mds.{0}'.format(self.fs.get_active_names()[0]),
                "damage", "ls", '--format=json-pretty'))
        self.assertEqual(len(damage), 1)
        damage_id = damage[0]['id']

        # If I try to create a dentry with the same name as the damaged guy
        # then that should be forbidden
        try:
            self.mount_a.touch("subdir/file_to_be_damaged")
        except CommandFailedError as e:
            self.assertEqual(e.exitstatus, errno.EIO)
        else:
            raise AssertionError("Expected EIO")

        # Attempting that touch will clear the client's complete flag, now
        # when I stat it I'll get EIO instead of ENOENT
        try:
            self.mount_a.stat("subdir/file_to_be_damaged", wait=True)
        except CommandFailedError as e:
            if isinstance(self.mount_a, FuseMount):
                self.assertEqual(e.exitstatus, errno.EIO)
            else:
                # Kernel client handles this case differently
                self.assertEqual(e.exitstatus, errno.ENOENT)
        else:
            raise AssertionError("Expected EIO")

        nfiles = self.mount_a.getfattr("./subdir", "zbkc.dir.files")
        self.assertEqual(nfiles, "2")

        self.mount_a.umount_wait()

        # Now repair the stats
        scrub_json = self.fs.mds_asok(["scrub_path", "/subdir", "repair"])
        log.info(json.dumps(scrub_json, indent=2))

        self.assertEqual(scrub_json["passed_validation"], False)
        self.assertEqual(scrub_json["raw_stats"]["checked"], True)
        self.assertEqual(scrub_json["raw_stats"]["passed"], False)

        # Check that the file count is now correct
        self.mount_a.mount()
        self.mount_a.wait_until_mounted()
        nfiles = self.mount_a.getfattr("./subdir", "zbkc.dir.files")
        self.assertEqual(nfiles, "1")

        # Clean up the omap object
        self.fs.rados(["setomapval", dirfrag_obj, "file_to_be_damaged_head", junk])

        # Clean up the damagetable entry
        self.fs.mon_manager.raw_cluster_cmd(
            'tell', 'mds.{0}'.format(self.fs.get_active_names()[0]),
            "damage", "rm", "{did}".format(did=damage_id))

        # Now I should be able to create a file with the same name as the
        # damaged guy if I want.
        self.mount_a.touch("subdir/file_to_be_damaged")
