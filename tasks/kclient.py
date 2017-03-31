"""
Mount/unmount a ``kernel`` client.
"""
import contextlib
import logging

from teuthology.misc import deep_merge
from teuthology import misc
from zbkcfs.kernel_mount import KernelMount

log = logging.getLogger(__name__)

@contextlib.contextmanager
def task(ctx, config):
    """
    Mount/unmount a ``kernel`` client.

    The config is optional and defaults to mounting on all clients. If
    a config is given, it is expected to be a list of clients to do
    this operation on. This lets you e.g. set up one client with
    ``zbkc-fuse`` and another with ``kclient``.

    Example that mounts all clients::

        tasks:
        - zbkc:
        - kclient:
        - interactive:

    Example that uses both ``kclient` and ``zbkc-fuse``::

        tasks:
        - zbkc:
        - zbkc-fuse: [client.0]
        - kclient: [client.1]
        - interactive:


    Pass a dictionary instead of lists to specify per-client config:

        tasks:
        -kclient:
            client.0:
                debug: true

    :param ctx: Context
    :param config: Configuration
    """
    log.info('Mounting kernel clients...')
    assert config is None or isinstance(config, list) or isinstance(config, dict), \
        "task kclient got invalid config"

    if config is None:
        config = ['client.{id}'.format(id=id_)
                  for id_ in misc.all_roles_of_type(ctx.cluster, 'client')]

    if isinstance(config, list):
        client_roles = config
        config = dict([r, dict()] for r in client_roles)
    elif isinstance(config, dict):
        client_roles = filter(lambda x: 'client.' in x, config.keys())
    else:
        raise ValueError("Invalid config object: {0} ({1})".format(config, config.__class__))

    # config has been converted to a dict by this point
    overrides = ctx.config.get('overrides', {})
    deep_merge(config, overrides.get('kclient', {}))

    clients = list(misc.get_clients(ctx=ctx, roles=client_roles))

    test_dir = misc.get_testdir(ctx)

    # Assemble mon addresses
    remotes_and_roles = ctx.cluster.remotes.items()
    roles = [roles for (remote_, roles) in remotes_and_roles]
    ips = [remote_.ssh.get_transport().getpeername()[0]
           for (remote_, _) in remotes_and_roles]
    mons = misc.get_mons(roles, ips).values()

    mounts = {}
    for id_, remote in clients:
        client_config = config.get("client.%s" % id_)
        if client_config is None:
            client_config = {}

        if config.get("disabled", False) or not client_config.get('mounted', True):
            continue

        kernel_mount = KernelMount(
            mons,
            test_dir,
            id_,
            remote,
            ctx.teuthology_config.get('ipmi_user', None),
            ctx.teuthology_config.get('ipmi_password', None),
            ctx.teuthology_config.get('ipmi_domain', None)
        )

        mounts[id_] = kernel_mount

        if client_config.get('debug', False):
            remote.run(args=["sudo", "bash", "-c", "echo 'module zbkc +p' > /sys/kernel/debug/dynamic_debug/control"])
            remote.run(args=["sudo", "bash", "-c", "echo 'module libzbkc +p' > /sys/kernel/debug/dynamic_debug/control"])

        kernel_mount.mount()

    ctx.mounts = mounts
    try:
        yield mounts
    finally:
        log.info('Unmounting kernel clients...')
        for mount in mounts.values():
            if mount.is_mounted():
                mount.umount()
