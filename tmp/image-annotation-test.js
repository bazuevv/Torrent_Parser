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

global.DataTransfer = class DataTransfer {
  constructor() {
    const files = [];
    this.files = files;
    this.items = { add(file) { files.push(file); } };
  }
};
global.ClipboardEvent = class ClipboardEvent {
  constructor(type, init) {
    this.type = type;
    this.bubbles = !!init.bubbles;
    this.cancelable = !!init.cancelable;
    this.clipboardData = init.clipboardData;
    this.defaultPrevented = false;
  }
  preventDefault() { this.defaultPrevented = true; }
};
global.Event = class Event {
  constructor(type, init) { this.type = type; this.bubbles = !!(init && init.bubbles); }
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

const sourceImage = fakeElement('img');
sourceImage.src = 'data:image/png;base64,AAAA';
sourceImage.getBoundingClientRect = () => ({ left: 0, top: 0, width: 200, height: 100 });
sourceImage.setPointerCapture = () => {};
const thumb = fakeElement('div');
thumb.querySelector = (selector) => {
  if (selector === 'img') return sourceImage;
  return null;
};
const preview = fakeElement('div');
preview.querySelector = (selector) => {
  if (selector === 'img[class*="previewImage_"]') return sourceImage;
  if (selector === '.claude-image-edit-btn') {
    return preview.children.find((child) => child.className === 'claude-image-edit-btn') || null;
  }
  return null;
};
registered.scan({ imageAttachments: [thumb], imagePreviews: [preview] });
check('на миниатюре кнопки редактирования больше нет', thumb.children.length === 0);
check('в preview добавлена одна кнопка редактирования',
  preview.children.filter((child) => child.textContent === '✎').length === 1);
check('в preview добавлено управление масштабом',
  preview.children.some((child) => child.className === 'claude-image-preview-zoom'));
check('повторный скан не дублирует кнопку', (() => {
  registered.scan({ imageAttachments: [thumb], imagePreviews: [preview] });
  return preview.children.filter((child) => child.textContent === '✎').length === 1
    && preview.children.filter((child) => child.className === 'claude-image-preview-zoom').length === 1;
})());
const previewEdit = preview.children.find((child) => child.textContent === '✎');
let unarmedClickStopped = false;
previewEdit.listeners.click({
  detail: 1,
  preventDefault() {},
  stopPropagation() { unarmedClickStopped = true; },
  stopImmediatePropagation() {},
});
check('click открытия миниатюры не переходит в редактор',
  unarmedClickStopped && previewEdit.listeners.pointerdown instanceof Function);

const api = window.__claudeImageAnnotation._test;
check('имя PNG добавляет суффикс', api.annotatedName('mockup.jpg') === 'mockup-annotated.png');
check('имя без расширения поддерживается', api.annotatedName('mockup') === 'mockup-annotated.png');
check('масштаб ограничен диапазоном 25–400%',
  api.clampZoom(0.01) === 0.25 && api.clampZoom(9) === 4);
check('колесо меняет масштаб в обе стороны',
  api.wheelZoom(1, -120) > 1 && api.wheelZoom(1, 120) < 1);
check('расстояние pinch считается по двум касаниям',
  api.touchDistance({ 1: { x: 0, y: 0 }, 2: { x: 3, y: 4 } }) === 5);

let wheelPrevented = false;
sourceImage.listeners.wheel({
  deltaY: -120, clientX: 100, clientY: 50,
  preventDefault() { wheelPrevented = true; }, stopPropagation() {},
});
check('колесо масштабирует штатный preview',
  wheelPrevented && /scale\(1\./.test(sourceImage.style.transform), sourceImage.style.transform);
const zoomFromTransform = () => Number(sourceImage.style.transform.match(/scale\(([\d.]+)/)[1]);
const beforePinch = zoomFromTransform();
const touchEvent = (id, x, y) => ({
  pointerType: 'touch', pointerId: id, clientX: x, clientY: y,
  preventDefault() {}, stopPropagation() {},
});
sourceImage.listeners.pointerdown(touchEvent(1, 80, 50));
sourceImage.listeners.pointerdown(touchEvent(2, 120, 50));
sourceImage.listeners.pointermove(touchEvent(2, 160, 50));
const afterPinch = zoomFromTransform();
check('pinch масштабирует штатный preview', afterPinch > beforePinch,
  `${beforePinch} → ${afterPinch}`);
sourceImage.listeners.pointerup(touchEvent(1, 80, 50));
sourceImage.listeners.pointerup(touchEvent(2, 160, 50));

let replacementAttached = false;
let originalRemoved = false;
const replacement = fakeElement('div');
const removeButton = { click() { originalRemoved = true; } };
const composer = {
  dispatchEvent(event) {
    check('новый файл передаётся композеру через paste',
      event.type === 'paste' && event.clipboardData.files[0].name === 'result.png');
    event.preventDefault();
    replacementAttached = true;
    return false;
  },
};
const composerHost = { querySelector() { return composer; } };
thumb.closest = () => composerHost;
const originalQuerySelector = thumb.querySelector;
thumb.querySelector = (selector) => {
  if (selector.includes('removeButton_')) return removeButton;
  return originalQuerySelector(selector);
};
document.querySelectorAll = (selector) => {
  if (selector.includes('attachedFilesContainer_')) {
    return replacementAttached ? [thumb, replacement] : [thumb];
  }
  return [];
};
let replaceError = 'callback не вызван';
api.replaceAttachment(
  { thumb, sourceUrl: sourceImage.src },
  { name: 'result.png', type: 'image/png' },
  (error) => { replaceError = error; },
);
check('замена вложения завершается без ошибки', replaceError === null,
  replaceError && replaceError.message ? replaceError.message : String(replaceError));
check('исходник удаляется только после появления результата',
  replacementAttached && originalRemoved);

const movableRect = {
  tool: 'rect', color: '#f00', width: 4,
  start: { x: 10, y: 20 }, end: { x: 50, y: 60 }, points: [{ x: 10, y: 20 }],
};
const movedRect = api.translateAction(movableRect, 7, -3);
check('перемещение сдвигает обе точки фигуры',
  movedRect.start.x === 17 && movedRect.start.y === 17
    && movedRect.end.x === 57 && movedRect.end.y === 57);
check('перемещение не мутирует исходное действие',
  movableRect.start.x === 10 && movableRect.end.y === 60);
check('прямоугольник выбирается по внутренней области',
  api.hitAction(movableRect, { x: 30, y: 40 }, 2));
check('точка вне прямоугольника не выбирает его',
  !api.hitAction(movableRect, { x: 80, y: 90 }, 2));
check('линию можно выбрать рядом со штрихом',
  api.hitAction({
    tool: 'line', width: 2,
    start: { x: 0, y: 0 }, end: { x: 100, y: 0 }, points: [],
  }, { x: 45, y: 4 }, 4));
check('кисть можно выбрать по любому сегменту',
  api.hitAction({
    tool: 'brush', width: 3,
    points: [{ x: 0, y: 0 }, { x: 20, y: 20 }, { x: 40, y: 0 }],
  }, { x: 30, y: 11 }, 2));
check('эллипс выбирается по внутренней области',
  api.hitAction({
    tool: 'ellipse', width: 2,
    start: { x: 10, y: 20 }, end: { x: 50, y: 60 }, points: [],
  }, { x: 30, y: 40 }, 2));
check('в панели объявлен инструмент выбора',
  block.includes("['select', '↖', 'Выбор и перемещение']"));
check('ПКМ всегда включает перемещение',
  block.includes("event.button === 2 || tool === 'select'"));
check('контекстное меню холста отключено',
  block.includes("canvas.addEventListener('contextmenu'"));
check('колесо подключено и к Canvas-редактору',
  block.includes("stage.addEventListener('wheel'")
    && block.includes("setEditorZoom(wheelZoom(editorZoom, event.deltaY)"));
check('pinch подключён и к Canvas-редактору',
  block.includes('pinch.zoom * touchDistance(touchPoints) / pinch.distance'));
check('перед редактором штатный preview закрывается',
  block.includes("button[class*=\"previewCloseButton_\"]")
    && block.includes('setTimeout(function () { openEditor(thumb); }, 0)'));

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
