/* 인증 패키지 점검 — 업체·제품·성분(원재료)·공장은 한 묶음이다. 하나라도 빠지면 심사가 성립하지 않는다.
   서류에서 찾지 못한 항목·판독 불가 서류(손글씨 등)를 보여 주고, 그 자리에서 수기 입력하거나
   (업체·컨설턴트) 보완 요청·수기 확인(오디터·운영자)을 한다. 서버: GET /cases/{id}/package-check */
const PKG_LABEL = {company:'업체 정보', product:'제품', material:'성분(원재료)', factory:'공장', scope:'취급 서비스', vehicle:'차량', cleaning:'세척 SOP'};
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
  const row = (k, ok, detail, btn) => `<tr><td><b>${PKG_LABEL[k]}</b></td><td>${ok?'<span class="badge strong">✓ 확인</span>':'<span class="badge attn">✕ 누락</span>'}</td><td class="muted">${detail||''}</td><td>${ok&&!['material','product'].includes(k)?'':(btn||'')}</td></tr>`;
  const ed = PKG_canEdit();
  const coMiss = ((it.company||{}).missing||[]).map(k=>PKG_CO_LABEL[k]||k).join(', ');
  const unl = ((it.material||{}).unlinked_products||[]);
  const JASA_KO = {penyimpanan:'보관', pengemasan:'포장', pendistribusian:'유통'};
  const rows = d.scheme==='logistics' ? [
    row('company', (it.company||{}).ok, coMiss ? '없음: '+esc(coMiss) : '', ed?`<button class="btn btn-sm" onclick="App.pkgEdit('${cid}','company')">수기 입력</button>`:''),
    row('scope', (it.scope||{}).ok, (it.scope||{}).ok ? ((it.scope||{}).scope||[]).map(s=>JASA_KO[s]||s).join(', ') : '취급 서비스 미선택', ed?`<button class="btn btn-sm" onclick="App.go('ent-apply')">신청서에서 선택</button>`:''),
    row('vehicle', (it.vehicle||{}).required ? (it.vehicle||{}).ok : true, (it.vehicle||{}).required ? '등록 '+((it.vehicle||{}).count||0)+'대' : '—', (it.vehicle||{}).required && !((it.vehicle||{}).ok) ? (ed?`<button class="btn btn-sm" onclick="App.entlAdd ? App.entlAdd('${cid}') : App.go('ent-apply')">차량 추가</button>`:'') : ''),
    row('facility', (it.facility||{}).required ? (it.facility||{}).ok : true, (it.facility||{}).required ? '' : '—', (it.facility||{}).required && !((it.facility||{}).ok) ? (ed?`<button class="btn btn-sm" onclick="App.pkgEdit('${cid}','factory')">시설</button>`:'') : ''),
    row('cleaning', (it.cleaning||{}).ok, '세척 SOP 서류', (it.cleaning||{}).ok ? '' : (ed?`<button class="btn btn-sm" onclick="App.go('ent-docs')">문서함에서 업로드</button>`:'')),
  ].join('') : [
    row('company', (it.company||{}).ok, coMiss ? '없음: '+esc(coMiss) : '', ed?`<button class="btn btn-sm" onclick="App.pkgEdit('${cid}','company')">수기 입력</button>`:''),
    row('product', (it.product||{}).ok, '등록 '+((it.product||{}).count||0)+'개', ed&&canCall('POST','/cases/{}/products')?`<button class="btn btn-sm" onclick="App.pkgEdit('${cid}','product')">제품 추가</button>`:''),
    row('material', (it.material||{}).ok, '등록 '+((it.material||{}).count||0)+'개'+(unl.length?` · 원재료 미연결 제품: ${esc(unl.slice(0,3).join(', '))}${unl.length>3?' 외 '+(unl.length-3):''}`:''), ed&&canCall('POST','/cases/{}/materials')?`<button class="btn btn-sm" onclick="App.pkgEdit('${cid}','material')">원재료 등록</button>`:''),
    row('factory', (it.factory||{}).ok, '', ed?`<button class="btn btn-sm" onclick="App.pkgEdit('${cid}','factory')">공장 입력</button>`:''),
  ].join('');
  const un = d.unreadable || [];
  const unRows = un.map(u=>`<tr><td>${esc(u.filename||'-')}</td><td class="mono">${u.confidence==null?'-':Number(u.confidence).toFixed(2)}</td>
      <td><div class="row-act"><button class="btn btn-sm btn-ghost" onclick="App.docDownload('${jsq(u.document_id)}','${jsq(u.filename||'')}')">원본</button>
      ${PKG_canAudit()?`<button class="btn btn-sm" onclick="App.pkgDocOk('${cid}','${jsq(u.document_id)}')">수기 확인 완료</button>`:''}</div></td></tr>`).join('');
  const up = d.unprocessed || [];
  const canReproc = ['consultant','operator','auditor','admin'].includes(AUTH.role);
  const head = d.complete
    ? `<span class="badge strong">패키지 완비</span>`
    : `<span class="badge attn">확인 필요 ${(d.gaps||[]).length + (un.length?1:0) + (up.length?1:0)}건</span>`;
  return `<section class="panel"><div class="panel-head"><h2>${d.scheme==='logistics' ? '인증 패키지 점검 · 업체·취급 서비스·차량·시설·세척 SOP' : '인증 패키지 점검 · 업체·제품·성분·공장'}</h2>
      <div class="row-act">${head}${PKG_canAudit() && !d.complete ? `<button class="btn btn-sm" onclick="App.pkgRequest('${cid}')">업체·컨설턴트에 보완 요청</button>`:''}
      <button class="btn btn-sm btn-ghost" onclick="App.pkgRefresh('${cid}')" ${RS._pkgRefreshing?.[cid]?'disabled aria-busy="true"':''}>${t(RS._pkgRefreshing?.[cid]?'pkg.checking':'pkg.refresh')}</button></div></div>
    <div class="tbl-wrap"><table><thead><tr><th>항목</th><th>상태</th><th>내용</th><th></th></tr></thead><tbody>${rows}</tbody></table></div>
    ${up.length?`<div class="note"><b>미판독 서류 ${up.length}건 — 판독 전</b><p>판독 기능이 돌지 않은 서류입니다(예전 업로드 등). 재처리하면 내용을 읽어 빠진 항목을 자동으로 채웁니다. 끝나면 다시 점검해 통보합니다.</p>
      ${canReproc?`<div class="row-act"><button class="btn btn-sm btn-primary" onclick="App.pkgReproc('${cid}')">미판독 서류 일괄 재처리</button></div>`:''}</div>`:''}
    ${un.length?`<div class="note attn"><b>판독 실패 서류 ${un.length}건</b><p>손글씨·저화질 등으로 내용을 읽지 못한 서류입니다. 원본을 보고 필요한 값을 수기로 입력한 뒤 '수기 확인 완료'로 처리하세요.</p></div>
      <div class="tbl-wrap"><table><thead><tr><th>서류</th><th>신뢰도</th><th></th></tr></thead><tbody>${unRows}</tbody></table></div>`:''}
  </section>`;
}
function PKG_resultKey(d){
  // JSON의 키·목록 순서 차이는 점검 결과의 변경이 아니다.
  const stable=v=>Array.isArray(v)?v.map(stable).sort((a,b)=>JSON.stringify(a).localeCompare(JSON.stringify(b))):
    v&&typeof v==='object'?Object.fromEntries(Object.keys(v).sort().map(k=>[k,stable(v[k])])):v;
  return JSON.stringify(stable(d));
}
function PKG_resultNotice(d){
  if(d.complete) return t('pkg.complete');
  const labels=(d.gaps||[]).map(k=>{
    let label=t('pkg.item.'+k);
    if(k==='company'){
      const missing=(d.items?.company?.missing||[]).map(f=>t('pkg.company.'+f));
      if(missing.length) label+=' ('+missing.join(', ')+')';
    }
    return label;
  });
  if(d.unreadable?.length) labels.push(t('pkg.unreadable'));
  if(d.unprocessed?.length) labels.push(t('pkg.unprocessed'));
  return tf('pkg.needs_review',{n:labels.length,items:labels.join('·')});
}
App.pkgRefresh = async function(cid){
  RS._pkgRefreshing=RS._pkgRefreshing||{};
  if(RS._pkgRefreshing[cid]) return;
  RS.pkg=RS.pkg||{};
  const previous=RS.pkg[cid];
  RS._pkgRefreshing[cid]=true; render();
  try{
    const r=await apiFetch('/cases/'+cid+'/package-check');
    if(!r.ok) return toast(await apiErr(r,t('pkg.error')));
    const d=await r.clone().json();
    if(!d || d.error) return toast(await apiErr(r,t('pkg.error')));
    RS.pkg[cid]=d;
    const unchanged=previous&&!previous.error&&PKG_resultKey(previous)===PKG_resultKey(d);
    toast(PKG_resultNotice(d)+(unchanged?' '+t('pkg.unchanged'):''));
  }catch(e){ toast(t('pkg.error')); }
  finally{ delete RS._pkgRefreshing[cid]; render(); }
};
App.pkgReproc = async function(cid){
  const r = await apiFetch('/cases/'+cid+'/documents/reprocess-pending', {method:'POST', body:'{}'});
  if(!r.ok) return toast(await apiErr(r, '재처리를 시작하지 못했습니다.'));
  const d = await r.json();
  toast(d.queued ? `미판독·분석 실패 서류 ${d.queued}건을 재처리 큐에 넣었습니다. 끝나면 자동으로 다시 점검합니다(1건당 수 초~수십 초).` : '재처리할 서류가 없습니다(원본 미보관 또는 자동 파싱 꺼짐).');
};
App.pkgEdit = function(cid, k){
  if(!PKG_canEdit()) return;
  if(k==='material') return App.pkgMaterialOpen(cid);
  if(k==='product' && !canCall('POST','/cases/{}/products')) return;
  const f = (id, label, ph) => `<div class="field"><label for="${id}">${label}</label><input class="in" id="${id}" placeholder="${ph||''}"></div>`;
  const forms = {
    company: f('pk-co','업체명')+f('pk-nib','사업자등록번호')+f('pk-ceo','대표자')+f('pk-addr','주소'),
    product: '<div id="pk-product" aria-live="polite"></div><hr>'+f('pk-pn','제품명')+f('pk-pd','설명(선택)'),
    factory: f('pk-fa','공장 주소')+f('pk-fr','공장등록번호(선택)'),
  };
  openModal(PKG_LABEL[k]+' 수기 입력', forms[k] + `<div class="inline-msg" id="pk-e"></div><p class="muted">서류에서 찾지 못한 값을 직접 입력합니다. 입력 기록은 감사 로그에 남습니다.</p>`,
    `<button class="btn" onclick="App.closeModal()">취소</button><button class="btn btn-primary" onclick="App.pkgSave('${cid}','${k}')">저장</button>`);
  if(k==='product'){
    S.pkgProduct={cid,pending:[],loading:true,busy:false};
    PKG_pendingLoad(S.pkgProduct,'product');
  }
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
    if(!PKG_canEdit() || !canCall('POST','/cases/{}/products')) return;
    if(!v('pk-pn')) return err('제품명을 입력해 주세요.');
    r = await apiFetch('/cases/'+cid+'/products', {method:'POST', body:JSON.stringify({name:v('pk-pn'), description:v('pk-pd')||null})});
  } else {
    if(!v('pk-fa')) return err('공장 주소를 입력해 주세요.');
    const b = {factory_address:v('pk-fa')}; if(v('pk-fr')) b.factory_reg_no = v('pk-fr');
    r = await apiFetch('/cases/'+cid+'/profile', {method:'PATCH', body:JSON.stringify(b)});
  }
  if(!r.ok) return err(await apiErr(r, '저장하지 못했습니다.'));
  closeModal(); await PKG_load(cid); render(); toast(PKG_LABEL[k]+'을(를) 입력했습니다.');
};

