// Display geometry only. Runtime checks and simulator motion use the original legs.
const xy = p => [p.lon, p.lat];
const same = (a, b) => a.lat === b.lat && a.lon === b.lon && a.alt_m === b.alt_m;
const mix = (a, b, t) => a.map((v, i) => v + (b[i] - v) * t);

export function isRemainingRoute(previous, next) {
  return next.length <= previous.length && next.every((p, i) =>
    same(p, previous[previous.length - next.length + i]));
}

export function makeCurve(position, route, steps = 16) {
  const waypoints = [position, ...route].filter((p, i, all) =>
    !i || p.lon !== all[i - 1].lon || p.lat !== all[i - 1].lat);
  const points = waypoints.map(xy);
  const altitudes = waypoints.map(p => Number(p.alt_m ?? 0));
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
  return {points, coordinates, progress, lengths, scale, altitudes};
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

// 협상 애니메이션. 신청 → 거절 → 재작성 → 승인이 0.5초 폴링 사이에 다 끝나서,
// 그대로 두면 화면에는 결과만 남습니다. 실제로 오간 경로를 느리게 되짚어 보여줍니다.
// 그리는 좌표는 전부 원장/시뮬레이터가 준 것이고, 여기서 새 경로를 만들지 않습니다.
export const GROW_MS = 3600;    // 산출 중인 경로가 앞으로 뻗어 나가는 시간
export const CHECK_MS = 1000;    // 다 그린 뒤 판정을 기다리는 순간
export const HOLD_MS = 2600;    // 무엇이 막았는지 읽을 시간
export const FADE_MS = 1600;    // 거절된 선이 사라지는 시간
export const APPROVED_HOLD_MS = 2200;   // 승인 표시가 남아 있는 시간

/** 이 구간 하나가 화면에서 살아 있는 시간. 다음 구간을 언제 시작할지가 여기서 나옵니다. */
export function stageLife(kind) {
  return kind === "approved"
    ? GROW_MS + CHECK_MS + APPROVED_HOLD_MS
    : GROW_MS + CHECK_MS + HOLD_MS + FADE_MS;
}

const ease = t => 1 - (1 - t) ** 3;

/** 곡선에서 두 진행값 사이만 잘라냅니다. 양 끝은 정확히 그 지점에 찍습니다. */
export function sliceCurve(curve, from, to) {
  const total = curve.progress.at(-1);
  const start = Math.max(0, Math.min(total, from));
  const end = Math.max(start, Math.min(total, to));
  const out = [pointOnCurve(curve, start)];
  for (let i = 0; i < curve.progress.length; i++)
    if (curve.progress[i] > start && curve.progress[i] < end) out.push(curve.coordinates[i]);
  out.push(pointOnCurve(curve, end));
  return out;
}

/**
 * 한 구간이 지금 곡선의 어디까지 그려져 있는지. 끝났으면 null.
 * 승인은 뻗고 끝(그 뒤는 평소의 승인 경로 표시가 이어받습니다).
 * 거절은 뻗고 · 머물고 · 드론 쪽으로 되감깁니다.
 */
export function stageWindow(kind, elapsed) {
  if (elapsed < 0) return null;
  if (elapsed < GROW_MS) return [0, ease(elapsed / GROW_MS)];
  return elapsed < stageLife(kind) ? [0, 1] : null;
}

/**
 * 이 구간이 지금 얼마나 진하게 보이는가.
 * 선을 드론 쪽으로 되감으면 잡아채는 것처럼 보여서, 자리에 둔 채 흐려지게 합니다.
 */
export function stageFade(kind, elapsed) {
  const after = elapsed - GROW_MS - CHECK_MS;
  if (after < 0) return 1;
  if (kind === "approved") return Math.max(0, 1 - after / APPROVED_HOLD_MS);
  return after < HOLD_MS ? 1 : Math.max(0, 1 - (after - HOLD_MS) / FADE_MS);
}

/** 이 구간이 지금 어느 단계인가. 화면에 뭐라고 쓸지가 여기서 갈립니다. */
export function stagePhase(kind, elapsed) {
  if (elapsed < GROW_MS) return "drawing";
  // 다 그린 다음 판정이 내려오는 순간. 이게 없으면 그리자마자 색이 바뀌어서
  // 누가 무엇을 정했는지가 안 보입니다.
  if (elapsed < GROW_MS + CHECK_MS) return "checking";
  return kind === "approved" ? "approved" : "refused";
}

/** 라벨을 붙일 자리. 선 끝을 따라다니면 글자가 계속 움직여서 읽기가 어렵습니다. */
export function labelAnchor(curve) {
  return curve.coordinates[0];
}

// 고도를 눈에 보이게 하는 기하. MapLibre 5 에는 공중에 뜨는 선이 없습니다
// (line-z-offset 이 번들에 아예 없습니다). 대신 fill-extrusion 으로 얇은 리본을
// 실제 고도에 세웁니다 — base 와 height 사이에 떠 있는 판이 곧 그 구간의 고도입니다.
const METRES_PER_DEG_LAT = 110_570;
// 실제 비행 회랑 크기로 잡습니다. 8m 폭으로 그렸더니 화면에서 1~3픽셀이라
// 아무리 정확해도 안 보였습니다. 보이지 않는 정확함은 화면에서 없는 것과 같습니다.
const RIBBON_HALF_M = 22;     // 표시용 반폭 → 44m 회랑 (충돌 판정 폭이 아님)
const RIBBON_THICK_M = 8;     // 리본 두께(위아래)

/**
 * 경로를 구간마다 하나씩 사각형으로 만듭니다. 구간마다 승인 고도가 다르므로
 * 판도 구간마다 따로 떠 있어야 합니다 — 한 덩어리로 만들면 그 차이가 사라집니다.
 */
export function ribbon(points, halfWidthM = RIBBON_HALF_M, thicknessM = RIBBON_THICK_M) {
  const out = [];
  for (let i = 0; i < points.length - 1; i++) {
    const a = points[i], b = points[i + 1];
    const scale = Math.cos(a.lat * Math.PI / 180) || 1;
    const dLat = b.lat - a.lat, dLon = (b.lon - a.lon) * scale;
    const length = Math.hypot(dLat, dLon);
    if (!(length > 0)) continue;
    // 진행 방향의 법선. 미터를 위도 도수로 바꿔서 폭을 잡습니다.
    const half = halfWidthM / METRES_PER_DEG_LAT;
    const nLat = (-dLon / length) * half, nLon = (dLat / length) * half / scale;
    const altitude = Number(b.alt_m ?? a.alt_m ?? 0);
    out.push({
      polygon: [
        [a.lon + nLon, a.lat + nLat], [b.lon + nLon, b.lat + nLat],
        [b.lon - nLon, b.lat - nLat], [a.lon - nLon, a.lat - nLat],
        [a.lon + nLon, a.lat + nLat],
      ],
      base: Math.max(0, altitude - thicknessM / 2),
      height: Math.max(0.5, altitude + thicknessM / 2),
    });
  }
  return out;
}

/** 같은 곡선을 공중 점선으로 표시합니다. 점선 간격은 출발점에 고정돼
 * 지나온 부분을 지워도 남은 도형이 밀리지 않습니다. 고도는 각 신청 구간을 따릅니다. */
export function curveRibbon(curve, from, to) {
  const dash = RIBBON_HALF_M * 2 / METRES_PER_DEG_LAT;
  const period = dash * 1.5;
  const out = [];
  for (let leg = 0; leg < curve.lengths.length - 1; leg++) {
    const start = Math.max(from, curve.lengths[leg]);
    const end = Math.min(to, curve.lengths[leg + 1]);
    for (let at = Math.floor(start / period) * period; at < end; at += period) {
      const left = Math.max(start, at), right = Math.min(end, at + dash);
      if (right <= left) continue;
      out.push(...ribbon(sliceCurve(curve, left, right).map(([lon, lat]) =>
        ({lon, lat, alt_m:curve.altitudes[leg + 1]}))));
    }
  }
  return out;
}

/**
 * 고도에 뜬 정육각 덩어리. MapLibre 는 심볼을 띄우지 못하므로 기체도 짐도 이걸로 그립니다.
 * 지면에 붙은 아이콘으로는 '떠서 난다'가 안 읽힙니다.
 */
export function hex(lat, lon, radiusM, base, thicknessM) {
  const r = radiusM / METRES_PER_DEG_LAT;
  const rLon = r / (Math.cos(lat * Math.PI / 180) || 1);
  const polygon = [];
  for (let i = 0; i <= 6; i++) {
    const angle = (i / 6) * 2 * Math.PI + Math.PI / 6;
    polygon.push([lon + rLon * Math.cos(angle), lat + r * Math.sin(angle)]);
  }
  return {polygon, base: Math.max(0, base), height: Math.max(0.5, base + thicknessM)};
}

/**
 * 쿼드콥터 한 대. 몸통 하나와 로터 넷을 고도에 띄웁니다.
 * 육각 한 덩어리로는 무엇인지 안 읽혀서 팔과 로터를 따로 세웁니다.
 */
export function droneBody(lat, lon, altitude, heading = 0) {
  const base = Math.max(0, altitude - 1.5);
  const parts = [hex(lat, lon, 3.2, base, 3)];
  const turn = (heading * Math.PI) / 180;
  const armM = 6.5;
  for (let i = 0; i < 4; i++) {
    const angle = turn + Math.PI / 4 + (i / 4) * 2 * Math.PI;
    const dLat = (armM * Math.cos(angle)) / METRES_PER_DEG_LAT;
    const dLon = (armM * Math.sin(angle)) / METRES_PER_DEG_LAT
      / (Math.cos(lat * Math.PI / 180) || 1);
    parts.push(hex(lat + dLat, lon + dLon, 2.4, base + 0.8, 1.4));
  }
  return parts;
}

/** 실은 짐. 기체 위로 개수만큼 쌓입니다. */
export function cargoStack(lat, lon, altitude, count, radiusM = 4) {
  const boxes = [];
  for (let i = 0; i < count; i++)
    boxes.push(hex(lat, lon, radiusM, altitude + 4 + i * 4.5, 3.5));
  return boxes;
}
