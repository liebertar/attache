// Display geometry only. Runtime checks and simulator motion use the original legs.
const xy = p => [p.lon, p.lat];
const same = (a, b) => a.lat === b.lat && a.lon === b.lon && a.alt_m === b.alt_m;
const mix = (a, b, t) => a.map((v, i) => v + (b[i] - v) * t);

export function isRemainingRoute(previous, next) {
  return next.length <= previous.length && next.every((p, i) =>
    same(p, previous[previous.length - next.length + i]));
}

export function makeCurve(position, route, steps = 16) {
  const points = [xy(position), ...route.map(xy)].filter((p, i, all) =>
    !i || p.some((v, axis) => v !== all[i - 1][axis]));
  const coordinates = [], progress = [], lengths = [0];
  // Local longitude scale keeps distances appropriate for Manhattan.
  const scale = Math.cos(position.lat * Math.PI / 180);
  for (let i = 1; i < points.length; i++) {
    lengths.push(lengths[i - 1] + Math.hypot(
      (points[i][0] - points[i - 1][0]) * scale, points[i][1] - points[i - 1][1]));
  }
  for (let i = 0; i < points.length - 1; i++) {
    const a = points[Math.max(0, i - 1)], b = points[i];
    const c = points[i + 1], d = points[Math.min(points.length - 1, i + 2)];
    for (let j = 0; j < steps; j++) {
      const t = j / steps;
      // Catmull-Rom interpolation; never used to approve a route.
      coordinates.push(b.map((v, axis) => .5 * (
        2 * v + (-a[axis] + c[axis]) * t +
        (2 * a[axis] - 5 * v + 4 * c[axis] - d[axis]) * t * t +
        (-a[axis] + 3 * v - 3 * c[axis] + d[axis]) * t * t * t)));
      progress.push(lengths[i] + (lengths[i + 1] - lengths[i]) * t);
    }
  }
  coordinates.push(points.at(-1));
  progress.push(lengths.at(-1));
  return {points, coordinates, progress, lengths, scale};
}

export function routeProgress(curve, position, minimum = 0) {
  const p = xy(position);
  let best = minimum, distance = Infinity;
  for (let i = 0; i < curve.points.length - 1; i++) {
    if (curve.lengths[i + 1] < minimum) continue;
    const a = curve.points[i], b = curve.points[i + 1];
    const dx = (b[0] - a[0]) * curve.scale, dy = b[1] - a[1];
    const t = Math.max(0, Math.min(1,
      (((p[0] - a[0]) * curve.scale) * dx + (p[1] - a[1]) * dy) / (dx * dx + dy * dy)));
    const at = mix(a, b, t);
    const error = Math.hypot((p[0] - at[0]) * curve.scale, p[1] - at[1]);
    if (error < distance) {
      distance = error;
      best = Math.max(minimum, curve.lengths[i] + t * (curve.lengths[i + 1] - curve.lengths[i]));
    }
  }
  return best;
}

export function pointOnCurve(curve, progress) {
  const i = curve.progress.findIndex(p => p > progress);
  if (i < 0) return curve.coordinates.at(-1);
  if (!i) return curve.coordinates[0];
  return mix(curve.coordinates[i - 1], curve.coordinates[i],
    (progress - curve.progress[i - 1]) / (curve.progress[i] - curve.progress[i - 1]));
}

export function motionPoint(motion, now, duration) {
  const t = Math.max(0, Math.min(1, (now - motion.at) / duration));
  return motion.curve
    ? pointOnCurve(motion.curve, motion.from + (motion.to - motion.from) * t)
    : mix(motion.start, motion.end, t);
}
