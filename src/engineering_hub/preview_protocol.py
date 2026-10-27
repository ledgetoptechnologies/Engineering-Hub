"""Pure preview origin/asset validation shared by broker and browser gateways.

No application, state, credentials or transport initialization.
"""
import ipaddress
import re
from urllib.parse import urlsplit

_PATH = re.compile(r'[A-Za-z0-9_@./-]{1,512}\Z')
_DIGEST = re.compile(r'sha256:[a-f0-9]{64}\Z')
_MIME = {
    'html': ('text/html',), 'htm': ('text/html',),
    'js': ('text/javascript', 'application/javascript'),
    'mjs': ('text/javascript', 'application/javascript'),
    'css': ('text/css',), 'json': ('application/json',),
    'txt': ('text/plain',), 'svg': ('image/svg+xml',),
    'png': ('image/png',), 'jpg': ('image/jpeg',), 'jpeg': ('image/jpeg',),
    'webp': ('image/webp',), 'gif': ('image/gif',), 'ico': ('image/x-icon', 'image/vnd.microsoft.icon'),
    'woff': ('font/woff',), 'woff2': ('font/woff2',),
    'ttf': ('font/ttf',), 'otf': ('font/otf',), 'wasm': ('application/wasm',),
}


def _origin(value):
    if type(value) is not str or not value.isascii() or any(ord(c) < 33 or ord(c) == 127 for c in value):
        raise ValueError('Exact HTTPS preview origins required')
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise ValueError('Exact HTTPS preview origins required') from None
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
            or parsed.path or parsed.query or parsed.fragment or parsed.netloc != parsed.netloc.lower()
            or value != 'https://' + parsed.netloc or port is not None and not 1 <= port <= 65535):
        raise ValueError('Exact HTTPS preview origins required')
    host = parsed.hostname
    if any(char in host for char in ('\\', '%', ';', ',')):
        raise ValueError('Exact HTTPS preview origins required')
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        labels = host.split('.')
        if (len(host) > 253 or any(re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', label) is None
                                   for label in labels)
                or labels[-1].isdigit() or re.fullmatch(r'0x[0-9a-f]+', labels[-1])):
            # Browser URL parsing recognizes several noncanonical numeric-IP
            # spellings that Python's IP parser rightly rejects. Do not accept
            # them as DNS names and accidentally alias a protected origin.
            raise ValueError('Exact HTTPS preview origins required') from None
        authority = host
    else:
        authority = '[' + str(address) + ']' if address.version == 6 else str(address)
    if port is not None and port != 443:
        authority += ':' + str(port)
    # Browsers strip the default port and normalize literal IPs. Use that same
    # representation for equality, CSP sources and exact Host dispatch.
    return 'https://' + authority
