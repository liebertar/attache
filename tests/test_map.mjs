// Run with: node --test tests/test_map.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import * as geometry from '../ui/map-route.mjs';

const start = {lon:-73.97, lat:40.70};
const route = [{lon:-73.97, lat:40.71}, {lon:-73.96, lat:40.71},
  {lon:-73.96, lat:40.72}];

test('display curve preserves endpoints and leaves approved legs untouched', () => {
  const original = JSON.stringify(route);
  const curve = geometry.makeCurve(start, route);
  assert.deepEqual(curve.coordinates[0], [start.lon, start.lat]);
  assert.deepEqual(curve.coordinates.at(-1), [-73.96,40.72]);
  assert.equal(JSON.stringify(route), original);
  // 화면의 선은 판정받은 구간을 곧게 잇습니다. 모서리를 둥글리면 건물을 뚫고 가는 것처럼 보입니다.
  const legs = [start, ...route].map(p => [p.lon, p.lat]);
  const onALeg = ([x, y]) => legs.some((a, i) => i && (() => {
    const b = legs[i - 1], t = Math.hypot(a[0]-b[0], a[1]-b[1]);
    return Math.abs((x-b[0])*(a[1]-b[1]) - (y-b[1])*(a[0]-b[0])) / t < 1e-9;
  })());
  assert.ok(curve.coordinates.every(onALeg), '구간 밖으로 굽은 점이 있습니다');
  assert.ok(curve.coordinates.flat().every(Number.isFinite));
});

test('motion crosses a waypoint along the rendered curve, not a diagonal shortcut', () => {
  const curve = geometry.makeCurve(start, route);
  const from = geometry.routeProgress(curve, {lon:-73.97,lat:40.708});
  const to = geometry.routeProgress(curve, {lon:-73.968,lat:40.71}, from);
  const m = {curve,from,to,at:0};
  assert.deepEqual(geometry.motionPoint(m,250,500), geometry.pointOnCurve(curve,(from+to)/2));
  assert.deepEqual(geometry.motionPoint(m,1000,500), geometry.pointOnCurve(curve,to));
  assert.ok(geometry.motionPoint(m,250,500)[1] > 40.7095);
});

test('remaining waypoints reuse a flight; replacements and altitude changes do not', () => {
  assert.equal(geometry.isRemainingRoute(route,route.slice(1)),true);
  assert.equal(geometry.isRemainingRoute(route,[]),true);
  assert.equal(geometry.isRemainingRoute(route,[{...route.at(-1),alt_m:60}]),false);
  assert.equal(geometry.isRemainingRoute(route,[start,...route]),false);
});

test('zero length and duplicate waypoints produce finite stationary positions', () => {
  const curve = geometry.makeCurve(start,[start,start]);
  assert.deepEqual(geometry.pointOnCurve(curve,0),[start.lon,start.lat]);
  assert.equal(geometry.routeProgress(curve,start),0);
});

// Exercise UI state transitions without a browser or network; this is not visual QA.
function scene(overrides = {}) {
  let now = 0;
  const elements = new Map(), sources = new Map(), layers = new Map();
  const element = id => {
    if (!elements.has(id)) elements.set(id,
      {style:{},hidden:true,textContent:'',innerHTML:'',addEventListener(){}});
    return elements.get(id);
  };
  const map = {on(){}, addControl(){},
    addLayer(layer){layers.set(layer.id, structuredClone(layer));},
    setPaintProperty(id, name, value){layers.get(id).paint[name] = value;},
    getLayer(id){return layers.get(id);},
    getSource(id){
    if (!sources.has(id)) sources.set(id,{setData(data){this.data=data;}});
    return sources.get(id);
  }};
  const html = readFileSync(new URL('../ui/map.html',import.meta.url),'utf8');
  const imports = Object.fromEntries(html.match(/import \{([^}]+)\}/)[1]
    .split(',').map(name=>[name.trim(), geometry[name.trim()]]));
  const context = vm.createContext({...imports, console, Date, Map, Set, Math,
    location:{hostname:'localhost'}, performance:{now:()=>now},
    requestAnimationFrame(){}, document:{getElementById:element,querySelector:element},
    maplibregl:{Map:function(){return map;}, NavigationControl:function(){},
      Popup:function(){return {setLngLat(){return this;}, setHTML(){return this;},
        addTo(){return this;}};}}, ...overrides});
  const code = html.match(/<script type="module">([\s\S]*?)<\/script>/)[1]
    .replace(/^import .*?;\n/m,'');
  vm.runInContext(code,context);
  const run = (name,...args) => context[name](...args);
  return {run,get:name=>context[name],element,source:id=>sources.get(id)?.data,
    path:phase=>(sources.get('flightpath')?.data?.features || [])
      .filter(f=>f.properties.phase === phase),
    layer:id=>layers.get(id), time:value=>{now=value;}};
}

function endOf(path) {
  const ring = path.at(-1).geometry.coordinates[0];
  return ring[1].map((v, i) => (v + ring[2][i]) / 2);
}

