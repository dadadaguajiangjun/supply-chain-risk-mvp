const definitions = {
  'material-controls': {
    title: '物料管控表', eyebrow: 'MATERIAL CONTROL', key: 'id',
    fields: [['material_owner','物料负责人','text',true], ['purchase_owner','采购负责人','text'],
      ['material_code','物料编码','text',true], ['material_name','物料名称','text',true],
      ['enabled','启用（1启用 / 0停用）','select',true,['1','0']]],
  },
  materials: {
    title: '物料信息', eyebrow: 'MATERIAL', key: 'material_code',
    fields: [
      ['material_code','物料编码','text',true], ['material_name','物料名称','text',true],
      ['material_category','物料分类','select',false,['短保','长保','限定']], ['supply_time','供应时间','text'],
      ['unit','单位','text'], ['minimum_package','最小包装','text'],
      ['package_content','最小包装多少克/多少毫升','text'], ['case_pack','箱规','text'],
      ['volume','体积','number'], ['weight','重量','number'], ['safety_stock','安全库存','number'],
    ],
  },
  suppliers: {
    title: '供应商信息', eyebrow: 'SUPPLIER', key: 'supplier_key',
    fields: [
      ['supplier_code','供应商编码','text',true], ['supplier_name','名称','text',true],
      ['material_code','物料编码','text',true], ['material_name','物料名称','text',true],
      ['arrival_mode','到仓方式','select',false,['自提','含运']], ['warehouse','对应仓库','text'],
      ['vehicle_type','车型','text'], ['loading_qty','装载数量','number'], ['loading_package','装载箱/包','text'],
    ],
  },
  warehouses: {
    title: '仓库信息', eyebrow: 'WAREHOUSE', key: 'warehouse_key',
    fields: [['warehouse_english','仓库英文','text',true], ['warehouse_name','仓库名称','text',true], ['capacity','仓库库容','text']],
  },
  'transfer-routes': {
    title: '调拨路线', eyebrow: 'TRANSFER ROUTE', key: 'id',
    fields: [
      ['from_warehouse','调出仓库','text',true], ['to_warehouse','调入仓库','text',true],
      ['available_time','可用时间','text',false,null,'全年、全周、全月、具体日期、单数或双数'],
      ['theoretical_departure_time','理论发车时间','text'], ['theoretical_arrival_time','理论到货时间','text'],
      ['time_requirement','时间要求','text'],
    ],
  },
};
let currentMaster = 'materials';
let currentRows = [];
let masterPage = 1, masterRequest = 0, masterTotal = 0;
let editingKey = null;
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

