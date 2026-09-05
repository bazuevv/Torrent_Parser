/*
 * Контракт разделения Accs и Usage в живом claude-custom.js.
 * Accs показывает аккаунт, endpoint и лимиты; модель, effort и токены
 * последнего хода принадлежат только Usage.
 *
 * Запуск: node tmp/accs-display-test.js
 */
'use strict';
const fs = require('fs');
const path = require('path');

const SRC = path.join(__dirname, '..', '.claude', 'patches', 'claude-custom.js');
const source = fs.readFileSync(SRC, 'utf8');

function between(from, to) {
  const start = source.indexOf(from);
  const end = source.indexOf(to, start + from.length);
  if (start < 0 || end < 0) throw new Error('не найден блок: ' + from);
  return source.slice(start, end);
}

let passed = 0;
function check(condition, message) {
  if (!condition) throw new Error(message);
  passed += 1;
}

const subtitleSource = between('  function accountSubtitle(', '  /** «Pro (почта)»');
eval(subtitleSource + '\nglobal.__accountSubtitle = accountSubtitle;');

check(global.__accountSubtitle({
  provider: 'openai', plan: 'Plus', email: 'user@example.test',
  baseUrl: 'http://localhost', model: 'gpt-5.6-sol',
  runtime: { effort: 'high' },
}) === '', 'OpenAI subtitle не должен показывать модель или effort');
check(global.__accountSubtitle({
  provider: 'anthropic', oauth: false, baseUrl: 'https://provider.test',
  model: 'claude-opus-test',
}) === 'https://provider.test', 'у провайдера должен остаться только endpoint');

const accs = between(' * ACCOUNT SWITCHER BUTTON', ' * MOOD GAUGE');
check(!accs.includes('function openaiRuntimeBlock'), 'Accs всё ещё содержит runtime-блок');
check(!accs.includes("['кэш', last.cached_input_tokens]"), 'Accs всё ещё выводит кэш');
check(!accs.includes("['reasoning', last.reasoning_output_tokens]"), 'Accs всё ещё выводит reasoning');
check(source.includes("' · ' + d.effort"), 'Usage не выводит значение effort после модели');
check(!source.includes("' · усилие ' + d.effort"), 'Usage всё ещё подписывает effort словом «усилие»');

console.log('accs-display: ' + passed + '/' + passed + ' checks passed');
