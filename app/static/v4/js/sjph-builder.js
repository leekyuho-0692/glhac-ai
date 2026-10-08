/* ===== SJPH 매뉴얼 빌더(구 화면 이식) ===== */
RS.sjb = RS.sjb || {};

const SJB_ROLE_LAYOUT = ['applicant','consultant','penyelia_halal','admin'];
const SJB_ROLE_PLACE = ['consultant','auditor','operator','admin'];

const SJB_canLayout = () => SJB_ROLE_LAYOUT.includes(AUTH.role);
const SJB_canPlace = () => SJB_ROLE_PLACE.includes(AUTH.role);

const SJB_pick = (accept) => new Promise((resolve) => {
  const inp = document.createElement('input');
  inp.type = 'file';
  inp.accept = accept;
  inp.style.display = 'none';
  inp.onchange = () => { const f = inp.files && inp.files[0] ? inp.files[0] : null; resolve(f); inp.remove(); };
  document.body.appendChild(inp);
  inp.click();
});

const SJB_dataURL = (file) => new Promise((resolve, reject) => {
  const fr = new FileReader();
  fr.onload = () => resolve(fr.result);
  fr.onerror = () => reject(new Error('read'));
  fr.readAsDataURL(file);
});

async function SJB_load(cid){
  try{
    const [lr, pr] = await Promise.all([
      apiFetch('/cases/'+cid+'/sjph-manual/layout'),
      apiFetch('/cases/'+cid+'/manual-placement?lang=ko')
    ]);
    if(!lr.ok || !pr.ok){
      RS.sjb[cid] = {error:true, status:(!lr.ok ? lr.status : pr.status)};
      return;
    }
    const L = await lr.json();
    const P = await pr.json();
    RS.sjb[cid] = {L, P};
  }catch(e){
    RS.sjb[cid] = {error:true};
  }
}

function SJB_panel(cid){
  if(!cid) return '';
  const d = RS.sjb[cid];
  if(!d){
    if(RS._sjbLoading !== cid){ RS._sjbLoading = cid; SJB_load(cid).then(()=>{ RS._sjbLoading = null; render(); }); }
    return `<section class="panel"><p class="empty">불러오는 중…</p></section>`;
  }
  if(d.error){
    return `<section class="panel"><p class="empty">${d.status===403 ? '권한이 없습니다.' : '매뉴얼 빌더를 불러오지 못했습니다.'}</p></section>`;
  }
  const L = d.L, P = d.P;
  return `<section class="panel">${SJB_head(L)}${SJB_sections(cid, L)}${SJB_signers(cid, L)}${SJB_place(cid, P)}</section>`;
}

function SJB_head(L){
  const done = L.done == null ? 0 : L.done;
  const total = L.total == null ? 0 : L.total;
  return `<div class="panel-head"><h2>할랄 매뉴얼 빌더</h2><span class="badge ${L.ready?'strong':'attn'}">섹션 ${done}/${total}</span></div>`;
}

function SJB_statusBadge(s){
  if(s.image) return '<span class="badge strong">이미지 삽입됨</span>';
  if(s.auto) return '<span class="badge">자동 생성 인정</span>';
  if(s.has_default) return '<span class="badge">기본 내용 포함</span>';
  return '<span class="badge attn">이미지 필요</span>';
}

function SJB_sections(cid, L){
  const list = L.sections || [];
  const can = SJB_canLayout();
  const rows = list.map((s, i) => {
    const img = s.image;
    const acts = can ? `<div class="row-act">
      ${i > 0 ? `<button class="btn btn-sm" onclick="App.sjbMove('${cid}',${i},-1)">▲</button>` : ''}
      ${i < list.length-1 ? `<button class="btn btn-sm" onclick="App.sjbMove('${cid}',${i},1)">▼</button>` : ''}
      <button class="btn btn-sm" onclick="App.sjbImg('${cid}',${i})">이미지 올리기</button>
      ${img ? `<button class="btn btn-sm btn-ghost" onclick="App.sjbImgDel('${cid}',${i})">이미지 빼기</button>` : ''}
    </div>` : '';
    return `<div class="row">
      <div style="min-width:0">
        ${i+1}. <b>${esc(s.ko||'')}</b> <span class="muted">${esc(s.en||'')}</span> ${SJB_statusBadge(s)}
        ${img ? `<div class="muted" style="font-size:12px">${esc(img.filename||'')}</div>` : ''}
        ${img && img.caption ? `<div class="muted" style="font-size:12px;white-space:pre-wrap;max-height:80px;overflow:auto">${esc(img.caption)}</div>` : ''}
      </div>
      ${acts}
    </div>`;
  }).join('');
  return `<h3>섹션 순서 · 이미지 삽입</h3><div class="rows">${rows || '<p class="empty">섹션이 없습니다.</p>'}</div>`;
}

