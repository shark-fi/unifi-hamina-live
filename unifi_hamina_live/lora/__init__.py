"""Live LoRaWAN gateway telemetry, projected into the same neutral models the
Wi-Fi, cellular and HaLow sides use.

A MikroTik LoRa gateway (here a wAP LR8 running RouterOS 6) is not a Wi-Fi radio
and not an association point — it is a LoRaWAN *packet forwarder*, listening
across a sub-GHz band plan (US915 here, 902-928 MHz, eight 125 kHz uplink
channels plus a 500 kHz one) and forwarding what it hears to a network server.
So this package is deliberate about which parts are real:

* **Real, off the box** (via the RouterOS binary API on TCP 8728, since
  RouterOS 6 has no REST): the gateway's EUI, its channel-plan/region, the
  network server it forwards to, whether it is enabled, and the box's ethernet
  MAC — used as the gateway's stable 48-bit identity, because the 64-bit EUI is
  not itself a MAC.
* **Real, declared by you**: where the gateway sits on a floor plan, and
  optional cosmetic overrides (display name, model). See :class:`.source.LoraSpec`.
* **Invented**: the Wi-Fi band and channel the gateway reports downstream. There
  is no honest map from a sub-GHz LoRa band plan to a 2.4/5/6 GHz channel; there
  is only a *stable, out-of-the-way* one, chosen so the gateway does not read as
  a co-channel neighbour of a real UniFi radio. See :mod:`.rf`.

The invented parts never overwrite the real ones: the true carrier — that it is
a LoRaWAN gateway, its region/plan, EUI and upstream server — lives on
``Radio.carrier_label`` beside the costume, ``Radio.technology`` is ``"lora"``,
and every LoRa-derived access point is marked ``source="lora"``.
``Radio.carrier_mhz`` is left ``None`` on purpose: a gateway spreads across a
whole sub-band rather than sitting on one centre frequency, so there is no
single carrier to report, and inventing one would be a wrong number dressed as a
measurement.

The gateway is the only thing drawn. Its end devices are visible in the box's
live *Traffic* sniffer (DevAddr, frequency, RSSI, SNR), but that is a stream of
ephemeral, un-locatable sessions — a single gateway's RSSI cannot position a
device — so nothing here fabricates client positions from it.
"""
