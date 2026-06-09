# coding=utf-8
"""Multiple Sonarr/Radarr instances (#156).

First-class Arr instances: one Bazarr+ install can connect to several named
Sonarr/Radarr instances (split libraries such as TV, anime, 4K). Bazarr local
IDs are canonical; upstream Sonarr/Radarr IDs become per-instance metadata
scoped by ``arr_instance_id``.
"""
