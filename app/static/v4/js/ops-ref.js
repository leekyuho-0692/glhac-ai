/* ===== 운영도구 · 규정·기준 데이터 ===== */
(function(){

  if(typeof OPS_TABS === 'undefined') return;

  RS.opsf = RS.opsf || {};
  S.opsf = S.opsf || {};

  // ---------- 공통 ----------
  const OPSF_count = (n) => `<span class="count">${n}</span>`;

  const OPSF_err = (st, msg) => `<p class="empty">${esc(st===403?'이 기능은 권한이 없습니다(필요 역할).':(msg||'불러오지 못했습니다.'))}</p>`;

  const OPSF_load = async (key, path) => {
    try{
      const r = await apiFetch(path);
      if(r.ok){ RS.opsf[key] = await r.json(); }
      else { RS.opsf[key] = {error:true, status:r.status}; }
    }catch(e){ RS.opsf[key] = {error:true}; }
  };

  const OPSF_pick = (key, path, renderIfReady) => {
    if(!RS.opsf[key]){
      if(RS._opsfL !== key){ RS._opsfL = key; OPSF_load(key, path).then(()=>{ RS._opsfL=null; render(); }); }
      return '<p class="empty">불러오는 중…</p>';
    }
    return null;
  };

  // ---------- 1) 규정·법령 레지스트리 ----------
  const OPSF_STATE_KO = { draft:'초안', review:'검토', effective:'발효', retired:'폐지' };

  const OPSF_regRows = (list, meta) => {
    const catLabel = (k) => {
      const c = (meta.categories||[]).find(x=>x.key===k); return c ? c.label : k;
    };
    const rows = list.slice(0,100).map(r => {
      const st = r.state||'draft';
      const badge = st==='effective' ? 'badge strong' : (st==='draft' ? 'badge attn' : 'badge');
      return `<tr>
        <td class="mono">${esc(r.reg_number||r.reg_id||'')}</td>
        <td>${esc(r.title||'')}</td>
        <td class="muted">${esc(catLabel(r.category)||'-')}</td>
        <td><span class="${badge}">${esc(OPSF_STATE_KO[st]||st)}</span></td>
        <td class="mono">${esc(r.effective_date||'-')}</td>
        <td class="num mono">v${esc(String(r.version_count||1))}</td>
        <td class="row-act">
          <button class="btn btn-sm" onclick="App.opsfViewReg('${esc(r.reg_id)}')">보기</button>
          <button class="btn btn-sm" onclick="App.opsfEditReg('${esc(r.reg_id)}')">수정</button>
          <button class="btn btn-sm" onclick="App.opsfTransReg('${esc(r.reg_id)}')">상태 전환</button>
        </td>
      </tr>`;
    }).join('');
    const more = list.length>100 ? `<p class="muted" style="margin-top:8px">최근 100건 표시 (외 ${list.length-100}건)</p>` : '';
    return `<div class="tbl-wrap"><table>
      <thead><tr><th>법령번호</th><th>제목</th><th>구분</th><th>상태</th><th>시행일</th><th class="num">버전</th><th></th></tr></thead>
      <tbody>${rows}</tbody></table></div>${more}`;
  };

  const OPSF_regPanel = () => {
    let metaReady = RS.opsf.meta && !RS.opsf.meta.error;
    let listReady = RS.opsf.regs && !RS.opsf.regs.error;
    let loading = false;
    let waitList = null, waitMeta = null;
    if(!listReady){ waitList = OPSF_pick('regs','/regulations'); }
    if(!metaReady){ waitMeta = OPSF_pick('meta','/regulations/meta'); }

    const head = `<div class="panel-head"><h2>규정·법령 레지스트리</h2>
      <button class="btn btn-primary btn-sm" onclick="App.opsfNewReg()" ${metaReady?'':'disabled'}>규정 추가</button></div>`;

    if(!listReady){
      const e = RS.opsf.regs && RS.opsf.regs.error ? RS.opsf.regs : null;
      if(e) return `<section class="panel">${head}${OPSF_err(e.status,'규정 목록을 불러오지 못했습니다.')}</section>`;
      return `<section class="panel">${head}<p class="empty">불러오는 중…</p></section>`;
    }
    if(!metaReady){
      const e = RS.opsf.meta && RS.opsf.meta.error ? RS.opsf.meta : null;
      return `<section class="panel">${head}${e?OPSF_err(e.status,'분류 정보를 불러오지 못했습니다.'):'<p class="empty">불러오는 중…</p>'}</section>`;
    }

    const data = RS.opsf.regs;
    const list = (data.items||[]);
    const meta = RS.opsf.meta;
    if(!list.length) return `<section class="panel">${head}<p class="empty">등록된 법령이 없습니다.</p></section>`;
    return `<section class="panel">${head}${OPSF_regRows(list, meta)}</section>`;
  };

  // ---- 규정 폼 모달 ----
  const OPSF_regFormBody = (meta, cur) => {
    cur = cur || {};
    const selCat = (meta.categories||[]).map(c =>
      `<option value="${esc(c.key)}" ${c.key===cur.category?'selected':''}>${esc(c.label)}</option>`).join('');
    const stages = (meta.impact_stages||[]).map(s =>
      `<label class="chip"><input type="checkbox" data-opsf-stage value="${esc(s.key)}" ${(cur.impact_stages||[]).includes(s.key)?'checked':''}> ${esc(s.label)}</label>`).join('');
    const secs = (meta.impact_sections||[]).map(s =>
      `<label class="chip"><input type="checkbox" data-opsf-sec value="${esc(s.key)}" ${(cur.impact_sections||[]).includes(s.key)?'checked':''}> ${esc(s.label)}</label>`).join('');
    return `<div style="display:flex;flex-direction:column;gap:14px">
      <div class="field"><label>제목 <span class="req">*</span></label><input class="in" id="opsfTitle" value="${esc(cur.title||'')}"></div>
      <div class="grid-2">
        <div class="field"><label>법령번호</label><input class="in" id="opsfNum" value="${esc(cur.reg_number||'')}"></div>
        <div class="field"><label>시행일</label><input class="in" type="date" id="opsfEff" placeholder="YYYY-MM-DD" value="${esc(cur.effective_date||'')}"></div>
      </div>
      <div class="field"><label>분류</label><select class="in" id="opsfCat"><option value="">—</option>${selCat}</select></div>
      <div class="field"><label>본문요약</label><textarea class="in" id="opsfSummary" rows="3">${esc(cur.summary||'')}</textarea></div>
      <fieldset><legend>영향 심사단계</legend><div class="chipbar">${stages||'<span class="muted">항목 없음</span>'}</div></fieldset>
      <fieldset><legend>영향 증거섹션</legend><div class="chipbar">${secs||'<span class="muted">항목 없음</span>'}</div></fieldset>
    </div>`;
  };

  const OPSF_collect = () => {
    const stages = Array.from(document.querySelectorAll('[data-opsf-stage]')).filter(x=>x.checked).map(x=>x.value);
    const secs = Array.from(document.querySelectorAll('[data-opsf-sec]')).filter(x=>x.checked).map(x=>x.value);
    return {
      title: (($('#opsfTitle')||{}).value||'').trim(),
      reg_number: (($('#opsfNum')||{}).value||'').trim() || null,
      effective_date: (($('#opsfEff')||{}).value||'').trim() || null,
      category: (($('#opsfCat')||{}).value||'') || null,
      summary: (($('#opsfSummary')||{}).value||'').trim(),
      impact_stages: stages,
      impact_sections: secs
    };
  };

  App.opsfNewReg = function(){
    const meta = RS.opsf.meta || {};
    openModal('규정 추가', OPSF_regFormBody(meta, {}),
      `<button class="btn" onclick="App.closeModal()">취소</button>
       <button class="btn btn-primary" onclick="App.opsfCreateReg()">추가</button>`, true);
  };

  App.opsfCreateReg = async function(){
    const body = OPSF_collect();
    if(!body.title) return toast('제목을 입력하세요.');
    try{
      const r = await apiFetch('/regulations', {method:'POST', body:JSON.stringify(body)});
      if(!r.ok){ let d=null; try{ d=await r.json(); }catch(_){}
        if(r.status===422){ toast('입력값을 확인해 주세요(제목 필수, 시행일은 YYYY-MM-DD).' + (d&&d.detail&&d.detail.code?' ('+esc(d.detail.code)+')':'')); return; }
        toast('규정 추가에 실패했습니다.' + (d&&d.detail&&d.detail.code?' ('+esc(d.detail.code)+')':'')); return; }
      delete RS.opsf.regs;
      await OPSF_load('regs','/regulations');
      closeModal(); render(); toast('규정을 추가했습니다.');
    }catch(e){ toast('규정 추가에 실패했습니다.'); }
  };

  App.opsfViewReg = async function(regId){
    openModal('규정 상세', '<p class="empty">불러오는 중…</p>',
      `<button class="btn" onclick="App.closeModal()">닫기</button>`, true);
    try{
      const r = await apiFetch('/regulations/'+regId);
      let body;
      if(!r.ok){
        let d=null; try{ d=await r.json(); }catch(_){}
        body = `<p class="empty">${esc(r.status===403?'이 기능은 권한이 없습니다(필요 역할).':'규정을 불러오지 못했습니다.')}${d&&d.detail&&d.detail.code?' ('+esc(d.detail.code)+')':''}</p>`;
      } else {
        const d = await r.json();
        const c = d.content||{};
        const vers = (d.versions||[]).map(v=>`<li class="row"><div style="flex:1;min-width:0">
          <div><span class="badge">v${esc(String(v.version))}</span> <strong>${esc(v.title||'')}</strong></div>
          <div class="muted mono" style="font-size:11px">${esc(v.at||'')} · ${esc(v.actor||'')}</div></div></li>`).join('');
        body = `<div style="display:flex;flex-direction:column;gap:16px">
          <dl class="dl">
            <div><dt>법령번호</dt><dd class="mono">${esc(c.reg_number||'-')}</dd></div>
            <div><dt>상태</dt><dd>${esc(OPSF_STATE_KO[d.state]||d.state||'')}</dd></div>
            <div><dt>시행일</dt><dd class="mono">${esc(c.effective_date||'-')}</dd></div>
            <div><dt>분류</dt><dd>${esc(c.category||'-')}</dd></div>
            <div><dt>버전</dt><dd class="mono">v${esc(String(c.version||1))} / ${esc(String(c.version_count||1))}건</dd></div>
            <div><dt>수정자</dt><dd class="mono">${esc(c.updated_by||'-')}</dd></div>
            <div><dt>수정시각</dt><dd class="mono">${esc(c.updated_at||'-')}</dd></div>
          </dl>
          <div><h3 style="margin:0 0 6px">요약</h3><p class="note">${esc(c.summary||'-')}</p></div>
          <fieldset><legend>버전 이력 (${OPSF_count((d.versions||[]).length)})</legend>
            <ul class="rows">${vers||'<li class="row"><span class="muted">이력 없음</span></li>'}</ul></fieldset>
        </div>`;
      }
      openModal('규정 상세', body, `<button class="btn" onclick="App.closeModal()">닫기</button>`, true);
    }catch(e){ toast('규정을 불러오지 못했습니다.'); }
  };

  App.opsfEditReg = async function(regId){
    const meta = RS.opsf.meta || {};
    openModal('규정 수정', '<p class="empty">불러오는 중…</p>',
      `<button class="btn" onclick="App.closeModal()">닫기</button>`, true);
    try{
      const r = await apiFetch('/regulations/'+regId);
      if(!r.ok){ openModal('규정 수정', '<p class="empty">규정을 불러오지 못했습니다.</p>', `<button class="btn" onclick="App.closeModal()">닫기</button>`, true); return; }
      const d = await r.json(); const cur = d.content||{};
      openModal('규정 수정', OPSF_regFormBody(meta, cur),
        `<button class="btn" onclick="App.closeModal()">취소</button>
         <button class="btn btn-primary" onclick="App.opsfUpdateReg('${esc(regId)}')">저장</button>`, true);
    }catch(e){ toast('규정을 불러오지 못했습니다.'); }
  };

  App.opsfUpdateReg = async function(regId){
    const body = OPSF_collect();
    if(!body.title) return toast('제목을 입력하세요.');
    try{
      const r = await apiFetch('/regulations/'+regId, {method:'PUT', body:JSON.stringify(body)});
      if(!r.ok){ let dd=null; try{ dd=await r.json(); }catch(_){}
        if(r.status===422){ toast('입력값을 확인해 주세요(제목 필수, 시행일은 YYYY-MM-DD).' + (dd&&dd.detail&&dd.detail.code?' ('+esc(dd.detail.code)+')':'')); return; }
        toast('규정 수정에 실패했습니다.' + (dd&&dd.detail&&dd.detail.code?' ('+esc(dd.detail.code)+')':'')); return; }
      delete RS.opsf.regs;
      await OPSF_load('regs','/regulations');
      closeModal(); render(); toast('규정을 수정했습니다.');
    }catch(e){ toast('규정 수정에 실패했습니다.'); }
  };

  App.opsfTransReg = async function(regId){
    const meta = RS.opsf.meta || {};
    let curState = 'draft';
    const item = ((RS.opsf.regs&&RS.opsf.regs.items)||[]).find(x=>x.reg_id===regId);
    if(item) curState = item.state||'draft';
    const trans = meta.transitions || {};
    const opts = Object.keys(trans).filter(to => (trans[to]||[]).includes(curState));
    if(!opts.length){
      openModal('상태 전환', '<p class="empty">전환 가능한 상태가 없습니다.</p>', `<button class="btn" onclick="App.closeModal()">닫기</button>`);
      return;
    }
    const sel = opts.map(k=>`<option value="${esc(k)}">${esc(OPSF_STATE_KO[k]||k)}</option>`).join('');
    openModal('상태 전환', `<div style="display:flex;flex-direction:column;gap:12px">
      <p class="muted">현재 상태: ${esc(OPSF_STATE_KO[curState]||curState)}</p>
      <div class="field"><label>목표 상태</label><select class="in" id="opsfToState">${sel}</select></div>
      <div class="field"><label>사유 (발효/폐지 시 권장)</label><input class="in" id="opsfReason"></div>
      <p class="note attn">발효 시 운영·감사 역할로 알림이 전송됩니다.</p>
    </div>`,
      `<button class="btn" onclick="App.closeModal()">취소</button>
       <button class="btn btn-primary" onclick="App.opsfDoTrans('${esc(regId)}')">전환</button>`, true);
  };

  App.opsfDoTrans = async function(regId){
    const to_state = (($('#opsfToState')||{}).value||'').trim();
    const reason = (($('#opsfReason')||{}).value||'').trim();
    if(!to_state) return toast('목표 상태를 선택하세요.');
    try{
      const r = await apiFetch('/regulations/'+regId+'/transition', {method:'POST', body:JSON.stringify({to_state, reason})});
      if(!r.ok){ let d=null; try{ d=await r.json(); }catch(_){}
        toast('상태 전환에 실패했습니다.' + (d&&d.detail&&d.detail.code?' ('+esc(d.detail.code)+')':'')); return; }
      delete RS.opsf.regs;
      await OPSF_load('regs','/regulations');
      closeModal(); render(); toast('상태를 전환했습니다.');
    }catch(e){ toast('상태 전환에 실패했습니다.'); }
  };

  // ---------- 2) LPH 레퍼런스 ----------
  const OPSF_lphPanel = () => {
    if(!canCall('GET','/admin/lph-references')) return '';   // 조회 admin·consultant(main.py:11066) — operator 는 403
    const head = `<div class="panel-head"><h2>LPH 레퍼런스</h2>
      ${canCall('POST','/admin/lph-references')?`<button class="btn btn-primary btn-sm" onclick="App.opsfNewLph()">추가</button>`:''}</div>`;
    const loading = OPSF_pick('lph','/admin/lph-references');
    if(loading){
      const e = RS.opsf.lph && RS.opsf.lph.error ? RS.opsf.lph : null;
      return `<section class="panel">${head}${e?OPSF_err(e.status,'LPH 목록을 불러오지 못했습니다.'):loading}</section>`;
    }
    const data = RS.opsf.lph;
    const list = Array.isArray(data) ? data : (data.items||[]);
    if(!list.length) return `<section class="panel">${head}<p class="empty">등록된 LPH가 없습니다.</p></section>`;
    const rows = list.map(x=>`<tr>
      <td>${esc(x.name||'')}</td>
      <td class="mono">${esc(x.accreditation_no||'-')}</td>
      <td class="muted">${esc(x.region||'-')}</td>
    </tr>`).join('');
    return `<section class="panel">${head}<div class="tbl-wrap"><table>
      <thead><tr><th>이름</th><th>인정번호</th><th>지역</th></tr></thead><tbody>${rows}</tbody></table></div></section>`;
  };

  App.opsfNewLph = function(){
    openModal('LPH 추가', `<div style="display:flex;flex-direction:column;gap:12px">
      <div class="field"><label>이름 <span class="req">*</span></label><input class="in" id="opsfLphName"></div>
      <div class="field"><label>인정번호</label><input class="in" id="opsfLphNo"></div>
      <div class="field"><label>지역</label><input class="in" id="opsfLphRegion"></div>
    </div>`,
      `<button class="btn" onclick="App.closeModal()">취소</button>
       <button class="btn btn-primary" onclick="App.opsfCreateLph()">추가</button>`);
  };

  App.opsfCreateLph = async function(){
    const name = (($('#opsfLphName')||{}).value||'').trim();
    if(!name) return toast('이름을 입력하세요.');
    const body = {
      name,
      accreditation_no: (($('#opsfLphNo')||{}).value||'').trim() || null,
      region: (($('#opsfLphRegion')||{}).value||'').trim() || null,
      status: 'active'
    };
    try{
      const r = await apiFetch('/admin/lph-references', {method:'POST', body:JSON.stringify(body)});
      if(!r.ok){ let d=null; try{ d=await r.json(); }catch(_){}
        toast('LPH 추가에 실패했습니다.' + (d&&d.detail&&d.detail.code?' ('+esc(d.detail.code)+')':'')); return; }
      delete RS.opsf.lph;
      await OPSF_load('lph','/admin/lph-references');
      closeModal(); render(); toast('LPH를 추가했습니다.');
    }catch(e){ toast('LPH 추가에 실패했습니다.'); }
  };

  // ---------- 3) KMA 1360 면제 용어 ----------
  const OPSF_kmaPanel = () => {
    if(!canCall('GET','/admin/kma1360-exempt')) return '';   // admin 전용(main.py:1779)
    const head = `<div class="panel-head"><h2>KMA 1360 면제 용어</h2></div>`;
    const loading = OPSF_pick('kma','/admin/kma1360-exempt');
    if(loading){
      const e = RS.opsf.kma && RS.opsf.kma.error ? RS.opsf.kma : null;
      return `<section class="panel">${head}${e?OPSF_err(e.status,'면제 용어를 불러오지 못했습니다.'):loading}</section>`;
    }
    const terms = (RS.opsf.kma && RS.opsf.kma.terms) || [];
    if(!terms.length) return `<section class="panel">${head}<p class="empty">등록된 용어가 없습니다.</p></section>`;
    const chips = terms.map(t=>`<span class="chip">${esc(t)}</span>`).join('');
    return `<section class="panel">${head}<div class="chipbar">${chips}</div>
      <p class="muted" style="margin-top:8px">총 ${OPSF_count(terms.length)}개 용어 (읽기 전용)</p></section>`;
  };

  // ---------- 4) 원재료 온톨로지 ----------
  const OPSF_ontoPanel = () => {
    if(!canCall('GET','/admin/ontology/stats')) return '';   // 통계·재시드 admin 전용(main.py:1736·1744)
    const head = `<div class="panel-head"><h2>원재료 온톨로지</h2>
      <button class="btn btn-sm" onclick="App.opsfReseedConfirm()">재시드</button></div>`;
    const loading = OPSF_pick('onto','/admin/ontology/stats');
    if(loading){
      const e = RS.opsf.onto && RS.opsf.onto.error ? RS.opsf.onto : null;
      return `<section class="panel">${head}${e?OPSF_err(e.status,'온톨로지 통계를 불러오지 못했습니다.'):loading}</section>`;
    }
    const d = RS.opsf.onto || {};
    if(d.reseed_result){
      const rr = d.reseed_result;
      const summary = `<p class="note attn" style="margin-bottom:10px">재시드 완료 — 온톨로지 ${esc(String(rr.ontology_count))}건 · 재판정 ${esc(String(rr.rescreened))}건 · 변경 ${esc(String(rr.changed))}건</p>`;
      var extra = summary;
    }
    const rules = (d.rule_versions||[]).map(r=>`<li class="row"><div style="flex:1">
      <span class="badge">${esc(r.code||'')}</span> <span class="muted">${esc(r.jurisdiction||'')}</span>
      <span class="tag">${esc(r.status||'')}</span></div></li>`).join('');
    return `<section class="panel">${head}${extra||''}
      <div class="summary">
        <div><span class="k">온톨로지 항목</span><span class="v mono">${esc(String(d.ontology_count||0))}</span></div>
        <div><span class="k">규칙 버전</span><span class="v mono">${esc(String((d.rule_versions||[]).length))}</span></div>
      </div>
      <fieldset style="margin-top:12px"><legend>규칙 버전 (${OPSF_count((d.rule_versions||[]).length)})</legend>
        <ul class="rows">${rules||'<li class="row"><span class="muted">없음</span></li>'}</ul></fieldset>
    </section>`;
  };

  App.opsfReseedConfirm = function(){
    openModal('온톨로지 재시드', `<div style="display:flex;flex-direction:column;gap:12px">
      <p>온톨로지를 삭제 후 재시딩하고, <strong>기존 원재료를 전부 재판정</strong>합니다.</p>
      <p class="note attn">실행에 시간이 걸릴 수 있습니다. 실행 중에는 창을 닫지 마세요.</p>
      <div class="field"><label><input type="checkbox" id="opsfRescreen" checked> 재판정 실행 (rescreen=true)</label></div>
    </div>`,
      `<button class="btn" onclick="App.closeModal()">취소</button>
       <button class="btn btn-primary" onclick="App.opsfReseed()">실행</button>`);
  };

  App.opsfReseed = async function(){
    const rescreen = !!(($('#opsfRescreen')||{}).checked);
    toast('재시드 실행 중…');
    try{
      const r = await apiFetch('/admin/ontology/reseed?rescreen='+(rescreen?'true':'false'), {method:'POST'});
      if(!r.ok){ let d=null; try{ d=await r.json(); }catch(_){}
        toast('재시드에 실패했습니다.' + (d&&d.detail&&d.detail.code?' ('+esc(d.detail.code)+')':'')); return; }
      const out = await r.json();
      delete RS.opsf.onto;
      await OPSF_load('onto','/admin/ontology/stats');
      if(RS.opsf.onto && !RS.opsf.onto.error){ RS.opsf.onto.reseed_result = out; }
      closeModal(); render(); toast('재시드를 완료했습니다.');
    }catch(e){ toast('재시드에 실패했습니다.'); }
  };

  // ---------- 5) 물류 인증 연동 이벤트 ----------
  const OPSF_LS_KO = { pending:'대기', failed:'실패', processed:'완료' };

  const OPSF_syncPanel = () => {
    const loading = OPSF_pick('sync','/admin/logistics-sync');
    const head = `<div class="panel-head"><h2>물류 인증 연동 이벤트</h2>
      <button class="btn btn-sm" onclick="App.opsfReloadSync()">새로고침</button></div>`;
    if(loading){
      const e = RS.opsf.sync && RS.opsf.sync.error ? RS.opsf.sync : null;
      return `<section class="panel">${head}${e?OPSF_err(e.status,'연동 이벤트를 불러오지 못했습니다.'):loading}</section>`;
    }
    const d = RS.opsf.sync || {};
    const counts = d.counts || {};
    const items = (d.items||[]);
    const rows = items.slice(0,100).map(x=>{
      const st = x.status||'';
      const badge = st==='processed' ? 'badge strong' : (st==='failed' ? 'badge attn' : 'badge');
      const err = x.last_error ? `<div class="muted mono" style="font-size:11px">${esc(x.last_error)}</div>` : '';
      const canResend = (st==='pending' || st==='failed');
      return `<tr>
        <td class="mono">${esc(x.created_at||'')}</td>
        <td>${esc(x.event_type||'')}<div class="muted">${esc(x.company_name||'')}</div></td>
        <td><span class="${badge}">${esc(OPSF_LS_KO[st]||st)}</span>
          ${x.retry_count?`<div class="muted mono" style="font-size:11px">재시도 ${esc(String(x.retry_count))}회</div>`:''}</td>
        <td class="mono">${esc(x.certificate_no||'-')}${err}</td>
        <td class="row-act">${canResend?`<button class="btn btn-sm" onclick="App.opsfResend('${esc(x.id)}')">재전송</button>`:''}</td>
      </tr>`;
    }).join('');
    const more = items.length>100 ? `<p class="muted" style="margin-top:8px">최근 100건 표시 (외 ${items.length-100}건)</p>` : '';
    return `<section class="panel">${head}
      <div class="summary" style="margin-bottom:12px">
        <div><span class="k">대기</span><span class="v mono">${esc(String(counts.pending||0))}</span></div>
        <div><span class="k">실패</span><span class="v mono">${esc(String(counts.failed||0))}</span></div>
        <div><span class="k">완료</span><span class="v mono">${esc(String(counts.processed||0))}</span></div>
      </div>
      ${items.length? `<div class="tbl-wrap"><table>
        <thead><tr><th>시각</th><th>이벤트</th><th>상태</th><th>인증번호 / 오류</th><th></th></tr></thead>
        <tbody>${rows}</tbody></table></div>${more}` : '<p class="empty">연동 이벤트가 없습니다.</p>'}
    </section>`;
  };

  App.opsfReloadSync = async function(){
    delete RS.opsf.sync;
    await OPSF_load('sync','/admin/logistics-sync');
    render();
  };

  App.opsfResend = async function(eventId){
    try{
      const r = await apiFetch('/admin/logistics-sync/'+eventId+'/resend', {method:'POST'});
      if(!r.ok){ let d=null; try{ d=await r.json(); }catch(_){}
        toast('재전송에 실패했습니다.' + (d&&d.detail&&d.detail.code?' ('+esc(d.detail.code)+')':'')); return; }
      const out = await r.json();
      delete RS.opsf.sync;
      await OPSF_load('sync','/admin/logistics-sync');
      render();
      toast(out.status==='processed' ? '재전송 완료했습니다.' : '재전송 실패했습니다.');
    }catch(e){ toast('재전송에 실패했습니다.'); }
  };

  // ---------- 탭 등록 ----------
  const OPSF_render = () => {
    return [OPSF_regPanel(), OPSF_lphPanel(), OPSF_kmaPanel(), OPSF_ontoPanel(), OPSF_syncPanel()]
      .map(html => `<section class="panel" style="margin-bottom:0">${html.replace(/^<section class="panel">|<\/section>$/g,'')}</section>`)
      .join('');
  };

  OPS_TABS.push({ key:'ref', label:'규정·기준 데이터', render: OPSF_render });

})();
