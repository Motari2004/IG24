import os
import sys
import asyncio
import random
import logging
import json
import threading
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError
from flask import Flask, render_template, send_from_directory, abort
from flask_socketio import SocketIO, emit

# Render detection & early monkey patch
IS_RENDER = os.environ.get('RENDER') == 'True'
if IS_RENDER:
    import eventlet
    eventlet.monkey_patch()

# Logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)

# Config / env fallback
DEFAULT_USER = os.environ.get('INSTAGRAM_USERNAME', 'hopefreymosingi')
DEFAULT_PASS = os.environ.get('INSTAGRAM_PASSWORD', 'Scorpio2004')
MAX_DAILY_FOLLOWS = 50
MIN_FOLLOW_DELAY = 2
MAX_FOLLOW_DELAY = 5
HASHTAGS_TO_SEARCH = ["photography", "nature", "travel", "art", "fitness"]

app = Flask(__name__)
app.config['SECRET_KEY'] = 'secret-key-2026'
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='eventlet' if IS_RENDER else 'threading')

class InstagramBot:
    def __init__(self, user_data, socketio=None):
        self.username = user_data['username']
        self.password = user_data.get('password', '')
        self.socketio = socketio
        self.target_follows = int(user_data.get('target_follows', MAX_DAILY_FOLLOWS))
        self.followed_today_count = 0
        self.session_batch_count = 0
        self.browser = None
        self.context = None
        self.page = None

        self.cookie_file = f"/tmp/cookies_{self.username}.json" if IS_RENDER else f"cookies_{self.username}.json"

    def web_log(self, msg, level="info"):
        formatted = f"[{self.username}] {msg}"
        print(formatted)
        if level == "info":
            logging.info(formatted)
        else:
            logging.warning(formatted)
        if self.socketio:
            self.socketio.emit('log_update', {'msg': formatted})

    async def start(self, playwright):
        self.web_log("🚀 Launching browser...")
        launch_args = ["--disable-notifications", "--start-maximized"]
        headless = True if IS_RENDER else False

        if IS_RENDER:
            launch_args.extend(["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"])

        self.browser = await playwright.chromium.launch(
            headless=headless,
            args=launch_args
        )
        self.context = await self.browser.new_context(no_viewport=True)

        cookies_loaded = False

        if IS_RENDER:
            session_cookies_str = os.environ.get('SESSION_COOKIES')
            if session_cookies_str:
                try:
                    cookies = json.loads(session_cookies_str)
                    await self.context.add_cookies(cookies)
                    self.web_log("🍪 Loaded session from Render ENV (SESSION_COOKIES)")
                    cookies_loaded = True
                except Exception as e:
                    self.web_log(f"⚠️ Failed to parse SESSION_COOKIES: {e}", "warn")

        if not cookies_loaded and os.path.exists(self.cookie_file):
            try:
                with open(self.cookie_file, 'r') as f:
                    cookies = json.load(f)
                await self.context.add_cookies(cookies)
                self.web_log("🍪 Loaded cookies from local file")
                cookies_loaded = True
            except Exception as e:
                self.web_log(f"⚠️ Local cookie file error: {e}", "warn")

        if not cookies_loaded:
            self.web_log("⚠️ No cookies loaded → will try login if credentials exist")

        self.page = await self.context.new_page()

        # Light resource blocking (only media/font)
        if IS_RENDER:
            await self.context.route("**/*", lambda route:
                route.abort() if route.request.resource_type in ["image", "media", "font"]
                else route.continue_())

        return True

    async def login_or_check(self):
        self.web_log("🌍 Navigating to Instagram...")
        try:
            await self.page.goto("https://www.instagram.com/", wait_until="commit")

            for attempt in range(1, 21):
                self.web_log(f"⏳ Verifying session (Attempt {attempt}/20)...")
                await asyncio.sleep(5)
                if await self.page.locator('svg[aria-label="Home"]').is_visible():
                    self.web_log("✅ Session active!")
                    return True

            if not self.password:
                self.web_log("❌ No password for fallback login")
                return False

            self.web_log("🔑 Session expired → logging in...")
            await self.page.goto("https://www.instagram.com/accounts/login/")
            await asyncio.sleep(3)

            await self.page.fill('input[name="username"]', self.username)
            await self.page.fill('input[name="password"]', self.password)
            await self.page.click('button[type="submit"]')

            await self.page.wait_for_selector('svg[aria-label="Home"]', timeout=60000)

            cookies = await self.context.cookies()
            with open(self.cookie_file, 'w') as f:
                json.dump(cookies, f)
            self.web_log("✅ Login successful - cookies saved")
            return True

        except Exception as e:
            self.web_log(f"❌ Login/session failed: {str(e)[:100]}", "warn")
            return False

    async def search_hashtag(self, hashtag):
        self.web_log(f"🔎 Searching: #{hashtag}")
        try:
            await self.page.goto(f"https://www.instagram.com/explore/tags/{hashtag}/", wait_until="domcontentloaded")
            await self.page.wait_for_selector('div._aagu, article', timeout=60000)
            await self.page.wait_for_load_state('networkidle', timeout=30000)
            
            await self.page.mouse.wheel(0, 2000)
            await asyncio.sleep(5)  # extra wait
            
            links = await self.page.locator('a:has(div._aagu)').evaluate_all(
                "els => els.map(el => el.getAttribute('href'))"
            )
            unique_urls = [f"https://www.instagram.com{l}" for l in list(dict.fromkeys(links)) if "/p/" in l]
            return unique_urls[:12]
        except Exception as e:
            self.web_log(f"⚠️ Search failed for #{hashtag}: {str(e)[:80]}", "warn")
            try:
                timestamp = int(asyncio.get_event_loop().time())
                screenshot_filename = f"search_timeout_{self.username}_{timestamp}.png"
                await self.page.screenshot(path=f"/tmp/{screenshot_filename}", full_page=True)
                render_domain = os.environ.get('RENDER_EXTERNAL_HOSTNAME', 'ig24.onrender.com')
                self.web_log(f"📸 Search timeout screenshot: https://{render_domain}/debug-screenshot/{screenshot_filename}")
            except:
                pass
            return []

    async def process_post(self, post_url):
        self.web_log(f"📸 Processing: {post_url.split('/')[-2]}")
        try:
            # Retry page load on timeout
            for retry in range(1, 3):
                try:
                    await self.page.goto(post_url, wait_until="domcontentloaded", timeout=60000)
                    break
                except PlaywrightTimeoutError:
                    self.web_log(f"Page.goto timeout (retry {retry}/2)...")
                    await asyncio.sleep(5)

            await self.page.wait_for_selector('div._aagu, article', timeout=60000)
            await self.page.wait_for_load_state('networkidle', timeout=30000)
            await asyncio.sleep(random.uniform(3, 6))

            username_selector = 'span._ap3a._aaco._aacw._aacx._aad7._aade'
            user_trigger = self.page.locator(username_selector).last

            if await user_trigger.is_visible():
                target_user = await user_trigger.inner_text()
                await user_trigger.click()

                try:
                    self.web_log(f"⏳ Waiting for {target_user} profile data...")
                    await self.page.wait_for_url(f"**/{target_user}/", timeout=15000)
                    await self.page.wait_for_selector('header', timeout=8000)
                    self.web_log(f"👤 Profile {target_user} loaded.")
                except Exception:
                    self.web_log("⚠️ Profile header slow, attempting immediate click...")

                try:
                    followers_btn = self.page.locator(f'a[href="/{target_user}/followers/"]').first
                    await followers_btn.click(force=True)

                    self.web_log("⏳ Modal triggered, waiting for content to render...")
                    await self.page.wait_for_selector('div[role="dialog"], div._aano', timeout=20000)
                    await asyncio.sleep(6)  # extra time for list load
                    self.web_log("👥 Followers list ready.")
                except Exception as e:
                    self.web_log(f"🔒 Could not open followers: {str(e)[:50]}")
                    try:
                        timestamp = int(asyncio.get_event_loop().time())
                        screenshot_filename = f"modal_fail_{self.username}_{timestamp}.png"
                        await self.page.screenshot(path=f"/tmp/{screenshot_filename}", full_page=True)
                        render_domain = os.environ.get('RENDER_EXTERNAL_HOSTNAME', 'ig24.onrender.com')
                        self.web_log(f"📸 Modal fail screenshot: https://{render_domain}/debug-screenshot/{screenshot_filename}")
                    except:
                        pass
                    return

                self.web_log("🏃 Starting follow sequence...")

                # ────────────────────────────────────────────────
                # DEBUG SCREENSHOT + PUBLIC URL
                # ────────────────────────────────────────────────
                try:
                    timestamp = int(asyncio.get_event_loop().time())
                    screenshot_filename = f"follow_start_{self.username}_{timestamp}.png"
                    screenshot_path = f"/tmp/{screenshot_filename}"
                    await self.page.screenshot(path=screenshot_path, full_page=True)
                    self.web_log(f"📸 Debug screenshot saved: {screenshot_path}")

                    render_domain = os.environ.get('RENDER_EXTERNAL_HOSTNAME', 'ig24.onrender.com')
                    screenshot_url = f"https://{render_domain}/debug-screenshot/{screenshot_filename}"
                    self.web_log(f"🔗 View screenshot: {screenshot_url}")
                except Exception as screenshot_err:
                    self.web_log(f"⚠️ Screenshot failed: {screenshot_err}", "warn")

                await asyncio.sleep(random.uniform(5.5, 9.5))

                follow_selector = 'div[role="dialog"] button >> text="Follow"'
                scroll_container = self.page.locator('div._aano').first

                while self.followed_today_count < self.target_follows:
                    follow_buttons = self.page.locator(follow_selector)
                    count = await follow_buttons.count()

                    if count == 0:
                        try:
                            await scroll_container.evaluate('el => el.scrollTop += 650')
                        except:
                            await self.page.mouse.wheel(0, 650)
                        await asyncio.sleep(random.uniform(4.0, 7.0))
                        continue

                    processed_this_batch = 0
                    max_per_batch = 4

                    for i in range(count):
                        if self.followed_today_count >= self.target_follows:
                            break
                        if processed_this_batch >= max_per_batch:
                            break

                        btn = follow_buttons.nth(i)
                        try:
                            await btn.scroll_into_view_if_needed(timeout=5000)
                            await asyncio.sleep(random.uniform(0.8, 1.5))
                            await btn.click(
                                force=True,
                                delay=random.uniform(90, 190),
                                timeout=10000
                            )
                            self.followed_today_count += 1
                            self.session_batch_count += 1
                            self.web_log(f"✅ Followed ({self.followed_today_count}/{self.target_follows})")
                            await asyncio.sleep(random.uniform(
                                MIN_FOLLOW_DELAY,
                                MAX_FOLLOW_DELAY
                            ))
                            processed_this_batch += 1
                        except Exception:
                            continue

                    try:
                        await scroll_container.evaluate('el => el.scrollTop += 850')
                    except:
                        await self.page.mouse.wheel(0, 850)
                    await asyncio.sleep(random.uniform(2.0, 4.5))

                try:
                    await self.page.keyboard.press("Escape")
                except:
                    try:
                        await self.page.locator('div[role="dialog"] button[aria-label="Close"]').click(timeout=4000)
                    except:
                        pass
        except Exception as e:
            self.web_log(f"⚠️ Skipping post error: {str(e)[:50]}", "warn")

    async def close(self):
        if self.browser:
            await self.browser.close()
            self.web_log("🔒 Browser closed.")

