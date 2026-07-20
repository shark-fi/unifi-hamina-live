"""Structural-change (staleness) detection for the OpenIntent import."""

from unifi_hamina_live.config import Settings
from unifi_hamina_live.models import FloorPlan
from unifi_hamina_live.refresh.openintent import OpenIntentRefresher
from unifi_hamina_live.unifi import placement


def _fp(id="p1", name="Ground", w=1000, h=800, mpp=0.05, img="img-v1"):
    return FloorPlan(id=id, site_id="s1", name=name, source="innerspace",
                     width_px=w, height_px=h, meters_per_px=mpp, image_ref=img)


def test_signatures_ignore_ap_positions():
    # plan_signatures is derived only from the floor plan, so AP x,y can't
    # appear in it — same plan => same signature.
    assert placement.plan_signatures([_fp()]) == placement.plan_signatures([_fp()])


def test_diff_detects_add_remove_change():
    old = placement.plan_signatures([_fp("p1"), _fp("p2", name="Second")])
    new = placement.plan_signatures([_fp("p2", name="Second-renamed"), _fp("p3")])
    d = placement.diff_signatures(old, new)
    assert d["added"] == ["p3"] and d["removed"] == ["p1"] and d["changed"] == ["p2"]
    assert placement.has_changes(d)


def _refresher():
    r = OpenIntentRefresher(Settings())
    # simulate a completed export → baseline captured on next evaluate
    r._need_baseline = True
    return r


def test_ap_move_does_not_go_stale():
    r = _refresher()
    plans = [_fp()]
    assert r.evaluate(plans) is None      # baseline captured
    # an AP move changes nothing in the floor-plan signature
    assert r.evaluate(plans) is None
    assert r.stale is False


def test_map_rescale_goes_stale_then_recovers():
    r = _refresher()
    r.evaluate([_fp(mpp=0.05)])           # baseline
    action = r.evaluate([_fp(mpp=0.10)])  # scale changed
    assert action == "became_stale" and r.stale is True
    assert r.stale_detail["changed"] == ["p1"]
    # a fresh export re-baselines and clears
    r._need_baseline = True
    assert r.evaluate([_fp(mpp=0.10)]) is None
    assert r.stale is False


def test_replaced_image_goes_stale():
    r = _refresher()
    r.evaluate([_fp(img="img-v1")])
    assert r.evaluate([_fp(img="img-v2")]) == "became_stale"


def test_added_plan_goes_stale():
    r = _refresher()
    r.evaluate([_fp("p1")])
    assert r.evaluate([_fp("p1"), _fp("p2")]) == "became_stale"
    assert r.stale_detail["added"] == ["p2"]
