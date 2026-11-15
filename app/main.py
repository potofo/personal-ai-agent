"""Personal AI agent: web chat + voice chat on an Azure OpenAI Realtime model.

Each Coder workspace runs one instance of this app for one user, with that
user's own Azure OpenAI API key. The key never leaves the backend: text chat
goes through a server-side Realtime WebSocket, and voice chat uses WebRTC with
an ephemeral client secret that the backend mints and uses itself to relay the
SDP offer/answer.
"""
import asyncio
import functools
import json
import logging
import os
import sqlite3
import uuid
from contextlib import asynccontextmanager, closing
from datetime import datetime
from pathlib import Path
from typing import Literal
from urllib.parse import quote, urlparse
from zoneinfo import ZoneInfo

import httpx
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field
from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import InvalidStatus, WebSocketException

from .skills import SKILL_TEMPLATE, SkillError, SkillStore, check_name, parse_skill_md

log = logging.getLogger('personal-agent')
logging.basicConfig(level=os.getenv('LOG_LEVEL', 'INFO'))

DATA = os.getenv('DATA_DIR', '/data')
os.makedirs(DATA, exist_ok=True)
DB = os.path.join(DATA, 'agent.db')
TZ = os.getenv('APP_TIMEZONE', 'Asia/Tokyo')
STATIC = Path(__file__).parent / 'static'
skills = SkillStore(Path(DATA) / 'skills')
DEFAULT_SKILLS = Path(__file__).parent / 'default_skills'
CONTEXT_MESSAGES = int(os.getenv('CONTEXT_MESSAGES', '20'))
BASE_INSTRUCTIONS = os.getenv('AGENT_INSTRUCTIONS') or (
    'あなたはユーザー専属の日本語パーソナルアシスタントです。簡潔で親切に答えてください。'
    'ツールが無い操作（メール送信など）を実行したふりをしてはいけません。'
)
scheduler = BackgroundScheduler(timezone=TZ)


# ---------------------------------------------------------------- settings

class AzureError(Exception):
    """An error reported by (or while talking to) Azure OpenAI. Safe to show the owner."""


def settings() -> dict:
    endpoint = os.getenv('AZURE_OPENAI_ENDPOINT', '').strip().rstrip('/')
    if '/openai' in endpoint:  # accept a pasted ".../openai/v1/..." URL
        endpoint = endpoint.split('/openai', 1)[0]
    return {
        'endpoint': endpoint,
        'api_key': os.getenv('AZURE_OPENAI_API_KEY', '').strip(),
        'deployment': (os.getenv('AZURE_OPENAI_REALTIME_DEPLOYMENT')
                       or os.getenv('AZURE_OPENAI_DEPLOYMENT') or 'gpt-realtime-2.1-mini').strip(),
        'voice': os.getenv('REALTIME_VOICE', 'marin').strip(),
        'transcribe_model': os.getenv('REALTIME_TRANSCRIBE_MODEL', 'whisper-1').strip(),
        'transcribe_language': os.getenv('REALTIME_TRANSCRIBE_LANGUAGE', 'ja').strip(),
        # Chat model deployment that supports the Responses API web_search tool. Empty = no web search.
        'search_deployment': os.getenv('WEB_SEARCH_DEPLOYMENT', '').strip(),
    }


def require_settings() -> dict:
    s = settings()
    missing = [k for k, v in (('AZURE_OPENAI_ENDPOINT', s['endpoint']), ('AZURE_OPENAI_API_KEY', s['api_key'])) if not v]
    if missing:
        raise AzureError('未設定: ' + ', '.join(missing) + '（Coder のワークスペース設定でパラメーターを入力してください）')
    return s


def azure_error(r: httpx.Response) -> AzureError:
    try:
        msg = r.json().get('error', {}).get('message') or r.text
    except ValueError:
        msg = r.text
    return AzureError(f'Azure OpenAI HTTP {r.status_code}: {msg[:500]}')


