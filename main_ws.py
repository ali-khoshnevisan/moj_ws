import asyncio
import json
import base64
import hashlib
import math
import os
import re
import shutil
import random
from urllib.parse import urljoin
import websockets
import aiohttp
import requests
from playwright.async_api import async_playwright
from instagrapi import Client
from instagrapi.exceptions import ChallengeRequired, LoginRequired, BadPassword
from filelock import FileLock, Timeout as LockTimeout

PROFILES_DIR = "/var/www/firefox_profiles"
DJANGO_SERVICE_CREATE_PROFILE_URL = "http://127.0.0.1:6688/api/social-media/profile/"
STREAM_FPS = 3
JPEG_QUALITY = 20

PROXY_HOST = "proxy.ghostvps.com"
PROXY_PASSWORD = "92964b4ea532a8e1"
PROFILE_LOCK_DIR = os.path.join(PROFILES_DIR, ".locks")
PROFILE_LOCK_TIMEOUT = 180

# ---------- Device Diversity ----------
FIREFOX_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:128.0) Gecko/20100101 Firefox/128.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:129.0) Gecko/20100101 Firefox/129.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:130.0) Gecko/20100101 Firefox/130.0",
    "Mozilla/5.0 (Windows NT 11.0; Win64; x64; rv:128.0) Gecko/20100101 Firefox/128.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:128.0) Gecko/20100101 Firefox/128.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:129.0) Gecko/20100101 Firefox/129.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14.0; rv:128.0) Gecko/20100101 Firefox/128.0",
    "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0",
    "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:129.0) Gecko/20100101 Firefox/129.0",
]

VIEWPORTS = [
    {"width": 1366, "height": 768},
    {"width": 1440, "height": 900},
    {"width": 1536, "height": 864},
    {"width": 1600, "height": 900},
    {"width": 1920, "height": 1080},
    {"width": 1280, "height": 720},
    {"width": 1680, "height": 1050},
]

PLATFORMS = ["Win32", "MacIntel", "Linux x86_64"]

COUNTRY_LOCALE_MAP = {
    "us": {"country": "US", "country_code": 1, "locale": "en_US", "timezone_offset": -14400,
           "accept_lang": "en-US,en;q=0.9"},
    "de": {"country": "DE", "country_code": 49, "locale": "de_DE", "timezone_offset": 3600,
           "accept_lang": "de-DE,de;q=0.9,en;q=0.8"},
    "fr": {"country": "FR", "country_code": 33, "locale": "fr_FR", "timezone_offset": 3600,
           "accept_lang": "fr-FR,fr;q=0.9,en;q=0.8"},
    "gb": {"country": "GB", "country_code": 44, "locale": "en_GB", "timezone_offset": 0,
           "accept_lang": "en-GB,en;q=0.9"},
    "es": {"country": "ES", "country_code": 34, "locale": "es_ES", "timezone_offset": 3600,
           "accept_lang": "es-ES,es;q=0.9,en;q=0.8"},
    "it": {"country": "IT", "country_code": 39, "locale": "it_IT", "timezone_offset": 3600,
           "accept_lang": "it-IT,it;q=0.9,en;q=0.8"},
    "nl": {"country": "NL", "country_code": 31, "locale": "nl_NL", "timezone_offset": 3600,
           "accept_lang": "nl-NL,nl;q=0.9,en;q=0.8"},
    "tr": {"country": "TR", "country_code": 90, "locale": "tr_TR", "timezone_offset": 10800,
           "accept_lang": "tr-TR,tr;q=0.9,en;q=0.8"},
}


def country_from_proxy_username(proxy_username):
    match = re.search(r"cr\.([a-z]{2})", proxy_username or "", re.IGNORECASE)
    return match.group(1).lower() if match else None


def apply_locale_for_country(client, country_code):
    locale_data = COUNTRY_LOCALE_MAP.get((country_code or "").lower())
    if not locale_data:
        print(f"no locale mapping for country '{country_code}', leaving instagrapi defaults (US)")
        return
    client.country = locale_data["country"]
    client.country_code = locale_data["country_code"]
    client.locale = locale_data["locale"]
    client.timezone_offset = locale_data["timezone_offset"]


