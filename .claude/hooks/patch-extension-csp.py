#!/usr/bin/env python3
"""Хук: все правки `extension.js` расширения Claude Code.

Патчей два, и живут они в одном хуке намеренно — хуки одного события
выполняются параллельно, поэтому два процесса, переписывающих один и тот
же мегабайтный файл, затёрли бы правки друг друга (прецедент с
localize.py в CLAUDE.md). Имя файла историческое: сначала был только
CSP.

1. CSP — добавляет connect-src в CSP-meta тег (см. ниже).
2. Перезапуск extension host — дописывает в конец файла блок, который
   следит за заявкой от панели Accs, вызывает
   `workbench.action.restartExtensionHost`, а после рестарта открывает
   заново вкладку, из которой заявку прислали. Нужен потому, что смена
   аккаунта провайдера подменяет ~/.claude/settings.json, а `env` оттуда
   читает CLI-процесс при старте — и стартует он один раз на активацию
   хоста. Переоткрытие вкладки нужно потому, что панель, созданную
   умершим хостом, VSCode уже не оживляет: рестарт сам по себе обновляет
   только tree view. Подробности — в комментарии к RESTART_BLOCK.

Патч 1: добавляет connect-src в CSP-meta тег extension.js Claude Code,
чтобы webview JS (наш claude-custom.js) мог делать fetch на:

  - https://api.anthropic.com — on-demand-ping проверки интернета (📡 в overlay).
  - http://localhost:*        — http-server.py: /save-log, /locale-drift
                                и любые будущие endpoint'ы локального сервиса.

Без этого патча CSP webview по умолчанию `default-src 'none'` блокирует
любой fetch (TypeError: Failed to fetch при попытке).

Идемпотентен:
  - Уже пропатчено нашим текущим CSP_CONNECT_SRC → ничего не делаем.
  - Найден старый/иной connect-src (от прошлых версий патча или из самого
    Anthropic) → удаляем его и вставляем актуальный.
  - CSP не найден (Anthropic поменял структуру до неузнаваемости) → молча
    выходим, пишет ничего.

CSP-meta тег в extension.js собирается template literal'ом вида:
    <meta http-equiv="Content-Security-Policy" content="default-src 'none';
     ${L}; ${O}; ${A}; script-src 'nonce-${D}'; ${I};">

Раньше патч искал якорь `${O};">` (предполагая что ${O} последняя
переменная), но после обновления Claude Code 2.1.126 шаблон удлинился
(${A}, ${I}), и старый якорь перестал находиться. Теперь захватывается
весь content="..." регуляркой по `${X}` внутри (отличает основной CSP
от вспомогательного error-page CSP с `{{NONCE}}` плейсхолдером).

Регистрация в .claude/settings.json — только на SessionStart. Файл
extension.js обновляется лишь при переустановке/обновлении расширения
Claude Code, поэтому повторять патч на каждом UserPromptSubmit смысла
нет (а сама операция, хоть и идемпотентна, читает мегабайтный файл
с диска).
"""

import glob
import json
import os
import re
import sys

# ext_patch лежит рядом; при запуске скриптом sys.path[0] — эта папка,
# но вставляем явно, чтобы импорт не зависел от способа запуска.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ext_patch  # noqa: E402

HOME = os.path.expanduser("~")
EXT_GLOB = os.path.join(HOME, ".vscode/extensions/anthropic.claude-code-*-linux-x64")

CSP_CONNECT_SRC = "connect-src https://api.anthropic.com http://localhost:*"