# ---------------------------------------------------------------- storage

JOBS_CONVERSATION = 'jobs'  # scheduled job results go to their own conversation


def now_iso() -> str:
    return datetime.now(ZoneInfo(TZ)).isoformat(timespec='seconds')


def db() -> sqlite3.Connection:
    c = sqlite3.connect(DB, timeout=10)
    c.execute('PRAGMA journal_mode=WAL')
    c.execute('CREATE TABLE IF NOT EXISTS messages (id INTEGER PRIMARY KEY, role TEXT, content TEXT, created TEXT)')
    c.execute('CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, hour INTEGER, minute INTEGER, prompt TEXT)')
    c.execute('CREATE TABLE IF NOT EXISTS conversations (id TEXT PRIMARY KEY, title TEXT NOT NULL, '
              "kind TEXT NOT NULL DEFAULT 'chat', created TEXT NOT NULL, updated TEXT NOT NULL)")
    cols = [r[1] for r in c.execute('PRAGMA table_info(messages)')]
    if 'channel' not in cols:
        c.execute("ALTER TABLE messages ADD COLUMN channel TEXT NOT NULL DEFAULT 'text'")
    if 'conversation_id' not in cols:
        c.execute('ALTER TABLE messages ADD COLUMN conversation_id TEXT')
        c.execute('CREATE INDEX IF NOT EXISTS messages_conversation ON messages(conversation_id, id)')
    # Messages from before conversations existed become one conversation ("これまでの会話").
    if c.execute('SELECT 1 FROM messages WHERE conversation_id IS NULL LIMIT 1').fetchone():
        first, last = c.execute('SELECT MIN(created), MAX(created) FROM messages WHERE conversation_id IS NULL').fetchone()
        legacy = str(uuid.uuid4())
        c.execute('INSERT INTO conversations VALUES(?,?,?,?,?)', (legacy, 'これまでの会話', 'chat', first or now_iso(), last or now_iso()))
        c.execute('UPDATE messages SET conversation_id=? WHERE conversation_id IS NULL', (legacy,))
    c.commit()
    return c


def create_conversation(title: str = '新しいチャット', kind: str = 'chat', conv_id: str | None = None) -> str:
    conv_id = conv_id or str(uuid.uuid4())
    with closing(db()) as c, c:
        c.execute('INSERT OR IGNORE INTO conversations VALUES(?,?,?,?,?)', (conv_id, title, kind, now_iso(), now_iso()))
    return conv_id


def conversation_exists(conv_id: str) -> bool:
    with closing(db()) as c:
        return c.execute('SELECT 1 FROM conversations WHERE id=?', (conv_id,)).fetchone() is not None


def record(role: str, content: str, channel: str, conv_id: str) -> None:
    with closing(db()) as c, c:
        ts = now_iso()
        c.execute('INSERT INTO messages(role,content,created,channel,conversation_id) VALUES(?,?,?,?,?)',
                  (role, content, ts, channel, conv_id))
        c.execute('UPDATE conversations SET updated=? WHERE id=?', (ts, conv_id))
        if role == 'user':  # name a new chat after its first message
            title = ' '.join(content.split())[:40]
            c.execute("UPDATE conversations SET title=? WHERE id=? AND title='新しいチャット'", (title, conv_id))


def recent_messages(conv_id: str, limit: int) -> list[dict]:
    with closing(db()) as c:
        rows = c.execute('SELECT role,content,created,channel FROM messages WHERE conversation_id=? '
                         'ORDER BY id DESC LIMIT ?', (conv_id, limit)).fetchall()
    return [{'role': r, 'content': t, 'created': d, 'channel': ch} for r, t, d, ch in reversed(rows)]


