/* Сборщик ников для player.html.
   Запускается закладкой на любой странице bongacams.com: там уже есть куки
   сессии и пройденный Cloudflare, поэтому оба источника доступны как
   обычные same-origin запросы — ровно те же, что делает сама страница.

   Источник 1: /tools/listing_v3.php — ник + номер видеосервера (vsid).
   Источник 2: /sitemap.xml из robots.txt — только ники, зато весь каталог.

   Результат: JSON [["ник","edge",зрители], …] в буфере обмена. */
(async () => {
  const out = {};
  const diag = [];

  const add = (user, edge, viewers) => {
    if (typeof user !== 'string' || !user) return;
    const key = user.toLowerCase();
    const prev = out[key];
    if (prev && (prev[1] || !edge)) return;        // запись с edge не затираем пустой
    out[key] = [user, edge ? String(edge) : '', viewers | 0];
  };

  /* Номер сервера у модели лежит либо в vsid (число или строка), либо внутри
     esid вида "live-edge67-rn" / "live-edge-us14-rn". Берём что найдётся. */
  const edgeOf = node => {
    if (Number.isInteger(node.vsid)) return String(node.vsid);
    if (typeof node.vsid === 'string' && /^\d+$/.test(node.vsid)) return node.vsid;
    const m = typeof node.esid === 'string' && /live-edge-?(us\d+|\d+)/.exec(node.esid);
    return m ? m[1] : '';
  };

  const walk = node => {
    if (!node || typeof node !== 'object') return;
    if (!Array.isArray(node) && typeof node.username === 'string') {
      const edge = edgeOf(node);
      if (edge) add(node.username, edge, node.viewers | 0);
    }
    for (const v of Object.values(node)) walk(v);
  };

  const pause = ms => new Promise(r => setTimeout(r, ms));
  const count = () => Object.keys(out).length;

  let tab = 'female';
  try {
    tab = JSON.parse(document.getElementById('listingConfiguration').textContent)
            .initData.livetab || tab;
  } catch (e) { diag.push('на странице нет listingConfiguration, вкладка: female'); }

  /* --- 1. Листинг: подбираем рабочий вариант запроса ------------------------ */
  const variants = [
    { q: `livetab=${tab}&offset=0&limit=100`, ajax: true },
    { q: `livetab=${tab}&offset=0&limit=60`, ajax: false },
    { q: `livetab=${tab}&offset=0&limit=100&online_only=true`, ajax: true },
    { q: `livetab=${tab}&offset=0&limit=100&_blocks=1`, ajax: true },
  ];

  let winner = null, sample = '';
  for (const v of variants) {
    const url = `/tools/listing_v3.php?${v.q}`;
    try {
      const res = await fetch(url, {
        credentials: 'same-origin',
        headers: v.ajax ? { 'X-Requested-With': 'XMLHttpRequest' } : {},
      });
      const text = await res.text();
      if (!sample) sample = text.slice(0, 400);

      let data = null;
      try { data = JSON.parse(text); } catch (e) { /* не JSON */ }

      const before = count();
      if (data) walk(data);
      const got = count() - before;

      diag.push(`${v.q} → HTTP ${res.status}, ${res.headers.get('content-type') || '?'}, ` +
                `${data ? 'JSON' : 'не JSON'}, моделей: ${got}`);
      if (got > 0) { winner = v; break; }
    } catch (e) {
      diag.push(`${v.q} → ошибка запроса: ${e.message}`);
    }
    await pause(300);
  }

  let pages = winner ? 1 : 0;
  if (winner) {
    const limit = Number(/limit=(\d+)/.exec(winner.q)[1]);   // шаг = размер страницы
    for (let offset = limit; offset < 6000; offset += limit) {
      const q = winner.q.replace(/offset=\d+/, `offset=${offset}`);
      let data;
      try {
        const res = await fetch(`/tools/listing_v3.php?${q}`, {
          credentials: 'same-origin',
          headers: winner.ajax ? { 'X-Requested-With': 'XMLHttpRequest' } : {},
        });
        if (!res.ok) break;
        data = await res.json();
      } catch (e) { break; }

      const before = count();
      walk(data);
      pages++;
      if (count() === before) break;              // новых ников нет — каталог кончился
      await pause(300);
    }
  }
  const withEdge = count();

  /* --- 2. Sitemap: каталог целиком, но без номеров серверов ----------------- */
  let fromMap = 0;
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
  } catch (e) { diag.push('sitemap недоступен: ' + e.message); }

  const arr = Object.values(out);
  try { await navigator.clipboard.writeText(JSON.stringify(arr)); } catch (e) { /* см. prompt ниже */ }

  const report = `Собрано ников: ${arr.length}\n` +
                 `с номером сервера: ${withEdge} (страниц листинга: ${pages})\n` +
                 `из sitemap, только имена: ${fromMap}\n\nСкопировано в буфер.`;

  if (withEdge > 0) {
    alert(report);
  } else {
    /* Листинг не дался — показываем, что именно ответил сервер.
       Текст в prompt можно выделить и скопировать. */
    prompt(report + '\n\nЛистинг не отдал моделей. Скопируйте эту диагностику:',
           diag.join(' | ') + ' || ответ: ' + sample.replace(/\s+/g, ' '));
  }
})();