/* 제품·원재료 모두 원래 추출값을 한 번에 처리해 이름 수정·제외 이력을 남긴다. */
function PKG_pendingReviewer(){ return ['auditor','consultant','operator','admin','fatwa_liaison'].includes(AUTH.role); }
function PKG_pendingTable(s, kind){
  const product=kind==='product', key=product?'pkgProduct':'pkgMaterial';
  const edit=PKG_canEdit() && canCall('POST',product?'/cases/{}/products':'/cases/{}/materials') && canCall('POST','/cases/{}/pending-extractions/apply');
  const reviewer=PKG_pendingReviewer(), pending=s.pending||[];
  return `<h4>${t(product?'intake.pending_product':'intake.pending_material')}</h4>
    ${!reviewer?`<p class="note">${t('intake.reviewer_notice')} <button class="btn btn-sm" onclick="App.intakeMemo('${jsq(s.cid)}')">${t('intake.memo')}</button></p>`:''}
    ${s.loading?`<p class="muted">${t('members.loading')}</p>`:s.error?`<p class="note attn">${esc(s.error)}</p>`:!pending.length?`<p class="muted">${t('intake.empty')}</p>`:`
      <div class="tbl-wrap"><table><thead><tr><th>${t('intake.select')}</th><th>${t('intake.field.name')}</th><th>${t('intake.field.filename')}</th>${reviewer?`<th>${t('intake.dismiss')}</th>`:''}<th>${t('intake.field.status')}</th></tr></thead><tbody>
      ${pending.map((it,i)=>`<tr><td><input type="checkbox" aria-label="${t('intake.select')} ${i+1}" ${it.selected?'checked':''} ${!edit||s.busy||it.saved?'disabled':''} onchange="App.pkgPendingToggle('${kind}',${i},'selected',this.checked)"></td>
        <td>${reviewer?`<input class="in" aria-label="${t('intake.field.name')} ${i+1}" value="${esc(it.name)}" ${!edit||s.busy||it.saved?'disabled':''} oninput="S.${key}.pending[${i}].name=this.value">`:esc(it.value)}</td>
        <td>${esc(it.filename||t('intake.empty'))}</td>${reviewer?`<td><input type="checkbox" aria-label="${t('intake.dismiss')} ${i+1}" ${it.dismiss?'checked':''} ${!edit||s.busy||it.saved?'disabled':''} onchange="App.pkgPendingToggle('${kind}',${i},'dismiss',this.checked)"></td>`:''}
        <td>${esc(it.result||(it.reason==='possible_same_product'?t('intake.reason.possible_same_product'):''))}</td></tr>`).join('')}</tbody></table></div>`}
    ${INTAKE_conflicts(s.conflicts)}
    ${edit&&reviewer?`<label class="field">${t('intake.reason')}<textarea class="in" required oninput="S.${key}.reason=this.value">${esc(s.reason||'')}</textarea></label>`:''}
    <div class="row-act"><button class="btn btn-sm" ${s.busy?'disabled':''} onclick="App.${product?'pkgProductRefresh':'pkgMaterialRefresh'}()">${t('intake.reload')}</button>
      ${edit?`<button class="btn btn-primary" ${s.busy||s.loading||!pending.some(it=>!it.saved)?'disabled':''} onclick="App.${product?'pkgProductApply':'pkgMaterialApply'}()">${t('intake.apply')}</button>`:''}</div>`;
}
App.pkgPendingToggle = function(kind, i, field, checked){
  const s=kind==='product'?S.pkgProduct:S.pkgMaterial;
  if(!s || s.busy || (field==='dismiss'&&!PKG_pendingReviewer())) return;
  s.pending[i][field]=checked;
  if(checked) s.pending[i][field==='selected'?'dismiss':'selected']=false;
  (kind==='product'?PKG_productRender:PKG_materialRender)(s);
};
function PKG_productRender(s){
  const el=$('#pk-product');
  if(el && S.pkgProduct===s) el.innerHTML=PKG_pendingTable(s,'product')+(s.notice?`<p class="note" role="status">${esc(s.notice)}</p>`:'');
}
async function PKG_pendingLoad(s, kind){
  const redraw=kind==='product'?PKG_productRender:PKG_materialRender;
  s.loading=true; s.error=''; redraw(s);
  try{
    const r=await apiFetch('/cases/'+s.cid+'/pending-extractions');
    if(!r.ok){ s.error=await apiErr(r,t('intake.error')); return; }
    const d=await r.json(), prev=s.pending||[];
    s.pending=(d.items||[]).filter(it=>it.kind===(kind==='product'?'product_names':'material_names')).map(it=>{
      const old=prev.find(p=>p.document_id===it.document_id && p.value===it.value);
      return old || {...it,name:it.value,selected:false,dismiss:false,result:''};
    });
    s.failed=d.failed||[];
    s.conflicts=d.conflicts||[];
  }catch(e){ s.error=t('intake.error'); }
  finally{ s.loading=false; redraw(s); }
}
async function PKG_pendingApply(s, kind){
  const product=kind==='product', redraw=product?PKG_productRender:PKG_materialRender;
  if(!s || s.busy || !PKG_canEdit() || !canCall('POST',product?'/cases/{}/products':'/cases/{}/materials') || !canCall('POST','/cases/{}/pending-extractions/apply')) return;
  const reviewer=PKG_pendingReviewer();
  const reason=(s.reason||'').trim();
  if(reviewer&&!reason){ s.notice=t('intake.reason_required'); return redraw(s); }
  const selected=s.pending.filter(it=>(it.selected||(reviewer&&it.dismiss))&&!it.saved);
  if(!selected.length){ s.notice=t('intake.select_required'); return redraw(s); }
  if(reviewer&&selected.some(it=>!it.dismiss&&!it.name.trim())){ s.notice=t('intake.name_required'); return redraw(s); }
  const values=[], renames=Object.create(null), dismiss=[];
  for(const it of selected){
    if(!reviewer) values.push(it.value);
    else if(it.dismiss) dismiss.push(it.value);
    else if(it.name.trim()===it.value) values.push(it.value);
    else renames[it.value]=it.name.trim();
  }
  s.busy=true; s.notice=''; redraw(s);
  try{
    const r=await apiFetch('/cases/'+s.cid+'/pending-extractions/apply', {method:'POST',body:JSON.stringify(reviewer?{values,renames,dismiss,reason}:{values})});
    if(!r.ok){ s.notice=await apiErr(r,t('intake.error')); return; }
    selected.forEach(it=>{it.saved=true; it.selected=false;});
    s.notice=t('intake.saved');
    await PKG_pendingLoad(s,kind);
    await PKG_load(s.cid); render();
  }catch(e){ s.notice=t('intake.error'); }
  finally{ s.busy=false; redraw(s); }
}
App.pkgProductRefresh = async function(){
  const s=S.pkgProduct; if(s && !s.busy) await PKG_pendingLoad(s,'product');
};
App.pkgProductApply = function(){ return PKG_pendingApply(S.pkgProduct,'product'); };

