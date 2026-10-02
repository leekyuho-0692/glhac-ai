/* ===== 자기선언(SEHATI) 경로 ===== */
Object.assign(I18N.ko, {'nav.aud-selfdecl':'자기선언 검증'});
if(I18N.en) Object.assign(I18N.en, {'nav.aud-selfdecl':'Self-Declaration'});
if(I18N.id) Object.assign(I18N.id, {'nav.aud-selfdecl':'Pernyataan Mandiri'});
if(typeof NAV !== 'undefined' && NAV.aud) NAV.aud.push('aud-selfdecl');
NAVC['aud-selfdecl'] = () => (RS.cases||[]).filter(c=>!c.done && (c.pathway==='self_declare' || c.status==='pathway_determination')).length;

/* ── 공통 헬퍼 ── */
async function SDC_req(method, path, body){
  try{
    const opts = {method};
    if(body!==undefined) opts.body = JSON.stringify(body);
    const r = await apiFetch(path, opts);
    let data = null;
    try{ data = await r.json(); }catch(e){}
    return {ok:r.ok, status:r.status, data:data||{}};
  }catch(e){ return {ok:false, status:0, data:{}}; }
}
function SDC_err(msg, res){
  const code = res && res.data && res.data.code ? ` (${res.data.code})` : '';
  if(res && res.status===403) return toast('권한이 없습니다'+code);
  if(res && res.status===409){
    const b = res.data && res.data.blockers ? ` (${res.data.blockers.join(', ')})` : code;
    return toast(msg+' 실패: '+((res.data&&res.data.code)||'충돌')+b);
  }
  toast(msg+' 실패'+code);
}
function SDC_dl(res){
  if(!res || !res.data || typeof res.data!=='object') return '';
  const skip = new Set(['case_id','pathway','next_state','assessment']);
  const rows = Object.keys(res.data).filter(k=>!skip.has(k)).map(k=>{
    const v = res.data[k];
    const sv = (v&&typeof v==='object') ? JSON.stringify(v) : String(v==null?'':v);
    return `<div><dt>${esc(k)}</dt><dd class="mono" style="font-size:12px;white-space:pre-wrap">${esc(sv)}</dd></div>`;
  }).join('');
  return `<dl class="dl">${rows}</dl>`;
}
async function SDC_load(cid){
  try{
    const r = await apiFetch('/cases/'+cid+'/committee/status');
    RS.sdcStatus = RS.sdcStatus||{};
    RS.sdcStatus[cid] = r.ok ? (await r.json()) : {error:true, status:r.status};
  }catch(e){ RS.sdcStatus = RS.sdcStatus||{}; RS.sdcStatus[cid] = {error:true}; }
}

