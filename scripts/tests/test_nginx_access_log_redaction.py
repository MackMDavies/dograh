import re
import unittest
from pathlib import Path


TEMPLATE = Path(__file__).parents[2] / "deploy/templates/nginx.remote.conf.template"


class NginxAccessLogRedactionTest(unittest.TestCase):
    def test_access_log_omits_query_strings_and_referers(self):
        source = TEMPLATE.read_text()
        formats = re.findall(r"^log_format dograh_redacted (.*);$", source, re.MULTILINE)

        self.assertEqual(len(formats), 1)
        log_format = formats[0]
        self.assertIn("$uri", log_format)
        self.assertIsNone(re.search(r"\$(?:request|request_uri|args|query_string|http_referer)\b", log_format))

    def test_http_and_https_servers_use_the_redacted_access_log(self):
        source = TEMPLATE.read_text()
        self.assertEqual(source.count("access_log /var/log/nginx/access.log dograh_redacted;"), 2)


if __name__ == "__main__":
    unittest.main()
