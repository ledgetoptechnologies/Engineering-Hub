"""Portable parent preflight must not load the Unix executor runtime."""
import os
from pathlib import Path
import subprocess
import sys
import pytest


def test_parent_preflight_imports_from_checkout_without_runtime():
    root=Path(__file__).resolve().parents[1]
    code=("import sys;sys.path[:0]=[sys.argv[1],sys.argv[1]+'/src'];"
          "from scripts.hub_local_windows_parent import _preflight;"
          "from scripts.hub_local_fixture_metadata import decode_fixture;"
          "assert 'hub_container_executor.ledger' not in sys.modules;"
          "assert 'fcntl' not in sys.modules or sys.platform!='win32';"
          "exec('try:\\n _preflight({})\\nexcept KeyError:\\n pass');"
          "assert 'hub_container_executor.ledger' not in sys.modules")
    result=subprocess.run([sys.executable,'-I','-B','-c',code,str(root)],
        stdout=subprocess.PIPE,stderr=subprocess.PIPE,stdin=subprocess.DEVNULL,timeout=15,env=os.environ.copy())
    assert result.returncode==0, result.stderr.decode(errors='replace')


@pytest.mark.skipif(sys.platform=='win32',reason='executor runtime is Linux-only')
def test_public_executor_exports_keep_original_identities():
    import hub_container_executor as package
    from hub_container_executor.adapter import HostAdapter, KnownTerminalFailure
    from hub_container_executor.asgi import create_app
    from hub_container_executor.ledger import OwnerBusy, SQLiteLedger
    expected={'HostAdapter':HostAdapter,'KnownTerminalFailure':KnownTerminalFailure,
              'create_app':create_app,'OwnerBusy':OwnerBusy,'SQLiteLedger':SQLiteLedger}
    namespace={}
    exec('from hub_container_executor import *',namespace)
    for name,value in expected.items():
        assert getattr(package,name) is value and namespace[name] is value
    with pytest.raises(AttributeError): getattr(package,'missing_export')
