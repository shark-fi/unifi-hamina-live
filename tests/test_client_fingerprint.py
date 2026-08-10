"""Fields a map overlay needs from a client: alias, fingerprint id, vendor.

dev_id keys UniFi's own client icon on its CDN
(static.ui.com/fingerprint/0/<dev_id>_101x101.png), so it has to survive the
console's several ways of reporting it — including as a string, and including a
user override that must win.
"""
from unifi_hamina_live.unifi.normalize import client


def _sta(**kw):
    base = {"mac": "aa:bb:cc:dd:ee:ff", "ap_mac": "11:22:33:44:55:66"}
    base.update(kw)
    return base


def test_alias_is_kept_apart_from_hostname():
    c = client(_sta(hostname="iPhone-12", name="Jenn's iPhone"), "default", {})
    assert c.name == "Jenn's iPhone"
    assert c.hostname == "iPhone-12"


def test_alias_absent_leaves_name_unset_but_hostname_still_falls_back():
    c = client(_sta(name="Living Room TV"), "default", {})
    assert c.name == "Living Room TV"
    assert c.hostname == "Living Room TV"      # long-standing fallback, unchanged
    assert client(_sta(hostname="nas"), "default", {}).name is None


def test_dev_id_from_each_shape_the_console_uses():
    assert client(_sta(dev_id=4488), "default", {}).dev_id == 4488
    assert client(_sta(dev_id="4488"), "default", {}).dev_id == 4488
    assert client(_sta(fingerprint={"computed_dev_id": 91}), "default", {}).dev_id == 91
    assert client(_sta(fingerprint={"dev_id": 92}), "default", {}).dev_id == 92


def test_dev_id_override_wins_because_the_console_renders_it():
    c = client(_sta(dev_id=1, dev_id_override=4488), "default", {})
    assert c.dev_id == 4488


def test_dev_id_absent_or_unusable_is_none_not_an_error():
    assert client(_sta(), "default", {}).dev_id is None
    assert client(_sta(dev_id=None), "default", {}).dev_id is None
    assert client(_sta(dev_id=""), "default", {}).dev_id is None
    assert client(_sta(dev_id="not-a-number"), "default", {}).dev_id is None
    assert client(_sta(fingerprint=None), "default", {}).dev_id is None
    # True would coerce to 1 and point at some unrelated device type
    assert client(_sta(dev_id=True), "default", {}).dev_id is None


def test_vendor_comes_from_the_oui():
    assert client(_sta(oui="Apple, Inc."), "default", {}).vendor == "Apple, Inc."
    assert client(_sta(oui=""), "default", {}).vendor is None
    assert client(_sta(), "default", {}).vendor is None
