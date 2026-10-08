/* ===== 양식 관리·심사 설정 ===== */
const FM_DEFAULT_LANG = {aud:'en', sha:'id'};
const FM_LANG_DEFAULT = 'ko';

async function FM_loadForms(){
  try{ const r = await apiFetch('/admin/forms'); RS.fm = r.ok ? (await r.json()) : {error:true, status:r.status}; }
  catch(e){ RS.fm = {error:true}; }
}
async function FM_loadSettings(){
  try{ const r = await apiFetch('/admin/audit-settings'); RS.fmSet = r.ok ? (await r.json()) : {error:true, status:r.status}; }
  catch(e){ RS.fmSet = {error:true}; }
}

function FM_formsPanel(){
  const d = RS.fm;
  if(!d){ if(RS._fmLoading!==1){ RS._fmLoading=1; FM_loadForms().then(()=>{ RS._fmLoading=null; render(); }); } return '<section class="panel"><p class="empty">불러오는 중…</p></section>'; }
  if(d.error) return `<section class="panel"><p class="empty">${d.status===403?'권한이 없습니다.':'불러오지 못했습니다.'}</p></section>`;
  const items = d.items||[];
  const rows = items.map(it=>{
    const ver = it.active_version||0;
    const verTxt = ver ? `v${ver}` : '기본 서식';
    const act = (it.versions||[]).find(v=>v.version===ver)||{};
    const vers = (it.versions||[]).map(v=>`<div class="row-act" style="justify-content:space-between;align-items:center;gap:8px"><span><span class="mono">v${v.version}</span> · ${esc(v.filename||'')}${v.note?` · <span class="muted">${esc(v.note)}</span>`:''}${v.applied_at?` · <span class="muted" style="font-size:11px">${esc(v.applied_at)}</span>`:''}</span><span class="row-act"><button class="btn btn-sm" onclick="App.fmActivate('${esc(it.key)}', ${v.version})">이 버전 적용</button><button class="btn btn-sm btn-ghost" onclick="App.fmDownload('${esc(it.key)}', ${v.version})">내려받기</button></span></div>`).join('') || '<p class="empty">버전 이력이 없습니다.</p>';
    return `<li class="row"><div style="flex:1;min-width:0"><div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap"><span class="tag">${esc(it.label||it.key)}</span><span class="badge ${ver?'strong':''}">${verTxt}</span>${act.applied_at?`<span class="muted" style="font-size:11px">적용 ${esc(act.applied_at)}</span>`:''}${it.using_default?'<span class="muted" style="font-size:11px">(시스템 기본 사용 중)</span>':''}</div><details style="margin-top:8px"><summary>버전 ${ (it.versions||[]).length }개</summary><div class="dl" style="margin-top:8px">${vers}</div></details></div><div class="row-act"><button class="btn btn-sm" onclick="App.fmDownload('${esc(it.key)}', null)">현재 서식 내려받기</button><input type="file" accept=".docx" id="fm-file-${esc(it.key)}" style="display:none" onchange="App.fmUpload('${esc(it.key)}')"><button class="btn btn-sm btn-primary" onclick="document.getElementById('fm-file-${esc(it.key)}').click()">새 버전 업로드</button><button class="btn btn-sm btn-ghost" onclick="App.fmActivate('${esc(it.key)}', 0)">기본 서식으로</button></div></li>`;
  }).join('');
  return `<section class="panel" style="margin-bottom:22px"><div class="panel-head"><h2>서식 관리</h2></div><p class="muted" style="margin:8px 0">서식을 끌어다 놓거나 선택하면 새 버전이 되고 즉시 전체 문서에 반영됩니다.</p><ul class="rows">${rows||'<p class="empty">등록된 서식이 없습니다.</p>'}</ul></section>`;
}

App.fmDownload = async function(key, version){
  const q = (version===0||version==null) ? '' : ('?version='+version);
  const r = await apiFetch('/admin/forms/'+encodeURIComponent(key)+'/download'+q);
  if(!r.ok){ toast('파일을 불러올 수 없습니다.'); return; }
  try{
    const blob = await r.blob();
    const fname = (version? (key+'_v'+version) : (key+'_current')) + '.docx';
    const a = document.createElement('a'); a.href = URL.createObjectURL(blob); a.download = fname;
    document.body.appendChild(a); a.click(); setTimeout(()=>{ URL.revokeObjectURL(a.href); a.remove(); }, 4000);
  }catch(e){ toast('파일을 불러올 수 없습니다.'); }
};

