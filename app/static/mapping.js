function setMappingMessage(text,type='error') { const box=document.getElementById('mappingMessage');box.textContent=text;box.className='message '+type; }
const mappingForm = document.getElementById('mappingForm');
let mappingCache = [], mappingPage = 1, mappingTotal = 0, mappingRequest = 0;
function renderMappings() {
  const rows = mappingCache;
  document.getElementById('mappingRows').innerHTML = rows.map(item => `<tr>
    <td>${item.id}</td><td>${escapeHtml(item.source_system)}</td><td>${escapeHtml(item.raw_warehouse_code || '')}</td>
    <td>${escapeHtml(item.raw_warehouse_name || '')}</td><td>${escapeHtml(item.standard_warehouse_code)}</td>
    <td>${escapeHtml(item.standard_warehouse_name)}</td><td>${escapeHtml(item.special_alias || '')}</td>
    <td>${escapeHtml(item.effective_start_date || '—')} 至 ${escapeHtml(item.effective_end_date || '—')}</td>
    <td>${item.enabled ? '启用' : '停用'}</td><td><button class="filter mapping-edit" data-id="${item.id}">编辑</button></td>
  </tr>`).join('');
}
async function loadMappings(keepPage = false) {
  if(keepPage!==true)mappingPage=1;
  const request=++mappingRequest;
  const query = new URLSearchParams({page:mappingPage,page_size:100});
  for(const [id,key] of [['mappingSourceFilter','source'],['mappingEnabledFilter','enabled'],['mappingKeywordFilter','search']]){
    const value=document.getElementById(id).value;if(value!=='')query.set(key,value);
  }
  const response = await fetch(`/api/warehouse-mappings?${query}`);
  const payload = await response.json();
  if(request!==mappingRequest)return;
  if(!response.ok) {setMappingMessage(payload.detail||'读取失败');return;}
  mappingCache = payload.rows || [];mappingTotal=payload.total??mappingCache.length;
  const sourceFilter = document.getElementById('mappingSourceFilter');
  const selectedSource = sourceFilter.value;
  const sources = payload.sources || [];
  sourceFilter.innerHTML = '<option value="">全部来源</option>' + sources.map(value => `<option value="${escapeHtml(value)}">${escapeHtml(value)}</option>`).join('');
  sourceFilter.value = selectedSource;
  renderMappings();
  document.getElementById('mappingCount').textContent=`共 ${mappingTotal} 条 · 第 ${mappingPage} 页`;
  document.getElementById('mappingPrev').disabled=mappingPage===1;
  document.getElementById('mappingNext').disabled=mappingPage*100>=mappingTotal;
}
document.getElementById('mappingSourceFilter').addEventListener('change', loadMappings);
document.getElementById('mappingEnabledFilter').addEventListener('change', loadMappings);
document.getElementById('mappingKeywordFilter').addEventListener('input', ()=>{clearTimeout(window.mappingTimer);window.mappingTimer=setTimeout(loadMappings,250);});
mappingForm.addEventListener('submit', async event => {
  event.preventDefault();
  const data = Object.fromEntries(new FormData(mappingForm).entries());
  data.enabled = mappingForm.elements.enabled.checked;
  ['raw_warehouse_code','raw_warehouse_name','special_alias','effective_start_date','effective_end_date','remark'].forEach(k => { if (!data[k]) data[k] = null; });
  const id = document.getElementById('mappingId').value;
  const response = await fetch(id ? `/api/warehouse-mappings/${id}` : '/api/warehouse-mappings', {
    method: id ? 'PUT' : 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(data)
  });
  const payload = await response.json();
  if (!response.ok) return setMappingMessage(payload.detail || '保存映射失败');
  mappingForm.reset(); mappingForm.elements.enabled.checked = true; document.getElementById('mappingId').value = '';
  await loadMappings(); setMappingMessage('仓库映射已保存。', 'success');
});
document.getElementById('mappingRows').addEventListener('click', event => {
  const button = event.target.closest('.mapping-edit'); if (!button) return;
  const item = mappingCache.find(row => String(row.id) === button.dataset.id); if (!item) return;
  document.getElementById('mappingId').value = item.id;
  [...mappingForm.elements].forEach(el => { if (el.name && el.name !== 'enabled') el.value = item[el.name] || ''; });
  mappingForm.elements.enabled.checked = Boolean(item.enabled); mappingForm.scrollIntoView({behavior:'smooth'});
});
document.getElementById('mappingCancel').addEventListener('click', () => { mappingForm.reset(); mappingForm.elements.enabled.checked = true; document.getElementById('mappingId').value = ''; });
document.getElementById('mappingImport').addEventListener('change', async event => {
  if (!event.target.files.length) return;
  const body = new FormData(); body.append('file', event.target.files[0]);
  const response = await fetch('/api/warehouse-mappings/import', {method:'POST', body}); const payload = await response.json();
  if (!response.ok) return setMappingMessage(payload.detail || '导入失败');
  await loadMappings(); setMappingMessage(`映射导入完成：新增 ${payload.inserted}，更新 ${payload.updated}，跳过 ${payload.skipped}。`, 'success');
});


document.getElementById('mappingPrev').addEventListener('click',()=>{mappingPage--;loadMappings(true);});
document.getElementById('mappingNext').addEventListener('click',()=>{mappingPage++;loadMappings(true);});