def get_random_device_config(country_code=None):
    ua = random.choice(FIREFOX_USER_AGENTS)
    viewport = random.choice(VIEWPORTS)
    platform = random.choice(PLATFORMS)

    locale_data = COUNTRY_LOCALE_MAP.get((country_code or "us").lower(), COUNTRY_LOCALE_MAP["us"])

    return {
        "user_agent": ua,
        "viewport": viewport,
        "platform": platform,
        "locale": locale_data["locale"].replace("_", "-"),
        "accept_lang": locale_data["accept_lang"],
        "hardware_concurrency": random.choice([4, 6, 8, 12, 16]),
        "device_memory": random.choice([4, 8, 16]),
    }


def get_device_config_for_profile(profile_path, country_code=None):
    digest = hashlib.md5(profile_path.encode("utf-8")).hexdigest()
    seed = int(digest, 16)

    ua = FIREFOX_USER_AGENTS[seed % len(FIREFOX_USER_AGENTS)]
    viewport = VIEWPORTS[seed % len(VIEWPORTS)]
    platform = PLATFORMS[seed % len(PLATFORMS)]
    hardware_concurrency = [4, 6, 8, 12, 16][seed % 5]
    device_memory = [4, 8, 16][seed % 3]

    locale_data = COUNTRY_LOCALE_MAP.get((country_code or "us").lower(), COUNTRY_LOCALE_MAP["us"])

    return {
        "user_agent": ua,
        "viewport": viewport,
        "platform": platform,
        "locale": locale_data["locale"].replace("_", "-"),
        "accept_lang": locale_data["accept_lang"],
        "hardware_concurrency": hardware_concurrency,
        "device_memory": device_memory,
    }


os.makedirs(PROFILES_DIR, exist_ok=True)
os.makedirs(PROFILE_LOCK_DIR, exist_ok=True)

playwright = None



def profile_lock_path(profile_path):
    name = os.path.basename(profile_path.rstrip("/").rstrip("\\"))
    return os.path.join(PROFILE_LOCK_DIR, f"ffprofile-{name}.lock")

def build_auth_headers(token):
    if not token:
        return {}
    return {"Authorization": f"Bearer {token}"}

def generate_instagrapi_session(playwright_cookies, output_json_path, port, username):
    cookie_dict = {
        c["name"]: c["value"]
        for c in playwright_cookies
        if c["domain"] in [".instagram.com", "instagram.com"]
    }

    if "sessionid" not in cookie_dict:
        print("session id not found!")
        return False, "auth"

    try:
        cl = Client()
        proxy_url = f"{username}:{PROXY_PASSWORD}@{PROXY_HOST}:{port}"
        cl.set_proxy(proxy_url)

        country_code = country_from_proxy_username(username)
        apply_locale_for_country(cl, country_code)

        hardware_profiles = [
            {"android_version": 33, "android_release": "13", "dpi": "480dpi",
             "resolution": "1080x2400", "manufacturer": "Samsung",
             "device": "SM-G998B", "model": "SM-G998B", "cpu": "qcom"},
            {"android_version": 34, "android_release": "14", "dpi": "420dpi",
             "resolution": "1080x2340", "manufacturer": "Google/google",
             "device": "panther", "model": "Pixel 7", "cpu": "panther"},
            {"android_version": 34, "android_release": "14", "dpi": "480dpi",
             "resolution": "1344x2992", "manufacturer": "Google/google",
             "device": "husky", "model": "Pixel 8 Pro", "cpu": "husky"},
        ]
        app_versions = [
            {"app_version": "428.0.0.47.67", "version_code": "961145276",
             "bloks_versioning_id": "7189b949425f9bf80ea8bd880cf5a3080b292d9b1c4b38a18d112f7c4b71e7a8"},
            {"app_version": "385.0.0.47.74", "version_code": "378906843",
             "bloks_versioning_id": "a8973d49a9cc6a6f65a4997c10216ce2a06f65a517010e64885e92029bb19221"},
            {"app_version": "364.0.0.35.86", "version_code": "374010953",
             "bloks_versioning_id": "8ccf54aad76788a6ca03ddfc33afcdcf692f2f5a3ba814ea73d5facba7fa2c2d"},
        ]

        account_key = cookie_dict.get("ds_user_id", cookie_dict["sessionid"])
        digest = int(hashlib.md5(account_key.encode("utf-8")).hexdigest(), 16)
        hardware = hardware_profiles[digest % len(hardware_profiles)]
        app_info = app_versions[digest % len(app_versions)]

        cl.set_device(hardware)
        cl.device_settings.update(app_info)
        cl.bloks_versioning_id = app_info["bloks_versioning_id"]
        cl.set_user_agent()

        cl.login_by_sessionid(cookie_dict["sessionid"])
        cl.dump_settings(output_json_path)
        print(f"session file created successfully: {output_json_path} (locale country={cl.country}, device={hardware['model']}, app_version={app_info['app_version']})")
        return True, None
    except (ChallengeRequired, LoginRequired, BadPassword) as e:
        print(f"instagram rejected the login ({type(e).__name__}): {e}")
        return False, "auth"
    except Exception as e:
        print(f"error in create session id with instagrapi: {e}")
        return False, "error"


