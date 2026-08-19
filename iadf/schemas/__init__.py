from pathlib import Path

SCHEMA_DIR = Path(__file__).parent
RESULT_ALGEBRA = ('PASS', 'FAIL', 'NOT_RUN', 'SKIPPED', 'UNKNOWN', 'ERROR', 'INCONCLUSIVE', 'TIMEOUT', 'STALE', 'EXPIRED', 'SUPERSEDED')

class SchemaValidationError(Exception): pass
def load_schema(name): return {}
def validate_document(data, name): pass
