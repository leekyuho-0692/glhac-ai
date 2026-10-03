/* ===== 오디터 현장심사 서명·의견·재심 모듈 ===== */
RS.auds = RS.auds || {};

async function AUDS_load(cid){
  try{
    const [rs, ro, rc] = await Promise.all([
      apiFetch('/cases/'+cid+'/onsite/sign'),
      apiFetch('/cases/'+cid+'/onsite/reviewer-opinions'),
      apiFetch('/cases/'+cid+'/onsite-checklist')
    ]);
    if(!rs.ok && !ro.ok){
      RS.auds[cid] = {error:true, status:(rs.status===403||ro.status===403)?403:rs.status};
      return;
    }
    const sign = rs.ok ? (await rs.json()) : {error:true, status:rs.status};
    const opin = ro.ok ? (await ro.json()) : {error:true, status:ro.status};
    const checklist = rc.ok ? (await rc.json()) : {error:true, status:rc.status};
    RS.auds[cid] = {sign, opinions:opin, checklist};
  }catch(e){ RS.auds[cid] = {error:true}; }
}

const AUDS_PARTY_KO = {auditor:'심사원', supervisor:'책임심사원'};

function AUDS_signList(s){
  if(!s || s.error) return '';
  const rows = ['auditor','supervisor'].map(p=>{
    const v = s[p];
    const val = v
      ? `${esc(v.name||'-')} <span class="muted mono">${esc(v.at||'')}</span>`
      : '<span class="badge attn">미서명</span>';
    return `<div><dt>${AUDS_PARTY_KO[p]}</dt><dd>${val}</dd></div>`;
  }).join('');
  return `<dl>${rows}</dl>`;
}

function AUDS_itemLabel(d, key){
  const it = ((d && d.checklist && d.checklist.items) || []).find(x=>x.item_key===key);
  return (it && it.label) ? it.label : key;
}

function AUDS_onsite(){
  const cid = S.selOnsite;
  if(!cid) return '';
  const d = RS.auds[cid];
  if(!d){
    if(RS._audsLoading!==cid){ RS._audsLoading=cid; AUDS_load(cid).then(()=>{ RS._audsLoading=null; render(); }); }
    return '<section class="panel"><h2>현장심사 서명</h2><p class="empty">불러오는 중…</p></section>';
  }
  if(d.error){
    const msg = d.status===403 ? '이 화면을 볼 권한이 없습니다.' : '불러오지 못했습니다.';
    return `<section class="panel"><h2>현장심사 서명</h2><p class="empty">${msg}</p></section>`;
  }
  const s = d.sign || {};
  const ops = (d.opinions && d.opinions.items) || [];
  const opKeys = Object.keys(ops);

  const signBtns = ['auditor','supervisor'].map(p=>{
    if(s[p]) return '';
    return `<button class="btn btn-sm" onclick="App.audsSign('${esc(cid)}','${p}')">${AUDS_PARTY_KO[p]} 서명</button>`;
  }).join(' ');

  const opRows = opKeys.length
    ? `<ul class="rows">${opKeys.map(k=>{
        const v = ops[k];
        const badge = v.opinion==='nonconformity'
          ? '<span class="badge attn">부적합</span>'
          : '<span class="badge strong">적합</span>';
        return `<li class="row"><div style="min-width:0"><div style="font-size:12px">${esc(AUDS_itemLabel(d,k))} ${badge}</div><div>${esc(v.comment||'')}</div><div class="muted mono" style="font-size:11px">${esc(v.actor||'')} · ${esc(v.at||'')}</div></div></li>`;
      }).join('')}</ul>`
    : '<p class="empty">등록된 검토 의견이 없습니다.</p>';

  const items = (d.checklist && d.checklist.items) || [];
  const itemOpts = items.map(x=>`<option value="${esc(x.item_key)}">${esc(x.label||x.item_key)}</option>`).join('');

  const canWrite = (AUTH.role === 'fatwa_liaison' || AUTH.role === 'operator');
  const opForm = canWrite
    ? `<div class="field">
        <label for="auds-item">점검 항목 <span class="req">*</span></label>
        <select class="in" id="auds-item">
          <option value="">— 항목 선택 —</option>
          ${itemOpts}
        </select>
      </div>
      <div class="field">
        <label for="auds-opv">판정 <span class="req">*</span></label>
        <select class="in" id="auds-opv">
          <option value="comply">적합</option>
          <option value="nonconformity">부적합</option>
        </select>
      </div>
      <div class="field">
        <label for="auds-op">의견</label>
        <textarea class="in" id="auds-op" rows="3" placeholder="항목 의견을 입력하세요"></textarea>
      </div>
      <div class="inline"><button class="btn btn-primary" onclick="App.audsOpinion('${esc(cid)}')">의견 등록</button></div>`
    : '<p class="muted">검토 의견은 샤리아 위원·운영관리자가 작성합니다.</p>';

  const docBtns = [
    ['/cases/'+cid+'/factory-audit.pdf', '현장심사 보고서 PDF'],
    ['/cases/'+cid+'/factory-audit.docx', '현장심사 보고서 DOCX'],
    ['/cases/'+cid+'/preassess-report.pdf', '사전심사 보고서 PDF'],
    ['/cases/'+cid+'/mock-audit/ai-report.pdf', '모의심사 AI 보고서 PDF']
  ].map(([p,l])=>`<button class="btn btn-sm" onclick="App.audsDoc('${esc(p)}','${esc(l)}')">${l}</button>`).join(' ');

  return `
    <section class="panel">
      <div class="panel-head"><h2>현장심사 서명</h2><div>${signBtns}</div></div>
      ${AUDS_signList(s)}
    </section>
    <section class="panel">
      <div class="panel-head"><h2>심사원 의견</h2></div>
      ${opRows}
      ${opForm}
    </section>
    <section class="panel">
      <div class="panel-head"><h2>심사 문서 내려받기</h2></div>
      <div class="inline">${docBtns}</div>
    </section>`;
}

