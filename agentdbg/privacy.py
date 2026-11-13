"""Prepare captured values before any persistence."""

import json
import os
import re

PAYLOAD_LIMIT = 1024 * 1024
RUN_LIMIT = 100 * PAYLOAD_LIMIT
REDACTED = "[redacted]"


def environment_secrets(environ=None):
    environ = os.environ if environ is None else environ
    return frozenset(value for key, value in environ.items() if len(value) >= 4 and
                     re.search(r"TOKEN|SECRET|PASSWORD|API_KEY|CREDENTIAL|AUTH", key, re.I))


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sanitize(value, fields, secrets):
    changed = False

    def visit(item):
        nonlocal changed
        if isinstance(item, dict):
            result = {}
            for key, val in item.items():
                safe_key = visit(key) if isinstance(key, str) else key
                if isinstance(safe_key, str) and (safe_key.lower() in fields or safe_key.lower() in ("authorization", "proxy-authorization", "cookie", "set-cookie", "x-api-key")):
                    result[safe_key] = REDACTED
                    changed = True
                else:
                    result[safe_key] = visit(val)
            return result
        if isinstance(item, (list, tuple)):
            return [visit(val) for val in item]
        if isinstance(item, str):
            for secret in secrets:
                if secret and secret in item:
                    item = item.replace(secret, REDACTED)
                    changed = True
        return item

    return visit(value), changed


def prepare(value, fields, secrets, limit=PAYLOAD_LIMIT):
    value, redacted = sanitize(value, fields, secrets)
    data = canonical(value)
    if len(data) > limit:
        return {"truncated": True, "original_bytes": len(data)}, True
    return value, redacted
