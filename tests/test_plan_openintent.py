"""The OpenIntent floor-plan source, for consoles with no InnerSpace."""

import json
import logging
import zipfile

import pytest

from unifi_hamina_live.config import Settings
from unifi_hamina_live.models import AccessPoint, Site
from unifi_hamina_live.plan import load_openintent_plan, plan_id_for
from unifi_hamina_live.unifi.collector import Collector
from unifi_hamina_live.unifi.placement import scene_to_pixels


def _zip(tmp_path, doc, images=None, name="plan.zip"):
    path = tmp_path / name
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("openintent.json", json.dumps(doc))
        for n, b in (images or {}).items():
            z.writestr(n, b)
    return str(path)


def _doc(aps, w=2312.0, length=719.0, m_w=42.83, plan="Map"):
    return {
        "openintent_version": "2.0.1",
        "floorplans": [{
            "name": plan,
            "map_uri": "file://images/Map.png",
            "dimensions": [
                # `height` here is the CEILING, not the plan's second axis.
                {"width": w, "length": length, "height": 108.0, "unit": "pixels"},
                {"width": m_w, "length": 13.32, "height": 2.0, "unit": "meters"},
            ],
        }],
        "accesspoints": aps,
    }


def _ap(name, x, y, mac=None, plan="Map"):
    ap = {
        "name": name,
        "floorplan_name": plan,
        "coordinates": [
            {"coordinate_xyz": {"x": x, "y": y, "z": 108.0, "unit": "pixels"}},
            {"coordinate_xyz": {"x": x / 54.0, "y": y / 54.0, "z": 2.0,
                                "unit": "meters"}},
        ],
    }
    if mac:
        ap["mac_address"] = mac
    return ap


# --- the claim everything else rests on -----------------------------------

def test_coordinates_match_what_innerspace_would_have_produced(tmp_path):
    """An OpenIntent pixel IS a bridge plan pixel — same space, no conversion.

    Both sides re-centre about the image middle with no y flip, so an InnerSpace
    scene point and the OpenIntent pixel for the same AP must land on the same
    plan coordinate. A y flip on either side mirrors the whole plan about its
    middle, which looks plausible on a symmetric building and is why this is
    asserted rather than eyeballed.
    """
    img_w, img_h = 2312.0, 719.0
    map_shape = {"position": [{"x": 0.0, "y": 0.0}], "scale": {"x": 1.0, "y": 1.0}}
    # An AP well off centre on BOTH axes, so a flip or a transpose fails here.
    scene = {"x": -761.38, "y": 87.26}
    is_x, is_y = scene_to_pixels(scene, map_shape, img_w, img_h)

    plan = load_openintent_plan(
        _zip(tmp_path, _doc([_ap("AP1", is_x, is_y)])), "site-1")

    assert (plan.aps[0].x, plan.aps[0].y) == pytest.approx((is_x, is_y))
    # and specifically NOT the flip
    assert plan.aps[0].y != pytest.approx(img_h - is_y)


def test_real_export_round_trips(tmp_path):
    """Dimensions and scale survive, read from the fields a real export uses."""
    plan = load_openintent_plan(
        _zip(tmp_path, _doc([_ap("AP1", 394.62, 446.76)]),
             {"images/Map.png": b"\x89PNG\r\n\x1a\n"}), "site-1")

    fp = plan.floorplans[0]
    assert (fp.width_px, fp.height_px) == (2312.0, 719.0)
    assert fp.meters_per_px == pytest.approx(42.83 / 2312.0)
    assert fp.source == "openintent"
    assert plan.images[fp.id] == b"\x89PNG\r\n\x1a\n"


def test_ceiling_height_is_not_mistaken_for_the_plan_height(tmp_path):
    """`height` on a dimensions entry is the ceiling; the axis is `length`."""
    plan = load_openintent_plan(_zip(tmp_path, _doc([])), "site-1")
    assert plan.floorplans[0].height_px == 719.0  # not 108.0


def test_metre_coordinates_are_not_read_as_pixels(tmp_path):
    """Positions are published twice; the pixel one is chosen by unit, not order."""
    ap = _ap("AP1", 394.62, 446.76)
    ap["coordinates"].reverse()  # metres first — legal, and a 43-px plan if naive
    plan = load_openintent_plan(_zip(tmp_path, _doc([ap])), "site-1")
    assert plan.aps[0].x == pytest.approx(394.62)


def test_plan_ids_are_stable_across_regeneration(tmp_path):
    """cells.json references plans by id, so a re-export must not renumber them."""
    a = load_openintent_plan(_zip(tmp_path, _doc([]), name="a.zip"), "site-1")
    b = load_openintent_plan(_zip(tmp_path, _doc([]), name="b.zip"), "site-1")
    assert a.floorplans[0].id == b.floorplans[0].id == plan_id_for("Map")


def test_plans_with_the_same_slug_stay_separate(tmp_path):
    assert plan_id_for("Floor 1") != plan_id_for("Floor-1")


def test_a_zip_that_is_not_openintent_says_so(tmp_path):
    path = tmp_path / "x.zip"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("data.json", json.dumps({"hello": "world"}))
    with pytest.raises(ValueError, match="floorplans"):
        load_openintent_plan(str(path), "site-1")


