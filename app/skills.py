"""Agent Skills: storage, import/export, management and model tools.

A skill is a folder containing SKILL.md (YAML front matter with `name` and
`description`, then Markdown instructions) plus optional files such as
references/, assets/ or scripts/ (https://agentskills.io).

The agent uses skills with progressive disclosure: only name + description of
enabled skills go into the instructions; the model calls `load_skill` to read
SKILL.md and `read_skill_file` to read bundled files. Scripts are never executed.
"""
import io
import json
import re
import shutil
import stat
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

import yaml

NAME_RE = re.compile(r'^[a-z0-9]+(?:-[a-z0-9]+)*$')
MAX_NAME = 64
MAX_DESCRIPTION = 1024
MAX_ARCHIVE_BYTES = 20 * 1024 * 1024
MAX_FILES = 300
MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_TOTAL_BYTES = 50 * 1024 * 1024
MAX_READ_CHARS = 40_000  # what a tool call returns to the model
IGNORED_PARTS = {'__MACOSX', '.git', '__pycache__', '.DS_Store'}

SKILL_TEMPLATE = """---
name: {name}
description: このスキルが何をするか、どんなときに使うかを具体的に書きます（エージェントはこの説明で使うかどうかを判断します）。
---

# {name}

## 手順
1. ...
2. ...

## 注意点
- ...
"""


class SkillError(ValueError):
    """Invalid skill or request. The message is safe to show to the user."""


def parse_skill_md(text: str) -> tuple[dict, str]:
    """Split SKILL.md into (front matter, body) and validate the required fields."""
    text = text.lstrip('﻿')
    m = re.match(r'^---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|$)(.*)\Z', text, re.S)
    if not m:
        raise SkillError('SKILL.md は先頭が「---」で始まる YAML フロントマターが必要です')
    try:
        meta = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError as e:
        raise SkillError(f'フロントマターの YAML が不正です: {e}') from e
    if not isinstance(meta, dict):
        raise SkillError('フロントマターは「キー: 値」の形式で書いてください')
    name = str(meta.get('name', '')).strip()
    description = str(meta.get('description', '')).strip()
    if not name:
        raise SkillError('フロントマターに name がありません')
    if len(name) > MAX_NAME or not NAME_RE.match(name):
        raise SkillError(f'name「{name}」が不正です（英小文字・数字・ハイフンのみ、{MAX_NAME} 文字以内、先頭と末尾はハイフン不可）')
    if not description:
        raise SkillError('フロントマターに description がありません')
    if len(description) > MAX_DESCRIPTION:
        raise SkillError(f'description は {MAX_DESCRIPTION} 文字以内にしてください')
    meta['name'], meta['description'] = name, description
    return meta, m.group(2)


def check_name(name: str) -> str:
    if not NAME_RE.match(name or '') or len(name) > MAX_NAME:
        raise SkillError(f'スキル名「{name}」が不正です')
    return name


def _safe_rel(path: str) -> PurePosixPath | None:
    """Normalise an archive or user path; None if it must be skipped; raise if it escapes."""
    p = PurePosixPath(path.replace('\\', '/'))
    if p.is_absolute() or '..' in p.parts or re.match(r'^[A-Za-z]:', path):
        raise SkillError(f'不正なパスを含んでいます: {path}')
    if any(part in IGNORED_PARTS or part.startswith('._') for part in p.parts):
        return None
    return p


