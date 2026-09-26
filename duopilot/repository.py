"""Shared documentation and source excerpts, pinned to a single Git snapshot.

Requests are data in one of the existing discussion messages. The coordinator
performs only allow-listed GET requests and never executes repository content.
"""
import base64
import binascii
import hashlib
import json
import re
from pathlib import PurePosixPath

from . import github

MAX_REQUESTS = 8
MAX_FILE_BYTES = 256_000
MAX_EXCERPT_CHARS = 1_800
MAX_EXCERPT_LINES = 60
_SHA = re.compile(r'[0-9a-f]{40}')
_SOURCE_SUFFIXES = {'.py', '.md', '.rst', '.toml', '.html', '.css', '.js', '.ts',
                    '.jsx', '.tsx', '.yml', '.yaml', '.jinja', '.jinja2', '.sh', '.ps1'}
_MANIFESTS = {'requirements.txt', 'requirements-dev.txt', 'package.json', 'tsconfig.json', 'dockerfile', 'makefile'}
_EXCLUDED_DIRS = {'instance', 'uploads', 'backups', 'data', 'logs', 'node_modules',
                  'venv', '__pycache__', 'vendor', 'dist', 'build'}


def readable_path(path):
    if not isinstance(path, str) or not 1 <= len(path) <= 240:
        return False
    if any(ord(c) < 32 or ord(c) == 127 for c in path) or any(c in path for c in ('\\', ':')):
        return False
    parts = path.split('/')
    if any(p in ('', '.', '..') or (p.startswith('.') and p != '.github') for p in parts):
        return False
    if any(p.lower() in _EXCLUDED_DIRS for p in parts[:-1]):
        return False
    name = parts[-1].lower()
    if name.startswith(('secrets.', 'credentials.')) or name in {'auth.json', 'id_rsa', 'id_ed25519'}:
        return False
    return PurePosixPath(name).suffix in _SOURCE_SUFFIXES or name in _MANIFESTS


def validate_requests(items):
    if not isinstance(items, list) or len(items) > 2:
        raise ValueError('Deux demandes de lecture au maximum par intervention.')
    for item in items:
        if not isinstance(item, dict) or set(item) != {'path', 'start_line'}:
            raise ValueError('Demande de lecture mal formée.')
        if not isinstance(item['path'], str) or not 1 <= len(item['path']) <= 240:
            raise ValueError('Chemin de lecture invalide.')
        if type(item['start_line']) is not int or not 1 <= item['start_line'] <= 1_000_000:
            raise ValueError('Numéro de ligne invalide.')


def _doc_rank(path):
    name = PurePosixPath(path).stem.lower()
    if name == 'readme':
        return 0
    if any(part in name for part in ('fonctionnal', 'feature', 'catalog', 'cartograph', 'presentation')):
        return 1
    if name == 'architecture':
        return 2
    if name == 'agents':
        return 3
    return 4


