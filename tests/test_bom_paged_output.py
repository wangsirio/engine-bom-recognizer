import csv
import io
import json
import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import fitz
from PIL import Image
from openpyxl import load_workbook

from engine_bom import webapp
from engine_bom.config import load_config
from engine_bom.history import build_record
from engine_bom.installation_review import review_images
from engine_bom.pipeline import BomPipeline
from engine_bom.providers.base import ProviderError, ProviderResponse
from engine_bom.run_consistency import compare_runs, snapshot


class SheetProvider:
    name = 'fixture'

    def __init__(self, fail=(), empty=(3,), conflict=True):
        self.fail = set(fail)
        self.empty = set(empty)
        self.conflict = conflict
        self.part_pages = []
        self.title_pages = []

    def describe(self):
        return {'provider': self.name, 'model': 'local-test-fixture'}

    def analyze(self, images, prompt):
        if 'TASK:TITLEBLOCK' in prompt:
            pages = json.loads(re.search(r'输入图片对应原始页码：(\[[^\]]+\])', prompt)[1])
            assert len(set(pages)) == 1
            page = pages[0]
            self.title_pages.append(page)
            block = {'drawing_name': f'Sheet {page}', 'designer': '工程师', 'date': '2026-01-01'}
            if page != 3:
                block['drawing_no'] = f'DWG-{page if self.conflict else 1}'
            result = {'title_block': dict(block, field_evidence={'material': {'label': '', 'value': ''}})}
        elif 'TASK:DIMS' in prompt:
            result = {'dimensions': [], 'balloons': []}
        else:
            assert len(images) == 1
            page = int(re.search(r'第 (\d+) / \d+ 页图像', prompt)[1])
            self.part_pages.append(page)
            if page in self.fail:
                raise ProviderError('测试中该页服务超时')
            result = {'components': [] if page in self.empty else [
                {'item_no': page * 10, 'quantity': page * 2, 'name_zh': '垫圈', 'part_number': 'W-1', 'source_page': 999}]}
        return ProviderResponse(raw_text=json.dumps(result, ensure_ascii=False), model='local-test-fixture')


class PagedOutputTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.folder = Path(temp.name)
        self.source = self.folder / 'fixture.pdf'
        with fitz.open() as pdf:
            for page in range(1, 4):
                pdf.new_page(width=360, height=300).insert_text((40, 50), f'Fixture page {page}')
            pdf.save(self.source)
        self.cfg = load_config()
        self.cfg.update(assembly={'enabled': False}, inspection={'enabled': False},
                        enterprise={'export_fields': {'part_number': '件号', 'source_page': '来源页',
                                                      'title_block.图号': '所属图号', 'quantity': '数量'}},
                        enterprise_title_template={'name': '测试模板', 'fields': [{'key': '图号', 'sources': ['drawing_no']}]})
        # An explicit Web mode must still use independent page reads and preserve printed rows.
        self.cfg['recognition'].update(mode='whole_doc', preserve_printed_rows=False, duplicate_quantity_policy='sum',
                                       min_confidence=1.0, detail_crops=False, region_crops=False)
        self.cfg['knowledge']['enabled'] = False

    def recognize(self, mode, provider=None, layout=None):
        from contextlib import nullcontext
        provider = provider or SheetProvider()
        with patch.object(webapp, 'load_web_config', return_value=self.cfg), \
             patch('engine_bom.pipeline.build_provider', return_value=provider), \
             (patch.object(BomPipeline, 'layout', return_value=layout) if layout else nullcontext()):
            result = webapp._job_bom(str(self.folder), str(self.source), {'output_mode': mode})
        self.assertEqual(provider.part_pages, [1, 2, 3])
        return result

    def export(self, result, query):
        handler = SimpleNamespace(path='/api/export/bom/fixture?' + query, _job_result=lambda _: result,
                                  _send=Mock(), _json=Mock())
        with patch.object(webapp, 'load_web_config', return_value=self.cfg):
            webapp.Handler._api_export_bom(handler, 'fixture')
        return handler

    def test_same_rows_distinct_output_scopes_and_original_pages(self):
        merged = self.recognize('merged')
        separate = self.recognize('per_page')
        rows = merged['bom']['components']
        self.assertEqual(rows, [row for unit in separate['page_results'] for row in unit['bom']['components']])
        self.assertEqual([(row['item_no'], row['source_page'], row['quantity']) for row in rows], [(10, 1, 2), (20, 2, 4)])
        self.assertEqual([item['status'] for item in separate['page_statuses']], ['recognized', 'recognized', 'empty'])
        self.assertNotIn('bom', separate)
        self.assertEqual([page['bom']['summary']['total_quantity'] for page in separate['page_results']], [2, 4, 0])
        self.assertFalse(any('序号不连续' in issue['message'] for page in separate['page_results'] for issue in page['issues']))
        self.assertEqual(merged['title_block'], {})
        self.assertIn('不同图号', merged['title_block_notice'])
        self.assertTrue(any('重复印刷' in text for text in merged['bom']['validation_issues']))
        self.assertEqual(separate['page_results'][1]['title_block']['source_page'], 2)

    def test_export_all_modes_respects_scope_and_page_title(self):
        merged = self.recognize('merged')
        separate = self.recognize('per_page')
        body = self.export(merged, 'fmt=json&profile=enterprise')._send.call_args.args[1]
        result = json.loads(body)
        self.assertEqual([r['所属图号'] for r in result['rows']], ['DWG-1', 'DWG-2'])
        self.assertNotIn('title_block', result)
        self.assertEqual(result['page_title_blocks']['2']['图号'], 'DWG-2')
        self.assertEqual(self.export(separate, 'fmt=json')._json.call_args.args[1], 400)
        self.assertEqual(self.export(separate, 'fmt=json&page=3')._json.call_args.args[1], 409)
        self.assertEqual(self.export(separate, 'fmt=json&page=4')._json.call_args.args[1], 404)
        _, raw, _, filename = self.export(separate, 'fmt=json&page=2')._send.call_args.args
        self.assertIn('-p002', filename)
        self.assertEqual(json.loads(raw)['title_block']['drawing_no'], 'DWG-2')
        self.assertEqual(json.loads(raw)['components'][0]['source_page'], 2)
        raw_csv = self.export(separate, 'fmt=csv&page=2&profile=enterprise')._send.call_args.args[1]
        lines = list(csv.DictReader(io.StringIO(raw_csv.decode('utf-8-sig'))))
        self.assertEqual(lines, [{'件号': 'W-1', '来源页': '2', '所属图号': 'DWG-2', '数量': '4'}])
        xlsx = self.export(separate, 'fmt=xlsx&page=2&profile=enterprise')._send.call_args.args[1]
        sheet = load_workbook(io.BytesIO(xlsx)).active
        self.assertEqual([cell.value for cell in sheet[2]], ['W-1', 2, 'DWG-2', 4])

    def test_failure_keeps_later_successes_and_blocks_combined_download(self):
        provider = SheetProvider(fail=(2,), empty=(), conflict=False)
        merged = self.recognize('merged', provider)
        separate = self.recognize('per_page', SheetProvider(fail=(2,), empty=(), conflict=False))
        self.assertEqual(provider.title_pages, [1, 3])
        self.assertEqual(merged['incomplete_pages'], [2])
        self.assertEqual(merged['page_statuses'][1]['rows'], None)
        self.assertEqual(merged['inspection']['visual_status'], 'not_run')
        for fmt in ('json', 'csv', 'xlsx'):
            self.assertEqual(self.export(merged, f'fmt={fmt}')._json.call_args.args[1], 409)
        self.assertEqual(self.export(separate, 'page=2&fmt=json')._json.call_args.args[1], 409)
        handler = self.export(separate, 'page=3&fmt=json&profile=enterprise')
        self.assertIsNone(json.loads(handler._send.call_args.args[1])['rows'][0]['所属图号'])
        self.assertEqual(build_record('fixture', 'bom', str(self.source), result=separate)['status'], 'incomplete')

    def test_localized_but_unreadable_table_requires_review(self):
        result = self.recognize('merged', SheetProvider(conflict=False), {'regions': [
            {'kind': 'bom_table', 'source_page': 3, 'bbox': [0.1, 0.1, 0.8, 0.8]}]})
        self.assertEqual(result['page_statuses'][2]['status'], 'needs_review')
        self.assertEqual(result['incomplete_pages'], [3])
        self.assertEqual(self.export(result, 'fmt=json')._json.call_args.args[1], 409)

    def test_crop_fallback_and_layout_cache_keep_document_identity(self):
        images = [Image.new('RGB', (80, 80)) for _ in range(2)]
        _, note = review_images([images[1]], None, images)
        self.assertIn('原第2页', note)
        self.assertIn('"source_page": 2', note)
        self.cfg['recognition']['region_crops'] = True
        pipeline = BomPipeline(self.cfg)
        with patch('engine_bom.drawing_layout.locate_regions', side_effect=[{'regions': []}, {'regions': []}]) as locate:
            a = pipeline.layout(images[:1])
            self.assertIs(a, pipeline.layout(images[:1]))
            self.assertIsNot(a, pipeline.layout(images[1:]))
            self.assertEqual(locate.call_count, 2)

    def test_run_consistency_never_compares_modes_or_failed_pages(self):
        separate = self.recognize('per_page')
        merged = self.recognize('merged')
        self.assertTrue(snapshot(separate)['bom'] != '[]')
        (self.folder / 'merged.json').write_text(json.dumps({'result': merged}), encoding='utf-8')
        self.assertEqual(compare_runs(separate, self.folder)['status'], 'no_reference')
