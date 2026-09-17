function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>'"]/g, char => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;'
  }[char]));
}


function formatNumber(value) { return Number(value||0).toLocaleString('zh-CN',{maximumFractionDigits:2}); }
