const state = {context: null, current: null};
const element = identifier => document.getElementById(identifier);

async function request(path, body) {
  const response = await fetch(path, body === undefined ? {} : {
    method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)
  });
  const value = await response.json();
  if (!response.ok) throw new Error(value.error || '请求失败');
  return value;
}

function message(value, error = false) {
  element('status').textContent = value;
  element('status').className = error ? 'error' : '';
}

async function guarded(action) {
  try { await action(); } catch (error) { message(error.message, true); }
}

function selected() {
  const selection = {};
  for (const control of state.context.product.options) selection[control[0]] = element('select-' + control[0]).value;
  return {revision: element('revision').value, selection};
}

function renderControls() {
  const revision = element('revision').value || state.context.default_revision;
  element('revision').replaceChildren();
  for (const dataset of state.context.datasets) {
    const option = document.createElement('option');
    option.value = dataset.revision;
    option.textContent = dataset.label + ' · ' + dataset.revision.slice(0, 9);
    element('revision').append(option);
  }
  element('revision').value = revision;
  if (!element('revision').value) element('revision').value = state.context.default_revision;
  if (element('controls').children.length) return;
  for (const [key, labelText, choices] of state.context.product.options) {
    const label = document.createElement('label');
    label.textContent = labelText;
    const select = document.createElement('select');
    select.id = 'select-' + key;
    for (const choice of choices) {
      const option = document.createElement('option');
      option.value = choice;
      option.textContent = choice;
      select.append(option);
    }
    select.value = state.context.product.selection[key];
    label.append(select);
    element('controls').append(label);
  }
}

function displayValue(value) {
  if (value === null || value === undefined) return '未观测';
  if (typeof value === 'number') return Number.isInteger(value) ? String(value) : value.toFixed(4);
  return typeof value === 'object' ? JSON.stringify(value) : String(value);
}

function drawChart(series, threshold) {
  const canvas = element('chart');
  const context = canvas.getContext('2d');
  context.clearRect(0, 0, canvas.width, canvas.height);
  const values = series.filter(point => typeof point.y === 'number' && Number.isFinite(point.y));
  context.font = '12px sans-serif';
  if (!values.length) { context.fillText('该范围没有可定量的描述值', 30, 50); return; }
  const low = Math.min(0, ...values.map(point => point.y));
  const high = Math.max(1, ...values.map(point => point.y), threshold || 0);
  const scale = value => 250 - (value - low) / (high - low || 1) * 210;
  context.strokeStyle = '#90a5ad';
  context.beginPath(); context.moveTo(45, 20); context.lineTo(45, 250); context.lineTo(940, 250); context.stroke();
  if (typeof threshold === 'number' && Number.isFinite(threshold)) {
    context.strokeStyle = '#b05a2a';
    context.setLineDash([6, 4]);
    context.beginPath(); context.moveTo(45, scale(threshold)); context.lineTo(940, scale(threshold)); context.stroke();
    context.setLineDash([]);
    context.fillStyle = '#b05a2a';
    context.fillText('阈值 ' + threshold.toFixed(2), 850, scale(threshold) - 6);
  }
  context.fillStyle = '#0a7168';
  const scatter = values.every(point => typeof point.x === 'number');
  const minimumX = scatter ? Math.min(...values.map(point => point.x)) : 0;
  const maximumX = scatter ? Math.max(...values.map(point => point.x)) : 1;
  values.forEach((point, index) => {
    const position = scatter ? 60 + (point.x - minimumX) / (maximumX - minimumX || 1) * 850 : 55 + index * 875 / values.length;
    if (scatter) {
      context.beginPath(); context.arc(position, scale(point.y), 3.5, 0, Math.PI * 2); context.fill();
    } else {
      context.fillRect(position, Math.min(scale(point.y), scale(0)), Math.max(3, 800 / values.length), Math.max(1, Math.abs(scale(point.y) - scale(0))));
    }
    if (values.length < 12) context.fillText(point.label, position, 275);
  });
  context.fillStyle = '#29434e';
  context.fillText(high.toFixed(2), 0, 45); context.fillText(low.toFixed(2), 0, 250);
  context.fillText(scatter ? '记录稳健距离（点）；虚线为理论阈值（随维度，图示主维度）；无法评估的记录不绘制' : '按表中顺序展示描述值；缺测不绘制为零', 50, 20);
}

