/* ===== 정식 인증 신청 (formal-app) ===== */
const FA_FIELD_KO = {
  process_summary:'제조·공정 요약', halal_management:'할랄 관리 체계',
  non_halal_handling:'비할랄 제품 취급', non_halal_desc:'비할랄 취급 내용',
  preferred_audit_period:'희망 심사 시기', sales_channels:'판매처', signer_name:'대표자 성명'
};

RS.faElig = RS.faElig || {};
RS.faAi2 = RS.faAi2 || {};

async function FA_loadElig(cid){
  try{ const r = await apiFetch('/cases/'+cid+'/eligibility'); RS.faElig[cid] = r.ok ? (await r.json()) : {error:true, status:r.status}; }
  catch(e){ RS.faElig[cid] = {error:true}; }
}

async function FA_loadAi2(cid){
  try{ const r = await apiFetch('/cases/'+cid+'/ai-second-analysis'); RS.faAi2[cid] = r.ok ? (await r.json()) : {error:true, status:r.status}; }
  catch(e){ RS.faAi2[cid] = {error:true}; }
}

function FA_steps(e){
  const f = (e && e.formal) || {};
  let i;
  if(e && e.formal && f.status==='accepted') i = 6;
  else if(f.status==='submitted') i = 5;
  else if(e && e.verdict) i = 4;
  else if(e && e.preassess_ready) i = 3;
  else i = 2;
  const labels = ['자료 제출','AI 분석','오디터 판정','AI 2차 · 인증 가능 판정','정식 신청','관리자 접수','완료'];
  return `<ol style="display:flex;flex-wrap:wrap;gap:6px;list-style:none;padding:0;margin:0 0 12px">${labels.map((l,n)=>`<li style="padding:4px 10px;border-radius:14px;font-size:12px;border:1px solid var(--line);${n<i?'opacity:.6;':''}${n===i?'background:var(--brand,#14532d);color:#fff;border-color:transparent;font-weight:600;':''}">${n+1}. ${esc(l)}</li>`).join('')}</ol>`;
}

function FA_statusText(f){
  const st = f && f.status;
  if(st==='submitted') return '제출됨 · 접수 대기';
  if(st==='returned') return '반려됨';
  if(st==='accepted') return '접수 완료';
  return '미제출';
}