function SJB_signers(cid, L){
  const slots = L.signer_slots || [];
  const rows = slots.map((slot, j) => {
    const v = slot.value || {};
    const can = !!slot.can_edit;
    const stampBadge = v.stamp_document_id ? '<span class="badge strong">도장 등록됨</span>' : '<span class="badge">도장 없음</span>';
    const fields = can ? `<div class="inline">
        <input class="in" id="sjb-n-${j}" value="${esc(v.name||'')}" placeholder="성명">
        <input class="in" id="sjb-p-${j}" value="${esc(v.position||'')}" placeholder="${esc(slot.default_position||'')}">
        <button class="btn btn-sm btn-primary" onclick="App.sjbSigner('${cid}',${j})">저장</button>
        <button class="btn btn-sm" onclick="App.sjbStamp('${cid}',${j})">도장 올리기</button>
        ${v.stamp_document_id ? `<button class="btn btn-sm btn-ghost" onclick="App.sjbStampDel('${cid}',${j})">도장 빼기</button>` : ''}
      </div>`
      : `<div class="muted">${esc(v.name||'—')}${v.position ? ' · '+esc(v.position) : ''}</div>`;
    return `<div class="row">
      <div style="min-width:0"><b>${esc(slot.ko||'')}</b> <span class="muted">${esc(slot.en||'')}</span> ${stampBadge}${fields}</div>
    </div>`;
  }).join('');
  return `<h3>승인자 · 도장</h3>
    <p class="muted" style="font-size:12px">이름·직책은 매뉴얼 서명란에, 도장 이미지는 서명/도장 칸에 들어갑니다. 도장은 선택입니다.</p>
    <div class="rows">${rows || '<p class="empty">서명자 정보가 없습니다.</p>'}</div>`;
}

function SJB_place(cid, P){
  const slots = P.slots || [];
  if(!slots.length) return '';
  const cands = P.candidates || [];
  const can = SJB_canPlace();
  let manual = 0;
  slots.forEach(s => { if(s.decided_by === 'manual') manual++; });
  const rows = slots.map((s, k) => {
    let cell2;
    if(can){
      const opts = ['<option value="">— 비움 —</option>'];
      cands.forEach((cd, c) => {
        const sel = cd.document_id === s.document_id ? ' selected' : '';
        opts.push(`<option value="${c}"${sel}>${esc(cd.filename||'')}</option>`);
      });
      cell2 = `<select class="in" onchange="App.sjbPlace('${cid}',${k},this.value)">${opts.join('')}</select>`;
      if(s.decided_by === 'manual' && s.auto_suggestion && s.auto_suggestion.document_id !== s.document_id){
        cell2 += `<div class="muted" style="font-size:11px">AI 추정: ${esc(s.auto_suggestion.filename||'')}</div>`;
      }
    }else{
      cell2 = `<b>${esc(s.filename||'비움')}</b>`;
    }
    let deco;
    if(s.decided_by === 'manual') deco = '<span class="badge strong">사람 지정</span>';
    else if(s.decided_by === 'auto') deco = '<span class="badge attn">AI 추정</span>';
    else deco = '<span class="badge">비움</span>';
    const reason = s.reason ? `<div class="muted" style="font-size:11px">${esc(s.reason)}</div>` : '';
    return `<tr>
      <td><b>${esc(s.label||'')}</b><div class="muted" style="font-size:11px">${esc(s.anchor||'')}</div></td>
      <td>${cell2}</td>
      <td>${deco}${reason}</td>
    </tr>`;
  }).join('');
  return `<h3>부록별 도면 배치 <span class="badge">지정 ${manual} · 자동 ${slots.length-manual}</span></h3>
    <p class="muted" style="font-size:12px">아래 문서가 매뉴얼의 해당 부록에 그대로 들어갑니다. AI 추정은 초안이니 생성 전에 확인하세요. 바꾸면 '사람 지정'으로 기록됩니다.</p>
    <div class="tbl-wrap"><table>
      <thead><tr><th>삽입 위치</th><th>삽입 문서</th><th>결정</th></tr></thead>
      <tbody>${rows}</tbody>
    </table></div>
    ${can ? `<div style="margin-top:10px"><button class="btn btn-sm" onclick="App.sjbPlaceReset('${cid}')">전부 자동 추정으로 되돌리기</button></div>` : ''}`;
}