const STATUS_CLASS = {'一致': 'st-ok', '单元格可疑': 'st-cell', '整记录偏离': 'st-row', '无法评估': 'st-na'};

function renderBanner(view) {
  const banner = element('batch-banner');
  const assessment = view.batch_assessment;
  if (!assessment) { banner.replaceChildren(); banner.className = ''; return; }
  banner.textContent = '批次判定：' + assessment.verdict + '。' + assessment.explanation;
  banner.className = (assessment.verdict === '批次整体偏移' || assessment.verdict === '无法判定') ? 'warn' : '';
}

function renderRecords(view) {
  const header = document.createElement('tr');
  for (const label of ['记录', '判定', '稳健距离', '经验p', '可疑单元格', '复核意见']) {
    const cell = document.createElement('th'); cell.textContent = label; header.append(cell);
  }
  element('record-columns').replaceChildren(header);
  element('record-rows').replaceChildren();
  for (const record of view.records || []) {
    const line = document.createElement('tr'); line.tabIndex = 0;
    const id = document.createElement('td'); id.textContent = record.record_id; line.append(id);
    const status = document.createElement('td');
    const pill = document.createElement('span');
    pill.className = 'pill ' + (STATUS_CLASS[record.status] || 'st-na');
    pill.textContent = record.status;
    status.append(pill); line.append(status);
    const distance = document.createElement('td'); distance.textContent = displayValue(record.distance); line.append(distance);
    const p = document.createElement('td'); p.textContent = record.p === null ? '—' : record.p; line.append(p);
    const cells = document.createElement('td');
    cells.textContent = (record.cells || []).filter(cell => cell.suspicious)
      .map(cell => cell.field + '(残差' + cell.residual + ')').join('；');
    line.append(cells);
    const reviews = document.createElement('td');
    reviews.textContent = (record.reviews || []).map(review => review.review_id + ':' + review.decision).join('；');
    line.append(reviews);
    line.onclick = () => { element('details').textContent = JSON.stringify(record, null, 2); };
    line.onkeydown = event => { if (event.key === 'Enter') line.click(); };
    element('record-rows').append(line);
  }
}

function renderModel(result) {
  const view = result.view;
  const lines = [];
  if (view.model) {
    const model = view.model;
    lines.push('模型指纹 ' + model.fingerprint.slice(0, 16) + ' … · 数据版本 ' + result.revision.slice(0, 16) + ' …（保存的分析记录同时冻结两者）');
    lines.push('模型字段：' + model.fields_used.join('、') + '；参照拟合 ' + model.reference_fitted + ' 条');
    for (const group of model.duplicate_groups)
      lines.push('近似重复：' + group.merged.join('、') + ' 并入 ' + group.kept + '（稳健相关 ' + group.correlation + '）');
    for (const dropped of model.fields_dropped)
      lines.push('未入模：' + dropped.field + '（' + dropped.reason + '）');
    for (const pair of model.pairs_insufficient)
      lines.push('共同观测不足：' + pair.fields.join('×') + ' 仅 ' + pair.common_observations + ' 条，按独立处理');
    lines.push('校准：' + model.calibration.method + '；行级阈值=卡方' + model.flag_probability +
               '分位（随维度），参照经验上限 ' + displayValue(model.empirical_threshold) +
               '；单元格残差限 ±' + model.cell_residual_limit);
    lines.push('生效的复核意见：' + (model.reviews_applied.join('、') || '无') +
               '；被排除的参照记录：' + (model.reference_excluded.join('、') || '无'));
    for (const extreme of model.reference_extreme)
      lines.push('参照内极端记录：' + extreme.record_id + '（留一法距离 ' + extreme.distance + '），建议优先复核');
    const matrix = model.correlation_matrix;
    lines.push('稳健相关矩阵（' + matrix.fields.join('、') + '）：');
    for (const [index, row] of matrix.matrix.entries())
      lines.push('  ' + matrix.fields[index] + '  ' + row.map(value => value.toFixed(3)).join('  '));
  } else {
    lines.push('模型不可用：参照不足或字段不满足建模条件，本批次记录全部标记为无法评估。');
  }
  element('model-panel').textContent = lines.join('\n');
  element('limitations').replaceChildren();
  for (const limitation of view.limitations || []) {
    const item = document.createElement('li'); item.textContent = limitation;
    element('limitations').append(item);
  }
}

