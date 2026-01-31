import os
import sys
import gc  # Garbage collection for memory management

# --- RENDER-SPECIFIC PATCHING ---
IS_PRODUCTION = os.environ.get('RENDER') is not None

if IS_PRODUCTION:
    import eventlet
    eventlet.monkey_patch()

import asyncio
import random
import logging
import json
import threading
from flask import Flask, render_template
from flask_socketio import SocketIO, emit
from playwright.async_api import async_playwright
import config

app = Flask(__name__)
app.config['SECRET_KEY'] = 'insta-secret-2026'

socket_mode = 'eventlet' if IS_PRODUCTION else 'threading'
socketio = SocketIO(app, cors_allowed_origins="*", async_mode=socket_mode, ping_timeout=60)

logging.getLogger('werkzeug').setLevel(logging.ERROR)

class InstagramBot:
    def __init__(self, user_data, socketio_instance):
        self.username = user_data['username']
        self.password = user_data['password']
        self.cookie_file = f"cookies_{self.username}.json"
        if IS_PRODUCTION:
            self.cookie_file = f"/tmp/{self.cookie_file}"
            
        self.followed_today_count = 0
        self.session_batch_count = 0 
        self.browser = None
        self.context = None
        self.page = None
        self.socketio = socketio_instance

    def web_log(self, message):
        formatted_msg = f"> {message}"
        print(formatted_msg, flush=True)
        self.socketio.emit('bot_update', {'msg': formatted_msg, 'count': self.followed_today_count})

    async def keep_alive_ping(self):
        try:
            self.socketio.emit('heartbeat', {'status': 'active'})
        except: pass

    async def start(self, playwright):
        headless_mode = True if IS_PRODUCTION else config.HEADLESS_MODE
        self.web_log(f"🚀 STARTING: Browser (Headless={headless_mode})")
        
        # --- EXTREME MEMORY SAVING ARGS ---
        args = [
            "--no-sandbox", 
            "--disable-setuid-sandbox", 
            "--disable-dev-shm-usage", 
            "--single-process",            # Vital for 512MB RAM
            "--disable-gpu", 
            "--disable-dev-tools",
            "--no-zygote",
            "--disable-accelerated-2d-canvas"
        ]

        self.browser = await playwright.chromium.launch(headless=headless_mode, args=args)
        
        # Lower resolution context uses less memory
        self.context = await self.browser.new_context(
            viewport={'width': 800, 'height': 600},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
        )
        
        self.context.set_default_navigation_timeout(120000)
        self.context.set_default_timeout(120000)
        self.page = await self.context.new_page()
        
        # --- RESOURCE BLOCKING (RAM SAVER) ---
        async def intercept(route):
            # Block images, CSS, and fonts to save ~200MB of RAM
            if route.request.resource_type in ["image", "media", "font", "stylesheet"]:
                await route.abort()
            else:
                await route.continue_()
        
        await self.page.route("**/*", intercept)

        env_cookies = os.environ.get('SESSION_COOKIES')
        if env_cookies:
            try:
                await self.context.add_cookies(json.loads(env_cookies))
                self.web_log("✅ Cookies loaded from Render Env.")
            except:
                self.web_log("⚠️ Env Cookie Parse Error.")
        elif os.path.exists(self.cookie_file):
            try:
                with open(self.cookie_file, 'r') as f:
                    await self.context.add_cookies(json.load(f))
                self.web_log("✅ Cookies loaded from file.")
            except: pass

        return True

    async def check_if_logged_in(self):
        # We use simpler selectors because CSS is blocked
        markers = ['svg[aria-label="Home"]', 'a[href*="/direct/inbox/"]', 'span:has-text("Search")']
        for _ in range(10):
            for selector in markers:
                try:
                    if await self.page.locator(selector).first.is_visible():
                        return True
                except: continue
            await asyncio.sleep(3)
        return False

    async def login(self):
        self.web_log("NAVIGATING: Opening Instagram...")
        try:
            await self.page.goto("https://www.instagram.com/", wait_until="domcontentloaded")
            if await self.check_if_logged_in():
                self.web_log("✨ Session verified.")
                return True
            
            self.web_log("🔑 Manual login required...")
            await self.page.goto("https://www.instagram.com/accounts/login/")
            await asyncio.sleep(5)
            await self.page.fill('input[name="username"]', self.username)
            await self.page.fill('input[name="password"]', self.password)
            await self.page.click('button[type="submit"]')
            await asyncio.sleep(15)
            
            if await self.check_if_logged_in():
                cookies = await self.context.cookies()
                with open(self.cookie_file, 'w') as f:
                    json.dump(cookies, f)
                return True
        except Exception as e:
            self.web_log(f"❌ Login Error: {str(e)[:40]}")
        return False

    async def search_hashtag(self, hashtag):
        self.web_log(f"🔎 SEARCHING: #{hashtag}")
        try:
            await self.page.goto(f"https://www.instagram.com/explore/tags/{hashtag}/", wait_until="domcontentloaded")
            await asyncio.sleep(8)
            # Since CSS is blocked, we find links directly
            links = await self.page.locator('a[href*="/p/"]').evaluate_all(
                "els => els.map(el => el.getAttribute('href'))"
            )
            return [f"https://www.instagram.com{l}" for l in links if "/p/" in l][:8]
        except:
            return []

    async def process_post(self, post_url, target):
        try:
            self.web_log(f"📸 Opening Post: {post_url.split('/')[-2]}")
            await self.page.goto(post_url, wait_until="domcontentloaded", timeout=90000)
            await asyncio.sleep(8)

            header_clicked = False
            # Simple text-based or link-based selectors work best when CSS is blocked
            selectors = ['header a[href^="/"]', 'a.x1i10hfl']

            for attempt in range(1, 6):
                await self.keep_alive_ping()
                self.web_log(f"⏳ Waiting for profile... (Attempt {attempt}/5)")
                for sel in selectors:
                    try:
                        trigger = self.page.locator(sel).first
                        if await trigger.is_visible():
                            await trigger.click()
                            header_clicked = True
                            break
                    except: continue
                if header_clicked: break
                await asyncio.sleep(10)

            if not header_clicked:
                self.web_log("❌ Profile link not found. Skipping.")
                return

            await asyncio.sleep(6)
            await self.page.wait_for_selector('a[href*="/follower"]', timeout=30000)
            await self.page.locator('a[href*="/follower"]').first.click()
            
            await self.page.wait_for_selector('div[role="dialog"]', timeout=30000)
            await asyncio.sleep(5)
            
            while self.followed_today_count < target:
                await self.keep_alive_ping()
                if self.session_batch_count >= 10:
                    self.web_log("⏳ Batch limit reached. Resting 60s...")
                    await asyncio.sleep(60)
                    self.session_batch_count = 0

                # Look for the "Follow" text in buttons
                follow_btn = self.page.locator('div[role="dialog"] button:has-text("Follow")').first
                
                if await follow_btn.is_visible():
                    await follow_btn.click()
                    self.followed_today_count += 1
                    self.session_batch_count += 1
                    self.web_log(f"✅ Followed ({self.followed_today_count}/{target})")
                    await asyncio.sleep(random.uniform(10, 18))
                else:
                    await self.page.mouse.wheel(0, 800)
                    await asyncio.sleep(5)
                    # If we don't see buttons, the list might be empty
                    if await self.page.locator('button:has-text("Follow")').count() == 0: 
                        break
            
            await self.page.keyboard.press("Escape")
        except Exception as e:
            self.web_log(f"⚠️ Skip: {str(e)[:50]}")

    async def close(self):
        try:
            if self.browser: await self.browser.close()
            # Explicitly clear variables for GC
            self.page = None
            self.context = None
            self.browser = None
        except: pass

