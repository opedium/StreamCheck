import hashlib
import re
import sys
import time
import json
import random
import base64
import urllib
from os import path

import requests
requests.packages.urllib3.disable_warnings()
import subprocess
from functools import partial

subprocess.Popen = partial(subprocess.Popen, encoding="utf-8")
import execjs

if getattr(sys, 'frozen', None):
    basedir = sys._MEIPASS
else:
    basedir = path.dirname(__file__)


try:
    node_modules = path.join(basedir, 'static', 'node_modules')
    login_path = path.join(basedir, 'static', 'login.js')
    login_js = execjs.compile(open(login_path, 'r', encoding='utf-8').read(), cwd=node_modules)
except:
    node_modules = path.join(basedir, '..', 'static', 'node_modules')
    login_path = path.join(basedir, '..', 'static', 'login.js')
    login_js = execjs.compile(open(login_path, 'r', encoding='utf-8').read(), cwd=node_modules)


def generateSecretPhoneNum(phone):
    sign = login_js.call('generateSecretPhoneNum', phone)
    return sign
def generateSecretCode(phone, code):
    sign = login_js.call('generateSecretCode', phone, code)
    return sign

try:
    node_modules = path.join(basedir, 'node_modules')
    dy_path = path.join(basedir, 'static', 'dy_ab.js')
    dy_js = execjs.compile(open(dy_path, 'r', encoding='utf-8').read(), cwd=node_modules)
    sign_path = path.join(basedir, 'static', 'dy_live_sign.js')
    sign_js = execjs.compile(open(sign_path, 'r', encoding='utf-8').read(), cwd=node_modules)
except:
    node_modules = path.join(basedir, '..', 'node_modules')
    dy_path = path.join(basedir, '..', 'static', 'dy_ab.js')
    dy_js = execjs.compile(open(dy_path, 'r', encoding='utf-8').read(), cwd=node_modules)
    sign_path = path.join(basedir, '..', 'static', 'dy_live_sign.js')
    sign_js = execjs.compile(open(sign_path, 'r', encoding='utf-8').read(), cwd=node_modules)

# Also try to compile the cleaner a_bogus.js (389 lines, no self-destruct)
# from DouyinLiveWebFetcher, used as fallback when dy_ab.js crashes.
# Note: compiled WITHOUT cwd since a_bogus.js is self-contained (no node_modules required).
_abogus_js_ctx = None
try:
    ab_path = path.join(basedir, 'static', 'a_bogus.js')
    if not path.exists(ab_path):
        ab_path = path.join(basedir, '..', 'static', 'a_bogus.js')
    if path.exists(ab_path):
        _abogus_js_ctx = execjs.compile(open(ab_path, 'r', encoding='utf-8').read())
except Exception:
    _abogus_js_ctx = None


def trans_cookies(cookies_str):
    cookies = {
        # "douyin.com": "",
    }
    for i in cookies_str.split("; "):
        try:
            cookies[i.split('=')[0]] = '='.join(i.split('=')[1:])
        except:
            continue
    # cookies = {i.split('=')[0]: '='.join(i.split('=')[1:]) for i in cookies_str.split('; ')}
    return cookies


# 私信传obj, 其他的拼接
def generate_req_sign(e, priK):
    sign = dy_js.call('get_req_sign', e, priK)
    return sign


