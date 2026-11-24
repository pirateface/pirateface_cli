"""Shared HTTP plumbing for every outbound request: the User-Agent pf sends."""

import urllib.request
from importlib.metadata import version


def user_agent():
    """The User-Agent every outbound request carries: pirateface/<version>."""
    return f"pirateface/{version('pirateface')}"


def request(url, headers=None, data=None, method=None):
    """A Request for url carrying the User-Agent, with the given extra headers/data/method."""
    return urllib.request.Request(
        url,
        data=data,
        headers={"User-Agent": user_agent(), **(headers or {})},
        method=method,
    )
