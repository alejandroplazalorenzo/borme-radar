"""borme-radar: keep company records in sync with the BORME and discover new companies
of watched groups.

Flow: download (``source``, ``download``) -> parse (``parser``, ``acts``, ``officers``)
-> match (``matching``) -> keep what matters and derive the history (``pipeline``,
``record``) -> discover (``discovery``, ``relations``) -> store (``store``) -> report.
Calibration and evaluation read the cached corpus offline (``corpus``, ``evaluate``).

Left out on purpose: Section C (legal notices, free prose) and competitor detection by
corporate purpose.
"""

__version__ = "0.2.0"
