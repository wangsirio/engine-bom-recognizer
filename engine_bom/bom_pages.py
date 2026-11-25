"""Build Web BOM output scopes from authoritative, independently read PDF pages."""
from collections import defaultdict
from dataclasses import replace

from .drawing_layout import titleblock_images, titleblock_page_numbers
from .inspect_rules import check_bom_table, check_titleblock
from .schema import normalize_part_number
from .titleblock import TITLE_FIELDS

OUTPUT_MODES = ('merged', 'per_page')


def read_title(provider, images, page_numbers, page_count, template):
    from .titleblock import extract_titleblock
    result = extract_titleblock(provider, images, total_pages=page_count, source_page_numbers=page_numbers,
                                **({'template': template} if template else {}))
    fields = result['fields']
    data = {'title_block': fields, 'missing_required': result['missing_required']}
    if template:
        from .titleblock_template import apply_template
        data['enterprise_title_block'], data['title_block_template'] = apply_template(fields, template)
    fields.pop('enterprise_fields', None)
    return data


def has_title(data):
    fields = data['title_block']
    return (fields.get('extraction_status') != 'provider_failed'
            and (any(str(fields.get(key) or '').strip() for key in TITLE_FIELDS)
                 or any(value is not None for value in data.get('enterprise_title_block', {}).values())))


def build_paged_output(result, provider, images, layout, output_mode, title_template):
    regions = layout.get('regions') or []
    titles = defaultdict(list)
    table_pages = set()
    for region in regions:
        if region['kind'] == 'title_block':
            titles[region['source_page']].append(region)
        elif region['kind'] == 'bom_table':
            table_pages.add(region['source_page'])

    statuses = [dict(status) for status in result.page_statuses]
    blocks = {}
    grouped = defaultdict(list)
    for row in result.bom.components:
        grouped[row.source_page].append(row)
    page_results = []
    # Omit raw model traces from each page copy; the parent keeps them once.
    model_info = {k: v for k, v in result.bom.model_info.items() if k != 'agent_loop'}
    for status in statuses:
        page = status['page']
        if status['status'] == 'failed':
            continue
        if status['status'] == 'empty' and page in table_pages:
            status.update(status='needs_review', message='定位到疑似明细栏，但没有提取出明细行。')
        regions_on_page = titles[page]
        image_inputs = titleblock_images(images, regions_on_page) if regions_on_page else [images[page - 1]]
        image_pages = titleblock_page_numbers(images, regions_on_page) if regions_on_page else [page]
        header = read_title(provider, image_inputs, image_pages, len(images), title_template)
        blocks[str(page)] = header
        if output_mode == 'per_page':
            issues = check_titleblock(header['title_block']) if has_title(header) else []
            issues += check_bom_table([row.to_dict() for row in grouped[page]], sequence_start=None)
            page_bom = replace(result.bom, document_id=f'{result.document_id}-p{page:03d}', model_info=model_info,
                               components=grouped[page], assembly={}, process_time_s=None,
                               validation_issues=list(dict.fromkeys(reason for row in grouped[page] for reason in row.review_reasons)))
            page_results.append({'page': page, 'bom': page_bom.to_dict(), 'issues': issues, **header})

    unfinished = [s['page'] for s in statuses if s['status'] in ('failed', 'needs_review')]
    combined = {'output_mode': output_mode, 'document_id': result.document_id,
                'source_sha256': result.bom.model_info.get('source_sha256'), 'model_info': model_info,
                'process_time_s': result.process_time_s,
                'page_statuses': statuses, 'incomplete_pages': unfinished,
                'recognition_status': ('incomplete' if unfinished else 'complete'),
                'page_title_blocks': blocks}
    if output_mode == 'per_page':
        return {**combined, 'page_results': page_results}

    # A drawing number conflict is not resolved by guessing a main page.
    candidates = [block for block in blocks.values() if has_title(block)]
    drawing_numbers = {value.strip().casefold() for block in candidates
                       if isinstance(value := block['title_block'].get('drawing_no'), str) and value.strip()}
    header = {'title_block': {}, 'missing_required': []}
    if len(drawing_numbers) > 1:
        combined['title_block_notice'] = '各页识别到不同图号，未自动选主标题栏。企业导出中引用的标题栏值按每行来源页取值。'
    elif candidates:
        header = next((item for item in candidates if str(item['title_block'].get('drawing_no') or '').strip()), candidates[0])
    return {**combined, **header, 'bom': result.bom.to_dict()}


def duplicate_part_notes(components):
    pages_by_part = defaultdict(set)
    labels = {}
    for component in components:
        number = component.get('part_number')
        if key := normalize_part_number(number or ''):
            pages_by_part[key].add(component['source_page'])
            labels.setdefault(key, number)
    return [f'件号 {labels[key]} 在第{"、".join(map(str, sorted(pages)))}页均出现；保留各行与原数量，请核实是否重复印刷。'
            for key, pages in pages_by_part.items() if len(pages) > 1]
