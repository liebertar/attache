// Run with: node --test tests/test_map.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import * as geometry from '../frontend/map-route.mjs';

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
  // 카메라 조작은 호출만 기록합니다. 키 하나가 지도를 어떻게 움직였는지는 여기서 읽습니다.
  // 인자는 이쪽 realm 으로 복사합니다 — vm 안에서 만든 배열은 프로토타입이 달라 deepEqual 이 값이 같아도 틀립니다.
  const calls = [], record = name => (...args) => { calls.push([name, ...structuredClone(args)]); };
  const map = {on(){}, addControl(){}, keyboard:{disable:record('keyboard.disable')},
    panBy:record('panBy'), easeTo:record('easeTo'), flyTo:record('flyTo'),
    zoomIn:record('zoomIn'), zoomOut:record('zoomOut'), getBearing:()=>-28, getPitch:()=>45,
    addLayer(layer){layers.set(layer.id, structuredClone(layer));},
    setPaintProperty(id, name, value){layers.get(id).paint[name] = value;},
    getLayer(id){return layers.get(id);},
    getSource(id){
    if (!sources.has(id)) sources.set(id,{setData(data){this.data=data;}});
    return sources.get(id);
  }};
  const html = readFileSync(new URL('../frontend/map.html',import.meta.url),'utf8');
  const imports = Object.fromEntries(html.match(/import \{([^}]+)\}/)[1]
    .split(',').map(name=>[name.trim(), geometry[name.trim()]]));
  const {document:documentOverrides, ...rest} = overrides;
  const context = vm.createContext({...imports, console, Date, Map, Set, Math,
    location:{hostname:'localhost'}, performance:{now:()=>now},
    requestAnimationFrame(){}, document:{getElementById:element,querySelector:element, ...documentOverrides},
    maplibregl:{Map:function(){return map;}, NavigationControl:function(){}, AttributionControl:function(){},
      Popup:function(){return {setLngLat(){return this;}, setHTML(){return this;},
        addTo(){return this;}};}}, ...rest});
  const code = html.match(/<script type="module">([\s\S]*?)<\/script>/)[1]
    .replace(/^import .*?;\n/m,'');
  vm.runInContext(code,context);
  const run = (name,...args) => context[name](...args);
  return {run,get:name=>context[name],element,source:id=>sources.get(id)?.data,
    path:phase=>(sources.get('flightpath')?.data?.features || [])
      .filter(f=>f.properties.phase === phase),
    layer:id=>layers.get(id), time:value=>{now=value;}, calls, html};
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
  assert.match(ui.element('llm-line').textContent, /^tower · rules · drones · Nemotron Nano$/);
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
  assert.match(ui.element('banner').innerHTML, /<i>waiting for a person<\/i>/, '사람을 기다리는 줄은 기울임');
  assert.equal(ui.element('banner').hidden, false);
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
  // 모듈의 const 는 vm 문맥의 전역이 아니라, 디버그 핸들(window.__skynet.api)로 읽습니다.
  const api = ui => JSON.stringify(ui.get('window').__skynet.api);
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
  assert.match(feed, /tower<\/b> airspace notice/);
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

// 정보 수집. 날씨는 정책(이륙 정지), 사고는 공지(구역) — /state 의 weather · incidents · intake 모양 그대로.
test('the banner says a weather hold and who read it, a held report waits for a person, an unread bulletin shows raw', () => {
  const ui = scene();
  const bulletin = {id:'wx-1', kind:'weather', text:'KNYC 0929Z WIND 240 AT 18 GUST 28 KT VIS 2SM RA',
                    published_tick:2175, until_tick:2700};
  ui.run('renderSnapshot', {...snapshot(), bulletins:[bulletin]}, {ledger:[], notices:[], intake:{items:[]}, weather:{hold:null, held:[]}});
  assert.match(ui.element('banner').innerHTML, /WEATHER.*not yet read/);
  const hold = {id:'wx-1', reason:'WEATHER HOLD · gusts 14 m/s > 12', until_tick:2700, since_tick:2200, source:'grammar', report:{}};
  ui.run('renderSnapshot', {...snapshot(), bulletins:[bulletin]}, {ledger:[], notices:[],
    intake:{items:[{id:'wx-1', kind:'weather', read_by:'grammar'}]}, weather:{hold, held:[]}});
  const banner = ui.element('banner').innerHTML;
  assert.match(banner, /<b>WEATHER HOLD<\/b> · gusts 14 m\/s &gt; 12 · takeoffs held until tick 2700 — read by the rule grammar/);
  assert.doesNotMatch(banner, /not yet read/);
  assert.equal(ui.element('banner').hidden, false);
  // 모델이 읽은 보고서는 사람을 기다립니다 — 아무것도 세우지 않고 그렇게 말합니다.
  ui.run('renderSnapshot', {...snapshot(), bulletins:[]}, {ledger:[], notices:[], intake:{items:[]},
    weather:{hold:null, held:[{id:'wx-2', breaches:['gusts 20 m/s > 12'], source:'model:nvidia/nemotron-3-super-120b-a12b'}]},
    llm:{enabled:true, models:{super:'nvidia/nemotron-3-super-120b-a12b'}}});
  assert.match(ui.element('banner').innerHTML, /gusts 20 m\/s &gt; 12 — read by the super agent, <i>waiting for a person<\/i>/);
  // 못 읽은 것은 원문 그대로, 이유와 함께.
  ui.run('renderSnapshot', {...snapshot(), bulletins:[]}, {ledger:[], notices:[],
    intake:{items:[{id:'t1', kind:null, why:'no model', text:'Gusty afternoon <b>expected</b>'}]}, weather:{hold:null, held:[]}});
  assert.match(ui.element('banner').innerHTML, /INTAKE<\/b> · Gusty afternoon &lt;b&gt;expected&lt;\/b&gt; — not read by the runtime \(no model\)/);
});

test('an incident is painted like a zone and named on the banner; held it is neither', () => {
  const ui = scene();
  const ring = [[40.705, -74.015], [40.705, -74.012], [40.703, -74.012], [40.703, -74.015]];
  const notice = {id:'fdny-1', name:'FIRE · 1 Bowling Green', kind:'incident', applied:true, held:false,
                  source:'grammar', until_tick:3600, polygon:ring};
  const incident = {id:'fdny-1', name:'FIRE · 1 Bowling Green', kind:'fire', radius_m:200, until_tick:3600, applied:true, held:false};
  ui.run('renderSnapshot', {...snapshot(), bulletins:[{id:'fdny-1', kind:'incident', text:'FDNY 3-ALARM FIRE AT 1 BOWLING GREEN', published_tick:3000, until_tick:3600}]},
    {ledger:[], notices:[notice], incidents:[incident], intake:{items:[{id:'fdny-1', kind:'incident', read_by:'grammar'}]}, weather:{hold:null, held:[]}});
  const banner = ui.element('banner').innerHTML;
  assert.match(banner, /<b>FIRE · 1 Bowling Green<\/b> · 200 m keep-out until tick 3600 — read by the rule grammar/);
  assert.match(banner, /landing areas inside unusable/);
  assert.doesNotMatch(banner, /not yet read/);
  assert.equal(ui.source('zone').features.length, 1);
  assert.equal(ui.source('zone').features[0].properties.id, 'fdny-1');
  assert.equal(ui.source('zone').features[0].properties.name, 'FIRE · 1 Bowling Green',
               '원에 이름이 붙어야 건물이 가려도 무엇이 닫혔는지 보입니다');
  const held = {...notice, applied:false, held:true, source:'model:x'};
  ui.run('renderSnapshot', snapshot(), {ledger:[], notices:[held], incidents:[{...incident, applied:false, held:true}], intake:{items:[]}, weather:{hold:null, held:[]}});
  assert.equal(ui.source('zone').features.length, 0, '보류 중인 사고는 아무것도 안 막습니다');
  assert.match(ui.element('banner').innerHTML, /FIRE · 1 Bowling Green<\/b> · 200 m keep-out — read by the Agent agent, <i>waiting for a person<\/i>/);
});

test('a takeoff refused by the weather hold says WEATHER HOLD; a landing refused by the incident names it', () => {
  const ui = scene();
  const held = denial('wx-deny', {decision:{verdict:'denied', reason:'WEATHER HOLD · gusts 14 m/s > 12 (weather-hold:fly_route)',
    code:'policy', policy_hit:'weather-hold:fly_route', detail:{policy:'weather-hold:fly_route', until_tick:2700}}});
  ui.run('renderDenials', {ledger:[held]}, 0);
  assert.equal(ui.element('denial-why').textContent, 'WEATHER HOLD · takeoffs held until tick 2700');
  ui.time(GROW_MS + CHECK_MS + 10); ui.run('draw');
  assert.match(ui.source('stage-label').features[0].properties.label, /^REJECTED · WEATHER HOLD · takeoffs held until tick 2700/);
  assert.equal(ui.source('blocker').features.length, 0, '대기는 다각형이 아닙니다');
  // 사고 원 안의 착륙장. 런타임은 막은 구역의 이름(blocked_name)을 값으로 줍니다.
  const fire = denial('fire-deny', {proposal:{asset_id:'drone-02', action:'fly_route',
    params:{legs:[start, ...route], blocked_kind:'landing', blocked_volume:'fdny-1', blocked_name:'FIRE · 1 Bowling Green',
            blocked_leg:3, blocked_at:{lat:route.at(-1).lat, lon:route.at(-1).lon},
            blocked_polygon:[[40.705, -74.015], [40.705, -74.012], [40.703, -74.012]], blocked_floor_m:0, blocked_ceiling_m:null}},
    decision:{verdict:'denied', reason:'착륙 지점 둘레에 FIRE · 1 Bowling Green', code:'airspace', policy_hit:'airspace', forbids:'fdny-1'}});
  ui.run('renderDenials', {ledger:[fire]}, 0);
  assert.equal(ui.element('denial-why').textContent, 'NO ROOM TO LAND · FIRE · 1 Bowling Green');
  const depart = denial('dep-deny', {proposal:{asset_id:'drone-03', action:'depart', params:{}},
    decision:{verdict:'denied', reason:'WEATHER HOLD · gusts 14 m/s > 12 (weather-hold:depart)', code:'policy',
              policy_hit:'weather-hold:depart', detail:{policy:'weather-hold:depart', until_tick:2700}}});
  ui.run('renderDenials', {ledger:[depart]}, 100);
  assert.equal(ui.element('denial-what').textContent, 'depart');
  assert.match(ui.element('denial-why').textContent, /^WEATHER HOLD/);
});