async def patch_profile(bot_id, auth_headers, payload):
    update_url = f"{DJANGO_SERVICE_CREATE_PROFILE_URL.rstrip('/')}/{bot_id}/"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.patch(update_url, headers=auth_headers, json=payload) as response:
                ok = 200 <= int(response.status) < 300
                try:
                    print("patch_profile:", response.status, await response.json())
                except Exception:
                    print("patch_profile:", response.status, await response.text())
                return ok
    except Exception as e:
        print("patch_profile failed:", e)
        return False


async def create_profile(headers):
    try:
        async with (aiohttp.ClientSession() as session):
            async with session.get(urljoin(DJANGO_SERVICE_CREATE_PROFILE_URL, "allocate-port/"),
                                   headers=headers) as response_port:
                if response_port.status == 401 or response_port.status == 403:
                    return False, {"msg": "authentication failed, please log in again", "type": "error"}
                data_port = await response_port.json()

                if "error" in data_port:
                    return False, {"msg": data_port["error"], "type": "error"}
                port = int(data_port["allocated_port"])

            async with session.get(
                    urljoin(DJANGO_SERVICE_CREATE_PROFILE_URL, "max_id/"), headers=headers
            ) as response:
                if response.status == 401 or response.status == 403:
                    return False, {"msg": "authentication failed, please log in again", "type": "error"}
                data = await response.json()
                print(data)
                user_id = int(data["max_id"]) + 1
    except Exception as e:
        print(e)
        return False, {"msg": "could not set a id", "type": "error"}


    profile_name = f"bot-profile-{user_id}"
    profile_path = os.path.join(PROFILES_DIR, profile_name)

    if os.path.exists(profile_path):
        shutil.rmtree(profile_path, ignore_errors=True)
    os.makedirs(profile_path, exist_ok=True)

    return True, [profile_path, profile_name, port]

async def stream(ws, page):
    delay = 1 / STREAM_FPS

    try:
        while True:
            img = await page.screenshot(
                type="jpeg",
                quality=JPEG_QUALITY,
                scale="css"
            )

            await ws.send(base64.b64encode(img).decode())

            await asyncio.sleep(delay)

    except websockets.ConnectionClosed:
        print("client disconnected")

    except asyncio.CancelledError:
        print("stream cancelled")

    except Exception as e:
        print("stream error:", e)