class SkillStore:
    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.state_file = root / '.state.json'

    # ------------------------------------------------------------ state

    def _disabled(self) -> set[str]:
        try:
            return set(json.loads(self.state_file.read_text()).get('disabled', []))
        except (OSError, ValueError):
            return set()

    def set_enabled(self, name: str, enabled: bool) -> None:
        self._dir(name)
        disabled = self._disabled()
        (disabled.discard if enabled else disabled.add)(name)
        self.state_file.write_text(json.dumps({'disabled': sorted(disabled)}))

    # ------------------------------------------------------------ read

    def _dir(self, name: str) -> Path:
        d = self.root / check_name(name)
        if not (d / 'SKILL.md').is_file():
            raise SkillError(f'スキル「{name}」はありません')
        return d

    def files(self, name: str) -> list[dict]:
        d = self._dir(name)
        return [{'path': p.relative_to(d).as_posix(), 'size': p.stat().st_size}
                for p in sorted(d.rglob('*')) if p.is_file() and not p.is_symlink()]

    def all(self) -> list[dict]:
        disabled, out = self._disabled(), []
        for d in sorted(p for p in self.root.iterdir() if p.is_dir() and (p / 'SKILL.md').is_file()):
            item = {'name': d.name, 'enabled': d.name not in disabled}
            try:
                meta, _ = parse_skill_md((d / 'SKILL.md').read_text(encoding='utf-8'))
                item |= {'description': meta['description'], 'error': None}
            except (SkillError, UnicodeDecodeError) as e:
                item |= {'description': '', 'error': str(e), 'enabled': False}
            files = self.files(d.name)
            item |= {'file_count': len(files), 'size': sum(f['size'] for f in files)}
            out.append(item)
        return out

    def get(self, name: str) -> dict:
        d = self._dir(name)
        return {'name': name, 'enabled': name not in self._disabled(),
                'content': (d / 'SKILL.md').read_text(encoding='utf-8'), 'files': self.files(name)}

    def enabled(self) -> list[dict]:
        return [s for s in self.all() if s['enabled'] and not s['error']]

    def read_file(self, name: str, path: str) -> str:
        d = self._dir(name)
        rel = _safe_rel(path)
        target = (d / rel).resolve() if rel else None
        if not target or not target.is_relative_to(d.resolve()) or not target.is_file():
            raise SkillError(f'スキル「{name}」にファイル「{path}」はありません')
        data = target.read_bytes()
        if b'\x00' in data[:8000]:
            return f'（バイナリファイルのため内容は表示できません: {path}, {len(data)} バイト）'
        text = data.decode('utf-8', errors='replace')
        return text if len(text) <= MAX_READ_CHARS else text[:MAX_READ_CHARS] + '\n…（以下省略）'

    # ------------------------------------------------------------ write

    def save(self, content: str, name: str | None = None) -> str:
        """Create or update a skill from SKILL.md text. With `name`, the front matter must match it."""
        meta, _ = parse_skill_md(content)
        if name is not None and meta['name'] != check_name(name):
            raise SkillError(f'フロントマターの name（{meta["name"]}）がスキル名（{name}）と一致しません')
        d = self.root / meta['name']
        d.mkdir(exist_ok=True)
        (d / 'SKILL.md').write_text(content, encoding='utf-8')
        return meta['name']

    def delete(self, name: str) -> None:
        shutil.rmtree(self._dir(name))
        disabled = self._disabled() - {name}
        self.state_file.write_text(json.dumps({'disabled': sorted(disabled)}))

    # ------------------------------------------------------------ import / export

    def import_bytes(self, filename: str, data: bytes, overwrite: bool = False) -> dict:
        if len(data) > MAX_ARCHIVE_BYTES:
            raise SkillError(f'ファイルが大きすぎます（上限 {MAX_ARCHIVE_BYTES // 1024 // 1024}MB）')
        if not zipfile.is_zipfile(io.BytesIO(data)):
            if filename.lower().endswith('.md') or data.lstrip(b'\xef\xbb\xbf').startswith(b'---'):
                name = parse_skill_md(data.decode('utf-8'))[0]['name']
                if (self.root / name).exists() and not overwrite:
                    return {'imported': [], 'skipped': [{'name': name, 'reason': '同名のスキルがあります'}]}
                return {'imported': [self.save(data.decode('utf-8'))], 'skipped': []}
            raise SkillError('.zip / .skill（スキルフォルダの zip）または SKILL.md を指定してください')
        return self._import_zip(zipfile.ZipFile(io.BytesIO(data)), overwrite)

    def _import_zip(self, zf: zipfile.ZipFile, overwrite: bool) -> dict:
        entries: dict[PurePosixPath, zipfile.ZipInfo] = {}
        total = 0
        for info in zf.infolist():
            if info.is_dir():
                continue
            if stat.S_ISLNK(info.external_attr >> 16):
                raise SkillError(f'シンボリックリンクは取り込めません: {info.filename}')
            rel = _safe_rel(info.filename)
            if rel is None:
                continue
            if info.file_size > MAX_FILE_BYTES:
                raise SkillError(f'ファイルが大きすぎます: {info.filename}')
            total += info.file_size
            entries[rel] = info
        if len(entries) > MAX_FILES or total > MAX_TOTAL_BYTES:
            raise SkillError('zip の中身が多すぎるか大きすぎます')
        roots = sorted({p.parent for p in entries if p.name == 'SKILL.md'}, key=lambda p: (len(p.parts), p.as_posix()))
        # A skill folder nested inside another skill is part of that skill, not a separate one.
        roots = [r for r in roots if not any(o != r and r.is_relative_to(o) for o in roots)]
        if not roots:
            raise SkillError('zip 内に SKILL.md が見つかりません')
        imported, skipped = [], []
        for root in roots:
            try:
                meta, _ = parse_skill_md(zf.read(entries[root / 'SKILL.md']).decode('utf-8'))
            except (SkillError, UnicodeDecodeError) as e:
                skipped.append({'name': f'{root.as_posix()}/SKILL.md' if root.parts else 'SKILL.md', 'reason': str(e)})
                continue
            name, target = meta['name'], self.root / meta['name']
            if target.exists() and not overwrite:
                skipped.append({'name': name, 'reason': '同名のスキルがあります'})
                continue
            with tempfile.TemporaryDirectory(dir=self.root) as tmp:
                staging = Path(tmp) / name
                for rel, info in entries.items():
                    if rel.is_relative_to(root):
                        dest = staging / rel.relative_to(root)
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        dest.write_bytes(zf.read(info))
                if target.exists():
                    shutil.rmtree(target)
                staging.rename(target)
            imported.append(name)
        return {'imported': imported, 'skipped': skipped}

    def export_zip(self, names: list[str]) -> bytes:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
            for name in names:
                d = self._dir(name)
                for f in self.files(name):
                    zf.write(d / f['path'], f'{name}/{f["path"]}')
        return buf.getvalue()

    # ------------------------------------------------------------ model integration

    def instructions(self) -> str:
        skills = self.enabled()
        if not skills:
            return ''
        lines = ['利用可能なスキル（Agent Skills）があります。ユーザーの依頼がスキルの説明に当てはまる場合は、'
                 '回答の前に load_skill でそのスキルの手順を読み込み、それに従ってください。'
                 'スキル内の参照ファイルが必要なら read_skill_file で読みます。スクリプトは実行できません。']
        lines += [f"- {s['name']}: {' '.join(s['description'].split())}" for s in skills]
        return '\n'.join(lines)

    def tool_defs(self) -> list[dict]:
        if not self.enabled():
            return []
        return [
            {'type': 'function', 'name': 'load_skill',
             'description': 'スキルの手順書（SKILL.md）と同梱ファイルの一覧を読み込む。依頼がスキルの説明に当てはまるときに最初に使う。',
             'parameters': {'type': 'object', 'properties': {'name': {'type': 'string', 'description': 'スキル名'}},
                            'required': ['name']}},
            {'type': 'function', 'name': 'read_skill_file',
             'description': 'スキルに同梱されたファイル（references/ など）を読む。',
             'parameters': {'type': 'object', 'properties': {
                 'name': {'type': 'string', 'description': 'スキル名'},
                 'path': {'type': 'string', 'description': 'スキルフォルダからの相対パス（例: references/guide.md）'}},
                 'required': ['name', 'path']}},
        ]

    def run_tool(self, tool: str, args: dict) -> str:
        name = str(args.get('name', '')).strip()
        if name not in {s['name'] for s in self.enabled()}:
            raise SkillError(f'有効なスキル「{name}」はありません')
        if tool == 'load_skill':
            files = [f['path'] for f in self.files(name) if f['path'] != 'SKILL.md']
            body = self.read_file(name, 'SKILL.md')
            return body + ('\n\n同梱ファイル:\n' + '\n'.join(f'- {p}' for p in files) if files else '')
        return self.read_file(name, str(args.get('path', '')))
