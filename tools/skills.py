"""Скиллы ролей конвейера для Claude Code и Codex — из спецификации pipeline.py.

Скилл пересказывает агенту ровно то, что проверяет сервер: этап, DoR, пункты DoD
с идентификаторами, артефакты с путями и обязательными разделами, как сдать работу.
Поэтому скиллы не пишутся руками: py tools/pm.py skills build пересобирает skills/,
тест следит, что в репозитории лежит свежая сборка.

py tools/pm.py skills build     — собрать skills/tt-<роль>/SKILL.md
py tools/pm.py skills install   — поставить в ~/.claude/skills и ~/.codex/skills
py tools/pm.py skills status    — что установлено и свежо ли
"""
import os
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pipeline as P
import recipes

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'skills'
MARK = 'tasktreker: generated'
KIND = dict(artifact='артефакт — файл в ветке задачи, сервер проверяет разделы и размер',
            evidence='доказательство — опиши в handoff, что сделано и где (≥20 символов)',
            machine='проверяет сервер', gate='решение ворот')


LIMIT = ['', '## Лимит токенов — пауза и продолжение с места остановки', '',
         '1. Пришло предупреждение, что лимит почти исчерпан, — не начинай новый шаг. Закоммить незавершённое '
         'в ветку задачи (`WIP: …`), чтобы ничего не потерялось.',
         '2. `tt_work op=pause ref=… run=… worker=… checkpoint="что сделано, что осталось, следующий шаг"` '
         '(можно `resets_at` — время сброса из сообщения о лимите) и остановись. Запуск и метка остаются за тобой.',
         '3. Сервер продолжит эту же сессию после сброса лимита: время сброса берётся из журнала сессии, '
         'без него — через 5 часов. Придёт «продолжай»: сделай `op=heartbeat`, `op=packet` (поле `resume` — твоя точка '
         'остановки) и продолжай с неё, не начиная заново.',
         '4. Если лимит оборвал тебя без предупреждения, ничего делать не нужно: сервер увидит отказ в журнале '
         'сессии и поставит паузу сам.',
         '', '## Эстафета — свежая сессия вместо разросшейся', '',
         'Каждый ход заново читает весь контекст сессии: на 150 тыс. токенов шаг стоит вдесятеро дороже, чем в свежей сессии. '
         'Пришло напоминание о потолке контекста (или сессия длинная, а до сдачи далеко) — доведи текущий шаг, закоммить и '
         '`tt_work op=pause reason=context ref=… run=… worker=… checkpoint="что сделано, что осталось, следующий шаг, '
         'нужные пути и разделы"`, затем остановись. Сервер сразу откроет свежую сессию этой роли с твоей точкой остановки: '
         'чем точнее она, тем меньше новой сессии читать заново. Если этап почти сдан — просто сдай handoff.']


def name(role):
    return 'tt-' + role.replace('_', '-')


def targets():
    claude = Path(os.environ.get('CLAUDE_CONFIG_DIR') or Path.home() / '.claude') / 'skills'
    codex = Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex') / 'skills'
    return dict(claude=claude, codex=codex)


def _stages(role):
    st = P.ROLES[role]['stage']
    return [s for s in (st if isinstance(st, tuple) else (st,)) if s]


def routing_line(role):
    rt = P.ROUTING[role]
    names = dict(claude='Claude', codex='GPT / Codex')
    order = ' → '.join(names[a] for a in rt['prefer'])
    return ('**ИИ, модель и усилия** (назначает сервер, не меняй сам): %s%s; класс модели по сложности задачи '
            '(простая / средняя / сложная) — %s, усилия — %s. Почему: %s. Если у ИИ кончились токены, задача '
            'переходит к следующему ИИ с той же ролью и точкой остановки.' % (
                order, {'developer': ' (ревьюер — всегда другой ИИ, чем разработчик)',
                        'escalator': ' (по возможности другой ИИ, чем агент с проблемой)'}.get(rt.get('cross'), ''),
                ' / '.join(str(max(x, P.ROLES[role]['min_tier'])) for x in rt['tier']), ' / '.join(rt['effort']), rt['why']))