// 기본은 지상(alt 0)입니다. 협상 재생은 지상 출발에만 하므로, 공중 기체를 보려면 alt_m 을 넘깁니다.
function snapshot(tick=1, round=1, remaining=route, position=start) {
  const world = {assets:{'drone-01':{id:'drone-01',battery:80,alt_m:0,...position,
      route:remaining.map(p => ({...p, alt_m:110}))}},
    depot_coords:start,scoreboard:{spend_usd:0},fleet_limit:450};
  return {tick,round,recall_tick:null,worlds:{guarded:world,direct:structuredClone(world)}};
}
function denial(id='denied-1', extra={}) {
  return {id,at:Date.now()/1000,outcome:'denied',
    proposal:{asset_id:'drone-01',action:'fly_route',params:{legs:[start,...route]}},
    decision:{verdict:'denied',reason:'금지 공역 <test>'},...extra};
}

function approval(id='approved-1', extra={}) {
  return {id,at:Date.now()/1000,outcome:'executed',
    proposal:{asset_id:'drone-01',action:'fly_route',params:{legs:[start,...route]}},
    decision:{verdict:'auto',reason:'한도 안'},...extra};
}

test('a partial draw keeps the curve endpoints and stays inside the route', () => {
  const curve = geometry.makeCurve(start,route);
  const total = curve.progress.at(-1);
  const half = geometry.sliceCurve(curve,0,total/2);
  assert.deepEqual(half[0],[start.lon,start.lat]);
  assert.ok(half.length < geometry.sliceCurve(curve,0,total).length);
  assert.deepEqual(geometry.sliceCurve(curve,0,total).at(-1),curve.coordinates.at(-1));
  assert.ok(half.flat().every(Number.isFinite));
});

const {GROW_MS, CHECK_MS, HOLD_MS, FADE_MS, APPROVED_HOLD_MS} = geometry;

test('a route is drawn, waits for a verdict, then holds and fades where it is', () => {
  assert.deepEqual(geometry.stageWindow('approved',0),[0,0]);
  assert.deepEqual(geometry.stageWindow('approved',GROW_MS),[0,1]);
  assert.equal(geometry.stageWindow('approved',geometry.stageLife('approved')),null);
  // 거절된 선은 끝까지 그려진 채로 남았다가 흐려집니다. 되감기지 않습니다.
  assert.deepEqual(geometry.stageWindow('rejected',GROW_MS + CHECK_MS + HOLD_MS),[0,1]);
  assert.equal(geometry.stageWindow('rejected',geometry.stageLife('rejected')),null);
  assert.equal(geometry.stageFade('rejected',GROW_MS + CHECK_MS + HOLD_MS - 10),1);
  const fading = geometry.stageFade('rejected',GROW_MS + CHECK_MS + HOLD_MS + FADE_MS / 2);
  assert.ok(fading > 0 && fading < 1);
  assert.equal(geometry.stageFade('rejected',geometry.stageLife('rejected')),0);
  // 다 그린 다음 판정을 기다리는 순간이 있어야 무엇이 결정됐는지가 보입니다.
  assert.equal(geometry.stagePhase('rejected', GROW_MS / 2),'drawing');
  assert.equal(geometry.stagePhase('rejected', GROW_MS + 10),'checking');
  assert.equal(geometry.stagePhase('rejected', GROW_MS + CHECK_MS + 10),'refused');
  assert.equal(geometry.stagePhase('approved', GROW_MS + CHECK_MS + 10),'approved');
});

test('the label sits at the start of the route, not on the moving head', () => {
  const curve = geometry.makeCurve(start, route);
  assert.deepEqual(geometry.labelAnchor(curve), curve.coordinates[0]);
  assert.deepEqual(geometry.labelAnchor(curve), [start.lon, start.lat]);
});

test('the flight path floats at the approved altitude, segment by segment', () => {
  const climb = [{lon:-73.97,lat:40.71,alt_m:60},{lon:-73.96,lat:40.71,alt_m:120}];
  const pieces = geometry.ribbon([{lon:-73.97,lat:40.70,alt_m:60},...climb]);
  assert.equal(pieces.length,2);
  assert.ok(pieces[0].base > 0, '땅에 붙으면 고도를 못 보여줍니다');
  assert.ok(pieces[1].base > pieces[0].base, '구간마다 승인 고도가 다르면 판도 따로 떠야 합니다');
  assert.ok(pieces.every(p => p.height > p.base && p.polygon.length === 5));
  assert.ok(pieces.flatMap(p => p.polygon).flat().every(Number.isFinite));
});

test('a snapshot raises a flight path for a drone that is flying', () => {
  const ui = scene();
  ui.run('renderSnapshot',snapshot(),null);
  ui.run('draw');
  const path = ui.source('flightpath').features;
  assert.ok(path.length, '승인 경로가 있으면 고도 판이 서야 합니다');
  assert.ok(path.every(f => f.properties.height > f.properties.base));
  ui.run('renderSnapshot',snapshot(1,2,[]),null); ui.run('draw');
  assert.equal(ui.source('flightpath').features.length, 0);
});

test('a drone that has stopped to work says so next to its name', () => {
  const ui = scene();
  ui.run('renderSnapshot',snapshot(1,1,[],{lon:-73.97,lat:40.70,state:'dropping',load:2}),null);
  ui.run('draw');
  // 남은 상자 수까지 씁니다. 상자가 하나씩 줄어드는 것과 같은 숫자입니다.
  assert.equal(ui.source('guarded').features[0].properties.work,'unloading 2');
  ui.run('renderSnapshot',snapshot(1,1,[],{lon:-73.97,lat:40.70,state:'loading',load:3}),null);
  ui.run('draw');
  assert.equal(ui.source('guarded').features[0].properties.work,'loading 3/6');
  assert.equal(ui.source('cargo').features.length, 3, '상자는 실린 개수만큼 쌓입니다');
  ui.run('renderSnapshot',snapshot(2,1,route,{lon:-73.97,lat:40.70,state:'delivering'}),null);
  ui.run('draw');
  assert.equal(ui.source('guarded').features[0].properties.work,'',
               '나는 중에는 아무것도 안 붙습니다');
});

