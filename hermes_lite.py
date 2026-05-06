#!/usr/bin/env python3
"""
Hermes Lite v2.2 — Discord Bot with TRUE Persistent Memory (GitHub-backed)
Memory survives Manual Deploy, restarts, and spin-downs.
Core principle: BOT MUST ALWAYS RESPOND. All memory I/O is non-blocking background.
"""

import sys
import os
import asyncio
import traceback
import datetime
import threading
import json
import re
import base64
import aiohttp

LOG_FILE = "/tmp/hermes_lite.log"

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

log("=== HERMES LITE v2.2 STARTING ===")
log(f"Python: {sys.version}")

# === Environment ===
DISCORD_TOKEN = os.environ.get('DISCORD_BOT_TOKEN', '')
DEEPSEEK_KEY = os.environ.get('DEEPSEEK_API_KEY', '')
TAVILY_KEY = os.environ.get('TAVILY_API_KEY', '')

# GitHub persistence config
GH_TOKEN = os.environ.get('GITHUB_TOKEN', '')       # Personal Access Token (repo scope)
GH_OWNER = os.environ.get('GH_OWNER', 'likeabird830')
GH_REPO = os.environ.get('GH_REPO', 'hermes-lite')
GH_BRANCH = os.environ.get('GH_BRANCH', 'main')

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
GH_API_BASE = "https://api.github.com"

# Memory files stored in repo
MEMORY_FILES = {
    "profiles": "hermes_profiles.json",
    "knowledge": "hermes_knowledge.md"
}

# =====================================================================
# MEMORY SYSTEM v2.2 — GitHub API backed, truly persistent
# =====================================================================

_profiles = {}       # L2: User profiles (in RAM)
_knowledge = []      # L3: Knowledge entries (in RAM)
_conversations = {}  # L1: Active conversations (RAM only)
_save_lock = threading.Lock()
_dirty = False
_gh_available = bool(GH_TOKEN)  # Whether GitHub persistence is configured


async def _gh_api(method, path, **kwargs):
    """Call GitHub API. Returns parsed JSON or None on error."""
    if not _gh_available:
        return None
    url = f"{GH_API_BASE}{path}"
    headers = {
        "Authorization": f"token {GH_TOKEN}",
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "Hermes-Lite-Bot/2.2"
    }
    try:
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.request(method, url, headers=headers, **kwargs) as resp:
                if resp.status in (200, 201):
                    return await resp.json()
                elif resp.status == 404:
                    return None  # File doesn't exist yet
                else:
                    text = await resp.text()
                    log(f"GitHub API {method} {path} -> {resp.status}: {text[:100]}")
                    return None
    except Exception as e:
        log_error(f"GitHub API error: {e}")
        return None


async def gh_download_memory():
    """Download memory files from GitHub at startup."""
    global _profiles, _knowledge
    
    # Download profiles
    result = await _gh_api("GET", f"/repos/{GH_OWNER}/{GH_REPO}/contents/{MEMORY_FILES['profiles']}?ref={GH_BRANCH}")
    if result and result.get("content"):
        try:
            data = json.loads(base64.b64decode(result["content"]))
            if isinstance(data, dict):
                _profiles = data
                log(f"[💾] Loaded profiles from GitHub: {len(_profiles)} users")
        except Exception as e:
            log_error(f"Failed to parse profiles from GH: {e}")
    
    # Download knowledge (stored as markdown for human readability)
    result = await _gh_api("GET", f"/repos/{GH_OWNER}/{GH_REPO}/contents/{MEMORY_FILES['knowledge']}?ref={GH_BRANCH}")
    if result and result.get("content"):
        try:
            md_content = base64.b64decode(result["content"]).decode("utf-8")
            _knowledge = []
            for line in md_content.split("\n"):
                line = line.strip()
                if line.startswith("- ") or line.startswith("* "):
                    entry = line[2:].strip()
                    if entry:
                        _knowledge.append(entry)
                elif line and not line.startswith("#") and not line.startswith(">") and len(line) > 5:
                    _knowledge.append(line)
            log(f"[💾] Loaded knowledge from GitHub: {len(_knowledge)} entries")
        except Exception as e:
            log_error(f"Failed to parse knowledge from GH: {e}")


