"""One customer-facing output template, adapted to the existing extraction formats."""
from hashlib import sha256

from .enterprise import export_field_mapping, validate_profile
from .export import CSV_HEADERS
from .titleblock_template import SOURCE_LABELS, validate_template

DEFAULT_NAME = '企业 BOM 输出模板'

# Editor labels are scoped; this doesn't rename an existing template or CSV column.
TITLE_SYSTEM_FIELDS = {**SOURCE_LABELS, 'drawing_no': '图纸图号', 'material': '图纸材料',
                       'weight': '图纸标注重量', 'title_no_field': '标题栏序号（原图明确标注）'}
BOM_SYSTEM_FIELDS = {**CSV_HEADERS, 'item_no': '零件序号', 'part_number': '零件件号／零件图号',
                     'material': '零件材料', 'weight': '零件单重(g)'}


def title_field_presets():
    groups = {
        'common': [('drawing_name', '图名'), ('drawing_no', '图号'), ('revision', '版本'),
                   ('material', '图纸材料'), ('scale', '比例'), ('weight', '图纸标注重量'), ('date', '日期')],
        'signoff': [('designer', '设计'), ('checker', '校核／审核'), ('approver', '批准')],
    }
    return {name: [{'output': output, 'sources': [{'type': 'system', 'key': key}]} for key, output in fields]
            for name, fields in groups.items()}


def from_legacy(profile, title, name=None):
    titles = []
    for field in (title or {}).get('fields', []):
        sources = [{'type': 'system', 'key': key} for key in field.get('sources', [])]
        if field.get('aliases'):
            sources.append({'type': 'drawing', 'labels': field['aliases']})
        titles.append({'output': field['key'], 'sources': sources})
    columns = []
    for raw, output in export_field_mapping(profile).items():
        scope, _, key = raw.partition('.')
        if scope == 'detail':
            source = {'type': 'drawing', 'key': key, 'labels': profile['detail_fields'][key]}
        elif scope == 'constant':
            source = {'type': 'fixed', 'key': key, 'value': profile['constant_fields'][key]}
        elif scope == 'title_block':
            source = {'type': 'title', 'key': key}
        else:
            source = {'type': 'system', 'key': raw}
        columns.append({'output': output.strip(), 'sources': [source]})
    return {'schema_version': 1, 'name': name or (title or {}).get('name') or DEFAULT_NAME,
            'title_fields': titles, 'bom_columns': columns}


def _text(value, label, limit):
    if not isinstance(value, str) or not 0 < len(value.strip()) <= limit or any(ord(c) < 32 for c in value):
        raise ValueError(f'{label}须为非空单行文本，最多 {limit} 字')
    return value.strip()


def _source(source, allowed):
    if not isinstance(source, dict) or source.get('type') not in allowed:
        raise ValueError('取值来源不适用于此字段')
    return source['type']


def to_legacy(template):
    if (not isinstance(template, dict) or set(template) != {'schema_version', 'name', 'title_fields', 'bom_columns'}
            or type(template['schema_version']) is not int or template['schema_version'] != 1):
        raise ValueError('请使用版本为 1 的企业 BOM 输出模板')
    name = _text(template['name'], '模板名称', 80)
    titles, columns = template['title_fields'], template['bom_columns']
    if not isinstance(titles, list) or len(titles) > 50:
        raise ValueError('整张图纸最多配置 50 个标题栏字段')
    if not isinstance(columns, list) or not 1 <= len(columns) <= 100:
        raise ValueError('零件明细须配置 1–100 个导出列')

    def field(row, limit):
        if not isinstance(row, dict) or set(row) != {'output', 'sources'} or not isinstance(row['sources'], list):
            raise ValueError('每个字段都须包含 output（输出名称）和 sources（取值来源数组）')
        return _text(row['output'], '输出名称', limit), row['sources']

    title_fields = []
    for row in titles:
        output, sources = field(row, 80)
        systems, labels = [], []
        for source in sources:
            kind = _source(source, ('system', 'drawing'))
            if kind == 'system':
                key = _text(source.get('key'), '标题栏来源字段', 80)
                if key not in TITLE_SYSTEM_FIELDS:
                    raise ValueError(f'「{output}」引用了未知的标题栏系统字段：{key}')
                systems.append(key)
            else:
                values = source.get('labels')
                if not isinstance(values, list) or not values:
                    raise ValueError(f'{output} 至少填写一个原图标题栏标签')
                labels.extend(values)
        title_fields.append({'key': output, 'sources': systems, 'aliases': labels})
    title = validate_template({'name': name, 'fields': title_fields}) if title_fields else None

    profile = {'export_fields': {}, 'detail_fields': {}, 'constant_fields': {}}
    for row in columns:
        output, sources = field(row, 120)
        if len(sources) != 1:
            raise ValueError(f'{output} 须指定一个取值来源')
        source = sources[0]
        kind = _source(source, ('system', 'title', 'drawing', 'fixed'))
        if kind in ('system', 'title'):
            key = _text(source.get('key'), '来源字段', 80)
            if kind == 'system' and key not in BOM_SYSTEM_FIELDS:
                raise ValueError(f'「{output}」引用了未知的明细栏系统字段：{key}；引用整张图的信息请选「引用图纸标题栏」')
            path = key if kind == 'system' else 'title_block.' + key
        else:
            # Use the displayed name for new custom fields, but preserve migrated IDs
            # so projects that already extracted values can still export them.
            if 'key' in source:
                key = _text(source['key'], '来源字段', 80)
            else:
                key = output if len(output) <= 80 else 'field_' + sha256(output.encode()).hexdigest()[:16]
            path = ('detail.' if kind == 'drawing' else 'constant.') + key
            if kind == 'drawing':
                profile['detail_fields'][key] = source.get('labels')
            else:
                if 'value' not in source:
                    raise ValueError(f'{output} 的固定值须填写文本、数字、布尔值或 null')
                profile['constant_fields'][key] = source['value']
        if path in profile['export_fields']:
            raise ValueError(f'零件明细不能重复引用同一个来源：{output}')
        profile['export_fields'][path] = output
    return name, validate_profile(profile), title


def import_template(data, profile, title, name=None):
    """Read new templates and the two historical output formats without writing."""
    if not isinstance(data, dict):
        raise ValueError('模板文件须为 JSON 对象')
    notice = ''
    if 'schema_version' in data or 'bom_columns' in data or 'title_fields' in data:
        template = data
    elif 'fields' in data:
        template = from_legacy(profile, validate_template(data))
        notice = '已转换旧版标题栏模板，保留当前零件列。'
    elif 'export_fields' in data:
        template = from_legacy(validate_profile(data), title, name)
        notice = '已转换旧版企业导出列，保留当前标题栏；文件里的审图规则和参数词典不会随输出模板导入。'
    else:
        raise ValueError('不是可识别的 BOM 输出模板或旧版字段配置')
    name, fields, title = to_legacy(template)
    return from_legacy(fields, title, name), notice