test('warehouse, curved green path and drone update from snapshots and clear on reset', () => {
  const ui = scene();
  ui.run('renderSnapshot',snapshot(),null);
  ui.run('draw');
  assert.equal(ui.source('depot').features[0].geometry.coordinates[0],start.lon);
  const first = ui.path('approved');
  ui.time(500);
  ui.run('renderSnapshot',snapshot(2,1,route.slice(1),{lon:-73.969,lat:40.71}),null);
  ui.time(750); ui.run('draw');
  const later = ui.path('approved');
  // 곡선은 다시 만들지 않습니다(경유점이 빠져도 같은 곡선). 다만 지나온 구간은 지웁니다.
  assert.deepEqual(later.at(-1).geometry, first.at(-1).geometry, '목적지는 그대로여야 합니다');
  assert.ok(later.length < first.length, '지나온 구간이 안 지워지고 있습니다');
  assert.ok(ui.source('guarded').features[0].geometry.coordinates[1] > 40.70);
  ui.run('renderSnapshot',snapshot(1,2,[]),null); ui.run('draw');
  assert.equal(ui.path('approved').length,0);
});

test('final denials alert once, grow the submitted legs, and expire without polling', () => {
  const ui = scene(), e = denial();
  ui.run('renderDenials',{ledger:[{...e,outcome:'pending'}]},0);
  assert.equal(ui.element('denial').hidden,true);
  ui.run('renderDenials',{ledger:[e]},100);
  assert.equal(ui.element('denial').hidden,false);
  assert.equal(ui.element('denial-who').textContent, 'drone-01');
  assert.equal(ui.element('denial-what').textContent, 'delivery route');
  assert.ok(ui.element('denial-why').textContent.includes('<test>'));
  // 거절된 경로는 드론 쪽에서 뻗어 나갑니다. 다 뻗은 뒤에 신청서의 끝점에 닿습니다.
  ui.time(200); ui.run('draw');
  assert.equal(ui.path('rejected').length, 0, '판정 전에는 붉지 않습니다');
  const partial = ui.path('pending');
  ui.time(100 + GROW_MS + CHECK_MS + 50); ui.run('draw');
  const full = ui.path('rejected');
  assert.match(ui.source('stage-label').features[0].properties.label,/^REJECTED/);
  assert.ok(full.length > partial.length);
  assert.ok(endOf(full)[1] <= route.at(-1).lat);
  ui.run('renderDenials',{ledger:[e]},7000);
  ui.time(100 + geometry.stageLife('rejected') + 50); ui.run('draw');
  assert.equal(ui.path('rejected').length,0);
  ui.time(8101); ui.run('draw');   // 알림은 8초
  assert.equal(ui.element('denial').hidden,true);
  assert.equal(ui.path('rejected').length,0);
});

test('an approved corridor grows yellow before changing colour and carrying the flight', () => {
  const ui = scene();
  ui.run('renderSnapshot',snapshot(),null);
  ui.run('renderDenials',{ledger:[approval()]},0);
  ui.time(100); ui.run('draw');
  const growing = ui.path('pending');
  assert.ok(growing.length > 0);           // 판정 전이라 아직 초록이 아닙니다
  assert.equal(ui.path('approved').length,0);
  assert.equal(ui.source('stage-label').features[0].properties.label,'PLANNING…');
  ui.time(GROW_MS + CHECK_MS + 10); ui.run('draw');
  assert.match(ui.source('stage-label').features[0].properties.label,/^APPROVED · /);
  ui.time(geometry.stageLife('approved') + 50); ui.run('draw');
  const settled = ui.path('approved');
  assert.ok(growing.length < settled.length);
  assert.ok(endOf(settled)[1] <= route.at(-1).lat);
});

test('the ledger is newest first, so stages are re-sorted into the order they happened', () => {
  const ui = scene();
  const at = Date.now() / 1000;
  const legs = [start, ...route];
  const rejected = {id:'r', at: at - 0.2, outcome:'denied',
    proposal:{asset_id:'drone-01', action:'fly_route', params:{legs}},
    decision:{verdict:'denied', reason:'x'}};
  const approved = {id:'a', at, outcome:'executed',
    proposal:{asset_id:'drone-01', action:'fly_route', params:{legs}},
    decision:{verdict:'auto', reason:'ok'}};
  ui.run('renderDenials', {ledger:[approved, rejected]}, 0);   // 최신이 앞
  ui.time(GROW_MS + CHECK_MS + 10); ui.run('draw');
  // 먼저 일어난 것은 거절입니다. 승인이 먼저 재생되면 순서가 뒤집힌 것입니다.
  assert.ok(ui.path('rejected').length > 0);
  assert.equal(ui.path('approved').length, 0);
});

test('a route refused for a held pad says so, and draws no blocker', () => {
  const ui = scene();
  const e = denial('held',{decision:{verdict:'denied',reason:'x',code:'resource_held',
    detail:{resource:'pad:launch',holder:'drone-03'}}});
  ui.run('renderDenials',{ledger:[e]},0);
  ui.time(GROW_MS + CHECK_MS + 10); ui.run('draw');
  const label = ui.source('stage-label').features[0].properties.label;
  assert.match(label,/^REJECTED · pad:launch is held by drone-03/);
  assert.equal(ui.source('blocker').features.length,0);
  assert.equal(ui.source('breach').features.length,0);
});

