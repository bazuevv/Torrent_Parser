/* Агент spy-режима Chaturbate для player.html.

   Кука sessionid у Chaturbate HttpOnly — вытащить её из вкладки и отдать
   плееру нельзя, поэтому все платные запросы (вход в spy, остановка,
   получение приватного HLS) делает эта закладка прямо на chaturbate.com,
   где сессия уже есть.

   Связь с плеером — через вкладку-мост cb-bridge.html: CSP сайта
   (connect-src 'self' …) запрещает странице любые запросы к плееру, но
   не запрещает window.open и postMessage. Закладка открывает мост
   именованным окном и переправляет ему команды и результаты; мост сам
   ходит на сервер плеера, будучи его же страницей.

   Запуск: на chaturbate.com, войдя в аккаунт, нажать закладку один раз —
   рядом откроется маленькая вкладка моста. Дальше агент живёт, пока
   открыты обе вкладки; команды приходят по postMessage раз в long-poll.
   Повторное нажатие закладки выключает. Состояние видно в плашке в углу
   страницы. */

/* Реверс SPA Chaturbate (бандлы web2.static.mmcdn.com/cachebust):
     вход в spy:  POST /tipping/spy_on_private_show_request/<room>/
                   chat_username, price, fan_club_price + X-CSRFToken
     выход:       POST /tipping/private_show_cancel/<room>/
                   understands_minimum_charge=true
     поток:       POST /get_edge_hls_url_ajax/ (room_slug, bandwidth)
                   — при активном spy начинает отдавать приватный url;
                   запасной источник — hls_source в initialRoomDossier. */
