/* 인증 패키지 점검 — 업체·제품·성분(원재료)·공장은 한 묶음이다. 하나라도 빠지면 심사가 성립하지 않는다.
   서류에서 찾지 못한 항목·판독 불가 서류(손글씨 등)를 보여 주고, 그 자리에서 수기 입력하거나
   (업체·컨설턴트) 보완 요청·수기 확인(오디터·운영자)을 한다. 서버: GET /cases/{id}/package-check */
const PKG_LABEL = {company:'업체 정보', product:'제품', material:'성분(원재료)', factory:'공장'};
const PKG_CO_LABEL = {company_name:'업체명', nib:'사업자등록번호', responsible_person:'대표자', address:'주소'};
function PKG_canEdit(){ return ['applicant','consultant','admin'].includes(AUTH.role); }
function PKG_canAudit(){ return ['auditor','operator','admin'].includes(AUTH.role); }
async function PKG_load(cid){
  RS.pkg = RS.pkg || {};
  try{ const r = await apiFetch('/cases/'+cid+'/package-check'); RS.pkg[cid] = r.ok ? await r.json() : {error:r.status}; }
  catch(e){ RS.pkg[cid] = {error:true}; }
}
function PKG_panel(cid){
  if(!cid) return '';
  RS.pkg = RS.pkg || {};
  const d = RS.pkg[cid];
  if(!d){
    if(RS._pkgLoading !== cid){ RS._pkgLoading = cid; PKG_load(cid).then(()=>{ RS._pkgLoading = null; render(); }); }
    return '';
  }
  if(d.error) return '';
  const it = d.items || {};
  const row = (k, ok, detail, btn) => `<tr><td><b>${PKG_LABEL[k]}</b></td><td>${ok?'<span class="badge strong">✓ 확인</span>':'<span class="badge attn">✕ 누락</span>'}</td><td class="muted">${detail||''}</td><td>${ok?'':(btn||'')}</td></tr>`;
  const ed = PKG_canEdit();
  const coMiss = ((it.company||{}).missing||[]).map(k=>PKG_CO_LABEL[k]||k).join(', ');
  const unl = ((it.material||{}).unlinked_products||[]);
  const rows = [
    row('company', (it.company||{}).ok, coMiss ? '없음: '+esc(coMiss) : '', ed?`<button class="btn btn-sm" onclick="App.pkgEdit('${cid}','company')">수기 입력</button>`:''),
    row('product', (it.product||{}).ok, '등록 '+((it.product||{}).count||0)+'개', ed?`<button class="btn btn-sm" onclick="App.pkgEdit('${cid}','product')">제품 추가</button>`:''),
    row('material', (it.material||{}).ok, '등록 '+((it.material||{}).count||0)+'개'+(unl.length?` · 원재료 미연결 제품: ${esc(unl.slice(0,3).join(', '))}${unl.length>3?' 외 '+(unl.length-3):''}`:''), ed?`<button class="btn btn-sm" onclick="App.pkgEdit('${cid}','material')">원재료 추가</button>`:''),
    row('factory', (it.factory||{}).ok, '', ed?`<button class="btn btn-sm" onclick="App.pkgEdit('${cid}','factory')">공장 입력</button>`:''),
  ].join('');
  const un = d.unreadable || [];
  const unRows = un.map(u=>`<tr><td>${esc(u.filename||'-')}</td><td class="mono">${u.confidence==null?'-':Number(u.confidence).toFixed(2)}</td>
      <td><div class="row-act"><button class="btn btn-sm btn-ghost" onclick="App.docDownload('${esc(u.document_id)}','${esc(u.filename||'')}')">원본</button>
      ${PKG_canAudit()?`<button class="btn btn-sm" onclick="App.pkgDocOk('${cid}','${esc(u.document_id)}')">수기 확인 완료</button>`:''}</div></td></tr>`).join('');
  const up = d.unprocessed || [];
  const canReproc = ['consultant','operator','auditor','admin'].includes(AUTH.role);
  const head = d.complete
    ? `<span class="badge strong">패키지 완비</span>`
    : `<span class="badge attn">확인 필요 ${(d.gaps||[]).length + (un.length?1:0) + (up.length?1:0)}건</span>`;
  return `<section class="panel"><div class="panel-head"><h2>인증 패키지 점검 · 업체·제품·성분·공장</h2>
      <div class="row-act">${head}${PKG_canAudit() && !d.complete ? `<button class="btn btn-sm" onclick="App.pkgRequest('${cid}')">업체·컨설턴트에 보완 요청</button>`:''}
      <button class="btn btn-sm btn-ghost" onclick="App.pkgRefresh('${cid}')">다시 점검</button></div></div>
    <div class="tbl-wrap"><table><thead><tr><th>항목</th><th>상태</th><th>내용</th><th></th></tr></thead><tbody>${rows}</tbody></table></div>
    ${up.length?`<div class="note"><b>미판독 서류 ${up.length}건 — 판독 전</b><p>판독 기능이 돌지 않은 서류입니다(예전 업로드 등). 재처리하면 내용을 읽어 빠진 항목을 자동으로 채웁니다. 끝나면 다시 점검해 통보합니다.</p>
      ${canReproc?`<div class="row-act"><button class="btn btn-sm btn-primary" onclick="App.pkgReproc('${cid}')">미판독 서류 일괄 재처리</button></div>`:''}</div>`:''}
    ${un.length?`<div class="note attn"><b>판독 실패 서류 ${un.length}건</b><p>손글씨·저화질 등으로 내용을 읽지 못한 서류입니다. 원본을 보고 필요한 값을 수기로 입력한 뒤 '수기 확인 완료'로 처리하세요.</p></div>
      <div class="tbl-wrap"><table><thead><tr><th>서류</th><th>신뢰도</th><th></th></tr></thead><tbody>${unRows}</tbody></table></div>`:''}
  </section>`;
}
App.pkgRefresh = async function(cid){ await PKG_load(cid); render(); };
App.pkgReproc = async function(cid){
  const r = await apiFetch('/cases/'+cid+'/documents/reprocess-pending', {method:'POST', body:'{}'});
  if(!r.ok) return toast(await apiErr(r, '재처리를 시작하지 못했습니다.'));
  const d = await r.json();
  toast(d.queued ? `미판독 서류 ${d.queued}건을 재처리 큐에 넣었습니다. 끝나면 자동으로 다시 점검합니다(1건당 수 초~수십 초).` : '재처리할 서류가 없습니다(원본 미보관 또는 자동 파싱 꺼짐).');
};
App.pkgEdit = function(cid, k){
  const f = (id, label, ph) => `<div class="field"><label for="${id}">${label}</label><input class="in" id="${id}" placeholder="${ph||''}"></div>`;
  const forms = {
    company: f('pk-co','업체명')+f('pk-nib','사업자등록번호')+f('pk-ceo','대표자')+f('pk-addr','주소'),
    product: f('pk-pn','제품명')+f('pk-pd','설명(선택)'),
    material: f('pk-mn','원재료명')+f('pk-ms','공급사(선택)')+f('pk-mo','원산지(선택)'),
    factory: f('pk-fa','공장 주소')+f('pk-fr','공장등록번호(선택)'),
  };
  openModal(PKG_LABEL[k]+' 수기 입력', forms[k] + `<div class="inline-msg" id="pk-e"></div><p class="muted">서류에서 찾지 못한 값을 직접 입력합니다. 입력 기록은 감사 로그에 남습니다.</p>`,
    `<button class="btn" onclick="App.closeModal()">취소</button><button class="btn btn-primary" onclick="App.pkgSave('${cid}','${k}')">저장</button>`);
};
App.pkgSave = async function(cid, k){
  const v = id => ((($('#'+id)||{}).value)||'').trim();
  const err = m => { const e=$('#pk-e'); if(e) e.textContent=m; };
  let r;
  if(k==='company'){
    const b = {}; [['company_name','pk-co'],['nib','pk-nib'],['responsible_person','pk-ceo'],['address','pk-addr']].forEach(([key,id])=>{ if(v(id)) b[key]=v(id); });
    if(!Object.keys(b).length) return err('입력한 값이 없습니다.');
    r = await apiFetch('/cases/'+cid+'/profile', {method:'PATCH', body:JSON.stringify(b)});
  } else if(k==='product'){
    if(!v('pk-pn')) return err('제품명을 입력해 주세요.');
    r = await apiFetch('/cases/'+cid+'/products', {method:'POST', body:JSON.stringify({name:v('pk-pn'), description:v('pk-pd')||null})});
  } else if(k==='material'){
    if(!v('pk-mn')) return err('원재료명을 입력해 주세요.');
    r = await apiFetch('/cases/'+cid+'/materials', {method:'POST', body:JSON.stringify({name:v('pk-mn'), supplier:v('pk-ms')||null, origin:v('pk-mo')||null})});
  } else {
    if(!v('pk-fa')) return err('공장 주소를 입력해 주세요.');
    const b = {factory_address:v('pk-fa')}; if(v('pk-fr')) b.factory_reg_no = v('pk-fr');
    r = await apiFetch('/cases/'+cid+'/profile', {method:'PATCH', body:JSON.stringify(b)});
  }
  if(!r.ok) return err(await apiErr(r, '저장하지 못했습니다.'));
  closeModal(); await PKG_load(cid); render(); toast(PKG_LABEL[k]+'을(를) 입력했습니다.');
};
App.pkgDocOk = async function(cid, docId){
  const r = await apiFetch('/documents/'+docId+'/review', {method:'PATCH', body:JSON.stringify({review_status:'approved'})});
  if(!r.ok) return toast(await apiErr(r, '처리하지 못했습니다.'));
  await PKG_load(cid); render(); toast('수기 확인 완료로 처리했습니다.');
};
App.pkgRequest = function(cid){
  openModal('보완 요청', `<p>빠진 항목과 판독 불가 서류를 업체·담당 컨설턴트에게 알립니다.</p>
    <div class="field"><label for="pk-note">요청 내용(선택)</label><textarea class="in" id="pk-note" placeholder="예: 생산결과기록이 손글씨라 판독되지 않습니다. 원재료명을 직접 입력해 주세요."></textarea></div>`,
    `<button class="btn" onclick="App.closeModal()">취소</button><button class="btn btn-primary" onclick="App.pkgRequestDo('${cid}')">보내기</button>`);
};
App.pkgRequestDo = async function(cid){
  const note = ((($('#pk-note')||{}).value)||'').trim();
  const r = await apiFetch('/cases/'+cid+'/package-check/request', {method:'POST', body:JSON.stringify({note})});
  if(!r.ok) return toast(await apiErr(r, '요청을 보내지 못했습니다.'));
  closeModal(); toast('업체·컨설턴트에 보완 요청을 보냈습니다.');
};
// 화면 연결 — 업체: 홈·신청, 컨설턴트: 업체별 현황, 오디터: 사전심사·업체 상세
{ const wrap = (v, pick) => { const f = VIEWS[v]; if(typeof f==='function') VIEWS[v] = () => PKG_panel(pick()) + f(); };
  const ent = () => ((RS.cases||[])[0]||{}).case_id;
  wrap('ent-home', ent); wrap('ent-apply', ent);
  wrap('cons-status', () => S.selCompany);
  wrap('aud-pre', () => S.selPre);
  wrap('aud-detail', () => S.audCo); }
