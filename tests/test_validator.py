import unittest

from sshtemp import validate_template
from sshtemp.exceptions import SshTempValidationError


class ValidateTemplateTests(unittest.TestCase):
    def test_valid_minimal_template(self):
        template = """
_HOST: 10.0.0.1
_USERNAME: admin
show version
"""
        result = validate_template(template)
        self.assertTrue(result.valid)
        self.assertEqual(result.issues, [])
        self.assertEqual(len(result.warnings), 1)
        self.assertIn("_EXPECT", result.warnings[0])

    def test_no_expect_warning_when_expect_set(self):
        template = """
_HOST: 10.0.0.1
_USERNAME: admin
_EXPECT: #\s*$
show version
"""
        result = validate_template(template)
        self.assertTrue(result.valid)
        self.assertEqual(result.warnings, [])

    def test_empty_directive_prefix(self):
        result = validate_template("_HOST: 10.0.0.1", directive_prefix="")
        self.assertFalse(result.valid)
        self.assertIn("directive_prefix must not be empty", result.issues)

    def test_missing_host(self):
        template = """
_USERNAME: admin
show version
"""
        result = validate_template(template)
        self.assertFalse(result.valid)
        self.assertTrue(any("_HOST" in issue for issue in result.issues))

    def test_missing_username(self):
        template = """
_HOST: 10.0.0.1
show version
"""
        result = validate_template(template)
        self.assertFalse(result.valid)
        self.assertTrue(any("_USERNAME" in issue for issue in result.issues))

    def test_unknown_directive(self):
        template = """
_HOST: 10.0.0.1
_USERNAME: admin
_xEXPECT: prompt
show version
"""
        result = validate_template(template)
        self.assertFalse(result.valid)
        self.assertTrue(any("unknown directive" in issue for issue in result.issues))

    def test_invalid_expect_regex(self):
        template = """
_HOST: 10.0.0.1
_USERNAME: admin
_EXPECT: [unclosed
show version
"""
        result = validate_template(template)
        self.assertFalse(result.valid)
        self.assertTrue(any("_EXPECT" in issue for issue in result.issues))

    def test_if_without_then(self):
        template = """
_HOST: 10.0.0.1
_USERNAME: admin
_IF: password
show version
"""
        result = validate_template(template)
        self.assertFalse(result.valid)
        self.assertTrue(any("_IF without" in issue for issue in result.issues))

    def test_then_without_if(self):
        template = """
_HOST: 10.0.0.1
_USERNAME: admin
_THEN: secret
show version
"""
        result = validate_template(template)
        self.assertFalse(result.valid)
        self.assertTrue(any("_THEN without" in issue for issue in result.issues))

    def test_unclosed_capture_block(self):
        template = """
_HOST: 10.0.0.1
_USERNAME: admin
_CAPTURE:config
show version
"""
        result = validate_template(template)
        self.assertFalse(result.valid)
        self.assertTrue(any("unclosed" in issue for issue in result.issues))

    def test_end_without_capture(self):
        template = """
_HOST: 10.0.0.1
_USERNAME: admin
_END
show version
"""
        result = validate_template(template)
        self.assertFalse(result.valid)
        self.assertTrue(any("_END without" in issue for issue in result.issues))

    def test_invalid_errorfail_value(self):
        template = """
_HOST: 10.0.0.1
_USERNAME: admin
_ERRORFAIL: maybe
show version
"""
        result = validate_template(template)
        self.assertFalse(result.valid)
        self.assertTrue(any("_ERRORFAIL" in issue for issue in result.issues))

    def test_invalid_encoding(self):
        template = """
_HOST: 10.0.0.1
_USERNAME: admin
_ENCODING: bad-codec-name
show version
"""
        result = validate_template(template)
        self.assertFalse(result.valid)
        self.assertTrue(any("_ENCODING" in issue for issue in result.issues))

    def test_invalid_sleep(self):
        template = """
_HOST: 10.0.0.1
_USERNAME: admin
_SLEEP: abc
show version
"""
        result = validate_template(template)
        self.assertFalse(result.valid)
        self.assertTrue(any("_SLEEP" in issue for issue in result.issues))

    def test_invalid_placeholder_name(self):
        template = """
_HOST: 10.0.0.1
_USERNAME: admin
_PASSWORD: ${my-var}
show version
"""
        result = validate_template(template)
        self.assertFalse(result.valid)
        self.assertTrue(any("invalid placeholder" in issue for issue in result.issues))
        self.assertTrue(any("my-var" in issue for issue in result.issues))

    def test_placeholder_on_comment_line_is_ignored(self):
        template = """
_HOST: 10.0.0.1
_USERNAME: admin
_# password: ${my-var}
show version
"""
        result = validate_template(template)
        self.assertTrue(result.valid)

    def test_raise_on_error(self):
        template = "_USERNAME: admin\nshow version\n"
        with self.assertRaises(SshTempValidationError) as ctx:
            validate_template(template, raise_on_error=True)
        self.assertTrue(ctx.exception.issues)

    def test_directives_only_without_commands_is_valid(self):
        template = """
_HOST: 10.0.0.1
_USERNAME: admin
_EXPECT: #\s*$
"""
        result = validate_template(template)
        self.assertTrue(result.valid)


if __name__ == "__main__":
    unittest.main()
