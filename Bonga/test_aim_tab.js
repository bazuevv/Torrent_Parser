#!/usr/bin/env node
/* Проверка cbAimTab из player.html — правила, по которым плеер наводит
   вкладку Chaturbate на страницу нужной комнаты.

   Почему это вообще проверяется отдельно: присутствие в комнате держит
   оплаченный показ. Пока вкладка стояла на главной, сайт отзывал право на
   просмотр на тридцатой секунде (прогоны 30.08 — 30.1, 31.4 и 30.4 с), причём
   API отвечал success:true с пустым адресом. Ошибка здесь стоит токенов, а
   увидеть её можно только по деньгам, поэтому цена регрессии высокая.

   Функцию вырезаем из player.html по имени и выполняем в vm-контексте:
   поднимать ради неё девятитысячный файл с DOM нечем и незачем. Если
   разметку функции изменят так, что вырезка сломается, тест упадёт с явной
   ошибкой — это лучше, чем молча перестать что-либо проверять.

   Запуск: node Bonga/test_aim_tab.js */

'use strict';

const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const SOURCE = fs.readFileSync(path.join(__dirname, 'player.html'), 'utf8');

function cutFunction(name) {
  const at = SOURCE.indexOf(`function ${name}(`);
  if (at < 0) throw new Error(`в player.html нет функции ${name}`);
  let depth = 0;
  for (let i = SOURCE.indexOf('{', at); i < SOURCE.length; i++) {
    if (SOURCE[i] === '{') depth++;
    else if (SOURCE[i] === '}' && --depth === 0) return SOURCE.slice(at, i + 1);
  }
  throw new Error(`не нашёл конец функции ${name}`);
}

const CODE = cutFunction('cbAimTab');

/* Стенд: вкладка, закладка и заглушки всего, к чему функция тянется наружу. */
function stage({ room = '', agent = false, live = room !== '', blocked = false } = {}) {
  const opened = [];
  const said = [];
  const logged = [];
  let focused = 0;
  const tab = () => ({ closed: false, focus() { focused++; } });

  const box = {
    cbWindow: live ? tab() : null,
    cbWindowRoom: room,
    cbAgentName: agent ? 'adm211' : '',
    cbAgentVersion: agent ? 8 : 0,
    cbAgentSeen: agent ? Date.now() : 0,
    cbAgentFresh: () => Date.now() - box.cbAgentSeen < 20000,
    cbAgentReady: () => box.cbAgentFresh() && !!box.cbAgentName &&
                        box.cbAgentName !== 'AnonymousUser',
    logEvent: (tag, text) => logged.push(`${tag}: ${text}`),
    say: (text, kind) => said.push(`${kind || 'ok'}: ${text}`),
    t: (key, vars) => key + (vars ? ' ' + JSON.stringify(vars) : ''),
    window: {
      open(url, name) {
        opened.push({ url, name });
        return blocked ? null : tab();
      },
    },
  };
  vm.createContext(box);
  vm.runInContext(CODE, box);
  return {
    aim: who => box.cbAimTab(who),
    opened, said, logged,
    focus: () => focused,
    state: box,
  };
}

let ok = true;
const check = (title, pass, detail = '') => {
  if (!pass) ok = false;
  console.log(`  ${pass ? 'OK  ' : 'ПРОВАЛ'} ${title}${detail ? ` — ${detail}` : ''}`);
};

/* 1. Вкладки нет: открываем сразу страницу комнаты, а не главную. */
{
  const s = stage();
  const wait = s.aim('Kitty');
  check('вкладки нет: вход отложен', wait === true, String(wait));
  check('вкладки нет: открыт адрес комнаты',
        s.opened.length === 1 && s.opened[0].url === 'https://ru.chaturbate.com/Kitty/',
        JSON.stringify(s.opened));
  check('вкладки нет: имя окна прежнее — вкладка не плодится',
        s.opened[0].name === 'bongaCbTab', s.opened[0].name);
  check('вкладки нет: просим нажать закладку',
        s.said.some(line => line.includes('spy.нажмитеЗакладку')), JSON.stringify(s.said));
}

