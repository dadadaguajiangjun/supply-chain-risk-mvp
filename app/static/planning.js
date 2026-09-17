// Preserve existing bookmarks after moving the tools into their own pages.
if(location.hash==='#upload')location.replace(workspaceUrl('/data-import'));
if(location.hash==='#mapping')location.replace(workspaceUrl('/master-data#mapping'));
const planningDate = document.getElementById('planningDate');
const planningWarehouse = document.getElementById('planningWarehouse');
const planningMaterial = document.getElementById('planningMaterial');

function numberText(value) {
  return Number(value || 0).toLocaleString('zh-CN', { maximumFractionDigits: 2 });
}

let planningData = [], planningPage = 1, planningTotal = 0;
const planningExtra = [['planningOwner','material_owner','material_owners'], ['planningBuyer','purchase_owner','purchase_owners'], ['planningSupplier','supplier','suppliers'], ['planningCategory','material_category','material_categories'], ['planningArea','transfer_area','transfer_areas']];
function renderPlanningRows() {
  const pageSize = 100;
  document.getElementById('planningRows').innerHTML = planningData.map(row => {
    const text = [row.material_owner,row.purchase_owner,row.warehouse,row.supplier,row.arrival_mode,row.material_name,row.material_code,row.transfer_area];
    const numbers = [row.transfer_summary_3d,row.purchase_data_future_7d,row.inventory,row.inventory_plus_transfer,row.inventory_plus_transfer_purchase,row.booking_avg_7d,row.booking_3d,row.booking_today,row.saleable_booking_avg_7d,row.saleable_booking_3d,row.saleable_booking_today];
    return `<tr>${text.map(v=>`<td>${escapeHtml(v||'—')}</td>`).join('')}${numbers.map(v=>`<td>${numberText(v)}</td>`).join('')}</tr>`;
  }).join('');
  document.getElementById('planningEmpty').classList.toggle('hidden',planningData.length>0);
  document.getElementById('planningCount').textContent = `共 ${planningTotal} 行 · 第 ${planningPage} / ${Math.max(1,Math.ceil(planningTotal/pageSize))} 页`;
  document.getElementById('planningPrev').disabled = planningPage===1;
  document.getElementById('planningNext').disabled = planningPage*pageSize>=planningTotal;
}
const planningChoices = new Map();
function updatePlanningSuggestions(input) {
  const term=input.value.trim().toLocaleLowerCase();
  const entries=(planningChoices.get(input.id)||[]).filter(([label,value])=>`${label} ${value}`.toLocaleLowerCase().includes(term)).slice(0,50);
  const fragment=document.createDocumentFragment();
  entries.forEach(([label])=>fragment.append(new Option(label,label)));
  document.getElementById(input.getAttribute('list')).replaceChildren(fragment);
}
function fillPlanningFilter(input, entries) {
  planningChoices.set(input.id,entries);
  updatePlanningSuggestions(input);
}
function appendPlanningFilter(query,input,key) {
  const text=input.value.trim();
  if(!text)return;
  const selected=(planningChoices.get(input.id)||[]).find(([label,value])=>label===text||value===text);
  query.set(key,selected?selected[1]:text);
  if(!selected)query.append('search_fields',key);
}
let planningRequest = 0;
let planningAbort = null, planningFiltersVersion = '';
async function loadPlanningDashboard(keepPage = false) {
  if (!planningDate?.value) return;
  if (keepPage !== true) planningPage=1;
  const request = ++planningRequest;
  planningAbort?.abort();planningAbort=new AbortController();
  document.getElementById('planningCount').textContent='正在加载…';
  document.getElementById('planningPrev').disabled=true;
  document.getElementById('planningNext').disabled=true;
  const query = new URLSearchParams({business_date:planningDate.value,page:planningPage,page_size:100});
  if(planningFiltersVersion)query.set('filters_version',planningFiltersVersion);
  appendPlanningFilter(query,planningWarehouse,'warehouse');
  appendPlanningFilter(query,planningMaterial,'material_code');
  planningExtra.forEach(([id,key])=>appendPlanningFilter(query,document.getElementById(id),key));
  try {
    const response=await fetch(`/api/planning-dashboard?${query}`,{signal:planningAbort.signal});
    if(!response.ok) throw new Error('看板读取失败，请检查服务后重试');
    const payload=await response.json(); if(request!==planningRequest)return;
    planningData=payload.rows||[]; planningTotal=payload.total??planningData.length; renderPlanningRows();
    document.getElementById('planningSnapshotLabel').textContent=payload.snapshot_date?`库存批次日期：${payload.snapshot_date}；文件快照日期：${(payload.stock_file_dates||[]).join('、')||'未知'}${payload.stock_date_unknown?'（存在日期未确认记录）':''}`:'暂无库存快照，展示基础资料及已有业务数据';
    if(payload.filters) {
      fillPlanningFilter(planningWarehouse,(payload.filters.warehouses||[]).map(v=>[v,v]));
      fillPlanningFilter(planningMaterial,(payload.filters.materials||[]).map(v=>[`${v.material_code} ${v.material_name||''}`,v.material_code]));
      planningExtra.forEach(([id,,key])=>fillPlanningFilter(document.getElementById(id),(payload.filters[key]||[]).map(v=>[v,v])));
      planningFiltersVersion=payload.filters_version||'';
    }
  } catch(error) { if(error.name==='AbortError'||request!==planningRequest)return;document.getElementById('planningCount').textContent='加载失败，请点击查询重试';document.getElementById('planningSnapshotLabel').textContent=error.message; }
}
if(planningDate) {
  planningDate.value=new Date(Date.now()-new Date().getTimezoneOffset()*60000).toISOString().slice(0,10);
  document.getElementById('planningSearch').addEventListener('click',loadPlanningDashboard);
  document.getElementById('planningPrev').addEventListener('click',()=>{planningPage--;loadPlanningDashboard(true);});
  document.getElementById('planningNext').addEventListener('click',()=>{planningPage++;loadPlanningDashboard(true);});
  const textInputs=[planningWarehouse,planningMaterial,...planningExtra.map(([id])=>document.getElementById(id))];
  textInputs.forEach(input=>{
    input.addEventListener('input',()=>updatePlanningSuggestions(input));
    input.addEventListener('keydown',event=>{if(event.key==='Enter'&&!event.isComposing){event.preventDefault();loadPlanningDashboard();}});
  });
  document.getElementById('planningReset').addEventListener('click',()=>{
    textInputs.forEach(input=>{input.value='';updatePlanningSuggestions(input);});loadPlanningDashboard();
  });
  loadPlanningDashboard();
}
