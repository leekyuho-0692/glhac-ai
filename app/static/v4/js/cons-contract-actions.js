/* ===== 컨설턴트·관리자 계약 처리 액션 ===== */
RS.cca = RS.cca || {};
RS.ccaFail = RS.ccaFail || {};

// ── 로더 ──
async function CCA_load(cid){
  try{
    const r = await apiFetch('/cases/' + cid + '/contract');
    if(!r.ok) RS.cca[cid] = {error:true, status:r.status};
    else RS.cca[cid] = await r.json();
  }catch(e){ RS.cca[cid] = {error:true}; }
}

// ── 계약 상태 라벨 ──
function CCA_statusLabel(st){
  const M = {none:'미생성', draft:'작성 중', issued:'발급됨', requested:'승인 요청',
             sent:'발송됨', received:'접수·확인', signing:'서명 진행', signed:'양자 서명 완료',
             confirmed:'최종 확인됨'};
  return M[st] || st || '—';
}

// ── 서버 오류 → 안내문 ──
function CCA_failTxt(r, d, okRoles){
  if(r.status === 403) return '이 작업은 권한이 없습니다' + (okRoles ? '(' + okRoles + ')' : '') + '.';
  const code = d && d.detail && d.detail.code ? ' (' + d.detail.code + ')' : '';
  return '작업에 실패했습니다.' + code;
}

// ── 공통 처리: POST → 실패 toast / 성공 시 캐시 무효화 ──
async function CCA_post(cid, path, body, failMsg, okMsg, reloadFn){
  let r, d = null;
  try{
    r = await apiFetch(path, {method:'POST', body: JSON.stringify(body || {})});
    try{ d = await r.json(); }catch(e){ d = null; }
  }catch(e){
    toast(failMsg + '에 실패했습니다.'); return false;
  }
  if(!r.ok){ toast(failMsg + '에 실패했습니다.' + CCA_failTxt(r, d, '')); return false; }
  delete RS.cca[cid];
  if(RS.entCt) delete RS.entCt[cid];
  try{ if(reloadFn) await reloadFn(); }catch(e){}
  await CCA_load(cid);
  render();
  if(okMsg) toast(okMsg);
  return true;
}

// ── 현재 상태에서 노출할 버튼 판단 ──
function CCA_actions(d, isAdmin){
  const st = d.exists ? d.status : 'none';
  const signed = !!d.signed_a && !!d.signed_b;
  const out = [];
  // 계약서 생성 (없거나 초안일 때)
  if(!d.exists || st === 'none' || st === 'draft')
    out.push(['generate','계약서 생성','App.ccaGenerate']);
  // 서명 요청 (오디터 권한 — 관리자 화면에선 노출 안 함)
  if(!isAdmin && st === 'received')
    out.push(['reqsig','서명 요청 발송','App.ccaRequestSignature']);
  // 계약 확정 (운영자·양자 서명 완료)
  if(isAdmin && signed && st !== 'confirmed')
    out.push(['confirm','계약 확정','App.ccaConfirm']);
  // 반려 (되돌릴 수 있는 단계)
  if(['requested','sent','received','signing','signed','confirmed'].indexOf(st) >= 0)
    out.push(['return','반려','App.ccaReturnOpen']);
  // PDF
  if(d.exists)
    out.push(['pdf','계약서 PDF','App.ccaPdf']);
  return out;
}

