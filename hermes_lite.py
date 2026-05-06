#!/usr/bin/env python3
"""
Hermes Lite - Ultra lightweight Discord bot for Render
Only Discord + DeepSeek + Tavily, ~50MB RAM
"""

import sys
import os
import asyncio
import traceback
import datetime
import threading
import json
import hashlib

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

log("=== HERMES LITE STARTING ===")
log(f"Python: {sys.version}")
log(f"Args: {sys.argv}")

# Check environment
DISCORD_TOKEN = os.environ.get('DISCORD_BOT_TOKEN', '')
DEEPSEEK_KEY = os.environ.get('DEEPSEEK_API_KEY', '')
TAVILY_KEY = os.environ.get('TAVILY_API_KEY', '')

log(f"DISCORD_TOKEN set: {bool(DISCORD_TOKEN)}")
log(f"DEEPSEEK_KEY set: {bool(DEEPSEEK_KEY)}")
log(f"TAVILY_KEY set: {bool(TAVILY_KEY)}")

if not DISCORD_TOKEN:
    log("FATAL: DISCORD_BOT_TOKEN not set!")
    sys.exit(1)
if not DEEPSEEK_KEY:
    log("FATAL: DEEPSEEK_API_KEY not set!")
    sys.exit(1)

log("Environment OK, importing libraries...")

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

log("All imports OK, setting up bot...")

# Bot setup - enable all needed intents
intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.messages = True

bot = commands.Bot(command_prefix='!', intents=intents)

# Track if messages are arriving at all
_message_count = 0

DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"

# === Memory System: file-based per-user conversation memory ===
MEMORY_FILE = "/tmp/hermes_memory.json"
_memory_cache = {}  # in-memory cache to avoid disk reads on every msg

def _memory_key(user_id):
    return str(user_id)

def load_memories():
    """Load all memories from JSON file into cache."""
    global _memory_cache
    try:
        if os.path.exists(MEMORY_FILE):
            with open(MEMORY_FILE, 'r') as f:
                _memory_cache = json.load(f)
            log(f"Memory loaded: {len(_memory_cache)} users")
        else:
            _memory_cache = {}
    except Exception as e:
        log_error(f"Failed to load memory: {e}")
        _memory_cache = {}

def save_memories():
    """Persist memory cache to JSON file."""
    try:
        with open(MEMORY_FILE, 'w') as f:
            json.dump(_memory_cache, f, ensure_ascii=False)
    except Exception as e:
        log_error(f"Failed to save memory: {e}")

def get_user_memory(user_id):
    """Get memory entries for a user."""
    key = _memory_key(user_id)
    return _memory_cache.get(key, [])

def add_user_memory(user_id, role, content):
    """Append a memory entry for a user (kept under 10 entries, auto-summarizes)."""
    key = _memory_key(user_id)
    if key not in _memory_cache:
        _memory_cache[key] = []
    
    _memory_cache[key].append({
        "role": role,
        "content": content[:500],  # truncate long messages
        "ts": datetime.datetime.now().isoformat()
    })
    
    # Keep only last 10 exchanges (20 messages) to stay within token limits
    if len(_memory_cache[key]) > 20:
        _memory_cache[key] = _memory_cache[key][-20:]
    
    save_memories()

# Load memory at startup
load_memories()
# ===============================================================

HERMES_SYSTEM_PROMPT = """你是 Hermes（赫尔墨斯），一个友好、有记忆力的 AI 助手。

## 性格特点
- 用中文回复（除非对方用英文）
- 简洁但有温度，不要废话
- 偶尔可以用 emoji，但不要过度
- 你有记忆功能，能记住之前聊过的事情

## 关于你的能力
- 你运行在云端服务器上
- 你可以搜索网络信息
- 你会记住用户告诉你的重要事情
- 你的对话是持久化的，不是一次性的

## 回复原则
1. 直接回答问题，不要绕弯子
2. 如果不确定，诚实说不知道
3. 用户问"你在哪"/"怎么运行"时，如实回答：你运行在云端服务器上，通过 Discord 跟用户对话
4. 不要主动说"我不会存储你的数据"之类的话 —— 实际上你有记忆功能"""

async def ask_deepseek(messages):
    headers = {
        "Authorization": f"Bearer {DEEPSEEK_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": "deepseek-chat",
        "messages": messages,
        "max_tokens": 2000,
        "temperature": 0.7
    }
    
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(DEEPSEEK_URL, headers=headers, json=payload) as resp:
            text = await resp.text()
            if resp.status == 200:
                data = await resp.json()
                return data['choices'][0]['message']['content']
            else:
                return f"DeepSeek API error {resp.status}: {text[:200]}"

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
                    return "\n".join([f"• {r['title']}: {r['url']}" for r in results[:3]])
            return None

@bot.event
async def on_ready():
    log(f'Hermes Lite READY! Logged in as {bot.user}')

