/* Агент spy-режима Chaturbate для player.html.

   Кука sessionid у Chaturbate HttpOnly — вытащить её из вкладки и отдать
   плееру нельзя, поэтому все платные запросы (вход в spy, остановка,
   получение приватного HLS) делает эта закладка прямо на chaturbate.com,
   где сессия уже есть. Сервер плеера держит очередь команд и отдаёт её
   сюда long-poll'ом; результат уходит обратно.

   Запуск: на chaturbate.com, войдя в аккаунт, нажать закладку один раз.
   Дальше агент живёт, пока вкладка открыта: команд нет — он спит в повисшем
   запросе, команда появилась — исполняет её. Повторное нажатие выключает.
   Состояние видно в плашке в углу страницы.

   Адрес плеера подставляется в __PLAYER_ORIGIN__ при создании ссылки.
   localhost браузер из https-страницы пускает, а вот http://192.168.x —
   это mixed content; для доступа с других устройств нужен https или
   открытый по http сайт. */

/* Реверс SPA Chaturbate (бандлы web2.static.mmcdn.com/cachebust):
     вход в spy:  POST /tipping/spy_on_private_show_request/<room>/
                   chat_username, price, fan_club_price + X-CSRFToken
     выход:       POST /tipping/private_show_cancel/<room>/
                   understands_minimum_charge=true
     поток:       POST /get_edge_hls_url_ajax/ (room_slug, bandwidth)
                   — при активном spy начинает отдавать приватный url;
                   запасной источник — hls_source в initialRoomDossier. */