# Захватываем основной CSP-meta тег.
# Признак отличающий его от error-page CSP — наличие `${...}` (template
# literal-переменная) внутри content. Error-page CSP содержит только
# буквальный `{{NONCE}}` плейсхолдер, без `${...}`.
#
# Имя переменной НЕ фиксируем: минификатор выбирает его сам и меняет
# от сборки к сборке. В 2.1.126 это были одиночные заглавные
# (`${L}; ${O}; ${A}`), в 2.1.220 — строчные (`${p}; ${f}; ${m}`).
# Прежний паттерн `\$\{[A-Z]\}` после обновления перестал совпадать,
# патч тихо переставал применяться, и webview терял fetch к
# api.anthropic.com и localhost (пинг 📡 показывал csp_blocked,
# locale-drift и models-list не доезжали до http-server.py).
CSP_META_RE = re.compile(
    r'(<meta http-equiv="Content-Security-Policy" content=")'
    r"(default-src 'none';[^\"]*?\$\{[A-Za-z_$][A-Za-z0-9_$]*\}[^\"]*?)"
    r'(")',
    re.DOTALL,
)

# Удаляем уже существующий connect-src (любой формы), чтобы заменить
# его актуальным. Захватывает с ведущим пробелом и опционально с `;` на
# конце, чтобы при удалении не оставалось двойного пробела/висящих `;`.
OLD_CONNECT_SRC_RE = re.compile(r"\s*connect-src[^;\"]*;?")

# --- блок перезапуска extension host -----------------------------------
#
# Дописывается в конец extension.js. Файл — CommonJS-бандл (внутри уже
# есть `require("vscode")`), поэтому дописанный top-level код исполняется
# при загрузке модуля, то есть на активации расширения, и `require` там
# доступен.
RESTART_BEGIN = "/* claude-exthost-restart */"
RESTART_END = "/* /claude-exthost-restart */"

RESTART_BLOCK_RE = re.compile(
    re.escape(RESTART_BEGIN) + r".*?" + re.escape(RESTART_END),
    re.DOTALL,
)

