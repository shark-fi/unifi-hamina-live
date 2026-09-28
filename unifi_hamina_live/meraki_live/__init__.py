"""Live Cisco Meraki APs, read from the Dashboard API and projected into the same
neutral models the UniFi, cellular, HaLow and LoRa sides use.

Not to be confused with :mod:`..meraki`, which runs the other way: it *serves*
a Meraki-shaped API backed by UniFi data. This package *reads* a real Meraki
organization, so an MR / CW access point can sit on the same map as the UniFi
APs around it — the Hamina panel, the dashboard and the neutral API all see it
with no new plumbing.

Everything here is real — a Meraki AP is a Wi-Fi radio, so unlike the cellular,
HaLow and LoRa sources there is no costume:

* per band: channel, width and power from ``devices/{serial}/wireless/status``
  (the BSS that is actually broadcasting), and channel busy time from the org's
  ``wireless/devices/channelUtilization/byDevice``;
* online state from ``organizations/{org}/devices/statuses``;
* clients from ``networks/{net}/clients``, joined to their AP by
  ``recentDeviceSerial``. Meraki's client list carries SSID and IP but no
  per-client signal or band, so those stay ``None`` rather than guessed.

Every Meraki-derived access point is marked ``source="meraki"``.
"""
