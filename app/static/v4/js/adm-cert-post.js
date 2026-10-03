/* ===== 관리자 인증서 발급 후 관리 (파트와 최종승인·정지/재개/서명·SiHalal) ===== */
RS.admcp = RS.admcp || {};

const ADMCP_MU = { cert: [] };

async function ADMCP_load(cid){
  const out = { fatwa: null, products: null, sihalal: null };
  try{
    const [rf, rp, rs] = await Promise.all([
      apiFetch('/cases/'+cid+'/fatwa/status'),
      apiFetch('/cases/'+cid+'/certificate/products'),
      apiFetch('/cases/'+cid+'/sihalal/status'),
    ]);
    out.fatwa = rf.ok ? await rf.json() : {error:true, status:rf.status};
    out.products = rp.ok ? await rp.json() : {error:true, status:rp.status};
    out.sihalal = rs.ok ? await rs.json() : {error:true, status:rs.status};
  }catch(e){
    out.fatwa = out.fatwa || {error:true};
    out.products = out.products || {error:true};
    out.sihalal = out.sihalal || {error:true};
  }
  RS.admcp[cid] = out;
  return out;
}

function ADMCP_clear(cid){
  delete RS.admcp[cid];
  if(RS.admCert) delete RS.admCert[cid];
}

function ADMCP_fatwaPanel(cid, d){
  const hs = {
    none: '없음', review: '심의 중', provisional: '가승인', approved: '최종승인',
  };
  const f = d.fatwa || {};
  const decision = f.decision || 'pending';
  const rows = [
    ['파트와 상태', hs[f.fatwa_status] || f.fatwa_status || '—'],
    ['결정', decision === 'approved' ? '승인' : (decision === 'rejected' ? '거부' : '대기')],
    ['결정번호', f.decision_no || '—'],
    ['결정일', f.decided_at || '—'],
    ['최종승인일', f.final_approved_at || '—'],
  ];
  const dl = `<dl>${rows.map(([k, v]) => `<div><dt>${esc(k)}</dt><dd class="mono">${esc(String(v))}</dd></div>`).join('')}</dl>`;
  let note = '';
  let act = '';
  if(f.fatwa_status === 'provisional'){
    note = `<p class="note attn">샤리아 위원회가 가승인했습니다. 최종승인해야 인증서를 발급할 수 있습니다.</p>`;
    act = `<div class="row-act"><button class="btn btn-primary" onclick="App.admcpFinal('${cid}')">최종 승인</button></div>`;
  } else if(f.fatwa_status === 'approved'){
    note = `<p class="note">최종승인이 완료되었습니다. 인증서를 발급할 수 있습니다.</p>`;
  }
  return `<section class="panel"><div class="panel-head"><h2>파트와 최종승인</h2></div>${dl}${note}${act}</section>`;
}

function ADMCP_certPanel(cid, d){
  const cert = (RS.admCert && RS.admCert[cid]) || null;
  if(!cert || !cert.issued) return '';
  const st = cert.status || 'active';
  const rows = [
    ['인증서 번호', cert.certificate_no || '—'],
    ['상태', st === 'active' ? '활성' : (st === 'suspended' ? '정지' : st)],
  ];
  const dl = `<dl>${rows.map(([k, v]) => `<div><dt>${esc(k)}</dt><dd class="mono">${esc(String(v))}</dd></div>`).join('')}</dl>`;
  let btns = '';
  if(st === 'active'){
    btns += `<button class="btn" onclick="App.admcpReason('${cid}','suspend')">정지</button>`;
  } else if(st === 'suspended'){
    btns += `<button class="btn" onclick="App.admcpReason('${cid}','reactivate')">재개</button>`;
  }
  btns += `<button class="btn" onclick="App.admcpReason('${cid}','sign')">전자서명</button>`;
  btns += `<button class="btn" onclick="App.admcpReason('${cid}','unlock')">잠금 해제</button>`;
  return `<section class="panel"><div class="panel-head"><h2>발급 후 관리</h2></div>${dl}<div class="row-act" style="display:flex;gap:8px;flex-wrap:wrap">${btns}</div></section>`;
}

