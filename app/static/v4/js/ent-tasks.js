/* ===== 기업 지금 할 일 (현장심사 일정·모의심사 증거·시정조치) ===== */
RS.entt = RS.entt || {};

async function ENTT_load(cid){
  try{
    const [s1, s2, s3, s4] = await Promise.all([
      apiFetch('/cases/'+cid+'/onsite-schedule').then(r=>r.ok?r.json():null).catch(()=>null),
      apiFetch('/cases/'+cid+'/mock-audit/detail').then(r=>r.ok?r.json():null).catch(()=>null),
      apiFetch('/cases/'+cid+'/findings').then(r=>r.ok?r.json():[]).catch(()=>[]),
      apiFetch('/cases/'+cid+'/corrective-actions').then(r=>r.ok?r.json():[]).catch(()=>[])
    ]);
    RS.entt[cid] = { sched:s1, mock:s2, findings:s3 || [], cars:s4 || [] };
  }catch(e){
    RS.entt[cid] = { sched:null, mock:null, findings:[], cars:[] };
  }
}

function ENTT_sevLabel(sev){
  if(sev === 'major') return '중대';
  if(sev === 'minor') return '경미';
  if(sev === 'observation') return '관찰';
  return esc(sev || '');
}

function ENTT_verdictBadge(v){
  if(v === 'comply') return '<span class="badge strong">적합</span>';
  if(v === 'nonconformity') return '<span class="badge attn">부적합</span>';
  return '<span class="muted">미판정</span>';
}

function ENTT_schedCard(cid, c, sched){
  if(!sched) return '';
  if(c.step8 !== 3 && c.step8 !== 4) return '';
  const st = sched.status;
  if(st === 'confirmed'){
    const cf = sched.confirmed || {};
    const when = [cf.date || '', cf.time || ''].filter(x=>x).join(' ');
    return `<fieldset class="fieldset"><legend>현장심사 가능일 제시</legend>
      <div class="note"><b>현장심사 일정 확정</b><p class="mono">${esc(when)}</p></div></fieldset>`;
  }
  if(st === 'reproposed' && (sched.auditor_dates || []).length){
    const rows = sched.auditor_dates.map((d, i)=>`<li class="row" style="gap:10px">
      <span class="mono" style="flex:1">${esc(d)}</span>
      <button class="btn btn-sm btn-primary" onclick="App.enttAccept(${i})">이 날짜로 확정</button></li>`).join('');
    return `<fieldset class="fieldset"><legend>현장심사 가능일 제시</legend>
      <p class="muted">오디터가 아래 후보일을 제안했습니다. 하나를 선택해 확정해 주세요.</p>
      <ul class="rows">${rows}</ul></fieldset>`;
  }
  const already = (sched.client_dates || []).length
    ? `<p class="muted">제시한 가능일: <span class="mono">${esc(sched.client_dates.join(', '))}</span></p>` : '';
  return `<fieldset class="fieldset"><legend>현장심사 가능일 제시</legend>
    <p class="muted">방문 가능한 날짜를 최대 3개까지 선택해 주세요.</p>
    ${already}
    <div class="grid-3">
      <div class="field"><label>가능일 1</label><input class="in" type="date" id="entt-d1"></div>
      <div class="field"><label>가능일 2</label><input class="in" type="date" id="entt-d2"></div>
      <div class="field"><label>가능일 3</label><input class="in" type="date" id="entt-d3"></div>
    </div>
    <div id="entt-sched-e" class="inline-msg"></div>
    <div class="inline" style="margin-top:10px">
      <button class="btn btn-primary" onclick="App.enttPropose()">가능일 제시</button>
    </div>
  </fieldset>`;
}

function ENTT_mockCard(cid, c, mock){
  if(!mock) return '';
  const show = c.step8 === 3 || (mock.evidence_total || 0) > 0;
  if(!show) return '';
  const secs = mock.sections || [];
  const rows = secs.map((s, i)=>`<tr>
    <td>${esc(s.label || s.section || '')}</td>
    <td class="num">${esc(String(s.evidence_count || 0))}</td>
    <td>${ENTT_verdictBadge(s.verdict)}</td>
    <td>
      <input type="file" id="entt-ev-${i}" accept="image/*,video/*,.pdf" hidden onchange="App.enttUpload(${i}, this.files[0])">
      <button class="btn btn-sm" onclick="document.getElementById('entt-ev-${i}').click()">파일 선택</button>
    </td></tr>`).join('');
  return `<fieldset class="fieldset"><legend>모의심사 증거 업로드</legend>
    <p class="muted">섹션별 현장 사진·영상을 올려 주세요. 오디터가 이 자료를 보고 모의심사 체크리스트를 판정합니다.</p>
    <div class="tbl-wrap"><table>
      <thead><tr><th>섹션</th><th class="num">제출 수</th><th>오디터 판정</th><th>올리기</th></tr></thead>
      <tbody>${rows || '<tr><td colspan="4" class="muted">섹션 정보가 없습니다.</td></tr>'}</tbody>
    </table></div></fieldset>`;
}

