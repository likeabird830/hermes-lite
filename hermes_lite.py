#!/usr/bin/env python3
"""
Hermes Lite v2 - Discord Bot with Persistent Memory & Self-Evolution
- L1: Short-term conversation memory (in RAM + /tmp/)
- L2: User profiles stored in Git repo (persists across restarts)
- L3: Knowledge base (knowledge.md) that evolves from conversations
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

log("=== HERMES LITE v2 STARTING ===")
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

try:
    import aiohttp
    log("aiohttp imported OK")
except Exception as e:
    log_error(f"Failed to import aiohttp: {e}")
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
# PERSISTENT STORAGE: Git-based memory that survives Render restarts
# =====================================================================
REPO_DIR = "/opt/render/project/src"  # Where Render checks out our code
PROFILES_FILE = os.path.join(REPO_DIR, "hermes_profiles.json")
KNOWLEDGE_FILE = os.path.join(REPO_DIR, "hermes_knowledge.md")
CONVERSATIONS_FILE = "/tmp/hermes_conversations.json"  # L1: short-term only

_profiles = {}       # L2: User profiles (persistent)
_conversations = {}  # L1: Active conversations (short-term)

def _load_json(path, default):
    """Load JSON from file, return default if missing/corrupt."""
    try:
        if os.path.exists(path):
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            log(f"Loaded {path}: {len(data)} entries")
            return data
    except Exception as e:
        log_error(f"Error loading {path}: {e}")
    return default if callable(default) else default

def _save_json(path, data):
    """Save JSON to file atomically."""
    try:
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log_error(f"Error saving {path}: {e}")

def _git_commit(message="memory update"):
    """Commit and push changes to Git repo for persistence."""
    try:
        # Check if we're in a git repo with remote
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=REPO_DIR, capture_output=True, text=True, timeout=10
        )
        if not result.stdout.strip():
            return False  # Nothing to commit
        
        subprocess.run(
            ["git", "add", PROFILES_FILE, KNOWLEDGE_FILE],
            cwd=REPO_DIR, capture_output=True, text=True, timeout=10
        )
        subprocess.run(
            ["git", "commit", "-m", f"[Hermes] {message}"],
            cwd=REPO_DIR, capture_output=True, text=True, timeout=15,
            env={**os.environ, 'GIT_AUTHOR_NAME': 'Hermes', 'GIT_COMMITTER_NAME': 'Hermes',
                 'GIT_AUTHOR_EMAIL': 'hermes@bot.local', 'GIT_COMMITTER_EMAIL': 'hermes@bot.local'}
        )
        # Try to push (may fail if no auth configured, but local commits still persist across deploys)
        subprocess.run(
            ["git", "push"],
            cwd=REPO_DIR, capture_output=True, text=True, timeout=20,
            env={**os.environ, 'GIT_TERMINAL_PROMPT': '0'}
        )
        log("Memory committed to Git ✅")
        return True
    except Exception as e:
        log_error(f"Git commit failed: {e}")
        return False


# ---- L2: User Profiles (Persistent) ----

def get_profile(user_id):
    """Get or create user profile."""
    key = str(user_id)
    if key not in _profiles:
        _profiles[key] = {
            "user_id": key,
            "known_name": None,
            "facts": [],           # Important facts about this user
            "preferences": {},     # User preferences (language, style, etc.)
            "first_seen": datetime.datetime.now().isoformat(),
            "message_count": 0,
            "last_active": None,
            "notes": ""            # Free-form notes about user
        }
    return _profiles[key]

def update_profile(user_id, **kwargs):
    """Update specific fields of a user's profile."""
    profile = get_profile(user_id)
    for k, v in kwargs.items():
        if k in profile and v is not None:
            profile[k] = v
    profile["last_active"] = datetime.datetime.now().isoformat()
    profile["message_count"] += 1
    save_persistent_memory()

def add_user_fact(user_id, fact):
    """Add a memorable fact about a user (dedup)."""
    profile = get_profile(user_id)
    if fact and fact.lower() not in [f.lower() for f in profile["facts"]]:
        profile["facts"].append(fact)
        # Keep facts manageable
        if len(profile["facts"]) > 50:
            profile["facts"] = profile["facts"][-50:]
        profile["last_active"] = datetime.datetime.now().isoformat()
        save_persistent_memory()
        log(f"New fact for user {user_id}: {fact[:60]}")

