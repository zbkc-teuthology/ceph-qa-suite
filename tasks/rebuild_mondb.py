"""
Test if we can recover the leveldb from OSD after where all leveldbs are
corrupted
"""

import logging
import os.path
import shutil
import tempfile

import zbkc_manager
from teuthology import misc as teuthology

log = logging.getLogger(__name__)


def push_directory(path, remote, remote_dir):
    """
    local_temp_path=`mktemp`
    tar czf $local_temp_path $path
    ssh remote mkdir -p remote_dir
    remote_temp_path=`mktemp`
    scp $local_temp_path $remote_temp_path
    rm $local_temp_path
    tar xzf $remote_temp_path -C $remote_dir
    ssh remote:$remote_temp_path
    """
    fd, local_temp_path = tempfile.mkstemp(suffix='.tgz',
                                           prefix='rebuild_mondb-')
    os.close(fd)
    cmd = ' '.join(['tar', 'cz',
                    '-f', local_temp_path,
                    '-C', path,
                    '--', '.'])
    teuthology.sh(cmd)
    _, fname = os.path.split(local_temp_path)
    fd, remote_temp_path = tempfile.mkstemp(suffix='.tgz',
                                            prefix='rebuild_mondb-')
    os.close(fd)
    remote.put_file(local_temp_path, remote_temp_path)
    os.remove(local_temp_path)
    remote.run(args=['sudo',
                     'tar', 'xz',
                     '-C', remote_dir,
                     '-f', remote_temp_path])
    remote.run(args=['sudo', 'rm', '-fr', remote_temp_path])


def task(ctx, config):
    """
    Test monitor recovery from OSD
    """
    if config is None:
        config = {}
    assert isinstance(config, dict), \
        'task only accepts a dict for configuration'

    first_mon = teuthology.get_first_mon(ctx, config)
    (mon,) = ctx.cluster.only(first_mon).remotes.iterkeys()

    manager = zbkc_manager.ZbkcManager(
        mon,
        ctx=ctx,
        logger=log.getChild('zbkc_manager'))

    mons = ctx.cluster.only(teuthology.is_type('mon'))
    assert mons
    # note down the first cluster_name and mon_id
    # we will recover it later on
    cluster_name = None
    mon_id = None
    for remote, roles in mons.remotes.iteritems():
        is_mon = teuthology.is_type('mon')
        for role in roles:
            if not is_mon(role):
                continue
            cluster, _, m = teuthology.split_role(role)
            if cluster_name is None:
                cluster_name = cluster
                mon_id = m
            assert cluster_name == cluster
            log.info('killing {cluster}:mon.{mon}'.format(
                cluster=cluster,
                mon=m))
            manager.kill_mon(m)
            mon_data = os.path.join('/var/lib/zbkc/mon/',
                                    '{0}-{1}'.format(cluster_name, m))
            if m == mon_id:
                # so we will only need to recreate the store.db for the
                # first mon, would be easier than mkfs on it then replace
                # the its store.db with the recovered one
                store_dir = os.path.join(mon_data, 'store.db')
                remote.run(args=['sudo', 'rm', '-r', store_dir])
            else:
                remote.run(args=['sudo', 'rm', '-r', mon_data])

    local_mstore = tempfile.mkdtemp()

    # collect the maps from all OSDs
    osds = ctx.cluster.only(teuthology.is_type('osd'))
    assert osds
    for osd, roles in osds.remotes.iteritems():
        is_osd = teuthology.is_type('osd')
        for role in roles:
            if not is_osd(role):
                continue
            cluster, _, osd_id = teuthology.split_role(role)
            assert cluster_name == cluster
            log.info('collecting maps from {cluster}:osd.{osd}'.format(
                cluster=cluster,
                osd=osd_id))
            # push leveldb to OSD
            osd_mstore = os.path.join(teuthology.get_testdir(ctx), 'mon-store')
            osd.run(args=['sudo', 'mkdir', '-m', 'o+x', '-p', osd_mstore])

            push_directory(local_mstore, osd, osd_mstore)
            log.info('rm -rf {0}'.format(local_mstore))
            shutil.rmtree(local_mstore)
            # update leveldb with OSD data
            options = '--op update-mon-db --mon-store-path {0}'
            log.info('cot {0}'.format(osd_mstore))
            manager.objectstore_tool(pool=None,
                                     options=options.format(osd_mstore),
                                     args='',
                                     osd=osd_id,
                                     do_revive=False)
            # pull the updated mon db
            log.info('pull dir {0} -> {1}'.format(osd_mstore, local_mstore))
            local_mstore = tempfile.mkdtemp()
            teuthology.pull_directory(osd, osd_mstore, local_mstore)
            log.info('rm -rf osd:{0}'.format(osd_mstore))
            osd.run(args=['sudo', 'rm', '-fr', osd_mstore])

    # recover the first_mon with re-built mon db
    # pull from recovered leveldb from client
    mon_store_dir = os.path.join('/var/lib/zbkc/mon',
                                 '{0}-{1}'.format(cluster_name, mon_id))
    push_directory(local_mstore, mon, mon_store_dir)
    mon.run(args=['sudo', 'chown', '-R', 'zbkc:zbkc', mon_store_dir])
    shutil.rmtree(local_mstore)
    default_keyring = '/etc/zbkc/{cluster}.keyring'.format(
        cluster=cluster_name)
    keyring_path = config.get('keyring_path', default_keyring)
    # fill up the caps in the keyring file
    mon.run(args=['sudo',
                  'zbkc-authtool', keyring_path,
                  '-n', 'mon.',
                  '--cap', 'mon', 'allow *'])
    mon.run(args=['sudo',
                  'zbkc-authtool', keyring_path,
                  '-n', 'client.admin',
                  '--cap', 'mon', 'allow *',
                  '--cap', 'osd', 'allow *',
                  '--cap', 'mds', 'allow *'])
    mon.run(args=['sudo', '-u', 'zbkc',
                  'zbkc-monstore-tool', mon_store_dir,
                  'rebuild', '--', '--keyring',
                  keyring_path])

    # revive monitors
    # the initial monmap is in the zbkc.conf, so we are good.
    n_mons = 0
    for remote, roles in mons.remotes.iteritems():
        is_mon = teuthology.is_type('mon')
        for role in roles:
            if not is_mon(role):
                continue
            cluster, _, m = teuthology.split_role(role)
            assert cluster_name == cluster
            if mon_id != m:
                log.info('running mkfs on {cluster}:mon.{mon}'.format(
                    cluster=cluster,
                    mon=m))
                remote.run(
                    args=[
                        'sudo',
                        'zbkc-mon',
                        '--cluster', cluster,
                        '--mkfs',
                        '-i', m,
                        '--keyring', keyring_path])
            manager.revive_mon(m)
            n_mons += 1

    manager.wait_for_mon_quorum_size(n_mons, timeout=30)
    for osd, roles in osds.remotes.iteritems():
        is_osd = teuthology.is_type('osd')
        for role in roles:
            if not is_osd(role):
                continue
            _, _, osd_id = teuthology.split_role(role)
            log.info('reviving osd.{0}'.format(osd_id))
            manager.revive_osd(osd_id)
