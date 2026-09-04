/*
 * Стенд для модуля INPUT RING из claude-custom.js.
 *
 * Проверяется то, ради чего модуль писался: обводка поля ввода
 * получает номер сектора индикатора Mood, «данных нет» оставляет
 * рамку штатной, горячая правка inputRingColor применяется без
 * перезагрузки окна, а в DOM ничего не пишется, пока значение не
 * поменялось.
 *
 * Блоки вырезаются из живого файла — оба сразу, MOOD GAUGE и
 * INPUT RING: связь между ними (подписка на значение и границы
 * секторов) и есть самое хрупкое место, и фейковый индикатор проверял
 * бы стенд сам с собой. В живом окне то же самое стоит Reload Window
 * на каждую догадку.
 *
 * Запуск: node tmp/input-ring-test.js
 */
'use strict';
const fs = require('fs');
const path = require('path');

// Путь выводим от самого стенда, а не пишем абсолютным: проект уже
// однажды переезжал (на NVMe), и зашитый путь пережил бы переезд
// молча — стенд читал бы старую копию файла либо падал.
const SRC = path.join(__dirname, '..', '.claude', 'patches', 'claude-custom.js');
const lines = fs.readFileSync(SRC, 'utf8').split('\n');

/** Блок = от '(function () {' после строки-приметы до первой '})();' */
function cut(marker) {
  const headIdx = lines.findIndex((l) => l.includes(marker));
  if (headIdx < 0) throw new Error('заголовок не найден: ' + marker);
  const startIdx = lines.findIndex((l, i) => i > headIdx && l === '(function () {');
  const endIdx = lines.findIndex((l, i) => i > startIdx && l === '})();');
  return lines.slice(startIdx, endIdx + 1).join('\n');
}

const moodBlock = cut('MOOD GAUGE');
const ringBlock = cut('INPUT RING — обводка поля ввода');

/* ---------- фейковое окружение ---------- */

let now = 0;
const timers = [];
let nextTimerId = 1;

global.setTimeout = (fn, delay) => {
  timers.push({ id: nextTimerId++, fn, delay: delay || 0, next: now + (delay || 0), repeat: false });
  return nextTimerId - 1;
};
global.setInterval = (fn, delay) => {
  timers.push({ id: nextTimerId++, fn, delay: delay || 0, next: now + (delay || 0), repeat: true });
  return nextTimerId - 1;
};
global.clearTimeout = (id) => {
  const i = timers.findIndex((t) => t.id === id);
  if (i >= 0) timers.splice(i, 1);
};
global.performance = { now: () => now };
global.location = { href: 'vscode-webview://test/index.html' };

// Что отдавать на GET /custom-config. Меняется прямо в прогоне —
// это и есть «пользователь правит конфиг из окна настроек».
let liveConfig = { inputRingColor: 'mood' };
let configRequests = 0;

global.fetch = (url) => {
  if (String(url).indexOf('/custom-config') >= 0) {
    configRequests++;
    return Promise.resolve({ json: () => Promise.resolve({ ok: true, config: liveConfig }) });
  }
  return Promise.reject(new Error('в стенде нет такого endpoint: ' + url));
};

/** Микротаски: fetch(...).then отрабатывает после возврата в цикл. */
const flush = () => new Promise((r) => setImmediate(r));

/* ---------- фейковый DOM ---------- */

function element(opts) {
  const o = opts || {};
  const classes = new Set();
  const attrs = Object.assign({}, o.attrs);
  return {
    writes: 0,          // сколько раз модуль реально что-то записал
    classList: {
      add(c) { if (!classes.has(c)) { classes.add(c); this.__owner.writes++; } },
      remove(c) { if (classes.delete(c)) this.__owner.writes++; },
      contains: (c) => classes.has(c),
    },
    hasAttribute: (name) => Object.prototype.hasOwnProperty.call(attrs, name),
    getAttribute: (name) => (Object.prototype.hasOwnProperty.call(attrs, name) ? attrs[name] : null),
    setAttribute(name, value) { attrs[name] = String(value); this.writes++; },
    removeAttribute(name) { delete attrs[name]; this.writes++; },
    // MOOD GAUGE ищет здесь футер и поле ввода; в стенде их нет, и
    // индикатор не монтируется — нас интересует только его значение.
    querySelector: () => null,
    __classes: classes,
    __attrs: attrs,
  };
}

function makeElement(opts) {
  const el = element(opts);
  el.classList.__owner = el;
  return el;
}

// Контейнеров два, как в живом окне: внешний только позиционирует,
// рамку рисует внутренний — тот, которому расширение ставит режим.
const outer = makeElement({});
const inner = makeElement({ attrs: { 'data-permission-mode': 'default' } });
const containers = [outer, inner];

let queryCalls = 0;
global.document = {
  readyState: 'complete',
  addEventListener() {},
  querySelectorAll(sel) {
    queryCalls++;
    return sel.indexOf('inputContainer_') >= 0 ? containers : [];
  },
};

