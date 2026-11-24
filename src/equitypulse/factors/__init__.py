"""Factor inputs and sector-relative factor percentiles (docs/phase1a_spec.md, "Factors").

Every input for date D uses only bars dated on or before D (rolling windows look back). Input
functions take one security's split-adjusted, ISIN-stitched daily frame from `adjust`; the
cross-section turns inputs into within-sector percentiles (0-100).
"""