(async () => {
  const TARGETS = [...new Set(['__PLAYER_ORIGIN__', 'http://127.0.0.1:8777'])];
  const ID_KEY = 'cbAgentId';
  const SPY_KEY = 'cbSpy';            // «я в spy» — для recovery после рестарта сервера
  const REFRESH_MS = 45000;

  /* Второе нажатие закладки выключает агента. */
  if (window.__cbAgent) {
    window.__cbAgent.off();
    return;
  }

  const badge = document.createElement('div');
  badge.style.cssText = 'position:fixed;z-index:2147483647;right:12px;bottom:12px;' +
    'padding:8px 12px;border-radius:8px;background:#1c1f26;color:#e6e8ec;' +
    'font:12px/1.4 system-ui,sans-serif;box-shadow:0 4px 16px rgba(0,0,0,.5);' +
    'cursor:pointer;max-width:320px';
  badge.title = 'Нажмите, чтобы выключить агента';
  const say = text => { badge.textContent = 'Агент CB: ' + text; };

  let stopped = false;
  const off = () => {
    stopped = true;
    badge.remove();
    delete window.__cbAgent;
  };
  badge.onclick = off;
  document.body.appendChild(badge);
  window.__cbAgent = { badge, off };

  const pause = ms => new Promise(r => setTimeout(r, ms));

  let agentId = '';
  try { agentId = localStorage.getItem(ID_KEY) || ''; } catch (e) { /* приватный режим */ }
  if (!agentId) {
    agentId = 'a' + Math.random().toString(36).slice(2, 10);
    try { localStorage.setItem(ID_KEY, agentId); } catch (e) { /* пусть живёт без id */ }
  }

  /* ---- связь с плеером ------------------------------------------------ */

  let home = null;
  async function findHome() {
    for (const base of TARGETS) {
      try {
        const res = await fetch(base + '/api/cb/spy');
        if (res.ok) { home = base; return true; }
      } catch (e) { /* плеер по этому адресу недоступен */ }
    }
    return false;
  }

  async function post(path, data) {
    /* Формы сайта уходят form-urlencoded с CSRF-заголовком; запросы
       same-origin, поэтому куки сессии летят сами. */
    const res = await fetch(path, {
      method: 'POST',
      credentials: 'same-origin',
      headers: {
        'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
        'X-Requested-With': 'XMLHttpRequest',
        'X-CSRFToken': (/(?:^|;\s*)csrftoken=([A-Za-z0-9_-]+)/.exec(document.cookie) || [])[1] || '',
      },
      body: new URLSearchParams(data).toString(),
    });
    return { status: res.status, text: await res.text() };
  }

  /* Ответы tipping/* — не JSON (на сайте их парсит собственный класс),
     поэтому мягкий разбор: сначала JSON, потом key=value. */
  function softParse(text) {
    try { return JSON.parse(text); } catch (e) { /* не JSON — ниже */ }
    const out = {};
    for (const part of String(text).split('&')) {
      const eq = part.indexOf('=');
      if (eq <= 0) continue;
      out[part.slice(0, eq)] = decodeURIComponent(part.slice(eq + 1).replace(/\+/g, ' '));
    }
    return out;
  }
  const isTrue = v => v === true || v === 'true' || v === 'True' || v === 1;

  /* ---- данные комнаты -------------------------------------------------- */

  function localDossier() {
    const d = window.initialRoomDossier;   // на странице комнаты уже распарсен сайтом
    if (d && typeof d === 'object') return d;
    if (typeof d === 'string') { try { return JSON.parse(d); } catch (e) { return null; } }
    return null;
  }

  function parseDossier(html) {
    const m = /window\.initialRoomDossier\s*=\s*"(\{.*?\})"\s*;/.exec(html);
    if (!m) return null;
    try {
      return JSON.parse(m[1].replace(/\\u([0-9a-fA-F]{4})/g,
        (_, h) => String.fromCharCode(parseInt(h, 16))));
    } catch (e) { return null; }
  }

  async function roomDossier(room) {
    const own = localDossier();
    if (own && String(own.broadcaster_username || '').toLowerCase() === room.toLowerCase())
      return own;
    try {
      const res = await fetch('/' + room + '/', { credentials: 'same-origin' });
      if (!res.ok) return null;
      return parseDossier(await res.text());
    } catch (e) { return null; }
  }

  async function streamUrl(room) {
    try {
      const r = await post('/get_edge_hls_url_ajax/', { room_slug: room, bandwidth: 'high' });
      const data = softParse(r.text);
      if (data && typeof data.url === 'string' && data.url.startsWith('https://'))
        return { url: data.url, room_status: String(data.room_status || '') };
    } catch (e) { /* попробуем dossier */ }
    const d = await roomDossier(room);
    if (d && typeof d.hls_source === 'string' && d.hls_source.startsWith('https://'))
      return { url: d.hls_source, room_status: String(d.room_status || 'private') };
    return { url: '', room_status: '' };
  }

  /* ---- команды ---------------------------------------------------------- */

  async function doStart(cmd) {
    const d = await roomDossier(cmd.room);
    if (!d) return { ok: false, error: 'страница комнаты недоступна' };
    const viewer = String(d.viewer_username || '');
    if (!viewer || viewer === 'AnonymousUser')
      return { ok: false, error: 'вкладка не авторизована — войдите в аккаунт' };
    if (String(d.room_status || '') !== 'private')
      return { ok: false, error: `комната не в привате (${d.room_status || '?'})` };
    const price = Number(d.spy_private_show_price) || 0;
    if (!price) return { ok: false, error: 'комната не называет цену spy' };
    const balance = Number(d.token_balance) || 0;
    if (balance < price)
      return { ok: false, error: `не хватает токенов: ${balance} < ${price}/мин` };

    const r = await post(`/tipping/spy_on_private_show_request/${cmd.room}/`, {
      chat_username: viewer,
      price: String(price),
      fan_club_price: String(price),
    });
    const data = softParse(r.text);
    if (!isTrue(data && data.success))
      return { ok: false, error: `сайт отклонил вход (HTTP ${r.status})`,
               raw: r.text.slice(0, 2000) };

    const s = await streamUrl(cmd.room);
    try { localStorage.setItem(SPY_KEY, JSON.stringify({ room: cmd.room, at: Date.now() })); } catch (e) {}
    return { ok: true, url: s.url, room_status: s.room_status, price,
             raw: r.text.slice(0, 2000) };
  }

  async function doStop(cmd) {
    const r = await post(`/tipping/private_show_cancel/${cmd.room}/`, {
      understands_minimum_charge: 'true',
    });
    const data = softParse(r.text);
    const gone = /not (?:currently )?(?:in|watching)|no active|nothing to cancel/i.test(r.text);
    try { localStorage.removeItem(SPY_KEY); } catch (e) {}
    if (isTrue(data && data.success) || gone)
      return { ok: true, raw: r.text.slice(0, 500) };
    return { ok: false, error: `сайт отклонил остановку (HTTP ${r.status})`,
             raw: r.text.slice(0, 1000) };
  }

  async function doGetUrl(cmd) {
    const s = await streamUrl(cmd.room);
    return { ok: !!s.url, url: s.url, room_status: s.room_status };
  }

  async function report(payload) {
    for (let attempt = 0; attempt < 3 && !stopped; attempt++) {
      try {
        const res = await fetch(home + '/api/cb/agent/result', {
          method: 'POST',
          headers: { 'Content-Type': 'text/plain;charset=UTF-8' },   // без preflight
          body: JSON.stringify(payload),
        });
        if (res.ok) return true;
      } catch (e) { /* сеть моргнула — подождём и повторим */ }
      await pause(2000 + Math.random() * 3000);
    }
    return false;
  }

  /* ---- главный цикл ------------------------------------------------------ */

  let refreshAt = 0;
  let recovering = false;

  while (!stopped) {
    if (!home && !(await findHome())) {
      say('плеер не отвечает');
      await pause(5000);
      continue;
    }

    let answer = null;
    try {
      const me = localDossier() || {};
      const res = await fetch(home + '/api/cb/agent/poll', {
        method: 'POST',
        headers: { 'Content-Type': 'text/plain;charset=UTF-8' },
        body: JSON.stringify({ id: agentId,
                               username: String(me.viewer_username || ''),
                               busy: false }),
      });
      answer = await res.json();
    } catch (e) {
      say('нет связи с плеером');
      home = null;                       // плеер перезапустился или исчез
      await pause(3000 + Math.random() * 5000);
      continue;
    }

    const spy = (answer.spy && answer.spy.spy) || {};
    const agent = (answer.spy && answer.spy.agent) || {};

    /* Вкладка считает себя в spy, а сервер — нет (рестарт плеера, потеря
       результата): списание могло продолжиться, orphan всегда останавливаем. */
    let mine = null;
    try { mine = JSON.parse(localStorage.getItem(SPY_KEY) || 'null'); } catch (e) {}
    if (mine && mine.room && !recovering &&
        (spy.state === 'idle' ||
         (spy.room && spy.room.toLowerCase() !== String(mine.room).toLowerCase()))) {
      recovering = true;
      await report({ act: 'recovery', room: mine.room });
      continue;
    }
    if (spy.state === 'idle' && !mine) recovering = false;

    const cmd = answer.cmd;
    if (cmd) {
      say(`${cmd.act} → ${cmd.room}…`);
      let out;
      try {
        if (cmd.act === 'spy_start') out = await doStart(cmd);
        else if (cmd.act === 'spy_stop') out = await doStop(cmd);
        else if (cmd.act === 'url_get') out = await doGetUrl(cmd);
        else out = { ok: false, error: 'неизвестная команда' };
      } catch (e) {
        out = { ok: false, error: String((e && e.message) || e) };
      }
      out.id = cmd.id;
      out.act = cmd.act;
      out.room = cmd.room;
      await report(out);
      continue;                          // сразу за следующей командой
    }

    /* Команд нет: пока идёт spy, периодически подтверждаем поток. */
    if (spy.state === 'spying' && spy.room) {
      if (Date.now() - refreshAt >= REFRESH_MS) {
        refreshAt = Date.now();
        try {
          const s = await streamUrl(spy.room);
          await report({ act: 'refresh', room: spy.room,
                         url: s.url, room_status: s.room_status });
        } catch (e) { /* скажется следующим кругом */ }
      }
      say(`spy ${spy.room} · ${agent.username || 'аккаунт'}`);
    } else if (agent.anon) {
      say('вкладка без входа — откройте аккаунт на сайте');
    } else {
      say(`${agent.username || 'готов'} · жду команду`);
    }
    /* Пустой poll-ответ пришёл — сразу висим в следующем: таймеров нет,
       поэтому фоновый троттлинг вкладки агенту не страшен. */
  }
})();
