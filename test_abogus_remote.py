import subprocess, json, sys

ab_path = '/root/StreamCheck/Douyin_Spider/static/a_bogus.js'
dpf = 'app_name=live_stream&webcast_sdk_version=1.0.15&aid=6383&device_platform=web'
ua = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36'

js_code = (
    "eval(require('fs').readFileSync(" + json.dumps(ab_path) + ",'utf-8'));"
    "var r=get_ab(" + json.dumps(dpf) + "," + json.dumps(ua) + ");"
    "console.log(JSON.stringify({a_bogus:r}));"
)

proc = subprocess.run(['node', '-e', js_code], capture_output=True, text=True, timeout=15)
print('returncode:', proc.returncode)
print('stdout:', proc.stdout[:300])
print('stderr:', proc.stderr[:300])

if proc.returncode == 0:
    for line in proc.stdout.strip().split('\n'):
        try:
            obj = json.loads(line)
            if 'a_bogus' in obj:
                print('SUCCESS! a_bogus length:', len(obj['a_bogus']))
                print('a_bogus:', obj['a_bogus'][:80], '...')
        except:
            pass
