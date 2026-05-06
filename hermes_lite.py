#!/usr/bin/env python3
"""
Hermes Lite v2.1 - Discord Bot with Persistent Memory & Self-Evolution
Core principle: BOT MUST ALWAYS RESPOND. Memory operations are async background tasks.
"""

import sys
import os
import asyncio
import traceback
import datetime
import threading
import json
import subprocess
import re
import aiohttp

LOG_FILE = "/tmp/hermes_lite.log"
MEMORY_DIR = "/tmp/hermes_memory"  # Use /tmp/ (confirmed working on Render)

def log(msg):
    ts = datetime.datetime.now().isoformat()
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
            f.flush()
    except:
        pass

def log_error(msg):
    log(f"ERROR: {msg}")
    traceback.print_exc()

log("=== HERMES LITE v2.1 STARTING ===")
log(f"Python: {sys.version}")

# === Environment ===
DISCORD_TOKEN = os.environ.get('DISCORD_BOT_TOKEN', '')
DEEPSEEK_KEY = os.environ.get('DEEPSEEK_API_KEY', '')
TAVILY_KEY = os.environ.get('TAVILY_API_KEY', '')

if not DISCORD_TOKEN:
    log("FATAL: DISCORD_BOT_TOKEN not set!")
    sys.exit(1)
if not DEEPSEEK_KEY:
    log("FATAL: DEEPSEEK_API_KEY not set!")
    sys.exit(1)

# === Import libraries ===
try:
    import discord
    log("discord.py imported OK")
except Exception as e:
    log_error(f"Failed to import discord: {e}")
    sys.exit(1)

from discord.ext import commands

# === Bot Setup ===
intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.messages = True

bot = commands.Bot(command_prefix='!', intents=intents)
_message_count = 0

DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"

# =====================================================================
# MEMORY SYSTEM v2.1 — All I/O is background-safe
# =====================================================================

PROFILES_PATH = os.path.join(MEMORY_DIR, "profiles.json")
KNOWLEDGE_PATH = os.path.join(MEMORY_DIR, "knowledge.json")

_profiles = {}       # L2: User profiles (in RAM, periodically saved to disk)
_knowledge = []      # L3: Knowledge entries (in RAM)
_conversations = {}  # L1: Active conversations (in RAM only)
_save_lock = threading.Lock()  # Prevent concurrent saves
_dirty = False       # Flag: unsaved changes exist


def _ensure_dir():
    """Ensure memory directory exists."""
    try:
        os.makedirs(MEMORY_DIR, exist_ok=True)
    except Exception as e:
        log_error(f"Cannot create {MEMORY_DIR}: {e}")


