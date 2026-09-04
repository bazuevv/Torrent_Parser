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

const path = require('path');
// Путь выводим от самого стенда, а не пишем абсолютным: проект уже
// однажды переезжал (на NVMe), и зашитый путь пережил бы переезд
// молча — стенд читал бы старую копию файла либо падал.
const SRC = path.join(__dirname, '..', 'patches', 'claude-custom.js');
const lines = fs.readFileSync(SRC, 'utf8').split('\n');

/** Вырезает IIFE-блок, следующий за заголовком с указанным текстом. */
function cut(headText) {
  const head = lines.findIndex((l) => l.includes(headText));
  if (head < 0) throw new Error('заголовок не найден: ' + headText);
  const start = lines.findIndex((l, i) => i > head && l === '(function () {');
  const end = lines.findIndex((l, i) => i > start && l === '})();');
  return lines.slice(start, end + 1).join('\n');
}

// Модуль цитирования вставляет текст через общий помощник, поэтому
// стенд поднимает оба блока — иначе проверялась бы заглушка, а не то,
// что работает в окне.
const composerBlock = cut('COMPOSER TEXT — чтение и вставка');
const block = cut('QUOTE FROM SELECTION');

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
  /* Эмуляция редактора. Ключевой момент, ради которого стенд и
   * существует: Chromium НЕ делает разрыва из `\n` внутри insertText —
   * именно поэтому многострочная цитата склеивалась в одну строку.
   * Здесь это воспроизведено буквально: перенос внутри insertText
   * молча теряется, а строку рвёт только insertLineBreak. */
  execCommand(cmd, ui, value) {
    if (cmd === 'insertLineBreak') {
      composerText += '\n';
      commands.push('break');
      return true;
    }
    if (cmd !== 'insertText') return false;
    composerText += String(value).replace(/\n/g, '');
    commands.push('text:' + value);
    caretAtEnd = true;
    return true;
  },
};

const commands = [];

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

eval(composerBlock);   // общий помощник — до модуля, как и в bootstrap
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

/* --- сценарий 5: вставка многострочной цитаты.
 * Проверяем не аргумент одного вызова, а то, что ОКАЗАЛОСЬ в поле:
 * ровно здесь ломалось раньше — текст вставлялся целиком, `\n` внутри
 * него терялся, и три строки склеивались в одну. */
selectionText = 'hello\nworld';
fire('mouseup');
composerText = 'уже набранное';
commands.length = 0;
btn().onclick({ preventDefault() {}, stopPropagation() {} });

check('цитата вставлена', commands.length > 0, commands.join(' | '));
check('каждая строка процитирована',
  composerText.includes('> hello') && composerText.includes('> world'),
  JSON.stringify(composerText));
check('строки цитаты РАЗДЕЛЕНЫ переносами, а не склеены',
  composerText === 'уже набранное\n> hello\n> world\n',
  JSON.stringify(composerText));
check('переносы сделаны insertLineBreak, а не \\n внутри текста',
  commands.filter((c) => c === 'break').length === 3,
  commands.join(' | '));
check('после цитаты каретка на следующей строке',
  composerText.endsWith('\n'), JSON.stringify(composerText));
check('кнопка спрятана после вставки', !visible());

/* --- сценарий 6: пустой composer не получает лишний перевод строки сверху */
selectionText = 'one';
fire('mouseup');
composerText = '';
commands.length = 0;
btn().onclick({ preventDefault() {}, stopPropagation() {} });
check('в пустой composer цитата идёт без ведущего перевода строки',
  composerText === '> one\n', JSON.stringify(composerText));

/* --- сценарий 7: два соседних абзаца страницы.
 * Selection API отдаёт их разделёнными пустой строкой — так
 * сериализуются блочные элементы. Для пользователя это две строки
 * подряд, и разделитель в цитате не нужен: он давал осиротевший
 * `> ` между строками и цитату разреженнее оригинала. */
selectionText = 'Работает как задумано.\n\nТекущая сессия зафиксировала переход';
fire('mouseup');
composerText = '';
commands.length = 0;
btn().onclick({ preventDefault() {}, stopPropagation() {} });
check('два абзаца дают две строки цитаты, без пустой между ними',
  composerText === '> Работает как задумано.\n> Текущая сессия зафиксировала переход\n',
  JSON.stringify(composerText));

/* --- сценарий 8: несколько пустых строк подряд тоже исчезают */
selectionText = 'первая\n\n\n\nвторая';
fire('mouseup');
composerText = '';
btn().onclick({ preventDefault() {}, stopPropagation() {} });
check('подряд идущие пустые строки не оставляют следов',
  composerText === '> первая\n> вторая\n', JSON.stringify(composerText));

/* --- сценарий 9: выделены одни пробелы — вставлять нечего.
 * Пара переводов строки была бы мусором по нажатию, которое
 * пользователь считает безрезультатным. */
selectionText = '   \n \n';
fire('mouseup');
composerText = 'было';
btn().onclick({ preventDefault() {}, stopPropagation() {} });
check('пустое выделение ничего не вставляет',
  composerText === 'было', JSON.stringify(composerText));

/* --- сценарий 10: отступы внутри строк не трогаем.
 * Выбрасываются только целиком пустые строки, а ведущие пробелы
 * кода — часть содержимого. */
selectionText = 'def f():\n    return 1';
fire('mouseup');
composerText = '';
btn().onclick({ preventDefault() {}, stopPropagation() {} });
check('отступы в непустых строках сохранены',
  composerText === '> def f():\n>     return 1\n', JSON.stringify(composerText));

/* ---------- отчёт ---------- */
let failed = 0;
for (const r of results) {
  if (!r.ok) failed++;
  console.log((r.ok ? '  OK  ' : ' FAIL ') + r.name + (r.detail ? '  [' + r.detail + ']' : ''));
}
console.log(failed ? `\n${failed} проверок провалено` : `\nвсе ${results.length} проверок пройдены`);
process.exit(failed ? 1 : 0);
