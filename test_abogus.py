#!/usr/bin/env python3
"""Test whether a_bogus.js compiles and runs correctly via execjs."""
import execjs
import os
import sys

ab_path = '/root/StreamCheck/Douyin_Spider/static/a_bogus.js'
print('File exists:', os.path.exists(ab_path))
print('File size:', os.path.getsize(ab_path))

# Check various node_modules locations
for label, p in [
    ('utils/static/node_modules', '/root/StreamCheck/Douyin_Spider/utils/static/node_modules'),
    ('static/node_modules', '/root/StreamCheck/Douyin_Spider/static/node_modules'),
    ('utils/node_modules', '/root/StreamCheck/Douyin_Spider/utils/node_modules'),
    ('Douyin_Spider/node_modules', '/root/StreamCheck/Douyin_Spider/node_modules'),
    ('StreamCheck/node_modules', '/root/StreamCheck/node_modules'),
]:
    print(f'{label} exists: {os.path.exists(p)}')

# Try compiling the new a_bogus.js without any cwd
print('\n--- Try 1: compile without cwd ---')
try:
    ctx = execjs.compile(open(ab_path).read())
    print('Compilation SUCCESS')
    result = ctx.call('get_ab', 'test=1&aid=6383', 'Mozilla/5.0')
    print('Result:', result[:80], '...')
except Exception as e:
    print(f'FAILED: {type(e).__name__}: {str(e)[:300]}')

# Try with node_modules cwd
nm_path = '/root/StreamCheck/Douyin_Spider/static/node_modules'
if os.path.exists(nm_path):
    print('\n--- Try 2: compile with node_modules cwd ---')
    try:
        ctx = execjs.compile(open(ab_path).read(), cwd=nm_path)
        print('Compilation SUCCESS')
        result = ctx.call('get_ab', 'test=1&aid=6383', 'Mozilla/5.0')
        print('Result:', result[:80], '...')
    except Exception as e:
        print(f'FAILED: {type(e).__name__}: {str(e)[:300]}')