test('provenance: tier tag from the model id, drafter word, and the approved headline names the rule', () => {
  const ui = scene();
  const llm = {enabled:true, host:'ollama', models:{nano:'nemotron-3-nano', super:'', ultra:''}};
  const wrote = approval('w1', {proposal:{asset_id:'drone-01', action:'fly_route', author:'nemotron-3-nano',
    params:{legs:[start,...route], drafter:'nano:nemotron-3-nano'}}, decision:{verdict:'auto', reason:'한도 안', code:'within_limits'}});
  const byRules = approval('w2', {proposal:{asset_id:'drone-02', action:'fly_route', author:'rules',
    params:{legs:[start,...route], drafter:'astar'}}, decision:{verdict:'auto', reason:'한도 안', code:'within_limits'}});
  ui.run('renderSnapshot', snapshot(), {ledger:[wrote, byRules], llm, locks:{}});
  const feed = ui.element('feed').innerHTML;
  assert.match(feed, /drone-01<\/b> <span class="tier">nano<\/span>/, '모델이 쓴 신청서는 티어 한 단어');
  assert.ok(!/nemotron-3-nano/.test(feed), '모델 id 전체는 화면에 안 씁니다');
  assert.ok(!/drone-02<\/b> <span class="tier">/.test(feed), '규칙이 쓴 것은 표시가 없습니다');
  assert.match(feed, /delivery route · A\*/);
  assert.match(ui.element('llm-line').textContent, /Nemotron nano · via ollama/);
  ui.time(100); ui.run('draw');
  const labels = ui.source('stage-label').features.map(f => f.properties.label);
  assert.ok(labels.some(l => l === 'PLANNING… · nano'), labels.join('|'));
  ui.time(GROW_MS + CHECK_MS + 10); ui.run('draw');
  const later = ui.source('stage-label').features.map(f => f.properties.label);
  assert.ok(later.some(l => /^APPROVED · within limits$/.test(l)), later.join('|'));
  ui.run('renderSnapshot', snapshot(), {ledger:[], llm:{enabled:false, models:{}}, locks:{}});
  assert.match(ui.element('llm-line').textContent, /rules only/);
});

test('a traffic refusal names the other aircraft and blinks its corridor; a delayed approval says who it waited for', () => {
  const ui = scene();
  const crossing = denial('x1', {proposal:{asset_id:'drone-01', action:'fly_route',
    params:{legs:[start,...route], blocked_kind:'traffic', blocked_asset:'drone-03', blocked_leg:1,
            blocked_at:{lat:route[0].lat, lon:route[0].lon}}},
    decision:{verdict:'denied', reason:'교차', code:'airspace'}});
  ui.run('renderDenials',{ledger:[crossing]},0);
  ui.run('corridorAlpha','drone-03',1);
  ui.time(GROW_MS + CHECK_MS + HOLD_MS / 6); ui.run('draw');
  assert.match(ui.source('stage-label').features[0].properties.label, /^REJECTED · CROSSES drone-03/);
  assert.equal(ui.source('blocker').features.length, 0, '교차는 다각형이 아니라 기체입니다');
  assert.ok(ui.layer('flightpath:drone-03').paint['fill-extrusion-opacity'] < .85, '상대 회랑이 깜빡입니다');
  const later = approval('x2', {proposal:{asset_id:'drone-02', action:'fly_route',
    params:{legs:[start,...route], resolution:'delay', holding_for:'drone-03'}},
    decision:{verdict:'auto', reason:'한도 안', code:'within_limits'}});
  ui.run('renderDenials',{ledger:[later]},0);
  ui.time(GROW_MS + CHECK_MS + 10); ui.run('draw');
  const labels = ui.source('stage-label').features.map(f => f.properties.label);
  assert.ok(labels.some(l => /^APPROVED · after drone-03$/.test(l)), labels.join('|'));
  ui.run('renderSnapshot', snapshot(1,1,[],{...start, state:'ready', holding_for:'drone-03'}), null);
  ui.run('draw');
  assert.equal(ui.source('guarded').features[0].properties.work, 'holding for drone-03');
});

test('a route approved while already in the air is not replayed from the old spot', () => {
  const ui = scene();
  ui.run('renderSnapshot',snapshot(1,1,[],{...start,alt_m:55}),null);
  ui.run('renderDenials',{ledger:[approval()]},0);
  ui.time(GROW_MS / 2); ui.run('draw');
  assert.equal(ui.path('pending').length,0,'나는 중에는 옛 자리에서 노란 선을 다시 그리지 않습니다');
  assert.equal(ui.source('stage-label').features.length,0);
});

test('a queued decision has not travelled anywhere yet, so nothing is drawn', () => {
  const ui = scene();
  ui.run('renderDenials',{ledger:[approval('q',{decision:{verdict:'queued',reason:'대기'}})]},0);
  ui.time(100); ui.run('draw');
  assert.equal(ui.path('rejected').length,0);
  assert.equal(ui.path('approved').length,0);
});