test('the scoreboard has rows for takeoffs during a hold and flights into an incident scene', () => {
  const ui = scene();
  const snap = snapshot();
  snap.worlds.guarded.scoreboard = {weather_hold_takeoffs:0, incident_incursions:0};
  snap.worlds.direct.scoreboard = {weather_hold_takeoffs:2, incident_incursions:1};
  ui.run('renderSnapshot', snap, null);
  const rows = ui.element('rows').innerHTML;
  assert.match(rows, /Takeoffs during a weather hold<\/td>\s*<td class="zero">0<\/td>\s*<td class="hit">2<\/td>/);
  assert.match(rows, /Flights into an incident scene<\/td>\s*<td class="zero">0<\/td>\s*<td class="hit">1<\/td>/);
});

test('intake and weather ledger lines read as words, and opening a hold raises no recall card', () => {
  const ui = scene();
  const line = (id, action, code, detail={}, extra={}) => ({id, at:Date.now()/1000, outcome:'noted',
    proposal:{asset_id:'intake', action, author:'runtime', params:{}, ...extra},
    decision:{verdict:'auto', reason:'WEATHER HOLD · gusts 14 m/s > 12', code, detail}});
  const entries = [
    line('i1', 'intake', 'intake_received', {source:'sim'}),
    line('i2', 'intake', 'intake_read', {kind:'weather', read_by:'grammar'}),
    line('i3', 'intake', 'intake_unreadable', {why:'no model'}),
    line('w1', 'weather_hold', 'weather_hold', {until_tick:2700}, {asset_id:'fleet'}),
    line('w2', 'weather_hold', 'weather_hold_expired', {until_tick:2700}, {asset_id:'fleet'}),
    line('k1', 'incident_keepout', 'incident_keepout', {name:'FIRE · 1 Bowling Green', radius_m:200, until_tick:3600}, {asset_id:'fleet'}),
    {...line('l1', 'lift_weather_hold', 'weather_hold_lifted', {}, {asset_id:'fleet'}), outcome:'done',
     decision:{verdict:'auto', reason:'x', code:'weather_hold_lifted', approved_by:'관제사'}},
  ];
  ui.run('renderSnapshot', snapshot(), {ledger:entries, llm:{enabled:false, models:{}}, locks:{}, notices:[], intake:{items:[]}, weather:{hold:null, held:[]}});
  const feed = ui.element('feed').innerHTML;
  assert.match(feed, /tower<\/b> information intake[\s\S]*received from sim/);
  assert.match(feed, /read by the rule grammar · weather/);
  assert.match(feed, /not read \(no model\)/);
  assert.match(feed, /tower<\/b> weather hold[\s\S]*WEATHER HOLD · gusts 14 m\/s > 12 · takeoffs held until tick 2700/);
  assert.match(feed, /weather hold expired at tick 2700/);
  assert.match(feed, /incident keep-out[\s\S]*FIRE · 1 Bowling Green · 200 m keep-out until tick 3600/);
  assert.match(feed, /lift the weather hold[\s\S]*weather hold lifted by a person · 관제사/);
  assert.doesNotMatch(feed, /r_intake|r_weather/);
  ui.run('renderDenials', {ledger:entries}, 0);
  assert.equal(ui.element('denial').hidden, true, '대기가 열린 것은 회수 카드가 아닙니다');
});

// 기체 이름 아랫줄. 어느 모델이 이 기체를 모는지 — 런타임 쪽은 /state.agents, 직접 쪽은 시뮬레이터 필드.
test('the second label line names the model flying the aircraft, or "rules" when there is none', () => {
  // 직접 쪽 이름표도 봐야 하므로 BOTH 로 엽니다(지도는 기본으로 런타임 쪽만 그립니다).
  const ui = scene({localStorage:{getItem:key => key === 'skynet-show' ? 'both' : null, setItem(){}}});
  const snap = snapshot();
  snap.worlds.direct.assets['drone-01'].agent_model = 'nvidia/nemotron-3-super-120b-a12b';
  ui.run('renderSnapshot', snap, {ledger:[], llm:{enabled:true, models:{nano:'nemotron-3-nano:4b'}, host:'ollama'},
    agents:{'drone-01':{model:'nemotron-3-nano:4b', host:'ollama', world:'guarded', last_seen_tick:1, display:'Nemotron Nano 4B'}}});
  ui.run('draw');
  assert.equal(ui.source('guarded').features[0].properties.sub, 'Nemotron Nano 4B · ↑ 110 m');
  assert.equal(ui.source('direct').features[0].properties.sub, 'Nemotron Super 120B · ↑ 110 m',
               '직접 쪽은 원래 id 를 다듬어 씁니다');
  // 머리 카드의 모델 줄에도 기체 에이전트가 몇 대인지.
  assert.equal(ui.element('llm-line').textContent, 'tower · rules · drones · Nemotron Nano 4B ×1');
  // 필드가 없으면(옛 런타임·규칙 기단) 규칙입니다. 고도가 없으면 모델 이름만.
  const grounded = snapshot(1, 1, [], {lon:-73.97, lat:40.70, state:'ready'});
  ui.run('renderSnapshot', grounded, {ledger:[], llm:{enabled:false, models:{}}});
  ui.run('draw');
  assert.equal(ui.source('guarded').features[0].properties.sub, 'rules');
  assert.equal(ui.source('direct').features[0].properties.sub, 'rules');
  assert.doesNotMatch(ui.element('llm-line').textContent, /drones/);
  // 에이전트가 있어도 전부 규칙이면 "rules only" 뒤에 같은 말을 또 붙이지 않습니다.
  ui.run('renderSnapshot', grounded, {ledger:[], llm:{enabled:false, models:{}},
    agents:{'drone-01':{model:'', host:'', world:'guarded', last_seen_tick:1, display:''}}});
  assert.equal(ui.element('llm-line').textContent, 'rules only — no agent model configured');
  ui.run('draw');
  assert.equal(ui.source('guarded').features[0].properties.sub, 'rules');
  // display 가 비어 있으면 model id 를 다듬고, 그것도 없으면 규칙.
  assert.equal(ui.run('modelName', 'nemotron-3-nano:4b'), 'Nemotron Nano 4B');
  assert.equal(ui.run('modelName', 'gpt-oss-20b'), 'Gpt Oss 20B');
  assert.equal(ui.run('modelName', ''), '');
});

