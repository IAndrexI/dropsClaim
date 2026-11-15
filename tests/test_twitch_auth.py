import os, tempfile, unittest
from unittest import mock
from src.auth import twitch_auth as t


class TokenStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.p = mock.patch.multiple(t, DATA_DIR=self.tmp, TOKEN_FILE=os.path.join(self.tmp, "tok.json"))
        self.p.start()

    def tearDown(self):
        self.p.stop()

    def test_roundtrip_and_clear(self):
        t.save_token({"access_token": "a", "client_id": "cid"})
        self.assertEqual(t.load_token()["access_token"], "a")
        self.assertEqual(t.client_id_for("a"), "cid")
        t.clear_token()
        self.assertIsNone(t.load_token())

    def test_env_fallback_uses_web_client(self):
        self.assertEqual(t.get_credentials("cookie"), ("cookie", t.WEB_CLIENT_ID))

    def test_invalid_stored_token_falls_back(self):
        t.save_token({"access_token": "dead", "client_id": "cid"})
        with mock.patch.object(t, "validate_access_token", return_value=None):
            self.assertEqual(t.get_credentials("cookie")[0], "cookie")

    def test_poll_pending(self):
        with mock.patch.object(t, "_post_form", return_value=(400, {"message": "authorization_pending"})):
            self.assertEqual(t.poll_device_login("x")["status"], "pending")

    def test_poll_ok_saves(self):
        with mock.patch.object(t, "_post_form", return_value=(200, {"access_token": "n", "refresh_token": "r"})), \
             mock.patch.object(t, "validate_access_token", return_value={"login": "bob", "user_id": "1"}):
            self.assertEqual(t.poll_device_login("x"), {"status": "ok", "login": "bob"})
        self.assertEqual(t.load_token()["login"], "bob")


if __name__ == "__main__":
    unittest.main()
