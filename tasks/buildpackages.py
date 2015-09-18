"""
Build ceph packages
"""
import logging
import os
import subprocess
from teuthology import packaging
from teuthology import misc as teuthology
from teuthology.config import config as teuth_config
from teuthology.task import install
from teuthology.openstack import OpenStack
import urlparse

log = logging.getLogger(__name__)

def get_install_config(ctx):
    for task in ctx.config['tasks']:
        if task.keys()[0] == 'install':
            config = task['install']
    if config is None:
        config = {}
    assert isinstance(config, dict), \
        "task install only supports a dictionary for configuration"

    project, = config.get('project', 'ceph'),
    log.debug('project %s' % project)
    overrides = ctx.config.get('overrides')
    if overrides:
        install_overrides = overrides.get('install', {})
        teuthology.deep_merge(config, install_overrides.get(project, {}))
    log.debug('install config %s' % config)
    return config

def task(ctx, config):
    """
    Build Ceph packages. This task will automagically be run
    before the task that need to install packages (this is taken
    care of by the internal teuthology task).

    The config should be as follows:

    buildpackages:
      machine:
        disk: 40 # GB
        ram: 15000 # MB
        cpus: 16

    example:

    tasks:
    - buildpackages:
        machine:
          disk: 40 # GB
          ram: 15000 # MB
          cpus: 16
    - install:
    """
    log.info('Beginning buildpackages...')
    if config is None:
        config = {}
    assert isinstance(config, dict), \
        'task only accepts a dict for config not ' + str(config)
    d = os.path.join(os.path.dirname(__file__), 'buildpackages')
    install_config = get_install_config(ctx)
    log.info('install_config ' + str(install_config))
    for remote in ctx.cluster.remotes.iterkeys():
        gitbuilder = install._get_gitbuilder_project(
            ctx, remote, install_config)
        tag = packaging._get_config_value_for_remote(
            ctx, remote, install_config, 'tag')
        branch = packaging._get_config_value_for_remote(
            ctx, remote, install_config, 'branch')
        sha1 = packaging._get_config_value_for_remote(
            ctx, remote, install_config, 'sha1')
        uri_reference = gitbuilder.uri_reference
        url = gitbuilder.base_url
        assert '/' + uri_reference in url, \
            (url + ' (from template ' + teuth_config.baseurl_template +
             ') does not contain /' + uri_reference)
        if 'ref/' in uri_reference:
            ref = os.path.basename(uri_reference)
        else:
            ref = ''
        subprocess.check_call(
            "make -C " + d + " " + os.environ['HOME'] + "/.ssh_agent",
            shell=True)
        target = os.path.dirname(urlparse.urlparse(url).path.strip('/'))
        target = os.path.dirname(target) + '-' + sha1
        openstack = OpenStack()
        select = '^(vps|eg)-'
        flavor = openstack.flavor(config['machine'], select)
        cmd = (". " + os.environ['HOME'] + "/.ssh_agent ; make -C " + d +
               " CEPH_GIT_BASE_URL=" + teuth_config.ceph_git_base_url + 
               " CEPH_OS_TYPE=" + gitbuilder.os_type +
               " CEPH_OS_VERSION=" + gitbuilder.os_version +
               " CEPH_DIST=" + gitbuilder.distro +
               " CEPH_ARCH=" + gitbuilder.arch +
               " CEPH_SHA1=" + (sha1 or '')  +
               " CEPH_TAG=" + (tag or '') +
               " CEPH_BRANCH=" + (branch or '') +
               " CEPH_REF=" + ref +
               " GITBUILDER_URL=" + url +
               " FLAVOR=" + flavor +
               " " + target +
               " ")
        log.info("buildpackages: " + cmd)
        subprocess.check_call(cmd, shell=True)
    teuth_config.gitbuilder_host = openstack.get_ip('packages-repository', '')
    log.info('Finished buildpackages')