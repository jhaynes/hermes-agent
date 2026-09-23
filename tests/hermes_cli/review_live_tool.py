"""Bridge only to the already-installed canonical tools, never reviewed core.

Invoked by review_submission with a clean Python import path. Running the feature
checkout's Kanban DB code against the live board would prematurely activate its
schema; verify the imported tool and DB paths before opening any board instead.
"""
import json
from pathlib import Path
import sys


def main():
    runtime = Path(sys.argv[1]).resolve()
    sys.path.insert(0, str(runtime))
    from tools import kanban_tools
    from hermes_cli import kanban_db, kanban_db_connect

    for module in (kanban_tools, kanban_db, kanban_db_connect):
        if not module.__file__ or not Path(module.__file__).resolve().is_relative_to(runtime):
            raise RuntimeError(f'Wrong canonical runtime: {module.__file__}')
    request = json.load(sys.stdin)
    handlers = {'kanban_show': kanban_tools._handle_show,
                'kanban_complete': kanban_tools._handle_complete,
                'kanban_comment': kanban_tools._handle_comment}
    print(handlers[request['name']](request['args']))


if __name__ == '__main__':
    main()