async function SJB_fail(r){
  if(r.status === 403){ toast('권한이 없습니다.'); return; }
  let code = '';
  try{ const j = await r.json(); if(j && j.detail && typeof j.detail === 'object') code = j.detail.code || ''; }catch(e){}
  toast('저장에 실패했습니다.' + (code ? ' ('+code+')' : ''));
}

async function SJB_freshLayout(cid){
  const r = await apiFetch('/cases/'+cid+'/sjph-manual/layout');
  if(!r.ok) return null;
  return await r.json();
}

function SJB_state(L){
  return {
    order: (L.sections||[]).map(s => s.key),
    inserts: (function(){ const o = {}; (L.sections||[]).forEach(s => { if(s.image) o[s.key] = s.image; }); return o; })(),
    signers: Object.assign({}, L.signers||{})
  };
}

async function SJB_saveLayout(cid, mutate){
  let L;
  try{ L = await SJB_freshLayout(cid); }catch(e){ toast('네트워크 오류가 발생했습니다.'); return false; }
  if(!L){ toast('저장에 실패했습니다.'); return false; }
  const st = SJB_state(L);
  if(mutate) mutate(st, L);
  try{
    const r = await apiFetch('/cases/'+cid+'/sjph-manual/layout', {method:'POST', body:JSON.stringify(st)});
    if(!r.ok){ await SJB_fail(r); return false; }
    return true;
  }catch(e){ toast('네트워크 오류가 발생했습니다.'); return false; }
}

App.sjbMove = async function(cid, i, d){
  const ok = await SJB_saveLayout(cid, (st) => {
    const j = i + d;
    if(j < 0 || j >= st.order.length) return;
    const t = st.order[i]; st.order[i] = st.order[j]; st.order[j] = t;
  });
  if(!ok) return;
  delete RS.sjb[cid]; render(); toast('섹션 순서를 바꿨습니다.');
};

App.sjbImg = async function(cid, i){
  const file = await SJB_pick('image/*,.pdf');
  if(!file) return;
  let dataURL;
  try{ dataURL = await SJB_dataURL(file); }catch(e){ toast('파일을 읽을 수 없습니다.'); return; }
  let document_id;
  try{
    const r = await apiFetch('/cases/'+cid+'/documents', {method:'POST', body:JSON.stringify({filename:file.name, file_b64:dataURL, doc_type:'manual_section'})});
    if(!r.ok){
      if(r.status === 413){ toast('파일이 너무 큽니다.'); return; }
      await SJB_fail(r); return;
    }
    const j = await r.json();
    document_id = j.document_id;
  }catch(e){ toast('네트워크 오류가 발생했습니다.'); return; }
  const ok = await SJB_saveLayout(cid, (st, L) => {
    const s = (L.sections||[])[i];
    if(!s) return;
    st.inserts[s.key] = {document_id, filename:file.name};
  });
  if(!ok) return;
  delete RS.sjb[cid]; render(); toast('이미지를 넣었습니다.');
};

App.sjbImgDel = async function(cid, i){
  if(!confirm('이 섹션의 이미지를 뺄까요?')) return;
  const ok = await SJB_saveLayout(cid, (st, L) => {
    const s = (L.sections||[])[i];
    if(s) delete st.inserts[s.key];
  });
  if(!ok) return;
  delete RS.sjb[cid]; render(); toast('이미지를 뺐습니다.');
};

App.sjbSigner = async function(cid, j){
  const name = (($('#sjb-n-'+j)||{}).value||'').trim();
  const position = (($('#sjb-p-'+j)||{}).value||'').trim();
  const ok = await SJB_saveLayout(cid, (st, L) => {
    const slot = (L.signer_slots||[])[j];
    if(!slot) return;
    const cur = st.signers[slot.key] || {};
    st.signers[slot.key] = Object.assign({}, cur, {name, position});
  });
  if(!ok) return;
  delete RS.sjb[cid]; render(); toast('승인자를 저장했습니다.');
};

