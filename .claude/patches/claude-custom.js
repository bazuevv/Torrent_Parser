/*
 * Кастомный JavaScript для webview расширения Claude Code (anthropic.claude-code).
 *
 * Канонический источник — этот файл. Хук .claude/hooks/patch-claude-webview.py
 * инлайнит его содержимое в bootstrap-блок внутри webview/index.js
 * (CSP блокирует <script src> к внешним webview-ресурсам).
 *
 * Конфигурация — `.claude/patches/claude-custom-config.toml`. Хук
 * подставляет её в bootstrap как `window.__CLAUDE_CUSTOM_CONFIG__`.
 * Поддерживаемые ключи:
 *   - logs (bool)            : включить/выключить console.log + warn
 *   - pollIntervalMs (int)   : период пуллинга CSS, мс (по умолчанию 5000)
 *   - throttleMs (int)       : минимальный интервал между обновлениями
 *                              CSS на DOM-мутациях, мс (по умолчанию 200)
 *
 * Workflow редактирования: правишь файл → новая сессия Claude Code или
 * UserPromptSubmit-хук синхронизирует его в webview → Reload Window /
 * закрыть-открыть панель Claude Code, чтобы перезапустился bootstrap.
 *
 * Для CSS отдельный reload не нужен — он горячо перезагружается через
 * <link rel="stylesheet"> с cache-bust query (пуллинг + DOM-мутации).
 */

(function () {
  if (window.__claudeCustomScriptInstalled) return;
  window.__claudeCustomScriptInstalled = true;

  var cfg = window.__CLAUDE_CUSTOM_CONFIG__ || {};
  var LOGS_ENABLED = cfg.logs === true;
  var POLL_MS = typeof cfg.pollIntervalMs === 'number' && cfg.pollIntervalMs > 0
    ? cfg.pollIntervalMs : 5000;
  var THROTTLE_MS = typeof cfg.throttleMs === 'number' && cfg.throttleMs >= 0
    ? cfg.throttleMs : 200;
  var VISIBILITY_REFRESH_DELAY_MS =
    typeof cfg.visibilityRefreshDelayMs === 'number' && cfg.visibilityRefreshDelayMs >= 0
      ? cfg.visibilityRefreshDelayMs
      : 0;
  var DEBUG_OVERLAY_ENABLED = cfg.debugOverlay === true;
  var AUTO_PING_AFTER_SILENCE_SEC = typeof cfg.autoPingAfterSilenceSec === 'number' && cfg.autoPingAfterSilenceSec > 0
    ? cfg.autoPingAfterSilenceSec : 0;
  var PING_INTERVAL_SEC = typeof cfg.pingIntervalSec === 'number' && cfg.pingIntervalSec > 0
    ? cfg.pingIntervalSec : 0;
  var MAX_PINGS = typeof cfg.maxPingsPerProcessing === 'number' && cfg.maxPingsPerProcessing >= 0
    ? cfg.maxPingsPerProcessing : 0; // 0 = без ограничений

  /* ============================================================
   * PERF PROBE — прибор, а не функция
   *
   * Родился из отказа 2026-09-02: содержимое вкладки появлялось через
   * 30–40 с после Reload Window, поле ввода моргало по контуру и не
   * принимало ввод, потом всё проходило само. Бандл был цел, наш JS
   * исполнялся, в webview-errors.log — ни одного исключения, и датчик
   * пустой вкладки молчал (его setTimeout сам исполнился с
   * опозданием, когда рисовать уже было что). То есть главный поток
   * был ЗАНЯТ, а не сломан — но чем именно, сказать было нечем.
   *
   * Прибор мерит три вещи, и порядок здесь важен:
   *
   * 1. `lag` — насколько опаздывает секундный таймер. Это единственный
   *    показатель, который НЕ зависит от нашего кода: он одинаково
   *    ловит и наши сканы, и чужой рендер. Без него любая цифра ниже
   *    остаётся уликой без состава преступления.
   * 2. `ours` — сколько миллисекунд за интервал провели в наших
   *    обработчиках, с разбивкой по именам. Вместе с `lag` даёт
   *    главный ответ: наша это вина или чужая. Большой lag при
   *    крошечном `ours` — значит виноват не патч.
   * 3. `dom` — размер дерева и число ходов в нём. Проверяет
   *    предположение, которое до сих пор проверить было нечем:
   *    виртуализирует ли расширение список сообщений. Если нет, то
   *    querySelectorAll по всему документу на каждую мутацию — это
   *    O(размер переписки) десять раз в секунду.
   *
   * Отчёт уходит в hooks-runtime/webview-errors.log с `kind: "perf"`
   * своим fetch'ем, а не через claudeReportError: у того лимит
   * в 20 отправок на страницу, и регулярные сводки съели бы квоту,
   * оставив настоящее исключение без канала.
   *
   * Молчит, пока всё в порядке (см. пороги), — иначе журнал
   * заполнился бы ровными строками «всё хорошо», в которых
   * настоящую аномалию пришлось бы искать глазами. Исключение —
   * первый отчёт: он baseline, по нему видно норму этой машины.
   *
   * Диагностика, а не функция: safeMode прибор НЕ гасит. В
   * безопасный режим уходят именно при поломке, и остаться там без
   * измерений — ровно та ситуация, из которой прибор и родился.
   *
   * Управление: `perfProbe` в claude-custom-config.toml.
   * ============================================================ */
  var PERF_ENABLED = cfg.perfProbe !== false;
  var PERF_INTERVAL_MS = 10000;

  // Пороги отчёта. Бюджет наших обработчиков — 150 мс на 10 с, то есть
  // 1.5% главного потока: выше этого патч уже заметен пользователю.
  // Лаг таймера в 500 мс — та граница, за которой ввод в поле начинает
  // «залипать»: кадр держится дольше трёх периодов отрисовки.
  var PERF_BUDGET_MS = 150;
  var PERF_LAG_MS = 500;

  // Предел отчётов на страницу. Непрерывная проблема опишется первыми
  // же сводками; дальше это дубликаты, а место в журнале общее
  // со сборщиком исключений.
  var PERF_MAX_REPORTS = 60;

  var perfBuckets = {};   // имя → {calls, ms}
  var perfCounters = {};  // имя → число событий (мутации, сканы)
  var perfLagMax = 0;
  var perfLagSum = 0;
  var perfLagTicks = 0;
  var perfReports = 0;
  var perfLastTick = 0;

  function perfAdd(name, ms) {
    var b = perfBuckets[name];
    if (!b) { b = perfBuckets[name] = { calls: 0, ms: 0 }; }
    b.calls++;
    b.ms += ms;
  }

  /**
   * Обёртка-измеритель. Возвращает функцию с той же семантикой, что
   * и переданная: прибор не имеет права менять поведение того, что
   * измеряет, — включая исключения (finally, а не try/catch).
   */
  function perfWrap(name, fn) {
    if (!PERF_ENABLED) return fn;
    return function () {
      var t0 = performance.now();
      try {
        return fn.apply(this, arguments);
      } finally {
        perfAdd(name, performance.now() - t0);
      }
    };
  }

  function perfBump(name, n) {
    if (!PERF_ENABLED) return;
    perfCounters[name] = (perfCounters[name] || 0) + (n || 1);
  }

  /** Снимок размера дерева. Считается раз в интервал, не на мутацию. */
  function perfDom() {
    try {
      return {
        nodes: document.getElementsByTagName('*').length,
        messages: document.querySelectorAll('[data-testid="assistant-message"]').length,
        // Именно эти узлы обходит tagTimestampLines на каждой мутации.
        paragraphs: document.querySelectorAll('[data-testid="assistant-message"] p').length,
        inputs: document.querySelectorAll('[class*="inputContainer_"]').length,
      };
    } catch (e) {
      return { error: String(e && e.message || e) };
    }
  }

  function perfFlush() {
    var ticks = perfLagTicks;
    var lagAvg = ticks ? Math.round(perfLagSum / ticks) : 0;
    var oursMs = 0;
    var ours = {};
    for (var name in perfBuckets) {
      if (!perfBuckets.hasOwnProperty(name)) continue;
      var b = perfBuckets[name];
      oursMs += b.ms;
      ours[name] = { calls: b.calls, ms: Math.round(b.ms) };
    }

    var loud = perfReports === 0
      || oursMs >= PERF_BUDGET_MS
      || perfLagMax >= PERF_LAG_MS;

    if (loud && perfReports < PERF_MAX_REPORTS) {
      perfReports++;
      try {
        fetch('http://localhost:18923/webview-error', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            kind: 'perf',
            message: 'ours ' + Math.round(oursMs) + ' мс / '
              + (PERF_INTERVAL_MS / 1000) + ' с · лаг таймера макс '
              + Math.round(perfLagMax) + ' мс'
              + (perfReports === 1 ? ' · baseline' : ''),
            interval_sec: PERF_INTERVAL_MS / 1000,
            ours_ms: Math.round(oursMs),
            ours: ours,
            counters: perfCounters,
            lag: { max: Math.round(perfLagMax), avg: lagAvg, ticks: ticks },
            dom: perfDom(),
            safe_mode: cfg.safeMode === true,
            href: location.href.slice(0, 200),
            ts: Date.now(),
          }),
          keepalive: true,
        }).catch(function () {});
      } catch (e) {}
    }

    perfBuckets = {};
    perfCounters = {};
    perfLagMax = 0;
    perfLagSum = 0;
    perfLagTicks = 0;
  }

  function perfInit() {
    if (!PERF_ENABLED) return;
    perfLastTick = performance.now();
    // Лаг считается по секундному таймеру, а не по requestAnimationFrame:
    // rAF в скрытой вкладке не вызывается вовсе, и фоновые панели
    // рисовали бы картину «поток свободен» при любой загрузке.
    setInterval(function () {
      var now = performance.now();
      var lag = now - perfLastTick - 1000;
      perfLastTick = now;
      if (lag < 0) lag = 0;
      if (lag > perfLagMax) perfLagMax = lag;
      perfLagSum += lag;
      perfLagTicks++;
    }, 1000);
    setInterval(perfFlush, PERF_INTERVAL_MS);
  }

  // Наружу — чтобы модули футера мерили свои сканы тем же прибором:
  // вторая реализация счётчика разошлась бы с первой, и сравнивать
  // цифры из одного отчёта стало бы нельзя.
  window.__claudePerf = {
    wrap: perfWrap,
    bump: perfBump,
    enabled: PERF_ENABLED,
    /** Внеочередная сводка — для отладки из консоли. */
    flush: function () { perfFlush(); },
  };

  // === Locale-drift detector ===
  // Когда в DOM появляется выпадашка меню `/` ([class*="menuPopup_"]),
  // collectMenuItems() пробегает по всем её пунктам и собирает тройки
  // {section, label, title}. Дальше maybeCollectAndSend (debounced 500мс)
  // отправляет результат POST'ом на локальный http-server.py
  // (.claude/hooks/http-server.py, endpoint /locale-drift), который
  // перезаписывает .claude/hooks-runtime/locales-drift-pending.json.
  // Анализатор drift в localize.py читает этот файл и эмитит warning,
  // если найден дрейф (английский title там, где у нас должен быть
  // русский) или новая команда (label, которой нет в словаре).
  // localhost (а не 127.0.0.1) — для консистентности с уже работающим
  // fetch на /save-log; CSP webview допускает оба, но используем один и тот же.
  var LOCALE_DRIFT_URL = 'http://localhost:18923/locale-drift';
  var LOCALE_DRIFT_DEBOUNCE_MS = 500;
  var localeDriftTimer = null;
  var lastDriftHash = '';
  var driftSendInFlight = false;

  // Сборщик каталога моделей. Когда в DOM появляется popup селектора
  // моделей (`[class*="modelItem_"]`), отправляем на /models-list полный
  // список моделей (включая value-ID, который недоступен из textContent
  // и достаётся через React-fiber `.key`). Файл models-list.json — это
  // готовый каталог для построения внешнего переключателя моделей.
  var MODELS_LIST_URL = 'http://localhost:18923/models-list';
  var MODELS_LIST_DEBOUNCE_MS = 500;
  var modelsListTimer = null;
  var lastModelsHash = '';
  var modelsSendInFlight = false;

  /**
   * On-demand пинг через fetch к api.anthropic.com. Работает благодаря
   * патчу CSP в extension.js (connect-src https://api.anthropic.com).
   * Вызывается по клику на 📡 в overlay.
   */
  var onDemandPingState = 'idle'; // 'idle' | 'checking' | 'online' | 'offline' | 'csp_blocked'
  var onDemandPingTs = 0;
  var onDemandPingMs = 0; // время пинга в мс (только при online)
  var autoPingCount = 0;  // счётчик автоматических пингов за текущий processing
  var inlinePingClicked = false; // клик по 📡 в строке — показать результат

  function onDemandPing() {
    if (onDemandPingState === 'checking') return;
    onDemandPingState = 'checking';
    onDemandPingTs = Date.now();
    var startTime = Date.now();
    var controller = typeof AbortController !== 'undefined' ? new AbortController() : null;
    var timeoutId = setTimeout(function () {
      if (controller) controller.abort();
      onDemandPingState = 'offline';
      onDemandPingTs = Date.now();
      logInfo('on-demand ping timeout → offline');
    }, 5000);

    fetch('https://api.anthropic.com/', {
      method: 'HEAD',
      mode: 'no-cors',
      cache: 'no-store',
      signal: controller ? controller.signal : undefined,
    }).then(function () {
      clearTimeout(timeoutId);
      onDemandPingMs = Date.now() - startTime;
      onDemandPingState = 'online';
      onDemandPingTs = Date.now();
      logInfo('on-demand ping → online (' + onDemandPingMs + 'ms)');
    }).catch(function (e) {
      clearTimeout(timeoutId);
      var elapsed = Date.now() - startTime;
      if (e && e.name === 'AbortError') return; // timeout уже обработан
      if (elapsed < 200) {
        onDemandPingState = 'csp_blocked';
        logInfo('on-demand ping csp_blocked (' + elapsed + 'ms)');
      } else {
        onDemandPingState = 'offline';
        logInfo('on-demand ping offline (' + elapsed + 'ms)');
      }
      onDemandPingTs = Date.now();
    });
  }

  function logInfo() {
    if (!LOGS_ENABLED) return;
    try { console.log.apply(console, ['[claude-custom]'].concat([].slice.call(arguments))); } catch (_) {}
  }
  function logWarn() {
    if (!LOGS_ENABLED) return;
    try { console.warn.apply(console, ['[claude-custom]'].concat([].slice.call(arguments))); } catch (_) {}
  }

  /**
   * ===== Compact-confirm popup =====
   *
   * Иконка автосжатия — `<button class="usage_<hash> usageButtonV2_<hash>"
   * title="N% context used — click to compact">` в правом нижнем углу
   * inputFooter. Один клик по ней мгновенно запускает /compact, что
   * легко сделать случайно. Перехватываем click в capture-фазе и
   * показываем confirmation popup рядом с иконкой; реальный compact
   * запускается только при подтверждении («Сжать»).
   *
   * Логика обхода capture-listener'а: при подтверждении ставим
   * dataset.compactConfirmed='true', потом target.click(). При повторном
   * проходе через наш listener этот флаг прочитан и удалён, событие
   * пропускается дальше — React-handler срабатывает.
   *
   * Стили popup'а через VSCode CSS-vars (--vscode-editorHoverWidget-*),
   * чтобы выглядел как нативный hover-tooltip того же UI.
   */

  var COMPACT_CONFIRM_ID = 'claude-compact-confirm';

  function hideCompactConfirmPopup() {
    var popup = document.getElementById(COMPACT_CONFIRM_ID);
    if (popup) popup.remove();
    document.removeEventListener('click', compactOutsideClickHandler, true);
    document.removeEventListener('keydown', compactKeydownHandler, true);
  }

  function compactOutsideClickHandler(e) {
    var popup = document.getElementById(COMPACT_CONFIRM_ID);
    if (popup && !popup.contains(e.target)) {
      // Если клик за пределами — закрыть; click на самой usage-кнопке
      // тоже считаем «закрытием», чтобы повторный клик не открывал
      // дублирующий popup.
      hideCompactConfirmPopup();
    }
  }

  function compactKeydownHandler(e) {
    if (e.key === 'Escape') {
      hideCompactConfirmPopup();
    } else if (e.key === 'Enter') {
      var yesBtn = document.getElementById('claude-compact-yes');
      if (yesBtn) yesBtn.click();
    }
  }

  /**
   * Показывает confirmation popup для /compact.
   *
   * @param target — DOM-узел, относительно которого позиционируется popup.
   * @param onConfirm — опциональный callback. Если задан, вызывается
   *   вместо штатного `target.click()` (нужно для debug-overlay-иконки,
   *   когда настоящей usage-кнопки в DOM ещё нет — кликать нечего).
   */
  function showCompactConfirmPopup(target, onConfirm) {
    hideCompactConfirmPopup();

    var popup = document.createElement('div');
    popup.id = COMPACT_CONFIRM_ID;
    popup.style.cssText = [
      'position: fixed',
      'background: var(--vscode-editorHoverWidget-background, rgba(40,40,40,0.97))',
      'color: var(--vscode-editorHoverWidget-foreground, #ddd)',
      'border: 1px solid var(--vscode-editorHoverWidget-border, rgba(255,255,255,0.1))',
      'border-radius: 6px',
      'padding: 10px 12px',
      'font-size: 13px',
      'font-family: -apple-system, BlinkMacSystemFont, sans-serif',
      'box-shadow: 0 4px 14px rgba(0,0,0,0.35)',
      'z-index: 100000',
      'min-width: 200px',
    ].join(';');

    var msg = document.createElement('div');
    msg.style.cssText = 'margin-bottom: 10px;';
    msg.textContent = 'Сжать контекст сейчас?';
    popup.appendChild(msg);

    var btnRow = document.createElement('div');
    btnRow.style.cssText = 'display: flex; gap: 8px;';

    function makeBtn(id, label, primary) {
      var b = document.createElement('button');
      b.id = id;
      b.type = 'button';
      b.textContent = label;
      var bg = primary
        ? 'var(--vscode-button-background, #0e639c)'
        : 'var(--vscode-button-secondaryBackground, #3a3d41)';
      var fg = primary
        ? 'var(--vscode-button-foreground, #fff)'
        : 'var(--vscode-button-secondaryForeground, #ccc)';
      b.style.cssText = [
        'flex: 1',
        'padding: 5px 10px',
        'cursor: pointer',
        'background: ' + bg,
        'color: ' + fg,
        'border: none',
        'border-radius: 4px',
        'font-size: 12px',
        'font-family: inherit',
      ].join(';');
      return b;
    }

    // «Сжать» — attention-стиль (жёлтый): операция необратимая, но
    // не разрушительная как «Очистить разговор», поэтому warning, а не
    // destructive. Текст оставляем не-жирным, чтобы не звать к клику.
    var yesBtn = makeBtn('claude-compact-yes', 'Сжать', true);
    // Цвет — ярко-жёлтый напрямую, без VSCode-переменной:
    // --vscode-statusBarItem-warningBackground = #cca700 (горчичный),
    // что в Electron-webview выглядит тускло. Берём чистый amber.
    yesBtn.style.background = '#FBCD44';
    yesBtn.style.color = '#000';
    // «Отмена» — primary-стиль (синий) + bold, чтобы безопасное
    // действие визуально доминировало (см. тот же паттерн в
    // showClearConfirmPopup).
    var noBtn = makeBtn('claude-compact-no', 'Отмена', true);
    noBtn.style.fontWeight = 'bold';
    btnRow.appendChild(yesBtn);
    btnRow.appendChild(noBtn);
    popup.appendChild(btnRow);

    document.body.appendChild(popup);

    // Позиционирование над иконкой; если не помещается — под ней.
    var rect = target.getBoundingClientRect();
    var pRect = popup.getBoundingClientRect();
    var top = rect.top - pRect.height - 8;
    if (top < 8) top = rect.bottom + 8;
    var left = rect.left + rect.width / 2 - pRect.width / 2;
    if (left < 8) left = 8;
    if (left + pRect.width > window.innerWidth - 8) {
      left = window.innerWidth - pRect.width - 8;
    }
    popup.style.top = top + 'px';
    popup.style.left = left + 'px';

    yesBtn.addEventListener('click', function (e) {
      e.stopPropagation();
      hideCompactConfirmPopup();
      if (typeof onConfirm === 'function') {
        // Альтернативный путь: вызов из debug-overlay, где target —
        // фиктивная иконка-триггер, а реальное действие /compact
        // отправляется коллбэком через textarea.
        onConfirm();
        return;
      }
      target.dataset.compactConfirmed = 'true';
      // Триггерим оригинальный click; наш capture-listener увидит флаг
      // и пропустит событие дальше — React-handler запустит compact.
      target.click();
    });
    noBtn.addEventListener('click', function (e) {
      e.stopPropagation();
      hideCompactConfirmPopup();
    });

    // Регистрируем outside-click и Escape с задержкой, чтобы текущее
    // событие click (которое и открыло popup) не закрыло его сразу же.
    setTimeout(function () {
      document.addEventListener('click', compactOutsideClickHandler, true);
      document.addEventListener('keydown', compactKeydownHandler, true);
    }, 0);

    yesBtn.focus();
  }

  /**
   * Отправляет slash-команду (например, "/compact") как сообщение
   * пользователя — нужен для случаев, когда нативной кнопки в UI нет
   * (например, /compact icon появляется только при заполненном контексте).
   *
   * React-controlled textarea игнорирует обычное `ta.value = ...`,
   * поэтому используем native value setter из прототипа
   * HTMLTextAreaElement — он триггерит React-tracker, и компонент
   * подхватывает значение. Затем dispatch input → keydown Enter.
   *
   * Возвращает true, если textarea найдена, false иначе.
   */
  function triggerSlashCommandViaInput(cmd) {
    // Composer в Claude Code 2.x — это <div contentEditable="plaintext-only"
    // role="textbox" aria-label="Message input">, НЕ <textarea>. Поэтому
    // ищем contenteditable, а не textarea.
    var input =
      document.querySelector('[role="textbox"][contenteditable][aria-label*="essage" i]') ||
      document.querySelector('[role="textbox"][contenteditable]') ||
      document.querySelector('div[contenteditable]');
    if (!input) {
      logWarn('triggerSlashCommand: composer (contenteditable) not found');
      return false;
    }
    try {
      input.focus();
      // execCommand('insertText') триггерит native input event с
      // правильным inputType, который React-handler onInput подхватывает
      // как штатный ввод (в отличие от прямой записи в textContent).
      // selectAll + insertText заменяет существующий текст (если был).
      document.execCommand('selectAll', false, null);
      var inserted = document.execCommand('insertText', false, cmd);
      if (!inserted) {
        // Fallback: ручная установка textContent + диспатч InputEvent.
        input.textContent = cmd;
        input.dispatchEvent(new InputEvent('input', {
          bubbles: true, cancelable: true,
          inputType: 'insertText', data: cmd,
        }));
      }
      logInfo('triggerSlashCommand: set', JSON.stringify(cmd),
        '→ textContent=', JSON.stringify(input.textContent));

      setTimeout(function () {
        // (1) Найти кнопку отправки (sendButton_/sendIcon_/aria-label).
        var sendBtn = null;
        var p = input.parentElement;
        while (p && p !== document.body && !sendBtn) {
          sendBtn = p.querySelector(
            'button[class*="sendButton_"],' +
            'button[class*="sendIcon_"],' +
            'button[aria-label*="send" i],' +
            'button[aria-label*="отправ" i]'
          );
          p = p.parentElement;
        }
        if (sendBtn && !sendBtn.disabled) {
          sendBtn.click();
          logInfo('triggerSlashCommand: clicked send button',
            sendBtn.getAttribute('aria-label') || sendBtn.className);
          return;
        }
        if (sendBtn) {
          logWarn('triggerSlashCommand: send button found but disabled');
        }
        // (2) Last resort: Enter keydown — React's onKeyDown handler
        // обычно его слушает (см. webview/index.js: r.key==="Enter").
        input.dispatchEvent(new KeyboardEvent('keydown', {
          key: 'Enter', code: 'Enter', keyCode: 13, which: 13,
          bubbles: true, cancelable: true,
        }));
        logInfo('triggerSlashCommand: dispatched Enter on composer');
      }, 50);
      return true;
    } catch (e) {
      logWarn('triggerSlashCommandViaInput failed:', e && e.message ? e.message : e);
      return false;
    }
  }

  function compactClickInterceptor(e) {
    var target = e.target.closest && e.target.closest('button[class*="usage_"]');
    if (!target) return;
    // Не дёргаем подтверждение для кнопок без характерного title:
    // фильтр снижает риск зацепить чужой usage_ от другого компонента.
    var title = target.getAttribute('title') || '';
    if (title.indexOf('compact') < 0 && title.indexOf('сжат') < 0) return;

    if (target.dataset.compactConfirmed === 'true') {
      delete target.dataset.compactConfirmed;
      return; // прошли подтверждение — пропускаем дальше к React
    }
    e.stopPropagation();
    e.preventDefault();
    showCompactConfirmPopup(target);
  }

  /**
   * Confirmation для пункта меню `/` «Очистить разговор». По аналогии
   * с compactClickInterceptor: capture-listener, dataset-флаг для
   * пропуска повторного click'а. Идентификация цели — по label в
   * `[class*="commandLabel_"]` (точное совпадение с RU- или EN-вариантом
   * после локализации словарём). Сам action React'а — открыть новый чат
   * без сохранения текущего; легко сделать случайно, потому и confirm.
   */
  var CLEAR_CONFIRM_ID = 'claude-clear-confirm';

  function hideClearConfirmPopup() {
    var popup = document.getElementById(CLEAR_CONFIRM_ID);
    if (popup) popup.remove();
    document.removeEventListener('click', clearOutsideClickHandler, true);
    document.removeEventListener('keydown', clearKeydownHandler, true);
  }

  function clearOutsideClickHandler(e) {
    var popup = document.getElementById(CLEAR_CONFIRM_ID);
    if (popup && !popup.contains(e.target)) hideClearConfirmPopup();
  }

  function clearKeydownHandler(e) {
    if (e.key === 'Escape') {
      hideClearConfirmPopup();
    } else if (e.key === 'Enter') {
      var yesBtn = document.getElementById('claude-clear-yes');
      if (yesBtn) yesBtn.click();
    }
  }

  function showClearConfirmPopup(target) {
    hideClearConfirmPopup();

    var popup = document.createElement('div');
    popup.id = CLEAR_CONFIRM_ID;
    popup.style.cssText = [
      'position: fixed',
      'background: var(--vscode-editorHoverWidget-background, rgba(40,40,40,0.97))',
      'color: var(--vscode-editorHoverWidget-foreground, #ddd)',
      'border: 1px solid var(--vscode-editorHoverWidget-border, rgba(255,255,255,0.1))',
      'border-radius: 6px',
      'padding: 10px 12px',
      'font-size: 13px',
      'font-family: -apple-system, BlinkMacSystemFont, sans-serif',
      'box-shadow: 0 4px 14px rgba(0,0,0,0.35)',
      'z-index: 100000',
      'min-width: 240px',
    ].join(';');

    var msg = document.createElement('div');
    msg.style.cssText = 'margin-bottom: 10px;';
    msg.textContent = 'Очистить разговор? Текущий чат будет удален.';
    popup.appendChild(msg);

    var btnRow = document.createElement('div');
    btnRow.style.cssText = 'display: flex; gap: 8px;';

    function makeBtn(id, label, primary) {
      var b = document.createElement('button');
      b.id = id;
      b.type = 'button';
      b.textContent = label;
      var bg = primary
        ? 'var(--vscode-button-background, #0e639c)'
        : 'var(--vscode-button-secondaryBackground, #3a3d41)';
      var fg = primary
        ? 'var(--vscode-button-foreground, #fff)'
        : 'var(--vscode-button-secondaryForeground, #ccc)';
      b.style.cssText = [
        'flex: 1',
        'padding: 5px 10px',
        'cursor: pointer',
        'background: ' + bg,
        'color: ' + fg,
        'border: none',
        'border-radius: 4px',
        'font-size: 12px',
        'font-family: inherit',
      ].join(';');
      return b;
    }

    // «Очистить» — деструктивное действие (удаляет текущий чат), красный
    // фон вместо синего primary; шрифт остаётся обычным, чтобы кнопка
    // не «звала» к клику.
    var yesBtn = makeBtn('claude-clear-yes', 'Очистить', true);
    yesBtn.style.background =
      'var(--vscode-statusBarItem-errorBackground, #c42b1c)';
    yesBtn.style.color =
      'var(--vscode-statusBarItem-errorForeground, #fff)';
    // «Отмена» — безопасное действие по умолчанию: синий primary + bold,
    // чтобы визуально доминировала и снижала риск случайного клика по
    // красной деструктивной кнопке.
    var noBtn = makeBtn('claude-clear-no', 'Отмена', true);
    noBtn.style.fontWeight = 'bold';
    btnRow.appendChild(yesBtn);
    btnRow.appendChild(noBtn);
    popup.appendChild(btnRow);

    document.body.appendChild(popup);

    // Позиционирование: над пунктом «Очистить разговор», прижато к
    // левому краю самого пункта (чтобы попап стоял прямо над текстом
    // лейбла, а не плавал по центру). Если сверху не помещается —
    // сдвигаем под пункт; по горизонтали — clamp к viewport'у.
    var rect = target.getBoundingClientRect();
    var pRect = popup.getBoundingClientRect();
    var top = rect.top - pRect.height - 8;
    if (top < 8) top = rect.bottom + 8;
    var left = rect.left;
    if (left < 8) left = 8;
    if (left + pRect.width > window.innerWidth - 8) {
      left = window.innerWidth - pRect.width - 8;
    }
    popup.style.top = top + 'px';
    popup.style.left = left + 'px';

    yesBtn.addEventListener('click', function (e) {
      e.stopPropagation();
      hideClearConfirmPopup();
      target.dataset.clearConfirmed = 'true';
      target.click();
    });
    noBtn.addEventListener('click', function (e) {
      e.stopPropagation();
      hideClearConfirmPopup();
    });

    setTimeout(function () {
      document.addEventListener('click', clearOutsideClickHandler, true);
      document.addEventListener('keydown', clearKeydownHandler, true);
    }, 0);

    yesBtn.focus();
  }

  function clearClickInterceptor(e) {
    var target = e.target.closest && e.target.closest('[class*="commandItem_"]');
    if (!target) return;
    var labelEl = target.querySelector('[class*="commandLabel_"]');
    var label = labelEl ? (labelEl.textContent || '').trim() : '';
    // Сравниваем с обеими локалями: словарь подменяет RU после первого
    // открытия меню, но при cold-start меню сначала появляется в EN.
    if (label !== 'Очистить разговор' && label !== 'Clear conversation') return;

    if (target.dataset.clearConfirmed === 'true') {
      delete target.dataset.clearConfirmed;
      return;
    }
    e.stopPropagation();
    e.preventDefault();
    showClearConfirmPopup(target);
  }

  /**
   * Помечает абзацы ассистентского сообщения, начинающиеся с эмодзи 📬,
   * классом `.claude-ts-line` (на него рассчитан CSS из claude-custom.css).
   */
  function tagTimestampLines() {
    var nodes = document.querySelectorAll(
      '[data-testid="assistant-message"] p:not(.claude-ts-line)'
    );
    for (var i = 0; i < nodes.length; i++) {
      var p = nodes[i];
      var text = (p.textContent || '').replace(/^[\s ]+/, '');
      if (text.indexOf('\u{1F4EC} ') === 0) {
        p.classList.add('claude-ts-line');
      }
    }
  }

  /**
   * Перевешивает <link rel="stylesheet"> к claude-custom.css с уникальным
   * query-параметром, чтобы браузер качал файл заново. <link> идёт под
   * CSP `style-src vscode-resource:` — это работает, в отличие от fetch
   * (его CSP `connect-src` режет). Старый <link> удаляется только после
   * успешной загрузки нового.
   *
   * Не делает refresh, если вкладка не активна (`document.visibilityState
   * !== 'visible'`) — фоновые webview-панели не дёргают диск и не плодят
   * `?t=…` URL'ы в DevTools Sources. При активации вкладки сработает
   * handler на `visibilitychange` (см. init), который догоняет CSS.
   */
  var lastCssRefresh = 0;
  function refreshCustomCss() {
    refreshCalls++;
    // visibilityState в Anthropic-webview всегда 'visible', поэтому
    // используем document.hasFocus(): только сфокусированная вкладка
    // VSCode действительно «активная». Это даёт нам нужное поведение —
    // refreshCustomCss работает только в активной вкладке.
    if (!document.hasFocus()) {
      refreshSkippedNotFocused++;
      return;
    }
    var now = Date.now();
    if (now - lastCssRefresh < THROTTLE_MS) {
      refreshSkippedThrottled++;
      return;
    }
    lastCssRefresh = now;
    refreshExecuted++;

    // Подстраховка: удалить «зависший» <style> от старой fetch-реализации,
    // если он остался в DOM — иначе он будет перебивать свежий <link>.
    var staleStyle = document.getElementById('claude-custom-style');
    if (staleStyle && staleStyle.parentNode) {
      staleStyle.parentNode.removeChild(staleStyle);
    }

    var existing = document.getElementById('claude-custom-css');
    if (!existing) return;
    var baseHref = (existing.href || '').split('?')[0];
    if (!baseHref) return;

    var fresh = document.createElement('link');
    fresh.rel = 'stylesheet';
    fresh.href = baseHref + '?t=' + now;
    fresh.onload = function () {
      if (existing.parentNode) existing.parentNode.removeChild(existing);
      fresh.id = 'claude-custom-css';
      logInfo('css link refreshed at', new Date().toISOString());
    };
    fresh.onerror = function () {
      if (fresh.parentNode) fresh.parentNode.removeChild(fresh);
      logWarn('css link load failed');
    };
    document.head.appendChild(fresh);
  }

  /**
   * Persistent debug-overlay в правом верхнем углу webview. Виден на
   * любой вкладке (активной и неактивной). Показывает обратный отсчёт
   * до следующего CSS-poll'а. При visibility-resume переключается в
   * режим «refresh через …» и потом возвращается в обычный режим.
   *
   * Стили инлайн (cssText), чтобы overlay не зависел от перезагружаемого
   * claude-custom.css.
   */
  var initTimeMs = Date.now();
  var lastPollAt = initTimeMs;
  var overlayMode = 'poll'; // 'poll' | 'visibility'
  var visibilityResumeAt = 0;

  // Диагностика — для понимания, почему visibility-resume countdown
  // не срабатывает в этом расширении (см. issue от 2026-04-28).
  var lastVisibilityState = document.visibilityState;
  var lastVisibilityChangeAt = null;
  var lastVisibilityChangeNewState = null;
  var visibilityChangeCount = 0;
  var lastFocusState = document.hasFocus();
  var lastFocusChangeAt = null;
  var focusChangeCount = 0;
  // Счётчики refreshCustomCss: показывают, что фильтры действительно
  // блокируют ненужные refresh'и.
  var refreshCalls = 0;
  var refreshSkippedNotFocused = 0;
  var refreshSkippedThrottled = 0;
  var refreshExecuted = 0;

  // Лог переходов состояний сессии — хранит последние MAX_STATE_LOG
  // записей с таймстампами. Копируется через 📋.
  var MAX_STATE_LOG_DISPLAY = 20; // сколько показывать в overlay
  var stateLog = [];              // полный лог (без лимита)
  var prevTrackedStates = {};

  // Текущие состояния — используются и в overlay, и в inline-индикаторе
  var busyState = false;
  var actuallyStreaming = false;
  var busyNoStreamSec = 0;
  var contentSilenceSec = 0;
  var lastDomMutationAt = Date.now(); // последнее изменение DOM (любое)
  var lastContentLen = 0;             // длина контента последнего assistant-message
  var lastContentChangeAt = Date.now(); // когда контент последний раз менялся

  function trackStateChange(key, newVal) {
    var prev = prevTrackedStates[key];
    if (prev === newVal) return;
    prevTrackedStates[key] = newVal;
    if (prev === undefined) return; // первичная инициализация — не логируем
    var ts = new Date().toLocaleTimeString('ru-RU', {hour12: false});
    var entry = ts + ' ' + key + ': ' + prev + ' → ' + newVal;
    stateLog.push(entry);
  }

  // Доступ к session-store через React Fiber tree.
  // sessionStore — экземпляр класса Wn (см. webview/index.js): содержит
  // .activeSession (signal с текущей session) и .sessions (signal-массив).
  // session — экземпляр eX: .busy, .connection, .error, .messages, и т.п.
  var sessionStore = null;       // Wn-instance
  var sessionStoreFoundAt = null;
  var lastSessionFindAttempt = 0;
  var sessionFindDiag = '';      // диагностика последнего поиска

  function ensureDebugOverlay() {
    var overlay = document.getElementById('claude-custom-debug');
    if (overlay) return overlay;
    overlay = document.createElement('div');
    overlay.id = 'claude-custom-debug';
    overlay.style.cssText =
      'position:fixed;top:12px;right:12px;z-index:99999;' +
      'background:rgba(0,0,0,0.85);color:#fff;' +
      'padding:8px 14px;border-radius:6px;' +
      'font:12px/1.4 monospace;pointer-events:none;user-select:none;' +
      'box-shadow:0 2px 8px rgba(0,0,0,0.5);' +
      'min-width:240px;max-width:50vw;max-height:80vh;' +
      'overflow:auto;text-align:left;white-space:pre-wrap;word-break:break-all;';
    // Свёрнутая панель: statusIcon + ➕ + 📋 (горизонтальная полоска)
    var miniBar = document.createElement('div');
    miniBar.id = 'claude-custom-debug-mini';
    miniBar.style.cssText =
      'display:none;align-items:center;gap:6px;' +
      'pointer-events:auto;user-select:none;';

    var miniStatus = document.createElement('span');
    miniStatus.id = 'claude-custom-debug-mini-status';
    miniStatus.style.cssText = 'font-size:14px;';
    miniBar.appendChild(miniStatus);

    // Текстовый контейнер — updateDebugOverlay обновляет его, а не overlay
    var textDiv = document.createElement('div');
    textDiv.id = 'claude-custom-debug-text';
    overlay.appendChild(textDiv);

    /* Метка «это наш узел» — по ней базовый наблюдатель отличает свои
     * мутации от чужих (см. init). Оверлей живёт в том же body, что и
     * приложение, и его записи иначе выглядели бы как активность
     * страницы: обновляли бы отметку времени последней мутации, на
     * которой держится детектор тишины, и запускали бы обход дерева.
     * То есть патч будил бы сам себя.
     *
     * Помечаются ровно те узлы, в которые мы пишем: цель мутации —
     * всегда один из них, а не корень оверлея. Проверка по свойству,
     * а не через `contains`: она стоит одно сравнение на запись, а
     * записей в шторме тысячи. */
    overlay.__claudeOwnNode = true;
    textDiv.__claudeOwnNode = true;
    miniBar.__claudeOwnNode = true;
    miniStatus.__claudeOwnNode = true;

    // Панель иконок (правый верхний угол, видна только в развёрнутом виде)
    var iconsBar = document.createElement('div');
    iconsBar.id = 'claude-custom-debug-icons';
    iconsBar.style.cssText =
      'position:absolute;top:4px;right:6px;display:flex;gap:6px;' +
      'align-items:center;pointer-events:auto;user-select:none;';

    var isMinimized = true;
    textDiv.style.display = 'none';
    iconsBar.style.display = 'none';
    miniBar.style.display = 'flex';
    overlay.style.minWidth = 'auto';
    overlay.style.maxWidth = 'auto';
    overlay.style.padding = '4px 8px';

    function btnStyle() { return 'cursor:pointer;font-size:13px;opacity:0.6;'; }
    function hoverIn(el) { el.style.opacity = '1'; }
    function hoverOut(el) { el.style.opacity = '0.6'; }

    // Кнопка сворачивания (в развёрнутом виде)
    var collapseBtn = document.createElement('span');
    collapseBtn.textContent = '▲';
    collapseBtn.title = 'Свернуть';
    collapseBtn.style.cssText = btnStyle() + 'font-size:20px;line-height:1;position:relative;top:-2px;';
    collapseBtn.addEventListener('mouseenter', function () { hoverIn(collapseBtn); });
    collapseBtn.addEventListener('mouseleave', function () { hoverOut(collapseBtn); });
    collapseBtn.addEventListener('click', function () {
      isMinimized = true;
      textDiv.style.display = 'none';
      iconsBar.style.display = 'none';
      miniBar.style.display = 'flex';
      overlay.style.minWidth = 'auto';
      overlay.style.maxWidth = 'auto';
      overlay.style.padding = '4px 8px';
      collapseBtn.textContent = '▲';
    });

    // Кнопка копирования (в развёрнутом виде)
    var copyBtn = document.createElement('span');
    copyBtn.textContent = '📋';
    copyBtn.title = 'Скопировать содержимое';
    copyBtn.style.cssText = btnStyle();
    copyBtn.addEventListener('mouseenter', function () { hoverIn(copyBtn); });
    copyBtn.addEventListener('mouseleave', function () { hoverOut(copyBtn); });
    copyBtn.addEventListener('click', function () {
      try {
        // Копируем overlay-текст + ПОЛНЫЙ лог (не обрезанный)
        var overlayText = textDiv.textContent || '';
        // Заменяем отображаемый лог на полный
        var displayLogHeader = '—— state log (' + stateLog.length + ' total) ——';
        var idx = overlayText.indexOf('—— state log');
        var text;
        if (idx >= 0) {
          text = overlayText.substring(0, idx) +
            '—— FULL state log (' + stateLog.length + ' entries) ——\n' +
            stateLog.join('\n');
        } else {
          text = overlayText + '\n—— FULL state log (' + stateLog.length + ' entries) ——\n' +
            stateLog.join('\n');
        }
        navigator.clipboard.writeText(text).then(function () {
          copyBtn.textContent = '✅';
          setTimeout(function () { copyBtn.textContent = '📋'; }, 1500);
        });
      } catch (e) {
        try {
          var range = document.createRange();
          range.selectNodeContents(textDiv);
          var sel = window.getSelection();
          sel.removeAllRanges();
          sel.addRange(range);
          document.execCommand('copy');
          sel.removeAllRanges();
          copyBtn.textContent = '✅';
          setTimeout(function () { copyBtn.textContent = '📋'; }, 1500);
        } catch (e2) {}
      }
    });

    // Кнопка пинга
    var pingBtn = document.createElement('span');
    pingBtn.textContent = '📡';
    pingBtn.title = 'Проверить интернет (on-demand ping)';
    pingBtn.style.cssText = btnStyle();
    pingBtn.addEventListener('mouseenter', function () { hoverIn(pingBtn); });
    pingBtn.addEventListener('mouseleave', function () { hoverOut(pingBtn); });
    pingBtn.addEventListener('click', function () {
      onDemandPing();
      pingBtn.textContent = '⏳';
      // Возвращаем 📡 как только пинг завершится (не по таймеру)
      var checkId = setInterval(function () {
        if (onDemandPingState !== 'checking') {
          clearInterval(checkId);
          pingBtn.textContent = '📡';
        }
      }, 100);
      // Safety: максимум 10 сек
      setTimeout(function () { clearInterval(checkId); pingBtn.textContent = '📡'; }, 10000);
    });

    // Кнопка сохранения лога в файл
    var saveBtn = document.createElement('span');
    saveBtn.textContent = '💾';
    saveBtn.title = 'Сохранить лог в файл';
    saveBtn.style.cssText = btnStyle();
    saveBtn.addEventListener('mouseenter', function () { hoverIn(saveBtn); });
    saveBtn.addEventListener('mouseleave', function () { hoverOut(saveBtn); });
    saveBtn.addEventListener('click', function () {
      var text = (textDiv.textContent || '').trim();
      saveBtn.textContent = '⏳';
      fetch('http://localhost:18923/save-log', {
        method: 'POST',
        headers: { 'Content-Type': 'text/plain' },
        body: text,
      }).then(function (r) { return r.json(); })
        .then(function (data) {
          if (data.status === 'saved') {
            saveBtn.textContent = '✅';
            saveBtn.title = 'Сохранено: ' + data.file;
            logInfo('log saved:', data.path);
          } else {
            saveBtn.textContent = '❌';
            logWarn('save error:', data.error);
          }
          setTimeout(function () { saveBtn.textContent = '💾'; saveBtn.title = 'Сохранить лог в файл'; }, 2000);
        })
        .catch(function (e) {
          saveBtn.textContent = '❌';
          saveBtn.title = 'HTTP-сервер недоступен';
          logWarn('save failed:', e);
          setTimeout(function () { saveBtn.textContent = '💾'; saveBtn.title = 'Сохранить лог в файл'; }, 2000);
        });
    });

    // Кнопка вызова /compact с подтверждением. Нужна, когда нативная
    // usage-иконка ещё не показалась (контекст не заполнен), но хочется
    // принудительно сжать историю или просто проверить confirm-popup.
    var compactBtn = document.createElement('span');
    compactBtn.textContent = '🗜';
    compactBtn.title = '/compact (с подтверждением)';
    compactBtn.style.cssText = btnStyle();
    compactBtn.addEventListener('mouseenter', function () { hoverIn(compactBtn); });
    compactBtn.addEventListener('mouseleave', function () { hoverOut(compactBtn); });
    compactBtn.addEventListener('click', function (e) {
      // stopPropagation, чтобы outside-click handler popup'а (если уже
      // открыт другой) не среагировал на этот же click.
      e.stopPropagation();
      showCompactConfirmPopup(compactBtn, function () {
        triggerSlashCommandViaInput('/compact');
      });
    });

    // Быстрые переключатели модели — короткие текстовые кнопки S/O/H.
    // Клик по кнопке открывает модельный popup через UI и кликает нужный
    // modelItem_ напрямую (без отправки сообщения в чат).
    // Алиасы взяты из models-list.json:
    // `default` → Sonnet, `opus` → Opus 4.7, `haiku` → Haiku 4.5.

    // Ждёт появления элемента в DOM (не более maxWait мс), затем вызывает cb(true/false).
    function waitForElement(selector, maxWait, cb) {
      var waited = 0;
      var interval = 60;
      function check() {
        if (document.querySelector(selector)) { cb(true); return; }
        waited += interval;
        if (waited < maxWait) { setTimeout(check, interval); } else { cb(false); }
      }
      setTimeout(check, interval);
    }

    // Ищет modelItem_ с нужным alias через React-fiber key и кликает его.
    function tryClickModelItemByKey(alias) {
      var items = document.querySelectorAll('[class*="modelItem_"]');
      for (var i = 0; i < items.length; i++) {
        var el = items[i];
        for (var k in el) {
          if (k.indexOf('__reactFiber') === 0) {
            var fiber = el[k];
            if (fiber && fiber.key === alias) { el.click(); return true; }
            break;
          }
        }
      }
      return false;
    }

    // Ищет кнопку, открывающую модельный popup. Пробует:
    //   1. Классовые паттерны `modelSelector_` / `modelTrigger_` / `modelPicker_`
    //   2. Кнопку «Show command menu (/)» → потом «Сменить модель…»
    function findModelSelectorTrigger() {
      var patterns = [
        '[class*="modelSelector_"]',
        '[class*="modelTrigger_"]',
        '[class*="modelPicker_"]',
        '[class*="modelChooser_"]',
      ];
      for (var i = 0; i < patterns.length; i++) {
        var el = document.querySelector(patterns[i]);
        if (el) return el;
      }
      return null;
    }

    // Ищет кнопку команд-меню «Show command menu (/)» по title / aria-label.
    function findCommandMenuButton() {
      return (
        document.querySelector('button[title="Show command menu (/)"]') ||
        document.querySelector('button[aria-label="Show command menu (/)"]') ||
        document.querySelector('button[title*="command menu" i]') ||
        document.querySelector('button[aria-label*="command menu" i]')
      );
    }

    // Ищет commandItem_ в открытом меню по тексту label.
    function findMenuItemByText(text) {
      var items = document.querySelectorAll('[class*="commandItem_"]');
      for (var i = 0; i < items.length; i++) {
        if (items[i].textContent.indexOf(text) !== -1) return items[i];
      }
      return null;
    }

    // === Silent-режим: прячем popup'ы пока идёт автоматический клик ===
    // CSS-правило сидит постоянно, активируется через body[data-claude-silent-switch="1"].
    // visibility:hidden НЕ блокирует программный .click(), только убирает визуальный
    // показ. Так что React-handler модельного пункта срабатывает штатно, но
    // пользователь не видит «всплытие → исчезновение» меню.
    function ensureSilentStyleInjected() {
      if (document.getElementById('claude-silent-switch-style')) return;
      var s = document.createElement('style');
      s.id = 'claude-silent-switch-style';
      s.textContent = [
        'body[data-claude-silent-switch="1"] [class*="menuPopup_"],',
        'body[data-claude-silent-switch="1"] [class*="popupContent_"],',
        'body[data-claude-silent-switch="1"] [class*="popupRoot_"],',
        'body[data-claude-silent-switch="1"] [class*="popup_"],',
        'body[data-claude-silent-switch="1"] [class*="modelItem_"],',
        'body[data-claude-silent-switch="1"] [class*="commandItem_"],',
        'body[data-claude-silent-switch="1"] [class*="sectionHeader_"],',
        'body[data-claude-silent-switch="1"] [role="menu"],',
        'body[data-claude-silent-switch="1"] [role="listbox"]',
        '{ visibility: hidden !important; opacity: 0 !important; }',
      ].join('\n');
      document.head.appendChild(s);
    }
    function beginSilentSwitch() {
      ensureSilentStyleInjected();
      document.body.setAttribute('data-claude-silent-switch', '1');
    }
    function endSilentSwitch() {
      document.body.removeAttribute('data-claude-silent-switch');
    }

    // Главная функция переключения модели.
    // Сначала пробует прямой клик (если popup уже открыт).
    // Иначе — ищет прямую кнопку-триггер или идёт через command-menu.
    // По умолчанию silent=true: popup-контейнеры спрятаны на время операции.
    function switchModel(alias, letter, btn, silent) {
      if (silent !== false) silent = true;

      function finish(success) {
        if (silent) endSilentSwitch();
        btn.textContent = success ? '✓' : '✗';
        setTimeout(function () { btn.textContent = letter; }, 1200);
      }

      if (silent) beginSilentSwitch();

      // Popup уже открыт?
      if (tryClickModelItemByKey(alias)) {
        logInfo('switchModel: direct click on open popup →', alias);
        finish(true);
        return;
      }

      btn.textContent = '⏳';

      // Прямая кнопка-триггер модели?
      var directTrigger = findModelSelectorTrigger();
      if (directTrigger) {
        logInfo('switchModel: clicking direct trigger', directTrigger.className);
        directTrigger.click();
        waitForElement('[class*="modelItem_"]', 1500, function (found) {
          if (!found) {
            logWarn('switchModel: model popup did not appear after trigger');
            finish(false);
            return;
          }
          finish(tryClickModelItemByKey(alias));
        });
        return;
      }

      // Fallback: command menu → «Сменить модель…» → modelItem_
      var menuBtn = findCommandMenuButton();
      if (!menuBtn) {
        logWarn('switchModel: neither modelTrigger nor commandMenuBtn found');
        finish(false);
        return;
      }
      logInfo('switchModel: opening command menu…');
      menuBtn.click();
      waitForElement('[class*="commandItem_"]', 1500, function (menuFound) {
        if (!menuFound) {
          logWarn('switchModel: command menu did not open');
          finish(false);
          return;
        }
        var modelMenuItem = findMenuItemByText('Сменить модель') || findMenuItemByText('model');
        if (!modelMenuItem) {
          logWarn('switchModel: «Сменить модель» item not found');
          // Закрываем меню — Escape (даже под visibility:hidden оно остаётся открытым)
          document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
          finish(false);
          return;
        }
        modelMenuItem.click();
        waitForElement('[class*="modelItem_"]', 1500, function (popupFound) {
          if (!popupFound) {
            logWarn('switchModel: model popup did not open after menu item click');
            finish(false);
            return;
          }
          finish(tryClickModelItemByKey(alias));
        });
      });
    }

    function makeModelBtn(letter, alias, displayName) {
      var b = document.createElement('span');
      b.textContent = letter;
      b.title = 'Переключить на ' + displayName;
      b.style.cssText = btnStyle() +
        'font-weight:bold;font-family:-apple-system,sans-serif;' +
        'min-width:14px;text-align:center;';
      b.addEventListener('mouseenter', function () { hoverIn(b); });
      b.addEventListener('mouseleave', function () { hoverOut(b); });
      b.addEventListener('click', function (e) {
        e.stopPropagation();
        switchModel(alias, letter, b);
      });
      return b;
    }
    var modelSBtn = makeModelBtn('S', 'default', 'Sonnet 4.6');
    var modelOBtn = makeModelBtn('O', 'opus',    'Opus 4.7');
    var modelHBtn = makeModelBtn('H', 'haiku',   'Haiku 4.5');

    iconsBar.appendChild(pingBtn);
    iconsBar.appendChild(saveBtn);
    iconsBar.appendChild(compactBtn);
    iconsBar.appendChild(modelSBtn);
    iconsBar.appendChild(modelOBtn);
    iconsBar.appendChild(modelHBtn);
    iconsBar.appendChild(copyBtn);
    iconsBar.appendChild(collapseBtn);
    overlay.appendChild(iconsBar);

    // Кнопка развернуть (в свёрнутом виде)
    var expandBtn = document.createElement('span');
    expandBtn.textContent = '▼';
    expandBtn.title = 'Развернуть';
    expandBtn.style.cssText = btnStyle() + 'font-size:20px;line-height:1;position:relative;top:-1px;';
    expandBtn.addEventListener('mouseenter', function () { hoverIn(expandBtn); });
    expandBtn.addEventListener('mouseleave', function () { hoverOut(expandBtn); });
    expandBtn.addEventListener('click', function () {
      isMinimized = false;
      // Пока панель была свёрнута, в текстовый блок не писали. Сбрасываем
      // память о последней записи, иначе ближайший тик решит, что писать
      // нечего, и развёрнутая панель покажет содержимое времён
      // сворачивания.
      lastOverlayText = null;
      textDiv.style.display = 'block';
      iconsBar.style.display = 'flex';
      miniBar.style.display = 'none';
      overlay.style.minWidth = '240px';
      overlay.style.maxWidth = '50vw';
      overlay.style.padding = '8px 14px';
      expandBtn.textContent = '▼';
    });

    // Кнопка копирования (в свёрнутом виде)
    var miniCopyBtn = document.createElement('span');
    miniCopyBtn.textContent = '📋';
    miniCopyBtn.title = 'Скопировать содержимое';
    miniCopyBtn.style.cssText = btnStyle();
    miniCopyBtn.addEventListener('mouseenter', function () { hoverIn(miniCopyBtn); });
    miniCopyBtn.addEventListener('mouseleave', function () { hoverOut(miniCopyBtn); });
    miniCopyBtn.addEventListener('click', function () { copyBtn.click(); });

    // Кнопка пинга (в свёрнутом виде)
    var miniPingBtn = document.createElement('span');
    miniPingBtn.textContent = '📡';
    miniPingBtn.title = 'Проверить интернет';
    miniPingBtn.style.cssText = btnStyle();
    miniPingBtn.addEventListener('mouseenter', function () { hoverIn(miniPingBtn); });
    miniPingBtn.addEventListener('mouseleave', function () { hoverOut(miniPingBtn); });
    miniPingBtn.addEventListener('click', function () { pingBtn.click(); });

    var miniSaveBtn = document.createElement('span');
    miniSaveBtn.textContent = '💾';
    miniSaveBtn.title = 'Сохранить лог';
    miniSaveBtn.style.cssText = btnStyle();
    miniSaveBtn.addEventListener('mouseenter', function () { hoverIn(miniSaveBtn); });
    miniSaveBtn.addEventListener('mouseleave', function () { hoverOut(miniSaveBtn); });
    miniSaveBtn.addEventListener('click', function () { saveBtn.click(); });

    miniBar.appendChild(miniPingBtn);
    miniBar.appendChild(miniSaveBtn);
    miniBar.appendChild(miniCopyBtn);
    miniBar.appendChild(expandBtn);
    overlay.appendChild(miniBar);

    if (document.body) {
      document.body.appendChild(overlay);
    } else {
      document.addEventListener('DOMContentLoaded', function () {
        document.body.appendChild(overlay);
      });
    }
    return overlay;
  }

  function fmtAgo(tsMs) {
    if (tsMs == null) return '—';
    var s = Math.round((Date.now() - tsMs) / 1000);
    return s + 's ago';
  }

  /**
   * Находит React root container (#root) и возвращает корневой fiber.
   * React 18+ хранит контейнер как свойство DOM-узла с ключом
   * вида `__reactContainer$<hash>` (новый рендерер) или
   * `_reactRootContainer` (legacy).
   */
  function findReactRootFiber() {
    var rootEl = document.getElementById('root');
    if (!rootEl) return null;
    var keys = Object.keys(rootEl);
    for (var i = 0; i < keys.length; i++) {
      if (keys[i].indexOf('__reactContainer$') === 0) {
        var container = rootEl[keys[i]];
        // .stateNode у container'а — FiberRootNode; .current — корневой фибер
        if (container && container.stateNode && container.stateNode.current) {
          return container.stateNode.current;
        }
        if (container && container.current) return container.current;
        return container;
      }
    }
    if (rootEl._reactRootContainer && rootEl._reactRootContainer._internalRoot) {
      return rootEl._reactRootContainer._internalRoot.current;
    }
    return null;
  }

  /**
   * Обходит fiber-дерево DFS и для каждого фибера передаёт visit(fiber).
   * Прерывается, если visit вернул `true`. Защита от циклов.
   */
  function walkFibers(rootFiber, visit) {
    if (!rootFiber) return;
    var stack = [rootFiber];
    var visited = new Set();
    while (stack.length) {
      var fiber = stack.pop();
      if (!fiber || visited.has(fiber)) continue;
      visited.add(fiber);
      if (visit(fiber) === true) return;
      if (fiber.child) stack.push(fiber.child);
      if (fiber.sibling) stack.push(fiber.sibling);
    }
  }

  /**
   * Проверяет, является ли объект экземпляром сессии (eX).
   * Признак: есть `busy` (с .value boolean), `connection`, `messages`,
   * `error` — основные Signals сессии.
   */
  function isSessionInstance(obj) {
    if (!obj || typeof obj !== 'object') return false;
    if (!('busy' in obj) || !('connection' in obj) || !('messages' in obj)) return false;
    var busy = obj.busy;
    if (!busy || typeof busy !== 'object' || !('value' in busy)) return false;
    return typeof busy.value === 'boolean';
  }

  /**
   * Ищет экземпляр сессии (eX) в fiber-дереве.
   * Стратегия:
   * 1. Проверяем props.context.viewSession — может быть session напрямую
   * 2. Ищем любой fiber, у которого props/state/stateNode содержит
   *    объект с busy+connection+messages Signals.
   */
  /**
   * Проверяет, является ли объект Wn sessionStore. Признак: есть
   * `activeSession` (Signal с .value) и `sessions` (Signal-массив).
   */
  function isSessionStore(obj) {
    if (!obj || typeof obj !== 'object') return false;
    if (!('activeSession' in obj) || !('sessions' in obj)) return false;
    var as = obj.activeSession;
    return as && typeof as === 'object' && 'value' in as;
  }

  function findSessionStore() {
    var rootFiber = findReactRootFiber();
    if (!rootFiber) return null;
    var found = null;
    walkFibers(rootFiber, function (fiber) {
      var sources = [fiber.memoizedProps, fiber.memoizedState, fiber.stateNode];
      for (var i = 0; i < sources.length; i++) {
        var c = sources[i];
        if (!c || typeof c !== 'object') continue;

        // Проверим props.sessions — может быть Wn store
        if ('sessions' in c) {
          var sessStore = c.sessions;
          if (isSessionStore(sessStore)) {
            var eX = sessStore.activeSession.value;
            if (isSessionInstance(eX)) { found = eX; return true; }
          }
        }

        // Проверим context.viewSession
        if ('context' in c && c.context && typeof c.context === 'object') {
          var vs = c.context.viewSession;
          if (isSessionInstance(vs)) { found = vs; return true; }
          if (vs && typeof vs === 'object' && 'value' in vs && isSessionInstance(vs.value)) {
            found = vs.value; return true;
          }
        }

        // Проверим session prop напрямую
        if ('session' in c && isSessionInstance(c.session)) {
          found = c.session; return true;
        }

        // Проверим сам объект
        if (isSessionInstance(c)) { found = c; return true; }
      }
    });
    return found;
  }

  function ensureSessionStore() {
    if (sessionStore) return sessionStore;
    var now = Date.now();
    if (now - lastSessionFindAttempt < 2000) return null; // throttle поиска
    lastSessionFindAttempt = now;
    sessionFindDiag = '';
    try {
      // Шаг 1: есть ли #root?
      var rootEl = document.getElementById('root');
      if (!rootEl) {
        sessionFindDiag = '#root not found';
        return null;
      }
      // Шаг 2: есть ли fiber-ключ?
      var keys = Object.keys(rootEl);
      var fiberKey = '';
      for (var i = 0; i < keys.length; i++) {
        if (keys[i].indexOf('__react') === 0 || keys[i].indexOf('_react') === 0) {
          fiberKey = keys[i];
          break;
        }
      }
      if (!fiberKey) {
        sessionFindDiag = '#root keys: ' + keys.filter(function(k){ return k.indexOf('_') === 0; }).join(', ');
        return null;
      }
      sessionFindDiag = 'key: ' + fiberKey.slice(0, 25);
      // Шаг 3: traversal
      sessionStore = findSessionStore();
      if (sessionStore) {
        sessionStoreFoundAt = now;
        sessionFindDiag += ' → FOUND';
      } else {
        // Подсчитаем сколько фиберов обошли и какие candidateProps видели
        var fiberCount = 0;
        var propsWithSession = 0;
        var firstCandidateKeys = '';
        var firstCandidateLocation = '';
        var rootFiber = findReactRootFiber();
        walkFibers(rootFiber, function(fiber) {
          fiberCount++;
          // Проверяем props, state, stateNode
          var sources = [
            ['props', fiber.memoizedProps],
            ['state', fiber.memoizedState],
            ['stateNode', fiber.stateNode],
          ];
          for (var s = 0; s < sources.length; s++) {
            var c = sources[s][1];
            if (c && typeof c === 'object' && ('session' in c || 'activeSession' in c || 'sessions' in c)) {
              propsWithSession++;
              if (!firstCandidateKeys) {
                firstCandidateLocation = sources[s][0];
                try {
                  var ks = Object.keys(c).slice(0, 15).join(',');
                  firstCandidateKeys = ks;
                  // Проверим activeSession
                  if ('activeSession' in c) {
                    var as = c.activeSession;
                    firstCandidateKeys += '\n  AS type=' + typeof as;
                    if (as && typeof as === 'object') {
                      firstCandidateKeys += ' keys=' + Object.keys(as).slice(0, 8).join(',');
                    }
                  }
                  // Если есть context — проверим его ключи (может быть Wn)
                  if ('context' in c && c.context && typeof c.context === 'object') {
                    var ctxKeys = Object.keys(c.context).slice(0, 15).join(',');
                    firstCandidateKeys += '\n  ctx: ' + ctxKeys;
                    // Если context — это Wn (имеет activeSession)?
                    if ('activeSession' in c.context) {
                      var as2 = c.context.activeSession;
                      firstCandidateKeys += '\n  ctx.AS type=' + typeof as2;
                      if (as2 && typeof as2 === 'object') {
                        firstCandidateKeys += ' keys=' + Object.keys(as2).slice(0, 8).join(',');
                      }
                    }
                  }
                  // Если sessions — проверим тип
                  if ('sessions' in c) {
                    var sess = c.sessions;
                    firstCandidateKeys += '\n  sessions type=' + typeof sess;
                    if (sess && typeof sess === 'object' && 'value' in sess) {
                      firstCandidateKeys += ' (signal, len=' + (Array.isArray(sess.value) ? sess.value.length : '?') + ')';
                    } else if (Array.isArray(sess)) {
                      firstCandidateKeys += ' (array, len=' + sess.length + ')';
                    }
                  }
                  // viewSession — ключевой кандидат
                  if ('context' in c && c.context && typeof c.context === 'object' && 'viewSession' in c.context) {
                    var vs = c.context.viewSession;
                    firstCandidateKeys += '\n  viewSession type=' + typeof vs;
                    if (vs && typeof vs === 'object') {
                      firstCandidateKeys += ' keys=' + Object.keys(vs).slice(0, 10).join(',');
                      if ('value' in vs) {
                        var vsv = vs.value;
                        firstCandidateKeys += '\n  viewSession.value type=' + typeof vsv;
                        if (vsv && typeof vsv === 'object') {
                          firstCandidateKeys += ' keys=' + Object.keys(vsv).slice(0, 10).join(',');
                        }
                      }
                      if ('busy' in vs) firstCandidateKeys += '\n  viewSession HAS busy!';
                    }
                  }
                } catch (e) { firstCandidateKeys = 'error: ' + e; }
              }
            }
          }
        });
        sessionFindDiag += ' → fibers:' + fiberCount + ' hits:' + propsWithSession;
        if (firstCandidateKeys) {
          sessionFindDiag += '\n  1st@' + firstCandidateLocation + ': ' + firstCandidateKeys;
        }
      }
    } catch (e) {
      sessionFindDiag = 'error: ' + (e && e.message ? e.message : e);
      logWarn('findSessionStore failed:', e);
    }
    return sessionStore;
  }

  // Последнее записанное в оверлей — чтобы не переписывать DOM тем же
  // самым (см. хвост updateDebugOverlay). null, а не '': пустая строка
  // тоже бывает значением, и её первая запись должна состояться.
  var lastOverlayText = null;
  var lastOverlayIcon = null;
  var lastOverlayTitle = null;

  function updateDebugOverlay() {
    var overlay = ensureDebugOverlay();
    var now = Date.now();

    // Текущее состояние фокуса (опрос — fallback, если событие focus не
    // приходит). Если значение поменялось — запишем как «событие».
    var currentFocus = document.hasFocus();
    if (currentFocus !== lastFocusState) {
      lastFocusState = currentFocus;
      lastFocusChangeAt = now;
      focusChangeCount++;
    }

    var lines = [];

    // Строка состояния соединения — в самом верху overlay
    var sess = ensureSessionStore();
    var connState = '?';
    busyState = false;
    var streamingState = false;
    var sendStarted = null;
    var errState = null;
    if (sess) {
      try {
        var conn = sess.connection && sess.connection.value;
        if (conn && conn.state && 'value' in conn.state) connState = conn.state.value;
        else if (conn && conn.state && typeof conn.state === 'string') connState = conn.state;
        busyState = !!(sess.busy && sess.busy.value);
        streamingState = !!sess.hasStreamingMessages;
        sendStarted = sess.sendStartedAt;
        errState = sess.error && sess.error.value;
      } catch (_) {}
    }

    var online = typeof navigator !== 'undefined' ? navigator.onLine : true;
    var domSilenceSec = Math.round((Date.now() - lastDomMutationAt) / 1000);

    // Отслеживание контента assistant-message (спиннер не влияет)
    var assistantMsgs = document.querySelectorAll('[data-testid="assistant-message"]');
    var lastMsg = assistantMsgs.length > 0 ? assistantMsgs[assistantMsgs.length - 1] : null;
    var currentContentLen = lastMsg ? lastMsg.textContent.length : 0;
    if (currentContentLen !== lastContentLen) {
      lastContentLen = currentContentLen;
      lastContentChangeAt = Date.now();
    }
    contentSilenceSec = Math.round((Date.now() - lastContentChangeAt) / 1000);

    // Timeout-детекция: busy=true и не стримит уже > N секунд →
    // возможная проблема с соединением до API.
    // Используем собственный _busyStartedAt, потому что sess.sendStartedAt
    // сбрасывается при получении system/init (одновременно с busy=true).
    busyNoStreamSec = 0;
    if (busyState && prevTrackedStates._busyStartedAt) {
      busyNoStreamSec = Math.round((Date.now() - prevTrackedStates._busyStartedAt) / 1000);
    }

    // Собственный трекинг стриминга — hasStreamingMessages в eX не
    // сбрасывается, поэтому отслеживаем по росту кол-ва messages.
    var nMsgsNow = 0;
    try {
      var m = sess && sess.messages && sess.messages.value;
      nMsgsNow = m ? m.length : 0;
    } catch (_) {}

    if (busyState && !prevTrackedStates._wasBusy) {
      // busy только что стал true → запоминаем кол-во messages и время
      prevTrackedStates._msgCountAtBusyStart = nMsgsNow;
      prevTrackedStates._busyStartedAt = Date.now();
      autoPingCount = 0;
      inlinePingClicked = false; // сброс для нового processing
      lastContentChangeAt = Date.now(); // сброс таймера контента
      lastContentLen = 0;
      onDemandPing();
      autoPingCount++;
    }
    if (!busyState && prevTrackedStates._wasBusy) {
      // busy завершился → чистим таймер
      prevTrackedStates._busyStartedAt = null;
    }
    prevTrackedStates._wasBusy = busyState;

    actuallyStreaming = busyState &&
      nMsgsNow > (prevTrackedStates._msgCountAtBusyStart || 0);

    var statusIcon = '⚪';
    var statusText = 'unknown';
    if (!sess) {
      statusIcon = '⚫'; statusText = 'session not found';
    } else if (errState) {
      statusIcon = '🔴'; statusText = 'error: ' + String(errState).slice(0, 25);
    } else if (!online) {
      statusIcon = '🔴'; statusText = 'OFFLINE (navigator.onLine=false)';
    } else if (connState !== 'connected') {
      statusIcon = '🔴'; statusText = connState;
    // Старый busyNoStreamSec-блок убран — заменён на domSilenceSec выше
    } else if (busyState && contentSilenceSec > AUTO_PING_AFTER_SILENCE_SEC) {
      // busy, но контент assistant-message не менялся > N секунд
      // Авто-пинг для диагностики
      var underLimit = MAX_PINGS === 0 || autoPingCount < MAX_PINGS;
      if (PING_INTERVAL_SEC > 0 && underLimit && onDemandPingState !== 'checking') {
        var sincePing = onDemandPingTs > 0 ? (Date.now() - onDemandPingTs) / 1000 : Infinity;
        if (sincePing >= PING_INTERVAL_SEC) {
          onDemandPing();
          autoPingCount++;
        }
      }
      if (onDemandPingState === 'offline') {
        statusIcon = '🔴'; statusText = 'no data ' + domSilenceSec + 's — INTERNET DOWN';
      } else if (onDemandPingState === 'online') {
        statusIcon = '🔵'; statusText = 'no data ' + domSilenceSec + 's — internet OK';
      } else if (onDemandPingState === 'checking') {
        statusIcon = '🟠'; statusText = 'no data ' + domSilenceSec + 's — checking…';
      } else {
        statusIcon = '🟣'; statusText = 'no data ' + domSilenceSec + 's';
      }
    } else if (busyState && actuallyStreaming) {
      statusIcon = '🟢'; statusText = 'streaming response…';
    } else if (busyState) {
      statusIcon = '🟡'; statusText = 'server processing…';
    } else {
      statusIcon = '⚪'; statusText = 'idle / connected';
    }
    lines.push(statusIcon + ' ' + statusText + (online ? '' : ' [OFFLINE]'));

    // Трекинг изменений ключевых состояний.
    // Из status убираем: секундный счётчик (processing Ns) и
    // ping-подстатусы (checking/INTERNET DOWN/server slow) —
    // они уже логируются через trackStateChange('ping').
    var statusForLog = statusText
      .replace(/\d+s/g, '')
      .replace(/\s+/g, ' ')
      .trim();
    trackStateChange('status', statusForLog);
    trackStateChange('busy', busyState);
    trackStateChange('streaming', streamingState);
    trackStateChange('conn', connState);
    trackStateChange('online', online);
    // noData: логируем только переход receiving ↔ silent (без посекундного спама)
    var noDataState = (busyState && contentSilenceSec > AUTO_PING_AFTER_SILENCE_SEC) ? 'silent' : 'receiving';
    if (busyState) trackStateChange('noData', noDataState);
    // Логируем результат пинга напрямую (без trackStateChange, чтобы
    // не дублировать old→new при каждом изменении времени).
    if (onDemandPingState === 'online' || onDemandPingState === 'offline' || onDemandPingState === 'csp_blocked') {
      var pingIcon = onDemandPingState === 'online' ? '✅' : onDemandPingState === 'offline' ? '❌' : '⚠️';
      var pingVal = pingIcon + (onDemandPingState === 'online' && onDemandPingMs > 0 ? onDemandPingMs + 'ms' : onDemandPingState);
      // Записываем только если это новый результат (не тот же что уже последний в логе)
      var lastLog = stateLog.length > 0 ? stateLog[stateLog.length - 1] : '';
      if (lastLog.indexOf('ping: ' + pingVal) === -1) {
        var ts = new Date().toLocaleTimeString('ru-RU', {hour12: false});
        stateLog.push(ts + ' ping: ' + pingVal);
          }
    }

    if (overlayMode === 'visibility') {
      var remainingV = Math.max(0, visibilityResumeAt - now);
      lines.push('Refresh через ' + (remainingV / 1000).toFixed(1) + 's (resume)');
      if (remainingV <= 0) overlayMode = 'poll';
    } else {
      var elapsed = now - lastPollAt;
      var remainingP = Math.max(0, POLL_MS - elapsed);
      lines.push('Refresh через ' + (remainingP / 1000).toFixed(1) + 's');
    }

    lines.push('—— diag ——');
    lines.push('visState: ' + document.visibilityState);
    lines.push('hasFocus: ' + currentFocus);
    lines.push('init: ' + fmtAgo(initTimeMs));
    lines.push('visChanges: ' + visibilityChangeCount + (lastVisibilityChangeAt ? ' (last ' + fmtAgo(lastVisibilityChangeAt) + ' → ' + lastVisibilityChangeNewState + ')' : ''));
    lines.push('focusChanges: ' + focusChangeCount + (lastFocusChangeAt ? ' (last ' + fmtAgo(lastFocusChangeAt) + ' → ' + (lastFocusState ? 'focus' : 'blur') + ')' : ''));
    lines.push('—— refresh ——');
    lines.push('calls: ' + refreshCalls);
    lines.push('skip (notFocused): ' + refreshSkippedNotFocused);
    lines.push('skip (throttle): ' + refreshSkippedThrottled);
    lines.push('executed: ' + refreshExecuted);

    lines.push('—— session ——');
    // sess уже получен выше (в блоке status line)
    if (!sess) {
      lines.push('session: NOT FOUND');
      if (sessionFindDiag) lines.push('diag: ' + sessionFindDiag);
    } else {
      try {
        var sessId = (sess.sessionId && sess.sessionId.value) || sess.internalId || '?';
        var nMsgs = sess.messages && sess.messages.value;
        lines.push('id: ' + String(sessId).slice(0, 12) + '…');
        lines.push('busy:' + busyState + ' stream:' + actuallyStreaming + ' conn:' + connState);
        if (busyState && contentSilenceSec > 1) {
          lines.push('⚠️ no data: ' + contentSilenceSec + 's');
        }
        if (onDemandPingState !== 'idle') {
          var odAge = onDemandPingTs > 0 ? Math.round((Date.now() - onDemandPingTs) / 1000) + 's ago' : '';
          var odMs = onDemandPingState === 'online' && onDemandPingMs > 0 ? ' ' + onDemandPingMs + 'ms' : '';
          lines.push('📡 ping: ' + onDemandPingState + odMs + (odAge ? ' (' + odAge + ')' : ''));
        }
        lines.push('msgs: ' + (nMsgs ? nMsgs.length : '?'));
        if (sendStarted != null) {
          lines.push('sendStartedAt: ' + Math.round(performance.now() - sendStarted) + 'ms ago');
        }
      } catch (e) {
        lines.push('read error: ' + (e && e.message ? e.message : e));
      }
    }

    // Лог переходов — показываем последние MAX_STATE_LOG_DISPLAY
    if (stateLog.length > 0) {
      var logStart = Math.max(0, stateLog.length - MAX_STATE_LOG_DISPLAY);
      lines.push('—— state log (' + stateLog.length + ' total) ——');
      for (var l = logStart; l < stateLog.length; l++) {
        lines.push(stateLog[l]);
      }
    }

    /* Запись в DOM — единственное, что этот оверлей отдаёт наружу,
     * и одновременно единственное, чем он вредит.
     *
     * Тик идёт каждые 100 мс, а присваивание textContent — мутация
     * дерева независимо от того, изменился текст или нет. Раньше их
     * было ровно десять в секунду, и каждая будила все наши
     * MutationObserver'ы (базовый плюс по одному на модуль футера),
     * а те обходят документ целиком. При большой переписке это
     * постоянная фоновая работа, которой никто не заказывал:
     * в свёрнутом виде полотно вообще никому не видно.
     *
     * Отсюда два правила. Скрытому текстовому блоку не пишем вовсе —
     * его содержимое всё равно перечитается при разворачивании
     * ближайшим тиком. И ничего не пишем, если строка не изменилась:
     * значок статуса и подсказка меняются раз в секунды, а не десять
     * раз в секунду.
     *
     * Вычисления выше остаются нетронутыми: из них берутся busyState
     * и contentSilenceSec для авто-пинга и значка 📡, и пропуск
     * расчёта сломал бы их вместе с оверлеем. */
    var textEl = document.getElementById('claude-custom-debug-text');
    if (textEl && textEl.style.display !== 'none') {
      var text = lines.join('\n');
      if (text !== lastOverlayText) {
        lastOverlayText = text;
        textEl.textContent = text;
        perfBump('overlay-writes');
      }
    }

    // Обновляем миниатюрный статус-индикатор (видён в свёрнутом виде).
    var ms = document.getElementById('claude-custom-debug-mini-status');
    if (ms && statusIcon !== lastOverlayIcon) {
      lastOverlayIcon = statusIcon;
      ms.textContent = statusIcon;
      perfBump('overlay-writes');
    }
    var ov = document.getElementById('claude-custom-debug');
    var titleText = statusIcon + ' ' + statusText;
    if (ov && titleText !== lastOverlayTitle) {
      lastOverlayTitle = titleText;
      ov.title = titleText;
      perfBump('overlay-writes');
    }
  }

  /**
   * Показывает 📡 рядом со спиннером, когда DOM не получает новых
   * данных дольше autoPingAfterSilenceSec. Исчезает при появлении данных.
   */
  var pingIndicatorEl = null;

  function updateInlinePingIndicator() {
    var shouldShow = busyState && contentSilenceSec > AUTO_PING_AFTER_SILENCE_SEC;

    if (!shouldShow) {
      // Убираем индикатор
      if (pingIndicatorEl && pingIndicatorEl.parentNode) {
        pingIndicatorEl.parentNode.removeChild(pingIndicatorEl);
      }
      pingIndicatorEl = null;
      return;
    }

    // Ищем spinner row
    var spinnerRows = document.querySelectorAll('[class*="spinnerRow_"]');
    if (spinnerRows.length === 0) return;
    var spinnerRow = spinnerRows[spinnerRows.length - 1]; // последний

    // Ищем container внутри spinnerRow
    var container = spinnerRow.querySelector('[class*="container_"]');
    if (!container) container = spinnerRow;

    // Создаём или обновляем индикатор (inline, в той же строке)
    if (!pingIndicatorEl) {
      pingIndicatorEl = document.createElement('span');
      pingIndicatorEl.id = 'claude-ping-indicator';
      pingIndicatorEl.style.cssText =
        'margin-left:8px;font-size:12px;opacity:0.7;cursor:pointer;' +
        'font-family:monospace;pointer-events:auto;user-select:none;';
      pingIndicatorEl.title = 'Проверить интернет';
      pingIndicatorEl.addEventListener('click', function () {
        inlinePingClicked = true;
        onDemandPing();
      });
    }

    // До клика — только 📡, после клика — 📡 + результат
    if (!inlinePingClicked) {
      pingIndicatorEl.textContent = '📡';
    } else {
      var icon = onDemandPingState === 'online' ? '✅'
        : onDemandPingState === 'offline' ? '❌'
        : onDemandPingState === 'checking' ? '⏳'
        : '📡';
      var detail = '';
      if (onDemandPingState === 'online' && onDemandPingMs > 0) detail = ' ' + onDemandPingMs + 'ms';
      else if (onDemandPingState === 'offline') detail = ' offline';
      else if (onDemandPingState === 'checking') detail = ' checking…';
      pingIndicatorEl.textContent = '📡 ' + icon + detail;
    }

    // Вставляем внутрь container (после text-span), если ещё не вставлен
    if (!pingIndicatorEl.parentNode) {
      container.appendChild(pingIndicatorEl);
    }
  }

  /**
   * Пробегает по выпадашке меню `/` ([class*="menuPopup_"]) и возвращает
   * массив пунктов {section, label, title} в порядке появления в DOM.
   * Возвращает [] если меню не открыто или пустое (например, фильтр
   * ничего не нашёл).
   *
   * Структура DOM (на момент Claude Code 2.1.126):
   *   <div class="menuPopup_G_S7FQ">
   *     <input class="filterInput_..." placeholder="Фильтр действий…">
   *     <div class="commandList_G_S7FQ">
   *       <div>
   *         <div class="sectionHeader_G_S7FQ">Контекст</div>
   *         <div class="commandItem_G_S7FQ" title="DESCRIPTION">
   *           <div class="commandContent_..."><span class="commandLabel_...">LABEL</span></div>
   *         </div>
   *         ...
   *       </div>
   *       <div> ... следующая секция ... </div>
   *     </div>
   *   </div>
   *
   * Сопоставление section ↔ commandItem делается через порядок обхода
   * querySelectorAll: querySelectorAll возвращает узлы в depth-first
   * порядке, поэтому sectionHeader всегда встречается перед своими
   * commandItem-сёстрами и устанавливает текущую секцию.
   */
  function collectMenuItems() {
    var menus = document.querySelectorAll('[class*="menuPopup_"]');
    if (!menus.length) return [];
    var items = [];
    for (var i = 0; i < menus.length; i++) {
      var menu = menus[i];
      // Помимо `/`-меню (commandItem_) этот же контейнер используется
      // селектором модели (modelItem_ + modelLabel_ + modelDescription_).
      // Структуры разные: у command-меню title лежит в атрибуте title,
      // у model-меню «описание» — это отдельный span class*="modelDescription_".
      var nodes = menu.querySelectorAll(
        '[class*="sectionHeader_"], [class*="commandItem_"], [class*="modelItem_"]'
      );
      var section = null;
      for (var j = 0; j < nodes.length; j++) {
        var el = nodes[j];
        var cls = el.className || '';
        if (cls.indexOf('sectionHeader_') !== -1) {
          section = (el.textContent || '').trim() || null;
        } else if (cls.indexOf('commandItem_') !== -1) {
          var labelEl = el.querySelector('[class*="commandLabel_"]');
          // textContent может включать "(Max)"-suffix; берём только верхний
          // текст самого commandLabel, без вложенных span'ов с маркерами
          var label = '';
          if (labelEl) {
            // Собираем только прямые текстовые дочерние узлы
            for (var k = 0; k < labelEl.childNodes.length; k++) {
              var c = labelEl.childNodes[k];
              if (c.nodeType === Node.TEXT_NODE) label += c.textContent;
            }
            label = (label || labelEl.textContent || '').trim();
          }
          var title = el.getAttribute('title') || '';
          items.push({ section: section, label: label, title: title.trim() });
        } else if (cls.indexOf('modelItem_') !== -1) {
          var mLabelEl = el.querySelector('[class*="modelLabel_"]');
          var mDescEl = el.querySelector('[class*="modelDescription_"]');
          var mLabel = mLabelEl ? (mLabelEl.textContent || '').trim() : '';
          var mDesc = mDescEl ? (mDescEl.textContent || '').trim() : '';
          items.push({ section: section, label: mLabel, title: mDesc });
        }
      }
    }
    return items;
  }

  /**
   * Собирает ВСЕ тексты вокруг открытого меню `/`: текстовые узлы +
   * содержимое атрибутов title/aria-label/placeholder. Захватывает не
   * только сам menuPopup_, но и родительский inputFooter_ — там лежат
   * кнопки +, /, иконка автосжатия и попап «X% of context remaining».
   *
   * Возвращает плоский массив уникальных строк. Дальше анализатор в
   * localize.py фильтрует «английские без перевода» по эвристике
   * (см. _looks_english + ru_values).
   */
  // Селектор для всех popup-подобных элементов: выпадашки меню /,
  // меню кнопки + (Add), меню «Режимы» (permission selector),
  // popover'ы автосжатия, hover-tooltip'ы и т.д. Используется и в
  // collectMenuTexts (что собирать), и в isMenuRelatedMutation (на
  // что реагировать). [class*="enuPopup_"] / [class*="opup_"] —
  // частичные совпадения, чтобы покрыть menuPopup_/Popup_/popup_
  // независимо от регистра первой буквы и префикса.
  var POPUP_LIKE_SELECTOR = (
    '[class*="enuPopup_"],' +
    '[class*="opup_"],' +
    '[class*="opover_"],' +
    '[class*="ropdown_"],' +
    '[role="menu"],' +
    '[role="listbox"]'
  );

  function collectMenuTexts() {
    var roots = [];
    // 1) Если открыто меню / (menuPopup_), берём родителя ВЕРХНЕГО уровня,
    //    который содержит и menuPopup_, и его inputFooter_-сосед. Footer
    //    нужен чтобы захватить кнопки +, /, usage и hover-popup'ы рядом.
    var slashMenus = document.querySelectorAll('[class*="menuPopup_"]');
    for (var i = 0; i < slashMenus.length; i++) {
      var menu = slashMenus[i];
      var p = menu.parentElement;
      var inputFooter = null;
      while (p && p !== document.body) {
        var inFooter = p.querySelector('[class*="inputFooter_"]');
        if (inFooter) { inputFooter = inFooter; break; }
        p = p.parentElement;
      }
      roots.push(inputFooter && inputFooter.parentElement
        ? inputFooter.parentElement
        : menu);
    }
    // 2) Все остальные popup-like узлы (Add-меню, Режимы, popover'ы…),
    //    которые рендерятся в body отдельно от menuPopup_/footer.
    var others = document.querySelectorAll(POPUP_LIKE_SELECTOR);
    for (var ep = 0; ep < others.length; ep++) {
      var node = others[ep];
      var alreadyIn = false;
      for (var r = 0; r < roots.length; r++) {
        if (roots[r] === node || roots[r].contains(node)) { alreadyIn = true; break; }
      }
      if (!alreadyIn) roots.push(node);
    }
    if (!roots.length) return [];

    var seen = Object.create(null);
    var out = [];
    function add(text) {
      if (!text) return;
      var t = String(text).replace(/\s+/g, ' ').trim();
      if (!t) return;
      // descriptions некоторых slash-команд (/update-config, /claude-api)
      // и блоки tooltip'ов могут быть длиной ~1500–2500 символов — лимит
      // 5000 даёт запас, чтобы строки не обрезались и корректно
      // дедуплицировались с command_drift.title в localize.py.
      if (t.length > 5000) t = t.slice(0, 5000);
      if (seen[t]) return;
      seen[t] = true;
      out.push(t);
    }

    for (var ri = 0; ri < roots.length; ri++) {
      var root = roots[ri];
      // Все text-nodes
      var walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, null, false);
      var node;
      while ((node = walker.nextNode())) add(node.nodeValue);
      // Атрибуты title/aria-label/aria-description/placeholder
      var withAttrs = root.querySelectorAll('[title], [aria-label], [aria-description], [placeholder]');
      var attrs = ['title', 'aria-label', 'aria-description', 'placeholder'];
      for (var a = 0; a < withAttrs.length; a++) {
        for (var ai = 0; ai < attrs.length; ai++) {
          var v = withAttrs[a].getAttribute(attrs[ai]);
          if (v) add(v);
        }
      }
      // Корневой элемент тоже может иметь атрибуты
      for (var ai2 = 0; ai2 < attrs.length; ai2++) {
        if (root.getAttribute) {
          var rv = root.getAttribute(attrs[ai2]);
          if (rv) add(rv);
        }
      }
    }
    return out;
  }

  function sendDriftReport(items) {
    if (driftSendInFlight) return;
    driftSendInFlight = true;
    var texts = collectMenuTexts();
    fetch(LOCALE_DRIFT_URL, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ items: items, texts: texts }),
    })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        driftSendInFlight = false;
        logInfo('locale-drift sent',
          items.length, 'items,', texts.length, 'texts →', d.path || d);
      })
      .catch(function (e) {
        driftSendInFlight = false;
        logWarn('locale-drift failed:', e && e.message ? e.message : e);
      });
  }

  /**
   * Собирает список моделей из popup'а селектора моделей.
   *
   * value (ID типа `claude-opus-4-7`) в DOM-тексте отсутствует — React
   * хранит его как `key` у элемента `<div key={V.value}>`. Достаём из
   * React-fiber: на каждом DOM-узле есть свойство `__reactFiber$xxx`
   * (имя суффикса — рандомный токен React), у fiber'а есть `.key`.
   *
   * Если popup не открыт — возвращает null. Дедуп — в maybeSaveModels.
   */
  function collectModelsList() {
    var items = document.querySelectorAll('[class*="modelItem_"]');
    if (!items.length) return null;
    var models = [];
    for (var i = 0; i < items.length; i++) {
      var el = items[i];
      var labelEl = el.querySelector('[class*="modelLabel_"]');
      var descEl = el.querySelector('[class*="modelDescription_"]');
      var displayName = labelEl ? (labelEl.textContent || '').trim() : '';
      var description = descEl ? (descEl.textContent || '').trim() : '';
      var cls = typeof el.className === 'string' ? el.className : '';
      var isActive = cls.indexOf('activeModelItem_') !== -1;

      var value = null;
      for (var k in el) {
        if (k.indexOf('__reactFiber') === 0) {
          var fiber = el[k];
          if (fiber && fiber.key) { value = fiber.key; }
          break;
        }
      }

      models.push({
        value: value,
        displayName: displayName,
        description: description,
        isActive: isActive,
      });
    }
    return models;
  }

  function sendModelsList(models) {
    if (modelsSendInFlight) return;
    modelsSendInFlight = true;
    fetch(MODELS_LIST_URL, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ models: models }),
    })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        modelsSendInFlight = false;
        logInfo('models-list sent', models.length, 'models →', d.path || d);
      })
      .catch(function (e) {
        modelsSendInFlight = false;
        logWarn('models-list failed:', e && e.message ? e.message : e);
      });
  }

  /**
   * Debounced trigger для каталога моделей. Симметрично maybeCollectAndSend,
   * но смотрит только на modelItem_-popup и шлёт на /models-list.
   */
  function maybeSaveModels() {
    if (modelsListTimer) clearTimeout(modelsListTimer);
    modelsListTimer = setTimeout(function () {
      modelsListTimer = null;
      var models = collectModelsList();
      if (!models || !models.length) return; // popup моделей не открыт
      var hash = JSON.stringify(models);
      if (hash === lastModelsHash) return;
      lastModelsHash = hash;
      sendModelsList(models);
    }, MODELS_LIST_DEBOUNCE_MS);
  }

  /**
   * Debounced trigger: вызывается при любой DOM-мутации, ждёт
   * LOCALE_DRIFT_DEBOUNCE_MS тишины, затем собирает и шлёт snapshot.
   * Дедуп через lastDriftHash — повторные одинаковые наборы не уходят.
   */
  function maybeCollectAndSend() {
    if (localeDriftTimer) clearTimeout(localeDriftTimer);
    localeDriftTimer = setTimeout(function () {
      localeDriftTimer = null;
      var items = collectMenuItems();
      if (!items.length) return; // меню не открыто — нечего слать
      var hash = JSON.stringify(items);
      if (hash === lastDriftHash) return; // не изменилось — пропустить
      lastDriftHash = hash;
      sendDriftReport(items);
    }, LOCALE_DRIFT_DEBOUNCE_MS);
  }

  /**
   * Фильтр мутаций — относится ли хоть одна к меню `/`?
   *
   * Старая логика дёргала maybeCollectAndSend на КАЖДУЮ мутацию body.
   * При включённом debug-overlay (setInterval каждые 100мс
   * обновляет содержимое #claude-custom-debug) общий MutationObserver
   * получал постоянный поток событий, и debounce-таймер 500мс
   * сбрасывался быстрее, чем успевал сработать. В результате snapshot
   * меню никогда не отправлялся, даже если меню было открыто 10 секунд.
   *
   * Теперь maybeCollectAndSend вызывается только когда мутация
   * фактически относится к меню — добавлен/изменён узел `menuPopup_`,
   * `commandItem_`, `sectionHeader_`, `filterInput_`, либо мутация
   * произошла внутри уже открытого `menuPopup_` (фильтрация по поиску).
   */
  function isMenuRelatedMutation(mutations) {
    for (var i = 0; i < mutations.length; i++) {
      var m = mutations[i];

      // Появление новых popup-like узлов (открытие любого меню/popover'а)
      var added = m.addedNodes;
      for (var j = 0; j < added.length; j++) {
        var n = added[j];
        if (n.nodeType !== 1) continue;
        var cls = typeof n.className === 'string' ? n.className : '';
        if (/menuPopup_|popup_|popover_|dropdown_|commandItem_|sectionHeader_|filterInput_/.test(cls)) {
          return true;
        }
        // ARIA-роли (на случай если меню без CSS-modules-классов)
        if (n.getAttribute) {
          var role = n.getAttribute('role');
          if (role === 'menu' || role === 'listbox' || role === 'menuitem') return true;
        }
        if (n.querySelector && n.querySelector(POPUP_LIKE_SELECTOR)) {
          return true;
        }
      }

      // Любая мутация ВНУТРИ уже открытого popup-like (фильтрация по
      // поиску в /-меню, выбор в Add, прочее). Для текстовых узлов
      // проверяем родителя.
      var target = m.target;
      if (target && target.nodeType === 3) target = target.parentElement;
      if (target && typeof target.closest === 'function') {
        if (target.closest(POPUP_LIKE_SELECTOR)) return true;
      }
    }
    return false;
  }

  /**
   * Помечает страницу для контекстного меню VSCode.
   *
   * Меню по правой кнопке (Cut/Copy/Paste) рисует оболочка VSCode, а не
   * страница: дописать в него пункт из нашего JS нельзя в принципе.
   * Штатный путь — атрибут `data-vscode-context`: VSCode при клике идёт
   * от узла под курсором вверх по дереву, собирает эти атрибуты и по
   * ним решает, какие вклады `webview/context` показать. Значение
   * `webviewSection` должно совпадать с `when` в манифесте расширения
   * (его вписывает patch-extension-settings.py).
   *
   * Ставим на <body>, потому что пункт нужен «в любом месте страницы».
   * `preventDefaultContextMenuItems` не трогаем: Cut/Copy/Paste должны
   * остаться, наш пункт к ним добавляется, а не заменяет их.
   *
   * body React не перерисовывает (он рендерит в #root), так что одной
   * установки хватает.
   */
  function markVscodeContext() {
    try {
      if (!document.body) return;
      document.body.setAttribute('data-vscode-context', JSON.stringify({
        webviewSection: 'claude-custom',
      }));
    } catch (e) {
      logWarn('не удалось пометить body для контекстного меню:', e);
    }
  }

  function init() {
    logInfo('init at', new Date().toISOString());
    var cspMeta = document.querySelector('meta[http-equiv="Content-Security-Policy"]');
    if (cspMeta) logInfo('CSP:', cspMeta.content);

    // Прибор запускается первым: всё, что происходит на загрузке
    // страницы, должно попасть в baseline-отчёт.
    perfInit();
    markVscodeContext();
    tagTimestampLines();
    refreshCustomCss();

    // Перехват click на иконке автосжатия (capture-фаза) — показывает
    // confirmation popup вместо мгновенного запуска /compact.
    document.addEventListener('click', compactClickInterceptor, true);
    // То же для пункта меню `/` «Очистить разговор» — спрашивает
    // подтверждение прежде, чем стирать текущий чат.
    document.addEventListener('click', clearClickInterceptor, true);

    /* Наблюдатель разделён на две части, и это разделение — суть
     * правки 59.3.
     *
     * СИНХРОННО, на каждую мутацию, остаётся только то, чему нужна
     * точность момента: отметка времени последней ЧУЖОЙ мутации.
     * На ней держится детектор тишины — 📡 и авто-пинг, — и загнать
     * её под throttle значило бы врать про возраст последних данных.
     * Никаких обращений к документу здесь нет: перебор записей стоит
     * O(числа записей) и не зависит от размера переписки.
     *
     * ПОД THROTTLE уходит всё, что обходит дерево: покраска меток 📬
     * (querySelectorAll по всем абзацам ответов) и разбор мутаций для
     * locale-drift. Именно они стоили дорого — а стриминг ответа даёт
     * тысячи мутаций в секунду, и делать этот обход на каждую из них
     * было бессмысленно: результат один и тот же. Задержка в четверть
     * секунды перед покраской метки времени незаметна глазу.
     *
     * `characterData` остаётся включённым. Соблазн убрать его велик —
     * это самый шумный источник, — но именно им виден стриминг текста
     * ответа: без него детектор тишины считал бы поток данных паузой
     * и 📡 загорался бы посреди работающего ответа.
     *
     * Свои мутации не считаются чужими. Оверлей живёт в том же body,
     * и его записи раньше и обновляли отметку времени, и запускали
     * обход дерева — то есть патч будил сам себя. */
    var BASE_THROTTLE_MS = 250;
    // Потолок буфера. MutationRecord держит ссылки на узлы, поэтому
    // расти без предела ему нельзя. Переполнение означает шторм
    // мутаций (стриминг), а не открытие меню `/` — снимок меню
    // соберётся при следующем его показе.
    var BASE_BUFFER_MAX = 1000;
    var baseBuffer = [];
    var baseTimer = null;

    var baseFlush = perfWrap('base-flush', function () {
      baseTimer = null;
      var batch = baseBuffer;
      baseBuffer = [];
      tagTimestampLines();
      // Locale-drift detector — собираем snapshot ТОЛЬКО когда мутация
      // относится к меню `/`. Иначе debug-overlay и стриминг ответов
      // в чате постоянно сбрасывают debounce-таймер, и snapshot
      // никогда не доходит до отправки.
      if (isMenuRelatedMutation(batch)) {
        maybeCollectAndSend();
        maybeSaveModels();
      }
    });

    new MutationObserver(perfWrap('base-observer', function (mutations) {
      perfBump('mutation-records', mutations.length);
      var foreign = false;
      for (var mi = 0; mi < mutations.length; mi++) {
        var target = mutations[mi].target;
        if (target && target.nodeType === 3) target = target.parentNode;
        if (target && target.__claudeOwnNode) continue;
        foreign = true;
        if (baseBuffer.length < BASE_BUFFER_MAX) baseBuffer.push(mutations[mi]);
      }
      if (!foreign) return;
      lastDomMutationAt = Date.now();
      if (baseTimer) return;
      baseTimer = setTimeout(baseFlush, BASE_THROTTLE_MS);
    })).observe(document.body, {
      childList: true,
      subtree: true,
      characterData: true,
    });

    // Периодический пуллинг — для активных вкладок без DOM-активности.
    // refreshCustomCss сам выходит, если вкладка не visible.
    // lastPollAt тикается всегда, даже когда refresh — no-op (вкладка
    // скрыта), чтобы overlay-таймер был предсказуем.
    setInterval(perfWrap('css-poll', function () {
      refreshCustomCss();
      lastPollAt = Date.now();
    }), POLL_MS);

    // Когда вкладка становится активной — делаем refresh, чтобы догнать
    // изменения CSS, накопленные за время неактивности. Задержка
    // VISIBILITY_REFRESH_DELAY_MS позволяет увидеть, как именно цвет
    // меняется. 0 — мгновенно.
    // visibilitychange в Anthropic-webview не срабатывает (state всегда
    // visible). Оставляем подписку только для диагностики — счётчик
    // в overlay помогает увидеть, если в каком-то будущем релизе
    // расширение начнёт его эмитить.
    document.addEventListener('visibilitychange', function () {
      visibilityChangeCount++;
      lastVisibilityChangeAt = Date.now();
      lastVisibilityChangeNewState = document.visibilityState;
      lastVisibilityState = document.visibilityState;
    });

    // Реальный триггер «возврат на вкладку» в этом расширении —
    // window-focus после blur. Первый focus при init не должен
    // считаться возвратом, поэтому через флаг seenBlurSinceLastFocus.
    var seenBlurSinceLastFocus = false;
    window.addEventListener('focus', function () {
      if (lastFocusState !== true) {
        lastFocusState = true;
        lastFocusChangeAt = Date.now();
        focusChangeCount++;
      }
      if (!seenBlurSinceLastFocus) return; // первый focus после init
      seenBlurSinceLastFocus = false;

      var doRefresh = function () {
        lastCssRefresh = 0;
        refreshCustomCss();
        lastPollAt = Date.now();
      };
      if (VISIBILITY_REFRESH_DELAY_MS > 0) {
        if (DEBUG_OVERLAY_ENABLED) {
          overlayMode = 'visibility';
          visibilityResumeAt = Date.now() + VISIBILITY_REFRESH_DELAY_MS;
        }
        setTimeout(doRefresh, VISIBILITY_REFRESH_DELAY_MS);
      } else {
        doRefresh();
      }
    });
    window.addEventListener('blur', function () {
      if (lastFocusState !== false) {
        lastFocusState = false;
        lastFocusChangeAt = Date.now();
        focusChangeCount++;
      }
      seenBlurSinceLastFocus = true;
    });

    // Persistent debug-overlay тикает каждые 100мс на любой вкладке —
    // только если включён в конфиге (debugOverlay=true). Иначе вообще
    // никаких overlay-элементов не создаётся.
    if (DEBUG_OVERLAY_ENABLED) {
      ensureDebugOverlay();
      setInterval(perfWrap('overlay-tick', function () {
        updateDebugOverlay();
        updateInlinePingIndicator();
      }), 100);
      updateDebugOverlay();
    } else {
      // Даже без overlay, inline-индикаторы должны работать
      setInterval(perfWrap('ping-indicator', function () {
        updateInlinePingIndicator();
      }), 500);
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();

/* ============================================================
 * DOM WATCH — один наблюдатель на все модули
 *
 * Каждый модуль, которому нужно доставить свою кнопку в футер или
 * в строку сессии, заводил собственный MutationObserver на
 * document.body с `subtree: true` и вызывал из него полный скан
 * документа. К этапу 58 таких наблюдателей стало семь, и только
 * у одного (FIND IN PAGE) стоял throttle — там его поставили после
 * того, как скан на каждую мутацию подвесил webview.
 *
 * Чем это плохо, видно из арифметики. Стриминг ответа даёт тысячи
 * мутаций в секунду; браузер вызывает каждый из семи обработчиков
 * отдельно; каждый обработчик обходит документ целиком. Работа
 * растёт как «мутации × модули × размер переписки», причём почти вся
 * она бесполезна: между двумя соседними мутациями футер не меняется.
 *
 * Здесь один наблюдатель, один throttle и один периодический
 * обход-подстраховка на всех. Модуль вместо своего observer'а
 * регистрирует функцию скана:
 *
 *     window.__claudeDomWatch.register('mood', scan);
 *
 * Что даёт регистрация, кроме экономии:
 *
 * - **Чужие падения не мешают.** Скан каждого модуля вызывается
 *   в своём try/catch: исключение в одном раньше оборвало бы цикл
 *   и оставило остальных без вызова (тот же урок, что с обёрткой
 *   вокруг инлайн-JS в bootstrap).
 * - **Реентерабельности нет.** Сканы вставляют узлы, вставка рождает
 *   мутации, мутации будят наблюдателя — цикл «скан → мутация →
 *   скан» уже подвешивал окно однажды. Пока идёт проход, новый
 *   не начинается.
 * - **Свои мутации не считаются.** Оверлей и наши панели живут в том
 *   же body; без фильтра патч будил бы сам себя.
 * - **Есть предохранитель.** Если скан модуля съедает больше своего
 *   бюджета за минуту, он отключается насовсем и об этом уходит
 *   запись в журнал. Кнопка исчезнет при ближайшем ре-рендере —
 *   но это лучше подвешенного окна, а запись в журнале назовёт
 *   виновника без дознания.
 *
 * Время каждого скана идёт в прибор (`scan:<имя>` в отчёте perf) —
 * поэтому в сводке видно не только «наши обработчики съели столько-то»,
 * но и кто именно.
 * ============================================================ */
(function () {
  if (window.__claudeDomWatchInstalled) return;
  window.__claudeDomWatchInstalled = true;

  /* Регистратор публикуется ПЕРВЫМ делом, до всей настройки ниже.
   *
   * Модули обращаются к нему из своих init(), и если бы объекта не
   * оказалось, вызов бросил бы TypeError. Инлайн-JS обёрнут в общий
   * try/catch на уровне bootstrap, так что одно такое исключение
   * оборвало бы установку ВСЕХ последующих модулей — ровно та
   * поломка, ради которой этот этап и затевался.
   *
   * Функции ниже объявлены через `function`, поэтому уже видны;
   * переменные состояния получат значения строкой ниже, а методы
   * вызовут их сильно позже — из init() модулей. */
  window.__claudeDomWatch = {
    /**
     * Зарегистрировать скан модуля. Вызывается на мутациях (с общим
     * throttle) и раз в SWEEP_MS. Первый вызов — сразу, чтобы кнопка
     * появлялась без ожидания.
     */
    register: function (name, fn) {
      var wrapped = perf ? perf.wrap('scan:' + name, fn) : fn;
      subs.push({ name: name, fn: wrapped, spent: 0, disabled: false, failed: false });
      // Первый скан идёт через ту же обёртку, что и последующие: иначе
      // самый дорогой вызов — по неотрисованному ещё дереву — не попал
      // бы в отчёт прибора.
      try { wrapped(); } catch (e) {}
    },
    /** Внеочередной обход — например после действия, меняющего футер. */
    kick: function () { schedule(); },
  };

  // Пауза между обходами. Меньше — незаметно быстрее (кнопка и так
  // появляется в пределах кадра-двух), больше — заметно позже при
  // пересоздании футера React'ом.
  var THROTTLE_MS = 200;

  // Подстраховочный обход: observer пропускает узлы, отрисованные до
  // его подключения, а также случаи, когда мутация пришла в момент,
  // когда проход уже шёл.
  var SWEEP_MS = 3000;

  // Бюджет одного модуля — 4 секунды на минуту, то есть около 7%
  // главного потока. Столько скан кнопки не может стоить ни при
  // каком размере переписки; если стоит — он в цикле.
  var GUARD_WINDOW_MS = 60000;
  var GUARD_BUDGET_MS = 4000;

  var perf = window.__claudePerf || null;
  var subs = [];
  var timer = null;
  var running = false;

  function report(payload) {
    try {
      payload.href = location.href.slice(0, 200);
      payload.ts = Date.now();
      fetch('http://localhost:18923/webview-error', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
        keepalive: true,
      }).catch(function () {});
    } catch (e) {}
  }

  /* Общий обход документа — один на проход вместо одного на модуль.
   *
   * Восемь сканов искали одни и те же узлы одним и тем же селектором,
   * каждый своим `querySelectorAll` по всему документу. При 11 700
   * узлах (список сообщений не виртуализирован) это была львиная доля
   * их стоимости: в замере 2026-09-03 сканы стоили 56–99 мс каждый,
   * и разброс между ними — шум, а не разница в работе.
   *
   * Контекст передаётся сканам аргументом. Модуль обязан принять его
   * как необязательный: скан вызывают и вне прохода (при регистрации,
   * из своих обработчиков), и тогда он ищет узлы сам. */
  function buildContext() {
    return {
      inputs: document.querySelectorAll('[class*="inputContainer_"]'),
      sessions: document.querySelectorAll('[class*="sessionItem_"]'),
      imageAttachments: document.querySelectorAll(
        '[class*="attachedFilesContainer_"] [class*="pill_"]'
      ),
      imagePreviews: document.querySelectorAll('[class*="previewContainer_"]'),
    };
  }

  function runAll() {
    // Скан мутирует DOM, мутация будит наблюдателя. Без этого флага
    // получается тот самый цикл «скан → мутация → скан».
    if (running) return;
    running = true;
    try {
      var ctx = buildContext();
      for (var i = 0; i < subs.length; i++) {
        var s = subs[i];
        if (s.disabled) continue;
        var t0 = performance.now();
        try {
          s.fn(ctx);
        } catch (e) {
          // Падение одного скана не должно лишать вызова остальных.
          // Сообщаем один раз: если модуль падает, он падает на каждой
          // мутации, и журнал заполнился бы одной строкой.
          if (!s.failed) {
            s.failed = true;
            report({
              kind: 'scan-error',
              message: 'скан модуля «' + s.name + '» упал: '
                + String(e && e.message || e),
              module: s.name,
              stack: String(e && e.stack || '').slice(0, 1500),
            });
          }
        }
        s.spent += performance.now() - t0;
      }
    } finally {
      running = false;
    }
  }

  function guard() {
    for (var i = 0; i < subs.length; i++) {
      var s = subs[i];
      if (s.disabled) continue;
      if (s.spent > GUARD_BUDGET_MS) {
        s.disabled = true;
        report({
          kind: 'scan-guard',
          message: 'скан модуля «' + s.name + '» съел '
            + Math.round(s.spent) + ' мс за минуту — модуль отключён',
          module: s.name,
          spent_ms: Math.round(s.spent),
        });
      }
      s.spent = 0;
    }
  }

  function schedule() {
    if (timer) return;
    timer = setTimeout(function () {
      timer = null;
      runAll();
    }, THROTTLE_MS);
  }

  /* Узлы, ради которых вообще существуют сканы: поле ввода с футером
   * (шесть модулей) и строка списка сессий (SESSION MOVER). Мутация
   * вне этих поддеревьев ничего для нас не меняет.
   *
   * Замер 2026-09-03 показал, зачем это нужно. В покое приложение
   * мутирует DOM непрерывно — 20–30 записей в секунду, — и throttle
   * честно пропускал по пять проходов в секунду. Каждый проход это
   * восемь обходов документа, а документ здесь не виртуализирован:
   * 11 700 узлов на 190 сообщений. Выходило 5–7% главного потока
   * ради футера, который всё это время не менялся.
   *
   * Проверка стоит `closest` на запись — десятки шагов вверх по
   * дереву против восьми обходов всего документа. */
  var RELEVANT = '[class*="inputContainer_"], [class*="inputFooter_"], [class*="sessionItem_"], [class*="attachedFilesContainer_"], [class*="pill_"], [class*="previewOverlay_"], [class*="previewContainer_"]';

  function isRelevant(m) {
    var target = m.target;
    if (target && target.nodeType === 3) target = target.parentNode;
    // Наши собственные узлы (оверлей, панели) — не повод для скана.
    if (!target || target.__claudeOwnNode) return false;
    if (target.closest && target.closest(RELEVANT)) return true;

    /* Монтирование самого контейнера: React вставляет поле ввода
     * целиком, и тогда цель мутации — его будущий родитель, а сам
     * контейнер лежит в addedNodes. Без этой ветки кнопка появлялась
     * бы только со следующим обходом-подстраховкой, то есть через
     * секунды после открытия вкладки. */
    var added = m.addedNodes;
    if (!added) return false;
    for (var i = 0; i < added.length; i++) {
      var node = added[i];
      // Текстовые узлы — самый частый гость при стриминге ответа,
      // и они заведомо ничего не монтируют.
      if (node.nodeType !== 1) continue;
      if (node.matches && node.matches(RELEVANT)) return true;
      if (node.querySelector && node.querySelector(RELEVANT)) return true;
    }
    return false;
  }

  function onMutations(mutations) {
    for (var i = 0; i < mutations.length; i++) {
      if (!isRelevant(mutations[i])) continue;
      if (perf) perf.bump('batches-relevant');
      schedule();
      return;
    }
    // Ни одна мутация нас не касается — прохода не будет вовсе.
    // Промах фильтра не страшен: раз в SWEEP_MS идёт полный обход,
    // и кнопка встанет с задержкой, а не потеряется.
    if (perf) perf.bump('batches-skipped');
  }

  function observeBody() {
    try {
      // characterData здесь не нужен: модули следят за появлением и
      // порядком узлов, а не за текстом внутри них. Это заодно снимает
      // самый шумный источник — стриминг ответа.
      new MutationObserver(
        perf ? perf.wrap('dom-watch', onMutations) : onMutations
      ).observe(document.body, { childList: true, subtree: true });
    } catch (e) {}
  }

  // Этот блок — не модуль, а инфраструктура: он исполняется сразу при
  // загрузке файла, а не по DOMContentLoaded, потому что модули ниже
  // регистрируются у него. На этот момент body может ещё не
  // существовать, и observe(null) бросил бы исключение — молча, внутри
  // catch, оставив всех подписчиков без наблюдателя вообще.
  if (document.body) observeBody();
  else document.addEventListener('DOMContentLoaded', observeBody);

  setInterval(runAll, SWEEP_MS);
  setInterval(guard, GUARD_WINDOW_MS);
})();

/* ============================================================
 * COMPOSER TEXT — чтение и вставка текста в поле ввода
 *
 * Поле ввода — `div[contenteditable="plaintext-only"]`, React-
 * controlled. С ним связаны две ловушки, на каждую из которых уже
 * наступали, и обе стоят отдельной функции.
 *
 * ЧТЕНИЕ. `textContent` склеивает строки: он конкатенирует текст
 * узлов и о разрывах не знает. `innerText` учитывает раскладку и
 * отдаёт ровно то, что видит пользователь, — с `\n` на каждом
 * переносе, независимо от того, хранит их поле символами или <br>.
 *
 * ВСТАВКА. Один `insertText` со всей строкой не годится: перенос
 * внутри неё Chromium разрывом не делает. Снаружи это выглядит
 * особенно обманчиво — каретка встаёт куда надо, выделение показывает
 * несколько строк, а видимый текст склеен в одну. Так и есть: поле
 * рисует текст прозрачным, а видимое рисует зеркало `.mentionMirror_*`
 * (см. заметку про EMOJI PICKER), и до зеркала переносы не доезжают.
 * Поэтому вставляем построчно, а между строками просим редактор
 * сделать разрыв — тем же действием, что и Shift+Enter.
 *
 * Пойманы обе ловушки порознь: чтение — на возврате черновика
 * keepalive, вставка — там же, а потом ещё раз на цитировании.
 * Отсюда общий блок: третьей копии быть не должно.
 * ============================================================ */
(function () {
  if (window.__claudeComposerTextInstalled) return;
  window.__claudeComposerTextInstalled = true;

  window.__claudeComposer = {
    /** Содержимое поля с сохранением переносов. */
    read: function (el) {
      if (!el) return '';
      return (typeof el.innerText === 'string' ? el.innerText : el.textContent) || '';
    },

    /**
     * Вставляет текст в фокусированное поле, воспроизводя переносы.
     * Возвращает false, если редактор отказал, — вызывающий решает,
     * что делать дальше.
     */
    insertMultiline: function (text) {
      var lines = String(text).split('\n');
      for (var i = 0; i < lines.length; i++) {
        if (i > 0) {
          var broke = false;
          try {
            broke = document.execCommand('insertLineBreak');
          } catch (e) {
            broke = false;
          }
          if (!broke) {
            try { document.execCommand('insertText', false, '\n'); } catch (e) {}
          }
        }
        // Пустая строка — это сам разрыв, вставлять сверх него нечего.
        if (!lines[i]) continue;
        try {
          if (!document.execCommand('insertText', false, lines[i])) return false;
        } catch (e) {
          return false;
        }
      }
      return true;
    },
  };
})();

/* ============================================================
 * SESSION MOVER — перенос сессий чата между проектами Claude Code
 * ============================================================
 *
 * Добавляет кнопку 📁 «Переместить в проект» в `.sessionActions_*`
 * каждой строки списка локальных сессий. По клику открывает модалку
 * со списком существующих проектов (GET /list-projects) и полем для
 * создания нового проекта (POST /create-project + /move-session).
 *
 * Извлечение session_id: через React-fiber.key sessionItem-кнопки
 * (Claude Code хранит туда `session.sessionId.value`).
 *
 * Бэкенд: http-server.py в .claude/hooks/, endpoints:
 *   - GET  /list-projects
 *   - POST /move-session   (auto-detect source если не указан)
 *   - POST /create-project
 *
 * Изолировано от основной IIFE — отдельный модуль с защитой
 * от двойной установки через window.__claudeSessionMoverInstalled.
 */
(function () {
  if (window.__claudeSessionMoverInstalled) return;
  window.__claudeSessionMoverInstalled = true;

  // Единственный модуль, у которого выключателя не было: без него
  // безопасный режим не мог погасить всё и поиск виновника поломки
  // упирался бы в неснимаемую кнопку в списке сессий.
  if ((window.__CLAUDE_CUSTOM_CONFIG__ || {}).sessionMover === false) return;

  var API_BASE = 'http://localhost:18923';
  var MOVE_BTN_CLASS = 'claude-move-session-btn';
  var INSTALLED_ATTR = 'data-claude-move-installed';

  function logInfo() {
    var cfg = window.__CLAUDE_CUSTOM_CONFIG__ || {};
    if (!cfg.logs) return;
    try { console.log.apply(console, ['[session-mover]'].concat([].slice.call(arguments))); } catch (e) {}
  }

  /** Отправляет диагностику на http-server (логирует в файл). */
  function sendDiag(tag, data) {
    try {
      fetch(API_BASE + '/diag', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ tag: tag, data: data || {} }),
        keepalive: true,
      }).catch(function () {});
    } catch (e) {}
  }

  sendDiag('install', { ua: navigator.userAgent, ts: Date.now() });

  /** Извлекает sessionId из React-fiber sessionItem.
   *
   * DOM-fiber кнопки (<button class="sessionItem_*">) имеет key=null,
   * потому что `key` в React передаётся не на DOM-элемент, а на
   * fiber родительского компонента (SessionItem forwardRef). Поднимаемся
   * по `.return` пока не найдём fiber с непустым строковым ключом —
   * это и есть `session.sessionId.value` (UUID).
   *
   * Fallback: если key — число (порядковый индекс из `??o1`), значит
   * sessionId.value отсутствовал; такие сессии пропускаем.
   */
  function getSessionIdFromElement(el) {
    if (!el) return null;
    var keys = Object.keys(el);
    var fiber = null;
    for (var i = 0; i < keys.length; i++) {
      if (keys[i].indexOf('__reactFiber') === 0) {
        fiber = el[keys[i]];
        break;
      }
    }
    if (!fiber) return null;
    var hops = 0;
    while (fiber && hops < 10) {
      if (typeof fiber.key === 'string' && fiber.key.length > 0) {
        return fiber.key;
      }
      fiber = fiber.return;
      hops++;
    }
    return null;
  }

  function getSessionName(sessionItem) {
    var nameEl = sessionItem.querySelector('[class*="sessionName_"]');
    if (!nameEl) return '(unnamed)';
    return (nameEl.textContent || '').trim() || '(unnamed)';
  }

  function createMoveButton(sessionId, sessionName) {
    var btn = document.createElement('button');
    btn.className = MOVE_BTN_CLASS;
    btn.title = 'Переместить в другой проект';
    btn.setAttribute('aria-label', 'Переместить сессию в другой проект');
    btn.setAttribute('type', 'button');
    btn.textContent = '📁';
    btn.addEventListener('click', function (e) {
      e.stopPropagation();
      e.preventDefault();
      openMoveDialog(sessionId, sessionName);
    });
    // sessionItem-кнопка реагирует на mousedown — глушим оба события
    btn.addEventListener('mousedown', function (e) { e.stopPropagation(); });
    return btn;
  }

  function processSessionItem(sessionItem) {
    if (sessionItem.getAttribute(INSTALLED_ATTR) === '1') return;
    var actionsEl = sessionItem.querySelector('[class*="sessionActions_"]');
    if (!actionsEl) return;
    var sessionId = getSessionIdFromElement(sessionItem);
    if (!sessionId) return;
    var sessionName = getSessionName(sessionItem);
    var btn = createMoveButton(sessionId, sessionName);
    var deleteBtn = actionsEl.querySelector('[class*="deleteButton_"]');
    if (deleteBtn) {
      actionsEl.insertBefore(btn, deleteBtn);
    } else {
      actionsEl.appendChild(btn);
    }
    sessionItem.setAttribute(INSTALLED_ATTR, '1');
  }

  function scanSessionItems(ctx) {
    // Узлы даёт общий обход (см. DOM WATCH) — один на все модули.
    // Свой поиск остаётся для вызовов вне прохода: при регистрации
    // и из обработчиков самого модуля.
    var items = (ctx && ctx.sessions)
      || document.querySelectorAll('[class*="sessionItem_"]');
    if (!window.__claudeMoverScanLogged && items.length > 0) {
      window.__claudeMoverScanLogged = true;
      var first = items[0];
      var reactKeys = Object.keys(first).filter(function (k) {
        return k.indexOf('__react') === 0 || k.indexOf('_react') === 0;
      });
      var fiberKey = reactKeys.filter(function (k) { return k.indexOf('Fiber') >= 0; })[0];
      var fiber = fiberKey ? first[fiberKey] : null;
      // Собираем цепочку .return и их keys (до 6 уровней вверх),
      // чтобы видеть, на каком предке хранится sessionId.value.
      var chain = [];
      var f = fiber;
      for (var h = 0; h < 6 && f; h++) {
        chain.push({
          type: f.type && (f.type.displayName || f.type.name || (typeof f.type === 'string' ? f.type : '<anon>')),
          key: typeof f.key === 'string' ? f.key : (f.key == null ? null : String(f.key)),
        });
        f = f.return;
      }
      sendDiag('first-scan', {
        itemCount: items.length,
        firstClassName: (first.className || '').slice(0, 200),
        reactKeys: reactKeys,
        fiberKey: fiberKey || null,
        fiberKeyOnFiber: fiber && typeof fiber.key !== 'undefined' ? String(fiber.key) : 'NO-KEY',
        returnChain: chain,
        hasSessionActions: !!first.querySelector('[class*="sessionActions_"]'),
        hasDeleteBtn: !!first.querySelector('[class*="deleteButton_"]'),
      });
    }
    var inserted = 0;
    for (var i = 0; i < items.length; i++) {
      try {
        var before = items[i].getAttribute(INSTALLED_ATTR) === '1';
        processSessionItem(items[i]);
        if (!before && items[i].getAttribute(INSTALLED_ATTR) === '1') inserted++;
      } catch (e) {
        sendDiag('process-error', { err: String(e) });
      }
    }
    if (inserted > 0) sendDiag('btn-inserted', { count: inserted });
  }

  function pluralize(n, forms) {
    var mod10 = n % 10, mod100 = n % 100;
    if (mod100 >= 11 && mod100 <= 14) return forms[2];
    if (mod10 === 1) return forms[0];
    if (mod10 >= 2 && mod10 <= 4) return forms[1];
    return forms[2];
  }

  /** Возвращает имя последней папки пути.
   * '/home/vladimir/Документы/Projects/Flying_Player' → 'Flying_Player'
   * '/home/vladimir' → 'vladimir'
   * '/'             → '/'
   */
  function basenameOfPath(p) {
    if (!p) return p;
    var s = String(p);
    // Убираем trailing slashes (кроме самого корня)
    while (s.length > 1 && s.charAt(s.length - 1) === '/') {
      s = s.slice(0, -1);
    }
    var idx = s.lastIndexOf('/');
    if (idx < 0) return s;
    if (idx === 0 && s.length === 1) return '/';
    return s.slice(idx + 1) || s;
  }

  /** Открывает модалку переноса сессии. */
  function openMoveDialog(sessionId, sessionName) {
    closeMoveDialog();

    var overlay = document.createElement('div');
    overlay.id = 'claude-move-overlay';
    overlay.className = 'claude-move-overlay';

    var modal = document.createElement('div');
    modal.className = 'claude-move-modal';
    overlay.appendChild(modal);

    var header = document.createElement('div');
    header.className = 'claude-move-header';
    header.textContent = 'Переместить сессию';
    modal.appendChild(header);

    var sub = document.createElement('div');
    sub.className = 'claude-move-subheader';
    sub.textContent = sessionName;
    modal.appendChild(sub);

    var sid = document.createElement('div');
    sid.className = 'claude-move-sid';
    sid.textContent = 'ID: ' + sessionId;
    modal.appendChild(sid);

    var loading = document.createElement('div');
    loading.className = 'claude-move-loading';
    loading.textContent = 'Загрузка списка проектов...';
    modal.appendChild(loading);

    var listLabel = document.createElement('div');
    listLabel.className = 'claude-move-section-label';
    listLabel.textContent = 'Выбрать существующий проект:';
    listLabel.style.display = 'none';
    modal.appendChild(listLabel);

    var listEl = document.createElement('div');
    listEl.className = 'claude-move-list';
    listEl.style.display = 'none';
    modal.appendChild(listEl);

    var newLabel = document.createElement('div');
    newLabel.className = 'claude-move-section-label';
    newLabel.textContent = 'Создать новый проект:';
    newLabel.style.display = 'none';
    modal.appendChild(newLabel);

    var newRow = document.createElement('div');
    newRow.className = 'claude-move-new-row';
    newRow.style.display = 'none';
    modal.appendChild(newRow);

    var newInput = document.createElement('input');
    newInput.type = 'text';
    newInput.className = 'claude-move-new-input';
    newInput.placeholder = '/абсолютный/путь/к/проекту';
    newRow.appendChild(newInput);

    var newBtn = document.createElement('button');
    newBtn.className = 'claude-move-new-btn';
    newBtn.textContent = 'Создать и переместить';
    newBtn.setAttribute('type', 'button');
    newBtn.addEventListener('click', function () {
      var p = (newInput.value || '').trim();
      if (!p) { newInput.focus(); return; }
      createProjectAndMove(sessionId, p);
    });
    newRow.appendChild(newBtn);

    var footer = document.createElement('div');
    footer.className = 'claude-move-footer';
    modal.appendChild(footer);

    var cancelBtn = document.createElement('button');
    cancelBtn.className = 'claude-move-cancel';
    cancelBtn.textContent = 'Отмена';
    cancelBtn.setAttribute('type', 'button');
    cancelBtn.addEventListener('click', closeMoveDialog);
    footer.appendChild(cancelBtn);

    overlay.addEventListener('click', function (e) {
      if (e.target === overlay) closeMoveDialog();
    });
    var escHandler = function (e) {
      if (e.key === 'Escape') closeMoveDialog();
    };
    document.addEventListener('keydown', escHandler);
    overlay._escHandler = escHandler;

    document.body.appendChild(overlay);

    loadProjectList(sessionId, sessionName, loading, listLabel, listEl, newLabel, newRow);
  }

  /** Делает fetch GET /list-projects с авто-ретраями: до 10 попыток
   * с интервалом 1 сек. Каждая попытка попадает строкой в лог
   * (внутри блока `loading`). При успехе лог сворачивается до одной
   * строки. При финальном фейле — кнопка «Повторить» под логом. */
  function loadProjectList(sessionId, sessionName, loading, listLabel, listEl, newLabel, newRow) {
    var MAX_ATTEMPTS = 10;
    var RETRY_DELAY_MS = 1000;

    // Перестраиваем содержимое loading: заголовок + контейнер с логом.
    while (loading.firstChild) loading.removeChild(loading.firstChild);
    loading.style.display = '';

    var logTitle = document.createElement('div');
    logTitle.textContent = 'Загрузка списка проектов...';
    loading.appendChild(logTitle);

    var logBox = document.createElement('div');
    logBox.className = 'claude-move-log';
    loading.appendChild(logBox);

    listLabel.style.display = 'none';
    listEl.style.display = 'none';
    newLabel.style.display = 'none';
    newRow.style.display = 'none';
    while (listEl.firstChild) listEl.removeChild(listEl.firstChild);

    function nowStr() {
      var d = new Date();
      function pad(n) { return n < 10 ? '0' + n : '' + n; }
      return pad(d.getHours()) + ':' + pad(d.getMinutes()) + ':' + pad(d.getSeconds());
    }
    function appendLogLine(text, kind) {
      var line = document.createElement('div');
      line.className = 'claude-move-log-line ' +
        (kind ? ('claude-move-log-' + kind) : '');
      line.textContent = '[' + nowStr() + '] ' + text;
      logBox.appendChild(line);
      logBox.scrollTop = logBox.scrollHeight;
    }

    function attempt(n) {
      appendLogLine('Попытка ' + n + '/' + MAX_ATTEMPTS + ': GET /list-projects ...');
      fetch(API_BASE + '/list-projects')
        .then(function (r) {
          if (!r.ok) throw new Error('HTTP ' + r.status);
          return r.json();
        })
        .then(function (data) {
          var count = (data && data.projects) ? data.projects.length : 0;
          appendLogLine('✓ Получено ' + count + ' проект(ов)', 'ok');
          onSuccess(data);
        })
        .catch(function (err) {
          var emsg = (err && err.message) || String(err);
          appendLogLine('✗ Ошибка: ' + emsg, 'err');
          if (n < MAX_ATTEMPTS) {
            setTimeout(function () { attempt(n + 1); }, RETRY_DELAY_MS);
          } else {
            onFinalError(err);
          }
        });
    }

    function onSuccess(data) {
      loading.style.display = 'none';
      listLabel.style.display = '';
      listEl.style.display = '';
      newLabel.style.display = '';
      newRow.style.display = '';

      var projects = (data && data.projects) || [];
      if (!projects.length) {
        var empty = document.createElement('div');
        empty.className = 'claude-move-empty';
        empty.textContent = 'Нет существующих проектов';
        listEl.appendChild(empty);
        return;
      }
      projects.forEach(function (p) {
        var fullPath = p.display_name || p.encoded_name;
        var shortName = basenameOfPath(fullPath);

        var item = document.createElement('button');
        item.className = 'claude-move-item';
        item.setAttribute('type', 'button');
        // Полный путь — в нативном tooltip (browser показывает его
        // после ~500мс удержания курсора).
        item.setAttribute('title', fullPath);

        var name = document.createElement('div');
        name.className = 'claude-move-item-name';
        name.textContent = shortName;
        item.appendChild(name);

        var meta = document.createElement('div');
        meta.className = 'claude-move-item-meta';
        var count = (typeof p.session_count === 'number') ? p.session_count : 0;
        meta.textContent = count + ' ' + pluralize(count, ['сессия', 'сессии', 'сессий']);
        item.appendChild(meta);

        item.addEventListener('click', function () {
          showConfirmMove(sessionId, sessionName,
                          p.encoded_name, fullPath, shortName);
        });
        listEl.appendChild(item);
      });
    }

    function onFinalError(err) {
      logTitle.textContent = 'Все ' + MAX_ATTEMPTS + ' попыток провалились — сервер недоступен.';
      var retryBtn = document.createElement('button');
      retryBtn.className = 'claude-move-new-btn';
      retryBtn.textContent = 'Повторить';
      retryBtn.setAttribute('type', 'button');
      retryBtn.style.marginTop = '8px';
      retryBtn.addEventListener('click', function () {
        loadProjectList(sessionId, sessionName, loading, listLabel, listEl, newLabel, newRow);
      });
      loading.appendChild(retryBtn);
    }

    attempt(1);
  }

  function closeMoveDialog() {
    var overlay = document.getElementById('claude-move-overlay');
    if (!overlay) return;
    if (overlay._escHandler) {
      document.removeEventListener('keydown', overlay._escHandler);
    }
    overlay.remove();
  }

  function setBusy(busy) {
    var overlay = document.getElementById('claude-move-overlay');
    if (!overlay) return;
    if (busy) overlay.classList.add('claude-move-busy');
    else overlay.classList.remove('claude-move-busy');
  }

  /** Показывает диалог подтверждения переноса в текущей модалке.
   *
   * @param {string} sessionId    UUID переносимой сессии.
   * @param {string} sessionName  Имя сессии для отображения.
   * @param {string} targetEncoded Имя папки в ~/.claude/projects/.
   * @param {string} targetFullPath Полный путь проекта (для tooltip/info).
   * @param {string} targetShortName Basename проекта.
   *
   * При «Перенести» — moveSession + переход в success-тост.
   * При «Назад» — возврат к списку проектов через loadProjectList.
   */
  function showConfirmMove(sessionId, sessionName, targetEncoded,
                           targetFullPath, targetShortName) {
    var overlay = document.getElementById('claude-move-overlay');
    if (!overlay) return;
    var modal = overlay.querySelector('.claude-move-modal');
    if (!modal) return;
    modal.innerHTML = '';
    modal.classList.remove('claude-move-success');

    var h = document.createElement('div');
    h.className = 'claude-move-header';
    h.textContent = 'Подтвердите перенос';
    modal.appendChild(h);

    var question = document.createElement('div');
    question.className = 'claude-move-confirm-question';
    question.textContent = 'Вы действительно хотите перенести сессию';
    modal.appendChild(question);

    var sessionBox = document.createElement('div');
    sessionBox.className = 'claude-move-confirm-box';
    var sessionLabel = document.createElement('div');
    sessionLabel.className = 'claude-move-confirm-label';
    sessionLabel.textContent = 'Сессия';
    sessionBox.appendChild(sessionLabel);
    var sessionValue = document.createElement('div');
    sessionValue.className = 'claude-move-confirm-value';
    sessionValue.textContent = sessionName || '(без названия)';
    sessionValue.setAttribute('title', 'ID: ' + sessionId);
    sessionBox.appendChild(sessionValue);
    modal.appendChild(sessionBox);

    var arrow = document.createElement('div');
    arrow.className = 'claude-move-confirm-arrow';
    arrow.textContent = '↓';
    modal.appendChild(arrow);

    var targetBox = document.createElement('div');
    targetBox.className = 'claude-move-confirm-box';
    var targetLabel = document.createElement('div');
    targetLabel.className = 'claude-move-confirm-label';
    targetLabel.textContent = 'В проект';
    targetBox.appendChild(targetLabel);
    var targetValue = document.createElement('div');
    targetValue.className = 'claude-move-confirm-value';
    targetValue.textContent = targetShortName;
    targetBox.appendChild(targetValue);
    var targetPath = document.createElement('div');
    targetPath.className = 'claude-move-confirm-path';
    targetPath.textContent = targetFullPath;
    targetBox.appendChild(targetPath);
    modal.appendChild(targetBox);

    var footer = document.createElement('div');
    footer.className = 'claude-move-footer';
    modal.appendChild(footer);

    // Порядок: [Скопировать] (зелёная) — [Перенести] (синяя) —
    // [Отмена] (ghost). Увеличенный gap между ними через
    // .claude-move-footer-spaced (см. claude-custom.css).
    footer.classList.add('claude-move-footer-spaced');

    var copyBtn = document.createElement('button');
    copyBtn.className = 'claude-move-new-btn claude-move-copy';
    copyBtn.textContent = 'Скопировать';
    copyBtn.setAttribute('type', 'button');
    copyBtn.setAttribute('title',
      'Создать копию сессии в выбранном проекте (с новым UUID); исходная остаётся на месте');
    copyBtn.addEventListener('click', function () {
      copySession(sessionId, targetEncoded);
    });
    footer.appendChild(copyBtn);

    var confirmBtn = document.createElement('button');
    confirmBtn.className = 'claude-move-new-btn';
    confirmBtn.textContent = 'Перенести';
    confirmBtn.setAttribute('type', 'button');
    confirmBtn.addEventListener('click', function () {
      moveSession(sessionId, targetEncoded);
    });
    footer.appendChild(confirmBtn);

    var backBtn = document.createElement('button');
    backBtn.className = 'claude-move-cancel claude-move-ghost';
    backBtn.textContent = 'Отмена';
    backBtn.setAttribute('type', 'button');
    backBtn.addEventListener('click', function () {
      // Возвращаемся к списку проектов — перерисовываем модалку заново
      // через openMoveDialog. Это удалит overlay и создаст новый,
      // что приведёт к повторной загрузке списка.
      closeMoveDialog();
      openMoveDialog(sessionId, sessionName);
    });
    footer.appendChild(backBtn);
  }

  /** Копирует сессию в другой проект через POST /copy-session.
   * В отличие от moveSession, исходная сессия остаётся на месте,
   * а у копии новый UUID. */
  function copySession(sessionId, targetEncoded) {
    setBusy(true);
    fetch(API_BASE + '/copy-session', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: sessionId, target_project: targetEncoded }),
    })
      .then(function (r) { return r.json().then(function (j) { return { ok: r.ok, body: j }; }); })
      .then(function (res) {
        if (!res.ok) {
          alert('Ошибка копирования: ' + ((res.body && res.body.error) || 'unknown'));
          setBusy(false);
          return;
        }
        logInfo('copied', res.body);
        sendDiag('copy-success', {
          sessionId: sessionId,
          newSessionId: res.body && res.body.new_session_id,
        });
        // Список текущего проекта не меняется (исходная сессия осталась),
        // поэтому signal-refresh не нужен. Показываем success-тост,
        // refreshMethod='signal' чтобы не предлагать Reload Window.
        showCopySuccessToast(targetEncoded, res.body && res.body.new_session_id);
      })
      .catch(function (err) {
        alert('Сетевая ошибка: ' + ((err && err.message) || err));
        setBusy(false);
      });
  }

  function showCopySuccessToast(targetEncoded, newSessionId) {
    var overlay = document.getElementById('claude-move-overlay');
    if (!overlay) return;
    var modal = overlay.querySelector('.claude-move-modal');
    if (!modal) return;
    modal.innerHTML = '';
    modal.classList.add('claude-move-success');

    var h = document.createElement('div');
    h.className = 'claude-move-header';
    h.textContent = '✓ Сессия скопирована';
    modal.appendChild(h);

    var sub = document.createElement('div');
    sub.className = 'claude-move-subheader';
    sub.textContent = 'В проект: ' + targetEncoded;
    modal.appendChild(sub);

    if (newSessionId) {
      var sid = document.createElement('div');
      sid.className = 'claude-move-sid';
      sid.textContent = 'Новый ID: ' + newSessionId;
      modal.appendChild(sid);
    }

    var hint = document.createElement('div');
    hint.className = 'claude-move-hint';
    hint.textContent = 'Исходная сессия осталась на месте. ' +
      'Чтобы открыть копию в её проекте — переключитесь туда (откройте папку в VSCode).';
    modal.appendChild(hint);

    var footer = document.createElement('div');
    footer.className = 'claude-move-footer';
    modal.appendChild(footer);

    var okBtn = document.createElement('button');
    okBtn.className = 'claude-move-cancel';
    okBtn.textContent = 'Закрыть';
    okBtn.setAttribute('type', 'button');
    okBtn.addEventListener('click', closeMoveDialog);
    footer.appendChild(okBtn);

    overlay.classList.remove('claude-move-busy');
  }

  function moveSession(sessionId, targetEncoded) {
    setBusy(true);
    fetch(API_BASE + '/move-session', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: sessionId, target_project: targetEncoded }),
    })
      .then(function (r) { return r.json().then(function (j) { return { ok: r.ok, body: j }; }); })
      .then(function (res) {
        if (!res.ok) {
          alert('Ошибка переноса: ' + ((res.body && res.body.error) || 'unknown'));
          setBusy(false);
          return;
        }
        logInfo('moved', res.body);
        // Пытаемся обновить список сразу: убрать перенесённую сессию
        // из React signal-store, минуя reload window (он ломает webview).
        var refreshed = tryRefreshList(sessionId);
        sendDiag('move-success', { sessionId: sessionId, refreshed: refreshed });
        var sourceProject = res.body && res.body.source_project;
        showSuccessToast(targetEncoded, refreshed, sessionId, sourceProject);
      })
      .catch(function (err) {
        alert('Сетевая ошибка: ' + ((err && err.message) || err));
        setBusy(false);
      });
  }

  // ---- Авто-обновление списка сессий после переноса ----
  //
  // Самый чистый способ — попросить React signal-store удалить
  // перенесённую сессию из массива `sessions.value`. Если по какой-то
  // причине обнаружить store не удалось, fallback — программный клик
  // по табу Web → Local, который заставляет панель перерисоваться.

  function findReactRootFiberLocal() {
    var rootEl = document.getElementById('root');
    if (!rootEl) return null;
    var keys = Object.keys(rootEl);
    for (var i = 0; i < keys.length; i++) {
      if (keys[i].indexOf('__reactContainer$') === 0) {
        var container = rootEl[keys[i]];
        if (container && container.stateNode && container.stateNode.current) {
          return container.stateNode.current;
        }
        if (container && container.current) return container.current;
        return container;
      }
    }
    return null;
  }

  function walkFibersLocal(rootFiber, visit) {
    if (!rootFiber) return;
    var stack = [rootFiber];
    var visited = new Set();
    while (stack.length) {
      var fiber = stack.pop();
      if (!fiber || visited.has(fiber)) continue;
      visited.add(fiber);
      if (visit(fiber) === true) return;
      if (fiber.child) stack.push(fiber.child);
      if (fiber.sibling) stack.push(fiber.sibling);
    }
  }

  /** Ищет signal `sessions` со списком сессий проекта. Признак —
   * объект-родитель имеет и `sessions`, и `activeSession`, где
   * `sessions.value` — массив, а в каждом элементе массива есть
   * `sessionId.value` (UUID). */
  function findSessionsSignal() {
    var rootFiber = findReactRootFiberLocal();
    if (!rootFiber) return null;
    var found = null;
    walkFibersLocal(rootFiber, function (fiber) {
      var sources = [fiber.memoizedProps, fiber.memoizedState, fiber.stateNode];
      for (var i = 0; i < sources.length; i++) {
        var c = sources[i];
        if (!c || typeof c !== 'object') continue;
        var container = null;
        if ('sessions' in c && 'activeSession' in c) container = c;
        else if (c.context && typeof c.context === 'object' &&
                 'sessions' in c.context && 'activeSession' in c.context) {
          container = c.context;
        }
        if (!container) continue;
        var sig = container.sessions;
        if (sig && typeof sig === 'object' && 'value' in sig &&
            Array.isArray(sig.value)) {
          // Доп. валидация: элементы массива должны выглядеть как сессии
          var v = sig.value;
          if (v.length === 0 ||
              (v[0] && v[0].sessionId && 'value' in v[0].sessionId)) {
            found = sig;
            return true;
          }
        }
      }
    });
    return found;
  }

  /** Программный клик Web → Local — fallback если signal не найден. */
  function tabTrick() {
    var segmented = document.querySelector('[class*="segmented_"]');
    if (!segmented) return false;
    var tabs = segmented.querySelectorAll('button, [role="tab"]');
    if (tabs.length < 2) return false;
    var activeIdx = -1, otherIdx = -1;
    for (var i = 0; i < tabs.length; i++) {
      var cn = tabs[i].className || '';
      if (cn.indexOf('tabActive_') >= 0 || cn.indexOf('active_') >= 0) activeIdx = i;
      else if (otherIdx < 0) otherIdx = i;
    }
    if (activeIdx < 0 || otherIdx < 0) return false;
    try {
      tabs[otherIdx].click();
      setTimeout(function () { try { tabs[activeIdx].click(); } catch (e) {} }, 80);
      return true;
    } catch (e) { return false; }
  }

  /** Главный обработчик авто-обновления. Возвращает строку с
   * описанием способа: 'signal' | 'tab' | 'none'. */
  function tryRefreshList(removedSessionId) {
    try {
      var sig = findSessionsSignal();
      if (sig) {
        var filtered = sig.value.filter(function (s) {
          return !(s && s.sessionId && s.sessionId.value === removedSessionId);
        });
        if (filtered.length !== sig.value.length) {
          sig.value = filtered; // signal setter — триггер ре-рендера
          return 'signal';
        }
      }
    } catch (e) {
      sendDiag('refresh-signal-error', { err: String(e) });
    }
    try {
      if (tabTrick()) return 'tab';
    } catch (e) {
      sendDiag('refresh-tab-error', { err: String(e) });
    }
    return 'none';
  }

  /** Показывает экран успеха в той же модалке.
   *
   * Раньше после переноса вызывали window.location.reload() — это ломало
   * webview Claude Code (после reload панель оставалась пустой, потому
   * что webview теряет связь с extension host).
   *
   * Теперь модалка превращается в success-сообщение с просьбой
   * закрыть/открыть панель Claude Code (или сделать Reload Window),
   * чтобы расширение перечитало список сессий из ~/.claude/projects/.
   */
  function showSuccessToast(targetEncoded, refreshMethod, sessionId, sourceProject) {
    // sourceProject больше не используется в этом тосте — отмена
    // переноса теперь происходит через confirmation-диалог *до* фактического
    // переноса (см. showConfirmMove), а не после.
    void sourceProject;

    var overlay = document.getElementById('claude-move-overlay');
    if (!overlay) return;
    var modal = overlay.querySelector('.claude-move-modal');
    if (!modal) return;
    modal.innerHTML = '';
    modal.classList.add('claude-move-success');

    var h = document.createElement('div');
    h.className = 'claude-move-header';
    h.textContent = '✓ Сессия перенесена';
    modal.appendChild(h);

    var sub = document.createElement('div');
    sub.className = 'claude-move-subheader';
    sub.textContent = 'В проект: ' + targetEncoded;
    modal.appendChild(sub);

    var hint = document.createElement('div');
    hint.className = 'claude-move-hint';
    if (refreshMethod === 'signal' || refreshMethod === 'tab') {
      hint.textContent = 'Список сессий обновлён автоматически.';
    } else {
      hint.textContent = 'Не удалось автоматически обновить список. ' +
        'Сделайте Developer: Reload Window — сессия точно перенесена ' +
        'на диск, после reload список перечитается.';
    }
    modal.appendChild(hint);

    var footer = document.createElement('div');
    footer.className = 'claude-move-footer';
    modal.appendChild(footer);

    if (refreshMethod !== 'signal' && refreshMethod !== 'tab') {
      var reloadBtn = document.createElement('button');
      reloadBtn.className = 'claude-move-new-btn';
      reloadBtn.textContent = 'Reload Window';
      reloadBtn.setAttribute('type', 'button');
      reloadBtn.addEventListener('click', function () {
        try { window.location.reload(); } catch (e) {}
      });
      footer.appendChild(reloadBtn);
    }

    var okBtn = document.createElement('button');
    okBtn.className = 'claude-move-cancel';
    okBtn.textContent = 'Закрыть';
    okBtn.setAttribute('type', 'button');
    okBtn.addEventListener('click', closeMoveDialog);
    footer.appendChild(okBtn);

    overlay.classList.remove('claude-move-busy');
  }

  /** Обратный перенос: возвращает сессию в исходный проект.
   * Используется кнопкой «Отменить» в success-тосте. */
  function undoMove(sessionId, sourceProject, btn) {
    if (btn) {
      btn.disabled = true;
      btn.textContent = 'Возврат...';
    }
    setBusy(true);
    fetch(API_BASE + '/move-session', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: sessionId, target_project: sourceProject }),
    })
      .then(function (r) { return r.json().then(function (j) { return { ok: r.ok, body: j }; }); })
      .then(function (res) {
        if (!res.ok) {
          alert('Не удалось отменить: ' + ((res.body && res.body.error) || 'unknown'));
          if (btn) { btn.disabled = false; btn.textContent = 'Отменить'; }
          setBusy(false);
          return;
        }
        logInfo('undo done', res.body);
        sendDiag('undo-success', { sessionId: sessionId });
        // Перерисуем тост: показываем что вернули, без дальнейшего undo
        showUndoneToast(sourceProject);
      })
      .catch(function (err) {
        alert('Сетевая ошибка при отмене: ' + ((err && err.message) || err));
        if (btn) { btn.disabled = false; btn.textContent = 'Отменить'; }
        setBusy(false);
      });
  }

  function showUndoneToast(returnedTo) {
    var overlay = document.getElementById('claude-move-overlay');
    if (!overlay) return;
    var modal = overlay.querySelector('.claude-move-modal');
    if (!modal) return;
    modal.innerHTML = '';
    // Без класса 'claude-move-success' — нейтральный вид

    var h = document.createElement('div');
    h.className = 'claude-move-header';
    h.textContent = '↶ Перенос отменён';
    modal.appendChild(h);

    var sub = document.createElement('div');
    sub.className = 'claude-move-subheader';
    sub.textContent = 'Сессия возвращена в: ' + returnedTo;
    modal.appendChild(sub);

    var footer = document.createElement('div');
    footer.className = 'claude-move-footer';
    modal.appendChild(footer);

    var reloadBtn = document.createElement('button');
    reloadBtn.className = 'claude-move-new-btn';
    reloadBtn.textContent = 'Reload Window';
    reloadBtn.setAttribute('type', 'button');
    reloadBtn.setAttribute('title',
      'Чтобы вернувшаяся сессия снова появилась в списке этого проекта');
    reloadBtn.addEventListener('click', function () {
      try { window.location.reload(); } catch (e) {}
    });
    footer.appendChild(reloadBtn);

    var okBtn = document.createElement('button');
    okBtn.className = 'claude-move-cancel';
    okBtn.textContent = 'Закрыть';
    okBtn.setAttribute('type', 'button');
    okBtn.addEventListener('click', closeMoveDialog);
    footer.appendChild(okBtn);

    overlay.classList.remove('claude-move-busy');
  }

  function createProjectAndMove(sessionId, absPath) {
    setBusy(true);
    fetch(API_BASE + '/create-project', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ path: absPath }),
    })
      .then(function (r) { return r.json().then(function (j) { return { ok: r.ok, body: j }; }); })
      .then(function (res) {
        if (!res.ok) {
          alert('Не удалось создать проект: ' + ((res.body && res.body.error) || 'unknown'));
          setBusy(false);
          return;
        }
        logInfo('project created', res.body);
        // Серверу target_path передавать не нужно: при /create-project
        // он уже записал .cwd в новую папку, /move-session прочитает.
        moveSession(sessionId, res.body.encoded_name);
      })
      .catch(function (err) {
        alert('Сетевая ошибка: ' + ((err && err.message) || err));
        setBusy(false);
      });
  }

  function init() {
    sendDiag('init', { readyState: document.readyState, hasBody: !!document.body });
    // Свой наблюдатель и свой таймер-подстраховка заменены общими —
    // см. DOM WATCH. Первый скан делает сама регистрация.
    window.__claudeDomWatch.register('session-mover', scanSessionItems);
    logInfo('installed');
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();

/* ============================================================
 * EMOJI CATALOG — данные смайликов для picker'а и автозамены
 * ============================================================
 *
 * Компактный формат хранения: одна строка на категорию, элементы
 * разделены `|`, внутри элемента поля разделены пробелом:
 *
 *     <символ> <shortcode> <ключевое слово> <ключевое слово> ...
 *
 * Первое поле — сам эмодзи, второе — латинский shortcode (по нему
 * работает автозамена вида `:rocket:`), остальные — ключевые слова
 * для поиска (русские и английские). Все поля участвуют в поиске.
 *
 * Каталог регистрируется в `window.__claudeEmojiCatalog` независимо
 * от флагов конфига: им пользуются и EMOJI PICKER, и EMOJI AUTOREPLACE,
 * каждый из которых включается своим параметром.
 * ============================================================ */
(function () {
  if (window.__claudeEmojiCatalog) return;

  var RAW = [
    { id: 'smileys', name: 'Смайлы', icon: '😀', data:
      '😀 grinning улыбка радость smile happy|' +
      '😃 smiley улыбка радость open|' +
      '😄 laughing смех радость joy|' +
      '😁 beaming ухмылка зубы grin|' +
      '😆 grin_squint смех хохот laugh|' +
      '😅 sweat_smile нервный смех пот|' +
      '🤣 rofl хохот смех rolling|' +
      '😂 joy слёзы смех плачу tears|' +
      '🙂 slight_smile улыбка лёгкая|' +
      '🙃 upside_down перевёрнутый ирония|' +
      '😉 wink подмигивание флирт|' +
      '😊 blush смущение улыбка|' +
      '😇 innocent ангел нимб|' +
      '🥰 smiling_hearts влюблён сердечки love|' +
      '😍 heart_eyes влюблён восторг love|' +
      '🤩 star_struck звёзды восторг wow|' +
      '😘 kiss поцелуй воздушный|' +
      '😗 kissing поцелуй губы|' +
      '🥲 smile_tear улыбка слеза|' +
      '😋 yum вкусно язык tasty|' +
      '😛 tongue язык дразнить|' +
      '😜 winky_tongue язык подмигивание|' +
      '🤪 zany безумие дурачусь crazy|' +
      '😝 squint_tongue язык зажмурился|' +
      '🤑 money_mouth деньги доллар богатство|' +
      '🤗 hug обнимаю объятия hugs|' +
      '🤭 hand_over_mouth ой упс oops|' +
      '🤫 shush тише секрет quiet|' +
      '🤔 thinking думаю размышляю hmm|' +
      '🤐 zipper молчу рот закрыт|' +
      '🤨 raised_eyebrow бровь скепсис|' +
      '😐 neutral нейтрально безразлично|' +
      '😑 expressionless без эмоций|' +
      '😶 no_mouth без рта молчание|' +
      '😏 smirk ухмылка хитрость|' +
      '😒 unamused недовольство скука|' +
      '🙄 roll_eyes закатываю глаза|' +
      '😬 grimace неловко гримаса|' +
      '🤥 lying врёт нос ложь|' +
      '😌 relieved облегчение спокойствие|' +
      '😔 pensive грусть печаль sad|' +
      '😪 sleepy сонный устал|' +
      '🤤 drooling слюни|' +
      '😴 sleeping сплю сон zzz|' +
      '😷 mask маска болезнь|' +
      '🤒 thermometer температура болен|' +
      '🤕 head_bandage травма бинт|' +
      '🤢 nauseated тошнит|' +
      '🤮 vomiting рвота|' +
      '🤧 sneezing чихаю|' +
      '🥵 hot_face жарко жара|' +
      '🥶 cold_face холодно мороз|' +
      '🥴 woozy пьяный головокружение|' +
      '😵 dizzy_face без сознания|' +
      '🤯 exploding_head взрыв мозга шок|' +
      '🤠 cowboy ковбой шляпа|' +
      '🥳 partying праздник вечеринка party|' +
      '😎 sunglasses крутой очки cool|' +
      '🤓 nerd ботаник очки|' +
      '🧐 monocle монокль изучаю|' +
      '😕 confused растерянность|' +
      '😟 worried беспокойство|' +
      '🙁 slight_frown грусть|' +
      '😮 open_mouth удивление wow|' +
      '😯 hushed удивление|' +
      '😲 astonished шок изумление|' +
      '😳 flushed смущение краснею|' +
      '🥺 pleading умоляю прошу|' +
      '😦 frowning хмурюсь|' +
      '😨 fearful страх испуг|' +
      '😰 anxious тревога пот|' +
      '😢 cry плачу слеза sad|' +
      '😭 sob рыдаю плач|' +
      '😱 scream крик ужас|' +
      '😖 confounded мучение|' +
      '😣 persevere терплю|' +
      '😞 disappointed разочарование|' +
      '😩 weary устал|' +
      '😫 tired измотан|' +
      '🥱 yawning зевота скука|' +
      '😤 triumph злость пар|' +
      '😡 rage ярость злой angry|' +
      '😠 angry злой сердитый|' +
      '🤬 cursing ругань мат|' +
      '😈 smiling_imp чертёнок озорство|' +
      '👿 imp чёрт злой|' +
      '💀 skull череп смерть dead|' +
      '💩 poop какашка|' +
      '🤡 clown клоун|' +
      '👻 ghost привидение|' +
      '👽 alien пришелец|' +
      '🤖 robot робот бот|' +
      '😺 smiley_cat кот улыбка|' +
      '😻 heart_eyes_cat кот влюблён'
    },
    { id: 'gestures', name: 'Жесты', icon: '👍', data:
      '👍 thumbsup лайк класс отлично like|' +
      '👎 thumbsdown дизлайк плохо|' +
      '👌 ok_hand окей отлично|' +
      '🤌 pinched щепотка|' +
      '✌️ victory виктория мир|' +
      '🤞 crossed_fingers удача скрещенные|' +
      '🤟 love_you люблю жест|' +
      '🤘 horns рок коза|' +
      '🤙 call_me звони|' +
      '👈 point_left влево|' +
      '👉 point_right вправо|' +
      '👆 point_up вверх|' +
      '👇 point_down вниз|' +
      '☝️ index_up вверх один|' +
      '✋ raised_hand ладонь стоп|' +
      '🤚 back_hand ладонь|' +
      '🖐️ hand_splayed ладонь пальцы|' +
      '🖖 vulcan вулкан спок|' +
      '👋 wave привет пока hello|' +
      '🤝 handshake рукопожатие договор deal|' +
      '🙏 pray спасибо молитва пожалуйста thanks|' +
      '✍️ writing пишу рука|' +
      '💪 muscle сила бицепс мощь|' +
      '🦾 mechanical_arm протез сила|' +
      '👏 clap аплодисменты браво|' +
      '🙌 raising_hands ура руки победа|' +
      '👐 open_hands ладони|' +
      '🤲 palms_up прошу|' +
      '🫶 heart_hands сердечко руками|' +
      '🤦 facepalm рукалицо фейспалм|' +
      '🤷 shrug пожимаю плечами не знаю|' +
      '🙋 raising_hand поднял руку вопрос|' +
      '🙆 ok_person окей жест|' +
      '🙅 no_good нельзя нет|' +
      '💁 tipping_hand информация|' +
      '🫡 salute отдаю честь|' +
      '🧑‍💻 technologist программист разработчик coder|' +
      '👨‍💻 man_technologist программист разработчик|' +
      '👩‍💻 woman_technologist программистка разработчица|' +
      '🕵️ detective детектив расследование|' +
      '👀 eyes глаза смотрю внимание look|' +
      '👁️ eye глаз|' +
      '🧠 brain мозг ум'
    },
    { id: 'nature', name: 'Природа', icon: '🌿', data:
      '🐶 dog собака пёс|' +
      '🐱 cat кот кошка|' +
      '🐭 mouse мышь|' +
      '🐹 hamster хомяк|' +
      '🐰 rabbit кролик заяц|' +
      '🦊 fox лиса|' +
      '🐻 bear медведь|' +
      '🐼 panda панда|' +
      '🐨 koala коала|' +
      '🐯 tiger тигр|' +
      '🦁 lion лев|' +
      '🐮 cow корова|' +
      '🐷 pig свинья|' +
      '🐸 frog лягушка|' +
      '🐵 monkey обезьяна|' +
      '🙈 see_no_evil обезьяна не вижу|' +
      '🙉 hear_no_evil обезьяна не слышу|' +
      '🙊 speak_no_evil обезьяна молчу|' +
      '🐔 chicken курица|' +
      '🐧 penguin пингвин|' +
      '🐦 bird птица|' +
      '🦆 duck утка|' +
      '🦉 owl сова|' +
      '🐺 wolf волк|' +
      '🐴 horse лошадь|' +
      '🦄 unicorn единорог|' +
      '🐝 bee пчела|' +
      '🐛 caterpillar гусеница червяк|' +
      '🦋 butterfly бабочка|' +
      '🐌 snail улитка|' +
      '🐞 lady_beetle божья коровка|' +
      '🐜 ant муравей|' +
      '🕷️ spider паук|' +
      '🐢 turtle черепаха|' +
      '🐍 snake змея питон python|' +
      '🦎 lizard ящерица|' +
      '🐙 octopus осьминог|' +
      '🦐 shrimp креветка|' +
      '🐬 dolphin дельфин|' +
      '🐳 whale кит|' +
      '🐟 fish рыба|' +
      '🦈 shark акула|' +
      '🐊 crocodile крокодил|' +
      '🐘 elephant слон|' +
      '🦒 giraffe жираф|' +
      '🌵 cactus кактус|' +
      '🌲 evergreen ёлка дерево|' +
      '🌳 tree дерево|' +
      '🌴 palm пальма|' +
      '🌱 seedling росток|' +
      '🌿 herb трава зелень|' +
      '🍀 clover клевер удача|' +
      '🍁 maple_leaf лист клён|' +
      '🍂 fallen_leaves листья осень|' +
      '🌷 tulip тюльпан|' +
      '🌹 rose роза|' +
      '🌺 hibiscus цветок|' +
      '🌻 sunflower подсолнух|' +
      '💐 bouquet букет|' +
      '🌸 cherry_blossom сакура цветение|' +
      '☀️ sunny солнце ясно|' +
      '🌤️ sun_small_cloud солнце облака|' +
      '⛅ partly_cloudy облачно|' +
      '☁️ cloud облако|' +
      '🌧️ rain дождь|' +
      '⛈️ thunderstorm гроза|' +
      '❄️ snowflake снежинка снег|' +
      '⛄ snowman снеговик|' +
      '🔥 fire огонь пожар горит|' +
      '💧 droplet капля вода|' +
      '🌊 ocean волна море|' +
      '🌈 rainbow радуга|' +
      '⭐ star звезда|' +
      '🌟 glowing_star звезда сияние|' +
      '✨ sparkles искры блеск магия|' +
      '⚡ zap молния энергия быстро|' +
      '🌙 crescent_moon луна ночь|' +
      '🌍 earth земля планета мир|' +
      '🪐 planet планета сатурн'
    },
    { id: 'food', name: 'Еда', icon: '🍎', data:
      '🍎 apple яблоко|' +
      '🍐 pear груша|' +
      '🍊 tangerine мандарин апельсин|' +
      '🍋 lemon лимон|' +
      '🍌 banana банан|' +
      '🍉 watermelon арбуз|' +
      '🍇 grapes виноград|' +
      '🍓 strawberry клубника|' +
      '🫐 blueberries черника|' +
      '🍒 cherries вишня черешня|' +
      '🍑 peach персик|' +
      '🥭 mango манго|' +
      '🍍 pineapple ананас|' +
      '🥥 coconut кокос|' +
      '🥝 kiwi киви|' +
      '🍅 tomato помидор|' +
      '🥑 avocado авокадо|' +
      '🍆 eggplant баклажан|' +
      '🥔 potato картошка|' +
      '🥕 carrot морковь|' +
      '🌽 corn кукуруза|' +
      '🌶️ hot_pepper перец острый|' +
      '🥒 cucumber огурец|' +
      '🥬 leafy_green салат|' +
      '🥦 broccoli брокколи|' +
      '🧄 garlic чеснок|' +
      '🧅 onion лук|' +
      '🍄 mushroom гриб|' +
      '🥜 peanuts арахис орехи|' +
      '🍞 bread хлеб|' +
      '🥐 croissant круассан|' +
      '🥖 baguette багет|' +
      '🥨 pretzel крендель|' +
      '🧀 cheese сыр|' +
      '🥚 egg яйцо|' +
      '🍳 cooking яичница готовка|' +
      '🥞 pancakes блины|' +
      '🧇 waffle вафля|' +
      '🥓 bacon бекон|' +
      '🍔 hamburger бургер|' +
      '🍟 fries картошка фри|' +
      '🍕 pizza пицца|' +
      '🌭 hotdog хотдог|' +
      '🥪 sandwich сэндвич|' +
      '🌮 taco тако|' +
      '🌯 burrito буррито|' +
      '🥗 salad салат|' +
      '🍝 spaghetti паста макароны|' +
      '🍜 ramen рамен лапша|' +
      '🍲 stew суп|' +
      '🍣 sushi суши|' +
      '🍤 fried_shrimp креветка|' +
      '🍚 rice рис|' +
      '🍦 ice_cream мороженое|' +
      '🍩 doughnut пончик|' +
      '🍪 cookie печенье|' +
      '🎂 birthday торт день рождения|' +
      '🍰 cake пирожное торт|' +
      '🍫 chocolate шоколад|' +
      '🍬 candy конфета|' +
      '🍭 lollipop леденец|' +
      '🍯 honey мёд|' +
      '🥛 milk молоко|' +
      '☕ coffee кофе|' +
      '🍵 tea чай|' +
      '🧃 juice сок|' +
      '🥤 cup_straw напиток|' +
      '🍺 beer пиво|' +
      '🍻 beers пиво тост|' +
      '🍷 wine вино|' +
      '🥂 champagne шампанское тост|' +
      '🍾 bottle_pop шампанское праздник|' +
      '🥃 whisky виски|' +
      '🧊 ice лёд'
    },
    { id: 'activity', name: 'Активность', icon: '⚽', data:
      '⚽ soccer футбол мяч|' +
      '🏀 basketball баскетбол|' +
      '🏈 football американский футбол|' +
      '⚾ baseball бейсбол|' +
      '🎾 tennis теннис|' +
      '🏐 volleyball волейбол|' +
      '🎱 pool бильярд|' +
      '🏓 ping_pong пинг понг|' +
      '🏸 badminton бадминтон|' +
      '🥊 boxing бокс|' +
      '🥋 martial_arts кимоно|' +
      '⛳ golf гольф|' +
      '🏹 bow_arrow лук стрела|' +
      '🎣 fishing рыбалка|' +
      '🏂 snowboard сноуборд|' +
      '⛷️ ski лыжи|' +
      '🏄 surfing сёрфинг|' +
      '🏊 swimming плавание|' +
      '🚴 cycling велосипед|' +
      '🏃 running бег|' +
      '🧗 climbing скалолазание|' +
      '🏆 trophy кубок победа|' +
      '🥇 gold_medal медаль первое место|' +
      '🥈 silver_medal медаль второе|' +
      '🥉 bronze_medal медаль третье|' +
      '🎖️ medal награда|' +
      '🎯 dart цель дартс точно|' +
      '🎮 video_game игра геймпад|' +
      '🕹️ joystick джойстик|' +
      '🎲 dice кубик игра|' +
      '🧩 puzzle пазл головоломка|' +
      '🎨 art искусство палитра|' +
      '🎭 performing театр маски|' +
      '🎤 microphone микрофон караоке|' +
      '🎧 headphones наушники музыка|' +
      '🎵 note нота музыка|' +
      '🎶 notes музыка ноты|' +
      '🎸 guitar гитара|' +
      '🎹 piano пианино|' +
      '🥁 drum барабан|' +
      '🎺 trumpet труба|' +
      '🎻 violin скрипка|' +
      '🎬 clapper кино хлопушка|' +
      '🎉 tada праздник ура поздравляю party|' +
      '🎊 confetti конфетти праздник|' +
      '🎈 balloon шарик|' +
      '🎁 gift подарок|' +
      '🎃 jack_o_lantern тыква хэллоуин|' +
      '🎄 christmas_tree ёлка новый год'
    },
    { id: 'travel', name: 'Транспорт', icon: '🚗', data:
      '🚗 car машина авто|' +
      '🚕 taxi такси|' +
      '🚙 suv внедорожник|' +
      '🚌 bus автобус|' +
      '🏎️ racing_car гонка болид|' +
      '🚓 police_car полиция|' +
      '🚑 ambulance скорая|' +
      '🚒 fire_engine пожарная|' +
      '🚚 truck грузовик|' +
      '🚜 tractor трактор|' +
      '🛴 scooter самокат|' +
      '🚲 bike велосипед|' +
      '🏍️ motorcycle мотоцикл|' +
      '✈️ airplane самолёт полёт|' +
      '🚀 rocket ракета запуск релиз|' +
      '🛸 ufo нло|' +
      '🚁 helicopter вертолёт|' +
      '⛵ sailboat парусник|' +
      '🚤 speedboat катер|' +
      '🛳️ ship корабль|' +
      '🚂 locomotive поезд паровоз|' +
      '🚆 train электричка|' +
      '🚇 metro метро|' +
      '🗺️ map карта|' +
      '🧭 compass компас|' +
      '🏔️ mountain гора|' +
      '🌋 volcano вулкан|' +
      '🏕️ camping кемпинг палатка|' +
      '🏖️ beach пляж|' +
      '🏝️ island остров|' +
      '🏠 house дом|' +
      '🏡 house_garden дом сад|' +
      '🏢 office офис здание|' +
      '🏥 hospital больница|' +
      '🏦 bank банк|' +
      '🏨 hotel отель|' +
      '🏫 school школа|' +
      '🗼 tower башня|' +
      '🏰 castle замок|' +
      '🌃 night_city ночь город|' +
      '🌉 bridge мост|' +
      '🎡 ferris_wheel колесо обозрения'
    },
    { id: 'objects', name: 'Объекты', icon: '💻', data:
      '💻 laptop ноутбук компьютер computer|' +
      '🖥️ desktop монитор компьютер|' +
      '⌨️ keyboard клавиатура|' +
      '🖱️ computer_mouse мышь|' +
      '🖨️ printer принтер|' +
      '💾 floppy дискета сохранить save|' +
      '💿 cd диск|' +
      '🔌 plug розетка питание|' +
      '🔋 battery батарея заряд|' +
      '📱 mobile телефон смартфон|' +
      '☎️ telephone телефон|' +
      '📞 receiver трубка звонок|' +
      '📷 camera фото камера|' +
      '📹 video_camera видеокамера|' +
      '🎥 movie_camera камера кино|' +
      '📺 tv телевизор|' +
      '📻 radio радио|' +
      '🎙️ studio_mic микрофон подкаст|' +
      '⏱️ stopwatch секундомер время|' +
      '⏰ alarm_clock будильник время|' +
      '⌛ hourglass песочные часы ожидание|' +
      '🔦 flashlight фонарь|' +
      '💡 bulb идея лампочка idea|' +
      '🕯️ candle свеча|' +
      '🧯 extinguisher огнетушитель|' +
      '💸 money_wings деньги улетели расход|' +
      '💵 dollar доллар деньги|' +
      '💰 moneybag мешок денег|' +
      '💳 credit_card карта оплата|' +
      '🧾 receipt чек счёт|' +
      '⚖️ balance весы баланс|' +
      '🔧 wrench ключ инструмент фикс fix|' +
      '🔨 hammer молоток|' +
      '🛠️ tools инструменты настройка|' +
      '⚙️ gear шестерня настройки config|' +
      '🧰 toolbox ящик инструментов|' +
      '🧲 magnet магнит|' +
      '🔩 nut_bolt болт гайка|' +
      '⛓️ chains цепи|' +
      '🔒 lock замок закрыто secure|' +
      '🔓 unlock открыто замок|' +
      '🔑 key ключ|' +
      '🚪 door дверь|' +
      '🛏️ bed кровать|' +
      '🚿 shower душ|' +
      '🧹 broom метла уборка cleanup|' +
      '🧼 soap мыло|' +
      '🪣 bucket ведро|' +
      '📦 package пакет коробка сборка build|' +
      '📫 mailbox почта ящик|' +
      '📧 email почта письмо|' +
      '📤 outbox исходящие|' +
      '📥 inbox входящие|' +
      '📜 scroll свиток документ|' +
      '📄 page документ файл file|' +
      '📊 bar_chart график диаграмма статистика|' +
      '📈 chart_up рост график вверх|' +
      '📉 chart_down падение график вниз|' +
      '📋 clipboard буфер планшет|' +
      '📌 pushpin кнопка закрепить|' +
      '📍 round_pushpin метка место|' +
      '📎 paperclip скрепка вложение|' +
      '📏 ruler линейка|' +
      '✂️ scissors ножницы вырезать|' +
      '🗄️ file_cabinet шкаф архив|' +
      '🗑️ wastebasket корзина удалить delete|' +
      '📁 folder папка каталог|' +
      '📂 open_folder папка открыта|' +
      '📅 calendar календарь дата|' +
      '📓 notebook блокнот тетрадь|' +
      '📕 closed_book книга|' +
      '📖 open_book книга чтение документация docs|' +
      '📚 books книги библиотека|' +
      '🔖 bookmark закладка|' +
      '🏷️ label ярлык тег tag|' +
      '🔍 mag лупа поиск search|' +
      '🔬 microscope микроскоп исследование|' +
      '🔭 telescope телескоп|' +
      '📡 satellite антенна спутник связь|' +
      '💊 pill таблетка|' +
      '🩺 stethoscope стетоскоп|' +
      '🧪 test_tube пробирка тест test|' +
      '🧬 dna днк|' +
      '🩹 bandage пластырь патч patch|' +
      '✏️ pencil карандаш|' +
      '🖊️ pen ручка|' +
      '🖌️ paintbrush кисть|' +
      '📝 memo заметка написать note'
    },
    { id: 'symbols', name: 'Символы', icon: '❤️', data:
      '❤️ heart сердце любовь красное|' +
      '🧡 orange_heart сердце оранжевое|' +
      '💛 yellow_heart сердце жёлтое|' +
      '💚 green_heart сердце зелёное|' +
      '💙 blue_heart сердце синее|' +
      '💜 purple_heart сердце фиолетовое|' +
      '🖤 black_heart сердце чёрное|' +
      '🤍 white_heart сердце белое|' +
      '💔 broken_heart разбитое сердце|' +
      '💕 two_hearts сердечки|' +
      '💖 sparkling_heart сердце блеск|' +
      '💘 heart_arrow стрела амур|' +
      '💝 heart_ribbon подарок сердце|' +
      '💯 hundred сто отлично точно|' +
      '💢 anger злость|' +
      '💥 boom взрыв бум|' +
      '💫 dizzy_star звёзды|' +
      '💦 sweat_drops капли|' +
      '💨 dash дым скорость|' +
      '✅ white_check_mark готово выполнено ок done|' +
      '☑️ ballot_check галочка чек|' +
      '✔️ check_mark галочка|' +
      '❌ x ошибка нет крест error fail|' +
      '⭕ o круг|' +
      '🚫 no_entry_sign запрет нельзя|' +
      '⛔ no_entry стоп запрет|' +
      '❗ exclamation восклицательный важно|' +
      '❓ question вопрос|' +
      '‼️ bangbang двойной восклицательный|' +
      '⚠️ warning внимание предупреждение осторожно|' +
      '☢️ radioactive радиация|' +
      '♻️ recycle переработка рефакторинг|' +
      '🔄 arrows_counterclockwise обновить синхронизация refresh|' +
      '▶️ play плей воспроизвести|' +
      '⏸️ pause пауза|' +
      '⏹️ stop стоп|' +
      '⏭️ next следующий|' +
      '⏮️ previous предыдущий|' +
      '⏩ fast_forward перемотка|' +
      '🔀 shuffle перемешать|' +
      '🔁 repeat повтор цикл|' +
      '⬆️ arrow_up вверх|' +
      '⬇️ arrow_down вниз|' +
      '⬅️ arrow_left влево|' +
      '➡️ arrow_right вправо|' +
      '↩️ arrow_back назад откат|' +
      '🔝 top наверх|' +
      '🆕 new новое|' +
      '🆗 ok_button окей|' +
      '🆘 sos помощь|' +
      '🔔 bell колокольчик уведомление|' +
      '🔕 no_bell без звука|' +
      '🔊 loud_sound звук громко|' +
      '🔇 mute без звука|' +
      '📢 loudspeaker объявление|' +
      '💬 speech_balloon сообщение чат комментарий|' +
      '💭 thought_balloon мысль|' +
      '♾️ infinity бесконечность|' +
      '⚛️ atom атом|' +
      '©️ copyright копирайт|' +
      '™️ tm торговая марка|' +
      '🔢 numbers цифры номера|' +
      '🔤 abc алфавит буквы'
    },
    { id: 'dev', name: 'Код', icon: '🚀', data:
      '🚀 deploy деплой релиз запуск|' +
      '🐛 bug баг ошибка дефект|' +
      '✅ done готово выполнено|' +
      '❌ fail провал ошибка|' +
      '⚠️ warn предупреждение|' +
      '🔧 fix фикс починка|' +
      '🔨 build сборка|' +
      '📦 release пакет релиз|' +
      '🧪 tests тесты|' +
      '🔍 review ревью поиск|' +
      '💡 idea идея|' +
      '📝 docs документация заметка|' +
      '🔥 hotfix горит срочно|' +
      '🎯 goal цель задача|' +
      '⏱️ perf производительность|' +
      '🧹 cleanup рефакторинг уборка|' +
      '🩹 hotpatch патч заплатка|' +
      '📈 metrics метрики рост|' +
      '💻 code код разработка|' +
      '⚙️ config конфиг настройки|' +
      '🔒 security безопасность|' +
      '🗑️ remove удалить|' +
      '🚧 wip в работе стройка|' +
      '🤖 automation бот автоматизация|' +
      '🎉 merged влито готово'
    }
  ];

  var categories = [];
  var byCode = {};
  var all = [];

  for (var i = 0; i < RAW.length; i++) {
    var raw = RAW[i];
    var parts = raw.data.split('|');
    var items = [];
    for (var j = 0; j < parts.length; j++) {
      var fields = parts[j].split(/\s+/).filter(function (s) { return s.length > 0; });
      if (fields.length < 2) continue;
      var kw = fields.slice(1).join(' ').toLowerCase();
      var item = {
        ch: fields[0],
        code: fields[1],
        // Строка для поиска: shortcode + все ключевые слова, нижним
        // регистром, чтобы search() не приводил регистр на каждый вызов.
        kw: kw,
        words: kw.split(' '),
      };
      items.push(item);
      all.push(item);
      // Первый встреченный shortcode выигрывает — категория `dev`
      // идёт последней и не перетирает канонические коды.
      if (!byCode[item.code]) byCode[item.code] = item.ch;
    }
    categories.push({ id: raw.id, name: raw.name, icon: raw.icon, items: items });
  }

  window.__claudeEmojiCatalog = {
    categories: categories,
    all: all,
    byCode: byCode,
    /**
     * Поиск по подстроке в shortcode и ключевых словах. Результат
     * ранжируется четырьмя корзинами, иначе запрос «баг» выдаёт
     * 🥖 (ба-гет) раньше 🐛:
     *   1. точное совпадение shortcode;
     *   2. точное совпадение любого ключевого слова;
     *   3. совпадение с начала слова;
     *   4. совпадение в середине слова.
     */
    search: function (query) {
      var q = String(query || '').trim().toLowerCase();
      if (!q) return [];
      var byShortcode = [];
      var byWord = [];
      var byPrefix = [];
      var rest = [];
      for (var k = 0; k < all.length; k++) {
        var it = all[k];
        if (it.code === q) { byShortcode.push(it); continue; }
        var pos = it.kw.indexOf(q);
        if (pos < 0) continue;
        if (it.words.indexOf(q) >= 0) byWord.push(it);
        else if (pos === 0 || it.kw.charAt(pos - 1) === ' ') byPrefix.push(it);
        else rest.push(it);
      }
      // Категория `dev` намеренно дублирует символы из других разделов
      // (🔧 wrench и 🔧 fix), поэтому в выдаче схлопываем по символу.
      var seen = {};
      return byShortcode.concat(byWord, byPrefix, rest).filter(function (it) {
        if (seen[it.ch]) return false;
        seen[it.ch] = true;
        return true;
      });
    },
  };
})();

/* ============================================================
 * EMOJI PICKER — кнопка 😀 в футере поля ввода чата
 * ============================================================
 *
 * Добавляет кнопку 😀 слева от кнопки микрофона, в правом верхнем
 * углу поля ввода (`.micButtonWrapper_*`); если диктовка отключена
 * и микрофона нет — в футер `.inputFooter_*` рядом с меню `/`.
 * Расположение выбирается в VSCode Settings UI пунктом
 * `claudeCode.emojiButtonPlacement` (mic | footer): значение приходит
 * в конфиг bootstrap, а смена подхватывается на лету опросом
 * http-server.py, без Reload Window.
 * По клику открывается панель: строка поиска, табы категорий,
 * сетка смайликов и блок «Недавние» (localStorage). Выбранный
 * смайлик вставляется в позицию каретки composer'а; сообщение при
 * этом НЕ отправляется — панель остаётся открытой, можно вставить
 * несколько подряд.
 *
 * Composer в Claude Code 2.x — `div[contenteditable="plaintext-only"]
 * [role="textbox"]`, а не textarea, и он React-controlled. Поэтому:
 *   - вставка идёт через document.execCommand('insertText'), который
 *     эмитит native input-event с корректным inputType (тот же приём,
 *     что в triggerSlashCommandViaInput);
 *   - на mousedown внутри панели вызывается preventDefault, чтобы
 *     фокус (и выделение) не уходили из composer'а;
 *   - позиция каретки дополнительно запоминается через
 *     selectionchange — на случай, когда фокус всё же ушёл
 *     (например, в поле поиска панели).
 *
 * React перерисовывает поле ввода, поэтому кнопка переустанавливается
 * по мутациям DOM и периодическим обходом — оба даёт общий
 * наблюдатель (см. DOM WATCH), у модуля своего больше нет.
 *
 * Управление: `emojiPicker` и `emojiRecentLimit` в
 * .claude/patches/claude-custom-config.toml, расположение кнопки —
 * `claudeCode.emojiButtonPlacement` в настройках VSCode.
 * ============================================================ */
(function () {
  if (window.__claudeEmojiPickerInstalled) return;
  window.__claudeEmojiPickerInstalled = true;

  var cfg = window.__CLAUDE_CUSTOM_CONFIG__ || {};
  if (cfg.emojiPicker !== true) return;

  var catalog = window.__claudeEmojiCatalog;
  if (!catalog) return;

  var RECENT_LIMIT =
    typeof cfg.emojiRecentLimit === 'number' && cfg.emojiRecentLimit >= 0
      ? cfg.emojiRecentLimit
      : 24;
  var RECENT_KEY = 'claudeCustomEmojiRecent';
  var BTN_CLASS = 'claude-emoji-btn';
  var PANEL_ID = 'claude-emoji-panel';

  // Расположение кнопки: 'mic' | 'footer'. Значение приходит из
  // VSCode Settings UI (claudeCode.emojiButtonPlacement) — хук
  // patch-claude-webview.py кладёт его в конфиг bootstrap. Но
  // bootstrap перечитывается только при Reload Window, поэтому
  // текущее значение дополнительно опрашивается у http-server.py:
  // так смена настройки применяется почти сразу, как и правки CSS.
  var PLACEMENT_URL = 'http://localhost:18923/vscode-settings';
  var PLACEMENT_POLL_MS = 5000;
  var placement = cfg.emojiButtonPlacement === 'footer' ? 'footer' : 'mic';

  var panel = null;          // корневой элемент панели (или null)
  var anchorBtn = null;      // кнопка 😀, относительно которой открыта панель
  var searchInput = null;
  var gridEl = null;
  var previewEl = null;
  var tabsEl = null;
  var activeCategory = null; // id категории или '__recent__'
  var activeIndex = -1;      // индекс подсвеченной ячейки в сетке
  var renderedItems = [];    // элементы, показанные в сетке сейчас
  var savedRange = null;     // последняя позиция каретки в composer'е

  function logInfo() {
    if (!cfg.logs) return;
    try {
      console.log.apply(console, ['[emoji-picker]'].concat([].slice.call(arguments)));
    } catch (e) {}
  }

  /* ---------- composer и каретка ---------- */

  function getComposer() {
    return (
      document.querySelector('[role="textbox"][contenteditable][aria-label*="essage" i]') ||
      document.querySelector('[role="textbox"][contenteditable]') ||
      document.querySelector('div[contenteditable]')
    );
  }

  /** Запоминает каретку, пока она находится внутри composer'а. */
  function rememberCaret() {
    var composer = getComposer();
    if (!composer) return;
    var sel = window.getSelection();
    if (!sel || sel.rangeCount === 0) return;
    var range = sel.getRangeAt(0);
    if (!composer.contains(range.startContainer)) return;
    savedRange = range.cloneRange();
  }

  /** Ставит каретку в composer: сохранённая позиция или конец текста. */
  function restoreCaret(composer) {
    var sel = window.getSelection();
    if (!sel) return;
    if (savedRange && composer.contains(savedRange.startContainer)) {
      sel.removeAllRanges();
      sel.addRange(savedRange);
      return;
    }
    if (sel.rangeCount > 0 && composer.contains(sel.getRangeAt(0).startContainer)) {
      return; // каретка уже в composer'е — не двигаем
    }
    var range = document.createRange();
    range.selectNodeContents(composer);
    range.collapse(false); // в конец
    sel.removeAllRanges();
    sel.addRange(range);
  }

  /**
   * Вставляет символ в позицию каретки composer'а. Возвращает true,
   * если вставка удалась.
   */
  function insertEmoji(ch) {
    var composer = getComposer();
    if (!composer) {
      logInfo('composer не найден, вставка отменена');
      return false;
    }
    var searchWasFocused = !!searchInput && document.activeElement === searchInput;
    try {
      composer.focus();
      restoreCaret(composer);
      var inserted = document.execCommand('insertText', false, ch);
      if (!inserted) {
        // Fallback: ручная вставка текстового узла + синтетический
        // InputEvent, чтобы React-обработчик onInput увидел изменение.
        var sel = window.getSelection();
        if (sel && sel.rangeCount > 0) {
          var range = sel.getRangeAt(0);
          range.deleteContents();
          var node = document.createTextNode(ch);
          range.insertNode(node);
          range.setStartAfter(node);
          range.collapse(true);
          sel.removeAllRanges();
          sel.addRange(range);
        } else {
          composer.textContent = (composer.textContent || '') + ch;
        }
        composer.dispatchEvent(new InputEvent('input', {
          bubbles: true, cancelable: true, inputType: 'insertText', data: ch,
        }));
      }
      rememberCaret();
      pushRecent(ch);
      if (searchWasFocused) {
        // Клик мышью по ячейке при активном поиске: фокус возвращаем
        // в поле, чтобы можно было продолжить набирать запрос.
        searchInput.focus();
      }
      return true;
    } catch (e) {
      logInfo('вставка не удалась:', (e && e.message) || e);
      return false;
    }
  }

  /* ---------- недавние ---------- */

  function loadRecent() {
    if (RECENT_LIMIT === 0) return [];
    try {
      var raw = window.localStorage.getItem(RECENT_KEY);
      if (!raw) return [];
      var arr = JSON.parse(raw);
      if (!Array.isArray(arr)) return [];
      return arr.filter(function (s) { return typeof s === 'string' && s; })
        .slice(0, RECENT_LIMIT);
    } catch (e) {
      return [];
    }
  }

  function pushRecent(ch) {
    if (RECENT_LIMIT === 0) return;
    var recent = loadRecent().filter(function (s) { return s !== ch; });
    recent.unshift(ch);
    recent = recent.slice(0, RECENT_LIMIT);
    try {
      window.localStorage.setItem(RECENT_KEY, JSON.stringify(recent));
    } catch (e) {}
    // Если сейчас открыт таб «Недавние» — не перерисовываем сетку
    // прямо под курсором, иначе ячейки прыгают при серии вставок.
  }

  /** Находит описание смайлика по символу (для tooltip'а «недавних»). */
  function findByChar(ch) {
    for (var i = 0; i < catalog.all.length; i++) {
      if (catalog.all[i].ch === ch) return catalog.all[i];
    }
    return { ch: ch, code: '', kw: '' };
  }

  /* ---------- панель ---------- */

  function itemTitle(item) {
    return item.code ? ':' + item.code + ':  ' + item.kw : item.ch;
  }

  function renderGrid(items) {
    renderedItems = items;
    activeIndex = -1;
    gridEl.textContent = '';
    if (!items.length) {
      var empty = document.createElement('div');
      empty.className = 'claude-emoji-empty';
      empty.textContent = 'Ничего не найдено';
      gridEl.appendChild(empty);
      setPreview(null);
      return;
    }
    for (var i = 0; i < items.length; i++) {
      var item = items[i];
      var cell = document.createElement('button');
      cell.type = 'button';
      cell.className = 'claude-emoji-cell';
      cell.textContent = item.ch;
      cell.title = itemTitle(item);
      cell.setAttribute('data-index', String(i));
      gridEl.appendChild(cell);
    }
    setPreview(items[0]);
  }

  function setPreview(item) {
    if (!previewEl) return;
    if (!item) {
      previewEl.textContent = '';
      return;
    }
    previewEl.textContent = '';
    var ch = document.createElement('span');
    ch.className = 'claude-emoji-preview-ch';
    ch.textContent = item.ch;
    var name = document.createElement('span');
    name.className = 'claude-emoji-preview-name';
    name.textContent = item.code ? ':' + item.code + ':' : '';
    previewEl.appendChild(ch);
    previewEl.appendChild(name);
  }

  function showCategory(id) {
    activeCategory = id;
    var tabs = tabsEl.querySelectorAll('.claude-emoji-tab');
    for (var i = 0; i < tabs.length; i++) {
      var isActive = tabs[i].getAttribute('data-cat') === id;
      tabs[i].classList.toggle('claude-emoji-tab-active', isActive);
    }
    if (id === '__recent__') {
      renderGrid(loadRecent().map(findByChar));
      return;
    }
    for (var j = 0; j < catalog.categories.length; j++) {
      if (catalog.categories[j].id === id) {
        renderGrid(catalog.categories[j].items);
        return;
      }
    }
  }

  function setActiveIndex(index) {
    var cells = gridEl.querySelectorAll('.claude-emoji-cell');
    if (!cells.length) return;
    if (index < 0) index = 0;
    if (index > cells.length - 1) index = cells.length - 1;
    for (var i = 0; i < cells.length; i++) {
      cells[i].classList.toggle('claude-emoji-cell-active', i === index);
    }
    activeIndex = index;
    cells[index].scrollIntoView({ block: 'nearest' });
    setPreview(renderedItems[index]);
  }

  /** Сколько ячеек помещается в строку — для навигации стрелками.
   * Считаем по фактическому переносу (offsetTop), а не делением
   * ширин: с учётом gap деление даёт погрешность в одну колонку. */
  function columnCount() {
    var cells = gridEl.querySelectorAll('.claude-emoji-cell');
    if (!cells.length) return 1;
    var firstTop = cells[0].offsetTop;
    var count = 0;
    for (var i = 0; i < cells.length; i++) {
      if (cells[i].offsetTop !== firstTop) break;
      count++;
    }
    return Math.max(1, count);
  }

  function buildPanel() {
    var root = document.createElement('div');
    root.id = PANEL_ID;
    root.className = 'claude-emoji-panel';

    var searchRow = document.createElement('div');
    searchRow.className = 'claude-emoji-search-row';
    searchInput = document.createElement('input');
    searchInput.type = 'text';
    searchInput.className = 'claude-emoji-search';
    searchInput.placeholder = 'Поиск: улыбка, rocket, баг…';
    searchInput.setAttribute('aria-label', 'Поиск смайлика');
    searchRow.appendChild(searchInput);
    root.appendChild(searchRow);

    tabsEl = document.createElement('div');
    tabsEl.className = 'claude-emoji-tabs';
    var tabDefs = [];
    if (RECENT_LIMIT > 0) {
      tabDefs.push({ id: '__recent__', icon: '🕘', name: 'Недавние' });
    }
    for (var i = 0; i < catalog.categories.length; i++) {
      tabDefs.push({
        id: catalog.categories[i].id,
        icon: catalog.categories[i].icon,
        name: catalog.categories[i].name,
      });
    }
    for (var j = 0; j < tabDefs.length; j++) {
      var tab = document.createElement('button');
      tab.type = 'button';
      tab.className = 'claude-emoji-tab';
      tab.textContent = tabDefs[j].icon;
      tab.title = tabDefs[j].name;
      tab.setAttribute('data-cat', tabDefs[j].id);
      tabsEl.appendChild(tab);
    }
    root.appendChild(tabsEl);

    gridEl = document.createElement('div');
    gridEl.className = 'claude-emoji-grid';
    root.appendChild(gridEl);

    previewEl = document.createElement('div');
    previewEl.className = 'claude-emoji-preview';
    root.appendChild(previewEl);

    // Фокус не должен уходить из composer'а при кликах по панели —
    // иначе теряется выделение и вставка уедет не туда. Исключение —
    // поле поиска, которому фокус как раз нужен.
    root.addEventListener('mousedown', function (e) {
      if (e.target === searchInput) return;
      e.preventDefault();
    });

    root.addEventListener('click', function (e) {
      var tabBtn = e.target.closest && e.target.closest('.claude-emoji-tab');
      if (tabBtn) {
        searchInput.value = '';
        showCategory(tabBtn.getAttribute('data-cat'));
        return;
      }
      var cell = e.target.closest && e.target.closest('.claude-emoji-cell');
      if (cell) {
        var idx = parseInt(cell.getAttribute('data-index'), 10);
        var item = renderedItems[idx];
        if (item) insertEmoji(item.ch);
      }
    });

    root.addEventListener('mouseover', function (e) {
      var cell = e.target.closest && e.target.closest('.claude-emoji-cell');
      if (!cell) return;
      var idx = parseInt(cell.getAttribute('data-index'), 10);
      if (renderedItems[idx]) setPreview(renderedItems[idx]);
    });

    searchInput.addEventListener('input', function () {
      var q = searchInput.value.trim();
      if (!q) {
        showCategory(activeCategory || tabDefs[0].id);
        return;
      }
      var tabs = tabsEl.querySelectorAll('.claude-emoji-tab');
      for (var k = 0; k < tabs.length; k++) {
        tabs[k].classList.remove('claude-emoji-tab-active');
      }
      renderGrid(catalog.search(q));
    });

    root.addEventListener('keydown', onPanelKeydown);
    document.body.appendChild(root);
    return root;
  }

  function onPanelKeydown(e) {
    if (e.key === 'Escape') {
      e.preventDefault();
      e.stopPropagation();
      closePanel(true);
      return;
    }
    var cols = columnCount();
    if (e.key === 'ArrowRight') {
      e.preventDefault();
      setActiveIndex(activeIndex < 0 ? 0 : activeIndex + 1);
    } else if (e.key === 'ArrowLeft') {
      e.preventDefault();
      setActiveIndex(activeIndex <= 0 ? 0 : activeIndex - 1);
    } else if (e.key === 'ArrowDown') {
      e.preventDefault();
      setActiveIndex(activeIndex < 0 ? 0 : activeIndex + cols);
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      setActiveIndex(activeIndex < 0 ? 0 : activeIndex - cols);
    } else if (e.key === 'Enter') {
      e.preventDefault();
      var item = renderedItems[activeIndex < 0 ? 0 : activeIndex];
      if (item) {
        insertEmoji(item.ch);
        // Enter вставляет, но фокус оставляем в поиске — так можно
        // набрать следующий запрос без мыши.
        if (searchInput) searchInput.focus();
      }
    }
  }

  function positionPanel() {
    if (!panel || !anchorBtn) return;
    var rect = anchorBtn.getBoundingClientRect();
    var pRect = panel.getBoundingClientRect();
    var top = rect.top - pRect.height - 6;
    if (top < 6) top = Math.min(rect.bottom + 6, window.innerHeight - pRect.height - 6);
    if (top < 6) top = 6;
    var left = rect.left + rect.width / 2 - pRect.width / 2;
    if (left + pRect.width > window.innerWidth - 6) {
      left = window.innerWidth - pRect.width - 6;
    }
    if (left < 6) left = 6;
    panel.style.top = top + 'px';
    panel.style.left = left + 'px';
  }

  function openPanel(btn) {
    rememberCaret();
    anchorBtn = btn;
    panel = buildPanel();
    var firstTab = RECENT_LIMIT > 0 && loadRecent().length
      ? '__recent__'
      : catalog.categories[0].id;
    showCategory(firstTab);
    positionPanel();
    searchInput.focus();
    setTimeout(function () {
      document.addEventListener('mousedown', onOutsideMouseDown, true);
      document.addEventListener('keydown', onDocumentKeydown, true);
      window.addEventListener('resize', positionPanel);
    }, 0);
    logInfo('панель открыта');
  }

  function closePanel(focusComposer) {
    if (!panel) return;
    panel.remove();
    panel = null;
    anchorBtn = null;
    searchInput = null;
    gridEl = null;
    previewEl = null;
    tabsEl = null;
    renderedItems = [];
    activeIndex = -1;
    document.removeEventListener('mousedown', onOutsideMouseDown, true);
    document.removeEventListener('keydown', onDocumentKeydown, true);
    window.removeEventListener('resize', positionPanel);
    if (focusComposer) {
      var composer = getComposer();
      if (composer) {
        composer.focus();
        restoreCaret(composer);
      }
    }
    logInfo('панель закрыта');
  }

  function onOutsideMouseDown(e) {
    if (!panel) return;
    if (panel.contains(e.target)) return;
    if (e.target.closest && e.target.closest('.' + BTN_CLASS)) return; // toggle сам себя
    closePanel(false);
  }

  function onDocumentKeydown(e) {
    if (e.key === 'Escape' && panel) {
      e.preventDefault();
      e.stopPropagation();
      closePanel(true);
    }
  }

  /* ---------- кнопка рядом с микрофоном ----------
   *
   * Основное место — `[class*="micButtonWrapper_"]`: absolute-контейнер
   * в правом верхнем углу поля ввода (top:5px; right:0), где живёт
   * кнопка диктовки. Кнопка 😀 встаёт слева от микрофона, для чего
   * wrapper переводится в flex классом `claude-emoji-mic-host`
   * (по умолчанию элементы внутри блочные и встали бы друг под друга).
   *
   * Микрофон рендерится только при `speechToTextEnabled`, поэтому
   * остаётся запасное место — футер `[class*="inputFooter_"]` рядом
   * с кнопкой меню `/`.
   */

  function createButton(host) {
    var btn = document.createElement('button');
    btn.type = 'button'; // важно: поле ввода внутри <form>, submit нам не нужен
    btn.className = BTN_CLASS;
    btn.title = 'Вставить смайлик';
    btn.setAttribute('aria-label', 'Вставить смайлик');
    btn.textContent = '😀';
    // Наследуем нативные классы соседней кнопки — так наша получает те
    // же размеры, радиус и hover, что и штатные, без привязки к хэшу
    // в имени класса.
    var sibling = host.querySelector('button[class*="micButton_"]') ||
      host.querySelector('button[class*="menuButton_"]');
    if (sibling && sibling.className) {
      // У микрофона класс состояния `recording_*` появляется во время
      // записи — забирать его себе нельзя.
      var inherited = sibling.className.split(/\s+/).filter(function (c) {
        return c && c.indexOf('recording') !== 0;
      });
      inherited.push(BTN_CLASS);
      btn.className = inherited.join(' ');
    }
    btn.addEventListener('mousedown', function (e) {
      // Не отдаём фокус кнопке: каретка должна остаться в composer'е.
      e.preventDefault();
      e.stopPropagation();
    });
    btn.addEventListener('click', function (e) {
      e.preventDefault();
      e.stopPropagation();
      if (panel) closePanel(true);
      else openPanel(btn);
    });
    return btn;
  }

  /** Вставка в контейнер микрофона. Возвращает true, если удалось. */
  function mountNearMic(container) {
    var wrapper = container.querySelector('[class*="micButtonWrapper_"]');
    if (!wrapper) return false;
    var micBtn = wrapper.querySelector('button[class*="micButton_"]');
    if (!micBtn) return false;
    var btn = createButton(wrapper);
    wrapper.classList.add('claude-emoji-mic-host');
    wrapper.insertBefore(btn, micBtn);
    // Полю ввода нужен правый отступ под вторую иконку, иначе текст
    // уезжает под неё. Ставим маркер на контейнер, а не правим
    // `messageInput_*` глобально: без нашей кнопки отступ не нужен.
    var inputWrap = micBtn.closest('[class*="messageInputContainer_"]') || wrapper.parentNode;
    if (inputWrap && inputWrap.classList) inputWrap.classList.add('claude-emoji-inline');
    logInfo('кнопка встроена рядом с микрофоном');
    return true;
  }

  /** Запасная вставка в футер, рядом с кнопкой меню `/`. */
  function mountInFooter(container) {
    var footer = container.querySelector('[class*="inputFooter_"]');
    if (!footer) return false;
    var btn = createButton(footer);
    var menuBtn = footer.querySelector('button[class*="menuButton_"]');
    if (menuBtn && menuBtn.parentNode === footer) {
      footer.insertBefore(btn, menuBtn.nextSibling);
    } else {
      var spacer = footer.querySelector('[class*="spacer_"]');
      if (spacer && spacer.parentNode === footer) footer.insertBefore(btn, spacer);
      else footer.appendChild(btn);
    }
    logInfo('кнопка встроена в футер (микрофон недоступен)');
    return true;
  }

  function scanInputs(ctx) {
    // Узлы даёт общий обход (см. DOM WATCH) — один на все модули.
    // Свой поиск остаётся для вызовов вне прохода: при регистрации
    // и из обработчиков самого модуля.
    var containers = (ctx && ctx.inputs)
      || document.querySelectorAll('[class*="inputContainer_"]');
    for (var i = 0; i < containers.length; i++) {
      var container = containers[i];
      // Футер и поле ввода лежат в одном fieldset.inputContainer_*,
      // поэтому одной проверки хватает на оба варианта размещения.
      if (container.querySelector('.' + BTN_CLASS)) continue;
      if (!container.querySelector('[role="textbox"][contenteditable]')) continue;
      // При 'mic' футер остаётся запасным вариантом: микрофона нет,
      // когда выключена диктовка (speechToTextEnabled).
      if (placement === 'footer') mountInFooter(container);
      else if (!mountNearMic(container)) mountInFooter(container);
    }
    // Панель без своей кнопки (React перерисовал поле ввода) —
    // закрываем, чтобы не висела оторванной от анкера.
    if (panel && anchorBtn && !document.body.contains(anchorBtn)) {
      closePanel(false);
    }
  }

  /** Снимает кнопку и следы её размещения — перед переносом. */
  function unmountButtons() {
    var buttons = document.querySelectorAll('.' + BTN_CLASS);
    for (var i = 0; i < buttons.length; i++) {
      var host = buttons[i].parentNode;
      buttons[i].remove();
      // Класс на контейнере микрофона нужен только пока кнопка в нём.
      if (host && host.classList) host.classList.remove('claude-emoji-mic-host');
    }
    var marked = document.querySelectorAll('.claude-emoji-inline');
    for (var j = 0; j < marked.length; j++) {
      marked[j].classList.remove('claude-emoji-inline');
    }
  }

  /**
   * Опрашивает http-server.py на предмет смены
   * claudeCode.emojiButtonPlacement в VSCode Settings UI.
   * Сервер может быть не запущен — тогда молча остаёмся на значении
   * из bootstrap.
   */
  function pollPlacement() {
    fetch(PLACEMENT_URL, { cache: 'no-store' })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (data) {
        if (!data) return;
        var next = data.emojiButtonPlacement === 'footer' ? 'footer' : 'mic';
        if (next === placement) return;
        logInfo('расположение кнопки сменилось:', placement, '→', next);
        placement = next;
        if (panel) closePanel(false);
        unmountButtons();
        scanInputs();
      })
      .catch(function () {});
  }

  function init() {
    // Наблюдатель и подстраховочный таймер — общие (см. DOM WATCH).
    window.__claudeDomWatch.register('emoji-picker', scanInputs);
    pollPlacement();
    setInterval(pollPlacement, PLACEMENT_POLL_MS);
    // Каретку запоминаем всегда, а не только при открытой панели:
    // к моменту клика по 😀 фокус уже может быть в другом месте.
    document.addEventListener('selectionchange', rememberCaret);
    logInfo('installed, смайликов в каталоге:', catalog.all.length);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();

/* ============================================================
 * EMOJI AUTOREPLACE — текстовые смайлики → эмодзи прямо при наборе
 * ============================================================
 *
 * Слушает input-события composer'а и, когда перед кареткой оказалась
 * распознаваемая последовательность, заменяет её на эмодзи:
 *
 *   :)  :-)  =)  ;)  :(  <3      → 🙂 🙂 🙂 😉 🙁 ❤️
 *   :D  :P  :o  xD  :3           → 😃 😛 😮 😆 😺
 *   :rocket:  :bug:  :done:      → 🚀 🐛 ✅
 *
 * Shortcode-форма `:name:` резолвится по индексу EMOJI CATALOG,
 * то есть покрывает все смайлики каталога.
 *
 * Два режима срабатывания — из-за конфликтов с обычным текстом:
 *
 * 1. МГНОВЕННЫЕ — паттерны, у которых не бывает продолжения
 *    (`:)`, `<3`, `:/`, `\o/`, а также `:name:`). Заменяются сразу
 *    по вводу последнего символа.
 *
 * 2. ОТЛОЖЕННЫЕ — паттерны, которые могут оказаться началом чего-то
 *    длиннее (`:D`, `:o`, `:3`, `xD`). Заменяются только когда следом
 *    введён пробел или перевод строки. Иначе `:o` схлопывался бы
 *    в 😮 прямо посреди набора `:ok_hand:`, а `xD` — посреди слова.
 *
 * Ложные срабатывания дополнительно отсекаются требованием границы
 * слева: перед последовательностью должен быть пробел, перевод строки
 * или начало текста. Поэтому `http://`, `C:/Users` и `10:00` остаются
 * как есть.
 *
 * Замена выполняется через execCommand('insertText') по выделенному
 * Range — так React видит штатный input-event, а пользователь может
 * откатить замену обычным Ctrl+Z.
 *
 * Известное ограничение: отложенный паттерн в самом конце сообщения
 * (`ок :D` + сразу Enter) уходит текстом — разделитель после него
 * ввести уже не успевают. Мгновенные паттерны этим не страдают.
 *
 * Управление: `emojiAutoReplace` в claude-custom-config.toml.
 * ============================================================ */
(function () {
  if (window.__claudeEmojiAutoReplaceInstalled) return;
  window.__claudeEmojiAutoReplaceInstalled = true;

  var cfg = window.__CLAUDE_CUSTOM_CONFIG__ || {};
  if (cfg.emojiAutoReplace !== true) return;

  // Мгновенные паттерны. Внутри таблицы порядок важен: более длинные
  // варианты проверяются первыми, иначе `:-)` схлопнется по хвосту
  // `-)`, а `</3` — по хвосту `<3`.
  // Голые `8)` и `B)` намеренно не включены — они превращали бы
  // нумерацию списка («8) сделать») в 😎; оставлены формы с дефисом.
  var INSTANT = [
    ['>:(', '😠'],
    ['</3', '💔'],
    [":'(", '😢'],
    ['\\o/', '🙌'],
    ['8-)', '😎'],
    ['B-)', '😎'],
    [':-)', '🙂'],
    [':-(', '🙁'],
    [':-D', '😃'],
    [':-P', '😛'],
    [':-p', '😛'],
    [':-O', '😮'],
    [':-o', '😮'],
    [':-|', '😐'],
    [':-/', '😕'],
    [';-)', '😉'],
    [':)', '🙂'],
    ['=)', '🙂'],
    [':(', '🙁'],
    [';)', '😉'],
    [':|', '😐'],
    [':/', '😕'],
    [':*', '😘'],
    ['<3', '❤️'],
  ];

  // Отложенные: `<двоеточие><буква/цифра>` — потенциальное начало
  // shortcode (`:o` в `:ok_hand:`), а `xD` — потенциальное начало
  // слова. Такие заменяются только после ввода пробела.
  var DEFERRED = [
    [':D', '😃'],
    [':P', '😛'],
    [':p', '😛'],
    [':O', '😮'],
    [':o', '😮'],
    [':3', '😺'],
    ['xD', '😆'],
    ['XD', '😆'],
  ];

  var SHORTCODE_RE = /:([a-z0-9_]{2,24}):$/i;
  var MAX_LOOKBEHIND = 32;
  var BOUNDARY_RE = /[\s\u00A0]/;

  var catalog = window.__claudeEmojiCatalog;
  var replacing = false; // защита от реакции на собственную вставку

  function logInfo() {
    if (!cfg.logs) return;
    try {
      console.log.apply(console, ['[emoji-autoreplace]'].concat([].slice.call(arguments)));
    } catch (e) {}
  }

  function getComposer() {
    return (
      document.querySelector('[role="textbox"][contenteditable][aria-label*="essage" i]') ||
      document.querySelector('[role="textbox"][contenteditable]') ||
      document.querySelector('div[contenteditable]')
    );
  }

  /**
   * Слева от найденной последовательности должен быть пробел или
   * начало текста — иначе `http:/` превратилось бы в `http😕`.
   */
  function hasBoundary(text, startIndex) {
    if (startIndex <= 0) return true;
    return BOUNDARY_RE.test(text.charAt(startIndex - 1));
  }

  function matchTable(table, text) {
    for (var k = 0; k < table.length; k++) {
      var pattern = table[k][0];
      if (text.length < pattern.length) continue;
      if (text.slice(text.length - pattern.length) !== pattern) continue;
      if (!hasBoundary(text, text.length - pattern.length)) continue;
      return { len: pattern.length, ch: table[k][1] };
    }
    return null;
  }

  /**
   * Ищет, что заменить в тексте перед кареткой.
   * Возвращает {len, text} — сколько символов снять и что вставить,
   * либо null.
   */
  function findReplacement(before) {
    var hit = matchTable(INSTANT, before);
    if (hit) return { len: hit.len, text: hit.ch };

    if (catalog) {
      var m = SHORTCODE_RE.exec(before);
      if (m && hasBoundary(before, m.index)) {
        var ch = catalog.byCode[m[1].toLowerCase()];
        if (ch) return { len: m[0].length, text: ch };
      }
    }

    // Отложенные срабатывают по только что введённому разделителю.
    // Снимаем паттерн вместе с разделителем и возвращаем его обратно
    // после эмодзи — тогда каретка остаётся в конце, а не перед пробелом.
    var last = before.charAt(before.length - 1);
    if (last && BOUNDARY_RE.test(last)) {
      var deferredHit = matchTable(DEFERRED, before.slice(0, before.length - 1));
      if (deferredHit) {
        return { len: deferredHit.len + 1, text: deferredHit.ch + last };
      }
    }
    return null;
  }

  /** Заменяет `len` символов перед кареткой на `text`. */
  function replaceBeforeCaret(node, offset, len, text) {
    var sel = window.getSelection();
    if (!sel) return;
    var range = document.createRange();
    try {
      range.setStart(node, offset - len);
      range.setEnd(node, offset);
    } catch (e) {
      return; // узел успел измениться под нами
    }
    sel.removeAllRanges();
    sel.addRange(range);

    replacing = true;
    try {
      if (!document.execCommand('insertText', false, text)) {
        // Fallback, если execCommand недоступен: правим данные узла
        // напрямую и сообщаем React о вводе синтетическим событием.
        node.deleteData(offset - len, len);
        node.insertData(offset - len, text);
        var after = document.createRange();
        after.setStart(node, offset - len + text.length);
        after.collapse(true);
        sel.removeAllRanges();
        sel.addRange(after);
        var composer = getComposer();
        if (composer) {
          composer.dispatchEvent(new InputEvent('input', {
            bubbles: true, cancelable: true, inputType: 'insertText', data: text,
          }));
        }
      }
      logInfo('заменено на', JSON.stringify(text));
    } catch (e) {
      logInfo('замена не удалась:', (e && e.message) || e);
    } finally {
      // Снимаем флаг после того, как отработают обработчики нашего
      // же input-события, иначе замена вызовет сама себя.
      setTimeout(function () { replacing = false; }, 0);
    }
  }

  function onInput(e) {
    if (replacing) return;
    var composer = getComposer();
    if (!composer || e.target !== composer) return;
    // Реагируем только на набор текста: удаления и вставки из буфера
    // (insertFromPaste) не трогаем — пользователь их не набирал.
    var type = e.inputType || '';
    if (type !== 'insertText' && type !== 'insertCompositionText' &&
        type !== 'insertLineBreak' && type !== 'insertParagraph' && type !== '') {
      return;
    }

    var sel = window.getSelection();
    if (!sel || sel.rangeCount === 0) return;
    var range = sel.getRangeAt(0);
    if (!range.collapsed) return;
    var node = range.startContainer;
    if (node.nodeType !== 3) return; // ждём текстовый узел
    if (!composer.contains(node)) return;

    var offset = range.startOffset;
    var before = node.data.slice(Math.max(0, offset - MAX_LOOKBEHIND), offset);
    var found = findReplacement(before);
    if (!found) return;
    replaceBeforeCaret(node, offset, found.len, found.text);
  }

  function init() {
    // Слушаем на document: composer пересоздаётся React'ом, вешать
    // обработчик на сам элемент пришлось бы после каждого ре-рендера.
    document.addEventListener('input', onInput, true);
    logInfo('installed | мгновенных:', INSTANT.length,
      '| отложенных:', DEFERRED.length,
      '| shortcodes:', catalog ? Object.keys(catalog.byCode).length : 0);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();

/* ============================================================
 * SETTINGS MENU FIX — чиним пункт меню `/` «Общие настройки…»
 * ============================================================
 *
 * Симптом: выбор пункта «Настройки → Общие настройки…» не открывает
 * настройки расширения, а отправляет в чат слэш-команду `/config`
 * (та печатает usage вида `key=value`). Оба пункта при этом
 * подсвечиваются в меню одновременно.
 *
 * Причина — коллизия идентификаторов в самом расширении (2.1.220).
 * Пункт меню регистрируется так:
 *
 *     k6 = { config: "slash-command-config", ... }
 *     registerAction({ id: k6.config, label: "General config…" },
 *                    "Settings", () => t.openConfig())
 *
 * а слэш-команды CLI — так:
 *
 *     let l = `slash-command-${invocation}`;      // /config → "slash-command-config"
 *     registerAction({ id: l, label: `/${invocation}` },
 *                    "Slash Commands", () => send(`/${invocation}`))
 *
 * Пока в CLI не было команды `/config`, конфликта не возникало.
 * Теперь она есть, id совпадают буквально, и обработчик пункта меню
 * перекрывается обработчиком слэш-команды.
 *
 * Что делает патч: перехватывает click в capture-фазе (тот же приём,
 * что в compactClickInterceptor), опознаёт пункт по лейблу и вызывает
 * `openConfig()` — метод контекста приложения, который достаётся
 * через React-fiber. Если контекст найти не удалось, событие
 * не блокируется: пусть отработает штатное (пусть и неверное)
 * поведение, это лучше мёртвой кнопки.
 *
 * Управление: `fixSettingsMenuItem` в claude-custom-config.toml.
 * ============================================================ */
(function () {
  if (window.__claudeSettingsMenuFixInstalled) return;
  window.__claudeSettingsMenuFixInstalled = true;

  var cfg = window.__CLAUDE_CUSTOM_CONFIG__ || {};
  if (cfg.fixSettingsMenuItem !== true) return;

  // Лейбл зависит от локали (localize.py переводит меню) и от того,
  // каким символом расширение набрало многоточие.
  var LABELS = [
    'общие настройки…',
    'общие настройки...',
    'general config…',
    'general config...',
  ];

  var cachedContext = null;

  function logInfo() {
    if (!cfg.logs) return;
    try {
      console.log.apply(console, ['[settings-menu-fix]'].concat([].slice.call(arguments)));
    } catch (e) {}
  }

  function getFiber(el) {
    var keys = Object.keys(el);
    for (var i = 0; i < keys.length; i++) {
      if (keys[i].indexOf('__reactFiber') === 0) return el[keys[i]];
    }
    return null;
  }

  /** Контекст приложения — объект с методами openConfig/openHelp. */
  function looksLikeContext(obj) {
    return !!obj && typeof obj === 'object' &&
      typeof obj.openConfig === 'function' &&
      typeof obj.openHelp === 'function';
  }

  /** Ищет контекст среди значений props (и в самом props). */
  function contextFromProps(props) {
    if (!props || typeof props !== 'object') return null;
    if (looksLikeContext(props)) return props;
    if (looksLikeContext(props.context)) return props.context;
    var keys = Object.keys(props);
    for (var i = 0; i < keys.length; i++) {
      if (looksLikeContext(props[keys[i]])) return props[keys[i]];
    }
    return null;
  }

  /** Ищет контекст в цепочке хуков компонента. */
  function contextFromState(fiber) {
    var state = fiber.memoizedState;
    var hops = 0;
    while (state && hops < 40) {
      if (looksLikeContext(state.memoizedState)) return state.memoizedState;
      // Signals/стейт часто лежат ещё на уровень глубже, в .value.
      if (state.memoizedState && looksLikeContext(state.memoizedState.value)) {
        return state.memoizedState.value;
      }
      state = state.next;
      hops++;
    }
    return null;
  }

  /**
   * Поднимается от кликнутого пункта вверх по дереву фиберов —
   * контекст почти наверняка прокинут в один из родительских
   * компонентов меню, так что до полного обхода дело не доходит.
   */
  function findContextUpwards(el) {
    var fiber = getFiber(el);
    var hops = 0;
    while (fiber && hops < 40) {
      var found = contextFromProps(fiber.memoizedProps) || contextFromState(fiber);
      if (found) return found;
      fiber = fiber.return;
      hops++;
    }
    return null;
  }

  /** Запасной путь: обход дерева от корня React. */
  function findContextFromRoot() {
    var root = document.getElementById('root');
    if (!root) return null;
    var keys = Object.keys(root);
    var containerKey = null;
    for (var i = 0; i < keys.length; i++) {
      if (keys[i].indexOf('__reactContainer') === 0) { containerKey = keys[i]; break; }
    }
    if (!containerKey) return null;

    var stack = [root[containerKey]];
    var seen = new Set();
    var visited = 0;
    while (stack.length && visited < 6000) {
      var fiber = stack.pop();
      if (!fiber || seen.has(fiber)) continue;
      seen.add(fiber);
      visited++;
      var found = contextFromProps(fiber.memoizedProps) || contextFromState(fiber);
      if (found) return found;
      if (fiber.child) stack.push(fiber.child);
      if (fiber.sibling) stack.push(fiber.sibling);
    }
    return null;
  }

  function getContext(el) {
    if (looksLikeContext(cachedContext)) return cachedContext;
    cachedContext = findContextUpwards(el) || findContextFromRoot();
    logInfo('контекст приложения', cachedContext ? 'найден' : 'НЕ найден');
    return cachedContext;
  }

  /** Закрывает выпадашку меню — React слушает Escape на document. */
  function closeMenu() {
    var escape = { key: 'Escape', code: 'Escape', keyCode: 27, which: 27,
      bubbles: true, cancelable: true };
    document.dispatchEvent(new KeyboardEvent('keydown', escape));
    var input = document.querySelector('[role="textbox"][contenteditable]');
    if (input) input.dispatchEvent(new KeyboardEvent('keydown', escape));
  }

  function isTargetItem(item) {
    var labelEl = item.querySelector('[class*="commandLabel_"]');
    var text = ((labelEl ? labelEl.textContent : item.textContent) || '')
      .trim().toLowerCase();
    if (!text) return false;
    // Слэш-команда `/config` живёт в том же меню и лейбл у неё
    // начинается со слэша — её трогать нельзя.
    if (text.charAt(0) === '/') return false;
    return LABELS.indexOf(text) >= 0;
  }

  function onClickCapture(e) {
    if (!e.target.closest) return;
    var item = e.target.closest('[class*="commandItem_"]');
    if (!item || !isTargetItem(item)) return;

    var context = getContext(item);
    if (!context) {
      // Чинить нечем — пропускаем событие дальше, чтобы поведение
      // осталось хотя бы прежним, а не исчезло совсем.
      logInfo('контекст не найден, отдаём событие React-обработчику');
      return;
    }

    e.preventDefault();
    e.stopPropagation();
    try {
      context.openConfig();
      logInfo('openConfig() вызван');
    } catch (err) {
      logInfo('openConfig() упал:', (err && err.message) || err);
    }
    closeMenu();
  }

  function init() {
    document.addEventListener('click', onClickCapture, true);
    logInfo('installed');
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();

/* ============================================================
 * CACHE USAGE BUTTON
 *
 * Кнопка «Usage» в футере поля ввода, слева от «Править
 * автоматически». По клику открывается панель со статистикой
 * prompt-кэша текущей сессии: размер контекста, вердикт последнего
 * хода (попадание/промах и пауза до него), суммы чтений и записей,
 * оценка стоимости против варианта без кэша и список последних
 * промахов с длиной паузы.
 *
 * Не путать с соседней кнопкой «Cache» (модуль CACHE KEEPALIVE):
 * эта показывает статистику, та поддерживает кэш живым.
 *
 * Данные берёт GET http://localhost:18923/cache-usage — там
 * http-server.py разбирает JSONL-транскрипт сессии (модуль
 * .claude/hooks/cache_usage.py). Ничего не считается в webview:
 * транскрипты бывают по 70 МБ, разбор инкрементальный и живёт
 * на стороне сервера.
 *
 * Почему кнопка, а не инжект в каждое сообщение: статистика нужна
 * изредка, а строка в шапке каждого ответа быстро превращается в шум.
 *
 * Управление: `usageButton` в claude-custom-config.toml.
 * ============================================================ */
(function () {
  if (window.__claudeCacheButtonInstalled) return;
  window.__claudeCacheButtonInstalled = true;

  var cfg = window.__CLAUDE_CUSTOM_CONFIG__ || {};

  var API_URL = 'http://localhost:18923/cache-usage';
  var BTN_CLASS = 'claude-usage-btn';
  var BARE_CLASS = 'claude-usage-btn-bare';  // без донора стиля
  var PANEL_ID = 'claude-cache-panel';

  // Соседний контрол, слева от которого встаём. Лейбл локализуется,
  // поэтому ищем оба варианта; при промахе есть запасные якоря.
  var AUTO_EDIT_RE = /Править автоматически|Edit automatically/i;

  var UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

  // Во сколько ставок input обходится промах сверх попадания:
  // заплачено по записи (2x) вместо чтения (0.1x).
  var MISS_LOSS_MULT = 1.9;

  var panel = null;
  var anchorBtn = null;

  function logInfo() {
    if (!cfg.logs) return;
    try {
      console.log.apply(console, ['[cache-usage]'].concat([].slice.call(arguments)));
    } catch (e) {}
  }

  /* ---------- какая сессия открыта в этой вкладке ---------- */

  /**
   * Без session id сервер отдаёт самый свежий .jsonl в папке проекта —
   * то есть все вкладки показывают одну и ту же сессию, и она ещё
   * и перескакивает, когда в соседней вкладке появляется новое
   * сообщение. Поэтому id берём из React-fiber и передаём явно.
   *
   * Идём вверх по `.return` от поля ввода: sessionId лежит в пропсах
   * или состоянии одного из родительских компонентов чата. Значение
   * бывает и голой строкой, и Preact-сигналом — в бандле поля вида
   * `session.sessionId.value` встречаются наравне с обычными.
   *
   * Существующий getSessionIdFromElement из SESSION MOVER здесь не
   * годится: он читает React-key элемента списка сессий, а у открытой
   * вкладки такого элемента нет.
   */
  function fiberOf(el) {
    if (!el) return null;
    var keys = Object.keys(el);
    for (var i = 0; i < keys.length; i++) {
      if (keys[i].indexOf('__reactFiber') === 0) return el[keys[i]];
    }
    return null;
  }

  // Имена пропсов, под которыми лежит объект сессии. В бандле это
  // экземпляр класса с полями-сигналами (`sessionId`, `summary`,
  // `busy`), а вкладки живут внутри одного webview — какая активна,
  // знает `activeSession`.
  var HOLDER_KEYS = ['session', 'activeSession', 'currentSession', 'conversation'];

  /** Достаёт UUID из объекта-держателя сессии (поле или сигнал). */
  function idFromHolder(h) {
    if (!h || typeof h !== 'object') return null;
    var v;
    try { v = h.sessionId; } catch (e) { return null; }
    if (typeof v === 'string' && UUID_RE.test(v)) return v;
    if (v && typeof v === 'object' && typeof v.value === 'string'
        && UUID_RE.test(v.value)) {
      return v.value;
    }
    return null;
  }

  /** Разворачивает сигнал: у Preact значение лежит в `.value`. */
  function unwrap(v) {
    if (v && typeof v === 'object' && v.value && typeof v.value === 'object') {
      return v.value;
    }
    return v;
  }

  /**
   * Ищет id в объекте пропсов/состояния.
   *
   * Плоской проверки `obj.sessionId` не хватает: в пропсы приходит
   * не id, а сам объект сессии (`props.session.sessionId.value`) или
   * стор с активной вкладкой (`props.activeSession.value.sessionId.value`).
   * Поэтому смотрим на уровень глубже — сперва по известным именам,
   * потом общим перебором, на случай другого имени пропса.
   */
  function pickSessionId(obj) {
    if (!obj || typeof obj !== 'object') return null;
    var direct = idFromHolder(obj);
    if (direct) return direct;

    var i, got;
    for (i = 0; i < HOLDER_KEYS.length; i++) {
      var h = obj[HOLDER_KEYS[i]];
      if (!h || typeof h !== 'object') continue;
      got = idFromHolder(h) || idFromHolder(unwrap(h));
      if (got) return got;
    }

    var keys;
    try { keys = Object.keys(obj); } catch (e) { return null; }
    for (i = 0; i < keys.length && i < 30; i++) {
      var c;
      try { c = obj[keys[i]]; } catch (e) { continue; }
      if (!c || typeof c !== 'object') continue;
      got = idFromHolder(c) || idFromHolder(unwrap(c));
      if (got) return got;
    }
    return null;
  }

  /**
   * Состояние функционального компонента — не объект, а связный список
   * хуков: значение каждого лежит в `.memoizedState`, следующий в
   * `.next`. Прямая проверка fiber.memoizedState видит только первый
   * хук (а чаще вообще служебную структуру), поэтому идём по цепочке.
   */
  function pickFromHooks(state) {
    var hook = state;
    var n = 0;
    while (hook && typeof hook === 'object' && n < 40) {
      var got = pickSessionId(hook.memoizedState);
      if (got) return got;
      hook = hook.next;
      n++;
    }
    return null;
  }

  function pickFromFiber(f) {
    if (!f) return null;
    return pickSessionId(f.memoizedProps)
      || pickSessionId(f.memoizedState)
      || pickFromHooks(f.memoizedState);
  }

  function findSessionId() {
    var anchor = document.querySelector('[class*="inputContainer_"]')
      || document.getElementById('root')
      || document.body;
    var fiber = fiberOf(anchor);
    var hops = 0;
    while (fiber && hops < 80) {
      var id = pickFromFiber(fiber);
      if (id) {
        logInfo('id найден подъёмом, шагов:', hops);
        return id;
      }
      fiber = fiber.return;
      hops++;
    }
    // Подъём не помог — обходим вниз от корня. Очередь ограничена:
    // панель открывается по клику, но подвесить webview обходом
    // на десятки тысяч узлов всё равно нельзя.
    var root = fiberOf(document.getElementById('root') || document.body);
    var queue = root ? [root] : [];
    var seen = 0;
    while (queue.length && seen < 12000) {
      var f = queue.shift();
      seen++;
      if (!f) continue;
      var found = pickFromFiber(f);
      if (found) {
        logInfo('id найден обходом вниз, узлов просмотрено:', seen);
        return found;
      }
      if (f.child) queue.push(f.child);
      if (f.sibling) queue.push(f.sibling);
    }
    logInfo('id не найден: подъём', hops, 'шагов, обход', seen, 'узлов');
    return null;
  }

  // Резолвер — общая зависимость: им же пользуются кнопки ByPass
  // и Cache, чтобы работать именно со своей вкладкой. Регистрируем ДО
  // проверки флага, иначе выключенный usageButton утащил бы за собой
  // и соседние модули (тот же приём, что с __claudeEmojiCatalog).
  window.__claudeSessionId = findSessionId;

  if (cfg.usageButton !== true) return;

  /* ---------- форматирование ---------- */

  function human(n) {
    if (typeof n !== 'number' || !isFinite(n)) return '—';
    if (n >= 1e9) return (n / 1e9).toFixed(1) + 'B';
    if (n >= 1e6) return (n / 1e6).toFixed(1) + 'M';
    if (n >= 1e3) return (n / 1e3).toFixed(1) + 'k';
    return String(Math.round(n));
  }

  function money(v) {
    return typeof v === 'number' ? '$' + v.toFixed(2) : '—';
  }

  function hhmm(iso) {
    if (typeof iso !== 'string' || iso.length < 16) return '—';
    // Транскрипт пишет UTC; показываем в локальной зоне пользователя.
    var d = new Date(iso);
    if (isNaN(d.getTime())) return iso.slice(11, 16);
    return ('0' + d.getHours()).slice(-2) + ':' + ('0' + d.getMinutes()).slice(-2);
  }

  function gapText(gap) {
    if (typeof gap !== 'number') return '';
    if (gap < 1) return Math.round(gap * 60) + ' с';
    if (gap < 90) return gap.toFixed(1) + ' мин';
    return (gap / 60).toFixed(1) + ' ч';
  }

  /* ---------- панель ---------- */

  function closePanel() {
    if (panel) panel.remove();
    panel = null;
    anchorBtn = null;
    document.removeEventListener('mousedown', onOutside, true);
    document.removeEventListener('keydown', onKeydown, true);
  }

  function onOutside(e) {
    if (!panel) return;
    if (panel.contains(e.target)) return;
    if (anchorBtn && anchorBtn.contains(e.target)) return;
    closePanel();
  }

  function onKeydown(e) {
    if (e.key === 'Escape') closePanel();
  }

  function row(label, value, cls, title) {
    var el = document.createElement('div');
    el.className = 'claude-cache-row' + (cls ? ' ' + cls : '');
    if (title) el.title = title;
    var l = document.createElement('span');
    l.className = 'claude-cache-label';
    l.textContent = label;
    var v = document.createElement('span');
    v.className = 'claude-cache-value';
    v.textContent = value;
    el.appendChild(l);
    el.appendChild(v);
    return el;
  }

  function renderError(body, text) {
    var p = document.createElement('div');
    p.className = 'claude-cache-empty';
    p.textContent = text;
    body.appendChild(p);
  }

  /** Короткое имя модели: claude-opus-5 → opus-5, glm-5.3 → glm-5.3. */
  function shortModel(name) {
    return String(name || '').replace(/^claude-/, '') || '—';
  }

  /**
   * История сессии: промахи вперемешку с переключениями модели
   * и аккаунта, по времени.
   *
   * Раньше здесь были «последние промахи», и они выглядели
   * беспричинными: «переписано 900k» без намёка, что минутой раньше
   * сменили аккаунт. Теперь причина стоит рядом со следствием.
   *
   * Ленту готовит сервер (`history`). Запасной путь по `miss_log`
   * нужен, пока в окне работает webview, загруженный до обновления
   * сервера: список промахов без событий — это ровно прежнее
   * поведение, и оно лучше пустого раздела.
   */
  function renderHistory(body, d, ttl, rate) {
    var items = d.history;
    if (!items) {
      items = (d.miss_log || []).filter(function (m) {
        // «Переписано 0» — частичное попадание: кэш сработал, терять
        // было нечего. В ленте такие записи только шумят.
        return m.written > 0;
      }).map(function (m) {
        return {
          kind: 'miss', ts: m.ts, gap: m.gap, written: m.written,
        };
      });
    }
    if (!items.length) return;

    var title = document.createElement('div');
    title.className = 'claude-cache-head claude-cache-head-sub';
    title.textContent = 'история';
    body.appendChild(title);

    for (var i = 0; i < items.length; i++) {
      var it = items[i];
      if (it.kind === 'miss') body.appendChild(missRow(it, ttl, rate));
      else body.appendChild(eventRow(it));
    }
  }

  /** Строка промаха. */
  function missRow(m, ttl, rate) {
    // Объяснённые промахи (сразу после смены модели/аккаунта) сервер
    // в ленту не кладёт вовсе — они гарантированы самим переключением
    // и не сообщают ничего сверх строки события. Здесь остаются только
    // два случая. Пауза короче TTL — аномалия: кэш обязан был выжить,
    // значит префикс сломало что-то другое (смена tools, окно
    // просмотра назад). Пауза длиннее — обычное вытеснение по времени.
    var known = typeof m.gap === 'number';
    var cls = '';
    var hint = '';
    if (!known) {
      cls = '';
      hint = '';
    } else if (m.gap < ttl) {
      cls = 'claude-cache-unexpected';
      hint = 'пауза короче TTL (' + ttl + ' мин), кэш должен был выжить';
    } else {
      cls = 'claude-cache-expired';
      hint = 'пауза больше TTL (' + ttl + ' мин) — кэш вытеснен '
        + 'по истечении максимального времени хранения';
    }
    // Потеря — разница между тем, что заплачено (запись, 2x),
    // и тем, что стоило бы попадание (чтение, 0.1x). Не полная
    // стоимость записи: даже при попадании префикс не бесплатен.
    var lost = m.written * MISS_LOSS_MULT * rate / 1e6;
    return row(
      hhmm(m.ts) + '  ·  пауза ' + (gapText(m.gap) || '—'),
      'переписано ' + human(m.written) + '  (-' + money(lost) + ')',
      cls, hint
    );
  }

  /** Строка переключения: смена модели или аккаунта. */
  function eventRow(ev) {
    var isAccount = ev.kind === 'account';
    var from = isAccount ? (ev.from || '—') : shortModel(ev.from);
    var to = isAccount ? (ev.to || '—') : shortModel(ev.to);
    var el = row(
      hhmm(ev.ts) + '  ·  ' + (isAccount ? 'аккаунт' : 'модель'),
      from + ' → ' + to,
      'claude-cache-event' + (isAccount ? ' claude-cache-event-account' : ''),
      isAccount
        ? 'Переключение аккаунта. Кэш нового провайдера пуст, поэтому '
          + 'первый ход после переключения промахивается закономерно'
        : 'Смена модели. Префикс кэша у каждой модели свой, поэтому '
          + 'первый ход после смены промахивается закономерно'
    );
    if (ev.pending) {
      // Ход после переключения ещё не сделан — кэш не проверялся.
      // Без пометки событие читалось бы как обошедшееся без промаха.
      el.classList.add('claude-cache-event-pending');
      el.title += '. Ход после него ещё не сделан';
    }
    return el;
  }

  function renderStats(body, d, guessed) {
    var last = d.last || {};
    // TTL приходит с сервера (он читает конфиг по mtime), поэтому
    // правка cacheKeepaliveTtlMinutes видна сразу, без Reload Window.
    var ttl = typeof d.ttl_minutes === 'number' && d.ttl_minutes > 0
      ? d.ttl_minutes : 60;
    var rate = (d.rates && d.rates.input) || 5.0;
    var verdict = last.verdict || '—';
    var gap = gapText(last.gap);

    var head = document.createElement('div');
    head.className = 'claude-cache-head';
    head.textContent = 'контекст ' + human(d.context);
    var sub = document.createElement('span');
    sub.className = 'claude-cache-sub';
    sub.textContent = d.model || '';
    head.appendChild(sub);
    body.appendChild(head);

    var verdictCls =
      verdict === 'попадание' ? 'claude-cache-hit' :
      verdict === 'старт' ? '' : 'claude-cache-miss';
    body.appendChild(row(
      'последний ход',
      verdict + (gap ? ' · пауза ' + gap : ''),
      verdictCls
    ));
    body.appendChild(row(
      'чтение / запись',
      human(last.read) + ' / ' + human(last.write)
    ));

    body.appendChild(document.createElement('hr'));

    body.appendChild(row('запросов', String(d.requests)));
    body.appendChild(row('прочитано из кэша', human(d.read)));
    body.appendChild(row('записано в кэш', human(d.write)));
    body.appendChild(row(
      'промахов',
      d.misses + (d.rewritten ? ' · переписано ' + human(d.rewritten) : ''),
      d.misses ? 'claude-cache-miss' : ''
    ));

    body.appendChild(document.createElement('hr'));

    body.appendChild(row('с кэшем', money(d.cost)));
    body.appendChild(row('без кэша было бы', money(d.cost_naive)));
    body.appendChild(row('экономия', d.ratio + '×', 'claude-cache-hit'));
    if (d.wasted) {
      // Минус — это расход, а не поступление; знак снимает двусмысленность.
      body.appendChild(row(
        'потеряно на промахах', '-' + money(d.wasted), 'claude-cache-miss'
      ));
    }

    // Промахи, у которых нашлась причина, показываем отдельной строкой:
    // это разница между «кэш течёт» и «я много переключался».
    if (d.explained_misses) {
      body.appendChild(row(
        'из них после переключений', String(d.explained_misses),
        'claude-cache-explained',
        'Промахи сразу после смены модели или аккаунта. Кэш в этот '
        + 'момент холодный по определению, поэтому в потери они не идут'
      ));
    }

    renderHistory(body, d, ttl, rate);

    // Показываем, чью статистику видим. Без этого нельзя отличить
    // «данные моей вкладки» от «данные соседней, попавшей под
    // автоопределение по свежести файла».
    var foot = document.createElement('div');
    foot.className = 'claude-cache-foot';
    foot.textContent = 'сессия ' + String(d.session || '—').slice(0, 8)
      + (guessed ? ' · выбрана по свежести файла' : '');
    body.appendChild(foot);
  }

  function openPanel(btn) {
    closePanel();
    anchorBtn = btn;

    panel = document.createElement('div');
    panel.id = PANEL_ID;
    panel.className = 'claude-cache-panel';

    var body = document.createElement('div');
    body.className = 'claude-cache-body';
    body.textContent = 'загрузка…';
    panel.appendChild(body);
    document.body.appendChild(panel);

    // Футер прижат к низу окна, поэтому раскрываемся вверх от кнопки.
    var r = btn.getBoundingClientRect();
    panel.style.bottom = Math.max(8, window.innerHeight - r.top + 6) + 'px';
    panel.style.right = Math.max(8, window.innerWidth - r.right) + 'px';

    document.addEventListener('mousedown', onOutside, true);
    document.addEventListener('keydown', onKeydown, true);

    var sid = findSessionId();
    logInfo(sid ? 'session id: ' + sid : 'session id не найден, спрашиваем свежую');
    var url = sid ? API_URL + '?session=' + encodeURIComponent(sid) : API_URL;

    fetch(url, { cache: 'no-store' })
      .then(function (res) { return res.json(); })
      .then(function (d) {
        if (!panel) return;
        body.textContent = '';
        if (d && d.ok) renderStats(body, d, !sid);
        else renderError(body, (d && d.error) || 'нет данных');
        // Высота стала известна только сейчас — переставляем, чтобы
        // панель не уезжала за верхний край на длинном списке промахов.
        var rr = btn.getBoundingClientRect();
        panel.style.bottom = Math.max(8, window.innerHeight - rr.top + 6) + 'px';
      })
      .catch(function (err) {
        if (!panel) return;
        body.textContent = '';
        renderError(body, 'http-server.py недоступен (порт 18923)');
        logInfo('fetch failed', err);
      });
  }

  /* ---------- кнопка ---------- */

  /**
   * Ищет точку вставки и донора стиля.
   *
   * Донор — обязательно сама `<button>`, а не обёртка вокруг неё:
   * футер собран из CSS-модулей (`footerButton_<hash>`), и весь вид —
   * размеры, шрифт, hover — висит на кнопке. Копирование класса
   * с внешнего div'а даёт пустой контейнерный класс, после чего
   * проступает UA-оформление <button> — серая плашка с рамкой.
   *
   * Точка вставки — предок найденной кнопки, лежащий прямо в футере:
   * контрол режима обёрнут в свой контейнер, и вставлять надо рядом
   * с обёрткой, иначе кнопка окажется внутри чужого поповера.
   */
  function findAnchor(footer) {
    var buttons = footer.querySelectorAll('button');
    var match = null;
    for (var i = 0; i < buttons.length; i++) {
      if (AUTO_EDIT_RE.test(buttons[i].textContent || '')) {
        match = buttons[i];
        break;
      }
    }
    if (match) {
      var node = match;
      while (node.parentNode && node.parentNode !== footer) node = node.parentNode;
      return {
        before: node.parentNode === footer ? node : null,
        donor: match,
      };
    }
    // Режим переключён на другой (лейбл иной) — донора берём с любой
    // штатной кнопки футера, чтобы вид всё равно совпал.
    return {
      before: null,
      donor: footer.querySelector('[class*="footerButton_"]')
        || footer.querySelector('button'),
    };
  }

  /**
   * Иконка-цилиндр БД перед подписью.
   *
   * Штатные кнопки футера собраны как `<svg 20×20 fill="none">` плюс
   * голый `<span>` с текстом; размер иконки задаёт сам класс
   * `footerButton_`, поэтому важно повторить именно эту разметку —
   * тогда наша иконка отмасштабируется вместе с родными.
   *
   * Строим через createElementNS, а не innerHTML: SVG живёт в своём
   * namespace, а innerHTML в webview может упереться в Trusted Types.
   */
  function cacheIcon() {
    var NS = 'http://www.w3.org/2000/svg';
    function el(name, attrs) {
      var node = document.createElementNS(NS, name);
      for (var k in attrs) {
        if (Object.prototype.hasOwnProperty.call(attrs, k)) {
          node.setAttribute(k, attrs[k]);
        }
      }
      return node;
    }
    var svg = el('svg', {
      width: '20', height: '20', viewBox: '0 0 20 20', fill: 'none',
    });
    var stroke = {
      stroke: 'currentColor',
      'stroke-width': '1.2',
      'stroke-linecap': 'round',
    };
    // Верхний эллипс + бока цилиндра + средняя перемычка.
    svg.appendChild(el('ellipse', {
      cx: '10', cy: '5.75', rx: '5.25', ry: '2.25',
      stroke: 'currentColor', 'stroke-width': '1.2',
    }));
    svg.appendChild(el('path', Object.assign({
      d: 'M4.75 5.75V14.25C4.75 15.49 7.1 16.5 10 16.5C12.9 16.5 15.25 15.49 15.25 14.25V5.75',
    }, stroke)));
    svg.appendChild(el('path', Object.assign({
      d: 'M4.75 10C4.75 11.24 7.1 12.25 10 12.25C12.9 12.25 15.25 11.24 15.25 10',
    }, stroke)));
    return svg;
  }

  function createButton(donor) {
    var btn = document.createElement('button');
    btn.type = 'button';
    if (donor && typeof donor.className === 'string' && donor.className) {
      btn.className = donor.className + ' ' + BTN_CLASS;
    } else {
      // Донора не нашли — гасим UA-оформление своими руками.
      btn.className = BTN_CLASS + ' ' + BARE_CLASS;
    }
    btn.title = 'Статистика prompt-кэша сессии';
    btn.appendChild(cacheIcon());
    var label = document.createElement('span');
    label.textContent = 'Usage';
    btn.appendChild(label);
    // Без preventDefault фокус уходит из composer'а: пользователь
    // теряет позицию каретки просто посмотрев статистику.
    btn.addEventListener('mousedown', function (e) { e.preventDefault(); });
    btn.addEventListener('click', function (e) {
      e.preventDefault();
      e.stopPropagation();
      if (panel) closePanel();
      else openPanel(btn);
    });
    return btn;
  }

  function mount(container) {
    var footer = container.querySelector('[class*="inputFooter_"]');
    if (!footer) return false;
    var anchor = findAnchor(footer);
    var btn = createButton(anchor.donor);
    // Порядок в футере: Usage · Cache · ByPass · автоправки. Ищем
    // самого левого из уже вставленных соседей — так позиция не зависит
    // от того, какой модуль смонтировался первым.
    var neighbour = footer.querySelector('.claude-keepalive-btn')
      || footer.querySelector('.claude-bypass-btn');
    if (neighbour && neighbour.parentNode === footer) {
      footer.insertBefore(btn, neighbour);
    } else if (anchor.before) {
      footer.insertBefore(btn, anchor.before);
    } else {
      // Лейбл не найден — цепляемся за spacer, он разделяет левую
      // и правую группы футера.
      var spacer = footer.querySelector('[class*="spacer_"]');
      if (spacer && spacer.parentNode === footer) {
        footer.insertBefore(btn, spacer.nextSibling);
      } else {
        footer.appendChild(btn);
      }
    }
    logInfo(
      'кнопка встроена',
      anchor.before ? 'рядом с автоправками' : 'по запасному якорю',
      anchor.donor ? 'стиль от ' + anchor.donor.className : 'без донора стиля'
    );
    return true;
  }

  /**
   * Держит порядок Usage · Cache · ByPass даже если кнопки появились
   * не в том порядке. Вставка при монтировании этого не гарантирует:
   * React пересоздаёт футер, модули домонтируются по своим таймерам,
   * и кто окажется первым — как повезёт.
   */
  function ensureOrder(footer) {
    var me = footer.querySelector('.' + BTN_CLASS);
    if (!me || me.parentNode !== footer) return;
    var right = footer.querySelector('.claude-keepalive-btn')
      || footer.querySelector('.claude-bypass-btn');
    if (!right || right.parentNode !== footer) return;
    // DOCUMENT_POSITION_FOLLOWING — сосед идёт ПОСЛЕ нас, всё верно.
    if (!(me.compareDocumentPosition(right) & 4)) {
      footer.insertBefore(me, right);
      logInfo('порядок восстановлен: Usage перед соседями');
    }
  }

  function scan(ctx) {
    // Узлы даёт общий обход (см. DOM WATCH) — один на все модули.
    // Свой поиск остаётся для вызовов вне прохода: при регистрации
    // и из обработчиков самого модуля.
    var containers = (ctx && ctx.inputs)
      || document.querySelectorAll('[class*="inputContainer_"]');
    for (var i = 0; i < containers.length; i++) {
      var footer = containers[i].querySelector('[class*="inputFooter_"]');
      // Проверяем по наличию элемента, а не по флагу-атрибуту: React
      // пересоздаёт поле ввода вместе с нашими атрибутами.
      if (containers[i].querySelector('.' + BTN_CLASS)) {
        if (footer) ensureOrder(footer);
        continue;
      }
      if (!containers[i].querySelector('[role="textbox"][contenteditable]')) continue;
      mount(containers[i]);
    }
    // Панель пережила пересоздание своей кнопки — закрываем, иначе
    // она повиснет непривязанной.
    if (panel && anchorBtn && !document.body.contains(anchorBtn)) closePanel();
  }

  function init() {
    // Наблюдатель и подстраховочный таймер — общие (см. DOM WATCH).
    window.__claudeDomWatch.register('usage', scan);
    logInfo('installed');
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();

/* ============================================================
 * BYPASS BUTTON
 *
 * Кнопка «ByPass» в футере, между Cache и «Править автоматически».
 * Ручной вход в тот же режим, который включает слово `да!`: пока он
 * активен, PreToolUse хук bypass-check.sh отвечает harness'у
 * permissionDecision=allow, и окна подтверждения не появляются.
 *
 * Механизм один и тот же — marker-файл .claude/hooks-runtime/<session_id>.
 * bypass-magic-word.py ставит его по слову, эта кнопка — через
 * POST /bypass. Поэтому состояние общее: включив режим словом, увидишь
 * кнопку активной, и наоборот. Выключить кнопкой можно, словом — нет
 * (магическое слово только включает).
 *
 * В отличие от слова, кнопка умеет и снимать marker, так что это ещё
 * и единственный способ выйти из режима, не закрывая сессию.
 *
 * Состояние опрашивается раз в BYPASS_POLL_MS: режим могли включить
 * словом в этой же вкладке, и кнопка обязана это отразить.
 *
 * Управление: `bypassButton` в claude-custom-config.toml.
 * ============================================================ */
(function () {
  if (window.__claudeBypassButtonInstalled) return;
  window.__claudeBypassButtonInstalled = true;

  var cfg = window.__CLAUDE_CUSTOM_CONFIG__ || {};
  if (cfg.bypassButton !== true) return;

  var API_URL = 'http://localhost:18923/bypass';
  var BTN_CLASS = 'claude-bypass-btn';
  var BARE_CLASS = 'claude-bypass-btn-bare';
  var ON_CLASS = 'claude-bypass-on';
  var BYPASS_POLL_MS = 5000;
  var AUTO_EDIT_RE = /Править автоматически|Edit automatically/i;
  var STORAGE_KEY = 'claudeCustomBypass';
  var CARRY_MS = (typeof cfg.buttonStateCarrySec === 'number'
    && cfg.buttonStateCarrySec >= 0 ? cfg.buttonStateCarrySec : 600) * 1000;

  var active = false;
  var carryChecked = false;  // перенос состояния пробовали делать

  function logInfo() {
    if (!cfg.logs) return;
    try {
      console.log.apply(console, ['[bypass-btn]'].concat([].slice.call(arguments)));
    } catch (e) {}
  }

  function sessionId() {
    var fn = window.__claudeSessionId;
    return typeof fn === 'function' ? fn() : null;
  }

  /* ---------- иконка ---------- */

  function boltIcon() {
    var NS = 'http://www.w3.org/2000/svg';
    var svg = document.createElementNS(NS, 'svg');
    svg.setAttribute('width', '20');
    svg.setAttribute('height', '20');
    svg.setAttribute('viewBox', '0 0 20 20');
    svg.setAttribute('fill', 'none');
    var path = document.createElementNS(NS, 'path');
    path.setAttribute('d', 'M11.5 2.5L5 11h3.5L8 17.5L15 9h-3.5z');
    path.setAttribute('fill', 'currentColor');
    svg.appendChild(path);
    return svg;
  }

  /* ---------- состояние ---------- */

  /**
   * Штатный «выключенный» вид берём у самого расширения: в CSS-модуле
   * футера рядом с footerButton_<hash> лежит footerButtonInactive_<hash>
   * с тем же хешем. Достраиваем имя из класса донора — так приглушённая
   * кнопка выглядит ровно как неактивная штатная.
   */
  function inactiveClassFrom(donorClass) {
    var m = /(?:^|\s)footerButton_(\w+)(?:\s|$)/.exec(donorClass || '');
    return m ? 'footerButtonInactive_' + m[1] : '';
  }

  function applyState(btn) {
    var inactive = btn.getAttribute('data-inactive-class') || '';
    if (inactive) {
      if (active) btn.classList.remove(inactive);
      else btn.classList.add(inactive);
    }
    if (active) btn.classList.add(ON_CLASS);
    else btn.classList.remove(ON_CLASS);
    btn.title = active
      ? 'Bypass включён: tool-вызовы идут без подтверждения. Клик — выключить.'
      : 'Bypass выключен. Клик — пропускать tool-вызовы без подтверждения '
        + 'до конца сессии (то же, что слово «да!»).';
  }

  function applyStateAll() {
    var nodes = document.querySelectorAll('.' + BTN_CLASS);
    for (var i = 0; i < nodes.length; i++) applyState(nodes[i]);
  }

  /* ---------- перенос состояния между сессиями ---------- */

  /**
   * Состояние bypass живёт в marker-файле на стороне сервера и привязано
   * к session id, а перезагрузка окна создаёт новую сессию — плюс
   * SessionEnd-хук маркер удаляет. Поэтому после перезагрузки режим
   * честно выключен, и восстановить его можно только поставив маркер
   * заново.
   *
   * Запоминаем последний явный выбор в localStorage и переносим его,
   * если он свежее CARRY_MS. Окно намеренно короткое: авто-снятие
   * подтверждений при закрытии сессии — защитное свойство механизма,
   * и превращать его в бессрочное включение нельзя. Перезагрузка
   * занимает секунды, так что нескольких минут достаточно.
   */
  function readLast() {
    try {
      var raw = localStorage.getItem(STORAGE_KEY);
      var obj = raw ? JSON.parse(raw) : null;
      return obj && typeof obj === 'object' ? obj : null;
    } catch (e) {
      return null;
    }
  }

  function writeLast(on) {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify({
        on: !!on, at: Date.now(),
      }));
    } catch (e) {}
  }

  function shouldCarry() {
    var last = readLast();
    return !!(last && last.on === true && typeof last.at === 'number'
      && Date.now() - last.at <= CARRY_MS);
  }

  function refresh() {
    var sid = sessionId();
    if (!sid) return;
    fetch(API_URL + '?session=' + encodeURIComponent(sid), { cache: 'no-store' })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (!d || !d.ok) return;

        // Первый ответ после загрузки страницы: сервер говорит
        // «выключен», но пользователь включал режим только что —
        // значит это последствие перезагрузки, а не его решение.
        if (!carryChecked) {
          carryChecked = true;
          if (!d.active && shouldCarry()) {
            logInfo('переношу включённый режим на новую сессию');
            send(sid, true);
            return;
          }
        }

        if (d.active !== active) {
          active = !!d.active;
          applyStateAll();
          logInfo('состояние обновлено:', active ? 'включён' : 'выключен');
        }
      })
      .catch(function () {});
  }

  /** Ставит или снимает маркер на сервере. */
  function send(sid, want) {
    return fetch(API_URL, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session: sid, active: want }),
    })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (!d || !d.ok) throw new Error((d && d.error) || 'отказ сервера');
        active = !!d.active;
        applyStateAll();
        return active;
      });
  }

  function toggle(btn) {
    var sid = sessionId();
    if (!sid) {
      logInfo('session id не определён — переключать нечего');
      btn.title = 'Не удалось определить сессию вкладки';
      return;
    }
    var want = !active;
    send(sid, want)
      .then(function (state) {
        // Запоминаем именно явный выбор пользователя — на него потом
        // опирается перенос после перезагрузки.
        writeLast(state);
        logInfo(state ? 'включён' : 'выключен');
      })
      .catch(function (err) {
        logInfo('переключение не удалось', err);
        btn.title = 'http-server.py недоступен (порт 18923)';
      });
  }

  /* ---------- встраивание ---------- */

  function findAnchor(footer) {
    var buttons = footer.querySelectorAll('button');
    var match = null;
    for (var i = 0; i < buttons.length; i++) {
      if (AUTO_EDIT_RE.test(buttons[i].textContent || '')) {
        match = buttons[i];
        break;
      }
    }
    if (match) {
      var node = match;
      while (node.parentNode && node.parentNode !== footer) node = node.parentNode;
      return {
        before: node.parentNode === footer ? node : null,
        donor: match,
      };
    }
    return {
      before: null,
      donor: footer.querySelector('[class*="footerButton_"]')
        || footer.querySelector('button'),
    };
  }

  function createButton(donor) {
    var btn = document.createElement('button');
    btn.type = 'button';
    var donorClass = donor && typeof donor.className === 'string'
      ? donor.className : '';
    if (donorClass) {
      btn.className = donorClass + ' ' + BTN_CLASS;
      var inactive = inactiveClassFrom(donorClass);
      if (inactive) btn.setAttribute('data-inactive-class', inactive);
    } else {
      btn.className = BTN_CLASS + ' ' + BARE_CLASS;
    }
    btn.appendChild(boltIcon());
    var label = document.createElement('span');
    label.textContent = 'ByPass';
    btn.appendChild(label);
    applyState(btn);
    btn.addEventListener('mousedown', function (e) { e.preventDefault(); });
    btn.addEventListener('click', function (e) {
      e.preventDefault();
      e.stopPropagation();
      toggle(btn);
    });
    return btn;
  }

  function mount(container) {
    var footer = container.querySelector('[class*="inputFooter_"]');
    if (!footer) return false;
    var anchor = findAnchor(footer);
    var btn = createButton(anchor.donor);
    // Порядок в футере: Usage · Cache · ByPass · автоправки. ByPass —
    // самый правый из наших, поэтому просто встаёт перед обёрткой
    // автоправок; соседи слева ориентируются уже на него.
    if (anchor.before) {
      footer.insertBefore(btn, anchor.before);
    } else {
      var spacer = footer.querySelector('[class*="spacer_"]');
      if (spacer && spacer.parentNode === footer) {
        footer.insertBefore(btn, spacer.nextSibling);
      } else {
        footer.appendChild(btn);
      }
    }
    return true;
  }

  function scan(ctx) {
    // Узлы даёт общий обход (см. DOM WATCH) — один на все модули.
    // Свой поиск остаётся для вызовов вне прохода: при регистрации
    // и из обработчиков самого модуля.
    var containers = (ctx && ctx.inputs)
      || document.querySelectorAll('[class*="inputContainer_"]');
    for (var i = 0; i < containers.length; i++) {
      if (containers[i].querySelector('.' + BTN_CLASS)) continue;
      if (!containers[i].querySelector('[role="textbox"][contenteditable]')) continue;
      mount(containers[i]);
    }
  }

  function init() {
    refresh();
    // Наблюдатель и подстраховочный таймер — общие (см. DOM WATCH).
    window.__claudeDomWatch.register('bypass', scan);
    setInterval(refresh, BYPASS_POLL_MS);
    logInfo('installed');
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();

/* ============================================================
 * FIND IN PAGE
 *
 * Кнопка 🔍 рядом с иконкой автосжатия (`usageButtonV2_*`) в футере.
 * По клику (или Ctrl+F) открывается строка поиска: подсветка всех
 * совпадений в переписке, переход по ним, счётчик «текущее/всего».
 *
 * Подсветка сделана через CSS Custom Highlight API
 * (`CSS.highlights` + `::highlight()`), а НЕ обёрткой в <mark>.
 * Это принципиально: переписка React-управляемая, любая вставка
 * элементов в неё слетает на ближайшем ре-рендере, а во время
 * стриминга ответа он происходит непрерывно. Highlight API работает
 * поверх Range и DOM вообще не трогает — совпадения переживают
 * перерисовку, пока живы текстовые узлы.
 *
 * Область поиска — `messagesContainer_*`; поле ввода исключено
 * намеренно: там текст прозрачный, а видимую копию рисует зеркало
 * (см. правило про .mentionMirror_ в claude-custom.css), и подсветка
 * в нём выглядела бы как совпадение в пустоте.
 *
 * Управление: `findInPage` в claude-custom-config.toml.
 * ============================================================ */
(function () {
  if (window.__claudeFindInPageInstalled) return;
  window.__claudeFindInPageInstalled = true;

  var cfg = window.__CLAUDE_CUSTOM_CONFIG__ || {};
  if (cfg.findInPage !== true) return;
  // Без Highlight API подсветить нечем, а ломать DOM ради этого нельзя.
  if (typeof CSS === 'undefined' || !CSS.highlights || typeof Highlight === 'undefined') {
    return;
  }

  var BTN_CLASS = 'claude-find-btn';
  var BARE_CLASS = 'claude-find-btn-bare';  // без донора стиля
  var BAR_ID = 'claude-find-bar';

  var scanning = false;      // защита от повторного входа
  var disabled = false;      // выключено предохранителем
  var mounts = 0;            // вставок с прошлой проверки
  var HL_ALL = 'claude-find';
  var HL_ACTIVE = 'claude-find-active';
  var DEBOUNCE_MS = 150;

  var bar = null;
  var input = null;
  var counter = null;
  var ranges = [];
  var index = -1;
  var debounceTimer = null;

  function logInfo() {
    if (!cfg.logs) return;
    try {
      console.log.apply(console, ['[find]'].concat([].slice.call(arguments)));
    } catch (e) {}
  }

  function searchRoot() {
    return document.querySelector('[class*="messagesContainer_"]') || document.body;
  }

  /* ---------- поиск ---------- */

  function collectRanges(query) {
    var found = [];
    if (!query) return found;
    var root = searchRoot();
    var needle = query.toLowerCase();

    var walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
      acceptNode: function (node) {
        if (!node.nodeValue) return NodeFilter.FILTER_REJECT;
        var el = node.parentElement;
        if (!el) return NodeFilter.FILTER_REJECT;
        // Своя строка поиска и поле ввода из выдачи исключены.
        if (el.closest('#' + BAR_ID)) return NodeFilter.FILTER_REJECT;
        if (el.closest('[class*="inputContainer_"]')) return NodeFilter.FILTER_REJECT;
        // <script>/<style> попадают в обход TreeWalker'а как текст.
        var tag = el.tagName;
        if (tag === 'SCRIPT' || tag === 'STYLE') return NodeFilter.FILTER_REJECT;
        return NodeFilter.FILTER_ACCEPT;
      },
    });

    var node;
    while ((node = walker.nextNode())) {
      var text = node.nodeValue.toLowerCase();
      var from = 0;
      for (;;) {
        var at = text.indexOf(needle, from);
        if (at === -1) break;
        var r = document.createRange();
        r.setStart(node, at);
        r.setEnd(node, at + needle.length);
        found.push(r);
        from = at + needle.length;
        if (found.length > 5000) return found;  // защита от «а» на длинной переписке
      }
    }
    return found;
  }

  function paint() {
    try {
      if (!ranges.length) {
        CSS.highlights.delete(HL_ALL);
        CSS.highlights.delete(HL_ACTIVE);
        return;
      }
      // Highlight — Set-подобный объект; добавляем по одному, а не
      // через spread: совпадений бывают тысячи, и раскладывать их
      // в аргументы вызова незачем.
      var all = new Highlight();
      for (var i = 0; i < ranges.length; i++) all.add(ranges[i]);
      CSS.highlights.set(HL_ALL, all);
      var current = ranges[index];
      if (current) CSS.highlights.set(HL_ACTIVE, new Highlight(current));
      else CSS.highlights.delete(HL_ACTIVE);
    } catch (e) {
      logInfo('подсветка не удалась', e);
    }
  }

  function updateCounter() {
    if (!counter) return;
    if (!input || !input.value) counter.textContent = '';
    else if (!ranges.length) counter.textContent = 'нет совпадений';
    else counter.textContent = (index + 1) + '/' + ranges.length;
  }

  function scrollToCurrent() {
    var r = ranges[index];
    if (!r) return;
    var el = r.startContainer.parentElement;
    if (el && el.scrollIntoView) {
      el.scrollIntoView({ block: 'center', behavior: 'smooth' });
    }
  }

  function runSearch(keepIndex) {
    var query = input ? input.value : '';
    var previous = index;
    ranges = collectRanges(query);
    if (!ranges.length) index = -1;
    else if (keepIndex && previous >= 0) index = Math.min(previous, ranges.length - 1);
    else index = 0;
    paint();
    updateCounter();
    if (index >= 0 && !keepIndex) scrollToCurrent();
    logInfo('совпадений:', ranges.length);
  }

  function step(delta) {
    if (!ranges.length) return;
    // Узлы могли исчезнуть при ре-рендере — тогда пересчитываем.
    var alive = ranges[index] && ranges[index].startContainer.isConnected;
    if (!alive) {
      runSearch(true);
      if (!ranges.length) return;
    }
    index = (index + delta + ranges.length) % ranges.length;
    paint();
    updateCounter();
    scrollToCurrent();
  }

  /* ---------- строка поиска ---------- */

  function closeBar() {
    if (debounceTimer) { clearTimeout(debounceTimer); debounceTimer = null; }
    if (bar) bar.remove();
    bar = null;
    input = null;
    counter = null;
    ranges = [];
    index = -1;
    try {
      CSS.highlights.delete(HL_ALL);
      CSS.highlights.delete(HL_ACTIVE);
    } catch (e) {}
  }

  function navButton(label, title, onClick) {
    var b = document.createElement('button');
    b.type = 'button';
    b.className = 'claude-find-nav';
    b.textContent = label;
    b.title = title;
    b.addEventListener('mousedown', function (e) { e.preventDefault(); });
    b.addEventListener('click', function (e) {
      e.preventDefault();
      e.stopPropagation();
      onClick();
    });
    return b;
  }

  function openBar(anchor) {
    if (bar) { input.focus(); input.select(); return; }

    bar = document.createElement('div');
    bar.id = BAR_ID;
    bar.className = 'claude-find-bar';

    input = document.createElement('input');
    input.type = 'text';
    input.className = 'claude-find-input';
    input.placeholder = 'Поиск по переписке';
    input.addEventListener('input', function () {
      if (debounceTimer) clearTimeout(debounceTimer);
      debounceTimer = setTimeout(function () { runSearch(false); }, DEBOUNCE_MS);
    });
    input.addEventListener('keydown', function (e) {
      if (e.key === 'Enter') {
        e.preventDefault();
        if (!ranges.length) runSearch(false);
        else step(e.shiftKey ? -1 : 1);
      } else if (e.key === 'Escape') {
        e.preventDefault();
        closeBar();
      }
    });

    counter = document.createElement('span');
    counter.className = 'claude-find-count';

    bar.appendChild(input);
    bar.appendChild(counter);
    bar.appendChild(navButton('↑', 'Предыдущее (Shift+Enter)', function () { step(-1); }));
    bar.appendChild(navButton('↓', 'Следующее (Enter)', function () { step(1); }));
    bar.appendChild(navButton('✕', 'Закрыть (Esc)', closeBar));
    document.body.appendChild(bar);

    placeBar();
    input.focus();
  }

  /**
   * Ставит строку над всей формой ввода, а не над кнопкой.
   *
   * Футер — часть той же рамки, что и поле: при многострочном тексте
   * форма растёт вверх и накрывает панель, привязанную к кнопке.
   * Якорь по `inputContainer_` держит строку выше рамки целиком.
   */
  /**
   * Контейнер ВИДИМОГО поля ввода.
   *
   * Просто `querySelector('[class*="inputContainer_"]')` брал первый
   * попавшийся узел — а он не обязательно тот, в котором лежит
   * композер (свои inputContainer_ есть и у скрытых, и у поповерных
   * форм). Панель из-за этого уезжала к левому краю окна, а не к краю
   * формы. Идём от самого поля ввода вверх — тот же приём, из-за
   * отсутствия которого модуль вчера подвесил webview.
   */
  function hostElement() {
    var composer = document.querySelector('[role="textbox"][contenteditable]');
    if (composer) {
      var host = composer.closest('[class*="inputContainer_"]');
      if (host) return host;
    }
    return document.querySelector('[class*="inputContainer_"]');
  }

  function placeBar() {
    if (!bar) return;
    var host = hostElement();
    if (!host) return;
    var r = host.getBoundingClientRect();
    bar.style.bottom = Math.max(8, window.innerHeight - r.top + 8) + 'px';
    // Левый край панели совпадает с левым краем формы — кнопка тоже
    // слева, и строка раскрывается прямо над ней. Если панель шире
    // остатка окна, поджимаем, чтобы не уехала за правый край.
    var width = bar.offsetWidth || 0;
    var left = Math.max(8, Math.min(r.left, window.innerWidth - width - 8));
    bar.style.left = left + 'px';
    bar.style.right = 'auto';
  }

  /* ---------- кнопка ---------- */

  function lensIcon() {
    var NS = 'http://www.w3.org/2000/svg';
    var svg = document.createElementNS(NS, 'svg');
    svg.setAttribute('width', '20');
    svg.setAttribute('height', '20');
    svg.setAttribute('viewBox', '0 0 20 20');
    svg.setAttribute('fill', 'none');
    var circle = document.createElementNS(NS, 'circle');
    circle.setAttribute('cx', '9');
    circle.setAttribute('cy', '9');
    circle.setAttribute('r', '4.75');
    circle.setAttribute('stroke', 'currentColor');
    circle.setAttribute('stroke-width', '1.3');
    var handle = document.createElementNS(NS, 'path');
    handle.setAttribute('d', 'M12.6 12.6L16 16');
    handle.setAttribute('stroke', 'currentColor');
    handle.setAttribute('stroke-width', '1.3');
    handle.setAttribute('stroke-linecap', 'round');
    svg.appendChild(circle);
    svg.appendChild(handle);
    return svg;
  }

  function createButton(donor) {
    var btn = document.createElement('button');
    btn.type = 'button';
    var donorClass = donor && typeof donor.className === 'string' ? donor.className : '';
    // Без класса штатной кнопки проступает UA-оформление <button> —
    // серая плашка с рамкой, чужеродная в футере.
    btn.className = donorClass
      ? donorClass + ' ' + BTN_CLASS
      : BTN_CLASS + ' ' + BARE_CLASS;
    btn.title = 'Поиск по переписке (Ctrl+F)';
    btn.appendChild(lensIcon());
    btn.addEventListener('mousedown', function (e) { e.preventDefault(); });
    btn.addEventListener('click', function (e) {
      e.preventDefault();
      e.stopPropagation();
      if (bar) closeBar();
      else openBar(btn);
    });
    return btn;
  }

  /**
   * Место — в левой группе футера, сразу за кнопкой меню `/`.
   *
   * Донора стиля берём у неё же, а не у `footerButton_`: меню —
   * иконка без подписи, той же формы и размера, что наша лупа.
   * `footerButton_` рассчитан на «иконка + текст» и даёт лишний
   * отступ справа.
   */
  function findSlot(footer) {
    var menu = footer.querySelector('[class*="menuButton_"]');
    if (menu && menu.parentNode === footer) {
      return { before: menu.nextSibling, donor: menu };
    }
    var donor = footer.querySelector('[class*="footerButton_"]')
      || footer.querySelector('button');
    return { before: footer.firstChild, donor: donor };
  }

  function mount(footer) {
    var slot = findSlot(footer);
    var btn = createButton(slot.donor);
    if (slot.before) footer.insertBefore(btn, slot.before);
    else footer.appendChild(btn);
    mounts++;
    return true;
  }

  /**
   * Сканирует ТОЛЬКО настоящее поле ввода.
   *
   * Раньше здесь был `querySelectorAll('[class*="inputFooter_"]')`,
   * и это подвесило webview: свой `inputFooter_` есть у поповера меню
   * `/` (см. SETTINGS MENU FIX). Кнопка вставлялась и туда, React сносил
   * её при перерисовке, MutationObserver звал скан заново — цикл
   * mount → мутация → mount, главный поток вставал, окно переставало
   * отвечать. Остальные модули не страдали именно потому, что идут
   * от `inputContainer_` и требуют внутри поле ввода; здесь тот же
   * фильтр.
   */
  function scan(ctx) {
    if (scanning || disabled) return;
    scanning = true;
    try {
      // Узлы даёт общий обход (см. DOM WATCH) — один на все модули.
      // Свой поиск остаётся для вызовов вне прохода: при регистрации
      // и из обработчиков самого модуля.
      var containers = (ctx && ctx.inputs)
        || document.querySelectorAll('[class*="inputContainer_"]');
      for (var i = 0; i < containers.length; i++) {
        if (!containers[i].querySelector('[role="textbox"][contenteditable]')) continue;
        var footer = containers[i].querySelector('[class*="inputFooter_"]');
        if (!footer) continue;
        if (footer.querySelector('.' + BTN_CLASS)) {
          ensureOrder(footer);
          continue;
        }
        mount(footer);
      }
      if (bar) placeBar();
    } finally {
      scanning = false;
    }
  }

  /** Держит кнопку сразу за меню `/`, если ре-рендер её сдвинул. */
  function ensureOrder(footer) {
    var me = footer.querySelector('.' + BTN_CLASS);
    var menu = footer.querySelector('[class*="menuButton_"]');
    if (!me || !menu) return;
    if (me.parentNode !== footer || menu.parentNode !== footer) return;
    if (menu.nextSibling !== me) footer.insertBefore(me, menu.nextSibling);
  }

  /**
   * Предохранитель. Если вставок за минуту оказалось неправдоподобно
   * много — значит кнопку кто-то сносит и мы попали в цикл. Лучше
   * выключить поиск, чем подвесить окно: цену такой ошибки я уже
   * заплатил чужим временем.
   */
  function guard() {
    if (mounts > 200) {
      disabled = true;
      logInfo('слишком много вставок за минуту — модуль отключён');
    }
    mounts = 0;
  }

  function onHotkey(e) {
    if (!(e.ctrlKey || e.metaKey) || e.key !== 'f') return;
    var anchor = document.querySelector('.' + BTN_CLASS);
    if (!anchor) return;
    e.preventDefault();
    e.stopPropagation();
    openBar(anchor);
  }

  function init() {
    // Наблюдатель, его throttle и подстраховочный таймер — общие
    // (см. DOM WATCH). Свой throttle этот модуль завёл первым, после
    // того как скан на каждую мутацию подвесил webview; теперь он
    // распространён на всех, а здесь остаётся только счётчик вставок:
    // он ловит не дороговизну скана, а цикл «вставили — снесли».
    window.__claudeDomWatch.register('find-in-page', scan);
    setInterval(guard, 60000);
    document.addEventListener('keydown', onHotkey, true);
    window.addEventListener('resize', placeBar);
    logInfo('installed');
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();

/* ============================================================
 * QUOTE FROM SELECTION — контекстная кнопка цитирования
 * ============================================================
 *
 * Когда выделение текста ЗАВЕРШЕНО, у его конца появляется значок
 * цитаты (❝). По клику текст преобразуется в цитату (`> ` перед
 * каждой строкой) и вставляется в composer с новой строки.
 *
 * Работает с выделением в:
 *   - переписке Claude (сообщения, промеки)
 *   - коде и форматированном тексте
 *
 * Примеры вставки (composer не пуст):
 *   "hello\nworld"       → "\n> hello\n> world\n"
 *   "one"                → "\n> one\n"
 *   "абзац\n\nабзац"     → "\n> абзац\n> абзац\n"
 *
 * Последний пример — не описка: соседние абзацы Selection API отдаёт
 * разделёнными пустой строкой, и в цитате она не нужна (см. toQuote).
 *
 * Перевод строки в конце обязателен: он оставляет каретку в начале
 * следующей строки, сразу под цитатой. Иначе набор ответа начинался
 * бы с нажатия Enter, чтобы не дописывать свой текст к чужой цитате.
 *
 * Момент появления. Кнопка ждёт конца выделения — отпускания мыши,
 * а для клавиатурного выделения паузы в изменениях. Показывать её
 * по `selectionchange` нельзя: событие приходит на каждый сдвиг
 * границы, и кнопка прыгала бы за курсором через весь фрагмент,
 * мешая целиться, — а нажать её во время протаскивания всё равно
 * невозможно.
 *
 * Кнопка исчезает, когда:
 *   - началось новое выделение или был клик мимо (mousedown)
 *   - выделение снято
 *   - был выполнен клик по ней (вставка произошла)
 *
 * Управление: `quoteFromSelection` в claude-custom-config.toml.
 * ============================================================ */
(function () {
  if (window.__claudeQuoteFromSelectionInstalled) return;
  window.__claudeQuoteFromSelectionInstalled = true;

  var cfg = window.__CLAUDE_CUSTOM_CONFIG__ || {};
  if (cfg.quoteFromSelection !== true) return;

  var quoteButton = null;    // текущая кнопка (или null)
  var currentSelection = null; // сохранённое выделение

  // Идёт ли выделение прямо сейчас (зажата кнопка мыши). Пока идёт,
  // кнопки быть не должно: `selectionchange` прилетает на каждый
  // сдвиг курсора, и кнопка прыгала бы за ним, мешая целиться в конец
  // фрагмента — а попасть по ней в этот момент всё равно нельзя.
  var selecting = false;

  // У выделения с клавиатуры (Shift+стрелки) нет момента «отпустили
  // кнопку», поэтому концом считаем паузу в изменениях. Полсекунды —
  // заметно дольше интервала автоповтора клавиши, так что при
  // удержании Shift+→ кнопка не мигает на каждом символе.
  var KEYBOARD_SETTLE_MS = 500;
  var settleTimer = null;

  function logInfo() {
    if (!cfg.logs) return;
    try {
      console.log.apply(console, ['[quote-from-selection]'].concat([].slice.call(arguments)));
    } catch (e) {}
  }

  function getComposer() {
    return (
      document.querySelector('[role="textbox"][contenteditable][aria-label*="essage" i]') ||
      document.querySelector('[role="textbox"][contenteditable]') ||
      document.querySelector('div[contenteditable]')
    );
  }

  /**
   * Получает выделённый текст из Selection API.
   * Возвращает {text, range} или null, если ничего не выделено.
   */
  function getSelection() {
    var sel = window.getSelection();
    if (!sel || sel.toString().length === 0) return null;
    return {
      text: sel.toString(),
      range: sel.rangeCount > 0 ? sel.getRangeAt(0).cloneRange() : null
    };
  }

  /**
   * Преобразует текст в цитату: `> ` перед каждой строкой.
   *
   * Пустые строки выбрасываются. Это не косметика: соседние абзацы
   * страницы Selection API отдаёт разделёнными пустой строкой
   * (`абзац\n\nабзац`) — так сериализуются блочные элементы. Для
   * пользователя же это две строки подряд, он их такими и видел,
   * выделяя. Оставлять разделитель значило бы вставлять цитату
   * разреженнее оригинала, да ещё и с осиротевшим `> ` посередине.
   *
   * Обратная сторона: пустая строка внутри выделенного кода тоже
   * исчезнет. Отличить её от разделителя абзацев нечем — в тексте
   * выделения это один и тот же `\n\n`, — а плотная цитата ближе
   * к тому, что видел глаз.
   */
  function toQuote(text) {
    var lines = text.split('\n');
    var quoted = [];
    for (var i = 0; i < lines.length; i++) {
      if (!lines[i].trim()) continue;
      quoted.push('> ' + lines[i]);
    }
    return quoted.join('\n');
  }

  /**
   * Вставляет цитату в composer как новую строку.
   * Если в composer уже есть текст, добавляет \n перед цитатой.
   */
  function insertQuoteIntoComposer(text) {
    var composer = getComposer();
    if (!composer) {
      logInfo('composer не найден');
      return false;
    }

    try {
      var quote = toQuote(text);
      if (!quote) {
        // После отсева пустых строк цитировать нечего — выделены были
        // одни пробелы. Вставлять при этом пару переводов строки
        // значило бы мусорить в поле по нажатию, которое пользователь
        // считает безрезультатным.
        logInfo('в выделении нет текста');
        return false;
      }

      composer.focus();

      // Ставим каретку в конец
      var sel = window.getSelection();
      var range = document.createRange();
      range.selectNodeContents(composer);
      range.collapse(false); // в конец
      sel.removeAllRanges();
      sel.addRange(range);

      // Проверяем, есть ли уже текст в composer. Читаем через общий
      // помощник: textContent склеил бы строки и пустым полем счёл бы
      // только по-настоящему пустое.
      var currentText = window.__claudeComposer.read(composer);
      var hasText = currentText.trim().length > 0;
      // Перевод строки в конце ставит каретку в начало следующей
      // строки — сразу под цитатой, готовой к набору ответа. Без него
      // каретка оставалась в конце последней процитированной строки,
      // и первое, что приходилось делать, — жать Enter, дописывая
      // свой текст к чужой цитате.
      var textToInsert = (hasText ? '\n' : '') + quote + '\n';

      // Построчно, через общий помощник: один insertText со всем
      // текстом склеил бы цитату из нескольких строк в одну — Chromium
      // не делает разрыва из `\n` внутри вставляемой строки.
      var inserted = window.__claudeComposer.insertMultiline(textToInsert);

      if (!inserted) {
        // Fallback: вставляем вручную. Каретку ставим сами — присвоение
        // textContent пересоздаёт узлы и позицию не сохраняет.
        composer.textContent = currentText + textToInsert;
        var endRange = document.createRange();
        endRange.selectNodeContents(composer);
        endRange.collapse(false);
        sel.removeAllRanges();
        sel.addRange(endRange);
        composer.dispatchEvent(new InputEvent('input', {
          bubbles: true, cancelable: true, inputType: 'insertText', data: textToInsert,
        }));
      }

      logInfo('цитата вставлена (' + quote.length + ' символов)');
      return true;
    } catch (e) {
      logInfo('ошибка при вставке:', (e && e.message) || e);
      return false;
    }
  }

  /**
   * Создаёт и показывает кнопку цитирования рядом с выделением.
   */
  function showQuoteButton() {
    var sel = getSelection();
    if (!sel || !sel.range) {
      hideQuoteButton();
      return;
    }

    currentSelection = sel;

    // Создаём кнопку, если её нет
    if (!quoteButton) {
      quoteButton = document.createElement('button');
      quoteButton.type = 'button';
      quoteButton.className = 'claude-quote-btn';
      quoteButton.textContent = '❝';
      quoteButton.title = 'Процитировать';
      quoteButton.style.cssText =
        'position:fixed;width:36px;height:36px;padding:3px 0 0;margin:0;border:none;' +
        'border-radius:6px;background:#8B5A2B;color:#fff;font-size:34px;line-height:1;' +
        'display:flex;align-items:center;justify-content:center;overflow:hidden;' +
        'cursor:pointer;z-index:10000;';

      quoteButton.addEventListener('mousedown', function (e) {
        e.preventDefault();
        e.stopPropagation();
      });

      quoteButton.addEventListener('click', function (e) {
        e.preventDefault();
        e.stopPropagation();
        if (currentSelection && currentSelection.text) {
          insertQuoteIntoComposer(currentSelection.text);
          hideQuoteButton();
        }
      });

      document.body.appendChild(quoteButton);
    }

    // Позиционируем кнопку выше выделения, справа
    try {
      var rect = sel.range.getBoundingClientRect();
      // Кнопка выше выделения, справа от конца. Низ кнопки — с зазором
      // над верхней границей выделения, полностью над строкой.
      var btnH = quoteButton.offsetHeight || 36;
      var x = rect.right + 4;
      var y = rect.top - btnH - 6;  // зазор 6px над выделением

      quoteButton.style.left = x + 'px';
      quoteButton.style.top = y + 'px';
      quoteButton.style.display = 'block';
    } catch (e) {
      logInfo('ошибка при позиционировании:', (e && e.message) || e);
      hideQuoteButton();
    }
  }

  /**
   * Скрывает кнопку цитирования.
   */
  function hideQuoteButton() {
    if (quoteButton) {
      quoteButton.style.display = 'none';
    }
    currentSelection = null;
  }

  function cancelSettle() {
    if (settleTimer) {
      clearTimeout(settleTimer);
      settleTimer = null;
    }
  }

  /** Показ после паузы — для выделения с клавиатуры. */
  function scheduleSettledShow() {
    cancelSettle();
    settleTimer = setTimeout(function () {
      settleTimer = null;
      if (selecting) return;  // мышь снова в деле, ждём mouseup
      if (window.getSelection().toString().length > 0) showQuoteButton();
    }, KEYBOARD_SETTLE_MS);
  }

  /** Показывает кнопку немедленно, если есть что цитировать. */
  function showIfSelected() {
    cancelSettle();
    if (window.getSelection().toString().length > 0) showQuoteButton();
    else hideQuoteButton();
  }

  /**
   * Изменение выделения. Само по себе поводом показать кнопку не
   * является: событие прилетает на каждый сдвиг границы, то есть
   * десятки раз за одно протаскивание мышью. Здесь только гасим
   * кнопку и, для клавиатурного выделения, заводим таймер паузы.
   */
  function handleSelectionChange() {
    if (window.getSelection().toString().length === 0) {
      cancelSettle();
      hideQuoteButton();
      return;
    }
    // Выделение мышью ещё идёт — момент показа наступит на mouseup.
    if (selecting) {
      cancelSettle();
      hideQuoteButton();
      return;
    }
    scheduleSettledShow();
  }

  /** Начало выделения мышью; клик по самой кнопке им не считается. */
  function handleMouseDown(e) {
    if (quoteButton && (e.target === quoteButton || quoteButton.contains(e.target))) {
      return;
    }
    selecting = true;
    cancelSettle();
    hideQuoteButton();
  }

  /** Конец выделения мышью — единственный момент, когда кнопка нужна. */
  function handleMouseUp(e) {
    // Отпускание на самой кнопке — это её нажатие, а не конец
    // выделения: показывать нечего, клик сейчас всё уберёт сам.
    if (quoteButton && e && (e.target === quoteButton || quoteButton.contains(e.target))) {
      return;
    }
    selecting = false;
    showIfSelected();
  }

  function init() {
    // Показ — только по завершению выделения: отпустили мышь либо
    // клавиатурное выделение перестало меняться.
    document.addEventListener('mouseup', handleMouseUp);
    document.addEventListener('touchend', handleMouseUp);
    document.addEventListener('selectionchange', handleSelectionChange);

    // Начало протаскивания и клик мимо — оба гасят кнопку.
    document.addEventListener('mousedown', handleMouseDown);

    logInfo('контекстная кнопка цитирования активирована');
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();

/* ============================================================
 * CACHE KEEPALIVE
 *
 * Кнопка «Cache» между Usage и ByPass. Включённая — не даёт истечь
 * prompt-кэшу сессии: при простое дольше cacheKeepaliveMinutes
 * отправляет в чат короткое сообщение, и попадание в кэш продлевает
 * его TTL ещё на час (TTL обновляется при каждом использовании).
 *
 * Почему это модуль webview, а не хук. Хуки Claude Code реактивны:
 * они отвечают на события, но сами инициировать сообщение не могут.
 * Отправить его способен только тот, у кого есть поле ввода, то есть
 * webview. Серверная часть при этом всё равно нужна — именно она
 * знает, когда был последний ход (GET /cache-usage → last.ts).
 *
 * Отправляем не по таймеру вслепую, а по факту простоя: пока идёт
 * обычная работа, кэш продлевается сам, и лишние сообщения — это
 * потраченная квота и мусор в истории. Проверка раз в минуту, отправка
 * только когда простой реально дошёл до порога.
 *
 * Три случая, когда ход пропускается:
 *   - в поле ввода есть черновик — затирать его недопустимо;
 *   - модель отвечает прямо сейчас (в футере кнопка «стоп»);
 *   - session id не определился.
 *
 * Состояние тоггла хранится в localStorage по session id: у каждой
 * вкладки своё, и переживает Reload Window.
 *
 * Управление: `cacheKeepalive`, `cacheKeepaliveMinutes`,
 * `cacheKeepaliveMessage` в claude-custom-config.toml.
 * ============================================================ */
(function () {
  if (window.__claudeCacheKeepaliveInstalled) return;
  window.__claudeCacheKeepaliveInstalled = true;

  var cfg = window.__CLAUDE_CUSTOM_CONFIG__ || {};
  if (cfg.cacheKeepalive !== true) return;

  var USAGE_URL = 'http://localhost:18923/cache-usage';
  var BTN_CLASS = 'claude-keepalive-btn';
  var BARE_CLASS = 'claude-keepalive-btn-bare';
  var ON_CLASS = 'claude-keepalive-on';
  var STORAGE_KEY = 'claudeCustomCacheKeepalive';
  var LAST_KEY = '__last';
  var TICK_MS = 60000;
  var CARRY_MS = (typeof cfg.buttonStateCarrySec === 'number'
    && cfg.buttonStateCarrySec >= 0 ? cfg.buttonStateCarrySec : 600) * 1000;
  var AUTO_EDIT_RE = /Править автоматически|Edit automatically/i;

  // Стартовые значения — из bootstrap; дальше их обновляет pollConfig().
  var IDLE_MINUTES = typeof cfg.cacheKeepaliveMinutes === 'number'
    && cfg.cacheKeepaliveMinutes > 0 ? cfg.cacheKeepaliveMinutes : 55;
  var MESSAGE = typeof cfg.cacheKeepaliveMessage === 'string'
    && cfg.cacheKeepaliveMessage ? cfg.cacheKeepaliveMessage : 'keepalive';
  var MIN_CONTEXT = typeof cfg.cacheKeepaliveMinContext === 'number'
    && cfg.cacheKeepaliveMinContext >= 0 ? cfg.cacheKeepaliveMinContext : 50000;
  // Верхняя граница окна: TTL кэша. Дольше него поддерживать уже нечего.
  var TTL_MINUTES = typeof cfg.cacheKeepaliveTtlMinutes === 'number'
    && cfg.cacheKeepaliveTtlMinutes > 0 ? cfg.cacheKeepaliveTtlMinutes : 60;

  var CONFIG_URL = 'http://localhost:18923/custom-config';
  var CONFIG_POLL_MS = 5000;

  var NOTE_ID = 'claude-keepalive-note';
  var NOTE_TIMEOUT_MS = 12000;

  var enabled = false;
  var stateLoaded = false;   // состояние из localStorage уже прочитано
  var pendingDraft = null;   // черновик, снятый на время отправки
  var lastIdleMin = null;
  var cacheLost = false;     // простой перевалил за TTL, поддерживать нечего
  var noteEl = null;
  var noteTimer = null;

  function human(n) {
    if (typeof n !== 'number' || !isFinite(n)) return '—';
    if (n >= 1e6) return (n / 1e6).toFixed(1) + 'M';
    if (n >= 1e3) return (n / 1e3).toFixed(1) + 'k';
    return String(Math.round(n));
  }

  function logInfo() {
    if (!cfg.logs) return;
    try {
      console.log.apply(console, ['[keepalive]'].concat([].slice.call(arguments)));
    } catch (e) {}
  }

  function sessionId() {
    var fn = window.__claudeSessionId;
    return typeof fn === 'function' ? fn() : null;
  }

  /* ---------- хранилище состояния ---------- */

  function readStore() {
    try {
      var raw = localStorage.getItem(STORAGE_KEY);
      var obj = raw ? JSON.parse(raw) : null;
      return obj && typeof obj === 'object' ? obj : {};
    } catch (e) {
      return {};
    }
  }

  function writeStore(store) {
    // Записей накапливается по одной на сессию, а сессия создаётся
    // на каждую перезагрузку окна. Оставляем только свежие, иначе
    // localStorage растёт без предела.
    var keys = Object.keys(store).filter(function (k) { return k !== LAST_KEY; });
    if (keys.length > 30) {
      keys.slice(0, keys.length - 30).forEach(function (k) { delete store[k]; });
    }
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(store));
    } catch (e) {}
  }

  /**
   * Возвращает состояние для текущей сессии или null, если session id
   * ещё не определился (React-дерево не отрисовано).
   *
   * Перезагрузка окна создаёт НОВУЮ сессию, поэтому записи под старым
   * id для неё бесполезны. Чтобы выбор не сбрасывался, переносим его
   * из последнего явного включения — но только если оно свежее
   * CARRY_MS. Перезагрузка занимает секунды, так что короткого окна
   * достаточно; без ограничения включение недельной давности молча
   * оживало бы в новом разговоре.
   */
  function loadEnabled() {
    var sid = sessionId();
    if (!sid) return null;
    var store = readStore();
    if (typeof store[sid] === 'boolean') return store[sid];

    var last = store[LAST_KEY];
    if (last && last.on === true && typeof last.at === 'number'
        && Date.now() - last.at <= CARRY_MS) {
      // Переносим в новую сессию и фиксируем, чтобы дальше читалось
      // напрямую. __last намеренно не трогаем: иначе окно давности
      // продлевалось бы каждой перезагрузкой и стало бы бессрочным.
      store[sid] = true;
      writeStore(store);
      logInfo('состояние перенесено с прошлой сессии');
      return true;
    }
    return false;
  }

  function saveEnabled(on) {
    var sid = sessionId();
    if (!sid) return;
    var store = readStore();
    // Пишем явный false, а не удаляем: иначе выключение выглядело бы
    // как «выбора не было» и перенос включил бы кнопку обратно.
    store[sid] = !!on;
    store[LAST_KEY] = { on: !!on, at: Date.now() };
    writeStore(store);
  }

  /* ---------- иконка ---------- */

  function clockIcon() {
    var NS = 'http://www.w3.org/2000/svg';
    var svg = document.createElementNS(NS, 'svg');
    svg.setAttribute('width', '20');
    svg.setAttribute('height', '20');
    svg.setAttribute('viewBox', '0 0 20 20');
    svg.setAttribute('fill', 'none');
    var circle = document.createElementNS(NS, 'circle');
    circle.setAttribute('cx', '10');
    circle.setAttribute('cy', '10');
    circle.setAttribute('r', '6.25');
    circle.setAttribute('stroke', 'currentColor');
    circle.setAttribute('stroke-width', '1.2');
    var hands = document.createElementNS(NS, 'path');
    hands.setAttribute('d', 'M10 6.25V10l2.75 1.6');
    hands.setAttribute('stroke', 'currentColor');
    hands.setAttribute('stroke-width', '1.2');
    hands.setAttribute('stroke-linecap', 'round');
    hands.setAttribute('stroke-linejoin', 'round');
    svg.appendChild(circle);
    svg.appendChild(hands);
    return svg;
  }

  /* ---------- состояние кнопки ---------- */

  function inactiveClassFrom(donorClass) {
    var m = /(?:^|\s)footerButton_(\w+)(?:\s|$)/.exec(donorClass || '');
    return m ? 'footerButtonInactive_' + m[1] : '';
  }

  /* ---------- уведомление при слишком малом контексте ---------- */

  function hideNote() {
    if (noteTimer) {
      clearTimeout(noteTimer);
      noteTimer = null;
    }
    if (noteEl) noteEl.remove();
    noteEl = null;
    document.removeEventListener('mousedown', onNoteOutside, true);
    document.removeEventListener('keydown', onNoteKeydown, true);
  }

  function onNoteOutside(e) {
    if (noteEl && !noteEl.contains(e.target)) hideNote();
  }

  function onNoteKeydown(e) {
    if (e.key === 'Escape') hideNote();
  }

  function showNote(btn, lines) {
    hideNote();
    noteEl = document.createElement('div');
    noteEl.id = NOTE_ID;
    noteEl.className = 'claude-keepalive-note';
    for (var i = 0; i < lines.length; i++) {
      var p = document.createElement('div');
      p.className = i === 0
        ? 'claude-keepalive-note-head'
        : 'claude-keepalive-note-line';
      p.textContent = lines[i];
      noteEl.appendChild(p);
    }
    document.body.appendChild(noteEl);

    var r = btn.getBoundingClientRect();
    noteEl.style.bottom = Math.max(8, window.innerHeight - r.top + 6) + 'px';
    noteEl.style.right = Math.max(8, window.innerWidth - r.right) + 'px';

    document.addEventListener('mousedown', onNoteOutside, true);
    document.addEventListener('keydown', onNoteKeydown, true);
    noteTimer = setTimeout(hideNote, NOTE_TIMEOUT_MS);
  }

  /**
   * Текст отказа. Считаем то, что реально предотвращается: холодный
   * старт переписывает весь префикс по ставке записи (2x) вместо
   * чтения (0.1x), то есть стоит T × 1.9 × ставка_input.
   *
   * Отношение цены прогрева к цене холодного старта — ровно 19 и от
   * размера контекста не зависит (T сокращается). Поэтому порог здесь
   * не про окупаемость, а про абсолютную величину ставки: на мелком
   * контексте предотвращать нечего, а сообщение в истории и расход
   * квоты остаются те же.
   */
  function tooSmallLines(d) {
    var rate = (d.rates && d.rates.input) || 5.0;
    var avoided = d.context * 1.9 * rate / 1e6;
    return [
      'Контекст сессии — ' + human(d.context) + ' токенов.',
      'Поддержание включается от ' + human(MIN_CONTEXT) + '.',
      '',
      'Пока холодный старт обойдётся примерно в $' + avoided.toFixed(2)
        + ' — дешевле, чем сообщение поддержания в истории и расход квоты '
        + 'на него. Вернись к кнопке, когда контекст подрастёт.',
    ];
  }

  /**
   * Подсказка описывает три состояния: выключено, сторожит, кэш уже
   * потерян. Третье важно показывать явно — иначе включённая кнопка,
   * которая сознательно ничего не делает, выглядит как сломанная.
   *
   * Порог и простой подписаны отдельными словами: формулировка
   * «Простой сейчас: 3 мин из 55» читалась двусмысленно, первое число
   * принимали за настройку.
   */
  function titleFor() {
    if (!enabled) {
      return 'Поддержание кэша выключено. Клик — включить: при простое '
        + 'от ' + IDLE_MINUTES + ' до ' + TTL_MINUTES + ' мин будет '
        + 'отправлено короткое сообщение, чтобы кэш не истёк. Доступно '
        + 'от ' + human(MIN_CONTEXT) + ' токенов контекста.';
    }
    var head = 'Поддержание кэша включено, окно ' + IDLE_MINUTES + '–'
      + TTL_MINUTES + ' мин.';
    if (cacheLost) {
      return head + ' Простой ' + lastIdleMin.toFixed(0) + ' мин — кэш уже '
        + 'истёк, поддерживать нечего. Возобновлю охрану после твоего '
        + 'следующего сообщения. Клик — выключить.';
    }
    if (lastIdleMin !== null) {
      var left = IDLE_MINUTES - lastIdleMin;
      head += left > 0
        ? ' Простой ' + lastIdleMin.toFixed(0) + ' мин, до отправки около '
          + Math.round(left) + ' мин.'
        : ' Простой ' + lastIdleMin.toFixed(0) + ' мин — сообщение уйдёт '
          + 'при ближайшей проверке.';
    }
    return head + ' Клик — выключить.';
  }

  function applyState(btn) {
    var inactive = btn.getAttribute('data-inactive-class') || '';
    if (inactive) {
      if (enabled) btn.classList.remove(inactive);
      else btn.classList.add(inactive);
    }
    if (enabled) btn.classList.add(ON_CLASS);
    else btn.classList.remove(ON_CLASS);
    btn.title = titleFor();
  }

  function applyStateAll() {
    var nodes = document.querySelectorAll('.' + BTN_CLASS);
    for (var i = 0; i < nodes.length; i++) applyState(nodes[i]);
  }

  /* ---------- отправка ---------- */

  function composerOf(container) {
    return container.querySelector('[role="textbox"][contenteditable]');
  }

  /* Чтение и многострочная вставка — общие (см. COMPOSER TEXT).
   * Обе функции жили здесь и были написаны по следам поломок с этим
   * полем; цитирование наступило на ту же вставку повторно, поэтому
   * они переехали в общий блок. */
  function readComposer(el) {
    return window.__claudeComposer.read(el);
  }

  function insertMultiline(text) {
    return window.__claudeComposer.insertMultiline(text);
  }

  /**
   * Отправляет сообщение через штатное поле ввода.
   *
   * Текст вставляем execCommand'ом: поле React-controlled, прямая
   * запись в textContent теряется на ближайшем ре-рендере (тот же
   * приём, что в EMOJI PICKER).
   *
   * Отправляем кликом по штатной кнопке, а не синтетическим Enter:
   * обработчик Enter живёт в React и может меняться между версиями,
   * а кнопка — стабильная точка входа.
   */
  function trySend() {
    var containers = document.querySelectorAll('[class*="inputContainer_"]');
    for (var i = 0; i < containers.length; i++) {
      var container = containers[i];
      var el = composerOf(container);
      if (!el) continue;

      // Кнопка отправки во время генерации превращается в «стоп»:
      // отличаем по иконке из того же CSS-модуля футера.
      if (container.querySelector('[class*="stopIcon_"]')) {
        logInfo('пропуск: модель отвечает');
        return false;
      }
      if (!container.querySelector('[class*="sendButton_"]')) continue;

      // Черновик не мешает отправке: убираем его, шлём keepalive
      // и возвращаем обратно. Текст держим в pendingDraft до успешного
      // возврата — если что-то пойдёт не так, его подберёт scan().
      var draft = readComposer(el);
      el.focus();
      if (draft) {
        pendingDraft = draft;
        try {
          document.execCommand('selectAll', false, null);
          document.execCommand('delete', false, null);
        } catch (e) {
          logInfo('не удалось очистить поле — отправку пропускаю');
          pendingDraft = null;
          return false;
        }
      }

      var ok = insertMultiline(MESSAGE);
      if (!ok) {
        logInfo('вставка текста не удалась');
        restoreDraft(container, 0);
        return false;
      }
      clickSendWhenReady(container, 0);
      return true;
    }
    return false;
  }

  /**
   * Возвращает черновик в поле, когда оно освободится.
   *
   * Ждём именно пустого поля: сразу после клика там ещё лежит наш
   * keepalive, а если пользователь начал печатать — затирать его
   * нельзя. Поэтому при непустом поле просто ждём дальше, а по
   * истечении попыток оставляем pendingDraft висеть: его подберёт
   * ближайший scan(), когда поле освободится. Так черновик не теряется
   * даже при неудачной отправке.
   *
   * Позиция каретки внутри черновика не сохраняется — текст
   * возвращается целиком, каретка встаёт в конец.
   */
  function restoreDraft(container, attempt) {
    if (!pendingDraft) return;
    var el = composerOf(container);
    if (!el) return;
    if ((el.textContent || '') === '') {
      var text = pendingDraft;
      pendingDraft = null;
      el.focus();
      if (insertMultiline(text)) {
        logInfo('черновик возвращён,', text.length, 'символов,',
          text.split('\n').length, 'строк');
      } else {
        pendingDraft = text;  // не вышло — пусть попробует scan()
      }
      return;
    }
    if (attempt >= 40) return;  // 40 × 50 мс = 2 с
    setTimeout(function () { restoreDraft(container, attempt + 1); }, 50);
  }

  /**
   * Жмёт кнопку отправки, дождавшись, когда она разблокируется.
   *
   * В бандле она `disabled: !busy && !hasContent`, то есть при пустом
   * поле заблокирована. React обновляет состояние асинхронно: клик
   * сразу после execCommand приходится на ещё выключенную кнопку и
   * молча пропадает, а текст остаётся висеть в поле. Вдобавок React
   * может заменить сам узел, поэтому кнопку каждый раз ищем заново,
   * а не держим ссылку.
   *
   * Если за отведённое время кнопка так и не ожила, убираем свой текст:
   * оставлять его в поле хуже, чем не отправить — пользователь потом
   * наткнётся на чужую строку в композере.
   */
  function clickSendWhenReady(container, attempt) {
    var send = container.querySelector('[class*="sendButton_"]');
    if (send && !send.disabled) {
      send.click();
      logInfo('отправлено сообщение поддержания, попыток:', attempt);
      restoreDraft(container, 0);
      return;
    }
    if (attempt >= 30) {  // 30 × 50 мс = 1.5 с
      logInfo('кнопка отправки не разблокировалась — убираем текст');
      var el = composerOf(container);
      if (el && (el.textContent || '').trim() === MESSAGE.trim()) {
        el.focus();
        try {
          document.execCommand('selectAll', false, null);
          document.execCommand('delete', false, null);
        } catch (e) {}
      }
      restoreDraft(container, 0);
      return;
    }
    setTimeout(function () {
      clickSendWhenReady(container, attempt + 1);
    }, 50);
  }

  /**
   * Подтягивает настройки с сервера.
   *
   * Значения из bootstrap фиксируются на момент загрузки окна, поэтому
   * правка TOML не действовала до Reload Window — и порог показывался
   * старый, хотя в файле стоял новый. Опрос снимает это: тот же приём
   * уже используется для emojiButtonPlacement.
   *
   * Сам флаг cacheKeepalive намеренно не обновляем: модуль либо
   * установлен со всеми обработчиками, либо нет, и включить его на лету
   * всё равно нельзя — для этого нужен Reload Window.
   */
  function applyLiveConfig(c) {
    if (!c || typeof c !== 'object') return;
    var changed = [];

    if (typeof c.cacheKeepaliveMinutes === 'number'
        && c.cacheKeepaliveMinutes > 0
        && c.cacheKeepaliveMinutes !== IDLE_MINUTES) {
      IDLE_MINUTES = c.cacheKeepaliveMinutes;
      changed.push('порог ' + IDLE_MINUTES + ' мин');
    }
    if (typeof c.cacheKeepaliveMessage === 'string'
        && c.cacheKeepaliveMessage
        && c.cacheKeepaliveMessage !== MESSAGE) {
      MESSAGE = c.cacheKeepaliveMessage;
      changed.push('текст сообщения');
    }
    if (typeof c.cacheKeepaliveMinContext === 'number'
        && c.cacheKeepaliveMinContext >= 0
        && c.cacheKeepaliveMinContext !== MIN_CONTEXT) {
      MIN_CONTEXT = c.cacheKeepaliveMinContext;
      changed.push('порог контекста ' + human(MIN_CONTEXT));
    }
    if (typeof c.cacheKeepaliveTtlMinutes === 'number'
        && c.cacheKeepaliveTtlMinutes > 0
        && c.cacheKeepaliveTtlMinutes !== TTL_MINUTES) {
      TTL_MINUTES = c.cacheKeepaliveTtlMinutes;
      changed.push('TTL ' + TTL_MINUTES + ' мин');
    }

    if (changed.length) {
      normalizeWindow();
      applyStateAll();  // подсказки пересобираются с новыми значениями
      logInfo('конфиг обновлён:', changed.join(', '));
    }
  }

  /**
   * Порог обязан быть строго меньше TTL, иначе окно срабатывания пусто
   * и кнопка молча не делает ничего. Чинить это отказом было бы хуже:
   * пользователь увидел бы включённую кнопку без эффекта. Поджимаем
   * порог и говорим об этом в консоль.
   */
  function normalizeWindow() {
    if (IDLE_MINUTES < TTL_MINUTES) return;
    var fixed = Math.max(1, TTL_MINUTES - 5);
    logInfo('порог', IDLE_MINUTES, 'мин не меньше TTL', TTL_MINUTES,
      '— окно пусто, поджимаю порог до', fixed);
    IDLE_MINUTES = fixed;
  }

  function pollConfig() {
    fetch(CONFIG_URL, { cache: 'no-store' })
      .then(function (r) { return r.json(); })
      .then(function (d) { if (d && d.ok) applyLiveConfig(d.config); })
      .catch(function () {});
  }

  function tick() {
    if (!enabled) return;
    var sid = sessionId();
    if (!sid) return;
    fetch(USAGE_URL + '?session=' + encodeURIComponent(sid), { cache: 'no-store' })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (!d || !d.ok || !d.last || !d.last.ts) return;
        var ts = Date.parse(d.last.ts);
        if (isNaN(ts)) return;
        lastIdleMin = (Date.now() - ts) / 60000;

        // Верхняя граница окна. Кэш живёт TTL_MINUTES от НАЧАЛА
        // последнего запроса; после этого он уже вытеснен, и сообщение
        // ничего не продлевает — оно оплачивает полную перезапись
        // префикса по ставке записи плюс output ответа. То есть ровно
        // тот холодный старт, ради предотвращения которого всё
        // затевалось. Молчим и ждём, пока пользователь напишет сам:
        // его сообщение создаст новый кэш, и охрана возобновится.
        cacheLost = lastIdleMin >= TTL_MINUTES;
        applyStateAll();
        if (cacheLost) {
          logInfo('простой', lastIdleMin.toFixed(0), 'мин — кэш уже истёк, не отправляю');
          return;
        }
        if (lastIdleMin >= IDLE_MINUTES) trySend();
      })
      .catch(function () {});
  }

  /* ---------- встраивание ---------- */

  function findAnchor(footer) {
    var buttons = footer.querySelectorAll('button');
    var match = null;
    for (var i = 0; i < buttons.length; i++) {
      if (AUTO_EDIT_RE.test(buttons[i].textContent || '')) {
        match = buttons[i];
        break;
      }
    }
    if (match) {
      var node = match;
      while (node.parentNode && node.parentNode !== footer) node = node.parentNode;
      return { before: node.parentNode === footer ? node : null, donor: match };
    }
    return {
      before: null,
      donor: footer.querySelector('[class*="footerButton_"]')
        || footer.querySelector('button'),
    };
  }

  function createButton(donor) {
    var btn = document.createElement('button');
    btn.type = 'button';
    var donorClass = donor && typeof donor.className === 'string'
      ? donor.className : '';
    if (donorClass) {
      btn.className = donorClass + ' ' + BTN_CLASS;
      var inactive = inactiveClassFrom(donorClass);
      if (inactive) btn.setAttribute('data-inactive-class', inactive);
    } else {
      btn.className = BTN_CLASS + ' ' + BARE_CLASS;
    }
    btn.appendChild(clockIcon());
    var label = document.createElement('span');
    label.textContent = 'Cache';
    btn.appendChild(label);
    applyState(btn);
    btn.addEventListener('mousedown', function (e) { e.preventDefault(); });
    btn.addEventListener('click', function (e) {
      e.preventDefault();
      e.stopPropagation();
      hideNote();
      if (enabled) {
        setEnabled(false);
      } else {
        requestEnable(btn);
      }
    });
    return btn;
  }

  function setEnabled(on) {
    enabled = on;
    saveEnabled(on);
    applyStateAll();
    logInfo(on ? 'включено' : 'выключено');
    if (on) tick();
  }

  /**
   * Включение — только после проверки размера контекста у сервера.
   * Синхронно решить нельзя: пока тоггл выключен, тики не идут и
   * свежих данных на руках нет.
   */
  function requestEnable(btn) {
    var sid = sessionId();
    if (!sid) {
      showNote(btn, ['Не удалось определить сессию вкладки.']);
      return;
    }
    fetch(USAGE_URL + '?session=' + encodeURIComponent(sid), { cache: 'no-store' })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (!d || !d.ok) {
          showNote(btn, ['Статистика недоступна: '
            + ((d && d.error) || 'нет данных') + '.']);
          return;
        }
        if (typeof d.context === 'number' && d.context < MIN_CONTEXT) {
          logInfo('отказ: контекст', d.context, '<', MIN_CONTEXT);
          showNote(btn, tooSmallLines(d));
          return;
        }
        setEnabled(true);
      })
      .catch(function () {
        showNote(btn, ['http-server.py недоступен (порт 18923).']);
      });
  }

  function mount(container) {
    var footer = container.querySelector('[class*="inputFooter_"]');
    if (!footer) return false;
    var anchor = findAnchor(footer);
    var btn = createButton(anchor.donor);
    // Порядок в футере: Usage · Cache · ByPass · автоправки. Опираемся
    // на соседа, а не на порядок инициализации модулей: React
    // пересоздаёт футер, и кто смонтируется первым — не гарантировано.
    // Встаём перед ByPass, если он уже есть, иначе перед автоправками
    // (тогда ByPass позже встанет справа от нас сам).
    var neighbour = footer.querySelector('.claude-bypass-btn');
    if (neighbour && neighbour.parentNode === footer) {
      footer.insertBefore(btn, neighbour);
    } else if (anchor.before) {
      footer.insertBefore(btn, anchor.before);
    } else {
      var spacer = footer.querySelector('[class*="spacer_"]');
      if (spacer && spacer.parentNode === footer) {
        footer.insertBefore(btn, spacer.nextSibling);
      } else {
        footer.appendChild(btn);
      }
    }
    return true;
  }

  /**
   * Держит порядок Usage · Cache · ByPass даже если кнопки появились
   * не в том порядке. Вставка при монтировании этого не гарантирует:
   * React пересоздаёт футер, модули домонтируются по своим таймерам,
   * и кто окажется первым — как повезёт.
   */
  function ensureOrder(footer) {
    var me = footer.querySelector('.' + BTN_CLASS);
    var right = footer.querySelector('.claude-bypass-btn');
    if (!me || !right) return;
    if (me.parentNode !== footer || right.parentNode !== footer) return;
    // DOCUMENT_POSITION_FOLLOWING — сосед идёт ПОСЛЕ нас, всё верно.
    if (!(me.compareDocumentPosition(right) & 4)) {
      footer.insertBefore(me, right);
      logInfo('порядок восстановлен: Cache перед ByPass');
    }
  }

  /**
   * Дочитывает сохранённое состояние, когда session id наконец
   * определился. При init() его ещё нет: резолвер работает по
   * React-fiber, а дерево на тот момент не отрисовано — раньше из-за
   * этого кнопка всегда стартовала выключенной, даже если сохранённое
   * состояние было.
   */
  function loadStateWhenReady() {
    if (stateLoaded) return;
    var value = loadEnabled();
    if (value === null) return;  // session id ещё не резолвится
    stateLoaded = true;
    if (value !== enabled) {
      enabled = value;
      applyStateAll();
    }
    logInfo('состояние восстановлено:', enabled ? 'включено' : 'выключено');
    if (enabled) tick();
  }

  function scan(ctx) {
    loadStateWhenReady();
    // Узлы даёт общий обход (см. DOM WATCH) — один на все модули.
    // Свой поиск остаётся для вызовов вне прохода: при регистрации
    // и из обработчиков самого модуля.
    var containers = (ctx && ctx.inputs)
      || document.querySelectorAll('[class*="inputContainer_"]');
    for (var i = 0; i < containers.length; i++) {
      // Страховка: если возврат черновика не удался с первого раза
      // (поле было занято, отправка сорвалась), подбираем его здесь.
      if (pendingDraft) restoreDraft(containers[i], 39);
      var footer = containers[i].querySelector('[class*="inputFooter_"]');
      if (containers[i].querySelector('.' + BTN_CLASS)) {
        if (footer) ensureOrder(footer);
        continue;
      }
      if (!containers[i].querySelector('[role="textbox"][contenteditable]')) continue;
      mount(containers[i]);
    }
  }

  function init() {
    // Состояние не читаем здесь: резолвер session id работает по
    // React-fiber, а дерево на этот момент ещё не отрисовано.
    // Этим занимается loadStateWhenReady() из scan(), как только
    // id станет доступен.
    normalizeWindow();
    // Наблюдатель и подстраховочный таймер — общие (см. DOM WATCH).
    // Регистрация делает первый скан сама, до applyStateAll: порядок
    // здесь тот же, что был у явного вызова.
    window.__claudeDomWatch.register('keepalive', scan);
    applyStateAll();
    pollConfig();
    setInterval(tick, TICK_MS);
    setInterval(pollConfig, CONFIG_POLL_MS);
    logInfo('installed, порог', IDLE_MINUTES, 'мин');
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();

/* ============================================================
 * ACCOUNT SWITCHER BUTTON
 *
 * Кнопка «Accs» в футере, слева от Usage. По клику открывается панель
 * со списком settings-файлов из ~/.claude/ — каждый такой файл это
 * аккаунт своего провайдера (Anthropic, Z.AI, ...). Выбор пункта
 * подменяет активный ~/.claude/settings.json копией выбранного.
 *
 * Вся работа с файлами — на стороне сервера (account_switcher.py),
 * здесь только список и клик: webview не имеет доступа к ФС.
 *
 * ВАЖНО про применение. `env` из settings.json процесс Claude Code
 * читает при старте, а стартует он один раз на активацию extension
 * host — поэтому смена провайдера НЕ действует на текущее окно сама
 * по себе. Панель не притворяется, что переключение уже подействовало:
 * молчаливая подмена под работающей сессией — худший из вариантов,
 * пользователь считал бы, что говорит с другой моделью.
 *
 * Вместо этого сразу после успешного переключения открывается модалка
 * с предложением перезапустить extension host (см. openModal). Это
 * дешевле Reload Window: окно, редакторы и терминалы остаются на месте.
 *
 * Управление: `accountsButton` и `accountsRestartPrompt`
 * в claude-custom-config.toml.
 * ============================================================ */
(function () {
  if (window.__claudeAccountsButtonInstalled) return;
  window.__claudeAccountsButtonInstalled = true;

  var cfg = window.__CLAUDE_CUSTOM_CONFIG__ || {};
  if (cfg.accountsButton !== true) return;

  var API_URL = 'http://localhost:18923/accounts';
  var RESTART_URL = 'http://localhost:18923/restart-exthost';
  var ENV_URL = 'http://localhost:18923/account-env';
  var BTN_CLASS = 'claude-accs-btn';
  var BARE_CLASS = 'claude-accs-btn-bare';
  var PANEL_ID = 'claude-accs-panel';
  var MODAL_ID = 'claude-accs-modal';
  // Список известных имён настроек для автодополнения в форме.
  // Панель открыта одна на окно, поэтому id фиксированный.
  var SET_LIST_ID = 'claude-accs-setting-names';
  var AUTO_EDIT_RE = /Править автоматически|Edit automatically/i;

  // Сколько ждём подтверждения от расширения. Наблюдатель в extension.js
  // опрашивает заявку раз в секунду, поэтому запас в четыре секунды
  // покрывает и медленный диск, и попадание в середину его цикла.
  var ACK_TIMEOUT_MS = 4000;
  var ACK_POLL_MS = 400;
  // Сколько ждать переоткрытия вкладки, прежде чем убрать модалку самим.
  // Рестарт хоста занимает ~3 с, оживление вкладки — ещё ~1.5 с; запас
  // взят на медленный старт расширения.
  var ACK_CLOSE_MS = 20000;

  // Отсутствие ключа трактуем как «включено»: bootstrap webview
  // перечитывается только при Reload Window, и на устаревшем bootstrap
  // предложение о перезапуске молча пропало бы — ровно та функция,
  // ради которой всё и делается.
  var restartPrompt = cfg.accountsRestartPrompt !== false;

  var panel = null;
  var modal = null;
  var switching = false;   // защита от двойного клика по пункту
  var restarting = false;  // заявка на перезапуск отправлена, ждём хост

  // Аккаунт, который был активен на момент отрисовки списка, — то есть
  // тот, на котором реально работает текущий процесс Claude Code.
  // Нужен для отката: отказ от перезапуска возвращает settings.json
  // к нему, иначе на диске остался бы один провайдер, а в работающей
  // сессии — другой.
  var activeFile = null;

  // Замыкание «отказаться», выставляемое openModal. Обработчик Escape
  // живёт на уровне модуля и о содержимом модалки не знает.
  var modalDecline = null;

  function logInfo() {
    if (!cfg.logs) return;
    try {
      console.log.apply(console, ['[accs-btn]'].concat([].slice.call(arguments)));
    } catch (e) {}
  }

  /* ---------- иконка ---------- */

  /**
   * Две фигурки — «аккаунты». Разметка повторяет штатные кнопки футера:
   * `<svg 20×20 fill="none">` + голый `<span>`, размер задаёт класс
   * footerButton_. Через createElementNS, а не innerHTML: SVG живёт
   * в своём namespace, а innerHTML упирается в Trusted Types.
   */
  function accsIcon() {
    var NS = 'http://www.w3.org/2000/svg';
    var svg = document.createElementNS(NS, 'svg');
    svg.setAttribute('width', '20');
    svg.setAttribute('height', '20');
    svg.setAttribute('viewBox', '0 0 20 20');
    svg.setAttribute('fill', 'none');
    function path(d) {
      var p = document.createElementNS(NS, 'path');
      p.setAttribute('d', d);
      p.setAttribute('fill', 'currentColor');
      svg.appendChild(p);
    }
    // Передняя фигура (голова + плечи) и приглушённый силуэт за ней.
    path('M8 10a2.6 2.6 0 100-5.2A2.6 2.6 0 008 10zm0 1.2c-2.4 0-4.4 1.3-4.4 2.9V16h8.8v-1.9c0-1.6-2-2.9-4.4-2.9z');
    var back = document.createElementNS(NS, 'path');
    back.setAttribute('d', 'M13.4 9.4a2.2 2.2 0 100-4.4 2.2 2.2 0 000 4.4zm.4 1.3c-.5 0-1 .06-1.45.17 1.1.72 1.8 1.78 1.8 2.99V16h3.05v-1.7c0-1.45-1.6-2.6-3.4-2.6z');
    back.setAttribute('fill', 'currentColor');
    back.setAttribute('opacity', '0.55');
    svg.appendChild(back);
    return svg;
  }

  /* ---------- панель ---------- */

  function closePanel() {
    if (!panel) return;
    if (panel.parentNode) panel.parentNode.removeChild(panel);
    panel = null;
    document.removeEventListener('mousedown', onOutside, true);
    document.removeEventListener('keydown', onKeydown, true);
  }

  function onOutside(e) {
    if (!panel) return;
    if (panel.contains(e.target)) return;
    // Клик по самой кнопке обрабатывает её собственный listener —
    // иначе панель закрылась бы здесь и тут же открылась заново.
    if (e.target.closest && e.target.closest('.' + BTN_CLASS)) return;
    closePanel();
  }

  function onKeydown(e) {
    if (e.key !== 'Escape') return;
    e.preventDefault();
    // Escape при открытой форме env закрывает сначала её: набирая
    // значение, промахнуться мимо клавиши легко, а закрытая панель
    // унесла бы с собой всю правку.
    var form = panel && panel.querySelector('.claude-accs-env');
    if (form && form.contains(document.activeElement)) {
      form.parentNode.removeChild(form);
      return;
    }
    closePanel();
  }

  function positionPanel(btn) {
    if (!panel) return;
    // Футер прижат к низу окна — раскрываемся вверх от кнопки.
    var r = btn.getBoundingClientRect();
    panel.style.bottom = Math.max(8, window.innerHeight - r.top + 6) + 'px';
    panel.style.left = Math.max(8, r.left) + 'px';
  }

  function renderError(body, text) {
    var div = document.createElement('div');
    div.className = 'claude-accs-empty';
    div.textContent = text;
    body.appendChild(div);
  }

  /** Строка аккаунта: имя + endpoint, активный помечен галкой. */
  function gearIcon() {
    var NS = 'http://www.w3.org/2000/svg';
    var svg = document.createElementNS(NS, 'svg');
    svg.setAttribute('width', '14');
    svg.setAttribute('height', '14');
    svg.setAttribute('viewBox', '0 0 16 16');
    svg.setAttribute('fill', 'none');
    var p = document.createElementNS(NS, 'path');
    // Зубцы (восемь выступов по кругу) и ось отверстия — вторым
    // контуром: fill-rule="evenodd" делает его дыркой, а не накладкой.
    p.setAttribute('d',
      'M9.1 1.5l.3 1.7c.4.14.78.34 1.12.58l1.6-.62 1.4 2.42-1.3 1.13c.03.2.05.4.05.61'
      + 's-.02.41-.05.61l1.3 1.13-1.4 2.42-1.6-.62c-.34.24-.72.44-1.12.58l-.3 1.7h-2.8'
      + 'l-.3-1.7a4.6 4.6 0 01-1.12-.58l-1.6.62-1.4-2.42 1.3-1.13a4.7 4.7 0 010-1.22'
      + 'L1.88 5.58l1.4-2.42 1.6.62c.34-.24.72-.44 1.12-.58l.3-1.7h2.8zM8 5.9'
      + 'a2.1 2.1 0 100 4.2 2.1 2.1 0 000-4.2z');
    p.setAttribute('fill', 'currentColor');
    p.setAttribute('fill-rule', 'evenodd');
    svg.appendChild(p);
    return svg;
  }

  /** Значок «?» с описанием ключа, стоящий перед крестиком удаления.
   *
   * Показывается только у знакомых ключей: у незнакомого он обещал бы
   * объяснение, которого у нас нет. Место под него держим всегда —
   * иначе крестики в соседних строках встали бы в разные колонки.
   * Текст берётся с сервера (`hints` в ответе /account-env), а не из
   * этого файла: правка описания там применяется перезапуском сервера,
   * а здесь потребовала бы Reload Window. */
  function hintMark(text) {
    var mark = document.createElement('span');
    mark.className = 'claude-accs-env-hint';
    mark.textContent = '?';
    setHint(mark, text);
    return mark;
  }

  function setHint(mark, text) {
    mark.title = text || '';
    mark.style.visibility = text ? 'visible' : 'hidden';
  }

  function delButton(row, title) {
    var del = document.createElement('button');
    del.type = 'button';
    del.className = 'claude-accs-env-del';
    del.textContent = '✕';
    del.title = title;
    del.addEventListener('mousedown', function (e) { e.preventDefault(); });
    del.addEventListener('click', function () {
      if (row.parentNode) row.parentNode.removeChild(row);
    });
    return del;
  }

  function envRow(list, key, value, hints) {
    var row = document.createElement('div');
    row.className = 'claude-accs-env-row';

    var keyInput = document.createElement('input');
    keyInput.type = 'text';
    keyInput.className = 'claude-accs-env-key';
    keyInput.value = key || '';
    keyInput.placeholder = 'ПЕРЕМЕННАЯ';
    keyInput.spellcheck = false;
    row.appendChild(keyInput);

    var valInput = document.createElement('input');
    valInput.type = 'text';
    valInput.className = 'claude-accs-env-val';
    valInput.value = value == null ? '' : String(value);
    valInput.placeholder = 'значение';
    valInput.spellcheck = false;
    row.appendChild(valInput);

    var mark = hintMark(hints && hints[key]);
    keyInput.addEventListener('input', function () {
      setHint(mark, hints && hints[keyInput.value.trim()]);
    });
    row.appendChild(mark);
    row.appendChild(delButton(row, 'Удалить переменную'));

    list.appendChild(row);
    return row;
  }

  /** Поле значения настройки — по типу из справочника.
   *
   * Переключатель и список вместо текстового поля не украшательство:
   * `switchModelsOnFlag` должен остаться булевым, а значение вне
   * enum'а расширение молча игнорирует, и настройка выглядит заданной,
   * не действуя. Текущее значение добавляется в список, даже если его
   * там нет: показать «max» как «low» значило бы соврать про файл. */
  function settingField(spec, value) {
    var kind = (spec && spec.type) || 'text';
    if (kind !== 'bool' && kind !== 'enum') {
      var input = document.createElement('input');
      input.type = 'text';
      input.className = 'claude-accs-env-val';
      input.value = value == null ? '' : String(value);
      input.placeholder = 'значение';
      input.spellcheck = false;
      return input;
    }

    var sel = document.createElement('select');
    sel.className = 'claude-accs-env-val claude-accs-env-sel';
    var options = kind === 'bool'
      ? [['true', 'да'], ['false', 'нет']]
      : (spec.options || []).map(function (o) { return [o, o]; });
    var current = kind === 'bool'
      ? (value === true || value === 'true' ? 'true' : 'false')
      : (value == null ? '' : String(value));
    var known = options.some(function (o) { return o[0] === current; });
    if (!known) options.unshift([current, current || '(не задано)']);
    for (var i = 0; i < options.length; i++) {
      var opt = document.createElement('option');
      opt.value = options[i][0];
      opt.textContent = options[i][1];
      sel.appendChild(opt);
    }
    sel.value = current;
    sel.dataset.kind = kind;
    return sel;
  }

  function settingRow(list, key, value, specs) {
    var row = document.createElement('div');
    row.className = 'claude-accs-env-row';

    var keyInput = document.createElement('input');
    keyInput.type = 'text';
    keyInput.className = 'claude-accs-env-key';
    keyInput.value = key || '';
    keyInput.placeholder = 'настройка';
    keyInput.spellcheck = false;
    keyInput.setAttribute('list', SET_LIST_ID);
    row.appendChild(keyInput);

    var wrap = document.createElement('span');
    wrap.className = 'claude-accs-env-field';
    wrap.appendChild(settingField(specs && specs[key], value));
    row.appendChild(wrap);

    var mark = hintMark(specs && specs[key] && specs[key].hint);
    keyInput.addEventListener('input', function () {
      var spec = specs && specs[keyInput.value.trim()];
      setHint(mark, spec && spec.hint);
      // Тип поля зависит от имени настройки, а его правят прямо здесь:
      // переименовали `model` в `verbose` — поле обязано стать
      // переключателем, иначе в файл уйдёт строка вместо булева.
      var kind = (spec && spec.type) || 'text';
      if (wrap.firstChild && (wrap.firstChild.dataset.kind || 'text') === kind) return;
      var old = wrap.firstChild ? wrap.firstChild.value : '';
      wrap.textContent = '';
      wrap.appendChild(settingField(spec, old));
    });
    row.appendChild(mark);
    row.appendChild(delButton(row, 'Удалить настройку'));

    list.appendChild(row);
    return row;
  }

  /** Собирает раздел из полей формы. Пустые имена пропускаем: строка,
   * добавленная кнопкой «+» и оставленная незаполненной, — это не ключ
   * с пустым именем, а просто передумали. */
  function collectRows(list, typed) {
    var out = {};
    var rows = list.querySelectorAll('.claude-accs-env-row');
    for (var i = 0; i < rows.length; i++) {
      var key = rows[i].querySelector('.claude-accs-env-key').value.trim();
      if (!key) continue;
      var field = rows[i].querySelector('.claude-accs-env-val');
      // Булево отправляем булевым, а не строкой «true»: сервер по
      // справочнику привёл бы и строку, но в файле аккаунта с чужой
      // настройкой гадать о типе было бы не по чему.
      out[key] = (typed && field.dataset.kind === 'bool')
        ? field.value === 'true'
        : field.value;
    }
    return out;
  }

  /** Форма правки настроек аккаунта под его строкой. */
  function openConfigEditor(item, acc, btn, body) {
    var open = item.querySelector('.claude-accs-env');
    if (open) {
      // Повторный клик по шестерёнке — закрыть. Правки при этом
      // теряются: они не сохранены, и делать вид, что сохранены, хуже.
      item.removeChild(open);
      positionPanel(btn);
      return;
    }

    var form = document.createElement('div');
    form.className = 'claude-accs-env';
    item.appendChild(form);

    var loading = document.createElement('div');
    loading.className = 'claude-accs-env-note';
    loading.textContent = 'загрузка…';
    form.appendChild(loading);
    positionPanel(btn);

    fetch(ENV_URL + '?file=' + encodeURIComponent(acc.file), { cache: 'no-store' })
      .then(function (res) { return res.json(); })
      .then(function (d) {
        if (!panel || !form.parentNode) return;
        if (!d || !d.ok) throw new Error((d && d.error) || 'нет данных');
        form.textContent = '';
        renderConfigForm(form, acc, d, btn, body);
        positionPanel(btn);
      })
      .catch(function (err) {
        if (!panel || !form.parentNode) return;
        form.textContent = '';
        var e = document.createElement('div');
        e.className = 'claude-accs-env-note claude-accs-env-err';
        e.textContent = 'Не удалось прочитать файл: ' + ((err && err.message) || err);
        form.appendChild(e);
        positionPanel(btn);
      });
  }

  /** Заголовок раздела формы. */
  function sectionHead(form, text) {
    var head = document.createElement('div');
    head.className = 'claude-accs-env-sec';
    head.textContent = text;
    form.appendChild(head);
  }

  function renderConfigForm(form, acc, data, btn, body) {
    var hints = data.hints || {};
    var envHints = hints.env || {};
    var setHints = hints.settings || {};
    var env = data.env || {};
    // Раздел настроек рисуем, только если сервер его прислал: webview
    // может работать с сервером, поднятым до этой правки, и пустая
    // секция означала бы «настроек нет», а не «сервер о них не знает».
    var settings = data.settings && typeof data.settings === 'object'
      ? data.settings : null;

    var head = document.createElement('div');
    head.className = 'claude-accs-env-head';
    head.textContent = acc.file;
    form.appendChild(head);

    var names, i;
    var setList = null;

    if (settings) {
      var datalist = document.createElement('datalist');
      datalist.id = SET_LIST_ID;
      names = Object.keys(setHints);
      for (i = 0; i < names.length; i++) {
        var opt = document.createElement('option');
        opt.value = names[i];
        datalist.appendChild(opt);
      }
      form.appendChild(datalist);

      sectionHead(form, 'настройки');
      setList = document.createElement('div');
      setList.className = 'claude-accs-env-list';
      form.appendChild(setList);

      names = Object.keys(settings);
      for (i = 0; i < names.length; i++) {
        settingRow(setList, names[i], settings[names[i]], setHints);
      }
      if (!names.length) settingRow(setList, '', '', setHints);
    }

    sectionHead(form, 'env · переменные окружения');
    var list = document.createElement('div');
    list.className = 'claude-accs-env-list';
    form.appendChild(list);

    names = Object.keys(env);
    for (i = 0; i < names.length; i++) {
      envRow(list, names[i], env[names[i]], envHints);
    }
    if (!names.length) envRow(list, '', '', envHints);

    var status = document.createElement('div');
    status.className = 'claude-accs-env-note';
    form.appendChild(status);

    var foot = document.createElement('div');
    foot.className = 'claude-accs-env-foot';
    form.appendChild(foot);

    var addSet = null;
    if (setList) {
      addSet = document.createElement('button');
      addSet.type = 'button';
      addSet.className = 'claude-accs-env-btn';
      addSet.textContent = '+ настройка';
      addSet.addEventListener('mousedown', function (e) { e.preventDefault(); });
      addSet.addEventListener('click', function () {
        var row = settingRow(setList, '', '', setHints);
        positionPanel(btn);
        try { row.querySelector('.claude-accs-env-key').focus(); } catch (e) {}
      });
      foot.appendChild(addSet);
    }

    var add = document.createElement('button');
    add.type = 'button';
    add.className = 'claude-accs-env-btn';
    add.textContent = '+ переменная';
    add.addEventListener('mousedown', function (e) { e.preventDefault(); });
    add.addEventListener('click', function () {
      var row = envRow(list, '', '', envHints);
      positionPanel(btn);
      try { row.querySelector('.claude-accs-env-key').focus(); } catch (e) {}
    });
    foot.appendChild(add);

    var save = document.createElement('button');
    save.type = 'button';
    save.className = 'claude-accs-env-btn claude-accs-env-save';
    save.textContent = 'Сохранить';
    save.addEventListener('mousedown', function (e) { e.preventDefault(); });
    save.addEventListener('click', function () {
      save.disabled = true;
      add.disabled = true;
      if (addSet) addSet.disabled = true;
      status.className = 'claude-accs-env-note';
      status.textContent = 'сохранение…';

      var payload = { file: acc.file, env: collectRows(list, false) };
      // Раздела нет — не отправляем его вовсе: пустой объект сервер
      // понял бы как «удалить все настройки».
      if (setList) payload.settings = collectRows(setList, true);

      fetch(ENV_URL, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      })
        .then(function (res) { return res.json(); })
        .then(function (d) {
          if (!panel || !form.parentNode) return;
          save.disabled = false;
          add.disabled = false;
          if (addSet) addSet.disabled = false;
          if (!d || !d.ok) {
            throw new Error((d && (d.message || d.error)) || 'сервер отказал');
          }
          logInfo('настройки сохранены:', acc.file);
          // Перерисовываем весь список: подпись строки (endpoint и
          // модель) только что изменилась, и оставлять старую нельзя.
          // Форма при этом закрывается вместе со старой разметкой,
          // поэтому итог показываем в подвале панели.
          renderAccounts(body, d.accounts, btn);
          var note = document.createElement('div');
          note.className = 'claude-accs-env-note';
          note.textContent = '✓ ' + (d.message || 'сохранено');
          body.appendChild(note);
          positionPanel(btn);
        })
        .catch(function (err) {
          if (!panel || !form.parentNode) return;
          save.disabled = false;
          add.disabled = false;
          if (addSet) addSet.disabled = false;
          status.className = 'claude-accs-env-note claude-accs-env-err';
          status.textContent = 'Не сохранено: ' + ((err && err.message) || err);
          positionPanel(btn);
        });
    });
    foot.appendChild(save);
  }

  /* ---------- лимиты подписки ---------- */

  /**
   * Полоски расхода лимитов claude.ai в строке аккаунта: пятичасовое
   * окно и недельное (у Max к ним добавляются окна по моделям).
   *
   * Данные приходят полем `usage` в самом аккаунте — сервер читает их
   * из кэша Claude Code (`cachedUsageUtilization` в ~/.claude.json).
   * Свежесть поэтому не наша: CLI обновляет кэш, пока работает на
   * OAuth-логине, и если сессия давно идёт через стороннего провайдера,
   * числа успевают состариться. Возраст записи есть в подсказке, а
   * после USAGE_STALE_SEC полоски ещё и приглушаются — молча показывать
   * вчерашние проценты как сегодняшние нельзя.
   *
   * Полоски лежат внутри строки-кнопки и клик не перехватывают:
   * строка целиком означает «переключиться на этот аккаунт», и
   * мёртвая зона в её правой половине была бы неожиданностью.
   */
  var showUsage = cfg.accountsUsageBars !== false;

  // Возраст данных, после которого блок считается устаревшим.
  // Полчаса: CLI обновляет кэш в каждой сессии, и запись старше
  // получаса означает, что на этом логине давно никто не работал.
  var USAGE_STALE_SEC = 1800;

  /** «1 ч 51 мин», «6 д», «меньше минуты» — сколько осталось. */
  function formatLeft(sec) {
    if (typeof sec !== 'number' || !isFinite(sec) || sec < 0) return '';
    if (sec < 60) return 'меньше минуты';
    var min = Math.floor(sec / 60) % 60;
    var hours = Math.floor(sec / 3600) % 24;
    var days = Math.floor(sec / 86400);
    if (days > 0) return days + ' д' + (hours ? ' ' + hours + ' ч' : '');
    if (hours > 0) return hours + ' ч' + (min ? ' ' + min + ' мин' : '');
    return min + ' мин';
  }

  /** «обновлены 4 мин назад» — насколько данным можно верить. */
  function formatAge(sec) {
    if (typeof sec !== 'number' || !isFinite(sec) || sec < 0) {
      return 'время обновления неизвестно';
    }
    if (sec < 90) return 'обновлены только что';
    return 'обновлены ' + formatLeft(sec) + ' назад';
  }

  // Атрибут-метка строки, которой полагаются полоски. Ставится по
  // acc.oauth, поэтому свежие числа есть куда положить даже тогда,
  // когда кэш пуст и при первой отрисовке полосок не было.
  var USAGE_HOST_ATTR = 'data-claude-usage-host';

  /**
   * Окна лимитов: ключ в ответе API, подпись полоски, полное название.
   *
   * Таблица повторяет USAGE_WINDOWS из account_switcher.py — числа
   * приходят двумя путями (кэш через сервер и живой ответ отсюда), и
   * одно и то же окно не должно называться в них по-разному. Меняете
   * здесь — правьте и там.
   */
  var USAGE_WINDOWS = [
    ['five_hour', '5 ч', 'Сессия (5 часов)'],
    ['seven_day', '7 дн', 'Неделя (7 дней)'],
    ['seven_day_opus', 'Opus', 'Неделя, Opus'],
    ['seven_day_sonnet', 'Sonnet', 'Неделя, Sonnet'],
  ];

  /**
   * Приводит `rate_limits` из ответа расширения к тому же виду, в
   * котором лимиты присылает сервер, — чтобы отрисовка была одна.
   */
  function windowsFromRateLimits(limits) {
    if (!limits || typeof limits !== 'object') return null;
    var windows = [];
    for (var i = 0; i < USAGE_WINDOWS.length; i++) {
      var key = USAGE_WINDOWS[i][0];
      var w = limits[key];
      if (!w || typeof w !== 'object') continue;
      var pct = w.utilization;
      if (typeof pct !== 'number' || !isFinite(pct)) continue;
      var left = null;
      if (typeof w.resets_at === 'string' && w.resets_at) {
        var at = Date.parse(w.resets_at);
        if (!isNaN(at)) left = Math.round((at - Date.now()) / 1000);
      }
      // То же правило, что на сервере: окно с прошедшим временем сброса
      // уже началось заново, и прежние проценты к нему не относятся
      // (см. anthropic_usage в account_switcher.py).
      var expired = left !== null && left <= 0;
      windows.push({
        key: key,
        label: USAGE_WINDOWS[i][1],
        title: USAGE_WINDOWS[i][2],
        percent: expired ? 0 : pct,
        resetsInSec: expired ? null : left,
        expired: expired,
      });
    }
    return windows.length ? { windows: windows, ageSec: 0 } : null;
  }

  /* ---------- свежие лимиты у самого расширения ---------- */

  /**
   * Сервер отдаёт лимиты из кэша, который ведёт CLI (`~/.claude.json`),
   * и обновляет он его, только когда его об этом просят: пока никто не
   * открывал «Аккаунт и расход», числа стареют. Прецедент (2026-09-01):
   * пятичасовое окно успело сброситься, штатное окно показывало 2%,
   * а панель — 33% из вчерашнего кэша.
   *
   * Поэтому при открытии панели мы просим свежие числа у самого
   * расширения — тем же вызовом `session.getUsage()`, которым их берёт
   * штатное окно «Account & Usage». Побочный эффект тут главный: CLI
   * при этом перезаписывает свой кэш, так что свежие числа достаются и
   * серверу, и всем остальным потребителям.
   *
   * Объект сессии достаём из React-fiber — тем же приёмом, что
   * `findSessionId` в CACHE USAGE BUTTON и `openConfig` в SETTINGS MENU
   * FIX. Признак нужного объекта — метод `getUsage`.
   */
  function fiberOfEl(el) {
    if (!el) return null;
    var keys = Object.keys(el);
    for (var i = 0; i < keys.length; i++) {
      if (keys[i].indexOf('__reactFiber') === 0) return el[keys[i]];
    }
    return null;
  }

  function hasUsageApi(o) {
    return !!o && typeof o === 'object' && typeof o.getUsage === 'function';
  }

  /** Ищет объект сессии в пропсах/состоянии одного fiber'а. */
  function usageHolderIn(obj) {
    if (!obj || typeof obj !== 'object') return null;
    if (hasUsageApi(obj)) return obj;
    var keys;
    try { keys = Object.keys(obj); } catch (e) { return null; }
    for (var i = 0; i < keys.length && i < 30; i++) {
      var c;
      try { c = obj[keys[i]]; } catch (e) { continue; }
      if (hasUsageApi(c)) return c;
      // Сессия бывает завёрнута в сигнал: значение лежит в `.value`.
      if (c && typeof c === 'object' && hasUsageApi(c.value)) return c.value;
    }
    return null;
  }

  function findUsageHolder() {
    var anchor = document.querySelector('[class*="inputContainer_"]')
      || document.getElementById('root')
      || document.body;
    var fiber = fiberOfEl(anchor);
    var hops = 0;
    while (fiber && hops < 80) {
      var found = usageHolderIn(fiber.memoizedProps)
        || usageHolderIn(fiber.memoizedState);
      if (found) return found;
      // Состояние функционального компонента — связный список хуков.
      var hook = fiber.memoizedState;
      var n = 0;
      while (hook && typeof hook === 'object' && n < 40) {
        var got = usageHolderIn(hook.memoizedState);
        if (got) return got;
        hook = hook.next;
        n++;
      }
      fiber = fiber.return;
      hops++;
    }
    return null;
  }

  /** Перерисовывает полоски во всех строках, которым они положены. */
  function applyUsage(body, usage) {
    var hosts = body.querySelectorAll('[' + USAGE_HOST_ATTR + ']');
    for (var i = 0; i < hosts.length; i++) {
      var host = hosts[i];
      var fresh = usageBlock(usage);
      var old = host.querySelector('.claude-accs-usage');
      if (!fresh) {
        if (old) host.removeChild(old);
        continue;
      }
      if (old) host.replaceChild(fresh, old);
      else host.appendChild(fresh);
    }
  }

  function markUsageBusy(body, busy) {
    var blocks = body.querySelectorAll('.claude-accs-usage');
    for (var i = 0; i < blocks.length; i++) {
      blocks[i].classList.toggle('claude-accs-usage-busy', !!busy);
    }
  }

  /**
   * Просит у расширения свежие лимиты и заменяет ими показанные из кэша.
   * Молчаливая: не вышло — остаются кэшированные числа, и подсказка
   * честно называет их возраст.
   */
  function refreshUsage(body) {
    if (!showUsage) return;
    if (!body.querySelector('[' + USAGE_HOST_ATTR + ']')) return;
    var holder = findUsageHolder();
    if (!holder) {
      logInfo('объект сессии с getUsage не найден — остаёмся на кэше');
      return;
    }
    var promise;
    try { promise = holder.getUsage(); } catch (e) { return; }
    if (!promise || typeof promise.then !== 'function') return;

    markUsageBusy(body, true);
    promise.then(function (res) {
      // Панель могли закрыть или перерисовать, пока шёл запрос.
      if (!panel || !body.isConnected) return;
      markUsageBusy(body, false);
      var usage = res && res.usage;
      var fresh = usage && windowsFromRateLimits(usage.rate_limits);
      if (!fresh) {
        logInfo('расширение не отдало лимиты — остаёмся на кэше');
        return;
      }
      applyUsage(body, fresh);
      logInfo('лимиты обновлены у расширения');
    }, function (err) {
      if (!panel || !body.isConnected) return;
      markUsageBusy(body, false);
      logInfo('getUsage отказал', err);
    });
  }

  function usageBlock(usage) {
    var wins = usage && usage.windows;
    if (!wins || !wins.length) return null;

    var box = document.createElement('span');
    box.className = 'claude-accs-usage';
    if (typeof usage.ageSec === 'number' && usage.ageSec > USAGE_STALE_SEC) {
      box.className += ' claude-accs-usage-stale';
    }

    var lines = [];
    for (var i = 0; i < wins.length; i++) {
      var w = wins[i] || {};
      var pct = typeof w.percent === 'number' && isFinite(w.percent) ? w.percent : 0;
      var shown = Math.max(0, Math.min(100, pct));

      var line = document.createElement('span');
      line.className = 'claude-accs-usage-row';
      // Уровень расхода четвертями: 0 — меньше четверти лимита,
      // 3 — больше трёх четвертей. Цвет шкалы и процента берётся из
      // него, поэтому строка красится целиком одним атрибутом.
      line.setAttribute('data-level',
        String(Math.min(3, Math.floor(shown / 25))));

      var label = document.createElement('span');
      // Подпись всегда белая: цветом говорит шкала, а подпись только
      // называет окно. Ключ окна в разметке остаётся — по нему видно,
      // какая шкала перед тобой, и на него можно опереться, если
      // палитру снова захочется сделать по-оконной.
      label.className = 'claude-accs-usage-label';
      if (w.key) label.setAttribute('data-window', w.key);
      label.textContent = w.label || '';
      line.appendChild(label);

      // У сброшенного окна отсчитывать нечего: время прошло, а нового
      // рубежа кэш не знает — его назовёт только следующий ответ API.
      var left = formatLeft(w.resetsInSec);
      var barText = w.expired ? 'сброшен' : left;
      var hint = w.expired
        ? ' · окно сброшено, свежих данных нет'
        : (left ? ' · сброс через ' + left : '');

      var track = document.createElement('span');
      track.className = 'claude-accs-usage-track';
      var fill = document.createElement('span');
      fill.className = 'claude-accs-usage-fill';
      // Цвет полоски задаёт CSS по ключу окна: держать палитру в JS
      // значило бы требовать Reload Window на каждый подбор оттенка.
      if (w.key) fill.setAttribute('data-window', w.key);
      fill.style.width = shown + '%';
      track.appendChild(fill);

      // Время до сброса — внутри шкалы, одним слоем и всегда одним
      // цветом (подобран на стенде usage-text-picker.html вместе с
      // цветом подложки). Менять цвет по границе заливки двумя слоями
      // нельзя: сглаживание глифов смешивается с нижним текстом, а не
      // с фоном, и буквы получают светлую кайму (прецедент 55.2).
      if (barText) {
        var when = document.createElement('span');
        when.className = 'claude-accs-usage-when';
        when.textContent = barText;
        track.appendChild(when);
      }
      line.appendChild(track);

      var val = document.createElement('span');
      val.className = 'claude-accs-usage-pct';
      val.textContent = Math.floor(shown) + '%';
      line.appendChild(val);

      box.appendChild(line);

      lines.push((w.title || w.label || '') + ' · ' + Math.floor(shown) + '%' + hint);
    }
    lines.push('Данные Claude Code, ' + formatAge(usage.ageSec));
    box.title = lines.join('\n');
    return box;
  }

  /**
   * Подпись под названием аккаунта.
   *
   * У логина claude.ai это тариф и почта: endpoint у него всегда
   * `api.anthropic.com`, одинаковый у любого такого аккаунта, и в
   * строке он не отличает его ни от чего. У аккаунта провайдера
   * наоборот — адрес и модель и есть всё различие.
   *
   * Тариф идёт первым, потому что он короткий: длинная почта уходит
   * под многоточие, и обрезаться должна именно она, а не он. Модель
   * показываем только когда она задана явно — пустой разделитель
   * выглядел бы как потерянное значение.
   */
  function accountSubtitle(acc) {
    // У логина claude.ai тариф и почта стоят в строке имени, а вторую
    // строку занимают полоски лимитов — endpoint там уже не нужен.
    // Но если ни тарифа, ни почты нет (файлы CLI не прочитались),
    // строка не должна оставаться пустой: показываем endpoint.
    if (acc.oauth && (acc.plan || acc.email)) return '';
    return acc.model ? acc.baseUrl + '  ·  ' + acc.model : acc.baseUrl;
  }

  /** «Pro (почта)» — приписка к названию OAuth-аккаунта. */
  function accountMeta(acc) {
    if (!acc.oauth) return '';
    var parts = [];
    if (acc.plan) parts.push(acc.plan);
    if (acc.email) parts.push('(' + acc.email + ')');
    return parts.join(' ');
  }

  function accountRow(acc, btn, body) {
    var item = document.createElement('div');
    item.className = 'claude-accs-item';

    var line = document.createElement('div');
    line.className = 'claude-accs-line';
    item.appendChild(line);

    var row = document.createElement('button');
    row.type = 'button';
    row.className = 'claude-accs-row'
      + (acc.isActive ? ' claude-accs-row-active' : '');

    var mark = document.createElement('span');
    mark.className = 'claude-accs-mark';
    mark.textContent = acc.isActive ? '✓' : '';
    row.appendChild(mark);

    var text = document.createElement('span');
    text.className = 'claude-accs-text';

    var name = document.createElement('span');
    name.className = 'claude-accs-name';
    var nameText = document.createElement('span');
    nameText.textContent = acc.name;
    name.appendChild(nameText);

    // Тариф и почта — приписка в той же строке, приглушённая: имя
    // аккаунта должно оставаться главным словом строки.
    var meta = accountMeta(acc);
    if (meta) {
      var metaEl = document.createElement('span');
      metaEl.className = 'claude-accs-meta';
      metaEl.textContent = meta;
      // Почта длинная и обрезается многоточием — под курсором должна
      // читаться целиком.
      metaEl.title = meta;
      name.appendChild(metaEl);
    }
    text.appendChild(name);

    var subtitle = accountSubtitle(acc);
    if (subtitle) {
      var sub = document.createElement('span');
      sub.className = 'claude-accs-sub';
      sub.textContent = subtitle;
      sub.title = subtitle;
      text.appendChild(sub);
    }

    // Лимиты приходят только у аккаунтов на OAuth-логине claude.ai —
    // у стороннего провайдера своих окон нет, и рисовать там нечего.
    // Помечаем по acc.oauth, а не по наличию acc.usage: кэша может не
    // быть вовсе, а место под свежие числа нужно всё равно.
    //
    // Полоски идут второй строкой внутри текстовой колонки, а не
    // справа от неё: почта занимает всю ширину строки имени, и справа
    // от неё места уже нет.
    if (showUsage && acc.oauth) {
      text.setAttribute(USAGE_HOST_ATTR, '1');
      var usage = usageBlock(acc.usage);
      if (usage) text.appendChild(usage);
    }

    row.appendChild(text);

    row.addEventListener('mousedown', function (e) { e.preventDefault(); });
    row.addEventListener('click', function (e) {
      e.preventDefault();
      e.stopPropagation();
      if (acc.isActive || switching) return;
      switchTo(acc.file, btn, body);
    });
    line.appendChild(row);

    var gear = document.createElement('button');
    gear.type = 'button';
    gear.className = 'claude-accs-gear';
    gear.title = 'Настройки и переменные окружения этого аккаунта';
    gear.appendChild(gearIcon());
    gear.addEventListener('mousedown', function (e) { e.preventDefault(); });
    gear.addEventListener('click', function (e) {
      // Без остановки клик дошёл бы до строки и переключил аккаунт —
      // а шестерёнка обещает совсем другое.
      e.preventDefault();
      e.stopPropagation();
      openConfigEditor(item, acc, btn, body);
    });
    line.appendChild(gear);

    return item;
  }

  function renderAccounts(body, accounts, btn) {
    body.textContent = '';

    var head = document.createElement('div');
    head.className = 'claude-accs-head';
    head.textContent = 'Аккаунт провайдера';
    body.appendChild(head);

    if (!accounts || !accounts.length) {
      renderError(body, 'В ~/.claude/ нет файлов settings*.json');
      return;
    }
    activeFile = null;
    for (var i = 0; i < accounts.length; i++) {
      if (accounts[i] && accounts[i].isActive) activeFile = accounts[i].file;
      body.appendChild(accountRow(accounts[i], btn, body));
    }

    var foot = document.createElement('div');
    foot.className = 'claude-accs-foot';
    foot.textContent = restartPrompt
      ? 'Смена требует перезапуска расширения — предложим сразу после выбора'
      : 'Смена применится после Developer: Reload Window';
    body.appendChild(foot);
  }

  /** Аккаунт из списка по имени файла (для заголовка модалки). */
  function findAccount(accounts, file) {
    for (var i = 0; accounts && i < accounts.length; i++) {
      if (accounts[i] && accounts[i].file === file) return accounts[i];
    }
    return null;
  }

  function switchTo(file, btn, body) {
    switching = true;
    // Запоминаем ДО запроса: после успешной подмены сервер вернёт уже
    // новый список, и узнать, откуда мы ушли, будет не по чему.
    var prevFile = activeFile;
    fetch(API_URL, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ file: file }),
    })
      .then(function (res) { return res.json(); })
      .then(function (d) {
        switching = false;
        if (!panel) return;
        if (d && d.ok) {
          logInfo('переключено на', file);
          if (restartPrompt) {
            // Панель закрываем: дальше разговор идёт в модалке, и
            // оставленный позади список только мешал бы — он всё равно
            // показывает состояние, которое ещё не применилось.
            var acc = findAccount(d.accounts, file);
            closePanel();
            openModal(acc, file, prevFile, findAccount(d.accounts, prevFile));
            return;
          }
          renderAccounts(body, d.accounts, btn);
          var note = document.createElement('div');
          note.className = 'claude-accs-note';
          note.textContent = d.message;
          body.appendChild(note);
        } else {
          renderError(body, (d && (d.message || d.error)) || 'не удалось переключить');
        }
        positionPanel(btn);
      })
      .catch(function (err) {
        switching = false;
        if (!panel) return;
        renderError(body, 'http-server.py недоступен (порт 18923)');
        logInfo('switch failed', err);
      });
  }

  /* ---------- модалка «перезапустить расширение?» ----------
   *
   * Аккаунт уже переключён — ~/.claude/settings.json подменён на диске.
   * Осталось применить: `env` оттуда читает процесс `claude` при старте,
   * а стартует он один раз на активацию extension host. Поэтому здесь
   * предлагается перезапуск хоста, а не Reload Window: окно, редакторы
   * и терминалы при этом остаются на месте.
   *
   * Сам перезапуск делает блок, инжектированный в extension.js
   * (patch-extension-csp.py). Webview лишь кладёт заявку через
   * POST /restart-exthost и следит за подтверждением — если его нет,
   * значит инжекция не активна, и об этом надо сказать прямо, а не
   * висеть в ожидании перезапуска, которого не будет.
   *
   * Отказ («Позже», Escape, клик мимо) НЕ просто закрывает окно:
   * он возвращает settings.json прежнему аккаунту. Иначе на диске
   * остался бы один провайдер, а в работающей сессии — другой, и
   * ближайшая посторонняя перезагрузка окна молча сменила бы модель.
   * Список после отказа снова показывает реально активный аккаунт.
   */

  function closeModal() {
    if (!modal) return;
    if (modal.parentNode) modal.parentNode.removeChild(modal);
    modal = null;
    modalDecline = null;
    document.removeEventListener('keydown', onModalKeydown, true);
  }

  function onModalKeydown(e) {
    if (e.key === 'Escape') {
      e.preventDefault();
      e.stopPropagation();
      if (modalDecline) modalDecline();
    }
  }

  function modalRow(parent, className, text) {
    var el = document.createElement('div');
    el.className = className;
    el.textContent = text;
    parent.appendChild(el);
    return el;
  }

  function openModal(acc, file, prevFile, prevAcc) {
    closeModal();

    modal = document.createElement('div');
    modal.id = MODAL_ID;
    modal.className = 'claude-accs-overlay';

    var box = document.createElement('div');
    box.className = 'claude-accs-modal';
    modal.appendChild(box);

    modalRow(box, 'claude-accs-modal-head', '✓ Аккаунт переключён');

    var card = document.createElement('div');
    card.className = 'claude-accs-modal-card';
    modalRow(card, 'claude-accs-modal-label', 'Активен');
    modalRow(card, 'claude-accs-modal-value', (acc && acc.name) || file);
    if (acc && acc.baseUrl) {
      modalRow(card, 'claude-accs-modal-sub',
        acc.model ? acc.baseUrl + '  ·  ' + acc.model : acc.baseUrl);
    }
    box.appendChild(card);

    modalRow(box, 'claude-accs-modal-text',
      'Настройки провайдера читает процесс Claude Code при запуске, '
      + 'поэтому в текущем окне пока работает прежний аккаунт. '
      + 'Чтобы применить смену, нужно перезапустить расширение.');

    modalRow(box, 'claude-accs-modal-warn',
      '⚠ Текущий диалог прервётся. Переписка сохранена на диске и '
      + 'откроется заново, но идущая задача будет остановлена. '
      + 'Редакторы, вкладки и терминалы останутся на месте.');

    var status = document.createElement('div');
    status.className = 'claude-accs-modal-status';
    box.appendChild(status);

    var footer = document.createElement('div');
    footer.className = 'claude-accs-modal-foot';
    box.appendChild(footer);

    var later = document.createElement('button');
    later.type = 'button';
    later.className = 'claude-accs-modal-btn claude-accs-modal-ghost';
    later.textContent = 'Позже';
    footer.appendChild(later);

    var go = document.createElement('button');
    go.type = 'button';
    go.className = 'claude-accs-modal-btn claude-accs-modal-primary';
    go.textContent = '⟳ Перезапустить расширение';
    footer.appendChild(go);

    /** Отказ: возвращаем прежний аккаунт и закрываем окно. */
    function decline() {
      // Заявка уже отправлена — откатывать поздно: хост вот-вот
      // перезапустится и подхватит новый settings.json.
      if (restarting) return;
      if (!prevFile || prevFile === file) {
        closeModal();
        return;
      }
      later.disabled = true;
      go.disabled = true;
      setStatus(status,
        'Возврат на ' + ((prevAcc && prevAcc.name) || prevFile) + '…', 'wait');

      fetch(API_URL, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        // revert говорит серверу, что переключения для пользователя не
        // было: процесс CLI как работал на прежнем аккаунте, так и
        // работает. Без этого признака история панели Usage объясняла
        // бы промахи кэша двумя переключениями, которых не случилось.
        body: JSON.stringify({ file: prevFile, revert: true }),
      })
        .then(function (res) { return res.json(); })
        .then(function (d) {
          if (!modal) return;
          if (d && d.ok) {
            logInfo('откат на', prevFile);
            closeModal();
            return;
          }
          throw new Error((d && (d.message || d.error)) || 'сервер отказал');
        })
        .catch(function (err) {
          if (!modal) return;
          // Молча закрыться нельзя: на диске остался новый аккаунт,
          // и пользователь должен знать, что откат не удался.
          later.disabled = false;
          go.disabled = false;
          later.textContent = 'Закрыть';
          setStatus(status,
            'Не удалось вернуть прежний аккаунт: '
            + ((err && err.message) || err)
            + '. На диске остался новый — выберите нужный в панели Accs.',
            'err');
          logInfo('revert failed', err);
        });
    }

    modalDecline = decline;
    later.addEventListener('click', function () {
      // После неудачного отката кнопка становится «Закрыть»: повторять
      // запрос, который только что провалился, смысла нет.
      if (later.textContent === 'Закрыть') closeModal();
      else decline();
    });
    go.addEventListener('click', function () {
      requestRestart(go, later, status);
    });

    document.body.appendChild(modal);
    document.addEventListener('keydown', onModalKeydown, true);
    try { go.focus(); } catch (e) {}

    // Клик мимо модалки = «Позже». Перезапуск — действие с потерями,
    // случайно запустить его мимо кнопки нельзя, а отложить — можно.
    modal.addEventListener('mousedown', function (e) {
      if (e.target === modal) decline();
    });
  }

  function setStatus(status, text, kind) {
    status.textContent = text;
    status.className = 'claude-accs-modal-status'
      + (kind ? ' claude-accs-modal-status-' + kind : '');
  }

  function requestRestart(go, later, status) {
    go.disabled = true;
    later.disabled = true;
    // С этого момента отказ запрещён: откатывать подмену бессмысленно,
    // хост вот-вот перезапустится и прочитает новый settings.json.
    restarting = true;
    setStatus(status, 'Заявка отправлена, ждём расширение…', 'wait');

    // Своё имя знает только сам webview: панель, созданную умершим
    // хостом, оживить нельзя, и новый хост о ней уже ничего не помнит.
    // Без sessionId он сможет вкладку только закрыть, а открыть заново
    // ту же переписку — нет.
    var sessionId = null;
    try {
      var resolve = window.__claudeSessionId;
      if (typeof resolve === 'function') sessionId = resolve();
    } catch (e) {
      logInfo('session id получить не удалось', e);
    }
    if (!sessionId) logInfo('session id неизвестен — вкладку переоткрыть будет нечем');

    fetch(RESTART_URL, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ sessionId: sessionId || '' }),
    })
      .then(function (res) { return res.json(); })
      .then(function (d) {
        if (!d || !d.ok) throw new Error((d && d.error) || 'сервер отказал');
        logInfo('заявка на перезапуск', d.token);
        waitForAck(go, later, status, Date.now());
      })
      .catch(function (err) {
        restarting = false;
        go.disabled = false;
        later.disabled = false;
        setStatus(status, 'http-server.py недоступен (порт 18923): '
          + ((err && err.message) || err), 'err');
        logInfo('restart request failed', err);
      });
  }

  /** Опрос подтверждения. Успех обычно не успевает отрисоваться —
   * расширение перезапускается и webview пересоздаётся. Ценность
   * опроса в обратном исходе: молчание значит, что блок в extension.js
   * не активен, и пользователю надо об этом сказать. */
  function waitForAck(go, later, status, startedAt) {
    if (!modal) return;
    if (Date.now() - startedAt > ACK_TIMEOUT_MS) {
      // Перезапуска не будет — значит откат снова имеет смысл.
      restarting = false;
      go.disabled = false;
      later.disabled = false;
      setStatus(status,
        'Расширение не ответило на заявку. Похоже, блок перезапуска '
        + 'в extension.js не активен — аккаунт переключён, но применить '
        + 'его придётся вручную: Developer: Reload Window.', 'err');
      return;
    }
    fetch(RESTART_URL, { cache: 'no-store' })
      .then(function (res) { return res.json(); })
      .then(function (d) {
        if (!modal) return;
        if (d && d.acked) {
          setStatus(status, 'Расширение принимает перезапуск…', 'ok');
          // Штатно эту модалку убирает не таймер, а сама вкладка: после
          // рестарта хоста расширение переоткрывает её, и DOM создаётся
          // с нуля. Таймер — на случай, когда переоткрытия не случилось:
          // модалка перекрывает окно и ввод, и висеть до конца сессии
          // она не должна. Откат отсюда уже запрещён (restarting), так
          // что закрытие безопасно — на диске нужный аккаунт.
          setTimeout(function () {
            if (modal) {
              logInfo('модалка закрыта по таймауту — вкладка не переоткрылась');
              closeModal();
            }
          }, ACK_CLOSE_MS);
          return;
        }
        setTimeout(function () {
          waitForAck(go, later, status, startedAt);
        }, ACK_POLL_MS);
      })
      .catch(function () {
        if (!modal) return;
        setTimeout(function () {
          waitForAck(go, later, status, startedAt);
        }, ACK_POLL_MS);
      });
  }

  function openPanel(btn) {
    closePanel();

    panel = document.createElement('div');
    panel.id = PANEL_ID;
    panel.className = 'claude-accs-panel';

    var body = document.createElement('div');
    body.className = 'claude-accs-body';
    body.textContent = 'загрузка…';
    panel.appendChild(body);
    document.body.appendChild(panel);
    positionPanel(btn);

    document.addEventListener('mousedown', onOutside, true);
    document.addEventListener('keydown', onKeydown, true);

    fetch(API_URL, { cache: 'no-store' })
      .then(function (res) { return res.json(); })
      .then(function (d) {
        if (!panel) return;
        body.textContent = '';
        if (d && d.ok) renderAccounts(body, d.accounts, btn);
        else renderError(body, (d && d.error) || 'нет данных');
        // Высота стала известна только сейчас — переставляем, иначе
        // длинный список уедет за верхний край окна.
        positionPanel(btn);
        // Кэшированные числа уже на экране; свежие догоняют их через
        // расширение (и заодно обновляют кэш для остальных).
        refreshUsage(body);
      })
      .catch(function (err) {
        if (!panel) return;
        body.textContent = '';
        renderError(body, 'http-server.py недоступен (порт 18923)');
        logInfo('fetch failed', err);
      });
  }

  /* ---------- встраивание ---------- */

  /** См. одноимённую функцию в CACHE USAGE BUTTON — логика та же. */
  function findAnchor(footer) {
    var buttons = footer.querySelectorAll('button');
    var match = null;
    for (var i = 0; i < buttons.length; i++) {
      if (AUTO_EDIT_RE.test(buttons[i].textContent || '')) {
        match = buttons[i];
        break;
      }
    }
    if (match) {
      var node = match;
      while (node.parentNode && node.parentNode !== footer) node = node.parentNode;
      return {
        before: node.parentNode === footer ? node : null,
        donor: match,
      };
    }
    return {
      before: null,
      donor: footer.querySelector('[class*="footerButton_"]')
        || footer.querySelector('button'),
    };
  }

  function createButton(donor) {
    var btn = document.createElement('button');
    btn.type = 'button';
    var donorClass = donor && typeof donor.className === 'string'
      ? donor.className : '';
    if (donorClass) {
      btn.className = donorClass + ' ' + BTN_CLASS;
    } else {
      btn.className = BTN_CLASS + ' ' + BARE_CLASS;
    }
    btn.appendChild(accsIcon());
    var label = document.createElement('span');
    label.textContent = 'Accs';
    btn.appendChild(label);
    btn.title = 'Аккаунты провайдеров: подменить ~/.claude/settings.json';
    btn.addEventListener('mousedown', function (e) { e.preventDefault(); });
    btn.addEventListener('click', function (e) {
      e.preventDefault();
      e.stopPropagation();
      if (panel) closePanel();
      else openPanel(btn);
    });
    return btn;
  }

  function mount(container) {
    var footer = container.querySelector('[class*="inputFooter_"]');
    if (!footer) return false;
    var anchor = findAnchor(footer);
    var btn = createButton(anchor.donor);
    // Порядок в футере: Accs · Usage · Cache · ByPass · автоправки.
    // Опираемся на соседа, а не на порядок инициализации модулей:
    // React пересоздаёт футер, и кто смонтируется первым — не
    // гарантировано. Встаём перед самым левым из уже существующих.
    var neighbour = footer.querySelector('.claude-usage-btn')
      || footer.querySelector('.claude-cache-btn')
      || footer.querySelector('.claude-bypass-btn');
    if (neighbour && neighbour.parentNode === footer) {
      footer.insertBefore(btn, neighbour);
    } else if (anchor.before) {
      footer.insertBefore(btn, anchor.before);
    } else {
      var spacer = footer.querySelector('[class*="spacer_"]');
      if (spacer && spacer.parentNode === footer) {
        footer.insertBefore(btn, spacer.nextSibling);
      } else {
        footer.appendChild(btn);
      }
    }
    return true;
  }

  /**
   * Держит Accs левее соседей, даже если те смонтировались позже.
   * Вставка при монтировании этого не гарантирует — см. ensureOrder
   * в CACHE KEEPALIVE.
   */
  function ensureOrder(footer) {
    var me = footer.querySelector('.' + BTN_CLASS);
    if (!me || me.parentNode !== footer) return;
    var right = footer.querySelector('.claude-usage-btn')
      || footer.querySelector('.claude-cache-btn')
      || footer.querySelector('.claude-bypass-btn');
    if (!right || right.parentNode !== footer) return;
    // DOCUMENT_POSITION_FOLLOWING — сосед идёт ПОСЛЕ нас, всё верно.
    if (!(me.compareDocumentPosition(right) & 4)) {
      footer.insertBefore(me, right);
      logInfo('порядок восстановлен: Accs перед соседями');
    }
  }

  function scan(ctx) {
    // Узлы даёт общий обход (см. DOM WATCH) — один на все модули.
    // Свой поиск остаётся для вызовов вне прохода: при регистрации
    // и из обработчиков самого модуля.
    var containers = (ctx && ctx.inputs)
      || document.querySelectorAll('[class*="inputContainer_"]');
    for (var i = 0; i < containers.length; i++) {
      var footer = containers[i].querySelector('[class*="inputFooter_"]');
      if (containers[i].querySelector('.' + BTN_CLASS)) {
        if (footer) ensureOrder(footer);
        continue;
      }
      if (!containers[i].querySelector('[role="textbox"][contenteditable]')) continue;
      mount(containers[i]);
    }
  }

  function init() {
    // Наблюдатель и подстраховочный таймер — общие (см. DOM WATCH).
    window.__claudeDomWatch.register('accounts', scan);
    logInfo('installed');
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();

/* ============================================================
 * LIMIT ALERT STOP BUTTON
 *
 * Плавающая круглая кнопка-плашка с иконкой «стоп», висит по центру
 * НАД панелью ввода (position: fixed, как панель Accs). Появляется
 * ТОЛЬКО пока играет звук уведомления о сбросе 5-часового лимита
 * (монитор limit_alert.py запускает ffplay; состояние — поле playing
 * в GET /limit-reset-alert) и исчезает сама, когда звук закончился
 * или остановлен. Клик — POST /limit-reset-alert-stop.
 *
 * Почему кнопка живёт по состоянию, а не постоянно: останавливать
 * нечего почти всегда — окно сбрасывается раз в часы, и постоянная
 * кнопка обещала бы действие впустую. Отдельного TOML-ключа модулю
 * не нужно: пока не сыграет звук (который включает limitResetAlert),
 * он не делает ровно ничего.
 *
 * Вид плашки — в claude-custom.css (`.claude-limit-stop-float`),
 * иконка — SVG через createElementNS (Trusted Types, приём accsIcon).
 * Опрос раз в секунду — статус сервера, не DOM-scan: общий наблюдатель
 * (DOM WATCH) здесь ради пересчёта позиции, когда React пересоздал
 * панель ввода.
 * ============================================================ */
(function () {
  if (window.__claudeLimitStopInstalled) return;
  window.__claudeLimitStopInstalled = true;

  var STATUS_URL = 'http://localhost:18923/limit-reset-alert';
  var STOP_URL = 'http://localhost:18923/limit-reset-alert-stop';
  var BTN_CLASS = 'claude-limit-stop-float';
  var POLL_MS = 1000;
  // Три промаха подряд считаются «не играет»: сервер, который не
  // отвечает, звуком не управляет, и плашка убирается до лучших
  // времён, а не висит мёртвой.
  var FAIL_TOLERANCE = 3;

  var playing = false;
  var fails = 0;
  var btn = null; // плашка одна на окно, над панелью активной вкладки

  function stopIcon() {
    var NS = 'http://www.w3.org/2000/svg';
    var svg = document.createElementNS(NS, 'svg');
    svg.setAttribute('width', '14');
    svg.setAttribute('height', '14');
    svg.setAttribute('viewBox', '0 0 16 16');
    var rect = document.createElementNS(NS, 'rect');
    rect.setAttribute('x', '4');
    rect.setAttribute('y', '4');
    rect.setAttribute('width', '8');
    rect.setAttribute('height', '8');
    rect.setAttribute('rx', '1.5');
    rect.setAttribute('fill', 'currentColor');
    svg.appendChild(rect);
    return svg;
  }

  function inputContainer() {
    // Самый нижний видимый контейнер поля ввода — панель активной
    // вкладки. Полей ввода бывает несколько (правая панель, вкладки),
    // плашка одна и должна висеть над тем, что сейчас перед глазами.
    var all = document.querySelectorAll('[class*="inputContainer_"]');
    var best = null;
    var bestBottom = 0;
    for (var i = 0; i < all.length; i++) {
      var r = all[i].getBoundingClientRect();
      if (r && r.height > 0 && r.bottom > bestBottom) {
        best = all[i];
        bestBottom = r.bottom;
      }
    }
    return best;
  }

  function position() {
    if (!btn) return;
    var c = inputContainer();
    if (!c) return;
    var r = c.getBoundingClientRect();
    // Центр панели по горизонтали (translateX(-50%) в CSS доворачивает
    // плашку серединой к точке), снизу — чуть выше верхнего края.
    btn.style.bottom = Math.max(8, window.innerHeight - r.top + 10) + 'px';
    btn.style.left = (r.left + r.width / 2) + 'px';
  }

  function mount() {
    if (btn) {
      position();
      return;
    }
    btn = document.createElement('button');
    btn.type = 'button';
    btn.className = BTN_CLASS;
    btn.__claudeOwnNode = true;
    btn.title = 'Остановить звук уведомления о сбросе лимита';
    btn.appendChild(stopIcon());
    btn.addEventListener('mousedown', function (e) { e.preventDefault(); });
    btn.addEventListener('click', function (e) {
      e.preventDefault();
      e.stopPropagation();
      // Не ждём следующего опроса: звук гасится сейчас — плашка
      // должна уйти сразу, а не через секунду.
      fetch(STOP_URL, { method: 'POST' })
        .then(poll)
        .catch(poll);
    });
    document.body.appendChild(btn);
    position();
  }

  function unmount() {
    if (btn && btn.parentNode) btn.parentNode.removeChild(btn);
    btn = null;
  }

  function scan() {
    // Общий наблюдатель (DOM WATCH) зовёт скан на мутации — панель
    // ввода могла пересоздаться, плашка должна остаться над ней.
    if (playing) mount();
    else unmount();
  }

  function poll() {
    fetch(STATUS_URL)
      .then(function (r) { return r.json(); })
      .then(function (d) {
        fails = 0;
        var now = !!(d && d.playing);
        if (now !== playing) {
          playing = now;
          scan();
        } else if (now) {
          // Панель ввода могла переехать (смена вкладки, ресайз) —
          // пересчёт дешевле, чем промахнувшаяся плашка.
          position();
        }
      })
      .catch(function () {
        fails++;
        if (fails >= FAIL_TOLERANCE && playing) {
          playing = false;
          scan();
        }
      });
  }

  function init() {
    window.__claudeDomWatch.register('limit-stop', scan);
    window.addEventListener('resize', position);
    setInterval(poll, POLL_MS);
    poll();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();

/* ============================================================
 * MOOD GAUGE
 *
 * Индикатор «Mood» в футере поля ввода, слева от кнопки Accs.
 * Полукруглая шкала из четырёх равных секторов — красный, оранжевый,
 * жёлтый, зелёный — и стрелка поверх неё: 0 указывает в начало
 * красного (влево), 100 — в конец зелёного (вправо).
 *
 * ЧТО ИЗМЕРЯЕТ. Как часто теряется живой prompt-кэш. Промах после
 * долгой паузы неизбежен — TTL кончился, платить за перезапись
 * пришлось бы в любом случае. А промах, когда с прошлого хода прошло
 * меньше TTL, означает потерю на ровном месте: кэш должен был быть
 * жив. Считается доля таких ходов: ни одного — зелёная зона, потери
 * изредка — жёлтая, каждый третий-второй ход — оранжевая, кэш не
 * доживает почти никогда — красная.
 *
 * Доля, а не счёт: в длинной сессии три потери на сотню ходов и три
 * подряд на четыре хода — совершенно разные истории, а по одному
 * счётчику неразличимы.
 *
 * ВТОРОЙ ФАКТОР — раскрывалось ли контекстное окно. Сессия, где
 * контекст хотя бы раз поднимался выше `moodContextGoal`, сдвигает
 * стрелку к зелёному, а та, что так и не дотянула, — к красному.
 * Это поправка к основной метрике, а не отдельная шкала: сдвиг
 * фиксированный, порядка половины сектора. Пока ходов меньше
 * MIN_CHANCES, фактор не применяется вовсе — окно свежей сессии
 * просто не успело вырасти, и штрафовать за это не за что.
 *
 * Данные — GET /cache-usage (тот же endpoint, что у кнопки Usage):
 * `early_misses` — ходы, потерявшие живой кэш, `early_chances` — ходы,
 * на которых он вообще мог сработать. Сравнение пауз с TTL делает
 * сервер: у него полная история промахов, а в `miss_log` для UI
 * приходит только хвост из двадцати последних.
 *
 * Значение можно поставить и вручную — `window.__claudeMood.set(0..100)`,
 * читается `.get()`; следующий опрос (`moodPollSec`) его перебьёт.
 * Это отладочный вход, а не способ показать что-то своё.
 *
 * Не кнопка: кликов не принимает и курсор не меняет. Поэтому класс
 * штатной footerButton_ не заимствуется (в отличие от Accs и Usage) —
 * hover-подсветка на неинтерактивном элементе обещала бы действие,
 * которого нет.
 *
 * Цвета секторов, толщина дуги и размер живут в claude-custom.css
 * (`.claude-mood-arc-1..4`), а не в атрибутах SVG: CSS перечитывается
 * горячо, поэтому палитру можно править без Reload Window.
 *
 * Управление: `moodGauge` в claude-custom-config.toml.
 * ============================================================ */
(function () {
  if (window.__claudeMoodGaugeInstalled) return;
  window.__claudeMoodGaugeInstalled = true;

  var cfg = window.__CLAUDE_CUSTOM_CONFIG__ || {};
  if (cfg.moodGauge !== true) return;

  var ROOT_CLASS = 'claude-mood';
  var NEEDLE_CLASS = 'claude-mood-needle';
  var NODATA_CLASS = 'claude-mood-nodata';

  var USAGE_URL = 'http://localhost:18923/cache-usage';
  var POLL_MS = (typeof cfg.moodPollSec === 'number' && cfg.moodPollSec > 0
    ? cfg.moodPollSec : 20) * 1000;

  // Сколько неудачных опросов подряд терпим, прежде чем признать, что
  // данных нет. Одиночный сбой (сервер перезапускается хуком, вкладка
  // только что открылась) не должен гасить шкалу: мигание серым
  // читалось бы как поломка индикатора.
  var FAIL_TOLERANCE = 3;

  // Позиция при нулевых потерях — середина зелёного сектора (75..100).
  // Не 100: стрелка, упёршаяся в край шкалы, выглядит сломанной.
  var VALUE_CLEAN = 94;

  // Диапазон, в котором живут все остальные исходы. Верх — почти
  // граница с зелёным: даже единственная потеря на сотню ходов
  // не должна выглядеть как «потерь нет». Низ — почти край красного,
  // это случай «кэш не доживает ни разу».
  var VALUE_LOSS_TOP = 74;
  var VALUE_LOSS_BOTTOM = 2;

  // Верхний предел с учётом поправки за контекст: у нулевых потерь
  // база и так почти у края, а прибавка не должна упирать стрелку
  // в самый конец шкалы.
  var VALUE_MAX = 98;

  // Минимальный знаменатель доли. Без него первая же потеря в начале
  // сессии давала бы «1 из 1» — сто процентов и красную зону, хотя
  // одна точка ещё ничего не говорит о том, как поведёт себя кэш
  // дальше. С ростом сессии ограничение перестаёт действовать само.
  // Этот же порог решает, созрела ли сессия для поправки за контекст.
  var MIN_CHANCES = 10;

  // Отметка, выше которой контекстное окно считается раскрывшимся.
  var CONTEXT_GOAL = typeof cfg.moodContextGoal === 'number'
    && cfg.moodContextGoal > 0 ? cfg.moodContextGoal : 250000;

  // Насколько поправка за контекст двигает стрелку — примерно
  // полсектора. Больше значило бы, что второй фактор перебивает
  // главный: потери кэша важнее того, докуда доросло окно.
  var CONTEXT_SHIFT = 12;

  // Геометрия в единицах viewBox. Центр шкалы внизу, дуга — верхняя
  // половина окружности радиуса R: 180° слева (значение 0) до 0°
  // справа (значение 100). Габариты viewBox с запасом на толщину дуги.
  var VIEW_W = 34;
  var VIEW_H = 20;
  var CX = 17;
  var CY = 17;
  var R = 13;
  var SECTORS = 4;

  // Стрелка не достаёт до дуги: на краях шкалы остриё иначе сливается
  // с сектором и перестаёт читаться.
  var NEEDLE_LEN = R - 2.2;
  var NEEDLE_HALF_W = 1.65;  // полуширина у основания
  var HUB_R = 2.1;

  // Значение общее для всех экземпляров: футеров в DOM столько,
  // сколько открытых полей ввода, и показывать в них разное было бы
  // враньём — метрика одна на окно.
  var value = VALUE_CLEAN;

  // До первого удачного опроса шкала показана серой: цветная шкала
  // с зелёной стрелкой утверждала бы «всё хорошо», хотя на деле мы
  // ещё ничего не знаем.
  var haveData = false;
  var fails = 0;
  var detail = 'данных ещё нет';

  function logInfo() {
    if (!cfg.logs) return;
    try {
      console.log.apply(console, ['[mood]'].concat([].slice.call(arguments)));
    } catch (e) {}
  }

  /* ---------- рисование ---------- */

  function round3(n) { return Math.round(n * 1000) / 1000; }

  /** Точка на дуге по углу в градусах (0° — справа, 180° — слева). */
  function polar(deg) {
    var rad = deg * Math.PI / 180;
    return { x: CX + R * Math.cos(rad), y: CY - R * Math.sin(rad) };
  }

  /**
   * Дуга i-го сектора. sweep=1 — по часовой стрелке на экране:
   * угол убывает, а ось Y в SVG направлена вниз.
   */
  function arcPath(i) {
    var step = 180 / SECTORS;
    var from = polar(180 - i * step);
    var to = polar(180 - (i + 1) * step);
    return 'M ' + round3(from.x) + ' ' + round3(from.y)
      + ' A ' + R + ' ' + R + ' 0 0 1 ' + round3(to.x) + ' ' + round3(to.y);
  }

  /**
   * SVG-шкала. Через createElementNS, а не innerHTML: SVG живёт
   * в своём namespace, а innerHTML упирается в Trusted Types
   * (тот же приём, что в accsIcon у ACCOUNT SWITCHER BUTTON).
   */
  function createSvg() {
    var NS = 'http://www.w3.org/2000/svg';
    var svg = document.createElementNS(NS, 'svg');
    svg.setAttribute('viewBox', '0 0 ' + VIEW_W + ' ' + VIEW_H);
    svg.setAttribute('fill', 'none');
    svg.setAttribute('class', 'claude-mood-svg');
    // Для скринридера шкала — декорация: значение и без неё есть
    // в title всего индикатора.
    svg.setAttribute('aria-hidden', 'true');

    for (var i = 0; i < SECTORS; i++) {
      var arc = document.createElementNS(NS, 'path');
      arc.setAttribute('d', arcPath(i));
      arc.setAttribute('class', 'claude-mood-arc claude-mood-arc-' + (i + 1));
      svg.appendChild(arc);
    }

    // Стрелка нарисована в положении «0» — остриём влево; значение
    // задаётся поворотом всей группы вокруг центра (см. applyTo).
    var needle = document.createElementNS(NS, 'g');
    needle.setAttribute('class', NEEDLE_CLASS);

    var pointer = document.createElementNS(NS, 'path');
    pointer.setAttribute('d',
      'M ' + round3(CX - NEEDLE_LEN) + ' ' + CY
      + ' L ' + CX + ' ' + round3(CY - NEEDLE_HALF_W)
      + ' L ' + CX + ' ' + round3(CY + NEEDLE_HALF_W) + ' Z');
    pointer.setAttribute('class', 'claude-mood-pointer');
    needle.appendChild(pointer);

    var hub = document.createElementNS(NS, 'circle');
    hub.setAttribute('cx', CX);
    hub.setAttribute('cy', CY);
    hub.setAttribute('r', HUB_R);
    hub.setAttribute('class', 'claude-mood-hub');
    needle.appendChild(hub);

    svg.appendChild(needle);
    return svg;
  }

  /**
   * Индикатор — кнопка, как Accs и Usage: заимствует класс штатной
   * `footerButton_`, поэтому размеры, шрифт и подсветка при наведении
   * достаются даром и совпадают с соседями.
   *
   * Раньше он был неинтерактивным `div`: обещать действие, которого
   * нет, было бы неправдой. Действие появилось — окно истории, —
   * и вид приведён в соответствие.
   */
  function createGauge(donor) {
    var root = document.createElement('button');
    root.type = 'button';
    var donorClass = donor && typeof donor.className === 'string'
      ? donor.className : '';
    root.className = donorClass
      ? donorClass + ' ' + ROOT_CLASS
      : ROOT_CLASS + ' ' + ROOT_CLASS + '-bare';
    root.appendChild(createSvg());
    var label = document.createElement('span');
    label.className = 'claude-mood-label';
    label.textContent = 'Mood';
    root.appendChild(label);
    // Без preventDefault фокус уходит из composer'а: пользователь
    // теряет позицию каретки просто посмотрев историю.
    root.addEventListener('mousedown', function (e) { e.preventDefault(); });
    root.addEventListener('click', function (e) {
      e.preventDefault();
      e.stopPropagation();
      if (panel) closePanel();
      else openPanel(root);
    });
    applyTo(root);
    return root;
  }

  /* ---------- значение ---------- */

  /** Ставит стрелку и подсказку одному экземпляру индикатора. */
  function applyTo(root) {
    var needle = root.querySelector('.' + NEEDLE_CLASS);
    if (needle) {
      // CSS-свойство transform, а не одноимённый атрибут SVG: только
      // по нему работает transition (см. .claude-mood-needle в CSS).
      needle.style.transform = 'rotate(' + round3(value * 180 / 100) + 'deg)';
    }
    if (haveData) root.classList.remove(NODATA_CLASS);
    else root.classList.add(NODATA_CLASS);
    // Подсказка многострочная: факторов уже два, и в одну строку
    // они склеивались бы в нечитаемую ленту.
    root.title = haveData
      ? 'Mood: ' + Math.round(value) + ' / 100\n' + detail
      : 'Mood: нет данных · ' + detail;
  }

  // Слушатели значения — сейчас это INPUT RING, красящий обводку поля
  // ввода. Подписка, а не опрос: значение меняется раз в moodPollSec,
  // и таймер у подписчика почти всегда заставал бы его прежним.
  var listeners = [];

  function notify() {
    for (var i = 0; i < listeners.length; i++) {
      // Падение подписчика не должно ронять обновление самой шкалы:
      // здесь мы уже посреди её отрисовки.
      try { listeners[i](); } catch (e) {}
    }
  }

  function applyAll() {
    var roots = document.querySelectorAll('.' + ROOT_CLASS);
    for (var i = 0; i < roots.length; i++) applyTo(roots[i]);
    notify();
  }

  window.__claudeMood = {
    /**
     * Ставит значение 0..100 вручную; выходящее за границы прижимается.
     * Отладочный вход: ближайший опрос перезапишет значение своим.
     */
    set: function (v) {
      var num = Number(v);
      if (!isFinite(num)) return value;
      value = Math.max(0, Math.min(100, num));
      haveData = true;
      detail = 'значение поставлено вручную';
      applyAll();
      logInfo('значение', value);
      return value;
    },
    get: function () { return value; },
    /**
     * Номер сектора шкалы под стрелкой: 0 — красный, 3 — зелёный.
     * null означает «данных ещё нет» — тот же случай, в котором сама
     * шкала обесцвечивается. Отдаём номер, а не цвет: палитра живёт
     * в CSS и правится горячо, а вторая её копия в JS разошлась бы
     * с первой на ближайшей правке.
     */
    level: function () {
      if (!haveData) return null;
      return Math.max(0, Math.min(SECTORS - 1, Math.floor(value / (100 / SECTORS))));
    },
    /** Подписка на смену значения; вызывается после отрисовки шкалы. */
    onChange: function (fn) {
      if (typeof fn === 'function') listeners.push(fn);
    },
    /** Внеочередной опрос — например после правки TTL в конфиге. */
    refresh: function () { tick(); },
  };

  /* ---------- метрика: ранние потери кэша ---------- */

  function sessionId() {
    // Резолвер регистрирует CACHE USAGE BUTTON — он же общая
    // зависимость кнопок Cache и ByPass. Без id сервер отдал бы самый
    // свежий транскрипт проекта, то есть чужую вкладку.
    var fn = window.__claudeSessionId;
    return typeof fn === 'function' ? fn() : null;
  }

  function plural(n, one, few, many) {
    var mod10 = n % 10;
    var mod100 = n % 100;
    if (mod10 === 1 && mod100 !== 11) return one;
    if (mod10 >= 2 && mod10 <= 4 && (mod100 < 12 || mod100 > 14)) return few;
    return many;
  }

  function num(v) { return typeof v === 'number' && isFinite(v) ? v : null; }

  /**
   * Ходы, потерявшие живой кэш, и ходы, на которых он мог сработать.
   *
   * Обычно оба числа приходят с сервера готовыми. Запасной путь —
   * пересчёт по `miss_log`: он нужен, пока в окне работает webview,
   * загруженный до обновления сервера. Там только двадцать последних
   * промахов, так что доля выйдет заниженной — но заниженная оценка
   * на несколько минут лучше серой шкалы.
   */
  function earlyStats(data) {
    var ttl = num(data.ttl_minutes) > 0 ? data.ttl_minutes : 60;
    var early = num(data.early_misses);
    var chances = num(data.early_chances);
    if (early === null || chances === null) {
      var log = data.miss_log || [];
      early = 0;
      for (var i = 0; i < log.length; i++) {
        var gap = num(log[i].gap);
        // Промах без разобранной метки времени пропускаем: неизвестную
        // паузу не с чем сравнить, а записать её в потери — соврать.
        if (gap !== null && gap < ttl) early++;
      }
      chances = Math.max((num(data.requests) || 1) - 1, 0);
    }
    return {
      early: early,
      chances: chances,
      ttl: ttl,
      // Пика может не быть у старого сервера — тогда поправку за
      // контекст просто не применяем (см. valueFor).
      peak: num(data.context_peak),
    };
  }

  /**
   * Доля потерь → положение стрелки.
   *
   * Ноль потерь стоит особняком: это единственный случай, ради
   * которого держится зелёная зона, и попадать в неё «почти нулевой»
   * доле нельзя — иначе зелёный перестанет что-либо гарантировать.
   * Всё остальное раскладывается линейно от верха жёлтого (потери
   * единичны) до низа красного (кэш не доживает ни разу).
   */
  function shareOf(early, chances) {
    return Math.min(early / Math.max(chances, MIN_CHANCES), 1);
  }

  function valueFor(st) {
    var base;
    if (st.early <= 0) {
      base = VALUE_CLEAN;
    } else {
      var share = shareOf(st.early, st.chances);
      base = VALUE_LOSS_BOTTOM
        + (VALUE_LOSS_TOP - VALUE_LOSS_BOTTOM) * (1 - share);
    }
    base += contextShift(st);
    return Math.max(VALUE_LOSS_BOTTOM, Math.min(VALUE_MAX, base));
  }

  /**
   * Поправка за контекстное окно: раскрывшееся выше CONTEXT_GOAL
   * тянет стрелку к зелёному, так и не доросшее — к красному.
   *
   * Молодая сессия поправку не получает ни в какую сторону: окно
   * там маленькое просто потому, что разговор только начался, и
   * штраф за это говорил бы о темпе работы, а не о кэше. Порог
   * зрелости — тот же MIN_CHANCES, что и у доли потерь.
   */
  function contextShift(st) {
    if (st.peak === null || st.chances < MIN_CHANCES) return 0;
    return st.peak >= CONTEXT_GOAL ? CONTEXT_SHIFT : -CONTEXT_SHIFT;
  }

  /** Токены человеку: 250000 → «250k». */
  function tokens(n) {
    if (n >= 1e6) return (n / 1e6).toFixed(1) + 'M';
    if (n >= 1000) return Math.round(n / 1000) + 'k';
    return String(Math.round(n));
  }

  /**
   * Расшифровка для подсказки — по строке на фактор, чтобы было
   * видно, откуда взялось положение стрелки.
   */
  function describe(st) {
    // Формулировка про кэш безличная: «N ходов потеряли» ломалось бы
    // на единице, а «потерян» — на нуле.
    var lines = [st.early === 0
      ? 'живой кэш не терялся ни разу · TTL ' + st.ttl + ' мин'
      : 'живой кэш терялся на ' + st.early + ' из ' + st.chances + ' '
        + plural(st.chances, 'хода', 'ходов', 'ходов')
        + ' · ' + Math.round(shareOf(st.early, st.chances) * 100)
        + '% · TTL ' + st.ttl + ' мин'];

    if (st.peak !== null) {
      var peak = 'пик контекста ' + tokens(st.peak);
      if (st.chances < MIN_CHANCES) {
        lines.push(peak + ' — сессия ещё короткая, в счёт не идёт');
      } else if (st.peak >= CONTEXT_GOAL) {
        lines.push(peak + ' — окно раскрывалось выше '
          + tokens(CONTEXT_GOAL) + ', плюс к оценке');
      } else {
        lines.push(peak + ' — окно не дотянуло до '
          + tokens(CONTEXT_GOAL) + ', минус к оценке');
      }
    }
    return lines.join('\n');
  }

  function onFail(reason) {
    fails++;
    if (fails < FAIL_TOLERANCE) return;
    if (haveData || detail !== reason) {
      haveData = false;
      detail = reason;
      applyAll();
      logInfo('данных нет:', reason);
    }
  }

  function tick() {
    var sid = sessionId();
    if (!sid) {
      onFail('сессия ещё не определилась');
      return;
    }
    fetch(USAGE_URL + '?session=' + encodeURIComponent(sid), { cache: 'no-store' })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (!d || !d.ok) {
          onFail((d && d.error) || 'сервер не отдал статистику');
          return;
        }
        var st = earlyStats(d);
        fails = 0;
        haveData = true;
        value = valueFor(st);
        detail = describe(st);
        applyAll();
      })
      .catch(function () { onFail('сервер недоступен'); });
  }


  /* ---------- окно истории ---------- */

  /**
   * История шкалы за сессию — по клику на индикаторе, как у Accs
   * и Usage: та же панель, то же закрытие по клику мимо и Escape.
   *
   * История не копится в браузере, а приходит с сервера: он строит её
   * пересчётом уже разобранного транскрипта (GET /mood-history), и
   * потому она полна с первого хода сессии — включая ходы, сделанные
   * до появления самого окна.
   *
   * Значение каждой точки считает `valueFor` — та же функция, что
   * ставит стрелку. Второй копии формулы нет намеренно: разойдясь
   * однажды, кривая и стрелка рассказывали бы об одном ходе разное,
   * и объяснить расхождение было бы нечем.
   */
  var HISTORY_URL = 'http://localhost:18923/mood-history';
  var PANEL_ID = 'claude-mood-panel';

  // Габариты графика в единицах viewBox. Ширина с запасом под подписи
  // слева: значения шкалы (0..100) и без них читались бы, но тогда
  // непонятно, где границы зон.
  var CH_W = 420;
  var CH_H = 150;
  var CH_PAD_L = 26;
  var CH_PAD_R = 6;
  var CH_PAD_T = 6;
  var CH_PAD_B = 16;

  var panel = null;

  function closePanel() {
    if (!panel) return;
    if (panel.parentNode) panel.parentNode.removeChild(panel);
    panel = null;
    document.removeEventListener('mousedown', onOutside, true);
    document.removeEventListener('keydown', onKeydown, true);
  }

  function onOutside(e) {
    if (!panel) return;
    if (panel.contains(e.target)) return;
    // Клик по самому индикатору обрабатывает его собственный
    // listener — иначе панель закрылась бы здесь и тут же открылась.
    if (e.target.closest && e.target.closest('.' + ROOT_CLASS)) return;
    closePanel();
  }

  function onKeydown(e) {
    if (e.key !== 'Escape') return;
    e.preventDefault();
    closePanel();
  }

  function positionPanel(btn) {
    if (!panel) return;
    // Футер прижат к низу окна — раскрываемся вверх от кнопки.
    var r = btn.getBoundingClientRect();
    panel.style.bottom = Math.max(8, window.innerHeight - r.top + 6) + 'px';
    panel.style.left = Math.max(8, r.left) + 'px';
  }

  function svgEl(name, attrs) {
    var el = document.createElementNS('http://www.w3.org/2000/svg', name);
    for (var k in attrs) {
      if (attrs.hasOwnProperty(k)) el.setAttribute(k, attrs[k]);
    }
    return el;
  }

  /** Точка ряда → значение шкалы тем же кодом, что и у стрелки. */
  function valueOfPoint(p) {
    return valueFor({
      early: num(p.early_misses) || 0,
      chances: num(p.early_chances) || 0,
      peak: num(p.context_peak),
    });
  }

  /**
   * График значения по ходам.
   *
   * Фон — те же четыре зоны, что у самой шкалы: без них кривая
   * читалась бы как абстрактные числа, а вопрос всегда один — в какой
   * зоне сессия шла и когда из неё вышла.
   *
   * По оси X — номер хода, а не время: паузы между ходами бывают
   * часами, и на временной оси вся работа сжалась бы в несколько
   * пятен. Время остаётся в подписи под графиком и в подсказке точки.
   */
  function chart(series) {
    var svg = svgEl('svg', {
      viewBox: '0 0 ' + CH_W + ' ' + CH_H,
      width: '100%',
      height: CH_H,
      preserveAspectRatio: 'none',
    });
    svg.setAttribute('class', 'claude-mood-chart');

    var x0 = CH_PAD_L;
    var y0 = CH_PAD_T;
    var w = CH_W - CH_PAD_L - CH_PAD_R;
    var h = CH_H - CH_PAD_T - CH_PAD_B;

    // Зоны: снизу вверх красная, оранжевая, жёлтая, зелёная.
    var zones = ['claude-mood-zone-1', 'claude-mood-zone-2',
      'claude-mood-zone-3', 'claude-mood-zone-4'];
    for (var z = 0; z < zones.length; z++) {
      svg.appendChild(svgEl('rect', {
        x: x0, width: w,
        y: y0 + h * (1 - (z + 1) / 4), height: h / 4,
        class: zones[z],
      }));
    }

    // Подписи границ зон — 0, 25, 50, 75, 100.
    for (var v = 0; v <= 100; v += 25) {
      var y = y0 + h * (1 - v / 100);
      svg.appendChild(svgEl('line', {
        x1: x0, x2: x0 + w, y1: y, y2: y, class: 'claude-mood-grid',
      }));
      var t = svgEl('text', {
        x: x0 - 4, y: y + 3, class: 'claude-mood-axis',
        'text-anchor': 'end',
      });
      t.textContent = String(v);
      svg.appendChild(t);
    }

    if (!series.length) return svg;

    // Кривая. Один ход — одна точка; при сотнях ходов линия и так
    // сплошная, поэтому маркеры не рисуем: они слились бы в кашу.
    var pts = [];
    for (var i = 0; i < series.length; i++) {
      var px = x0 + (series.length === 1 ? w / 2 : w * i / (series.length - 1));
      var py = y0 + h * (1 - valueOfPoint(series[i]) / 100);
      pts.push(round3(px) + ',' + round3(py));
    }
    svg.appendChild(svgEl('polyline', {
      points: pts.join(' '), class: 'claude-mood-line',
    }));

    // Последняя точка — текущее положение стрелки. Её помечаем: глаз
    // должен находить «где мы сейчас» без пересчёта.
    var last = pts[pts.length - 1].split(',');
    svg.appendChild(svgEl('circle', {
      cx: last[0], cy: last[1], r: 3, class: 'claude-mood-dot',
    }));
    return svg;
  }

  /** Короткая сводка под графиком. */
  function summary(data) {
    var series = data.series || [];
    var box = document.createElement('div');
    box.className = 'claude-mood-summary';

    var last = series.length ? series[series.length - 1] : null;
    var first = series.length ? series[0] : null;
    var rows = [];
    if (last) {
      rows.push(['сейчас', Math.round(valueOfPoint(last)) + ' / 100']);
      rows.push(['ходов в сессии', String(series.length)]);
      rows.push(['потерь живого кэша',
        last.early_misses + ' из ' + last.early_chances]);
      rows.push(['пик контекста', tokens(last.context_peak)]);
      if (first && first.ts && last.ts) {
        rows.push(['период', first.ts.slice(11, 16) + ' — ' + last.ts.slice(11, 16)]);
      }
    }
    rows.push(['TTL кэша', (num(data.ttl_minutes) || 60) + ' мин']);

    for (var i = 0; i < rows.length; i++) {
      var line = document.createElement('div');
      line.className = 'claude-mood-summary-row';
      var k = document.createElement('span');
      k.textContent = rows[i][0];
      var v = document.createElement('span');
      v.className = 'claude-mood-summary-val';
      v.textContent = rows[i][1];
      line.appendChild(k);
      line.appendChild(v);
      box.appendChild(line);
    }
    return box;
  }

  function renderError(body, text) {
    var div = document.createElement('div');
    div.className = 'claude-mood-empty';
    div.textContent = text;
    body.appendChild(div);
  }

  function openPanel(btn) {
    closePanel();

    panel = document.createElement('div');
    panel.id = PANEL_ID;
    panel.className = 'claude-mood-panel';

    var head = document.createElement('div');
    head.className = 'claude-mood-head';
    head.textContent = 'История Mood за сессию';
    panel.appendChild(head);

    var body = document.createElement('div');
    body.className = 'claude-mood-body';
    body.textContent = 'загрузка…';
    panel.appendChild(body);
    document.body.appendChild(panel);
    positionPanel(btn);

    document.addEventListener('mousedown', onOutside, true);
    document.addEventListener('keydown', onKeydown, true);

    var sid = sessionId();
    fetch(HISTORY_URL + (sid ? '?session=' + encodeURIComponent(sid) : ''),
          { cache: 'no-store' })
      .then(function (res) { return res.json(); })
      .then(function (d) {
        if (!panel) return;
        body.textContent = '';
        if (!d || !d.ok) {
          renderError(body, (d && d.error) || 'нет данных');
        } else if (!(d.series || []).length) {
          renderError(body, 'в сессии ещё нет ходов');
        } else {
          body.appendChild(chart(d.series));
          body.appendChild(summary(d));
        }
        positionPanel(btn);
      })
      .catch(function (err) {
        if (!panel) return;
        body.textContent = '';
        renderError(body, 'http-server.py недоступен (порт 18923)');
        logInfo('история недоступна', err);
      });
  }

  /* ---------- монтирование ---------- */

  /** Самый левый из наших контролов футера — перед ним и встаём. */
  function leftmostNeighbour(footer) {
    var sel = ['.claude-accs-btn', '.claude-usage-btn',
      '.claude-cache-btn', '.claude-bypass-btn'];
    for (var i = 0; i < sel.length; i++) {
      var el = footer.querySelector(sel[i]);
      if (el && el.parentNode === footer) return el;
    }
    return null;
  }

  function mount(container) {
    var footer = container.querySelector('[class*="inputFooter_"]');
    if (!footer) return false;
    var gauge = createGauge(footer.querySelector('[class*="footerButton_"]'));
    // Порядок в футере: Mood · Accs · Usage · Cache · ByPass.
    // Опираемся на соседа, а не на порядок инициализации модулей:
    // React пересоздаёт футер, и кто смонтируется первым — не
    // гарантировано (та же логика, что в ACCOUNT SWITCHER BUTTON).
    var neighbour = leftmostNeighbour(footer);
    if (neighbour) {
      footer.insertBefore(gauge, neighbour);
    } else {
      var spacer = footer.querySelector('[class*="spacer_"]');
      if (spacer && spacer.parentNode === footer) {
        footer.insertBefore(gauge, spacer.nextSibling);
      } else {
        footer.appendChild(gauge);
      }
    }
    return true;
  }

  /**
   * Держит Mood левее соседей, даже если те смонтировались позже.
   * Вставка при монтировании этого не гарантирует — см. ensureOrder
   * в ACCOUNT SWITCHER BUTTON.
   */
  function ensureOrder(footer) {
    var me = footer.querySelector('.' + ROOT_CLASS);
    if (!me || me.parentNode !== footer) return;
    var right = leftmostNeighbour(footer);
    if (!right) return;
    // DOCUMENT_POSITION_FOLLOWING — сосед идёт ПОСЛЕ нас, всё верно.
    if (!(me.compareDocumentPosition(right) & 4)) {
      footer.insertBefore(me, right);
      logInfo('порядок восстановлен: Mood перед соседями');
    }
  }

  function scan(ctx) {
    // Узлы даёт общий обход (см. DOM WATCH) — один на все модули.
    // Свой поиск остаётся для вызовов вне прохода: при регистрации
    // и из обработчиков самого модуля.
    var containers = (ctx && ctx.inputs)
      || document.querySelectorAll('[class*="inputContainer_"]');
    for (var i = 0; i < containers.length; i++) {
      var footer = containers[i].querySelector('[class*="inputFooter_"]');
      if (containers[i].querySelector('.' + ROOT_CLASS)) {
        if (footer) ensureOrder(footer);
        continue;
      }
      if (!containers[i].querySelector('[role="textbox"][contenteditable]')) continue;
      mount(containers[i]);
    }
  }

  function init() {
    // Наблюдатель и подстраховочный таймер — общие (см. DOM WATCH).
    window.__claudeDomWatch.register('mood', scan);
    // Первый опрос сразу: до него шкала серая, и задержка на целый
    // период читалась бы как «индикатор не работает».
    tick();
    setInterval(tick, POLL_MS);
    logInfo('installed, опрос раз в', POLL_MS / 1000, 'с');
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();

/* ============================================================
 * INPUT RING — обводка поля ввода цветом индикатора Mood
 *
 * Расширение окрашивает рамку поля ввода при фокусе по режиму
 * разрешений: оранжевый — обычный, синий — план, красный — bypass.
 * Модуль умеет заменить этот признак другим — цветом сектора, в
 * который сейчас смотрит стрелка Mood.
 *
 * Зачем: индикатор в футере занимает 27×16 точек, а рамка обрамляет
 * всё поле ввода — то место, куда смотришь, когда пишешь сообщение.
 * Состояние кэша при этом замечаешь, не отводя взгляда.
 *
 * КАК. Цвет рамки расширение держит в переменной --focus-ring-color
 * и меняет её правилами по `data-permission-mode`. Мы подменяем ту же
 * переменную: border-color, box-shadow и всё остальное расширение
 * выведет из неё само. Свои правила про саму рамку разошлись бы с
 * оригиналом на первом же обновлении расширения.
 *
 * Модуль ставит только класс и номер сектора (0..3) атрибутом —
 * цвета живут в CSS рядом с секторами шкалы, откуда и взяты.
 * О палитре JS не знает ничего, как и в самом MOOD GAUGE.
 *
 * Значение берётся у него же через `window.__claudeMood.level()`, а
 * обновления приходят подпиской: опрос своим таймером почти всегда
 * заставал бы значение прежним — оно меняется раз в moodPollSec.
 * Выключенный moodGauge не публикует этот объект вовсе, и тогда
 * красить нечем: рамка остаётся штатной.
 *
 * «Данных ещё нет» (сессия не определилась, сервер молчит) — тоже
 * штатная рамка, а не зелёная: шкала в этот момент обесцвечивается
 * ровно потому, что утверждать «всё хорошо» ещё рано.
 *
 * Управление: `inputRingColor` в claude-custom-config.toml. Параметр
 * горячий — опрашивается через /custom-config, как cacheKeepalive*.
 * ============================================================ */
(function () {
  if (window.__claudeInputRingInstalled) return;
  window.__claudeInputRingInstalled = true;

  var cfg = window.__CLAUDE_CUSTOM_CONFIG__ || {};

  var RING_CLASS = 'claude-ring-mood';
  var LEVEL_ATTR = 'data-claude-mood-level';

  var CONFIG_URL = 'http://localhost:18923/custom-config';
  var CONFIG_POLL_MS = 5000;

  // "mood" — красить по индикатору, всё остальное — не вмешиваться.
  // Незнакомое значение трактуется как штатное поведение: это
  // безопасная сторона, рамка остаётся такой, какой её задумало
  // расширение.
  var mode = cfg.inputRingColor === 'mood' ? 'mood' : 'mode';

  function logInfo() {
    if (!cfg.logs) return;
    try {
      console.log.apply(console, ['[input-ring]'].concat([].slice.call(arguments)));
    } catch (e) {}
  }

  /** Сектор шкалы под стрелкой либо null, если красить не по чему. */
  function currentLevel() {
    if (mode !== 'mood') return null;
    var api = window.__claudeMood;
    if (!api || typeof api.level !== 'function') return null;
    return api.level();
  }

  function applyTo(el, level) {
    if (level === null) {
      if (el.classList.contains(RING_CLASS)) {
        el.classList.remove(RING_CLASS);
        el.removeAttribute(LEVEL_ATTR);
      }
      return;
    }
    // Пишем только при изменении: присваивание мутирует DOM даже когда
    // значение то же, а каждая мутация будит общий наблюдатель — то
    // самое, из-за чего debug-оверлей однажды будил патч сам собой.
    var text = String(level);
    if (el.getAttribute(LEVEL_ATTR) !== text) el.setAttribute(LEVEL_ATTR, text);
    if (!el.classList.contains(RING_CLASS)) el.classList.add(RING_CLASS);
  }

  function scan(ctx) {
    // Узлы даёт общий обход (см. DOM WATCH); свой поиск — для вызовов
    // вне прохода: при регистрации и из подписки на значение Mood.
    var containers = (ctx && ctx.inputs)
      || document.querySelectorAll('[class*="inputContainer_"]');
    var level = currentLevel();
    for (var i = 0; i < containers.length; i++) {
      // Рамку рисует тот из двух контейнеров, которому расширение
      // ставит режим разрешений; внешний — только позиционирование,
      // и переменная на нём ничего бы не изменила: у внутреннего
      // при фокусе своё правило, оно перебило бы унаследованное.
      if (!containers[i].hasAttribute('data-permission-mode')) continue;
      applyTo(containers[i], level);
    }
  }

  function applyLiveConfig(c) {
    if (!c || typeof c !== 'object') return;
    var next = c.inputRingColor === 'mood' ? 'mood' : 'mode';
    if (next === mode) return;
    mode = next;
    logInfo('режим обводки:', mode);
    // Сразу, не дожидаясь ближайшей мутации: смена настройки — это
    // действие пользователя, и отклик на него должен быть виден.
    scan();
  }

  function pollConfig() {
    fetch(CONFIG_URL, { cache: 'no-store' })
      .then(function (r) { return r.json(); })
      .then(function (d) { if (d && d.ok) applyLiveConfig(d.config); })
      .catch(function () {});
  }

  function init() {
    // Наблюдатель и подстраховочный таймер — общие (см. DOM WATCH).
    // Якорный класс `inputContainer_` уже в RELEVANT, отдельной
    // записи фильтру не нужно.
    window.__claudeDomWatch.register('input-ring', scan);
    var api = window.__claudeMood;
    if (api && typeof api.onChange === 'function') api.onChange(scan);
    else logInfo('индикатор Mood выключен — обводка остаётся штатной');
    pollConfig();
    setInterval(pollConfig, CONFIG_POLL_MS);
    logInfo('installed, режим обводки:', mode);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();

/* ============================================================
 * SETTINGS PANEL — окно настроек из контекстного меню
 *
 * Открывается пунктом «⚙ Настройки» контекстного меню страницы и
 * правит `claude-custom-config.toml` — тот же файл, что и рука.
 *
 * Путь до окна длиннее обычного, и это не прихоть: меню по правой
 * кнопке рисует оболочка VSCode, а не страница. Поэтому пункт
 * объявлен в манифесте расширения (patch-extension-settings.py),
 * страница помечена атрибутом `data-vscode-context`, команду ловит
 * блок в extension.js (patch-extension-csp.py), и он же шлёт сюда
 * postMessage. Здесь — последнее звено: показать окно.
 *
 * Список параметров и подсказки приходят с сервера
 * (`GET /config-schema`), а подсказки — это комментарии из самого
 * TOML. Описывать флаги второй раз здесь значило бы завести два
 * источника правды о том, что они делают, и они разошлись бы на
 * первой же правке файла.
 *
 * Модуль не имеет своего выключателя в конфиге намеренно: выключив
 * его, пользователь остался бы без единственного окна, из которого
 * флаги и включаются обратно.
 * ============================================================ */
(function () {
  if (window.__claudeSettingsPanelInstalled) return;
  window.__claudeSettingsPanelInstalled = true;

  var cfg = window.__CLAUDE_CUSTOM_CONFIG__ || {};
  var SCHEMA_URL = 'http://localhost:18923/config-schema';
  var SAVE_URL = 'http://localhost:18923/custom-config';
  var PANEL_ID = 'claude-settings-panel';

  var panel = null;
  var items = [];       // описания с сервера
  var controls = {};    // ключ → функция чтения текущего значения из UI

  function logInfo() {
    if (!cfg.logs) return;
    try {
      console.log.apply(console, ['[settings-panel]'].concat([].slice.call(arguments)));
    } catch (e) {}
  }

  /** Первая строка подсказки — она же заголовок параметра в списке. */
  function firstLine(text) {
    var line = String(text || '').split('\n')[0].trim();
    return line;
  }

  function closePanel() {
    if (!panel) return;
    if (panel.parentNode) panel.parentNode.removeChild(panel);
    panel = null;
    controls = {};
    document.removeEventListener('keydown', onKeydown, true);
  }

  function onKeydown(e) {
    if (e.key !== 'Escape') return;
    // Окно перекрывает страницу целиком, поэтому Escape закрывает его
    // и не должен уходить дальше — иначе заодно снимет выделение или
    // закроет что-то за ним.
    e.preventDefault();
    e.stopPropagation();
    closePanel();
  }

  /* ---------- элементы управления ---------- */

  /**
   * Строка параметра. Тип решает вид: булев — переключатель, строка
   * с перечнем значений — список, остальное — поле ввода. Ключ
   * показывается как есть: он же стоит в TOML, и по нему пользователь
   * найдёт параметр в файле.
   */
  function paramRow(item) {
    var row = document.createElement('div');
    row.className = 'claude-settings-row';

    var left = document.createElement('div');
    left.className = 'claude-settings-name';

    var key = document.createElement('div');
    key.className = 'claude-settings-key';
    key.textContent = item.key;
    left.appendChild(key);

    var hint = firstLine(item.hint);
    if (hint) {
      var sub = document.createElement('div');
      sub.className = 'claude-settings-hint';
      sub.textContent = hint;
      // Полный текст — в подсказке при наведении: у некоторых
      // параметров это несколько абзацев с историей поломки, и
      // разворачивать их в списке из тридцати строк нечитаемо.
      if (item.hint && item.hint !== hint) sub.title = item.hint;
      left.appendChild(sub);
    }
    row.appendChild(left);

    var wrap = document.createElement('div');
    wrap.className = 'claude-settings-control';

    if (item.type === 'bool') {
      var box = document.createElement('input');
      box.type = 'checkbox';
      box.checked = !!item.value;
      box.className = 'claude-settings-check';
      wrap.appendChild(box);
      controls[item.key] = function () { return box.checked; };
    } else if (item.options && item.options.length) {
      // Перечень значений сервер достаёт из строки `Варианты: a | b`
      // в комментарии параметра. Список, а не поле ввода: значение вне
      // перечня модуль не поймёт и молча откатится на своё поведение —
      // настройка выглядела бы заданной, ничего не делая.
      var select = document.createElement('select');
      select.className = 'claude-settings-select';
      for (var oi = 0; oi < item.options.length; oi++) {
        var opt = document.createElement('option');
        opt.value = item.options[oi];
        opt.textContent = item.options[oi];
        select.appendChild(opt);
      }
      select.value = String(item.value);
      wrap.appendChild(select);
      controls[item.key] = function () { return select.value; };
    } else {
      var input = document.createElement('input');
      input.type = item.type === 'number' ? 'number' : 'text';
      input.value = String(item.value);
      input.className = 'claude-settings-input';
      wrap.appendChild(input);
      controls[item.key] = function () {
        if (item.type !== 'number') return input.value;
        var num = Number(input.value);
        // Нечисло в числовом поле не отправляем: сервер записал бы его
        // строкой, и параметр молча перестал бы действовать.
        return isFinite(num) ? num : item.value;
      };
    }
    row.appendChild(wrap);
    return row;
  }

  /* ---------- сохранение ---------- */

  function changedValues() {
    var out = {};
    for (var i = 0; i < items.length; i++) {
      var item = items[i];
      var read = controls[item.key];
      if (!read) continue;
      var now = read();
      if (now !== item.value) out[item.key] = now;
    }
    return out;
  }

  function save(status, button) {
    var values = changedValues();
    var keys = Object.keys(values);
    if (!keys.length) {
      setStatus(status, 'Менять нечего — значения те же.', '');
      return;
    }
    button.disabled = true;
    setStatus(status, 'Сохраняю…', 'wait');
    fetch(SAVE_URL, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ values: values }),
    })
      .then(function (res) { return res.json(); })
      .then(function (d) {
        button.disabled = false;
        if (!d || !d.ok) throw new Error((d && d.error) || 'сервер отказал');
        // Запоминаем новые значения как исходные, иначе повторное
        // нажатие отправило бы те же правки ещё раз.
        for (var i = 0; i < items.length; i++) {
          if (items[i].key in values) items[i].value = values[items[i].key];
        }
        // Честно про момент применения: CSS подхватывается горячо,
        // а флаги модулей живут в bootstrap, который перечитывается
        // только при перезагрузке окна.
        setStatus(status,
          'Сохранено (' + keys.length + '): ' + keys.join(', ')
          + '. Флаги модулей применятся после Developer: Reload Window.',
          'ok');
        logInfo('сохранено', values);
      })
      .catch(function (err) {
        button.disabled = false;
        setStatus(status, 'Не удалось сохранить: '
          + ((err && err.message) || err), 'err');
      });
  }

  function setStatus(el, text, kind) {
    el.textContent = text;
    el.className = 'claude-settings-status'
      + (kind ? ' claude-settings-status-' + kind : '');
  }

  /* ---------- окно ---------- */

  function render(body, status) {
    body.textContent = '';
    var search = document.createElement('input');
    search.type = 'search';
    search.className = 'claude-settings-search';
    search.placeholder = 'Поиск по названию и описанию';
    body.appendChild(search);

    var list = document.createElement('div');
    list.className = 'claude-settings-list';
    body.appendChild(list);

    /* Раскладка по разделам. Разделы заданы в самом TOML строками
     * `# ==== Название ====` и приходят с сервера у каждого параметра —
     * поэтому новый флаг попадает в свой раздел сам, а человеку,
     * читающему файл руками, видна та же структура.
     *
     * Все свёрнуты при открытии: тридцать пять параметров подряд —
     * это стена, в которой ничего не найти, а свёрнутый список из
     * тринадцати заголовков обозрим целиком.
     *
     * `<details>` вместо своей раскрывашки: он умеет разворачиваться
     * без единой строки JS, доступен с клавиатуры и ищется штатным
     * Ctrl+F браузера. */
    var rows = [];
    var groups = [];
    var byName = {};
    for (var i = 0; i < items.length; i++) {
      var item = items[i];
      var name = item.section || 'Прочее';
      var group = byName[name];
      if (!group) {
        var box = document.createElement('details');
        box.className = 'claude-settings-group';
        var summary = document.createElement('summary');
        summary.className = 'claude-settings-group-head';
        var label = document.createElement('span');
        label.textContent = name;
        summary.appendChild(label);
        var count = document.createElement('span');
        count.className = 'claude-settings-group-count';
        summary.appendChild(count);
        box.appendChild(summary);
        list.appendChild(box);
        group = byName[name] = { box: box, count: count, total: 0 };
        groups.push(group);
      }
      group.total++;
      var row = paramRow(item);
      rows.push({ row: row, item: item, group: group });
      group.box.appendChild(row);
    }
    for (var g = 0; g < groups.length; g++) {
      groups[g].count.textContent = String(groups[g].total);
    }

    /* Пометка о несохранённых правках.
     *
     * «Отмена» и крестик закрывают окно, ничего не записывая, — и это
     * правильно, но молча потерять десяток переключённых флагов
     * обидно. Поэтому строка статуса говорит, что правки есть и они
     * пока только в окне.
     *
     * Слушатель один на весь список (события всплывают), а не по
     * одному на каждый из тридцати пяти элементов управления. */
    list.addEventListener('input', markDirty, true);
    list.addEventListener('change', markDirty, true);

    function markDirty() {
      var pending = Object.keys(changedValues()).length;
      if (!pending) {
        setStatus(status, '', '');
        return;
      }
      setStatus(status, 'Не сохранено: ' + pending + ' '
        + (pending === 1 ? 'изменение' : 'изменений')
        + ' — «Отмена» их отбросит.', 'wait');
    }

    /* Поиск по ключу и по описанию: половину параметров помнишь не по
     * имени, а по тому, что они делают.
     *
     * Найденное показывается сразу: раздел с совпадениями
     * разворачивается сам, а пустой прячется целиком. Иначе поиск в
     * свёрнутом списке выглядел бы как «ничего не найдено» — совпадения
     * есть, но спрятаны за закрытым заголовком.
     *
     * При очистке строки поиска разделы снова сворачиваются: это
     * исходное состояние окна, и оставлять после поиска развёрнутую
     * стену значило бы менять его молча. */
    search.addEventListener('input', function () {
      var q = search.value.trim().toLowerCase();
      var k;
      for (k = 0; k < groups.length; k++) groups[k].hits = 0;

      for (var j = 0; j < rows.length; j++) {
        var it = rows[j].item;
        var hit = !q
          || it.key.toLowerCase().indexOf(q) !== -1
          || String(it.hint || '').toLowerCase().indexOf(q) !== -1;
        rows[j].row.style.display = hit ? '' : 'none';
        if (hit) rows[j].group.hits++;
      }

      for (k = 0; k < groups.length; k++) {
        var group = groups[k];
        group.box.style.display = (!q || group.hits) ? '' : 'none';
        group.box.open = !!q && !!group.hits;
        group.count.textContent = q
          ? group.hits + ' / ' + group.total
          : String(group.total);
      }
    });

    search.focus();
  }

  function openPanel() {
    closePanel();

    panel = document.createElement('div');
    panel.id = PANEL_ID;
    panel.className = 'claude-settings-backdrop';
    // Клик мимо окна закрывает его, как у остальных панелей. Проверка
    // именно на подложку: клик внутри окна всплывает сюда же.
    panel.addEventListener('mousedown', function (e) {
      if (e.target === panel) closePanel();
    });

    var win = document.createElement('div');
    win.className = 'claude-settings-window';
    panel.appendChild(win);

    var head = document.createElement('div');
    head.className = 'claude-settings-head';
    var title = document.createElement('span');
    title.textContent = '⚙ Настройки патча';
    head.appendChild(title);

    // Крестик — третий способ закрыть окно, вдобавок к «Отмене» и
    // Escape. Своя кнопка нужна потому, что у окна нет рамки VSCode
    // с системным крестиком: это div на подложке, а не диалог
    // оболочки, и закрывать его нечем, кроме того, что нарисуем сами.
    var closeX = document.createElement('button');
    closeX.type = 'button';
    closeX.className = 'claude-settings-x';
    closeX.textContent = '✕';
    closeX.title = 'Закрыть без сохранения (Esc)';
    closeX.setAttribute('aria-label', 'Закрыть');
    closeX.addEventListener('click', closePanel);
    head.appendChild(closeX);
    win.appendChild(head);

    var sub = document.createElement('div');
    sub.className = 'claude-settings-sub';
    sub.textContent = 'claude-custom-config.toml';
    win.appendChild(sub);

    var body = document.createElement('div');
    body.className = 'claude-settings-body';
    body.textContent = 'загрузка…';
    win.appendChild(body);

    var footer = document.createElement('div');
    footer.className = 'claude-settings-footer';
    var status = document.createElement('div');
    status.className = 'claude-settings-status';
    footer.appendChild(status);

    // Порядок «Сохранить → Отмена».
    var cancelBtn = document.createElement('button');
    cancelBtn.type = 'button';
    cancelBtn.className = 'claude-settings-btn';
    cancelBtn.textContent = 'Отмена';
    cancelBtn.title = 'Закрыть, не записывая правки в файл';
    cancelBtn.addEventListener('click', closePanel);

    var saveBtn = document.createElement('button');
    saveBtn.type = 'button';
    saveBtn.className = 'claude-settings-btn claude-settings-btn-primary';
    saveBtn.textContent = 'Сохранить';
    saveBtn.addEventListener('click', function () { save(status, saveBtn); });

    footer.appendChild(saveBtn);
    footer.appendChild(cancelBtn);
    win.appendChild(footer);

    document.body.appendChild(panel);
    document.addEventListener('keydown', onKeydown, true);

    fetch(SCHEMA_URL, { cache: 'no-store' })
      .then(function (res) { return res.json(); })
      .then(function (d) {
        if (!panel) return;
        if (!d || !d.ok) throw new Error((d && d.error) || 'сервер отказал');
        items = d.items || [];
        render(body, status);
        if (d.path) sub.title = d.path;
      })
      .catch(function (err) {
        if (!panel) return;
        body.textContent = 'http-server.py недоступен (порт 18923): '
          + ((err && err.message) || err);
      });
  }

  /* ---------- вход ---------- */

  window.addEventListener('message', function (e) {
    // Сообщение приходит от блока в extension.js по нажатию пункта
    // меню. Ключ нарочно свой, чтобы не пересечься с протоколом
    // приложения — и чтобы его обработчик так же спокойно прошёл мимо
    // нашего сообщения.
    var data = e && e.data;
    if (!data || data.__claudeCustom !== 'open-settings') return;
    logInfo('открываю по команде из контекстного меню');
    openPanel();
  });

  // Отладочный вход: окно можно открыть и без меню.
  window.__claudeSettings = { open: openPanel, close: closePanel };
})();

/* ============================================================
 * IMAGE ANNOTATION EDITOR — ручная разметка вложений
 *
 * Добавляет кнопку ✎ в левый верхний угол открытого штатного preview
 * прикреплённого изображения. Редактор живёт целиком внутри webview:
 * исходник уже доступен как data URL, Canvas рисует поверх него в родном
 * разрешении, а результат возвращается React-композеру как новый File.
 *
 * Исходник удаляется только после того, как новая миниатюра появилась
 * в DOM. При ошибке добавления исходное вложение остаётся на месте.
 * ============================================================ */
(function () {
  var cfg = window.__CLAUDE_CUSTOM_CONFIG__ || {};
  if (!cfg.imageAnnotationEditor) return;
  if (window.__claudeImageAnnotationInstalled) return;
  window.__claudeImageAnnotationInstalled = true;

  var EDIT_CLASS = 'claude-image-edit-btn';
  var MARK_CLASS = 'claude-image-editable';
  // В 2.1.220 composer рисует вложения компонентом pill_lcdCYQ.
  // Ограничение родителем обязательно: pill_* встречается и в других
  // частях интерфейса, а редактор относится только к ещё не отправленным
  // вложениям активного поля ввода.
  var THUMB_SELECTOR = '[class*="attachedFilesContainer_"] [class*="pill_"]';
  var PREVIEW_SELECTOR = '[class*="previewContainer_"]';
  var PREVIEW_IMAGE_SELECTOR = 'img[class*="previewImage_"]';
  var COMPOSER_SELECTOR = '[role="textbox"][contenteditable]';
  var active = null;

  function own(el) {
    if (el) el.__claudeOwnNode = true;
    return el;
  }

  function button(label, className, title, handler) {
    var el = own(document.createElement('button'));
    el.type = 'button';
    el.className = className || '';
    el.textContent = label;
    if (title) {
      el.title = title;
      el.setAttribute('aria-label', title);
    }
    if (handler) el.addEventListener('click', handler);
    return el;
  }

  function imageOf(thumb) {
    var img = thumb && thumb.querySelector('img');
    if (!img || !/^data:image\//i.test(img.src || '')) return null;
    return img;
  }

  function previewImageOf(preview) {
    var img = preview && preview.querySelector(PREVIEW_IMAGE_SELECTOR);
    if (!img || !/^data:image\//i.test(img.src || '')) return null;
    return img;
  }

  function thumbForSource(sourceUrl, thumbs) {
    var candidates = thumbs || document.querySelectorAll(THUMB_SELECTOR);
    for (var i = 0; i < candidates.length; i++) {
      var img = imageOf(candidates[i]);
      if (img && img.src === sourceUrl) return candidates[i];
    }
    return null;
  }

  function closeEditor() {
    if (!active) return;
    document.removeEventListener('keydown', active.keyHandler, true);
    if (active.overlay && active.overlay.parentNode) active.overlay.remove();
    active = null;
  }

  function annotatedName(name) {
    var clean = String(name || 'image').replace(/\.[^.]+$/, '');
    return clean + '-annotated.png';
  }

  function drawAction(ctx, action) {
    if (!action) return;
    var pts = action.points || [];
    var start = action.start || pts[0];
    var end = action.end || pts[pts.length - 1];
    ctx.save();
    ctx.strokeStyle = action.color || '#ef4444';
    ctx.lineWidth = action.width || 3;
    ctx.lineCap = 'round';
    ctx.lineJoin = 'round';
    ctx.beginPath();

    if (action.tool === 'brush') {
      if (!pts.length) { ctx.restore(); return; }
      ctx.moveTo(pts[0].x, pts[0].y);
      if (pts.length === 1) ctx.lineTo(pts[0].x + 0.01, pts[0].y + 0.01);
      for (var i = 1; i < pts.length; i++) ctx.lineTo(pts[i].x, pts[i].y);
    } else if (action.tool === 'line' && start && end) {
      ctx.moveTo(start.x, start.y);
      ctx.lineTo(end.x, end.y);
    } else if (action.tool === 'rect' && start && end) {
      ctx.rect(start.x, start.y, end.x - start.x, end.y - start.y);
    } else if (action.tool === 'ellipse' && start && end) {
      var cx = (start.x + end.x) / 2;
      var cy = (start.y + end.y) / 2;
      var rx = Math.abs(end.x - start.x) / 2;
      var ry = Math.abs(end.y - start.y) / 2;
      ctx.ellipse(cx, cy, Math.max(rx, 0.01), Math.max(ry, 0.01), 0, 0, Math.PI * 2);
    }
    ctx.stroke();
    ctx.restore();
  }

  function cloneAction(action) {
    return {
      tool: action.tool,
      color: action.color,
      width: action.width,
      start: action.start ? { x: action.start.x, y: action.start.y } : null,
      end: action.end ? { x: action.end.x, y: action.end.y } : null,
      points: (action.points || []).map(function (p) { return { x: p.x, y: p.y }; }),
    };
  }

  function translateAction(action, dx, dy) {
    var moved = cloneAction(action);
    if (moved.start) { moved.start.x += dx; moved.start.y += dy; }
    if (moved.end) { moved.end.x += dx; moved.end.y += dy; }
    moved.points.forEach(function (p) { p.x += dx; p.y += dy; });
    return moved;
  }

  function actionBounds(action) {
    var pts = (action.points || []).slice();
    if (action.start) pts.push(action.start);
    if (action.end) pts.push(action.end);
    if (!pts.length) return null;
    var minX = pts[0].x;
    var maxX = pts[0].x;
    var minY = pts[0].y;
    var maxY = pts[0].y;
    for (var i = 1; i < pts.length; i++) {
      minX = Math.min(minX, pts[i].x);
      maxX = Math.max(maxX, pts[i].x);
      minY = Math.min(minY, pts[i].y);
      maxY = Math.max(maxY, pts[i].y);
    }
    var pad = Math.max(1, Number(action.width) || 1) / 2;
    return { x: minX - pad, y: minY - pad, width: maxX - minX + pad * 2, height: maxY - minY + pad * 2 };
  }

  function segmentDistance(point, a, b) {
    var dx = b.x - a.x;
    var dy = b.y - a.y;
    if (!dx && !dy) return Math.hypot(point.x - a.x, point.y - a.y);
    var t = ((point.x - a.x) * dx + (point.y - a.y) * dy) / (dx * dx + dy * dy);
    t = Math.max(0, Math.min(1, t));
    return Math.hypot(point.x - (a.x + t * dx), point.y - (a.y + t * dy));
  }

  function hitAction(action, point, tolerance) {
    var extra = Math.max(0, tolerance || 0);
    var radius = Math.max(1, Number(action.width) || 1) / 2 + extra;
    var pts = action.points || [];
    if (action.tool === 'brush') {
      if (pts.length === 1) return Math.hypot(point.x - pts[0].x, point.y - pts[0].y) <= radius;
      for (var i = 1; i < pts.length; i++) {
        if (segmentDistance(point, pts[i - 1], pts[i]) <= radius) return true;
      }
      return false;
    }
    if (action.tool === 'line' && action.start && action.end) {
      return segmentDistance(point, action.start, action.end) <= radius;
    }
    if ((action.tool === 'rect' || action.tool === 'ellipse') && action.start && action.end) {
      var left = Math.min(action.start.x, action.end.x) - radius;
      var right = Math.max(action.start.x, action.end.x) + radius;
      var top = Math.min(action.start.y, action.end.y) - radius;
      var bottom = Math.max(action.start.y, action.end.y) + radius;
      if (point.x < left || point.x > right || point.y < top || point.y > bottom) return false;
      if (action.tool === 'rect') return true;
      var cx = (action.start.x + action.end.x) / 2;
      var cy = (action.start.y + action.end.y) / 2;
      var rx = Math.abs(action.end.x - action.start.x) / 2 + radius;
      var ry = Math.abs(action.end.y - action.start.y) / 2 + radius;
      return rx > 0 && ry > 0 && ((point.x - cx) * (point.x - cx)) / (rx * rx)
        + ((point.y - cy) * (point.y - cy)) / (ry * ry) <= 1;
    }
    return false;
  }

  function drawSelection(ctx, action) {
    var bounds = actionBounds(action);
    if (!bounds) return;
    ctx.save();
    ctx.strokeStyle = '#60a5fa';
    ctx.lineWidth = 1;
    if (ctx.setLineDash) ctx.setLineDash([6, 4]);
    ctx.strokeRect(bounds.x - 4, bounds.y - 4, bounds.width + 8, bounds.height + 8);
    ctx.restore();
  }

  function dispatchFile(file, composer) {
    try {
      var transfer = new DataTransfer();
      transfer.items.add(file);
      var paste = new ClipboardEvent('paste', {
        bubbles: true,
        cancelable: true,
        clipboardData: transfer,
      });
      composer.dispatchEvent(paste);
      if (paste.defaultPrevented) return true;
    } catch (e) {}

    // Запасной путь для Electron-сборок, которые не принимают
    // clipboardData в конструкторе ClipboardEvent.
    try {
      var input = document.querySelector('input[type="file"][multiple]');
      if (!input) return false;
      var dt = new DataTransfer();
      dt.items.add(file);
      input.files = dt.files;
      input.dispatchEvent(new Event('change', { bubbles: true }));
      return true;
    } catch (e2) {
      return false;
    }
  }

  function replaceAttachment(state, file, done) {
    var composerHost = state.thumb.closest('[class*="inputContainer_"]');
    var composer = composerHost && composerHost.querySelector(COMPOSER_SELECTOR);
    if (!composer) composer = document.querySelector(COMPOSER_SELECTOR);
    if (!composer) { done(new Error('поле ввода чата не найдено')); return; }

    var before = document.querySelectorAll(THUMB_SELECTOR).length;
    if (!dispatchFile(file, composer)) {
      done(new Error('расширение не приняло новый файл'));
      return;
    }

    var started = Date.now();
    (function waitForThumbnail() {
      var now = document.querySelectorAll(THUMB_SELECTOR).length;
      if (now > before) {
        var original = state.thumb;
        if (!document.body.contains(original)) {
          var candidates = document.querySelectorAll(THUMB_SELECTOR);
          for (var i = 0; i < candidates.length; i++) {
            var img = imageOf(candidates[i]);
            if (img && img.src === state.sourceUrl) { original = candidates[i]; break; }
          }
        }
        var remove = original && original.querySelector(
          'button[class*="removeButton_"], button[title="Remove attachment"]'
        );
        if (!remove) {
          done(new Error('новая картинка добавлена, но кнопка удаления исходника не найдена'));
          return;
        }
        remove.click();
        done(null);
        return;
      }
      if (Date.now() - started > 5000) {
        done(new Error('новая миниатюра не появилась за 5 секунд'));
        return;
      }
      setTimeout(waitForThumbnail, 80);
    })();
  }

  function openEditor(thumb) {
    var sourceImg = imageOf(thumb);
    if (!sourceImg) return;
    closeEditor();

    var overlay = own(document.createElement('div'));
    overlay.className = 'claude-image-editor-overlay';
    var panel = own(document.createElement('section'));
    panel.className = 'claude-image-editor-panel';
    panel.setAttribute('role', 'dialog');
    panel.setAttribute('aria-modal', 'true');
    panel.setAttribute('aria-label', 'Редактор изображения');
    overlay.appendChild(panel);

    var header = own(document.createElement('header'));
    header.className = 'claude-image-editor-header';
    var title = own(document.createElement('strong'));
    title.textContent = 'Разметка изображения';
    var close = button('×', 'claude-image-editor-close', 'Отменить редактирование', closeEditor);
    header.appendChild(title);
    header.appendChild(close);
    panel.appendChild(header);

    var toolbar = own(document.createElement('div'));
    toolbar.className = 'claude-image-editor-toolbar';
    var tools = own(document.createElement('div'));
    tools.className = 'claude-image-editor-tools';
    var toolDefs = [
      ['select', '↖', 'Выбор и перемещение'],
      ['brush', '✎', 'Кисть'],
      ['line', '╱', 'Линия'],
      ['rect', '□', 'Прямоугольник'],
      ['ellipse', '○', 'Эллипс'],
    ];
    var tool = 'brush';
    var toolButtons = {};
    function chooseTool(next) {
      tool = next;
      if (canvas) canvas.dataset.tool = next;
      Object.keys(toolButtons).forEach(function (key) {
        toolButtons[key].classList.toggle('is-active', key === tool);
      });
    }
    toolDefs.forEach(function (def) {
      var b = button(def[1], 'claude-image-tool-btn', def[2], function () { chooseTool(def[0]); });
      b.dataset.tool = def[0];
      toolButtons[def[0]] = b;
      tools.appendChild(b);
    });
    toolbar.appendChild(tools);

    var colors = own(document.createElement('div'));
    colors.className = 'claude-image-editor-colors';
    var color = '#ef3340';
    var swatches = [];
    ['#ef3340', '#38b879', '#4387df', '#ffd43b', '#ffffff', '#111827'].forEach(function (value) {
      var sw = button('', 'claude-image-color-btn', 'Цвет ' + value, function () {
        color = value;
        picker.value = value;
        updateColors();
      });
      sw.style.setProperty('--annotation-color', value);
      sw.dataset.color = value;
      colors.appendChild(sw);
      swatches.push(sw);
    });
    var picker = own(document.createElement('input'));
    picker.type = 'color';
    picker.value = color;
    picker.className = 'claude-image-color-picker';
    picker.title = 'Другой цвет';
    picker.setAttribute('aria-label', 'Другой цвет');
    picker.addEventListener('input', function () { color = picker.value; updateColors(); });
    colors.appendChild(picker);
    function updateColors() {
      swatches.forEach(function (sw) {
        sw.classList.toggle('is-active', sw.dataset.color.toLowerCase() === color.toLowerCase());
      });
    }
    toolbar.appendChild(colors);

    var history = own(document.createElement('div'));
    history.className = 'claude-image-editor-history';
    var undoBtn = button('↶', 'claude-image-history-btn', 'Отменить действие (Ctrl+Z)', undo);
    var redoBtn = button('↷', 'claude-image-history-btn', 'Вернуть действие (Ctrl+Shift+Z)', redo);
    history.appendChild(undoBtn);
    history.appendChild(redoBtn);
    var widthLabel = own(document.createElement('label'));
    widthLabel.className = 'claude-image-width-label';
    widthLabel.textContent = 'Толщина';
    var widthInput = own(document.createElement('input'));
    widthInput.type = 'range';
    widthInput.min = '1';
    widthInput.max = '24';
    widthInput.value = '4';
    widthInput.className = 'claude-image-width-input';
    widthLabel.appendChild(widthInput);
    history.appendChild(widthLabel);
    toolbar.appendChild(history);
    panel.appendChild(toolbar);

    var stage = own(document.createElement('div'));
    stage.className = 'claude-image-editor-stage';
    var canvas = own(document.createElement('canvas'));
    canvas.className = 'claude-image-editor-canvas';
    canvas.tabIndex = 0;
    stage.appendChild(canvas);
    panel.appendChild(stage);

    var footer = own(document.createElement('footer'));
    footer.className = 'claude-image-editor-footer';
    var status = own(document.createElement('span'));
    status.className = 'claude-image-editor-status';
    var cancelBtn = button('Отмена', 'claude-image-editor-action', 'Закрыть без сохранения', closeEditor);
    var saveBtn = button('Сохранить', 'claude-image-editor-action is-primary', 'Сохранить и заменить вложение', save);
    footer.appendChild(status);
    footer.appendChild(cancelBtn);
    footer.appendChild(saveBtn);
    panel.appendChild(footer);

    var ctx = canvas.getContext('2d');
    var base = new Image();
    var actions = [];
    var historyEntries = [];
    var redoEntries = [];
    var draft = null;
    var drawing = false;
    var selectedIndex = -1;
    var drag = null;

    function updateHistory() {
      undoBtn.disabled = historyEntries.length === 0;
      redoBtn.disabled = redoEntries.length === 0;
    }

    function render(showSelection) {
      if (!base.naturalWidth) return;
      ctx.clearRect(0, 0, canvas.width, canvas.height);
      ctx.drawImage(base, 0, 0, canvas.width, canvas.height);
      actions.forEach(function (action) { drawAction(ctx, action); });
      drawAction(ctx, draft);
      if (showSelection !== false && selectedIndex >= 0 && actions[selectedIndex]) {
        drawSelection(ctx, actions[selectedIndex]);
      }
    }

    function undo() {
      if (!historyEntries.length) return;
      var entry = historyEntries.pop();
      if (entry.type === 'add') {
        actions.splice(entry.index, 1);
        selectedIndex = -1;
      } else if (entry.type === 'move') {
        actions[entry.index] = cloneAction(entry.before);
        selectedIndex = entry.index;
      }
      redoEntries.push(entry);
      render();
      updateHistory();
    }

    function redo() {
      if (!redoEntries.length) return;
      var entry = redoEntries.pop();
      if (entry.type === 'add') {
        actions.splice(entry.index, 0, cloneAction(entry.action));
        selectedIndex = entry.index;
      } else if (entry.type === 'move') {
        actions[entry.index] = cloneAction(entry.after);
        selectedIndex = entry.index;
      }
      historyEntries.push(entry);
      render();
      updateHistory();
    }

    function point(event) {
      var rect = canvas.getBoundingClientRect();
      return {
        x: (event.clientX - rect.left) * canvas.width / rect.width,
        y: (event.clientY - rect.top) * canvas.height / rect.height,
      };
    }

    function strokeWidth() {
      var rect = canvas.getBoundingClientRect();
      var scale = rect.width ? canvas.width / rect.width : 1;
      return Number(widthInput.value) * scale;
    }

    canvas.addEventListener('contextmenu', function (event) {
      // ПКМ зарезервирована для перемещения. Не позволяем webview/VSCode
      // открыть поверх жеста штатное контекстное меню.
      event.preventDefault();
      event.stopPropagation();
      if (event.stopImmediatePropagation) event.stopImmediatePropagation();
    });
    canvas.addEventListener('pointerdown', function (event) {
      if ((event.button !== 0 && event.button !== 2) || !base.naturalWidth) return;
      event.preventDefault();
      var p = point(event);
      // Правая кнопка всегда выбирает/двигает, какой бы инструмент ни
      // был активен. Левая делает то же только в явном режиме select.
      if (event.button === 2 || tool === 'select') {
        selectedIndex = -1;
        var tolerance = 8 * (canvas.width / Math.max(1, canvas.getBoundingClientRect().width));
        for (var i = actions.length - 1; i >= 0; i--) {
          if (hitAction(actions[i], p, tolerance)) { selectedIndex = i; break; }
        }
        drag = selectedIndex >= 0 ? {
          index: selectedIndex,
          origin: p,
          before: cloneAction(actions[selectedIndex]),
        } : null;
        drawing = !!drag;
        if (drag) {
          canvas.setPointerCapture(event.pointerId);
          canvas.dataset.dragging = 'true';
        }
        render();
        return;
      }
      canvas.setPointerCapture(event.pointerId);
      draft = { tool: tool, color: color, width: strokeWidth(), start: p, end: p, points: [p] };
      drawing = true;
      render();
    });
    canvas.addEventListener('pointermove', function (event) {
      if (!drawing || (!draft && !drag)) return;
      var p = point(event);
      if (drag) {
        actions[drag.index] = translateAction(
          drag.before,
          p.x - drag.origin.x,
          p.y - drag.origin.y
        );
        render();
        return;
      }
      if (draft.tool === 'brush') draft.points.push(p);
      draft.end = p;
      render();
    });
    function finish(event, commit) {
      if (!drawing) return;
      drawing = false;
      try { canvas.releasePointerCapture(event.pointerId); } catch (e) {}
      if (drag) {
        var moved = actions[drag.index];
        var dx = moved.start && drag.before.start ? moved.start.x - drag.before.start.x
          : moved.points[0].x - drag.before.points[0].x;
        var dy = moved.start && drag.before.start ? moved.start.y - drag.before.start.y
          : moved.points[0].y - drag.before.points[0].y;
        if (commit && (Math.abs(dx) > 0.001 || Math.abs(dy) > 0.001)) {
          historyEntries.push({
            type: 'move',
            index: drag.index,
            before: cloneAction(drag.before),
            after: cloneAction(moved),
          });
          redoEntries.length = 0;
        } else if (!commit) {
          actions[drag.index] = cloneAction(drag.before);
        }
        drag = null;
        delete canvas.dataset.dragging;
        render();
        updateHistory();
        return;
      }
      if (commit && draft) {
        actions.push(draft);
        historyEntries.push({ type: 'add', index: actions.length - 1, action: cloneAction(draft) });
        redoEntries.length = 0;
        selectedIndex = actions.length - 1;
      }
      draft = null;
      render();
      updateHistory();
    }
    canvas.addEventListener('pointerup', function (event) { finish(event, true); });
    canvas.addEventListener('pointercancel', function (event) { finish(event, false); });

    function save() {
      if (saveBtn.disabled) return;
      saveBtn.disabled = true;
      cancelBtn.disabled = true;
      status.textContent = 'Подготавливаю изображение…';
      // Рамка выбора — интерфейс редактора, в результирующий PNG она
      // попадать не должна.
      render(false);
      canvas.toBlob(function (blob) {
        // Снимок уже сформирован; возвращаем служебную рамку на случай,
        // если добавление вложения завершится ошибкой и редактор останется.
        render();
        if (!blob) {
          status.textContent = 'Не удалось создать PNG';
          saveBtn.disabled = false;
          cancelBtn.disabled = false;
          return;
        }
        var name = annotatedName(sourceImg.alt || thumb.title || 'image');
        var file = new File([blob], name, { type: 'image/png', lastModified: Date.now() });
        status.textContent = 'Прикрепляю результат…';
        replaceAttachment(active, file, function (err) {
          if (err) {
            status.textContent = err.message + '. Исходник сохранён.';
            saveBtn.disabled = false;
            cancelBtn.disabled = false;
            return;
          }
          closeEditor();
        });
      }, 'image/png');
    }

    function onKeydown(event) {
      if (event.key === 'Escape') {
        event.preventDefault();
        event.stopPropagation();
        closeEditor();
        return;
      }
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'z') {
        event.preventDefault();
        event.stopPropagation();
        if (event.shiftKey) redo(); else undo();
      } else if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'y') {
        event.preventDefault();
        event.stopPropagation();
        redo();
      }
    }

    overlay.addEventListener('mousedown', function (event) {
      if (event.target === overlay) closeEditor();
    });
    document.body.appendChild(overlay);
    document.addEventListener('keydown', onKeydown, true);
    active = {
      overlay: overlay,
      thumb: thumb,
      sourceUrl: sourceImg.src,
      keyHandler: onKeydown,
    };
    chooseTool(tool);
    updateColors();
    updateHistory();
    saveBtn.disabled = true;
    status.textContent = 'Загружаю изображение…';

    base.onload = function () {
      canvas.width = base.naturalWidth;
      canvas.height = base.naturalHeight;
      render();
      saveBtn.disabled = false;
      status.textContent = base.naturalWidth + ' × ' + base.naturalHeight;
      canvas.focus();
    };
    base.onerror = function () {
      status.textContent = 'Не удалось открыть изображение';
    };
    base.src = sourceImg.src;
  }

  function addEditButton(preview, thumbs) {
    var sourceImg = previewImageOf(preview);
    if (!sourceImg || preview.querySelector('.' + EDIT_CLASS)) return;
    var thumb = thumbForSource(sourceImg.src, thumbs);
    if (!thumb) return;
    preview.classList.add(MARK_CLASS);
    var edit = button('✎', EDIT_CLASS, 'Редактировать изображение', function (event) {
      event.preventDefault();
      event.stopPropagation();
      if (event.stopImmediatePropagation) event.stopImmediatePropagation();
      openEditor(thumb);
    });
    edit.addEventListener('mousedown', function (event) {
      event.preventDefault();
      event.stopPropagation();
    });
    preview.insertBefore(edit, preview.firstChild);
  }

  function scan(ctx) {
    var thumbs = (ctx && ctx.imageAttachments) || document.querySelectorAll(THUMB_SELECTOR);
    var previews = (ctx && ctx.imagePreviews) || document.querySelectorAll(PREVIEW_SELECTOR);
    for (var i = 0; i < previews.length; i++) addEditButton(previews[i], thumbs);
    if (active && active.thumb && !document.body.contains(active.thumb)) closeEditor();
  }

  function init() {
    window.__claudeDomWatch.register('image-annotation', scan);
  }

  // Отладочный вход и минимальная поверхность для автономного стенда.
  window.__claudeImageAnnotation = {
    open: openEditor,
    close: closeEditor,
    scan: scan,
    _test: {
      drawAction: drawAction,
      annotatedName: annotatedName,
      dispatchFile: dispatchFile,
      replaceAttachment: replaceAttachment,
      cloneAction: cloneAction,
      translateAction: translateAction,
      actionBounds: actionBounds,
      hitAction: hitAction,
    },
  };

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();

/* ============================================================
 * BLANK VIEW DETECTOR — датчик пустой вкладки
 *
 * Отказ 2026-09-01: вкладки расширения пусты, при этом бандл цел,
 * наш JS исполняется, протокол webview работает, исключений нет.
 * Разбор упёрся в то, что о самом факте «ничего не нарисовано» не
 * оставалось ни одной записи — приложение не падает, оно просто не
 * рисует, и через сутки об этом уже не спросишь.
 *
 * Датчик через BLANK_AFTER_MS смотрит, есть ли в #root содержимое, и
 * при пустоте один раз отправляет снимок состояния: смонтировался ли
 * React, сколько узлов в body, какие ключевые классы приложения
 * присутствуют, установились ли наши модули. Этого набора хватит,
 * чтобы в следующий раз сразу отделить «React не смонтировался» от
 * «нарисовал, но невидимо».
 *
 * Диагностика, а не функция: safeMode её не гасит — иначе в самом
 * безопасном режиме, куда уходят при поломке, мы снова остались бы
 * без данных.
 * ============================================================ */
(function () {
  if (window.__claudeBlankDetectorInstalled) return;
  window.__claudeBlankDetectorInstalled = true;

  var BLANK_AFTER_MS = 12000;

  function snapshot() {
    var root = document.getElementById('root');
    var body = document.body;
    var text = '';
    try { text = (root && root.innerText || '').trim(); } catch (e) {}
    return {
      hasRoot: !!root,
      rootChildren: root ? root.children.length : -1,
      rootTextLen: text.length,
      bodyChildren: body ? body.children.length : -1,
      bodyClass: body ? String(body.className).slice(0, 120) : '',
      // Классы приложения, по которым видно, дошёл ли рендер до чата
      appNodes: {
        message: document.querySelectorAll('[data-testid="assistant-message"]').length,
        container: document.querySelectorAll('[class*="messageContainer_"]').length,
        input: document.querySelectorAll('[class*="inputContainer_"]').length,
        footer: document.querySelectorAll('[class*="inputFooter_"]').length,
        session: document.querySelectorAll('[class*="sessionItem_"]').length,
      },
      ours: {
        boot: !!window.__claudeCustomBootInstalled,
        base: !!window.__claudeCustomScriptInstalled,
        accs: !!window.__claudeAccountsButtonInstalled,
        mood: !!window.__claudeMoodGaugeInstalled,
        emoji: !!window.__claudeEmojiPickerInstalled,
        mover: !!window.__claudeSessionMoverInstalled,
      },
    };
  }

  setTimeout(function () {
    var s = snapshot();
    // Признак нормы: React смонтировался и что-то нарисовал. Пустой
    // #root при живом бандле — это и есть тот самый отказ.
    if (s.hasRoot && s.rootChildren > 0 && s.rootTextLen > 0) return;
    try {
      fetch('http://localhost:18923/webview-error', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          kind: 'blank-view',
          message: 'вкладка пуста через ' + (BLANK_AFTER_MS / 1000) + ' с после загрузки',
          href: location.href.slice(0, 200),
          snapshot: s,
          ts: Date.now(),
        }),
        keepalive: true,
      }).catch(function () {});
    } catch (e) {}
  }, BLANK_AFTER_MS);
})();