// ── 패널 HTML (컨설턴트/관리자 공용) ──
function CCA_panel(cid, isAdmin){
  if(!cid){
    return `<section class="panel"><div class="panel-head"><h2>계약 처리</h2></div><p class="empty">대상 업체를 선택하세요.</p></section>`;
  }
  const d = RS.cca[cid];
  const picker = isAdmin ? `<div class="inline" style="margin-top:10px">
    <select class="in" onchange="App.ccaPick(this.value)">
      <option value="">— 대상 업체 선택 —</option>
      ${(RS.cases||[]).map(c=>`<option value="${esc(c.case_id)}" ${c.case_id===cid?'selected':''}>${esc(c.company_name)} · ${esc(c.status||'')}</option>`).join('')}
    </select>
  </div>` : '';

  if(!d){
    if(RS._ccaLoading !== cid){ RS._ccaLoading = cid; CCA_load(cid).then(()=>{ RS._ccaLoading=null; render(); }); }
    return `<section class="panel"><div class="panel-head"><h2>계약 처리</h2></div>${picker}<p class="empty">불러오는 중…</p></section>`;
  }
  if(d.error){
    return `<section class="panel"><div class="panel-head"><h2>계약 처리</h2></div>${picker}
      <p class="empty">${d.status===403 ? '이 화면을 볼 권한이 없습니다.' : '불러오지 못했습니다.'}</p></section>`;
  }
  const st = d.exists ? d.status : 'none';
  const rows = [
    `<div><dt>상태</dt><dd>${d.exists ? `<span class="badge ${st==='confirmed'||st==='signed'?'strong':'attn'}">${esc(CCA_statusLabel(st))}</span>` : '<span class="badge">미생성</span>'}</dd></div>`,
    d.contract_no ? `<div><dt>계약번호</dt><dd class="mono">${esc(d.contract_no)}</dd></div>` : '',
    d.exists && d.fee != null ? `<div><dt>수수료</dt><dd class="mono num">${esc(_money(d.fee, d.currency||''))}</dd></div>` : '',
    d.effective_date ? `<div><dt>발효일</dt><dd class="mono">${esc(d.effective_date)}</dd></div>` : '',
    d.exists ? `<div><dt>서명</dt><dd>
      A ${d.signed_a ? '<span class="badge strong">완료</span>' : '<span class="badge attn">대기</span>'}
      B ${d.signed_b ? '<span class="badge strong">완료</span>' : '<span class="badge attn">대기</span>'}
    </dd></div>` : ''
  ].filter(x=>x).join('');

  const acts = CCA_actions(d, isAdmin);
  const btns = acts.map(([k,l,fn])=>`<button class="btn ${k==='pdf'?'btn-ghost':'btn-primary'}" onclick="${fn}('${esc(cid)}')">${esc(l)}</button>`).join('');

  return `<section class="panel"><div class="panel-head"><h2>계약 처리</h2></div>${picker}
    <dl class="dl">${rows}</dl>
    <div class="row-act" style="margin-top:14px;flex-wrap:wrap;gap:8px">${btns || '<span class="muted">가능한 작업이 없습니다.</span>'}</div>
  </section>`;
}

// ── 대상 선택 ──
App.ccaPick = function(cid){
  if(!cid) return;
  S.ccaAdmCase = cid;
  if(!RS.cca[cid]) CCA_load(cid).then(()=>render());
  render();
};

// ── 쓰기 동작 ──
App.ccaGenerate = async function(cid){
  await CCA_post(cid, '/cases/'+cid+'/contract/generate', {}, '계약서 생성', '계약서를 생성했습니다.', loadCons);
};
App.ccaRequestSignature = async function(cid){
  await CCA_post(cid, '/cases/'+cid+'/contract/request-signature', {}, '서명 요청', '서명 요청을 발송했습니다.', loadCons);
};
App.ccaConfirm = async function(cid){
  await CCA_post(cid, '/cases/'+cid+'/contract/confirm', {}, '계약 확정', '계약을 확정했습니다.', S.role==='adm'?loadAdm:loadCons);
};
App.ccaReturnOpen = function(cid){
  openModal('계약 반려',
    `<div class="field"><label>반려 사유</label><textarea id="cca-reason" class="in" rows="3" placeholder="이전 단계로 되돌리는 사유를 적어 주세요."></textarea></div>
     <p class="inline-msg">사유는 상대 담당자에게 그대로 전달됩니다.</p>`,
    `<button class="btn" onclick="App.closeModal()">취소</button>
     <button class="btn btn-primary" onclick="App.ccaReturn('${esc(cid)}')">반려</button>`);
};
App.ccaReturn = async function(cid){
  const reason = (($('#cca-reason')||{}).value||'').trim();
  App.closeModal();
  const ok = await CCA_post(cid, '/cases/'+cid+'/contract/return', {reason:reason}, '반려', '이전 단계로 반려했습니다.', S.role==='adm'?loadAdm:loadCons);
  if(!ok) return;
};

// ── 관리자 전용: 승인·수령 ──
App.ccaApprove = async function(cid){
  await CCA_post(cid, '/cases/'+cid+'/contract/approve', {}, '계약 승인', '계약을 승인·발송했습니다.', loadAdm);
};
App.ccaReceive = async function(cid){
  await CCA_post(cid, '/cases/'+cid+'/contract/receive', {}, '계약서 수령 확인', '접수·확인 처리했습니다.', loadAdm);
};

// ── PDF ──
App.ccaPdf = async function(cid){
  try{
    const r = await apiFetch('/cases/'+cid+'/contract/pdf');
    if(!r.ok) return toast('파일을 불러올 수 없습니다.');
    const blob = await r.blob();
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = 'contract_' + cid.slice(0,8) + '.pdf';
    document.body.appendChild(a); a.click();
    setTimeout(()=>{ URL.revokeObjectURL(a.href); a.remove(); }, 4000);
  }catch(e){ toast('파일을 불러올 수 없습니다.'); }
};

