/* ===== 모의심사 · 서류 준비 (GL-HAC v4) ===== */
RS.pr = RS.pr || {};
RS.prEdu = RS.prEdu || {};
RS.prMeta = RS.prMeta || {};
const PR_ST = {not_started:'준비 전', pending_review:'검토 대기', revision_requested:'보완 요청', confirmed:'확인 완료', not_applicable:'해당 없음'};
const PR_BADGE = (st)=> st==='confirmed' ? 'strong' : (st==='revision_requested' ? 'attn' : '');

const PR_load = async (cid)=>{
  try{ const r = await apiFetch('/cases/'+cid+'/prep');
    if(r.ok){ const d = await r.json(); RS.pr[cid] = d; RS.prMeta[cid] = {t:Date.now()}; }
    else RS.pr[cid] = {error:true, status:r.status};
  }catch(e){ RS.pr[cid] = {error:true}; }
};
const PR_loadEdu = async (cid)=>{
  try{ const r = await apiFetch('/cases/'+cid+'/education-docs');
    if(r.ok) RS.prEdu[cid] = await r.json();
    else RS.prEdu[cid] = {error:true, status:r.status};
  }catch(e){ RS.prEdu[cid] = {error:true}; }
};
const PR_loadCase = async (cid)=>{ await PR_load(cid); await PR_loadEdu(cid); };

const PR_loading = (cid, what, fn)=>{
  if(what==='edu'){ if(RS._prEduLoad!==cid){ RS._prEduLoad=cid; PR_loadEdu(cid).then(()=>{ RS._prEduLoad=null; render(); }); } }
  else { if(RS._prLoad!==cid){ RS._prLoad=cid; PR_load(cid).then(()=>{ RS._prLoad=null; render(); }); } }
};

/* ---- 캔버스 서명 ---- */
let PR_pad, PR_pctx, PR_drawing=false, PR_padInitDone=false;
const PR_padInit = ()=>{
  PR_pad = $('#pr-pad'); if(!PR_pad) return;
  PR_pad.width = PR_pad.clientWidth || 480; PR_pad.height = 140;
  PR_pctx = PR_pad.getContext('2d');
  PR_pctx.lineWidth = 2.2; PR_pctx.lineJoin='round'; PR_pctx.lineCap='round';
  PR_pctx.strokeStyle = (S.prInk||'#1f2937');
  PR_drawing = false; PR_padInitDone = true;
  const pos = (e)=>{ const r = PR_pad.getBoundingClientRect(); return {x:(e.touches?e.touches[0].clientX:e.clientX)-r.left, y:(e.touches?e.touches[0].clientY:e.clientY)-r.top}; };
  const down = (e)=>{ e.preventDefault(); PR_drawing=true; const p=pos(e); PR_pctx.beginPath(); PR_pctx.moveTo(p.x,p.y); };
  const move = (e)=>{ if(!PR_drawing) return; e.preventDefault(); const p=pos(e); PR_pctx.lineTo(p.x,p.y); PR_pctx.stroke(); };
  const up = (e)=>{ if(e) e.preventDefault(); PR_drawing=false; };
  PR_pad.addEventListener('mousedown',down); PR_pad.addEventListener('mousemove',move);
  window.addEventListener('mouseup',up);
  PR_pad.addEventListener('touchstart',down,{passive:false}); PR_pad.addEventListener('touchmove',move,{passive:false});
  PR_pad.addEventListener('touchend',up);
};
App.prPadClear = function(){
  if(!PR_pad){ PR_pad = $('#pr-pad'); if(PR_pad){ PR_pctx = PR_pad.getContext('2d'); } }
  if(PR_pad&&PR_pctx) PR_pctx.clearRect(0,0,PR_pad.width,PR_pad.height);
};
const PR_padData = ()=>{ if(!PR_pad) return ''; try{ return PR_pad.toDataURL('image/png'); }catch(e){ return ''; } };

