/*
 * Стенд для модуля QUOTE FROM SELECTION из claude-custom.js.
 *
 * Блок вырезается из живого файла и прогоняется на фейковых
 * window/document с виртуальным временем. Проверяется то, ради чего
 * писалась правка: кнопка появляется только по ЗАВЕРШЕНИИ выделения,
 * а вставленная цитата оставляет каретку на следующей строке.
 *
 * Зачем стенд: проверять это в живом окне — Reload Window на каждую
 * догадку, а состояние здесь держится на флаге и таймере, то есть
 * ломается молча.
 */
'use strict';
const fs = require('fs');

const SRC = '/mnt/Projects/Torrent_Parser/.claude/patches/claude-custom.js';
const lines = fs.readFileSync(SRC, 'utf8').split('\n');

const headIdx = lines.findIndex((l) => l.includes('QUOTE FROM SELECTION'));
if (headIdx < 0) throw new Error('заголовок QUOTE FROM SELECTION не найден');
const startIdx = lines.findIndex((l, i) => i > headIdx && l === '(function () {');
const endIdx = lines.findIndex((l, i) => i > startIdx && l === '})();');
const block = lines.slice(startIdx, endIdx + 1).join('\n');

/* ---------- фейковое окружение ---------- */

let now = 0;
const timers = [];
let nextTimerId = 1;

global.setTimeout = (fn, delay) => {
  const t = { id: nextTimerId++, fn, next: now + (delay || 0) };
  timers.push(t);
  return t.id;
};
global.clearTimeout = (id) => {
  const i = timers.findIndex((t) => t.id === id);
  if (i >= 0) timers.splice(i, 1);
};

function advance(ms) {
  const until = now + ms;
  for (;;) {
    const due = timers.filter((t) => t.next <= until).sort((a, b) => a.next - b.next)[0];
    if (!due) break;
    now = due.next;
    clearTimeout(due.id);
    due.fn();
  }
  now = until;
}

/** Текущее выделение страницы. */
let selectionText = '';
let composerText = '';
let caretAtEnd = false;

const composer = {
  tagName: 'DIV',
  get textContent() { return composerText; },
  set textContent(v) { composerText = v; },
  focus() {},
  dispatchEvent() { return true; },
};

const listeners = {};
const appended = [];

global.document = {
  readyState: 'complete',
  body: { appendChild: (el) => appended.push(el) },
  addEventListener(type, fn) { (listeners[type] = listeners[type] || []).push(fn); },
  querySelector() { return composer; },
  createRange: () => ({
    selectNodeContents() {}, collapse() {},
    getBoundingClientRect: () => ({ right: 100, top: 50 }),
    cloneRange() { return this; },
  }),
  createElement: () => ({
    style: {}, classList: { add() {}, remove() {} },
    addEventListener(type, fn) { this['on' + type] = fn; },
    contains: () => false,
    offsetHeight: 36,
  }),
  // Вся вставка идёт через execCommand — его аргумент и проверяем.
  execCommand(cmd, ui, value) {
    if (cmd !== 'insertText') return false;
    composerText += value;
    caretAtEnd = true;
    lastInserted = value;
    return true;
  },
};

let lastInserted = null;

global.window = {
  __CLAUDE_CUSTOM_CONFIG__: { quoteFromSelection: true, logs: false },
  getSelection: () => ({
    toString: () => selectionText,
    rangeCount: selectionText ? 1 : 0,
    getRangeAt: () => document.createRange(),
    removeAllRanges() {}, addRange() {},
  }),
};
global.InputEvent = class {};

/* ---------- прогон ---------- */

eval(block);

function fire(type, target) {
  (listeners[type] || []).forEach((fn) => fn({ target: target || {}, preventDefault() {}, stopPropagation() {} }));
}

// Кнопка создаётся лениво, при первом показе, поэтому берём её
// из body каждый раз заново, а не один раз в начале.
const btn = () => appended[0] || null;
const visible = () => !!(btn() && btn().style.display === 'block');

const results = [];
function check(name, cond, detail) {
  results.push({ name, ok: !!cond, detail: detail || '' });
}

/* --- сценарий 1: протаскивание мышью.
 * Во время выделения кнопки быть не должно ни на одном шаге. */
selectionText = '';
fire('mousedown');
let shownDuringDrag = false;
for (const partial of ['h', 'he', 'hel', 'hell', 'hello']) {
  selectionText = partial;
  fire('selectionchange');
  if (visible()) shownDuringDrag = true;
}
check('во время протаскивания кнопки нет', !shownDuringDrag);
check('после selectionchange кнопка всё ещё скрыта', !visible());

fire('mouseup');
check('после отпускания мыши кнопка появилась', visible());

/* --- сценарий 2: выделение с клавиатуры.
 * Момента «отпустили» нет, поэтому концом считается пауза. */
btn().style.display = 'none';
selectionText = '';
fire('selectionchange');           // сняли прошлое выделение
selectionText = 'a';
fire('selectionchange');
advance(200);
selectionText = 'ab';
fire('selectionchange');           // продолжаем выделять — таймер сбросился
advance(200);
check('во время набора Shift+стрелка кнопка не мигает', !visible());
advance(600);                      // пауза дольше KEYBOARD_SETTLE_MS
check('после паузы кнопка появилась', visible());

/* --- сценарий 3: снятие выделения гасит кнопку */
selectionText = '';
fire('selectionchange');
check('снятое выделение убирает кнопку', !visible());

/* --- сценарий 4: новое протаскивание гасит прежнюю кнопку */
selectionText = 'x';
fire('mouseup');                   // показали
check('кнопка показана перед новым выделением', visible());
fire('mousedown');                 // начали новое выделение
check('начало нового выделения гасит кнопку', !visible());

/* --- сценарий 5: вставка цитаты.
 * Главное здесь — перевод строки в конце: он и ставит каретку
 * в начало следующей строки. */
selectionText = 'hello\nworld';
fire('mouseup');
composerText = 'уже набранное';
lastInserted = null;
btn().onclick({ preventDefault() {}, stopPropagation() {} });

check('цитата вставлена', lastInserted !== null, String(lastInserted));
check('каждая строка процитирована',
  lastInserted && lastInserted.includes('> hello') && lastInserted.includes('> world'),
  JSON.stringify(lastInserted));
check('перед цитатой перевод строки (composer не пуст)',
  lastInserted && lastInserted.startsWith('\n'), JSON.stringify(lastInserted));
check('после цитаты перевод строки — каретка на следующей строке',
  lastInserted && lastInserted.endsWith('\n'), JSON.stringify(lastInserted));
check('кнопка спрятана после вставки', !visible());

/* --- сценарий 6: пустой composer не получает лишний перевод строки сверху */
selectionText = 'one';
fire('mouseup');
composerText = '';
lastInserted = null;
btn().onclick({ preventDefault() {}, stopPropagation() {} });
check('в пустой composer цитата идёт без ведущего перевода строки',
  lastInserted === '> one\n', JSON.stringify(lastInserted));

/* ---------- отчёт ---------- */
let failed = 0;
for (const r of results) {
  if (!r.ok) failed++;
  console.log((r.ok ? '  OK  ' : ' FAIL ') + r.name + (r.detail ? '  [' + r.detail + ']' : ''));
}
console.log(failed ? `\n${failed} проверок провалено` : `\nвсе ${results.length} проверок пройдены`);
process.exit(failed ? 1 : 0);
