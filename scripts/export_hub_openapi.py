"""Mechanically export/check the Hub contract without tokens or a live service."""
import argparse
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from agent_message_bus.auth import AuthSettings
from engineering_hub.app import Settings, create_app
from engineering_hub.auth import AuthConfig


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--write', action='store_true', help='Regenerate the fixed repository contract')
    args = parser.parse_args()
    destination = Path(__file__).resolve().parents[1] / 'contracts/hub-v1.openapi.json'
    with TemporaryDirectory(prefix='hub-contract-') as scratch:
        settings = Settings(Path(scratch) / 'contract.sqlite3',
                            AuthSettings('contract-fixture-hermes', 'contract-fixture-codex',
                                         claude_token='contract-fixture-claude'),
                            AuthConfig('https://hub.invalid'))
        content = json.dumps(create_app(settings).openapi(), indent=2) + '\n'
    if args.write:
        destination.write_text(content, encoding='utf-8', newline='\n')
        print('Regenerated Hub OpenAPI from current source (no service started)')
    elif destination.read_text(encoding='utf-8') != content:
        raise SystemExit('Hub OpenAPI differs; run scripts/export_hub_openapi.py --write')
    else:
        print('Published Hub OpenAPI matches current source')


if __name__ == '__main__':
    main()
