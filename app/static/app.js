const form = document.getElementById('uploadForm');
const fileInput = document.getElementById('fileInput');
const fileList = document.getElementById('fileList');
const dropzone = document.getElementById('dropzone');
const message = document.getElementById('message');
const submitButton = document.getElementById('submitButton');
const summary = document.getElementById('summary');
const riskRows = document.getElementById('riskRows');
const emptyState = document.getElementById('emptyState');
const searchInput = document.getElementById('searchInput');
const qualityRows = document.getElementById('qualityRows');
const unmatchedWarehouses = document.getElementById('unmatchedWarehouses');

let currentRunId = null;
let allRows = [];
let activeRisk = '全部';

document.getElementById('businessDate').value = new Date().toISOString().slice(0, 10);

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>'"]/g, char => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;'
  }[char]));
}

function fileSize(bytes) {
  return bytes > 1024 * 1024 ? `${(bytes / 1024 / 1024).toFixed(1)} MB` : `${Math.ceil(bytes / 1024)} KB`;
}

function showFiles() {
  fileList.innerHTML = [...fileInput.files].map(file => `
    <div class="file-chip"><span>${escapeHtml(file.name)}</span><small>${fileSize(file.size)}</small></div>
  `).join('');
}

fileInput.addEventListener('change', showFiles);
['dragenter', 'dragover'].forEach(event => dropzone.addEventListener(event, e => {
  e.preventDefault(); dropzone.classList.add('dragover');
}));
['dragleave', 'drop'].forEach(event => dropzone.addEventListener(event, e => {
  e.preventDefault(); dropzone.classList.remove('dragover');
}));
dropzone.addEventListener('drop', event => {
  const transfer = new DataTransfer();
  [...event.dataTransfer.files].forEach(file => transfer.items.add(file));
  fileInput.files = transfer.files;
  showFiles();
});

function setMessage(text, type = 'error') {
  message.className = `message ${type}`;
  message.textContent = text;
}

function formatNumber(value) {
  return Number(value || 0).toLocaleString('zh-CN', { maximumFractionDigits: 2 });
}

const runStatusLabels = {
  validated: '质检完成', completed: '计算完成', blocked: '日期校验阻断', failed: '处理失败', processing: '处理中'
};

function formatDateTime(value) {
  if (!value) return '—';
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString('zh-CN', {
    month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit'
  });
}

