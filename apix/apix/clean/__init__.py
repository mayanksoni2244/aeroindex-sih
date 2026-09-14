"""
Cleaning layer.

  pipeline.py — dedupe -> validate -> outlier-flag -> impute -> normalize,
                in that fixed order, flagging rather than deleting.
"""
from apix.clean.pipeline import (  # noqa: F401
    CleanReport,
    clean_cycle,
    flag_outliers,
    impute_missing,
    normalize,
    validate,
)