test('old ledger entries do not replay alerts; non-flight denials do not invent paths', () => {
  const ui = scene();
  ui.run('renderDenials',{ledger:[denial('old',{at:Date.now()/1000-60})]},0);
  assert.equal(ui.element('denial').hidden,true);
  ui.run('renderDenials',{ledger:[denial('charge',{
    proposal:{asset_id:'drone-01',action:'fast_charge',params:{}},
  })]},100);
  assert.equal(ui.element('denial').hidden,false);
  ui.time(1050); ui.run('draw');
  assert.equal(ui.path('rejected').length,0);
  assert.equal(ui.element('denial-what').textContent,'fast charge');
});

test('route completion clears approval after animation and round reset clears alerts', () => {
  const ui = scene();
  ui.run('renderSnapshot',snapshot(),null);
  ui.time(500);
  ui.run('renderSnapshot',snapshot(2,1,[],route.at(-1)),{ledger:[denial()]});
  ui.time(750); ui.run('draw');
  assert.equal(ui.path('approved').length,0, '거절 재생 중에는 비행 회랑을 겹치지 않습니다');
  ui.time(1001); ui.run('draw');
  assert.equal(ui.path('approved').length,0);
  ui.run('renderSnapshot',snapshot(1,2,[]),null); ui.run('draw');
  assert.equal(ui.element('denial').hidden,true);
  assert.equal(ui.path('rejected').length,0);
});


test('one elevated geometry changes colour, blinks independently and fades without ground sources', () => {
  const ui = scene();
  const legs = [start,...route].map(p=>({...p,alt_m:55}));
  const e = denial('colour',{proposal:{asset_id:'drone-01',action:'fly_route',params:{legs}}});
  ui.run('renderDenials',{ledger:[e]},0);
  ui.time(GROW_MS + CHECK_MS / 2); ui.run('draw');
  const yellow = ui.path('pending').map(f=>f.geometry);
  assert.ok(yellow.length);
  assert.ok(ui.path('pending').every(f=>f.properties.base > 0));
  ui.time(GROW_MS + CHECK_MS); ui.run('draw');
  assert.deepEqual(ui.path('rejected').map(f=>f.geometry),yellow);
  const bright = ui.layer('flightpath:drone-01').paint['fill-extrusion-opacity'];
  ui.run('corridorAlpha','drone-02',1);
  ui.time(GROW_MS + CHECK_MS + HOLD_MS / 6); ui.run('draw');
  assert.ok(ui.layer('flightpath:drone-01').paint['fill-extrusion-opacity'] < bright);
  assert.equal(ui.layer('flightpath:drone-02').paint['fill-extrusion-opacity'],bright);
  ui.time(geometry.stageLife('rejected') - 10); ui.run('draw');
  assert.ok(ui.layer('flightpath:drone-01').paint['fill-extrusion-opacity'] < .1);
  for (const id of ['pending','approved','rejected']) assert.equal(ui.source(id),undefined);
});

test('the elevated curve keeps per-leg altitude and fixed dash positions after flight progress', () => {
  const c = geometry.makeCurve({...start,alt_m:55},route.map((p,i)=>({...p,alt_m:55+i*10})));
  const all = geometry.curveRibbon(c,0,c.progress.at(-1));
  const remaining = geometry.curveRibbon(c,c.progress.at(-1)/2,c.progress.at(-1));
  assert.deepEqual(remaining.at(-1),all.at(-1));
  assert.ok(remaining.length < all.length);
  // 리본 윗면은 기체 고도 4.5m 아래(RIBBON_DROP_M), 두께 3m 입니다.
  assert.equal(all[0].height,50.5);
  assert.equal(all[0].base,47.5);
  assert.equal(all.at(-1).height,70.5);
});

test('a vertex where the altitude changes carries a vertical dotted column shaped like the corridor dashes', () => {
  const level = geometry.makeCurve({...start, alt_m:0}, [{...route[0], alt_m:90}, {...route[1], alt_m:90}]);
  const stepped = geometry.makeCurve({...start, alt_m:0}, [{...route[0], alt_m:60}, {...route[1], alt_m:100}]);
  const total = stepped.lengths.at(-1);
  assert.ok(geometry.curveRibbon(stepped, 0, total).every(p => !p.column), '회랑 조각은 회랑만');
  const columns = geometry.curveColumns(stepped, 0, total);
  assert.ok(columns.length > 0 && columns.every(p => p.column));
  const [lon, lat] = stepped.points[1];
  const atVertex = columns.filter(p => Math.abs(p.polygon[0][0] - lon) < 2e-4 && Math.abs(p.polygon[0][1] - lat) < 2e-4);
  const takeoff = columns.filter(p => Math.abs(p.polygon[0][1] - start.lat) < 2e-4);
  assert.ok(atVertex.length >= 1, `꼭짓점 기둥 토막 ${atVertex.length}`);
  assert.ok(takeoff.length >= 1, `이륙 기둥 토막 ${takeoff.length}`);
  // 기둥은 60m 판 윗면(55.5)에서 100m 판 윗면(95.5)까지, 토막은 회랑 점선과 같은 36m 이하.
  assert.ok(atVertex.every(p => p.base >= 55.4 && p.height <= 95.6 && p.height - p.base <= 36.01));
  // 판의 발자국은 회랑 폭(18m) × 두께(3m). 정육면체가 아닙니다.
  const metres = (a, b) => Math.hypot((a[0] - b[0]) * Math.cos(lat * Math.PI / 180), a[1] - b[1]) * 111320;
  const ring = atVertex[0].polygon;
  const sides = [metres(ring[0], ring[1]), metres(ring[1], ring[2])].sort((x, y) => y - x);
  assert.ok(Math.abs(sides[0] - 18) < 0.5 && Math.abs(sides[1] - 3) < 0.5, `발자국 ${sides.map(v => v.toFixed(1))}`);
  assert.equal(geometry.curveColumns(level, 0, level.lengths.at(-1)).filter(p => Math.abs(p.polygon[0][1] - start.lat) > 2e-4).length, 0,
    '고도가 같으면 꼭짓점 기둥이 없습니다');
  assert.equal(geometry.curveColumns(stepped, total * 0.9, total).length, 0, '지나온 꼭짓점의 기둥은 창 밖입니다');
});

