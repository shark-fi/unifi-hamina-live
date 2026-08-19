"""Floor plans from somewhere other than the console.

InnerSpace is the usual source, but it is a UniFi application and not every
console has it — a UniFi Express 7 cannot install it at all. Without a plan
there is no AP placement, no RSSI locate (it needs a scale), and nowhere to put
a private cellular cell, because every placement mechanism resolves against a
floor plan.

Hamina already holds those plans, and its OpenIntent export carries the image,
the dimensions in pixels AND metres, and the AP positions. So the map can come
from there and the live data from UniFi, joined by name.
"""

from .openintent import (
    OpenIntentPlan, PlanAP, load_openintent_plan, norm_name, plan_id_for,
)

__all__ = ["OpenIntentPlan", "PlanAP", "load_openintent_plan", "norm_name",
           "plan_id_for"]
