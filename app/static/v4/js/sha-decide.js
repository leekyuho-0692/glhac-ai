/* ===== 샤리아 심의 결과 확정 ===== */
RS.shad = RS.shad || {};

const SHAD_DEC = [['approved','적합(가승인)'],['conditional','조건부'],['rejected','부적합']];

async function SHAD_load(cid){
  try{
    const [fr, sr, gr] = await Promise.all([
      apiFetch('/cases/'+cid+'/fatwa').catch(()=>null),
      apiFetch('/cases/'+cid+'/fatwa/status').catch(()=>null),
      apiFetch('/cases/'+cid+'/fatwa/sign').catch(()=>null)
    ]);
    const fatwa  = (fr && fr.ok) ? (await fr.json()) : null;
    const status = (sr && sr.ok) ? (await sr.json()) : null;
    const sign   = (gr && gr.ok) ? (await gr.json()) : null;
    RS.shad[cid] = {fatwa, status, sign};
  }catch(e){ RS.shad[cid] = {fatwa:null, status:null, sign:null}; }
}

function SHAD_esc(v){ return esc(v==null?'':String(v)); }

function SHAD_statusPanel(cid, d){
  const s = d.status || {}, f = d.fatwa || {};
  const dl = `<div class="dl">
    <div><dt>파트와 상태</dt><dd class="mono">${SHAD_esc(s.fatwa_status||'-')}</dd></div>
    <div><dt>결정</dt><dd class="mono">${SHAD_esc(s.decision||f.decision||'-')}</dd></div>
    <div><dt>결정번호</dt><dd class="mono">${SHAD_esc(s.decision_no||f.decision_no||'-')}</dd></div>
    <div><dt>결정일</dt><dd class="mono">${SHAD_esc(s.decided_at||'-')}</dd></div>
    <div><dt>최종승인일</dt><dd class="mono">${SHAD_esc(s.final_approved_at||f.final_approved_at||'-')}</dd></div>
    <div><dt>투표</dt><dd class="mono">${Number(s.votes_approve||0)} / ${Number(s.votes_total||0)}</dd></div>
  </div>`;
  const head = (f.committee_head||(d.fatwa&&d.fatwa.committee_head)||'');
  const sec  = (f.committee_secretary||'');
  const opts = SHAD_DEC.map(([k,l])=>`<option value="${k}"${(s.decision||f.decision)===k?' selected':''}>${l}</option>`).join('');
  const form = `<fieldset>
    <legend>결정 입력</legend>
    <div class="grid-2">
      <div class="field"><label>결정</label><select class="in" id="shad-decision">${opts}</select></div>
      <div class="field"><label>위원장</label><input class="in" id="shad-head" value="${SHAD_esc(head)}"></div>
    </div>
    <div class="field"><label>간사</label><input class="in" id="shad-sec" value="${SHAD_esc(sec)}"></div>
    <div class="field"><label>위원회 의견</label><textarea class="in" id="shad-note" rows="3">${SHAD_esc(f.committee_note||'')}</textarea></div>
    <div class="inline-msg" id="shad-e"></div>
    <button class="btn btn-primary" onclick="App.shadDecide('${cid}')">심의 결과 확정</button>
  </fieldset>`;
  const note = `<p class="note">적합으로 확정하면 가승인 상태가 되고, 운영관리자의 최종승인 후 인증서 발급 단계로 넘어갑니다.</p>`;
  return `<section class="panel">
    <div class="panel-head"><h2>심의 결과 확정</h2></div>
    ${dl}${note}${form}
  </section>`;
}

function SHAD_signPanel(cid, d){
  const sg = (d.sign && d.sign.signatures) || {};
  const keys = Object.keys(sg);
  let tbl;
  if(!keys.length){ tbl = '<p class="empty">등록된 서명이 없습니다.</p>'; }
  else{
    tbl = `<div class="tbl-wrap"><table><thead><tr><th>서명자</th><th>이름</th><th>서명 시각</th></tr></thead><tbody>${
      keys.map(k=>{ const v = sg[k]||{}; return `<tr><td class="mono">${SHAD_esc(k)}</td><td>${SHAD_esc(v.name||'-')}</td><td class="mono">${SHAD_esc(v.at||'-')}</td></tr>`; }).join('')
    }</tbody></table></div>`;
  }
  const btns = `<div class="row-act">
    <button class="btn btn-sm" onclick="App.shadSignOpen('${cid}')">서명하기</button>
    <button class="btn btn-sm" onclick="App.shadDecree('${cid}')">결정문 생성</button>
    <button class="btn btn-sm" onclick="App.shadDecreePdf('${cid}')">결정문 PDF</button>
  </div>`;
  return `<section class="panel">
    <div class="panel-head"><h2>위원 서명·결정문</h2></div>
    ${tbl}${btns}
  </section>`;
}

