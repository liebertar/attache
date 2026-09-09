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
    if (!elements.has(id)) elements.set(id,{style:{},hidden:true,textContent:'',innerHTML:''});
    return elements.get(id);
  };
  const map = {on(){}, getSource(id){
    if (!sources.has(id)) sources.set(id,{setData(data){this.data=data;}});
    return sources.get(id);
  }};
  const context = vm.createContext({...geometry, console, Date, Map, Set, Math,
    location:{hostname:'localhost'}, performance:{now:()=>now},
    requestAnimationFrame(){}, document:{getElementById:element,querySelector:element},
    maplibregl:{Map:function(){return map;}}});
  const html = readFileSync(new URL('../ui/map.html',import.meta.url),'utf8');
  const code = html.match(/<script type="module">([\s\S]*?)<\/script>/)[1]
    .replace(/^import .*?;\n/m,'');
  vm.runInContext(code,context);
  const run = (name,...args) => context[name](...args);
  return {run,element,source:id=>sources.get(id)?.data, time:value=>{now=value;}};
}

function snapshot(tick=1, round=1, remaining=route, position=start) {
  const world = {assets:{'drone-01':{id:'drone-01',battery:80,...position,route:remaining}},
    depot_coords:start,scoreboard:{spend_usd:0},fleet_limit:450};
  return {tick,round,recall_tick:null,worlds:{guarded:world,direct:structuredClone(world)}};
}
function denial(id='denied-1', extra={}) {
  return {id,at:Date.now()/1000,outcome:'denied',
    proposal:{asset_id:'drone-01',action:'fly_route',params:{legs:[start,...route]}},
    decision:{verdict:'denied',reason:'금지 공역 <test>'},...extra};
}

test('warehouse, curved green path and drone update from snapshots and clear on reset', () => {
  const ui = scene();
  ui.run('renderSnapshot',snapshot(),null);
  ui.run('draw');
  assert.equal(ui.source('depot').features[0].geometry.coordinates[0],start.lon);
  const original = JSON.stringify(ui.source('approved'));
  ui.time(500);
  ui.run('renderSnapshot',snapshot(2,1,route.slice(1),{lon:-73.969,lat:40.71}),null);
  ui.time(750); ui.run('draw');
  assert.equal(JSON.stringify(ui.source('approved')),original);
  assert.ok(ui.source('guarded').features[0].geometry.coordinates[1] > 40.70);
  ui.run('renderSnapshot',snapshot(1,2,[]),null); ui.run('draw');
  assert.equal(ui.source('approved').features.length,0);
  assert.equal(ui.source('guarded-trail').features.length,0);
});

test('final denials alert once, retain submitted straight legs, and expire without polling', () => {
  const ui = scene(), e = denial();
  ui.run('renderDenials',{ledger:[{...e,outcome:'pending'}]},0);
  assert.equal(ui.element('denial').hidden,true);
  ui.run('renderDenials',{ledger:[e]},100);
  assert.equal(ui.element('denial').hidden,false);
  assert.ok(ui.element('denial-detail').textContent.includes('<test>'));
  assert.equal(ui.source('rejected').features[0].geometry.coordinates.length,4);
  ui.run('renderDenials',{ledger:[e]},7000);
  ui.run('expireDenials',8101);
  assert.equal(ui.element('denial').hidden,true);
  assert.equal(ui.source('rejected').features.length,0);
});

test('old ledger entries do not replay alerts; non-flight denials do not invent paths', () => {
  const ui = scene();
  ui.run('renderDenials',{ledger:[denial('old',{at:Date.now()/1000-60})]},0);
  assert.equal(ui.element('denial').hidden,true);
  ui.run('renderDenials',{ledger:[denial('charge',{
    proposal:{asset_id:'drone-01',action:'fast_charge',params:{}},
  })]},100);
  assert.equal(ui.element('denial').hidden,false);
  assert.equal(ui.source('rejected').features.length,0);
  assert.match(ui.element('#denial strong').textContent,/요청 거절/);
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
