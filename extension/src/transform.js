/* Fitting plan coordinates to screen coordinates.
 *
 * The overlay pins to the rects of `section[data-testid^="stats-tooltip-"]`,
 * which means it can only decorate an AP InnerSpace draws a label for. Anything
 * without a label — an AP the console does not annotate, a wall, a transmitter
 * we located ourselves — has nowhere to go.
 *
 * But every labelled AP is a correspondence: we know its position in plan
 * pixels AND where its label is on screen. A handful of those determine the
 * mapping, and then anything expressed in plan pixels can be drawn.
 *
 * Deliberately axis-independent rather than a similarity transform. InnerSpace
 * never rotates the canvas, and fitting x and y separately absorbs a
 * non-square pixel aspect without needing a rotation term nobody can observe.
 * It also makes the y-flip fall out of the data (sy comes back negative)
 * instead of being asserted — and a y convention asserted rather than measured
 * is how APs ended up mirrored about the middle of the plan before.
 */
"use strict";

/** Least-squares fit of screen = scale * plan + offset, per axis.
 *  pairs: [{ plan: {x, y}, screen: {x, y} }, ...]
 *  Returns null when the input cannot determine a mapping. */
function fitPlanToScreen(pairs) {
  const pts = (pairs || []).filter(
    (p) => p && p.plan && p.screen &&
      Number.isFinite(p.plan.x) && Number.isFinite(p.plan.y) &&
      Number.isFinite(p.screen.x) && Number.isFinite(p.screen.y));
  if (pts.length < 2) return null;

  const axis = (get) => {
    const xs = pts.map((p) => get(p.plan));
    const ys = pts.map((p) => get(p.screen));
    const n = xs.length;
    const mx = xs.reduce((a, b) => a + b, 0) / n;
    const my = ys.reduce((a, b) => a + b, 0) / n;
    let sxx = 0, sxy = 0;
    for (let i = 0; i < n; i++) {
      sxx += (xs[i] - mx) ** 2;
      sxy += (xs[i] - mx) * (ys[i] - my);
    }
    // Every AP at the same plan x (a vertical row) determines no x scale.
    if (sxx < 1e-9) return null;
    const scale = sxy / sxx;
    return { scale, offset: my - scale * mx };
  };

  const fx = axis((p) => p.x);
  const fy = axis((p) => p.y);
  if (!fx || !fy) return null;

  const t = { sx: fx.scale, tx: fx.offset, sy: fy.scale, ty: fy.offset,
              n: pts.length };
  // Residual in SCREEN pixels: how far the fit misses the labels it was built
  // from. A large value means the labels are not where the plan says, which is
  // the console having moved on — not a reason to draw anyway.
  let sum = 0;
  for (const p of pts) {
    const q = applyTransform(t, p.plan);
    sum += (q.x - p.screen.x) ** 2 + (q.y - p.screen.y) ** 2;
  }
  t.rms = Math.sqrt(sum / pts.length);
  return t;
}

/** Plan pixels -> screen pixels. */
function applyTransform(t, plan) {
  return { x: t.sx * plan.x + t.tx, y: t.sy * plan.y + t.ty };
}

/** Screen -> plan, for hit-testing. Null if the transform is degenerate. */
function invertTransform(t, screen) {
  if (!t || Math.abs(t.sx) < 1e-12 || Math.abs(t.sy) < 1e-12) return null;
  return { x: (screen.x - t.tx) / t.sx, y: (screen.y - t.ty) / t.sy };
}

/** Is this fit good enough to place things nobody can see the truth of?
 *
 *  Two points always fit exactly — the residual is structurally zero and says
 *  nothing, the same trap as a three-anchor position fix. So a two-point fit is
 *  usable but never "verified", and callers are told which they have. */
function transformQuality(t, maxRmsPx = 24) {
  if (!t) return { usable: false, verified: false, why: "no fit" };
  if (t.n < 2) return { usable: false, verified: false, why: "need 2 labels" };
  if (!Number.isFinite(t.rms)) {
    return { usable: false, verified: false, why: "fit did not converge" };
  }
  if (t.n === 2) {
    return { usable: true, verified: false,
             why: "2 labels fit exactly — residual proves nothing" };
  }
  if (t.rms > maxRmsPx) {
    return { usable: false, verified: false,
             why: `labels disagree with the plan by ${Math.round(t.rms)} px` };
  }
  return { usable: true, verified: true, why: `${t.n} labels, ${t.rms.toFixed(1)} px rms` };
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = { fitPlanToScreen, applyTransform, invertTransform,
                     transformQuality };
}
