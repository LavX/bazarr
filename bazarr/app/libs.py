# coding=utf-8

import os
import sys


def set_libs():
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), '../custom_libs/'))
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), '../'))
    # added this last one so Bazarr's modules can be imported by jobs in jobs_queue


set_libs()
