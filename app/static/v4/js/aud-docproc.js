/* ===== 감사 문서 처리 (재처리·재분류·번역·확인대기) ===== */
const AUDD_DOC_STATUS = { approved: '승인', rejected: '반려', rework: '재요청', pending: '대기' };

async function AUDD_fetchDoc(cid, path, method, body){
  const r = await apiFetch(path, method ? { method, body: body ? JSON.stringify(body) : undefined } : undefined).catch(() => null);
  if(!r) return { ok: false, error: true };
  if(!r.ok){
    let code = null;
    try { const j = await r.json(); code = j && (j.detail && j.detail.code) || j && j.code; } catch(e){}
    return { ok: false, status: r.status, code };
  }
  try { return { ok: true, data: await r.json() }; } catch(e){ return { ok: true, data: null }; }
}

async function AUDD_loadDocs(cid){
  RS.auddDocs = RS.auddDocs || {};
  const lang=S.lang;
  const res = await AUDD_fetchDoc(cid, '/cases/' + cid + '/documents?lang='+encodeURIComponent(lang));
  if(lang!==S.lang) return;
  RS.auddDocs[cid] = res.ok ? { rows: res.data || [] } : { error: true, status: res.status };
}

async function AUDD_loadPend(cid){
  RS.auddPend = RS.auddPend || {};
  const res = await AUDD_fetchDoc(cid, '/cases/' + cid + '/pending-extractions');
  RS.auddPend[cid] = res.ok ? res.data : { error: true, status: res.status };
}

function AUDD_docTable(cid, rows, mode){
  const problems = typeof PDN_panel === 'function' ? PDN_panel(cid) : '';
  if(!rows.length) return problems + '<section class="panel"><p class="empty">제출 서류가 없습니다.</p></section>';
  const body = rows.map(d => {
    const kind = esc(d.doc_type_ko || d.doc_type || '');
    const st = AUDD_DOC_STATUS[d.review_status || 'pending'] || esc(d.review_status || '');
    const open = `<button class="link" onclick="App.docDownload('${d.document_id}','${jsq(d.filename||'')}')">열기</button>`;
    let act;
    if(mode === 'aiq'){
      // 재처리(consultant·operator main.py:5318)·재분류(applicant·consultant·operator 5039) — 오디터는 403 이라 숨김
      act = `<div class="row-act">
        ${canCall('POST','/documents/{}/reprocess')?`<button class="btn btn-sm" onclick="App.auddRepro('${cid}','${d.document_id}')">재처리</button>`:''}
        ${canCall('PATCH','/documents/{}/reclassify')?`<button class="btn btn-sm" onclick="App.auddRecl('${cid}','${d.document_id}')">재분류</button>`:''}
        <button class="btn btn-sm" onclick="App.auddTrans('${d.document_id}')">번역</button></div>`;
    } else if(!canCall('PATCH','/documents/{}/review')){
      act = '';
    } else {
      act = `<div class="row-act">
        <button class="btn btn-sm btn-primary" onclick="App.auddReview('${cid}','${d.document_id}','approved')">승인</button>
        <button class="btn btn-sm" onclick="App.auddReviewReason('${cid}','${d.document_id}','rejected')">반려</button>
        <button class="btn btn-sm" onclick="App.auddReview('${cid}','${d.document_id}','rework')">재요청</button></div>`;
    }
    return `<tr><td>${esc(d.filename||'')}<div class="muted" style="font-size:11px">${open}</div></td>
      <td>${kind}</td><td><span class="badge ${d.review_status==='approved'?'strong':d.review_status==='rejected'?'attn':''}">${st}</span></td>
      <td>${act}</td></tr>`;
  }).join('');
  return problems + `<div class="tbl-wrap"><table><thead><tr><th>파일</th><th>종류</th><th>검토 상태</th><th>동작</th></tr></thead><tbody>${body}</tbody></table></div>`;
}

