/*
 * Стенд для блока DOM WATCH из claude-custom.js.
 *
 * Блок вырезается из живого файла (по маркерам), а вокруг него
 * подставляются фейковые window/document/fetch. Проверяется то, ради
 * чего блок писался: один наблюдатель на всех, throttle, отсутствие
 * реентерабельности, изоляция падений, предохранитель по бюджету и
 * игнорирование собственных мутаций.
 */
'use strict';
const fs = require('fs');

const path = require('path');
// Путь выводим от самого стенда, а не пишем абсолютным: проект уже
// однажды переезжал (на NVMe), и зашитый путь пережил бы переезд
// молча — стенд читал бы старую копию файла либо падал.
const SRC = path.join(__dirname, '..', '.claude', 'patches', 'claude-custom.js');
const lines = fs.readFileSync(SRC, 'utf8').split('\n');

// Блок = от строки с '(function () {' после заголовка DOM WATCH
// до первой строки '})();'
const headIdx = lines.findIndex((l) => l.includes('DOM WATCH — один наблюдатель'));
if (headIdx < 0) throw new Error('заголовок DOM WATCH не найден');
const startIdx = lines.findIndex((l, i) => i > headIdx && l === '(function () {');
const endIdx = lines.findIndex((l, i) => i > startIdx && l === '})();');
const block = lines.slice(startIdx, endIdx + 1).join('\n');

/* ---------- фейковое окружение ---------- */

let now = 0;
const timers = [];        // {id, fn, delay, next, repeat}
let nextTimerId = 1;
const posts = [];
let observerCb = null;

function schedule(fn, delay, repeat) {
  const t = { id: nextTimerId++, fn, delay, next: now + delay, repeat };
  timers.push(t);
  return t.id;
}

global.setTimeout = (fn, delay) => schedule(fn, delay || 0, false);
global.setInterval = (fn, delay) => schedule(fn, delay || 0, true);
global.clearTimeout = (id) => {
  const i = timers.findIndex((t) => t.id === id);
  if (i >= 0) timers.splice(i, 1);
};

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

global.performance = { now: () => now };
global.location = { href: 'vscode-webview://test/index.html' };
global.fetch = (url, opts) => {
  posts.push(JSON.parse(opts.body));
  return { catch: () => {} };
};
global.MutationObserver = class {
  constructor(cb) { observerCb = cb; }
  observe() { this.observing = true; }
};
let queryCalls = 0;
global.document = {
  body: {},
  addEventListener() {},
  // Счётчик обходов документа: ради его сокращения и делалась 60.2.
  querySelectorAll(sel) {
    queryCalls++;
    return [{ sel }];
  },
};
global.window = {};

/* Фейковые узлы. Достаточно того, чем пользуется фильтр
 * релевантности: closest/matches/querySelector и nodeType. */
function node(opts) {
  const o = opts || {};
  return {
    nodeType: 1,
    __claudeOwnNode: !!o.own,
    // relevant — узел лежит внутри поля ввода/строки сессии
    closest: () => (o.relevant ? {} : null),
    matches: () => !!o.isContainer,
    querySelector: () => (o.holdsContainer ? {} : null),
  };
}

/** Эмуляция пачки мутаций от браузера. */
function mutate(targets, added) {
  observerCb(targets.map((t) => ({ target: t, addedNodes: added || [] })));
}

/* ---------- прогон ---------- */

eval(block);
const watch = window.__claudeDomWatch;

const results = [];
function check(name, cond, detail) {
  results.push({ name, ok: !!cond, detail: detail || '' });
}

check('регистратор опубликован', typeof watch.register === 'function');

// --- сканы вызываются, первый раз сразу при регистрации
let a = 0, b = 0;
watch.register('alpha', () => { a++; });
watch.register('beta', () => { b++; });
check('первый скан при регистрации', a === 1 && b === 1, `a=${a} b=${b}`);

// --- throttle: сто мутаций подряд дают ОДИН проход
const foreign = node({ relevant: true });
for (let i = 0; i < 100; i++) mutate([foreign]);
check('мутации не сканируют синхронно', a === 1, `a=${a}`);
advance(250);
check('сто мутаций → один проход', a === 2 && b === 2, `a=${a} b=${b}`);

// --- свои узлы не будят наблюдателя
const own = node({ own: true, relevant: true });
for (let i = 0; i < 50; i++) mutate([own]);
advance(1000);
check('свои мутации игнорируются', a === 2, `a=${a}`);

// --- смешанная пачка (свой + чужой) обход всё же запускает
mutate([own, foreign]);
advance(250);
check('чужой узел в пачке со своим будит', a === 3, `a=${a}`);

