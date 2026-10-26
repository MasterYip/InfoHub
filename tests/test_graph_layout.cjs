// Run with: node --test tests/test_graph_layout.cjs
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../infohub/apps/web/index.html'), 'utf8');
const helpers = source.slice(source.indexOf('const GRAPH_RANGE'), source.indexOf('const OLD_GRAPH_CFG'))
  + source.slice(source.indexOf('function graphPhysicsStep'), source.indexOf('// A live, force-directed'));
const scope = {};
vm.createContext(scope);
vm.runInContext(helpers + '\nthis.defaults = GRAPH_DEF; this.ranges = GRAPH_RANGE;', scope);
const {graphControl: control, graphPhysicsStep: step, graphLabelLayout: labels, graphWrapTitle: wrap, defaults} = scope;
const node = (id, x, y, messages = 1) => ({id, n_messages: messages, title: `Topic ${id}`, seed: true, deg: 0, r: 5, p: {x, y, vx: 0, vy: 0}});

test('default controls match the requested 59 / 0 / 95 / 30 preset', () => {
  for (const [key,value] of Object.entries({repulsion:59,distance:0,gravity:95,damping:30})) {
    assert.equal(Math.round(control(key, defaults[key], true)), value);
  }
});

test('untouched previous defaults migrate while custom settings stay intact', () => {
  const prefs = source.slice(source.indexOf('const OLD_GRAPH_CFG'), source.indexOf('const EXPLORE_FILTERS'));
  for (const saved of [{}, {repulsion:5000,distance:110,gravity:0.0022,damping:0.14},
                       {repulsion:1000,distance:200,gravity:0.01,damping:0}]) {
    const target = {readPreference:()=>saved}; vm.createContext(target);
    vm.runInContext(helpers + prefs + '\nthis.config=GRAPH_CFG;',target);
    assert.deepEqual(JSON.parse(JSON.stringify(target.config)), saved.repulsion===1000?saved:JSON.parse(JSON.stringify(defaults)));
  }
});

test('controls are bounded, monotone, invertible and linear in idealized spacing', () => {
  for (const key of Object.keys(defaults)) {
    let prev = -1;
    assert.equal(control(key, 0), 0);
    for (let i = 0; i <= 100; i++) {
      const physical = control(key, i);
      assert.ok(physical > prev); prev = physical;
      assert.ok(Math.abs(control(key, physical, true) - i) < 1e-8);
    }
    assert.equal(control(key, -10), control(key, 0));
    assert.equal(control(key, 110), control(key, 100));
  }
  for (const key of ['gravity', 'repulsion']) {
    const radius = [0, 25, 50, 75, 100].map(t => Math.cbrt(key === 'gravity' ? defaults.repulsion / (control(key, t) + scope.ranges.gravity[1] / 124) : control(key, t) / defaults.gravity));
    for (let i = 2; i < radius.length; i++) assert.ok(Math.abs(radius[i] - 2 * radius[i-1] + radius[i-2]) < 1e-8);
  }
});

test('dense coincident starts stay finite and stop on the original settle timer', () => {
  const nodes = Array.from({length: 400}, (_, i) => node(i, 400, 300, 400-i));
  const edges = nodes.slice(1).map(b => ({a: nodes[0], b}));
  nodes[0].deg = 399; nodes.slice(1).forEach(n => n.deg = 1);
  let alpha = 1;
  for (let t = 0; t < 360; t++) {
    step(nodes, edges, {...defaults, gravity: 0.02, repulsion: 16000}, alpha, 400, 300, nodes[0]);
    alpha *= 0.985;
    for (const n of nodes) {
      assert.ok(Object.values(n.p).every(Number.isFinite));
      assert.ok(Math.abs(n.p.vx) <= 30 && Math.abs(n.p.vy) <= 30);
    }
  }
  assert.equal(nodes[0].p.x, 400); assert.equal(nodes[0].p.vx, 0);
  const frozen=JSON.stringify(nodes);
  step(nodes,edges,defaults,0.015,400,300);
  assert.equal(JSON.stringify(nodes),frozen);
});

test('increasing damping removes more momentum without changing force parameters', () => {
  const a = node(1, 0, 0), b = node(1, 0, 0); a.p.vx = b.p.vx = 10;
  step([a], [], {...defaults, damping: 0.15}, 1, 0, 0);
  step([b], [], {...defaults, damping: 0.8}, 1, 0, 0);
  assert.ok(b.p.vx < a.p.vx);
});