function renderDashboard(payload) {
  const latest = payload.latest_run;
  const dashboardSummary = payload.summary || {};
  const quality = payload.quality_totals || {};
  const mapping = payload.mapping || {};
  const statusDot = document.getElementById('dashboardStatusDot');
  const statusText = document.getElementById('dashboardStatusText');

  if (!latest) {
    statusDot.className = 'status-dot waiting';
    statusText.textContent = '等待首次数据导入';
    document.getElementById('dashboardStatusHint').textContent = '请先导入库存余量和报货订单实时文件。';
  } else if (dashboardSummary.data_ready) {
    statusDot.className = 'status-dot ready';
    statusText.textContent = latest.status === 'validated' ? '数据接入与质检已完成' : '核心数据已就绪';
    document.getElementById('dashboardStatusHint').textContent = '可继续检查质检明细和维护基础仓库映射。';
  } else {
    statusDot.className = 'status-dot attention';
    statusText.textContent = latest.status === 'processing' ? '数据正在处理' : '数据仍需补齐或修正';
    document.getElementById('dashboardStatusHint').textContent = latest.error_message || '请检查缺失文件、日期状态和质检异常。';
  }

  document.getElementById('dashboardLatestDate').textContent = latest?.business_date || '—';
  document.getElementById('dashboardRunStatus').textContent = latest ? (runStatusLabels[latest.status] || latest.status) : '暂无处理批次';
  document.getElementById('dashboardFileCount').textContent = `${dashboardSummary.successful_file_count || 0} / ${payload.file_readiness.length}`;
  document.getElementById('dashboardValidRows').textContent = formatNumber(quality.valid_rows);
  document.getElementById('dashboardRowHint').textContent = latest ? `原始 ${formatNumber(quality.original_rows)} 行 · 删除 ${formatNumber(quality.deleted_rows)} 行` : '等待数据导入';
  document.getElementById('dashboardMappingCount').textContent = formatNumber(mapping.enabled);
  document.getElementById('dashboardMappingHint').textContent = `共 ${formatNumber(mapping.total)} 条 · 停用 ${formatNumber(mapping.disabled)} 条`;
  document.getElementById('qualityBatchLabel').textContent = latest ? latest.business_date : '暂无批次';
  document.getElementById('masterEnabledMappings').textContent = formatNumber(mapping.enabled);
  document.getElementById('masterDisabledMappings').textContent = formatNumber(mapping.disabled);
  document.getElementById('masterUnmatchedRecords').textContent = formatNumber(dashboardSummary.unmatched_warehouse_records);

  const dateFilter = document.getElementById('dashboardDateFilter');
  const statusFilter = document.getElementById('dashboardStatusFilter');
  const fileFilter = document.getElementById('dashboardFileFilter');
  const selectedDate = dateFilter.value;
  const selectedStatus = statusFilter.value;
  const selectedFile = fileFilter.value;
  dateFilter.innerHTML = '<option value="">全部日期</option>' + (payload.filters?.business_dates || []).map(value => `<option value="${escapeHtml(value)}">${escapeHtml(value)}</option>`).join('');
  statusFilter.innerHTML = '<option value="">全部状态</option>' + (payload.filters?.statuses || []).map(value => `<option value="${escapeHtml(value)}">${escapeHtml(runStatusLabels[value] || value)}</option>`).join('');
  fileFilter.innerHTML = '<option value="">全部文件</option>' + payload.file_readiness.map(item => `<option value="${escapeHtml(item.file_type)}">${escapeHtml(item.label)}</option>`).join('');
  dateFilter.value = payload.filters?.selected_business_date || selectedDate;
  statusFilter.value = payload.filters?.selected_status || selectedStatus;
  fileFilter.value = selectedFile;

  const readiness = selectedFile ? payload.file_readiness.filter(item => item.file_type === selectedFile) : payload.file_readiness;
  document.getElementById('readinessList').innerHTML = readiness.map(item => {
    const state = item.present && item.status === 'success' ? 'ready' : item.required ? 'missing' : 'optional';
    const note = item.present
      ? `${formatNumber(item.row_count)} 行${item.file_date ? ` · 日期 ${escapeHtml(item.file_date)}` : ''}`
      : item.required ? '必需文件，尚未识别' : '可选文件，尚未导入';
    return `<div class="readiness-item">
      <span class="readiness-icon ${state}">${state === 'ready' ? '✓' : state === 'missing' ? '!' : '–'}</span>
      <div><strong>${escapeHtml(item.label)}${item.required ? '<em>必需</em>' : ''}</strong><small>${note}</small></div>
      <span class="readiness-state ${state}">${state === 'ready' ? '已就绪' : state === 'missing' ? '待补齐' : '可选'}</span>
    </div>`;
  }).join('');

  document.getElementById('masterUnmatchedList').innerHTML = (payload.unmatched_warehouses || []).map(item => `
    <div><strong>${escapeHtml(item.raw_warehouse_name || item.raw_warehouse_code || '未命名仓库')}</strong><span>${escapeHtml(item.source_system)} · ${formatNumber(item.record_count)} 条</span></div>
  `).join('') || '<p class="empty-inline">当前筛选批次没有待维护仓库。</p>';

  const qualityItems = [
    ['原始行数', quality.original_rows], ['有效行数', quality.valid_rows],
    ['删除行数', quality.deleted_rows], ['必填缺失', quality.required_missing_count],
    ['日期失败', quality.date_parse_failure_count], ['数值失败', quality.numeric_parse_failure_count],
    ['重复记录', quality.duplicate_count], ['未匹配仓库', quality.unmatched_warehouse_count]
  ];
  document.getElementById('qualitySnapshot').innerHTML = qualityItems.map(([label, value]) => `
    <div class="quality-item"><span>${label}</span><strong>${formatNumber(value)}</strong></div>
  `).join('');

  const recentRows = payload.recent_runs || [];
  document.getElementById('recentRunRows').innerHTML = recentRows.map(run => `<tr>
    <td><b>${escapeHtml(run.business_date)}</b></td><td>${escapeHtml(formatDateTime(run.created_at))}</td>
    <td><span class="batch-status ${escapeHtml(run.status)}">${escapeHtml(runStatusLabels[run.status] || run.status)}</span></td>
    <td>${formatNumber(run.file_count)}</td><td>${formatNumber(run.valid_rows)}</td>
    <td>${formatNumber(run.unmatched_warehouse_count)}</td><td>${escapeHtml(run.stock_date_status || '—')}</td>
  </tr>`).join('');
  document.getElementById('recentRunEmpty').classList.toggle('hidden', recentRows.length > 0);
  document.querySelector('.recent-card .table-scroll').classList.toggle('hidden', recentRows.length === 0);
}