# ==========================================
# FLASK ROUTES
# ==========================================
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/debug-screenshot/<filename>')
def serve_screenshot(filename):
    if not filename.startswith('follow_start_') and not filename.startswith('modal_fail_'):
        abort(403)
    try:
        return send_from_directory('/tmp', filename)
    except FileNotFoundError:
        abort(404)
    except Exception as e:
        logging.error(f"Screenshot serve error: {e}")
        abort(500)

@socketio.on('start_bot')
def handle_start_bot(data):
    target = int(data.get('target_follows', 10))

    user_data = {
        'username': DEFAULT_USER,
        'password': DEFAULT_PASS,
        'target_follows': target
    }

    emit('log_update', {'msg': f"🚀 Starting bot... Target: {target} follows"})

    threading.Thread(
        target=lambda: asyncio.run(run_worker(user_data)),
        daemon=True
    ).start()

async def run_worker(user_data):
    bot = InstagramBot(user_data, socketio)

    try:
        async with async_playwright() as p:
            if await bot.start(p):
                if await bot.login_or_check():
                    hashtags = list(HASHTAGS_TO_SEARCH)
                    random.shuffle(hashtags)

                    for tag in hashtags:
                        if bot.followed_today_count >= bot.target_follows:
                            break
                        urls = await bot.search_hashtag(tag)
                        for url in urls:
                            if bot.followed_today_count >= bot.target_follows:
                                break
                            await bot.process_post(url)

                    bot.web_log(f"🏁 Finished following {bot.followed_today_count} accounts!")
    finally:
        await bot.close()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"Starting on port {port} (Render: {IS_RENDER})")
    socketio.run(app, host="0.0.0.0", port=port, debug=False, allow_unsafe_werkzeug=True)