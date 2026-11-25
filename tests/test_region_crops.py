# -*- coding: utf-8 -*-
"""标题栏/明细栏区域裁切接入 BOM 识别的回归测试。

约束（改这里之前先读）：
  * 裁切图是原页的局部，永远不产生新页；source_page 必须保留原始页码。
  * 逐页识别时 images 只有一页，而区域坐标是全文档页码，两者不能混用索引。
  * 定位失败必须退回原四象限裁切，不能让 BOM 识别整体失败。
"""
import json
import os
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

from engine_bom.agent_loop import AgentLoop
from engine_bom.config import load_config
from engine_bom.drawing_layout import (bom_table_images, padded_bbox, region_entries,
                                       titleblock_images)
from engine_bom.installation_review import review_images
from engine_bom.pipeline import BomPipeline
from engine_bom.prompts import get_prompt_builder
from engine_bom.providers import ProviderResponse


class CropGeometryTests(unittest.TestCase):
    def test_padded_bbox_clamps_and_uses_per_kind_padding(self):
        title = padded_bbox({'kind': 'title_block', 'bbox': [0.90, 0.95, 0.99, 0.99]})
        self.assertEqual(title[2], 1.0)
        self.assertEqual(title[3], 1.0)
        table = padded_bbox({'kind': 'bom_table', 'bbox': [0.02, 0.02, 0.5, 0.5]})
        self.assertEqual(table[0], 0.0)
        self.assertEqual(table[1], 0.0)

    def test_region_entries_crop_only_located_regions(self):
        images = [Image.new('RGB', (2000, 2000))]
        regions = [
            {'kind': 'title_block', 'source_page': 1, 'bbox': [0.60, 0.80, 0.98, 0.99]},
            {'kind': 'bom_table', 'source_page': 1, 'bbox': [0.05, 0.60, 0.55, 0.95]},
            {'kind': 'view', 'source_page': 1, 'bbox': [0.0, 0.0, 1.0, 1.0]},
        ]
        entries = region_entries(images, regions)
        self.assertEqual([e['kind'] for e in entries], ['title_block', 'bom_table'])
        # 裁切块必须小于整页，否则等于没有裁切
        for entry in entries:
            self.assertLess(entry['image'].size[0] * entry['image'].size[1],
                            2000 * 2000)
        self.assertGreater(entries[1]['image'].size[0], entries[1]['image'].size[1] * 0.5)
        self.assertTrue(0.0 <= entries[0]['bbox'][0] and entries[0]['bbox'][2] <= 1.0)

    def test_degenerate_regions_are_rejected(self):
        from engine_bom.drawing_layout import region_entries, usable_bbox
        images = [Image.new('RGB', (2000, 2000))]
        bad = [
            {'kind': 'bom_table', 'source_page': 1, 'bbox': [0.5, 0.5, 0.2, 0.2]},   # 反向
            {'kind': 'bom_table', 'source_page': 1, 'bbox': [0.0, 0.0, 1.0, 1.0]},   # 整页=没裁切
            {'kind': 'bom_table', 'source_page': 9, 'bbox': [0.1, 0.1, 0.3, 0.3]},   # 页码越界
            {'kind': 'bom_table', 'source_page': 1, 'bbox': [-0.5, -0.5, 1.5, 1.5]}, # 出界
            {'kind': 'bom_table', 'source_page': 1, 'bbox': 'junk'},
            {'kind': 'bom_table'},
        ]
        self.assertEqual(region_entries(images, bad), [])
        self.assertFalse(usable_bbox([0.5, 0.5, 0.2, 0.2]))
        self.assertFalse(usable_bbox([0.0, 0.0, 1.0, 1.0]))
        self.assertFalse(usable_bbox([0.0, 0.0, 0.001, 0.5]))

    def test_legacy_kind_helpers_still_return_page_plus_crop(self):
        images = [Image.new('RGB', (100, 100))]
        regions = [{'kind': 'bom_table', 'source_page': 1, 'bbox': [0.1, 0.1, 0.5, 0.5]}]
        self.assertEqual(len(bom_table_images(images, regions)), 2)
        self.assertEqual(len(titleblock_images(images, regions)), 1)  # 无标题栏时退化为末页


class ReviewImageMappingTests(unittest.TestCase):
    def test_crops_follow_page_numbers_in_full_document_order(self):
        doc = [Image.new('RGB', (2000 + n, 2000)) for n in range(3)]
        regions = [
            {'kind': 'bom_table', 'source_page': 2, 'bbox': [0.05, 0.6, 0.55, 0.95]},
            {'kind': 'bom_table', 'source_page': 3, 'bbox': [0.05, 0.6, 0.55, 0.95]},
        ]
        observed, note = review_images([doc[1]], regions, doc)
        # 只裁当前页（第 2 页）的区域，不把第 3 页的裁切塞进来
        self.assertEqual(len(observed), 2)
        self.assertIn('"source_page": 2', note)
        self.assertNotIn('"source_page": 3', note)
        self.assertIn('裁切不是新页', note)

    def test_quadrant_fallback_when_nothing_located(self):
        images, note = review_images([Image.new('RGB', (100, 100))], None)
        self.assertEqual(len(images), 5)
        self.assertIn('裁切不是新页', note)


