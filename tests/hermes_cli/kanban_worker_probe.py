"""Real CLI bootstrap for synthetic worker tests; never contact public services."""
import faulthandler
import ipaddress
import os
from pathlib import Path
import runpy
import sys

root = Path(sys.argv.pop(1)).resolve()
sys.path.insert(0, str(root))
import hermes_cli.kanban_db as kb

assert Path(kb.__file__).resolve().is_relative_to(root), kb.__file__
print(f"worker_boot pid={os.getpid()} source={kb.__file__}", flush=True)
faulthandler.dump_traceback_later(20, repeat=True)


def local_only(event, args):
    if event == "socket.connect" and isinstance(args[1], tuple):
        host = args[1][0]
        if not ipaddress.ip_address(host).is_loopback:
            import traceback
            traceback.print_stack()
            raise SystemExit(f"Synthetic worker attempted non-local connection: {host}")


sys.addaudithook(local_only)
runpy.run_path(str(root / "hermes"), run_name="__main__")