/* ── A. 오디터 자기선언 검증 (마스터-디테일) ── */
VIEWS['aud-selfdecl'] = () => {
  const list = (RS.cases||[]).filter(c=>!c.done);
  if(!list.length) return `<section class="panel"><p class="empty">대상 업체가 없습니다.</p></section>`;
  let cur = (S.selSdc && list.find(c=>c.case_id===S.selSdc)) ? S.selSdc : list[0].case_id;
  S.selSdc = cur;
  const nav = `<nav class="mlist" aria-label="목록"><div class="mh">업체 ${list.length}개사</div>${list.map(c=>
    `<button class="mitem ${c.case_id===cur?'on':''}" onclick="App.sdcSel('${c.case_id}')"><span class="t"><span>${esc(c.company_name)}</span><span class="badge">${esc(_s8label(c))}</span></span><span class="muted mono" style="font-size:11px">${esc(c.status||'')} · ${esc(c.pathway||'')}</span></button>`
  ).join('')}</nav>`;
  const c = list.find(x=>x.case_id===cur) || list[0];
  const a = (S.sdcAssess||{})[cur];

  let pathBlock = `<fieldset><legend>경로 판정</legend>
    <div class="inline"><button class="btn btn-sm" onclick="App.sdcAssess('${cur}')">경로 판정 실행</button>${a?`<button class="btn btn-sm btn-primary" onclick="App.sdcConfirm('${cur}','self_declare')">자기선언으로 확정</button><button class="btn btn-sm" onclick="App.sdcConfirm('${cur}','reguler')">정규로 확정</button>`:''}</div>`;
  if(a){
    pathBlock += SDC_dl(a);
    if(a.data && a.data.assessment){
      pathBlock += `<dl class="dl"><div><dt>위험등급</dt><dd class="mono">${esc(a.data.assessment.risk_category||'')}</dd></div><div><dt>핵심원료</dt><dd class="mono num">${esc(String(a.data.assessment.critical_ingredient_count))}</dd></div><div><dt>증빙완비</dt><dd class="mono">${a.data.assessment.evidence_complete?'예':'아니오'}</dd></div></dl>`;
    }
  }
  pathBlock += `</fieldset>`;

  const pendBlock = `<fieldset><legend>동반자(Pendamping)</legend>
    <div class="inline"><input id="sdc-pend" class="in" placeholder="사용자 id">
      <button class="btn btn-sm" onclick="App.sdcAssign('${cur}')">동반자 배정</button>
      <button class="btn btn-sm btn-primary" onclick="App.sdcVerify('${cur}','verified')">동반자 검증 완료</button>
      <button class="btn btn-sm" onclick="App.sdcVerifyRejectOpen('${cur}')">반려</button>
      <button class="btn btn-sm" onclick="App.sdcVerify('${cur}','rework')">재작업</button>
    </div>
    <div class="muted" style="font-size:11px;margin-top:4px">반려 시 경로가 reguler 로 되돌아갑니다.</div>
  </fieldset>`;

  const docBlock = `<fieldset><legend>자기선언서</legend>
    <div class="inline">
      <button class="btn btn-sm" onclick="App.sdcPreview('${cur}')">미리보기</button>
      <button class="btn btn-sm" onclick="App.sdcPdf('${cur}')">PDF</button>
    </div>
  </fieldset>`;

  const detail = `<section class="panel"><div class="panel-head"><h2>${esc(c.company_name||'')}</h2><span class="badge">${esc(_s8label(c))}</span></div>
    <div class="summary">
      <div><span class="k">경로</span><span class="v mono">${esc(c.pathway||'-')}</span></div>
      <div><span class="k">상태</span><span class="v mono">${esc(c.status||'')}</span></div>
      <div><span class="k">담당</span><span class="v">${esc(_myRole(c))}</span></div>
      <div><span class="k">다음 조치</span><span class="v">${esc(c.next_action||'')}</span></div>
    </div>
    ${_stepper8(c.step8, c.hold, c.done)}
    <div style="display:flex;flex-direction:column;gap:18px;margin-top:14px">${pathBlock}${pendBlock}${docBlock}</div>
  </section>`;

  return `<div class="md">${nav}<div style="display:flex;flex-direction:column;gap:22px;min-width:0">${detail}</div></div>`;
};
App.sdcSel = function(id){ S.selSdc = id; render(); };
App.sdcAssess = async function(cid){
  const res = await SDC_req('POST', `/cases/${cid}/pathway/assess`);
  if(!res.ok) return SDC_err('경로 판정', res);
  S.sdcAssess = S.sdcAssess||{}; S.sdcAssess[cid] = res;
  toast('경로 판정 완료'); render();
};
App.sdcConfirm = async function(cid, pathway){
  const res = await SDC_req('POST', `/cases/${cid}/pathway/confirm`, {pathway, override_reason:null});
  if(!res.ok) return SDC_err('경로 확정', res);
  toast('경로 확정: '+pathway);
  if(typeof loadAud==='function') await loadAud();
  render();
};
App.sdcAssign = async function(cid){
  const uid = (($('#sdc-pend')||{}).value||'').trim();
  if(!uid) return toast('사용자 id 를 입력하세요.');
  const res = await SDC_req('POST', `/cases/${cid}/pendamping/assign`, {pendamping_id:uid});
  if(!res.ok) return SDC_err('동반자 배정', res);
  toast('동반자 배정 완료');
};
App.sdcVerifyRejectOpen = function(cid){
  openModal('동반자 검증 반려',
    `<fieldset><legend>반려 사유</legend>
      <div class="field"><label for="sdc-rej-note" class="req">사유</label>
        <textarea id="sdc-rej-note" class="in" rows="3"></textarea></div>
      <div class="inline-msg">사유를 입력해야 반려할 수 있습니다.</div>
    </fieldset>`,
    `<button class="btn" onclick="App.closeModal()">취소</button><button class="btn btn-primary" onclick="App.sdcVerifyRejectDo('${cid}')">반려</button>`);
};
App.sdcVerifyRejectDo = async function(cid){
  const note = (($('#sdc-rej-note')||{}).value||'').trim();
  if(!note) return toast('반려 사유를 입력하세요.');
  closeModal();
  const res = await SDC_req('POST', `/cases/${cid}/pendamping/verify`, {decision:'rejected', note:note, signature_ref:null});
  if(!res.ok) return SDC_err('동반자 검증', res);
  toast('처리 완료: rejected');
  if(typeof loadAud==='function') await loadAud();
  render();
};
App.sdcVerify = async function(cid, decision){
  const res = await SDC_req('POST', `/cases/${cid}/pendamping/verify`, {decision, note:null, signature_ref:null});
  if(!res.ok) return SDC_err('동반자 검증', res);
  toast(decision==='verified'?'검증 완료':'처리 완료: '+decision);
  if(typeof loadAud==='function') await loadAud();
  render();
};
App.sdcPreview = async function(cid){
  try{
    const r = await apiFetch(`/cases/${cid}/self-declaration/preview`);
    if(!r.ok){
      const j = await r.json().catch(()=>({}));
      if(r.status===403) return toast('권한이 없습니다'+(j.code?` (${j.code})`:''));
      return toast('미리보기를 불러올 수 없습니다'+(j.code?` (${j.code})`:''));
    }
    const txt = await r.text();
    openModal('자기선언서 미리보기', `<pre class="mono" style="white-space:pre-wrap;font-size:12px;margin:0">${esc(txt)}</pre>`,
      '<button class="btn" onclick="App.closeModal()">닫기</button>', true);
  }catch(e){ toast('미리보기를 불러올 수 없습니다.'); }
};
App.sdcPdf = async function(cid){
  const filename = `self-declaration-${cid}.pdf`;
  try{
    const r = await apiFetch(`/cases/${cid}/self-declaration.pdf`);
    if(!r.ok) return toast('PDF 를 불러올 수 없습니다.');
    const blob = await r.blob();
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob); a.download = filename;
    document.body.appendChild(a); a.click();
    setTimeout(()=>{ URL.revokeObjectURL(a.href); a.remove(); }, 4000);
  }catch(e){ toast('PDF 를 불러올 수 없습니다.'); }
};