class PromptTests(unittest.TestCase):
    def test_v5_documents_crops_and_keeps_original_page_numbers(self):
        text = get_prompt_builder('v5')(mode='per_page', page_no=1, total_pages=1)
        self.assertIn('区域裁切图 v5', text)
        self.assertIn('禁止把裁切序号当页码', text)
        self.assertIn('裁切边缘待核实', text)
        # v5 必须向后兼容 v4 的保真规则
        self.assertIn('标准号保真 v4', text)
        self.assertIn('重量列 v4', text)

    def test_every_registered_prompt_version_builds(self):
        from engine_bom.prompts import PROMPTS
        for version in PROMPTS:
            text = get_prompt_builder(version)(mode='per_page', page_no=1, total_pages=1)
            self.assertIn('components', text)


class _SpyProvider(object):
    name = 'spy'

    def __init__(self):
        self.calls = []

    def analyze(self, images, prompt):
        self.calls.append((list(images), prompt))
        raw = json.dumps({'components': [
            {'item_no': 1, 'part_number': 'X1', 'name_zh': '轴', 'quantity': 2,
             'source_page': 1}]}, ensure_ascii=False)
        return ProviderResponse(raw, 'spy')

    def describe(self):
        return {'provider': 'spy'}

    def close(self):
        pass


class PipelineCropWiringTests(unittest.TestCase):
    LAYOUT = {'status': 'validated', 'events': [], 'regions': [
        {'kind': 'title_block', 'source_page': 1, 'bbox': [0.60, 0.80, 0.98, 0.99],
         'evidence': '右下角标题栏'},
        {'kind': 'bom_table', 'source_page': 1, 'bbox': [0.05, 0.30, 0.60, 0.80],
         'evidence': '左侧明细表'},
    ]}

    def _run(self, layout, recognition=None):
        cfg = load_config()
        cfg['knowledge']['enabled'] = False
        cfg['assembly']['enabled'] = False
        if recognition:
            cfg['recognition'].update(recognition)
        with tempfile.TemporaryDirectory() as folder:
            source = os.path.join(folder, 'crop-fixture.png')
            Image.new('RGB', (1600, 1200), 'white').save(source)
            cfg['output'].update(dir=folder, formats=['json'])
            pipeline = BomPipeline(cfg)
            spy = _SpyProvider()
            pipeline.provider = spy
            pipeline.agent = AgentLoop(spy, 1)
            with patch('engine_bom.drawing_layout.locate_regions', return_value=layout):
                result = pipeline.process_file(source)
        return pipeline, spy, result

    def test_located_regions_are_sent_alongside_the_full_page(self):
        pipeline, spy, result = self._run(self.LAYOUT)
        self.assertEqual(result.status, 'ok', result.errors)
        images, prompt = spy.calls[0]
        self.assertEqual(len(images), 3)  # 整页 + 标题栏 + 明细栏
        self.assertGreater(images[0].size[0], images[1].size[0])  # 裁切块更小
        self.assertIn('按版面定位出的标题栏/明细表区域', prompt)
        self.assertIn('区域裁切图 v5', prompt)
        self.assertEqual(pipeline._layout_cache['status'], 'validated')

    def test_layout_is_located_once_per_pipeline(self):
        sentinel = {'status': 'validated', 'events': [], 'regions': []}
        calls = []

        def fake(provider, images, template=None):
            calls.append(images)
            return sentinel

        cfg = load_config()
        cfg['knowledge']['enabled'] = False
        cfg['assembly']['enabled'] = False
        with tempfile.TemporaryDirectory() as folder:
            source = os.path.join(folder, 'cache-fixture.png')
            Image.new('RGB', (1600, 1200), 'white').save(source)
            cfg['output'].update(dir=folder, formats=['json'])
            pipeline = BomPipeline(cfg)
            spy = _SpyProvider()
            pipeline.provider = spy
            pipeline.agent = AgentLoop(spy, 1)
            with patch('engine_bom.drawing_layout.locate_regions', side_effect=fake):
                pipeline.process_file(source)
            self.assertEqual(len(calls), 1)

    def test_layout_failure_falls_back_to_quadrants_without_failing_bom(self):
        pipeline, spy, result = self._run(
            {'status': 'provider_failed', 'regions': [], 'events': []})
        self.assertEqual(result.status, 'ok', result.errors)
        images, prompt = spy.calls[0]
        self.assertEqual(len(images), 1 + 4)  # 逐页模式：当前页 + 四象限
        self.assertIn('附加放大图是原页的局部', prompt)

    def test_region_crops_switch_disables_layout_call(self):
        sentinel = {'status': 'validated', 'events': [], 'regions': [
            {'kind': 'bom_table', 'source_page': 1, 'bbox': [0.05, 0.3, 0.6, 0.8]}]}
        with patch('engine_bom.drawing_layout.locate_regions', return_value=sentinel) as located:
            pipeline, spy, result = self._run(self.LAYOUT, {'region_crops': False})
            self.assertFalse(located.called)
        self.assertEqual(pipeline._layout_cache['status'], 'disabled')
        # 区域开关关闭后仍保留原有四象限行为
        self.assertEqual(len(spy.calls[0][0]), 1 + 4)

    def test_detail_crops_switch_still_disables_everything(self):
        pipeline, spy, result = self._run(self.LAYOUT, {'detail_crops': False})
        self.assertEqual(len(spy.calls[0][0]), 1)  # 逐页模式：单页单图


if __name__ == '__main__':
    unittest.main()