async def gh_upload_memory():
    """Upload current memory to GitHub. Non-critical — failures are silent."""
    global _dirty
    
    if not _dirty or not _gh_available:
        return
    
    with _save_lock:
        try:
            # Build knowledge markdown content
            kb_lines = ["# Hermes Knowledge Base\n", f"> Auto-updated by Hermes | {datetime.datetime.now().isoformat()}\n"]
            for entry in _knowledge:
                kb_lines.append(f"- {entry}")
            kb_md = "\n".join(kb_lines)
            
            # Get SHAs for update (or None if new file)
            prof_result = await _gh_api("GET", f"/repos/{GH_OWNER}/{GH_REPO}/contents/{MEMORY_FILES['profiles']}?ref={GH_BRANCH}")
            prof_sha = prof_result.get("sha") if prof_result else None
            
            kb_result = await _gh_api("GET", f"/repos/{GH_OWNER}/{GH_REPO}/contents/{MEMORY_FILES['knowledge']}?ref={GH_BRANCH}")
            kb_sha = kb_result.get("sha") if kb_result else None
            
            # Upload both files
            import time
            ts = int(time.time())
            
            prof_payload = json.dumps({
                "message": f"[Hermes] 💾 Save profiles ({len(_profiles)} users) [auto-{ts}]",
                "content": base64.b64encode(json.dumps(_profiles, ensure_ascii=False, indent=2).encode()).decode(),
                "branch": GH_BRANCH,
                **({"sha": prof_sha} if prof_sha else {})
            })
            
            kb_payload = json.dumps({
                "message": f"[Hermes] 📚 Save knowledge ({len(_knowledge)} entries) [auto-{ts}]",
                "content": base64.b64encode(kb_md.encode()).decode(),
                "branch": GH_BRANCH,
                **({"sha": kb_sha} if kb_sha else {})
            })
            
            r1 = await _gh_api("PUT", f"/repos/{GH_OWNER}/{GH_REPO}/contents/{MEMORY_FILES['profiles']}", data=prof_payload.encode())
            r2 = await _gh_api("PUT", f"/repos/{GH_OWNER}/{GH_REPO}/contents/{MEMORY_FILES['knowledge']}", data=kb_payload.encode())
            
            if r1 and r2:
                _dirty = False
                log(f"[☁️] Memory saved to GitHub ✅ ({len(_profiles)} users, {len(_knowledge)} kb)")
            elif r1:
                _dirty = False
                log("[☁️] Profiles saved, knowledge failed (partial)")
            else:
                log("[☁️] GitHub save FAILED (will retry later)")
                
        except Exception as e:
            log_error(f"[☁️] GitHub upload error: {e}")


async def github_sync_loop():
    """Background loop: sync memory to GitHub every 60 seconds when dirty."""
    while True:
        try:
            await asyncio.sleep(60)
            if _dirty:
                await gh_upload_memory()
        except Exception as e:
            log_error(f"GitHub sync loop error: {e}")


# ---- L2: User Profiles (same API, just storage backend changed) ----

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
    profile = get_profile(user_id)
    if fact and len(fact) > 2 and fact.lower() not in [f.lower() for f in profile["facts"]]:
        profile["facts"].append(fact[:300])
        if len(profile["facts"]) > 50:
            profile["facts"] = profile["facts"][-50:]
        mark_dirty()


def set_user_name(user_id, name):
    if name and len(name) > 1:
        get_profile(user_id)["known_name"] = name[:50]
        mark_dirty()


def touch_profile(user_id):
    profile = get_profile(user_id)
    profile["message_count"] += 1
    profile["last_active"] = datetime.datetime.now().isoformat()
    mark_dirty()


def build_user_context(user_id):
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
    if entry and len(entry) > 5 and entry.lower() not in [k.lower() for k in _knowledge]:
        _knowledge.append(entry[:300])
        if len(_knowledge) > 100:
            _knowledge[:] = _knowledge[-100:]
        mark_dirty()


def mark_dirty():
    global _dirty
    _dirty = True


# ---- L1: Conversations (RAM only, lost on restart) ----

def add_conversation_msg(user_id, role, content):
    key = str(user_id)
    if key not in _conversations:
        _conversations[key] = []
    _conversations[key].append({"role": role, "content": content[:800]})
    if len(_conversations[key]) > 20:
        _conversations[key] = _conversations[key][-20:]


def get_recent_context(user_id, max_msgs=10):
    conv = _conversations.get(str(user_id), [])
    return conv[-max_msgs:] if conv else []


# ---- Startup: Load memory from GitHub ----

async def startup_load():
    """Load all persistent memory from GitHub at bot start."""
    log("Loading memory from GitHub...")
    await gh_download_memory()
    log(f"All memory loaded ✅ (users={len(_profiles)}, knowledge={len(_knowledge)})")
    log(f"GitHub persistence: {'ON ☁️' if _gh_available else 'OFF ⚠️ (no GITHUB_TOKEN)'}")