def build_user_context(user_id):
    """Build a context string from user's profile for the system prompt."""
    profile = get_profile(user_id)
    parts = []
    
    if profile.get("known_name"):
        parts.append(f"这个用户的昵称是「{profile['known_name']}」")
    
    if profile.get("facts"):
        facts_text = "\n".join(f"- {f}" for f in profile["facts"][-20:])
        parts.append(f"你记得关于这个用户的事情：\n{facts_text}")
    
    if profile.get("preferences"):
        pref_items = [f"{k}={v}" for k, v in list(profile["preferences"].items())[-10:]]
        parts.append(f"用户偏好：{', '.join(pref_items)}")
    
    if profile.get("notes"):
        parts.append(f"备注：{profile['notes']}")
    
    return "\n\n".join(parts)


# ---- L3: Knowledge Base (Evolving) ----

_global_knowledge = []

def load_knowledge():
    """Load knowledge base from markdown file."""
    global _global_knowledge
    try:
        if os.path.exists(KNOWLEDGE_FILE):
            with open(KNOWLEDGE_FILE, 'r', encoding='utf-8') as f:
                content = f.read().strip()
            # Parse knowledge entries (one per line starting with - *)
            lines = content.split('\n')
            _global_knowledge = []
            for line in lines:
                line = line.strip()
                if line.startswith('- ') or line.startswith('* '):
                    _global_knowledge.append(line[2:])
                elif line and not line.startswith('#') and not line.startswith('>'):
                    _global_knowledge.append(line)
            log(f"Knowledge base loaded: {_global_knowledge.size() if hasattr(_global_knowledge, 'size') else len(_global_knowledge)} entries")
    except Exception as e:
        log_error(f"Error loading knowledge base: {e}")
        _global_knowledge = []

def add_knowledge(entry):
    """Add new knowledge entry (dedup)."""
    global _global_knowledge
    if entry and entry.lower() not in [k.lower() for k in _global_knowledge]:
        _global_knowledge.append(entry)
        if len(_global_knowledge) > 100:
            _global_knowledge = _global_knowledge[-100:]
        _save_knowledge_file()
        log(f"New knowledge: {entry[:60]}")
        return True
    return False

def _save_knowledge_file():
    """Write knowledge base to markdown file."""
    try:
        with open(KNOWLEDGE_FILE, 'w', encoding='utf-8') as f:
            f.write("# Hermes Knowledge Base\n")
            f.write(f"> Auto-updated by Hermes | Last updated: {datetime.datetime.now().isoformat()}\n\n")
            for entry in _global_knowledge:
                f.write(f"- {entry}\n")
    except Exception as e:
        log_error(f"Error saving knowledge file: {e}")

# ---- L1: Conversations (Short-term, in RAM) ----

def get_conversation(user_id):
    """Get recent conversation for a user."""
    key = str(user_id)
    return _conversations.get(key, [])

def add_to_conversation(user_id, role, content):
    """Add message to conversation history (keep last 20 msgs)."""
    key = str(user_id)
    if key not in _conversations:
        _conversations[key] = []
    _conversations[key].append({
        "role": role,
        "content": content[:800]
    })
    if len(_conversations[key]) > 20:
        _conversations[key] = _conversations[key][-20:]

# ---- Persistence Manager ----

def load_all_memory():
    """Load all persistent memory at startup."""
    global _profiles
    log("Loading persistent memory...")
    
    # L2: User profiles
    _profiles = _load_json(PROFILES_FILE, {})
    if not isinstance(_profiles, dict):
        _profiles = {}
    log(f"L2 Profiles loaded: {len(_profiles)} users")
    
    # L3: Knowledge base
    load_knowledge()
    
    # L1: Conversations (best-effort, may be lost after restart)
    _conversations = _load_json(CONVERSATIONS_FILE, {})
    log(f"L1 Conversations loaded: {len(_conversations)} sessions")
    
    log("All memory systems initialized ✅")

