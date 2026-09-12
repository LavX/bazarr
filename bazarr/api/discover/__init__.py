from .search import api_ns_discover_search
from .download import api_ns_discover_download
from .library import api_ns_discover_library
from .metadata import api_ns_discover_metadata
from .feeds import api_ns_discover_feeds
from .summary import api_ns_discover_summary

api_ns_list_discover = [api_ns_discover_search, api_ns_discover_download, api_ns_discover_library,
                        api_ns_discover_metadata, api_ns_discover_feeds, api_ns_discover_summary]
