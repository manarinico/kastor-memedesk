"""Three-way decision state. Missing or stale data is NEVER permission."""
import math
from dataclasses import dataclass, field, asdict

ALLOW = "ALLOW"
BLOCK = "BLOCK"
INSUFFICIENT = "INSUFFICIENT_DATA"


@dataclass
class Gate:
    name: str
    state: str
    reason: str = ""
    value: object = None

    def to_dict(self):
        return asdict(self)


def allow(name, value=None, reason="ok"):
    return Gate(name, ALLOW, reason, value)


def block(name, reason, value=None):
    return Gate(name, BLOCK, reason, value)


def insufficient(name, reason, value=None):
    return Gate(name, INSUFFICIENT, reason, value)


def combine(gates):
    """BLOCK beats INSUFFICIENT beats ALLOW. Empty list = INSUFFICIENT (nothing proved)."""
    if not gates:
        return insufficient("combined", "no gates evaluated")
    for g in gates:
        if g.state == BLOCK:
            return g
    for g in gates:
        if g.state == INSUFFICIENT:
            return g
    for g in gates:
        if g.state != ALLOW:  # unknown state string -> treat as insufficient
            return insufficient(g.name, f"unknown state {g.state!r}")
    return allow("combined")


def num(obj, *path):
    """Safe nested numeric getter. Returns None for missing / non-numeric / NaN / inf."""
    cur = obj
    for p in path:
        if isinstance(cur, dict) and p in cur:
            cur = cur[p]
        elif isinstance(cur, list) and isinstance(p, int) and -len(cur) <= p < len(cur):
            cur = cur[p]
        else:
            return None
    if isinstance(cur, bool) or cur is None:
        return None
    try:
        v = float(cur)
    except (TypeError, ValueError):
        return None
    if math.isnan(v) or math.isinf(v):
        return None
    return v