async function loadDashboard() {
  try {
    const params = new URLSearchParams();
    const selectedDate = document.getElementById('dashboardDateFilter').value;
    const selectedStatus = document.getElementById('dashboardStatusFilter').value;
    if (selectedDate) params.set('business_date', selectedDate);
    if (selectedStatus) params.set('run_status', selectedStatus);
    const response = await fetch(`/api/dashboard${params.size ? `?${params}` : ''}`);
    if (!response.ok) throw new Error('看板接口不可用');
    renderDashboard(await response.json());
  } catch (error) {
    document.getElementById('dashboardStatusDot').className = 'status-dot attention';
    document.getElementById('dashboardStatusText').textContent = '看板读取失败';
    document.getElementById('dashboardStatusHint').textContent = error.message || '请刷新页面重试。';
  }
}

document.getElementById('applyDashboardFilters').addEventListener('click', loadDashboard);
document.getElementById('dashboardFileFilter').addEventListener('change', loadDashboard);
document.getElementById('resetDashboardFilters').addEventListener('click', () => {
  document.getElementById('dashboardDateFilter').value = '';
  document.getElementById('dashboardStatusFilter').value = '';
  document.getElementById('dashboardFileFilter').value = '';
  loadDashboard();
});

function riskClass(level) {
  return level === '红色' ? 'red' : level === '黄色' ? 'yellow' : 'green';
}

function renderRows() {
  const query = searchInput.value.trim().toLowerCase();
  const filtered = allRows.filter(row => {
    const riskMatches = activeRisk === '全部' || row.risk_level === activeRisk;
    const text = `${row.warehouse} ${row.material_code} ${row.material_name}`.toLowerCase();
    return riskMatches && (!query || text.includes(query));
  });
  riskRows.innerHTML = filtered.map(row => {
    const t1Supply = Number(row.t1_purchase) + Number(row.t1_transfer_in) - Number(row.t1_transfer_out);
    const t2Supply = Number(row.t2_purchase) + Number(row.t2_transfer_in) - Number(row.t2_transfer_out);
    return `<tr class="clickable-row" data-risk-id="${escapeHtml(row.id)}">
      <td><span class="risk-pill ${riskClass(row.risk_level)}">${escapeHtml(row.risk_level)}</span></td>
      <td>${escapeHtml(row.warehouse)}</td>
      <td><b>${escapeHtml(row.material_code)}</b><br><small>${escapeHtml(row.material_name)}</small></td>
      <td>${formatNumber(row.available_stock)}</td>
      <td>${formatNumber(row.t1_demand)}</td>
      <td>${formatNumber(t1Supply)}</td>
      <td class="${Number(row.t1_projected_stock) < 0 ? 'negative' : ''}">${formatNumber(row.t1_projected_stock)}</td>
      <td>${formatNumber(row.t2_demand)}</td>
      <td>${formatNumber(t2Supply)}</td>
      <td class="${Number(row.t2_projected_stock) < 0 ? 'negative' : ''}">${formatNumber(row.t2_projected_stock)}</td>
      <td>${escapeHtml(row.risk_reason)}</td>
    </tr>`;
  }).join('');
  emptyState.classList.toggle('hidden', filtered.length > 0);
}