def instructions(conv_id: str | None, channel: str = 'text') -> str:
    """Base instructions plus this conversation's recent history (shared by its text and voice turns)."""
    now = datetime.now(ZoneInfo(TZ)).strftime('%Y-%m-%d %H:%M')
    lines = [BASE_INSTRUCTIONS, f'現在日時: {now}（{TZ}）']
    if channel == 'voice':
        lines.append('いまは音声で会話しています。Markdown の記号やコード、URL は読み上げず、自然な話し言葉で答えてください。'
                     '図やインフォグラフィックを求められたら、要点を口頭で伝え、テキストチャットで頼めば図を表示できると案内してください。')
    else:
        lines.append('回答は Markdown（見出し、箇条書き、表、太字、絵文字）で読みやすく書いてかまいません。'
                     'インフォグラフィック、図解、チャート、ダッシュボード風のまとめなど視覚的な表示を求められたら、短い説明に続けて '
                     '```html のコードブロックを 1 つ出力してください。画面がそれを図として描画します。'
                     'HTML は自己完結させ、インラインの CSS と SVG だけを使います（<script>、外部の画像・フォント・CSS の読み込みは禁止）。'
                     '幅は 100%（最大 760px 程度）で、スマートフォンでも崩れないようにし、天気などはアイコンを SVG か絵文字で描いてください。')
    if settings()['search_deployment']:
        lines.append('ニュース、株価、天気、最近の出来事、自信のない事実など、最新または正確な情報が必要な質問には、'
                     '推測で答えずに web_search ツールで調べてから、出典（サイト名）を添えて答えてください。'
                     '音声で話しているときは、検索の前に「調べますね」と短く伝えてください。')
    if skill_text := skills.instructions():
        lines.append(skill_text)
    history = recent_messages(conv_id, CONTEXT_MESSAGES) if conv_id else []
    if history:
        names = {'user': 'ユーザー', 'assistant': 'アシスタント', 'job': '定期ジョブ結果'}
        lines.append('以下はこのチャットのこれまでの記録（テキスト・音声）です。文脈として参照してください。')
        lines += [f"[{names.get(m['role'], m['role'])}] {m['content'][:1500]}" for m in history]
    return '\n'.join(lines)


# ---------------------------------------------------------------- tools

WEB_SEARCH_TOOL = {
    'type': 'function',
    'name': 'web_search',
    'description': 'インターネットで最新情報を検索し、要点と出典を返す。ニュース、株価、天気、最近の出来事、'
                   '知らない固有名詞など、最新または正確な情報が必要なときに使う。',
    'parameters': {
        'type': 'object',
        'properties': {'query': {'type': 'string', 'description': '調べたい内容。具体的に（日本語可）'}},
        'required': ['query'],
    },
}


def tool_defs(s: dict) -> list[dict]:
    return ([WEB_SEARCH_TOOL] if s['search_deployment'] else []) + skills.tool_defs()


async def web_search(query: str) -> str:
    """Search the web through the Azure OpenAI Responses API web_search tool on a chat deployment."""
    s = require_settings()
    if not s['search_deployment']:
        raise AzureError('Web 検索は無効です（WEB_SEARCH_DEPLOYMENT が未設定）')
    now = datetime.now(ZoneInfo(TZ)).strftime('%Y-%m-%d %H:%M')
    body = {
        'model': s['search_deployment'],
        'input': query,
        'tools': [{'type': 'web_search'}],
        'instructions': f'現在日時は {now}（{TZ}）。Web を検索し、質問への答えの要点を日本語で簡潔に（8 文以内）まとめ、'
                        '最後に主要な出典を「- サイト名: URL」の形式で最大 5 件挙げてください。',
    }
    try:
        async with httpx.AsyncClient(timeout=120) as http:
            r = await http.post(f"{s['endpoint']}/openai/v1/responses", headers={'api-key': s['api_key']}, json=body)
    except httpx.HTTPError as e:
        raise AzureError(f'Web 検索の接続エラー: {type(e).__name__}: {e}') from e
    if r.status_code >= 400:
        raise azure_error(r)
    text = ''.join(c.get('text', '') for o in r.json().get('output', []) if o.get('type') == 'message'
                   for c in o.get('content', []) if c.get('type') == 'output_text')
    return text.strip() or '検索結果が得られませんでした。'


