# -*- coding: utf-8 -*-
"""Fresh source evidence for project cards; never change the manual backlog."""
import json
import re
from pathlib import Path
from datetime import datetime, timezone

import memory as mem
import mdparse as md
from project_routes import inspection_path


def document_tasks(text):
    result = {}
    for line in text.splitlines():
        cells = [c.strip() for c in line.strip().strip('|').split('|')]
        if line.startswith('|') and len(cells) >= 4 and re.fullmatch(r'[A-ZА-Я]+-?\d+', cells[0]):
            gap = bool(re.fullmatch(r'P[0-2]', cells[1]))
            result[cells[0]] = {'id': cells[0], 'title': cells[-1] if gap else cells[1],
                               'status_text': cells[2] if gap else cells[-1], 'gap': gap}
    for match in re.finditer(r'^###\s+([A-ZА-Я]+-\d+)[.:]?\s+([^\n]+)\n([\s\S]*?)(?=^### |^## |\Z)', text, re.M):
        status = re.search(r'\*\*P\d\s*·\s*([^·\n]+)', match[3])
        if status:
            result[match[1]] = {'id': match[1], 'title': match[2], 'status_text': status[1].strip()}
    for i, match in enumerate(re.finditer(r'^\s*[-*]\s+\[([ xX])\]\s+(.+)$', text, re.M)):
        result['checkbox-'+str(i)] = {'id': 'checkbox-'+str(i), 'title': match[2],
                                      'status_text': 'Готово' if match[1].lower() == 'x' else 'План'}
    for match in re.finditer(r'^\s*[-*]\s+([A-ZА-Я]+-?\d+):\s*(.+?)\s+—\s+(готово|реализовано|частично|план)\.?\s*$', text, re.M | re.I):
        result[match[1]] = {'id': match[1], 'title': match[2], 'status_text': match[3]}
    for item in result.values():
        s = item['status_text'].lower()
        item['status'] = ('in_progress' if any(x in s for x in ('partial','implemented')) else 'blocked' if s.startswith('verify') else 'queued') if item.get('gap') else 'done' if s in ('готово','выполнено','реализовано','done') else 'in_progress' if 'частично' in s else 'queued'
    return list(result.values())


def build():
    data = Path(mem.DATA)
    projects = json.loads((data/'projects.json').read_text(encoding='utf-8')).get('projects', {})
    try:
        sessions = json.loads((data/'codex_sessions.json').read_text(encoding='utf-8')).get('sessions', [])
    except (OSError, ValueError):
        sessions = []
    try:
        records = json.loads((data/'memory'/'index.json').read_text(encoding='utf-8')).get('records', [])
    except (OSError, ValueError):
        records = []
    output = {}
    for key, project in projects.items():
        ss = [s for s in sessions if key in s.get('projects', [])]
        rr = [r for r in records if (r.get('project') == key or r.get('foreign_project') == key)
              and r.get('status') == 'активна' and r.get('source_provider') == 'codex']
        documents = []
        for relative in project.get('tracking_documents', []):
            path = Path(inspection_path(project)) / relative
            if not path.is_file():
                documents.append({'source': relative, 'missing': True, 'tasks': []})
                continue
            raw = path.read_text(encoding='utf-8-sig', errors='replace')
            text = md.redact_secrets(raw)[0]
            documents.append({'source': relative, 'digest': md.digest_of(text),
                'modified_at': datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(timespec='seconds'),
                'tasks': document_tasks(text)})
        output[key] = {'codex_sessions': len(ss),
            'requests': sum(r.get('role') == 'user' and r.get('root') == r['id'] for r in rr),
            'answers': sum(r.get('role') == 'assistant' and not any(p['id'] == r.get('parent') and p.get('role') == 'assistant' for p in rr) for r in rr),
            'latest': [{'id': r['id'], 'title': r['title'], 'date': r.get('message_time') or r.get('created'), 'role': r.get('role')}
                       for r in sorted(rr, key=lambda r: (r.get('message_time') or r.get('created', ''), r['id']), reverse=True)
                       if r.get('role') in ('user','assistant')][:6],
            'documents': documents}
    result = {'schema': 1, 'generated_at': datetime.now(timezone.utc).isoformat(timespec='seconds'), 'projects': output}
    (data/'project_activity.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    return result


if __name__ == '__main__':
    report = build()
    print('Сведения о работе GPT / Codex и исходных планах: %d проектов' % len(report['projects']))
