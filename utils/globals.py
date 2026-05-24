"""Global Function files"""

import uuid
from datetime import datetime

def to_bool(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() in ("true", "1", "yes", "y", "on")
    return bool(value)

def to_int(value):
    try:
        return int(value)
    except:
        return value

def generate_id(prefix: str = "") -> str:
    """Generate unique ID"""
    return f"{prefix}{uuid.uuid4().hex[:12]}"


def get_timestamp() -> int:
    """Get current timestamp"""
    return int(datetime.now().timestamp())
