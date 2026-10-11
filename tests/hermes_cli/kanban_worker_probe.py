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

if '--redaction-unavailable' in sys.argv:
    sys.argv.remove('--redaction-unavailable')
    def unavailable_redactor(*args, **kwargs):
        raise RuntimeError('synthetic redactor unavailable')
    kb.redact_review_value = unavailable_redactor

assert Path(kb.__file__).resolve().is_relative_to(root), kb.__file__
if '--replace-request-route' in sys.argv:
    index = sys.argv.index('--replace-request-route')
    replacement = sys.argv[index + 1]
    del sys.argv[index:index + 2]
    from agent.client_lifecycle import ClientLifecycleMixin
    original_request_client = ClientLifecycleMixin._create_request_openai_client
    requests_created = 0
    def replace_request_route(self, **kwargs):
        global requests_created
        requests_created += 1
        if requests_created == 1:
            self.base_url = replacement
            self._client_kwargs['base_url'] = replacement
        return original_request_client(self, **kwargs)
    ClientLifecycleMixin._create_request_openai_client = replace_request_route
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
