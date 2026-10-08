/* ===== 인증 분야(섹터) 선택·필터 ===== */
const SC_SECTORS = [['food','식품'],['cosmetics','화장품'],['household','생활용품'],['warehouse','창고'],['transport','운송']];
function SC_label(code){ const f = SC_SECTORS.find(s=>s[0]===code); return f ? f[1] : (code||''); }
function SC_chipBar(view){ return `<div class="chipbar"><button class="chip ${!S.scFilter?'on':''}" onclick="App.scFilter('')">전체</button>${SC_SECTORS.map(([c,l])=>`<button class="chip ${S.scFilter===c?'on':''}" onclick="App.scFilter('${c}')">${l}${S.scFilter===c?' <span class="badge strong">선택</span>':''}</button>`).join('')}</div>`; }
App.scFilter = function(code){ S.scFilter = code || ''; render(); };

/* ---------- A. 신청 생성 모달 분야 선택 ---------- */
const SC_origOpenModal = openModal;
openModal = function(title, body, foot, wide){
  if(typeof body === 'string' && body.indexOf('id="cn-sch"') !== -1 && body.indexOf('id="cn-sector"') === -1){
    const sel = `<div class="field"><label for="cn-sector">인증 분야 <span class="req">*</span></label><select class="in" id="cn-sector" onchange="App.scSyncScheme()">${SC_SECTORS.map(([c,l])=>`<option value="${c}">${l}</option>`).join('')}</select></div>`;
    const anchor = '<div class="field"><label for="cn-sch">';
    body = body.indexOf(anchor) !== -1 ? body.replace(anchor, sel + anchor) : sel + body;
  }
  return SC_origOpenModal(title, body, foot, wide);
};
App.scSyncScheme = function(){
  const sec = ($('#cn-sector')||{}).value || '';
  const sch = $('#cn-sch');
  if(sch){ sch.value = (sec==='warehouse'||sec==='transport') ? 'logistics' : 'product'; sch.dispatchEvent(new Event('change')); }
  const boxes = document.querySelectorAll('input[name=cn-jasa]');
  boxes.forEach(b=>{
    if(sec==='warehouse') b.checked = (b.value==='penyimpanan'||b.value==='pengemasan');
    else if(sec==='transport') b.checked = (b.value==='pendistribusian');
  });
};
const SC_origCnCreate = App.cnCreate;
App.cnCreate = async function(){
  const sec = (($('#cn-sector')||{}).value)||'';
  const before = new Set((RS.cases||[]).map(c=>c.case_id));
  const r = await SC_origCnCreate.apply(this, arguments);
  let cid = null;
  (RS.cases||[]).forEach(c=>{ if(!before.has(c.case_id)) cid = c.case_id; });
  if(!cid){ try{ const rr = await apiFetch('/cases'); if(rr.ok){ const j = await rr.json(); (j.items||j||[]).forEach(c=>{ if(!before.has(c.case_id)) cid = c.case_id; }); } }catch(e){} }
  if(cid && sec){ try{ await apiFetch('/cases/'+cid+'/sector', {method:'POST', body:JSON.stringify({sector:sec, sectors:[sec]})}); }catch(e){} try{ if(typeof loadEnt==='function') await loadEnt(); }catch(e){} render(); }
  return r;
};

