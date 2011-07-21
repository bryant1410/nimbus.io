# -*- coding: utf-8 -*-
"""
http_util.py

utility functions for connecting wiht DIY via HTTP
"""
import os
import time
import urllib

def compute_uri(key, action=None):
    """
    Create the REST URI sent to the server
    """
    work_key = urllib.quote_plus(key)
    path = os.path.join(os.sep, "data", work_key)
    if action is not None:
        path = "?".join([path, "action=%s" % (action, ), ])
    return path

def current_timestamp():
    """
    return the current time as an integer
    """
    return int(time.time())

