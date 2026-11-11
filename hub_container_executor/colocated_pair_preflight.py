"""Compare protected pilot configuration data; never start or approve a pilot."""
import argparse
import json
import sys

from engineering_hub.trusted_queued_session import parse_enrollment, MAX_ENROLLMENT_BYTES
from engineering_hub.unified_pilot_startup import read_protected_file
from hub_container_executor.colocated_session import validate_colocated_session
from hub_container_executor.installed_coordinator import parse_configuration as coordinator_config, MAX_CONFIG_BYTES as COORDINATOR_LIMIT
from hub_container_executor.installed_runner import parse_configuration as runner_config, MAX_CONFIG_BYTES as RUNNER_LIMIT


def validate_pair(runner_raw, coordinator_raw, session_raw):
    """Validate closed data and their common pins; no host state or token reads."""
    try:
        runner = runner_config(runner_raw)
        coordinator = coordinator_config(coordinator_raw)
        session = parse_enrollment(session_raw)
        session, _, _ = validate_colocated_session(coordinator, session)
        if runner.manifest.digest() != coordinator.manifest.digest():
            raise ValueError()
        if runner.resources.get('single_pilot_protocol', False) is not coordinator.single_pilot_protocol:
            raise ValueError()
        coordinator_digests = {(slot, version): digest for slot, version, digest in coordinator.bindings}
        runner_digests = {(slot, version): digest for slot, version, digest in runner.resources['secret_bindings']}
        if coordinator_digests[coordinator.roles['executor']] != runner_digests[runner.bearer_slot]:
            raise ValueError()
        fields = session.release.__dataclass_fields__
        if any(getattr(session.release, name) != getattr(runner.release, name) for name in fields):
            raise ValueError()
        return {'state': 'static_pair_consistent_not_approved', 'manifest_digest': runner.manifest.digest(),
                'single_pilot_protocol': coordinator.single_pilot_protocol}
    except Exception:
        raise ValueError('colocated pair validation failed') from None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument('--runner', required=True)
    parser.add_argument('--coordinator', required=True)
    parser.add_argument('--session', required=True)
    args = parser.parse_args(argv)
    try:
        values = [read_protected_file(path, maximum_bytes=limit, require_readonly=True) for path,limit in (
            (args.runner, RUNNER_LIMIT), (args.coordinator, COORDINATOR_LIMIT), (args.session, MAX_ENROLLMENT_BYTES))]
        print(json.dumps(validate_pair(*values), sort_keys=True))
        return 0
    except Exception:
        print('colocated pair validation failed', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
