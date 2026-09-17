// All pages share one revision. Do not navigate back to unversioned cached HTML.
(() => {
  const revision=document.querySelector('meta[name="workspace-version"]').content;
  const routes=new Set(['/','/data-import','/master-data','/data-edit','/purchase-reconciliation']);
  window.workspaceUrl=(path,version=revision)=>{
    const url=new URL(path,location.origin);
    if(url.origin===location.origin&&routes.has(url.pathname))url.searchParams.set('ui',version);
    return url.pathname+url.search+url.hash;
  };
  // Replace this entry rather than adding an extra Back step; keep query/hash.
  if(routes.has(location.pathname)&&new URL(location.href).searchParams.get('ui')!==revision){
    history.replaceState(history.state,'',workspaceUrl(location.href));
  }
  window.addEventListener('pageshow',async event=>{
    if(!event.persisted)return;
    try {
      const response=await fetch('/api/ui-version',{cache:'no-store'});
      if(!response.ok)return;
      const current=await response.json();
      // Retain forms/filters when restoring the same revision from browser history.
      if(current.version&&current.version!==revision)location.replace(workspaceUrl(location.href,current.version));
    } catch (_) { /* Keep the restored page usable if the local server is stopped. */ }
  });
})();
