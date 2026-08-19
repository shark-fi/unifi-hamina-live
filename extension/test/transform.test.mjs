/* node --test extension/test/ */
import { test } from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const { fitPlanToScreen, applyTransform, invertTransform, transformQuality } =
  require("../src/transform.js");

/** Screen points generated from a known mapping, so the fit has a right answer. */
const gen = (plans, sx, tx, sy, ty, noise = () => 0) =>
  plans.map((p, i) => ({
    plan: p,
    screen: { x: sx * p.x + tx + noise(i), y: sy * p.y + ty + noise(i) },
  }));

const PLANS = [{ x: 100, y: 200 }, { x: 800, y: 250 }, { x: 400, y: 600 },
               { x: 950, y: 90 }];

test("recovers the mapping it was generated from", () => {
  const t = fitPlanToScreen(gen(PLANS, 0.5, 30, -0.5, 700));
  assert.ok(Math.abs(t.sx - 0.5) < 1e-9);
  assert.ok(Math.abs(t.tx - 30) < 1e-6);
  assert.ok(Math.abs(t.sy + 0.5) < 1e-9);
  assert.ok(Math.abs(t.ty - 700) < 1e-6);
  assert.ok(t.rms < 1e-9);
});

test("the y-flip comes out of the data, not an assumption", () => {
  // InnerSpace plan y grows one way and the screen the other. Asserting that
  // instead of measuring it is what mirrored every AP about the plan's middle
  // in an earlier version.
  const t = fitPlanToScreen(gen(PLANS, 1, 0, -1, 719));
  assert.ok(t.sy < 0, "a downward screen axis must appear as a negative scale");
  const top = applyTransform(t, { x: 0, y: 700 });
  const bottom = applyTransform(t, { x: 0, y: 0 });
  assert.ok(top.y < bottom.y, "high plan y must land near the top of the screen");
});

test("a point round-trips through the inverse", () => {
  const t = fitPlanToScreen(gen(PLANS, 0.42, -17, -0.42, 512));
  const plan = { x: 613, y: 271 };
  const back = invertTransform(t, applyTransform(t, plan));
  assert.ok(Math.abs(back.x - plan.x) < 1e-6);
  assert.ok(Math.abs(back.y - plan.y) < 1e-6);
});

test("fewer than two labels is no fit at all", () => {
  assert.equal(fitPlanToScreen([]), null);
  assert.equal(fitPlanToScreen(gen([PLANS[0]], 1, 0, 1, 0)), null);
});

test("labels in a straight line cannot determine both axes", () => {
  // Every AP at the same plan x — common on a corridor — leaves the x scale
  // unconstrained. Better to refuse than to invent one.
  const column = [{ x: 500, y: 100 }, { x: 500, y: 300 }, { x: 500, y: 600 }];
  assert.equal(fitPlanToScreen(gen(column, 1, 0, -1, 700)), null);
});

test("malformed entries are dropped rather than poisoning the fit", () => {
  const pairs = gen(PLANS, 0.5, 30, -0.5, 700);
  pairs.push({ plan: { x: NaN, y: 1 }, screen: { x: 1, y: 1 } });
  pairs.push({ plan: { x: 1, y: 1 } });               // no screen
  pairs.push(null);
  const t = fitPlanToScreen(pairs);
  assert.equal(t.n, 4);
  assert.ok(Math.abs(t.sx - 0.5) < 1e-9);
});

test("noise shows up in the residual", () => {
  const t = fitPlanToScreen(gen(PLANS, 0.5, 30, -0.5, 700, (i) => (i % 2 ? 6 : -6)));
  assert.ok(t.rms > 1, `expected a visible residual, got ${t.rms}`);
});

test("two labels are usable but never verified", () => {
  // Two points fit exactly: the residual is structurally zero and proves
  // nothing, the same trap as a three-anchor position fix.
  const t = fitPlanToScreen(gen(PLANS.slice(0, 2), 0.5, 30, -0.5, 700));
  const q = transformQuality(t);
  assert.equal(t.rms, 0);
  assert.equal(q.usable, true);
  assert.equal(q.verified, false, "a zero residual on 2 points is not evidence");
});

test("a fit that disagrees with its own labels is refused", () => {
  const t = fitPlanToScreen(gen(PLANS, 0.5, 30, -0.5, 700, (i) => i * 90));
  const q = transformQuality(t);
  assert.equal(q.usable, false);
  assert.match(q.why, /disagree/);
});

test("three good labels are verified", () => {
  const q = transformQuality(fitPlanToScreen(gen(PLANS, 0.5, 30, -0.5, 700)));
  assert.equal(q.usable, true);
  assert.equal(q.verified, true);
});