/* ---------- B. 기업 가입 폼 분야 프리필·저장 ---------- */
const SC_origSubmitEnt = App.submitEnt;
App.submitEnt = async function(e){
  const cat = $('#es-cat');
  if(cat){ const f = SC_SECTORS.find(s=>s[1]===cat.value); S.scSignupSector = f ? f[0] : ''; }
  const r = await SC_origSubmitEnt.apply(this, arguments);
  if(S.role && (RS.cases||[])[0] && S.scSignupSector){
    const cid = RS.cases[0].case_id;
    try{ await apiFetch('/cases/'+cid+'/sector', {method:'POST', body:JSON.stringify({sector:S.scSignupSector, sectors:[S.scSignupSector]})}); }catch(e){}
    render();
  }
  return r;
};
if(S.urlRef){
  apiFetch('/invites/'+encodeURIComponent(S.urlRef)+'/check').then(r=> r.ok ? r.json() : null).then(j=>{
    if(j && j.valid && j.sectors && j.sectors[0]){ S.scInviteSector = j.sectors[0]; render(); }
  }).catch(()=>{});
}
const SC_origRender = render;
render = function(){
  SC_origRender.apply(this, arguments);
  try{
    if(S.scInviteSector && !S.scInviteApplied){
      const cat = $('#es-cat');
      if(cat){
        const lbl = SC_label(S.scInviteSector);
        for(let i=0;i<cat.options.length;i++){ if(cat.options[i].value===lbl || cat.options[i].text===lbl){ cat.selectedIndex = i; break; } }
        S.scInviteApplied = true;
      }
    }
    if(S.view==='cons-new'){
      const note = $('#iv-note');
      if(note && !$('#iv-sector-box')){
        const wrap = note.closest('.field') || note;
        const html = `<div class="field" id="iv-sector-box"><label>분야 <span class="req">*</span></label><div class="inline">${SC_SECTORS.map(([c,l])=>`<label class="tag"><input type="checkbox" name="iv-sector" value="${c}"> ${l}</label>`).join('')}</div></div><div class="field"><label for="iv-contact">담당자</label><input class="in" id="iv-contact"></div><div class="field"><label for="iv-phone">연락처</label><input class="in" id="iv-phone"></div>`;
        wrap.insertAdjacentHTML('afterend', html);
      }
    }
    if(S.scInvitePending && RS.consInv && RS.consInv[0] && RS.consInv[0].invite_id && RS.consInv[0].invite_id !== S.scLastInv){
      const inv = RS.consInv[0].invite_id; const meta = S.scInvitePending;
      S.scLastInv = inv; S.scInvitePending = null;
      apiFetch('/consultant/invites/'+inv+'/meta', {method:'POST', body:JSON.stringify(meta)}).catch(()=>{});
    }
  }catch(e){}
};
document.addEventListener('click', e=>{
  const b = e.target.closest('button');
  if(!b || S.view!=='cons-new') return;
  if(!/초대|링크/.test(b.innerText)) return;
  const checked = document.querySelectorAll('input[name=iv-sector]:checked');
  if(!checked.length){ e.stopImmediatePropagation(); e.preventDefault(); toast('분야를 1개 이상 선택해 주세요.'); return; }
  S.scInvitePending = {sectors:[...checked].map(x=>x.value), contact_name:(($('#iv-contact')||{}).value||'').trim(), phone:(($('#iv-phone')||{}).value||'').trim()};
}, true);

/* ---------- D. 담당 컨설턴트 변경 요청 ---------- */
{ const f = VIEWS['ent-consultant']; VIEWS['ent-consultant'] = () => f() + `<section class="panel"><div class="panel-head"><h2>담당 컨설턴트 변경</h2></div><p class="note">담당 컨설턴트 변경이 필요하면 사유와 희망 컨설턴트를 적어 요청해 주세요. 관리자 승인 후 반영됩니다.</p><button class="btn btn-primary" onclick="App.scChangeReq()">변경 요청</button></section>`; }
App.scChangeReq = function(){
  openModal('담당 컨설턴트 변경 요청', `<div class="field"><label for="sc-reason">변경 사유 <span class="req">*</span></label><textarea class="in" id="sc-reason" rows="4"></textarea><div class="inline-msg" id="sc-req-msg"></div></div><div class="field"><label for="sc-pref">희망 컨설턴트</label><input class="in" id="sc-pref" placeholder="이름 또는 기관(선택)"></div>`, `<button class="btn" onclick="App.closeModal()">취소</button><button class="btn btn-primary" onclick="App.scChangeReqDo()">요청</button>`);
};
App.scChangeReqDo = async function(){
  const reason = (($('#sc-reason')||{}).value||'').trim();
  const pref = (($('#sc-pref')||{}).value||'').trim();
  if(!reason){ const m = $('#sc-req-msg'); if(m) m.textContent = '사유를 입력하세요'; return; }
  const cid = (RS.cases||[])[0] && RS.cases[0].case_id;
  const orgId = (AUTH && AUTH.org_id) || cid;
  try{
    const r = await apiFetch('/orgs/'+orgId+'/consultant-change-request', {method:'POST', body:JSON.stringify({reason, preferred_consultant:pref})});
    if(!r.ok){
      if(r.status===409){ closeModal(); toast('이미 변경 요청이 접수돼 있습니다.'); return; }
      if(r.status===422){ const m = $('#sc-req-msg'); if(m) m.textContent = '사유를 입력하세요'; else toast('사유를 입력하세요'); return; }
      toast('변경 요청에 실패했습니다.'); return;
    }
    closeModal(); toast('변경 요청을 보냈습니다. 관리자 승인 후 반영됩니다.');
  }catch(e){ toast('변경 요청에 실패했습니다.'); }
};

/* ---------- E. 목록 분야 필터 ---------- */
['adm-flow','aud-dossier','cons-status'].forEach(vid=>{
  if(!VIEWS[vid]) return;
  const f = VIEWS[vid];
  VIEWS[vid] = () => {
    const chips = SC_chipBar(vid);
    const all = RS.cases;
    RS.cases = S.scFilter ? (all||[]).filter(c=>c.sector===S.scFilter) : all;
    try{ return chips + f(); } finally { RS.cases = all; }
  };
});