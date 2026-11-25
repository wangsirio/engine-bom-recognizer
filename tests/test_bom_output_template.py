import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from engine_bom import webapp
from engine_bom.bom_output_template import (
    BOM_SYSTEM_FIELDS, TITLE_SYSTEM_FIELDS, from_legacy, import_template, title_field_presets, to_legacy,
)
from engine_bom.enterprise import mapped_components
from engine_bom.titleblock_template import apply_template

ENDPOINT = '/api/enterprise/bom-output-template'
EXAMPLE = Path(__file__).resolve().parents[1] / 'static' / 'bom-output-template.example.json'


def example():
    return json.loads(EXAMPLE.read_text(encoding='utf-8'))


def request(path, body=None, authorized=True):
    return SimpleNamespace(path=path, _authorized=lambda: authorized, _read_json_body=lambda: body, _json=Mock())


class BomOutputTemplateTests(unittest.TestCase):
    def test_system_fields_use_separate_catalogs_and_values(self):
        self.assertNotIn('quantity', TITLE_SYSTEM_FIELDS)
        self.assertNotIn('part_number', TITLE_SYSTEM_FIELDS)
        self.assertNotIn('drawing_no', BOM_SYSTEM_FIELDS)
        self.assertEqual(TITLE_SYSTEM_FIELDS['material'], '图纸材料')
        self.assertEqual(BOM_SYSTEM_FIELDS['material'], '零件材料')
        data = example()
        with self.assertRaisesRegex(ValueError, '标题栏系统字段'):
            to_legacy(dict(data, title_fields=[{'output': '用量', 'sources': [{'type': 'system', 'key': 'quantity'}]}]))
        data['title_fields'] = title_field_presets()['common']
        data['bom_columns'] = [
            {'output': '零件材料', 'sources': [{'type': 'system', 'key': 'material'}]},
            {'output': '图纸材料', 'sources': [{'type': 'title', 'key': '图纸材料'}]},
        ]
        _, profile, title = to_legacy(data)
        header, _ = apply_template({'material': '图纸材料值'}, title)
        rows = mapped_components([{'material': '零件材料值'}], profile, header)['rows']
        self.assertEqual(rows, [{'图纸材料': '图纸材料值', '零件材料': '零件材料值'}])

    def test_title_presets_are_existing_extractable_fields(self):
        presets = title_field_presets()
        expected = {
            'common': {'drawing_name', 'drawing_no', 'revision', 'material', 'scale', 'weight', 'date'},
            'signoff': {'designer', 'checker', 'approver'},
        }
        for group, keys in expected.items():
            self.assertEqual({field['sources'][0]['key'] for field in presets[group]}, keys)
        name, profile, title = to_legacy(dict(example(), title_fields=presets['common'] + presets['signoff']))
        self.assertEqual(len(title['fields']), 10)
        self.assertEqual(from_legacy(profile, title, name)['title_fields'], presets['common'] + presets['signoff'])

    def test_example_combines_both_scopes_and_custom_sources(self):
        data = example()
        name, profile, title = to_legacy(data)
        title_data, _ = apply_template({'drawing_name': '减速器', 'drawing_no': 'A-01'}, title)
        rows = mapped_components([{'part_number': 'P1', 'detail_values': {'涂层代号': '001'}}], profile, title_data)['rows']
        self.assertEqual(name, '甲方工程图 BOM 示例')
        self.assertEqual(title_data, {'图名': '减速器', '图号': 'A-01', '版本': None, '图纸材料': None,
                                      '比例': None, '图纸标注重量': None, '日期': None, '设计部门': None})
        self.assertEqual(rows[0]['所属图纸图号'], 'A-01')
        self.assertEqual(rows[0]['涂层代号'], '001')
        self.assertEqual(rows[0]['工厂代码'], 'A01')
        self.assertEqual(data, from_legacy(profile, title, name))

    def test_example_preserves_defaults_and_matches_download_format(self):
        data = example()
        default_title = title_field_presets()['common']
        default_bom = from_legacy({}, None)['bom_columns']
        self.assertEqual(data['title_fields'][:len(default_title)], default_title)
        self.assertEqual(data['bom_columns'][:len(default_bom)], default_bom)
        imported, _ = import_template(data, {}, None)
        self.assertEqual(EXAMPLE.read_text(encoding='utf-8'), json.dumps(imported, ensure_ascii=False, indent=2) + '\n')

    def test_load_old_templates_and_keep_other_scope(self):
        profile = {'export_fields': {'detail.coat': '涂层号', 'title_block.code': '所属图纸图号'},
                   'detail_fields': {'coat': ['涂层代号']}}
        title = {'name': '旧标题栏', 'fields': [{'key': 'code', 'sources': ['drawing_no']}]}
        combined, _ = import_template(title, profile, None)
        _, actual_profile, actual_title = to_legacy(combined)
        self.assertEqual(actual_profile['detail_fields'], profile['detail_fields'])
        self.assertEqual(actual_profile['export_fields'], profile['export_fields'])
        self.assertEqual(actual_title['fields'][0]['key'], 'code')
        combined, notice = import_template(profile, {}, title)
        self.assertIn('审图规则', notice)
        self.assertEqual(combined['title_fields'][0]['output'], 'code')

    def test_invalid_rows_rejected_and_title_may_be_disabled(self):
        data = example()
        data['title_fields'] = []
        self.assertIsNone(to_legacy(data)[2])
        for invalid in (
            {'output': '件号', 'sources': [{'type': 'system', 'key': 'drawing_no'}]},
            {'output': '件号', 'sources': [{'type': 'drawing', 'labels': []}]},
            {'output': '件号', 'sources': [{'type': 'fixed'}]},
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                to_legacy(dict(data, bom_columns=[invalid]))

    def test_current_configuration_fallback_atomic_save_and_legacy_access(self):
        with TemporaryDirectory() as folder:
            home = Path(folder)
            with patch.multiple(webapp, SETTINGS_PATH=str(home / 'web_settings.json'), DATA_DIR=folder,
                                CONFIG_PATH=None, _ROOT=folder):
                webapp.save_web_settings({'enterprise': {'parameter_dictionary': ['硬度'],
                                                          'export_fields': {'part_number': '旧件号'}}})
                get = request(ENDPOINT)
                webapp.Handler.do_GET(get)
                first_load = get._json.call_args.args[0]
                self.assertTrue(first_load['suggest_title_presets'])
                self.assertEqual(first_load['template']['title_fields'], [])
                self.assertEqual(first_load['title_presets'], title_field_presets())
                self.assertIsNone(webapp.load_titleblock_template())
                old = {'name': '旧标题栏', 'fields': [{'key': '旧图号', 'sources': ['drawing_no']}]}
                webapp.Handler.do_POST(request('/api/enterprise/titleblock-template', {'template': old}))
                old_path = home / 'titleblock_template.json'
                self.assertTrue(old_path.is_file())
                webapp.Handler.do_GET(get)
                self.assertFalse(get._json.call_args.args[0]['suggest_title_presets'])
                fallback = get._json.call_args.args[0]['template']
                self.assertEqual(fallback['title_fields'][0]['output'], '旧图号')
                self.assertEqual(fallback['bom_columns'][0]['output'], '旧件号')

                before = (home / 'web_settings.json').read_bytes()
                preview = request(ENDPOINT + '/preview', example())
                webapp.Handler.do_POST(preview)
                self.assertEqual(before, (home / 'web_settings.json').read_bytes())
                self.assertIn('template', preview._json.call_args.args[0])
                invalid = request(ENDPOINT, dict(example(), bom_columns=[]))
                webapp.Handler.do_POST(invalid)
                self.assertEqual(invalid._json.call_args.args[1], 400)
                self.assertEqual(before, (home / 'web_settings.json').read_bytes())

                req = request(ENDPOINT, example())
                webapp.Handler.do_POST(req)
                cfg = webapp.load_web_config()
                self.assertEqual(cfg['enterprise']['parameter_dictionary'], ['硬度'])
                self.assertEqual(cfg['enterprise']['export_fields']['part_number'], '图号/件号')
                self.assertEqual(webapp.load_titleblock_template()['fields'][0]['key'], '图名')
                webapp.Handler.do_GET(get)
                self.assertEqual(get._json.call_args.args[0]['template'], req._json.call_args.args[0]['template'])
                self.assertEqual(json.loads(old_path.read_text(encoding='utf-8'))['name'], '旧标题栏')

                webapp.Handler.do_POST(request('/api/enterprise/titleblock-template', {'template': None}))
                self.assertIsNone(webapp.load_titleblock_template())
                webapp.Handler.do_GET(get)
                self.assertFalse(get._json.call_args.args[0]['suggest_title_presets'])
                webapp.Handler.do_POST(request('/api/enterprise/profile', {'export_fields': {'quantity': '数量'}}))
                self.assertEqual(webapp.load_web_config()['enterprise']['export_fields'], {'quantity': '数量'})

    def test_requires_token_and_does_not_write_in_fixed_mode(self):
        req = request(ENDPOINT, authorized=False)
        webapp.Handler.do_GET(req)
        self.assertEqual(req._json.call_args.args[1], 403)
        req = request(ENDPOINT, example())
        with patch.object(webapp, 'CONFIG_PATH', '/fixed'), patch.object(webapp, 'save_web_settings') as save:
            webapp.Handler.do_POST(req)
        self.assertEqual(req._json.call_args.args[1], 409)
        save.assert_not_called()
