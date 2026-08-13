/* Сборщик ников для player.html.

   Запускается закладкой на любой странице bongacams.com: там уже есть куки
   сессии и пройденная проверка Cloudflare, поэтому источники доступны как
   обычные same-origin запросы — ровно те же, что делает сама страница.

   Первое нажатие: собирает listing + sitemap, отправляет в базу плеера и
   включает автосбор каждые 60 секунд (пока вкладка открыта). Повторное
   нажатие — выключает. Состояние видно в плашке в углу страницы.

   Источник 1: /tools/listing_v3.php — ник + номер видеосервера (vsid).
   Источник 2: /sitemap.xml из robots.txt — только имена, зато весь каталог;
   опрашивается один раз, каталог имён почти не меняется. */
(async () => {
  const PERIOD = 60000;
  const TARGETS = [...new Set(['__PLAYER_ORIGIN__', 'http://127.0.0.1:8777'])];

  /* Второе нажатие закладки выключает автосбор. */
  if (window.__bonga) {
    clearInterval(window.__bonga.timer);
    window.__bonga.badge.remove();
    delete window.__bonga;
    return;
  }

  const badge = document.createElement('div');
  badge.style.cssText = 'position:fixed;z-index:2147483647;right:12px;bottom:12px;' +
    'padding:8px 12px;border-radius:8px;background:#1c1f26;color:#e6e8ec;' +
    'font:12px/1.4 system-ui,sans-serif;box-shadow:0 4px 16px rgba(0,0,0,.5);' +
    'cursor:pointer;max-width:320px';
  badge.title = 'Нажмите, чтобы выключить автосбор';
  badge.onclick = () => {
    clearInterval(window.__bonga.timer);
    badge.remove();
    delete window.__bonga;
  };
  document.body.appendChild(badge);
  window.__bonga = { badge, timer: 0 };

  const say = text => { badge.textContent = 'Сборщик ников: ' + text; };
  const pause = ms => new Promise(r => setTimeout(r, ms));

  const edgeOf = node => {
    if (Number.isInteger(node.vsid)) return String(node.vsid);
    if (typeof node.vsid === 'string' && /^\d+$/.test(node.vsid)) return node.vsid;
    const m = typeof node.esid === 'string' && /live-edge-?(us\d+|\d+)/.exec(node.esid);
    return m ? m[1] : '';
  };

  async function collect(withSitemap) {
    const out = {};
    const add = (user, edge, viewers) => {
      if (typeof user !== 'string' || !user) return;
      const key = user.toLowerCase();
      const prev = out[key];
      if (prev && (prev[1] || !edge)) return;      // запись с edge не затираем пустой
      out[key] = [user, edge ? String(edge) : '', viewers | 0];
    };
    const walk = node => {
      if (!node || typeof node !== 'object') return;
      if (!Array.isArray(node) && typeof node.username === 'string') {
        const edge = edgeOf(node);
        if (edge) add(node.username, edge, node.viewers | 0);
      }
      for (const v of Object.values(node)) walk(v);
    };

    let tab = 'female';
    try {
      tab = JSON.parse(document.getElementById('listingConfiguration').textContent)
              .initData.livetab || tab;
    } catch (e) { /* не страница каталога — берём вкладку по умолчанию */ }

    /* Листинг: те же запросы, что уходят при прокрутке каталога. */
    let pages = 0;
    const variants = [
      { q: `livetab=${tab}&offset=0&limit=100`, ajax: true },
      { q: `livetab=${tab}&offset=0&limit=60`, ajax: false },
      { q: `livetab=${tab}&offset=0&limit=100&online_only=true`, ajax: true },
    ];

    let winner = null;
    for (const v of variants) {
      try {
        const res = await fetch(`/tools/listing_v3.php?${v.q}`, {
          credentials: 'same-origin',
          headers: v.ajax ? { 'X-Requested-With': 'XMLHttpRequest' } : {},
        });
        if (!res.ok) continue;
        const before = Object.keys(out).length;
        walk(await res.json());
        if (Object.keys(out).length > before) { winner = v; pages = 1; break; }
      } catch (e) { /* пробуем следующий вариант */ }
      await pause(300);
    }

    if (winner) {
      const limit = Number(/limit=(\d+)/.exec(winner.q)[1]);
      for (let offset = limit; offset < 6000; offset += limit) {
        if (!window.__bonga) break;               // выключили посреди обхода
        try {
          const res = await fetch(
            `/tools/listing_v3.php?${winner.q.replace(/offset=\d+/, `offset=${offset}`)}`,
            { credentials: 'same-origin',
              headers: winner.ajax ? { 'X-Requested-With': 'XMLHttpRequest' } : {} });
          if (!res.ok) break;
          const before = Object.keys(out).length;
          walk(await res.json());
          pages++;
          if (Object.keys(out).length === before) break;   // каталог кончился
        } catch (e) { break; }
        await pause(300);
      }
    }

    const withEdge = Object.keys(out).length;

    /* Sitemap: полный каталог имён, но без номеров серверов. Меняется редко,
       поэтому тянем только при первом запуске. */
    let fromMap = 0;
    if (withSitemap) {
      try {
        const locs = async url => {
          const text = await (await fetch(url, { credentials: 'same-origin' })).text();
          return [...text.matchAll(/<loc>([^<]+)<\/loc>/g)].map(m => m[1]);
        };
        let urls = await locs('/sitemap.xml');
        const children = urls.filter(u => /sitemap/i.test(u)).slice(0, 12);
        if (children.length) {
          urls = [];
          for (const child of children) {
            urls.push(...await locs(child));
            await pause(300);
          }
        }
        for (const url of urls) {
          const m = /^https?:\/\/[^/]+\/(?:profile\/)?([A-Za-z0-9_.-]{3,30})\/?$/.exec(url);
          if (m) { add(m[1], '', 0); fromMap++; }
        }
      } catch (e) { /* sitemap закрыт — работаем одним листингом */ }
    }

    return { arr: Object.values(out), withEdge, pages, fromMap };
  }

  async function send(arr) {
    for (const base of TARGETS) {
      try {
        const res = await fetch(base + '/api/accounts', {
          method: 'POST',
          headers: { 'Content-Type': 'text/plain;charset=UTF-8' },   // «простой» запрос, без preflight
          body: JSON.stringify(arr),
        });
        if (res.ok) return await res.json();
      } catch (e) { /* пробуем следующий адрес */ }
    }
    return null;
  }

  const clock = () => new Date().toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit', second: '2-digit' });

  async function cycle(first) {
    say(first ? 'первый сбор…' : 'обновляю…');
    const { arr, withEdge, pages, fromMap } = await collect(first);

    // Сбор длится несколько секунд — за это время его могли выключить.
    if (!window.__bonga) return;
    if (!arr.length) return say(`${clock()}: каталог ничего не отдал`);
    const posted = await send(arr);

    if (posted) {
      say(`${clock()}: с сервером ${withEdge}${first ? `, из sitemap ${fromMap}` : ''}` +
          ` → новых ${posted.added}, в базе ${posted.total}`);
    } else {
      say(`${clock()}: собрано ${arr.length}, но плеер не отвечает`);
    }

    if (first) {
      alert(`Сборщик запущен.\n\nСобрано ников: ${arr.length}\n` +
            `с номером сервера: ${withEdge} (страниц листинга: ${pages})\n` +
            `из sitemap, только имена: ${fromMap}\n` +
            (posted ? `Добавлено в базу: новых ${posted.added}, всего ${posted.total}\n` : 'Плеер не ответил\n') +
            `\nДальше обновляю каждые ${PERIOD / 1000} с, пока эта вкладка открыта.\n` +
            'Выключить — плашка в правом нижнем углу или повторное нажатие закладки.');
    }
  }

  await cycle(true);
  if (window.__bonga) window.__bonga.timer = setInterval(() => cycle(false), PERIOD);
})();
