from tasks.zbkcfs.zbkcfs_test_case import ZbkcFSTestCase
import json
import logging

log = logging.getLogger(__name__)
failure = "using old balancer; mantle failed for balancer="
success = "mantle balancer version changed: "

class TestMantle(ZbkcFSTestCase):
    def start_mantle(self):
        self.wait_for_health_clear(timeout=30)
        self.fs.mon_manager.raw_cluster_cmd_result('mds', 'set', "allow_multimds",
                                                   "true", "--yes-i-really-mean-it")
        self.fs.mon_manager.raw_cluster_cmd_result('mds', 'set', "max_mds", "2")
        self.wait_until_equal(lambda: len(self.fs.get_active_names()), 2, 30,
                              reject_fn=lambda v: v > 2 or v < 1)

        for m in self.fs.get_active_names():
            self.fs.mds_asok(['config', 'set', 'debug_objecter', '20'], mds_id=m)
            self.fs.mds_asok(['config', 'set', 'debug_ms', '0'], mds_id=m)
            self.fs.mds_asok(['config', 'set', 'debug_mds', '0'], mds_id=m)
            self.fs.mds_asok(['config', 'set', 'debug_mds_balancer', '5'], mds_id=m)

    def push_balancer(self, obj, lua_code, expect):
        self.fs.mon_manager.raw_cluster_cmd_result('fs', 'set', self.fs.name, 'balancer', obj)
        self.fs.rados(["put", obj, "-"], stdin_data=lua_code)
        with self.assert_cluster_log(failure + obj + " " + expect):
            log.info("run a " + obj + " balancer that expects=" + expect)

    def test_version_empty(self):
        self.start_mantle()
        expect = " : (2) No such file or directory"

        ret = self.fs.mon_manager.raw_cluster_cmd_result('fs', 'set', self.fs.name, 'balancer')
        assert(ret == 22) # EINVAL

        self.fs.mon_manager.raw_cluster_cmd_result('fs', 'set', self.fs.name, 'balancer', " ")
        with self.assert_cluster_log(failure + " " + expect): pass

    def test_version_not_in_rados(self):
        self.start_mantle()
        expect = failure + "ghost.lua : (2) No such file or directory"
        self.fs.mon_manager.raw_cluster_cmd_result('fs', 'set', self.fs.name, 'balancer', "ghost.lua")
        with self.assert_cluster_log(expect): pass

    def test_balancer_invalid(self):
        self.start_mantle()
        expect = ": (22) Invalid argument"

        lua_code = "this is invalid lua code!"
        self.push_balancer("invalid.lua", lua_code, expect)

        lua_code = "BAL_LOG()"
        self.push_balancer("invalid_log.lua", lua_code, expect)

        lua_code = "BAL_LOG(0)"
        self.push_balancer("invalid_log_again.lua", lua_code, expect)

    def test_balancer_valid(self):
        self.start_mantle()
        lua_code = "BAL_LOG(0, \"test\")\nreturn {3, 4}"
        self.fs.mon_manager.raw_cluster_cmd_result('fs', 'set', self.fs.name, 'balancer', "valid.lua")
        self.fs.rados(["put", "valid.lua", "-"], stdin_data=lua_code)
        with self.assert_cluster_log(success + "valid.lua"):
            log.info("run a valid.lua balancer")

    def test_return_invalid(self):
        self.start_mantle()
        expect = ": (22) Invalid argument"

        lua_code = "return \"hello\""
        self.push_balancer("string.lua", lua_code, expect)

        lua_code = "return 3"
        self.push_balancer("number.lua", lua_code, expect)

        lua_code = "return {}"
        self.push_balancer("dict_empty.lua", lua_code, expect)

        lua_code = "return {\"this\", \"is\", \"a\", \"test\"}"
        self.push_balancer("dict_of_strings.lua", lua_code, expect)

        lua_code = "return {3, \"test\"}"
        self.push_balancer("dict_of_mixed.lua", lua_code, expect)

        lua_code = "return {3}"
        self.push_balancer("not_enough_numbers.lua", lua_code, expect)

        lua_code = "return {3, 4, 5, 6, 7, 8, 9}"
        self.push_balancer("too_many_numbers.lua", lua_code, expect)

    def test_dead_osd(self):
        self.start_mantle()
        expect = " : (110) Connection timed out"

        # kill the OSDs so that the balancer pull from RADOS times out
        osd_map = json.loads(self.fs.mon_manager.raw_cluster_cmd('osd', 'dump', '--format=json-pretty'))
        for i in range(0, len(osd_map['osds'])):
          self.fs.mon_manager.raw_cluster_cmd_result('osd', 'down', str(i))
          self.fs.mon_manager.raw_cluster_cmd_result('osd', 'out', str(i))

        # trigger a pull from RADOS
        self.fs.mon_manager.raw_cluster_cmd_result('fs', 'set', self.fs.name, 'balancer', "valid.lua")

        # make the timeout a little longer since dead OSDs spam zbkc -w
        with self.assert_cluster_log(failure + "valid.lua" + expect, timeout=30):
            log.info("run a balancer that should timeout")

        # cleanup
        for i in range(0, len(osd_map['osds'])):
          self.fs.mon_manager.raw_cluster_cmd_result('osd', 'in', str(i))