/* ---- 업로드 ---- */
App.prUpload = function(cid, key){
  const inp = document.getElementById('pr-file-'+key); if(!inp || !inp.files || !inp.files[0]) return;
  const f = inp.files[0];
  const fr = new FileReader();
  fr.onload = async ()=>{
    let b64 = String(fr.result||''); const i = b64.indexOf(','); if(i>=0) b64 = b64.slice(i+1);
    try{
      const r = await apiFetch('/cases/'+cid+'/sjph-evidence',{method:'POST', body:JSON.stringify({item_key:key, filename:f.name, file_b64:b64})});
      if(!r.ok){ let d='업로드'; try{ const j=await r.json(); if(j&&j.detail&&j.detail.code) d+=' ('+j.detail.code+')'; }catch(e){} toast(d+'에 실패했습니다.'); return; }
      delete RS.pr[cid]; await PR_load(cid); render(); toast('파일을 업로드했습니다.');
    }catch(e){ toast('업로드에 실패했습니다.'); }
  };
  fr.readAsDataURL(f);
};
App.prPick = function(key){ const inp = document.getElementById('pr-file-'+key); if(inp) inp.click(); };

/* ---- PATCH ---- */
const PR_patch = async (cid, key, status, note)=>{
  let body = {status:status}; if(note) body.note = note;
  try{
    const r = await apiFetch('/cases/'+cid+'/prep/'+key,{method:'PATCH', body:JSON.stringify(body)});
    if(!r.ok){ let t='상태 변경'; if(r.status===403) t='권한이 없습니다'; else { try{ const j=await r.json(); if(j&&j.detail&&j.detail.code) t+=' ('+j.detail.code+')'; }catch(e){} } toast(t); return; }
    delete RS.pr[cid]; await PR_load(cid); render(); toast('변경했습니다.');
  }catch(e){ toast('상태 변경에 실패했습니다.'); }
};
App.prNA = function(cid, key){
  const body = `<p class="muted" style="margin:0 0 10px">해당 없음 사유를 입력하세요.</p><div class="field"><label>사유</label><input class="in" id="pr-na-reason" placeholder="예: 본 업체는 해당 공정이 없습니다"></div>`;
  const foot = `<button class="btn" onclick="App.closeModal()">취소</button><button class="btn btn-primary" onclick="App.prNADo('${cid}','${key}')">확인</button>`;
  openModal('해당 없음 처리', body, foot);
};
App.prNADo = function(cid, key){
  const reason = (($('#pr-na-reason')||{}).value||'').trim();
  if(!reason){ toast('사유를 입력하세요.'); return; }
  closeModal(); PR_patch(cid, key, 'not_applicable', reason);
};
App.prRevert = function(cid, key){ PR_patch(cid, key, 'pending_review', ''); };

/* ---- 오디터 액션 ---- */
App.prConfirm = function(cid, key){ PR_patch(cid, key, 'confirmed', ''); };
App.prRevReq = function(cid, key){
  const body = `<p class="muted" style="margin:0 0 10px">보완 요청 사유를 입력하세요.</p><div class="field"><label>사유</label><input class="in" id="pr-rev-reason" placeholder="보완이 필요한 점"></div>`;
  const foot = `<button class="btn" onclick="App.closeModal()">취소</button><button class="btn btn-primary" onclick="App.prRevReqDo('${cid}','${key}')">확인</button>`;
  openModal('보완 요청', body, foot);
};
App.prRevReqDo = function(cid, key){
  const reason = (($('#pr-rev-reason')||{}).value||'').trim();
  if(!reason){ toast('사유를 입력하세요.'); return; }
  closeModal(); PR_patch(cid, key, 'revision_requested', reason);
};

/* ---- 컨설턴트 의견 ---- */
App.prNote = function(cid, key){
  const body = `<div class="field"><label>의견</label><textarea class="in" id="pr-note" rows="3" placeholder="컨설턴트 의견을 입력하세요"></textarea></div>`;
  const foot = `<button class="btn" onclick="App.closeModal()">취소</button><button class="btn btn-primary" onclick="App.prNoteDo('${cid}','${key}')">등록</button>`;
  openModal('의견 등록', body, foot);
};
App.prNoteDo = async function(cid, key){
  const note = (($('#pr-note')||{}).value||'').trim();
  if(!note){ toast('의견을 입력하세요.'); return; }
  closeModal();
  try{
    const r = await apiFetch('/cases/'+cid+'/prep/'+key+'/note',{method:'POST', body:JSON.stringify({note:note})});
    if(!r.ok){ let t='의견 등록'; if(r.status===403) t='권한이 없습니다'; else { try{ const j=await r.json(); if(j&&j.detail&&j.detail.code) t+=' ('+j.detail.code+')'; }catch(e){} } toast(t); return; }
    delete RS.pr[cid]; await PR_load(cid); render(); toast('의견을 등록했습니다.');
  }catch(e){ toast('의견 등록에 실패했습니다.'); }
};

