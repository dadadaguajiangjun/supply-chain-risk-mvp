const esc2 = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const num = value => Number(value || 0).toLocaleString('zh-CN',{maximumFractionDigits:2});
const reconcileTextFields=[['reconcileKeyword','search'],['reconcileSupplier','supplier'],['reconcileWarehouse','warehouse'],['reconcileMaterial','material']];
let reconcilePage=1, reconcileTotal=0, reconcileRequest=0, reconcileAbort=null;
let reconcileQuery=null;
function reconcileMessage(text) { document.getElementById('reconcileMessage').textContent=text; }
async function loadReconciliation(keepPage=false) {
  if(!document.getElementById('reconcileFilters').reportValidity())return;
  if(!keepPage||!reconcileQuery) {
    reconcilePage=1;
    reconcileQuery=new URLSearchParams({business_date:document.getElementById('reconcileDate').value,days:document.getElementById('reconcileDays').value,page_size:100});
    reconcileTextFields.forEach(([id,key])=>reconcileQuery.set(key,document.getElementById(id).value.trim()));
  }
  const query=new URLSearchParams(reconcileQuery);query.set('page',reconcilePage);
  const request=++reconcileRequest;
  reconcileAbort?.abort();reconcileAbort=new AbortController();
  const prev=document.getElementById('reconcilePrev'),next=document.getElementById('reconcileNext');
  prev.disabled=true;next.disabled=true;
  document.getElementById('reconcileCount').textContent='正在加载…';
  reconcileMessage('');
  try {
    const response=await fetch(`/api/purchase-reconciliation?${query}`,{signal:reconcileAbort.signal});
    if(!response.ok)throw new Error('核对数据读取失败，请检查服务后重新查询。');
    const payload=await response.json();if(request!==reconcileRequest)return;
    const rows=payload.rows||[];reconcileTotal=payload.total??rows.length;
    document.getElementById('reconcileCount').textContent=`共 ${reconcileTotal} 条采购明细 · 第 ${reconcilePage} / ${Math.max(1,Math.ceil(reconcileTotal/100))} 页`;
    document.getElementById('reconcileRows').innerHTML=rows.map(row=>{
      const text=['order_no','purchase_date','supplier_name','warehouse','material_code','material_name'];
      return `<tr>${text.map(key=>`<td>${esc2(row[key])}</td>`).join('')}<td>${num(row.purchase_qty)}</td><td>${esc2(row.delivery_date)}</td>${['remaining_qty','kingdee_received_qty','flux_expected_qty','flux_received_qty','received_difference'].map(key=>`<td>${num(row[key])}</td>`).join('')}<td>${esc2(row.first_inbound_date)||'—'}</td><td>${esc2(row.last_inbound_date)||'—'}</td><td><span class="status-tag">${row.quantity_matched?'是':'否'}</span></td><td><span class="status-tag">${row.arrival_on_time?'是':'否'}</span></td><td><input id="remark-${row.id}" value="${esc2(row.remark)}" placeholder="少入、延迟等原因" aria-label="${esc2(row.order_no)}备注原因"></td><td><button class="button secondary" data-save-remark="${row.id}">保存</button></td></tr>`;
    }).join('');
    document.getElementById('reconcileEmpty').textContent='当前日期范围和筛选条件下没有采购记录。';
    document.getElementById('reconcileEmpty').classList.toggle('hidden',rows.length>0);
    prev.disabled=reconcilePage===1;next.disabled=reconcilePage*100>=reconcileTotal;
  } catch(error) {
    if(error.name==='AbortError'||request!==reconcileRequest)return;
    document.getElementById('reconcileRows').replaceChildren();
    document.getElementById('reconcileEmpty').classList.add('hidden');
    document.getElementById('reconcileCount').textContent='加载失败';
    reconcileMessage(error.message);
  }
}
async function saveRemark(id,button) {
  button.disabled=true;
  try {
    const remark=document.getElementById(`remark-${id}`).value;
    const response=await fetch(`/api/purchase-records/${id}/remark`,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({remark})});
    if(!response.ok)throw new Error('备注保存失败，请重试。');
    reconcileMessage('备注已保存。');
  } catch(error) {reconcileMessage(error.message);} finally {button.disabled=false;}
}
document.getElementById('reconcileRows').addEventListener('click',event=>{
  const button=event.target.closest('[data-save-remark]');if(button)saveRemark(button.dataset.saveRemark,button);
});
document.getElementById('reconcileDate').value=new Date(Date.now()-new Date().getTimezoneOffset()*60000).toISOString().slice(0,10);
document.getElementById('reconcileFilters').addEventListener('submit',event=>{event.preventDefault();loadReconciliation();});
document.getElementById('reconcileReset').addEventListener('click',()=>{reconcileTextFields.forEach(([id])=>document.getElementById(id).value='');loadReconciliation();});
document.getElementById('reconcilePrev').addEventListener('click',()=>{reconcilePage--;loadReconciliation(true);});
document.getElementById('reconcileNext').addEventListener('click',()=>{reconcilePage++;loadReconciliation(true);});
loadReconciliation();
