"""Rewrite outbound links so Chinese-language sources open translated.

Two mechanisms exist:

  1. translate.goog subdomain proxy (current, reliable)
       https://www.qbitai.com/2026/08/1.html
     ->  https://www-qbitai-com.translate.goog/2026/08/1.html
           ?_x_tr_sl=zh-CN&_x_tr_tl=en&_x_tr_hl=en

  2. translate.google.com/translate?u=... (legacy, widely deprecated,
     now redirects or errors for most sites)

We use (1). Some hosts break under the proxy — CDN-signed URLs, sites that
hard-check Host headers. Add those to PROXY_DENY and they'll be emitted as
plain links with a flag so the template can mark them.
"""
from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit, urlencode, parse_qsl

TR_PARAMS = {"_x_tr_sl": "zh-CN", "_x_tr_tl": "en", "_x_tr_hl": "en"}

# Hosts where the translate proxy is known to fail or add nothing.
PROXY_DENY = {
    "mp.weixin.qq.com",   # blocks the proxy outright
    "chinai.substack.com",
    "www.chinatalk.media",
    "www.pekingnology.com",
    "www.gingerriver.com",
    "sinocism.com",
}


def to_proxy(url: str) -> tuple[str, bool]:
    """Return (url, proxied). Falls back to the original URL on anything odd."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return url, False

    if parts.scheme not in ("http", "https") or not parts.netloc:
        return url, False
    if parts.netloc in PROXY_DENY:
        return url, False
    if parts.netloc.endswith(".translate.goog"):
        return url, True  # already proxied

    # www.qbitai.com -> www-qbitai-com.translate.goog
    # A literal hyphen in the host becomes '--' to stay reversible.
    host = parts.netloc.split(":")[0]
    proxied_host = host.replace("-", "--").replace(".", "-") + ".translate.goog"

    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query.update(TR_PARAMS)

    return urlunsplit(
        ("https", proxied_host, parts.path or "/", urlencode(query), parts.fragment)
    ), True


# The proxy is DISABLED. Tested against every Chinese source in the feed set,
# from two different network paths:
#   36Kr, GeekPark, Leiphone, Jiemian  -> HTTP 408, the proxy cannot fetch them
#   QbitAI, Solidot                    -> HTTP 200 but the body comes back in
#                                         Chinese; confirmed in a real browser,
#                                         Google's own banner renders and no
#                                         translation is applied
# So every proxied link was either broken or a lie, while the page carried a
# `TR` badge and a footer promising Google Translate. Linking direct is honest,
# and the on-page detail now carries the substance the proxy was there to
# provide. `to_proxy` is kept because the URL construction is correct and worth
# not re-deriving if a working proxy ever appears.
PROXY_ENABLED = False


def apply(item, force: bool = False):
    """Attach .link (what the template renders) and .link_direct to an item."""
    if PROXY_ENABLED and (item.lang == "zh" or force):
        item.link, item.proxied = to_proxy(item.url)
    else:
        item.link, item.proxied = item.url, False
    item.link_direct = item.url
    return item
