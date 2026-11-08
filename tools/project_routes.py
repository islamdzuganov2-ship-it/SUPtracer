"""Shared, evidence-based project identity. Similar names never merge projects."""
import posixpath
import re
from pathlib import Path


def norm(path):
    value = str(path or '').strip().replace('\\', '/').rstrip('/')
    return posixpath.normpath(value).lower() if value else ''


def paths(projects):
    return sorted({(norm(p), key) for key, meta in projects.items()
                   for p in [meta.get('path', '')] + list(meta.get('usage_paths') or []) if norm(p)},
                  key=lambda item: (-len(item[0]), item[0], item[1]))


def resolve_path(path, entries):
    cwd = norm(path)
    hits = [(prefix, key) for prefix, key in entries
            if cwd == prefix or cwd.startswith(prefix + '/') or
               cwd == prefix + '.worktrees' or cwd.startswith(prefix + '.worktrees/')]
    if hits:
        depth = max(len(p) for p, _ in hits)
        owners = sorted({key for prefix, key in hits if len(prefix) == depth})
        return {'project': owners[0] if len(owners) == 1 else None,
                'method': 'path' if len(owners) == 1 else 'ambiguous_path', 'candidates': owners}
    wt = re.search(r'/\.codex/worktrees/[^/]+/([^/]+)(?:/|$)', cwd)
    if wt:
        owners = sorted({key for prefix, key in entries if prefix.rsplit('/', 1)[-1] == wt[1]})
        return {'project': owners[0] if len(owners) == 1 else None,
                'method': 'codex_worktree' if len(owners) == 1 else 'ambiguous_worktree' if owners else 'unknown',
                'candidates': owners}
    return {'project': None, 'method': 'unknown', 'candidates': []}


def slug(path):
    return re.sub(r'[^A-Za-z0-9]', '-', str(path or '').replace('/', '\\').rstrip('\\')).lower()


def resolve_slug(name, projects):
    return resolve_slug_entries(name, paths(projects))


def resolve_slug_entries(name, entries, ambiguity_key=None):
    low = name.lower()
    exact = {key for path, key in entries if slug(path) == low}
    if exact:
        return next(iter(exact)) if len(exact) == 1 else ambiguity_key
    matches = [(len(slug(path)), key) for path, key in entries
               if any(low.startswith(slug(path) + suffix) for suffix in
                      ('--claude-worktrees-', '-worktrees-'))]
    if not matches:
        return None
    depth = max(n for n, _ in matches)
    owners = {key for n, key in matches if n == depth}
    return next(iter(owners)) if len(owners) == 1 else ambiguity_key


def conflicts(projects):
    by_path = {}
    for path, key in paths(projects):
        by_path.setdefault(path, set()).add(key)
    return [(path, sorted(keys)) for path, keys in by_path.items() if len(keys) > 1]


def inspection_path(meta):
    """An explicitly selected live checkout; never sum a clone and its worktree."""
    active = meta.get('inspection_path')
    return str(active) if active and Path(active).is_dir() else str(meta.get('path') or '')