# Токен-протокол намеренно простой: заявку пишет http-server.py, здесь
# только чтение и сверка. Разбор см. в шапке RESTART_REQUEST_FILE
# в .claude/hooks/http-server.py — оба конца обязаны совпадать по именам
# файлов и по полю `token`.
RESTART_BLOCK = RESTART_BEGIN + """
// Перезапуск extension host по заявке от панели Accs.
//
// Смена аккаунта провайдера подменяет ~/.claude/settings.json, но `env`
// оттуда применяет к себе CLI-процесс `claude` при старте, а стартует он
// один раз на активацию extension host. Значит применить смену без
// полной перезагрузки окна можно только перезапуском хоста.
//
// Рестарт хоста обновляет только tree view (сессии слева, агенты
// справа): контент webview-вкладок — диалогов Claude Code — это iframe
// в окне VSCode, он переживает смерть extension host и остаётся со
// старым JS, но уже без владельца. Поэтому активную вкладку после
// рестарта закрываем и открываем заново по sessionId (подробности —
// у reviveActiveTab).
//
// Делает это уже НОВАЯ жизнь хоста: код блока живёт в умирающем
// процессе и «после рестарта» исполнить ничего не может. Отсюда файл
// RELOAD — поручение, которое текущий процесс оставляет преемнику.
//
// Webview командой VSCode не располагает, поэтому связь через файл:
// http-server.py кладёт <workspace>/.claude/hooks-runtime/
// restart-exthost-request.json, а этот блок его читает.
//
// Область действия задаётся сама: путь выводится из корней воркспейса
// ЭТОГО окна, поэтому чужие проекты заявку не видят, а окна с тем же
// проектом перезапустятся каждое по одному разу.
try {
  (function () {
    var vscode = require("vscode");
    var fs = require("fs");
    var path = require("path");
    var REQ = "restart-exthost-request.json";
    var ACK = "restart-exthost-ack.json";
    var RELOAD = "restart-exthost-reload.json";
    var RESTART_CMD = "workbench.action.restartExtensionHost";
    var OPEN_CMD = "claude-vscode.editor.open";
    // Команда пункта «⚙ Настройки». Имя обязано совпадать с тем,
    // что вписывает в манифест patch-extension-settings.py, —
    // иначе пункт в меню будет, а обработчика у него не окажется.
    var SETTINGS_CMD = "claudeCustom.openSettings";
    var POLL_MS = 1000;
    // Пауза перед рестартом: панель Accs опрашивает ack каждые 400 мс
    // (ACK_POLL_MS в claude-custom.js) — дадим ей увидеть подтверждение
    // и отрисовать статус до того, как хост умрёт.
    var RESTART_DELAY_MS = 1600;
    // Пауза перед оживлением вкладки в новом хосте: расширение должно
    // успеть активироваться и зарегистрировать сериализатор, иначе
    // восстанавливать панель будет некому.
    var RELOAD_DELAY_MS = 1500;
    // Заявка старше этого срока — не наша: рестарта не случилось,
    // а вкладки давно живут своей жизнью.
    var RELOAD_TTL_MS = 60000;

    function readJson(file) {
      try {
        return JSON.parse(fs.readFileSync(file, "utf8"));
      } catch (e) {
        // Файла нет, или его перезаписывают прямо сейчас — штатно.
        return null;
      }
    }

    function tokenOf(file) {
      var d = readJson(file);
      return d && typeof d.token === "string" ? d.token : "";
    }

    function runtimeDirs() {
      var out = [];
      var folders = vscode.workspace.workspaceFolders || [];
      for (var i = 0; i < folders.length; i++) {
        try {
          out.push(path.join(folders[i].uri.fsPath, ".claude", "hooks-runtime"));
        } catch (e) {}
      }
      return out;
    }

    // --- журнал -------------------------------------------------------
    //
    // Пишем в файл, а не в console: вывод extension host уходит в
    // логи VSCode, где его не разобрать, а этот файл лежит рядом
    // с заявками и читается одной командой. Смотреть:
    // `tail -50 .claude/hooks-runtime/exthost-restart.log`.
    var LOG_FILE_NAME = "exthost-restart.log";
    var LOG_MAX_BYTES = 262144;

    function logPath() {
      var dirs = runtimeDirs();
      return dirs.length ? path.join(dirs[0], LOG_FILE_NAME) : "";
    }

    function log(msg) {
      var f = logPath();
      if (!f) return;
      try {
        fs.appendFileSync(
          f,
          new Date().toISOString() + " [pid:" + process.pid + "] " + msg + "\\n");
      } catch (e) {}
    }

    function rotateLog() {
      var f = logPath();
      if (!f) return;
      try {
        if (fs.statSync(f).size > LOG_MAX_BYTES) fs.writeFileSync(f, "");
      } catch (e) {}
    }

    /** Перехват webview-API расширения — только для журнала.
     *
     * Наш блок исполняется при загрузке модуля, то есть ДО вызова
     * activate(), поэтому подмена методов `vscode.window` успевает
     * встать раньше, чем расширение ими воспользуется. Так видно,
     * восстанавливает ли оно панели после рестарта хоста
     * (deserializeWebviewPanel) или создаёт их заново.
     */
    /* --- пункт «⚙ Настройки» в контекстном меню страницы -------------
     *
     * Пункт объявлен в манифесте расширения (это делает
     * patch-extension-settings.py), а страница помечена атрибутом
     * `data-vscode-context` — иначе VSCode не считает клик «нашим».
     * Здесь остаётся обработчик: команду выполняет extension host, а
     * панель настроек рисует webview, и между ними нужен мостик.
     *
     * Мостик — postMessage в webview. Файл-заявка, как у перезапуска
     * хоста, тут не годится: её пришлось бы опрашивать, а меню должно
     * отзываться сразу. Сообщение нарочно непохожее на протокол
     * приложения (`__claudeCustom`), чтобы его обработчик спокойно
     * прошёл мимо незнакомого ключа.
     *
     * Кому слать. Правый клик всегда происходит в той панели, которая
     * сейчас активна, поэтому берём её по `panel.active`. Реестр
     * пополняется в обёртках выше: панель приходит либо из
     * createWebviewPanel, либо из deserializeWebviewPanel после
     * восстановления, и оба пути ведут сюда.
     */
    var panels = [];

    function remember(panel) {
      try {
        if (!panel || panels.indexOf(panel) !== -1) return panel;
        panels.push(panel);
        // Без снятия мёртвых панелей массив рос бы всю жизнь окна, а
        // обращение к disposed-панели бросает исключение.
        if (typeof panel.onDidDispose === "function") {
          panel.onDidDispose(function () {
            var i = panels.indexOf(panel);
            if (i !== -1) panels.splice(i, 1);
          });
        }
      } catch (e) {
        log("не удалось запомнить панель: " + e);
      }
      return panel;
    }

    function activePanel() {
      for (var i = 0; i < panels.length; i++) {
        try {
          if (panels[i] && panels[i].active) return panels[i];
        } catch (e) {}
      }
      return null;
    }

    function registerSettingsCommand() {
      try {
        vscode.commands.registerCommand(SETTINGS_CMD, function () {
          var panel = activePanel();
          if (!panel) {
            // Панель настроек живёт в webview, и без него команда
            // бессильна. Молчать нельзя: пользователь нажал пункт меню
            // и вправе знать, почему ничего не произошло.
            log("команда " + SETTINGS_CMD + ": активной панели нет");
            try {
              vscode.window.showWarningMessage(
                "Claude Code: не найдено активное окно чата — "
                + "панель настроек открыть не в чем.");
            } catch (e) {}
            return;
          }
          log("команда " + SETTINGS_CMD + " → panel "
            + JSON.stringify(String(panel.title)));
          try {
            panel.webview.postMessage({ __claudeCustom: "open-settings" });
          } catch (e) {
            log("postMessage не прошёл: " + e);
          }
        });
        log("команда " + SETTINGS_CMD + " зарегистрирована");
      } catch (e) {
        // Повторная регистрация того же id бросает исключение — это
        // нормально при реактивации хоста, отдельного разбора не нужно.
        log("не удалось зарегистрировать " + SETTINGS_CMD + ": " + e);
      }
    }

    function instrumentWebviewApi() {
      try {
        var origCreate = vscode.window.createWebviewPanel;
        if (typeof origCreate === "function" && !origCreate.__claudeWrapped) {
          var wrappedCreate = function (viewType, title, showOptions, options) {
            log("createWebviewPanel viewType=" + viewType
              + " title=" + JSON.stringify(String(title))
              + " retainContextWhenHidden="
              + !!(options && options.retainContextWhenHidden));
            return remember(origCreate.apply(vscode.window, arguments));
          };
          wrappedCreate.__claudeWrapped = true;
          vscode.window.createWebviewPanel = wrappedCreate;
        }
      } catch (e) {
        log("не удалось обернуть createWebviewPanel: " + e);
      }

      try {
        var origReg = vscode.window.registerWebviewPanelSerializer;
        if (typeof origReg === "function" && !origReg.__claudeWrapped) {
          var wrappedReg = function (viewType, serializer) {
            log("registerWebviewPanelSerializer viewType=" + viewType);
            var proxy = {
              deserializeWebviewPanel: function (panel, state) {
                log("deserializeWebviewPanel viewType=" + viewType
                  + " title=" + JSON.stringify(String(panel && panel.title))
                  + " state=" + (state ? "есть" : "нет"));
                remember(panel);
                return serializer.deserializeWebviewPanel(panel, state);
              },
            };
            return origReg.call(vscode.window, viewType, proxy);
          };
          wrappedReg.__claudeWrapped = true;
          vscode.window.registerWebviewPanelSerializer = wrappedReg;
        }
      } catch (e) {
        log("не удалось обернуть registerWebviewPanelSerializer: " + e);
      }
    }

    // dir -> токен, известный на момент последней проверки. Базовый
    // снимок берётся при первом же взгляде на папку, поэтому заявка,
    // которая только что вызвала перезапуск, после реактивации уже не
    // считается новой — иначе получился бы бесконечный цикл.
    var seen = Object.create(null);

    function scan() {
      var dirs = runtimeDirs();
      for (var i = 0; i < dirs.length; i++) {
        var dir = dirs[i];
        var tok = tokenOf(path.join(dir, REQ));
        if (!(dir in seen)) { seen[dir] = tok; continue; }
        if (!tok || tok === seen[dir]) continue;
        seen[dir] = tok;

        // Подтверждение пишем ДО команды: процесс вот-вот умрёт, после
        // неё записать уже не успеем. Если команда не найдётся —
        // подтверждение снимаем, чтобы панель не считала заявку принятой.
        var ackFile = path.join(dir, ACK);
        log("новая заявка token=" + tok + " dir=" + dir);
        try {
          fs.writeFileSync(ackFile, JSON.stringify({
            token: tok, pid: process.pid, ts: Date.now(),
          }));
        } catch (e) {
          log("ack записать не удалось: " + e);
        }

        var reloadFile = path.join(dir, RELOAD);
        // Кого переоткрывать после рестарта. sessionId кладёт в заявку
        // сам webview — только он знает своё имя. Заголовок запоминаем
        // здесь: после рестарта по нему проверим, что активна всё та же
        // вкладка, а не другая, на которую успели переключиться.
        var req = readJson(path.join(dir, REQ)) || {};
        var target = activeClaudeTab();
        var handoff = {
          ts: Date.now(),
          sessionId: typeof req.sessionId === "string" ? req.sessionId : "",
          label: target ? String(target.label) : "",
        };
        log("после рестарта переоткрою session=" + (handoff.sessionId || "—")
          + " вкладка=" + JSON.stringify(handoff.label));

        // Запасной путь, когда команды рестарта в сборке нет: окно
        // целиком. Дороже, но смена аккаунта применится, и вкладки
        // перезагрузятся заодно — заявка тут лишняя.
        var fallbackReload = function () {
          try { fs.unlinkSync(reloadFile); } catch (e2) {}
          try { fs.unlinkSync(ackFile); } catch (e2) {}
          try { vscode.commands.executeCommand("workbench.action.reloadWindow"); } catch (e2) {}
        };

        setTimeout(function () {
          // Наличие команды выясняем СПИСКОМ, а не по отклонению её
          // промиса. При удачном рестарте хост умирает, все pending-RPC
          // отклоняются как «Canceled», и обработчик ошибки успевает
          // отработать перед смертью процесса — отличить это от
          // настоящего «команды нет» невозможно. Прежняя версия там и
          // стирала заявку на перезагрузку вкладок: рестарт проходил,
          // а новый хост не находил поручения.
          Promise.resolve(vscode.commands.getCommands(true)).then(function (all) {
            if (!all || all.indexOf(RESTART_CMD) === -1) {
              log("команды " + RESTART_CMD + " нет — перезагружаю окно");
              fallbackReload();
              return;
            }
            // Заявку оставляем ДО команды: после неё этот процесс уже
            // не исполнит ничего.
            try {
              fs.writeFileSync(reloadFile, JSON.stringify(handoff));
              log("заявка на переоткрытие оставлена: " + reloadFile);
            } catch (e2) {
              log("заявку на переоткрытие записать не удалось: " + e2);
            }
            log("вызываю " + RESTART_CMD);
            // Ошибку намеренно глушим пустым обработчиком: реагировать
            // на неё нельзя (см. выше), а без него это unhandled
            // rejection в логе расширения.
            Promise.resolve(
              vscode.commands.executeCommand(RESTART_CMD)
            ).then(undefined, function () {});
          }, function (err) {
            // Список команд недоступен — пробуем хотя бы окно.
            log("getCommands не ответил (" + err + ") — перезагружаю окно");
            fallbackReload();
          });
        }, RESTART_DELAY_MS);
        return;
      }
    }

    /** Активная вкладка активной группы, если это панель Claude Code. */
    function activeClaudeTab() {
      try {
        var group = vscode.window.tabGroups && vscode.window.tabGroups.activeTabGroup;
        var tab = group && group.activeTab;
        var viewType = tab && tab.input && tab.input.viewType;
        if (typeof viewType === "string" && viewType.indexOf("claude") !== -1) {
          return tab;
        }
        log("активная вкладка не панель Claude Code (viewType=" + viewType + ")");
      } catch (e) {
        log("активную вкладку определить не удалось: " + e);
      }
      return null;
    }

    /** Оживление активной вкладки после рестарта хоста.
     *
     * Панель, созданная умершим хостом, остаётся в окне без владельца:
     * заголовок и содержимое на месте, но отвечать на её сообщения
     * некому. Сама собой она не оживёт. VSCode просит расширение
     * восстановить панель (deserializeWebviewPanel) только когда та
     * СТАНОВИТСЯ видимой, а видимую он не трогает — её содержимое цело,
     * повода вмешиваться нет. Скрытые вкладки поэтому оживают при
     * первом показе, активная — никогда.
     *
     * Отсюда единственный способ: уничтожить панель и дать расширению
     * создать её заново — `claude-vscode.editor.open` зовёт
     * createPanel(sessionId, …) и открывает ИМЕННО ту переписку.
     * Мягче не выходит:
     *   - «Developer: Reload Webviews» обнуляет содержимое панели, а
     *     отвечать на запрос контента после смерти владельца некому —
     *     вкладка становится пустой при живом заголовке;
     *   - показ заново (уйти на соседнюю вкладку и вернуться) VSCode
     *     не считает поводом для восстановления: DOM остаётся прежним
     *     со всем, что на нём было.
     *
     * `claude-vscode.reopenClosedSession` тоже не годится: он берёт
     * сессию из списка недавно закрытых, а тот живёт в памяти хоста.
     * Новый хост про закрытую нами панель ничего не знает, список пуст
     * — команда молча открывала пустой диалог вместо переписки.
     */
    function reviveActiveTab(handoff) {
      var tab = activeClaudeTab();
      if (!tab) return;

      var label = String(tab.label);
      if (handoff.label && label !== handoff.label) {
        // За время рестарта переключились на другую вкладку. Трогать
        // её нельзя: она живая, а мёртвая оживёт при показе сама.
        log("активна другая вкладка (" + JSON.stringify(label) + ") — не трогаю");
        return;
      }
      if (!handoff.sessionId) {
        // Закрыть, не умея открыть ту же переписку, — хуже, чем
        // оставить мёртвую вкладку: её хотя бы видно в списке.
        log("session id неизвестен — вкладку не трогаю");
        return;
      }

      Promise.resolve(vscode.commands.getCommands(true)).then(function (all) {
        if (!all || all.indexOf(OPEN_CMD) === -1) {
          log("команды " + OPEN_CMD + " нет — вкладку не трогаю");
          return null;
        }
        log("закрываю активную вкладку " + JSON.stringify(label));
        return Promise.resolve(vscode.window.tabGroups.close(tab)).then(function () {
          log("вкладка закрыта, открываю session=" + handoff.sessionId);
          // Третий аргумент — колонка. ViewColumn.Active означает «в
          // той же группе»; конкретный номер расширение трактует иначе
          // и заодно переставляет себе предпочитаемое место.
          return vscode.commands.executeCommand(
            OPEN_CMD, handoff.sessionId, undefined, vscode.ViewColumn.Active);
        }).then(function () {
          log("переоткрытие выполнено");
        });
      }).then(undefined, function (err) {
        log("оживление вкладки отказало: " + err);
      });
    }

    /** Исполнение заявки, оставленной прошлой жизнью хоста.
     *
     * Файл намеренно НЕ удаляется: окна с тем же воркспейсом
     * перезапускаются каждое по своему расписанию, и первое же
     * удаление лишило бы остальные перезагрузки вкладок. От вечного
     * действия защищает TTL, от повторов внутри процесса — то, что
     * заявка читается один раз при старте, а не в scan().
     */
    function consumeReloadRequest() {
      var dirs = runtimeDirs();
      for (var i = 0; i < dirs.length; i++) {
        var file = path.join(dirs[i], RELOAD);
        var data = null;
        try {
          data = JSON.parse(fs.readFileSync(file, "utf8"));
        } catch (e) {
          log("заявки на перезагрузку нет в " + dirs[i]);
          continue;
        }
        if (!data || typeof data.ts !== "number") {
          log("заявка на перезагрузку без ts: " + file);
          continue;
        }
        var age = Date.now() - data.ts;
        if (age > RELOAD_TTL_MS) {
          log("заявка протухла (" + Math.round(age / 1000) + " с) — удаляю");
          try { fs.unlinkSync(file); } catch (e) {}
          continue;
        }
        log("заявка свежая (" + age + " мс), session="
          + (data.sessionId || "—") + ", жду " + RELOAD_DELAY_MS + " мс");
        setTimeout(function () { reviveActiveTab(data); }, RELOAD_DELAY_MS);
        return;
      }
    }

    rotateLog();
    log("=== блок активирован ===");
    instrumentWebviewApi();
    registerSettingsCommand();
    consumeReloadRequest();
    scan();
    var timer = setInterval(scan, POLL_MS);
    // Таймер не должен сам по себе держать процесс живым.
    if (timer && typeof timer.unref === "function") timer.unref();
  })();
} catch (e) {
  console.error("claude-exthost-restart:", e);
}
""" + RESTART_END + "\n"