# --- Worker Function ---
def run_worker(target_count):
    print(f"System Initialized. Waiting for command...", flush=True)
    print(f"[SYSTEM] Bot started for {target_count} follows.", flush=True)
    
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    async def task():
        user_data = {
            'username': os.environ.get('INSTAGRAM_USERNAME', config.INSTAGRAM_USERNAME),
            'password': os.environ.get('INSTAGRAM_PASSWORD', config.INSTAGRAM_PASSWORD)
        }
        async with async_playwright() as p:
            bot = InstagramBot(user_data, socketio)
            if await bot.start(p):
                if await bot.login():
                    tags = list(config.HASHTAGS_TO_SEARCH)
                    random.shuffle(tags)
                    for tag in tags:
                        if bot.followed_today_count >= target_count: break
                        urls = await bot.search_hashtag(tag)
                        for url in urls:
                            if bot.followed_today_count >= target_count: break
                            await bot.process_post(url, target_count)
                await bot.close()
            socketio.emit('bot_update', {'msg': '🏁 Sequence Completed.', 'count': target_count})
        
        # Force Memory Cleanup
        gc.collect()

    loop.run_until_complete(task())
    loop.close()

# --- Routes ---
@app.route('/')
def index():
    return render_template('index.html')

@socketio.on('start_request')
def handle_start(data):
    target = int(data.get('count', 5))
    threading.Thread(target=run_worker, args=(target,), daemon=True).start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    socketio.run(app, host='0.0.0.0', port=port, debug=False)