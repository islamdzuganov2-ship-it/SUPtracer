"""Сессия сервера досок: токен записи, проверка Origin и журнал отказов (TT-121; SEC-006/007, OBS-04, SEC-TC-07/08).

До этого запись по HTTP требовала только статический заголовок `X-TaskTreker: 1` и локальный `Host`.
Такой заголовок повторит любой локальный процесс, а Host — не удостоверение. Теперь:

  * у каждого запуска сервера свой случайный токен сессии (256 бит). Он есть только в странице,
    которую отдаёт этот сервер (`<meta name="tt-session">`), и только в памяти процесса;
    на диск не пишется. Запись без токена — 403;
  * браузерный запрос с чужим `Origin` или `Sec-Fetch-Site: cross-site|same-site` — 403, даже с
    токеном. Это закрывает CSRF и DNS rebinding вместе с проверкой Host;
  * решения владельца по-прежнему требуют токен владельца (`owner_auth`, TTU-018): токен сессии
    даёт права агента, не владельца;
  * каждый отказ записывается в `data/security/http_audit.jsonl`: время, путь, причина, Origin.
    Тел запросов и токенов в журнале нет.

Ограничение, которое нельзя снять этим модулем: процесс того же пользователя Windows может открыть
страницу и прочитать токен. Защита от него — отдельный пользователь и ACL для исполнителей (SEC-009,
`docs/БЕЗОПАСНОСТЬ.md`).
"""
import hmac
import json
import os
import secrets
from datetime import datetime, timezone
from pathlib import Path

HEADER = 'X-TaskTreker-Session'
META = 'tt-session'
TOKEN = secrets.token_hex(32)
LOCAL_HOSTS = ('127.0.0.1', 'localhost', '[::1]')


def check(value):
    return isinstance(value, str) and len(value) == len(TOKEN) and hmac.compare_digest(value, TOKEN)


def origin_ok(headers, port):
    """None — запрос допустим по происхождению, иначе причина отказа."""
    site = (headers.get('Sec-Fetch-Site') or '').lower()
    if site in ('cross-site', 'same-site'):
        return 'запрос с другого сайта (Sec-Fetch-Site: %s)' % site
    origin = headers.get('Origin')
    if origin is None:
        return None                       # не браузер или тот же источник без Origin: решает токен сессии
    allowed = {'http://%s:%s' % (h, port) for h in LOCAL_HOSTS}
    if origin not in allowed:
        return 'чужой Origin: %s' % origin[:100]
    return None


def inject(html):
    """Токен сессии — в страницу дашборда, которую отдаёт этот сервер."""
    tag = ('<meta name="%s" content="%s">' % (META, TOKEN)).encode('ascii')
    i = html.find(b'<head>')
    return html[:i + 6] + tag + html[i + 6:] if i >= 0 else tag + html


def _audit_path():
    base = os.environ.get('TASKTREKER_DATA') or str(Path(__file__).resolve().parent.parent / 'data')
    return Path(base) / 'security' / 'http_audit.jsonl'


def audit(path, reason, headers, code=403):
    """Отказ — в журнал. Сбой записи журнала не меняет ответ (отказ остаётся отказом)."""
    try:
        p = _audit_path(); p.parent.mkdir(parents=True, exist_ok=True)
        row = dict(at=datetime.now(timezone.utc).isoformat(timespec='seconds'), path=path[:120], code=code, reason=reason[:200],
                   origin=(headers.get('Origin') or '')[:100], site=(headers.get('Sec-Fetch-Site') or '')[:20],
                   agent=(headers.get('User-Agent') or '')[:80])
        with open(p, 'a', encoding='utf-8') as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + '\n')
        import sec_audit
        sec_audit.record('http_denied', '%s %s: %s' % (code, path[:120], reason[:200]), op='http', origin='http')
    except OSError:
        pass
