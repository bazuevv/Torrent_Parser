/*
 * Стенд для модуля LIMIT ALERT STOP BUTTON из claude-custom.js.
 *
 * Проверяется то, ради чего модуль писался: кнопка существует только
 * пока поле playing истинно, не дублируется при повторных опросах,
 * уходит после клика по «Стоп» и после трёх промахов сервера,
 * возвращается в пересозданный React'ом футер, а её узлы помечены
 * __claudeOwnNode. Блок вырезается из живого файла — иначе стенд
 * проверял бы копию, а не то, что едет в webview.
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

function matchesSel(el, sel) {
  if (sel === 'button') return el.tagName === 'BUTTON';
  let m = /^\.([\w-]+)$/.exec(sel);
  if (m) return el.classList.contains(m[1]);
  m = /^\[class\*="([^"]*)"\]$/.exec(sel);
  if (m) return String(el.className).indexOf(m[1]) >= 0;
  if (sel === '[role="textbox"][contenteditable]') {
    return el.attrs['role'] === 'textbox' && 'contenteditable' in el.attrs;
  }
  // Незнакомый селектор — ошибка, а не «не совпало»: модуль сменил
  // запрос к DOM, и стенд обязан это поймать, а не проглотить.
  throw new Error('стенд не умеет селектор: ' + sel);
}

function makeElement(tag, cls, attrs) {
  const classes = new Set(cls ? cls.split(/\s+/).filter(Boolean) : []);
  const el = {
    tagName: String(tag).toUpperCase(),
    attrs: attrs || {},
    children: [],
    parentNode: null,
    textContent: '',
    title: '',
    type: '',
    __claudeOwnNode: false,
    handlers: {},
    classList: {
      add(c) { classes.add(c); },
      remove(c) { classes.delete(c); },
      contains: (c) => classes.has(c),
    },
    get className() { return Array.from(classes).join(' '); },
    set className(v) { classes.clear(); String(v).split(/\s+/).filter(Boolean).forEach((c) => classes.add(c)); },
    appendChild(child) { el.children.push(child); child.parentNode = el; return child; },
    insertBefore(child, ref) {
      const i = el.children.indexOf(ref);
      if (i >= 0) el.children.splice(i, 0, child); else el.children.push(child);
      child.parentNode = el;
      return child;
    },
    removeChild(child) {
      const i = el.children.indexOf(child);
      if (i >= 0) el.children.splice(i, 1);
      child.parentNode = null;
      return child;
    },
    addEventListener(type, fn) { el.handlers[type] = fn; },
    removeEventListener() {},
    click() {
      const e = { preventDefault() { e.defaulted = true; }, stopPropagation() {} };
      if (el.handlers.click) el.handlers.click(e);
      return e;
    },
    matches: (sel) => matchesSel(el, sel),
    querySelector(sel) {
      for (const child of el.children) {
        if (matchesSel(child, sel)) return child;
        const deep = child.querySelector(sel);
        if (deep) return deep;
      }
      return null;
    },
  };
  el.querySelectorAll = () => { throw new Error('модуль не должен звутить querySelectorAll'); };
  return el;
}

/** Живой контейнер поля ввода: футер с кнопкой-донором + textarea. */
function makeContainer() {
  const container = makeElement('div', 'inputContainer_abc123');
  const footer = makeElement('div', 'inputFooter_def456');
  const donor = makeElement('button', 'footerButton_ghi789');
  footer.appendChild(donor);
  const textbox = makeElement('div', '', { role: 'textbox', contenteditable: 'plaintext-only' });
  container.appendChild(footer);
  container.appendChild(textbox);
  return container;
}

const containers = [makeContainer()];

global.document = {
  readyState: 'complete',
  addEventListener() {},
  createElement: (tag) => makeElement(tag, ''),
  querySelectorAll(sel) {
    if (sel.indexOf('inputContainer_') >= 0) return containers;
    throw new Error('стенд не умеет селектор: ' + sel);
  },
};

// Общий наблюдатель заменён фейком: сканы зовём руками, DOM WATCH
// проверяет свой стенд (domwatch-test.js).
const scans = {};
global.window = {
  __CLAUDE_CUSTOM_CONFIG__: {},
  __claudeDomWatch: {
    register(name, fn) { scans[name] = fn; },
    kick() {},
  },
};

/* ---------- фейковый сервер ---------- */

let serverPlaying = false;
let fetchBroken = false;
let stopRequests = 0;

global.fetch = (url, opts) => {
  if (fetchBroken) return Promise.reject(new Error('сервер недоступен'));
  const u = String(url);
  if (u.indexOf('/limit-reset-alert') >= 0 && opts && opts.method === 'POST') {
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

const BTN_CLASS = 'claude-limit-stop-btn';
const btnIn = (c) => c.querySelector('.' + BTN_CLASS);

async function run() {
  check('скан зарегистрирован под своим именем',
    typeof scans['limit-stop'] === 'function');

  /* --- покой: звука нет, кнопки нет (и после первого опроса) */
  await flush();
  check('без звука кнопки нет', !btnIn(containers[0]));

  /* --- звук начался: кнопка появилась, помечена и одна */
  serverPlaying = true;
  advance(1000);
  await flush();
  const btn = btnIn(containers[0]);
  check('звук играет — кнопка появилась', !!btn);
  check('кнопка в футере',
    !!btn && btn.parentNode.classList.contains('inputFooter_def456'));
  check('кнопка помечена __claudeOwnNode', !!btn && btn.__claudeOwnNode === true);
  check('кнопка взяла класс донора',
    !!btn && /footerButton_/.test(btn.className) && btn.classList.contains(BTN_CLASS));

  /* --- повторные опросы не плодят копий */
  advance(1000);
  advance(1000);
  await flush();
  let count = 0;
  containers[0].children.forEach((child) => {
    child.children && child.children.forEach((g) => { if (g.classList && g.classList.contains(BTN_CLASS)) count++; });
  });
  check('повторные опросы не дублируют кнопку', count === 1, `найдено ${count}`);

  /* --- клик: POST ушёл, звук погашен, кнопка ушла сама */
  btn.click();
  await flush();
  check('клик отправил POST /limit-reset-alert-stop', stopRequests === 1,
    `запросов ${stopRequests}`);
  check('после клика кнопка исчезла', !btnIn(containers[0]));

  /* --- звук кончился сам (длинный файл доиграл): кнопка уходит */
  serverPlaying = true;
  advance(1000);
  await flush();
  check('новый звук — кнопка вернулась', !!btnIn(containers[0]));
  serverPlaying = false;
  advance(1000);
  await flush();
  check('звук кончился — кнопка ушла сама', !btnIn(containers[0]));

  /* --- React пересоздал футер во время звука: скан вставит кнопку */
  serverPlaying = true;
  advance(1000);
  await flush();
  containers[0] = makeContainer(); // старый контейнер выброшен
  scans['limit-stop']();           // подстраховочный обход наблюдателя
  check('пересозданный футер получил кнопку', !!btnIn(containers[0]));

  /* --- сервер замолчал: три промаха убирают кнопку */
  fetchBroken = true;
  advance(1000);
  advance(1000);
  await flush();
  check('после двух промахов кнопка ещё есть (не мигает)', !!btnIn(containers[0]));
  advance(1000);
  await flush();
  check('третий промах — кнопка убрана', !btnIn(containers[0]));

  /* --- возврат сервера: всё работает снова */
  fetchBroken = false;
  serverPlaying = true;
  advance(1000);
  await flush();
  check('сервер ожил — кнопка вернулась', !!btnIn(containers[0]));

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