async def get_page(path, username, port, country_code=None):
    lock = FileLock(
        profile_lock_path(path),
        timeout=PROFILE_LOCK_TIMEOUT,
        thread_local=False,
    )
    await asyncio.to_thread(lock.acquire)

    device = get_device_config_for_profile(path, country_code)
    print(f"[Device] UA={device['user_agent'][:60]}... | Viewport={device['viewport']} | Platform={device['platform']}")

    try:
        browser = await playwright.firefox.launch_persistent_context(
            user_data_dir=path,
            headless=False,
            proxy={
                "server": f"http://{PROXY_HOST}:{port}",
                "username": username,
                "password": PROXY_PASSWORD
            },
            locale=device["locale"],
            user_agent=device["user_agent"],
            viewport=device["viewport"],
            extra_http_headers={
                "Accept-Language": device["accept_lang"]
            },
            firefox_user_prefs={
                "intl.accept_languages": device["accept_lang"].split(",")[0],
                "intl.locale.requested": device["locale"],
                "javascript.use_us_english_locale": False,
                "media.peerconnection.enabled": False,
                "geo.enabled": False,
                "toolkit.telemetry.enabled": False,
                "network.predictor.enabled": False,
                "browser.startup.homepage_override.mstone": "ignore",
                "startup.homepage_welcome_url": "about:blank",
                "startup.homepage_welcome_url.additional": "",
                "toolkit.telemetry.reportingpolicy.firstRun": False,
                "privacy.resistFingerprinting": False,  # چون خودمون اسپوف می‌کنیم
                "dom.webdriver.enabled": False,
            }
        )

        page = await browser.new_page()

        # Init script قوی‌تر برای تنوع fingerprint
        init_script = f"""
            Object.defineProperty(navigator, 'webdriver', {{ get: () => undefined }});
            Object.defineProperty(navigator, 'languages', {{ get: () => {json.dumps(device["accept_lang"].split(","))} }});
            Object.defineProperty(navigator, 'platform', {{ get: () => '{device["platform"]}' }});
            Object.defineProperty(navigator, 'hardwareConcurrency', {{ get: () => {device["hardware_concurrency"]} }});
            Object.defineProperty(navigator, 'deviceMemory', {{ get: () => {device["device_memory"]} }});
            Object.defineProperty(navigator, 'maxTouchPoints', {{ get: () => 0 }});

            // کمی نویز به canvas (خیلی ساده)
            const originalGetContext = HTMLCanvasElement.prototype.getContext;
            HTMLCanvasElement.prototype.getContext = function(type) {{
                const context = originalGetContext.apply(this, arguments);
                if (type === '2d') {{
                    const originalFillText = context.fillText;
                    context.fillText = function() {{
                        originalFillText.apply(this, arguments);
                    }};
                }}
                return context;
            }};
        """
        await page.add_init_script(init_script)

        await page.goto("about:blank")

    except Exception:
        release_lock(lock)
        raise

    return browser, page, lock


def release_lock(lock):
    if not lock:
        return
    try:
        lock.release()
    except Exception as e:
        print("lock release failed:", e)


async def clean_up(ws, stream_task, browser, page, lock=None):
    if stream_task:
        stream_task.cancel()
        try:
            await stream_task
        except asyncio.CancelledError:
            pass
        except Exception as e:
            print("stream task error during cleanup:", e)

    if page:
        try:
            await page.close()
        except:
            pass

    if browser:
        try:
            await browser.close()
        except:
            pass

    release_lock(lock)

    try:
        await ws.close()
    except:
        pass