function ENTT_carCard(cid, findings, cars){
  const open = (findings || []).filter(f=>f.status !== 'closed');
  if(!open.length) return '';
  const rows = open.map(f=>{
    const idx = (findings || []).indexOf(f);
    const rel = (cars || []).filter(x=>x.finding_id === f.finding_id);
    const car = rel.length ? rel[0] : null;
    let status = '';
    let canSubmit = true;
    if(car){
      if(car.status === 'submitted'){ status = '<span class="badge attn">검토 대기</span>'; canSubmit = false; }
      else if(car.status === 'accepted' || car.status === 'closed'){ status = '<span class="badge strong">수용됨</span>'; canSubmit = false; }
      else if(car.status === 'rejected'){
        status = '<span class="badge attn">반려됨</span>' +
          (car.reviewer_note ? `<p class="muted" style="margin:4px 0 0">${esc(car.reviewer_note)}</p>` : '');
      }
    }
    const btn = canSubmit
      ? `<button class="btn btn-sm btn-primary" onclick="App.enttCar(${idx})">시정조치 제출</button>` : '';
    return `<li class="row" style="flex-direction:column;align-items:stretch;gap:6px">
      <b>${esc(f.finding || '')}</b>
      <span class="muted">${esc(f.area || '')} · ${ENTT_sevLabel(f.severity)}</span>
      ${status ? `<div>${status}</div>` : ''}
      ${btn ? `<div class="row-act">${btn}</div>` : ''}
    </li>`;
  }).join('');
  return `<fieldset class="fieldset"><legend>시정조치(CAR) 제출</legend>
    <p class="muted">지적사항에 대한 조치 내용을 제출해 주세요.</p>
    <ul class="rows">${rows}</ul></fieldset>`;
}

function ENTT_section(){
  const c = (RS.cases || [])[0];
  if(!c) return '';
  const cid = c.case_id;
  const cache = RS.entt[cid];
  if(!cache){
    if(RS._enttLoading !== cid){
      RS._enttLoading = cid;
      ENTT_load(cid).then(()=>{ RS._enttLoading = null; render(); });
    }
    return '';
  }
  const cards = [
    ENTT_schedCard(cid, c, cache.sched),
    ENTT_mockCard(cid, c, cache.mock),
    ENTT_carCard(cid, cache.findings || [], cache.cars || [])
  ].filter(x=>x).join('');
  if(!cards) return '';
  return `<section class="panel">
    <div class="panel-head"><h2>지금 할 일</h2><span class="muted">기업이 처리해야 다음 단계로 넘어갑니다</span></div>
    ${cards}
  </section>`;
}

App.enttPropose = async function(){
  const c = (RS.cases || [])[0]; if(!c) return;
  const cid = c.case_id;
  const dates = ['entt-d1','entt-d2','entt-d3']
    .map(id=>((($('#'+id)||{}).value||'').trim()))
    .filter(x=>x);
  const e = $('#entt-sched-e');
  if(!dates.length){ if(e) e.textContent = '가능한 날짜를 하나 이상 선택해 주세요.'; return; }
  if(e) e.textContent = '';
  try{
    const r = await apiFetch('/cases/'+cid+'/onsite-schedule/propose', {method:'POST', body:JSON.stringify({dates})});
    if(!r.ok){
      let code = '';
      try{ const d = await r.json(); code = (d && d.detail && d.detail.code) ? (' ('+d.detail.code+')') : ''; }catch(x){}
      toast('가능일 제시에 실패했습니다.'+code);
      return;
    }
    delete RS.entt[cid];
    await ENTT_load(cid);
    render();
    toast('가능일을 제시했습니다.');
  }catch(x){ toast('가능일 제시에 실패했습니다.'); }
};

App.enttAccept = async function(idx){
  const c = (RS.cases || [])[0]; if(!c) return;
  const cid = c.case_id;
  const cache = RS.entt[cid]; if(!cache || !cache.sched) return;
  const dates = cache.sched.auditor_dates || [];
  const date = dates[idx]; if(!date) return;
  try{
    const r = await apiFetch('/cases/'+cid+'/onsite-schedule/accept-proposed', {method:'POST', body:JSON.stringify({date})});
    if(!r.ok){
      let code = '';
      try{ const d = await r.json(); code = (d && d.detail && d.detail.code) ? (' ('+d.detail.code+')') : ''; }catch(x){}
      if(r.status === 403) toast('권한이 없습니다.');
      else toast('일정 확정에 실패했습니다.'+code);
      return;
    }
    delete RS.entt[cid];
    await ENTT_load(cid);
    render();
    toast('현장심사 일정을 확정했습니다.');
  }catch(x){ toast('일정 확정에 실패했습니다.'); }
};

