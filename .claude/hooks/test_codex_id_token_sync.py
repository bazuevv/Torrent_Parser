#!/usr/bin/env python3
"""Tests for codex_id_token_sync and the OpenAI subscription automation."""

import base64
import datetime
import json
import os
import tempfile
import unittest
from unittest import mock

import account_switcher
import codex_bridge_manager
import codex_id_token_sync

BRIDGE_URL = (
    f"http://{codex_bridge_manager.BRIDGE_HOST}:"
    f"{codex_bridge_manager.BRIDGE_PORT}"
)
AUTH_NS = "https://api.openai.com/auth"


def make_jwt(email="user@gmail.com", start="2088-01-01T00:00:00+00:00",
             until="2088-01-31T00:00:00+00:00", plan="plus",
             with_dates=True):
    def enc(obj):
        raw = json.dumps(obj).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    auth_ns = {"chatgpt_plan_type": plan}
    if with_dates:
        auth_ns.update({
            "chatgpt_subscription_active_start": start,
            "chatgpt_subscription_active_until": until,
            "chatgpt_subscription_last_checked": start,
        })
    payload = {"email": email, AUTH_NS: auth_ns}
    return f"{enc({'alg': 'RS256'})}.{enc(payload)}.sig"


def local_paid_at(start):
    return datetime.datetime.fromisoformat(start).astimezone().replace(
        tzinfo=None).isoformat(timespec="minutes")


class SyncTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.claude_dir = os.path.join(tmp.name, "claude")
        os.makedirs(self.claude_dir)
        self.auth = os.path.join(tmp.name, "auth.json")
        self.subs = os.path.join(self.claude_dir, ".account-subs.json")
        for patch in (
                mock.patch.object(account_switcher, "CLAUDE_DIR", self.claude_dir),
                mock.patch.object(account_switcher, "SUBS_FILE", self.subs)):
            patch.start()
            self.addCleanup(patch.stop)
        self.write_account("settings_openai.json", BRIDGE_URL)
        self.write_account("settings_glm.json", "https://api.z.ai")

    def write_account(self, name, base_url):
        with open(os.path.join(self.claude_dir, name), "w", encoding="utf-8") as fh:
            json.dump({"env": {
                "ANTHROPIC_BASE_URL": base_url,
                "ANTHROPIC_AUTH_TOKEN": "key",
            }}, fh)

    def write_auth(self, token):
        with open(self.auth, "w", encoding="utf-8") as fh:
            json.dump({"tokens": {"id_token": token}}, fh)

    def write_subs(self, data):
        with open(self.subs, "w", encoding="utf-8") as fh:
            json.dump(data, fh)

    def subs_data(self):
        with open(self.subs, encoding="utf-8") as fh:
            return json.load(fh)

    def test_fills_missing_subscription_for_openai_account(self):
        self.write_auth(make_jwt())

        updated = codex_id_token_sync.sync(self.auth)

        self.assertEqual(updated, ["settings_openai.json"])
        record = self.subs_data()["settings_openai.json"]
        self.assertEqual(record["paidAt"], local_paid_at("2088-01-01T00:00:00+00:00"))
        self.assertEqual(record["days"], 30)
        self.assertEqual(record["email"], "user@gmail.com")
        self.assertEqual(record["plan"], "plus")
        self.assertNotIn("settings_glm.json", self.subs_data())  # не openai не тронут

    def test_active_subscription_skips_token_entirely(self):
        # Подписка действующая — auth.json может даже отсутствовать:
        # проверка токена не запускается вовсе.
        self.write_subs({"settings_openai.json": {
            "paidAt": "2088-01-01T00:00", "days": 3650,
            "email": "user@gmail.com", "plan": "plus"}})

        updated = codex_id_token_sync.sync(self.auth)

        self.assertEqual(updated, [])

    def test_renews_expired_subscription(self):
        self.write_subs({"settings_openai.json": {
            "paidAt": "2000-01-01T00:00", "days": 30,
            "email": "old@example.test", "plan": "plus"}})
        self.write_auth(make_jwt(start="2026-08-27T11:55:10+00:00",
                                 until="2026-09-26T11:55:10+00:00"))

        updated = codex_id_token_sync.sync(self.auth)

        self.assertEqual(updated, ["settings_openai.json"])
        record = self.subs_data()["settings_openai.json"]
        self.assertEqual(record["paidAt"], local_paid_at("2026-08-27T11:55:10+00:00"))
        self.assertEqual(record["email"], "user@gmail.com")

    def test_no_openai_accounts_never_touch_token(self):
        os.remove(os.path.join(self.claude_dir, "settings_openai.json"))

        updated = codex_id_token_sync.sync(self.auth)

        self.assertEqual(updated, [])
        self.assertFalse(os.path.exists(self.auth))
        self.assertFalse(os.path.exists(self.subs))

    def test_identical_data_after_expiry_is_not_rewritten(self):
        # Подписка истекла, продления в токене нет — данные те же,
        # файл не дёргается (иначе каждый старт сессии писал бы зря).
        start, until = "2000-01-01T00:00:00+00:00", "2000-01-31T00:00:00+00:00"
        self.write_auth(make_jwt(start=start, until=until))
        self.write_subs({"settings_openai.json": {
            "paidAt": local_paid_at(start), "days": 30,
            "email": "user@gmail.com", "plan": "plus"}})
        os.utime(self.subs, (1, 1))

        updated = codex_id_token_sync.sync(self.auth)

        self.assertEqual(updated, [])
        self.assertEqual(os.stat(self.subs).st_mtime, 1)

    def test_token_without_dates_fills_labels_only(self):
        self.write_auth(make_jwt(with_dates=False))

        updated = codex_id_token_sync.sync(self.auth)

        self.assertEqual(updated, ["settings_openai.json"])
        record = self.subs_data()["settings_openai.json"]
        self.assertEqual(record, {"email": "user@gmail.com", "plan": "plus"})

    def test_malformed_token_is_silent(self):
        self.write_auth("not-a-jwt")

        updated = codex_id_token_sync.sync(self.auth)

        self.assertEqual(updated, [])
        self.assertFalse(os.path.exists(self.subs))

    def test_manual_subscription_write_rejected_for_openai(self):
        ok, message = account_switcher.write_subscription(
            "settings_openai.json", "2026-01-01T00:00", 30,
            "user@gmail.com", "plus")

        self.assertFalse(ok)
        self.assertIn("автоматически", message)
        self.assertFalse(os.path.exists(self.subs))

    def test_manual_subscription_write_still_works_for_custom(self):
        ok, _ = account_switcher.write_subscription(
            "settings_glm.json", "2026-01-01T00:00", 30, None, None)

        self.assertTrue(ok)
        self.assertEqual(self.subs_data()["settings_glm.json"]["days"], 30)

    def test_account_config_hides_subscription_for_openai_only(self):
        self.write_subs({"settings_openai.json": {
            "paidAt": "2088-01-01T00:00", "days": 30}})

        ok, _, config = account_switcher.read_account_config("settings_openai.json")
        self.assertTrue(ok)
        self.assertIsNone(config["subscription"])

        ok, _, config = account_switcher.read_account_config("settings_glm.json")
        self.assertTrue(ok)
        self.assertIsNone(config["subscription"])  # записи и не было

        self.write_subs({"settings_glm.json": {"paidAt": "2026-01-01T00:00"}})
        ok, _, config = account_switcher.read_account_config("settings_glm.json")
        self.assertTrue(ok)
        self.assertEqual(config["subscription"]["paidAt"], "2026-01-01T00:00")


if __name__ == "__main__":
    unittest.main()