# --- the join, through the collector --------------------------------------

def _live(name, mac, plan=None, x=None, y=None):
    return AccessPoint(site_id="site-1", mac=mac, name=name, serial="SER" + mac[-2:],
                       model_code="U7PRO", model="u7-pro", online=True,
                       floorplan_id=plan, x=x, y=y)


def _collector(tmp_path, zip_path):
    settings = Settings(unifi_host="https://x", unifi_username="u",
                        unifi_password="p", plan_source="openintent",
                        plan_openintent_zip=zip_path)
    return Collector(settings)


def test_collector_places_live_aps_from_the_zip(tmp_path):
    z = _zip(tmp_path, _doc([_ap("Kitchen", 394.62, 446.76,
                                 mac="94:2A:6F:32:AD:DE")]),
             {"images/Map.png": b"\x89PNG"})
    c, aps, fps = _collector(tmp_path, z), [_live("Kitchen", "94:2a:6f:32:ad:de")], []

    c._openintent_placement(aps, fps, [Site(id="site-1", name="HQ", num_aps=1)])

    assert (aps[0].floorplan_id, aps[0].x, aps[0].y) == (fps[0].id, 394.62, 446.76)
    assert fps[0].source == "openintent"
    # the image must be served too, or the dashboard draws APs onto nothing
    assert c.floor_image(fps[0].id) == b"\x89PNG"


def test_a_mac_match_beats_a_rename_in_hamina(tmp_path):
    """Hamina lets you rename an AP; the MAC is the identity that survives it."""
    z = _zip(tmp_path, _doc([_ap("Renamed In Hamina", 100.0, 200.0,
                                 mac="94:2A:6F:32:AD:DE")]))
    c, aps = _collector(tmp_path, z), [_live("Kitchen", "94:2a:6f:32:ad:de")]

    c._openintent_placement(aps, [], [Site(id="site-1", name="HQ", num_aps=1)])

    assert aps[0].x == 100.0


def test_name_join_when_the_export_carries_no_mac(tmp_path):
    """Hamina-authored exports need not publish a MAC — the name is all there is."""
    z = _zip(tmp_path, _doc([_ap("  U7-Pro   Kitchen ", 1.0, 2.0)]))
    c, aps = _collector(tmp_path, z), [_live("U7-Pro Kitchen", "aa:bb:cc:00:11:22")]

    c._openintent_placement(aps, [], [Site(id="site-1", name="HQ", num_aps=1)])

    assert (aps[0].x, aps[0].y) == (1.0, 2.0)


def test_an_ap_matching_nothing_is_named_in_the_log(tmp_path, caplog):
    """0-of-4 silent mismatches are how this failure has actually shown up."""
    z = _zip(tmp_path, _doc([_ap("Access Point 1", 1.0, 2.0)]))
    c = _collector(tmp_path, z)

    with caplog.at_level(logging.WARNING):
        c._openintent_placement([_live("Kitchen", "aa:bb:cc:00:11:22")], [],
                                [Site(id="site-1", name="HQ", num_aps=1)])

    assert "Access Point 1" in caplog.text


def test_an_already_placed_ap_is_left_alone(tmp_path):
    """Classic Maps ran first; the zip is a fallback, not an override."""
    z = _zip(tmp_path, _doc([_ap("Kitchen", 1.0, 2.0, mac="94:2A:6F:32:AD:DE")]))
    c = _collector(tmp_path, z)
    aps = [_live("Kitchen", "94:2a:6f:32:ad:de", plan="legacy-1", x=9.0, y=9.0)]

    c._openintent_placement(aps, [], [Site(id="site-1", name="HQ", num_aps=1)])

    assert (aps[0].floorplan_id, aps[0].x) == ("legacy-1", 9.0)


def test_an_unreadable_zip_is_an_error_not_a_crash(tmp_path):
    """A missing mount must degrade to "no plans", not take the poll down."""
    c = _collector(tmp_path, str(tmp_path / "absent.zip"))
    fps = []

    c._openintent_placement([], fps, [Site(id="site-1", name="HQ", num_aps=1)])

    assert fps == []


# --- startup --------------------------------------------------------------

def test_the_openintent_refresher_is_not_started_against_a_zip_plan(tmp_path,
                                                                    caplog):
    """The refresher exports plans OUT of InnerSpace, which this console lacks.

    Left running it fails on a timer, and the log then blames a missing app
    instead of the setting that can never work here.
    """
    from fastapi.testclient import TestClient

    from unifi_hamina_live.app import create_app

    settings = Settings(unifi_host="https://x", unifi_username="u",
                        unifi_password="p", plan_source="openintent",
                        plan_openintent_zip=str(tmp_path / "plan.zip"),
                        openintent_refresh_enabled=True, poll_seconds=3600)
    app = create_app(settings=settings)
    with caplog.at_level(logging.WARNING), TestClient(app) as client:
        assert client.get("/api/health").status_code == 200
        assert getattr(app.state, "refresher", None) is None

    assert "OPENINTENT_REFRESH_ENABLED is ignored" in caplog.text
