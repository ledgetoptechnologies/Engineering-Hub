"""Validate actual TypeScript demo fixtures against the public Python DTOs.

Run after installing the Hub and Node >=22.12. No server/network/credentials.
Browser fixtures are synthetic; matching schemas does not verify live behavior.
"""
import json
from pathlib import Path
import shutil
import subprocess

from engineering_hub.app import TopicSnapshot
from engineering_hub.dto import OverviewDTO


def main():
    node = shutil.which('node')
    if not node:
        raise SystemExit('Node is required to validate the actual browser fixtures')
    source = '''
import {createDemoOverview,createDemoTopic} from './hub-ui/src/mocks.ts';
const now = new Date('2026-10-17T18:00:00Z');
console.log(JSON.stringify({overview:createDemoOverview(now),
                           topic:createDemoTopic('contract-fixture',now)}));
'''
    result = subprocess.run(
        [node, '--experimental-strip-types', '--input-type=module', '-e', source],
        cwd=Path(__file__).resolve().parents[1], check=True,
        capture_output=True, text=True, timeout=15,
    )
    data = json.loads(result.stdout)
    OverviewDTO.model_validate(data['overview'])
    TopicSnapshot.model_validate_json(json.dumps(data['topic']))
    print('Actual TypeScript overview/topic fixtures match public response DTOs')


if __name__ == '__main__':
    main()