function populateReviewControls(view) {
  const list = element('record-list');
  list.replaceChildren();
  for (const record of (view.records || []).slice(0, 500)) {
    const option = document.createElement('option');
    option.value = record.record_id;
    list.append(option);
  }
  const fields = (view.rows || []).map(row => row.field);
  const whatifField = element('whatif-field');
  const previousWhatif = whatifField.value;
  whatifField.replaceChildren();
  const reviewField = element('review-field');
  const previousReview = reviewField.value;
  reviewField.replaceChildren();
  const blank = document.createElement('option');
  blank.value = ''; blank.textContent = '整条记录';
  reviewField.append(blank);
  for (const field of fields) {
    const first = document.createElement('option');
    first.value = field; first.textContent = field;
    whatifField.append(first);
    const second = document.createElement('option');
    second.value = field; second.textContent = field;
    reviewField.append(second);
  }
  if (fields.includes(previousWhatif)) whatifField.value = previousWhatif;
  if (fields.includes(previousReview)) reviewField.value = previousReview;
}

function render(result, heading) {
  state.current = result;
  element('summary').replaceChildren();
  for (const [label, value] of Object.entries(result.view.summary)) {
    const card = document.createElement('article');
    const title = document.createElement('span'); title.textContent = label;
    const amount = document.createElement('strong'); amount.textContent = displayValue(value);
    card.append(title, amount); element('summary').append(card);
  }
  element('notes').replaceChildren();
  for (const note of result.view.notes) {
    const paragraph = document.createElement('p'); paragraph.textContent = note; element('notes').append(paragraph);
  }
  renderBanner(result.view);
  renderRecords(result.view);
  renderModel(result);
  populateReviewControls(result.view);
  const rows = result.view.rows;
  const fields = rows.length ? Object.keys(rows[0]) : [];
  const header = document.createElement('tr');
  for (const field of fields) { const cell = document.createElement('th'); cell.textContent = field; header.append(cell); }
  element('columns').replaceChildren(header); element('rows').replaceChildren();
  for (const row of rows) {
    const line = document.createElement('tr'); line.tabIndex = 0;
    for (const field of fields) { const cell = document.createElement('td'); cell.textContent = displayValue(row[field]); line.append(cell); }
    line.onclick = () => { element('details').textContent = JSON.stringify(row, null, 2); };
    line.onkeydown = event => { if (event.key === 'Enter') line.click(); };
    element('rows').append(line);
  }
  drawChart(result.view.series, result.view.threshold_line);
  element('details').textContent = JSON.stringify({revision: result.revision, selection: result.selection,
    history: result.view.history || null}, null, 2);
  message((heading || '已更新描述') + ' · 数据 ' + result.revision.slice(0, 12));
}

async function refresh() {
  state.context = await request('/api/context');
  renderControls();
  element('archive').replaceChildren();
  for (const notebook of state.context.notebooks) {
    const button = document.createElement('button'); button.textContent = notebook.title + ' #' + notebook.id;
    button.onclick = () => guarded(async () => {
      const saved = await request('/api/notebooks/' + notebook.id);
      element('revision').value = saved.revision;
      for (const [key, value] of Object.entries(saved.selection)) {
        const control = element('select-' + key); if (control) control.value = value;
      }
      render(saved.result, '已读取保存的描述，不随新数据变化');
    });
    element('archive').append(button);
  }
}