test('long English, CJK and unbroken titles wrap without truncation', () => {
  const measure = t => [...t].length * 6;
  for (const text of ['A long title about research and engineering', '\u4e2d\u6587'.repeat(30), 'a'.repeat(70)]) {
    const lines = wrap(text, measure, 90);
    assert.ok(lines.length > 1);
    assert.equal(lines.join('').replace(/\s/g,''), text.replace(/\s/g,''));
    assert.ok(lines.every(l => measure(l) <= 90));
  }
});

test('message count wins conflicts; zoom reveals more labels without overlap or clipping', () => {
  const measure = t => t.length * 6;
  const nodes = Array.from({length: 60}, (_, i) => node(i, 180 + i % 10 * 64, 150 + Math.floor(i/10) * 70, 60-i));
  const occupied = [{x: 0, y: 0, w: 200, h: 30}];
  let last = 0;
  for (const s of [0.5, 1, 1.5]) {
    const placed = labels(nodes, {s, tx: 500 - 500*s, ty: 350 - 350*s}, 1000, 700, measure, occupied);
    assert.ok(placed.length >= last); last = placed.length;
    for (let i=0; i<placed.length; i++) {
      const a = placed[i];
      assert.ok(a.x >= 0 && a.y >= 0 && a.x+a.w <= 1000 && a.y+a.h <= 700);
      for (const b of [...placed.slice(i+1), ...occupied]) assert.ok(a.x+a.w <= b.x || b.x+b.w <= a.x || a.y+a.h <= b.y || b.y+b.h <= a.y);
    }
  }
  const sameSpot = Array.from({length: 8}, (_, i) => node(i, 400, 300, i));
  const placed = labels(sameSpot, {s: 1, tx: 0, ty: 0}, 800, 600, measure);
  assert.equal(placed[0].node.id, 7);
  assert.ok(placed.length < sameSpot.length);
});

test('circles do not displace labels from their preferred position', () => {
  const main = node(1, 300, 200, 100), circle = node(2, 300, 225, 1);
  circle.title = ''; circle.r = 20;
  const placed = labels([main, circle], {s: 1, tx: 0, ty: 0}, 800, 600, t => t.length * 6);
  assert.equal(placed.length, 1);
  assert.equal(placed[0].y, 211);
  assert.equal(placed[0].backdrop, undefined);
});

test('zero damping preserves momentum while active and zero repulsion has no hidden collision force', () => {
  const a=node(1,100,100), b=node(2,101,100), cfg={...defaults, damping:0, repulsion:0, gravity:0};
  step([a,b],[],cfg,1,400,300);
  assert.equal(a.p.vx,0); assert.equal(b.p.vx,0);
  a.p.vx=3; a.p.vy=4;
  for(let i=0;i<100;i++) step([a],[],cfg,1,400,300);
  assert.equal(a.p.vx,3);assert.equal(a.p.vy,4);assert.equal(a.p.x,400);assert.equal(a.p.y,500);
  const left=node(3,100,100), right=node(4,110,100);
  step([left,right],[{a:left,b:right}],{...cfg,distance:0},1,0,0);
  assert.ok(left.p.vx>0 && right.p.vx<0,'zero link length has no minimum-radius rest length');
});

test('original spring response still follows a dragged hub with old and new damping presets', () => {
  for (const preset of [defaults,{distance:110,damping:0.14}]) {
    const parent=node(1,0,0), child=node(2,200,0);parent.deg=500;child.deg=1;
    step([parent,child],[{a:parent,b:child}],{...defaults,...preset,gravity:0,repulsion:0},0.3,0,0,parent);
    assert.ok(Math.abs(child.p.vx + (200-preset.distance)*0.02*(1-preset.damping))<1e-12);
    assert.equal(parent.p.x,0);
  }
});

test('hover reveals small adjacent topics with faded titles while preserving text collision rules', () => {
  const nodes=Array.from({length:8},(_,i)=>node(i,100+i*150,200,i*100));
  const focus=nodes[0], neighbors=new Set([nodes[1],nodes[2]]);
  const placed=labels(nodes,{s:0.5,tx:0,ty:0},800,600,t=>t.length*6,[],focus,neighbors);
  assert.equal(placed[0].node,focus);assert.equal(placed[0].opacity,1);
  for(const n of neighbors) assert.equal(placed.find(l=>l.node===n).opacity,0.5);
  assert.ok(placed.some(l=>l.opacity===0.25));
  for(let i=0;i<placed.length;i++) for(const b of placed.slice(i+1)) {
    const a=placed[i];assert.ok(a.x+a.w<=b.x||b.x+b.w<=a.x||a.y+a.h<=b.y||b.y+b.h<=a.y);
  }
});