function ADMCP_productsPanel(cid, d){
  const p = d.products || {};
  if(p.error){
    return `<section class="panel"><div class="panel-head"><h2>제품별 인증서</h2></div><p class="empty">${p.status===403?'이 화면을 볼 권한이 없습니다.':'불러오지 못했습니다.'}</p></section>`;
  }
  if(!p.issued || !(p.items || []).length){
    return `<section class="panel"><div class="panel-head"><h2>제품별 인증서</h2></div><p class="empty">발급된 제품 인증서가 없습니다.</p></section>`;
  }
  const head = `<tr><th>제품</th><th>분류</th><th>인증번호</th><th>공식번호</th><th>유효기간</th><th>상태</th><th></th></tr>`;
  const body = p.items.map(it => {
    const off = it.halal_no_is_official && it.official_bpjph_no ? it.official_bpjph_no : '—';
    const valid = `${esc(it.issue_date||'—')} ~ ${esc(it.expiry_date||'—')}`;
    return `<tr>
      <td>${esc(it.product_name||'')}</td>
      <td>${esc(it.category||'')}</td>
      <td class="mono">${esc(it.certificate_no||'')}</td>
      <td class="mono">${esc(off)}</td>
      <td class="mono" style="font-size:12px">${valid}</td>
      <td>${esc(it.status||'')}</td>
      <td><button class="btn btn-sm" onclick="App.admcpProductPdf('${cid}','${esc(it.product_id)}','${esc(it.certificate_no||'cert')}.pdf')">PDF</button></td>
    </tr>`;
  }).join('');
  return `<section class="panel"><div class="panel-head"><h2>제품별 인증서 <span class="count">${p.count||0}</span></h2></div>
    <div class="tbl-wrap"><table><thead>${head}</thead><tbody>${body}</tbody></table></div></section>`;
}

function ADMCP_sihalalPanel(cid, d){
  const s = d.sihalal || {};
  if(s.error){
    return `<section class="panel"><div class="panel-head"><h2>SiHalal 등록</h2></div><p class="empty">${s.status===403?'이 화면을 볼 권한이 없습니다.':'불러오지 못했습니다.'}</p></section>`;
  }
  const hs = {
    not_configured: '미연동', ready: '제출 가능', submitted: '제출됨', official_received: '공식번호 수령',
  };
  const rows = [
    ['상태', hs[s.status] || s.status || '—'],
    ['연동됨', s.configured ? '예' : '아니오'],
    ['제출됨', s.submitted ? '예' : '아니오'],
    ['제출번호', s.submission_id || '—'],
    ['공식번호', s.official_no || '—'],
    ['공식번호 여부', s.halal_no_is_official ? '공식' : '내부참조'],
  ];
  const dl = `<dl>${rows.map(([k, v]) => `<div><dt>${esc(k)}</dt><dd class="mono">${esc(String(v))}</dd></div>`).join('')}</dl>`;
  const note = !s.configured
    ? `<p class="note attn">SIHALAL 미연동입니다. LP3H 등록·자격증명(GLHAC_SIHALAL_API_URL/TOKEN) 설정 후 제출 가능합니다.</p>`
    : '';
  const subBtn = s.submitted ? '' : `<button class="btn btn-primary" onclick="App.admcpSihalalSubmit('${cid}')">SiHalal 제출</button>`;
  const importRow = `<div class="inline" style="display:flex;gap:8px;align-items:center;margin-top:10px">
    <input id="admcp-bpjph" class="in" placeholder="공식 할랄번호(No. Ketetapan Halal)">
    <button class="btn" onclick="App.admcpImportNo('${cid}')">공식 번호 등록</button>
  </div>`;
  return `<section class="panel"><div class="panel-head"><h2>SiHalal 등록</h2></div>${dl}${note}
    <div class="row-act" style="display:flex;gap:8px;flex-wrap:wrap;margin-top:10px">${subBtn}</div>${importRow}</section>`;
}

