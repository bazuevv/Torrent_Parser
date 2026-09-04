/*
 * Стенд для модуля LIMIT ALERT STOP BUTTON из claude-custom.js.
 *
 * Проверяется то, ради чего модуль писался: плавающая плашка «стоп»
 * существует только пока поле playing истинно, висит по центру НАД
 * панелью ввода (позиция пересчитывается при пересоздании панели и
 * ресайзе окна), не плодит копий, уходит после клика и после трёх
 * промахов сервера, а её узлы помечены __claudeOwnNode. Блок
 * вырезается из живого файла — иначе стенд проверял бы копию, а не
 * то, что едет в webview.
 *
 * Запуск: node tmp/limit-stop-button-test.js
 */
'use strict';
const fs = require('fs');
const path = require('path');

// Путь выводим от самого стенда: проект уже однажды переезжал, и
// зашитый путь пережил бы переезд молча.
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

const block = cut('LIMIT ALERT STOP BUTTON');

/* ---------- виртуальное время ---------- */

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
global.clearTimeout = global.clearInterval = (id) => {
  const i = timers.findIndex((t) => t.id === id);
  if (i >= 0) timers.splice(i, 1);
};

/** Прогоняет таймеры вперёд на ms виртуального времени. */
function advance(ms) {
  const until = now + ms;
  for (;;) {
    timers.sort((a, b) => a.next - b.next);
    const t = timers.find((x) => x.next <= until);
    if (!t) break;
    now = t.next;
    if (t.repeat) t.next = now + t.delay;
    else timers.splice(timers.indexOf(t), 1);
    t.fn();
  }
  now = until;
}

/* ---------- фейковый DOM ---------- */

function makeElement(tag, cls) {
  const classes = new Set(cls ? cls.split(/\s+/).filter(Boolean) : []);
  const attrs = {};
  const el = {
    tagName: String(tag).toUpperCase(),
    attrs,
    children: [],
    parentNode: null,
    textContent: '',
    title: '',
    type: '',
    style: {},
    __claudeOwnNode: false,
    handlers: {},
    classList: {
      add(c) { classes.add(c); },
      remove(c) { classes.delete(c); },
      contains: (c) => classes.has(c),
    },
    get className() { return Array.from(classes).join(' '); },
    set className(v) { classes.clear(); String(v).split(/\s+/).filter(Boolean).forEach((c) => classes.add(c)); },
    setAttribute(name, value) { attrs[name] = String(value); },
    getAttribute: (name) => (name in attrs ? attrs[name] : null),
    appendChild(child) { el.children.push(child); child.parentNode = el; return child; },
    removeChild(child) {
      const i = el.children.indexOf(child);
      if (i >= 0) el.children.splice(i, 1);
      child.parentNode = null;
      return child;
    },
    addEventListener(type, fn) { el.handlers[type] = fn; },
    removeEventListener() {},
    // Геометрию назначает тест (контейнерам), по умолчанию её нет.
    getBoundingClientRect: () => null,
    click() {
      const e = { preventDefault() {}, stopPropagation() {} };
      if (el.handlers.click) el.handlers.click(e);
    },
  };
  return el;
}

/** Контейнер поля ввода с заданной геометрией (rect). */
function makeContainer(rect) {
  const c = makeElement('div', 'inputContainer_abc123');
  c.getBoundingClientRect = () => rect;
  return c;
}

const innerHeight = 700;
let resizeHandler = null;
const scans = {};

global.window = {
  innerHeight,
  addEventListener(type, fn) { if (type === 'resize') resizeHandler = fn; },
  removeEventListener() {},
  __CLAUDE_CUSTOM_CONFIG__: {},
  __claudeDomWatch: {
    register(name, fn) { scans[name] = fn; },
    kick() {},
  },
};

global.document = {
  readyState: 'complete',
  addEventListener() {},
  body: makeElement('body'),
  createElement: (tag) => makeElement(tag, ''),
  createElementNS: (ns, tag) => makeElement(tag, ''),
  querySelectorAll(sel) {
    // Модуль ищет только контейнеры поля ввода; всё прочее — смена
    // контракта, которую стенд обязан поймать, а не проглотить.
    if (sel.indexOf('inputContainer_') >= 0) return containers;
    throw new Error('стенд не умеет селектор: ' + sel);
  },
};

const containers = [makeContainer({ top: 600, bottom: 660, left: 100, width: 400, height: 60 })];

/* ---------- фейковый сервер ---------- */

let serverPlaying = false;
let fetchBroken = false;
let stopRequests = 0;