class RepositoryContext:
    def __init__(self, metadata, token, limit):
        self.limit = limit
        self._token = token
        info = json.loads(metadata.partition('\n')[2])
        self.repo = info['repository']
        self.commit = info.get('snapshot_commit')
        self.tree = info.get('snapshot_tree')
        self.files = {}
        self.events = []
        self.blocks = []
        self._cache = {}
        self._seen = {}
        self.used_requests = 0
        self.notice = ''
        self._base = metadata
        self.available = False
        if not (_SHA.fullmatch(str(self.commit)) and _SHA.fullmatch(str(self.tree))):
            self.notice = 'Version du code indisponible : aucune lecture de fichier possible.'
            return
        try:
            tree = github._get_object(self.repo, f'git/trees/{self.tree}', {'recursive': '1'},
                                      self._token, max_bytes=8_000_000)
            if tree.get('sha') != self.tree or not isinstance(tree.get('tree'), list):
                raise github.GitHubError('Arborescence GitHub incohérente.')
            for entry in tree['tree']:
                if not isinstance(entry, dict):
                    continue
                path = entry.get('path')
                size = entry.get('size')
                if (entry.get('type') == 'blob' and entry.get('mode') in ('100644', '100755')
                        and readable_path(path) and _SHA.fullmatch(str(entry.get('sha')))
                        and type(size) is int and 0 <= size <= MAX_FILE_BYTES):
                    self.files[path] = {'sha': entry['sha'], 'size': size}
            self.available = bool(self.files)
            paths = sorted(self.files, key=lambda p: (_doc_rank(p), len(PurePosixPath(p).parts), p))
            # Spread source candidates over folders after the main documents.
            docs = [p for p in paths if PurePosixPath(p).suffix.lower() in ('.md', '.rst')]
            doc_set = set(docs)
            sources = [p for p in paths if p not in doc_set]
            groups = {}
            for path in sources:
                groups.setdefault(str(PurePosixPath(path).parent), []).append(path)
            spread = []
            while groups and len(spread) < 120:
                for folder in list(groups):
                    spread.append(groups[folder].pop(0))
                    if not groups[folder]:
                        del groups[folder]
            catalog = {'commit': self.commit, 'file_count': len(self.files),
                       'tree_truncated': bool(tree.get('truncated')),
                       'shown_paths': (docs[:8] + spread + docs[8:])[:120],
                       'catalog_partial': True}
            catalog_budget = max(300, min(2500, limit - len(metadata) - 2200))
            while len(json.dumps(catalog, ensure_ascii=False)) > catalog_budget and catalog['shown_paths']:
                catalog['shown_paths'].pop()
            catalog['catalog_partial'] = len(catalog['shown_paths']) < len(self.files) or bool(tree.get('truncated'))
            self._base += '\n\nFICHIERS REPÉRÉS (leur nom ne prouve pas une lecture) :\n'
            self._base += json.dumps(catalog, ensure_ascii=False, separators=(',', ':'))
            self.notice = 'Aucune documentation de présentation repérée ; les extraits peuvent être demandés pendant les quatre premiers tours.'
            # The opener receives documentation before making a proposal. The
            # same excerpts remain available to the second agent without re-fetch.
            chosen = []
            for path in docs:
                if _doc_rank(path) < 4 and (not chosen or PurePosixPath(path).stem.lower() != PurePosixPath(chosen[0]).stem.lower()):
                    chosen.append(path)
                if len(chosen) == 2:
                    break
            for path in chosen:
                self._read({'path': path, 'start_line': 1}, actor=None, after_turn=0)
            if chosen:
                self.notice = 'Lectures initiales : ' + '; '.join(f"{r['path']} ({r['status']})" for r in self.events)
        except github.GitHubError as exc:
            self.notice = 'Lecture des sources indisponible : ' + str(exc)

    def render(self):
        parts = [self._base, '\n\nEXTRAITS FOURNIS AUX DEUX AGENTS :'] + self.blocks
        parts.append(f'\nDemandes de lecture restantes : {MAX_REQUESTS - self.used_requests}. '
                     f'Lecture disponible : {self.available}. {self.notice[:500]}')
        result = '\n'.join(parts)
        if len(result) > self.limit:
            raise github.GitHubError('Contexte de lecture trop volumineux.')
        return result

    def request_reads(self, items, *, actor, after_turn):
        validate_requests(items)
        if after_turn not in (1, 2, 3, 4) or actor not in ('A', 'B'):
            raise ValueError('La lecture est limitée aux quatre premiers tours.')
        results = []
        for item in items:
            if self.used_requests >= MAX_REQUESTS:
                results.append(self._record(item, actor, after_turn, 'limited', note='Nombre de demandes atteint.'))
                continue
            self.used_requests += 1
            results.append(self._read(item, actor=actor, after_turn=after_turn))
        if results:
            self.notice = '; '.join(f"{r['path']}: {r['status']} — {r.get('note', '')}" for r in results)[:500]

    def _record(self, item, actor, after_turn, status, **fields):
        event = {'number': len(self.events) + 1, 'actor': actor, 'after_turn': after_turn,
                 'path': item['path'], 'start_line': item['start_line'],
                 'snapshot_commit': self.commit, 'status': status, **fields}
        self.events.append(event)
        return event

    def _read(self, item, *, actor, after_turn):
        path, start = item['path'], item['start_line']
        if not self.available:
            return self._record(item, actor, after_turn, 'unavailable', note='Arborescence indisponible ; fichier non lu.')
        if not readable_path(path):
            return self._record(item, actor, after_turn, 'refused', note='Ce chemin est exclu de la lecture des sources.')
        if path not in self.files:
            return self._record(item, actor, after_turn, 'not_found', note='Chemin absent de la liste accessible ; ce n’est pas une preuve de bug.')
        if (path, start) in self._seen:
            return self._record(item, actor, after_turn, 'cached', reference=self._seen[(path, start)],
                                note='Extrait déjà présent dans le contexte partagé.')
        room = self.limit - len(self._base) - sum(len(b) + 1 for b in self.blocks) - 1100
        if room < 300:
            return self._record(item, actor, after_turn, 'limited', note='Taille du contexte atteinte ; fichier non lu.')
        entry = self.files[path]
        try:
            if entry['sha'] not in self._cache:
                blob = github._get_object(self.repo, f"git/blobs/{entry['sha']}", {}, self._token)
                if blob.get('sha') != entry['sha'] or blob.get('encoding') != 'base64' or not isinstance(blob.get('content'), str):
                    raise github.GitHubError('Contenu GitHub incohérent.')
                raw = base64.b64decode(''.join(blob['content'].split()), validate=True)
                digest = hashlib.sha1(b'blob ' + str(len(raw)).encode() + b'\0' + raw).hexdigest()
                if len(raw) != entry['size'] or len(raw) > MAX_FILE_BYTES or digest != entry['sha']:
                    raise github.GitHubError('Contenu différent de la version demandée.')
                content = raw.decode('utf-8-sig')
                if '\0' in content or any(ord(c) < 32 and c not in '\t\n\r' for c in content):
                    raise github.GitHubError('Fichier non textuel.')
                lines = content.split('\n') if content else []
                if content.endswith('\n'):
                    lines.pop()
                self._cache[entry['sha']] = [line.removesuffix('\r') for line in lines]
            lines = self._cache[entry['sha']]
            if start > len(lines):
                return self._record(item, actor, after_turn, 'empty', total_lines=len(lines), note='Aucune ligne à cette position.')
            reference = f'S{len(self.blocks) + 1}'
            header = f'[{reference}] {path} — commit {self.commit} — lignes à partir de {start}\n'
            budget = min(MAX_EXCERPT_CHARS, room - len(header) - 120)
            selected = []
            end = start - 1
            partial_line = False
            for number in range(start, min(len(lines) + 1, start + MAX_EXCERPT_LINES)):
                line = f'{number}: {lines[number - 1]}'
                remaining = budget - sum(len(s) + 1 for s in selected)
                if remaining < 40:
                    break
                if len(line) + 1 > remaining:
                    line = line[:remaining - 25] + ' … [ligne tronquée]'
                    partial_line = True
                selected.append(line)
                end = number
                if partial_line:
                    break
            if not selected:
                return self._record(item, actor, after_turn, 'limited', note='Espace insuffisant pour un extrait.')
            excerpt = '\n'.join(selected)
            truncated = start > 1 or end < len(lines) or partial_line
            suffix = f'\nFin de l’extrait : ligne {end} / {len(lines)}. Lecture partielle : {truncated}.'
            self.blocks.append(header + excerpt + suffix)
            self._seen[(path, start)] = reference
            return self._record(item, actor, after_turn, 'read', reference=reference,
                                end_line=end, total_lines=len(lines), truncated=truncated,
                                last_line_partial=partial_line, blob_sha=entry['sha'], content=excerpt,
                                note='Extrait partagé avec les deux agents.')
        except (github.GitHubError, UnicodeError, binascii.Error, ValueError) as exc:
            note = str(exc) if isinstance(exc, github.GitHubError) else 'Contenu textuel illisible.'
            return self._record(item, actor, after_turn, 'error', note=note)


def build_discussion_context(config, topic, mode):
    if mode != 'live':
        return github.build_context(config, topic, mode)
    limit = int(config.get('MAX_CONTEXT_CHARS', 12000))
    if limit < 8000:
        return github.build_context(config, topic, mode)
    metadata_config = dict(config, MAX_CONTEXT_CHARS=max(4000, limit // 2))
    metadata = github.build_context(metadata_config, topic, mode)
    return RepositoryContext(metadata, str(config.get('GITHUB_TOKEN') or '').strip(), limit)