/* 2. Вкладка стоит на другой комнате: переоткрываем её с новым адресом.
      Это и есть смысл всей затеи — присутствовать там, за что платим. */
{
  const s = stage({ room: 'lisa', agent: true });
  const wait = s.aim('kitty');
  check('другая комната: вход отложен', wait === true, String(wait));
  check('другая комната: вкладка переведена на новый адрес',
        s.opened.length === 1 && s.opened[0].url === 'https://ru.chaturbate.com/kitty/',
        JSON.stringify(s.opened));
  check('другая комната: журнал объясняет перевод',
        s.logged.some(line => /перевожу вкладку Chaturbate с lisa на комнату kitty/.test(line)),
        JSON.stringify(s.logged));
  check('другая комната: закладка забыта — переход её убил',
        s.state.cbAgentName === '' && s.state.cbAgentSeen === 0,
        `${s.state.cbAgentName}/${s.state.cbAgentSeen}`);
  check('другая комната: запомнили новую комнату',
        s.state.cbWindowRoom === 'kitty', s.state.cbWindowRoom);
}

/* 3. Вкладка на главной — тот же перевод, но в журнале это видно отдельно. */
{
  const s = stage({ room: '', live: true, agent: true });
  s.aim('kitty');
  check('с главной: вкладка переведена в комнату',
        s.opened.length === 1 && s.opened[0].url === 'https://ru.chaturbate.com/kitty/',
        JSON.stringify(s.opened));
  check('с главной: журнал называет исходную страницу',
        s.logged.some(line => /с главной на комнату kitty/.test(line)),
        JSON.stringify(s.logged));
}

/* 4. Вкладка уже в нужной комнате и закладка жива — входим без переоткрытия. */
{
  const s = stage({ room: 'kitty', agent: true });
  const wait = s.aim('kitty');
  check('всё на месте: вход разрешён', wait === false, String(wait));
  check('всё на месте: вкладку не трогаем', s.opened.length === 0, JSON.stringify(s.opened));
  check('всё на месте: молчим', s.said.length === 0, JSON.stringify(s.said));
}

/* 5. Регистр имени комнаты сайту безразличен — перезагружать из-за него
      страницу нельзя: это стоило бы пользователю нажатия закладки впустую. */
{
  const s = stage({ room: 'kitty', agent: true });
  const wait = s.aim('KiTTy');
  check('другой регистр: вход разрешён', wait === false, String(wait));
  check('другой регистр: вкладку не переоткрываем', s.opened.length === 0,
        JSON.stringify(s.opened));
}

/* 6. Комната та, а закладку ещё не нажали: переоткрывать нечего, страница
      уже правильная — просто ждём hello. */
{
  const s = stage({ room: 'kitty', agent: false });
  const wait = s.aim('kitty');
  check('нет закладки: вход отложен', wait === true, String(wait));
  check('нет закладки: страницу не перезагружаем', s.opened.length === 0,
        JSON.stringify(s.opened));
  check('нет закладки: вкладку показываем пользователю', s.focus() === 1,
        String(s.focus()));
  check('нет закладки: просим её нажать',
        s.said.some(line => line.includes('spy.нажмитеЗакладку')), JSON.stringify(s.said));
}

/* 7. Всплывающие окна запрещены: без вкладки spy не заработает, и молчать
      об этом нельзя — иначе кнопка выглядит сломанной. */
{
  const s = stage({ blocked: true });
  const wait = s.aim('kitty');
  check('окна запрещены: вход отложен', wait === true, String(wait));
  check('окна запрещены: сказали пользователю',
        s.said.some(line => line.startsWith('err: spy.вкладкаНеОткрылась')),
        JSON.stringify(s.said));
}

/* 8. Пользователь закрыл вкладку сам — открываем заново. */
{
  const s = stage({ room: 'kitty', agent: true });
  s.state.cbWindow.closed = true;
  const wait = s.aim('kitty');
  check('вкладка закрыта: вход отложен', wait === true, String(wait));
  check('вкладка закрыта: открыли заново на той же комнате',
        s.opened.length === 1 && s.opened[0].url === 'https://ru.chaturbate.com/kitty/',
        JSON.stringify(s.opened));
}

console.log('\nИТОГ:', ok ? 'всё сошлось' : 'ЕСТЬ ПРОВАЛЫ');
process.exit(ok ? 0 : 1);