def _read(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def _write_if_changed(path: str, new_content: str) -> bool:
    try:
        old = _read(path)
    except OSError:
        old = None
    if old == new_content:
        return False
    # Только атомарно: extension.js правят несколько процессов сразу
    # (хуки одного события идут параллельно), см. ext_patch.py.
    ext_patch.atomic_write(path, new_content)
    return True


def _patch_csp(content: str) -> tuple[str, str | None]:
    """Возвращает (status, new_content).

    status:
      - "not_found" — CSP-meta тег не распознан (Anthropic поменял
        структуру шаблона); патчить нечего, это повод для warning'а;
      - "already"   — connect-src уже актуален, new_content = None;
      - "patched"   — new_content содержит обновлённый файл.

    Алгоритм:
      1. Найти основной CSP-meta тег по `${...}` маркеру.
      2. Из его content удалить старый connect-src (если был).
      3. Дописать наш CSP_CONNECT_SRC перед закрывающей кавычкой.
    """
    m = CSP_META_RE.search(content)
    if not m:
        return ("not_found", None)

    inner = m.group(2)
    # Уберём любые existing connect-src (предыдущие версии, чужие)
    cleaned = OLD_CONNECT_SRC_RE.sub("", inner).rstrip()
    if not cleaned.endswith(";"):
        cleaned += ";"

    new_inner = cleaned + " " + CSP_CONNECT_SRC + ";"
    if new_inner == inner:
        return ("already", None)

    patched = (
        content[: m.start()] + m.group(1) + new_inner + m.group(3) + content[m.end():]
    )
    return ("patched", patched)


def _patch_restart_block(content: str) -> tuple[str, str | None]:
    """Вставляет/обновляет блок перезапуска extension host.

    status:
      - "already" — блок уже актуален, new_content = None;
      - "patched" — new_content содержит обновлённый файл.

    В отличие от CSP-патча провалиться здесь нечему: блок дописывается
    в конец файла и ни на какие внутренности бандла не опирается. Если
    блок от прошлой версии патча найден — заменяется целиком, чтобы не
    копить дубли.
    """
    m = RESTART_BLOCK_RE.search(content)
    if m:
        if m.group(0) == RESTART_BLOCK.rstrip("\n"):
            return ("already", None)
        patched = content[: m.start()] + RESTART_BLOCK.rstrip("\n") + content[m.end():]
        return ("patched", patched)

    tail = content if content.endswith("\n") else content + "\n"
    return ("patched", tail + "\n" + RESTART_BLOCK)


def _patch_ext_dir(ext_dir: str) -> tuple[str, str]:
    """Патчит extension.js в каталоге расширения.

    Возвращает (csp_status, restart_status). Оба патча живут в одном
    хуке и делают ОДНУ запись файла намеренно: хуки одного события
    выполняются параллельно, и два процесса, переписывающих один и тот
    же мегабайтный файл, затёрли бы правки друг друга (прецедент с
    localize.py в CLAUDE.md).

    Статусы:
      csp     — "no_file" | "unreadable" | "not_found" | "already" |
                "patched" | "write_failed";
      restart — то же, кроме "not_found".
    """
    ext_js = os.path.join(ext_dir, "extension.js")
    if not os.path.isfile(ext_js):
        return ("no_file", "no_file")
    try:
        content = _read(ext_js)
    except OSError:
        return ("unreadable", "unreadable")

    csp_status, csp_content = _patch_csp(content)
    if csp_content is not None:
        content = csp_content

    restart_status, restart_content = _patch_restart_block(content)
    if restart_content is not None:
        content = restart_content

    if csp_status != "patched" and restart_status != "patched":
        return (csp_status, restart_status)

    try:
        _write_if_changed(ext_js, content)
    except OSError:
        return (
            "write_failed" if csp_status == "patched" else csp_status,
            "write_failed" if restart_status == "patched" else restart_status,
        )
    return (csp_status, restart_status)


def _emit_context(lines: list[str]) -> None:
    """Кладёт диагностику в additionalContext SessionStart.

    Молчаливый провал этого патча уже стоил нам рабочего fetch'а
    в webview (см. шапку файла), поэтому оба интересных исхода —
    «не нашли CSP» и «только что пропатчили» — сообщаются модели
    маркером `[csp-patch WARNING]`, а она обязана показать их
    пользователю (правило в CLAUDE.md).
    """
    if not lines:
        return
    output = {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": "\n".join(lines),
        }
    }
    try:
        print(json.dumps(output, ensure_ascii=False))
    except OSError:
        pass


