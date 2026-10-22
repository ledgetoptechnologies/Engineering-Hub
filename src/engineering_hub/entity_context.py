"""Bounded, inert task/decision context; URLs are validated but never fetched."""
import ipaddress
import re
from urllib.parse import unquote, urlsplit


def bounded_text(value, maximum):
    if type(value) is not str or not value.strip() or len(value) > maximum:
        raise ValueError('invalid_context')
    try:
        encoded = value.encode('utf-8')
    except UnicodeError:
        raise ValueError('invalid_context') from None
    if len(encoded) > maximum * 4 or '\x00' in value:
        raise ValueError('invalid_context')
    return value.strip()


def _url(value):
    if type(value) is not str or not value or len(value) > 2048:
        raise ValueError('invalid_context')
    # Browsers normalize backslashes/whitespace differently from urlsplit. Reject
    # ambiguous forms instead of attempting to repair an agent-supplied URL.
    if (not value.isascii() or any(ord(c) <= 32 or ord(c) == 127 for c in value)
            or '\\' in value or re.search(r'%(?![0-9a-fA-F]{2})', value)):
        raise ValueError('invalid_context')
    decoded = unquote(value)
    if any(ord(c) < 32 or ord(c) == 127 for c in decoded) or '\\' in decoded:
        raise ValueError('invalid_context')
    try:
        parts = urlsplit(value)
        port = parts.port
        host = parts.hostname
    except ValueError:
        raise ValueError('invalid_context') from None
    if (parts.scheme != 'https' or not value.startswith('https://') or not host
            or parts.username is not None or parts.password is not None
            or '%' in parts.netloc or parts.netloc.endswith(':')
            or (port is not None and not 1 <= port <= 65535)):
        raise ValueError('invalid_context')
    authority_pattern = r'\[[0-9a-fA-F:.]+\](?::[0-9]+)?' if ':' in host else r'[a-zA-Z0-9.-]+(?::[0-9]+)?'
    if re.fullmatch(authority_pattern, parts.netloc) is None:
        raise ValueError('invalid_context')
    try:
        ipaddress.ip_address(host)
    except ValueError:
        if (':' in host or len(host) > 253 or re.fullmatch(r'[0-9.]+', host)
                or any(not re.fullmatch(r'[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?', label)
                       for label in host.split('.'))):
            raise ValueError('invalid_context') from None
    return value


def validated_context(details=None, links=None):
    """Return only explicitly supplied fields; retain legacy default shapes."""
    result = {}
    if details is not None:
        result['details'] = bounded_text(details, 4096)
    if links is not None:
        if type(links) is not list or len(links) > 8:
            raise ValueError('invalid_context')
        normalized = []
        for link in links:
            if type(link) is not dict or set(link) != {'label', 'url'}:
                raise ValueError('invalid_context')
            normalized.append({'label': bounded_text(link['label'], 100), 'url': _url(link['url'])})
        result['links'] = normalized
    return result
