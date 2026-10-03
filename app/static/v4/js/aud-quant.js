/* ===== 오디터 정량 기준·측정 ===== */
const AUDQ_PARAM_KEYS = () => (RS.audqCrit && RS.audqCrit.items) ? RS.audqCrit.items.map(c=>c.key) : [];

async function AUDQ_loadCrit(){
  try{ const r = await apiFetch('/meta/quant-criteria');
    RS.audqCrit = r.ok ? {items:(await r.json()).criteria || []} : {error:true, status:r.status};
  }catch(e){ RS.audqCrit = {error:true}; }
}
async function AUDQ_loadMeas(cid){
  try{ const r = await apiFetch('/cases/'+cid+'/measurements');
    RS.audqMeas[cid] = r.ok ? await r.json() : {error:true, status:r.status};
  }catch(e){ RS.audqMeas[cid] = {error:true}; }
}
function AUDQ_verdictLabel(v){
  if(v==='pass') return '<span class="badge strong">적합</span>';
  if(v==='fail') return '<span class="badge attn">부적합</span>';
  return '<span class="badge">미정</span>';
}
function AUDQ_critTable(){
  const d = RS.audqCrit;
  if(!d){ if(RS._audqCritLoading!==1){ RS._audqCritLoading=1; AUDQ_loadCrit().then(()=>{ RS._audqCritLoading=null; render(); }); } return '<p class="empty">불러오는 중…</p>'; }
  if(d.error) return `<p class="empty">${d.status===403?'이 화면을 볼 권한이 없습니다.':'불러오지 못했습니다.'}</p>`;
  if(!d.items.length) return '<p class="empty">기준이 없습니다.</p>';
  return `<div class="tbl-wrap"><table><thead><tr>
    <th>항목</th><th>기준값</th><th>단위</th><th>근거</th>
  </tr></thead><tbody>${d.items.map(c=>`<tr>
    <td>${esc(c.ko||c.key)}</td>
    <td class="num mono">${esc(c.max==null?'':String(c.max))}</td>
    <td class="mono">${esc(c.unit||'')}</td>
    <td class="muted">${esc(c.basis||'')}</td>
  </tr>`).join('')}</tbody></table></div>`;
}
function AUDQ_matOptions(cid){
  const raw = RS.mats && RS.mats[cid];
  const arr = Array.isArray(raw) ? raw : (raw && Array.isArray(raw.items) ? raw.items : []);
  return arr;
}
function AUDQ_measTable(cid){
  const d = RS.audqMeas[cid];
  if(!d){ if(RS._audqMeasLoading!==cid){ RS._audqMeasLoading=cid; AUDQ_loadMeas(cid).then(()=>{ RS._audqMeasLoading=null; render(); }); } return '<p class="empty">불러오는 중…</p>'; }
  if(d.error) return `<p class="empty">${d.status===403?'이 화면을 볼 권한이 없습니다.':'불러오지 못했습니다.'}</p>`;
  const items = d.items || [];
  if(!items.length) return '<p class="empty">측정 기록이 없습니다.</p>';
  return `<div class="tbl-wrap"><table><thead><tr>
    <th>원재료</th><th>항목</th><th>측정값</th><th>단위</th><th>기준</th><th>판정</th><th>일시</th><th>기관</th><th></th>
  </tr></thead><tbody>${items.map(m=>`<tr>
    <td>${esc(m.material_name||'')}</td>
    <td>${esc(m.param_ko||m.param_key)}</td>
    <td class="num mono">${esc(m.value==null?'':String(m.value))}</td>
    <td class="mono">${esc(m.unit||'')}</td>
    <td class="num mono">${esc(m.threshold==null?'':String(m.threshold))}</td>
    <td>${AUDQ_verdictLabel(m.verdict)}</td>
    <td class="mono">${esc(m.tested_at||'')}</td>
    <td>${esc(m.lab_name||'')}</td>
    <td><button class="btn btn-sm btn-ghost" onclick="App.audqDel('${esc(m.measurement_id)}')">삭제</button></td>
  </tr>`).join('')}</tbody></table></div>`;
}
function AUDQ_addForm(cid){
  const crit = AUDQ_PARAM_KEYS();
  const mats = AUDQ_matOptions(cid);
  return `<fieldset><legend>측정값 추가</legend>
    <div class="grid-3">
      <div class="field"><label>항목 <span class="req">*</span></label>
        <select class="in" id="audq-param">${crit.map(k=>`<option value="${esc(k)}">${esc(k)}</option>`).join('')}</select></div>
      <div class="field"><label>원재료</label>
        <select class="in" id="audq-mid"><option value="">— 선택 —</option>${mats.map(m=>`<option value="${esc(m.material_id)}">${esc(m.name||m.material_id)}</option>`).join('')}</select></div>
      <div class="field"><label>측정값 <span class="req">*</span></label>
        <input class="in" id="audq-value" type="number" step="any" /></div>
      <div class="field"><label>단위</label><input class="in" id="audq-unit" /></div>
      <div class="field"><label>시험기관</label><input class="in" id="audq-lab" /></div>
      <div class="field"><label>시험일</label><input class="in" id="audq-tested" type="date" /></div>
    </div>
    <div class="field"><label>비고</label><input class="in" id="audq-note" /></div>
    <button class="btn btn-primary" onclick="App.audqAdd('${esc(cid)}')">추가</button>
  </fieldset>`;
}
function AUDQ_renameForm(cid){
  const mats = AUDQ_matOptions(cid);
  return `<fieldset><legend>원재료명 교정</legend>
    <div class="grid-2">
      <div class="field"><label>원재료</label>
        <select class="in" id="audq-mat">${mats.map(m=>`<option value="${esc(m.material_id)}">${esc(m.name||m.material_id)}</option>`).join('')}</select></div>
      <div class="field"><label>새 이름 <span class="req">*</span></label>
        <input class="in" id="audq-name" /></div>
    </div>
    <button class="btn btn-primary" onclick="App.audqRename('${esc(cid)}')">이름 교정</button>
  </fieldset>`;
}
VIEWS['aud-materials'] = (() => {
  const f = VIEWS['aud-materials'];
  return () => {
    const cid = S.selMat;
    if(!cid) return f ? f() : '';
    return (f ? f() : '') + `<section class="panel">
      <div class="panel-head"><h2>정량 기준·측정</h2></div>
      <h3>정량 기준표</h3>
      ${AUDQ_critTable()}
      <h3>측정 기록</h3>
      ${AUDQ_measTable(cid)}
      ${AUDQ_addForm(cid)}
      ${AUDQ_renameForm(cid)}
    </section>`;
  };
})();
App.audqAdd = async function(cid){
  const param_key = (($('#audq-param')||{}).value||'').trim();
  if(!param_key) return toast('항목을 선택하세요.');
  const v = (($('#audq-value')||{}).value||'').trim();
  if(v==='') return toast('측정값을 입력하세요.');
  const body = {param_key, value: parseFloat(v)};
  const mid = (($('#audq-mid')||{}).value||'').trim(); if(mid) body.material_id = mid;
  const unit = (($('#audq-unit')||{}).value||'').trim(); if(unit) body.unit = unit;
  const lab = (($('#audq-lab')||{}).value||'').trim(); if(lab) body.lab_name = lab;
  const tested = (($('#audq-tested')||{}).value||'').trim(); if(tested) body.tested_at = tested;
  const note = (($('#audq-note')||{}).value||'').trim(); if(note) body.note = note;
  try{
    const r = await apiFetch('/cases/'+cid+'/measurements', {method:'POST', body:JSON.stringify(body)});
    if(!r.ok){
      let msg = '측정값 추가에 실패했습니다.';
      if(r.status===403) msg = '권한이 없습니다.';
      else { try{ const d = await r.json(); if(d && d.detail && d.detail.code) msg += ' ('+d.detail.code+')'; }catch(e){} }
      return toast(msg);
    }
    delete RS.audqMeas[cid];
    await AUDQ_loadMeas(cid);
    render(); toast('측정값을 추가했습니다.');
  }catch(e){ toast('측정값 추가에 실패했습니다.'); }
};
App.audqDel = async function(mid){
  try{
    const r = await apiFetch('/measurements/'+mid, {method:'DELETE'});
    if(!r.ok){
      let msg = '삭제에 실패했습니다.';
      if(r.status===403) msg = '권한이 없습니다.';
      else { try{ const d = await r.json(); if(d && d.detail && d.detail.code) msg += ' ('+d.detail.code+')'; }catch(e){} }
      return toast(msg);
    }
    const cid = S.selMat;
    if(cid){ delete RS.audqMeas[cid]; await AUDQ_loadMeas(cid); }
    render(); toast('삭제했습니다.');
  }catch(e){ toast('삭제에 실패했습니다.'); }
};
App.audqRename = async function(cid){
  const mid = (($('#audq-mat')||{}).value||'').trim();
  const name = (($('#audq-name')||{}).value||'').trim();
  if(!mid) return toast('원재료를 선택하세요.');
  if(!name) return toast('새 이름을 입력하세요.');
  try{
    const r = await apiFetch('/materials/'+mid+'/rename', {method:'PATCH', body:JSON.stringify({name})});
    if(!r.ok){
      let msg = '이름 교정에 실패했습니다.';
      if(r.status===403) msg = '권한이 없습니다.';
      else { try{ const d = await r.json(); if(d && d.detail && d.detail.code) msg += ' ('+d.detail.code+')'; }catch(e){} }
      return toast(msg);
    }
    delete RS.mats[cid];
    render(); toast('원재료명을 교정했습니다.');
  }catch(e){ toast('이름 교정에 실패했습니다.'); }
};
RS.audqCrit = RS.audqCrit || null;
RS.audqMeas = RS.audqMeas || {};