/* 원재료 등록은 창을 유지한다 — 실패 행은 재시도하고 성공 행은 비워 연속 입력한다. */
function PKG_materialRow(){ return {name:'',supplier:'',origin:'',result:''}; }
function PKG_materialRender(s){
  const el=$('#pk-material');
  if(!el || S.pkgMaterial!==s) return;
  const edit=PKG_canEdit() && canCall('POST','/cases/{}/materials'), disabled=s.busy?'disabled':'';
  el.innerHTML=PKG_pendingTable(s,'material')+`
    ${(s.failed||[]).length?`<p class="note attn">AI 분석 실패 서류 ${s.failed.length}건 — 재처리 필요
      ${edit&&['consultant','operator','auditor','admin'].includes(AUTH.role)&&canCall('POST','/cases/{}/documents/reprocess-pending')?`<button class="btn btn-sm" ${disabled} onclick="App.pkgReproc('${jsq(s.cid)}')">재처리</button>`:'<span>컨설턴트·운영 담당자에게 재처리를 요청해 주세요.</span>'}</p>`:''}
    <hr><h4>여러 건 직접 입력</h4><p class="muted">빈 이름 행은 무시합니다. 저장된 행은 비워지므로 이어서 입력할 수 있습니다.</p>
    <div class="tbl-wrap"><table><thead><tr><th>원재료명</th><th>공급사(선택)</th><th>원산지(선택)</th><th>결과</th></tr></thead><tbody>
      ${s.rows.map((it,i)=>`<tr>${['name','supplier','origin'].map((k,j)=>`<td><input class="in" aria-label="${['원재료명','공급사','원산지'][j]} ${i+1}" value="${esc(it[k])}" ${!edit||s.busy?'disabled':''} oninput="S.pkgMaterial.rows[${i}].${k}=this.value;S.pkgMaterial.rows[${i}].result='';this.closest('tr').querySelector('.pk-result').textContent=''"></td>`).join('')}<td class="pk-result">${esc(it.result)}</td></tr>`).join('')}
    </tbody></table></div>
    ${edit?`<div class="row-act"><button class="btn" ${disabled} onclick="App.pkgMaterialAddRow()">행 추가</button><button class="btn btn-primary" ${disabled} onclick="App.pkgMaterialSave()">모두 저장</button></div>`:''}
    <hr><h4>파일로 일괄 등록</h4><p class="muted">원료 리스트·성분표를 올리면 구조 파서가 읽은 값은 등록되고, AI 추출값은 위 목록에서 확인합니다.</p>
    ${edit&&canCall('POST','/cases/{}/parse-file')?`<input class="in" type="file" aria-label="원료 리스트·성분표 파일" accept=".xlsx,.xls,.pdf,.csv,.docx,.doc,.txt,.hwp,.jpg,.jpeg,.png" ${disabled} onchange="App.pkgMaterialFile(this.files[0])">`:'<p class="muted">파일 업로드는 신청기업·컨설턴트 계정에서 할 수 있습니다.</p>'}
    ${s.fileResult?`<p class="note">${esc(s.fileResult)}</p>`:''}
    ${s.notice?`<p class="note" role="status">${esc(s.notice)}</p>`:''}`;
}
async function PKG_materialLoad(s){
  return PKG_pendingLoad(s,'material');
}
async function PKG_materialUpdated(s){
  await PKG_materialLoad(s);
  await PKG_load(s.cid); render(); PKG_materialRender(s);
}
App.pkgMaterialOpen = async function(cid){
  if(!PKG_canEdit() || !canCall('POST','/cases/{}/materials')) return;
  const s={cid,rows:[PKG_materialRow()],pending:[],failed:[],loading:true,busy:false};
  S.pkgMaterial=s;
  openModal('원재료 등록', '<div id="pk-material" aria-live="polite"></div>',
    '<button class="btn" onclick="App.closeModal()">닫기</button>', true);
  await PKG_materialLoad(s);
};
App.pkgMaterialRefresh = async function(){
  const s=S.pkgMaterial; if(!s || s.busy) return;
  await PKG_materialLoad(s);
};
App.pkgMaterialAddRow = function(){
  const s=S.pkgMaterial; if(!s || s.busy || !PKG_canEdit() || !canCall('POST','/cases/{}/materials')) return;
  s.rows.push(PKG_materialRow()); PKG_materialRender(s);
};
App.pkgMaterialApply = function(){ return PKG_pendingApply(S.pkgMaterial,'material'); };
App.pkgMaterialSave = async function(){
  const s=S.pkgMaterial;
  if(!s || s.busy || !PKG_canEdit() || !canCall('POST','/cases/{}/materials')) return;
  const rows=s.rows.filter(it=>it.name.trim());
  if(!rows.length){ s.notice='원재료명을 입력해 주세요.'; return PKG_materialRender(s); }
  s.busy=true; s.notice=''; PKG_materialRender(s);
  const groups=new Map();
  for(const it of rows){
    const key=it.name.replace(/\s/g,'').toLowerCase();
    if(!groups.has(key)) groups.set(key,[]);
    groups.get(key).push(it);
  }
  let saved=0, existing=0;
  try{
    for(const group of groups.values()){
      const it=group[0], name=it.name.trim();
      try{
        const r=await apiFetch('/cases/'+s.cid+'/materials', {method:'POST',body:JSON.stringify({name,supplier:it.supplier.trim()||null,origin:it.origin.trim()||null})});
        const d=r.status===409?await r.clone().json().catch(()=>({})):{};
        const duplicate=r.status===409 && (d.detail||{}).code==='DUPLICATE_MATERIAL';
        if(r.ok || duplicate){
          group.forEach((row,i)=>Object.assign(row,PKG_materialRow(),{result:(duplicate||i?'이미 등록됨: ':'저장됨: ')+name}));
          saved+=r.ok?1:0; existing+=group.length-(r.ok?1:0);
        }else{
          const reason='실패: '+await apiErr(r,'등록하지 못했습니다.');
          group.forEach(row=>{row.result=reason;});
        }
      }catch(e){ group.forEach(row=>{row.result='실패: 서버 연결을 확인해 주세요.';}); }
      PKG_materialRender(s);
    }
    s.notice=`${saved}건 저장, ${existing}건 이미 등록됨, ${rows.length-saved-existing}건 실패`;
    await PKG_materialUpdated(s);
  }finally{ s.busy=false; PKG_materialRender(s); }
};
App.pkgMaterialFile = async function(file){
  const s=S.pkgMaterial;
  if(!file || !s || s.busy || !PKG_canEdit() || !canCall('POST','/cases/{}/materials') || !canCall('POST','/cases/{}/parse-file')) return;
  if(file.size>20*1024*1024){ s.fileResult='실패: 용량 초과(파일당 최대 20MB)'; return PKG_materialRender(s); }
  s.busy=true; s.fileResult=file.name+' — 읽는 중…'; PKG_materialRender(s);
  try{
    const b64=await ENTA_fileB64(file);
    const r=await apiFetch('/cases/'+s.cid+'/parse-file', {method:'POST',body:JSON.stringify({doc_type:'material_list',filename:file.name,file_b64:b64})});
    if(!r.ok){ s.fileResult='실패: '+await ENTA_uploadError(r); return; }
    const d=await r.json(), ap=d.applied||{};
    s.fileResult=`${file.name} — 원재료 ${ap.materials_added||0}건 등록. 원재료 ${ap.materials_pending||0}건이 확인 대기에 있습니다(원재료 등록 창에서 확인).`;
    await PKG_materialUpdated(s);
  }catch(e){ s.fileResult='실패: 파일 읽기 또는 서버 연결을 확인해 주세요.'; }
  finally{ s.busy=false; PKG_materialRender(s); }
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
{ const wrap = (v, pick) => { const f = VIEWS[v]; if(typeof f==='function') VIEWS[v] = () => { const h = f(); return PKG_panel(pick()) + h; }; };
  const ent = () => ((RS.cases||[])[0]||{}).case_id;
  wrap('ent-home', ent); wrap('ent-apply', ent);
  wrap('cons-status', () => S.selCompany);
  wrap('aud-pre', () => S.selPre);
  wrap('aud-detail', () => S.audCo); }