test('a duplicate refusal is neither replayed as a route nor raised as a card', () => {
  const ui = scene();
  ui.run('renderDenials',{ledger:[denial('dup',{decision:{verdict:'denied',reason:'같은 신청',code:'duplicate'}})]},0);
  assert.equal(ui.element('denial').hidden, true);
  ui.time(GROW_MS + CHECK_MS + 10); ui.run('draw');
  assert.equal(ui.path('rejected').length, 0);
});

test('an endpoint refusal names the gap, not a leg; a withdrawal is its own card', () => {
  const ui = scene();
  const gap = denial('e1', {proposal:{asset_id:'drone-01', action:'fly_route',
    params:{legs:[start,...route], blocked_kind:'origin', blocked_leg:0, blocked_gap_m:240.4,
            blocked_at:{lat:start.lat, lon:start.lon}}},
    decision:{verdict:'denied', reason:'출발점', code:'airspace'}});
  ui.run('renderDenials',{ledger:[gap]},0);
  assert.equal(ui.element('denial-why').textContent, 'START 240 m FROM THE AIRCRAFT');
  assert.equal(ui.element('denial-more').textContent, '', '막은 물체가 없으니 고도 띠도 없습니다');
  ui.time(GROW_MS + CHECK_MS + HOLD_MS / 6); ui.run('draw');
  assert.match(ui.source('stage-label').features[0].properties.label, /^REJECTED · START 240 m FROM THE AIRCRAFT/);
  const withdrawn = {id:'w1', at:Date.now()/1000, outcome:'done',
    proposal:{asset_id:'drone-03', action:'divert_ground', author:'runtime',
              params:{withdrawn_for:'drone-02', intent:'i1'}},
    decision:{verdict:'auto', reason:'물림', policy_hit:'traffic', code:'withdrawn', detail:{for:'drone-02'}}};
  ui.run('renderDenials',{ledger:[withdrawn]},0);
  assert.equal(ui.element('#denial .tag').textContent, 'WITHDRAWN');
  assert.match(ui.element('denial-why').textContent, /drone-02/);
  assert.doesNotMatch(ui.element('denial-why').textContent, /a rule arrived/);
});

test('the banner says who read a NOTAM, and shows the raw text while nobody has', () => {
  const ui = scene();
  const bulletin = {id:'n1', kind:'notam', text:'AREA BOUNDED BY 404310N0735920W SFC-400FT AGL 0907-0912Z',
                    published_tick:525, until_tick:900};
  ui.run('renderSnapshot', {...snapshot(), bulletins:[bulletin]}, {ledger:[], notices:[]});
  assert.match(ui.element('banner').innerHTML, /NOTAM/);
  assert.match(ui.element('banner').innerHTML, /not yet read/);
  ui.run('renderSnapshot', {...snapshot(), bulletins:[bulletin]}, {ledger:[], notices:[
    {id:'n1', name:'East Village helipad', applied:true, held:false, source:'grammar', from_tick:525, until_tick:900}]});
  assert.match(ui.element('banner').innerHTML, /East Village helipad/);
  assert.match(ui.element('banner').innerHTML, /rule grammar/);
  assert.doesNotMatch(ui.element('banner').innerHTML, /not yet read/);
  ui.run('renderSnapshot', {...snapshot(), bulletins:[]}, {ledger:[], notices:[
    {id:'n2', name:'Harlem TFR', applied:false, held:true, source:'model:nvidia/nemotron-3-super-120b-a12b'}]});
  assert.match(ui.element('banner').innerHTML, /waiting for a person/);
  assert.equal(ui.element('banner').style.display, 'block');
});

test('a notice a person confirmed before its window says so, and is neither raw nor enforced', () => {
  const ui = scene();
  const bulletin = {id:'n3', kind:'notam', text:'MEDEVAC INBOUND HARLEM', published_tick:1350, until_tick:2100};
  ui.run('renderSnapshot', {...snapshot(), bulletins:[bulletin]}, {ledger:[], notices:[
    {id:'n3', name:'Harlem TFR', applied:false, held:false, source:'human', from_tick:1350, until_tick:2100,
     polygon:[[40.81, -73.94], [40.81, -73.93], [40.82, -73.93]]}]});
  const banner = ui.element('banner').innerHTML;
  assert.match(banner, /confirmed by a person/);
  assert.match(banner, /applies when the window opens/);
  assert.doesNotMatch(banner, /not yet read/);
  assert.doesNotMatch(banner, /pulled back/);
  assert.equal(ui.source('zone').features.length, 0, '확인만 됐고 아직 안 걸린 것은 칠하지 않습니다');
});

