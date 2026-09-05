/*
 * Стенд раскрывающейся строки попаданий в кэш из claude-custom.js.
 * Вырезает живые функции, чтобы проверять именно код webview-патча.
 *
 * Запуск: node tmp/cache-history-test.js
 */
'use strict';
const fs = require('fs');
const path = require('path');

const SRC = path.join(__dirname, '..', '.claude', 'patches', 'claude-custom.js');
const source = fs.readFileSync(SRC, 'utf8');

function cut(from, to) {
  const start = source.indexOf(from);
  const end = source.indexOf(to, start + from.length);
  if (start < 0 || end < 0) throw new Error('не найден блок: ' + from);
  return source.slice(start, end);
}

function element(tag) {
  const classes = new Set();
  const attrs = {};
  return {
    tagName: tag.toUpperCase(),
    children: [],
    textContent: '',
    title: '',
    hidden: false,
    handlers: {},
    classList: {
      add(name) { classes.add(name); },
      contains(name) { return classes.has(name); },
    },
    get className() { return Array.from(classes).join(' '); },
    set className(value) {
      classes.clear();
      String(value).split(/\s+/).filter(Boolean).forEach((name) => classes.add(name));
    },
    appendChild(child) { this.children.push(child); return child; },
    setAttribute(name, value) { attrs[name] = String(value); },
    getAttribute(name) { return Object.prototype.hasOwnProperty.call(attrs, name) ? attrs[name] : null; },
    addEventListener(name, handler) { this.handlers[name] = handler; },
  };
}

global.document = { createElement: element };
global.hhmm = (value) => String(value).slice(11, 16);
global.human = (value) => String(value);

const liveFunctions = [
  cut('  function row(', '  function renderError('),
  cut('  function shortModel(', '  /**\n   * История сессии:'),
  cut('  function hitRow(', '  /** Одна прежняя строка серии'),
  cut('  function hitDetailRow(', '  /** Строка промаха.'),
].join('\n');
eval(liveFunctions + '\nglobal.__cacheHistoryHitRow = hitRow;');

let passed = 0;
function check(condition, message) {
  if (!condition) throw new Error(message);
  passed += 1;
}

const group = global.__cacheHistoryHitRow({
  ts: '2026-09-05T20:53:00+00:00',
  started_ts: '2026-09-05T14:37:00+00:00',
  count: 61,
  read: 12600,
  models: ['glm-5.3', 'gpt-5.6-sol'],
  details: [
    { ts: '2026-09-05T14:37:00+00:00', started_ts: '2026-09-05T14:30:00+00:00', count: 20, read: 1700, model: 'glm-5.3' },
    { ts: '2026-09-05T14:45:00+00:00', started_ts: '2026-09-05T14:40:00+00:00', count: 41, read: 10900, model: 'gpt-5.6-sol' },
  ],
});

const summary = group.children[0];
const details = group.children[1];
check(group.classList.contains('claude-cache-hit-group'), 'нет контейнера группы');
check(summary.getAttribute('role') === 'button', 'сводка не является кнопкой');
check(summary.getAttribute('tabindex') === '0', 'сводка недоступна с клавиатуры');
check(summary.getAttribute('aria-expanded') === 'false', 'неверное начальное aria-expanded');
check(summary.children[0].textContent.startsWith('▸ '), 'нет стрелки свёрнутого состояния');
check(details.hidden && details.children.length === 2, 'детали должны быть скрыты и содержать две серии');

summary.handlers.click();
check(!details.hidden, 'клик не раскрыл детали');
check(summary.getAttribute('aria-expanded') === 'true', 'aria-expanded не обновился');
check(summary.children[0].textContent.startsWith('▾ '), 'нет стрелки раскрытого состояния');
check(details.children[0].children[0].textContent.includes('× 20'), 'потерян счётчик первой серии');
check(details.children[1].children[1].textContent === 'прочитано 10900', 'потерян объём второй серии');

let prevented = false;
summary.handlers.keydown({ key: ' ', preventDefault() { prevented = true; } });
check(prevented && details.hidden, 'Space не свернул подробности');

console.log('cache-history: ' + passed + '/' + passed + ' checks passed');