/* ── B. 샤리아 자기선언 안건(KFPH 위원회) 패널 ── */
App.sdcCommittee = function(cid){
  const st = (RS.sdcStatus||{})[cid];
  if(!st){ if(RS._sdcLoading!==cid){ RS._sdcLoading=cid; SDC_load(cid).then(()=>{ RS._sdcLoading=null; render(); }); }
    return '<section class="panel"><p class="empty">불러오는 중…</p></section>'; }
  if(st.error){
    return `<section class="panel"><p class="empty">${st.status===403?'이 화면을 볼 권한이 없습니다.':'불러오지 못했습니다.'}</p></section>`;
  }
  if(st.pathway !== 'self_declare') return ''; // 자기선언 케이스 아님 — 패널 생략
  const rows = `<dl class="dl">
    <div><dt>상태</dt><dd class="mono">${esc(st.status||'')}</dd></div>
    <div><dt>fatwa_status</dt><dd class="mono">${esc(st.fatwa_status||'-')}</dd></div>
    <div><dt>위원회 심의중</dt><dd class="mono">${st.in_committee?'예':'아니오'}</dd></div>
    <div><dt>결정</dt><dd class="mono">${esc(st.decision||'-')}</dd></div>
    <div><dt>결정자</dt><dd class="mono">${esc(st.decided_by||'-')}</dd></div>
    <div><dt>결정일</dt><dd class="mono">${esc(st.decided_at||'-')}</dd></div>
    <div><dt>사유</dt><dd>${esc(st.reason||'')}</dd></div>
    <div><dt>자기선언서 준비</dt><dd class="mono">${st.self_declaration_ready?'예':'아니오'}</dd></div>
  </dl>`;
  const pend = st.pendamping ? `<dl class="dl">
    <div><dt>동반자</dt><dd>${esc(st.pendamping.name||'')}</dd></div>
    <div><dt>동반자 결정</dt><dd class="mono">${esc(st.pendamping.decision||'-')}</dd></div>
    <div><dt>동반자 메모</dt><dd>${esc(st.pendamping.note||'')}</dd></div>
  </dl>` : '';
  const inCommittee = st.in_committee && st.pathway==='self_declare';
  const form = inCommittee ? `<fieldset><legend>KFPH 결정</legend>
    <div class="field"><label for="sdc-note">사유(반려 시 필수)</label>
      <textarea id="sdc-note" class="in" rows="3"></textarea></div>
    <div class="inline">
      <button class="btn btn-sm btn-primary" onclick="App.sdcDecide('${cid}','approve')">적합 결정</button>
      <button class="btn btn-sm" onclick="App.sdcDecide('${cid}','reject')">부적합 결정</button>
      <button class="btn btn-sm" onclick="App.sdcKetetapan('${cid}')">결정서(Ketetapan) PDF</button>
    </div>
  </fieldset>` : `<div class="inline"><button class="btn btn-sm" onclick="App.sdcKetetapan('${cid}')">결정서(Ketetapan) PDF</button></div>`;
  return `<section class="panel"><div class="panel-head"><h2>자기선언 안건(KFPH 위원회)</h2></div>${rows}${pend}${form}</section>`;
};
App.sdcDecide = async function(cid, decision){
  const reason = (($('#sdc-note')||{}).value||'').trim();
  if(decision==='reject' && !reason) return toast('부적합 결정에는 사유가 필요합니다.');
  const res = await SDC_req('POST', `/cases/${cid}/committee/decide`, {decision, reason:reason||null});
  if(!res.ok) return SDC_err('KFPH 결정', res);
  toast(decision==='approve'?'적합 결정 완료':'부적합 결정 완료');
  delete RS.sdcStatus[cid];
  await SDC_load(cid);
  if(typeof loadSha==='function') await loadSha();
  render();
};
App.sdcKetetapan = async function(cid){
  try{
    const r = await apiFetch(`/cases/${cid}/committee/ketetapan.pdf`);
    if(!r.ok) return toast('결정서를 불러올 수 없습니다.');
    const blob = await r.blob();
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob); a.download = `ketetapan-${cid}.pdf`;
    document.body.appendChild(a); a.click();
    setTimeout(()=>{ URL.revokeObjectURL(a.href); a.remove(); }, 4000);
  }catch(e){ toast('결정서를 불러올 수 없습니다.'); }
};

/* ── sha-review 화면 확장: 패널 삽입 ── */
if(VIEWS['sha-review']){
  const _sdcOrigShaReview = VIEWS['sha-review'];
  VIEWS['sha-review'] = () => {
    const base = _sdcOrigShaReview();
    const cid = S.selFatwa;
    if(!cid) return base;
    let extra;
    try{ extra = App.sdcCommittee(cid); }catch(e){ extra = ''; }
    return base + (extra||'');
  };
}