def _primary(role):
    """Вход этапа, который роль читает целиком по смыслу своей работы (TT-96)."""
    names = [P.ROLES[r]['artifacts'][i]['path'].split('/')[-1] for r, i in P.PRIMARY_INPUTS.get(role, [])]
    return ('вход твоего этапа (%s), ' % ', '.join(names)) if names else ''


def render(role):
    r = P.ROLES[role]
    stages = _stages(role)
    where = ', '.join('«%s»' % P.STAGE[s]['title'] for s in stages) or ('проблемы и эскалации всех этапов' if role == 'resolver' else 'ворота всех этапов')
    desc = ('Роль «%s» конвейера TaskTreker: этап %s. %s Используй, когда запуск TaskTreker назначен роли %s.'
            % (r['title'], where, r['mission'], r['title']))
    out = ['---', 'name: ' + name(role), 'description: "%s"' % desc.replace('"', "'"), '---', '',
           '<!-- %s из tools/pipeline.py — не править руками: py tools/pm.py skills build -->' % MARK, '',
           '# %s' % r['title'], '', r['mission'], '', routing_line(role), '',
           ('**Участок доски:** %s. Ты — оркестратор: все роли сообщают тебе о завершении работы через журнал тимлида; '
            'сервер сам проверяет DoR/DoD и двигает задачу, а ты решаешь ворота. Эскалации и проблемы разбирает Решала.' % where) if role == 'teamlead' else
           ('**Участок:** проблемы и эскалации всех этапов и задач вне конвейера. К тебе обращаются агенты (op=escalate) '
            'и сервер — когда сессия заканчивается без сдачи, DoD проваливается раз за разом или задача ходит по кругу.') if role == 'resolver' else
           ('**Участок доски:** %s. Ты сообщаешь о завершении работы тимлиду-оркестратору: '
            'op=handoff (сдать), op=return (вернуть назад), op=escalate (проблема — к Решале). '
            'Каждое такое действие попадает в журнал тимлида.' % where), '',
           '## Скоуп', ''] + ['- ' + x for x in r['scope']]
    if role == 'teamlead':
        out += ['', '## Ворота', '',
                '- **analysis** (анализ → разработка): BRD, SRS, HLD, ADR, модель угроз и, если нужны, дизайн и GTM '
                'согласованы между собой; разработка пойдёт без двояких трактовок.',
                '- **release** (тест → main): QA Sign-off, деплой в тест и проверки на тестовой ветке чисты; риски релиза приняты. '
                'После твоего approve main ждёт «да» владельца (в пошаговом режиме; при полной автономии — нет).',
                '', '## Как решать', '',
                '1. `tt_work op=claim` своего запуска, затем `tt_work op=packet ref=… run=…` — журнал, артефакты, результаты DoD ролей.',
                '2. Начни со сводки сервера в паспорте (`digest`): коды требований и их сверка (висящие ссылки, '
                'требования вне RTM), проваленные пункты DoD, возвраты, дефекты, CI. Документы читай разделами '
                '(`tt_work op=doc … section=…`) только там, где сводка показывает расхождение или риск. Не правь их — это работа ролей. '
                'Ворота простых задач с чистой сводкой сервер закрывает сам — к тебе приходят остальные.',
                '3. `tt_work op=gate decision=approve|return notes=…` — основания не короче 30 символов. '
                'Для return укажи `to_stage` и, если нужно, `roles` — кому исправлять.'] + LIMIT
        return '\n'.join(out) + '\n'
    if role == 'resolver':
        out += ['', '## Как разбирать проблему', '',
                '1. `tt_work op=claim` своего запуска, затем `tt_work op=packet ref=… run=…` — проблема (кто, на каком этапе, что '
                'пробовал, что нужно), журнал задачи, последние запуски и их итоги.',
                '2. Разберись по фактам: рабочая копия задачи, артефакты `docs/tasks/<задача>/`, журнал, ошибки запусков. '
                'Код и артефакты ролей не правь — это работа ролей; твоё дело — решение и указания.',
                '3. Найди **не меньше двух путей решения** с плюсами и минусами. Второе мнение запроси у другого ИИ командой '
                'из паспорта (`pm.py ask <claude|codex> "вопрос с контекстом"`) и сопоставь со своими вариантами.',
                '4. Пришли решение `tt_work op=resolve ref=… run=… worker=… options=[{title, pros, cons}, …] notes="почему этот путь" decision=…`:',
                '   - `continue` + `instructions` — роль продолжает с твоими конкретными указаниями (они придут ей в замечаниях);',
                '   - `return` + `to_stage`, `roles`, `instructions` — задача возвращается на ранний этап;',
                '   - `owner` + `question`, `why_owner` — нужен владелец: вопрос появится в колокольчике и отдельным уведомлением.',
                '', '## Когда звать владельца', '',
                'Только если без него решить нельзя: нужны его права или доступы (ключи, аккаунты, оплата), деньги, '
                'юридическое решение или продуктовый выбор, который агент принимать не вправе. Всё остальное решай сам. '
                'Вопрос владельцу — короткий, с вариантами из `options`: он ответит одной кнопкой или своим текстом.']
        out += ['', '## Известные решения', '',
                'Эти проблемы сервер закрывает сам, до тебя они доходят, только если рецепт не помог:', '']
        out += ['- **%s. %s** — %s' % (r['id'], r['title'], r['instructions']) for r in recipes.catalog()]
        out += ['', 'Накопленные решения — в паспорте (`known`). Решение, которое повторится, приложи к `op=resolve` как '
                '`recipe={title, keywords, instructions}`: оно пополнит базу, и в следующий раз агенту ответит сервер.'] + LIMIT
        return '\n'.join(out) + '\n'
    for st in stages:
        title = P.STAGE[st]['title']
        out += ['', '## DoR этапа «%s» — проверяет сервер до того, как ты получишь задачу' % title, '']
        out += ['- %s' % it['text'] for it in P.DOR[st]]
        out += ['', '## DoD этапа «%s» — без него handoff не принимается' % title, '',
                '| id | Пункт | Как подтверждается | «Неприменимо» |', '|---|---|---|---|']
        for it in P.role_dod(role, st):
            out.append('| `%s` | %s | %s | %s |' % (it['id'], it['text'], KIND[it['kind']] + (' (тесты/слияние — в фоне)' if it.get('heavy') else ''),
                                                  'можно, с причиной ≥10 символов' if it.get('allow_na') else 'нельзя'))
    out += ['', '## Артефакты', '', 'Все — в рабочей копии задачи, папка `docs/tasks/<id задачи>/`, закоммичены в ветку задачи.', '']
    for a in r['artifacts']:
        out.append('- `%s`%s — разделы-заголовки: %s; не меньше %d байт.' % (
            a['path'].replace(P.ART, 'docs/tasks/<id>') + ('/ADR-NNN.md' if a.get('dir') else ''),
            ' (необязательный)' if a.get('optional') else '',
            ', '.join('«%s…»' % s for s in a['sections']) or 'свободные', a['min_bytes'])
            + (' Для продуктовой задачи (исследование рынка) ещё разделы %s и не меньше %d байт.' % (
                ', '.join('«%s…»' % s for s in a['market']), a.get('market_bytes', a['min_bytes'])) if a.get('market') else ''))
    out += ['', '## Порядок работы', '',
            '1. `tt_work op=claim ref=… run=… model=… worker=…` — ровно как в задании сессии.',
            '2. `tt_work op=packet ref=… run=…` — паспорт роли: DoD, артефакты, артефакты предыдущих этапов (`upstream`), '
            'документы проекта (`project_docs`), замечания (`feedback`). Паспорт запрашивай один раз: до сдачи этапа он не меняется.',
            '3. Документы читай по указателю: сначала `docs/tasks/<id>/INDEX.md` (если есть) — документы с разделами и реестр требований '
            'с местом определения; у артефактов в `upstream` — вес в токенах. '
            'Нужный раздел — `tt_work op=doc ref=… path=… section=<номер>` или `code=<FR-3>`, либо Read с offset/limit по строкам указателя. '
            'Целиком без вопросов — свой артефакт, %sдокумент до порога класса сложности задачи (3, 5 или 8 тыс. токенов). '
            'Крупнее — целиком только когда задача этого требует (сквозная сверка, согласованность всех разделов): '
            '`tt_work op=doc ref=… run=… path=… full=true why=<зачем>` — обоснование видит тимлид, у запуска свой бюджет таких чтений. '
            'Прочитанное не перечитывай: его текст уже в контексте. Независимые действия (чтения, поиск, git) делай одним ходом: каждый ход заново читает весь контекст. '
            'Вывод команд держи коротким: тесты `-q`, `git diff --stat` прежде полного диффа, `grep` с ограничением. '
            'Работай только в рабочей копии задачи; коммить только свои файлы '
            '(`git add <свои файлы>`; при `index.lock` повтори через несколько секунд — в копии могут работать другие роли). '
            'Если песочница вообще не даёт писать в `.git` («Permission denied»), не трать попытки: сервер сам закоммитит '
            'файлы рабочей копии при handoff, return, escalate или pause.' % _primary(role),
            '4. `tt_work op=handoff ref=… run=… worker=… summary=… items={"<id>": {"status": "done"|"na", "evidence": "…"}}`'
            + {'product_owner': ' acceptance=[бизнес-критерии приёмки] needs={"design": bool, "marketing": bool, "research": bool, "reason": "…"}'
                                ' [contract_group="…"]',
               'reviewer': ' verdict="approve"', 'qa': ' signoff=true',
               'developer': ' — после возврата ещё fixes={"D-n": {"method": …, "test_level": …, "note": …}} по каждому открытому дефекту'}.get(role, '') + '.',
            '5. Ответ `accepted: false` — список несделанных пунктов `failed`: исправь и пришли handoff снова, запуск остаётся твоим. '
            '`checking` — сервер в фоне гоняет тесты и слияния; провал вернёт задачу тебе с замечаниями.',
            '6. Ветки не сливай и main не трогай: слияния делает сервер по пройденному DoD.']
    ret = {'reviewer': 'development', 'qa': 'development', 'devops': 'development|testing', 'developer': 'analysis',
           'system_analyst': 'idea', 'solution_architect': 'idea', 'designer': 'idea', 'marketer': 'idea'}.get(role)
    if ret:
        out += ['', '## Вернуть назад', '',
                '`tt_work op=return ref=… run=… worker=… to_stage=%s reason=…` — что не так и что исправить (≥30 символов).%s'
                % (ret, ' Сначала закоммить свой отчёт с замечаниями (%s).' % ('review.md' if role == 'reviewer' else 'test-report.md')
                   if role in ('reviewer', 'qa') else '')]
    if role in ('reviewer', 'qa', 'devops'):
        out += ['', '## Дефекты', '',
                'Каждый найденный дефект регистрируй в возврате: `defects=[{"title": "суть ≥10 символов", "severity": '
                '"critical|major|minor", "level": "unit|integration|e2e|load|manual|static", "test": "TC-… (если есть)"}]`. '
                + ('Возврат QA без дефектов не принимается. ' if role == 'qa' else '')
                + 'Из реестра считаются метрики качества: сколько дефектов найдено, на каком уровне, чем исправлено.'
                + (' Подпись QA (`signoff=true`) переводит исправленные дефекты в проверенные.' if role == 'qa' else '')]
    if role == 'developer':
        out += ['', '## Исправление дефектов', '',
                'После возврата в паспорте `defects` — открытые дефекты. Сдать этап можно, только указав по каждому способ '
                'исправления в `fixes`; сервер сверяет способ с коммитами ветки после регистрации дефекта:', '',
                '- `spec_change` — изменение ТЗ: правка артефактов анализа (SRS, BRD, HLD, API, модель данных);',
                '- `code_to_spec` — приведение кода к ТЗ: правка кода;',
                '- `workaround` — обходная мера: правка кода и `note` ≥20 символов — что обходится и почему не по ТЗ;',
                '- `test_fix` — поправка самого теста: правка файлов тестов и `test_level` — уровень поправленного теста.']
    out += LIMIT
    out += ['', '## Известные проблемы', '',
            '- Не пишется `.git` (песочница) — не повторяй коммит: сервер коммитит сам при handoff, return, escalate, pause.',
            '- Файлы и вывод — только UTF-8 (PowerShell: `-Encoding utf8`, Python: `encoding="utf-8"`): передача с «????» '
            'вместо текста не принимается.',
            '- Список задач постраничный: `tt_work op=list offset=… limit=…`.',
            '- Противоречие контрактов — эскалируй: сервер вернёт контракт системному аналитику с матрицей кодов.']
    out += ['', '## Проблема или ограничение — к Решале', '',
            'Нет доступа, противоречие в требованиях, инструмент не работает, не получается после двух попыток, нужно '
            'решение, которого нет в твоём скоупе, — не жди и не спрашивай владельца. '
            '`tt_work op=escalate ref=… run=… worker=… reason="в чём проблема" tried="что уже пробовал" need="что нужно"`. '
            'Проблему разберёт агент «Решала»: найдёт пути решения и вернёт задачу с указаниями или сам позовёт владельца.']
    if role == 'product_owner':
        out += ['', '## Исследование', '',
                'Сначала реши, продуктовая ли задача. **Продуктовая** (новая ценность для пользователя): ищи мировые практики и '
                'конкурентов веб-поиском; каждое утверждение о рынке — со ссылкой в разделе «Источники»; сервер требует не меньше '
                'трёх разных ссылок и разделы «Мировые практики», «Конкуренты», «Источники». **Техническая** (рефакторинг, '
                'инфраструктура, долг, исправление): без веб-поиска — `needs.research=false`, `po-market` — «неприменимо» с причиной; '
                'BRD короче, без разделов о рынке.', '',
                '`needs.design=true` — только если задача меняет интерфейс. `contract_group` — если задача делит API или модель '
                'данных с другими задачами проекта: их анализ пойдёт по очереди, без противоречивых контрактов.']
    return '\n'.join(out) + '\n'


