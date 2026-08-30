#!/usr/bin/env node
/* Проверка, что javascript:-ссылка агента влезает в закладку браузера.

   Firefox обрезает URL закладки на 65536 символах; Chrome при перетаскивании
   тоже молча ничего не сохраняет, если href раздут. encodeURIComponent
   комментариев на кириллице как раз перешагнул этот порог на агенте v9
   (26 КБ исходника → 70 КБ href). compactBookmarklet выкидывает комментарии
   до кодирования.

   Запуск: node Bonga/test_bookmark_href.js */

'use strict';

const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { spawnSync } = require('node:child_process');

const ROOT = __dirname;
const PLAYER = fs.readFileSync(path.join(ROOT, 'player.html'), 'utf8');
const AGENT = fs.readFileSync(path.join(ROOT, 'cb-bookmarklet.js'), 'utf8');

function cutFunction(name) {
  const at = PLAYER.indexOf(`function ${name}(`);
  if (at < 0) throw new Error(`в player.html нет функции ${name}`);
  let depth = 0;
  for (let i = PLAYER.indexOf('{', at); i < PLAYER.length; i++) {
    if (PLAYER[i] === '{') depth++;
    else if (PLAYER[i] === '}' && --depth === 0) return PLAYER.slice(at, i + 1);
  }
  throw new Error(`не нашёл конец функции ${name}`);
}

const box = { location: { origin: 'http://127.0.0.1:8777' } };
vm.createContext(box);
vm.runInContext(cutFunction('compactBookmarklet') + '\n' + cutFunction('bookmarkHref'), box);

let ok = true;
const check = (title, pass, detail = '') => {
  if (!pass) ok = false;
  console.log(`  ${pass ? 'OK  ' : 'ПРОВАЛ'} ${title}${detail ? ` — ${detail}` : ''}`);
};

const LIMIT = 65536;
const rawHref = 'javascript:' + encodeURIComponent(
  AGENT.replace('__PLAYER_ORIGIN__', box.location.origin));
const href = box.bookmarkHref(AGENT);
const compact = box.compactBookmarklet(
  AGENT.replace('__PLAYER_ORIGIN__', box.location.origin));

check('без сжатия href длиннее лимита закладки — иначе чинить было нечего',
      rawHref.length > LIMIT, `${rawHref.length} символов`);
check('сжатый href короче лимита Firefox 65536',
      href.length < LIMIT, `${href.length} символов`);
check('сжатый href начинается с javascript:', href.startsWith('javascript:'));
check('https:// в коде не съеден как комментарий',
      compact.includes("startsWith('https://')"),
      compact.includes('https://') ? 'адрес на месте' : 'пропал');
check('адрес плеера подставлен', compact.includes('http://127.0.0.1:8777'));

const checked = spawnSync('node', ['--check'], { input: compact, encoding: 'utf8' });
check('сжатый код разбирается как JavaScript', checked.status === 0,
      (checked.stderr || '').trim().slice(0, 160));

console.log('\nИТОГ:', ok ? 'всё сошлось' : 'ЕСТЬ ПРОВАЛЫ');
process.exit(ok ? 0 : 1);
