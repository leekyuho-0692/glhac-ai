/* ===== 컨설턴트 문서관리 — SJPH 매뉴얼 패널 ===== */
const CSJ_ELEMS = ['commitment','materials','process','product','monitoring'];
const CSJ_ST = ['not_started','gap','ok'];

const CSJ_get = async (path) => {
  try{ const r = await apiFetch(path); return r.ok ? await r.json() : {error:true, status:r.status}; }
  catch(e){ return {error:true}; }
};
const CSJ_key = (cid) => 'csj:'+cid;
function CSJ_cache(cid){
  const k = CSJ_key(cid); RS.__csj = RS.__csj || {}; return RS.__csj[k] || (RS.__csj[k] = {});
}
async function CSJ_loadSjph(cid){
  const c = CSJ_cache(cid);
  if(c.sjph && c.sjph.t) return;
  c.sjph = await CSJ_get('/cases/'+cid+'/sjph'); c.sjph.t = 1;
}
async function CSJ_loadEv(cid){
  const c = CSJ_cache(cid);
  if(c.ev && c.ev.t) return;
  c.ev = await CSJ_get('/cases/'+cid+'/sjph-evidence'); c.ev.t = 1;
}
const CSJ_err = (d, msg) => `<section class="panel"><p class="empty">${d && d.status===403 ? '권한이 없습니다.' : msg}</p></section>`;

const CSJ_sjph = (cid) => {
  const c = CSJ_cache(cid);
  if(!c.sjph){ CSJ_loadSjph(cid).then(()=>render()); return '<section class="panel"><p class="empty">불러오는 중…</p></section>'; }
  const d = c.sjph;
  if(d.error) return CSJ_err(d, 'HPAS 상태를 불러오지 못했습니다.');
  const els = d.elements || [];
  const rows = els.map((e,i)=>{
    const opts = CSJ_ST.map(s=>`<option value="${s}" ${e.status===s?'selected':''}>${esc(s)}</option>`).join('');
    return `<tr><td><span>${esc(e.element_ko||'')}</span> <span class="muted mono">${esc(e.element||'')}</span></td>
      <td><select class="in" id="csj-st-${i}">${opts}</select></td>
      <td><input class="in" id="csj-note-${i}" value="${esc(e.note||'')}" placeholder="메모"></td>
      <td class="num"><button class="btn btn-sm" onclick="App.csjSaveEl('${cid}', ${i})">저장</button></td></tr>`;
  }).join('');
  const bar = `<div class="summary"><div><span class="k">완료율</span><span class="v mono">${d.completion||0}%</span></div>
    <div><span class="k">Penyelia Halal</span><span class="v">${d.penyelia_ok?'등록됨':'미등록'}</span></div>
    <div><span class="k">전체 완료</span><span class="v">${d.complete?'예':'아니오'}</span></div></div>`;
  const manual = `<div class="fieldset"><legend>매뉴얼 생성·내려받기</legend>
    <div class="inline"><button class="btn btn-primary" onclick="App.csjManual('${cid}')">SJPH 매뉴얼 생성</button>
    <button class="btn" onclick="App.csjDoc('${cid}','docx')">DOCX</button>
    <button class="btn" onclick="App.csjDoc('${cid}','pdf')">PDF</button></div></div>`;
  return `<section class="panel">${bar}
    <div class="tbl-wrap"><table><thead><tr><th>요소</th><th>상태</th><th>메모</th><th></th></tr></thead><tbody>${rows}</tbody></table></div>
    ${manual}</section>`;
};

const CSJ_ev = (cid) => {
  const c = CSJ_cache(cid);
  if(!c.ev){ CSJ_loadEv(cid).then(()=>render()); return '<section class="panel"><p class="empty">불러오는 중…</p></section>'; }
  const d = c.ev;
  if(d.error) return CSJ_err(d, '증빙 목록을 불러오지 못했습니다.');
  const items = d.items || [];
  const rows = items.map((it,i)=>{
    const up = it.uploaded ? `<span class="badge strong">제출</span>` : `<span class="badge attn">미제출</span>`;
    const doc = it.document_id ? `<button class="link" onclick="App.docDownload('${it.document_id}', ${JSON.stringify(it.filename||'evidence')})">${esc(it.filename||'')}</button>` : '<span class="muted">—</span>';
    return `<tr><td><span>${esc(it.label||'')}</span> <span class="muted mono">${esc(it.item_key||'')}</span></td><td>${up}</td><td>${doc}</td>
      <td class="row-act"><label class="btn btn-sm">업로드<input type="file" style="display:none" onchange="App.csjEvUp('${cid}','${it.item_key}',this)"></label></td></tr>`;
  }).join('');
  const bar = `<div class="summary"><div><span class="k">제출</span><span class="v mono">${d.uploaded||0} / ${d.total||0}</span></div>
    <div><span class="k">완료율</span><span class="v mono">${d.completion||0}%</span></div></div>`;
  return `<section class="panel"><div class="panel-head"><h2>SJPH 증빙</h2>
    <button class="btn btn-sm" onclick="App.csjAutofile('${cid}')">업로드 문서에서 자동 연결</button></div>
    ${bar}<div class="tbl-wrap"><table><thead><tr><th>항목</th><th>상태</th><th>문서</th><th></th></tr></thead><tbody>${rows||'<tr><td colspan="4" class="empty">항목이 없습니다.</td></tr>'}</tbody></table></div></section>`;
};