function AUDD_aiqPanel(cid){
  let docs = RS.auddDocs && RS.auddDocs[cid];
  if(!docs){ if(RS._auddDocLoad !== cid){ RS._auddDocLoad = cid; AUDD_loadDocs(cid).then(()=>{ RS._auddDocLoad=null; render(); }); } return '<section class="panel"><h2>문서 재처리</h2><p class="empty">불러오는 중…</p></section>'; }
  if(docs.error) return `<section class="panel"><h2>문서 재처리</h2><p class="empty">${docs.status===403?'이 화면을 볼 권한이 없습니다.':'불러오지 못했습니다.'}</p></section>`;
  let pend = RS.auddPend && RS.auddPend[cid];
  let pendHtml;
  if(!pend){ if(RS._auddPendLoad !== cid){ RS._auddPendLoad = cid; AUDD_loadPend(cid).then(()=>{ RS._auddPendLoad=null; render(); }); } pendHtml = '<p class="empty">불러오는 중…</p>'; }
  else if(pend.error) pendHtml = `<p class="empty">${pend.status===403?'권한이 없습니다.':'불러오지 못했습니다.'}</p>`;
  else {
    const items = pend.items || [];
    const failed = pend.failed || [];
    const canApply = canCall('POST','/cases/{}/pending-extractions/apply');
    const reviewer = ['auditor','consultant','operator','admin','fatwa_liaison'].includes(AUTH.role);
    const rows = items.map((it,i) => `<li class="row"><label style="display:flex;gap:8px;align-items:center;flex:1;min-width:0">
      ${canApply?`<input type="checkbox" class="audd-pick" value="${i}" aria-label="${t('intake.select')}">`:''}
      <span>${reviewer&&canApply?`<input class="in" id="audd-name-${i}" value="${esc(it.value||'')}" aria-label="${t('intake.field.name')}">`:esc(it.value||'')}
      <span class="tag">${t(it.kind==='material_names'?'intake.materials':'intake.products')}</span><span class="muted">${esc(it.filename||'')}</span></span></label>
      ${reviewer&&canApply?`<label><input type="checkbox" id="audd-dismiss-${i}"> ${t('intake.dismiss')}</label>`:''}</li>`).join('');
    const failHtml=failed.length?`<ul class="note attn">${failed.map(f=>`<li>${esc(f.filename||'')} — ${t('intake.error')}</li>`).join('')}</ul>`:'';
    pendHtml = `${failHtml}${INTAKE_conflicts(pend.conflicts)}
      ${items.length?`<ul class="rows">${rows}</ul>${canApply?`
        ${reviewer?`<label class="field">${t('intake.reason')}<textarea id="audd-apply-reason" class="in" required></textarea></label>`:''}
        <div class="row-act"><button class="btn btn-sm" onclick="App.auddApply('${cid}')">${t('intake.apply')}</button><button class="btn btn-sm" onclick="App.auddApplyAll('${cid}')">${t('intake.apply_all')}</button></div>`:''}`:`<p class="empty">${t('intake.empty')}</p>`}`;

  }
  return `<section class="panel"><div class="panel-head"><h2>문서 재처리</h2><span class="count">${docs.rows.length}건</span></div>
    ${AUDD_docTable(cid, docs.rows, 'aiq')}
    <div class="fieldset" style="margin-top:16px"><legend>AI 추출 확인 대기</legend>${pendHtml}</div></section>`;
}

function AUDD_prePanel(cid){
  let docs = RS.auddDocs && RS.auddDocs[cid];
  if(!docs){ if(RS._auddDocLoad !== cid){ RS._auddDocLoad = cid; AUDD_loadDocs(cid).then(()=>{ RS._auddDocLoad=null; render(); }); } return '<section class="panel"><p class="empty">불러오는 중…</p></section>'; }
  if(docs.error) return `<section class="panel"><p class="empty">${docs.status===403?'이 화면을 볼 권한이 없습니다.':'불러오지 못했습니다.'}</p></section>`;
  return `<section class="panel"><div class="panel-head"><h2>제출 서류 검토</h2><span class="count">${docs.rows.length}건</span></div>${AUDD_docTable(cid, docs.rows, 'pre')}</section>`;
}

const AUDD_f = (fn) => { const x = VIEWS[fn]; if(typeof x === 'function'){ VIEWS[fn] = () => {
  const base = x() || '';
  const cid = fn === 'aud-aiq' ? S.selAiq : S.selPre;
  if(!cid) return base;
  const extra = fn === 'aud-aiq' ? AUDD_aiqPanel(cid) : AUDD_prePanel(cid);
  return base + extra;
}; } };
AUDD_f('aud-aiq'); AUDD_f('aud-pre');

App.auddRepro = async function(cid, did){
  const res = await AUDD_fetchDoc(cid, `/documents/${did}/reprocess`, 'POST', {});
  if(!res.ok){ return toast('재처리에 실패했습니다.' + (res.code ? ' (' + res.code + ')' : '')); }
  delete RS.auddDocs[cid]; delete RS.auddPend[cid];
  await AUDD_loadDocs(cid); render(); toast('재처리 완료');
};

