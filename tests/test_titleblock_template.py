import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from PIL import Image

from engine_bom import webapp
from engine_bom.titleblock import extract_titleblock
from engine_bom.titleblock_template import SOURCE_LABELS, apply_template, validate_template


TEMPLATE = {
    'name': '甲方模板',
    'fields': [
        {'key': 'drawing_code', 'sources': ['drawing_no'], 'aliases': ['图样代号', 'Drawing No.']},
        {'key': 'responsible', 'sources': ['checker', 'approver']},
        {'key': 'net_weight', 'sources': ['weight']},
        {'key': 'department', 'aliases': ['设计部门']},
    ],
}


class MappingTests(unittest.TestCase):
    def test_same_key_order_conflict_missing_and_whitelist(self):
        fields = {'drawing_no': '', 'designer': 'not exported', 'checker': '王', 'approver': '周',
                  'enterprise_fields': {'drawing_code': [{'label': '图样代号：', 'value': '001-A'}],
                                        'department': [{'label': '制造部门', 'value': 'unknown'}]}}
        mapped, meta = apply_template(fields, validate_template(TEMPLATE))
        self.assertEqual(mapped, {'drawing_code': '001-A', 'responsible': '王', 'net_weight': None, 'department': None})
        self.assertEqual(meta['conflicts'], ['responsible'])
        self.assertEqual(meta['missing_keys'], ['net_weight', 'department'])
        fields['checker'] = '   '
        mapped, meta = apply_template(fields, validate_template(TEMPLATE))
        self.assertEqual(mapped['responsible'], '周')
        self.assertEqual(meta['conflicts'], [])

    def test_rejects_duplicate_keys_sources_and_normalized_aliases(self):
        for fields in ([{'key': 'x', 'sources': ['date']}, {'key': ' x ', 'sources': ['revision']}],
                       [{'key': 'x', 'sources': ['bogus']}],
                       [{'key': 'x', 'sources': ['date', 'date']}],
                       [{'key': 'x', 'aliases': ['Drawing No.', 'Ｄｒａｗｉｎｇ no.：']}],
                       [{'key': 'x'}]):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                validate_template({'name': 'T', 'fields': fields})

    def test_model_receives_alias_prompt_and_returns_original_label_evidence(self):
        class Vision:
            name = 'real-model-stub'

            def analyze(self, images, prompt):
                self.prompt = prompt
                fields = {'field_evidence': {'material': {'label': '', 'value': ''}}, 'extra': {},
                          'enterprise_fields': {'department': [{'label': '设计部门', 'value': '工程部'}]}}
                return SimpleNamespace(raw_text=json.dumps({'title_block': fields}), model=self.name, latency_s=0)

        provider = Vision()
        template = validate_template(TEMPLATE)
        raw = extract_titleblock(provider, [Image.new('RGB', (10, 10))], template=template)['fields']
        self.assertIn('设计部门', provider.prompt)
        self.assertEqual(apply_template(raw, template)[0]['department'], '工程部')


class HandlerStub:
    def __init__(self, path, body=None):
        self.path = path
        self.body = body

    def _authorized(self):
        return True

    def _read_json_body(self):
        return self.body

    def _json(self, body, code=200):
        self._send(code, json.dumps(body).encode(), 'application/json')

    def _send(self, code, body, ctype, filename=None):
        self.response = (code, json.loads(body), filename)

    def _job_result(self, job_id):
        return self.result


class WebTemplateTests(unittest.TestCase):
    def test_import_persistence_bom_output_and_export_snapshot(self):
        endpoint = '/api/enterprise/titleblock-template'
        with TemporaryDirectory() as folder, patch.object(webapp, 'DATA_DIR', folder):
            request = HandlerStub(endpoint)
            webapp.Handler.do_GET(request)
            self.assertIsNone(request.response[1]['template'])
            self.assertEqual(request.response[1]['sources'], SOURCE_LABELS)

            request = HandlerStub(endpoint, {'template': TEMPLATE})
            webapp.Handler.do_POST(request)
            self.assertEqual(request.response[:2], (200, {'template': validate_template(TEMPLATE)}))
            path = Path(folder, 'titleblock_template.json')
            self.assertEqual(json.loads(path.read_text(encoding='utf-8')), validate_template(TEMPLATE))
            invalid = HandlerStub(endpoint, {'template': {'name': 'bad', 'fields': []}})
            webapp.Handler.do_POST(invalid)
            self.assertEqual(invalid.response[0], 400)
            self.assertEqual(webapp.load_titleblock_template(), validate_template(TEMPLATE))

            source = Path(folder, 'drawing.png')
            Image.new('RGB', (60, 60), 'white').save(source)
            config = {'provider': {'name': 'mock'}, 'ingestion': {'image_formats': ['.png']},
                      'output': {'dir': folder, 'formats': ['json']}}
            with patch.object(webapp, 'load_web_config', return_value=config):
                result = webapp._job_bom(folder, str(source), {})
                self.assertEqual(result['enterprise_title_block']['drawing_code'], 'XX490-ZM-01')
                self.assertIsNone(result['enterprise_title_block']['net_weight'])
                self.assertIn('material', result['title_block'])
                self.assertNotIn('material', result['enterprise_title_block'])

                disable = HandlerStub(endpoint, {'template': None})
                webapp.Handler.do_POST(disable)
                self.assertIsNone(webapp.load_titleblock_template())
                self.assertNotIn('enterprise_title_block', webapp._job_bom(folder, str(source), {}))
                export = HandlerStub('/api/export/bom/a?profile=enterprise&fmt=json')
                export.result = result
                webapp.Handler._api_export_bom(export, 'a')
                self.assertEqual(export.response[1]['title_block'], result['enterprise_title_block'])
                self.assertEqual(export.response[2], 'enterprise_bom.json')
                export.path = '/api/export/bom/a?fmt=json'
                webapp.Handler._api_export_bom(export, 'a')
                self.assertNotIn('title_block', export.response[1])