global.fetch = (url, opts) => {
  if (fetchBroken) return Promise.reject(new Error('сервер недоступен'));
  const u = String(url);
  if (u.indexOf('/limit-reset-alert-stop') >= 0) {
    stopRequests++;
    const was = serverPlaying;
    serverPlaying = false;
    return Promise.resolve({ json: () => Promise.resolve({ ok: true, stopped: was }) });
  }
  if (u.indexOf('/limit-reset-alert') >= 0) {
    return Promise.resolve({ json: () => Promise.resolve({ ok: true, playing: serverPlaying }) });
  }
  return Promise.reject(new Error('в стенде нет такого endpoint: ' + u));
};

/** Микротаски: fetch(...).then отрабатывает после возврата в цикл. */
const flush = () => new Promise((r) => setImmediate(r));

/* ---------- прогон ---------- */

const results = [];
function check(name, cond, detail) {
  results.push({ name, ok: !!cond, detail: detail || '' });
}

eval(block);

const BTN_CLASS = 'claude-limit-stop-float';
const badge = () => document.body.children.find((c) => c.classList.contains(BTN_CLASS)) || null;

async function run() {
  check('скан зарегистрирован под своим именем',
    typeof scans['limit-stop'] === 'function');

  /* --- покой: звука нет, плашки нет (и после первого опроса) */
  await flush();
  check('без звука плашки нет', !badge());

  /* --- звук начался: плашка в body, помечена, с иконкой */
  serverPlaying = true;
  advance(1000);
  await flush();
  const b = badge();
  check('звук играет — плашка появилась', !!b);
  check('плашка в body (fixed, не в футере)',
    !!b && b.parentNode === document.body);
  check('плашка помечена __claudeOwnNode', !!b && b.__claudeOwnNode === true);
  check('иконка — SVG, а не текст',
    !!b && b.children.length === 1 && b.children[0].tagName === 'SVG');

  /* --- позиция: центр панели, чуть выше её верхнего края */
  check('позиция: по центру панели',
    !!b && b.style.left === '300px', `left=${b && b.style.left}`);
  check('позиция: над панелью',
    !!b && b.style.bottom === '110px', `bottom=${b && b.style.bottom}`);

  /* --- повторные опросы не плодят копий */
  advance(1000);
  advance(1000);
  await flush();
  const count = document.body.children.filter((c) => c.classList.contains(BTN_CLASS)).length;
  check('повторные опросы не дублируют плашку', count === 1, `найдено ${count}`);

  /* --- клик: POST ушёл, звук погашен, плашка ушла сама */
  b.click();
  await flush();
  await flush();
  check('клик отправил POST /limit-reset-alert-stop', stopRequests === 1,
    `запросов ${stopRequests}`);
  check('после клика плашка исчезла', !badge());

  /* --- звук кончился сам: плашка уходит */
  serverPlaying = true;
  advance(1000);
  await flush();
  check('новый звук — плашка вернулась', !!badge());
  serverPlaying = false;
  advance(1000);
  await flush();
  check('звук кончился — плашка ушла сама', !badge());

  /* --- панель пересоздалась ниже и шире: позиция пересчиталась */
  serverPlaying = true;
  advance(1000);
  await flush();
  containers[0] = makeContainer({ top: 500, bottom: 560, left: 200, width: 600, height: 60 });
  scans['limit-stop'](); // подстраховочный обход общего наблюдателя
  const moved = badge();
  check('пересозданная панель: центр пересчитан',
    !!moved && moved.style.left === '500px', `left=${moved && moved.style.left}`);
  check('пересозданная панель: отступ сверху пересчитан',
    !!moved && moved.style.bottom === '210px', `bottom=${moved && moved.style.bottom}`);

  /* --- ресайз окна двигает плашку к новой геометрии */
  containers[0] = makeContainer({ top: 620, bottom: 680, left: 40, width: 200, height: 60 });
  resizeHandler();
  const resized = badge();
  check('resize пересчитал позицию',
    !!resized && resized.style.left === '140px' && resized.style.bottom === '90px',
    `left=${resized && resized.style.left} bottom=${resized && resized.style.bottom}`);

  /* --- сервер замолчал: три промаха убирают плашку */
  fetchBroken = true;
  advance(1000);
  advance(1000);
  await flush();
  check('после двух промахов плашка ещё есть (не мигает)', !!badge());
  advance(1000);
  await flush();
  check('третий промах — плашка убрана', !badge());

  /* --- возврат сервера: всё работает снова */
  fetchBroken = false;
  serverPlaying = true;
  advance(1000);
  await flush();
  check('сервер ожил — плашка вернулась', !!badge());

  const failed = results.filter((r) => !r.ok);
  results.forEach((r) => console.log((r.ok ? '  ok  ' : ' FAIL ') + r.name + (r.detail ? ' — ' + r.detail : '')));
  console.log('');
  if (failed.length) {
    console.log(`ПРОВАЛЕНО: ${failed.length} из ${results.length}`);
    process.exit(1);
  }
  console.log(`OK, ${results.length} проверок`);
}

run().catch((e) => { console.error('стенд упал:', e); process.exit(1); });
