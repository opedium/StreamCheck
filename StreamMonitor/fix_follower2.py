import json, os, sys, re, requests

os.chdir('/root/StreamCheck/StreamMonitor')

with open('live_stats.json') as f:
    stats = json.load(f)

live_id = stats['live_id']

cookie = ''
with open('.env') as f:
    for line in f:
        if line.startswith('DY_LIVE_COOKIES='):
            cookie = line.split('=', 1)[1].strip().strip("'").strip('"')
            break

headers = {
    'Cookie': cookie,
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'zh-CN,zh;q=0.9',
}

# Method 1: Get room page to extract sec_uid from RENDER_DATA
print('=== Method 1: Room page ===')
r = requests.get(f'https://live.douyin.com/{live_id}', headers=headers, timeout=20, allow_redirects=True)
print(f'Status: {r.status_code}, Final URL: {r.url[:120]}')

# Check for redirect to login
if 'login' in r.url.lower():
    print('REDIRECTED TO LOGIN - COOKIE IS EXPIRED')
    sys.exit(1)

render_match = re.search(r'window\.__RENDER_DATA__\s*=\s*({.*?});\s*</script>', r.text, re.DOTALL)
if render_match:
    try:
        render = json.loads(render_match.group(1))
        print('Found RENDER_DATA')
        # Navigate to find user data
        room_info = render.get('app', {}).get('initialState', {}).get('roomStore', {}).get('roomInfo', {})
        room = room_info.get('room', {})
        anchor = room_info.get('anchor', {})
        sec_uid = anchor.get('sec_uid', '') or room.get('sec_uid', '')
        print(f'sec_uid: {sec_uid}')
        print(f'anchor keys: {list(anchor.keys())[:10]}')
        print(f'room keys: {list(room.keys())[:10]}')
        
        if sec_uid:
            print(f'\n=== Method 2: User profile with sec_uid={sec_uid} ===')
            r2 = requests.get(f'https://www.douyin.com/user/{sec_uid}', headers=headers, timeout=20, allow_redirects=True)
            print(f'Status: {r2.status_code}')
            if 'login' in r2.url.lower():
                print('User profile redirected to login too - cookie issue')
            else:
                render2 = re.search(r'window\.__RENDER_DATA__\s*=\s*({.*?});\s*</script>', r2.text, re.DOTALL)
                if render2:
                    data2 = json.loads(render2.group(1))
                    print('User RENDER_DATA keys:', list(data2.keys())[:10])
                    # Try to find follower count in user data
                    user_data = data2.get('app', {}).get('initialState', {}).get('userStore', {}) or data2.get('user', {})
                    print('userStore keys:', list(user_data.keys())[:10] if isinstance(user_data, dict) else 'not dict')
                    # Search for follower_count in the whole render data
                    def find_key(obj, target, path=''):
                        if isinstance(obj, dict):
                            for k, v in obj.items():
                                if k == target:
                                    print(f'Found {target}={v} at path {path}.{k}')
                                    return v
                                result = find_key(v, target, f'{path}.{k}')
                                if result is not None:
                                    return result
                        return None
                    fc = find_key(data2, 'follower_count')
                    if fc:
                        fc_int = int(fc) if not isinstance(fc, str) else int(float(fc.replace('万','')) * 10000 if '万' in fc else float(fc))
                        stats['follower_after'] = fc_int
                        stats['new_follows'] = fc_int - stats['follower_before']
                        import datetime
                        stats['last_update'] = datetime.datetime.now().isoformat()
                        with open('live_stats.json', 'w') as f:
                            json.dump(stats, f, ensure_ascii=False, indent=2)
                        print(f'\nSUCCESS! follower_after = {fc_int:,}, new_follows = {stats["new_follows"]}')
    except json.JSONDecodeError as e:
        print(f'JSON parse error: {e}')
else:
    print('No RENDER_DATA found in page')
    # Show snippet of page around login
    if 'passport' in r.text[:5000]:
        print('Page appears to be login/redirect page')
    else:
        print('Page snippet:', r.text[2000:2500])

