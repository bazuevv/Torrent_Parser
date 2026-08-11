#!/usr/bin/env python3
"""HTTP-сервер плеера: статика + общая база аккаунтов.

`python -m http.server` умел только отдавать файлы, поэтому база ников жила
в localStorage — своя у каждого браузера, да ещё и отдельная для 127.0.0.1
и 192.168.1.100 (это разные origin). Здесь она общая: accounts.json лежит
рядом с плеером, читается и пополняется всеми клиентами сразу.

  GET  /api/accounts  -> {"accounts": [["Ник","67",3301], …]}
  POST /api/accounts  <- [["Ник","67",3301], …]  (сливается с существующей)
                      -> {"taken": N, "added": M, "total": K}
"""

import json
import os
import re
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.abspath(__file__))
STORE = os.path.join(ROOT, 'accounts.json')
PORT = 8777

EDGE_RE = re.compile(r'^(\d+|us\d+)?$')      # номер сервера, либо пусто
LOCK = threading.Lock()                      # сервер многопоточный, запись сериализуем
MAX_BODY = 32 * 1024 * 1024                  # 78 тыс. ников укладываются в ~3 МБ


def load():
    try:
        with open(STORE, encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def merge(rows):
    """Сливает строки [ник, edge, зрители] в базу. -> (принято, новых, всего)."""
    with LOCK:
        base = load()
        taken = added = 0

        for row in rows:
            if not isinstance(row, list) or not row:
                continue
            user = row[0]
            edge = '' if len(row) < 2 or row[1] is None else str(row[1])
            if not isinstance(user, str) or not user or not EDGE_RE.match(edge):
                continue

            viewers = 0
            if len(row) > 2:
                try:
                    viewers = int(row[2])
                except (TypeError, ValueError):
                    viewers = 0

            key = user.lower()
            prev = base.get(key)
            if prev is None:
                added += 1
            else:
                # Запись с известным сервером не затираем пустой.
                edge = edge or prev[1]
                viewers = viewers or prev[2]

            base[key] = [user, edge, viewers]
            taken += 1

        tmp = STORE + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(base, f, ensure_ascii=False, separators=(',', ':'))
        os.replace(tmp, STORE)               # подмена атомарная: читатели не увидят обрывок

        return taken, added, len(base)


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=ROOT, **kwargs)

    def _json(self, payload, code=200):
        body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.split('?')[0] == '/api/accounts':
            return self._json({'accounts': list(load().values())})
        return super().do_GET()

    def do_POST(self):
        if self.path.split('?')[0] != '/api/accounts':
            return self.send_error(404)

        try:
            length = int(self.headers.get('Content-Length') or 0)
        except ValueError:
            return self.send_error(400, 'bad length')
        if length <= 0 or length > MAX_BODY:
            return self.send_error(413, 'bad body size')

        try:
            rows = json.loads(self.rfile.read(length))
        except ValueError:
            return self.send_error(400, 'bad json')
        if not isinstance(rows, list):
            return self.send_error(400, 'expected array')

        taken, added, total = merge(rows)
        self.log_message('accounts: принято %d, новых %d, всего %d', taken, added, total)
        self._json({'taken': taken, 'added': added, 'total': total})


if __name__ == '__main__':
    print(f'Плеер: http://127.0.0.1:{PORT}/player.html')
    ThreadingHTTPServer(('0.0.0.0', PORT), Handler).serve_forever()