# query, data都是拼接字符串
def generate_a_bogus(query, data=""):
    """
    Generate a_bogus by calling Node.js directly via subprocess.
    
    execjs can NOT be used because the old dy_ab.js calls process.exit(-6) after
    computing signatures, which kills execjs's shared Node.js subprocess and poisons
    ALL subsequent execjs calls (even fresh compiles).
    
    Using subprocess ensures each call gets a fresh, isolated Node.js process.
    
    JS signature: get_ab(dpf, ua) where:
      - dpf = URL-encoded query string (query + data combined)
      - ua  = User-Agent string
    """
    # Combine query and data into one dpf string for the new JS interface
    if data:
        dpf = query + '&' + data if query else data
    else:
        dpf = query
    
    # Hardcode UA to avoid circular import (builder.header imports from dy_util).
    # IMPORTANT: This must match HeaderBuilder.ua in builder/header.py.
    # The a_bogus.js signature algorithm incorporates the UA string, so a mismatch
    # between the signed UA and the HTTP User-Agent header causes request rejection.
    ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    
    # Find a_bogus.js path
    ab_path = path.join(basedir, 'static', 'a_bogus.js')
    if not path.exists(ab_path):
        ab_path = path.join(basedir, '..', 'static', 'a_bogus.js')
    
    if not path.exists(ab_path):
        raise RuntimeError(f"a_bogus.js not found at {ab_path}")
    
    # Build Node.js script: eval the file (defines get_ab globally), then call it
    # json.dumps safely escapes Python strings for use in JS string literals
    js_code = (
        "eval(require('fs').readFileSync(" + json.dumps(ab_path) + ",'utf-8'));"
        "var r=get_ab(" + json.dumps(dpf) + "," + json.dumps(ua) + ");"
        "console.log(JSON.stringify({a_bogus:r}));"
    )
    
    try:
        proc = subprocess.run(
            ['node', '-e', js_code],
            capture_output=True, text=True, timeout=15, encoding='utf-8'
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError("generate_a_bogus: Node.js subprocess timed out (15s)")
    
    # Parse JSON output from stdout FIRST — Node.js may exit with code -6 even when stdout
    # contains a valid a_bogus (e.g., Node.js v24 behavior, or JS engine internal exit).
    # We should NOT reject based on returncode alone if we already have a valid result.
    for line in proc.stdout.strip().split('\n'):
        line = line.strip()
        try:
            obj = json.loads(line)
            if 'a_bogus' in obj:
                return obj['a_bogus']
        except json.JSONDecodeError:
            continue
    
    # Only raise if BOTH returncode != 0 AND stdout parsing failed to find a_bogus
    if proc.returncode != 0:
        raise RuntimeError(
            f"generate_a_bogus: Node.js exited with code {proc.returncode}: "
            f"stderr={proc.stderr[:300]} stdout={proc.stdout[:300]}"
        )
    
    raise RuntimeError(f"generate_a_bogus: could not parse result: stdout={proc.stdout[:300]}")


def generate_signature(room_id, user_unique_id):
    """
    Generate X-Bogus signature for Douyin live WebSocket connection.
    
    Uses subprocess instead of execjs because dy_live_sign.js calls process.exit(-6)
    internally after computing signatures, which kills execjs's shared Node.js subprocess.
    
    Using subprocess ensures each call gets a fresh, isolated Node.js process.
    
    Returns the X-Bogus string.
    """
    raw_string = f"live_id=1,aid=6383,version_code=180800,webcast_sdk_version=1.0.15,room_id={room_id},sub_room_id=,sub_channel_id=,did_rule=3,user_unique_id={user_unique_id},device_platform=web,device_type=,ac=,identity=audience"
    x_ms_stub = hashlib.md5(raw_string.encode("utf-8")).hexdigest()
    
    # Find dy_live_sign.js path
    sign_path = path.join(basedir, 'static', 'dy_live_sign.js')
    if not path.exists(sign_path):
        sign_path = path.join(basedir, '..', 'static', 'dy_live_sign.js')
    
    if not path.exists(sign_path):
        raise RuntimeError(f"dy_live_sign.js not found at {sign_path}")
    
    # Build Node.js script:
    # 1. Override process.exit to prevent the self-destruct (dy_live_sign.js calls process.exit(-6))
    # 2. Save original console.log, then silence it during eval (line 7067 test call prints garbage)
    # 3. Eval the file (defines window.byted_acrawler and get_signature globally)
    # 4. Restore console.log and call get_signature(x_ms_stub), print JSON result
    js_code = (
        "process.exit=function(){};"
        "var _cl=console.log;console.log=function(){};"
        "eval(require('fs').readFileSync(" + json.dumps(sign_path) + ",'utf-8'));"
        "console.log=_cl;"
        "var r=get_signature(" + json.dumps(x_ms_stub) + ");"
        "console.log(JSON.stringify(r));"
    )
    
    try:
        proc = subprocess.run(
            ['node', '-e', js_code],
            capture_output=True, text=True, timeout=15, encoding='utf-8'
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError("generate_signature: Node.js subprocess timed out (15s)")
    
    # Parse JSON output from stdout FIRST — Node.js may exit with code -6 even when stdout
    # contains a valid result. Same pattern as generate_a_bogus().
    # Two possible formats observed:
    #   Format A: {"X-Bogus":"..."} (direct object)
    #   Format B: ["ok", {"X-Bogus":"..."}] (array returned by get_signature)
    for line in proc.stdout.strip().split('\n'):
        line = line.strip()
        try:
            obj = json.loads(line)
            # Format A: direct {"X-Bogus": "..."}
            if isinstance(obj, dict) and 'X-Bogus' in obj:
                return obj['X-Bogus']
            # Format B: ["ok", {"X-Bogus": "..."}]
            if isinstance(obj, list) and len(obj) >= 2:
                inner = obj[1]
                if isinstance(inner, dict) and 'X-Bogus' in inner:
                    return inner['X-Bogus']
        except json.JSONDecodeError:
            continue
    
    # Only raise if BOTH returncode != 0 AND stdout parsing failed
    if proc.returncode != 0:
        raise RuntimeError(
            f"generate_signature: Node.js exited with code {proc.returncode}: "
            f"stderr={proc.stderr[:300]} stdout={proc.stdout[:300]}"
        )
    
    raise RuntimeError(f"generate_signature: could not parse result: stdout={proc.stdout[:300]}")


# 传递私钥
def generate_ree_key(prik):
    ree_key = dy_js.call('get_ree_key', prik)
    return ree_key


# 传递query, ticket, ts_sign, priK
def generate_bd_ticket_client_data(api, ticket, ts_sign, priK):
    timestamp = int(time.time())
    res_sign = f"ticket={ticket}&path={api}&timestamp={timestamp}"
    p = {
        'ts_sign': ts_sign,
        'req_content': 'ticket,path,timestamp',
        'req_sign': generate_req_sign(res_sign, priK),
        'timestamp': timestamp,
    }
    p = json.dumps(p, ensure_ascii=False, separators=(',', ':'))
    return base64.urlsafe_b64encode(p.encode('utf-8')).decode('utf-8')


def generate_msToken(randomlength=107):
    random_str = ''
    base_str = 'ABCDEFGHIGKLMNOPQRSTUVWXYZabcdefghigklmnopqrstuvwxyz0123456789='
    length = len(base_str) - 1
    for _ in range(randomlength):
        random_str += base_str[random.randint(0, length)]
    return random_str


def generate_ttwid():
    url = f"https://www.douyin.com/discover?modal_id=7376449060384935209"
    ttwid = None
    try:
        headers = {
            'user-agent': "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"
        }
        response = requests.get(url, headers=headers, verify=False)
        cookies_dict = response.cookies.get_dict()
        ttwid = cookies_dict.get('ttwid')
        return ttwid
    except Exception as e:
        return ttwid


def generate_fake_webid(random_length=19):
    random_str = ''
    base_str = '0123456789'
    length = len(base_str) - 1
    for _ in range(random_length):
        random_str += base_str[random.randint(0, length)]
    return random_str


def generate_webid(auth=None, url=""):
    if url == "":
        url = f"https://www.douyin.com/discover?modal_id=7376449060384935209"
    try:
        from builder.header import HeaderBuilder, HeaderType
        headers = HeaderBuilder().build(HeaderType.DOC)
        headers.set_header('cookie', auth.cookie_str if auth else "")
        headers.set_header("upgrade-insecure-requests", "1")
        response = requests.get(url, headers=headers.get(), verify=False)
        res_text = response.text
        user_unique_id = re.findall(r'\\"user_unique_id\\":\\"(.*?)\\"', res_text)[0]
        webid = user_unique_id
        return webid
    except Exception as e:
        # print("===================")
        # print(url)
        # print(e)
        # print("===================")
        return generate_fake_webid()


def ws_accept_key(ws_key):
    """calc the Sec-WebSocket-Accept key by Sec-WebSocket-key
    come from client, the return value used for handshake

    :ws_key: Sec-WebSocket-Key come from client
    :returns: Sec-WebSocket-Accept

    """
    import hashlib
    import base64
    try:
        magic = '258EAFA5-E914-47DA-95CA-C5AB0DC85B11'
        sha1 = hashlib.sha1()
        sha1.update(ws_key + magic)
        return base64.b64encode(sha1.digest())
    except Exception as e:
        return None


def generate_csrf_token(cookies_str):
    csrf_token_1, csrf_token_2 = None, None
    try:
        headers = {
            'accept': '*/*',
            'accept-language': 'zh-CN,zh;q=0.9,en;q=0.8,en-GB;q=0.7,en-US;q=0.6',
            'cache-control': 'no-cache',
            'cookie': cookies_str,
            'pragma': 'no-cache',
            'priority': 'u=1, i',
            'referer': 'https://www.douyin.com/?recommend=1',
            'sec-ch-ua': '"Microsoft Edge";v="125", "Chromium";v="125", "Not.A/Brand";v="24"',
            'sec-ch-ua-mobile': '?0',
            'sec-ch-ua-platform': '"Windows"',
            'sec-fetch-dest': 'empty',
            'sec-fetch-mode': 'cors',
            'sec-fetch-site': 'same-origin',
            'user-agent': "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
            'x-secsdk-csrf-request': '1',
            'x-secsdk-csrf-version': '1.2.22',
        }
        response = requests.head('https://www.douyin.com/service/2/abtest_config/', headers=headers, verify=False)
        return response.headers['X-Ware-Csrf-Token'].split(',')[1], response.headers['X-Ware-Csrf-Token'].split(',')[4]
    except Exception as e:
        return csrf_token_1, csrf_token_2


def generate_millisecond():
    millis = int(round(time.time() * 1000))
    return millis


def splice_url(params):
    splice_url_str = ''
    for key, value in params.items():
        if value is None:
            value = ''
        splice_url_str += key + '=' + urllib.parse.quote(str(value)) + '&'
    return splice_url_str[:-1]