function FA_audPanel(cid){
  const e = RS.faElig[cid];
  if(!e){
    if(RS._faELoading!==cid){ RS._faELoading=cid; FA_loadElig(cid).then(()=>{ RS._faELoading=null; render(); }); }
    return '<section class="panel"><p class="empty">불러오는 중…</p></section>';
  }
  if(e.error) return `<section class="panel"><p class="empty">${e.status===403?'이 화면을 볼 권한이 없습니다.':'불러오지 못했습니다.'}</p></section>`;
  const f = e.formal || {};
  let h = `<section class="panel"><div class="panel-head"><h2>인증 가능 판정 · 정식 신청 (FRM-07)</h2></div>`;
  h += FA_steps(e);
  h += `<div style="display:flex;flex-direction:column;gap:16px;margin-top:14px">`;
  // AI 2차
  const ai2Ready = e.preassess_ready;
  h += `<div><strong>AI 2차 분석</strong>`;
  if(e.ai2 === false || (e.ai2_probability==null && RS.faAi2[cid] && RS.faAi2[cid].exists===false)) h += `<p class="muted">아직 실행되지 않았습니다.</p>`;
  if(e.ai2_probability!=null){
    const prev = (RS.faAi2[cid] && RS.faAi2[cid].previous_probability);
    const rec = RS.faAi2[cid] && RS.faAi2[cid].recommendation;
    const bl = (RS.faAi2[cid] && RS.faAi2[cid].blockers) || [];
    const m = RS.faAi2[cid] && RS.faAi2[cid].materials;
    h += `<p class="mono">인증 가능성 ${prev!=null?esc(String(prev))+'% → ':''}${esc(String(e.ai2_probability))}%</p>`;
    if(rec) h += `<p class="muted">${esc(rec)}</p>`;
    if(bl.length) h += `<p>${bl.map(b=>`<span class="badge attn">${esc(b)}</span>`).join(' ')}</p>`;
    if(m) h += `<p class="muted">원재료 증빙 ${esc(String(m.with_evidence))}/${esc(String(m.total))}</p>`;
  }
  h += `<button class="btn btn-sm" onclick="App.faRunAi2('${esc(cid)}')" ${ai2Ready?'':'disabled'}>AI 2차 분석 실행</button>`;
  if(!ai2Ready) h += `<p class="muted" style="font-size:12px">사전심사 '가능' 판정 후 실행</p>`;
  h += `</div>`;
  // 판정
  h += `<div><strong>인증 가능 판정</strong>`;
  if(f && e.verdict){
    h += `<p><span class="badge ${e.verdict==='eligible'?'strong':'attn'}">${e.verdict==='eligible'?'인증 가능':'인증 불가'}</span></p>`;
    if(e.reason) h += `<p class="muted">${esc(e.reason)}</p>`;
    if(e.gen_doc_id) h += `<button class="btn btn-sm" onclick="App.genPdf('${esc(e.gen_doc_id)}','D-05_인증가능판정통지서.pdf')">D-05 통지서 PDF</button>`;
  } else if(e.verdict){
    h += `<p><span class="badge strong">${e.verdict==='eligible'?'인증 가능':'인증 불가'}</span></p>`;
    if(e.reason) h += `<p class="muted">${esc(e.reason)}</p>`;
    if(e.gen_doc_id) h += `<button class="btn btn-sm" onclick="App.genPdf('${esc(e.gen_doc_id)}','D-05_인증가능판정통지서.pdf')">D-05 통지서 PDF</button>`;
  } else {
    const canV = e.ai2_probability!=null;
    h += `<button class="btn btn-sm btn-primary" onclick="App.faVerdict('${esc(cid)}','eligible')" ${canV?'':'disabled'}>인증 가능</button> `;
    h += `<button class="btn btn-sm" onclick="App.faVerdict('${esc(cid)}','not_eligible')" ${canV?'':'disabled'}>인증 불가</button>`;
    if(!canV) h += `<p class="muted" style="font-size:12px">AI 2차 분석 후 판정할 수 있습니다.</p>`;
  }
  h += `</div>`;
  // 정식 신청
  h += `<div><strong>정식 신청 (FRM-07)</strong>`;
  h += `<p>${esc(FA_statusText(f))}</p>`;
  if(f.status==='returned' && f.return_reason) h += `<p class="muted">반려 사유: ${esc(f.return_reason)}</p>`;
  if(f.status) h += `<button class="btn btn-sm" onclick="App.faViewFormal('${esc(cid)}')">신청서 보기</button>`;
  h += `</div>`;
  h += `</div></section>`;
  return h;
}

const FA_origPreDetail = (typeof _audPreDetail!=='undefined') ? _audPreDetail : null;
if(FA_origPreDetail){ _audPreDetail = (cur, d) => FA_origPreDetail(cur, d) + FA_audPanel(cur); }

App.faRunAi2 = async function(cid){
  try{
    const r = await apiFetch('/cases/'+cid+'/ai-second-analysis', {method:'POST', body:'{}'});
    if(!r.ok){
      let code=null; try{ code=(await r.json()).detail && (await Promise.resolve()).code; }catch(_e){}
      toast(code==='PREASSESS_NOT_READY'?"사전심사 '가능' 판정 후 실행할 수 있습니다":'AI 2차 분석에 실패했습니다.');
      return;
    }
    delete RS.faElig[cid]; delete RS.faAi2[cid];
    render(); toast('AI 2차 분석을 실행했습니다.');
  }catch(e){ toast('AI 2차 분석에 실패했습니다.'); }
};

App.faVerdict = function(cid, v){
  const body = `<div class="fieldset">${v==='not_eligible'?`<div class="field"><label>판정 사유 <span class="req">*</span></label><textarea class="in" id="fa-reason"></textarea></div>`:''}<div class="field"><label>메모</label><textarea class="in" id="fa-note"></textarea></div></div>`;
  openModal(v==='eligible'?'인증 가능 판정':'인증 불가 판정', body, `<button class="btn" onclick="App.closeModal()">취소</button><button class="btn btn-primary" onclick="App.faVerdictDo('${esc(cid)}','${esc(v)}')">확인</button>`);
};

