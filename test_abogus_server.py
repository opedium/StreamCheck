"""Test a_bogus.js on the server with realistic params."""
import execjs
from os import path

basedir = path.dirname(path.abspath(__file__))
ab_path = path.join(basedir, 'Douyin_Spider', 'static', 'a_bogus.js')
if not path.exists(ab_path):
    ab_path = path.join(basedir, '..', 'static', 'a_bogus.js')
if not path.exists(ab_path):
    ab_path = '/root/StreamCheck/Douyin_Spider/static/a_bogus.js'

print('ab_path:', ab_path)
print('exists:', path.exists(ab_path))

if path.exists(ab_path):
    ctx = execjs.compile(open(ab_path).read())
    print('Compiled OK')
    
    # Realistic params like what get_webcast_detail sends
    query = 'app_name=live_stream&webid=...&xyz=123'
    data = 'aid=6383&device_platform=web'
    dpf = query + '&' + data if data else query
    ua = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36'
    
    try:
        result = ctx.call('get_ab', dpf, ua)
        print('Result:', result[:80])
        print('Full length:', len(result))
    except Exception as e:
        print('get_ab failed:', type(e).__name__, str(e)[:500])
else:
    print('a_bogus.js NOT FOUND')