function AUDS_padInit(){
  const cv = $('#auds-pad');
  if(!cv || !cv.getContext) return;
  const ctx = cv.getContext('2d');
  ctx.lineWidth = 2; ctx.lineCap = 'round'; ctx.lineJoin = 'round'; ctx.strokeStyle = '#111827';
  let drawing = false;
  const pos = (e)=>{
    const r = cv.getBoundingClientRect();
    const sx = cv.width / (r.width || 1), sy = cv.height / (r.height || 1);
    return {x:(e.clientX - r.left) * sx, y:(e.clientY - r.top) * sy};
  };
  cv.addEventListener('pointerdown', (e)=>{
    drawing = true;
    try{ cv.setPointerCapture(e.pointerId); }catch(_e){}
    const p = pos(e);
    ctx.beginPath(); ctx.moveTo(p.x, p.y);
    e.preventDefault();
  });
  cv.addEventListener('pointermove', (e)=>{
    if(!drawing) return;
    const p = pos(e);
    ctx.lineTo(p.x, p.y); ctx.stroke();
    S.audsInk = true;
    e.preventDefault();
  });
  const stop = ()=>{
    if(!drawing) return;
    drawing = false;
  };
  cv.addEventListener('pointerup', stop);
  cv.addEventListener('pointercancel', stop);
  cv.addEventListener('pointerleave', stop);
}

App.audsPadClear = function(){
  const cv = $('#auds-pad');
  if(cv && cv.getContext){
    const ctx = cv.getContext('2d');
    ctx.clearRect(0, 0, cv.width, cv.height);
  }
  S.audsInk = false;
};

App.audsSign = function(cid, party){
  const ko = AUDS_PARTY_KO[party] || party;
  S.audsInk = false;
  openModal(ko+' 서명', `
    <div class="field">
      <label for="auds-name">서명자 이름 <span class="req">*</span></label>
      <input class="in" id="auds-name" placeholder="이름을 입력하세요">
    </div>
    <div class="field">
      <label>서명 <span class="req">*</span></label>
      <canvas id="auds-pad" width="420" height="140" style="border:1px solid var(--line);border-radius:6px;touch-action:none;background:#fff;max-width:100%"></canvas>
      <div class="inline" style="margin-top:8px">
        <button class="btn btn-sm btn-ghost" onclick="App.audsPadClear()">지우기</button>
      </div>
      <div class="inline-msg" id="auds-sign-err"></div>
    </div>
  `, `<button class="btn" onclick="App.closeModal()">취소</button><button class="btn btn-primary" onclick="App.audsSignSubmit('${esc(cid)}','${party}')">확인</button>`);
  setTimeout(AUDS_padInit, 0);
};

App.audsSignSubmit = async function(cid, party){
  const err = $('#auds-sign-err');
  if(err) err.textContent = '';
  const name = (($('#auds-name')||{}).value||'').trim();
  if(!name){
    if(err) err.textContent = '이름을 입력하세요.';
    return;
  }
  if(!S.audsInk){
    if(err) err.textContent = '서명을 그려 주세요.';
    return;
  }
  const cv = $('#auds-pad');
  if(!cv || !cv.toDataURL){
    if(err) err.textContent = '서명을 그려 주세요.';
    return;
  }
  const image = cv.toDataURL('image/png');
  try{
    const r = await apiFetch('/cases/'+cid+'/onsite/sign', {method:'POST', body:JSON.stringify({party, name, image})});
    if(!r.ok){
      let code = '';
      try{ const j = await r.json(); if(j && j.detail && j.detail.code) code = '('+j.detail.code+')'; }catch(e){}
      return toast('서명 기록에 실패했습니다.'+code);
    }
    App.closeModal();
    delete RS.auds[cid];
    await AUDS_load(cid);
    render();
    toast('서명을 기록했습니다.');
  }catch(e){ toast('서명 기록에 실패했습니다.'); }
};

