/* Агент spy-режима Chaturbate для player.html.

   Кука sessionid у Chaturbate HttpOnly — вытащить её из вкладки и отдать
   плееру нельзя, поэтому все платные запросы (вход в spy, остановка,
   получение приватного HLS) делает эта закладка прямо на chaturbate.com,
   где сессия уже есть.

   Связь с плеером — postMessage в window.opener. CSP сайта
   (connect-src 'self' …) запрещает странице любые запросы к плееру, но
   postMessage не запрещает. Отсюда требование: вкладку Chaturbate должен
   открыть сам плеер — ссылку на его окно иначе не получить, поиск окна по
   имени работает лишь внутри своей группы вкладок.

   Запуск: нажать spy в плеере (он откроет вкладку), войти в аккаунт и
   нажать закладку один раз. Команды приходят по postMessage раз в
   long-poll. Повторное нажатие закладки выключает агента. Состояние видно
   в плашке в углу страницы. */

/* Реверс SPA Chaturbate (бандлы web2.static.mmcdn.com/cachebust):
     вход в spy:  POST /tipping/spy_on_private_show_request/<room>/
                   chat_username, price, fan_club_price + X-CSRFToken
     выход:       POST /tipping/private_show_cancel/<room>/
                   пустое тело для spy; understands_minimum_charge=true
                   только при подтверждённом минимальном времени private
     поток:       POST /get_edge_hls_url_ajax/ (room_slug, bandwidth)
                   — при активном spy начинает отдавать приватный url;
                   запасной источник — hls_source в initialRoomDossier. */
