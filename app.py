import os
import sys
import gc

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
from flask_socketio import SocketIO
from playwright.async_api import async_playwright
import config

app = Flask(__name__)
app.config['SECRET_KEY'] = 'insta-secret-2026'

socket_mode = 'eventlet' if IS_PRODUCTION else 'threading'
socketio = SocketIO(app, cors_allowed_origins="*", async_mode=socket_mode, ping_timeout=60)

# Clean console logs
logging.getLogger('werkzeug').setLevel(logging.ERROR)

class InstagramBot:
    def __init__(self, user_data, socketio_instance):
        self.username = user_data['username']
        self.password = user_data['password']
        self.socketio = socketio_instance
        self.cookie_file = f"cookies_{self.username}.json"
        if IS_PRODUCTION:
            self.cookie_file = f"/tmp/{self.cookie_file}"
            
        self.followed_today_count = 0
        self.session_batch_count = 0 
        self.browser = None
        self.context = None
        self.page = None

    def web_log(self, message):
        formatted_msg = f"> {message}"
        print(formatted_msg, flush=True)
        self.socketio.emit('bot_update', {'msg': formatted_msg, 'count': self.followed_today_count})

    async def keep_alive_ping(self):
        try: self.socketio.emit('heartbeat', {'status': 'active'})
        except: pass

    async def start(self, playwright):
        headless_mode = True if IS_PRODUCTION else config.HEADLESS_MODE
        self.web_log(f"🚀 STARTING: Browser (Headless={headless_mode})")
        
        args = ["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage", "--single-process"]
        if IS_PRODUCTION:
            args.extend(["--disable-gpu", "--no-zygote"])

        self.browser = await playwright.chromium.launch(headless=headless_mode, args=args)
        self.context = await self.browser.new_context(
            viewport={'width': 800, 'height': 600},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
        )
        
        self.context.set_default_navigation_timeout(90000)
        self.context.set_default_timeout(90000)
        self.page = await self.context.new_page()
        
        # RESOURCE BLOCKER: Blocks images/media but ALLOWS CSS (Stylesheets) for detection
        async def intercept(route):
            if route.request.resource_type in ["image", "media", "font"]: await route.abort()
            else: await route.continue_()
        await self.page.route("**/*", intercept)

        # Cookie Fetching (Env Variable First)
        env_cookies = os.environ.get('SESSION_COOKIES')
        if env_cookies:
            try:
                await self.context.add_cookies(json.loads(env_cookies))
                self.web_log("✅ Cookies loaded from Render Env.")
            except: self.web_log("⚠️ Env Cookie Parse Error.")
        elif os.path.exists(self.cookie_file):
            with open(self.cookie_file, 'r') as f:
                await self.context.add_cookies(json.load(f))
                self.web_log("✅ Cookies loaded from file.")

        return True

    async def check_if_logged_in(self):
        markers = ['svg[aria-label="Home"]', 'img[alt*="profile picture"]', 'span:has-text("Search")']
        for _ in range(15):
            for selector in markers:
                try:
                    if await self.page.locator(selector).first.is_visible(): return True
                except: continue
            await asyncio.sleep(2)
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
                with open(self.cookie_file, 'w') as f: json.dump(cookies, f)
                return True
        except Exception as e: self.web_log(f"❌ Login Error: {str(e)[:40]}")
        return False

    async def search_hashtag(self, hashtag):
        self.web_log(f"🔎 SEARCHING: #{hashtag}")
        try:
            await self.page.goto(f"https://www.instagram.com/explore/tags/{hashtag}/", wait_until="domcontentloaded")
            # Wait for the specific container you use in your logic
            await self.page.wait_for_selector('div._aagu', timeout=30000)
            
            for _ in range(2):
                await self.page.mouse.wheel(0, 1000)
                await asyncio.sleep(3) 
            
            links = await self.page.locator('a:has(div._aagu)').evaluate_all(
                "els => els.map(el => el.getAttribute('href'))"
            )
            unique_urls = [f"https://www.instagram.com{l}" for l in list(dict.fromkeys(links)) if "/p/" in l]
            self.web_log(f"📊 Extracted {len(unique_urls)} posts.")
            return unique_urls[:12]
        except Exception as e:
            self.web_log(f"⚠️ Search failed for #{hashtag}: {str(e)[:40]}")
            return []

    async def process_post(self, post_url, target):
        try:
            self.web_log(f"📸 Opening Post: {post_url.split('/')[-2]}")
            await self.page.goto(post_url, wait_until="domcontentloaded", timeout=60000)
            await asyncio.sleep(random.uniform(3, 5))

            # --- OPEN PROFILE ---
            # Using your specific class-based selector
            username_selector = 'span._ap3a._aaco._aacw._aacx._aad7._aade'
            user_trigger = self.page.locator(username_selector).last

            if await user_trigger.is_visible():
                await user_trigger.click()
                # Wait for profile to load
                await self.page.wait_for_selector('span:has-text("followers")', timeout=30000)
            else:
                self.web_log("❌ User trigger not found.")
                return

            # --- OPEN FOLLOWERS MODAL ---
            try:
                followers_btn = self.page.locator('a[href*="/followers/"]').first
                await followers_btn.wait_for(state="attached", timeout=10000)
                await followers_btn.click()
                await self.page.wait_for_selector('div[role="dialog"]', timeout=20000)
                await asyncio.sleep(3)
            except:
                self.web_log("⚠️ Followers modal failed to open.")
                return

            # --- FOLLOW LOOP ---
            while self.followed_today_count < target:
                await self.keep_alive_ping()
                if self.session_batch_count >= 10:
                    self.web_log("⏳ Resting 60s (Batch limit)...")
                    await asyncio.sleep(60)
                    self.session_batch_count = 0

                modal = self.page.locator('div[role="dialog"]')
                follow_btn = modal.get_by_role("button", name="Follow", exact=True).first
                
                if await follow_btn.is_visible():
                    await follow_btn.click()
                    self.followed_today_count += 1
                    self.session_batch_count += 1
                    self.web_log(f"✅ Followed ({self.followed_today_count}/{target})")
                    await asyncio.sleep(random.uniform(12, 20))
                else:
                    await self.page.mouse.wheel(0, 800)
                    await asyncio.sleep(4)
                    if await modal.get_by_role("button", name="Follow", exact=True).count() == 0:
                        break

            await self.page.keyboard.press("Escape")
        except Exception as e: self.web_log(f"⚠️ Skip Post: {str(e)[:40]}")

    async def close(self):
        try:
            if self.browser: await self.browser.close()
            gc.collect()
        except: pass

# --- Worker Logic ---
def run_worker(target_count):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    async def task():
        user_data = {'username': os.environ.get('INSTAGRAM_USERNAME', config.INSTAGRAM_USERNAME),
                     'password': os.environ.get('INSTAGRAM_PASSWORD', config.INSTAGRAM_PASSWORD)}
        async with async_playwright() as p:
            bot = InstagramBot(user_data, socketio)
            if await bot.start(p):
                if await bot.login():
                    hashtags = list(config.HASHTAGS_TO_SEARCH)
                    random.shuffle(hashtags)
                    for tag in hashtags:
                        if bot.followed_today_count >= target_count: break
                        urls = await bot.search_hashtag(tag)
                        for url in urls:
                            if bot.followed_today_count >= target_count: break
                            await bot.process_post(url, target_count)
                await bot.close()
            socketio.emit('bot_update', {'msg': '🏁 Sequence Completed.', 'count': target_count})
    loop.run_until_complete(task())
    loop.close()

@app.route('/')
def index(): return render_template('index.html')

@socketio.on('start_request')
def handle_start(data):
    target = int(data.get('count', 5))
    threading.Thread(target=run_worker, args=(target,), daemon=True).start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    socketio.run(app, host='0.0.0.0', port=port, debug=False)