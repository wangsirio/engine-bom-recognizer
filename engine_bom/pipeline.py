# -*- coding: utf-8 -*-
"""端到端流水线：输入 PDF/图片 -> 页面图像 -> 视觉模型识别 -> 解析/合并/校验 -> BOM 导出。

流程对应论文的四步闭环：
  Step1 文档解析结构化（ingestion）
  Step2 视觉模型识别（providers，可插拔；可选 RAG 知识注入 knowledge）
  Step3 解析与多层校验（parsing + schema.validate_components，含件号冲突检测）
  Step4 BOM 装配与导出（schema.merge_components + export）
"""
import os
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from .config import cfg_get, resolve_output_dir
from .enterprise import configured_detail_fields, detail_read_prompt, read_detail_values
from .export import export_bom
from .ingestion import IngestionError, load_document
from .knowledge import build_knowledge
from .parsing import response_to_components
from .prompts import get_prompt_builder
from .providers import ProviderError, build_provider
from .schema import BomComponent, BomDocument, merge_components, validate_components
from .agent_loop import AgentLoop, validate_bom_response
from .corrections import sha256


@dataclass
class ProcessResult(object):
    document_id: str
    source_file: str
    status: str                       # "ok" | "failed"
    bom: Optional[BomDocument] = None
    errors: List[str] = field(default_factory=list)
    outputs: List[str] = field(default_factory=list)
    process_time_s: float = 0.0
    agent_events: List[Dict[str, Any]] = field(default_factory=list)
    pages: List[Any] = field(default_factory=list, repr=False)
    page_statuses: List[Dict[str, Any]] = field(default_factory=list)


