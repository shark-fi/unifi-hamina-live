"""Live LTE / 5G cell telemetry, projected into the same neutral models the
Wi-Fi side uses.

A cell is not an access point. Everything downstream of :mod:`..models` —
the neutral API, the dashboard, the Meraki facade, the Catalyst facade — speaks
access points, so presenting a cell as one is what lets a radio Hamina has never
heard of land on a Hamina map with no new plumbing. That is a **costume**, and
this package is deliberate about which parts of it are real:

* real, straight from the core: cell identity (PLMN / gNB or eNB id / TAC /
  NCI), which UEs are attached to which cell, session and activity state.
* real, declared by you in ``cells.json``: the radio's technology, band,
  ARFCN/EARFCN, bandwidth and TX power. The core does not know them — the RAN
  does — so they are configuration, not telemetry.
* invented: the Wi-Fi band and channel the cell reports. There is no honest
  mapping from n48 to a Wi-Fi channel; there is only a stable one. See
  :mod:`.rf`.

The invented parts never overwrite the real ones: the true carrier lives on
``Radio.carrier_mhz`` / ``Radio.carrier_label`` beside the costume, and every
cell-derived access point is marked ``source="cellular"``.
"""