def main() -> int:
    messages: list[str] = []
    # extension.js этот хук правит дважды (CSP и блок перезапуска), и он же
    # мегабайтный бандл. Лок общий с остальными патчерами — файлы
    # расширения одни на все окна и проекты (см. ext_patch).
    with ext_patch.patch_lock():
        patched = [(d, _patch_ext_dir(d)) for d in glob.glob(EXT_GLOB)]

    for ext_dir, (status, restart_status) in patched:
        name = os.path.basename(ext_dir)

        if restart_status == "patched":
            messages.append(
                f"[exthost-restart WARNING] В `{name}/extension.js` только что "
                "обновлён блок перезапуска extension host. Чтобы он заработал, "
                "нужен `Developer: Reload Window` — Extension Host читает "
                "extension.js только при загрузке. До этого кнопка Accs "
                "переключит аккаунт, но применить смену предложением "
                "о перезапуске не сможет."
            )
        elif restart_status in ("unreadable", "write_failed"):
            messages.append(
                f"[exthost-restart WARNING] `{name}/extension.js` не удалось "
                f"{'прочитать' if restart_status == 'unreadable' else 'записать'} — "
                "блок перезапуска extension host не применён. Смена аккаунта "
                "в панели Accs будет требовать ручного `Developer: Reload Window`."
            )

        if status == "not_found":
            messages.append(
                f"[csp-patch WARNING] В `{name}/extension.js` не найден CSP-meta тег "
                "нужной структуры — connect-src НЕ добавлен. Значит webview не может "
                "делать fetch: пинг 📡 покажет csp_blocked, а locale-drift/models-list "
                "не дойдут до http-server.py. Проверь CSP_META_RE в "
                "`.claude/hooks/patch-extension-csp.py` — Anthropic поменял шаблон."
            )
        elif status == "patched":
            messages.append(
                f"[csp-patch WARNING] В `{name}/extension.js` только что добавлен "
                "connect-src (раньше его не было). Чтобы патч подхватился, нужен "
                "`Developer: Reload Window` — Extension Host читает extension.js "
                "только при загрузке."
            )
        elif status in ("unreadable", "write_failed"):
            messages.append(
                f"[csp-patch WARNING] `{name}/extension.js` не удалось "
                f"{'прочитать' if status == 'unreadable' else 'записать'} — "
                "патч CSP не применён. Проверь права на файл."
            )
    _emit_context(messages)
    return 0


if __name__ == "__main__":
    sys.exit(main())
