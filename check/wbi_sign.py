#!/usr/bin/env python3
# coding=utf-8
"""
WBI signing module for Bilibili API requests.
Handles daily key rotation, mixin key generation, and request signing.
Reference: https://github.com/realysy/bili-apis/blob/master/docs/misc/sign/wbi.md
"""

import json
import os
import time
from functools import reduce
from hashlib import md5
from urllib.parse import urlencode

import requests
from loguru import logger

# Fixed 64-element permutation table from Bilibili's frontend
MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52
]

CACHE_FILE = os.path.join(os.path.dirname(__file__), 'wbi_cache.json')
CACHE_TTL_SECONDS = 86400  # 24 hours


def _load_cache() -> dict:
    """Load the WBI key cache from disk."""
    try:
        if os.path.exists(CACHE_FILE):
            with open(CACHE_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def _save_cache(data: dict):
    """Save the WBI key cache to disk."""
    try:
        with open(CACHE_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f)
    except Exception as e:
        logger.warning(f"Failed to save WBI key cache: {e}")


def get_wbi_keys() -> tuple:
    """
    Fetch img_key and sub_key from Bilibili nav endpoint.
    Caches keys to wbi_cache.json with a 24-hour TTL.
    Returns (img_key, sub_key) tuple.
    """
    cache = _load_cache()
    cached_time = cache.get('ts', 0)
    if time.time() - cached_time < CACHE_TTL_SECONDS:
        img_key = cache.get('img_key', '')
        sub_key = cache.get('sub_key', '')
        if img_key and sub_key:
            logger.debug("Using cached WBI keys")
            return img_key, sub_key

    logger.info("Fetching fresh WBI keys from Bilibili nav endpoint")
    try:
        resp = requests.get(
            'https://api.bilibili.com/x/web-interface/nav',
            headers={
                'User-Agent': (
                    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                    'AppleWebKit/537.36 (KHTML, like Gecko) '
                    'Chrome/138.0.0.0 Safari/537.36'
                ),
                'Referer': 'https://www.bilibili.com/',
            },
            timeout=15
        )
        resp.raise_for_status()
        data = resp.json()
        img_url = data['data']['wbi_img']['img_url']
        sub_url = data['data']['wbi_img']['sub_url']
        img_key = img_url.rsplit('/', 1)[1].split('.')[0]
        sub_key = sub_url.rsplit('/', 1)[1].split('.')[0]

        _save_cache({
            'img_key': img_key,
            'sub_key': sub_key,
            'ts': int(time.time())
        })
        logger.info(f"WBI keys cached (img_key={img_key[:8]}...)")
        return img_key, sub_key
    except Exception as e:
        logger.error(f"Failed to fetch WBI keys: [{type(e).__name__}] {e}")
        # Fallback to cached keys even if expired
        if cache.get('img_key') and cache.get('sub_key'):
            logger.warning("Using expired cached WBI keys as fallback")
            return cache['img_key'], cache['sub_key']
        raise


def get_mixin_key(orig: str) -> str:
    """
    Generate mixin key from combined img_key + sub_key.
    Applies the fixed permutation table and returns first 32 characters.
    """
    return reduce(lambda s, i: s + orig[i], MIXIN_KEY_ENC_TAB, '')[:32]


def sign_params(params: dict, img_key: str, sub_key: str) -> dict:
    """
    Sign request parameters with WBI signing.

    1. Add wts (current unix timestamp)
    2. Sort params alphabetically by key
    3. Filter characters !'()* from all values
    4. URL-encode the sorted params
    5. Append mixin_key and compute MD5 -> w_rid
    6. Return original params dict with w_rid and wts added

    Args:
        params: Original query parameter dict (e.g. {'mid': '123', 'ps': '1'})
        img_key: WBI image key
        sub_key: WBI sub key

    Returns:
        Dict with w_rid and wts added
    """
    mixin_key = get_mixin_key(img_key + sub_key)
    curr_time = int(time.time())

    # Work on a copy
    to_sign = dict(params)
    to_sign['wts'] = curr_time

    # Sort alphabetically by key
    to_sign = dict(sorted(to_sign.items()))

    # Filter characters !'()* from all values
    to_sign = {
        k: ''.join(filter(lambda c: c not in "!'()*", str(v)))
        for k, v in to_sign.items()
    }

    # URL-encode
    query = urlencode(to_sign).replace('+', '%20')

    # MD5 with mixin_key appended
    wbi_sign = md5((query + mixin_key).encode()).hexdigest()

    # Return original params with signing fields added
    result = dict(params)
    result['w_rid'] = wbi_sign
    result['wts'] = curr_time
    return result
