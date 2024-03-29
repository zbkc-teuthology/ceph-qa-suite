"""
test_stress_watch task
"""
import contextlib
import logging
import proc_thrasher

from teuthology.orchestra import run

log = logging.getLogger(__name__)


@contextlib.contextmanager
def task(ctx, config):
    """
    Run test_stress_watch

    The config should be as follows:

    test_stress_watch:
        clients: [client list]

    example:

    tasks:
    - zbkc:
    - test_stress_watch:
        clients: [client.0]
    - interactive:
    """
    log.info('Beginning test_stress_watch...')
    assert isinstance(config, dict), \
        "please list clients to run on"
    testwatch = {}

    remotes = []

    for role in config.get('clients', ['client.0']):
        assert isinstance(role, basestring)
        PREFIX = 'client.'
        assert role.startswith(PREFIX)
        id_ = role[len(PREFIX):]
        (remote,) = ctx.cluster.only(role).remotes.iterkeys()
        remotes.append(remote)

        args =['ZBKC_CLIENT_ID={id_}'.format(id_=id_),
               'ZBKC_ARGS="{flags}"'.format(flags=config.get('flags', '')),
               'daemon-helper',
               'kill',
               'multi_stress_watch foo foo'
               ]

        log.info("args are %s" % (args,))

        proc = proc_thrasher.ProcThrasher({}, remote,
            args=[run.Raw(i) for i in args],
            logger=log.getChild('testwatch.{id}'.format(id=id_)),
            stdin=run.PIPE,
            wait=False
            )
        proc.start()
        testwatch[id_] = proc

    try:
        yield
    finally:
        log.info('joining watch_notify_stress')
        for i in testwatch.itervalues():
            i.join()