(() => {
  const PLAYER = '__PLAYER_ORIGIN__';
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
  let transport = null;               // окно-собеседник: плеер или вкладка-мост
  let transportReady = false;
  let helloTimer = 0;
  let helloDeadline = 0;
  const off = () => {
    stopped = true;
    clearInterval(helloTimer);
    badge.remove();
    delete window.__cbAgent;
    /* Ни плеер, ни мост не закрываем: следующий запуск найдёт их же. */
  };
  badge.onclick = off;
  document.body.appendChild(badge);
  window.__cbAgent = { badge, off };

  /* ---- связь с плеером ----------------------------------------------------
     Если эту вкладку открыл сам плеер (кнопка spy), говорим прямо с ним —
     он же origin сервера и держит long-poll. Иначе (вкладку открыли сами)
     открываем вкладку-мост cb-bridge.html: CSP сайта запрещает нам ходить
     на плеер fetch'ем, но не запрещает postMessage. */

  const toHub = payload => {
    if (transport && !transport.closed) {
      try { transport.postMessage(payload, PLAYER); return true; } catch (e) {}
    }
    return false;
  };

  /* На странице комнаты имя есть в initialRoomDossier, но плеер открывает
     главную Chaturbate, где dossier отсутствует. Общая SPA-шапка кладёт
     текущий аккаунт в $reactAppContext.logged_in_user. В зависимости от
     версии фронтенда это либо объект, либо строка вида
     "{username: name, is_supporter: false, ...}" — это не JSON-объект. */
  const accountName = value => {
    if (!value) return '';
    if (typeof value === 'object') return String(value.username || '');
    const text = String(value);
    try {
      const parsed = JSON.parse(text);
      if (parsed !== value) {
        const found = accountName(parsed);
        if (found) return found;
      }
    } catch (e) { /* строковое представление ниже */ }
    const found = /["']?username["']?\s*:\s*["']?([A-Za-z0-9_-]+)/i.exec(text);
    return found ? found[1] : '';
  };

  const viewerName = () => {
    const dossier = localDossier() || {};
    if (dossier.viewer_username) return String(dossier.viewer_username);
    const reactAccount = window.$reactAppContext &&
                         window.$reactAppContext.logged_in_user;
    const tsAccount = window.tsInstance && window.tsInstance.logged_in_user;
    const name = accountName(reactAccount) || accountName(tsAccount);
    if (name) return name;
    /* Имя нужно серверу только как признак готовой авторизованной вкладки;
       фактическое имя и баланс перед платным входом всё равно заново берутся
       из dossier комнаты. Меню профиля — устойчивый резервный признак входа. */
    return document.querySelector('[data-testid="user-header-menu"]')
      ? 'authenticated' : '';
  };

  const hello = () => {
    const msg = { v: 1, kind: 'hello',
                  username: viewerName() };
    toHub(msg);
    if (window.opener && window.opener !== transport && !window.opener.closed) {
      try { window.opener.postMessage(msg, PLAYER); } catch (e) {}
    }
  };

  function startHelloRetry() {
    clearInterval(helloTimer);
    helloDeadline = Date.now() + 7000;   // нет ответа — переходим на мост
    helloTimer = setInterval(() => {
      if (stopped || transportReady) return clearInterval(helloTimer);
      hello();
      if (Date.now() > helloDeadline) {
        clearInterval(helloTimer);
        openBridge();
      }
    }, 1500);
  }

  function openBridge() {
    say('открываю вкладку-мост…');
    transportReady = false;
    transport = window.open(
      PLAYER + '/cb-bridge.html?cb=' + encodeURIComponent(location.origin),
      'bongaCbBridge');
    if (!transport) {
      say('браузер закрыл всплывающее окно — разрешите и нажмите закладку снова');
      return;
    }
    /* Мост мог быть открыт раньше и уже не помнить нас (перезагрузка его или
       нашей вкладки) — здороваемся, пока не придёт подтверждение. */
    clearInterval(helloTimer);
    helloTimer = setInterval(() => {
      if (stopped || transportReady) return clearInterval(helloTimer);
      hello();
    }, 1500);
  }

  window.addEventListener('message', event => {
    if (event.origin !== PLAYER) return;
    const data = event.data;
    if (!data || data.v !== 1) return;

    if (data.kind === 'ready') {
      /* Отвечает то окно, которому адресован hello/ping: плеер (если эта
         вкладка открыта из него и жива) либо мост. При смене собеседника
         прежнему говорим «отсоединяюсь», чтобы он не поллил сервер зря. */
      if (transport && event.source !== transport && !transport.closed) {
        try { transport.postMessage({ v: 1, kind: 'detach' }, PLAYER); } catch (e) {}
      }
      transport = event.source;
      transportReady = true;
      clearInterval(helloTimer);
      say('связь с плеером есть, жду команду');
      return;
    }
    if (transport && event.source !== transport) return;
    if (data.kind === 'poll') handleAnswerSafe(data.answer || {});
  });

  /* Хаб отличает живую закладку от живого окна: сайт перезагружает страницу
     при переходе между комнатами, и пока пинг молчит, хаб называет серверу
     пустое имя — агент считается неподключённым, платный вход не стартует.
     Пинг дублируется в opener: после перезагрузки плеера связь восстанав-
     ливается сама, как только плеер снова привяжет вкладку. */
  const pingHub = () => {
    const msg = { v: 1, kind: 'ping', username: viewerName() };
    toHub(msg);
    if (window.opener && window.opener !== transport && !window.opener.closed) {
      try { window.opener.postMessage(msg, PLAYER); } catch (e) {}
    }
  };
  setInterval(() => { if (!stopped) pingHub(); }, 10000);

  /* ---- запросы к сайту (same-origin, CSP их разрешает) ------------------- */

  async function post(path, data) {
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

  /* ---- данные комнаты ------------------------------------------------------ */

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

  const report = payload => toHub({ v: 1, kind: 'result', payload });

  /* ---- обработка poll-ответа моста ---------------------------------------- */

  let refreshAt = 0;
  let recovering = false;

  async function handleAnswerSafe(answer) {
    try {
      await handleAnswer(answer);
    } catch (e) {
      /* Ошибка внутри обработчика не должна выглядеть как молчание агента:
         сервер иначе откатил бы команду по таймауту. */
      say('сбой обработки: ' + ((e && e.message) || e));
    }
  }

  async function handleAnswer(answer) {
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
      report({ act: 'recovery', room: mine.room });
      return;
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
      report(out);
      return;                          // следующий poll-ответ придёт своим ходом
    }

    /* Команд нет: пока идёт spy, периодически подтверждаем поток. */
    if (spy.state === 'spying' && spy.room) {
      if (Date.now() - refreshAt >= REFRESH_MS) {
        refreshAt = Date.now();
        try {
          const s = await streamUrl(spy.room);
          report({ act: 'refresh', room: spy.room,
                   url: s.url, room_status: s.room_status });
        } catch (e) { /* скажется следующим кругом */ }
      }
      say(`spy ${spy.room} · ${agent.username || 'аккаунт'}`);
    } else if (agent.anon) {
      say('вкладка без входа — откройте аккаунт на сайте');
    } else {
      say(`${agent.username || 'готов'} · жду команду`);
    }
  }

  /* Если вкладку открыл плеер — говорим с ним напрямую; своя вкладка —
     сначала пробуем opener, при молчании переключаемся на мост. */
  if (window.opener && !window.opener.closed) {
    transport = window.opener;
    hello();
    startHelloRetry();
  } else {
    openBridge();
  }
})();
