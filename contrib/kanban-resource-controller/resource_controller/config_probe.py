"""Source-pinned read-only config reader, in a fresh profile scope per call."""
import json
from pathlib import Path
import sys

sys.path.insert(0, sys.argv[1])
from hermes_cli.config import load_config, require_readable_config_before_write

# Despite its name this validator only reads; unlike load_config alone it
# refuses malformed user YAML instead of silently falling back to defaults.
import os
require_readable_config_before_write(Path(os.environ['HERMES_HOME']) / 'config.yaml')
print(json.dumps(load_config()['kanban']))