App.csjPanel = (cid) => CSJ_sjph(cid) + CSJ_ev(cid);

{ const f = VIEWS['cons-docs'];
  VIEWS['cons-docs'] = () => {
    const extra = (typeof S!=='undefined' && S.selDocCo) ? App.csjPanel(S.selDocCo) : '';
    return (f? f():'') + extra;
  };
}

App.csjSaveEl = async function(cid, i){
  const c = CSJ_cache(cid); const e = (c.sjph && c.sjph.elements) ? c.sjph.elements[i] : null;
  if(!e) return;
  const st = (($('#csj-st-'+i)||{}).value||'').trim();
  const note = (($('#csj-note-'+i)||{}).value||'').trim();
  const body = {element:e.element, status:st, note:note};
  try{
    const r = await apiFetch('/cases/'+cid+'/sjph', {method:'PATCH', body:JSON.stringify(body)});
    if(!r.ok){ let code=''; try{ const j=await r.json(); code=j.detail&&j.detail.code?' ('+j.detail.code+')':''; }catch(_){}
      if(r.status===403) return toast('권한이 없습니다.');
      return toast('저장에 실패했습니다.'+code); }
  }catch(e){ return toast('저장에 실패했습니다.'); }
  delete c.sjph; await CSJ_loadSjph(cid); render(); toast('저장했습니다.');
};

App.csjManual = async function(cid){
  toast('매뉴얼을 생성하고 있습니다…');
  try{
    const r = await apiFetch('/cases/'+cid+'/sjph/manual', {method:'POST', body:JSON.stringify({})});
    if(!r.ok){ let code=''; try{ const j=await r.json(); code=j.detail&&j.detail.code?' ('+j.detail.code+')':''; }catch(_){}
      if(r.status===403) return toast('권한이 없습니다.');
      if(r.status===409) return toast('결제 완료 후 생성할 수 있습니다.'+code);
      return toast('매뉴얼 생성에 실패했습니다.'+code); }
    const j = await r.json();
    toast('매뉴얼 v'+(j.version||'?')+' 생성 완료');
  }catch(e){ toast('매뉴얼 생성에 실패했습니다.'); }
};

App.csjDoc = async function(cid, fmt){
  const path = '/cases/'+cid+'/sjph-manual.'+fmt;
  const name = 'sjph-manual.'+fmt;
  try{
    const r = await apiFetch(path);
    if(!r.ok) return toast('파일을 불러올 수 없습니다.');
    const blob = await r.blob(); const a = document.createElement('a');
    a.href = URL.createObjectURL(blob); a.download = name;
    document.body.appendChild(a); a.click();
    setTimeout(()=>{ URL.revokeObjectURL(a.href); a.remove(); }, 4000);
  }catch(e){ toast('파일을 불러올 수 없습니다.'); }
};

App.csjAutofile = async function(cid){
  try{
    const r = await apiFetch('/cases/'+cid+'/sjph-evidence/autofile', {method:'POST', body:JSON.stringify({})});
    if(!r.ok){ let code=''; try{ const j=await r.json(); code=j.detail&&j.detail.code?' ('+j.detail.code+')':''; }catch(_){}
      if(r.status===403) return toast('권한이 없습니다.');
      return toast('자동 연결에 실패했습니다.'+code); }
    const j = await r.json();
    delete CSJ_cache(cid).ev; await CSJ_loadEv(cid); render();
    toast('증빙 '+(j.count||0)+'건을 연결했습니다.');
  }catch(e){ toast('자동 연결에 실패했습니다.'); }
};

App.csjEvUp = async function(cid, itemKey, input){
  const f = input && input.files && input.files[0];
  if(!f) return;
  const rd = new FileReader();
  rd.onload = async () => {
    const b64 = String(rd.result||'').split(',').pop();
    const body = {item_key:itemKey, file_b64:b64, filename:f.name};
    try{
      const r = await apiFetch('/cases/'+cid+'/sjph-evidence', {method:'POST', body:JSON.stringify(body)});
      if(!r.ok){ let code=''; try{ const j=await r.json(); code=j.detail&&j.detail.code?' ('+j.detail.code+')':''; }catch(_){}
        if(r.status===403) return toast('권한이 없습니다.');
        return toast('업로드에 실패했습니다.'+code); }
      delete CSJ_cache(cid).ev; await CSJ_loadEv(cid); render(); toast('업로드했습니다.');
    }catch(e){ toast('업로드에 실패했습니다.'); }
  };
  rd.readAsDataURL(f);
};
