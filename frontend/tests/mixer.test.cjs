const { test } = require('node:test');
const assert = require('node:assert/strict');
const { createQueue } = require('../mixer.js');
const { threeBandLevels } = require('../mixer.js');

test('multiband telemetry groups all frequencies into the three mixer meters', () => {
  assert.deepEqual(threeBandLevels([.2, .2, .1, .1, .2, .2]), [.4, .2, .4]);
  assert.deepEqual(threeBandLevels(), [0, 0, 0]);
});

test('rapid edits serialize requests and finish with the latest mix', async () => {
  let release;
  const calls = [];
  let stored = { sensitivity: 1, brightness_max: 80 };
  const queue = createQueue(async patch => {
    calls.push(patch);
    if (calls.length === 1) await new Promise(resolve => { release = resolve; });
    stored = { ...stored, ...patch };
    return { saved: true, settings: stored };
  }, stored);
  const first = queue.update({ sensitivity: 1.4 });
  const second = queue.update({ sensitivity: 2.1 });
  const last = queue.update({ sensitivity: 1.8, brightness_max: 60 });
  assert.equal(calls.length, 1);
  assert.equal(queue.snapshot().sensitivity, 1.8);
  release();
  await Promise.all([first, second, last]);
  assert.deepEqual(calls, [{ sensitivity: 1.4 }, { sensitivity: 1.8, brightness_max: 60 }]);
  assert.deepEqual(stored, { sensitivity: 1.8, brightness_max: 60 });
  assert.deepEqual(queue.snapshot(), stored);
});

test('a failed save stays visibly unsaved and can be retried', async () => {
  let fail = true;
  const states = [];
  const queue = createQueue(async patch => {
    if (fail) throw new Error('offline');
    return { saved: true, settings: patch };
  });
  queue.subscribe(state => states.push(state.phase));
  await assert.rejects(queue.update({ noise_gate_floor: 0.005 }), /offline/);
  assert.equal(states.at(-1), 'error');
  fail = false;
  await queue.flush();
  assert.equal(states.at(-1), 'saved');
  assert.equal(queue.snapshot().noise_gate_floor, 0.005);
});

test('re-entering a clean mixer takes the latest server settings', async () => {
  const queue = createQueue(async patch => ({ saved: true, settings: patch }), { sensitivity: 1 });
  queue.hydrate({ sensitivity: 2 });
  assert.equal(queue.snapshot().sensitivity, 2);
});

test('a poll started before a successful edit cannot restore stale settings', async () => {
  const queue = createQueue(async patch => ({ saved: true, settings: patch }), { sensitivity: 1 });
  const revision = queue.revision();
  await queue.update({ sensitivity: 2 });
  queue.hydrate({ sensitivity: 1 }, revision);
  assert.equal(queue.snapshot().sensitivity, 2);
});
