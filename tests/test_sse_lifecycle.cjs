// Run with: node --test tests/test_sse_lifecycle.cjs
const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { join } = require("node:path");
const { test } = require("node:test");
const { runInNewContext } = require("node:vm");

const script = readFileSync(join(__dirname, "../src/web/static/app.js"), "utf8");

function setup(elements = {}) {
  const listeners = {};
  const timers = new Map();
  const streams = [];
  let timerId = 0;
  let reloads = 0;
  let refreshes = 0;
  runInNewContext(script, {
    EventSource: class {
      constructor(url) {
        assert.equal(url, "/actions/events");
        this.closed = false;
        streams.push(this);
      }
      close() { this.closed = true; }
    },
    window: {
      addEventListener: (name, fn) => { listeners[name] = fn; },
      location: { reload: () => { reloads++; } },
      htmx: { ajax: () => { refreshes++; } },
    },
    document: {
      getElementById: id => elements[id] || (id === "task-panel" ? {} : null),
      querySelector: () => null,
      querySelectorAll: () => [],
      addEventListener() {},
      body: { addEventListener() {} },
    },
    localStorage: { getItem: () => null },
    setTimeout(fn) { timers.set(++timerId, fn); return timerId; },
    clearTimeout(id) { timers.delete(id); },
  });
  return {
    streams, timers,
    emit: (name, persisted = false) => listeners[name]({ persisted }),
    flush() {
      const pending = [...timers.values()];
      timers.clear();
      pending.forEach(fn => fn());
    },
    get reloads() { return reloads; },
    get refreshes() { return refreshes; },
  };
}

test("navigation releases streams; back/forward restores exactly one stream", () => {
  const app = setup();
  app.emit("pageshow");
  assert.equal(app.streams.length, 1);
  for (let i = 0; i < 12; i++) {
    const previous = app.streams.at(-1);
    app.emit("pagehide", true);
    assert.equal(previous.closed, true);
    assert.equal(previous.onmessage, null);
    assert.equal(previous.onerror, null);
    assert.equal(app.streams.filter(s => !s.closed).length, 0);
    app.emit("pageshow", true);
    assert.equal(app.streams.filter(s => !s.closed).length, 1);
  }
  assert.equal(app.refreshes, 12);
});

test("leaving during a connection error cancels the retry until restored", () => {
  const app = setup();
  app.streams[0].onerror();
  assert.equal(app.streams[0].closed, true);
  assert.equal(app.timers.size, 1);
  app.emit("pagehide", true);
  assert.equal(app.timers.size, 0);
  app.flush();
  assert.equal(app.streams.length, 1);
  app.emit("pageshow", true);
  assert.equal(app.streams.length, 2);
  app.streams[1].onerror();
  app.flush();
  assert.equal(app.streams.length, 3);
  assert.equal(app.streams.filter(s => !s.closed).length, 1);
});

test("a cached page does not replay its old task-completion reload", () => {
  const app = setup();
  app.streams[0].onmessage({ data: JSON.stringify({ type: "finished" }) });
  assert.equal(app.timers.size, 1);
  app.emit("pagehide", true);
  assert.equal(app.timers.size, 0);
  app.emit("pageshow", true);
  app.flush();
  assert.equal(app.reloads, 0);
  app.streams.at(-1).onmessage({ data: JSON.stringify({ type: "finished" }) });
  app.flush();
  assert.equal(app.reloads, 1);
});

test("collection counts stay visible while another lane reports progress", () => {
  const values = ['new', 'known', 'pages', 'detailed', 'skipped_details', 'detail_errors'].map(key => ({ dataset: { collectionCount: key }, textContent: '' }));
  const counts = { hidden: true, querySelectorAll: () => values };
  const counter = { textContent: '' };
  const app = setup({ 'collection-progress': counts, 'task-counter': counter });
  const main = { lane: 'main', done: 5, total: 0, result: {
    collect_progress: { pages: 5, new: 2, known: 242, skipped_details: 240, detailed: 3, detail_errors: 1 },
  } };
  app.streams[0].onmessage({ data: JSON.stringify({ type: 'progress', task: main }) });
  assert.equal(counts.hidden, false);
  assert.deepEqual(values.map(value => value.textContent), [2, 242, 5, 3, 240, 1]);
  assert.equal(values[5].dataset.hasErrors, 'true');
  app.streams[0].onmessage({ data: JSON.stringify({ type: 'progress', task: { lane: 'activity', done: 9 } }) });
  assert.equal(counts.hidden, false);
  assert.match(counter.textContent, /страниц: 5/);
});