function renderForm() {
  const definition = definitions[currentMaster];
  document.getElementById('maintenanceTitle').textContent = `手工维护${definition.title}`;
  document.getElementById('xlsxTemplate').href = `/api/master-data/${currentMaster}/template?format=xlsx`;
  document.getElementById('csvTemplate').href = `/api/master-data/${currentMaster}/template?format=csv`;
  const fields = definition.fields.map(([key,label,type,required,options,placeholder]) => {
    const requiredText = required ? ' required' : '';
    if (type === 'select') return `<label>${label}<select name="${key}"${requiredText}><option value="">请选择</option>${(options || []).map(value => `<option value="${esc(value)}">${esc(value)}</option>`).join('')}</select></label>`;
    return `<label>${label}<input name="${key}" type="${type}"${type === 'number' ? ' step="any"' : ''}${requiredText}${placeholder ? ` placeholder="${esc(placeholder)}"` : ''}></label>`;
  }).join('');
  document.getElementById('masterForm').innerHTML = `${fields}<div class="inline-actions master-form-actions"><button class="button primary" type="submit">保存</button><button id="masterCancel" class="button secondary" type="button">取消编辑</button></div>`;
  document.getElementById('masterCancel').addEventListener('click', resetForm);
}
function showMessage(text, error = false) { const element = document.getElementById('masterMessage'); element.textContent = text; element.className = `message ${error ? 'error' : 'success'}`; }
function resetForm() { editingKey = null; const form=document.getElementById('masterForm'); form.reset(); if(currentMaster==='material-controls')form.elements.enabled.value='1'; document.getElementById('masterCancel').classList.add('hidden'); }
function editMaster(index) {
  const row = currentRows[index]; const definition = definitions[currentMaster]; editingKey = row[definition.key]; const form = document.getElementById('masterForm');
  definition.fields.forEach(([key]) => { const input = form.elements.namedItem(key); if (input) input.value = row[key] ?? ''; });
  document.getElementById('masterCancel').classList.remove('hidden'); document.querySelector('.master-maintenance').scrollIntoView({behavior:'smooth',block:'start'});
}
async function loadMaster(keepPage = false) {
  if(currentMaster==='warehouse-mappings')return;
  if(keepPage!==true)masterPage=1;
  const request = ++masterRequest;
  const definition = definitions[currentMaster]; const query = new URLSearchParams({page:masterPage,page_size:100}); const search = document.getElementById('masterSearch').value.trim(); if (search) query.set('search', search);
  document.getElementById('masterPrev').disabled=true;document.getElementById('masterNext').disabled=true;
  const response = await fetch(`/api/master-data/${currentMaster}?${query}`); const payload = await response.json();
  if (request !== masterRequest) return;
  if (!response.ok) { showMessage(payload.detail || '基础数据读取失败', true); return; }
  currentRows = payload.rows || []; masterTotal = payload.total ?? currentRows.length;
  document.getElementById('masterTitle').textContent = definition.title; document.getElementById('masterEyebrow').textContent = definition.eyebrow;
  renderMasterRows();
}
function renderMasterRows() {
  const definition = definitions[currentMaster];
  document.getElementById('masterHead').innerHTML = `<tr>${definition.fields.map(([,label]) => `<th>${label}</th>`).join('')}<th>操作</th></tr>`;
  document.getElementById('masterRows').innerHTML = currentRows.map((row,index) => `<tr>${definition.fields.map(([key]) => `<td>${esc(row[key]) || '—'}</td>`).join('')}<td><button class="button secondary compact-button" type="button" data-edit-index="${index}">修改</button></td></tr>`).join('');
  document.querySelectorAll('[data-edit-index]').forEach(button => button.addEventListener('click', () => editMaster(Number(button.dataset.editIndex))));
  document.getElementById('masterEmpty').classList.toggle('hidden', currentRows.length > 0);
  document.getElementById('masterCount').textContent = `共 ${masterTotal} 条 · 第 ${masterPage} / ${Math.max(1,Math.ceil(masterTotal/100))} 页`;
  document.getElementById('masterPrev').disabled = masterPage===1;
  document.getElementById('masterNext').disabled = masterPage*100>=masterTotal;
}
document.getElementById('masterForm').addEventListener('submit', async event => {
  event.preventDefault(); const definition = definitions[currentMaster]; const values = Object.fromEntries(new FormData(event.currentTarget));
  definition.fields.filter(([, ,type]) => type === 'number').forEach(([key]) => { values[key] = values[key] === '' ? null : Number(values[key]); });
  const url = editingKey === null ? `/api/master-data/${currentMaster}` : `/api/master-data/${currentMaster}/${encodeURIComponent(editingKey)}`;
  const response = await fetch(url, {method: editingKey === null ? 'POST' : 'PUT', headers:{'Content-Type':'application/json'}, body:JSON.stringify(values)}); const payload = await response.json();
  if (!response.ok) { showMessage(payload.detail || '保存失败', true); return; } showMessage('保存成功。'); resetForm(); await loadMaster();
});
document.getElementById('masterImport').addEventListener('change', async event => {
  const file = event.target.files[0]; if (!file) return; const body = new FormData(); body.append('file', file);
  const response = await fetch(`/api/master-data/${currentMaster}/import`, {method:'POST',body}); const payload = await response.json();
  if (!response.ok) { showMessage(payload.detail || '导入失败', true); event.target.value=''; return; }
  const errorText = payload.errors?.length ? `；错误示例：${payload.errors.map(item => `第${item.row}行 ${item.message}`).join('；')}` : '';
  showMessage(`导入完成：新增${payload.inserted}条，更新${payload.updated}条，跳过${payload.skipped}条${errorText}`, payload.skipped > 0); event.target.value=''; await loadMaster();
});
function activateMaster() {
  const key=location.hash==='#mapping'?'warehouse-mappings':location.hash.slice(1);
  const target=Object.hasOwn(definitions,key)||key==='warehouse-mappings'?key:'materials';
  const button=document.querySelector('[data-master="'+target+'"]');
  clearTimeout(window.masterTimer);
  masterRequest++;
  const mapping = button.dataset.master === 'warehouse-mappings';
  document.getElementById('mapping').hidden = !mapping;
  document.getElementById('mapping').classList.toggle('hidden', !mapping);
  document.querySelector('.master-maintenance').classList.toggle('hidden', mapping);
  document.querySelector('.master-list-section').classList.toggle('hidden', mapping);
  document.querySelectorAll('[data-master]').forEach(item => {item.classList.remove('active');item.removeAttribute('aria-current');});
  button.classList.add('active');button.setAttribute('aria-current','page');currentMaster = button.dataset.master; editingKey = null;
  if(mapping) { loadMappings().catch(error=>setMappingMessage(error.message)); return; }
  document.getElementById('masterSearch').value = ''; document.getElementById('masterMessage').className = 'message hidden'; renderForm(); resetForm(); loadMaster();
}
window.addEventListener('hashchange',activateMaster);
document.getElementById('masterSearch').addEventListener('input', () => { clearTimeout(window.masterTimer); window.masterTimer = setTimeout(loadMaster, 250); });
document.getElementById('masterPrev').addEventListener('click',()=>{masterPage--;loadMaster(true);});
document.getElementById('masterNext').addEventListener('click',()=>{masterPage++;loadMaster(true);});
activateMaster();
