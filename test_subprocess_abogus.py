"""Test subprocess-based a_bogus generation."""
import subprocess
import json
import sys

ab_path = "/root/StreamCheck/Douyin_Spider/static/a_bogus.js"
dpf = "app_name=live_stream&webid=test&aid=6383"
ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"

# Build JS code using proper escaping
js_code = (
    "eval(require('fs').readFileSync('" + ab_path + "','utf-8'));"
    "var r=get_ab('" + dpf + "','" + ua + "');"
    "console.log(JSON.stringify({a_bogus:r}));"
)

print("JS code length:", len(js_code))
print("Running node...")

try:
    proc = subprocess.run(
        ['node', '-e', js_code],
        capture_output=True, text=True, timeout=15
    )
    print("Return code:", proc.returncode)
    print("Stdout:", proc.stdout[:300])
    if proc.stderr:
        print("Stderr:", proc.stderr[:300])
    
    if proc.returncode == 0:
        for line in proc.stdout.strip().split('\n'):
            line = line.strip()
            try:
                obj = json.loads(line)
                if 'a_bogus' in obj:
                    print("SUCCESS! a_bogus:", obj['a_bogus'][:80])
                    sys.exit(0)
            except json.JSONDecodeError:
                continue
        print("Could not parse output")
        sys.exit(1)
    else:
        print("Node.js failed")
        sys.exit(1)
except Exception as e:
    print("Error:", e)
    sys.exit(1)
