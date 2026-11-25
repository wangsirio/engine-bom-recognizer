import csv
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from openpyxl import load_workbook
from PIL import Image

from engine_bom import webapp
from engine_bom.agent_loop import AgentLoop
from engine_bom.config import load_config
from engine_bom.enterprise import mapped_components, validate_profile
from engine_bom.pipeline import BomPipeline
from engine_bom.schema import BomComponent, merge_components


PROFILE = {
    'export_fields': {'part_number': '件号', 'material': '零件材料', 'title_block.material': '图纸材料',
                      'detail.coat': '涂层号', 'constant.site': '工厂', 'constant.note': '人工补填'},
    'detail_fields': {'coat': ['涂层代号', '涂层编码']},
    'constant_fields': {'site': '01', 'note': None},
}


class EnterpriseCustomFieldTests(unittest.TestCase):
    def test_rejects_unknown_sources_and_bad_definitions(self):
        for patch_data in (
            {'export_fields': {'bad': '非法字段'}},
            {'export_fields': {'detail.unknown': '未配置字段'}},
            {'export_fields': {'constant.x': '未配置固定字段'}},
            {'detail_fields': {'coat': ['涂层代号', '涂层代号：']}},
            {'constant_fields': {'site': float('nan'), 'note': None}},
            {'constant_fields': {'site': {'bad': 'shape'}, 'note': None}},
        ):
            with self.subTest(patch_data=patch_data), self.assertRaises(ValueError):
                validate_profile(dict(PROFILE, **patch_data))

    def test_mapping_distinguishes_same_name_and_does_not_guess_missing(self):
        rows = [{'part_number': 'D10', 'material': '45钢', 'detail_values': {'coat': '002'}},
                {'part_number': 'D20', 'material': '铜'}]
        result = mapped_components(rows, PROFILE, {'material': 'Q235'})
        self.assertEqual(result['rows'], [
            {'件号': 'D10', '零件材料': '45钢', '图纸材料': 'Q235', '涂层号': '002', '工厂': '01', '人工补填': None},
            {'件号': 'D20', '零件材料': '铜', '图纸材料': 'Q235', '涂层号': None, '工厂': '01', '人工补填': None},
        ])
        self.assertIsNone(mapped_components(rows, PROFILE)['rows'][0]['图纸材料'])

    def test_extraction_uses_configured_headers_and_keeps_rows_separate(self):
        provider = Mock()
        provider.name = 'stub'
        provider.describe.return_value = {'provider': 'stub'}
        provider.analyze.return_value = SimpleNamespace(
            model='stub', latency_s=0, usage={}, raw_text=json.dumps({'components': [
                {'part_number': 'P1', 'quantity': 1, 'detail_reads': {
                    'coat': {'label': '涂层编码：', 'value': '002'},
                    'unconfigured': {'label': '任意列', 'value': 'ignore'}}},
                {'part_number': 'P2', 'quantity': 1, 'detail_values': {'coat': '不消费模型自报值'},
                 'detail_reads': {'coat': {'label': '材料', 'value': 'ignore'}}},
            ]}))
        with TemporaryDirectory() as folder:
            path = Path(folder, 'drawing.png')
            Image.new('RGB', (80, 80)).save(path)
            cfg = load_config()
            cfg.update(enterprise=PROFILE, output={'dir': folder, 'formats': ['json']})
            cfg['knowledge']['enabled'] = False
            cfg['assembly']['enabled'] = False
            pipeline = BomPipeline(cfg)
            pipeline.provider = provider
            pipeline.agent = AgentLoop(provider, 1)
            result = pipeline.process_file(str(path))
            self.assertEqual(result.status, 'ok', result.errors)
            data = json.loads(Path(folder, 'drawing.json').read_text(encoding='utf-8'))
        self.assertIn('涂层代号', provider.analyze.call_args.args[1])
        self.assertEqual(data['model_info']['enterprise_detail_fields'], ['coat'])
        self.assertEqual(data['components'][0]['detail_values'], {'coat': '002'})
        self.assertNotIn('detail_values', data['components'][1])
        parts = [BomComponent(part_number='P1', detail_values={'coat': coat}) for coat in ('A', 'B')]
        self.assertEqual(len(merge_components([parts])[0]), 2)

    def test_save_replaces_old_column_maps_and_keeps_other_settings(self):
        with TemporaryDirectory() as folder, patch.object(webapp, 'SETTINGS_PATH', str(Path(folder, 'settings.json'))):
            webapp.save_web_settings({'provider': {'name': 'mock'},
                                      'enterprise': dict(PROFILE, parameter_dictionary=['尺寸'])})
            webapp.save_web_settings({'enterprise': {'export_fields': {'part_number': '编号'},
                                                      'detail_fields': {}, 'constant_fields': {}}})
            saved = json.loads(Path(folder, 'settings.json').read_text(encoding='utf-8'))
            self.assertEqual(saved, {'provider': {'name': 'mock'}, 'enterprise': {
                'export_fields': {'part_number': '编号'}, 'detail_fields': {}, 'constant_fields': {},
                'parameter_dictionary': ['尺寸'],
            }})

    def test_profile_endpoint_lists_active_title_template_keys(self):
        request = SimpleNamespace(path='/api/enterprise/profile', _authorized=lambda: True, _json=Mock())
        with patch.object(webapp, 'load_web_config', return_value={'enterprise': PROFILE}), \
             patch.object(webapp, 'load_titleblock_template', return_value={'fields': [{'key': 'department'}]}):
            webapp.Handler.do_GET(request)
        config = request._json.call_args.args[0]
        self.assertEqual(config['title_keys'], ['department'])
        self.assertEqual(config['export_fields'], PROFILE['export_fields'])

    def test_web_exports_title_and_custom_detail_in_three_formats(self):
        result = {'bom': {'components': [{'part_number': 'D10', 'detail_values': {'coat': '001'}},
                                        {'part_number': 'D20', 'detail_values': {'coat': '=1+2'}}]},
                  'enterprise_title_block': {'material': 'Q235'}}
        with patch.object(webapp, 'load_web_config', return_value={'enterprise': PROFILE}):
            downloads = {}
            for fmt in ('json', 'csv', 'xlsx'):
                request = SimpleNamespace(path=f'/api/export/bom/test?profile=enterprise&fmt={fmt}',
                                          _job_result=lambda _: result, _send=Mock())
                webapp.Handler._api_export_bom(request, 'test')
                downloads[fmt] = request._send.call_args.args[1]
        out = json.loads(downloads['json'])
        self.assertEqual(out['title_block']['material'], 'Q235')
        self.assertEqual(out['rows'][0]['图纸材料'], 'Q235')
        self.assertEqual(out['rows'][0]['涂层号'], '001')
        self.assertEqual(out['rows'][1]['涂层号'], '=1+2')
        sheet = load_workbook(io.BytesIO(downloads['xlsx'])).active
        self.assertEqual(sheet['C2'].value, 'Q235')
        self.assertEqual(sheet['D2'].value, '001')
        self.assertEqual((sheet['D3'].value, sheet['D3'].data_type), ('=1+2', 's'))
        data = list(csv.DictReader(io.StringIO(downloads['csv'].decode('utf-8-sig'))))
        self.assertEqual(data[0]['涂层号'], '001')
        self.assertEqual(data[1]['涂层号'], "'=1+2")
        self.assertEqual(data[1]['人工补填'], '')
