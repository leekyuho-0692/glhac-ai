/* ===== 관리자 운영도구: 입금 매칭 탭 ===== */
(function(){
  const P = 'OPSP_';
  const Q = (s)=>($(s)||{}).value ? ($(s).value||'').trim() : '';

  const tabKey = 'pay';
  const ADMIN_ROLES = ['fatwa_liaison','operator'];

  const ok403 = (st)=>st===403;

  async function OPSP_fetch(path, opts){
    try{
      const r = await apiFetch(path, opts);
      if(!r.ok){
        let code = '';
        try{ const j = await r.json(); code = (j && j.detail && j.detail.code) || (j && j.code) || ''; }catch(e){}
        return {ok:false, status:r.status, code};
      }
      const d = await r.json();
      return {ok:true, data:d};
    }catch(e){ return {ok:false, status:0}; }
  }

  function OPSP_errBox(res, label){
    if(ok403(res.status)) return `<section class="panel"><p class="empty">${esc(label)} 기능은 권한이 없습니다(필요 역할: ${ADMIN_ROLES.map(x=>ROLE_LABEL[x]||x).join(', ')}).</p></section>`;
    return `<section class="panel"><p class="empty">${esc(label)} 정보를 불러오지 못했습니다.</p></section>`;
  }

  // ---------- 요약 ----------
  function OPSP_loadSum(){
    RS._opspSum = {loading:true};
    OPSP_fetch('/admin/payments/dashboard').then(res=>{
      RS._opspSum = res.ok ? res.data : {error:true, status:res.status};
      render();
    });
  }
  function OPSP_sum(){
    const d = RS._opspSum;
    if(!d){ if(!RS._opspSumLoading){ RS._opspSumLoading=true; OPSP_loadSum(); } return '<section class="panel"><p class="empty">불러오는 중…</p></section>'; }
    if(d.error) return OPSP_errBox(d, '결제 현황');
    const by = d.by_status || {};
    const chips = Object.keys(by).map(k=>`<span class="tag">${esc(k)} <span class="mono">${by[k]}</span></span>`).join(' ');
    return `<section class="panel">
      <div class="panel-head"><h2>결제 현황 요약</h2></div>
      <div class="summary">
        <div><span class="k">정산 완료액</span><span class="v mono">${esc(_money(d.settlement))}</span></div>
        <div><span class="k">입금 대기</span><span class="v mono">${d.waiting}</span></div>
        <div><span class="k">검증 필요</span><span class="v mono">${d.need_verification}</span></div>
        <div><span class="k">환불</span><span class="v mono">${d.refunded}</span></div>
        <div><span class="k">전체 인보이스</span><span class="v mono">${d.total_invoices}</span></div>
      </div>
      <div class="chipbar" style="margin-top:12px">${chips||'<span class="muted">상태 없음</span>'}</div>
    </section>`;
  }

  // ---------- 입금 목록 + 등록 ----------
  function OPSP_loadDep(){
    RS._opspDep = {loading:true};
    OPSP_fetch('/admin/deposits').then(res=>{
      RS._opspDep = res.ok ? res.data : {error:true, status:res.status};
      render();
    });
  }
  function OPSP_dep(){
    const d = RS._opspDep;
    if(!d){ if(!RS._opspDepLoading){ RS._opspDepLoading=true; OPSP_loadDep(); } return '<section class="panel"><p class="empty">불러오는 중…</p></section>'; }
    if(d.error) return OPSP_errBox(d, '입금 내역');
    const items = d.items || [];
    const shown = items.slice(0,100);
    const more = items.length - shown.length;
    const rows = shown.map(it=>`<tr>
      <td class="mono" style="font-size:11px">${esc((it.id||'').slice(0,8))}</td>
      <td>${esc(it.bank_name||'')}</td>
      <td class="mono">${esc(it.account||'')}</td>
      <td>${esc(it.depositor_name||'')}</td>
      <td class="num mono">${esc(_money(it.amount))}</td>
      <td>${esc(it.ref_memo||'')}</td>
      <td><span class="badge ${it.match_status==='matched'?'strong':'attn'}">${esc(it.match_status||'')}</span></td>
      <td class="mono" style="font-size:11px">${esc(it.deposit_at||'')}</td>
    </tr>`).join('');
    const form = `<div class="fieldset" style="margin-top:14px"><legend>입금 등록</legend>
      <div class="grid-3">
        <div class="field"><label>은행</label><input class="in" id="opspBank"></div>
        <div class="field"><label>계좌번호</label><input class="in" id="opspAcct"></div>
        <div class="field"><label>입금자명</label><input class="in" id="opspDepositor"></div>
        <div class="field"><label>금액 <span class="req">*</span></label><input class="in" id="opspAmount" type="number" min="0" step="0.01"></div>
        <div class="field" style="grid-column:span 2"><label>메모</label><input class="in" id="opspMemo"></div>
      </div>
      <div class="inline" style="margin-top:10px"><button class="btn btn-primary" onclick="App.opspAddDeposit()">등록</button></div>
    </div>`;
    return `<section class="panel">
      <div class="panel-head"><h2>입금 내역</h2><span class="count muted">${items.length}건</span></div>
      ${items.length ? `<div class="tbl-wrap"><table>
        <thead><tr><th>ID</th><th>은행</th><th>계좌</th><th>입금자</th><th class="num">금액</th><th>메모</th><th>매칭</th><th>시각</th></tr></thead>
        <tbody>${rows}</tbody></table></div>${more>0?`<p class="muted" style="font-size:12px;margin-top:6px">외 ${more}건</p>`:''}` : '<p class="empty">입금 내역이 없습니다.</p>'}
      ${form}
    </section>`;
  }
  App.opspAddDeposit = async function(){
    const amount = parseFloat(Q('#opspAmount'));
    if(!(amount>=0)){ toast('금액을 입력하세요.'); return; }
    const body = {
      bank_name: Q('#opspBank')||null,
      account_no: Q('#opspAcct')||null,
      depositor_name: Q('#opspDepositor')||null,
      amount: amount,
      ref_memo: Q('#opspMemo')||null
    };
    try{
      const r = await apiFetch('/admin/deposits',{method:'POST',body:JSON.stringify(body)});
      if(!r.ok){
        let detail=''; try{ const j=await r.json(); detail=(j&&j.detail&&j.detail.code)||''; }catch(e){}
        toast('입금 등록에 실패했습니다.'+(detail?` (${detail})`:''));
        return;
      }
      toast('입금을 등록했습니다.');
      RS._opspDep=null; RS._opspCand=null; RS._opspSum=null;
      OPSP_loadDep(); OPSP_loadCand(); OPSP_loadSum();
      render();
    }catch(e){ toast('입금 등록에 실패했습니다.'); }
  };

  // ---------- 매칭 후보 ----------
  function OPSP_loadCand(){
    RS._opspCand = {loading:true};
    OPSP_fetch('/admin/payments/match-candidates').then(res=>{
      RS._opspCand = res.ok ? res.data : {error:true, status:res.status};
      render();
    });
  }
  function OPSP_cand(){
    const d = RS._opspCand;
    if(!d){ if(!RS._opspCandLoading){ RS._opspCandLoading=true; OPSP_loadCand(); } return '<section class="panel"><p class="empty">불러오는 중…</p></section>'; }
    if(d.error) return OPSP_errBox(d, '매칭 후보');
    const items = d.items || [];
    const shown = items.slice(0,100);
    const more = items.length - shown.length;
    const rows = shown.map(it=>`<tr>
      <td class="mono">${(it.score||0).toFixed ? it.score.toFixed(2) : esc(it.score)}</td>
      <td>${esc(it.depositor||'')} <span class="muted mono" style="font-size:11px">${esc(it.ref_memo||'')}</span></td>
      <td class="num mono">${esc(_money(it.deposit_amount))}</td>
      <td>${esc(it.company||'')}</td>
      <td class="mono">${esc(it.invoice_no||'')}</td>
      <td class="num mono">${esc(_money(it.invoice_total))}</td>
      <td>${esc((it.reason||[]).join(', '))}${it.risk_flags&&it.risk_flags.length?`<span class="tag attn">${esc(it.risk_flags.join(', '))}</span>`:''}</td>
      <td><div class="row-act">
        <button class="btn btn-sm btn-primary" onclick="App.opspDecide('${it.id}','approved')">승인</button>
        <button class="btn btn-sm btn-ghost" onclick="App.opspDecide('${it.id}','rejected')">거절</button>
      </div></td>
    </tr>`).join('');
    return `<section class="panel">
      <div class="panel-head"><h2>매칭 후보</h2><span class="count muted">${items.length}건</span></div>
      ${items.length ? `<div class="tbl-wrap"><table>
        <thead><tr><th>점수</th><th>입금자</th><th class="num">입금액</th><th>업체</th><th>인보이스</th><th class="num">청구액</th><th>사유/위험</th><th></th></tr></thead>
        <tbody>${rows}</tbody></table></div>${more>0?`<p class="muted" style="font-size:12px;margin-top:6px">외 ${more}건</p>`:''}` : '<p class="empty">대기 중인 매칭 후보가 없습니다.</p>'}
    </section>`;
  }
  App.opspDecide = async function(candidateId, decision){
    try{
      const r = await apiFetch('/admin/match/'+encodeURIComponent(candidateId)+'/decide',
        {method:'POST', body:JSON.stringify({decision})});
      if(!r.ok){
        let detail=''; try{ const j=await r.json(); detail=(j&&j.detail&&j.detail.code)||''; }catch(e){}
        toast('처리에 실패했습니다.'+(detail?` (${detail})`:''));
        return;
      }
      toast(decision==='approved'?'승인했습니다.':'거절했습니다.');
      RS._opspCand=null; RS._opspDep=null; RS._opspSum=null;
      OPSP_loadCand(); OPSP_loadDep(); OPSP_loadSum();
      render();
    }catch(e){ toast('처리에 실패했습니다.'); }
  };

  function OPSP_render(){
    return OPSP_sum() + OPSP_dep() + OPSP_cand();
  }

  if (typeof OPS_TABS !== 'undefined') {
    OPS_TABS.push({key: tabKey, label: '입금 매칭', render: OPSP_render});
  }
})();