/* ---- 교육 서류 배포 ---- */
App.prIssueEdu = async function(cid){
  try{
    const r = await apiFetch('/cases/'+cid+'/education-docs/issue',{method:'POST', body:JSON.stringify({})});
    if(!r.ok){ let t='서류 배포'; if(r.status===403) t='권한이 없습니다'; else { try{ const j=await r.json(); if(j&&j.detail&&j.detail.code) t+=' ('+j.detail.code+')'; }catch(e){} } toast(t+'에 실패했습니다.'); return; }
    delete RS.prEdu[cid]; delete RS.pr[cid]; await PR_loadCase(cid); render(); toast('교육·매뉴얼 서류를 배포했습니다.');
  }catch(e){ toast('서류 배포에 실패했습니다.'); }
};

/* ---- 기업 동의 ---- */
App.prConsent = function(cid){
  const body = `<p class="muted" style="margin:0 0 10px">교육·매뉴얼 서류를 확인하고 서명한 뒤 동의해 주세요.</p>
    <div class="field"><label>서명자 이름</label><input class="in" id="pr-signer" placeholder="성명"></div>
    <div class="field"><label>서명</label><canvas id="pr-pad" style="width:100%;height:140px;border:1px solid #d1d5db;border-radius:8px;background:#fff"></canvas>
      <div class="inline"><button class="btn btn-sm btn-ghost" onclick="App.prPadClear()">지우기</button></div></div>
    <div class="field"><label><input type="checkbox" id="pr-agree"> 위 내용에 동의합니다.</label></div>`;
  const foot = `<button class="btn" onclick="App.closeModal()">취소</button><button class="btn btn-primary" onclick="App.prConsentDo('${cid}')">서명·동의 제출</button>`;
  openModal('교육·매뉴얼 서류 서명·동의', body, foot);
  setTimeout(PR_padInit, 0);
};
App.prConsentDo = async function(cid){
  const name = (($('#pr-signer')||{}).value||'').trim();
  const agree = !!($('#pr-agree')||{}).checked;
  if(!name){ toast('서명자 이름을 입력하세요.'); return; }
  if(!agree){ toast('동의 항목을 체크하세요.'); return; }
  const image = PR_padData();
  if(!image){ toast('서명을 입력하세요.'); return; }
  try{
    const r = await apiFetch('/cases/'+cid+'/education-docs/consent',{method:'POST', body:JSON.stringify({name:name, image:image, agree:true})});
    if(!r.ok){ let t='서명·동의'; if(r.status===403) t='권한이 없습니다'; else { try{ const j=await r.json(); if(j&&j.detail&&j.detail.code) t+=' ('+j.detail.code+')'; }catch(e){} } toast(t+'에 실패했습니다.'); return; }
    closeModal(); delete RS.prEdu[cid]; delete RS.pr[cid]; await PR_loadCase(cid); render(); toast('서명·동의가 완료되었습니다.');
  }catch(e){ toast('서명·동의에 실패했습니다.'); }
};

/* ---- 오디터 완료 ---- */
App.prComplete = async function(cid){
  try{
    const r = await apiFetch('/cases/'+cid+'/prep/complete',{method:'POST', body:JSON.stringify({})});
    if(!r.ok){
      if(r.status===403){ toast('권한이 없습니다'); return; }
      if(r.status===409){ let code=''; try{ const j=await r.json(); code=(j&&j.detail&&j.detail.code)||''; }catch(e){}
        if(code==='PREP_INCOMPLETE'){ toast('모든 항목을 확인해야 합니다. (PREP_INCOMPLETE)'); return; }
        if(code==='EDUCATION_CONSENT_REQUIRED'){ toast('교육 서류 서명·동의가 필요합니다. (EDUCATION_CONSENT_REQUIRED)'); return; }
        toast('모의심사를 완료할 수 없습니다. ('+(code||'409')+')'); return; }
      let t='모의심사 완료'; try{ const j=await r.json(); if(j&&j.detail&&j.detail.code) t+=' ('+j.detail.code+')'; }catch(e){} toast(t+'에 실패했습니다.'); return;
    }
    let j={}; try{ j = await r.json(); }catch(e){}
    delete RS.pr[cid]; await PR_load(cid); render();
    toast(j && j.pending ? '서브 오디터 확인 대기' : '모의심사를 완료했습니다.');
  }catch(e){ toast('모의심사 완료에 실패했습니다.'); }
};