(() => {
  const PLAYER = '__PLAYER_ORIGIN__';
  const AGENT_VERSION = 7;
  const SPY_KEY = 'cbSpy';            // «я в spy» — для recovery после рестарта сервера
  const BALANCE_ROOM_KEY = 'cbBalanceRoom';
  /* 20 с, а не 45: доступ к потоку сайт закрывает на 30-й секунде после входа
     (прогоны 30.08 — 30.1 с и 31.4 с до сплошных 403), и при сорока пяти мы
     ни разу не успевали подтвердить поток до его смерти. */
  const REFRESH_MS = 20000;
  const BALANCE_MS = 9000;

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
  const badgeTitle = 'Нажмите, чтобы скопировать. Повторный запуск закладки выключает агента';
  badge.title = badgeTitle;
  const say = text => { badge.textContent = `Агент CB v${AGENT_VERSION}: ${text}`; };

  let stopped = false;
  let transport = null;               // окно плеера (оно же window.opener)
  let transportReady = false;
  let helloTimer = 0;
  let copyTimer = 0;
  const off = () => {
    stopped = true;
    clearInterval(helloTimer);
    clearTimeout(copyTimer);
    badge.remove();
    delete window.__cbAgent;
    /* Плеер не закрываем: следующий запуск найдёт его же. */
  };
  const copyBadge = async () => {
    const text = badge.textContent;
    let copied = false;
    try {
      if (!navigator.clipboard || !navigator.clipboard.writeText)
        throw new Error('Clipboard API недоступен');
      await navigator.clipboard.writeText(text);
      copied = true;
    } catch (e) {
      /* Старые браузеры или строгие разрешения Clipboard API: копируем
         тем же пользовательским кликом через временное текстовое поле. */
      const field = document.createElement('textarea');
      field.value = text;
      field.setAttribute('readonly', '');
      field.style.cssText = 'position:fixed;left:-9999px;top:0';
      document.body.appendChild(field);
      field.select();
      try { copied = document.execCommand('copy'); } catch (copyError) {}
      field.remove();
    }
    clearTimeout(copyTimer);
    badge.style.outline = copied ? '2px solid #4fd38a' : '2px solid #ff6b6b';
    badge.title = copied ? `Скопировано: ${text}` : 'Не удалось скопировать информацию';
    copyTimer = setTimeout(() => {
      badge.style.outline = '';
      badge.title = badgeTitle;
    }, 1400);
  };
  badge.onclick = copyBadge;
  document.body.appendChild(badge);
  window.__cbAgent = { badge, off };

  /* ---- связь с плеером ----------------------------------------------------
     Говорим с окном, которое открыло эту вкладку: это и есть плеер, он же
     origin сервера и держит long-poll. Другого пути нет — CSP сайта
     запрещает нам ходить на плеер fetch'ем, а postMessage требует ссылки на
     его окно, которую даёт только window.opener. */

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

  const hello = () => toHub({ v: 1, kind: 'hello',
                              username: viewerName(),
                              agent_version: AGENT_VERSION });

  /* Плеер мог ещё не догрузиться или перезагрузиться — здороваемся, пока не
     придёт подтверждение. Срока у попыток нет: уходить больше некуда. */
  function startHelloRetry() {
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
  const pingHub = () => toHub({ v: 1, kind: 'ping', username: viewerName(),
                                agent_version: AGENT_VERSION });
  setInterval(() => { if (!stopped) pingHub(); }, 10000);

  /* ---- запросы к сайту (same-origin, CSP их разрешает) ------------------- */

  /* У fetch нет своего таймаута, а сервер ждёт результат команды 15 секунд.
     Прецедент 30.08: запрос к сайту завис, агент промолчал, в журнале осталось
     «агент промолчал» без единой подробности, а reportBalance навсегда застрял
     с balanceBusy=true — цифры расхода замерли на минуту. Любой запрос обязан
     завершиться сам, пусть и ошибкой: она попадёт в raw и объяснит причину. */
  const SITE_TIMEOUT_MS = 8000;

  async function siteFetch(path, init, timeout = SITE_TIMEOUT_MS) {
    const ctl = new AbortController();
    const timer = setTimeout(() => ctl.abort(), timeout);
    try {
      return await fetch(path, { ...init, signal: ctl.signal });
    } finally { clearTimeout(timer); }
  }

  async function post(path, data) {
    const res = await siteFetch(path, {
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
  const stopDone = (data, text) => {
    if (isTrue(data && data.success)) return true;
    if (/not (?:currently )?(?:in|watching)|no active|nothing to cancel/i.test(text))
      return true;
    /* Когда приват завершает сама модель, cancel отвечает success:false,
       но одновременно сообщает, что оплачиваемого просмотра уже нет.
       Это успешная остановка для нашей машины состояний, а не повод навечно
       оставаться в stopping и повторять одну и ту же команду. */
    return !!data && isTrue(data.can_access) &&
      Number(data.remaining_seconds) === 0;
  };

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

  async function roomDossier(room, fresh = false) {
    const own = fresh ? null : localDossier();
    if (own && String(own.broadcaster_username || '').toLowerCase() === room.toLowerCase())
      return own;
    try {
      // Страница комнаты весит сотни килобайт, поэтому срок вдвое больше
      // обычного запроса — но он всё равно конечный.
      const res = await siteFetch('/' + room + '/?agent_balance=' + Date.now(), {
        credentials: 'same-origin', cache: 'no-store',
      }, SITE_TIMEOUT_MS * 2);
      if (!res.ok) return null;
      return parseDossier(await res.text());
    } catch (e) { return null; }
  }

  const dossierBalance = dossier => {
    if (!dossier || dossier.token_balance === undefined || dossier.token_balance === null)
      return null;
    const value = Number(dossier.token_balance);
    return Number.isFinite(value) && value >= 0 ? Math.floor(value) : null;
  };

  /* Возвращает ещё и raw — что именно ответил сайт. Без этого «агент не отдал
     адреса» в журнале сервера неотличимо от «сайт отдал адрес, а мы его не
     признали»: прецедент 30.08, показ умер на 403, а причина осталась
     неизвестной, потому что ответ Chaturbate никуда не записывался. */
  async function streamUrl(room) {
    let raw = '';
    try {
      const r = await post('/get_edge_hls_url_ajax/', { room_slug: room, bandwidth: 'high' });
      raw = `ajax HTTP ${r.status}: ${r.text.slice(0, 400)}`;
      const data = softParse(r.text);
      if (data && typeof data.url === 'string' && data.url.startsWith('https://'))
        return { url: data.url, room_status: String(data.room_status || ''), raw };
    } catch (e) {
      raw = `ajax не прошёл: ${(e && e.message) || e}`;
    }
    const d = await roomDossier(room);
    if (d && typeof d.hls_source === 'string' && d.hls_source.startsWith('https://'))
      return { url: d.hls_source, room_status: String(d.room_status || 'private'),
               raw: `${raw} · адрес взят из dossier` };
    return { url: '', room_status: String((d && d.room_status) || ''),
             raw: `${raw} · dossier ${d ? 'без hls_source' : 'недоступен'}` };
  }

  /* ---- команды ---------------------------------------------------------- */

  async function doStart(cmd) {
    const d = await roomDossier(cmd.room, true);
    if (!d) return { ok: false, error: 'страница комнаты недоступна' };
    const viewer = String(d.viewer_username || '');
    if (!viewer || viewer === 'AnonymousUser')
      return { ok: false, error: 'вкладка не авторизована — войдите в аккаунт' };
    if (String(d.room_status || '') !== 'private')
      return { ok: false, error: `комната не в привате (${d.room_status || '?'})` };
    const price = Number(d.spy_private_show_price) || 0;
    if (!price) return { ok: false, error: 'комната не называет цену spy' };
    const balance = dossierBalance(d) ?? 0;
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
    try { localStorage.setItem(BALANCE_ROOM_KEY, cmd.room); } catch (e) {}
    /* Контрольное чтение баланса сразу после входа. Первое списание сайт
       делает в момент входа, а рядовой опрос приходит раз в 9 секунд — показ
       успевает оборваться раньше, и тогда за что списали остаётся неясным.
       balance здесь — точка отсчёта расхода, balance_after — цена входа. */
    const after = dossierBalance(await roomDossier(cmd.room, true));
    return { ok: true, url: s.url, room_status: s.room_status, price, balance,
             balance_after: after,
             raw: `вход: ${r.text.slice(0, 800)} · поток: ${s.raw}` };
  }

  async function doStop(cmd) {
    /* Штатный leavePrivateOrSpyShow для PrivateSpying отправляет пустое тело.
       understands_minimum_charge относится к досрочному выходу зрителя из
       обычного private; с ним cancel spy отвечал success:false и не выходил. */
    let r = await post(`/tipping/private_show_cancel/${cmd.room}/`, {});
    let data = softParse(r.text);
    if (!stopDone(data, r.text) && Number(data.remaining_seconds) > 0) {
      r = await post(`/tipping/private_show_cancel/${cmd.room}/`, {
        understands_minimum_charge: 'true',
      });
      data = softParse(r.text);
    }
    try { localStorage.removeItem(SPY_KEY); } catch (e) {}
    if (stopDone(data, r.text))
      return { ok: true, raw: r.text.slice(0, 500) };
    return { ok: false, error: `сайт отклонил остановку (HTTP ${r.status})`,
             raw: r.text.slice(0, 1000) };
  }

  async function doGetUrl(cmd) {
    const s = await streamUrl(cmd.room);
    return { ok: !!s.url, url: s.url, room_status: s.room_status, raw: s.raw };
  }

  const report = payload => toHub({ v: 1, kind: 'result',
                                    payload: { ...payload, agent_version: AGENT_VERSION } });

  /* Баланс запрашиваем независимо от long-poll команд: он может висеть 25 с,
     а фактический расход Spy должен появляться в плеере каждые 9 секунд. */
  let balanceBusy = false;
  const reportBalance = async () => {
    if (stopped || balanceBusy) return;
    let room = '';
    try {
      const mine = JSON.parse(localStorage.getItem(SPY_KEY) || 'null');
      room = String((mine && mine.room) || localStorage.getItem(BALANCE_ROOM_KEY) || '');
    } catch (e) { return; }
    if (!room) return;
    balanceBusy = true;
    try {
      const balance = dossierBalance(await roomDossier(room, true));
      if (balance !== null) report({ act: 'balance', room, balance });
    } finally { balanceBusy = false; }
  };
  setInterval(reportBalance, BALANCE_MS);

  /* ---- обработка poll-ответа плеера --------------------------------------- */

  let recovering = false;

  /* Обновление потока живёт своим таймером, а не внутри разбора poll-ответа:
     тот возвращается раз в CB_POLL_HOLD (25 с), поэтому двадцатисекундный
     цикл оттуда недостижим в принципе. */
  let spyNow = { state: 'idle', room: '' };
  let refreshBusy = false;
  const refreshStream = async () => {
    if (stopped || refreshBusy) return;
    if (spyNow.state !== 'spying' || !spyNow.room) return;
    refreshBusy = true;
    try {
      const s = await streamUrl(spyNow.room);
      report({ act: 'refresh', room: spyNow.room, url: s.url,
               room_status: s.room_status, raw: s.url ? '' : s.raw });
    } catch (e) {
      report({ act: 'refresh', room: spyNow.room, url: '', room_status: '',
               raw: `запрос не прошёл: ${(e && e.message) || e}` });
    } finally { refreshBusy = false; }
  };
  setInterval(refreshStream, REFRESH_MS);

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
    spyNow = { state: String(spy.state || 'idle'), room: String(spy.room || '') };

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

    if (spy.state === 'spying' && spy.room) {
      say(`spy ${spy.room} · ${agent.username || 'аккаунт'}`);
    } else if (agent.anon) {
      say('вкладка без входа — откройте аккаунт на сайте');
    } else {
      say(`${agent.username || 'готов'} · жду команду`);
    }
  }

  /* Единственный канал к плееру — window.opener. Вкладку, открытую не
     плеером, связать с ним нечем: fetch запрещает CSP сайта, а window.open
     по имени находит только окна своей группы вкладок. */
  if (window.opener && !window.opener.closed) {
    transport = window.opener;
    hello();
    startHelloRetry();
  } else {
    say('эту вкладку открыл не плеер — нажмите spy в плеере, ' +
        'он откроет вкладку Chaturbate сам');
  }
})();
