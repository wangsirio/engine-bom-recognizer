// Validate the editor's select lists and preset actions without a live model.
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');

class Node {
  constructor(tag) {
    this.tag = tag;
    this.children = [];
    this.value = '';
    this.attributes = {};
    this.classes = new Set();
    this.classList = {toggle: (name, enabled) => enabled ? this.classes.add(name) : this.classes.delete(name)};
  }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; }
  setAttribute(name, value) { this.attributes[name] = value; }
  focus() { this.focused = true; }
  click() {}
  get lastChild() { return this.children.at(-1); }
  get options() { return this.children; }
  nodes(tag) { return this.children.flatMap(n => [...(n.tag === tag ? [n] : []), ...n.nodes(tag)]); }
}
const field = (output, key) => ({output, sources: [{type: 'system', key}]});
const common = [field('图名', 'drawing_name'), field('图纸材料', 'material')];
const signoff = [field('设计', 'designer')];

async function withTemplate(titles, suggest) {
  const items = new Map();
  const get = id => {
    if (!items.has(id)) items.set(id, Object.assign(new Node('control'), {id: `output-template-${id}`}));
    return items.get(id);
  };
  const panel = {querySelector: id => get(id.replace('#output-template-', '')), querySelectorAll: () => []};
  const writes = [], downloads = [];
  const state = {title_sources: {drawing_name: '图名', material: '图纸材料', designer: '设计'},
    bom_sources: {part_number: '零件件号', material: '零件材料'}, title_presets: {common, signoff},
    suggest_title_presets: suggest, template: {name: '模拟模板', title_fields: titles, bom_columns: [field('件号', 'part_number')]}};
  vm.runInNewContext(fs.readFileSync('static/bom_output_template.js', 'utf8'), {
    document: {querySelector: () => panel, createElement: tag => new Node(tag), addEventListener() {}},
    Option: function Option(text, value) { this.textContent = text; this.value = value; this.nodes = () => []; },
    Blob,
    URL: {createObjectURL: blob => { downloads.push(blob); return 'blob:template'; }, revokeObjectURL() {}},
    setTimeout: callback => callback(),
    api: async (_, opts) => {
      if (!opts) return state;
      const template = JSON.parse(opts.body);
      writes.push(template);
      return {template};
    },
  });
  await new Promise(resolve => setImmediate(resolve));
  return {get, writes, downloads};
}

(async () => {
  const {get, writes} = await withTemplate([], true);
  assert.equal(get('title-presets').classes.has('hidden'), true);
  const systemOptions = () => get('rows').nodes('select')[1].options.map(option => option.textContent);
  assert.ok(systemOptions().includes('零件材料'));
  assert.ok(!systemOptions().includes('图纸材料'));
  get('title-tab').onclick();
  assert.equal(get('title-presets').classes.has('hidden'), false);
  assert.ok(systemOptions().includes('图纸材料'));
  assert.ok(!systemOptions().includes('零件材料'));
  assert.deepEqual(Object.keys(JSON.parse(get('preview').textContent).title_block), ['图名', '图纸材料']);
  get('preset-signoff').onclick();
  get('preset-common').onclick();
  assert.equal(writes.length, 0);
  await get('save').onclick();
  assert.deepEqual(writes[0].title_fields.map(f => f.output), ['图名', '图纸材料', '设计']);

  const existing = await withTemplate([field('自定义图名', 'drawing_name'), field('图纸材料', 'designer')], false);
  existing.get('title-tab').onclick();
  existing.get('preset-common').onclick();
  await existing.get('save').onclick();
  assert.deepEqual(existing.writes[0].title_fields, [field('自定义图名', 'drawing_name'), field('图纸材料', 'designer')]);

  // A blank backup field blocks both actions and reveals its input on the other tab.
  const invalid = await withTemplate([{output: '图名', sources: [
    {type: 'system', key: 'drawing_name'}, {type: 'system', key: ''},
  ]}], false);
  assert.equal(invalid.get('bom-tab').attributes['aria-pressed'], 'true');
  assert.equal(invalid.get('download').onclick(), false);
  assert.equal(invalid.get('save').onclick(), false);
  assert.equal(invalid.writes.length, 0);
  assert.equal(invalid.get('title-tab').attributes['aria-pressed'], 'true');
  const [_, primary, __, backup] = invalid.get('rows').nodes('select');
  assert.equal(primary.attributes['aria-invalid'], 'false');
  assert.equal(backup.attributes['aria-invalid'], 'true');
  assert.equal(backup.focused, true);
  assert.ok(invalid.get('rows').nodes('small').some(hint => hint.textContent === '请选择具体的标题栏系统字段。'));
  assert.match(invalid.get('title-tab').textContent, /1 处待修正/);

  // If a correction creates a duplicate, mark both inputs; a valid fix clears both.
  backup.value = 'drawing_name';
  backup.onchange();
  assert.equal(primary.attributes['aria-invalid'], 'true');
  assert.equal(backup.attributes['aria-invalid'], 'true');
  backup.value = 'material';
  backup.onchange();
  assert.equal(primary.attributes['aria-invalid'], 'false');
  assert.equal(backup.attributes['aria-invalid'], 'false');
  assert.ok(invalid.get('validation-summary').classes.has('hidden'));
  await invalid.get('save').onclick();
  assert.equal(invalid.writes[0].title_fields[0].sources[1].key, 'material');

  const fresh = await withTemplate([], true);
  await fresh.get('download').onclick();
  fresh.get('title-tab').onclick();
  await fresh.get('download').onclick();
  const [fromBomTab, fromTitleTab] = await Promise.all(fresh.downloads.map(blob => blob.text()));
  assert.equal(fromBomTab, fromTitleTab, 'Switching tabs must not alter even one byte of the download');
  const combined = JSON.parse(fromBomTab);
  assert.deepEqual(combined.title_fields, common);
  assert.deepEqual(combined.bom_columns, [field('件号', 'part_number')]);
  assert.match(fresh.get('message').textContent, /图纸标题栏 2 项、零件明细 1 列/);

  const empty = await withTemplate([], false);
  empty.get('title-tab').onclick();
  await empty.get('download').onclick();
  assert.deepEqual(JSON.parse(await empty.downloads[0].text()).title_fields, [], 'An explicitly empty title section stays empty');
  console.log('PASS: scoped fields, validation, and byte-identical combined downloads from either tab');
})().catch(error => { console.error(error); process.exitCode = 1; });