App.audsOpinion = async function(cid){
  const itemKey = (($('#auds-item')||{}).value||'').trim();
  if(!itemKey) return toast('점검 항목을 선택하세요.');
  const opinion = (($('#auds-opv')||{}).value||'').trim();
  const comment = (($('#auds-op')||{}).value||'').trim();
  try{
    const r = await apiFetch('/cases/'+cid+'/onsite/reviewer-opinion', {method:'POST', body:JSON.stringify({item_key:itemKey, opinion, comment})});
    if(!r.ok){
      if(r.status===403) return toast('권한이 없습니다.');
      let code = '';
      try{ const j = await r.json(); if(j && j.detail && j.detail.code) code = '('+j.detail.code+')'; }catch(e){}
      return toast('의견 등록에 실패했습니다.'+code);
    }
    delete RS.auds[cid];
    await AUDS_load(cid);
    render();
    toast('의견을 등록했습니다.');
  }catch(e){ toast('의견 등록에 실패했습니다.'); }
};

App.audsDoc = async function(path, filename){
  try{
    const r = await apiFetch(path);
    if(!r.ok) return toast('아직 생성되지 않았거나 내려받을 수 없습니다.');
    const blob = await r.blob();
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = filename;
    document.body.appendChild(a); a.click();
    setTimeout(()=>{ URL.revokeObjectURL(a.href); a.remove(); }, 4000);
  }catch(e){ toast('아직 생성되지 않았거나 내려받을 수 없습니다.'); }
};

App.audsFinal = async function(cid, kind){
  const body = {};
  if(kind === 'return'){
    const comment = (($('#auds-ret')||{}).value||'').trim();
    if(!comment) return toast('보완 요청 사유를 입력하세요.');
    body.comment = comment;
  } else {
    body.decision = kind === 'ok' ? 'ok' : 'hold';
    const note = (($('#auds-rec-note')||{}).value||'').trim();
    if(note) body.note = note;
  }
  const path = kind === 'return' ? '/audit-report/return' : '/audit-report/reconfirm';
  try{
    const r = await apiFetch('/cases/'+cid+path, {method:'POST', body:JSON.stringify(body)});
    if(!r.ok){
      let code = '';
      try{ const j = await r.json(); if(j && j.detail && j.detail.code) code = '('+j.detail.code+')'; }catch(e){}
      return toast((kind==='return'?'보완 반려에 실패했습니다.':'수정 확인에 실패했습니다.')+code);
    }
    delete RS.final[cid];
    await loadAud();
    render();
    toast(kind==='return' ? '보완 반려를 기록했습니다.' : '수정 확인을 기록했습니다.');
  }catch(e){ toast(kind==='return' ? '보완 반려에 실패했습니다.' : '수정 확인에 실패했습니다.'); }
};

function AUDS_finalPanel(){
  const cid = S.selFinal;
  if(!cid) return '';
  return `
    <section class="panel">
      <div class="panel-head"><h2>보고서 재심</h2></div>
      <p class="note">보고서에 보완이 필요하면 반려하고, 기업이 재제출하면 수정 내용을 확인합니다.</p>
      <div class="field">
        <label for="auds-ret">보완 요청 사유 <span class="req">*</span></label>
        <textarea class="in" id="auds-ret" rows="3" placeholder="보완이 필요한 부분을 구체적으로 입력하세요"></textarea>
      </div>
      <div class="inline"><button class="btn btn-primary" onclick="App.audsFinal('${esc(cid)}','return')">보완 반려</button></div>
      <hr style="margin:16px 0;border:none;border-top:1px solid var(--line,#e5e7eb)">
      <div class="field">
        <label for="auds-rec-note">확인 메모 (선택)</label>
        <input class="in" id="auds-rec-note" placeholder="수정 내용에 대한 메모">
      </div>
      <div class="inline">
        <button class="btn" onclick="App.audsFinal('${esc(cid)}','ok')">수정 확인(적합)</button>
        <button class="btn btn-ghost" onclick="App.audsFinal('${esc(cid)}','hold')">다시 보완 요청</button>
      </div>
    </section>`;
}

(function(){
  if(VIEWS['aud-report']){
    const f = VIEWS['aud-report'];
    VIEWS['aud-report'] = () => f() + AUDS_onsite();
  }
  if(VIEWS['aud-final']){
    const g = VIEWS['aud-final'];
    VIEWS['aud-final'] = () => g() + AUDS_finalPanel();
  }
})();