// ── 관리자 버튼 추가 주입(승인·수령) ──
function CCA_admExtra(cid){
  const d = RS.cca[cid];
  if(!d || !d.exists) return '';
  const st = d.status;
  const btns = [];
  if(st === 'requested' || st === 'draft')
    btns.push(`<button class="btn btn-primary" onclick="App.ccaApprove('${esc(cid)}')">계약 승인</button>`);
  if(st === 'sent' || st === 'issued')
    btns.push(`<button class="btn btn-primary" onclick="App.ccaReceive('${esc(cid)}')">계약서 수령 확인</button>`);
  return btns.length ? `<div class="row-act" style="margin-top:10px;gap:8px">${btns.join('')}</div>` : '';
}

// ── 화면 확장: cons-contract ──
(function(){
  const base = VIEWS['cons-contract'];
  if(typeof base === 'function'){
    VIEWS['cons-contract'] = () => base() + CCA_panel(S.selContract || '', false);
  }
})();

// ── 화면 확장: adm-contract ──
(function(){
  const base = VIEWS['adm-contract'];
  if(typeof base === 'function'){
    VIEWS['adm-contract'] = () => {
      const cid = S.ccaAdmCase || '';
      return base() + CCA_panel(cid, true) + CCA_admExtra(cid);
    };
  }
})();

// ── 단계 전환 패널 (cons-status) ──
App.ccaTransition = async function(){
  const cid = S.selCompany || '';
  if(!cid) return toast('대상 업체를 선택하세요.');
  const to = (($('#cca-to')||{}).value||'').trim();
  if(!to) return toast('전환할 목표 상태를 입력하세요.');
  S.ccaBlock = null;
  let r, d = null;
  try{
    r = await apiFetch('/cases/'+cid+'/transition', {method:'POST', body: JSON.stringify({to_state: to, action:'transition', actor_type:'system'})});
    try{ d = await r.json(); }catch(e){ d = null; }
  }catch(e){ toast('단계 전환에 실패했습니다.'); return; }
  if(!r.ok){
    if(r.status === 403){
      toast('이 작업은 권한이 없습니다.');
      S.ccaBlock = {kind:'403', msg:'이 전환은 현재 역할로 수행할 권한이 없습니다.'};
    }else if(r.status === 409 && d && d.detail){
      const bl = (d.detail.blockers || d.blockers || []);
      S.ccaBlock = {kind:'409', code: d.detail.code || 'TRANSITION_BLOCKED',
                    msg: '전환할 수 없습니다.', blockers: bl};
      toast('단계 전환에 실패했습니다. (' + (d.detail.code||'BLOCKED') + ')');
    }else{
      const code = d && d.detail && d.detail.code ? ' (' + d.detail.code + ')' : '';
      S.ccaBlock = {kind:'err', msg: '단계 전환에 실패했습니다.' + code};
      toast('단계 전환에 실패했습니다.' + code);
    }
    render();
    return;
  }
  S.ccaBlock = null;
  try{ await loadCons(); }catch(e){}
  render();
  toast('다음 단계로 전환했습니다: ' + to);
};

(function(){
  const base = VIEWS['cons-status'];
  if(typeof base === 'function'){
    VIEWS['cons-status'] = () => {
      const cid = S.selCompany || '';
      const c = (RS.cases||[]).find(x=>x.case_id===cid);
      let blk = '';
      if(S.ccaBlock){
        let extra = '';
        if(S.ccaBlock.blockers && S.ccaBlock.blockers.length){
          extra = '<ul>' + S.ccaBlock.blockers.map(b=>`<li>${esc(typeof b==='string'?b:JSON.stringify(b))}</li>`).join('') + '</ul>';
        }
        blk = `<div class="note attn"><strong>${esc(S.ccaBlock.msg||'')}</strong> ${S.ccaBlock.code?`<span class="mono">(${esc(S.ccaBlock.code)})</span>`:''}${extra}</div>`;
      }
      const cur = c ? `<span class="badge">${esc(c.status||'')}</span>` : '<span class="muted">대상 없음</span>';
      return base() + `<section class="panel"><div class="panel-head"><h2>단계 전환</h2>${cur}</div>
        ${blk}
        <div class="inline" style="margin-top:10px">
          <input id="cca-to" class="in" placeholder="예: consultant_review" />
          <button class="btn btn-primary" onclick="App.ccaTransition()">다음 단계로 전환</button>
        </div>
        <p class="inline-msg">승인·발급 상태는 전용 화면에서만 처리할 수 있습니다.</p>
      </section>`;
    };
  }
})();