App.enttUpload = function(idx, file){
  const c = (RS.cases || [])[0]; if(!c || !file) return;
  const cid = c.case_id;
  const cache = RS.entt[cid]; if(!cache || !cache.mock) return;
  const secs = cache.mock.sections || [];
  const sec = secs[idx]; if(!sec) return;
  const reader = new FileReader();
  reader.onload = async ()=>{
    const dataURL = reader.result;
    try{
      const r = await apiFetch('/cases/'+cid+'/documents', {method:'POST', body:JSON.stringify({
        filename: file.name, file_b64: dataURL, doc_type: 'mock_evidence_'+(sec.section||'')
      })});
      if(!r.ok){
        if(r.status === 413){ toast('파일이 너무 큽니다.'); return; }
        let code = '';
        try{ const d = await r.json(); code = (d && d.detail && d.detail.code) ? (' ('+d.detail.code+')') : ''; }catch(x){}
        if(r.status === 403) toast('권한이 없습니다.');
        else toast('증거 업로드에 실패했습니다.'+code);
        return;
      }
      delete RS.entt[cid];
      await ENTT_load(cid);
      render();
      toast('증거를 올렸습니다.');
    }catch(x){ toast('증거 업로드에 실패했습니다.'); }
  };
  reader.readAsDataURL(file);
};

App.enttCar = function(idx){
  const c = (RS.cases || [])[0]; if(!c) return;
  const cache = RS.entt[c.case_id]; if(!cache) return;
  const f = (cache.findings || [])[idx]; if(!f) return;
  const body = `<div class="fieldset"><legend>${esc(f.area||'')} · ${ENTT_sevLabel(f.severity)}</legend>
    <p>${esc(f.finding||'')}</p></div>
    <div class="field"><label>조치 내용 <span class="req">*</span></label><textarea class="in" id="entt-car-desc" rows="4"></textarea></div>
    <div class="field"><label>증빙 설명</label><input class="in" id="entt-car-ev" type="text"></div>
    <div class="field"><label>완료 예정일</label><input class="in" id="entt-car-due" type="date"></div>
    <div id="entt-car-e" class="inline-msg"></div>`;
  const foot = `<button class="btn" onclick="App.closeModal()">취소</button>
    <button class="btn btn-primary" onclick="App.enttCarDo(${idx})">제출</button>`;
  openModal('시정조치 제출', body, foot, false);
};

App.enttCarDo = async function(idx){
  const c = (RS.cases || [])[0]; if(!c) return;
  const cid = c.case_id;
  const cache = RS.entt[cid]; if(!cache) return;
  const f = (cache.findings || [])[idx]; if(!f) return;
  const desc = (($('#entt-car-desc')||{}).value||'').trim();
  const e = $('#entt-car-e');
  if(!desc){ if(e) e.textContent = '조치 내용을 입력해 주세요.'; return; }
  const ev = (($('#entt-car-ev')||{}).value||'').trim();
  const due = (($('#entt-car-due')||{}).value||'').trim();
  const payload = { description: desc };
  if(ev) payload.evidence = ev;
  if(due) payload.due_date = due;
  try{
    const r = await apiFetch('/findings/'+f.finding_id+'/car', {method:'POST', body:JSON.stringify(payload)});
    if(!r.ok){
      let code = '';
      try{ const d = await r.json(); code = (d && d.detail && d.detail.code) ? (' ('+d.detail.code+')') : ''; }catch(x){}
      if(r.status === 403){ if(e) e.textContent = '권한이 없습니다.'; else toast('권한이 없습니다.'); return; }
      if(e) e.textContent = '제출에 실패했습니다.'+code;
      else toast('시정조치 제출에 실패했습니다.'+code);
      return;
    }
    closeModal();
    delete RS.entt[cid];
    await ENTT_load(cid);
    if(typeof loadEnt === 'function') await loadEnt();
    render();
    toast('시정조치를 제출했습니다. 오디터 검토를 기다립니다.');
  }catch(x){
    if(e) e.textContent = '제출에 실패했습니다.';
    else toast('시정조치 제출에 실패했습니다.');
  }
};

{
  const f = VIEWS['ent-home'];
  VIEWS['ent-home'] = () => ENTT_section() + (f ? f() : '');
}
