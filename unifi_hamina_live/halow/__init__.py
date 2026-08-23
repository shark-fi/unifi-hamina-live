"""Live Wi-Fi HaLow (802.11ah) telemetry, projected into the same neutral
models the Wi-Fi and cellular sides use.

A HaLow AP *is* an 802.11 access point, so far less of a costume is needed here
than for a cell. What it is not is a 2.4/5/6 GHz access point: 802.11ah runs in
sub-GHz spectrum (902-928 MHz in the US), with 1/2/4/8 MHz channels that have no
place on the band axis Hamina draws. The Morse Micro driver in these radios
already papers over that for ``mac80211``'s benefit — ``iwinfo`` reports the S1G
channel mapped onto a legacy 2.4 GHz channel number — and that mapped number is
exactly the kind of plausible-but-wrong frequency this project keeps refusing to
draw as if it were real.

So this package is deliberate about which parts are real:

* **Real, off the radio** (via the radio's ``ubus``/``iwinfo`` API): the AP's
  BSSID and SSID, its TX power and noise floor, the WPA mode, and — for every
  associated station — its MAC and signal. A HaLow AP has a real, globally
  unique BSSID, so unlike a cell it needs no synthetic identity at all.
* **Real, declared by you**: where the radio sits on a floor plan, and optional
  cosmetic overrides (display name, model). See :class:`.source.HalowSpec`.
* **Invented**: the Wi-Fi band and channel the AP reports downstream. There is
  no honest map from an S1G sub-GHz channel to a 2.4/5/6 GHz one; there is only
  a *stable, out-of-the-way* one, chosen so a HaLow AP does not read as a
  co-channel neighbour of a real UniFi radio. See :mod:`.rf`.

The invented parts never overwrite the real ones: the true carrier — 802.11ah,
its country, and the driver's mapped channel — lives on ``Radio.carrier_label``
beside the costume, ``Radio.technology`` is ``"halow"``, and every HaLow-derived
access point is marked ``source="halow"``. The exact S1G centre frequency is not
fabricated: the permitted ``ubus`` surface does not expose it, so
``Radio.carrier_mhz`` is left ``None`` rather than filled with the mapped
2.4 GHz number, which would be a wrong frequency dressed as a measurement.
"""