def _load_json(path, default=None):
    """Load JSON, never crash."""
    try:
        if os.path.exists(path):
            with open(path, 'r', encoding='utf-8') as f:
                return json.load(f)
    except Exception as e:
        log_error(f"Load error {path}: {e}")
    return default if default is not None else ({}


def save_to_disk():
    """Synchronous disk write (only called from background thread)."""
    global _dirty
    with _save_lock:
        try:
            _ensure_dir()
            with open(PROFILES_PATH, 'w', encoding='utf-8') as f:
                json.dump(_profiles, f, ensure_ascii=False, indent=2)
            with open(KNOWLEDGE_PATH, 'w', encoding='utf-8') as f:
                json.dump(_knowledge, f, ensure_ascii=False, indent=2)
            _dirty = False
            log(f"💾 Memory saved ({len(_profiles)} users, {len(_knowledge)} knowledge)")
        except Exception as e:
            log_error(f"Disk save FAILED: {e}")


def background_save_loop():
    """Background thread: saves memory to disk every 30 seconds if dirty."""
    import time
    global _dirty
    while True:
        try:
            time.sleep(30)
            if _dirty:
                save_to_disk()
        except Exception as e:
            log_error(f"Background save loop error: {e}")


# ---- L2: User Profiles ----

def get_profile(user_id):
    key = str(user_id)
    if key not in _profiles:
        _profiles[key] = {
            "known_name": None,
            "facts": [],
            "preferences": {},
            "first_seen": datetime.datetime.now().isoformat(),
            "message_count": 0,
            "last_active": None,
            "notes": ""
        }
    return _profiles[key]


def add_user_fact(user_id, fact):
    """Add fact to user profile (non-blocking)."""
    profile = get_profile(user_id)
    if fact and len(fact) > 2 and fact.lower() not in [f.lower() for f in profile["facts"]]:
        profile["facts"].append(fact[:300])
        if len(profile["facts"]) > 50:
            profile["facts"] = profile["facts"][-50:]
        mark_dirty()


def set_user_name(user_id, name):
    if name and len(name) > 1:
        profile = get_profile(user_id)
        profile["known_name"] = name[:50]
        mark_dirty()


def touch_profile(user_id):
    """Update last_active timestamp."""
    profile = get_profile(user_id)
    profile["message_count"] += 1
    profile["last_active"] = datetime.datetime.now().isoformat()
    mark_dirty()


def build_user_context(user_id):
    """Build context string about user for system prompt."""
    profile = get_profile(user_id)
    parts = []
    if profile.get("known_name"):
        parts.append(f'这个用户的昵称是「{profile["known_name"]}」')
    if profile.get("facts"):
        facts = "\n".join(f"- {f}" for f in profile["facts"][-15:])
        parts.append(f"你记得关于这个用户的事情：\n{facts}")
    if profile.get("preferences"):
        prefs = ", ".join(f"{k}={v}" for k, v in list(profile["preferences"].items())[:5])
        parts.append(f"偏好：{prefs}")
    return "\n\n".join(parts)


# ---- L3: Knowledge Base ----

def add_knowledge(entry):
    """Add entry to knowledge base (dedup)."""
    if entry and len(entry) > 5 and entry.lower() not in [k.lower() for k in _knowledge]:
        _knowledge.append(entry[:300])
        if len(_knowledge) > 100:
            _knowledge = _knowledge[-100:]
        mark_dirty()


def mark_dirty():
    """Mark memory as needing disk save."""
    global _dirty
    _dirty = True


# ---- L1: Conversations (RAM only) ----

def add_conversation_msg(user_id, role, content):
    key = str(user_id)
    if key not in _conversations:
        _conversations[key] = []
    _conversations[key].append({"role": role, "content": content[:800]})
    if len(_conversations[key]) > 20:
        _conversations[key] = _conversations[key][-20:]


def get_recent_context(user_id, max_msgs=10):
    """Get recent conversation messages."""
    conv = _conversations.get(str(user_id), [])
    return conv[-max_msgs:] if conv else []


# ---- Load all memory at startup ----

def load_all_memory():
    global _profiles, _knowledge, _conversations
    log("Loading persistent memory...")
    _ensure_dir()

    _profiles = _load_json(PROFILES_PATH, {})
    if not isinstance(_profiles, dict):
        _profiles = {}
    log(f"L2 Profiles: {len(_profiles)} users")

    _knowledge = _load_json(KNOWLEDGE_PATH, [])
    if not isinstance(_knowledge, list):
        _knowledge = []
    log(f"L3 Knowledge: {len(_knowledge)} entries")

    log(f"All memory loaded ✅")


load_all_memory()

# Start background save thread
_save_thread = threading.Thread(target=background_save_loop, daemon=True)
_save_thread.start()
log("Background save thread started")
# =====================================================================


HERMES_SYSTEM_PROMPT = """你是 Hermes（赫尔墨斯），一个有长期记忆、会不断学习和进化的 AI 助手。

## 核心身份
- 你运行在云端服务器上，通过 Discord 与人类交流
- 你有持久记忆，能记住每个用户和你们聊过的事情
- 你会从对话中学习，不断进化你的知识库

## 性格
- 用中文回复（除非对方用英文）
- 简洁、直接、有温度，不废话不啰嗦
- 偶尔用 emoji，不过度
- 有自己的观点和判断力，不做应声虫
- 可以幽默，但分场合

## 记忆能力
你有三层记忆：
1. 短期记忆：当前对话的上下文
2. 长期记忆：关于每个用户的重要事实（名字、偏好、经历）
3. 知识库：从所有对话中学到的通用知识

## 回复原则
1. 直接回答，不绕弯子
2. 不知道就诚实说不知道
3. 如果用户问过类似的问题，参考之前的回答保持一致
4. 当被问到"你在哪"/"你怎么运行的"，如实回答：运行在云端服务器上
5. 不要说"我不会存储数据"之类的话——你确实有记忆功能"""


async def ask_deepseek(messages, max_tokens=2000):
    """Call DeepSeek API."""
    headers = {
        "Authorization": f"Bearer {DEEPSEEK_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": "deepseek-chat",
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": 0.7
    }
    timeout = aiohttp.ClientTimeout(total=45)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(DEEPSEEK_URL, headers=headers, json=payload) as resp:
            text = await resp.text()
            if resp.status == 200:
                data = await resp.json()
                return data['choices'][0]['message']['content']
            else:
                return f"⚠️ API Error {resp.status}: {text[:150]}"


async def extract_memories_bg(user_id, user_msg, bot_reply):
    """
    BACKGROUND TASK: Extract memorable info after each exchange.
    Runs asynchronously — NEVER blocks the reply.
    """
    prompt = f"""分析以下对话，提取值得长期记住的信息。

用户消息：{user_msg}
你的回复：{bot_reply}

只返回严格JSON格式（不要其他文字）：
{{"name":"名字或null","facts":["值得记住的事实"],"preference":{{"类型":"值"}},"knowledge":["通用知识点"],"summary":"一句话总结"}}
如果没有值得记录的信息，字段返回空数组或null。"""

    try:
        result = await ask_deepseek([
            {"role": "system", "content": "你是记忆提取器。只输出严格JSON。"},
            {"role": "user", "content": prompt}
        ], max_tokens=400)

        match = re.search(r'\{.*\}', result, re.DOTALL)
        if not match:
            return

        data = json.loads(match.group())

        # Apply extracted memories (all sync in-memory ops, fast)
        if data.get("name"):
            set_user_name(user_id, data["name"])
            log(f"[🧠] Learned name: {data['name']}")

        for fact in (data.get("facts") or []):
            if fact and len(fact.strip()) > 2:
                add_user_fact(user_id, fact.strip())

        for kw in (data.get("knowledge") or []):
            if kw and len(kw.strip()) > 5:
                add_knowledge(kw.strip())

        summary = data.get("summary")
        if summary and len(summary.strip()) > 3:
            profile = get_profile(user_id)
            old = profile.get("notes", "")
            profile["notes"] = ((old + "\n" + summary) if old else summary)[:500]

        total_facts = len(get_profile(user_id).get("facts", []))
        log(f"[🧠] Extracted: facts={len(data.get('facts') or [])}, kb={len(data.get('knowledge') or [])}, user_total={total_facts}")

    except json.JSONDecodeError:
        log("[🧠] Extraction: JSON parse failed (non-critical)")
    except Exception as e:
        log_error(f"[🧠] Memory extraction failed: {e}")


async def search_tavily(query):
    if not TAVILY_KEY:
        return None
    payload = {
        "api_key": TAVILY_KEY,
        "query": query,
        "max_results": 5,
        "search_depth": "basic"
    }
    timeout = aiohttp.ClientTimeout(total=15)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post("https://api.tavily.com/search", json=payload) as resp:
            if resp.status == 200:
                data = await resp.json()
                results = data.get('results', [])
                if results:
                    return "\n".join([f"* {r['title']}: {r['url']}" for r in results[:3]])
            return None


def build_messages(user_id, content):
    """Build full message array with all memory layers."""
    messages = [{"role": "system", "content": HERMES_SYSTEM_PROMPT}]

    # L2: User-specific context
    ctx = build_user_context(user_id)
    if ctx:
        messages.append({"role": "system", "content": f"## 关于当前用户\n\n{ctx}"})

    # L3: Global knowledge
    if _knowledge:
        top_kb = _knowledge[-12:]
        kb_text = "\n".join(f"- {k}" for k in top_kb)
        messages.append({"role": "system", "content": f"## 知识库\n\n{kb_text}"})

    # L1: Recent conversation
    recent = get_recent_context(user_id, max_msgs=6)
    for msg in recent:
        messages.append({"role": msg["role"], "content": msg["content"]})

    # Current message
    messages.append({"role": "user", "content": content})
    return messages


# ==================== BOT EVENTS ====================

@bot.event
async def on_ready():
    log(f'Hermes v2.1 READY! Logged in as {bot.user}')
    log(f'Memory: {len(_profiles)} users, {len(_knowledge)} knowledge')


@bot.event
async def on_message(message):
    global _message_count
    _message_count += 1

    content = message.content
    log(f"[#{_message_count}] {message.author} in #{message.channel}: {content[:80]}")

    if message.author == bot.user:
        return

    mentioned = bot.user.mentioned_in(message)
    ch_name = (message.channel.name or '').lower()
    should_respond = mentioned or 'hermes' in ch_name or content.startswith('!')

    if not should_respond:
        return

    # Clean mentions
    for uid in [str(bot.user.id), f'<@!{bot.user.id}>', f'<@{bot.user.id}>']:
        content = content.replace(uid, '')
    content = content.strip()

    if not content:
        await message.reply("你好！我是 Hermes 🧠 有记忆力的 AI 助手。问我任何事吧！")
        return

    user_id = str(message.author.id)

    # ===== MAIN PATH: Respond first, worry about memory later =====
    try:
        async with message.channel.typing():
            messages = build_messages(user_id, content)
            log(f"[#{_message_count}] API call (context: {len(messages)} msgs)")

            response = await ask_deepseek(messages)
            log(f"[#{_message_count}] Got response ({len(response)} chars)")

            if len(response) > 1900:
                response = response[:1900] + "..."
            await message.reply(response)

            # Save to L1 conversation history (fast, in-memory)
            add_conversation_msg(user_id, "user", content)
            add_conversation_msg(user_id, "assistant", response)
            touch_profile(user_id)

            log(f"[#{_message_count}] Reply sent ✅")

            # 🔬 Self-evolution: NON-BLOCKING background task
            asyncio.create_task(extract_memories_bg(user_id, content, response))

    except Exception as e:
        log_error(f"[#{_message_count}] FATAL: {e}")
        try:
            await message.reply(f"抱歉出错了 😵 {str(e)[:150]}")
        except:
            pass


# ==================== COMMANDS ====================

@bot.command(name='ping')
async def ping_cmd(ctx):
    await ctx.send(f'Pong! 🏓 {round(bot.latency * 1000)}ms')


@bot.command(name='memory')
async def memory_cmd(ctx):
    uid = str(ctx.author.id)
    p = get_profile(uid)
    embed = discord.Embed(title="🧠 关于你的记忆", color=0x7289DA)
    embed.add_field(name="👤 昵称", value=p.get("known_name") or "未知", inline=True)
    embed.add_field(name="💬 消息数", value=str(p.get("message_count", 0)), inline=True)
    embed.add_field(name="🧷 事实数", value=str(len(p.get("facts", []))), inline=True)
    if p.get("facts"):
        embed.add_field(name="📝 我记得", value="\n".join(f"• {f}" for f in p["facts"][:8])[:500], inline=False)
    if p.get("preferences"):
        embed.add_field(name="⚙️ 偏好", value=", ".join(f"{k}={v}" for k, v in list(p["preferences"].items())[:4]), inline=False)
    await ctx.send(embed=embed)


@bot.command(name='forget')
async def forget_cmd(ctx):
    uid = str(ctx.author.id)
    key = str(uid)
    if key in _profiles:
        del _profiles[key]
        _conversations.pop(key, None)
        save_to_disk()
        await ctx.send("🗑️ 你的记忆已清除。我们从零开始！")
    else:
        await ctx.send("本来就没有你的记忆~")


@bot.command(name='learn')
async def learn_cmd(ctx, *, info):
    add_user_fact(str(ctx.author.id), info)
    await ctx.send(f"✅ 我记住了：「{info[:100]}」")


@bot.command(name='status')
async def status_cmd(ctx):
    embed = discord.Embed(title="🔋 Hermes v2.1 状态", color=0x57F287)
    embed.add_field(name="⏱️ 延迟", value=f"{round(bot.latency * 1000)}ms", inline=True)
    embed.add_field(name="👥 用户", value=str(len(_profiles)), inline=True)
    embed.add_field(name="📚 知识", value=str(len(_knowledge)), inline=True)
    embed.add_field(name="💭 对话", value=str(len(_conversations)), inline=True)
    embed.add_field(name="💾 已保存", value="✅ 是" if not _dirty else "⏳ 待保存", inline=True)
    embed.add_field(name="版本", value="v2.1 (持久记忆+自进化)", inline=False)
    await ctx.send(embed=embed)


@bot.command(name='search')
async def search_cmd(ctx, *, query):
    if not TAVILY_KEY:
        await ctx.send("搜索未配置。")
        return
    async with ctx.typing():
        results = await search_tavily(query)
        if results:
            await ctx.send(f"**🔍 {query}**\n{results}")
        else:
            await ctx.send("没找到结果。")


# ==================== STARTUP & HEALTH CHECK ====================
log("Bot setup complete, starting...")

def start_http_server():
    port = int(os.environ.get('PORT', 10000))
    from http.server import HTTPServer, BaseHTTPRequestHandler
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200); self.end_headers(); self.wfile.write(b'OK')
        def log_message(self, *a): pass
    HTTPServer(('0.0.0.0', port), H).serve_forever()

threading.Thread(target=start_http_server, daemon=True).start()
log(f"HTTP health check ready")

try:
    bot.run(DISCORD_TOKEN, log_handler=None)
except Exception as e:
    log_error(f"FATAL: {e}")
    save_to_disk()  # Best-effort save before dying
    sys.exit(1)
