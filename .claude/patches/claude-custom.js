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

    var textEl = document.getElementById('claude-custom-debug-text');
    if (textEl) textEl.textContent = lines.join('\n');

    // Обновляем миниатюрный статус-индикатор (видён в свёрнутом виде).
    var ms = document.getElementById('claude-custom-debug-mini-status');
    if (ms) ms.textContent = statusIcon;
    var ov = document.getElementById('claude-custom-debug');
    if (ov) ov.title = statusIcon + ' ' + statusText;
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

  function init() {
    logInfo('init at', new Date().toISOString());
    var cspMeta = document.querySelector('meta[http-equiv="Content-Security-Policy"]');
    if (cspMeta) logInfo('CSP:', cspMeta.content);

    tagTimestampLines();
    refreshCustomCss();

    // Перехват click на иконке автосжатия (capture-фаза) — показывает
    // confirmation popup вместо мгновенного запуска /compact.
    document.addEventListener('click', compactClickInterceptor, true);
    // То же для пункта меню `/` «Очистить разговор» — спрашивает
    // подтверждение прежде, чем стирать текущий чат.
    document.addEventListener('click', clearClickInterceptor, true);

    new MutationObserver(function (mutations) {
      tagTimestampLines();
      lastDomMutationAt = Date.now();
      // Locale-drift detector — собираем snapshot ТОЛЬКО когда мутация
      // относится к меню `/`. Иначе debug-overlay (тикает каждые 100мс)
      // и стриминг ответов в чате постоянно сбрасывают debounce-таймер,
      // и snapshot никогда не доходит до отправки.
      if (isMenuRelatedMutation(mutations)) {
        maybeCollectAndSend();
        maybeSaveModels();
      }
    }).observe(document.body, {
      childList: true,
      subtree: true,
      characterData: true,
    });

    // Периодический пуллинг — для активных вкладок без DOM-активности.
    // refreshCustomCss сам выходит, если вкладка не visible.
    // lastPollAt тикается всегда, даже когда refresh — no-op (вкладка
    // скрыта), чтобы overlay-таймер был предсказуем.
    setInterval(function () {
      refreshCustomCss();
      lastPollAt = Date.now();
    }, POLL_MS);

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
      setInterval(function () {
        updateDebugOverlay();
        updateInlinePingIndicator();
      }, 100);
      updateDebugOverlay();
    } else {
      // Даже без overlay, inline-индикаторы должны работать
      setInterval(function () {
        updateInlinePingIndicator();
      }, 500);
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
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

  function scanSessionItems() {
    var items = document.querySelectorAll('[class*="sessionItem_"]');
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
    scanSessionItems();
    var observer = new MutationObserver(function () { scanSessionItems(); });
    observer.observe(document.body, { childList: true, subtree: true });
    // Подстраховочный таймер — на случай если observer пропустил
    // sessionItem, отрендеренный до его подключения.
    setInterval(scanSessionItems, 3000);
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
 * Добавляет кнопку 😀 в `.inputFooter_*` рядом с кнопкой меню `/`.
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
 * React перерисовывает футер, поэтому кнопка переустанавливается
 * MutationObserver'ом + подстраховочным таймером (как в SESSION MOVER).
 *
 * Управление: `emojiPicker` и `emojiRecentLimit` в
 * .claude/patches/claude-custom-config.toml.
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
  var SCAN_INTERVAL_MS = 3000;

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

  /** Сколько ячеек помещается в строку — для навигации стрелками. */
  function columnCount() {
    var cell = gridEl.querySelector('.claude-emoji-cell');
    if (!cell) return 1;
    var width = cell.offsetWidth;
    if (!width) return 1;
    return Math.max(1, Math.floor(gridEl.clientWidth / width));
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

  /* ---------- кнопка в футере ---------- */

  function createButton(footer) {
    var btn = document.createElement('button');
    btn.type = 'button'; // важно: футер внутри <form>, submit нам не нужен
    btn.className = BTN_CLASS;
    btn.title = 'Вставить смайлик';
    btn.setAttribute('aria-label', 'Вставить смайлик');
    btn.textContent = '😀';
    // Наследуем нативные классы соседней кнопки меню `/` — так наша
    // кнопка получает те же размеры и hover, что и штатные, без
    // привязки к хэшу в имени класса.
    var sibling = footer.querySelector('button[class*="menuButton_"]');
    if (sibling && sibling.className) {
      btn.className = sibling.className + ' ' + BTN_CLASS;
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

  function scanFooters() {
    var footers = document.querySelectorAll('[class*="inputFooter_"]');
    for (var i = 0; i < footers.length; i++) {
      var footer = footers[i];
      if (footer.querySelector('.' + BTN_CLASS)) continue;
      var btn = createButton(footer);
      var menuBtn = footer.querySelector('button[class*="menuButton_"]');
      if (menuBtn && menuBtn.parentNode === footer) {
        footer.insertBefore(btn, menuBtn.nextSibling);
      } else {
        var spacer = footer.querySelector('[class*="spacer_"]');
        if (spacer && spacer.parentNode === footer) footer.insertBefore(btn, spacer);
        else footer.appendChild(btn);
      }
      logInfo('кнопка встроена в футер');
    }
    // Панель без своей кнопки (футер перерисован) — закрываем, чтобы
    // не висела оторванной от анкера.
    if (panel && anchorBtn && !document.body.contains(anchorBtn)) {
      closePanel(false);
    }
  }

  function init() {
    scanFooters();
    new MutationObserver(function () { scanFooters(); })
      .observe(document.body, { childList: true, subtree: true });
    setInterval(scanFooters, SCAN_INTERVAL_MS);
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
