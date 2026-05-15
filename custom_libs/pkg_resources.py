# coding=utf-8

from importlib import metadata


class Distribution:
    def __init__(self, version):
        self.version = version


def get_distribution(distribution_name):
    return Distribution(metadata.version(distribution_name))
