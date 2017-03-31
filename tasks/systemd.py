"""
Systemd test
"""
import contextlib
import logging
import re
import time

from cStringIO import StringIO
from teuthology.orchestra import run

log = logging.getLogger(__name__)


@contextlib.contextmanager
def task(ctx, config):
    """
      - tasks:
          zbkc-deploy:
          systemd:

    Test zbkc systemd services can start, stop and restart and
    check for any failed services and report back errors
    """
    for remote, roles in ctx.cluster.remotes.iteritems():
        remote.run(args=['sudo', 'ps', '-eaf', run.Raw('|'),
                         'grep', 'zbkc'])
        r = remote.run(args=['sudo', 'systemctl', 'list-units', run.Raw('|'),
                             'grep', 'zbkc'], stdout=StringIO(),
                       check_status=False)
        log.info(r.stdout.getvalue())
        if r.stdout.getvalue().find('failed'):
            log.info("Zbkc services in failed state")

        # test overall service stop and start using zbkc.target
        # zbkc.target tests are meant for zbkc systemd tests
        # and not actual process testing using 'ps'
        log.info("Stopping all Zbkc services")
        remote.run(args=['sudo', 'systemctl', 'stop', 'zbkc.target'])
        r = remote.run(args=['sudo', 'systemctl', 'status', 'zbkc.target'],
                       stdout=StringIO(), check_status=False)
        log.info(r.stdout.getvalue())
        log.info("Checking process status")
        r = remote.run(args=['sudo', 'ps', '-eaf', run.Raw('|'),
                             'grep', 'zbkc'], stdout=StringIO())
        if r.stdout.getvalue().find('Active: inactive'):
            log.info("Sucessfully stopped all zbkc services")
        else:
            log.info("Failed to stop zbkc services")

        log.info("Starting all Zbkc services")
        remote.run(args=['sudo', 'systemctl', 'start', 'zbkc.target'])
        r = remote.run(args=['sudo', 'systemctl', 'status', 'zbkc.target'],
                       stdout=StringIO())
        log.info(r.stdout.getvalue())
        if r.stdout.getvalue().find('Active: active'):
            log.info("Sucessfully started all Zbkc services")
        else:
            log.info("info", "Failed to start Zbkc services")
        r = remote.run(args=['sudo', 'ps', '-eaf', run.Raw('|'),
                             'grep', 'zbkc'], stdout=StringIO())
        log.info(r.stdout.getvalue())
        time.sleep(4)

        # test individual services start stop
        name = remote.shortname
        mon_name = 'zbkc-mon@' + name + '.service'
        mds_name = 'zbkc-mds@' + name + '.service'
        mgr_name = 'zbkc-mgr@' + name + '.service'
        mon_role_name = 'mon.' + name
        mds_role_name = 'mds.' + name
        mgr_role_name = 'mgr.' + name
        m_osd = re.search('--id (\d+) --setuser zbkc', r.stdout.getvalue())
        if m_osd:
            osd_service = 'zbkc-osd@{m}.service'.format(m=m_osd.group(1))
            remote.run(args=['sudo', 'systemctl', 'status',
                             osd_service])
            remote.run(args=['sudo', 'systemctl', 'stop',
                             osd_service])
            time.sleep(4)  # immediate check will result in deactivating state
            r = remote.run(args=['sudo', 'systemctl', 'status', osd_service],
                           stdout=StringIO(), check_status=False)
            log.info(r.stdout.getvalue())
            if r.stdout.getvalue().find('Active: inactive'):
                log.info("Sucessfully stopped single osd zbkc service")
            else:
                log.info("Failed to stop zbkc osd services")
            remote.run(args=['sudo', 'systemctl', 'start',
                             osd_service])
            time.sleep(4)
        if mon_role_name in roles:
            remote.run(args=['sudo', 'systemctl', 'status', mon_name])
            remote.run(args=['sudo', 'systemctl', 'stop', mon_name])
            time.sleep(4)  # immediate check will result in deactivating state
            r = remote.run(args=['sudo', 'systemctl', 'status', mon_name],
                           stdout=StringIO(), check_status=False)
            if r.stdout.getvalue().find('Active: inactive'):
                log.info("Sucessfully stopped single mon zbkc service")
            else:
                log.info("Failed to stop zbkc mon service")
            remote.run(args=['sudo', 'systemctl', 'start', mon_name])
            time.sleep(4)
        if mgr_role_name in roles:
            remote.run(args=['sudo', 'systemctl', 'status', mgr_name])
            remote.run(args=['sudo', 'systemctl', 'stop', mgr_name])
            time.sleep(4)  # immediate check will result in deactivating state
            r = remote.run(args=['sudo', 'systemctl', 'status', mgr_name],
                           stdout=StringIO(), check_status=False)
            if r.stdout.getvalue().find('Active: inactive'):
                log.info("Sucessfully stopped single zbkc mgr service")
            else:
                log.info("Failed to stop zbkc mgr service")
            remote.run(args=['sudo', 'systemctl', 'start', mgr_name])
            time.sleep(4)
        if mds_role_name in roles:
            remote.run(args=['sudo', 'systemctl', 'status', mds_name])
            remote.run(args=['sudo', 'systemctl', 'stop', mds_name])
            time.sleep(4)  # immediate check will result in deactivating state
            r = remote.run(args=['sudo', 'systemctl', 'status', mds_name],
                           stdout=StringIO(), check_status=False)
            if r.stdout.getvalue().find('Active: inactive'):
                log.info("Sucessfully stopped single zbkc mds service")
            else:
                log.info("Failed to stop zbkc mds service")
            remote.run(args=['sudo', 'systemctl', 'start', mds_name])
            time.sleep(4)
    yield