class BomPipeline(object):
    """识别流水线。config 为 engine_bom.config.load_config 的产物。"""

    def __init__(self, config: Dict[str, Any], base_dir: Optional[str] = None):
        self.config = config
        self.base_dir = base_dir or os.getcwd()
        self.provider = build_provider(config.get("provider") or {})
        self.kb = build_knowledge(config.get("knowledge") or {}, self.base_dir)
        self.prompt_builder = get_prompt_builder(str(cfg_get(config, "recognition.prompt_version", "v1")))
        self.agent = AgentLoop(self.provider, cfg_get(config, "agent_loop.max_attempts", 3))
        self.detail_fields = configured_detail_fields(config.get('enterprise') or {})
        self._layout_cache: Optional[Dict[str, Any]] = None
        self._layout_images = ()

    def layout(self, images: List[Any]) -> Dict[str, Any]:
        """Locate drawing regions once per document; reused by crops and title block.

        A failed or disabled lookup is cached too, so a provider outage does not
        cost one extra model call per stage.
        """
        if (self._layout_cache is not None and len(self._layout_images) == len(images)
                and all(a is b for a, b in zip(self._layout_images, images))):
            return self._layout_cache
        self._layout_images = tuple(images)
        from .drawing_layout import locate_regions
        enabled = bool(cfg_get(self.config, "recognition.region_crops", True))
        if not enabled:
            self._layout_cache = {'status': 'disabled', 'regions': [], 'events': [],
                                   'warning': '配置已关闭区域裁切（recognition.region_crops）'}
            return self._layout_cache
        template = (self.config.get('enterprise') or {}).get('drawing_template')
        self._layout_cache = locate_regions(self.provider, images, template)
        return self._layout_cache

    def _recognize(self, images, prompt, result, page_no, doc_images=None):
        """Run one recognition call, prepending located title-block/parts-table crops.

        `doc_images` is the full document page list; region coordinates are page
        numbers in that list, while `images` may be a single page (per_page mode).
        Small pages stay single-image: below the detail threshold the whole page is
        already legible, and a layout call would only add latency.
        """
        if cfg_get(self.config, 'recognition.detail_crops', True):
            threshold = int(cfg_get(self.config, 'recognition.detail_crop_min_side', 1200) or 0)
            if any(max(image.size) > threshold for image in images):
                source = list(doc_images) if doc_images is not None else list(images)
                located = self.layout(source)
                regions = located.get('regions') or None
                from .installation_review import review_images
                images, note = review_images(images, regions, source)
                if regions:
                    prompt += ('\n【明细表原文核对】附加裁切图是按版面定位出的标题栏/明细表区域，'
                               '不是新页；请逐行核对序号、名称及数量。不得用相似功能名称或轴系常识'
                               '替换明细表原文，不得重复计数。')
                else:
                    prompt += ('\n【明细表原文核对】附加放大图是原页的局部，不是新页；请逐行核对'
                               '序号、名称及数量。不得用相似功能名称或轴系常识替换明细表原文，不得重复计数。')
                prompt += note
        outcome = self.agent.run(images, prompt + detail_read_prompt(self.detail_fields), validate_bom_response)
        result.agent_events.extend(dict(event, page_no=page_no) for event in outcome.events)
        if outcome.status != "validated":
            raise ProviderError("Agent loop 校验未通过，需复核：%s" % outcome.events[-1]["errors"])
        for component in outcome.value:
            component.detail_values = read_detail_values(component._detail_reads, self.detail_fields)
        return outcome.response, outcome.value

    # ------------------------------------------------------------------
    def process_file(self, path: str, retain_pages: bool = False, paged_output: bool = False) -> ProcessResult:
        self._layout_cache, self._layout_images = None, ()
        path = os.path.abspath(path)
        doc_id = os.path.splitext(os.path.basename(path))[0]
        started = time.time()
        result = ProcessResult(document_id=doc_id, source_file=path, status="failed")

        try:
            pages = load_document(path, self.config.get("ingestion") or {})
        except IngestionError as exc:
            result.errors.append(str(exc))
            return result
        if retain_pages:
            result.pages = pages

        mode = "per_page" if paged_output else str(cfg_get(self.config, "recognition.mode", "per_page"))
        doc_images = [getattr(p, 'original_image', None) or p.image for p in pages]
        trace_pages: List[Dict[str, Any]] = []
        pages_of_components: List[List[BomComponent]] = []
        try:
            if mode == "whole_doc":
                snippets = self.kb.retrieve(
                    "发动机 元器件 明细 BOM 图号 件号 数量 重量 单量 weight 材料 规格",
                    int(cfg_get(self.config, "knowledge.top_k", 3)))
                prompt = self.prompt_builder(mode="whole_doc", total_pages=len(pages),
                                             knowledge_snippets=snippets)
                resp, comps = self._recognize(doc_images, prompt, result, 0, doc_images)
                for c in comps:
                    if c.source_page is None:
                        c.source_page = 1
                pages_of_components.append(comps)
                trace_pages.append(self._trace_entry(0, resp, len(comps), []))
            else:
                snippets = self.kb.retrieve(
                    "发动机 元器件 明细 BOM 图号 件号 数量 重量 单量 weight 材料 规格",
                    int(cfg_get(self.config, "knowledge.top_k", 3)))
                for page in pages:
                    prompt = self.prompt_builder(mode="per_page", page_no=page.page_no,
                                                 total_pages=len(pages), knowledge_snippets=snippets)
                    if paged_output:
                        prompt += ('\n【按原页提取】逐表、逐行抄录当前页所有明细栏，保留原序号和数量。'
                                   '只输出真实明细行；如果本页没有明细行，返回空 components。'
                                   '不要用图中球标或其他页的内容补造明细行。')
                    try:
                        resp, comps = self._recognize([doc_images[page.page_no - 1]], prompt, result,
                                                      page.page_no, doc_images)
                    except ProviderError as exc:
                        if not paged_output:
                            raise
                        result.page_statuses.append({'page': page.page_no, 'status': 'failed', 'rows': None,
                                                     'message': str(exc)[:500]})
                        continue
                    for c in comps:
                        c.source_page = page.page_no
                    pages_of_components.append(comps)
                    trace_pages.append(self._trace_entry(page.page_no, resp, len(comps), []))
                    if paged_output:
                        result.page_statuses.append({'page': page.page_no, 'status': 'recognized' if comps else 'empty',
                                                     'rows': len(comps)})
        except ProviderError as exc:
            result.errors.append(str(exc))
            result.process_time_s = time.time() - started
            import json
            failed_dir = os.path.join(resolve_output_dir(self.config), "traces")
            os.makedirs(failed_dir, exist_ok=True)
            with open(os.path.join(failed_dir, doc_id + ".failed.json"), "w", encoding="utf-8") as f:
                json.dump({"status": "failed", "errors": result.errors,
                           "agent_events": result.agent_events}, f, ensure_ascii=False, indent=2)
            return result

        policy = str(cfg_get(self.config, "recognition.duplicate_quantity_policy", "keep_max"))
        preserve_rows = paged_output or bool(cfg_get(self.config, "recognition.preserve_printed_rows", True))
        if preserve_rows:
            merged = [c for page_comps in pages_of_components for c in page_comps]
            merge_notes = []
        else:
            merged, merge_notes = merge_components(pages_of_components, quantity_policy=policy)

        min_conf = float(cfg_get(self.config, "recognition.min_confidence", 0.0) or 0.0)
        if min_conf > 0 and not paged_output:
            kept, dropped = [], []
            for c in merged:
                (dropped if (c.confidence is not None and c.confidence < min_conf) else kept).append(c)
            for c in dropped:
                merge_notes.append("置信度低于阈值已过滤: %s (%.2f)" % (c.display_name(), c.confidence or 0))
            merged = kept

        strict = bool(cfg_get(self.config, "validation.strict", False))
        if paged_output:
            # Start with page-local findings. The Web layer builds the chosen output scope.
            issues = [f'第{page[0].source_page}页：{message}' for page in pages_of_components if page
                      for message in validate_components(page, allow_repeated_rows=True)]
        else:
            issues = validate_components(merged, strict=strict, allow_repeated_rows=preserve_rows)

        bom = BomDocument(
            document_id=doc_id,
            source_file=os.path.basename(path),
            generated_at=datetime.now().isoformat(timespec="seconds"),
            model_info={
                **self.provider.describe(),
                "prompt_version": str(cfg_get(self.config, "recognition.prompt_version", "v1")),
                "recognition_mode": mode,
                "preserve_printed_rows": preserve_rows,
                **({'enterprise_detail_fields': list(self.detail_fields)} if self.detail_fields else {}),
                "knowledge_base": self.kb.describe(),
                "pages": len(pages),
                "source_sha256": sha256(path),
                "agent_loop": {"status": "partial" if any(p['status'] == 'failed' for p in result.page_statuses) else "validated",
                               "events": result.agent_events},
            },
            components=merged,
            validation_issues=sorted(set(merge_notes + issues)),
            process_time_s=round(time.time() - started, 3),
        )

        if paged_output:
            # No ambiguous aggregate artifact on disk: the chosen scope controls Web exports.
            self._write_trace(self.base_dir, doc_id, bom, trace_pages, [])
            result.status, result.bom, result.process_time_s = 'ok', bom, bom.process_time_s or 0.0
            return result

        out_dir = resolve_output_dir(self.config)
        from .assembly import infer_assembly
        bom.assembly = infer_assembly(self.provider, [getattr(p,'original_image',None) or p.image for p in pages],
                                       [c.to_dict() for c in merged], self.config.get('assembly'))
        for _w in (bom.assembly.get('warnings') or []):
            bom.validation_issues.append('装配层级：' + _w)
        from .assembly import update_quantity_checks
        checked = {'assembly': bom.assembly, 'components': [c.to_dict() for c in merged],
                   'validation_issues': bom.validation_issues}
        update_quantity_checks(checked)
        bom.validation_issues = checked['validation_issues']
        formats = [str(x) for x in cfg_get(self.config, "output.formats", ["json"]) or []]
        result.outputs = export_bom(bom, os.path.join(out_dir, doc_id), formats)
        self._write_trace(out_dir, doc_id, bom, trace_pages, result.outputs)

        result.status = "ok"
        result.bom = bom
        result.process_time_s = bom.process_time_s or 0.0
        return result

    # ------------------------------------------------------------------
    def process_many(self, paths: List[str],
                     on_result: Optional[Callable[[ProcessResult], None]] = None) -> List[ProcessResult]:
        results: List[ProcessResult] = []
        for path in paths:
            try:
                res = self.process_file(path)
            except Exception as exc:  # 单文件崩溃不影响批量
                res = ProcessResult(document_id=os.path.splitext(os.path.basename(path))[0],
                                    source_file=path, status="failed", errors=["未捕获异常: %r" % exc])
            results.append(res)
            if on_result:
                on_result(res)
        return results

    # ------------------------------------------------------------------
    @staticmethod
    def _trace_entry(page_no: int, resp, n_components: int, warnings: List[str]) -> Dict[str, Any]:
        return {
            "page_no": page_no,
            "model": resp.model,
            "latency_s": round(resp.latency_s, 3),
            "usage": resp.usage,
            "n_components": n_components,
            "warnings": warnings,
            "raw_text": resp.raw_text,
        }

    @staticmethod
    def _write_trace(out_dir: str, doc_id: str, bom: BomDocument,
                     trace_pages: List[Dict[str, Any]], outputs: List[str]) -> None:
        """保存原始模型输出，便于调试与人工复核（对应论文 human-in-the-loop 的审计日志）。"""
        trace_dir = os.path.join(out_dir, "traces")
        if not os.path.isdir(trace_dir):
            os.makedirs(trace_dir, exist_ok=True)
        trace = {
            "document_id": doc_id,
            "source_file": bom.source_file,
            "generated_at": bom.generated_at,
            "model_info": bom.model_info,
            "summary": bom.summary(),
            "validation_issues": bom.validation_issues,
            "outputs": outputs,
            "pages": trace_pages,
        }
        import json
        path = os.path.join(trace_dir, "%s.trace.json" % doc_id)
        try:
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                json.dump(trace, f, ensure_ascii=False, indent=2)
        except OSError as exc:
            print("[warn] trace 写入失败 %s: %s" % (path, exc))


def print_result_summary(res: ProcessResult) -> None:
    """控制台打印单文档处理结果。"""
    if res.status != "ok":
        print("[FAIL] %s -> %s" % (res.document_id, "; ".join(res.errors)))
        return
    bom = res.bom
    print("[OK] %s: %d 个元器件, 总数量 %d, 耗时 %.1fs" % (
        res.document_id, bom.total_components, bom.total_quantity, res.process_time_s))
    for c in bom.components:
        conf = "" if c.confidence is None else " (%.2f)" % c.confidence
        print("  %2d. %-28s %-14s x%-4s %s" % (
            c.item_no or 0, c.display_name(), c.part_number or "-",
            str(c.quantity if c.quantity is not None else "?"), (c.material or "") + conf))
    if bom.validation_issues:
        print("  校验提示: %d 条（详见 JSON）" % len(bom.validation_issues))
    for out in res.outputs:
        print("  导出: %s" % out)
