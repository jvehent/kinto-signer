import functools

from cliquet.events import ACTIONS, ResourceChanged
from pyramid.exceptions import ConfigurationError

from kinto_signer import utils
from kinto_signer.updater import LocalUpdater
from kinto_signer.signer import heartbeat


def on_collection_changed(event, resources):
    """
    Listen to resource change events, to check if a new signature is
    requested.

    When a source collection specified in settings is modified, and its
    new metadata ``status`` is set to ``"to-sign"``, then sign the data
    and update the destination.
    """
    payload = event.payload
    key = "/buckets/{bucket_id}/collections/{collection_id}".format(**payload)
    resource = resources.get(key)

    # Only sign the configured resources.
    if resource is None:
        return

    # Only sign when the new collection status is "to-sign".
    should_sign = any([True for r in event.impacted_records
                       if r['new'].get('status') == 'to-sign'])
    if not should_sign:
        return

    registry = event.request.registry
    updater = LocalUpdater(
        signer=registry.signer,
        storage=registry.storage,
        permission=registry.permission,
        source=resource['source'],
        destination=resource['destination'])

    updater.sign_and_update_remote()


def includeme(config):
    # Register heartbeat to check signer integration.
    config.registry.heartbeats['signer'] = heartbeat

    settings = config.get_settings()

    # Check source and destination resources are configured.
    raw_resources = settings.get('signer.resources')
    if raw_resources is None:
        error_msg = "Please specify the kinto.signer.resources setting."
        raise ConfigurationError(error_msg)
    resources = utils.parse_resources(raw_resources)

    # Load the signers from their dotted location.
    # If the signer_backend setting is defined for a particular bucket
    # or a particular collection, then use a prefix for the related
    # settings names.
    config.registry.signers = {}
    backend_setting = 'signer_backend'
    for key, resource in resources.items():
        prefix = 'signer.'
        bucket_wide = '{bucket}.'.format(**resource['source'])
        collection_wide = '{bucket}_{collection}.'.format(**resource['source'])
        if (prefix + collection_wide + backend_setting) in settings:
            prefix += collection_wide
        elif (prefix + bucket_wide + backend_setting) in settings:
            prefix += bucket_wide

        # Fallback to the local ECDSA signer.
        default_signer_module = "kinto_signer.signer.local_ecdsa"
        signer_dotted_location = settings.get(prefix + backend_setting,
                                              default_signer_module)
        signer_module = config.maybe_dotted(signer_dotted_location)
        backend = signer_module.load_from_settings(settings, prefix)
        config.registry.signers[key] = backend

    # Expose the capabilities in the root endpoint.
    message = "Digital signatures for integrity and authenticity of records."
    docs = "https://github.com/Kinto/kinto-signer#kinto-signer"
    sorted_resources = sorted(resources.values(),
                              key=lambda r: r['source']['collection'])
    config.add_api_capability("signer", message, docs,
                              resources=sorted_resources)

    config.add_subscriber(
        functools.partial(on_collection_changed, resources=resources),
        ResourceChanged,
        for_actions=(ACTIONS.CREATE, ACTIONS.UPDATE),
        for_resources=('collection',)
    )
