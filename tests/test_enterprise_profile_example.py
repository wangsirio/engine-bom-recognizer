import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from engine_bom import webapp
from engine_bom.enterprise import mapped_components, validate_profile


class EnterpriseProfileExampleTests(unittest.TestCase):
    def test_downloaded_example_is_importable_and_maps_bom_fields(self):
        request = SimpleNamespace(path='/api/enterprise/profile/example', _send=Mock())
        webapp.Handler.do_GET(request)
        request._send.assert_called_once()
        status, body, content_type, filename = request._send.call_args.args
        self.assertEqual(status, 200)
        self.assertEqual(content_type, 'application/json; charset=utf-8')
        self.assertEqual(filename, 'enterprise-profile.example.json')
        profile = validate_profile(json.loads(body.decode('utf-8')))
        output = mapped_components([{'part_number': 'P-002', 'quantity': 3}], profile)
        self.assertEqual(output['rows'][0]['物料编码'], 'P-002')
        self.assertEqual(output['rows'][0]['数量'], 3)
        self.assertNotIn('part_number', output['rows'][0])