async def add_handler(ws):
    browser = None
    page = None
    profile_lock = None
    stream_task = None
    platform = None
    port = None
    profile_path = None
    country = None
    proxy_username = None
    auth_headers = {}

    try:
        async for msg in ws:
            data = json.loads(msg)

            if data["type"] == "init":
                country = data["country"]
                proxy_username = f"b8f3fab422699782c1f0__cr.{country};sessttl.120"
                auth_headers = build_auth_headers(data.get("token"))
                if not auth_headers:
                    await ws.send(json.dumps({
                        "type": "error",
                        "msg": "missing auth token"
                    }))
                    continue
                created, data = await create_profile(auth_headers)
                if not created:
                    await ws.send(json.dumps(data))
                else:
                    profile_path, profile_name, port = data
                    browser, page, profile_lock = await get_page(
                        profile_path, proxy_username, port, country_code=country
                    )

                    await ws.send(json.dumps({
                        "type": "size",
                        "w": page.viewport_size["width"],
                        "h": page.viewport_size["height"]
                    }))

                    if stream_task is None:
                        stream_task = asyncio.create_task(stream(ws, page))

            elif data["type"] == "reset":
                if page:
                    try:
                        await page.close()
                    except:
                        pass
                    page = None

                if browser:
                    try:
                        await browser.close()
                    except:
                        pass
                    browser = None

                if stream_task:
                    stream_task.cancel()
                    try:
                        await stream_task
                    except asyncio.CancelledError:
                        pass
                    stream_task = None

                await ws.send(json.dumps({
                    "type": "reset_success",
                    "msg": "لطفا دوباره کشور را انتخاب کنید."
                }))

            elif data["type"] == "goto":
                print('page:', page)
                url = None
                platform = data["platform"].lower()
                if platform == "instagram":
                    url = "https://www.instagram.com/"
                if platform == 'twitter':
                    url = "https://x.com/"
                if url:
                    await page.goto(
                        url,
                        wait_until="domcontentloaded",
                        timeout=60000
                    )
                else:
                    await ws.send(json.dumps({
                        "type": "error",
                        "msg": "platform is not available"
                    }))

            elif data["type"] == "click":
                await page.mouse.click(data["x"], data["y"])

            elif data["type"] == "type":
                if data["text"] == "Backspace":
                    await page.keyboard.press("Backspace")
                elif data["text"] == "Enter":
                    await page.keyboard.press("Enter")
                else:
                    await page.keyboard.type(data["text"])

            elif data["type"] == "end":
                url = None
                username = data["username"]
                max_actions_per_hour = data["max_actions_per_hour"]
                session_data_str = None
                if platform == "instagram":
                    url = f"https://www.instagram.com/{username}/"
                if platform == 'twitter':
                    url = f"https://x.com/{username}/"
                if platform == "instagram" and page:
                    try:
                        print("Cookie extractions")
                        playwright_cookies = await page.context.cookies()

                        profile_name = os.path.basename(profile_path.strip(r"\/"))
                        session_file_name = f"session_{profile_name}.json"
                        temp_session_path = os.path.join(PROFILES_DIR, session_file_name)

                        is_session_created, failure_kind = await asyncio.to_thread(
                            generate_instagrapi_session,
                            playwright_cookies,
                            temp_session_path,
                            port,
                            proxy_username
                        )
                        if is_session_created and os.path.exists(temp_session_path):
                            with open(temp_session_path, 'r', encoding='utf-8') as f:
                                session_data_str = f.read()
                            os.remove(temp_session_path)
                        else:
                            print('Session creation failed or file does not exist.')
                            if failure_kind == "auth":
                                await ws.send(json.dumps({
                                    "type": "error",
                                    "msg": "Instagram rejected this login (challenge or "
                                           "incomplete sign-in). Please log in fully and "
                                           "complete any verification before finishing."
                                }))

                    except Exception as e:
                        print(f"error in Cookie extraction: {e}")
                if profile_path and platform and url and session_data_str:
                    max_actions_count = max_actions_per_hour["follow"] + max_actions_per_hour["comment"] + \
                                        max_actions_per_hour["like"] + max_actions_per_hour["direct"]
                    pause_time = math.ceil(24 / max_actions_count) if max_actions_count > 0 else 1
                    async with aiohttp.ClientSession() as session:
                        async with session.post(
                                DJANGO_SERVICE_CREATE_PROFILE_URL,
                                headers=auth_headers,
                                json={
                                    "firefox_profile_path": profile_path,
                                    "firefox_json_path": session_data_str,
                                    "social_media": platform,
                                    "max_actions_per_hour": max_actions_per_hour,
                                    "username": username,
                                    "pause_time": pause_time,
                                    "proxy": {
                                        "host": "proxy.ghostvps.com",
                                        "port": port,
                                        "username": f"b8f3fab422699782c1f0__cr.{country};sessttl.120",
                                        "password": "92964b4ea532a8e1",
                                    },
                                    "url": url
                                }
                        ) as response:
                            print("status:", response.status)
                            response_body = await response.text()
                            print("body:", response_body)
                            if 200 <= int(response.status) < 300:
                                await ws.send(json.dumps({"status": "profile is created"}))
                            else:
                                await ws.send(json.dumps({"status": "profile is not created"}))

                            try:
                                print(await response.json())
                            except:
                                print(await response.text())

                await clean_up(ws, stream_task, browser, page, profile_lock)
    except websockets.ConnectionClosed:
        print("client disconnected")

    finally:
        await clean_up(ws, stream_task, browser, page, profile_lock)


