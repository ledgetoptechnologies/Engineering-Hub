"""First-install only: publish a privately delivered modular worker connection.

Never edits deployment settings, the Hub ledger, bus state or existing targets.
Run inside the existing NAS Hub container, then recreate/restart it explicitly.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import ssl
import stat
import sys

SCHEMA = 'engineering-hub.staging-first-install.v1'
PRIVATE = 'staging-private'
CONFIG = 'staging-service.json'
TEMP_CONFIG = '.staging-service.first-install.tmp'
MAX_BUNDLE = 128 * 1024


class InstallError(RuntimeError):
    """Only fixed, credential-free reason codes leave this module."""


def require(value, reason):
    if not value:
        raise InstallError(reason)


def unique(pairs):
    value = {}
    for key, item in pairs:
        require(key not in value, 'duplicate_bundle_key')
        value[key] = item
    return value


def validate_bundle(raw: bytes, expected_sha256: str, state_root: Path):
    """Pure configuration/credential parsing. No target paths are opened here."""
    require(type(expected_sha256) is str and re.fullmatch('[0-9a-f]{64}', expected_sha256), 'invalid_bundle_digest')
    require(type(raw) is bytes and len(raw) <= MAX_BUNDLE and
            hashlib.sha256(raw).hexdigest() == expected_sha256, 'bundle_digest_or_size')
    try:
        bundle = json.loads(raw, object_pairs_hook=unique,
            parse_constant=lambda _: (_ for _ in ()).throw(InstallError('invalid_bundle_json')))
        require(type(bundle) is dict and set(bundle) == {'schema', 'connection', 'worker_token', 'ca_pem'} and
                bundle['schema'] == SCHEMA, 'invalid_bundle_shape')
        connection = bundle['connection']
        require(type(connection) is dict and set(connection) <= {
            'schema', 'origin', 'credential_file', 'ca_file', 'admin_actors',
            'preview_origin', 'enabled', 'auto_run', 'projects'}, 'invalid_connection_shape')
        require(type(connection.get('admin_actors')) is list and connection['admin_actors'] and
                len(connection['admin_actors']) == len(set(connection['admin_actors'])), 'explicit_admin_required')
        require(connection.get('credential_file') == str(state_root / PRIVATE / 'worker.token') and
                connection.get('ca_file') == str(state_root / PRIVATE / 'worker-ca.pem'), 'fixed_private_references_required')
        from engineering_hub.staging_client import StagingConnectionConfig
        parsed = StagingConnectionConfig(**dict(connection, admin_actors=frozenset(connection['admin_actors'])))
        require(parsed.origin.startswith('https://') and bool(parsed.projects), 'private_https_profiles_required')
        token, ca = bundle['worker_token'], bundle['ca_pem']
        require(type(token) is str and token.isascii() and 16 <= len(token) <= 4096 and
                token == token.strip() and '\r' not in token and '\n' not in token and '\x00' not in token,
                'invalid_test_credential')
        require(type(ca) is str and ca.isascii() and len(ca) <= 65536 and
                re.fullmatch(r'(?:\s*-----BEGIN CERTIFICATE-----\s+[A-Za-z0-9+/=\s]+-----END CERTIFICATE-----\s*)+', ca),
                'invalid_ca_certificate')
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.load_verify_locations(cadata=ca)
        require(context.cert_store_stats()['x509'] > 0, 'invalid_ca_certificate')
        config = (json.dumps(connection, sort_keys=True, separators=(',', ':'), allow_nan=False) + '\n').encode()
        require(len(config) <= 65536, 'connection_size_limit')
        return config, (token + '\n').encode('ascii'), ca.encode('ascii'), parsed.enabled
    except InstallError:
        raise
    except Exception:
        raise InstallError('invalid_handoff_configuration') from None


def fingerprint(value):
    return value.st_dev, value.st_ino


def named_matches(directory, name, expected):
    try:
        return fingerprint(os.stat(name, dir_fd=directory, follow_symlinks=False)) == expected
    except FileNotFoundError:
        return False


def exclusive_file(directory, name, data, owned):
    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
    try:
        value = os.fstat(fd)
        owned[name] = fingerprint(value)
        os.fchmod(fd, 0o600)
        require(stat.S_ISREG(value.st_mode) and value.st_uid == os.geteuid() and value.st_nlink == 1,
                'new_file_identity')
        view = memoryview(data)
        while view:
            count = os.write(fd, view)
            require(count > 0, 'file_write_failed')
            view = view[count:]
        os.fsync(fd)
    finally:
        os.close(fd)


def install(raw: bytes, expected_sha256: str, state_root: Path):
    """POSIX first-install transaction. Failed publication removes only owned new files."""
    require(os.name == 'posix', 'linux_container_required')
    root = Path(state_root)
    require(root.is_absolute() and '..' not in root.parts and root.resolve(strict=True) == root,
            'existing_unaliased_state_root_required')
    config, token, ca, enabled = validate_bundle(raw, expected_sha256, root)
    rootfd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    privatefd = None
    private_identity = None
    root_owned, private_owned = {}, {}
    committed = False
    try:
        root_identity = fingerprint(os.fstat(rootfd))
        for name in (PRIVATE, CONFIG, TEMP_CONFIG):
            try:
                os.stat(name, dir_fd=rootfd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            raise InstallError('existing_staging_target_refused')
        os.mkdir(PRIVATE, 0o700, dir_fd=rootfd)
        # Record immediately; cleanup never adopts a directory created by another attempt.
        created = os.stat(PRIVATE, dir_fd=rootfd, follow_symlinks=False)
        private_identity = fingerprint(created)
        require(stat.S_ISDIR(created.st_mode) and created.st_uid == os.geteuid() and
                stat.S_IMODE(created.st_mode) == 0o700, 'new_private_directory_identity')
        privatefd = os.open(PRIVATE, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=rootfd)
        value = os.fstat(privatefd)
        require(fingerprint(value) == private_identity and value.st_uid == os.geteuid() and
                stat.S_IMODE(value.st_mode) == 0o700, 'new_private_directory_identity')
        os.fsync(rootfd)
        exclusive_file(privatefd, 'worker.token', token, private_owned)
        exclusive_file(privatefd, 'worker-ca.pem', ca, private_owned)
        os.fsync(privatefd)
        exclusive_file(rootfd, TEMP_CONFIG, config, root_owned)
        # All private files are complete/durable before an enabled config is named.
        require(fingerprint(os.stat(root, follow_symlinks=False)) == root_identity and
                named_matches(rootfd, PRIVATE, private_identity) and
                all(named_matches(privatefd, name, value) for name, value in private_owned.items()) and
                named_matches(rootfd, TEMP_CONFIG, root_owned[TEMP_CONFIG]), 'install_target_changed')
        # link is atomic and refuses an existing name; rename could overwrite older config.
        os.link(TEMP_CONFIG, CONFIG, src_dir_fd=rootfd, dst_dir_fd=rootfd, follow_symlinks=False)
        root_owned[CONFIG] = root_owned[TEMP_CONFIG]
        os.unlink(TEMP_CONFIG, dir_fd=rootfd)
        del root_owned[TEMP_CONFIG]
        os.fsync(rootfd)
        require(named_matches(rootfd, CONFIG, root_owned[CONFIG]) and
                os.stat(CONFIG, dir_fd=rootfd, follow_symlinks=False).st_nlink == 1, 'published_config_changed')
        committed = True
        return {'installed': True, 'restart_required': True, 'enabled': enabled,
                'existing_hub_state_changed': False, 'handoff_bundle_retained': True}
    except InstallError:
        raise
    except Exception:
        raise InstallError('installation_failed') from None
    finally:
        if not committed:
            # Refuse to delete any replaced/unknown object. No recursive cleanup.
            for name, value in tuple(root_owned.items()):
                if named_matches(rootfd, name, value):
                    os.unlink(name, dir_fd=rootfd)
            if privatefd is not None:
                for name, value in tuple(private_owned.items()):
                    if named_matches(privatefd, name, value):
                        os.unlink(name, dir_fd=privatefd)
                os.fsync(privatefd)
            if private_identity is not None and named_matches(rootfd, PRIVATE, private_identity):
                try:
                    os.rmdir(PRIVATE, dir_fd=rootfd)
                except OSError:
                    pass  # Unknown contents survive; config remains unpublished/removed.
            os.fsync(rootfd)
        if privatefd is not None:
            os.close(privatefd)
        os.close(rootfd)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle', required=True, type=Path)
    parser.add_argument('--expected-sha256', required=True)
    parser.add_argument('--state-root', type=Path, default=Path('/var/lib/hub'))
    args = parser.parse_args(argv)
    try:
        require(os.name == 'posix' and os.geteuid() == 568, 'nas_hub_uid_568_required')
        fd = os.open(args.bundle, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, 'rb') as stream:
            before = os.fstat(stream.fileno())
            require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1 and before.st_size <= MAX_BUNDLE,
                    'regular_bounded_handoff_required')
            raw = stream.read(MAX_BUNDLE + 1)
            after = os.fstat(stream.fileno())
            require((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) ==
                    (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns),
                    'handoff_changed')
        print(json.dumps(install(raw, args.expected_sha256, args.state_root), sort_keys=True))
        return 0
    except InstallError as error:
        print(json.dumps({'installed': False, 'code': str(error)}, sort_keys=True))
        return 1
    except Exception:
        print(json.dumps({'installed': False, 'code': 'installation_unavailable'}, sort_keys=True))
        return 1


if __name__ == '__main__':
    sys.exit(main())
