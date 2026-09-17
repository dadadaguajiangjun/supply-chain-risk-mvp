let purchaseRows = [], transferRows = [];
const numericFields = new Set(['order_qty','supplier_stock_qty','supplier_producible_qty','supplier_estimated_qty','demand_qty','planned_qty']);
const escapeHtml = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function resetForm(id) { document.getElementById(id).reset(); document.querySelector(`#${id} [name=id]`).value=''; }
function formPayload(form) { const data = Object.fromEntries(new FormData(form)); delete data.id; numericFields.forEach(key => { if(key in data) data[key] = Number(data[key] || 0); }); Object.keys(data).forEach(key => { if(data[key] === '') data[key] = null; }); return data; }
async function loadPlans() { const [p,t] = await Promise.all([fetch('/api/purchase-plans').then(r=>r.json()),fetch('/api/transfer-plans').then(r=>r.json())]); purchaseRows=p.rows||[]; transferRows=t.rows||[]; renderPlans(); await loadHistory(); }
function renderPlans() {
  document.getElementById('purchasePlanRows').innerHTML=purchaseRows.map(r=>`<tr>${['supplier_name','material_code','material_name','order_qty','arrival_warehouse','order_time','expected_arrival_time','supplier_stock_qty','supplier_producible_qty','supplier_estimated_qty','status','remark'].map(k=>`<td>${escapeHtml(r[k])}</td>`).join('')}<td><button class="button secondary" onclick="editPlan('purchase',${r.id})">修改</button></td></tr>`).join('');
  document.getElementById('transferPlanRows').innerHTML=transferRows.map(r=>`<tr>${['request_no','from_warehouse','to_warehouse','material_code','material_name','demand_qty','planned_qty','document_status','transport_status','expected_departure_time','expected_arrival_time','remark'].map(k=>`<td>${escapeHtml(r[k])}</td>`).join('')}<td><button class="button secondary" onclick="editPlan('transfer',${r.id})">修改</button></td></tr>`).join('');
}
function editPlan(type,id) { const row=(type==='purchase'?purchaseRows:transferRows).find(item=>item.id===id); const form=document.getElementById(`${type}Form`); Object.entries(row).forEach(([key,value])=>{ const input=form.elements.namedItem(key); if(input) input.value=value??''; }); window.scrollTo({top:0,behavior:'smooth'}); }
async function submitPlan(event,type) { event.preventDefault(); const form=event.currentTarget; const id=form.elements.namedItem('id').value; const response=await fetch(`/api/${type}-plans${id?`/${id}`:''}`,{method:id?'PUT':'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(formPayload(form))}); if(!response.ok){alert('保存失败');return;} resetForm(form.id); loadPlans(); }
document.getElementById('purchaseForm').addEventListener('submit',e=>submitPlan(e,'purchase')); document.getElementById('transferForm').addEventListener('submit',e=>submitPlan(e,'transfer'));
document.querySelectorAll('[data-edit-tab]').forEach(button=>button.addEventListener('click',()=>{ document.querySelectorAll('[data-edit-tab]').forEach(x=>x.classList.remove('active')); button.classList.add('active'); const purchase=button.dataset.editTab==='purchase'; document.getElementById('purchaseSection').classList.toggle('hidden',!purchase); document.getElementById('transferSection').classList.toggle('hidden',purchase); }));
let historyKind='purchase', historyPage=1, historyRows=[], executionTarget=null, historyRequest=0;
for(const type of ['purchase','transfer']) {
  const label=document.createElement('label'); label.textContent='关联正式单号';
  const input=document.createElement('input'); input.name='linked_order_no'; input.placeholder=type==='purchase'?'金蝶单据编号':'OMS需求单号'; label.append(input);
  document.getElementById(`${type}Form`).insertBefore(label,document.querySelector(`#${type}Form .inline-actions`));
}
async function loadHistory() {
  const request=++historyRequest, selectedKind=historyKind;
  const query=new URLSearchParams({page:historyPage});
  for(const [id,key] of [['historySearch','search'],['historyStatus','execution_status'],['historySource','source'],['historyStart','start_date'],['historyEnd','end_date']]) {
    const value=document.getElementById(id).value;if(value)query.set(key,value);
  }
  const response=await fetch(`/api/operations/${selectedKind}?${query}`);
  if(request!==historyRequest)return;
  if(!response.ok){document.getElementById('historyCount').textContent='读取失败，请重试';return;}
  const payload=await response.json();if(request!==historyRequest)return;historyRows=payload.rows;
  document.getElementById('historyTitle').textContent=historyKind==='purchase'?'采购历史与核对':'调拨历史与核对';
  document.getElementById('historyCount').textContent=`共 ${payload.total} 条 · 第 ${historyPage} 页`;
  document.getElementById('historyPrev').disabled=historyPage===1;
  document.getElementById('historyNext').disabled=historyPage*100>=payload.total;
  document.getElementById('historyRows').innerHTML=historyRows.map((r,index)=>`<tr>${[r.source==='formal'?'正式单据':'手工记录',r.display_no,r.material_code,r.material_name,r.warehouse_display,r.quantity,r.business_time,r.flux_received_qty,r.wms_last_inbound_date,r.link_status||r.reconciliation_status,r.original_status,r.execution_status,r.execution_note,r.operator,r.status_updated_at].map(v=>`<td>${escapeHtml(v??'—')}</td>`).join('')}<td><div class="inline-actions">${['已完成','延误','未完成'].map(status=>`<button class="button secondary compact-button" data-status="${status}" data-index="${index}">${status}</button>`).join('')}<button class="button secondary compact-button" data-detail="${index}">核对详情</button></div></td></tr>`).join('');
}
document.querySelectorAll('[data-edit-tab]').forEach(button=>button.addEventListener('click',()=>{historyKind=button.dataset.editTab;historyPage=1;loadHistory();}));
document.getElementById('historyQuery').addEventListener('click',()=>{historyPage=1;loadHistory();});
document.getElementById('historyPrev').addEventListener('click',()=>{historyPage--;loadHistory();});
document.getElementById('historyNext').addEventListener('click',()=>{historyPage++;loadHistory();});
document.getElementById('historyRows').addEventListener('click',event=>{
  const button=event.target.closest('button');if(!button)return;
  if(button.dataset.detail!==undefined){showHistoryDetail(Number(button.dataset.detail));return;}
  executionTarget=historyRows[Number(button.dataset.index)];
  const form=document.getElementById('executionForm');form.elements.execution_status.value=button.dataset.status;
  form.elements.operator.value=localStorage.getItem('executionOperator')||'';
  form.elements.execution_note.value=executionTarget.execution_note||'';
  document.getElementById('executionError').classList.add('hidden');document.getElementById('executionDialog').showModal();
});
document.getElementById('executionCancel').addEventListener('click',()=>document.getElementById('executionDialog').close());
document.getElementById('executionForm').addEventListener('submit',async event=>{
  event.preventDefault();const payload=Object.fromEntries(new FormData(event.currentTarget));
  const response=await fetch(`/api/operations/${historyKind}/${executionTarget.source}/${executionTarget.id}/execution`,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
  if(!response.ok){const error=await response.json();const el=document.getElementById('executionError');el.textContent=typeof error.detail==='string'?error.detail:'请检查填写内容';el.classList.remove('hidden');return;}
  localStorage.setItem('executionOperator',payload.operator);document.getElementById('executionDialog').close();await loadHistory();
});
const detailLabels={order_no:'采购单号',request_no:'需求单号',temporary_no:'临时编号',linked_order_no:'指定正式单号',resolved_order_no:'匹配正式单号',supplier_name:'供应商',purchase_date:'采购日期',order_time:'手工下单时间',warehouse:'仓库',arrival_warehouse:'到达仓库',from_warehouse:'调出仓库',to_warehouse:'调入仓库',material_code:'物料编码',material_name:'物料名称',purchase_qty:'采购数量',order_qty:'手工采购数量',remaining_qty:'金蝶剩余入库',remaining_wms_qty:'扣除WMS后剩余数量',demand_qty:'需求数量',planned_qty:'计划安排量',outbound_qty:'OMS出库量',inbound_qty:'OMS入库量',delivery_date:'交货日期',expected_departure_time:'预计发车',actual_departure_time:'实际发车',expected_arrival_time:'预计送达',actual_arrival_time:'实际送达',flux_received_qty:'WMS收货',received_difference:'WMS与系统收货差异',formal_qty:'对应正式数量',quantity_difference:'手工减正式数量',execution_status:'人工执行状态',execution_note:'情况说明',remark:'备注',line_remark:'行备注',link_status:'关联情况',reconciliation_status:'核对情况',counts_as_manual:'是否临时计入'};
async function showHistoryDetail(index) {
  const row=historyRows[index];
  const response=await fetch(`/api/operations/${historyKind}/${row.source}/${row.id}/audit`);const audit=response.ok?(await response.json()).rows:[];
  let html=`<div class="table-scroll"><table><tbody>${Object.entries(detailLabels).filter(([key])=>row[key]!==undefined&&row[key]!==null).map(([key,label])=>`<tr><th>${label}</th><td>${escapeHtml(row[key])}</td></tr>`).join('')}</tbody></table></div>`;
  html+='<h3>WMS收货原始记录</h3><div class="table-scroll"><table><thead><tr><th>参考编号3</th><th>仓库</th><th>物料编码</th><th>ASN单号</th><th>事务编号</th><th>预期数量</th><th>收货数量</th><th>入库日期</th></tr></thead><tbody>'+(row.wms_records||[]).map(r=>`<tr>${['reference_no3','warehouse_name','material_code','asn_no','transaction_no','expected_qty','received_qty','inbound_date'].map(k=>`<td>${escapeHtml(r[k])}</td>`).join('')}</tr>`).join('')+'</tbody></table></div>';
  html+='<h3>关联手工记录</h3>'+(row.manual_records||[]).map(r=>`<p>${escapeHtml(r.temporary_no)}：手工减正式 ${escapeHtml(r.quantity_difference)}；${escapeHtml(r.execution_status)}</p>`).join('');
  html+='<h3>状态修改历史</h3>'+audit.map(r=>`<p>${escapeHtml(r.created_at)} · ${escapeHtml(r.operator)} · ${escapeHtml(r.execution_status)} · ${escapeHtml(r.execution_note)}</p>`).join('');
  document.getElementById('historyDetailBody').innerHTML=html;document.getElementById('historyDetail').showModal();
}
document.getElementById('historyDetailClose').addEventListener('click',()=>document.getElementById('historyDetail').close());
loadPlans();
