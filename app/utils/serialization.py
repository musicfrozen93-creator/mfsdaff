"""
JSON Serialization Helpers

Recursively converts numpy/pandas types to native Python types
so that FastAPI/JSON serialization never fails on non-standard scalars.
"""

import numpy as np

# Optional pandas import — only used for type checking
try:
    import pandas as pd
    HAS_PANDAS = True
except ImportError:
    HAS_PANDAS = False


def clean_json_types(obj):
    """
    Recursively walk a dict/list/scalar and convert any numpy or pandas
    types into plain Python bool/int/float/str/None so the result is
    always JSON-serializable.

    Handles:
      - numpy.bool_          → bool
      - numpy.integer        → int
      - numpy.floating       → float
      - numpy.ndarray        → list  (recursive)
      - numpy.str_           → str
      - pandas.Timestamp     → ISO string
      - pandas NA / NaT      → None
      - dict / list           → recursive descent
      - everything else       → passthrough
    """
    # ── dict ──────────────────────────────────────────────────────────
    if isinstance(obj, dict):
        return {k: clean_json_types(v) for k, v in obj.items()}

    # ── list / tuple ──────────────────────────────────────────────────
    if isinstance(obj, (list, tuple)):
        return [clean_json_types(item) for item in obj]

    # ── numpy scalar types ────────────────────────────────────────────
    if isinstance(obj, (np.bool_,)):
        return bool(obj)

    if isinstance(obj, (np.integer,)):
        return int(obj)

    if isinstance(obj, (np.floating,)):
        value = float(obj)
        # Convert NaN/Inf to None for valid JSON
        if np.isnan(value) or np.isinf(value):
            return None
        return value

    if isinstance(obj, np.ndarray):
        return [clean_json_types(item) for item in obj.tolist()]

    if isinstance(obj, (np.str_,)):
        return str(obj)

    # ── pandas types (optional) ───────────────────────────────────────
    if HAS_PANDAS:
        if isinstance(obj, pd.Timestamp):
            return obj.isoformat()
        if pd.isna(obj):
            return None

    # ── native float NaN / Inf guard ─────────────────────────────────
    if isinstance(obj, float):
        if obj != obj or obj == float("inf") or obj == float("-inf"):
            return None

    return obj