App.faVerdictDo = async function(cid, v){
  const reason = (($('#fa-reason')||{}).value||'').trim();
  const note = (($('#fa-note')||{}).value||'').trim();
  if(v==='not_eligible' && !reason){ toast('사유를 입력해 주세요.'); return; }
  try{
    const r = await apiFetch('/cases/'+cid+'/eligibility/verdict', {method:'POST', body:JSON.stringify({verdict:v, reason, note})});
    if(!r.ok){
      let j={}; try{ j=await r.json(); }catch(_e){}
      const code=(j.detail&&j.detail.code)||j.code;
      if(r.status===403 || code==='NOT_MAIN_AUDITOR') toast('서브 오디터는 열람·확인만 할 수 있습니다.');
      else if(code==='AI2_REQUIRED') toast('AI 2차 분석 후 판정할 수 있습니다.');
      else if(code==='REASON_REQUIRED') toast('사유를 입력해 주세요.');
      else toast('판정에 실패했습니다.'+(code?' ('+code+')':''));
      return;
    }
    const j = await r.json();
    App.closeModal();
    if(j.pending){ toast('서브 오디터 확인 대기 — 승인 후 판정이 전달됩니다.'); return; }
    delete RS.faElig[cid]; delete RS.faAi2[cid];
    await loadAud(); render(); toast('인증 가능 판정을 등록했습니다.');
  }catch(e){ toast('판정에 실패했습니다.'); }
};

const FA_origQ = (typeof AUDQ!=='undefined' && AUDQ.pre) ? AUDQ.pre : null;
if(FA_origQ){ AUDQ.pre = c => FA_origQ(c) || (c.status==='consultant_review' && c.eligibility==null && c.owner==='auditor'); }

// ===== B. 기업 화면 =====
VIEWS['ent-formal'] = () => {
  const c = (RS.cases||[])[0];
  if(!c) return '<section class="panel"><p class="empty">대상 업체가 없습니다.</p></section>';
  const cid = c.case_id;
  const e = RS.faElig[cid];
  if(!e){ if(RS._faELoading!==cid){ RS._faELoading=cid; FA_loadElig(cid).then(()=>{ RS._faELoading=null; render(); }); } return '<section class="panel"><p class="empty">불러오는 중…</p></section>'; }
  if(e.error) return `<section class="panel"><p class="empty">${e.status===403?'이 화면을 볼 권한이 없습니다.':'불러오지 못했습니다.'}</p></section>`;
  const f = e.formal || {};
  if(e.verdict!=='eligible'){
    let h = `<section class="panel"><div class="panel-head"><h2>③ 정식 인증 신청</h2></div>`;
    h += FA_steps(e);
    h += `<p class="note attn">인증 가능 판정 후 작성할 수 있습니다.</p>`;
    if(e.verdict==='not_eligible') h += `<p class="muted">${esc(e.reason||'')} — 보완 후 재신청해 주세요.</p>`;
    return h + `</section>`;
  }
  if(f.status==='submitted' || f.status==='accepted'){
    let h = `<section class="panel"><div class="panel-head"><h2>③ 정식 인증 신청</h2></div>`;
    h += FA_steps(e);
    if(f.status==='submitted') h += `<p class="note attn">접수 대기 중${f.at?' · 제출 '+esc(String(f.at)):''}</p>`;
    else h += `<p class="note">접수 완료 — 견적서·계약서를 기다려 주세요.</p>`;
    h += `<button class="btn btn-sm" onclick="App.faViewFormal('${esc(cid)}')">제출한 신청서 보기</button> `;
    if(f.status==='accepted') h += `<button class="btn btn-sm btn-primary" onclick="App.go('ent-contract')">④ 계약으로 이동</button>`;
    return h + `</section>`;
  }
  // form
  let h = `<section class="panel"><div class="panel-head"><h2>③ 정식 인증 신청 (FRM-04)</h2></div>`;
  h += FA_steps(e);
  if(f.status==='returned' && f.return_reason) h += `<p class="note attn">반려 사유: ${esc(f.return_reason)}</p>`;
  h += `<div class="fieldset"><div class="field"><label>제조·공정 요약 <span class="req">*</span></label><textarea class="in" id="fa-process"></textarea></div>`;
  h += `<div class="field"><label>할랄 관리 체계 <span class="req">*</span></label><textarea class="in" id="fa-mgmt"></textarea></div>`;
  h += `<div class="field"><label>비할랄 제품 취급 <input type="checkbox" id="fa-nonhalal"></label><textarea class="in" id="fa-nonhalal-desc" placeholder="비할랄 취급 내용"></textarea></div>`;
  h += `<div class="field"><label>희망 심사 시기</label><input class="in" id="fa-period"></div>`;
  h += `<div class="field"><label>판매처</label><input class="in" id="fa-sales"></div>`;
  h += `<div class="field"><label><input type="checkbox" id="fa-pledge"> 신청 내용이 사실이며 할랄 기준을 준수할 것을 서약합니다 <span class="req">*</span></label></div>`;
  h += `<div class="field"><label>대표자 성명 <span class="req">*</span></label><input class="in" id="fa-signer"></div>`;
  h += `<div class="field"><label>서명</label><canvas id="fa-pad" width="420" height="140" style="border:1px solid var(--line);max-width:100%"></canvas><button class="btn btn-sm" onclick="App.faPadClear()">지우기</button></div>`;
  h += `<p class="inline-msg" id="fa-msg"></p>`;
  h += `<button class="btn btn-primary" onclick="App.faSubmit('${esc(cid)}')">정식 인증 신청서 제출</button>`;
  h += `</div></section>`;
  setTimeout(FA_padInit, 0);
  return h;
};

