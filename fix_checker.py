#!/usr/bin/env python3
"""Apply the cookie-reload fix to checker.py."""

import re

with open('/root/StreamCheck/check/checker.py', 'r') as f:
    content = f.read()

# Change 1: Add reload_cookie method to WeiboPoster class
old_1 = '    def _extract_xsrf(self) -> str:\n'
old_1 += '        """Extract XSRF token from the Web cookie string."""\n'

new_1 = '    def reload_cookie(self, web_cookie: str):\n'
new_1 += '        """Replace the cookie this poster uses with a fresh one."""\n'
new_1 += '        self.web_cookie = web_cookie\n'
new_1 += '        logger.info("WeiboPoster cookie reloaded")\n'
new_1 += '\n'
new_1 += old_1

assert old_1 in content, 'Change 1 pattern not found!'
content = content.replace(old_1, new_1, 1)
print('Change 1 applied OK')

# Change 2: In the main loop, when cookie validity check fails, reload from file
old_2_lines = [
    '                    # Re-check cookie validity before posting',
    '                    if not poster.check_validity():',
    '                        logger.error(',
    '                            f"Weibo cookie expired — skipping post for {bvid}, "',
    '                            f"will retry next cycle"',
    '                        )',
    '                        continue',
]
old_2 = '\n'.join(old_2_lines)

# Readability: build new_2 piece by piece
new_2_lines = [
    '                    # Re-check cookie validity before posting',
    '                    if not poster.check_validity():',
    '                        # Try to reload the cookie — the refresher may have updated it',
    '                        logger.warning(',
    '                            f"Weibo cookie expired — attempting reload from file..."',
    '                        )',
    '                        fresh_cookie = _load_cookie_from_json(',
    '                            _WEIBO_COOKIES_FILE, "WEIBO_COOKIE", "Weibo cookie (reload)"',
    '                        )',
    '                        if fresh_cookie and fresh_cookie != poster.web_cookie:',
    '                            poster.reload_cookie(fresh_cookie)',
    '                            if poster.check_validity():',
    '                                logger.info("Reloaded cookie is valid — proceeding with post")',
    '                            else:',
    '                                logger.error(',
    '                                    f"Reloaded Weibo cookie still invalid — "',
    '                                    f"skipping post for {bvid}, will retry next cycle"',
    '                                )',
    '                                continue',
    '                        else:',
    '                            logger.error(',
    '                                f"Weibo cookie expired — skipping post for {bvid}, "',
    '                                f"will retry next cycle"',
    '                            )',
    '                            continue',
]
new_2 = '\n'.join(new_2_lines)

assert old_2 in content, 'Change 2 pattern not found!'
content = content.replace(old_2, new_2, 1)
print('Change 2 applied OK')

with open('/root/StreamCheck/check/checker.py', 'w') as f:
    f.write(content)

print('Both changes applied successfully')