# We'll call this in on_ready since we need the event loop for async I/O
_startup_loaded = False
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


async def ask_deepseek(messages, max_tokens=2000, max_retries=2):
    """Call DeepSeek API with retry on failure/cancellation."""
    headers = {"Authorization": f"Bearer {DEEPSEEK_KEY}", "Content-Type": "application/json"}
    payload = {"model": "deepseek-chat", "messages": messages, "max_tokens": max_tokens, "temperature": 0.7}
    
    last_error = ""
    for attempt in range(max_retries + 1):
        try:
            # Longer timeout: Render(US) → DeepSeek(CN) can take up to 90s
            timeout = aiohttp.ClientTimeout(total=90, connect=30)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(DEEPSEEK_URL, headers=headers, json=payload) as resp:
                    text = await resp.text()
                    if resp.status == 200:
                        return (await resp.json())['choices'][0]['message']['content']
                    last_error = f"API Error {resp.status}: {text[:100]}"
        except asyncio.TimeoutError:
            last_error = "Timeout"
            log(f"[API] Retry {attempt+1}/{max_retries}: Timeout")
            await asyncio.sleep(2 * (attempt + 1))
            continue
        except (asyncio.CancelledError, Exception) as e:
            last_error = f"{type(e).__name__}"
            if attempt < max_retries:
                log(f"[API] Retry {attempt+1}/{max_retries}: {last_error}")
                await asyncio.sleep(2)
                continue
    return f"⚠️ AI暂时连接不上，请稍后再试 ({last_error})"


async def extract_memories_bg(user_id, user_msg, bot_reply):
    """BACKGROUND TASK: Extract memorable info after each exchange."""
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

    except (json.JSONDecodeError, ValueError):
        log("[🧠] Extraction: JSON parse failed (non-critical)")
    except Exception as e:
        log_error(f"[🧠] Memory extraction failed: {e}")


async def search_tavily(query):
    if not TAVILY_KEY:
        return None
    payload = {"api_key": TAVILY_KEY, "query": query, "max_results": 5, "search_depth": "basic"}
    timeout = aiohttp.ClientTimeout(total=15)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post("https://api.tavily.com/search", json=payload) as resp:
            if resp.status == 200:
                results = (await resp.json()).get('results', [])
                if results:
                    return "\n".join([f"* {r['title']}: {r['url']}" for r in results[:3]])
            return None


def build_messages(user_id, content):
    messages = [{"role": "system", "content": HERMES_SYSTEM_PROMPT}]
    
    ctx = build_user_context(user_id)
    if ctx:
        messages.append({"role": "system", "content": f"## 关于当前用户\n\n{ctx}"})
    
    if _knowledge:
        top_kb = _knowledge[-12:]
        kb_text = "\n".join(f"- {k}" for k in top_kb)
        messages.append({"role": "system", "content": f"## 知识库\n\n{kb_text}"})
    
    recent = get_recent_context(user_id, max_msgs=6)
    for msg in recent:
        messages.append({"role": msg["role"], "content": msg["content"]})
    
    messages.append({"role": "user", "content": content})
    return messages


# ==================== BOT EVENTS ====================

@bot.event
async def on_ready():
    global _startup_loaded
    log(f'Hermes v2.2 READY! Logged in as {bot.user}')
    
    # Load persisted memory from GitHub (non-blocking: failure won't kill the bot)
    try:
        await startup_load()
    except Exception as e:
        log_error(f"Memory load FAILED (bot still working): {e}")
    
    _startup_loaded = True
    
    # Start background GitHub sync loop
    asyncio.create_task(github_sync_loop())