def build():
    OUT.mkdir(exist_ok=True)
    written = []
    for role in P.ROLES:
        path = OUT / name(role) / 'SKILL.md'
        path.parent.mkdir(exist_ok=True)
        path.write_text(render(role), encoding='utf-8')
        written.append(str(path.relative_to(ROOT)))
    return written


def install(which=('claude', 'codex')):
    """Скопировать в каталоги скиллов клиентов. Чужие скиллы с тем же именем не трогаются."""
    report = {}
    for client in which:
        base = targets()[client]; done = []
        if not base.parent.exists():
            report[client] = 'клиент не установлен (%s нет)' % base.parent; continue
        for role in P.ROLES:
            dst = base / name(role) / 'SKILL.md'
            if dst.exists() and MARK not in dst.read_text(encoding='utf-8', errors='replace'):
                report.setdefault('skipped', []).append(str(dst)); continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_text(render(role), encoding='utf-8')
            done.append(name(role))
        report[client] = '%d скиллов в %s' % (len(done), base)
    return report


def uninstall(which=('claude', 'codex')):
    for client in which:
        for role in P.ROLES:
            d = targets()[client] / name(role)
            f = d / 'SKILL.md'
            if f.exists() and MARK in f.read_text(encoding='utf-8', errors='replace'):
                shutil.rmtree(d, ignore_errors=True)


def status():
    out = {}
    for client, base in targets().items():
        fresh = sum((base / name(r) / 'SKILL.md').exists() and (base / name(r) / 'SKILL.md').read_text(encoding='utf-8') == render(r)
                    for r in P.ROLES)
        out[client] = '%d из %d свежие' % (fresh, len(P.ROLES))
    return out


def main(argv=None):
    sys.stdout.reconfigure(encoding='utf-8')
    cmd = (argv or sys.argv[1:] or ['status'])[0]
    if cmd == 'build':
        print('\n'.join(build()))
    elif cmd == 'install':
        build(); print(install())
    elif cmd == 'uninstall':
        uninstall(); print('скиллы TaskTreker сняты')
    else:
        print(status())
    return 0


if __name__ == '__main__':
    sys.exit(main())