App.auddRecl = function(cid, did){
  openModal('문서 재분류', `<div class="field"><label>서류 종류(doc_type)</label><input class="in" id="audd-dt" placeholder="예: nib" /></div>` ,
    `<button class="btn" onclick="App.closeModal()">취소</button><button class="btn btn-primary" onclick="App.auddReclOk('${cid}','${did}')">확인</button>`);
};
App.auddReclOk = async function(cid, did){
  const v = (($('#audd-dt')||{}).value||'').trim();
  if(!v) return toast('서류 종류를 입력하세요.');
  const res = await AUDD_fetchDoc(cid, `/documents/${did}/reclassify`, 'PATCH', { doc_type: v });
  if(!res.ok){ return toast('재분류에 실패했습니다.' + (res.code ? ' (' + res.code + ')' : '')); }
  App.closeModal(); delete RS.auddDocs[cid]; await AUDD_loadDocs(cid); render(); toast('재분류 완료');
};

App.auddTrans = async function(did){
  const res = await AUDD_fetchDoc(null, `/documents/${did}/translate?lang=id`, 'POST');
  if(!res.ok){ return toast('번역에 실패했습니다.' + (res.code ? ' (' + res.code + ')' : '')); }
  const txt = (res.data && res.data.translated) || '';
  openModal('번역 결과' + (res.data && res.data.cached ? ' (캐시)' : ''),
    `<div class="note" style="white-space:pre-wrap;max-height:400px;overflow:auto">${esc(txt)}</div>`, 
    `<button class="btn" onclick="App.closeModal()">닫기</button>`, true);
};

App.auddReview = async function(cid, did, status){
  const res = await AUDD_fetchDoc(cid, `/documents/${did}/review`, 'PATCH', { review_status: status });
  if(!res.ok){ return toast('검토 저장에 실패했습니다.' + (res.code ? ' (' + res.code + ')' : '')); }
  delete RS.auddDocs[cid]; await AUDD_loadDocs(cid); render(); toast('검토 상태를 저장했습니다.');
};
App.auddReviewReason = function(cid, did, status){
  openModal('반려', `<div class="field"><label>사유</label><textarea class="in" id="audd-reason" rows="3"></textarea></div>`,
    `<button class="btn" onclick="App.closeModal()">취소</button><button class="btn btn-primary" onclick="App.auddReviewReasonOk('${cid}','${did}','${status}')">확인</button>`);
};
App.auddReviewReasonOk = function(cid, did, status){
  App.auddReview(cid, did, status);
};

function AUDD_reason(){
  const reviewer=['auditor','consultant','operator','admin','fatwa_liaison'].includes(AUTH.role);
  const reason=($('#audd-apply-reason')?.value||'').trim();
  if(reviewer&&!reason){ toast(t('intake.reason_required')); return null; }
  return reason;
}
App.auddApply = async function(cid){
  const reason=AUDD_reason(); if(reason===null) return;
  const pend=RS.auddPend?.[cid], items=pend?.items||[];
  const idx=Array.from(document.querySelectorAll('.audd-pick')).filter(x=>x.checked).map(x=>Number(x.value));
  const values=[],renames=Object.create(null),dismiss=[];
  for(const i of idx){
    const it=items[i]; if(!it) continue;
    const name=($('#audd-name-'+i)?.value??it.value).trim();
    if($('#audd-dismiss-'+i)?.checked) dismiss.push(it.value);
    else if(!name) return toast(t('intake.name_required'));
    else if(name!==it.value) renames[it.value]=name;
    else values.push(it.value);
  }
  if(!idx.length) return toast(t('intake.select_required'));
  const res=await AUDD_fetchDoc(cid,`/cases/${cid}/pending-extractions/apply`,'POST',{values,renames,dismiss,reason});
  if(!res.ok) return toast(t('intake.error')+(res.code?' ('+res.code+')':''));
  delete RS.auddPend[cid]; await AUDD_loadPend(cid); render(); toast(t('intake.saved'));
};
App.auddApplyAll = async function(cid){
  const reason=AUDD_reason(); if(reason===null) return;
  const res=await AUDD_fetchDoc(cid,`/cases/${cid}/pending-extractions/apply`,'POST',{reason});
  if(!res.ok) return toast(t('intake.error')+(res.code?' ('+res.code+')':''));
  delete RS.auddPend[cid]; await AUDD_loadPend(cid); render(); toast(t('intake.saved'));
};
