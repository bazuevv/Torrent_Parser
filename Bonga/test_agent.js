/* Проверка агента-закладки в изоляции: браузерное окружение подменяем
   заглушками, время двигаем сами.

   Запуск: node Bonga/test_agent.js

   Сторожит две поломки, которые уже стоили оплаченных показов (30.08):
   зависший запрос к сайту, из-за которого агент молчал на команду и
   навсегда переставал сообщать баланс, и обновление потока раз в 45 секунд
   при том, что доступ сайт закрывает на 30-й. */
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const SRC = path.join(__dirname, 'cb-bookmarklet.js');
const PLAYER = 'http://player.test';

let ok = true;
function check(name, cond, extra = '') {
  console.log((cond ? '  OK   ' : '  ПРОВАЛ ') + name + (extra ? ` — ${extra}` : ''));
  if (!cond) ok = false;
}

/* --- управляемое время ---------------------------------------------------- */
function makeClock() {
  let now = 0;
  let seq = 0;
  const timers = new Map();
  const api = {
    now: () => now,
    setTimeout(fn, ms) { const id = ++seq; timers.set(id, { at: now + (ms || 0), fn, every: 0 }); return id; },
    setInterval(fn, ms) { const id = ++seq; timers.set(id, { at: now + (ms || 0), fn, every: ms || 1 }); return id; },
    clear(id) { timers.delete(id); },
    /* Двигаем время шагами, между шагами отдавая управление микрозадачам:
       обработчики агента асинхронные, и без этого их продолжения не успеют
       выполниться до следующего срабатывания таймера. */
    async advance(ms) {
      const target = now + ms;
      while (true) {
        let next = null;
        for (const [id, t] of timers)
          if (t.at <= target && (next === null || t.at < timers.get(next).at)) next = id;
        if (next === null) break;
        const t = timers.get(next);
        now = t.at;
        if (t.every) t.at = now + t.every; else timers.delete(next);
        try { t.fn(); } catch (e) { console.log('  таймер бросил:', e.message); }
        for (let i = 0; i < 50; i++) await Promise.resolve();
      }
      now = target;
      for (let i = 0; i < 50; i++) await Promise.resolve();
    },
  };
  return api;
}

/* --- запуск агента в песочнице -------------------------------------------- */
function launch({ fetchImpl }) {
  const clock = makeClock();
  const sent = [];
  const store = new Map();
  const el = () => ({
    style: { cssText: '' }, textContent: '', title: '', dataset: {},
    setAttribute() {}, remove() {}, select() {}, appendChild() {},
    addEventListener() {}, querySelector: () => null,
  });
  const listeners = {};
  const hub = { closed: false, postMessage: (p) => sent.push(p) };

  const win = {
    __proto__: null,
    opener: hub,
    closed: false,
    location: { origin: 'https://ru.chaturbate.com', href: 'https://ru.chaturbate.com/' },
    addEventListener: (kind, fn) => { listeners[kind] = fn; },
    open: () => hub,
    $reactAppContext: { logged_in_user: { username: 'adm211' } },
  };
  const sandbox = {
    window: win,
    document: {
      cookie: 'csrftoken=abc123',
      body: el(),
      createElement: el,
      querySelector: () => ({}),
      addEventListener() {},
    },
    localStorage: {
      getItem: k => (store.has(k) ? store.get(k) : null),
      setItem: (k, v) => store.set(k, String(v)),
      removeItem: k => store.delete(k),
    },
    navigator: { clipboard: { writeText: async () => {} } },
    location: win.location,
    fetch: fetchImpl,
    AbortController,
    URLSearchParams,
    Date: { now: () => clock.now() },
    setTimeout: clock.setTimeout,
    setInterval: clock.setInterval,
    clearTimeout: clock.clear,
    clearInterval: clock.clear,
    console,
    Promise,
    JSON,
    Number,
    String,
    Math,
    Error,
  };
  sandbox.globalThis = sandbox;
  win.location = sandbox.location;

  const src = fs.readFileSync(SRC, 'utf8').replace('__PLAYER_ORIGIN__', PLAYER);
  vm.createContext(sandbox);
  vm.runInContext(src, sandbox, { filename: 'cb-bookmarklet.js' });

  /* Отдаём агенту poll-ответ так же, как это делает плеер. */
  const poll = answer => listeners.message({
    origin: PLAYER, source: hub, data: { v: 1, kind: 'poll', answer },
  });
  const ready = () => listeners.message({
    origin: PLAYER, source: hub, data: { v: 1, kind: 'ready' },
  });
  return { clock, sent, poll, ready, store,
           results: () => sent.filter(m => m.kind === 'result').map(m => m.payload) };
}

/* --- 1. Зависший запрос к сайту не подвешивает агента ---------------------- */
(async () => {
  let started = 0;
  const hung = (url, init) => {
    started += 1;
    return new Promise((_, reject) => {
      init.signal.addEventListener('abort', () => {
        const e = new Error('The operation was aborted.');
        e.name = 'AbortError';
        reject(e);
      });
    });
  };
  const a = launch({ fetchImpl: hung });
  a.ready();
  a.poll({ spy: { spy: { state: 'spying', room: 'testroom' }, agent: {} },
           cmd: { id: 'c1', act: 'url_get', room: 'testroom' } });

  await a.clock.advance(30000);
  const answers = a.results().filter(p => p.act === 'url_get');
  check('зависший запрос: агент всё равно ответил на команду', answers.length === 1,
        `ответов ${answers.length}, запросов к сайту ${started}`);
  check('зависший запрос: в ответе нет адреса', answers[0] && !answers[0].url);
  check('зависший запрос: причина попала в raw',
        !!(answers[0] && /abort/i.test(answers[0].raw || '')),
        answers[0] && String(answers[0].raw).slice(0, 90));

  /* --- 2. Обновление потока идёт чаще, чем сайт закрывает доступ ---------- */
  const url = 'https://edge1.live.mmcdn.com/v1/edge/streams/origin.testroom.ULID/llhls.m3u8?token=t';
  const good = async () => ({
    ok: true, status: 200,
    text: async () => JSON.stringify({ url, room_status: 'private' }),
  });
  const b = launch({ fetchImpl: good });
  b.ready();
  b.poll({ spy: { spy: { state: 'spying', room: 'testroom' }, agent: {} }, cmd: null });

  await b.clock.advance(60000);
  const refreshes = b.results().filter(p => p.act === 'refresh');
  check('обновление: за 60 с было не меньше трёх подтверждений потока',
        refreshes.length >= 3, `подтверждений ${refreshes.length}`);
  check('обновление: успевает до тридцатой секунды',
        refreshes.length >= 1, `первых ${refreshes.length}`);
  check('обновление: несёт адрес потока',
        refreshes.every(r => r.url === url));

  /* --- 3. Вне сессии поток не трогаем ------------------------------------- */
  const c = launch({ fetchImpl: good });
  c.ready();
  c.poll({ spy: { spy: { state: 'idle', room: '' }, agent: {} }, cmd: null });
  await c.clock.advance(60000);
  check('вне сессии: подтверждений потока нет',
        c.results().filter(p => p.act === 'refresh').length === 0);

  console.log('\nИТОГ:', ok ? 'всё сошлось' : 'ЕСТЬ ПРОВАЛЫ');
  process.exit(ok ? 0 : 1);
})();
