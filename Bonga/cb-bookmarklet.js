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
                   запасной источник — hls_source в initialRoomDossier.
     после входа: chatConnection.changeStatus("privatespying") — иначе
                   их плеер остаётся в privatenotwatching, не шлёт
                   playerQuality и сайт снимает право на 30-й секунде. */
(() => {
  const PLAYER = '__PLAYER_ORIGIN__';
  const AGENT_VERSION = 15;
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
    let cdnTimer = 0;
    let cdnWatch = null;
    const off = () => {
    stopped = true;
    clearInterval(helloTimer);
    clearTimeout(copyTimer);
    clearInterval(cdnTimer);
    try { if (cdnWatch && cdnWatch.disconnect) cdnWatch.disconnect(); } catch (e) {}
    badge.remove();
    delete window.__cbAgent;
    gateSiteMedia(false);
    pretendTabVisible(false);
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

  /* Комната, на странице которой стоит эта вкладка. Плееру это нужно, чтобы
     знать, присутствует ли вкладка в той комнате, за которую идёт списание:
     без присутствия сайт закрывает оплаченный показ на тридцатой секунде
     (прогоны 30.08 — 30.1, 31.4 и 30.4 с при room_status=private). Досье
     сайт кладёт только на страницу комнаты, это и есть точный признак. */
  const tabRoom = () => {
    const dossier = localDossier();
    return dossier ? String(dossier.broadcaster_username || '').toLowerCase() : '';
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

  const hello = () => toHub({ v: 1, kind: 'hello', username: viewerName(),
                              room: tabRoom(), agent_version: AGENT_VERSION });

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
                                room: tabRoom(), agent_version: AGENT_VERSION });
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

  /* ---- плеер сайта --------------------------------------------------------
     Наш POST в tipping/* оплачивает вход, но штатный обработчик после успеха
     ещё делает chatConnection.changeStatus("privatespying"). Без этого их
     видео остаётся в privatenotwatching: overlay вместо потока, sendQuality
     не уходит (бандл 6784-prod, startQualityTracking / sendQuality), и сайт
     отзывает право ровно на 30-й секунде — прогоны 30.08, в том числе lin_rin
     уже со вкладкой на странице комнаты.

     chatConnection живёт в событии roomLoaded (история длины 1, listen
     сразу отдаёт последний контекст). К нему не привязаться из DOM: id
     вебпак-модуля меняется с каждым cachebust, поэтому ищем фабрику по
     строкам roomLoaded/roomCleanup и зовём её через webpack require.

     Вкладка Chaturbate при просмотре в нашем плеере в фоне. sendQuality
     не шлёт метрики при document.hidden — на время spy притворяемся, что
     вкладка видима, иначе даже privatespying не спасёт.

     Их плеер при privatespying сам качает HLS (dulce_devil_ 30.08: показ
     в нашем плеере и параллельно на вкладке сайта). Видео сайта глушим:
     статус и метрики оставляем, сегменты mmcdn/highwebmedia — нет. Нельзя
     звать их stopVideoAndMetrics: он шлёт unload и сайт снова снимет право.

     Один pause на входе не держит: avgustina_love 30.08 15:44 — вкладка
     пустая ~30 с, потом их HLS всё равно стартует (отложенный attach после
     sendQuality). hls.js к тому моменту уже держит свой fetch, обход нашего
     wrap. Сторож v11 сбрасывал src и звал load каждые 0.5 с — картинка
     мерцала в такт таймеру. Теперь: CSS-прячем, pause/stopLoad, src не
     трогаем после первого глушения. */

  const SITE_SPYING = 'privatespying';
  const SITE_IDLE = 'privatenotwatching';

  function webpackRequire() {
    const chunks = globalThis.webpackChunk_multimediallc_cb_ts;
    if (!chunks || typeof chunks.push !== 'function') return null;
    let req = null;
    try {
      chunks.push([[`cbAgent_${Date.now()}`], {}, r => { req = r; }]);
    } catch (e) { return null; }
    return req && req.m ? req : null;
  }

  function roomLoadedEvent(req) {
    if (!req || !req.m) return null;
    for (const id of Object.keys(req.m)) {
      let src = '';
      try { src = Function.prototype.toString.call(req.m[id]); } catch (e) { continue; }
      if (src.indexOf('roomLoaded') < 0 || src.indexOf('roomCleanup') < 0) continue;
      let exp;
      try { exp = req(id); } catch (e) { continue; }
      if (!exp || typeof exp !== 'object') continue;
      for (const key of Object.keys(exp)) {
        const v = exp[key];
        if (v && v.eventName === 'roomLoaded' && typeof v.listen === 'function')
          return v;
      }
    }
    return null;
  }

  function chatConnFrom(evt) {
    if (!evt) return null;
    let conn = null;
    let handle = null;
    try {
      handle = evt.listen(ctx => {
        if (ctx && ctx.chatConnection &&
            typeof ctx.chatConnection.changeStatus === 'function')
          conn = ctx.chatConnection;
      });
    } catch (e) { return null; }
    try { if (handle && handle.removeListener) handle.removeListener(); } catch (e) {}
    return conn;
  }

  let visPatched = false;
  function pretendTabVisible(on) {
    try {
      if (on && !visPatched) {
        Object.defineProperty(document, 'hidden',
                              { configurable: true, get: () => false });
        Object.defineProperty(document, 'visibilityState',
                              { configurable: true, get: () => 'visible' });
        visPatched = true;
      } else if (!on && visPatched) {
        delete document.hidden;
        delete document.visibilityState;
        visPatched = false;
      }
    } catch (e) { /* hidden мог быть неconfigurable */ }
  }

  const siteVideoEl = () => document.getElementById('chat-player') ||
                            document.querySelector('[data-testid="video"]');

  function allSiteVideos() {
    const out = [];
    const add = v => { if (v && out.indexOf(v) < 0) out.push(v); };
    add(siteVideoEl());
    try {
      const q = document.querySelectorAll && document.querySelectorAll('video');
      if (q) for (let i = 0; i < q.length; i++) add(q[i]);
    } catch (e) {}
    return out;
  }

  function stopHls(v) {
    const box = [v && v.hls, v && v._hls, window.vooduPlayer];
    for (let i = 0; i < box.length; i++) {
      const p = box[i];
      if (!p) continue;
      try { if (typeof p.stopLoad === 'function') p.stopLoad(); } catch (e) {}
      try { if (typeof p.pause === 'function') p.pause(); } catch (e) {}
      try { if (typeof p.detachMedia === 'function') p.detachMedia(); } catch (e) {}
    }
  }

  const QUIET_CSS_ID = 'cb-agent-quiet';
  function quietCss(on) {
    try {
      if (on) {
        if (document.getElementById(QUIET_CSS_ID)) return;
        const s = document.createElement('style');
        s.id = QUIET_CSS_ID;
        s.textContent = 'video{opacity:0!important;visibility:hidden!important}';
        const host = document.head || document.documentElement || document.body;
        if (host && host.appendChild) host.appendChild(s);
      } else {
        const s = document.getElementById(QUIET_CSS_ID);
        if (s && s.parentNode && s.parentNode.removeChild) s.parentNode.removeChild(s);
        else if (s && s.remove) s.remove();
      }
    } catch (e) {}
  }

  /* hard: сбросить src (только на входе). Повторный сброс + load даёт
     мерцание: их плеер снова attach, кадр, наш teardown, снова attach. */
  function pauseSiteVideo(hard) {
    quietCss(true);
    const list = allSiteVideos();
    for (let i = 0; i < list.length; i++) {
      const v = list[i];
      try { if (typeof v.pause === 'function') v.pause(); } catch (e) {}
      try { v.muted = true; v.volume = 0; } catch (e) {}
      try {
        if (v.style) {
          v.style.opacity = '0';
          v.style.visibility = 'hidden';
        }
      } catch (e) {}
      stopHls(v);
      if (!hard) continue;
      try {
        if (typeof v.removeAttribute === 'function') v.removeAttribute('src');
        v.src = '';
        v.srcObject = null;
        if (typeof v.load === 'function') v.load();
      } catch (e) {}
    }
    return list.length > 0;
  }

  function onSitePlaying(e) {
    const t = e && e.target;
    if (!t) return;
    const tag = String(t.tagName || '').toUpperCase();
    if (tag !== 'VIDEO' && tag !== 'AUDIO') return;
    pauseSiteVideo(false);
  }

  /* Сегменты и плейлисты CDN — это и есть параллельная трансляция. Ajax
     get_edge_hls_url на chaturbate.com не трогаем: по нему агент обновляет
     адрес для нашего плеера. */
  const isCdnMedia = url => {
    const u = String((url && url.url) || url || '');
    if (!/mmcdn\.com|highwebmedia\.com/i.test(u)) return false;
    return /\.(m3u8|m4s|mp4|ts|jpg|jpeg)(\?|$)/i.test(u) ||
           /\/(seg_|chunklist|llhls|stream\?room=)/i.test(u);
  };

  /* Трафик вкладки: отсечено — wrap не пустил запрос; ушло — браузер всё же
     скачал (свой fetch у hls.js, native src). Размер по Resource Timing:
     у CDN часто нет Timing-Allow-Origin, тогда transferSize=0, но запрос
     всё равно считаем. Нельзя перечитывать весь буфер performance: там
     сидят куски с начала жизни страницы, в том числе до spy. */
  let cdnBlocked = 0;
  let cdnPassed = 0;
  let cdnBytes = 0;
  function resetCdnStats() { cdnBlocked = 0; cdnPassed = 0; cdnBytes = 0; }
  function fmtTraffic(n) {
    n = Number(n) || 0;
    if (n < 1024) return n + ' Б';
    if (n < 1024 * 1024) return Math.round(n / 102.4) / 10 + ' КБ';
    return Math.round(n / 104857.6) / 10 + ' МБ';
  }
  function noteCdnLoaded(e) {
    const name = (e && e.name) || '';
    if (!isCdnMedia(name) && !/live\.mmcdn\.com/i.test(name)) return;
    cdnPassed += 1;
    cdnBytes += Number(e.transferSize) || Number(e.encodedBodySize) || 0;
  }
  function startCdnWatch() {
    try {
      if (typeof PerformanceObserver !== 'function') return;
      cdnWatch = new PerformanceObserver(list => {
        const ents = list.getEntries();
        for (let i = 0; i < ents.length; i++) noteCdnLoaded(ents[i]);
      });
      cdnWatch.observe({ type: 'resource', buffered: false });
    } catch (e) { cdnWatch = null; }
  }
  function cdnStats() {
    return { blocked: cdnBlocked, passed: cdnPassed, bytes: cdnBytes };
  }
  function cdnLine() {
    const s = cdnStats();
    return `трафик ${fmtTraffic(s.bytes)} · ушло ${s.passed} · отсечено ${s.blocked}`;
  }

  const SILENCE_MS = 500;
  let mediaGate = null;
  function gateSiteMedia(on) {
    if (on) {
      if (mediaGate) return;
      const hosts = [];
      if (typeof window !== 'undefined') hosts.push(window);
      if (typeof globalThis !== 'undefined' && hosts.indexOf(globalThis) < 0)
        hosts.push(globalThis);
      const fetchWas = hosts.map(h => h.fetch);
      const XHR = typeof XMLHttpRequest !== 'undefined' ? XMLHttpRequest : null;
      const HME = typeof HTMLMediaElement !== 'undefined' ? HTMLMediaElement : null;
      const HVE = typeof HTMLVideoElement !== 'undefined' ? HTMLVideoElement : null;
      const MS = typeof MediaSource !== 'undefined' ? MediaSource : null;
      const xhrOpen = XHR && XHR.prototype.open;
      const xhrSend = XHR && XHR.prototype.send;
      const playWas = HME && HME.prototype.play;
      const playVideoWas = HVE && HVE.prototype.play !== playWas ? HVE.prototype.play : null;
      const addSB = MS && MS.prototype.addSourceBuffer;
      mediaGate = { hosts, fetchWas, xhrOpen, xhrSend, playWas, playVideoWas,
                    addSB, XHR, HME, HVE, MS };

      const blocked = () => {
        cdnBlocked += 1;
        return Promise.reject(new Error('cb-agent: cdn media blocked'));
      };
      hosts.forEach((h, i) => {
        if (typeof fetchWas[i] !== 'function') return;
        h.fetch = function (input, init) {
          if (isCdnMedia(input)) return blocked();
          return fetchWas[i].apply(this, arguments);
        };
      });
      if (xhrOpen && xhrSend) {
        XHR.prototype.open = function (method, url) {
          this.__cbAgentUrl = url;
          return xhrOpen.apply(this, arguments);
        };
        XHR.prototype.send = function () {
          if (isCdnMedia(this.__cbAgentUrl)) {
            cdnBlocked += 1;
            try { this.abort(); } catch (e) {}
            return;
          }
          return xhrSend.apply(this, arguments);
        };
      }
      const mutePlay = function () { pauseSiteVideo(false); return Promise.resolve(); };
      if (playWas) HME.prototype.play = mutePlay;
      if (playVideoWas) HVE.prototype.play = mutePlay;
      if (addSB) {
        MS.prototype.addSourceBuffer = function () {
          throw new Error('cb-agent: mse blocked');
        };
      }
      try { document.addEventListener('playing', onSitePlaying, true); } catch (e) {}
      quietCss(true);
      resetCdnStats();
      mediaGate.silenceTimer = setInterval(() => pauseSiteVideo(false), SILENCE_MS);
    } else if (mediaGate) {
      try {
        mediaGate.hosts.forEach((h, i) => {
          if (mediaGate.fetchWas[i]) h.fetch = mediaGate.fetchWas[i];
        });
        if (mediaGate.xhrOpen) mediaGate.XHR.prototype.open = mediaGate.xhrOpen;
        if (mediaGate.xhrSend) mediaGate.XHR.prototype.send = mediaGate.xhrSend;
        if (mediaGate.playWas) mediaGate.HME.prototype.play = mediaGate.playWas;
        if (mediaGate.playVideoWas) mediaGate.HVE.prototype.play = mediaGate.playVideoWas;
        if (mediaGate.addSB) mediaGate.MS.prototype.addSourceBuffer = mediaGate.addSB;
        document.removeEventListener('playing', onSitePlaying, true);
        clearInterval(mediaGate.silenceTimer);
        quietCss(false);
      } catch (e) {}
      mediaGate = null;
    }
  }

  function tellSitePlayer(status) {
    const out = { status, conn: false, quiet: false, vis: visPatched };
    /* Сначала глушим CDN, потом меняем статус: иначе их плеер успеет
       открыть HLS до перехвата. */
    if (status === SITE_SPYING) {
      pretendTabVisible(true);
      gateSiteMedia(true);
      out.quiet = pauseSiteVideo(true);
      out.vis = visPatched;
    }
    const conn = chatConnFrom(roomLoadedEvent(webpackRequire()));
    if (conn) {
      try {
        out.was = String(conn.status || '');
        conn.changeStatus(status);
        out.conn = true;
      } catch (e) {
        out.error = String((e && e.message) || e);
      }
    }
    if (status !== SITE_SPYING) {
      gateSiteMedia(false);
      pretendTabVisible(false);
      out.vis = visPatched;
    }
    if (status === SITE_SPYING) out.quiet = pauseSiteVideo(true) || out.quiet;
    return out;
  }

  const siteLine = site => site.conn
    ? `плеер сайта ${site.was || '?'} → ${site.status}` +
      `${site.vis ? ', вкладка как видимая' : ''}` +
      `${site.quiet ? ', без видео сайта' : ''}`
    : `плеер сайта не найден (${site.error || 'нет roomLoaded'})`;

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
    const site = tellSitePlayer(SITE_SPYING);
    return { ok: true, url: s.url, room_status: s.room_status, price, balance,
             balance_after: after, site,
             raw: `вход: ${r.text.slice(0, 800)} · поток: ${s.raw} · ${siteLine(site)}` };
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
    const site = tellSitePlayer(SITE_IDLE);
    if (stopDone(data, r.text))
      return { ok: true, site, raw: `${r.text.slice(0, 500)} · ${siteLine(site)}` };
    return { ok: false, error: `сайт отклонил остановку (HTTP ${r.status})`,
             site, raw: `${r.text.slice(0, 1000)} · ${siteLine(site)}` };
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
      const cdn = cdnStats();
      report({ act: 'refresh', room: spyNow.room, url: s.url,
               room_status: s.room_status, cdn,
               raw: s.url ? '' : s.raw });
    } catch (e) {
      report({ act: 'refresh', room: spyNow.room, url: '', room_status: '',
               raw: `запрос не прошёл: ${(e && e.message) || e}` });
    } finally { refreshBusy = false; }
  };
  setInterval(refreshStream, REFRESH_MS);
  const paintBadge = () => {
    if (stopped) return;
    const traffic = cdnLine();
    if (spyNow.state === 'spying' && spyNow.room)
      say(`spy ${spyNow.room} · ${traffic}`);
    else if (spyNow.state === 'starting' && spyNow.room)
      say(`вход ${spyNow.room} · ${traffic}`);
    else if (spyNow.state === 'stopping' && spyNow.room)
      say(`стоп ${spyNow.room} · ${traffic}`);
  };
  cdnTimer = setInterval(paintBadge, 2000);

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
      /* Пока команда выполняется (вход — секунды), таймер плашки должен
         уже показывать трафик, а не висеть на «spy_start → …» до следующего
         poll (blondie_dirty_squirt 30.08 16:39). */
      if (cmd.act === 'spy_start')
        spyNow = { state: 'starting', room: String(cmd.room || '') };
      else if (cmd.act === 'spy_stop')
        spyNow = { state: 'stopping', room: String(cmd.room || '') };
      say(`${cmd.act} → ${cmd.room} · ${cdnLine()}`);
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
      if (cmd.act === 'spy_start' && out.ok)
        spyNow = { state: 'spying', room: String(cmd.room || '') };
      else if (cmd.act === 'spy_start' || cmd.act === 'spy_stop')
        spyNow = { state: 'idle', room: '' };
      paintBadge();
      return;
    }

    paintBadge();
    if (spy.state === 'spying' && spy.room) {
      /* paintBadge уже написал spy+трафик */
    } else if (agent.anon) {
      say('вкладка без входа — откройте аккаунт на сайте');
    } else {
      // Комнату показываем в плашке: по ней сразу видно, стоит ли вкладка
      // там, где нужно, или её ещё предстоит перевести.
      say(`${agent.username || 'готов'} · ${tabRoom() || 'главная'} · ${cdnLine()}`);
    }
  }

  /* Единственный канал к плееру — window.opener. Вкладку, открытую не
     плеером, связать с ним нечем: fetch запрещает CSP сайта, а window.open
     по имени находит только окна своей группы вкладок. */
  startCdnWatch();
  if (window.opener && !window.opener.closed) {
    transport = window.opener;
    hello();
    startHelloRetry();
  } else {
    say('эту вкладку открыл не плеер — нажмите spy в плеере, ' +
        'он откроет вкладку Chaturbate сам');
  }
})();