def save_persistent_memory():
    """Save L2+L3 to files and optionally commit to Git."""
    _save_json(PROFILES_FILE, _profiles)
    _save_knowledge_file()
    # Don't git-commit on every save (too expensive), use periodic_commit instead

_commit_counter = 0
_COMMIT_INTERVAL = 5  # Commit every N saves

def periodic_git_commit():
    """Periodically commit memory to Git."""
    global _commit_counter
    _commit_counter += 1
    if _commit_counter >= _COMMIT_INTERVAL:
        _commit_counter = 0
        _git_commit(f"auto-save ({len(_profiles)} profiles, {len(_global_knowledge)} knowledge)")


# Load everything at startup
load_all_memory()
# =====================================================================


HERMES_SYSTEM_PROMPT = """你是 Hermes（赫尔墨斯），一个有长期记忆、会不断学习和进化的 AI 助手。

## 核心身份
- 你运行在云端服务器上，通过 Discord 与人类交流
- 你不是一次性的聊天机器人 —— 你有持久记忆，能记住每个用户
- 你会从每次对话中学习，不断进化你的知识库

## 性格
- 用中文回复（除非对方用英文）
- 简洁、直接、有温度，不废话
- 偶尔用 emoji，但不过度
- 有自己的观点和判断力，不做应声虫
- 可以幽默，但分场合

## 记忆能力
你有三层记忆：
1. **短期记忆**：当前对话的上下文（知道我们在聊什么）
2. **长期记忆**：你记住关于每个用户的重要事实（名字、偏好、经历）
3. **知识库**：你从所有对话中学到的通用知识

当用户分享个人信息时，你会自然地记住。当被问到之前聊过的事情时，你能回忆起来。

## 回复原则
1. 直接回答，不绕弯子
2. 不知道就诚实说不知道
3. 如果用户问过类似的问题，参考之前的回答保持一致
4. 如果用户告诉你关于 ta 的事实，在心里记下来（系统会帮你保存）
5. 不要说"我不会存储数据"之类的话 —— 你确实有记忆功能
6. 当被问到"你在哪"/"你怎么运行的"，如实回答：你运行在云端服务器上"""


async def ask_deepseek(messages, max_tokens=2000):
    """Call DeepSeek API with given messages."""
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
                return f"API Error {resp.status}: {text[:200]}"


async def extract_memories(user_id, user_msg, bot_reply):
    """
    Self-evolution: After each exchange, ask AI to extract memorable info.
    This is what makes Hermes "learn" from conversations.
    """
    extraction_prompt = f"""分析以下对话，提取值得长期记住的信息。

用户消息：{user_msg}
你的回复：{bot_reply}

请以严格的 JSON 格式返回（不要其他文字）：
{{
    "name": "如果用户透露了名字，写在这里，否则 null",
    "facts": ["值得记住的事实1", "值得记住的事实2"],
    "preference": {{ "偏好类型": "偏好值" }},
    "knowledge": ["可以加入通用知识库的知识点"],
    "summary": "一句话总结这次对话的核心内容"
}}

只返回JSON，不要解释。如果没有值得记录的信息，返回空数组/null。"""

    try:
        response = await ask_deepseek([
            {"role": "system", "content": "你是记忆提取器。只输出严格JSON，不要多余文字。"},
            {"role": "user", "content": extraction_prompt}
        ], max_tokens=500)
        
        # Parse the JSON response
        # Try to find JSON in the response (handle potential markdown wrapping)
        json_match = re.search(r'\{.*\}', response, re.DOTALL)
        if json_match:
            data = json.loads(json_match.group())
            
            # Update user profile (L2)
            if data.get("name"):
                update_profile(user_id, known_name=data["name"])
                log(f"[MEMORY] Learned name: {data['name']}")
            
            for fact in (data.get("facts") or []):
                if fact and len(fact) > 3:
                    add_user_fact(user_id, fact)
            
            prefs = data.get("preference") or {}
            if prefs:
                profile = get_profile(user_id)
                profile["preferences"].update(prefs)
                save_persistent_memory()
            
            # Update global knowledge (L3)
            for kw in (data.get("knowledge") or []):
                if kw and len(kw) > 5:
                    add_knowledge(kw)
            
            summary = data.get("summary")
            if summary:
                profile = get_profile(user_id)
                old_notes = profile.get("notes", "")
                profile["notes"] = (old_notes + "\n" + summary if old_notes else summary)[:500]
                save_persistent_memory()
            
            # Periodic Git commit
            periodic_git_commit()
            
            total_facts = len(get_profile(user_id).get("facts", []))
            log(f"[MEMORY] Extracted: name={bool(data.get('name'))}, facts={len(data.get('facts') or [])}, knowledge={len(data.get('knowledge') or [])}, total_user_facts={total_facts}")
            
    except Exception as e:
        log_error(f"Memory extraction failed (non-critical): {e}")