App.fmUpload = async function(key){
  const el = document.getElementById('fm-file-'+key);
  const file = el && el.files && el.files[0];
  if(!file) return;
  const reader = new FileReader();
  reader.onload = async ()=>{
    const b64 = String(reader.result||'').split(',')[1] || '';
    try{
      const r = await apiFetch('/admin/forms/'+encodeURIComponent(key), {method:'POST', body:JSON.stringify({filename:file.name, file_b64:b64, note:''})});
      if(!r.ok){ toast('업로드에 실패했습니다.'); return; }
      const j = await r.json().catch(()=>({}));
      RS.fm = undefined;
      await FM_loadForms(); render();
      toast(j.version? ('v'+j.version+' 로 교체했습니다 — 새 문서부터 적용') : '업로드했습니다 — 새 문서부터 적용');
    }catch(e){ toast('업로드에 실패했습니다.'); }
  };
  reader.readAsDataURL(file);
};

App.fmActivate = async function(key, version){
  try{
    const r = await apiFetch('/admin/forms/'+encodeURIComponent(key)+'/activate', {method:'POST', body:JSON.stringify({version: version})});
    if(!r.ok){ toast('적용에 실패했습니다.'); return; }
    RS.fm = undefined; await FM_loadForms(); render();
    toast(version? ('v'+version+' 서식을 적용했습니다.') : '기본 서식으로 되돌렸습니다.');
  }catch(e){ toast('적용에 실패했습니다.'); }
};

function FM_settingsPanel(){
  const d = RS.fmSet;
  if(!d){ if(RS._fmSetLoading!==1){ RS._fmSetLoading=1; FM_loadSettings().then(()=>{ RS._fmSetLoading=null; render(); }); } return '<section class="panel"><p class="empty">불러오는 중…</p></section>'; }
  if(d.error) return `<section class="panel"><p class="empty">${d.status===403?'권한이 없습니다.':'불러오지 못했습니다.'}</p></section>`;
  return `<section class="panel"><div class="panel-head"><h2>심사 설정</h2><button class="btn btn-primary" onclick="App.fmSaveSettings()">저장</button></div><div class="grid-3"><div class="field"><label>1인 최대 동시 담당 건수</label><input class="in" id="fm-max" type="number" min="1" value="${esc(String(d.max_cases_per_auditor!=null?d.max_cases_per_auditor:''))}"></div><div class="field"><label>결정 방식</label><select class="in" id="fm-rule"><option value="majority"${d.decision_rule==='majority'?' selected':''}>과반수</option><option value="unanimous"${d.decision_rule==='unanimous'?' selected':''}>만장일치</option></select></div><div class="field"><label>교육 시간</label><input class="in" id="fm-hours" type="number" min="0" value="${esc(String(d.training_hours!=null?d.training_hours:''))}"></div></div><div class="note">정족수: 3인 고정(변경 불가)</div></section>`;
}

App.fmSaveSettings = async function(){
  const maxRaw = (($('#fm-max')||{}).value||'').trim();
  const hoursRaw = (($('#fm-hours')||{}).value||'').trim();
  const rule = (($('#fm-rule')||{}).value||'majority');
  const max = maxRaw===''? null : parseInt(maxRaw,10);
  const hours = hoursRaw===''? null : Number(hoursRaw);
  if(max!=null && (!isFinite(max)||max<1)){ toast('1인 최대 건수를 확인하세요.'); return; }
  if(hours!=null && (!isFinite(hours)||hours<0)){ toast('교육 시간을 확인하세요.'); return; }
  try{
    const r = await apiFetch('/admin/audit-settings', {method:'POST', body:JSON.stringify({max_cases_per_auditor:max, decision_rule:rule, training_hours:hours})});
    if(!r.ok){ let code=''; try{ const j=await r.json(); code=j&&j.detail&&j.detail.code?(' ('+j.detail.code+')'):''; }catch(e){} toast('저장에 실패했습니다.'+code); return; }
    RS.fmSet = undefined; await FM_loadSettings(); render(); toast('저장했습니다.');
  }catch(e){ toast('저장에 실패했습니다.'); }
};

{ const f = VIEWS['adm-docs']; if(f) VIEWS['adm-docs'] = () => FM_formsPanel() + f(); }
{ const f = VIEWS['adm-experts']; if(f) VIEWS['adm-experts'] = () => f() + FM_settingsPanel(); }

FM_origSetLang = App.setLang;
App.setLang = function(){ if(FM_origSetLang) FM_origSetLang.apply(this, arguments); try{ if(S && S.role) localStorage.setItem('glhac_lang_'+S.role, S.lang); }catch(e){} };

FM_origRender = render;
render = function(){ try{
  if(S && S.role && S.fmLangApplied !== S.role){
    let l = null;
    try{ l = localStorage.getItem('glhac_lang_'+S.role); }catch(e){}
    if(!l) l = FM_DEFAULT_LANG[S.role] || FM_LANG_DEFAULT;
    S.lang = l; S.fmLangApplied = S.role;
  }
}catch(e){}
FM_origRender.apply(this, arguments); };

FM_origGo = App.go;
App.go = function(){ RS.fm = undefined; RS.fmSet = undefined; return FM_origGo.apply(this, arguments); };