/* ---- 공통 렌더 ---- */
const PR_stBadge = (st)=>`<span class="badge ${PR_BADGE(st)}">${esc(PR_ST[st]||st||'')}</span>`;

const PR_docsTable = (cid, edu)=>{
  if(!edu) return '<section class="panel"><p class="empty">불러오는 중…</p></section>';
  if(edu.error) return `<section class="panel"><p class="empty">${edu.status===403?'이 화면을 볼 권한이 없습니다.':'불러오지 못했습니다.'}</p></section>`;
  const docs = edu.docs||[];
  if(!docs.length) return '<section class="panel"><p class="empty">계약 체결 후 배포됩니다.</p></section>';
  const rows = docs.map(d=>`<li class="row"><span>${esc(d.code||'')}</span><span>${esc(d.title||'')}</span><span>${esc(d.status||'')}</span><span class="row-act">${d.gen_doc_id?`<button class="btn btn-sm" onclick="App.genPdf('${esc(d.gen_doc_id)}','${esc(d.title||d.code||'')}')">[보기]</button>`:''}</span></li>`).join('');
  return `<section class="panel"><div class="panel-head"><h2>교육·매뉴얼 서류</h2>${edu.consented?`<span class="badge strong">동의 완료</span>`:''}</div><ul class="rows">${rows}</ul>${edu.consented?`<p class="muted" style="margin-top:10px">서명·동의 완료 ${esc(edu.consented_at||'')}</p>`:''}</section>`;
};

const PR_itemsTable = (cid, d, mode)=>{
  const items = d.items||[];
  const canUp = mode==='ent' || mode==='cons';
  const rows = items.map(it=>{
    const key = it.item_key;
    const notes = (it.notes||[]).slice(-1);
    const note = it.status==='revision_requested' && it.note ? `<span class="note attn">${esc(it.note)}</span>` : (it.note ? `<span class="muted">${esc(it.note)}</span>` : '');
    const cnote = notes.length ? `<span class="muted">의견: ${esc(notes[0].note||'')}</span>` : '';
    let acts = '';
    if(mode==='aud'){
      acts = `<button class="btn btn-sm" onclick="App.prConfirm('${cid}','${key}')">확인</button>
        <button class="btn btn-sm" onclick="App.prRevReq('${cid}','${key}')">보완 요청</button>
        <button class="btn btn-sm btn-ghost" onclick="App.prRevert('${cid}','${key}')">되돌리기</button>`;
    } else if(mode==='cons'){
      if(!it.applicable) acts = '—';
      else acts = `<button class="btn btn-sm" onclick="App.prNote('${cid}','${key}')">의견</button>
        <button class="btn btn-sm" onclick="App.prPick('${key}')">초안 업로드</button><input type="file" id="pr-file-${key}" style="display:none" onchange="App.prUpload('${cid}','${key}')">`;
    } else {
      if(!it.applicable || it.status==='not_applicable') acts = `<button class="btn btn-sm btn-ghost" onclick="App.prRevert('${cid}','${key}')">되돌리기</button>`;
      else acts = `<button class="btn btn-sm" onclick="App.prPick('${key}')">업로드</button><input type="file" id="pr-file-${key}" style="display:none" onchange="App.prUpload('${cid}','${key}')">
        <button class="btn btn-sm btn-ghost" onclick="App.prNA('${cid}','${key}')">해당 없음</button>`;
    }
    const fileHtml = it.uploaded && it.filename ? `<button class="link" onclick="App.docDownload('${esc(it.document_id||'')}','${esc(it.filename||'')}')">[파일] ${esc(it.filename)}</button>` : '';
    return `<li class="row"><span>${esc(it.label||key)}</span><span>${PR_stBadge(it.status)}</span><span>${fileHtml}${note}${cnote}</span><span class="row-act">${acts}</span></li>`;
  }).join('');
  return `<ul class="rows">${rows}</ul>`;
};

