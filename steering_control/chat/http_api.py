"""Keep loopback model traffic local, independently of HTTP_PROXY/NO_PROXY."""
import ipaddress
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, Request, build_opener, urlopen


def is_loopback_url(url):
    host = (urlsplit(url).hostname or "").lower().rstrip(".")
    if host == "localhost":
        return True
    try:
        address = ipaddress.ip_address(host)
        return address.is_loopback or bool(getattr(address, "ipv4_mapped", None) and address.ipv4_mapped.is_loopback)
    except ValueError:
        return False


def api_urlopen(request, *, timeout=90):
    url = request.full_url if isinstance(request, Request) else request
    if is_loopback_url(url):
        # Per-request opener; do not remove proxy settings needed for downloads.
        return build_opener(ProxyHandler({})).open(request, timeout=timeout)
    return urlopen(request, timeout=timeout)