async def run_tool(name: str, arguments: str) -> str:
    """Execute a tool call from the model. Errors are returned to the model as text, not raised."""
    try:
        args = json.loads(arguments or '{}')
        if name == 'web_search':
            query = str(args.get('query', '')).strip()[:500]
            if not query:
                return 'エラー: query が空です'
            log.info('web_search: %s', query)
            return await web_search(query)
        if name in ('load_skill', 'read_skill_file', 'list_skills', 'save_skill', 'write_skill_file'):
            log.info('%s: %s', name, {k: (v[:80] + '…' if isinstance(v, str) and len(v) > 80 else v) for k, v in args.items()})
            return skills.run_tool(name, args)
        return f'エラー: 不明なツール {name}'
    except (AzureError, SkillError, ValueError) as e:
        log.warning('tool %s failed: %s', name, e)
        return f'ツール実行エラー: {e}'


# ---------------------------------------------------------------- realtime: text

def _ws_url(s: dict) -> str:
    u = urlparse(s['endpoint'])
    scheme = 'ws' if u.scheme == 'http' else 'wss'  # http only for local mocks
    return f"{scheme}://{u.netloc}/openai/v1/realtime?model={quote(s['deployment'])}"


async def realtime_text(prompt: str, instr: str) -> str:
    """Send one user turn to the Realtime model over WebSocket and return the text reply."""
    s = require_settings()
    try:
        async with ws_connect(_ws_url(s), additional_headers={'api-key': s['api_key']},
                              open_timeout=20, max_size=None) as ws:
            session = {'type': 'realtime', 'instructions': instr, 'output_modalities': ['text']}
            if tool_defs(s):
                session |= {'tools': tool_defs(s), 'tool_choice': 'auto'}
            response_create = {'type': 'response.create', 'response': {'output_modalities': ['text']}}
            for event in (
                {'type': 'session.update', 'session': session},
                {'type': 'conversation.item.create', 'item': {'type': 'message', 'role': 'user',
                                                              'content': [{'type': 'input_text', 'text': prompt}]}},
                response_create,
            ):
                await ws.send(json.dumps(event))
            parts: list[str] = []
            tool_rounds = 0
            async with asyncio.timeout(300):
                async for raw in ws:
                    ev = json.loads(raw)
                    kind = ev.get('type')
                    if kind in ('response.output_text.delta', 'response.text.delta'):
                        parts.append(ev.get('delta', ''))
                    elif kind == 'error':
                        raise AzureError('Realtime error: ' + str(ev.get('error', {}).get('message', ev)))
                    elif kind == 'response.done':
                        resp = ev.get('response', {})
                        if resp.get('status') not in (None, 'completed'):
                            raise AzureError(f"応答が完了しませんでした: {resp.get('status')} {resp.get('status_details')}")
                        calls = [o for o in resp.get('output', []) if o.get('type') == 'function_call']
                        if calls and tool_rounds < 5:  # run the tools, hand back the results, ask again
                            tool_rounds += 1
                            for call in calls:
                                output = await run_tool(call.get('name', ''), call.get('arguments', ''))
                                await ws.send(json.dumps({'type': 'conversation.item.create', 'item': {
                                    'type': 'function_call_output', 'call_id': call.get('call_id'), 'output': output}}))
                            await ws.send(json.dumps(response_create))
                            parts = []
                            continue
                        if not parts:  # fall back to the final item
                            for item in resp.get('output', []):
                                for c in item.get('content', []):
                                    parts.append(c.get('text') or c.get('transcript') or '')
                        return ''.join(parts).strip()
    except InvalidStatus as e:
        body = e.response.body.decode(errors='replace')[:300] if e.response.body else ''
        raise AzureError(f'Realtime WebSocket 接続失敗 HTTP {e.response.status_code}: {body}') from e
    except (WebSocketException, OSError, TimeoutError) as e:
        raise AzureError(f'Realtime WebSocket エラー: {type(e).__name__}: {e}') from e
    raise AzureError('応答を受け取る前に接続が閉じられました')