/* --- фильтр релевантности: мутация вне поля ввода и списка сессий
 * не должна вызывать сканы вовсе. Это главное приобретение 60.1:
 * в покое приложение мутирует DOM непрерывно, и раньше каждая такая
 * мутация оплачивалась восемью обходами документа. */
const irrelevant = node({});
const aBeforeIrrelevant = a;
for (let i = 0; i < 200; i++) mutate([irrelevant]);
advance(250);
check('нерелевантные мутации не сканируют', a === aBeforeIrrelevant,
  `a=${a}`);

// --- но монтирование самого контейнера ловится через addedNodes
mutate([irrelevant], [node({ isContainer: true })]);
advance(250);
check('контейнер в addedNodes будит', a === aBeforeIrrelevant + 1, `a=${a}`);

// --- и контейнер, лежащий ГЛУБЖЕ добавленного узла, тоже
mutate([irrelevant], [node({ holdsContainer: true })]);
advance(250);
check('контейнер внутри addedNodes будит', a === aBeforeIrrelevant + 2, `a=${a}`);

/* --- общий контекст: документ обходится один раз на проход, а не
 * один раз на модуль. Это вся суть 60.2 — при 11 700 узлах восемь
 * одинаковых querySelectorAll и были львиной долей стоимости скана. */
let seenCtx = null;
watch.register('ctx-probe', (ctx) => { seenCtx = ctx; });
queryCalls = 0;
mutate([foreign]);
advance(250);
check('контекст доходит до скана',
  seenCtx && Array.isArray(seenCtx.inputs) && Array.isArray(seenCtx.sessions)
    && Array.isArray(seenCtx.imageAttachments) && Array.isArray(seenCtx.imagePreviews),
  JSON.stringify(seenCtx));
check('обход документа один на проход, а не на модуль',
  queryCalls === 4, `querySelectorAll вызван ${queryCalls} раз при 3 подписчиках`);

// --- скан вне прохода получает undefined и ищет узлы сам
let ctxOutside = 'не вызывался';
watch.register('ctx-fallback', (ctx) => { ctxOutside = ctx; });
check('вне прохода контекста нет', ctxOutside === undefined, String(ctxOutside));

// --- периодический обход-подстраховка
const beforeSweep = a;
advance(3000);
check('периодический обход работает', a > beforeSweep, `${beforeSweep}→${a}`);

// --- падение одного скана не мешает остальным
let good = 0;
watch.register('bad', () => { throw new Error('нарочно'); });
watch.register('good', () => { good++; });
const goodAfterRegister = good;
mutate([foreign]);
advance(250);
check('падение соседа не мешает', good === goodAfterRegister + 1, `good=${good}`);
check('падение скана сообщено один раз',
  posts.filter((p) => p.kind === 'scan-error').length === 1,
  JSON.stringify(posts.filter((p) => p.kind === 'scan-error').map((p) => p.module)));

// --- реентерабельность: скан, мутирующий DOM, не вызывает сам себя
let depth = 0, maxDepth = 0;
watch.register('recursive', () => {
  depth++;
  maxDepth = Math.max(maxDepth, depth);
  mutate([foreign]);          // как будто вставили узел
  depth--;
});
advance(250);
mutate([foreign]);
advance(250);
check('нет рекурсии скан→мутация→скан', maxDepth === 1, `maxDepth=${maxDepth}`);

// --- предохранитель: дорогой скан отключается за минуту
let hogCalls = 0;
watch.register('hog', () => { hogCalls++; now += 900; });  // 900 мс за вызов
const hogBefore = hogCalls;
advance(60000);
const guardPosts = posts.filter((p) => p.kind === 'scan-guard');
check('предохранитель отключил дорогой скан',
  guardPosts.length === 1 && guardPosts[0].module === 'hog',
  JSON.stringify(guardPosts.map((p) => p.module + ':' + p.spent_ms)));
const hogAtGuard = hogCalls;
advance(10000);
check('отключённый скан больше не зовут', hogCalls === hogAtGuard,
  `${hogAtGuard}→${hogCalls}`);
check('живые сканы продолжают работать', good > goodAfterRegister + 1, `good=${good}`);

/* ---------- отчёт ---------- */
let failed = 0;
for (const r of results) {
  if (!r.ok) failed++;
  console.log((r.ok ? '  OK  ' : ' FAIL ') + r.name + (r.detail ? '  [' + r.detail + ']' : ''));
}
console.log(failed ? `\n${failed} проверок провалено` : `\nвсе ${results.length} проверок пройдены`);
process.exit(failed ? 1 : 0);
