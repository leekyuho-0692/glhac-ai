/* ===== 감사 기록 무결성 검증(구 화면 이식 · 서명 키 경계 구분) ===== */
RS.ach = RS.ach || {};

const ACH_ROLES = ['admin', 'operator', 'auditor', 'fatwa_liaison'];

function ACH_d(iso){ return iso ? String(iso).slice(0,10) : '—'; }

function ACH_ok(){
  return !!(AUTH && ACH_ROLES.indexOf(AUTH.role) !== -1);
}

async function ACH_load(cid){
  try{
    const r = await apiFetch('/cases/'+cid+'/audit-verify');
    RS.ach[cid] = r.ok ? (await r.json()) : {error:true, status:r.status};
  }catch(e){ RS.ach[cid] = {error:true}; }
}

function ACH_panel(cid){
  if(!ACH_ok()) return '';
  if(!cid) return '';

  const d = RS.ach[cid];
  if(!d){
    if(RS._achLoading !== cid){
      RS._achLoading = cid;
      ACH_load(cid).then(()=>{ RS._achLoading = null; render(); });
    }
    return '<section class="panel"><p class="empty">불러오는 중…</p></section>';
  }
  if(d.error){
    if(d.status === 403) return '';
    return '<section class="panel"><p class="empty">무결성 검증을 불러오지 못했습니다.</p></section>';
  }

  const count = Number(d.count || 0);
  const verified = Number(d.verified || 0);
  const legacy = Number(d.legacy || 0);
  const status = d.status;

  let body = '';
  if(status === 'ok'){
    body = `<div class="note"><b>✓ 무결</b><p>처리 기록 ${count}건 전부 현재 서명 키로 검증되었습니다.</p></div>`;
  }else if(status === 'ok_with_legacy'){
    body =
      `<div class="note"><b>✓ 검증 구간 무결</b><p>현재 서명 키로 ${verified}건 검증 · 위·변조 흔적 없음.</p></div>` +
      `<div class="note"><b>이전 서명 체계 ${legacy}건 (${esc(ACH_d(d.legacy_until))} 까지)</b><p>${esc(ACH_d(d.epoch))} 서명 키 변경 이전에 기록된 내역이라 현재 키로는 다시 검증할 수 없습니다. 위·변조로 판정하지 않으며, 기록 자체는 원본 그대로 보존돼 있습니다.</p></div>`;
  }else if(status === 'broken'){
    body = `<div class="note attn"><b>⚠ 무결성 깨짐</b><p>서명 키 변경(${esc(ACH_d(d.epoch))}) 이후 기록 중 서명이 맞지 않는 항목이 있습니다 (기록 ${esc(String(d.broken_at || '').slice(0,8))}…). 운영 책임자에게 즉시 보고하세요.</p></div>`;
  }

  const summary =
    `<section class="summary">` +
      `<div><span class="k">전체</span><span class="v mono">${count}</span></div>` +
      `<div><span class="k">검증됨</span><span class="v mono">${verified}</span></div>` +
      `<div><span class="k">이전 서명 체계</span><span class="v mono">${legacy}</span></div>` +
    `</section>`;

  return `<section class="panel"><div class="panel-head"><h2>감사 기록 무결성</h2><div class="row-act"><button class="btn btn-sm btn-ghost" onclick="App.achRecheck('${esc(cid)}')">다시 검증</button></div></div>${body}${summary}</section>`;
}

App.achRecheck = function(cid){
  if(!cid) return;
  delete RS.ach[cid];
  render();
  toast('무결성을 다시 검증합니다.');
};

(function(){
  ['adm-flow', 'aud-dossier'].forEach((v)=>{
    const f = VIEWS[v];
    if(typeof f === 'function'){
      VIEWS[v] = () => { const h = f(); return h + ACH_panel(S.selFlow); };
    }
  });
})();