# ---------------------------------------------------------------- realtime: voice

def voice_session(s: dict, instr: str) -> dict:
    audio_input: dict = {'turn_detection': {'type': 'server_vad'}}
    if s['transcribe_model']:
        t = {'model': s['transcribe_model']}
        if s['transcribe_language']:
            t['language'] = s['transcribe_language']
        audio_input['transcription'] = t
    session = {'type': 'realtime', 'model': s['deployment'], 'instructions': instr,
               'audio': {'input': audio_input, 'output': {'voice': s['voice']}}}
    if tool_defs(s):  # tool calls arrive in the browser, which runs them through /api/tools/call
        session |= {'tools': tool_defs(s), 'tool_choice': 'auto'}
    return session


async def webrtc_answer(offer_sdp: str, conv_id: str) -> str:
    """Mint a short-lived client secret with the user's key, then exchange SDP with Azure."""
    s = require_settings()
    calls_url = os.getenv('AZURE_OPENAI_REALTIME_WEBRTC_URL') or f"{s['endpoint']}/openai/v1/realtime/calls?webrtc_filter=on"
    try:
        async with httpx.AsyncClient(timeout=30) as http:
            r = await http.post(f"{s['endpoint']}/openai/v1/realtime/client_secrets",
                                headers={'api-key': s['api_key']},
                                json={'session': voice_session(s, instructions(conv_id, 'voice'))})
            if r.status_code >= 400:
                raise azure_error(r)
            body = r.json()
            token = body.get('value') or (body.get('client_secret') or {}).get('value')
            if not token:
                raise AzureError('一時トークンを取得できませんでした')
            r = await http.post(calls_url, content=offer_sdp,
                                headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/sdp'})
            if r.status_code >= 400:
                raise azure_error(r)
            return r.text
    except httpx.HTTPError as e:
        raise AzureError(f'Azure OpenAI への接続エラー: {type(e).__name__}: {e}') from e


# ---------------------------------------------------------------- jobs

def run_job(job_id: str, prompt: str) -> None:
    try:
        answer = asyncio.run(realtime_text(prompt, instructions(None)))
    except Exception as e:  # noqa: BLE001 - record any failure in history
        log.exception('job %s failed', job_id)
        answer = f'ジョブ実行失敗: {e}'
    create_conversation('⏰ 定期ジョブの結果', 'jobs', JOBS_CONVERSATION)
    record('job', f'{prompt}\n→ {answer}', 'job', JOBS_CONVERSATION)


def register(job_id: str, hour: int, minute: int, prompt: str) -> None:
    scheduler.add_job(run_job, CronTrigger(hour=hour, minute=minute, timezone=TZ), args=[job_id, prompt],
                      id=job_id, replace_existing=True, max_instances=1, coalesce=True, misfire_grace_time=600)


@asynccontextmanager
async def lifespan(_app):
    with closing(db()) as c:
        rows = c.execute('SELECT id,hour,minute,prompt FROM jobs').fetchall()
    try:
        if added := skills.seed_defaults(DEFAULT_SKILLS):
            log.info('seeded default skills: %s', ', '.join(added))
    except (OSError, SkillError) as e:  # never block startup on this
        log.warning('could not seed default skills: %s', e)
    scheduler.start()
    for row in rows:
        register(*row)
    yield
    scheduler.shutdown(wait=False)


# ---------------------------------------------------------------- API

app = FastAPI(lifespan=lifespan, title='Personal AI Agent')


class Chat(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    conversation_id: str | None = Field(default=None, max_length=64)


class Transcript(BaseModel):
    role: Literal['user', 'assistant']
    content: str = Field(min_length=1, max_length=8000)
    conversation_id: str = Field(min_length=1, max_length=64)


class ConversationTitle(BaseModel):
    title: str = Field(min_length=1, max_length=100)


class ToolCall(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    arguments: str = Field(default='{}', max_length=8000)


class SkillContent(BaseModel):
    content: str = Field(min_length=1, max_length=200_000)


class SkillEnabled(BaseModel):
    enabled: bool


class Job(BaseModel):
    hour: int = Field(ge=0, le=23)
    minute: int = Field(ge=0, le=59)
    prompt: str = Field(min_length=1, max_length=4000)


@app.get('/health')
def health():
    return {'ok': True}


@app.get('/api/status')
def status():
    s = settings()
    return {'configured': bool(s['endpoint'] and s['api_key']), 'endpoint_host': urlparse(s['endpoint']).netloc,
            'deployment': s['deployment'], 'voice': s['voice'], 'timezone': TZ,
            'web_search': s['search_deployment'] or None}


def require_conversation(conv_id: str) -> str:
    if not conversation_exists(conv_id):
        raise HTTPException(404, 'チャットが見つかりません（削除された可能性があります）')
    return conv_id


@app.post('/api/chat')
async def chat(req: Chat):
    """Without conversation_id a new chat is created; the response says which one was used."""
    conv_id = require_conversation(req.conversation_id) if req.conversation_id else create_conversation()
    instr = instructions(conv_id)  # history before this turn
    record('user', req.message, 'text', conv_id)
    try:
        answer = await realtime_text(req.message, instr)
    except AzureError as e:
        raise HTTPException(503, str(e)) from e
    record('assistant', answer, 'text', conv_id)
    return {'answer': answer, 'conversation_id': conv_id}


@app.get('/api/conversations')
def list_conversations():
    with closing(db()) as c:
        rows = c.execute('SELECT c.id, c.title, c.kind, c.created, c.updated, COUNT(m.id) FROM conversations c '
                         'LEFT JOIN messages m ON m.conversation_id = c.id GROUP BY c.id ORDER BY c.updated DESC').fetchall()
    return [{'id': i, 'title': t, 'kind': k, 'created': cr, 'updated': u, 'message_count': n} for i, t, k, cr, u, n in rows]


@app.post('/api/conversations')
def new_conversation():
    return {'id': create_conversation()}


@app.patch('/api/conversations/{conv_id}')
def rename_conversation(conv_id: str, req: ConversationTitle):
    require_conversation(conv_id)
    with closing(db()) as c, c:
        c.execute('UPDATE conversations SET title=? WHERE id=?', (req.title.strip(), conv_id))
    return {'id': conv_id, 'title': req.title.strip()}


@app.delete('/api/conversations/{conv_id}')
def delete_conversation(conv_id: str):
    require_conversation(conv_id)
    with closing(db()) as c, c:
        c.execute('DELETE FROM messages WHERE conversation_id=?', (conv_id,))
        c.execute('DELETE FROM conversations WHERE id=?', (conv_id,))
    return {'deleted': True}


@app.post('/api/realtime/sdp')
async def realtime_sdp(request: Request, conversation_id: str):
    require_conversation(conversation_id)
    offer = (await request.body()).decode(errors='replace')
    if not offer.startswith('v=0') or len(offer) > 100_000:
        raise HTTPException(400, 'SDP offer が不正です')
    try:
        return Response(await webrtc_answer(offer, conversation_id), media_type='application/sdp')
    except AzureError as e:
        raise HTTPException(503, str(e)) from e


@app.post('/api/tools/call')
async def tools_call(req: ToolCall):
    """Runs a tool call that the voice session (in the browser) received from the model."""
    return {'output': await run_tool(req.name, req.arguments)}


@app.post('/api/transcripts')
def add_transcript(req: Transcript):
    record(req.role, req.content, 'voice', require_conversation(req.conversation_id))
    return {'ok': True}


@app.get('/api/history')
def history(conversation_id: str):
    return recent_messages(require_conversation(conversation_id), 200)


@app.post('/api/jobs')
def add_job(req: Job):
    job_id = str(uuid.uuid4())
    with closing(db()) as c, c:
        c.execute('INSERT INTO jobs VALUES(?,?,?,?)', (job_id, req.hour, req.minute, req.prompt))
    register(job_id, req.hour, req.minute, req.prompt)
    return {'id': job_id, 'timezone': TZ}


@app.get('/api/jobs')
def jobs():
    with closing(db()) as c:
        rows = c.execute('SELECT id,hour,minute,prompt FROM jobs ORDER BY hour,minute').fetchall()
    return [{'id': i, 'hour': h, 'minute': m, 'prompt': p} for i, h, m, p in rows]


@app.delete('/api/jobs/{job_id}')
def delete_job(job_id: str):
    with closing(db()) as c, c:
        n = c.execute('DELETE FROM jobs WHERE id=?', (job_id,)).rowcount
    if not n:
        raise HTTPException(404, 'Not found')
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)
    return {'deleted': True}


# ---------------------------------------------------------------- skills API

def skill_errors(fn):
    """Map SkillError to HTTP 400 (404 when the skill does not exist)."""
    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        try:
            return await fn(*args, **kwargs)
        except SkillError as e:
            raise HTTPException(404 if 'ありません' in str(e) else 400, str(e)) from e
    return wrapper


def zip_response(data: bytes, filename: str) -> Response:
    return Response(data, media_type='application/zip',
                    headers={'Content-Disposition': f'attachment; filename="{filename}"'})


@app.get('/api/skills')
@skill_errors
async def list_skills():
    return skills.all()


@app.get('/api/skills/template')
async def skill_template(name: str = 'my-skill'):
    return {'content': SKILL_TEMPLATE.format(name=name)}


@app.post('/api/skills')
@skill_errors
async def create_skill(req: SkillContent):
    name = parse_skill_md(req.content)[0]['name']
    if (skills.root / name).exists():
        raise SkillError(f'スキル「{name}」は既にあります')
    return {'name': skills.save(req.content)}


@app.post('/api/skills/import')
@skill_errors
async def import_skills(request: Request, filename: str = 'skill.zip', overwrite: bool = False):
    """Body: the raw file (.zip / .skill / SKILL.md). Query: filename, overwrite."""
    data = await request.body()
    if not data:
        raise SkillError('ファイルが空です')
    return skills.import_bytes(filename, data, overwrite)


@app.get('/api/skills/export')
@skill_errors
async def export_all_skills():
    names = [s['name'] for s in skills.all()]
    if not names:
        raise SkillError('エクスポートするスキルがありません')
    return zip_response(skills.export_zip(names), f'skills-{datetime.now(ZoneInfo(TZ)):%Y%m%d}.zip')


@app.get('/api/skills/{name}')
@skill_errors
async def get_skill(name: str):
    return skills.get(name)


@app.put('/api/skills/{name}')
@skill_errors
async def update_skill(name: str, req: SkillContent):
    skills.get(name)  # must exist
    return {'name': skills.save(req.content, name)}


@app.patch('/api/skills/{name}')
@skill_errors
async def toggle_skill(name: str, req: SkillEnabled):
    skills.set_enabled(name, req.enabled)
    return {'name': name, 'enabled': req.enabled}


@app.delete('/api/skills/{name}')
@skill_errors
async def delete_skill(name: str):
    skills.delete(name)
    return {'deleted': True}


@app.get('/api/skills/{name}/export')
@skill_errors
async def export_skill(name: str):
    return zip_response(skills.export_zip([check_name(name)]), f'{name}.zip')


@app.get('/api/skills/{name}/files/{path:path}')
@skill_errors
async def skill_file(name: str, path: str):
    return {'path': path, 'content': skills.read_file(name, path)}


@app.get('/')
def index():
    return FileResponse(STATIC / 'index.html', headers={'Cache-Control': 'no-cache'})
