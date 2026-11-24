"""Shared HTTP plumbing for every outbound request: the User-Agent pf sends, and the one way to open a request.

Redirect policy is the one distinction between the four stacks: the Pirate Face API never follows a redirect
(an answer means one request against the host it named, so a credential is never carried to another host),
while Hugging Face follows redirects to a CDN but strips the Authorization header when the redirect crosses
hosts. Callers pick a policy explicitly; the opener is shared."""

import urllib.parse
import urllib.request
from importlib.metadata import version

# policy names: how a redirect is handled.
NO_REDIRECTS = "none"
SAME_HOST_AUTH = "same-host-auth"


class NoRedirects(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect: an answer means one request against the host it named. Returning None is what
    hands the 3xx back to the caller, so a credential on a request is not carried to another host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class SameHostAuth(urllib.request.HTTPRedirectHandler):
    """Follow redirects, but send the bearer token only to the host it was meant for: LFS files redirect to a
    CDN on another host, which needs no token."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new and urllib.parse.urlsplit(new.full_url)[:2] != urllib.parse.urlsplit(req.full_url)[:2]:
            new.remove_header("Authorization")
        return new


_OPENERS = {
    NO_REDIRECTS: urllib.request.build_opener(NoRedirects()),
    SAME_HOST_AUTH: urllib.request.build_opener(SameHostAuth()),
}


def open(prepared_request, timeout, policy):
    """Open a prepared Request with the policy's opener. policy is NO_REDIRECTS or SAME_HOST_AUTH."""
    return _OPENERS[policy].open(prepared_request, timeout=timeout)


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