test('a ledger line that went to a person is not replayed as an approved corridor', () => {
  const ui = scene();
  ui.run('renderSnapshot', snapshot(), {ledger:[{id:'h1', at:Date.now()/1000, outcome:'waiting',
    proposal:{asset_id:'drone-01', action:'fly_route', params:{legs:[start, ...route]}},
    decision:{verdict:'human', reason:'기체 한도 초과', code:'over_asset', detail:{spent:490, cap:320}}}]});
  assert.equal(ui.path('approved').length, 0);
  assert.equal(ui.path('pending').length, 0);
  assert.match(ui.element('feed').innerHTML, /HUMAN/);
  assert.match(ui.element('feed').innerHTML, /aircraft cap: \$490 > \$320/);
});

// 관제 권고. 런타임이 /state.advisories 에 싣는 모양 그대로.
function advisory(extra={}) {
  return {asset:'drone-02', tick:640, at:Date.now()/1000, ledger_id:'l_adv1', trigger:'refusals',
    refusals:[{tick:600, code:'airspace', blocked_kind:'traffic', blocked_asset:'drone-03', blocked_until_tick:700},
              {tick:610, code:'airspace', blocked_kind:'traffic', blocked_asset:'drone-03', blocked_until_tick:700},
              {tick:620, code:'airspace', blocked_kind:'forbidden', blocked_volume:'bldg-1'}],
    options:[{id:'hold', label:'hold on the ground until tick 700', legal:true, why:'drone-03 clears that volume at tick 700', until_tick:700},
             {id:'climb', label:'climb +30 m on the last filed legs', legal:false, why:'bldg-1 옥상 위 10m', shift_m:30},
             {id:'decline', label:'decline the job', legal:true, why:'no aircraft flies'},
             {id:'escalate', label:'escalate to a person', legal:true, why:'a controller looks'}],
    chosen:'hold', summary:'', model:'', source:'rules', ...extra};
}

test('a tower advisory card names the aircraft, lists the checked options, and hides after 12 s', () => {
  const ui = scene();
  ui.run('renderAdvisories', {advisories:[advisory()]}, 100);
  assert.equal(ui.element('advisory').hidden, false);
  assert.equal(ui.element('advisory-tag').textContent, 'TOWER ADVISORY');
  assert.equal(ui.element('advisory-who').textContent, 'drone-02');
  assert.equal(ui.element('advisory-source').textContent, 'rules · after 3 refusals in a row');
  // 규칙이 고른 권고는 화면 말로 조립합니다 — 런타임 문장을 그대로 쓰지 않습니다.
  assert.equal(ui.element('advisory-summary').textContent,
    'drone-02 was refused 3 times in a row. The rules suggest: hold on the ground until tick 700.');
  const options = ui.element('advisory-options').innerHTML;
  assert.match(options, /<li class="ok chosen">hold on the ground until tick 700 · legal<\/li>/);
  assert.match(options, /<li class="no">climb \+30 m on the filed legs · not legal — bldg-1 옥상 위 10m<\/li>/);
  assert.match(options, /<li class="ok">decline the order · legal<\/li>/);
  assert.match(options, /<li class="ok">escalate to a person · legal<\/li>/);
  assert.match(ui.element('advisory-note').textContent, /information only/);
  // 같은 권고는 다시 띄우지 않고, 12초 뒤에 내려갑니다.
  ui.time(100 + 12001); ui.run('draw');
  assert.equal(ui.element('advisory').hidden, true);
  ui.run('renderAdvisories', {advisories:[advisory()]}, 13000);
  assert.equal(ui.element('advisory').hidden, true);
  // 모델(super)이 쓴 요약은 그대로 보이고, 출처 단어가 바뀝니다.
  ui.run('renderAdvisories', {advisories:[advisory({ledger_id:'l_adv2', source:'super', chosen:'decline',
    model:'nemotron-3-super', summary:'Two filings crossed drone-03 and the third hit a roof. Decline this order and refile after tick 700.'})]}, 14000);
  assert.equal(ui.element('advisory').hidden, false);
  assert.match(ui.element('advisory-source').textContent, /^super · after 3 refusals in a row$/);
  assert.match(ui.element('advisory-summary').textContent, /^Two filings crossed drone-03/);
  assert.match(ui.element('advisory-options').innerHTML, /<li class="ok chosen">decline the order · legal<\/li>/);
  // 반려 뒤의 권고는 방아쇠를 그렇게 말합니다.
  ui.run('renderAdvisories', {advisories:[advisory({ledger_id:'l_adv3', trigger:'decline_after_refusals', chosen:'escalate'})]}, 15000);
  assert.equal(ui.element('advisory-source').textContent, 'rules · declined the order after 3 refusals');
  assert.match(ui.element('advisory-summary').textContent, /declined the order after 3 refusals\. The rules suggest: escalate to a person\./);
});