element('inspect').onclick = () => guarded(async () => render(await request('/api/explore', selected())));
element('save').onclick = () => guarded(async () => {
  const saved = await request('/api/notebooks', {...selected(), title: element('title').value});
  await refresh(); render(saved.result, '已保存记录 #' + saved.id);
});
element('raw').onclick = () => guarded(async () => {
  const revision = state.current ? state.current.revision : element('revision').value;
  element('details').textContent = JSON.stringify(await request('/api/data?revision=' + encodeURIComponent(revision)), null, 2);
});
element('upload').onchange = () => guarded(async () => {
  const file = element('upload').files[0]; if (!file) return;
  const data = JSON.parse(await file.text());
  const uploaded = await request('/api/datasets', {label: file.name, data});
  await refresh(); element('revision').value = uploaded.revision;
  render(await request('/api/explore', selected()), '已导入独立数据版本');
});
element('export').onclick = () => guarded(async () => {
  if (!state.current) throw new Error('请先查看一个范围');
  const data = await request('/api/data?revision=' + encodeURIComponent(state.current.revision));
  const blob = new Blob([JSON.stringify({description: state.current, data}, null, 2)], {type: 'application/json'});
  const url = URL.createObjectURL(blob); const link = document.createElement('a');
  link.href = url; link.download = 'analysis-' + state.current.revision.slice(0, 12) + '.json'; link.click();
  URL.revokeObjectURL(url);
});
element('whatif-run').onclick = () => guarded(async () => {
  if (!state.current) throw new Error('请先查看一个范围');
  const record = element('whatif-record').value.trim();
  if (!record) throw new Error('请填写试算记录编号');
  const raw = element('whatif-value').value;
  const change = {record_id: record, action: element('whatif-action').value,
                  field: element('whatif-field').value || null,
                  value: raw === '' ? null : Number(raw)};
  const result = await request('/api/whatif', {revision: state.current.revision,
    selection: state.current.selection, change});
  const lines = [...result.notes];
  lines.push('模型指纹：' + (result.model.fingerprint_before || '无').slice(0, 12) + ' → ' +
             (result.model.fingerprint_after || '无').slice(0, 12) +
             '（' + (result.model.changed ? '已变化' : '不变') + '）；参照经验上限 ' +
             displayValue(result.model.empirical_threshold_before) + ' → ' +
             displayValue(result.model.empirical_threshold_after));
  lines.push('判定状态变化 ' + result.changed_total + ' 条；距离发生变化 ' + result.distances_shifted + ' 条');
  for (const entry of result.changed_records.slice(0, 20))
    lines.push('  ' + entry.record_id + '：' + entry.before + ' → ' + entry.after +
               '（距离 ' + displayValue(entry.distance_before) + ' → ' + displayValue(entry.distance_after) + '）');
  const target = result.target;
  if (target.before && target.after)
    lines.push('目标记录 ' + target.record_id + '：' + target.before.status + ' → ' + target.after.status +
               '（距离 ' + displayValue(target.before.distance) + ' → ' + displayValue(target.after.distance) + '）');
  element('whatif-result').textContent = lines.join('\n');
});
element('review-save').onclick = () => guarded(async () => {
  const record = element('review-record').value.trim();
  if (!record) throw new Error('请填写复核记录编号');
  const raw = element('review-value').value;
  const review = {record_id: record, field: element('review-field').value || null,
                  decision: element('review-decision').value, note: element('review-note').value,
                  revised_value: raw === '' ? null : Number(raw)};
  const saved = await request('/api/reviews', {revision: element('revision').value, review});
  await refresh();
  element('revision').value = saved.revision;
  render(await request('/api/explore', selected()),
         '复核意见 ' + saved.review_id + ' 已保存为数据版本 ' + saved.revision.slice(0, 9) + '，原始记录未改动');
});
guarded(async () => { await refresh(); render(await request('/api/explore', selected())); });