function SHAD_returnPanel(cid){
  return `<section class="panel">
    <div class="panel-head"><h2>오디터로 반려</h2></div>
    <div class="field"><label>반려 사유 <span class="req">*</span></label><textarea class="in" id="shad-ret" rows="3"></textarea></div>
    <div class="inline-msg" id="shad-ret-e"></div>
    <div class="row-act">
      <button class="btn" onclick="App.shadReturn('${cid}')">오디터에게 반려</button>
    </div>
  </section>`;
}

function SHAD_section(){
  const cid = S.selFatwa;
  if(!cid) return '';
  const d = RS.shad[cid];
  if(!d){
    if(RS._shadLoading !== cid){
      RS._shadLoading = cid;
      SHAD_load(cid).then(()=>{ RS._shadLoading = null; render(); }, ()=>{ RS._shadLoading = null; render(); });
    }
    return '<section class="panel"><p class="empty">불러오는 중…</p></section>';
  }
  if(!d.fatwa && !d.status && !d.sign){
    return '<section class="panel"><p class="empty">불러오지 못했습니다.</p></section>';
  }
  return SHAD_statusPanel(cid, d) + SHAD_signPanel(cid, d) + SHAD_returnPanel(cid);
}

App.shadDecide = async function(cid){
  const decision   = (($('#shad-decision')||{}).value||'').trim();
  const note       = (($('#shad-note')||{}).value||'').trim();
  const head       = (($('#shad-head')||{}).value||'').trim();
  const sec        = (($('#shad-sec')||{}).value||'').trim();
  const eEl        = $('#shad-e');
  if(!decision){ if(eEl) eEl.textContent = '결정을 선택하세요.'; return; }
  if((decision==='conditional'||decision==='rejected') && !note){
    if(eEl) eEl.textContent = '조건부·부적합은 의견이 필요합니다.'; return;
  }
  if(eEl) eEl.textContent = '';
  const body = {decision};
  if(note)  body.committee_note = note;
  if(head)  body.committee_head = head;
  if(sec)   body.committee_secretary = sec;
  try{
    const r = await apiFetch('/cases/'+cid+'/fatwa', {method:'PATCH', body: JSON.stringify(body)});
    if(!r.ok){
      let code = '';
      try{ const j = await r.json(); code = (j && j.detail && j.detail.code) || ''; }catch(_){}
      if(r.status === 403) return toast('권한이 없습니다.');
      return toast('심의 결과 확정에 실패했습니다.' + (code ? ' ('+code+')' : ''));
    }
    delete RS.shad[cid];
    try{ await loadSha(); }catch(_){}
    await SHAD_load(cid);
    render();
    toast('심의 결과를 확정했습니다. 운영관리자 최종승인을 기다립니다.');
  }catch(e){ toast('심의 결과 확정에 실패했습니다.'); }
};

function SHAD_padInit(){
  const cv = document.getElementById('shad-pad');
  if(!cv) return;
  const ctx = cv.getContext('2d');
  let drawing = false;
  const pos = (e)=>{ const r = cv.getBoundingClientRect(); return {x:(e.clientX-r.left)*(cv.width/r.width), y:(e.clientY-r.top)*(cv.height/r.height)}; };
  cv.addEventListener('pointerdown', (e)=>{ drawing = true; S.shadInk = true; try{ cv.setPointerCapture(e.pointerId); }catch(_){} const p = pos(e); ctx.beginPath(); ctx.moveTo(p.x, p.y); });
  cv.addEventListener('pointermove', (e)=>{ if(!drawing) return; const p = pos(e); ctx.strokeStyle = '#111'; ctx.lineWidth = 2; ctx.lineCap = 'round'; ctx.lineTo(p.x, p.y); ctx.stroke(); });
  const end = ()=>{ drawing = false; };
  cv.addEventListener('pointerup', end);
  cv.addEventListener('pointercancel', end);
  cv.addEventListener('pointerleave', end);
}

App.shadPadClear = function(){
  const cv = document.getElementById('shad-pad');
  if(cv){ const ctx = cv.getContext('2d'); ctx.clearRect(0, 0, cv.width, cv.height); }
  S.shadInk = false;
};

