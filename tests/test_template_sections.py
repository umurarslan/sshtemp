import unittest

from sshtemp.template_sections import (
    TemplateSectionsError,
    expand_template_sections,
    prepare_parallel_template_sections,
    split_template_sections,
)


class SplitTemplateSectionsTests(unittest.TestCase):
    def test_single_host_returns_one_section(self):
        template = """
_HOST: 10.0.0.1
_USERNAME: admin
show version
"""
        sections = split_template_sections(template.strip())
        self.assertEqual(len(sections), 1)
        self.assertTrue(sections[0].startswith("_HOST: 10.0.0.1"))

    def test_multiple_hosts_split(self):
        template = """
_HOST: 10.0.0.1
_USERNAME: admin
show version

_HOST: 10.0.0.2
show version
"""
        sections = split_template_sections(template.strip())
        self.assertEqual(len(sections), 2)
        self.assertTrue(sections[0].startswith("_HOST: 10.0.0.1"))
        self.assertTrue(sections[1].startswith("_HOST: 10.0.0.2"))
        self.assertIn("_USERNAME: admin", sections[0])
        self.assertNotIn("_USERNAME:", sections[1])

    def test_allows_comments_before_first_host(self):
        template = """
_# setup
_HOST: 10.0.0.1
_USERNAME: admin
"""
        sections = split_template_sections(template.strip())
        self.assertEqual(len(sections), 1)

    def test_rejects_content_before_first_host(self):
        template = """
show version
_HOST: 10.0.0.1
_USERNAME: admin
"""
        with self.assertRaises(TemplateSectionsError) as ctx:
            split_template_sections(template.strip())
        self.assertIn("before first _HOST", str(ctx.exception))

    def test_rejects_missing_host(self):
        with self.assertRaises(TemplateSectionsError) as ctx:
            split_template_sections("_USERNAME: admin\nshow version")
        self.assertIn("no _HOST", str(ctx.exception))


class ExpandTemplateSectionsTests(unittest.TestCase):
    def test_injects_missing_stickies_after_host(self):
        sections = [
            "_HOST: 10.0.0.1\n_USERNAME: admin\n_PASSWORD: secret\n_EXPECT: #\\s*$\nshow version",
            "_HOST: 10.0.0.2\nshow version",
        ]
        expanded = expand_template_sections(sections)
        lines = expanded[1].splitlines()
        self.assertEqual(lines[0], "_HOST: 10.0.0.2")
        self.assertEqual(lines[1], "_USERNAME: admin")
        self.assertEqual(lines[2], "_PASSWORD: secret")
        self.assertEqual(lines[3], "_EXPECT: #\\s*$")
        self.assertEqual(lines[4], "show version")

    def test_does_not_override_existing_directives(self):
        sections = [
            "_HOST: 10.0.0.1\n_USERNAME: admin\n_EXPECT: #\\s*$",
            "_HOST: 10.0.0.2\n_USERNAME: root\nshow version",
        ]
        expanded = expand_template_sections(sections)
        self.assertIn("_USERNAME: root", expanded[1])
        self.assertNotIn("_USERNAME: admin", expanded[1].splitlines()[1])

    def test_does_not_inject_logid_error_wait_retry(self):
        sections = [
            (
                "_HOST: 10.0.0.1\n"
                "_USERNAME: admin\n"
                "_LOGID: device-a\n"
                "_WAIT: 120\n"
                "_RETRY: 3\n"
                "_ERROR:(?i)error:\n"
                "show version\n"
                "_ERROR:"
            ),
            "_HOST: 10.0.0.2\nshow version",
        ]
        expanded = expand_template_sections(sections)
        section_two = expanded[1]
        self.assertIn("_USERNAME: admin", section_two)
        self.assertNotIn("_LOGID:", section_two)
        self.assertNotIn("_WAIT:", section_two)
        self.assertNotIn("_RETRY:", section_two)
        self.assertNotIn("_ERROR:", section_two)

    def test_carries_state_across_three_sections(self):
        sections = [
            "_HOST: jump\n_USERNAME: clab\n_PASSWORD: jump-pass\n_PORT: 2222\n_EXPECT: #\\s*$",
            "_HOST: jump\nshow version",
            "_HOST: jump\n_PORT: 2223\nshow version",
        ]
        expanded = expand_template_sections(sections)
        self.assertIn("_USERNAME: clab", expanded[1])
        self.assertIn("_PASSWORD: jump-pass", expanded[2])
        self.assertIn("_PORT: 2223", expanded[2])
        self.assertNotIn("_PORT: 2222", expanded[2])

    def test_injects_shell_recv_buffer_encoding(self):
        sections = [
            "_HOST: 10.0.0.1\n_SHELL: 240x72\n_RECV_BUFFER: 32768\n_ENCODING: utf-8",
            "_HOST: 10.0.0.2\nshow version",
        ]
        expanded = expand_template_sections(sections)
        section_two = expanded[1]
        self.assertIn("_SHELL: 240x72", section_two)
        self.assertIn("_RECV_BUFFER: 32768", section_two)
        self.assertIn("_ENCODING: utf-8", section_two)

    def test_rejects_unclosed_capture_in_section(self):
        sections = [
            "_HOST: 10.0.0.1\n_USERNAME: admin",
            "_HOST: 10.0.0.2\n_CAPTURE:out.txt\nshow version",
        ]
        with self.assertRaises(TemplateSectionsError) as ctx:
            expand_template_sections(sections)
        self.assertIn("unclosed _CAPTURE", str(ctx.exception))

    def test_first_section_is_not_modified(self):
        sections = [
            "_HOST: 10.0.0.1\n_USERNAME: admin",
            "_HOST: 10.0.0.2\nshow version",
        ]
        expanded = expand_template_sections(sections)
        self.assertEqual(expanded[0], sections[0])


class PrepareParallelTemplateSectionsTests(unittest.TestCase):
    def test_split_and_expand_together(self):
        template = """
_# jump template
_HOST: 172.29.112.112
_USERNAME: clab
_PASSWORD: jump-pass
_PORT: 2222
_EXPECT: #\\s*$

_HOST: 172.29.112.112
_LOGID: 10.100.100.3
show version
"""
        sections = prepare_parallel_template_sections(template.strip())
        self.assertEqual(len(sections), 2)
        self.assertIn("_USERNAME: clab", sections[1])
        self.assertIn("_PORT: 2222", sections[1])
        self.assertIn("_LOGID: 10.100.100.3", sections[1])
        self.assertNotIn("_USERNAME: clab", sections[1].splitlines()[0])


if __name__ == "__main__":
    unittest.main()