/* ---- A. 기업 화면 ---- */
VIEWS['ent-prep'] = () => {
  const c = (RS.cases||[])[0];
  if(!c) return '<section class="panel"><p class="empty">진행 중인 케이스가 없습니다.</p></section>';
  const cid = c.case_id;
  const d = RS.pr[cid]; const edu = RS.prEdu[cid];
  let eduHtml, bodyHtml;
  if(!edu){ PR_loading(cid,'edu'); eduHtml = '<section class="panel"><p class="empty">불러오는 중…</p></section>'; }
  else if(edu.error) eduHtml = `<section class="panel"><p class="empty">${edu.status===403?'이 화면을 볼 권한이 없습니다.':'불러오지 못했습니다.'}</p></section>`;
  else if(!(edu.docs||[]).length && !edu.consented) eduHtml = '<section class="panel"><p class="empty">계약 체결 후 배포됩니다.</p></section>';
  else { eduHtml = PR_docsTable(cid, edu); if(!edu.consented) eduHtml += `<div class="panel" style="padding-top:0"><button class="btn btn-primary" onclick="App.prConsent('${cid}')">한 번에 서명·동의</button></div>`; }
  const eduPanel = `<section class="panel"><div class="panel-head"><h2>교육·매뉴얼 서류 (D-10 ~ D-14)</h2></div>${eduHtml}</section>`;
  if(!d){ PR_loading(cid,'prep'); bodyHtml = '<section class="panel"><p class="empty">불러오는 중…</p></section>'; }
  else if(d.error) bodyHtml = `<section class="panel"><p class="empty">${d.status===403?'이 화면을 볼 권한이 없습니다.':'불러오지 못했습니다.'}</p></section>`;
  else bodyHtml = `<section class="panel"><div class="panel-head"><h2>준비 서류 13종</h2><span class="count">${d.done||0}/${d.applicable||0} · ${d.completion||0}%</span></div>${PR_itemsTable(cid, d, 'ent')}</section>`;
  return `<section class="panel"><div class="panel-head"><h2>${t('nav.ent-prep')||'⑤ 모의심사·서류 준비'}</h2><span class="badge">${esc(c.company_name||'')}</span></div>${_stepper8(c.step8||0, c.hold, c.done)}</section>${eduPanel}${bodyHtml}`;
};

/* ---- B. 오디터 패널 ---- */
const PR_audPanel = (cid)=>{
  const d = RS.pr[cid]; const edu = RS.prEdu[cid];
  if(!d){ PR_loading(cid,'prep'); return '<section class="panel"><p class="empty">불러오는 중…</p></section>'; }
  if(d.error) return `<section class="panel"><p class="empty">${d.status===403?'이 화면을 볼 권한이 없습니다.':'불러오지 못했습니다.'}</p></section>`;
  const need = Math.max(0, (d.applicable||0) - (d.done||0));
  const canComplete = !!d.complete;
  const footBtn = `<button class="btn btn-primary" ${canComplete?'':'disabled'} onclick="App.prComplete('${cid}')">모의심사 완료 · 결과 통지 (D-16)</button>${canComplete?'':`<span class="muted">${d.done||0}/${d.applicable||0} 확인</span>`}`;
  return `<section class="panel"><div class="panel-head"><h2>모의심사 준비 서류 13종</h2><span class="count">${d.done||0}/${d.applicable||0} · ${d.completion||0}%</span></div>${PR_itemsTable(cid, d, 'aud')}</section>
    <section class="panel"><div class="panel-head"><h2>교육·매뉴얼 서류 동의</h2></div>${edu? (edu.error? `<p class="empty">${edu.status===403?'권한이 없습니다.':'불러오지 못했습니다.'}</p>` : (edu.consented? `<p class="muted">서명·동의 완료 ${esc(edu.consented_at||'')}</p>` : `<button class="btn" onclick="App.prIssueEdu('${cid}')">D-10~14 배포</button>`)) : '<p class="muted">불러오는 중…</p>'}</section>
    <section class="panel"><div class="panel-head"><h2>모의심사 완료</h2></div><div class="inline">${footBtn}</div></section>`;
};
if(typeof VIEWS['aud-mock']==='function'){ const _f = VIEWS['aud-mock']; VIEWS['aud-mock'] = () => _f() + (S.selMock? PR_audPanel(S.selMock):''); }

