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
  assert.ok(curve.coordinates.some(p => p[0] < -73.97)); // Smoothed corner.
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
function scene() {
  let now = 0;
  const elements = new Map(), sources = new Map();
  const element = id => {
    if (!elements.has(id)) elements.set(id,
      {style:{},hidden:true,textContent:'',innerHTML:'',addEventListener(){}});
    return elements.get(id);
  };
  const map = {on(){}, addControl(){}, setPaintProperty(){}, getLayer(){return {};},
    getSource(id){
    if (!sources.has(id)) sources.set(id,{setData(data){this.data=data;}});
    return sources.get(id);
  }};
  const context = vm.createContext({...geometry, console, Date, Map, Set, Math,
    location:{hostname:'localhost'}, performance:{now:()=>now},
    requestAnimationFrame(){}, document:{getElementById:element,querySelector:element},
    maplibregl:{Map:function(){return map;}, NavigationControl:function(){},
      Popup:function(){return {setLngLat(){return this;}, setHTML(){return this;},
        addTo(){return this;}};}}});
  const html = readFileSync(new URL('../ui/map.html',import.meta.url),'utf8');
  const code = html.match(/<script type="module">([\s\S]*?)<\/script>/)[1]
    .replace(/^import .*?;\n/m,'');
  vm.runInContext(code,context);
  const run = (name,...args) => context[name](...args);
  return {run,element,source:id=>sources.get(id)?.data, time:value=>{now=value;}};
}

function snapshot(tick=1, round=1, remaining=route, position=start) {
  const world = {assets:{'drone-01':{id:'drone-01',battery:80,alt_m:90,...position,
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
  ui.run('renderSnapshot',snapshot(1,1,[],{lon:-73.97,lat:40.70,state:'dropping'}),null);
  ui.run('draw');
  assert.equal(ui.source('guarded').features[0].properties.work,'unloading');
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
  const first = ui.source('approved').features[0].geometry.coordinates;
  ui.time(500);
  ui.run('renderSnapshot',snapshot(2,1,route.slice(1),{lon:-73.969,lat:40.71}),null);
  ui.time(750); ui.run('draw');
  const later = ui.source('approved').features[0].geometry.coordinates;
  // 곡선은 다시 만들지 않습니다(경유점이 빠져도 같은 곡선). 다만 지나온 구간은 지웁니다.
  assert.deepEqual(later.at(-1), first.at(-1), '목적지는 그대로여야 합니다');
  assert.ok(later.length < first.length, '지나온 구간이 안 지워지고 있습니다');
  assert.ok(ui.source('guarded').features[0].geometry.coordinates[1] > 40.70);
  ui.run('renderSnapshot',snapshot(1,2,[]),null); ui.run('draw');
  assert.equal(ui.source('approved').features.length,0);
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
  assert.equal(ui.source('rejected').features.length, 0, '판정 전에는 붉지 않습니다');
  const partial = ui.source('pending').features[0].geometry.coordinates;
  ui.time(100 + GROW_MS + CHECK_MS + 50); ui.run('draw');
  const full = ui.source('rejected').features[0].geometry.coordinates;
  assert.match(ui.source('stage-label').features[0].properties.label,/^REJECTED/);
  assert.ok(full.length > partial.length);
  assert.deepEqual(full.at(-1),[route.at(-1).lon,route.at(-1).lat]);
  ui.run('renderDenials',{ledger:[e]},7000);
  ui.time(100 + geometry.stageLife('rejected') + 50); ui.run('draw');
  assert.equal(ui.source('rejected').features.length,0);
  ui.time(8101); ui.run('draw');   // 알림은 8초
  assert.equal(ui.element('denial').hidden,true);
  assert.equal(ui.source('rejected').features.length,0);
});

test('an approved route redraws from the drone before the steady line takes over', () => {
  const ui = scene();
  ui.run('renderSnapshot',snapshot(),null);
  ui.run('renderDenials',{ledger:[approval()]},0);
  ui.time(100); ui.run('draw');
  const growing = ui.source('pending').features;
  assert.equal(growing.length,1);           // 판정 전이라 아직 초록이 아닙니다
  assert.equal(ui.source('approved').features.length,0);
  assert.equal(ui.source('stage-label').features[0].properties.label,'PLANNING…');
  ui.time(GROW_MS + CHECK_MS + 10); ui.run('draw');
  assert.match(ui.source('stage-label').features[0].properties.label,/^APPROVED · /);
  ui.time(geometry.stageLife('approved') + 50); ui.run('draw');
  const settled = ui.source('approved').features[0].geometry.coordinates;
  assert.ok(growing[0].geometry.coordinates.length < settled.length);
  assert.deepEqual(settled.at(-1),[route.at(-1).lon,route.at(-1).lat]);
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
  assert.equal(ui.source('rejected').features.length, 1);
  assert.equal(ui.source('approved').features.length, 0);
});

test('a queued decision has not travelled anywhere yet, so nothing is drawn', () => {
  const ui = scene();
  ui.run('renderDenials',{ledger:[approval('q',{decision:{verdict:'queued',reason:'대기'}})]},0);
  ui.time(100); ui.run('draw');
  assert.equal(ui.source('rejected').features.length,0);
  assert.equal(ui.source('approved').features.length,0);
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
  assert.equal(ui.source('rejected').features.length,0);
  assert.equal(ui.element('denial-what').textContent,'fast charge');
});

test('route completion clears approval after animation and round reset clears alerts', () => {
  const ui = scene();
  ui.run('renderSnapshot',snapshot(),null);
  ui.time(500);
  ui.run('renderSnapshot',snapshot(2,1,[],route.at(-1)),{ledger:[denial()]});
  ui.time(750); ui.run('draw');
  assert.equal(ui.source('approved').features.length,1);
  ui.time(1001); ui.run('draw');
  assert.equal(ui.source('approved').features.length,0);
  ui.run('renderSnapshot',snapshot(1,2,[]),null); ui.run('draw');
  assert.equal(ui.element('denial').hidden,true);
  assert.equal(ui.source('rejected').features.length,0);
});