async def edit_handler(ws):
    browser = None
    page = None
    profile_lock = None
    stream_task = None
    profile_path = None
    platform = None
    port = None
    proxy_username = None
    bot_id = None
    auth_headers = {}
    session_data_str = None
    country = None

    try:
        async for msg in ws:
            data = json.loads(msg)

            if data["type"] == "init":
                bot_id = str(data["bot_id"])
                auth_headers = build_auth_headers(data.get("token"))
                if not auth_headers:
                    await ws.send(json.dumps({
                        "type": "error",
                        "msg": "missing auth token"
                    }))
                    continue
                async with aiohttp.ClientSession() as session:
                    async with session.get(
                            urljoin(DJANGO_SERVICE_CREATE_PROFILE_URL, bot_id), headers=auth_headers
                    ) as response:
                        bot_info = await response.json()
                        print('bot_info.proxy', bot_info['proxy'])
                profile_path = bot_info['firefox_profile_path']
                proxy_username = bot_info['proxy']['username']
                port = bot_info['proxy']['port']
                platform = bot_info['social_media'].lower()
                country = country_from_proxy_username(proxy_username)

                await ws.send(json.dumps({
                    "type": "size",
                    "w": 1280,
                    "h": 720
                }))

                try:
                    browser, page, profile_lock = await get_page(
                        profile_path, proxy_username, port, country_code=country
                    )
                    await ws.send(json.dumps({
                        "type": "size",
                        "w": page.viewport_size["width"],
                        "h": page.viewport_size["height"]
                    }))
                except LockTimeout:
                    await ws.send(json.dumps({
                        "type": "error",
                        "msg": "this profile is busy (being refreshed or open elsewhere), please try again shortly"
                    }))
                    break

                if stream_task is None:
                    stream_task = asyncio.create_task(stream(ws, page))

                url = None
                if platform == "instagram":
                    url = "https://www.instagram.com/"
                if url:
                    await page.goto(
                        url,
                        wait_until="domcontentloaded",
                        timeout=60000
                    )
                else:
                    await ws.send(json.dumps({
                        "type": "error",
                        "msg": "platform is not available"
                    }))

            elif data["type"] == "click":
                await page.mouse.click(data["x"], data["y"])

            elif data["type"] == "type":
                if data["text"] == "Backspace":
                    await page.keyboard.press("Backspace")
                elif data["text"] == "Enter":
                    await page.keyboard.press("Enter")
                else:
                    await page.keyboard.type(data["text"])

            elif data["type"] == "end":
                failure_kind = None
                if platform == "instagram" and page:
                    try:
                        print("Cookie extractions (Edit Mode)")
                        playwright_cookies = await page.context.cookies()

                        profile_name = os.path.basename(profile_path.strip(r"\/"))
                        session_file_name = f"session_{profile_name}.json"
                        temp_session_path = os.path.join(PROFILES_DIR, session_file_name)

                        is_session_created, failure_kind = await asyncio.to_thread(
                            generate_instagrapi_session,
                            playwright_cookies,
                            temp_session_path,
                            port,
                            proxy_username
                        )

                        if is_session_created and os.path.exists(temp_session_path):
                            with open(temp_session_path, 'r', encoding='utf-8') as f:
                                session_data_str = f.read()
                            os.remove(temp_session_path)
                            print('session_data_str successfully updated for edit.')
                    except Exception as e:
                        failure_kind = "error"
                        print(f"error in Cookie extraction (Edit Mode): {e}")

                if failure_kind == "auth" and bot_id:
                    await patch_profile(bot_id, auth_headers, {
                        "status": "profile-is-not-login",
                    })
                    await ws.send(json.dumps({
                        "type": "error",
                        "status": "profile is not logged in",
                        "msg": "Instagram rejected this login (challenge or expired session). "
                               "The profile has been marked as not logged in — please log in "
                               "again and complete any verification Instagram asks for."
                    }))
                    await clean_up(ws, stream_task, browser, page, profile_lock)
                    break

                if session_data_str and bot_id:
                    ok = await patch_profile(bot_id, auth_headers, {
                        "firefox_json_path": session_data_str,
                        "status": "requested",
                    })
                    if ok:
                        await ws.send(json.dumps({"status": "profile session updated successfully"}))
                    else:
                        await ws.send(json.dumps({"status": "profile session update failed"}))

                await clean_up(ws, stream_task, browser, page, profile_lock)
    except websockets.ConnectionClosed:
        print("client disconnected")

    finally:
        await clean_up(ws, stream_task, browser, page, profile_lock)


async def router(ws):
    path = ws.request.path

    if path == "/add":
        await add_handler(ws)

    elif path == "/edit":
        await edit_handler(ws)

    else:
        await ws.close()


async def main():
    global playwright
    playwright = await async_playwright().start()

    server = await websockets.serve(router, "0.0.0.0", 9000)

    print("Remote Playwright Browser running with Device Diversity...")

    await server.wait_closed()


asyncio.run(main())