// 화면이 그리는 바로 그 건물(OpenFreeMap 벡터 타일의 building 레이어, render_height)을 공역 데이터로 뽑습니다.
//
// 왜 타일에서 뽑나: 뉴욕시 공개 데이터(fetch_buildings.py)로 판정했더니 타일에는 있는데 데이터에는
// 없는 건물이 있었고, 승인된 회랑이 그 건물을 뚫고 지나갔습니다. 판정이 보는 건물과 화면이 그리는
// 건물은 같은 것이어야 합니다. 사용: node scripts/fetch_tile_buildings.mjs [min_height_m]
// 결과: configs/airspace/nyc_buildings.json (기존 형식 그대로). 로컬 스택(3100)이 떠 있어야 합니다.
import {createRequire} from 'node:module';
import {writeFileSync} from 'node:fs';
const require = createRequire(`${process.env.HOME}/.npm/_npx/6bcb61ec6d5aea22/node_modules/playwright/package.json`);
const {chromium} = require('playwright');

// 기본 40m: 순항 90m 에 옥상 이격 50m 라 그 아래 건물은 순항 구간을 막지 않습니다. 20m 로 뽑으면
// 34,581동(17MB)이 나오고 계획기가 몇 배 느려집니다.
const MIN_HEIGHT = Number(process.argv[2] || 20);   // 순항 최저 70 m − 이격 50 m. 이보다 낮은 건물은 어느 고도로도 50 m 가 남습니다
// 서비스 영역: 창고(40.702, -73.970) 둘레 11km 가 드는 상자
const BBOX = {south: 40.655, north: 40.805, west: -74.035, east: -73.895};
const STEP = {lat: 0.018, lon: 0.030};   // 줌 14, 1400×900 화면 한 장이 덮는 것보다 조금 작게

// 지도 화면(map.html)은 매 프레임 그리느라 loaded() 가 영영 안 떨어집니다. 빈 지도를 따로 띄웁니다.
const PAGE = `<!doctype html><meta charset="utf-8"><div id="m" style="width:1400px;height:900px"></div>
<script src="https://cdn.jsdelivr.net/npm/maplibre-gl@5.9.0/dist/maplibre-gl.js"></script>
<script>
  const map = new maplibregl.Map({container:'m', style:'https://tiles.openfreemap.org/styles/positron',
    center:[-73.97, 40.72], zoom:14.2, pitch:0, attributionControl:false});
  window.__skynet = {map};
  map.on('load', () => { map.addLayer({id:'b', type:'fill', source:'openmaptiles', 'source-layer':'building',
    paint:{'fill-opacity':0.01}}); window.ready = true; });
</script>`;
const browser = await chromium.launch({channel: 'chrome', headless: true});
const page = await browser.newPage({viewport: {width: 1400, height: 900}});
await page.setContent(PAGE);
await page.waitForFunction(() => window.ready, null, {timeout: 60000});

const seen = new Map();
for (let lat = BBOX.south; lat < BBOX.north; lat += STEP.lat) {
  for (let lon = BBOX.west; lon < BBOX.east; lon += STEP.lon) {
    const rows = await page.evaluate(async ([lat, lon, minH]) => {
      const map = window.__skynet.map;
      map.jumpTo({center: [lon, lat], zoom: 14.2, pitch: 0, bearing: 0});
      await new Promise(resolve => map.once('idle', resolve));
      await new Promise(resolve => setTimeout(resolve, 200));
      const out = [];
      for (const f of map.querySourceFeatures('openmaptiles', {sourceLayer: 'building'})) {
        const h = Number(f.properties.render_height || 0);
        if (h < minH) continue;
        const polys = f.geometry.type === 'Polygon' ? [f.geometry.coordinates]
          : f.geometry.type === 'MultiPolygon' ? f.geometry.coordinates : [];
        for (const rings of polys) {
          const ring = rings[0].map(([x, y]) => [+y.toFixed(6), +x.toFixed(6)]);
          if (ring.length < 4) continue;
          out.push({ring, h: Math.round(h * 10) / 10, min_h: Number(f.properties.render_min_height || 0)});
        }
      }
      return out;
    }, [lat, lon, MIN_HEIGHT]);
    for (const r of rows) {
      // 타일 경계에서 같은 건물이 두 번 옵니다. 첫 꼭짓점 + 꼭짓점 수 + 높이로 하나만 남깁니다.
      const key = `${r.ring[0][0]},${r.ring[0][1]},${r.ring.length},${r.h}`;
      if (!seen.has(key)) seen.set(key, r);
    }
  }
}
const volumes = [...seen.values()].map((r, i) => ({
  id: `bldg-t${String(i + 1).padStart(5, '0')}`,
  name: `건물 ${Math.round(r.h)}m`,
  polygon: r.ring.slice(0, -1),
  floor_m: 0.0,
  ceiling_m: r.h,
  reference: 'AGL',
  rule: 'forbidden',
  reason: `건물 관통 불가 (옥상 ${Math.round(r.h)}m AGL)`,
  source: 'OpenFreeMap vector tiles (OpenStreetMap buildings, render_height)',
  tags: {render_height: r.h, render_min_height: r.min_h},
}));
writeFileSync('configs/airspace/nyc_buildings.json', JSON.stringify({
  source: 'OpenFreeMap / OpenStreetMap building layer, render_height — 화면이 그리는 건물과 같은 것',
  fetched: new Date().toISOString(), min_height_m: MIN_HEIGHT, bbox: BBOX, volumes,
}));
console.log('buildings', volumes.length, 'tallest', Math.max(...volumes.map(v => v.ceiling_m)));
await browser.close();
