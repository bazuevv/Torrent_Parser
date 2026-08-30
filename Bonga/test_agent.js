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
function launch({ fetchImpl, opener = true, dossier = null, webpack = null, video = null }) {
  const clock = makeClock();
  const sent = [];
  const store = new Map();
  const byId = {};
  const el = () => {
    const n = {
      id: '',
      style: { cssText: '', opacity: '', visibility: '' },
      textContent: '', title: '', dataset: {},
      setAttribute() {},
      remove() { if (n.id) delete byId[n.id]; },
      select() {},
      appendChild(child) { if (child && child.id) byId[child.id] = child; },
      addEventListener() {}, querySelector: () => null,
    };
    return n;
  };
  const host = el();
  const listeners = {};
  const hub = { closed: false, postMessage: (p) => sent.push(p) };

  const opened = [];
  const videoEl = video || { muted: false, play: async () => {} };
  const docEvents = {};
  const win = {
    __proto__: null,
    opener: opener ? hub : null,
    closed: false,
    location: { origin: 'https://ru.chaturbate.com', href: 'https://ru.chaturbate.com/' },
    addEventListener: (kind, fn) => { listeners[kind] = fn; },
    open: (url, name) => { opened.push({ url, name }); return hub; },
    $reactAppContext: { logged_in_user: { username: 'adm211' } },
    // Досье сайт кладёт только на страницу комнаты — по нему агент и
    // определяет, где стоит вкладка.
    initialRoomDossier: dossier,
    webpackChunk_multimediallc_cb_ts: webpack,
  };
  const sandbox = {
    window: win,
    document: {
      cookie: 'csrftoken=abc123',
      body: host,
      head: host,
      documentElement: host,
      hidden: true,
      visibilityState: 'hidden',
      createElement: el,
      querySelector: () => videoEl,
      querySelectorAll: sel => (String(sel).indexOf('video') >= 0 ? [videoEl] : []),
      getElementById: id => (id === 'chat-player' ? videoEl : (byId[id] || null)),
      addEventListener(kind, fn) {
        (docEvents[kind] || (docEvents[kind] = [])).push(fn);
      },
      removeEventListener(kind, fn) {
        const a = docEvents[kind];
        if (!a) return;
        const i = a.indexOf(fn);
        if (i >= 0) a.splice(i, 1);
      },
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
    TypeError,
    Object,
    Function,
    webpackChunk_multimediallc_cb_ts: webpack,
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
  const badge = () => ((win.__cbAgent || {}).badge || {}).textContent || '';
  const fireDoc = (kind, target) => {
    (docEvents[kind] || []).forEach(fn => {
      try { fn({ target }); } catch (e) {}
    });
  };
  return { clock, sent, poll, ready, store, opened, badge, sandbox, fireDoc,
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

  /* --- 4. Вкладка открыта не плеером -------------------------------------
     Раньше на этот случай открывалась вкладка-мост; её убрали, потому что
     дотянуться до плеера ей всё равно было нечем, зато она перехватывала
     команды у сервера и теряла их — остановка ждала подтверждения 95 секунд
     (журнал 30.08). Теперь агент обязан молча объяснить, что делать. */
  const d = launch({ fetchImpl: good, opener: false });
  await d.clock.advance(30000);
  check('без opener: ничего не открывает', d.opened.length === 0,
        JSON.stringify(d.opened));
  check('без opener: объясняет, что вкладку открывает плеер',
        /нажмите spy в плеере/.test(d.badge()), d.badge());
  check('без opener: наружу ничего не шлёт', d.sent.length === 0,
        `сообщений ${d.sent.length}`);

  /* --- 5. Агент называет плееру комнату своей вкладки ---------------------
     Плеер по ней решает, надо ли переводить вкладку: сидя на главной, она не
     присутствует в комнате, и сайт закрывает оплаченный показ на тридцатой
     секунде. */
  const greet = agent => (agent.sent.find(m => m.kind === 'hello') || {});

  const e = launch({ fetchImpl: good, dossier: { broadcaster_username: 'TestRoom' } });
  check('на странице комнаты: hello называет комнату', greet(e).room === 'testroom',
        JSON.stringify(greet(e)));
  await e.clock.advance(11000);
  const ping = e.sent.filter(m => m.kind === 'ping').pop() || {};
  check('на странице комнаты: пинг тоже называет комнату', ping.room === 'testroom',
        JSON.stringify(ping));

  const f = launch({ fetchImpl: good });
  check('на главной: комната пустая', greet(f).room === '', JSON.stringify(greet(f)));
  check('на главной: имя аккаунта всё равно есть', greet(f).username === 'adm211',
        JSON.stringify(greet(f)));

  /* --- 6. После входа агент будит плеер сайта, но не качает его HLS -------
     POST spy_on_private_show_request только оплачивает вход. Штатный код
     сайта следом делает changeStatus("privatespying") — иначе их плеер
     остаётся в privatenotwatching, не шлёт playerQuality и сайт закрывает
     поток на 30-й секунде (lin_rin 14:42, вкладка уже была на комнате).
     Их HLS при этом шёл параллельно с нашим (dulce_devil_ 30.08) — агент
     v10 глушит видео вкладки и режет сегменты CDN, ajax чата не трогает. */
  function dossierHtml() {
    const json = JSON.stringify({
      broadcaster_username: 'testroom',
      viewer_username: 'adm211',
      room_status: 'private',
      spy_private_show_price: 6,
      token_balance: 200,
    }).replace(/"/g, '\\u0022');
    return `window.initialRoomDossier = "${json}";`;
  }
  const startUrl = 'https://edge1.live.mmcdn.com/v1/edge/streams/origin.testroom.ULID/llhls.m3u8?token=t';
  const cdnPlaylist = 'https://edge18-hel.live.mmcdn.com/live-hls/amlst:testroom-sd-c6e-orig/playlist.m3u8';
  let cdnHits = 0;
  const startFetch = async (href) => {
    const u = String(href && href.url || href);
    if (/mmcdn\.com|highwebmedia\.com/i.test(u)) cdnHits += 1;
    if (u.includes('spy_on_private_show_request'))
      return { ok: true, status: 200, text: async () => JSON.stringify({ success: true }) };
    if (u.includes('private_show_cancel'))
      return { ok: true, status: 200,
               text: async () => JSON.stringify({ success: true, remaining_seconds: 0,
                                                  can_access: true }) };
    if (u.includes('get_edge_hls_url'))
      return { ok: true, status: 200,
               text: async () => JSON.stringify({ success: true, url: startUrl,
                                                  room_status: 'private' }) };
    return { ok: true, status: 200, text: async () => dossierHtml() };
  };

  function makeWebpack() {
    const statuses = [];
    const conn = {
      status: 'privatenotwatching',
      changeStatus(next) { statuses.push(next); this.status = next; },
    };
    const factories = {
      32939(module, exports) {
        /* Имена roomLoaded/roomCleanup обязаны быть в тексте фабрики:
           агент ищет модуль по ним, а не по номеру — номер плывёт с cachebust. */
        const roomLoaded = {
          eventName: 'roomLoaded',
          listen(fn) {
            fn({ chatConnection: conn, dossier: {} });
            return { removeListener() {} };
          },
        };
        const roomCleanup = { eventName: 'roomCleanup' };
        exports.X0 = roomLoaded;
        exports.Gr = roomCleanup;
      },
    };
    const cache = Object.create(null);
    function req(id) {
      if (cache[id]) return cache[id].exports;
      const box = { exports: {} };
      cache[id] = box;
      factories[id](box, box.exports, req);
      return box.exports;
    }
    req.m = factories;
    const chunks = [];
    chunks.push = function (chunk) {
      const runtime = chunk[2];
      if (typeof runtime === 'function') runtime(req);
      return Array.prototype.push.call(this, chunk);
    };
    return { chunks, conn, statuses };
  }

  const played = [];
  const paused = [];
  const videoStub = {
    id: 'chat-player',
    tagName: 'VIDEO',
    muted: false,
    volume: 1,
    src: 'blob:x',
    srcObject: {},
    style: { opacity: '', visibility: '' },
    hls: { stopLoad() { paused.push('stopLoad'); } },
    play: async () => { played.push('play'); },
    pause() { paused.push('pause'); },
    load() { paused.push('load'); },
    removeAttribute(name) { if (name === 'src') this.src = ''; },
  };
  const wp = makeWebpack();
  const g = launch({ fetchImpl: startFetch, webpack: wp.chunks, video: videoStub,
                     dossier: { broadcaster_username: 'testroom' } });
  g.ready();
  g.poll({ spy: { spy: { state: 'idle', room: '' }, agent: { username: 'adm211' } },
           cmd: { id: 's1', act: 'spy_start', room: 'testroom' } });
  for (let i = 0; i < 200; i++) await Promise.resolve();
  const startRes = g.results().filter(p => p.act === 'spy_start');
  check('вход: команда выполнилась', startRes.length === 1 && startRes[0].ok === true,
        JSON.stringify(startRes[0]));
  check('вход: плеер сайта переведён в privatespying',
        wp.statuses[0] === 'privatespying', JSON.stringify(wp.statuses));
  check('вход: отчёт знает, что плеер найден',
        !!(startRes[0] && startRes[0].site && startRes[0].site.conn),
        JSON.stringify(startRes[0] && startRes[0].site));
  check('вход: video.play не вызван', played.length === 0, JSON.stringify(played));
  check('вход: видео сайта поставлено на паузу',
        paused.includes('pause') && videoStub.src === '' && videoStub.muted === true,
        JSON.stringify({ paused, src: videoStub.src, muted: videoStub.muted }));
  check('вход: вкладка притворяется видимой',
        startRes[0] && startRes[0].site && startRes[0].site.vis === true,
        JSON.stringify(startRes[0] && startRes[0].site));
  check('вход: отчёт пишет, что видео сайта выключено',
        !!(startRes[0] && startRes[0].site && startRes[0].site.quiet === true),
        JSON.stringify(startRes[0] && startRes[0].site));
  check('вход: плашка v13', /Агент CB v13/.test(g.badge()), g.badge());

  let cdnBlocked = false;
  try {
    await g.sandbox.fetch(cdnPlaylist);
  } catch (e) {
    cdnBlocked = /cdn media blocked/.test(String(e && e.message || e));
  }
  check('вход: HLS сайта на CDN не уходит', cdnBlocked && cdnHits === 0,
        `blocked=${cdnBlocked} hits=${cdnHits}`);
  const ajax = await g.sandbox.fetch('https://ru.chaturbate.com/get_edge_hls_url_ajax/');
  const ajaxText = await ajax.text();
  check('вход: ajax чата по-прежнему проходит',
        ajax.ok && /"url"/.test(ajaxText), ajaxText.slice(0, 80));

  g.poll({ spy: { spy: { state: 'spying', room: 'testroom' }, agent: { username: 'adm211' } },
           cmd: null });
  for (let i = 0; i < 50; i++) await Promise.resolve();
  check('плашка: счётчик отсечённого CDN',
        /отсечено [1-9]/.test(g.badge()) && /ушло 0/.test(g.badge()),
        g.badge());

  /* Сторож не должен сбрасывать src: это и давало мерцание раз в 0.5 с. */
  const pausesAtStart = paused.length;
  const loadsAtStart = paused.filter(x => x === 'load').length;
  await g.clock.advance(30000);
  const loadsAfter = paused.filter(x => x === 'load').length;
  check('сторож: пауза повторяется после 30 с',
        paused.length > pausesAtStart, `было ${pausesAtStart}, стало ${paused.length}`);
  check('сторож: src не сбрасывается по таймеру',
        loadsAfter === loadsAtStart, `load было ${loadsAtStart}, стало ${loadsAfter}`);
  videoStub.src = cdnPlaylist;
  videoStub.srcObject = {};
  g.fireDoc('playing', videoStub);
  check('через 30 с: картинка спрятана без teardown',
        videoStub.style.opacity === '0' && videoStub.src === cdnPlaylist &&
        paused.includes('stopLoad'),
        JSON.stringify({ src: videoStub.src, opacity: videoStub.style.opacity,
                         last: paused.slice(-6) }));
  const cdnRefresh = g.results().filter(p => p.act === 'refresh' && p.cdn);
  check('refresh: несёт счётчик CDN',
        cdnRefresh.some(r => r.cdn.blocked >= 1 && r.cdn.passed === 0),
        JSON.stringify(cdnRefresh.map(r => r.cdn)));

  g.poll({ spy: { spy: { state: 'spying', room: 'testroom' }, agent: { username: 'adm211' } },
           cmd: { id: 's2', act: 'spy_stop', room: 'testroom' } });
  for (let i = 0; i < 200; i++) await Promise.resolve();
  check('выход: плеер сайта возвращён в privatenotwatching',
        wp.statuses.includes('privatenotwatching'), JSON.stringify(wp.statuses));
  cdnHits = 0;
  const afterStop = await g.sandbox.fetch(cdnPlaylist);
  check('выход: заслон CDN снят', afterStop && afterStop.ok === true && cdnHits === 1,
        `hits=${cdnHits}`);

  const h = launch({ fetchImpl: startFetch, dossier: { broadcaster_username: 'testroom' } });
  h.ready();
  h.poll({ spy: { spy: { state: 'idle', room: '' }, agent: { username: 'adm211' } },
           cmd: { id: 's3', act: 'spy_start', room: 'testroom' } });
  for (let i = 0; i < 200; i++) await Promise.resolve();
  const degraded = h.results().filter(p => p.act === 'spy_start');
  check('без webpack: вход всё равно проходит',
        degraded.length === 1 && degraded[0].ok === true,
        JSON.stringify(degraded[0]));
  check('без webpack: в отчёте плеер не найден',
        degraded[0] && degraded[0].site && degraded[0].site.conn === false,
        JSON.stringify(degraded[0] && degraded[0].site));

  console.log('\nИТОГ:', ok ? 'всё сошлось' : 'ЕСТЬ ПРОВАЛЫ');
  process.exit(ok ? 0 : 1);
})();
