from __future__ import annotations

SCHEMA_VERSION = 2

# Pressure enum values (kept identical across OSes and unchanged from schema 1).
PRESSURE_NORMAL = "normal"
PRESSURE_WARNING = "warning"
PRESSURE_CRITICAL = "critical"
PRESSURE_VALUES = frozenset({PRESSURE_NORMAL, PRESSURE_WARNING, PRESSURE_CRITICAL})

# Backend source identifiers.
SOURCE_DARWIN = "darwin-vm_stat+sysctl"
SOURCE_LINUX = "linux-proc"

# Policy/admission reason strings (post swap* rename).
REASON_UNKNOWN_TELEMETRY = "unknown-telemetry"
REASON_SWAP_BASELINE = "swap-baseline"
REASON_NONMONOTONIC_TIME = "nonmonotonic-time"
REASON_SAMPLE_GAP = "sample-gap"
REASON_SWAP_RESET = "swap-reset"
REASON_LOAD = "load"
REASON_PRESSURE = "pressure"
REASON_MEMORY = "memory"
REASON_SWAP_OUT = "swap-out"
REASON_RECOVERY_LOAD = "recovery-load"
REASON_RECOVERY_MEMORY = "recovery-memory"
REASON_RECOVERY_DWELL = "recovery-dwell"
REASON_ELIGIBLE = "eligible"
REASON_TELEMETRY_ERROR = "telemetry-error"

# Telemetry error codes (separate from the sanitized human diagnostic).
ERROR_PSI_UNAVAILABLE = "psi-unavailable"
ERROR_VM_STAT_TIMEOUT = "vm_stat-timeout"
ERROR_PARSE_ERROR = "parse-error"
ERROR_UNSUPPORTED_PLATFORM = "unsupported-platform"
ERROR_READ_ERROR = "read-error"

MAX_DIAGNOSTIC_LEN = 300

# Darwin subprocess/parse limits.
DARWIN_MAX_OUTPUT_BYTES = 16 * 1024
DARWIN_TIMEOUT_SECONDS = 1.0

# Linux procfs read limits.
LINUX_READ_CAP_BYTES = 65536
LINUX_READ_BYTES = LINUX_READ_CAP_BYTES + 1

# Byte-value safety ceiling: must stay exactly representable as a JSON double.
MAX_SAFE_BYTES = 2**53

MIN_PAGE_SIZE = 4096
MAX_PAGE_SIZE = 65536

# D2 defaults for Linux PSI thresholds (overridable via controller.json).
DEFAULT_LINUX_PSI_SOME_AVG10_WARNING = 10.0
DEFAULT_LINUX_PSI_FULL_AVG10_CRITICAL = 0.0
