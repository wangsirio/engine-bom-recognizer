const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const js = fs.readFileSync('static/app.js', 'utf8');
const nodes = new Map();
function find(key) {
  if (!nodes.has(key)) nodes.set(key, {value: '', classList: {toggle() {}}, addEventListener() {},
    replaceChildren(...options) { this.options = options; }, insertAdjacentHTML() {}});
  return nodes.get(key);
}
const context = vm.createContext({$: find, URLSearchParams,
  Option: function Option(text, value) { this.text = text; this.value = value; },
  fillTable: (_, headers, rows) => { context.pageRows = rows; },
  renderKV: (_, value) => { context.title = value; }, renderIssues() {},
  dethRenderBomTable: rows => rows.map(row => row.part_number).join(','),
});
vm.runInContext("let bomJobId = 'test-job', bomPageData;\n" +
  js.slice(js.indexOf('function bomStatusText('), js.indexOf('function renderBomResult(')), context);
const separate = {output_mode: 'per_page', page_statuses: [
  {page: 1, status: 'failed', rows: null}, {page: 2, status: 'recognized', rows: 1},
], incomplete_pages: [1], page_results: [{page: 2, title_block: {drawing_no: 'DWG-2'},
  bom: {components: [{part_number: 'P2'}], summary: {total_quantity: 1}}}]};
context.separate = separate;
vm.runInContext('bomPageData = separate; renderBomPaging(separate);', context);
assert.match(find('#bom-page-summary').textContent, /其他已识别的页面仍可单独下载/);
assert.match(context.pageRows[0][1], /不计为零行/);
assert.equal(find('#bom-result-page').value, '2');
assert.equal(context.title.drawing_no, 'DWG-2');
assert.equal(find('#bom-page-components').innerHTML, 'P2');
assert.equal(context.bomExportUrl('xlsx', true), '/api/export/bom/test-job?fmt=xlsx&profile=enterprise&page=2');
find('#bom-result-page').value = '1';
context.renderBomPage();
assert.equal(find('#bom-page-export').disabled, true);
assert.throws(() => context.bomExportUrl('json'), /第 1 页没有可导出/);
vm.runInContext("bomPageData = {output_mode: 'merged', incomplete_pages: [1]};", context);
assert.throws(() => context.bomExportUrl('json'), /暂停合并导出/);
console.log('PASS: page selection links, independent result viewer, failed page state and export guard');