App.sjbStamp = async function(cid, j){
  const file = await SJB_pick('image/*');
  if(!file) return;
  let dataURL;
  try{ dataURL = await SJB_dataURL(file); }catch(e){ toast('파일을 읽을 수 없습니다.'); return; }
  let document_id;
  try{
    const r = await apiFetch('/cases/'+cid+'/documents', {method:'POST', body:JSON.stringify({filename:file.name, file_b64:dataURL, doc_type:'signer_stamp'})});
    if(!r.ok){
      if(r.status === 413){ toast('파일이 너무 큽니다.'); return; }
      await SJB_fail(r); return;
    }
    const jj = await r.json();
    document_id = jj.document_id;
  }catch(e){ toast('네트워크 오류가 발생했습니다.'); return; }
  const ok = await SJB_saveLayout(cid, (st, L) => {
    const slot = (L.signer_slots||[])[j];
    if(!slot) return;
    const cur = st.signers[slot.key] || {};
    st.signers[slot.key] = Object.assign({}, cur, {stamp_document_id: document_id});
  });
  if(!ok) return;
  delete RS.sjb[cid]; render(); toast('도장을 등록했습니다.');
};

App.sjbStampDel = async function(cid, j){
  const ok = await SJB_saveLayout(cid, (st, L) => {
    const slot = (L.signer_slots||[])[j];
    if(!slot) return;
    const cur = st.signers[slot.key];
    if(cur) delete cur.stamp_document_id;
  });
  if(!ok) return;
  delete RS.sjb[cid]; render(); toast('도장을 뺐습니다.');
};

async function SJB_savePlacement(cid, mutate){
  let P;
  try{
    const r = await apiFetch('/cases/'+cid+'/manual-placement?lang=ko');
    if(!r.ok){ await SJB_fail(r); return false; }
    P = await r.json();
  }catch(e){ toast('네트워크 오류가 발생했습니다.'); return false; }
  const slots = {};
  (P.slots||[]).forEach(s => { if(s.decided_by === 'manual') slots[s.slot] = s.document_id || null; });
  if(mutate) mutate(slots, P);
  try{
    const r = await apiFetch('/cases/'+cid+'/manual-placement', {method:'PUT', body:JSON.stringify({slots})});
    if(!r.ok){ await SJB_fail(r); return false; }
    return true;
  }catch(e){ toast('네트워크 오류가 발생했습니다.'); return false; }
}

App.sjbPlace = async function(cid, k, val){
  const d = RS.sjb[cid];
  if(!d || !d.P) return;
  const P = d.P;
  const slot = (P.slots||[])[k];
  if(!slot) return;
  let docId = null;
  if(val !== ''){
    const c = P.candidates[Number(val)];
    if(!c) return;
    docId = c.document_id;
  }
  const ok = await SJB_savePlacement(cid, (slots) => { slots[slot.slot] = docId; });
  if(!ok) return;
  delete RS.sjb[cid]; render(); toast('배치를 저장했습니다.');
};

App.sjbPlaceReset = async function(cid){
  if(!confirm('사람이 지정한 배치를 모두 지우고 AI 추정으로 되돌립니다. 계속할까요?')) return;
  try{
    const r = await apiFetch('/cases/'+cid+'/manual-placement', {method:'PUT', body:JSON.stringify({slots:{}})});
    if(!r.ok){ await SJB_fail(r); return; }
  }catch(e){ toast('네트워크 오류가 발생했습니다.'); return; }
  delete RS.sjb[cid]; render(); toast('자동 추정으로 되돌렸습니다.');
};

(function(){
  const pick = (v) => {
    if(v === 'cons-docs') return S.selDocCo;
    if(v === 'ent-docs') return ((RS.cases||[])[0]||{}).case_id;
    if(v === 'aud-dossier') return S.selFlow;
    return null;
  };
  ['cons-docs','ent-docs','aud-dossier'].forEach((v) => {
    const f = VIEWS[v];
    if(typeof f === 'function'){
      VIEWS[v] = () => { const h = f(); return h + SJB_panel(pick(v)); };
    }
  });
})();