App.shadSignOpen = function(cid){
  const def = (AUTH && AUTH.username) || '';
  S.shadInk = false;
  const body = `<div class="field"><label>서명 자격</label><select class="in" id="shad-seat"><option value="chairman">위원장</option><option value="member">위원</option></select></div>
    <div class="field"><label>서명자</label><input class="in" id="shad-signer" value="${esc(def)}"></div>
    <div class="field"><label>서명</label>
      <canvas id="shad-pad" width="420" height="140" style="border:1px solid var(--line);border-radius:6px;touch-action:none;background:#fff;max-width:100%"></canvas>
      <div class="row-act" style="margin-top:8px"><button class="btn btn-sm btn-ghost" onclick="App.shadPadClear()">지우기</button></div>
    </div>
    <div class="inline-msg" id="shad-sign-e"></div>`;
  const foot = `<button class="btn" onclick="App.closeModal()">취소</button>
    <button class="btn btn-primary" onclick="App.shadSignDo('${cid}')">서명 등록</button>`;
  openModal('위원 서명', body, foot);
  setTimeout(SHAD_padInit, 0);
};

App.shadSignDo = async function(cid){
  const name = (($('#shad-signer')||{}).value||'').trim();
  const seat = (($('#shad-seat')||{}).value||'member');
  const eEl = $('#shad-sign-e');
  if(!name){ if(eEl) eEl.textContent = '서명자 이름을 입력하세요.'; return; }
  // 서버 정족수: member 에 'chairman' 이 든 서명 1건 + 서로 다른 서명자 2명 이상. 위원은 이름별로 구분한다.
  const member = seat === 'chairman' ? 'chairman' : 'member:' + name;
  if(!S.shadInk){ if(eEl) eEl.textContent = '서명을 그려 주세요.'; return; }
  const pad = document.getElementById('shad-pad');
  const image = pad ? pad.toDataURL('image/png') : '';
  if(!image){ if(eEl) eEl.textContent = '서명을 그려 주세요.'; return; }
  try{
    const r = await apiFetch('/cases/'+cid+'/fatwa/sign', {method:'POST', body: JSON.stringify({member, name, image})});
    if(!r.ok){
      let code = '';
      try{ const j = await r.json(); code = (j && j.detail && j.detail.code) || ''; }catch(_){}
      if(r.status === 403) return toast('권한이 없습니다.');
      return toast('서명 등록에 실패했습니다.' + (code ? ' ('+code+')' : ''));
    }
    App.closeModal();
    delete RS.shad[cid];
    await SHAD_load(cid);
    render();
    toast('서명을 등록했습니다.');
  }catch(e){ toast('서명 등록에 실패했습니다.'); }
};

App.shadDecree = async function(cid){
  try{
    const r = await apiFetch('/cases/'+cid+'/fatwa/decree', {method:'POST', body: JSON.stringify({})});
    if(!r.ok){
      let code = '';
      try{ const j = await r.json(); code = (j && j.detail && j.detail.code) || ''; }catch(_){}
      if(r.status === 403) return toast('권한이 없습니다.');
      if(r.status === 409 && code === 'FATWA_QUORUM_NOT_MET') return toast('서명 정족수가 부족합니다(위원장 서명 포함 2인 이상).');
      return toast('결정문 생성에 실패했습니다.' + (code ? ' ('+code+')' : ''));
    }
    toast('결정문을 생성했습니다.');
  }catch(e){ toast('결정문 생성에 실패했습니다.'); }
};

App.shadDecreePdf = async function(cid){
  try{
    const r = await apiFetch('/cases/'+cid+'/fatwa/decree.pdf');
    if(!r.ok) return toast(await apiErr(r, '파일을 불러올 수 없습니다.'));
    const blob = await r.blob();
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = 'fatwa_decree.pdf';
    document.body.appendChild(a); a.click();
    setTimeout(()=>{ URL.revokeObjectURL(a.href); a.remove(); }, 4000);
  }catch(e){ toast('파일을 불러올 수 없습니다.'); }
};

App.shadReturn = async function(cid){
  const reason = (($('#shad-ret')||{}).value||'').trim();
  const eEl = $('#shad-ret-e');
  if(!reason){ if(eEl) eEl.textContent = '반려 사유를 입력하세요.'; return; }
  if(eEl) eEl.textContent = '';
  try{
    const r = await apiFetch('/cases/'+cid+'/fatwa/return-to-auditor', {method:'POST', body: JSON.stringify({reason})});
    if(!r.ok){
      let code = '';
      try{ const j = await r.json(); code = (j && j.detail && j.detail.code) || ''; }catch(_){}
      if(r.status === 403) return toast('권한이 없습니다.');
      return toast('반려에 실패했습니다.' + (code ? ' ('+code+')' : ''));
    }
    delete RS.shad[cid];
    try{ await loadSha(); }catch(_){}
    await SHAD_load(cid);
    render();
    toast('오디터에게 반려했습니다.');
  }catch(e){ toast('반려에 실패했습니다.'); }
};

{ const f = VIEWS['sha-review']; VIEWS['sha-review'] = () => f() + SHAD_section(); }