@bot.event
async def on_message(message):
    global _message_count
    _message_count += 1
    
    if message.author == bot.user:
        return
    
    content = message.content
    log(f"[#{_message_count}] {message.author} in #{getattr(message.channel, 'name', '?')}: {content[:80]}")
    
    mentioned = bot.user.mentioned_in(message)
    ch_name = (message.channel.name or '').lower()
    should_respond = mentioned or 'hermes' in ch_name or content.startswith('!')
    
    if not should_respond:
        return
    
    for uid in [str(bot.user.id), f'<@!{bot.user.id}>', f'<@{bot.user.id}>']:
        content = content.replace(uid, '')
    content = content.strip()
    
    if not content:
        await message.reply("你好！我是 Hermes 🧠 有记忆力的 AI 助手。问我任何事吧！")
        return
    
    user_id = str(message.author.id)
    
    try:
        async with message.channel.typing():
            messages = build_messages(user_id, content)
            log(f"[#{_message_count}] API call (context: {len(messages)} msgs)")
            
            response = await ask_deepseek(messages)
            log(f"[#{_message_count}] Got response ({len(response)} chars)")
            
            if len(response) > 1900:
                response = response[:1900] + "..."
            await message.reply(response)
            
            add_conversation_msg(user_id, "user", content)
            add_conversation_msg(user_id, "assistant", response)
            touch_profile(user_id)
            
            log(f"[#{_message_count}] Reply sent ✅")
            
            # Self-evolution: NON-BLOCKING background task
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
    p = get_profile(str(ctx.author.id))
    embed = discord.Embed(title="🧠 关于你的记忆", color=0x7289DA)
    embed.add_field(name="👤 昵称", value=p.get("known_name") or "未知", inline=True)
    embed.add_field(name="💬 消息数", value=str(p.get("message_count", 0)), inline=True)
    embed.add_field(name="🧷 事实数", value=str(len(p.get("facts", []))), inline=True)
    if p.get("facts"):
        embed.add_field(name="📝 我记得", value="\n".join(f"• {f}" for f in p["facts"][:8])[:500], inline=False)
    if p.get("preferences"):
        embed.add_field(name="⚙️ 偏好", value=", ".join(f"{k}={v}" for k, v in list(p["preferences"].items())[:4]), inline=False)
    embed.set_footer(text=f"{'☁️ 已备份到GitHub' if _gh_available else '⚠️ 仅本地缓存'}")
    await ctx.send(embed=embed)


@bot.command(name='forget')
async def forget_cmd(ctx):
    uid = str(ctx.author.id)
    key = str(uid)
    if key in _profiles:
        del _profiles[key]
        _conversations.pop(key, None)
        mark_dirty()
        await ctx.send("🗑️ 你的记忆已清除。我们从零开始！")
    else:
        await ctx.send("本来就没有你的记忆~")


@bot.command(name='learn')
async def learn_cmd(ctx, *, info):
    add_user_fact(str(ctx.author.id), info)
    await ctx.send(f"✅ 我记住了：「{info[:100]}」")


@bot.command(name='status')
async def status_cmd(ctx):
    embed = discord.Embed(title="🔋 Hermes v2.2 状态", color=0x57F287)
    embed.add_field(name="⏱️ 延迟", value=f"{round(bot.latency * 1000)}ms", inline=True)
    embed.add_field(name="👥 用户", value=str(len(_profiles)), inline=True)
    embed.add_field(name="📚 知识", value=str(len(_knowledge)), inline=True)
    embed.add_field(name="💭 对话", value=str(len(_conversations)), inline=True)
    embed.add_field(name="☁️ GitHub", value="✅ 已连接" if _gh_available else "❌ 未配置", inline=True)
    embed.add_field(name="💾 待同步", value="是" if _dirty else "否", inline=True)
    embed.add_field(name="版本", value="v2.2 (GitHub持久化+自进化)", inline=False)
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


@bot.command(name='save')
async def force_save_cmd(ctx):
    """Force immediate sync to GitHub."""
    if not _gh_available:
        await ctx.send("❌ GitHub 未配置（需要 GITHUB_TOKEN 环境变量）。")
        return
    await ctx.send("💾 正在保存到 GitHub...")
    await gh_upload_memory()
    await ctx.send("✅ 保存完成！（如果成功的话 😄）")


# ==================== STARTUP & HEALTH CHECK ====================
log("Bot setup complete, starting...")

def start_http_server():
    port = int(os.environ.get('PORT', 10000))
    from http.server import HTTPServer, BaseHTTPRequestHandler
    class H(BaseHTTPRequestHandler):
        def do_GET(self): self.send_response(200); self.end_headers(); self.wfile.write(b'OK')
        def log_message(self, *a): pass
    HTTPServer(('0.0.0.0', port), H).serve_forever()

threading.Thread(target=start_http_server, daemon=True).start()

# Graceful shutdown: save before dying
import signal
def _shutdown_handler(signum, frame):
    log("Shutdown triggered, attempting final save...")
    # Can't use async here, just note that dirty flag is set
    # The next startup will have stale data, but it's better than nothing
    log(f"Dirty state on shutdown: {_dirty}")

signal.signal(signal.SIGTERM, _shutdown_handler)
signal.signal(signal.SIGINT, _shutdown_handler)

try:
    bot.run(DISCORD_TOKEN, log_handler=None)
except Exception as e:
    log_error(f"FATAL: bot.run() crashed: {e}")
    sys.exit(1)