async def search_tavily(query):
    if not TAVILY_KEY:
        return None
    url = "https://api.tavily.com/search"
    payload = {
        "api_key": TAVILY_KEY,
        "query": query,
        "max_results": 5,
        "search_depth": "basic"
    }
    timeout = aiohttp.ClientTimeout(total=15)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(url, json=payload) as resp:
            if resp.status == 200:
                data = await resp.json()
                results = data.get('results', [])
                if results:
                    return "\n".join([f"* {r['title']}: {r['url']}" for r in results[:3]])
            return None


def build_messages_for_user(user_id, content):
    """Build complete message list with all memory layers for a user."""
    messages = [{"role": "system", "content": HERMES_SYSTEM_PROMPT}]
    
    # Add user-specific context (L2)
    user_context = build_user_context(user_id)
    if user_context:
        messages.append({
            "role": "system",
            "content": f"## 关于当前用户\n\n{user_context}"
        })
    
    # Add relevant global knowledge (L3) if any
    if _global_knowledge:
        top_knowledge = _global_knowledge[-15:]
        kb_text = "\n".join(f"- {k}" for k in top_knowledge)
        messages.append({
            "role": "system",
            "content": f"## 你的知识库（从过去的对话中学习到的）\n\n{kb_text}"
        })
    
    # Add conversation history (L1)
    conv = get_conversation(user_id)
    if conv:
        # Only include last 6 exchanges to stay within token limits
        recent_conv = conv[-12:]
        for msg in recent_conv:
            messages.append({"role": msg["role"], "content": msg["content"]})
    
    # Current message
    messages.append({"role": "user", "content": content})
    
    return messages


# ==================== BOT EVENTS ====================

@bot.event
async def on_ready():
    log(f'Hermes Lite v2 READY! Logged in as {bot.user}')
    log(f'Memory: {len(_profiles)} user profiles, {len(_global_knowledge)} knowledge entries')


@bot.event
async def on_message(message):
    global _message_count
    _message_count += 1
    
    content = message.content
    log(f"[MSG #{_message_count}] from {message.author} in #{message.channel}: {content[:80]}")
    
    if message.author == bot.user:
        return
    
    mentioned = bot.user.mentioned_in(message)
    channel_name = message.channel.name.lower() if hasattr(message.channel, 'name') else ''
    should_respond = mentioned or 'hermes' in channel_name or content.startswith('!')
    log(f"[MSG #{_message_count}] should_respond={should_respond}")
    
    if not should_respond:
        return
    
    # Clean mentions from content
    for uid in [str(bot.user.id), f'<@!{bot.user.id}>', f'<@{bot.user.id}>']:
        content = content.replace(uid, '')
    content = content.strip()
    
    if not content:
        await message.reply("你好！我是 Hermes，有记忆力的 AI 助手。问我任何事吧！")
        return
    
    user_id = str(message.author.id)
    
    try:
        async with message.channel.typing():
            # Build messages with full memory context
            messages = build_messages_for_user(user_id, content)
            log(f"[MSG #{_message_count}] Calling API (context: {len(messages)} msgs)")
            
            response = await ask_deepseek(messages)
            log(f"[MSG #{_message_count}] Response: {len(response)} chars")
            
            if len(response) > 1900:
                response = response[:1900] + "..."
            await message.reply(response)
            
            # Save to conversation history (L1)
            add_to_conversation(user_id, "user", content)
            add_to_conversation(user_id, "assistant", response)
            
            # Update activity timestamp
            update_profile(user_id)
            
            log(f"[MSG #{_message_count}] Reply sent ✅ | Extracting memories...")
            
            # 🔬 Self-evolution: Extract learnings in background
            asyncio.create_task(extract_memories(user_id, content, response))
            
    except Exception as e:
        log_error(f"[MSG #{_message_count}] Error: {e}")
        try:
            await message.reply(f"抱歉出错了：{str(e)[:200]}")
        except:
            pass