@bot.event
async def on_message(message):
    global _message_count
    _message_count += 1
    
    # Extract content early so we can use it in checks
    content = message.content
    # DIAGNOSTIC: Log every single message to confirm gateway is alive
    log(f"[MSG #{_message_count}] from {message.author} in #{message.channel}: {content[:80]}")
    
    if message.author == bot.user:
        return
    
    mentioned = bot.user.mentioned_in(message)
    
    # Respond if: mentioned OR channel name contains 'hermes' OR starts with !
    channel_name = message.channel.name.lower() if hasattr(message.channel, 'name') else ''
    should_respond = mentioned or 'hermes' in channel_name or content.startswith('!')
    log(f"[MSG #{_message_count}] mentioned={mentioned} channel={channel_name} should_respond={should_respond}")
    
    if not should_respond:
        return
    
    # Clean mentions from content
    for uid in [str(bot.user.id), f'<@!{bot.user.id}>', f'<@{bot.user.id}>']:
        content = content.replace(uid, '')
    content = content.strip()
    
    log(f"[MSG #{_message_count}] Processing prompt: '{content[:100]}'")
    
    if not content:
        try:
            await message.reply("Hi! I'm Hermes Lite. Ask me anything or use `!search <query>`.")
        except Exception as e:
            log_error(f"Failed to reply: {e}")
        return
    
    try:
        async with message.channel.typing():
            log(f"[MSG #{_message_count}] Calling DeepSeek API...")
            
            # Build messages with memory context
            user_id = str(message.author.id)
            user_memory = get_user_memory(user_id)
            
            messages = [{"role": "system", "content": HERMES_SYSTEM_PROMPT}]
            
            # Add last 10 memory entries as context (keep token usage reasonable)
            if user_memory:
                mem_context = user_memory[-10:]
                for m in mem_context:
                    messages.append({"role": m["role"], "content": m["content"]})
                log(f"[MSG #{_message_count}] Loaded {len(mem_context)} memory entries for user")
            
            messages.append({"role": "user", "content": content})
            
            response = await ask_deepseek(messages)
            log(f"[MSG #{_message_count}] DeepSeek responded ({len(response)} chars)")
            
            if len(response) > 1900:
                response = response[:1900] + "..."
            await message.reply(response)
            log(f"[MSG #{_message_count}] Reply sent successfully!")
            
            # Save this exchange to memory
            add_user_memory(user_id, "user", content)
            add_user_memory(user_id, "assistant", response)
            log(f"[MSG #{_message_count}] Memory saved for user")
    except Exception as e:
        log_error(f"[MSG #{_message_count}] Error processing: {e}")
        try:
            await message.reply(f"Sorry, I hit an error: {str(e)[:200]}")
        except:
            pass

@bot.command(name='search')
async def search_cmd(ctx, *, query):
    if not TAVILY_KEY:
        await ctx.send("Search not configured.")
        return
    async with ctx.typing():
        results = await search_tavily(query)
        if results:
            await ctx.send(f"**Results for:** {query}\n{results}")
        else:
            await ctx.send("No results found.")

@bot.command(name='ping')
async def ping_cmd(ctx):
    await ctx.send(f'Pong! {round(bot.latency * 1000)}ms')

@bot.command(name='memory')
async def memory_cmd(ctx):
    """Show how many memory entries Hermes has for you."""
    user_id = str(ctx.author.id)
    mem = get_user_memory(user_id)
    count = len(mem)
    if count == 0:
        await ctx.send("🧹 我还没有关于你的任何记忆。聊起来吧！")
    else:
        first_ts = mem[0].get("ts", "?")[:10]
        await ctx.send(f"📚 我有 **{count//2}** 条对话记忆（从 {first_ts} 开始）。我记得你告诉过我的事情！")

@bot.command(name='forget')
async def forget_cmd(ctx):
    """Clear all your memories from Hermes."""
    user_id = str(ctx.author.id)
    key = _memory_key(user_id)
    if key in _memory_cache:
        del _memory_cache[key]
        save_memories()
        await ctx.send("🗑️ 已清除你所有的对话记忆。我们从零开始！")
    else:
        await ctx.send("本来就没有你的记忆，没什么可清的~")

@bot.command(name='ask')
async def ask_cmd(ctx, *, question):
    async with ctx.typing():
        user_id = str(ctx.author.id)
        user_memory = get_user_memory(user_id)
        
        messages = [{"role": "system", "content": HERMES_SYSTEM_PROMPT}]
        if user_memory:
            for m in user_memory[-10:]:
                messages.append({"role": m["role"], "content": m["content"]})
        messages.append({"role": "user", "content": question})
        try:
            response = await ask_deepseek(messages)
            if len(response) > 1900:
                response = response[:1900] + "..."
            await ctx.send(response)
            add_user_memory(user_id, "user", question)
            add_user_memory(user_id, "assistant", response)
        except Exception as e:
            await ctx.send(f"Error: {str(e)[:200]}")

log("Bot setup complete, starting bot.run()...")

# === Render Web Service: start dummy HTTP server on PORT ===
def start_http_server():
    """Minimal HTTP server for Render health check - returns 200 on /"""
    port = int(os.environ.get('PORT', 10000))
    from http.server import HTTPServer, BaseHTTPRequestHandler
    
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'OK')
        def log_message(self, *args):
            pass  # silence logs
    
    server = HTTPServer(('0.0.0.0', port), Handler)
    log(f"HTTP health check server started on port {port}")
    server.serve_forever()

http_thread = threading.Thread(target=start_http_server, daemon=True)
http_thread.start()
# ================================================================

try:
    bot.run(DISCORD_TOKEN, log_handler=None)
except Exception as e:
    log_error(f"FATAL in bot.run(): {e}")
    sys.exit(1)
