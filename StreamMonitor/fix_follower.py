import json, os, sys, re

os.chdir('/root/StreamCheck/StreamMonitor')

# Load live_stats
with open('live_stats.json') as f:
    stats = json.load(f)

live_id = stats['live_id']

# Load cookie from .env
cookie = ''
with open('.env') as f:
    for line in f:
        if line.startswith('DY_LIVE_COOKIES='):
            cookie = line.split('=', 1)[1].strip().strip("'").strip('"')
            break

if not cookie:
    print('ERROR: No DY_LIVE_COOKIES found')
    sys.exit(1)

print(f'Cookie found, length={len(cookie)}')
print(f'Live ID: {live_id}')
print(f'Current follower_after: {stats["follower_after"]:,}')
print(f'Current follower_before: {stats["follower_before"]:,}')

# Call Douyin API
import requests
headers = {
    'Cookie': cookie,
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Referer': f'https://live.douyin.com/{live_id}',
}

try:
    # Get room info
    r = requests.get(f'https://live.douyin.com/webcast/room/web/enter/?aid=6383&live_id=1&room_id=&web_rid={live_id}',
                     headers=headers, timeout=15)
    data = r.json()
    print(f'API response status: {r.status_code}')
    print(f'API ok: {data.get("status_code")}')
    
    user_info = data.get('data', {}).get('user', {}) or data.get('user', {})
    room_data = data.get('data', {}).get('room', {}) or data.get('room', {})
    
    # Try to get follower count
    fc = user_info.get('follower_count', 0)
    if fc:
        if isinstance(fc, str):
            fc = float(fc.replace('万', '')) * 10000 if '万' in fc else float(fc)
        fc_int = int(fc)
        print(f'Follower count from API: {fc_int:,}')
        
        stats['follower_after'] = fc_int
        stats['new_follows'] = fc_int - stats['follower_before']
        stats['last_update'] = __import__('datetime').datetime.now().isoformat()
        
        with open('live_stats.json', 'w') as f:
            json.dump(stats, f, ensure_ascii=False, indent=2)
        
        print(f'Updated! new_follows = {stats["new_follows"]}')
    else:
        print(f'No follower_count in response. user_info keys: {list(user_info.keys())[:10]}')
        # Try sec_uid approach
        sec_uid = room_data.get('sec_uid', '') or data.get('data', {}).get('sec_uid', '')
        if sec_uid:
            print(f'Trying user profile with sec_uid={sec_uid}')
            r2 = requests.get(f'https://www.douyin.com/user/{sec_uid}', headers=headers, timeout=15)
            # parse renderData from HTML
            match = re.search(r'window\.__RENDER_DATA__\s*=\s*({.*?});\s*</script>', r2.text, re.DOTALL)
            if match:
                import json as jmod
                render = jmod.loads(match.group(1))
                print('Got render data')
            else:
                print('No render data found, status:', r2.status_code)
                print('URL after redirect:', r2.url[:100])
        else:
            print('No sec_uid either')
    
except Exception as e:
    print(f'ERROR: {e}')
