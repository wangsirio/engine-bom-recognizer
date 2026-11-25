/* One editor for whole-drawing metadata and per-part export columns. */
(() => {
  'use strict';
  const panel = document.querySelector('#enterprise-panel');
  if (!panel) return;
  const get = id => panel.querySelector('#output-template-' + id);
  const endpoint = '/api/enterprise/bom-output-template';
  const example = '/static/bom-output-template.example.json';
  const typeLabel = {system: '使用明细栏系统字段', drawing: '读取原图明细栏自定义列',
    title: '引用图纸标题栏', fixed: '填写固定值或留空'};
  const titleTypeLabel = {system: '使用标题栏系统字段', drawing: '读取原图标题栏自定义标签'};
  let state, draft, section = 'bom_columns', busy = false, dirty = false;
  let validationActive = false;
  const errorTargets = new Map();

  function element(tag, text, className) {
    const node = document.createElement(tag);
    if (text) node.textContent = text;
    if (className) node.className = className;
    return node;
  }

  function message(text, error = false) {
    const node = get('message');
    node.textContent = text;
    node.className = error ? 'alert error' : 'hint';
  }

  function controls() {
    panel.querySelectorAll('[id^="output-template-"]:is(input,button), #output-template-rows :is(input,textarea,select,button)')
      .forEach(node => {
        const viewOnly = [get('bom-tab'), get('title-tab'), get('download'), get('reload')].includes(node);
        node.disabled = busy || (!state && node !== get('reload')) || Boolean(state?.read_only && !viewOnly);
      });
    get('state').textContent = busy ? '处理中' : !state ? '未加载' : state.read_only ? '只读' : dirty ? '有未保存修改' : '已保存／当前生效';
  }

  function preview() {
    if (!draft) return;
    const fields = draft.title_fields.filter(f => f.output.trim()).map(f => [f.output.trim(), null]);
    const row = draft.bom_columns.filter(f => f.output.trim()).map(f => {
      const source = f.sources[0];
      return [f.output.trim(), source?.type === 'fixed' ? source.value : null];
    });
    get('preview').textContent = JSON.stringify({...(fields.length ? {title_block: Object.fromEntries(fields)} : {}),
      rows: [Object.fromEntries(row)]}, null, 2);
  }

  function draftErrors() {
    const errors = new Map();
    const add = (path, text) => { if (!errors.has(path)) errors.set(path, text); };
    const validText = (text, limit) => text?.trim() && [...text.trim()].length <= limit && !/[\x00-\x1f]/.test(text);
    const normalizedLabel = text => text.normalize('NFKC').toLowerCase().replace(/\s+/g, '').replace(/:+$/, '');
    function unique(seen, value, path, text) {
      if (seen.has(value)) {
        add(seen.get(value), text);
        add(path, text);
      } else {
        seen.set(value, path);
      }
    }
    if (!validText(draft.name, 80)) add('name', '请填写模板名称（最多 80 字）。');
    for (const scope of ['title_fields', 'bom_columns']) {
      const title = scope === 'title_fields';
      const outputs = new Map(), columnSources = new Map();
      if (!title && !draft[scope].length) add(scope, '请至少添加一个零件明细字段。');
      draft[scope].forEach((field, index) => {
        const prefix = `${scope}.${index}`;
        const output = field.output.trim(), limit = title ? 80 : 120;
        if (!validText(field.output, limit)) add(`${prefix}.output`, `请填写输出名称（最多 ${limit} 字）。`);
        else unique(outputs, output, `${prefix}.output`, '输出名称重复，请换一个名称。');

        const systems = new Map(), originalLabels = new Map();
        let totalLabels = 0;
        field.sources.forEach((source, index) => {
          const path = `${prefix}.source.${index}`;
          if (source.type === 'system') {
            const catalog = title ? state.title_sources : state.bom_sources;
            if (!Object.hasOwn(catalog, source.key)) add(path, `请选择具体的${title ? '标题栏' : '明细栏'}系统字段。`);
            else if (title) unique(systems, source.key, path, '同一个系统字段不能重复添加。');
          } else if (source.type === 'title' && !validText(source.key, 80)) {
            add(path, '请选择要引用的标题栏输出名称。');
          } else if (source.type === 'drawing') {
            const labels = source.labels.map(label => label.trim()).filter(Boolean);
            const labelName = title ? '原图标题栏标签' : '原图明细表列名';
            if (!labels.length) add(path, `请至少填写一个${labelName}。`);
            totalLabels += labels.length;
            if (totalLabels > 10) add(path, `同一个字段最多可填写 10 个${labelName}。`);
            for (const label of labels) {
              const normalized = normalizedLabel(label);
              if (!validText(label, 60) || !normalized) add(path, `请填写有效的${labelName}（每项最多 60 字）。`);
              else unique(originalLabels, normalized, path, `${labelName}重复，请删除重复项。`);
            }
          }
          if (!title) {
            const key = source.key?.trim() || (['drawing', 'fixed'].includes(source.type) ? output : '');
            if (key) unique(columnSources, `${source.type}.${key}`, path, '同一个来源不能重复用作明细列。');
          }
        });
      });
    }
    return errors;
  }

  function registerError(path, control, hint) {
    if (!hint) {
      hint = element('small', '', 'output-template-error hidden');
      hint.id = `output-template-error-${path.replaceAll('.', '-')}`;
    }
    control.setAttribute('aria-describedby', hint.id);
    errorTargets.set(path, {control, hint});
    return hint;
  }

  function showValidation(errors = validationActive && draft ? draftErrors() : new Map()) {
    for (const [path, {control, hint}] of errorTargets) {
      const error = errors.get(path);
      control.setAttribute('aria-invalid', String(Boolean(error)));
      hint.textContent = error || '';
      hint.classList.toggle('hidden', !error);
    }
    for (const [tab, scope, label] of [['title-tab', 'title_fields', '图纸标题栏'], ['bom-tab', 'bom_columns', '零件明细']]) {
      const count = [...errors.keys()].filter(path => path === scope || path.startsWith(`${scope}.`)).length;
      get(tab).classList.toggle('output-template-tab-invalid', count > 0);
      get(tab).textContent = count ? `${label}（${count} 处待修正）` : label;
    }
    const scopeError = errors.get(section);
    get('scope-error').classList.toggle('hidden', !scopeError);
    get('scope-error').textContent = scopeError || '';
    get('add').setAttribute('aria-describedby', 'output-template-scope-error');
    const summary = get('validation-summary');
    summary.classList.toggle('hidden', !errors.size);
    if (errors.size) summary.textContent = `有 ${errors.size} 处需要修正，请查看红框旁的提示。`;
    else summary.textContent = '';
  }

  function validateBeforeSubmit() {
    validationActive = true;
    const errors = draftErrors();
    if (!errors.size) { showValidation(errors); return true; }
    const first = errors.keys().next().value;
    if (first !== 'name') section = first.split('.')[0];
    render();
    (errorTargets.get(first)?.control || get('add')).focus();
    return false;
  }

  function changed() {
    dirty = true;
    preview();
    showValidation();
    controls();
  }

  function button(text, onClick) {
    const node = element('button', text);
    node.type = 'button';
    node.onclick = onClick;
    return node;
  }

  function input(label, value, maxLength, onChange, multiline = false) {
    const wrapper = element('label', label);
    const node = element(multiline ? 'textarea' : 'input');
    node.value = value;
    node.maxLength = maxLength;
    if (multiline) node.rows = 2;
    node.oninput = () => { onChange(node.value); changed(); };
    wrapper.append(node);
    return wrapper;
  }

  function select(label, options, current, onChange) {
    const wrapper = element('label', label);
    const node = element('select');
    node.append(new Option('请选择…', ''));
    for (const [key, name] of options) node.append(new Option(name, key));
    node.value = current;
    node.onchange = () => { onChange(node.value); changed(); };
    wrapper.append(node);
    return wrapper;
  }

  function blankSource(kind) {
    return kind === 'drawing' ? {type: kind, labels: []} : kind === 'fixed' ? {type: kind, value: null} : {type: kind, key: ''};
  }

  function sourceFields(source, isTitle) {
    if (source.type === 'drawing') {
      const label = isTitle ? '原图标题栏标签（每行一个）' : '原图明细表列名（每行一个）';
      const field = input(label, (source.labels || []).join('\n'), 610,
        value => { source.labels = value.split(/\r?\n/); }, true);
      field.lastChild.placeholder = isTitle ? '设计部门\n设计单位' : '涂层代号\n涂层编码';
      return field;
    }
    if (source.type === 'fixed') {
      return input('固定值（不填则留空）', source.value ?? '', 32000,
        value => { source.value = value === '' ? null : value; });
    }
    const isReference = source.type === 'title';
    const options = isReference ? [...new Set(draft.title_fields.map(f => f.output.trim()).filter(Boolean))].map(key => [key, key])
      : Object.entries(isTitle ? state.title_sources : state.bom_sources);
    if (source.key && !options.some(([key]) => key === source.key)) options.push([source.key, `${source.key}（当前未配置）`]);
    const label = isReference ? '标题栏输出名称' : isTitle ? '标题栏系统字段' : '明细栏系统字段';
    const wrapper = select(label, options, source.key, value => { source.key = value; });
    if (isReference && !draft.title_fields.some(f => f.output.trim())) {
      wrapper.append(element('small', '先切到「图纸标题栏」添加字段，不用单独保存。'));
    }
    return wrapper;
  }

  function renderRow(field, i) {
    const isTitle = section === 'title_fields';
    const row = element('fieldset', '', 'title-template-field output-template-field');
    let lastName = field.output.trim();
    const output = input('输出名称', field.output, isTitle ? 80 : 120, value => {
      // Keep references intact when a unique title field is renamed in this draft.
      const next = value.trim();
      if (isTitle && lastName && next && !draft.title_fields.some(f => f !== field && f.output.trim() === lastName)) {
        draft.bom_columns.forEach(col => col.sources.forEach(source => {
          if (source.type === 'title' && source.key === lastName) source.key = next;
        }));
      }
      if (next) lastName = next;
      field.output = value;
    });
    output.lastChild.placeholder = isTitle ? '例如：设计部门' : '例如：物料编码';
    output.append(registerError(`${section}.${i}.output`, output.children[0]));
    row.append(element('legend', `字段 ${i + 1}`), output);
    field.sources.forEach((source, index) => {
      const line = element('div', '', 'output-template-source');
      const options = Object.entries(isTitle ? titleTypeLabel : typeLabel);
      const sourceType = select(index ? '备用来源' : '取值来源', options, source.type, value => {
        if (!value || value === source.type) return;
        field.sources[index] = blankSource(value);
        render();
        get('rows').children[i]?.querySelectorAll('.output-template-source')[index]?.querySelector('select')?.focus();
      });
      sourceType.lastChild.options[0].disabled = true;
      const value = sourceFields(source, isTitle);
      value.append(registerError(`${section}.${i}.source.${index}`, value.children[0]));
      line.append(sourceType, value);
      if (isTitle && field.sources.length > 1) line.append(button('移除来源', () => {
        field.sources.splice(index, 1);
        changed();
        render();
      }));
      row.append(line);
    });
    const tools = element('div', '', 'row');
    if (isTitle) tools.append(button('＋ 备用来源', () => {
      field.sources.push(blankSource('system'));
      changed();
      render();
      get('rows').children[i]?.querySelectorAll('.output-template-source select')[field.sources.length - 1]?.focus();
    }));
    tools.append(button('删除字段', () => {
      draft[section].splice(i, 1);
      changed();
      render();
      get('add').focus();
    }));
    row.append(tools);
    return row;
  }

  function render() {
    const title = section === 'title_fields';
    get('title-tab').setAttribute('aria-pressed', String(title));
    get('bom-tab').setAttribute('aria-pressed', String(!title));
    get('title-presets').classList.toggle('hidden', !title);
    get('scope-hint').textContent = title ? '每张图纸输出一份，只从标题栏取图名、图号、图纸材料等信息。可加备用来源，先查系统字段，再查原图标签。'
      : '每个零件输出一行，件号、数量、零件材料等从该零件的明细记录取值。要把整张图的图号带到每行，请选「引用图纸标题栏」。';
    get('name').value = draft?.name || '';
    errorTargets.clear();
    registerError('name', get('name'), get('name-error'));
    const rows = draft?.[section] || [];
    get('rows').replaceChildren(...rows.map(renderRow));
    if (!rows.length) get('rows').append(element('p', title ? '未配置标题栏输出。可加入预设，或逐个添加字段。' : '请至少添加一个零件明细字段。', 'hint'));
    preview();
    showValidation();
    controls();
  }

  function payload() {
    const result = JSON.parse(JSON.stringify(draft));
    for (const field of [...result.title_fields, ...result.bom_columns]) {
      field.sources.forEach(source => {
        if (source.type === 'drawing') source.labels = source.labels.map(a => a.trim()).filter(Boolean);
      });
    }
    return result;
  }

  const post = (url, data) => api(url, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(data)});

  async function run(action) {
    if (busy) return;
    busy = true;
    controls();
    try { await action(); }
    catch (error) { message(error.message, true); }
    finally { busy = false; controls(); }
  }

  function load() {
    return run(async () => {
      state = await api(endpoint);
      draft = state.template;
      // Initialize both halves together. Switching tabs must never change the
      // draft or the contents of a downloaded template.
      const preset = !state.read_only && state.suggest_title_presets && !draft.title_fields.length
        ? state.title_presets.common : [];
      if (preset.length) draft.title_fields = JSON.parse(JSON.stringify(preset));
      dirty = preset.length > 0;
      validationActive = false;
      render();
      message(state.read_only ? '固定部署配置模式，请在部署配置文件中修改。'
        : dirty ? `已预填 ${preset.length} 个常用标题栏字段，点「保存模板」后生效。` : '');
    });
  }

  function addTitlePreset(group) {
    const outputs = new Set(draft.title_fields.map(field => field.output.trim()));
    const sources = new Set(draft.title_fields.flatMap(field => field.sources
      .filter(source => source.type === 'system').map(source => source.key)));
    const additions = state.title_presets[group].filter(field =>
      !outputs.has(field.output) && !sources.has(field.sources[0].key));
    if (!additions.length) return message('这些预设已在模板中，未更改现有字段。');
    if (draft.title_fields.length + additions.length > 50) return message('加入后会超过 50 个标题栏字段，请先删除不需要的字段。', true);
    draft.title_fields.push(...JSON.parse(JSON.stringify(additions)));
    changed();
    render();
    message(`已在编辑区加入 ${additions.length} 个标题栏预设字段，点「保存模板」后生效。`);
  }

  get('name').oninput = () => { draft.name = get('name').value; changed(); };
  get('bom-tab').onclick = () => { section = 'bom_columns'; render(); };
  get('title-tab').onclick = () => { section = 'title_fields'; render(); };
  get('preset-common').onclick = () => addTitlePreset('common');
  get('preset-signoff').onclick = () => addTitlePreset('signoff');
  get('add').onclick = () => {
    const limit = section === 'title_fields' ? 50 : 100;
    if (draft[section].length >= limit) return message(`最多添加 ${limit} 个字段。`, true);
    draft[section].push({output: '', sources: [blankSource('system')]});
    changed();
    render();
    get('rows').lastElementChild.querySelector('input')?.focus();
  };
  get('save').onclick = () => validateBeforeSubmit() && run(async () => {
    const result = await post(endpoint, payload());
    draft = result.template;
    dirty = false;
    validationActive = false;
    render();
    message('已保存。新增原图字段或标题栏字段后，请用真实模型重新识别。');
  });

  async function importDraft(data) {
    const result = await post(endpoint + '/preview', data);
    draft = result.template;
    dirty = true;
    validationActive = false;
    render();
    message((result.notice ? result.notice + ' ' : '') + '已载入编辑区，保存后才会生效。');
  }

  get('example').onclick = () => {
    if (!dirty || window.confirm('用示例替换未保存的编辑内容？')) run(async () => importDraft(await api(example)));
  };
  get('import').onclick = () => run(async () => {
    const file = get('file').files[0];
    if (!file) return message('请先选择 JSON 模板文件。', true);
    if (file.size > 1024 * 1024) return message('模板文件不得超过 1 MB。', true);
    if (dirty && !window.confirm('用导入文件替换未保存的编辑内容？')) return;
    let data;
    try { data = JSON.parse(await file.text()); }
    catch (_) { throw new Error('模板文件不是有效的 JSON。'); }
    await importDraft(data);
  });
  get('download').onclick = () => validateBeforeSubmit() && run(async () => {
    const {template} = await post(endpoint + '/preview', payload());
    const blob = new Blob([JSON.stringify(template, null, 2) + '\n'], {type: 'application/json'});
    const anchor = element('a');
    const url = URL.createObjectURL(blob);
    anchor.href = url;
    anchor.download = 'bom-output-template.json';
    anchor.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    message(`已下载：图纸标题栏 ${template.title_fields.length} 项、零件明细 ${template.bom_columns.length} 列。`
      + (dirty ? '文件含编辑区中尚未保存的修改。' : ''));
  });
  get('reload').onclick = () => { if (!dirty || window.confirm('放弃未保存的修改？')) load(); };
  document.addEventListener('enterprise-profile-imported', () => {
    if (!dirty) load();
    else message('旧版配置已导入。若要采用它的列配置，请先「重新读取已保存版本」。');
  });
  render();
  load();
})();