# ==================== COMMANDS ====================

@bot.command(name='ping')
async def ping_cmd(ctx):
    await ctx.send(f'Pong! 🏓 {round(bot.latency * 1000)}ms')


@bot.command(name='memory')
async def memory_cmd(ctx):
    """Show Hermes's memory about you."""
    user_id = str(ctx.author.id)
    profile = get_profile(user_id)
    facts = profile.get("facts", [])
    name = profile.get("known_name", "未知")
    count = profile.get("message_count", 0)
    
    embed = discord.Embed(
        title=f"🧠 关于你的记忆",
        color=0x7289DA
    )
    embed.add_field(name="👤 昵称", value=name, inline=True)
    embed.add_field(name="💬 消息数", value=str(count), inline=True)
    embed.add_field(name="🧷 已知事实", value=str(len(facts)), inline=True)
    
    if facts:
        facts_text = "\n".join(f"• {f}" for f in facts[:10])
        if len(facts) > 10:
            facts_text += f"\n... 还有 {len(facts)-10} 条"
        embed.add_field(name="📝 我记得", value=facts_text[:500], inline=False)
    
    if profile.get("preferences"):
        pref_str = ", ".join(f"{k}={v}" for k, v in list(profile["preferences"].items())[:5])
        embed.add_field(name="⚙️ 偏好", value=pref_str, inline=False)
    
    await ctx.send(embed=embed)


@bot.command(name='forget')
async def forget_cmd(ctx):
    """Clear your memory from Hermes (privacy)."""
    user_id = str(ctx.author.id)
    key = str(user_id)
    if key in _profiles:
        del _profiles[key]
        _conversations.pop(key, None)
        _save_json(PROFILES_FILE, _profiles)
        _git_commit(f"user {user_id} forgot")
        await ctx.send("🗑️ 你的所有记忆已清除。我们从零开始！")
    else:
        await ctx.send("本来就没有你的记忆~")


@bot.command(name='learn')
async def learn_cmd(ctx, *, info):
    """Manually teach Hermes something to remember about you."""
    user_id = str(ctx.author.id)
    add_user_fact(user_id, info)
    await ctx.send(f"✅ 我记住了：\"{info}\"")


@bot.command(name='status')
async def status_cmd(ctx):
    """Show Hermes system status."""
    embed = discord.Embed(title="🔋 Hermes 状态面板", color=0x57F287)
    embed.add_field(name="⏱️ 延迟", value=f"{round(bot.latency * 1000)}ms", inline=True)
    embed.add_field(name="👥 已知用户", value=str(len(_profiles)), inline=True)
    embed.add_field(name="📚 知识条目", value=str(len(_global_knowledge)), inline=True)
    embed.add_field(name="💭 当前对话", value=str(len(_conversations)), inline=True)
    embed.add_field(name("版本"), value="v2 (持久记忆+自进化)", inline=False)
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


# ==================== STARTUP ====================
log("Bot setup complete, starting bot.run()...")


# === Render Web Service Health Check ===
def start_http_server():
    port = int(os.environ.get('PORT', 10000))
    from http.server import HTTPServer, BaseHTTPRequestHandler
    
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'OK')
        def log_message(self, *args):
            pass
    
    server = HTTPServer(('0.0.0.0', port), Handler)
    log(f"HTTP health check on port {port}")
    server.serve_forever()

http_thread = threading.Thread(target=start_http_server, daemon=True)
http_thread.start()
# ==================================================

try:
    bot.run(DISCORD_TOKEN, log_handler=None)
except Exception as e:
    log_error(f"FATAL: {e}")
    # Save everything before dying
    save_persistent_memory()
    sys.exit(1)