// Общий наблюдатель заменён на фейк: сканы зовём руками, чтобы
// проверять именно модуль, а не DOM WATCH (у него свой стенд).
const scans = {};
global.window = {
  __CLAUDE_CUSTOM_CONFIG__: { moodGauge: true, inputRingColor: 'mood', logs: false },
  __claudeDomWatch: {
    register(name, fn) { scans[name] = fn; fn(); },
    kick() {},
  },
};

/* ---------- прогон ---------- */

const results = [];
function check(name, cond, detail) {
  results.push({ name, ok: !!cond, detail: detail || '' });
}

eval(moodBlock);
eval(ringBlock);

const mood = window.__claudeMood;
const level = () => inner.getAttribute('data-claude-mood-level');
const ringed = () => inner.classList.contains('claude-ring-mood');

async function run() {
  check('скан зарегистрирован под своим именем', typeof scans['input-ring'] === 'function');

  /* Данных ещё нет: сессия не определилась, сервер молчит. Шкала в
   * этот момент обесцвечивается — рамка обязана остаться штатной,
   * а не показывать зелёный «всё хорошо» авансом. */
  check('без данных рамка штатная', !ringed() && level() === null,
    `class=${ringed()} level=${level()}`);

  // --- значение доходит до рамки подпиской, без единой мутации DOM
  mood.set(94);
  check('зелёный сектор доехал до рамки', ringed() && level() === '3',
    `level=${level()}`);

  // --- внешний контейнер не трогаем: у него нет ни рамки, ни режима
  check('внешний контейнер не тронут',
    !outer.classList.contains('claude-ring-mood') && outer.writes === 0,
    `writes=${outer.writes}`);

  // --- границы секторов: 0..24 → 0, 25..49 → 1, 50..74 → 2, 75..100 → 3
  const bounds = [[0, '0'], [24, '0'], [25, '1'], [49, '1'], [50, '2'],
    [74, '2'], [75, '3'], [100, '3']];
  let boundsOk = true;
  let boundsDetail = '';
  for (const [value, want] of bounds) {
    mood.set(value);
    if (level() !== want) {
      boundsOk = false;
      boundsDetail += ` ${value}→${level()} (ждали ${want})`;
    }
  }
  check('границы секторов совпадают со шкалой', boundsOk, boundsDetail.trim());

  // --- то же значение в DOM не переписывается: каждая запись будит
  //     общий наблюдатель, а скан приходит на каждую пачку мутаций
  mood.set(94);
  const writesBefore = inner.writes;
  mood.set(94);
  scans['input-ring']({ inputs: containers });
  scans['input-ring']({ inputs: containers });
  check('повторное значение не пишет в DOM', inner.writes === writesBefore,
    `${writesBefore}→${inner.writes}`);

  // --- контекст от общего обхода используется, свой поиск не идёт
  queryCalls = 0;
  scans['input-ring']({ inputs: containers });
  check('узлы берутся из общего контекста', queryCalls === 0,
    `querySelectorAll вызван ${queryCalls} раз`);

  // --- а вне прохода (подписка, регистрация) модуль ищет сам
  scans['input-ring']();
  check('вне прохода модуль ищет узлы сам', queryCalls === 1,
    `querySelectorAll вызван ${queryCalls} раз`);

  /* --- горячая правка: пользователь выбрал в окне настроек «mode».
   * Значение приходит из /custom-config, Reload Window не нужен. */
  check('конфиг опрашивается', configRequests > 0, `запросов ${configRequests}`);
  liveConfig = { inputRingColor: 'mode' };
  advance(5000);
  await flush();
  await flush();
  check('переключение на mode снимает обводку',
    !ringed() && level() === null, `class=${ringed()} level=${level()}`);

  // --- и не возвращается сама собой при следующем значении Mood
  mood.set(10);
  check('в режиме mode значение Mood рамку не трогает', !ringed(), `level=${level()}`);

  // --- возврат на mood красит снова, тем сектором, что сейчас
  liveConfig = { inputRingColor: 'mood' };
  advance(5000);
  await flush();
  await flush();
  check('возврат на mood красит текущим сектором',
    ringed() && level() === '0', `level=${level()}`);

  /* --- незнакомое значение параметра — это «не вмешиваться»:
   * безопасная сторона, рамка остаётся такой, какой её задумало
   * расширение. */
  liveConfig = { inputRingColor: 'rainbow' };
  advance(5000);
  await flush();
  await flush();
  check('незнакомый режим = штатная рамка', !ringed(), `level=${level()}`);

  /* ---------- отчёт ---------- */
  let failed = 0;
  for (const r of results) {
    if (!r.ok) failed++;
    console.log((r.ok ? '  OK  ' : ' FAIL ') + r.name + (r.detail ? '  [' + r.detail + ']' : ''));
  }
  console.log(failed ? `\n${failed} проверок провалено` : `\nвсе ${results.length} проверок пройдены`);
  process.exit(failed ? 1 : 0);
}

/** Прокрутить виртуальное время на ms, выполняя таймеры по порядку. */
function advance(ms) {
  const until = now + ms;
  for (;;) {
    const due = timers
      .filter((t) => t.next <= until)
      .sort((a, b) => a.next - b.next)[0];
    if (!due) break;
    now = due.next;
    if (due.repeat) due.next = now + due.delay;
    else clearTimeout(due.id);
    due.fn();
  }
  now = until;
}

run();
