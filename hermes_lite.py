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
            response = await ask_deepseek([
                {"role": "system", "content": "You are Hermes, a helpful and concise AI assistant."},
                {"role": "user", "content": content}
            ])
            log(f"[MSG #{_message_count}] DeepSeek responded ({len(response)} chars)")
            
            if len(response) > 1900:
                response = response[:1900] + "..."
            await message.reply(response)
            log(f"[MSG #{_message_count}] Reply sent successfully!")
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

@bot.command(name='ask')
async def ask_cmd(ctx, *, question):
    async with ctx.typing():
        messages = [
            {"role": "system", "content": "You are Hermes, a helpful AI assistant."},
            {"role": "user", "content": question}
        ]
        try:
            response = await ask_deepseek(messages)
            if len(response) > 1900:
                response = response[:1900] + "..."
            await ctx.send(response)
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