function ADMCP_section(){
  const cid = S.selCert;
  if(!cid) return '';
  const d = RS.admcp[cid];
  if(!d){ ADMCP_load(cid).then(() => render()); return ''; }
  return `<div style="display:flex;flex-direction:column;gap:22px;margin-top:22px">
    ${ADMCP_fatwaPanel(cid, d)}
    ${ADMCP_certPanel(cid, d)}
    ${ADMCP_productsPanel(cid, d)}
    ${ADMCP_sihalalPanel(cid, d)}
  </div>`;
}

App.admcpFinal = async function(cid){
  if(!cid) return;
  try{
    const r = await apiFetch('/cases/'+cid+'/fatwa/final-approve', {method:'POST', body:'{}'});
    if(!r.ok){
      let code = '';
      try{ const j = await r.json(); code = (j && j.detail && j.detail.code) || ''; }catch(e){}
      if(r.status === 403) return toast('운영관리자 권한이 필요합니다.');
      if(r.status === 409 && code === 'NO_PROVISIONAL_APPROVAL') return toast('샤리아 가승인이 아직 없습니다.');
      if(r.status === 409 && code === 'NOT_PROVISIONAL') return toast('가승인 상태가 아닙니다.');
      return toast('최종 승인에 실패했습니다.' + (code ? ' ('+code+')' : ''));
    }
    ADMCP_clear(cid);
    if(typeof loadAdm === 'function') { try{ await loadAdm(); }catch(e){} }
    await ADMCP_load(cid);
    render();
    toast('최종 승인되었습니다.');
  }catch(e){ toast('최종 승인에 실패했습니다.'); }
};

App.admcpReason = function(cid, kind){
  if(!cid) return;
  const titles = { suspend: '인증서 정지', reactivate: '인증서 재개', sign: '인증서 전자서명', unlock: '잠금 해제' };
  const needReason = (kind !== 'sign');
  const body = needReason
    ? `<div class="field"><label>사유 <span class="req">*</span></label><textarea id="admcp-reason" class="in" rows="3" placeholder="5자 이상 입력"></textarea></div>`
    : `<div class="field"><label>서명자</label><input id="admcp-reason" class="in" value="${esc((AUTH && AUTH.username) || '')}" readonly></div>`;
  openModal(titles[kind] || '처리', body,
    `<button class="btn" onclick="App.closeModal()">취소</button>
     <button class="btn btn-primary" onclick="App.admcpReasonOk('${cid}','${kind}')">확인</button>`, false);
};

App.admcpReasonOk = async function(cid, kind){
  const val = (($('#admcp-reason')||{}).value||'').trim();
  if(kind !== 'sign' && val.length < 5) return toast('사유를 5자 이상 입력하세요.');
  let path, method = 'POST', payload;
  if(kind === 'suspend'){ path = '/cases/'+cid+'/certificate/suspend'; payload = {reason: val}; }
  else if(kind === 'reactivate'){ path = '/cases/'+cid+'/certificate/reactivate'; payload = {reason: val}; }
  else if(kind === 'sign'){ path = '/cases/'+cid+'/certificate/sign'; payload = {}; }
  else if(kind === 'unlock'){ path = '/cases/'+cid+'/certificate/unlock'; payload = {reason: val}; }
  else return;
  try{
    const r = await apiFetch(path, {method, body: JSON.stringify(payload)});
    if(!r.ok){
      let code = '', have = '';
      try{ const j = await r.json(); code = (j && j.detail && j.detail.code) || ''; have = (j && j.detail && j.detail.have) || ''; }catch(e){}
      if(r.status === 403) return toast('권한이 없습니다.');
      if(code === 'BAD_CERT_STATE') return toast('인증서 상태가 올바르지 않습니다.' + (have ? ' ('+have+')' : ''));
      if(code === 'NO_ACTIVE_CERT') return toast('활성 인증서가 없습니다.');
      if(code === 'REASON_REQUIRED') return toast('사유를 5자 이상 입력하세요.');
      if(code === 'NOT_FROZEN') return toast('잠금 상태가 아닙니다.');
      if(code === 'CERT_NOT_FOUND') return toast('인증서를 찾을 수 없습니다.');
      return toast('처리에 실패했습니다.' + (code ? ' ('+code+')' : ''));
    }
    let j = {};
    try{ j = await r.json(); }catch(e){}
    if(j && j.pending_approval){
      App.closeModal();
      return toast('승인 요청을 올렸습니다. 다른 관리자 계정의 승인이 필요합니다.');
    }
    App.closeModal();
    ADMCP_clear(cid);
    if(typeof loadAdm === 'function') { try{ await loadAdm(); }catch(e){} }
    await ADMCP_load(cid);
    render();
    toast('처리되었습니다.');
  }catch(e){ toast('처리에 실패했습니다.'); }
};

