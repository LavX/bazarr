# coding=utf-8
"""The retired built-in provider ids the tests drive, in one place.

``test_provider_hub.py`` and ``test_retired_provider_config_compat.py`` both
parametrise over these lists, and
``test_provider_hub.py::test_retired_id_lists_partition_the_production_set``
asserts the two lists together are exactly
``provider_hub.migration.RETIRED_BUILT_IN_PROVIDER_IDS``. That assertion is the
point of the module: the next retirement cannot add an id to the production set
and forget to give it coverage here, because the partition goes red until the id
is listed in one of the two lists deliberately.
"""

# Retired while they were still shipping, so an upgrading install really can
# carry their config: a stale id in enabled_providers and the side tables, and
# for some of them a stale ``[<id>]`` section with the credentials the provider
# used to validate. These get the full compat treatment.
RETIRED_PROVIDER_IDS_UNDER_TEST = ["hosszupuska", "podnapisi", "subscenter", "xsubs"]

# Retired long before RETIRED_BUILT_IN_PROVIDER_IDS existed. Their claim on the
# id still matters (an untrusted plugin must not take it), which the set
# membership assertions cover, but no supported upgrade path still carries their
# config, so they are not part of the config-compat boot.
LEGACY_RETIRED_PROVIDER_IDS = [
    "argenteamdump",
    "subdivx",
    "subscene",
    "subscene_cloudscraper",
    "tusubtitulo",
]