function renderQuality(payload) {
  qualityRows.innerHTML = (payload.quality_reports || []).map(report => `<tr>
    <td>${escapeHtml(report.filename)}</td><td>${escapeHtml(report.sheet_name)}</td>
    <td>${formatNumber(report.original_rows)}</td><td>${formatNumber(report.valid_rows)}</td><td>${formatNumber(report.deleted_rows)}</td>
    <td>${formatNumber(report.required_missing_count)}</td><td>${formatNumber(report.date_parse_failure_count)}</td>
    <td>${formatNumber(report.numeric_parse_failure_count)}</td><td>${formatNumber(report.duplicate_count)}</td>
    <td>${formatNumber(report.unmatched_warehouse_count)}</td><td>${formatNumber(report.unmatched_material_count)}</td>
    <td>${formatNumber(report.negative_count)}</td><td>${escapeHtml(report.file_date || '未识别')}</td>
    <td>${escapeHtml(report.file_date_status)}</td><td>${report.calculation_allowed ? '是' : '否'}</td>
  </tr>`).join('');
  unmatchedWarehouses.innerHTML = (payload.unmatched_warehouses || []).map(item => `<div class="processed-file">
    <b>${escapeHtml(item.raw_warehouse_name || item.raw_warehouse_code)}</b>
    <span>${escapeHtml(item.source_file)}</span><span>${formatNumber(item.record_count)} 行</span>
    <span>建议：${escapeHtml(item.suggested_standard_name || '待维护')}</span>
  </div>`).join('') || '<div class="empty-state">没有未匹配仓库。</div>';
}