test('the feed shows the advisory as a runtime line naming the chosen option; it raises no denial card', () => {
  const ui = scene();
  const entry = {id:'l_adv1', at:Date.now()/1000, outcome:'noted',
    proposal:{asset_id:'drone-02', action:'advisory', author:'runtime',
              params:{options:advisory().options, chosen:'hold', trigger:'refusals'}},
    decision:{verdict:'auto', reason:'…', code:'advisory',
              detail:{resource:'drone-02', chosen:'hold', trigger:'refusals', source:'rules'}}};
  ui.run('renderSnapshot', snapshot(), {ledger:[entry], llm:{enabled:false, models:{}}, locks:{}, advisories:[]});
  const feed = ui.element('feed').innerHTML;
  assert.match(feed, /<b>drone-02<\/b> tower advisory/);
  assert.match(feed, /advisory · hold on the ground until tick 700/);
  assert.equal(ui.element('denial').hidden, true, '권고는 거절 카드가 아닙니다');
  ui.time(100); ui.run('draw');
  assert.equal(ui.path('pending').length, 0, '권고는 경로가 아니라 되짚어 그리지 않습니다');
  const bySuper = {...entry, id:'l_adv2', decision:{...entry.decision, detail:{...entry.decision.detail, source:'super'}}};
  ui.run('renderSnapshot', snapshot(), {ledger:[bySuper], llm:{enabled:false, models:{}}, locks:{}, advisories:[]});
  assert.match(ui.element('feed').innerHTML, /hold on the ground until tick 700 · super/);
});

test('the api ports come from ?rt= and ?sim=, and default to 8000/8100', () => {
  // 모듈의 const 는 vm 문맥의 전역이 아니라, 디버그 핸들(window.__attache.api)로 읽습니다.
  const api = ui => JSON.stringify(ui.get('window').__attache.api);
  const plain = scene({window:{}});
  assert.equal(api(plain), JSON.stringify({sim:'http://localhost:8100', rt:'http://localhost:8000'}));
  const second = scene({window:{}, location:{hostname:'localhost', search:'?rt=8010&sim=8110'}, URLSearchParams});
  assert.equal(api(second), JSON.stringify({sim:'http://localhost:8110', rt:'http://localhost:8010'}));
});

test('an applied runtime notice is painted on the ground; a held one is not', () => {
  const ui = scene();
  const ring = [[40.81, -73.94], [40.81, -73.93], [40.82, -73.93]];
  const held = {id:'n2', name:'Harlem', applied:false, held:true, polygon:ring, source:'model:x'};
  ui.run('renderSnapshot', snapshot(), {ledger:[], notices:[held]});
  assert.equal(ui.source('zone').features.length, 0, '보류 중인 공지는 아무것도 안 막습니다');
  assert.match(ui.element('banner').innerHTML, /waiting for a person/);
  ui.run('renderSnapshot', snapshot(), {ledger:[], notices:[{...held, applied:true, held:false, source:'human'}]});
  assert.equal(ui.source('zone').features.length, 1);
  assert.equal(ui.source('zone').features[0].properties.id, 'n2');
  assert.equal(ui.source('zone').features[0].geometry.coordinates[0].length, 4, '고리는 닫힙니다');
  assert.match(ui.element('banner').innerHTML, /read by a person/);
});

test('notice ledger lines read as words, not codes', () => {
  const ui = scene();
  const line = (id, code, detail={}) => ({id, at:Date.now()/1000, outcome:'unreadable',
    proposal:{asset_id:'airspace', action:'publish_notice', author:'runtime', params:{notice_id:'n9'}},
    decision:{verdict:'denied', reason:'…', code, detail}});
  ui.run('renderSnapshot', snapshot(), {ledger:[line('u1', 'notice_unreadable', {notice:'n9', why:'no model'}),
    line('p1', 'notice_published'), line('r1', 'notice_refused')], llm:{enabled:false, models:{}}, locks:{}});
  const feed = ui.element('feed').innerHTML;
  assert.match(feed, /airspace<\/b> airspace notice/);
  assert.match(feed, /not read by the runtime \(no model\)/);
  assert.match(feed, /confirmed by a person/);
  assert.match(feed, /refused by a person/);
  assert.doesNotMatch(feed, /r_notice/);
});

test('an approval that lands while the refusal is still playing is replayed yellow after it, not dropped', () => {
  const ui = scene();
  ui.run('renderDenials',{ledger:[denial('r1')]},0);
  ui.time(1000); ui.run('draw');                       // 빨강이 그려지는 중
  ui.run('renderDenials',{ledger:[approval('a1'), denial('r1')]},1000);
  ui.time(1016); ui.run('draw');                       // 다음 프레임: 예약된 승인이 살아 있어야 합니다
  const red = GROW_MS + CHECK_MS + HOLD_MS + FADE_MS;
  ui.time(red + 400); ui.run('draw');                  // 빨강 끝난 직후: 노랑이 뻗는 중
  assert.ok(ui.path('pending').length > 0, '승인 재생이 노랑으로 시작합니다');
  assert.equal(ui.path('approved').length, 0, '초록은 아직입니다');
  ui.time(red + GROW_MS + CHECK_MS + 200); ui.run('draw');
  assert.ok(ui.path('approved').length > 0, '그 다음에 초록');
});

test('a backlog of refusals is collapsed so the replay never falls more than one stage behind', () => {
  const ui = scene();
  ui.run('renderDenials',{ledger:[denial('b1')]},0);
  ui.time(500); ui.run('draw');
  const more = ['b2','b3','b4','b5'].map(id => denial(id));
  ui.run('renderDenials',{ledger:[...more.reverse(), denial('b1')]},500);
  ui.run('renderDenials',{ledger:[approval('ok'), ...more, denial('b1')]},600);
  const red = GROW_MS + CHECK_MS + HOLD_MS + FADE_MS;
  ui.time(red + 300); ui.run('draw');
  assert.ok(ui.path('pending').length > 0, '첫 빨강 다음에 곧바로 승인이 옵니다, 밀린 거절 넷은 버려집니다');
});
