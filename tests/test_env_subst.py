import sys
import unittest
from unittest.mock import patch

from sshtemp.env_subst import _env_lookup, find_env_var_names, substitute_env_vars


class EnvSubstTests(unittest.TestCase):
    def test_find_env_var_names_unique_in_order(self):
        text = "_PASSWORD: ${A}\n_THEN: ${B}\nssh ${A}@${HOST}\n"
        self.assertEqual(find_env_var_names(text), ["A", "B", "HOST"])

    def test_substitute_from_environ(self):
        text = "_PASSWORD: ${NOKIA_PASSWORD}\n"
        rendered, error = substitute_env_vars(
            text,
            environ={"NOKIA_PASSWORD": "secret"},
        )
        self.assertIsNone(error)
        self.assertEqual(rendered, "_PASSWORD: secret\n")

    def test_missing_env_returns_error(self):
        text = "_PASSWORD: ${MISSING_VAR}\n"
        rendered, error = substitute_env_vars(text, environ={})
        self.assertIn("missing environment variable: MISSING_VAR", error or "")
        self.assertIn("${MISSING_VAR}", rendered)

    def test_prompt_missing_fills_value(self):
        text = "_PASSWORD: ${PROMPTED}\n"

        def fake_prompt(_label: str) -> str:
            return "from-prompt"

        rendered, error = substitute_env_vars(
            text,
            environ={},
            prompt_missing=True,
            password_prompt=fake_prompt,
        )
        self.assertIsNone(error)
        self.assertEqual(rendered, "_PASSWORD: from-prompt\n")

    def test_prompt_missing_empty_value_is_error(self):
        text = "_PASSWORD: ${PROMPTED}\n"

        def fake_prompt(_label: str) -> str:
            return ""

        _, error = substitute_env_vars(
            text,
            environ={},
            prompt_missing=True,
            password_prompt=fake_prompt,
        )
        self.assertIn("missing environment variable: PROMPTED", error or "")

    def test_no_placeholders_is_noop(self):
        text = "_HOST: 10.0.0.1\n"
        rendered, error = substitute_env_vars(text, environ={})
        self.assertIsNone(error)
        self.assertEqual(rendered, text)

    def test_lowercase_and_mixed_case_names(self):
        text = "_PASSWORD: ${jump_server_password}\n${nokia_password}\n"
        rendered, error = substitute_env_vars(
            text,
            environ={
                "jump_server_password": "clab",
                "nokia_password": "NokiaSros1!",
            },
        )
        self.assertIsNone(error)
        self.assertEqual(
            rendered,
            "_PASSWORD: clab\nNokiaSros1!\n",
        )

    def test_invalid_placeholder_names_are_left_unchanged(self):
        text = "_PASSWORD: ${NOKIA-PASSWORD}\n"
        rendered, error = substitute_env_vars(text, environ={})
        self.assertIsNone(error)
        self.assertEqual(rendered, text)

    def test_env_lookup_exact_match(self):
        self.assertEqual(_env_lookup({"foo": "bar"}, "foo"), "bar")

    @patch.object(sys, "platform", "linux")
    def test_env_lookup_linux_exact_case_required(self):
        self.assertIsNone(_env_lookup({"foo": "bar"}, "FOO"))

    @patch.object(sys, "platform", "win32")
    def test_env_lookup_windows_case_insensitive(self):
        self.assertEqual(_env_lookup({"NOKIA_PASSWORD_": "secret"}, "nokia_password_"), "secret")

    @patch.object(sys, "platform", "linux")
    def test_env_lookup_linux_case_sensitive(self):
        self.assertIsNone(_env_lookup({"NOKIA_PASSWORD_": "secret"}, "nokia_password_"))

    @patch.object(sys, "platform", "win32")
    def test_substitute_windows_case_insensitive_environ(self):
        text = "_PASSWORD: ${nokia_password_}\n"
        rendered, error = substitute_env_vars(
            text,
            environ={"NOKIA_PASSWORD_": "NokiaSros1!"},
        )
        self.assertIsNone(error)
        self.assertEqual(rendered, "_PASSWORD: NokiaSros1!\n")

    def test_placeholder_in_comment_line_is_ignored(self):
        text = (
            "_# docs: use ${NOKIA_PASSWORD} or a literal line\n"
            "_PASSWORD: ${DEVICE_PASSWORD}\n"
        )
        rendered, error = substitute_env_vars(
            text,
            environ={"DEVICE_PASSWORD": "secret"},
        )
        self.assertIsNone(error)
        self.assertIn("${NOKIA_PASSWORD}", rendered)
        self.assertIn("_PASSWORD: secret", rendered)
        self.assertEqual(find_env_var_names(text), ["DEVICE_PASSWORD"])


if __name__ == "__main__":
    unittest.main()
