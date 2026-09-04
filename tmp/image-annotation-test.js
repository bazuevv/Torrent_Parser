/*
 * Автономный стенд IMAGE ANNOTATION EDITOR из claude-custom.js.
 * Проверяет загрузку модуля, регистрацию в общем DOM WATCH, имена
 * результирующих файлов и Canvas-команды всех базовых инструментов.
 */
'use strict';
const fs = require('fs');
const path = require('path');

const SRC = path.join(__dirname, '..', '.claude', 'patches', 'claude-custom.js');
const lines = fs.readFileSync(SRC, 'utf8').split('\n');
const headIdx = lines.findIndex((line) => line.includes('IMAGE ANNOTATION EDITOR —'));
if (headIdx < 0) throw new Error('заголовок IMAGE ANNOTATION EDITOR не найден');
const startIdx = lines.findIndex((line, i) => i > headIdx && line === '(function () {');
const endIdx = lines.findIndex((line, i) => i > startIdx && line === '})();');
const block = lines.slice(startIdx, endIdx + 1).join('\n');

let registered = null;
function fakeElement(tag) {
  const classes = new Set();
  return {
    tagName: String(tag).toUpperCase(),
    children: [],
    dataset: {},
    style: { setProperty() {} },
    classList: {
      add(name) { classes.add(name); },
      toggle(name, enabled) { if (enabled) classes.add(name); else classes.delete(name); },
      contains(name) { return classes.has(name); },
    },
    setAttribute() {},
    addEventListener(type, fn) { this.listeners = this.listeners || {}; this.listeners[type] = fn; },
    appendChild(child) { this.children.push(child); child.parentNode = this; return child; },
    insertBefore(child) { this.children.unshift(child); child.parentNode = this; return child; },
  };
}
global.window = {
  __CLAUDE_CUSTOM_CONFIG__: { imageAnnotationEditor: true },
  __claudeDomWatch: {
    register(name, scan) { registered = { name, scan }; },
  },
};
global.document = {
  readyState: 'complete',
  addEventListener() {},
  removeEventListener() {},
  createElement: fakeElement,
  querySelectorAll() { return []; },
  querySelector() { return null; },
  body: { contains() { return true; } },
};

eval(block);

const results = [];
function check(name, condition, detail) {
  results.push({ name, ok: !!condition, detail: detail || '' });
}

check('модуль установлен', window.__claudeImageAnnotationInstalled === true);
check('скан зарегистрирован в общем DOM WATCH',
  registered && registered.name === 'image-annotation' && typeof registered.scan === 'function');
check('публичный отладочный вход доступен',
  window.__claudeImageAnnotation && typeof window.__claudeImageAnnotation.open === 'function');

const sourceImage = { src: 'data:image/png;base64,AAAA' };
const thumb = fakeElement('div');
thumb.querySelector = (selector) => {
  if (selector === 'img') return sourceImage;
  if (selector === '.claude-image-edit-btn') {
    return thumb.children.find((child) => child.className === 'claude-image-edit-btn') || null;
  }
  return null;
};
registered.scan({ imageAttachments: [thumb] });
check('на миниатюру добавлена одна кнопка редактирования',
  thumb.children.length === 1 && thumb.children[0].textContent === '✎');
check('повторный скан не дублирует кнопку', (() => {
  registered.scan({ imageAttachments: [thumb] });
  return thumb.children.length === 1;
})());

const api = window.__claudeImageAnnotation._test;
check('имя PNG добавляет суффикс', api.annotatedName('mockup.jpg') === 'mockup-annotated.png');
check('имя без расширения поддерживается', api.annotatedName('mockup') === 'mockup-annotated.png');

function fakeContext() {
  const calls = [];
  const ctx = { calls };
  for (const name of [
    'save', 'restore', 'beginPath', 'stroke', 'moveTo', 'lineTo',
    'rect', 'ellipse',
  ]) {
    ctx[name] = (...args) => calls.push([name, ...args]);
  }
  return ctx;
}

const brush = fakeContext();
api.drawAction(brush, {
  tool: 'brush', color: '#f00', width: 4,
  points: [{ x: 1, y: 2 }, { x: 3, y: 4 }, { x: 5, y: 6 }],
});
check('кисть строит ломаную',
  brush.calls.filter((call) => call[0] === 'lineTo').length === 2,
  JSON.stringify(brush.calls));

const line = fakeContext();
api.drawAction(line, { tool: 'line', start: { x: 1, y: 2 }, end: { x: 8, y: 9 } });
check('линия использует две точки',
  line.calls.some((call) => call[0] === 'moveTo' && call[1] === 1 && call[2] === 2)
    && line.calls.some((call) => call[0] === 'lineTo' && call[1] === 8 && call[2] === 9));

const rect = fakeContext();
api.drawAction(rect, { tool: 'rect', start: { x: 3, y: 5 }, end: { x: 13, y: 25 } });
check('прямоугольник получает размеры',
  rect.calls.some((call) => call[0] === 'rect' && call[1] === 3 && call[2] === 5
    && call[3] === 10 && call[4] === 20));

const ellipse = fakeContext();
api.drawAction(ellipse, { tool: 'ellipse', start: { x: 2, y: 4 }, end: { x: 10, y: 16 } });
check('эллипс получает центр и радиусы',
  ellipse.calls.some((call) => call[0] === 'ellipse' && call[1] === 6 && call[2] === 10
    && call[3] === 4 && call[4] === 6));

for (const item of [brush, line, rect, ellipse]) {
  check('рисование сохраняет и восстанавливает Canvas-контекст',
    item.calls[0][0] === 'save' && item.calls[item.calls.length - 1][0] === 'restore');
}

let failed = 0;
for (const result of results) {
  if (!result.ok) failed++;
  console.log((result.ok ? '  OK  ' : ' FAIL ') + result.name
    + (result.detail ? `  [${result.detail}]` : ''));
}
console.log(failed ? `\n${failed} проверок провалено` : `\nвсе ${results.length} проверок пройдены`);
process.exit(failed ? 1 : 0);