function FA_padInit(){
  const cv = $('#fa-pad');
  if(!cv || !cv.getContext || cv.dataset.init==='1') return;
  cv.dataset.init='1';
  const ctx = cv.getContext('2d');
  ctx.lineWidth = 2; ctx.lineCap = 'round'; ctx.lineJoin = 'round'; ctx.strokeStyle = '#111827';
  let drawing = false;
  const pos = (e)=>{ const r = cv.getBoundingClientRect(); const sx = cv.width/(r.width||1), sy=cv.height/(r.height||1); return {x:(e.clientX-r.left)*sx, y:(e.clientY-r.top)*sy}; };
  cv.addEventListener('pointerdown', (e)=>{ drawing=true; try{ cv.setPointerCapture(e.pointerId); }catch(_e){} const p=pos(e); ctx.beginPath(); ctx.moveTo(p.x,p.y); e.preventDefault(); });
  cv.addEventListener('pointermove', (e)=>{ if(!drawing) return; const p=pos(e); ctx.lineTo(p.x,p.y); ctx.stroke(); S.faInk=true; e.preventDefault(); });
  const stop = ()=>{ if(!drawing) return; drawing=false; };
  cv.addEventListener('pointerup', stop); cv.addEventListener('pointercancel', stop); cv.addEventListener('pointerleave', stop);
}

App.faPadClear = function(){ const cv=$('#fa-pad'); if(cv && cv.getContext){ const ctx=cv.getContext('2d'); ctx.clearRect(0,0,cv.width,cv.height); } S.faInk=false; };

