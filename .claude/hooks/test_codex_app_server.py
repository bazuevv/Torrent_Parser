#!/usr/bin/env python3
"""Unit tests for the Codex app-server transport foundation."""

import json
import sys
import unittest

from codex_app_server import (
    CodexAppServerClient,
    CodexAppServerError,
    CodexRpcError,
    _safe_probe,
)


FAKE_SERVER = r'''
import json
import sys
import time

for raw in sys.stdin:
    message = json.loads(raw)
    method = message.get("method")
    request_id = message.get("id")
    if request_id is None:
        continue
    if method == "initialize":
        result = {"userAgent": "fake/1"}
    elif method == "account/read":
        result = {
            "account": {
                "type": "chatgpt",
                "email": "person@example.test",
                "planType": "pro",
            },
            "requiresOpenaiAuth": True,
        }
    elif method == "model/list":
        result = {"data": [{
            "id": "gpt-test",
            "displayName": "GPT Test",
            "isDefault": True,
        }], "nextCursor": None}
    elif method == "account/rateLimits/read":
        result = {
            "rateLimits": {"primary": {"usedPercent": 12}},
            "accountId": "must-not-leak",
        }
    elif method == "fail/test":
        print(json.dumps({"id": request_id, "error": {
            "code": 42, "message": "expected failure"
        }}), flush=True)
        continue
    elif method == "slow/test":
        time.sleep(1)
        result = {}
    else:
        result = {}
    print(json.dumps({"id": request_id, "result": result}), flush=True)
'''


def fake_command():
    return [sys.executable, "-u", "-c", FAKE_SERVER]


class CodexAppServerClientTests(unittest.TestCase):
    def test_snapshot_uses_rpc_without_credentials(self):
        with CodexAppServerClient(command=fake_command()) as client:
            snapshot = client.snapshot()

        self.assertEqual(
            snapshot["account"]["account"]["type"], "chatgpt"
        )
        self.assertEqual(snapshot["models"][0]["id"], "gpt-test")
        self.assertEqual(
            snapshot["rateLimits"]["rateLimits"]["primary"]["usedPercent"],
            12,
        )

    def test_rpc_error_is_preserved(self):
        with CodexAppServerClient(command=fake_command()) as client:
            with self.assertRaises(CodexRpcError) as caught:
                client.request("fail/test")
        self.assertEqual(caught.exception.error["code"], 42)

    def test_timeout_does_not_poison_later_shutdown(self):
        with CodexAppServerClient(command=fake_command(), timeout=0.05) as client:
            with self.assertRaisesRegex(CodexAppServerError, "timeout"):
                client.request("slow/test")

    def test_safe_probe_drops_unknown_account_fields(self):
        safe = _safe_probe({
            "account": {
                "account": {
                    "type": "chatgpt",
                    "email": "person@example.test",
                    "planType": "pro",
                    "accessToken": "must-not-leak",
                },
                "requiresOpenaiAuth": True,
            },
            "models": [{"id": "gpt-test", "secret": "must-not-leak"}],
            "rateLimits": {
                "rateLimits": {"primary": {"usedPercent": 12}},
                "accountId": "must-not-leak",
            },
        })
        encoded = json.dumps(safe)
        self.assertNotIn("must-not-leak", encoded)
        self.assertEqual(safe["account"]["planType"], "pro")


if __name__ == "__main__":
    unittest.main()