/* ---- C. 컨설턴트 화면 ---- */
VIEWS['cons-prep'] = () => {
  const list = RS.cases||[];
  if(!list.length) return '<section class="panel"><p class="empty">담당 케이스가 없습니다.</p></section>';
  const sel = (S.selCase && list.find(c=>c.case_id===S.selCase)) ? S.selCase : list[0].case_id; S.selCase = sel;
  const c = list.find(x=>x.case_id===sel) || list[0];
  const cid = c.case_id;
  const d = RS.pr[cid]; const edu = RS.prEdu[cid];
  const selHtml = list.length>1 ? `<select class="in" id="pr-case" onchange="App.prSelCase(this.value)">${list.map(x=>`<option value="${esc(x.case_id)}" ${x.case_id===cid?'selected':''}>${esc(x.company_name||'')}</option>`).join('')}</select>` : '';
  let bodyHtml;
  if(!d){ PR_loading(cid,'prep'); bodyHtml = '<section class="panel"><p class="empty">불러오는 중…</p></section>'; }
  else if(d.error) bodyHtml = `<section class="panel"><p class="empty">${d.status===403?'이 화면을 볼 권한이 없습니다.':'불러오지 못했습니다.'}</p></section>`;
  else bodyHtml = `<section class="panel"><div class="panel-head"><h2>준비 서류 13종</h2><span class="count">${d.done||0}/${d.applicable||0} · ${d.completion||0}%</span></div>${PR_itemsTable(cid, d, 'cons')}</section>`;
  let eduHtml;
  if(!edu){ PR_loading(cid,'edu'); eduHtml = '<section class="panel"><p class="empty">불러오는 중…</p></section>'; }
  else if(edu.error) eduHtml = `<section class="panel"><p class="empty">${edu.status===403?'권한이 없습니다.':'불러오지 못했습니다.'}</p></section>`;
  else if(edu.consented) eduHtml = `<section class="panel"><div class="panel-head"><h2>교육·매뉴얼 서류</h2><span class="badge strong">동의 완료</span></div><p class="muted">서명·동의 완료 ${esc(edu.consented_at||'')}</p></section>`;
  else eduHtml = `<section class="panel"><div class="panel-head"><h2>교육·매뉴얼 서류</h2></div><button class="btn" onclick="App.prIssueEdu('${cid}')">배포</button></section>`;
  return `<section class="panel"><div class="panel-head"><h2>${t('nav.cons-prep')||'모의심사 지원'}</h2>${selHtml}</div></section>${bodyHtml}${eduHtml}`;
};
App.prSelCase = function(id){ S.selCase = id; render(); };

/* ---- D. 배지 / 훅 ---- */
NAVC['ent-prep'] = ()=> (RS.cases||[]).reduce((n,c)=> n + ((RS.pr[c.case_id] && !RS.pr[c.case_id].error ? (RS.pr[c.case_id].items||[]).filter(i=>i.status==='revision_requested').length : 0)), 0);
const _prNavcAud = NAVC['aud-mock'];
NAVC['aud-mock'] = ()=> (typeof _prNavcAud==='function' ? _prNavcAud() : 0) + (function(){ let n=0; Object.keys(RS.pr||{}).forEach(k=>{ const d=RS.pr[k]; if(d&&!d.error&&d.items) n += d.items.filter(i=>i.status==='pending_review').length; }); return n; })();

if(typeof NAV!=='undefined'){
  NAV.ent = NAV.ent||[];
  const iEnt = NAV.ent.indexOf('ent-contract');
  if(NAV.ent.indexOf('ent-prep')<0){ if(iEnt>=0) NAV.ent.splice(iEnt+1,0,'ent-prep'); else NAV.ent.push('ent-prep'); }
  NAV.cons = NAV.cons||[];
  const iCons = NAV.cons.indexOf('cons-contract');
  if(NAV.cons.indexOf('cons-prep')<0){ if(iCons>=0) NAV.cons.splice(iCons+1,0,'cons-prep'); else NAV.cons.push('cons-prep'); }
}
Object.assign(I18N.ko, {'nav.ent-prep':'⑤ 모의심사·서류 준비','nav.cons-prep':'모의심사 지원'});
if(I18N.en) Object.assign(I18N.en, {'nav.ent-prep':'Mock Audit Prep','nav.cons-prep':'Mock Audit Support'});
if(I18N.id) Object.assign(I18N.id, {'nav.ent-prep':'Persiapan Audit Simulasi','nav.cons-prep':'Dukungan Audit Simulasi'});

const _prGo = App.go;
App.go = function(v){ RS.pr = {}; RS.prEdu = {}; RS._prLoad=null; RS._prEduLoad=null; return _prGo.apply(this, arguments); };
