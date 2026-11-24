"""Within-sector percentile ranks, 0 (lowest) to 100 (highest)."""

import pandas as pd

MIN_SECTOR_SIZE = 8  # params v1 factors.min_sector_size


def percentile(values: pd.Series) -> pd.Series:
    """0 for the lowest, 100 for the highest; ties share the average; NaN stays NaN."""
    valid = values.dropna()
    out = pd.Series(float("nan"), index=values.index)
    if len(valid) == 1:
        out[valid.index] = 50.0
    elif len(valid) > 1:
        ranks = valid.rank(method="average")
        out[valid.index] = (ranks - 1) / (len(valid) - 1) * 100.0
    return out


def sector_percentiles(
    values: pd.Series, sectors: pd.Series, *, min_size: int = MIN_SECTOR_SIZE
) -> pd.Series:
    """Percentile of each value within its sector. A sector with fewer than `min_size` valid
    values is ranked against the whole cross-section instead (there is no parent group yet),
    so a tiny sector never hands out 0s and 100s on two or three stocks."""
    sectors = sectors.reindex(values.index)
    universe = percentile(values)
    out = pd.Series(float("nan"), index=values.index)
    for _sector, idx in values.groupby(sectors, dropna=False).groups.items():
        group = values.loc[idx]
        if group.notna().sum() >= min_size:
            out.loc[idx] = percentile(group)
        else:
            out.loc[idx] = universe.loc[idx]
    return out
