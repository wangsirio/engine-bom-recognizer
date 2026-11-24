/* Persistent enterprise title-block schema editor. */
(() => {
  'use strict';
  const panel = document.querySelector('#title-template-panel');
  if (!panel) return;
  const get = id => panel.querySelector('#title-template-' + id);
  const path = '/api/enterprise/titleblock-template';
  let sources = {}, fields = [], active = null, busy = true, ready = false;

  function el(tag, text, className) {
    const element = document.createElement(tag);
    if (text) element.textContent = text;
    if (className) element.className = className;
    return element;
  }

  function message(text, error = false) {
    get('message').textContent = text;
    get('message').className = error ? 'alert error' : 'hint';
  }

  function controls() {
    panel.querySelectorAll('button,input,select,textarea').forEach(node => { node.disabled = busy || !ready; });
    get('disable').disabled = busy || !ready || !active;
    get('state').textContent = !ready ? (busy ? '加载中' : '加载失败') : active ? '已启用：' + active.name : '未启用';
  }

  function preview() {
    get('preview').textContent = JSON.stringify(Object.fromEntries(fields
      .filter(field => field.key.trim()).map(field => [field.key.trim(), null])), null, 2);
  }

  function button(label, action) {
    const node = el('button', label);
    node.type = 'button';
    node.addEventListener('click', action);
    return node;
  }

  function input(label, value, limit, onEdit) {
    const wrapper = el('label', label);
    const node = el('input');
    node.value = value;
    node.maxLength = limit;
    node.addEventListener('input', () => { onEdit(node.value); preview(); });
    wrapper.append(node);
    return wrapper;
  }

  function render() {
    const container = get('rows');
    container.replaceChildren();
    if (!fields.length) container.append(el('p', '还没有输出键。可添加一行，或载入示例后修改。', 'hint'));
    fields.forEach((field, index) => {
      const row = el('fieldset', '', 'title-template-field');
      const key = input('输出键', field.key, 80, value => { field.key = value; });
      key.lastChild.placeholder = '例如：drawing_code';
      row.append(el('legend', `输出字段 ${index + 1}`), key);
      const picker = el('label', '按优先级添加系统字段');
      const select = el('select');
      select.append(new Option('选择一个来源…', ''));
      Object.entries(sources).filter(([key]) => !field.sources.includes(key)).forEach(([key, label]) => {
        select.append(new Option(`${label} · ${key}`, key));
      });
      select.addEventListener('change', () => {
        if (!select.value) return;
        field.sources.push(select.value);
        render();
        container.children[index].querySelector('select').focus();
      });
      picker.append(select);
      row.append(picker);
      const order = el('ol', '', 'title-template-order');
      field.sources.forEach((source, position) => {
        const item = el('li');
        const remove = button('移除', () => {
          field.sources.splice(position, 1);
          render();
          container.children[index].querySelector('select').focus();
        });
        remove.setAttribute('aria-label', `移除来源 ${sources[source]}`);
        item.append(el('span', `${position + 1}. ${sources[source]}`), remove);
        order.append(item);
      });
      if (field.sources.length) row.append(order);
      const aliases = el('label', '补充原图标签（每行一个，系统字段之后查找）');
      const aliasInput = el('textarea');
      aliasInput.rows = 2;
      aliasInput.maxLength = 610;
      aliasInput.value = field.aliasText;
      aliasInput.placeholder = '图样代号\nDrawing No.';
      aliasInput.addEventListener('input', () => { field.aliasText = aliasInput.value; });
      aliases.append(aliasInput);
      row.append(aliases);
      row.append(button('删除此输出键', () => { fields.splice(index, 1); render(); get('add').focus(); }));
      container.append(row);
    });
    preview();
    controls();
  }

  function edit(template) {
    get('name').value = template?.name || '';
    fields = (template?.fields || []).map(field => ({key: field.key, sources: [...(field.sources || [])],
      aliasText: (field.aliases || []).join('\n')}));
    render();
  }

  async function commit(readTemplate) {
    busy = true;
    controls();
    message('正在保存…');
    try {
      const template = await readTemplate();
      const result = await api(path, {method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({template})});
      active = result.template;
      edit(active);
      message(active ? '已保存；后续 BOM 任务自动使用，已完成的结果不变。' : '已停用；后续任务使用默认标题栏。');
    } catch (error) {
      message(error.message, true);
    } finally {
      busy = false;
      controls();
    }
  }

  get('add').onclick = () => {
    if (fields.length >= 50) return message('最多配置 50 个输出键。', true);
    fields.push({key: '', sources: [], aliasText: ''});
    render();
    get('rows').lastChild.querySelector('input').focus();
  };
  get('save').onclick = () => commit(() => ({name: get('name').value, fields: fields.map(field => ({
    key: field.key, sources: field.sources, aliases: field.aliasText.split(/\r?\n/).map(s => s.trim()).filter(Boolean),
  }))}));
  get('disable').onclick = () => { if (window.confirm('停用已保存的标题栏模板？')) commit(() => null); };
  get('import').onclick = () => {
    const file = get('file').files[0];
    if (!file) return message('请先选择 JSON 模板文件。', true);
    if (file.size > 1024 * 1024) return message('模板文件不得超过 1 MB。', true);
    commit(async () => {
      let template;
      try { template = JSON.parse(await file.text()); }
      catch (_) { throw new Error('模板文件不是有效的 JSON。'); }
      if (!template || typeof template !== 'object' || Array.isArray(template)) throw new Error('模板文件须为 JSON 对象。');
      return template;
    });
  };
  get('example').onclick = async () => {
    busy = true;
    controls();
    try {
      edit(await api('/static/titleblock-template.example.json'));
      message('示例已载入编辑器，保存后才会启用。');
    } catch (error) { message(error.message, true); }
    finally { busy = false; controls(); }
  };

  controls();
  api(path).then(result => {
    sources = result.sources;
    active = result.template;
    ready = true;
    Object.entries(sources).forEach(([key, label]) => {
      const item = el('li', label + ' · ');
      item.append(el('code', key));
      get('sources').append(item);
    });
    edit(active);
  }).catch(error => message('加载模板失败：' + error.message, true))
    .finally(() => { busy = false; controls(); });
})();