function renderResult(payload) {
  currentRunId = payload.run_id;
  allRows = payload.rows || [];
  const resultMode = payload.result_mode || payload.run?.result_mode || 'official';
  const badge = document.getElementById('resultModeBadge');
  badge.textContent = resultMode === 'quality_only' ? '仅数据质检' : resultMode === 'simulation' ? '模拟结果' : '正式结果';
  badge.className = `mode-badge ${resultMode === 'simulation' ? 'simulation' : 'official'}`;
  document.getElementById('analysisDetails').classList.toggle('hidden', resultMode === 'quality_only');
  document.getElementById('exportButton').classList.toggle('hidden', resultMode === 'quality_only');
  document.getElementById('totalCount').textContent = payload.summary.total;
  document.getElementById('redCount').textContent = payload.summary['红色'];
  document.getElementById('yellowCount').textContent = payload.summary['黄色'];
  document.getElementById('greenCount').textContent = payload.summary['绿色'];
  document.getElementById('processedFiles').innerHTML = (payload.files || []).map(file => `
    <div class="processed-file">
      <b>${escapeHtml(file.filename)}</b>
      <span>${escapeHtml(file.label)}</span>
      <span>${formatNumber(file.row_count)} 行</span>
      <span class="${file.status === 'success' ? 'ok' : 'bad'}">${escapeHtml(file.message)}</span>
    </div>
  `).join('');
  summary.classList.remove('hidden');
  renderQuality(payload);
  renderRows();
  summary.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

form.addEventListener('submit', async event => {
  event.preventDefault();
  if (!fileInput.files.length) return setMessage('请先选择每日Excel文件。');
  submitButton.disabled = true;
  submitButton.textContent = '正在校验与计算…';
  message.classList.add('hidden');
  try {
    const response = await fetch('/api/runs', { method: 'POST', body: new FormData(form) });
    const payload = await response.json();
    if (!response.ok) {
      if (payload.quality_reports) {
        summary.classList.remove('hidden');
        renderQuality(payload);
      }
      const details = (payload.files || []).filter(f => f.status === 'error').map(f => `${f.filename}：${f.message}`).join('；');
      throw new Error(details || payload.detail || '处理失败');
    }
    setMessage(payload.result_mode === 'quality_only' ? '数据接入与质检完成，请查看看板和质检明细。' : `批次处理完成：共生成 ${payload.summary.total} 条仓库物料计算记录。`, 'success');
    renderResult(payload);
    loadDashboard();
  } catch (error) {
    setMessage(error.message || '上传失败，请检查文件后重试。');
  } finally {
    submitButton.disabled = false;
    submitButton.textContent = '上传并执行质检';
  }
});

document.querySelectorAll('.filter').forEach(button => button.addEventListener('click', () => {
  document.querySelectorAll('.filter').forEach(item => item.classList.remove('active'));
  button.classList.add('active');
  activeRisk = button.dataset.risk;
  renderRows();
}));
searchInput.addEventListener('input', renderRows);
document.getElementById('exportButton').addEventListener('click', () => {
  if (currentRunId) window.location.href = `/api/runs/${currentRunId}/export`;
});

riskRows.addEventListener('click', async event => {
  const row = event.target.closest('[data-risk-id]');
  if (!row || !currentRunId) return;
  const response = await fetch(`/api/runs/${currentRunId}/risks/${row.dataset.riskId}`);
  const payload = await response.json();
  if (!response.ok) return setMessage(payload.detail || '无法读取风险详情');
  const risk = payload.risk;
  const groups = payload.source_records || {};
  const recordTable = (title, records = []) => `<section class="detail-block"><h3>${title}（${records.length}）</h3>${records.length ? `<pre>${escapeHtml(JSON.stringify(records, null, 2))}</pre>` : '<p>无对应原始记录</p>'}</section>`;
  document.getElementById('riskDetail').innerHTML = `
    <p class="eyebrow">RISK TRACE</p><h2>${escapeHtml(risk.warehouse)} · ${escapeHtml(risk.material_code)}</h2>
    <p>${escapeHtml(risk.material_name)} · ${escapeHtml(payload.run.result_mode === 'simulation' ? '模拟结果' : '正式结果')}</p>
    <div class="detail-grid">
      <div><span>库存组成</span><b>${formatNumber(risk.current_stock)} - ${formatNumber(risk.allocated_qty)} - ${formatNumber(risk.frozen_qty)} = ${formatNumber(risk.available_stock)}</b></div>
      <div><span>T+1计算</span><b>${formatNumber(risk.available_stock)} + ${formatNumber(risk.t1_purchase)} + ${formatNumber(risk.t1_transfer_in)} - ${formatNumber(risk.t1_demand)} - ${formatNumber(risk.t1_transfer_out)} = ${formatNumber(risk.t1_projected_stock)}</b></div>
      <div><span>T+2计算</span><b>${formatNumber(risk.t1_projected_stock)} + ${formatNumber(risk.t2_purchase)} + ${formatNumber(risk.t2_transfer_in)} - ${formatNumber(risk.t2_demand)} - ${formatNumber(risk.t2_transfer_out)} = ${formatNumber(risk.t2_projected_stock)}</b></div>
      <div><span>安全库存</span><b>${formatNumber(risk.safety_stock)}</b></div>
    </div>
    ${recordTable('库存组成', groups.stock_components)}${recordTable('报货记录', groups.demand)}
    ${recordTable('采购单', groups.purchase)}${recordTable('调拨入', groups.transfer_in)}${recordTable('调拨出', groups.transfer_out)}`;
  document.getElementById('riskModal').classList.remove('hidden');
});
document.getElementById('closeRiskModal').addEventListener('click', () => document.getElementById('riskModal').classList.add('hidden'));


loadDashboard();