App.faSubmit = async function(cid){
  const msg = $('#fa-msg');
  const val = (id)=>((($(id)||{}).value)||'').trim();
  const data = {
    process_summary: val('#fa-process'), halal_management: val('#fa-mgmt'),
    non_halal_handling: !!(($('#fa-nonhalal')||{}).checked), non_halal_desc: val('#fa-nonhalal-desc'),
    preferred_audit_period: val('#fa-period'), sales_channels: val('#fa-sales'),
    pledge: !!(($('#fa-pledge')||{}).checked), signer_name: val('#fa-signer')
  };
  if(!S.faInk){ if(msg) msg.textContent='서명을 입력해 주세요.'; return; }
  const cv = $('#fa-pad');
  try{ data.signature = cv.toDataURL('image/png'); }catch(_e){ if(msg) msg.textContent='서명을 입력해 주세요.'; return; }
  try{
    const r = await apiFetch('/cases/'+cid+'/formal-application', {method:'POST', body:JSON.stringify(data)});
    if(!r.ok){
      let j={}; try{ j=await r.json(); }catch(_e){}
      const code=(j.detail&&j.detail.code)||j.code;
      if(code==='FIELDS_REQUIRED'){ const fs=(j.detail&&j.detail.fields)||[]; if(msg) msg.textContent='필수 항목: '+fs.map(x=>FA_FIELD_KO[x]||x).join(', '); return; }
      if(code==='PLEDGE_REQUIRED'){ if(msg) msg.textContent='서약에 동의해 주세요.'; return; }
      if(code==='SIGNATURE_REQUIRED'){ if(msg) msg.textContent='서명을 입력해 주세요.'; return; }
      if(code==='ELIGIBILITY_REQUIRED'){ if(msg) msg.textContent='인증 가능 판정 후 제출할 수 있습니다.'; return; }
      if(code==='FORMAL_ALREADY_SUBMITTED'||code==='FORMAL_ALREADY_ACCEPTED'){ if(msg) msg.textContent='이미 제출된 신청서가 있습니다.'; return; }
      if(msg) msg.textContent='제출에 실패했습니다.'+(code?' ('+code+')':'');
      return;
    }
    delete RS.faElig[cid];
    await loadEnt(); render(); toast('정식 인증 신청서를 제출했습니다. 관리자 접수 확인을 기다려 주세요.');
  }catch(e){ if(msg) msg.textContent='제출에 실패했습니다.'; }
};

if(NAV.ent && NAV.ent.indexOf('ent-pre')>=0 && NAV.ent.indexOf('ent-formal')<0){ NAV.ent.splice(NAV.ent.indexOf('ent-pre')+1, 0, 'ent-formal'); }
Object.assign(I18N.ko, {'nav.ent-formal':'③ 정식 인증 신청'});
if(I18N.en) Object.assign(I18N.en, {'nav.ent-formal':'③ Formal Application'});
if(I18N.id) Object.assign(I18N.id, {'nav.ent-formal':'③ Permohonan Resmi'});
NAVC['ent-formal'] = () => { const c=(RS.cases||[])[0]; return (c && c.eligibility==='eligible' && !['submitted','accepted'].includes(c.formal||'')) ? 1 : 0; };

// ===== C. 관리자 접수 보드 =====
async function FA_loadOps(){
  try{ const r = await apiFetch('/ops/formal-applications?status=all'); RS.faOps = r.ok ? (await r.json()) : {error:true, status:r.status}; }
  catch(e){ RS.faOps = {error:true}; }
}

function FA_admPanel(){
  const o = RS.faOps;
  if(o===undefined){
    if(!RS._faOpsLoading){ RS._faOpsLoading=true; FA_loadOps().then(()=>{ RS._faOpsLoading=false; render(); }); }
    return '<section class="panel"><p class="empty">불러오는 중…</p></section>';
  }
  if(o.error) return `<section class="panel"><p class="empty">${o.status===403?'이 화면을 볼 권한이 없습니다.':'불러오지 못했습니다.'}</p></section>`;
  const items = (o.items||[]).slice().sort((a,b)=>{
    const rank=s=>({submitted:0, returned:1, accepted:2}[s] ?? 3);
    return rank(a.formal)-rank(b.formal);
  });
  if(!items.length) return '<section class="panel"><p class="empty">정식 인증 신청이 없습니다.</p></section>';
  let h = `<section class="panel"><div class="panel-head"><h2>정식 인증 신청 접수 (${items.length})</h2></div><div class="tbl-wrap"><table><tbody>`;
  items.forEach((it,i)=>{
    const st=it.formal;
    const badge = st==='submitted'?'attn':(st==='accepted'?'strong':'');
    h += `<tr><td>${esc(it.company_name)}</td><td><span class="badge ${badge}">${esc(st||'')}</span></td><td class="muted mono">${esc(it.submitted_at||it.at||'')}</td><td><div class="row-act">`;
    h += `<button class="btn btn-sm" onclick="App.faViewFormal('${esc(it.case_id)}')">신청서 보기</button>`;
    if(st==='submitted'){
      h += `<button class="btn btn-sm" onclick="App.faReturn(${i})">반려</button>`;
      h += `<button class="btn btn-sm btn-primary" onclick="App.faAccept(${i})">접수 확인</button>`;
    }
    h += `</div></td></tr>`;
  });
  h += `</tbody></table></div></section>`;
  return h;
}