App.admcpProductPdf = async function(cid, productId, filename){
  try{
    const r = await apiFetch('/cases/'+cid+'/certificate/products/'+productId+'.pdf');
    if(!r.ok) return toast(await apiErr(r, '파일을 불러올 수 없습니다.'));
    const blob = await r.blob();
    const a = document.createElement('a'); a.href = URL.createObjectURL(blob); a.download = filename || 'certificate.pdf';
    document.body.appendChild(a); a.click(); setTimeout(()=>{ URL.revokeObjectURL(a.href); a.remove(); }, 4000);
  }catch(e){ toast('파일을 불러올 수 없습니다.'); }
};

App.admcpSihalalSubmit = async function(cid){
  if(!cid) return;
  try{
    const r = await apiFetch('/cases/'+cid+'/sihalal/submit', {method:'POST', body: JSON.stringify({})});
    if(!r.ok){
      let code = '';
      try{ const j = await r.json(); code = (j && j.code) || (j && j.detail && j.detail.code) || ''; }catch(e){}
      if(r.status === 403) return toast('권한이 없습니다.');
      if(r.status === 424 || code === 'SIHALAL_NOT_CONFIGURED') return toast('SIHALAL 미연동 상태입니다. 자격증명 설정 후 제출할 수 있습니다.');
      if(code === 'NOT_SELF_DECLARE') return toast('self-declare 케이스가 아닙니다.');
      if(code === 'NOT_APPROVED') return toast('KFPH 승인이 완료되지 않았습니다.');
      if(code === 'SIHALAL_SUBMIT_FAILED') return toast('SiHalal 제출에 실패했습니다.');
      return toast('SiHalal 제출에 실패했습니다.' + (code ? ' ('+code+')' : ''));
    }
    ADMCP_clear(cid);
    await ADMCP_load(cid);
    render();
    toast('SiHalal에 제출되었습니다.');
  }catch(e){ toast('SiHalal 제출에 실패했습니다.'); }
};

App.admcpImportNo = async function(cid){
  if(!cid) return;
  const v = (($('#admcp-bpjph')||{}).value||'').trim();
  if(v.length < 4) return toast('공식 할랄번호(No. Ketetapan Halal)를 확인하세요.');
  try{
    const r = await apiFetch('/cases/'+cid+'/sihalal/import-number', {method:'POST', body: JSON.stringify({official_no: v, source: 'manual'})});
    if(!r.ok){
      let code = '';
      try{ const j = await r.json(); code = (j && j.detail && j.detail.code) || (j && j.code) || ''; }catch(e){}
      if(r.status === 403) return toast('권한이 없습니다.');
      if(code === 'BAD_OFFICIAL_NO' || r.status === 422) return toast('공식 할랄번호(No. Ketetapan Halal)를 확인하세요.');
      return toast('공식 번호 등록에 실패했습니다.' + (code ? ' ('+code+')' : ''));
    }
    const j = await r.json();
    ADMCP_clear(cid);
    await ADMCP_load(cid);
    render();
    toast(j && j.idempotent ? '이미 등록된 번호입니다.' : '공식 번호가 등록되었습니다.');
  }catch(e){ toast('공식 번호 등록에 실패했습니다.'); }
};

{ const f = VIEWS['adm-cert']; VIEWS['adm-cert'] = () => f() + ADMCP_section(); }