// 빈 카드는 빈 상자입니다. 값이 오기 전에는 카드가 없고, 값이 오면 그때 생깁니다.
test('cards with nothing to say are hidden; they appear with their first content', () => {
  const ui = scene();
  for (const id of ['score', 'acts', 'legend', 'banner', 'lostlink', 'denial', 'advisory'])
    assert.equal(ui.element(id).hidden, true, `${id} 는 처음에 숨어 있어야 합니다`);
  ui.run('renderSnapshot', snapshot(), {ledger:[], notices:[]});
  assert.equal(ui.element('score').hidden, false);
  assert.equal(ui.element('legend').hidden, false);
  assert.equal(ui.element('acts').hidden, true, '기록이 없으면 기록 카드도 없습니다');
  assert.equal(ui.element('banner').hidden, true);
  assert.equal(ui.element('lostlink').hidden, true);
  assert.equal(ui.element('round-tick').textContent, 'round 1 · tick 1');
  ui.run('renderSnapshot', snapshot(), {ledger:[approval()], notices:[]});
  assert.equal(ui.element('acts').hidden, false);
  assert.equal(ui.element('feed').innerHTML.match(/class="ev/g).length, 1);
  // 범례는 화면에 있는 것만. 격자가 없으면 천장 색도, 출처 줄도 없습니다.
  const legend = ui.element('legend').innerHTML;
  assert.match(legend, /APPROVED/);
  assert.match(legend, /Warehouse/);
  assert.doesNotMatch(legend, /No-fly grid|Ceiling|FAA UAS/);
  assert.doesNotMatch(legend, /Closed zone|DRONE LANDING AREA/);
  const snap = snapshot();
  snap.worlds.guarded.bands = [{name:'b', rule:'ceiling', ceiling_m:61, polygon:[[40.7, -73.98], [40.7, -73.97], [40.71, -73.97]]}];
  snap.worlds.guarded.landing_areas = [{name:'Pier 17', lat:40.706, lon:-74.002}];
  snap.worlds.guarded.zone = {active:true, id:'z1', name:'Z', polygon:[[40.7, -73.98], [40.7, -73.97], [40.71, -73.97]]};
  ui.run('renderSnapshot', snap, {ledger:[], notices:[]});
  const full = ui.element('legend').innerHTML;
  assert.match(full, /No-fly grid[\s\S]*Ceiling[\s\S]*122&nbsp;m/);
  assert.match(full, /FAA UAS Facility Map/);
  assert.match(full, /DRONE LANDING AREA/);
  assert.match(full, /Closed zone/);
});

test('the feed keeps ten lines at most', () => {
  const ui = scene();
  const many = Array.from({length:14}, (_, i) => approval(`a${i}`));
  ui.run('renderSnapshot', snapshot(), {ledger:many, llm:{enabled:false, models:{}}});
  assert.equal(ui.element('feed').innerHTML.match(/class="ev/g).length, 10);
});

// 키. 안내판에 적힌 조합이 전부이고, 지도를 먼저 누를 필요가 없습니다.
test('the keys card is three plain lines, and the keys it lists move the map', () => {
  const handlers = {};
  const ui = scene({document:{addEventListener(type, fn){ handlers[type] = fn; }}});
  const keys = ui.html.match(/<div class="card meta" id="keys"[\s\S]*?<\/div>\n<\/div>/)[0];
  assert.doesNotMatch(keys, /<kbd/, '키 모양은 그림입니다 — kbd 칩이 아닙니다');
  assert.deepEqual([...keys.matchAll(/data-t="(keys_\w+)"/g)].map(m => m[1]),
    ['keys_title', 'keys_mouse', 'keys_arrows', 'keys_letters'], '줄마다 낭독용 문장이 하나씩');
  // 세로 직사각형: 한 줄에 조작 하나(마우스 셋, 키보드 여섯).
  const grid = keys.match(/<div class="kgrid" aria-hidden="true">([\s\S]*?)\n  <\/div>/)[1];
  assert.equal((grid.match(/class="kk"/g) || []).length, 10, '조작 열 줄');
  assert.equal((grid.match(/<svg class="mouse"/g) || []).length, 3, '마우스 왼쪽·휠·오른쪽');
  assert.equal((grid.match(/class="kc"/g) || []).length, 11, 'Ctrl · 화살표 · Shift+화살표 · + − · N · H · 1–4 · L');
  assert.ok(!/<button|tabindex/.test(grid), '안내의 키 모양은 누를 수 없습니다');
  assert.doesNotMatch(keys, /⇧/, 'Mac 기호 대신 글자로');
  assert.match(grid, /class="kc">Shift</);
  assert.match(grid, /class="kc">Ctrl</, '트랙패드용 Ctrl+드래그');
  const t = ui.get('t');
  assert.equal(t('keys_mouse'), 'drag pan · scroll zoom · right-drag or Ctrl+drag orbit');
  assert.equal(t('keys_arrows'), 'arrows pan · Shift+arrows rotate/tilt · +/- zoom');
  assert.equal(t('keys_letters'), 'N north · H home · 1-4 focus drone · L tower lines');
  // MapLibre 의 키 처리는 끕니다 — 둘 다 살아 있으면 한 번에 두 번 움직입니다.
  assert.ok(ui.calls.some(c => c[0] === 'keyboard.disable'));
  ui.run('renderSnapshot', snapshot(), null);
  ui.calls.length = 0;
  const press = (key, extra = {}) => {
    let prevented = false;
    handlers.keydown({key, target:{tagName:'BODY'}, preventDefault(){ prevented = true; }, ...extra});
    return prevented;
  };
  assert.equal(press('ArrowRight'), true);
  assert.deepEqual(ui.calls.at(-1).slice(0, 2), ['panBy', [100, 0]], '오른쪽 화살표는 오른쪽으로 이동');
  press('ArrowUp');
  assert.deepEqual(ui.calls.at(-1)[1], [0, -100]);
  press('ArrowLeft', {shiftKey:true});
  assert.equal(ui.calls.at(-1)[0], 'easeTo');
  assert.equal(ui.calls.at(-1)[1].bearing, -58, 'Shift+왼쪽은 회전');
  press('ArrowUp', {shiftKey:true});
  assert.equal(ui.calls.at(-1)[1].pitch, 60, 'Shift+위는 기울이기');
  press('+'); assert.equal(ui.calls.at(-1)[0], 'zoomIn');
  press('-'); assert.equal(ui.calls.at(-1)[0], 'zoomOut');
  press('n'); assert.equal(ui.calls.at(-1)[1].bearing, 0);
  press('h'); assert.equal(ui.calls.at(-1)[0], 'flyTo');
  press('1'); assert.equal(ui.calls.at(-1)[0], 'flyTo');
  assert.equal(ui.calls.at(-1)[1].zoom, 16.4, '숫자 키는 그 기체로');
  // L 은 관제 신호선을 껐다 켭니다. 카메라 키가 아니라 지도를 움직이지 않습니다.
  const cameraCalls = ui.calls.length;
  assert.equal(press('l'), true);
  assert.equal(ui.calls.length, cameraCalls, 'L 은 카메라를 움직이지 않습니다');
  assert.equal(ui.source('tower').features.length, 0, '끄면 탑도 사라집니다');
  press('l');
  // 글을 쓰는 중이거나 조합키가 눌려 있으면 지도가 아닙니다.
  const before = ui.calls.length;
  assert.equal(press('ArrowRight', {target:{tagName:'INPUT'}}), false);
  assert.equal(press('ArrowRight', {metaKey:true}), false);
  assert.equal(press('x'), false);
  assert.equal(ui.calls.length, before);
});

// 링크 두절. 런타임이 그 기체의 공간을 그대로 잡고 있다는 것이 이름표·회랑·카드·기록·범례에서 같이 보입니다.
test('a lost link is named on the aircraft, keeps its corridor with a pulsing shell, and raises a card until restored', () => {
  const ui = scene();
  const lost = {links:{'drone-01':{status:'lost', since_tick:400, last_seen_tick:399}}, ledger:[], notices:[]};
  ui.run('renderSnapshot', snapshot(2, 1, route, {lon:-73.97, lat:40.705, alt_m:110, state:'delivering'}), lost);
  ui.time(700); ui.run('draw');
  const label = ui.source('guarded').features[0].properties;
  assert.equal(label.work, 'LOST LINK', '상태 자리에 링크 두절');
  assert.equal(label.tone, '#ff9d95');
  assert.ok(ui.path('approved').length > 0, '회랑은 그대로 — 공간은 예약된 채입니다');
  const shell = ui.path('shell');
  assert.ok(shell.length > 0, '회랑 둘레에 껍질');
  assert.ok(shell.every(f => f.properties.height > f.properties.base));
  const opacity = ui.layer('flightpath-shell').paint['fill-extrusion-opacity'];
  assert.ok(opacity >= .25 && opacity <= .85, `테두리는 숨쉬는 범위 안 ${opacity}`);
  ui.time(1500); ui.run('draw');
  assert.notEqual(ui.layer('flightpath-shell').paint['fill-extrusion-opacity'], opacity, '껍질이 숨쉽니다');
  const card = ui.element('lostlink');
  assert.equal(card.hidden, false);
  assert.match(card.innerHTML, /LOST LINK<\/span><b>drone-01<\/b><span class="meta">since tick 400 · space reserved/);
  assert.equal(card.dataset?.asset ?? 'drone-01', 'drone-01');
  assert.match(ui.element('legend').innerHTML, /RESERVED \(lost link\)/);
  // 복구되면 전부 사라집니다.
  const back = {links:{'drone-01':{status:'ok', since_tick:420, last_seen_tick:420}}, ledger:[], notices:[]};
  ui.run('renderSnapshot', snapshot(3, 1, route, {lon:-73.97, lat:40.706, alt_m:110, state:'delivering'}), back);
  ui.time(2200); ui.run('draw');
  assert.equal(ui.element('lostlink').hidden, true);
  assert.equal(ui.path('shell').length, 0);
  assert.equal(ui.layer('flightpath-shell').paint['fill-extrusion-opacity'], 0);
  assert.equal(ui.source('guarded').features[0].properties.work, '');
  assert.doesNotMatch(ui.element('legend').innerHTML, /RESERVED/);
  // 런타임이 links 를 아직 안 실으면(옛 런타임) 아무도 안 끊긴 것입니다.
  ui.run('renderSnapshot', snapshot(4, 1, route, {lon:-73.97, lat:40.707, alt_m:110, state:'delivering'}), {ledger:[]});
  ui.run('draw');
  assert.equal(ui.element('lostlink').hidden, true);
});

test('link ledger lines read as words and raise no denial card', () => {
  const ui = scene();
  const line = (id, code, detail) => ({id, at:Date.now()/1000, outcome:'noted',
    proposal:{asset_id:'drone-02', action:code, author:'runtime', params:{}},
    decision:{verdict:'auto', reason:'…', code, detail}});
  ui.run('renderSnapshot', snapshot(), {ledger:[line('l1', 'link_lost', {since_tick:400, last_seen_tick:399}),
    line('l2', 'link_restored', {since_tick:400, restored_tick:431})], llm:{enabled:false, models:{}}});
  const feed = ui.element('feed').innerHTML;
  assert.match(feed, /drone-02<\/b> link lost[\s\S]*no telemetry since tick 400 — the filed space stays reserved/);
  assert.match(feed, /drone-02<\/b> link restored[\s\S]*telemetry back — reserved space released/);
  assert.doesNotMatch(feed, /r_link/);
  assert.equal(ui.element('denial').hidden, true);
});

// 테두리는 네 모서리의 가는 막대입니다. 정북으로 곧은 한 구간이면 옆 거리가 경도 차이 그대로라 미터로 잴 수 있습니다.
test('the corridor outline is four thin rails just outside the corridor, at its altitude', () => {
  const north = geometry.makeCurve({...start, alt_m:55}, [{lon:start.lon, lat:40.71, alt_m:55}]);
  const total = north.progress.at(-1);
  const inner = geometry.curveRibbon(north, 0, total);
  const shell = geometry.curveShell(north, 0, total);
  assert.ok(shell.length > 0 && shell.flatMap(p => p.polygon).flat().every(Number.isFinite));
  const east = lon => (lon - start.lon) * Math.cos(start.lat * Math.PI / 180) * 110570;
  const offsets = shell.map(p => p.polygon.slice(0, 4).reduce((sum, [lon]) => sum + east(lon), 0) / 4);
  const widths = shell.map(p => { const xs = p.polygon.map(([lon]) => east(lon)); return Math.max(...xs) - Math.min(...xs); });
  assert.ok(offsets.every(x => Math.abs(Math.abs(x) - 14) < 0.3), `막대는 중심에서 14 m (회랑 반폭 9 + 5): ${offsets.map(x => x.toFixed(1))}`);
  assert.ok(offsets.some(x => x > 0) && offsets.some(x => x < 0), '양옆 모두');
  assert.ok(widths.every(w => Math.abs(w - 1.6) < 0.1), `막대는 가늘게: ${widths.map(w => w.toFixed(2))}`);
  assert.ok(shell.every(p => Math.abs(p.height - p.base - 1.6) < 1e-9), '막대 두께도 1.6 m');
  // 위아래로 회랑을 5 m 씩 감쌉니다 — 위 막대 윗면과 아래 막대 밑면.
  const corridor = {base:Math.min(...inner.map(p => p.base)), height:Math.max(...inner.map(p => p.height))};
  assert.ok(Math.abs(Math.max(...shell.map(p => p.height)) - (corridor.height + 5)) < 1e-9);
  assert.ok(Math.abs(Math.min(...shell.map(p => p.base)) - (corridor.base - 5)) < 1e-9);
  // 옆 거리를 안 주면 ribbon 은 예전 그대로입니다(회랑 판 폭 18 m, 중심선 위).
  const plate = geometry.ribbon([{lon:start.lon, lat:40.70, alt_m:55}, {lon:start.lon, lat:40.71, alt_m:55}])[0];
  const xs = plate.polygon.map(([lon]) => east(lon));
  assert.ok(Math.abs(Math.max(...xs) - 9) < 0.1 && Math.abs(Math.min(...xs) + 9) < 0.1);
});

// 승인 화면. 링크 두절 통보 카드가 사람 말로 오르고, 배너가 끊긴 기체를 말합니다.
async function tower(state, compare) {
  const html = readFileSync(new URL('../frontend/index.html', import.meta.url), 'utf8');
  const elements = new Map();
  const noop = new Proxy({}, {get: () => () => {}});
  const element = id => {
    if (!elements.has(id)) elements.set(id, {style:{}, innerHTML:'', textContent:'', clientWidth:100, clientHeight:60,
      getContext: () => noop, dataset:{}});
    return elements.get(id);
  };
  const context = vm.createContext({console, Date, Map, Set, Math, Number, Object, JSON, String,
    location:{hostname:'localhost'}, window:{devicePixelRatio:1},
    document:{getElementById:element, addEventListener(){}},
    setInterval(){}, fetch: url => Promise.resolve({json: () => Promise.resolve(url.endsWith('/state') ? state : compare)})});
  vm.runInContext(html.match(/<script>([\s\S]*?)<\/script>/)[1], context);
  await context.refresh();
  return {element, html};
}

test('the approval screen lists a lost-link notice as a human decision and names the aircraft on its banner', async () => {
  const world = {assets:{}, pads:{}, scoreboard:{spend_usd:0, human_approvals:0}, fleet_limit:450, events:[]};
  const compare = {tick:431, recall_tick:null, worlds:{guarded:world, direct:structuredClone(world)}};
  const state = {llm:{enabled:false, models:{}}, ledger:[], incidents:[], weather:{},
    links:{'drone-02':{status:'lost', since_tick:400, last_seen_tick:399}, 'drone-01':{status:'ok', since_tick:0, last_seen_tick:431}},
    awaiting_human:[{id:'p1', asset_id:'drone-02', action:'lost_link_notice', cost_usd:0, blast_radius:'schedule',
                     rationale:'no telemetry since tick 400 <b>'}]};
  const ui = await tower(state, compare);
  const inbox = ui.element('inbox').innerHTML;
  assert.match(inbox, /<b>drone-02<\/b> · 링크 두절 통보/);
  assert.match(inbox, /no telemetry since tick 400 &lt;b&gt;/, '모델·런타임 문장은 마크업으로 실행되지 않습니다');
  assert.match(inbox, /data-ok="p1"/);
  const banner = ui.element('recall').innerHTML;
  assert.match(banner, /drone-02 링크 두절 \(틱 400 부터\)/);
  assert.doesNotMatch(banner, /drone-01/);
  assert.equal(ui.element('recall').style.display, 'block');
  // 아무도 안 끊겼고 카드도 없으면 배너도 없습니다.
  const quiet = await tower({...state, links:{}, awaiting_human:[]}, compare);
  assert.equal(quiet.element('recall').style.display, 'none');
  assert.match(quiet.element('inbox').innerHTML, /없음/);
});

// 로컬에서는 관제 대역(Super 자리)과 기체 모델이 같은 4B id 입니다. 드론 신청서에 "super" 가 붙으면
// 드론이 120B 를 부르는 것처럼 읽힙니다 — 기체 줄에는 그 기체에 실린 모델, 관제 줄은 따로.
test('a drone request is tagged with the drone\'s own model even when the tower stand-in shares its id', () => {
  const ui = scene();
  const runtime = {ledger:[approval('m1', {proposal:{asset_id:'drone-01', action:'fly_route',
      author:'nemotron-3-nano:4b', params:{legs:[start,...route], drafter:'astar'}},
      decision:{verdict:'auto', reason:'한도 안', code:'within_limits'}})],
    llm:{enabled:true, host:'ollama', models:{nano:'', super:'nemotron-3-nano:4b', ultra:''}},
    agents:{'drone-01':{model:'nemotron-3-nano:4b', host:'ollama', world:'guarded', last_seen_tick:1,
      display:'Nemotron Nano 4B'}}, locks:{}};
  ui.run('renderSnapshot', snapshot(), runtime);
  const feed = ui.element('feed').innerHTML;
  assert.match(feed, /drone-01<\/b> <span class="tier">Nano 4B<\/span>/, feed);
  assert.doesNotMatch(feed, /class="tier">super</);
  assert.equal(ui.element('llm-line').textContent,
    'tower · Nemotron Nano 4B (Super stand-in) · ollama · drones · Nemotron Nano 4B ×1');
  // 진짜 Super 가 관제에 있으면 대역 표시가 없습니다.
  ui.run('renderSnapshot', snapshot(), {...runtime,
    llm:{enabled:true, host:'nebius', models:{nano:'', super:'nvidia/nemotron-3-super-120b-a12b', ultra:''}}});
  assert.match(ui.element('llm-line').textContent, /^tower · Nemotron Super 120B · nebius · drones/);
});

// 패널마다 접기·펼치기. 상태는 저장소에 남고, 저장소가 막혀도 화면은 돕니다.
test('every panel minimizes and expands, the state persists, and a blocked storage breaks nothing', () => {
  const saved = {};
  const ui = scene({localStorage:{getItem:key => key === 'skynet-panels' ? '{"keys":true}' : null,
                                  setItem(key, value){ saved[key] = value; }}});
  assert.equal(ui.run('isMinimized', 'keys'), true, '저장된 상태로 시작합니다');
  ui.run('applyLanguage');
  assert.equal(ui.element('keys').min, true);
  for (const id of ['head', 'keys', 'legend', 'score', 'acts', 'briefing', 'banner', 'denial', 'advisory',
                    'lostlink']){
    ui.run('setMinimized', id, true);
    assert.equal(ui.run('isMinimized', id), true, id);
    ui.run('setMinimized', id, false);
    assert.equal(ui.run('isMinimized', id), false, id);
  }
  assert.deepEqual(JSON.parse(saved['skynet-panels']).keys, false);
  ui.run('setMinimized', 'banner', true);
  const bulletin = {id:'n1', kind:'notam', text:'AREA BOUNDED BY 404310N0735920W SFC-400FT AGL 0907-0912Z',
                    published_tick:525, until_tick:900};
  ui.run('renderSnapshot', {...snapshot(), bulletins:[bulletin]}, {ledger:[], notices:[]});
  assert.match(ui.element('banner').innerHTML, /data-min="banner" aria-expanded="false"[^>]*>\+</);
  assert.match(ui.element('banner').innerHTML, /NOTAM/, '접혀도 내용은 그려 둡니다 — 펼치면 바로 보이게');
  const blocked = scene({localStorage:{getItem(){ throw new Error('blocked'); }, setItem(){ throw new Error('blocked'); }}});
  blocked.run('setMinimized', 'score', true);
  assert.equal(blocked.run('isMinimized', 'score'), true);
  blocked.run('renderSnapshot', snapshot(), {ledger:[], notices:[]});
});

test('a minimized refusal card opens again when a new refusal arrives', () => {
  const ui = scene();
  ui.run('setMinimized', 'denial', true);
  ui.run('renderDenials', {ledger:[denial('fresh-1')]}, 0);
  assert.equal(ui.run('isMinimized', 'denial'), false);
});

test('lines the runtime wrote itself name the tower, not an internal id', () => {
  const ui = scene();
  const entry = approval('i1', {proposal:{asset_id:'intake', action:'intake', author:'runtime', params:{}},
    decision:{verdict:'auto', reason:'읽음', code:'intake_read', detail:{kind:'weather', read_by:'grammar'}}});
  ui.run('renderSnapshot', snapshot(), {ledger:[entry], locks:{}});
  const feed = ui.element('feed').innerHTML;
  assert.match(feed, /<b>tower<\/b>/, feed);
  assert.doesNotMatch(feed, /<b>intake<\/b>/);
});


// ── 관제 브리핑. 실제 모양 그대로: /state.briefing 항목(status 가 처지, trust 는 출처 도메인의 공식 여부)과
//    규칙마다 공지 책에 걸리는 BriefingNotice(id = rule_id, polygon, ceiling_m, citation). ─────────────────
function briefing(extra = {}) {
  return {enabled:true, source:'live', mode:'live', runs:1, last_run_tick:120, credits_used:3, budget:20,
    summary:'Two crane permits and one park closure near today’s landing areas.',
    items:[
      {id:'brief-c1', kind:'crane', place:'110th Street Manhattan', summary:'Tower crane permit, 95 m, active all week',
       url:'https://www1.nyc.gov/permit/123', domain:'nyc.gov', trust:'official', status:'applied',
       rule_id:'brief-c1', until_tick:5000},
      {id:'brief-p1', kind:'closure', place:'Morningside Park', summary:'Park closed for a film shoot until 18:00',
       url:'https://www.nycgovparks.org/closure', domain:'nycgovparks.org', trust:'official', status:'applied',
       rule_id:'brief-p1'},
      {id:'brief-e1', kind:'event', place:'Yankee Stadium', summary:'Game tonight, crowds from 18:00',
       url:'https://untrusted.example/news', domain:'untrusted.example', trust:'unofficial', status:'held',
       rule_id:'brief-e1'},
      {id:'brief-i1', kind:'info', place:'East Side', summary:'UN General Assembly week', url:'javascript:alert(1)',
       domain:'example.org', trust:'unofficial', status:'info', rule_id:null},
    ], ...extra};
}
function briefingNotices({eventApplied = false} = {}) {
  const circle = (lat, lon, r) => [[lat + r, lon], [lat, lon + r], [lat - r, lon], [lat, lon - r]];
  const cite = domain => ({source_url:`https://${domain}/x`, title:'t', domain, fetched_at:0, read_by:'grammar',
                           trust:'official', query:'', recorded:false});
  return [
    {id:'brief-c1', name:'CRANE · 110th Street Manhattan · 95 m', kind:'crane', applied:true, held:false,
     source:'grammar', polygon:circle(40.7995, -73.9535, 0.0002), floor_m:0, ceiling_m:95, citation:cite('nyc.gov')},
    {id:'brief-p1', name:'CLOSED · Morningside Park', kind:'closure', applied:true, held:false, source:'grammar',
     polygon:circle(40.805, -73.959, 0.0005), floor_m:0, ceiling_m:-1, citation:cite('nycgovparks.org')},
    {id:'brief-e1', name:'EVENT · Yankee Stadium', kind:'event', applied:eventApplied, held:!eventApplied,
     source:eventApplied ? 'human' : 'grammar', polygon:circle(40.8296, -73.9262, 0.003), floor_m:0,
     ceiling_m:null, citation:cite('untrusted.example')},
  ];
}

test('the tower briefing panel lists what the tower read, its source and how far it applies', () => {
  const ui = scene();
  ui.run('renderSnapshot', snapshot(), {ledger:[], notices:briefingNotices(), briefing:briefing()});
  const card = ui.element('briefing');
  assert.equal(card.hidden, false);
  assert.match(card.innerHTML, /tower briefing/, '제목은 다른 카드와 같은 줄에');
  assert.match(card.innerHTML, /data-min="briefing"/, '접기 단추도 같은 것');
  assert.match(card.innerHTML, /data-kind="crane"[\s\S]*110th Street Manhattan/);
  assert.match(card.innerHTML,
    /<a href="https:\/\/www1\.nyc\.gov\/permit\/123" target="_blank" rel="noopener noreferrer">nyc\.gov<\/a>/);
  assert.equal((card.innerHTML.match(/>APPLIED</g) || []).length, 2, '적용은 status 가 말합니다(trust 가 아니라)');
  assert.match(card.innerHTML, />WAITING FOR A PERSON</);
  assert.match(card.innerHTML, />INFO ONLY</);
  assert.doesNotMatch(card.innerHTML, /javascript:/, '검색이 준 주소는 http(s) 만 링크가 됩니다');
  assert.match(card.innerHTML, /briefed at tick 120 · 3 of 20 searches/);
  assert.equal(ui.element('banner').hidden, true, '브리핑 공지는 배너가 아니라 브리핑 카드가 말합니다');
  ui.run('renderSnapshot', snapshot(), {ledger:[], notices:[], briefing:briefing({source:'recorded'})});
  assert.match(ui.element('briefing').innerHTML, /RECORDED/, '녹화본은 녹화본이라고 말합니다');
  ui.run('renderSnapshot', snapshot(), {ledger:[], notices:[], briefing:{enabled:true, source:'recorded', runs:0,
    summary:'아직 브리핑하지 않았습니다.', items:[]}});
  assert.equal(ui.element('briefing').hidden, true, '한 번도 안 돌았으면 카드가 없습니다');
  ui.run('renderSnapshot', snapshot(), {ledger:[], notices:[]});
  assert.equal(ui.element('briefing').hidden, true, '브리핑이 없으면 카드도 없습니다');
});

test('a briefing crane stands on the map, an applied event is painted with its name, a closed landing area goes grey', () => {
  const ui = scene();
  const snap = snapshot();
  snap.worlds.guarded.landing_areas = [{id:'la-morningside', name:'Morningside Park', lat:40.805, lon:-73.959},
                                       {id:'la-pier', name:'Pier 17', lat:40.706, lon:-74.002}];
  ui.run('renderSnapshot', snap, {ledger:[], notices:briefingNotices(), briefing:briefing()});
  const crane = ui.source('brief-crane').features;
  assert.ok(crane.length >= 2, '가는 기둥과 꼭대기 팔');
  assert.ok(crane.some(f => Math.abs(f.properties.height - 95) < 0.01), '높이는 규칙의 천장 그대로');
  assert.equal(ui.source('brief-crane-label').features[0].properties.label, 'CRANE 95 m · nyc.gov');
  assert.equal(ui.source('zone').features.length, 0,
               '크레인·폐쇄는 붉은 원이 아니고, 사람을 기다리는 행사는 아무것도 안 막습니다');
  const [closed, open] = ui.source('landing').features.map(f => f.properties);
  assert.equal(closed.closed, true);
  assert.equal(closed.tag, 'CLOSED · nycgovparks.org');
  assert.equal(open.closed, false, '원 밖의 착륙장은 그대로');
  assert.equal(ui.source('landing-disc').features[0].properties.closed, true);
  ui.run('renderSnapshot', snap, {ledger:[], notices:briefingNotices({eventApplied:true}), briefing:briefing()});
  const zone = ui.source('zone').features;
  assert.equal(zone.length, 1, '사람이 확인한 행사는 구역처럼 칠합니다');
  assert.equal(zone[0].properties.name, 'EVENT · Yankee Stadium');
  assert.match(ui.element('legend').innerHTML, /Crane \(briefing\)[\s\S]*Closed landing area/);
});

test('a refusal caused by a briefing rule names the rule and its source', () => {
  const ui = scene();
  const crane = denial('c1', {proposal:{asset_id:'drone-01', action:'fly_route',
    params:{legs:[start, ...route], blocked_kind:'forbidden', blocked_volume:'brief-c1', blocked_leg:1,
            blocked_ceiling_m:95, blocked_at:{lat:route[0].lat, lon:route[0].lon},
            blocked_polygon:[[40.7995, -73.9535], [40.7996, -73.9535], [40.7996, -73.9534]]}},
    decision:{verdict:'denied', reason:'크레인', code:'airspace'}});
  ui.run('renderSnapshot', snapshot(), {ledger:[], notices:briefingNotices(), briefing:briefing()});
  ui.run('renderDenials', {ledger:[crane]}, 0);
  assert.equal(ui.element('denial-why').textContent, 'leg 1 enters CRANE 95 m (nyc.gov)');
  ui.time(GROW_MS + CHECK_MS + 10); ui.run('draw');
  assert.match(ui.source('stage-label').features[0].properties.label, /^REJECTED · CRANE 95 m \(nyc\.gov\)/);
  assert.equal(ui.source('blocker').features[0].properties.height, 95, '크레인은 건물처럼 세웁니다');
  // 항목 목록에서 빠져도(최근 몇 개만 실림) 공지 책의 출처로 이름을 붙입니다.
  const later = scene();
  later.run('renderSnapshot', snapshot(), {ledger:[], notices:briefingNotices(), briefing:briefing({items:[]})});
  later.run('renderDenials', {ledger:[crane]}, 0);
  assert.equal(later.element('denial-why').textContent, 'leg 1 enters CRANE 95 m (nyc.gov)');
  // 연출 자막도 크레인과 출처를 말합니다.
  const demo = demoShot({notices:briefingNotices().slice(0, 1)});
  assert.equal(demo.caption, 'Tower briefing — CRANE · 110th Street Manhattan · 95 m, from nyc.gov. '
    + 'It applies now: routes through it are refused.');
  assert.ok(Math.abs(demo.fly[1].center[1] - 40.7995) < 1e-9, '크레인 자리로');
});

// ── 모델이 한 일 (hover card). params.model_trace 만 씁니다. ──────────────────────
const AGENTS = {'drone-01':{model:'nemotron-3-nano:4b', host:'ollama', world:'guarded', last_seen_tick:1,
                            display:'Nemotron Nano 4B', model_ok:true}};
function traced(trace, params = {}) {
  return approval('tr1', {proposal:{asset_id:'drone-01', action:'fly_route', author:'nemotron-3-nano:4b',
    params:{legs:[start, ...route], drafter:'astar', model_trace:trace, ...params}},
    decision:{verdict:'auto', reason:'한도 안', code:'within_limits'}});
}

test('the hover card tells the model’s part in plain words, from the aircraft and from a ledger line', () => {
  const ui = scene();
  const trace = {form:{model:'nemotron-3-nano:4b', action:'fly_route', latency_ms:1820, used:true,
      concern:'has a delivery to Morningside Park and no cleared route',
      rationale:'Battery is full and the park is open.', fallback_reason:''},
    route:{source:'choice', draft:null,
      choice:{candidates:[{id:'a', label:'low route'}, {id:'b', label:'high route'}, {id:'c', label:'river route'}],
              chosen:'a', reason:'weather hold expected'}}};
  ui.run('renderSnapshot', snapshot(), {ledger:[traced(trace)], agents:AGENTS, locks:{}});
  assert.equal(ui.run('showTraceFor', 'drone-01'), true);
  const card = ui.element('trace');
  assert.equal(card.hidden, false);
  assert.match(card.innerHTML, /<b>Nemotron Nano 4B<\/b>/);
  assert.match(card.innerHTML, /1\.8 s/);
  assert.match(card.innerHTML,
    /<dt>Asked<\/dt><dd>has a delivery to Morningside Park and no cleared route<\/dd>/);
  assert.match(card.innerHTML,
    /<dt>Answered<\/dt><dd>delivery route — “Battery is full and the park is open\.”<\/dd>/);
  assert.match(card.innerHTML, /<dt>Code<\/dt><dd>accepted<\/dd>/);
  assert.match(card.innerHTML,
    /<dt>Route<\/dt><dd>the model chose the low route among 3 candidates — “weather hold expected”<\/dd>/);
  assert.match(card.innerHTML, /<dt>Runtime<\/dt><dd>APPROVED — within limits<\/dd>/);
  assert.doesNotMatch(card.innerHTML, /model_trace|[{}]/, '원문 JSON 은 안 보여 줍니다');
  // 기록 줄에 올려도 같은 카드가 그 줄의 신청서로 뜹니다.
  ui.run('hideTrace');
  ui.run('showTraceForRow', {dataset:{entry:'tr1'}});
  assert.equal(ui.element('trace').hidden, false);
  assert.match(ui.element('trace').innerHTML, /<b>Nemotron Nano 4B<\/b>/);
  ui.run('hideTrace', 'feed');
  assert.equal(ui.element('trace').hidden, true, '마우스가 떠나면 카드도 사라집니다');
});

test('the hover card says when rules wrote the form, and when the model draft failed', () => {
  const rules = scene();
  const noModel = {form:{model:'', concern:'battery at 18% and no charger booked', action:'fly_route',
      rationale:'', latency_ms:null, used:false, fallback_reason:'no model'},
    route:{source:'straight', choice:null, draft:null}};
  rules.run('renderSnapshot', snapshot(), {ledger:[traced(noModel)], locks:{}});
  rules.run('showTraceFor', 'drone-01');
  const plain = rules.element('trace').innerHTML;
  assert.match(plain, /<b>rules<\/b>/);
  assert.match(plain, /<dt>Answered<\/dt><dd>no model answer<\/dd>/);
  assert.match(plain, /<dt>Code<\/dt><dd>rules wrote it — no model<\/dd>/);
  assert.match(plain, /<dt>Route<\/dt><dd>straight line<\/dd>/);
  // 모델이 있었지만 제때 답하지 않은 것은 다른 일입니다 — 이름은 그 모델, 코드는 규칙이 대신 씀.
  const late = scene();
  const timeout = {form:{model:'', concern:'needs a route', action:'fly_route', rationale:'', latency_ms:9000,
      used:false, fallback_reason:'timeout'}, route:{source:'astar', choice:null, draft:null}};
  late.run('renderSnapshot', snapshot(), {ledger:[traced(timeout)], agents:AGENTS, locks:{}});
  late.run('showTraceFor', 'drone-01');
  assert.match(late.element('trace').innerHTML, /<b>Nemotron Nano 4B<\/b>/);
  assert.match(late.element('trace').innerHTML, /<dt>Code<\/dt><dd>rules wrote it — no answer in time<\/dd>/);
  assert.match(late.element('trace').innerHTML, /<dt>Route<\/dt><dd>A\* \(rules\)<\/dd>/);
  // 규칙이 후보 중에 고른 경우 — 둘째 스택의 실제 원장 모양 그대로(source 는 astar, choice 가 같이 옴).
  const picked = scene();
  const rulesChoice = {form:{model:'', concern:'has a delivery to Sara D. Roosevelt Park, no cleared route',
      action:'fly_route', rationale:'배달지 Sara D. Roosevelt Park, 배터리 55%', latency_ms:0, used:false,
      fallback_reason:'no model'},
    route:{source:'astar', draft:null, choice:{candidates:[{id:'a', label:'shortest', length_m:2739, max_alt_m:70},
      {id:'b', label:'lowest altitude', length_m:2736, max_alt_m:70}], chosen:'b',
      reason:'rules: the previous candidate was refused (airspace)'}}};
  picked.run('renderSnapshot', snapshot(), {ledger:[traced(rulesChoice, {route_choice:{chosen:'b', path:'rules',
    model:'', reason:'rules: the previous candidate was refused (airspace)', candidates:[]}})], locks:{}});
  picked.run('showTraceFor', 'drone-01');
  const chosen = picked.element('trace').innerHTML;
  assert.match(chosen, /<dt>Route<\/dt><dd>rules chose the lowest altitude among 2 candidates<\/dd>/);
  assert.doesNotMatch(chosen, /0\.0 s/, '규칙이 쓴 신청서에는 걸린 시간을 붙이지 않습니다');
  // 모델 초안이 건물을 지나 버려서 A* 가 그린 경우.
  const drafted = scene();
  const failed = {form:{model:'nemotron-3-nano:4b', concern:'refused once, needs a new route', action:'fly_route',
      rationale:'Go around the block to the west.', latency_ms:900, used:true, fallback_reason:''},
    route:{source:'astar', choice:null,
      draft:{asked:true, latency_ms:4200, breach:'crossed bldg-t02452, roof 114 m', used:false}}};
  drafted.run('renderSnapshot', snapshot(), {ledger:[traced(failed)], agents:AGENTS, locks:{}});
  drafted.run('showTraceFor', 'drone-01');
  assert.match(drafted.element('trace').innerHTML,
    /<dt>Route<\/dt><dd>model draft failed — it crossed a 114 m building; A\* drew it<\/dd>/);
});

test('an older line without a trace shows only who wrote and who drew it, in either language', () => {
  const ui = scene({localStorage:{getItem:key => key === 'skynet-lang' ? 'ko' : null, setItem(){}}});
  ui.run('renderSnapshot', snapshot(), {ledger:[approval('old1', {proposal:{asset_id:'drone-01',
    action:'fly_route', author:'rules', params:{legs:[start, ...route], drafter:'astar'}},
    decision:{verdict:'auto', reason:'한도 안', code:'within_limits'}})], locks:{}});
  ui.run('showTraceFor', 'drone-01');
  const card = ui.element('trace').innerHTML;
  assert.match(card, /<dt>신청서 작성<\/dt><dd>규칙<\/dd>/);
  assert.match(card, /<dt>경로 작성<\/dt><dd>A\*<\/dd>/);
  assert.doesNotMatch(card, /물음/, '흔적이 없으면 물음·답은 없습니다');
});

test('the hover card stands beside the aircraft, never on top of it', () => {
  const ui = scene();
  const size = {w:300, h:190};
  const wide = {w:1500, h:940, reserveRight:302};
  assert.equal(ui.run('placeTrace', {x:400, y:300}, size, wide).left, 456, '기체 오른쪽으로 비켜 섭니다');
  assert.equal(ui.run('placeTrace', {x:1100, y:300}, size, wide).left, 744, '오른쪽이 좁으면 왼쪽으로');
  for (const [anchor, view] of [[{x:400, y:300}, wide], [{x:1100, y:300}, wide],
                                [{x:700, y:60}, {w:760, h:400, reserveRight:0}]]){
    const at = ui.run('placeTrace', anchor, size, view);
    const covers = anchor.x >= at.left && anchor.x <= at.left + size.w
                && anchor.y >= at.top && anchor.y <= at.top + size.h;
    assert.ok(!covers, `기체를 가립니다 ${JSON.stringify(at)}`);
    assert.ok(at.left >= 12 && at.top >= 12, JSON.stringify(at));
  }
});

// ── 경로 선택. 후보는 코드가 그리고 모델은 고르기만 합니다. ───────────────────────
test('a route the model chose says who chose it and why, on the corridor and in the ledger line', () => {
  const ui = scene();
  const choice = {candidates:[
      {id:'a', label:'low route', legs_count:4, length_m:2310, max_alt_m:75, min_alt_m:60, reason_tags:['low']},
      {id:'b', label:'high route', legs_count:3, length_m:2100, max_alt_m:120, min_alt_m:110, reason_tags:['fast']}],
    chosen:'a', reason:'weather hold expected', model:'nemotron-3-nano:4b', path:'tools'};
  const entry = approval('rc1', {proposal:{asset_id:'drone-01', action:'fly_route', author:'nemotron-3-nano:4b',
    params:{legs:[start, ...route], drafter:'choice:nemotron-3-nano:4b', route_choice:choice}},
    decision:{verdict:'auto', reason:'한도 안', code:'within_limits'}});
  ui.run('renderSnapshot', snapshot(), {ledger:[entry], agents:AGENTS, locks:{}});
  const feed = ui.element('feed').innerHTML;
  assert.match(feed, /delivery route · model choice/);
  assert.match(feed, /Nemotron Nano 4B chose the low route — weather hold expected/);
  ui.time(GROW_MS + CHECK_MS + 10); ui.run('draw');
  assert.equal(ui.source('stage-label').features[0].properties.label,
    'APPROVED · Nemotron Nano 4B chose the low route — weather hold expected');
  const card = ui.run('assetCard', 'guarded',
    {id:'drone-01', state:'ready', battery:80, alt_m:0, route:[], delivered:0});
  assert.match(card, /<li class="chosen">low route · 4 legs · 2\.3 km · 60–75 m<\/li>/);
  assert.match(card, /<li class="">high route · 3 legs · 2\.1 km · 110–120 m<\/li>/);
  // 후보가 없는 신청서는 예전 그대로입니다.
  const plain = scene();
  plain.run('renderSnapshot', snapshot(), {ledger:[approval('p1', {decision:{verdict:'auto', reason:'한도 안',
    code:'within_limits'}})], locks:{}});
  plain.time(GROW_MS + CHECK_MS + 10); plain.run('draw');
  assert.equal(plain.source('stage-label').features[0].properties.label, 'APPROVED · within limits');
});

// ── 관제탑과 신호선. 파란 기체에는 선이 있고 직결에는 없습니다. ────────────────────
test('a guarded aircraft has a dotted line to the tower, a direct one has none, a lost link is grey and broken', () => {
  const ui = scene({localStorage:{getItem:key => key === 'skynet-show' ? 'both' : null, setItem(){}}});
  const snap = snapshot(2, 1, route, {lon:-73.97, lat:40.705, alt_m:110, state:'delivering'});
  snap.worlds.direct.assets = {'drone-09':{...snap.worlds.guarded.assets['drone-01'], id:'drone-09'}};
  ui.run('renderSnapshot', snap, {ledger:[], notices:[]});
  ui.time(700); ui.run('draw');
  const lines = ui.source('signal-line').features;
  assert.ok(lines.length > 3, '점선은 토막 여럿');
  assert.ok(lines.every(f => f.properties.asset === 'drone-01' && f.properties.kind === 'line'),
            '직결 기체(주황)에는 선이 없습니다');
  assert.ok(lines.every(f => f.properties.height > f.properties.base));
  const tower = ui.source('tower').features;
  assert.ok(tower.length >= 1 && tower.some(f => f.properties.height >= 250), '탑은 창고 위 250 m');
  ui.run('renderSnapshot', snapshot(3, 1, route, {lon:-73.97, lat:40.706, alt_m:110, state:'delivering'}),
    {ledger:[], notices:[], links:{'drone-01':{status:'lost', since_tick:400, last_seen_tick:399}}});
  ui.time(1000); ui.run('draw');
  const broken = ui.source('signal-line').features;
  assert.ok(broken.length && broken.every(f => f.properties.kind === 'lost'), '끊긴 링크는 회색 선');
  assert.ok(broken.length < lines.length, '가운데가 비어 토막이 줄어듭니다');
  assert.match(ui.element('legend').innerHTML, /SKY-NET TOWER[\s\S]*lost link/);
  assert.match(ui.element('legend').innerHTML, /symbolic position/);
});

test('a filing travels up the line and the verdict comes back down, a recall comes down red', () => {
  const ui = scene();
  ui.run('renderSnapshot', snapshot(), {ledger:[denial('sig-1')], notices:[]});
  ui.time(600); ui.run('draw');
  const filing = ui.source('signal-dot').features;
  assert.equal(filing.length, 1, '신청 하나 = 올라가는 점 하나');
  assert.equal(filing[0].properties.colour, '#ffd23f');
  assert.equal(filing[0].properties.kind, 'dot');
  ui.time(GROW_MS + CHECK_MS - 100); ui.run('draw');
  const verdict = ui.source('signal-dot').features;
  assert.equal(verdict.length, 1, '판정은 내려오는 점 하나');
  assert.equal(verdict[0].properties.colour, '#ff3b30', '거절은 빨강');
  // 회수는 관제가 내려보내는 것입니다.
  const recall = {id:'rc9', at:Date.now() / 1000, outcome:'done',
    proposal:{asset_id:'drone-01', action:'divert_ground', author:'runtime', params:{volume:'nofly-1'}},
    decision:{verdict:'auto', reason:'회수', code:'recalled', policy_hit:'nofly-1',
              detail:{resource:'nofly-1', policy:'nofly-1'}}};
  ui.run('renderSnapshot', snapshot(), {ledger:[recall, denial('sig-1')], notices:[]});
  ui.time(GROW_MS + CHECK_MS + 400); ui.run('draw');
  const down = ui.source('signal-dot').features;
  assert.ok(down.some(f => f.properties.colour === '#ff3b30'), '회수는 빨갛게 내려옵니다');
});

// ── 시연 연출 (?demo=1) ──────────────────────────────────────────────────────────
function demoScene(overrides = {}) {
  return scene({location:{hostname:'localhost', search:'?demo=1'}, URLSearchParams, ...overrides});
}
function demoShot(runtime, snap = snapshot()) {
  const ui = demoScene();
  ui.run('renderSnapshot', snap, {ledger:[], notices:[], ...runtime});
  ui.time(50); ui.run('draw');
  return {ui, fly:ui.calls.filter(c => c[0] === 'flyTo').at(-1), caption:ui.element('caption-text').textContent};
}

test('the demo director only runs with ?demo=1', () => {
  const ui = scene();
  ui.run('renderSnapshot', snapshot(), {ledger:[denial('off-1')], notices:[]});
  ui.time(200); ui.run('draw');
  assert.equal(ui.calls.filter(c => c[0] === 'flyTo').length, 0,
               '평소 화면에서 카메라는 저 혼자 움직이지 않습니다');
  assert.equal(ui.element('caption').hidden, true);
});

test('a refusal flies the camera to the aircraft and the building, with a caption built from ledger values', () => {
  const ui = demoScene();
  const blocked = {lat:40.7075, lon:-73.9695};
  const refused = denial('cap-1', {proposal:{asset_id:'drone-01', action:'fly_route',
    params:{legs:[start, ...route], drafter:'straight', blocked_kind:'forbidden', blocked_volume:'bldg-t1',
            blocked_leg:1, blocked_ceiling_m:114, blocked_at:blocked}},
    decision:{verdict:'denied', reason:'건물', code:'airspace'}});
  const snap = snapshot();
  snap.worlds.guarded.assets['drone-01'].job = 'Harlem';
  ui.run('renderSnapshot', snap, {ledger:[refused], notices:[]});
  ui.time(100); ui.run('draw');
  const fly = ui.calls.filter(c => c[0] === 'flyTo').at(-1);
  assert.ok(fly, '카메라가 갑니다');
  assert.ok(Math.abs(fly[1].center[1] - (start.lat + blocked.lat) / 2) < 1e-6, '기체와 막은 건물 사이');
  assert.ok(fly[1].zoom > 14 && fly[1].zoom <= 16.8, `줌 ${fly[1].zoom}`);
  assert.equal(ui.element('caption-text').textContent,
    'drone-01 filed a straight line to Harlem — it clips a 114 m building. Refused.');
  assert.equal(ui.element('caption').hidden, false);
  // 읽을 시간이 지나면 넓은 화면으로 돌아오고 자막이 내려갑니다.
  ui.time(7300); ui.run('draw');
  assert.equal(ui.calls.at(-1)[1].zoom, 14.5, '넓은 화면으로');
  assert.equal(ui.element('caption-text').textContent, '');
});

test('scheduled scenes take the camera to the zone, the warehouse, the fire circle and the dark aircraft', () => {
  const ring = [[40.81, -73.94], [40.81, -73.93], [40.82, -73.93]];
  const notam = demoShot({notices:[{id:'n1', name:'Harlem TFR', applied:true, held:false, source:'grammar',
    from_tick:525, until_tick:900, polygon:ring}]});
  assert.ok(Math.abs(notam.fly[1].center[1] - 40.815) < 1e-9, '구역 한가운데로(양 끝의 가운데)');
  assert.match(notam.caption, /^NOTAM · Harlem TFR, tick 525–900 — read by the rule grammar\./);

  const weather = demoShot({weather:{hold:{id:'wx1', reason:'WEATHER HOLD · gusts 14 m/s > 12',
    until_tick:2700, since_tick:2200, source:'grammar'}, held:[]}});
  assert.ok(Math.abs(weather.fly[1].center[1] - start.lat) < 1e-6, '창고가 화면 가운데');
  assert.equal(weather.caption, 'WEATHER HOLD · gusts 14 m/s > 12 — takeoffs held until tick 2700. '
    + 'Aircraft already in the air continue to land.');

  const fire = demoShot({incidents:[{id:'f1', name:'FIRE · 4705 Center Boulevard', kind:'fire',
    centre:[40.745618, -73.956797], radius_m:150, until_tick:3600, applied:true, held:false}]});
  assert.deepEqual(fire.fly[1].center, [-73.956797, 40.745618]);
  assert.match(fire.caption, /^FIRE · 4705 Center Boulevard — 150 m keep-out until tick 3600\./);

  const lost = demoShot({links:{'drone-01':{status:'lost', since_tick:3800, last_seen_tick:3799}}});
  assert.deepEqual(lost.fly[1].center, [start.lon, start.lat]);
  assert.equal(lost.fly[1].zoom, 16.2);
  assert.equal(lost.caption, 'drone-01 lost its link at tick 3800. The runtime keeps its filed space '
    + 'reserved — nobody else may enter it.');

  const advised = demoShot({advisories:[advisory({asset:'drone-01', ledger_id:'adv-9'})]});
  assert.deepEqual(advised.fly[1].center, [start.lon, start.lat]);
  assert.equal(advised.caption, 'After 3 refusals in a row the tower suggests to drone-01: '
    + 'hold on the ground until tick 700. Information only — the operator decides.');
});

test('a drag pauses the director for 20 s, and the scene it missed plays when the pause ends', () => {
  const ui = demoScene();
  ui.run('userMoved', {originalEvent:{}});
  ui.run('renderSnapshot', snapshot(), {ledger:[], notices:[], weather:{hold:{id:'wx1',
    reason:'WEATHER HOLD · gusts 14 m/s > 12', until_tick:2700, since_tick:2200, source:'grammar'}, held:[]}});
  ui.time(1000); ui.run('draw');
  assert.equal(ui.calls.filter(c => c[0] === 'flyTo').length, 0, '사람이 지도를 잡고 있는 동안은 가만히');
  assert.match(ui.element('caption-meta').textContent, /director paused/);
  ui.time(21000); ui.run('draw');
  assert.equal(ui.calls.filter(c => c[0] === 'flyTo').length, 1, '쉬고 나면 그 장면부터');
  assert.match(ui.element('caption-text').textContent, /^WEATHER HOLD/);
});

// ── PX4 SITL 거울 ────────────────────────────────────────────────────────────────
test('a PX4 mirror draws a ghost with its mode and mission step; without the field nothing is drawn', () => {
  const ui = scene();
  const pilot = {lat:40.705, lon:-73.968, alt_m:60, armed:true, mode:'AUTO.MISSION', mission_seq:3,
                 endpoint:'udp://127.0.0.1:14540', last_heartbeat_s:0.4};
  ui.run('renderSnapshot', snapshot(), {ledger:[], notices:[], autopilots:{'drone-01':pilot}});
  ui.time(100); ui.run('draw');
  assert.equal(ui.source('px4').features.length, 5, '몸통 하나와 로터 넷');
  assert.ok(ui.source('px4').features.every(f => f.properties.height > f.properties.base));
  assert.equal(ui.source('px4-label').features[0].properties.label,
               'PX4 SITL · drone-01\nAUTO.MISSION · step 3 · armed');
  assert.match(ui.element('legend').innerHTML, /PX4 SITL mirror/);
  ui.run('renderSnapshot', snapshot(), {ledger:[], notices:[]});
  ui.time(200); ui.run('draw');
  assert.equal(ui.source('px4').features.length, 0, '없는 필드는 아무것도 그리지 않습니다');
  assert.equal(ui.source('px4-label').features.length, 0);
});

// 화면 말은 두 언어가 같은 자리(%s)를 가져야 합니다 — 자막·카드는 값을 순서대로 끼웁니다.
test('every screen text key exists in both languages with the same number of placeholders', () => {
  const html = readFileSync(new URL('../frontend/map.html', import.meta.url), 'utf8');
  const TEXT = vm.runInNewContext(`(${html.match(/const TEXT = (\{[\s\S]*?\n\});/)[1]})`);
  const holes = text => (String(text).match(/%s/g) || []).length;
  const missing = Object.keys(TEXT.en).filter(key => !(key in TEXT.ko));
  assert.deepEqual(missing, [], `한국어가 없는 말: ${missing.join(', ')}`);
  const uneven = Object.keys(TEXT.en).filter(key => holes(TEXT.en[key]) !== holes(TEXT.ko[key]));
  assert.deepEqual(uneven, [], `자리 수가 다른 말: ${uneven.join(', ')}`);
});

test('captions speak Korean with the language toggle', () => {
  const ui = demoScene({localStorage:{getItem:key => key === 'skynet-lang' ? 'ko' : null, setItem(){}}});
  ui.run('renderSnapshot', snapshot(), {ledger:[], notices:[],
    links:{'drone-01':{status:'lost', since_tick:3800, last_seen_tick:3799}}});
  ui.time(50); ui.run('draw');
  assert.equal(ui.element('caption-text').textContent,
    'drone-01 링크 두절 (틱 3800). 런타임은 그 기체가 낸 공간을 그대로 잡아 둡니다 — 아무도 못 들어갑니다.');
});

test('a recall caption names the rule that pulled the route, not the aircraft', () => {
  const ui = demoScene();
  // 확인만 되고 아직 안 걸린 공지 — 장면은 없지만 이름은 공지 책에서 찾습니다.
  const tfr = {id:'nofly-1', name:'Harlem TFR', applied:false, held:false, source:'human', from_tick:1350,
               until_tick:2100, polygon:[[40.81, -73.94], [40.81, -73.93], [40.82, -73.93]]};
  const recall = {id:'rc-live', at:Date.now() / 1000, outcome:'done',
    proposal:{asset_id:'drone-01', action:'divert_ground', author:'runtime', params:{volume:'nofly-1'}},
    decision:{verdict:'auto', reason:'회수', code:'recalled', policy_hit:'nofly-1',
              detail:{resource:'drone-01', policy:'nofly-1'}}};
  ui.run('renderSnapshot', snapshot(), {ledger:[recall], notices:[tfr]});
  ui.time(50); ui.run('draw');
  assert.equal(ui.element('caption-text').textContent,
    'A rule arrived — drone-01’s approved route was pulled back (Harlem TFR). It holds until a new one is approved.'
      .replace('’', "'"));
});

// 녹화본(손으로 쓴 장면·전에 받아 둔 답)은 브리핑 카드에만이 아니라 지도·"무엇이 막았나"·자막에서도
// 녹화본이라고 말합니다. 빠지면 가짜 공지가 오늘 검색한 공지와 같은 말로 화면에 섭니다.
test('recorded briefing material says so on the map, in what-blocked-it and in the demo caption', () => {
  const recordedNotices = briefingNotices().map(n => ({...n, citation:{...n.citation, recorded:true}}));
  const recorded = briefing({source:'recorded'});
  recorded.items = recorded.items.map(item => ({...item, recorded:true}));
  const ui = scene();
  const snap = snapshot();
  snap.worlds.guarded.landing_areas = [{id:'la-morningside', name:'Morningside Park', lat:40.805, lon:-73.959}];
  ui.run('renderSnapshot', snap, {ledger:[], notices:recordedNotices, briefing:recorded});
  assert.equal(ui.source('brief-crane-label').features[0].properties.label, 'CRANE 95 m · nyc.gov · recorded');
  assert.equal(ui.source('landing').features[0].properties.tag, 'CLOSED · nycgovparks.org · recorded');
  const blocked = {blocked_volume:'brief-c1', blocked_kind:'forbidden', blocked_ceiling_m:95};
  assert.equal(ui.run('blockedLabel', blocked), 'CRANE 95 m (nyc.gov · recorded)');
  assert.equal(ui.run('blockPhrase', blocked), 'it clips a 95 m crane (nyc.gov · recorded)');
  assert.match(ui.run('briefingShot', recordedNotices[0]).caption(), /, from nyc\.gov · recorded\. It applies now/);
  // 항목 목록에서 빠져도(공지 책에만 남아도) 녹화본은 녹화본입니다.
  ui.run('renderSnapshot', snap, {ledger:[], notices:recordedNotices, briefing:{...recorded, items:[]}});
  assert.equal(ui.run('blockedLabel', blocked), 'CRANE 95 m (nyc.gov · recorded)');
  // 라이브 출처는 도메인만.
  ui.run('renderSnapshot', snap, {ledger:[], notices:briefingNotices(), briefing:briefing()});
  assert.equal(ui.source('brief-crane-label').features[0].properties.label, 'CRANE 95 m · nyc.gov');
  assert.equal(ui.run('blockedLabel', blocked), 'CRANE 95 m (nyc.gov)');
});

test('the approval screen says when a held notice comes from a recorded briefing, not a live search', async () => {
  const world = {assets:{}, pads:{}, scoreboard:{spend_usd:0, human_approvals:0}, fleet_limit:450, events:[]};
  const compare = {tick:10, recall_tick:null, worlds:{guarded:world, direct:structuredClone(world)}};
  const card = isRecorded => ({id:`n-${isRecorded}`, asset_id:'airspace', action:'publish_notice', cost_usd:0,
    blast_radius:'none', rationale:'EVENT · Union Square — march',
    params:{notice:{id:'brief-rec-1', citation:{domain:'eastvillage-bulletin.example', recorded:isRecorded}}}});
  const state = {llm:{enabled:false, models:{}}, ledger:[], incidents:[], weather:{}, links:{},
                 awaiting_human:[card(true)]};
  const ui = await tower(state, compare);
  assert.match(ui.element('inbox').innerHTML, /공역 공지[^<]*· <span class="rec">녹화본 · 지금 검색한 것 아님<\/span>/);
  const live = await tower({...state, awaiting_human:[card(false)]}, compare);
  assert.doesNotMatch(live.element('inbox').innerHTML, /녹화본/);
});