App.faReturn = function(i){
  const o=RS.faOps||{}; const items=o.items||[]; const it=items[i]; if(!it) return;
  openModal('신청 반려', `<div class="field"><label>반려 사유 <span class="req">*</span></label><textarea class="in" id="fa-ret-reason"></textarea></div>`, `<button class="btn" onclick="App.closeModal()">취소</button><button class="btn btn-primary" onclick="App.faReturnDo(${i})">반려</button>`);
};

App.faReturnDo = async function(i){
  const o=RS.faOps||{}; const items=o.items||[]; const it=items[i]; if(!it) return;
  const reason=(($('#fa-ret-reason')||{}).value||'').trim();
  if(!reason){ toast('반려 사유를 입력해 주세요.'); return; }
  try{
    const r = await apiFetch('/cases/'+it.case_id+'/formal-application/return', {method:'POST', body:JSON.stringify({reason})});
    if(!r.ok){ toast('반려에 실패했습니다.'); return; }
    App.closeModal(); RS.faOps=undefined; await loadAdm(); render(); toast('반려했습니다.');
  }catch(e){ toast('반려에 실패했습니다.'); }
};

App.faAccept = async function(i){
  const o=RS.faOps||{}; const items=o.items||[]; const it=items[i]; if(!it) return;
  try{
    const r = await apiFetch('/cases/'+it.case_id+'/formal-application/accept', {method:'POST', body:'{}'});
    if(!r.ok){ toast('접수에 실패했습니다.'); return; }
    RS.faOps=undefined; await loadAdm(); render(); toast('접수했습니다 — 계약 관리에서 견적서·계약서를 발송하세요.');
  }catch(e){ toast('접수에 실패했습니다.'); }
};

if(VIEWS['adm-contract']){ const FA_origAdm = VIEWS['adm-contract']; VIEWS['adm-contract'] = () => FA_admPanel() + FA_origAdm(); }
const FA_prevNavc = NAVC['adm-contract'];
NAVC['adm-contract'] = () => (FA_prevNavc?FA_prevNavc():0) + (RS.faOps && RS.faOps.items ? RS.faOps.items.filter(x=>x.formal==='submitted').length : 0);

const FA_origGo = App.go;
App.go = function(v){ RS.faOps = undefined; return FA_origGo.apply(this, arguments); };

// ===== 공통 신청서 보기 =====
App.faViewFormal = async function(cid){
  try{
    const r = await apiFetch('/cases/'+cid+'/formal-application');
    if(!r.ok){ toast('신청서를 불러올 수 없습니다.'); return; }
    const d = await r.json();
    const doc = d.doc || {}; const flds = doc.fields || {};
    let body = `<div class="summary"><div><span class="k">업체</span><span class="v">${esc(doc.company_name||'')}</span></div><div><span class="k">제출 시각</span><span class="v mono">${esc(d.submitted_at||d.at||'')}</span></div></div>`;
    body += `<dl>${Object.keys(FA_FIELD_KO).map(k=>`<div><dt>${esc(FA_FIELD_KO[k])}</dt><dd>${esc(flds[k]==null?'':(typeof flds[k]==='boolean'?(flds[k]?'예':'아니오'):String(flds[k])))}</dd></div>`).join('')}</dl>`;
    body += `<p>서약: ${doc.pledge?'✔ 동의':'—'}</p>`;
    if(doc.signature && /^data:image\//.test(doc.signature)) body += `<img style="max-width:240px;border:1px solid var(--line)" src="${doc.signature}">`;
    const foot = (d.gen_doc_id?`<button class="btn" onclick="App.genPdf('${esc(d.gen_doc_id)}','D-06_정식인증신청서.pdf')">PDF</button>`:'') + `<button class="btn btn-primary" onclick="App.closeModal()">닫기</button>`;
    openModal('정식 인증 신청서 (D-06)', body, foot, true);
  }catch(e){ toast('신청서를 불러올 수 없습니다.'); }
};
