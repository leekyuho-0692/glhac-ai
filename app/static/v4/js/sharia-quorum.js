/* ===== 샤리아 심의 · 정족수/의결 ===== */
(function(){
  RS.shq = RS.shq || {};
  RS.shqDecree = RS.shqDecree || {};

  const SHQ_RESULT_KO = { pending:'대기', passed:'적합', conditional:'조건부 적합', rejected:'부적합' };
  const SHQ_VOTE_ICON = { approve:'✔ 적합', conditional:'△ 조건부', reject:'✖ 부적합' };

  const SHQ_seatKey = (m) => m.role === 'chair' ? 'chairman' : (m.role + ':' + m.name);
  const SHQ_seatLabel = (m) => m.role === 'chair' ? ('위원장 · ' + m.name) : ('위원 · ' + m.name);

  RS._shqLoading = RS._shqLoading || {};
  async function SHQ_load(cid){
    try{
      const [rc, rv, rsg] = await Promise.all([
        apiFetch('/fatwa/committee'),
        apiFetch('/cases/' + cid + '/fatwa/votes'),
        apiFetch('/cases/' + cid + '/fatwa/sign')
      ]);
      const committee = rc.ok ? (await rc.json()) : { error:true, status:rc.status };
      let tally = null;
      if(rv.ok) tally = await rv.json();
      else if(rv.status !== 403) tally = { error:true, status:rv.status };
      const signs = rsg.ok ? (await rsg.json()) : {};
      RS.shq[cid] = { committee, tally, signs };
    }catch(e){ RS.shq[cid] = { error:true }; }
  }

  function SHQ_ensure(cid){
    if(!RS.shq[cid] && !RS._shqLoading[cid]){
      RS._shqLoading[cid] = true;
      SHQ_load(cid).then(()=>{ RS._shqLoading[cid] = false; render(); });
    }
  }

  function SHQ_panel(cid){
    if(!cid) return '';
    SHQ_ensure(cid);
    const d = RS.shq[cid];
    if(!d) return '<section class="panel"><p class="empty">불러오는 중…</p></section>';
    if(d.error) return '<section class="panel"><p class="empty">샤리아 심의 정보를 불러오지 못했습니다.</p></section>';
    const t = d.tally;
    if(!t || !t.member_keys) return '';
    const members = (d.committee && d.committee.members) || [];
    const seatMap = {};
    members.forEach(m => { seatMap[SHQ_seatKey(m)] = m; });
    const ballotMap = {};
    (t.ballots || []).forEach(b => { ballotMap[b.member] = b; });
    const signMap = d.signs || {};

    const defSeat = (S.shqSeat && t.member_keys.indexOf(S.shqSeat) >= 0) ? S.shqSeat : (t.member_keys.indexOf(AUTH.username) >= 0 ? AUTH.username : t.member_keys[0]);

    const rows = t.member_keys.map(k => {
      const b = ballotMap[k];
      const m = seatMap[k];
      const seatName = m ? SHQ_seatLabel(m) : k;
      const voteTxt = b ? (SHQ_VOTE_ICON[b.vote] || esc(b.vote)) : '— 미제출';
      const note = b && b.note ? `<div class="muted" style="font-size:11px">${esc(b.note)}</div>` : '';
      const sig = signMap[k] ? '✔' : '—';
      return `<tr><td>${esc(seatName)}${note}</td><td>${voteTxt}</td><td class="num">${sig}</td></tr>`;
    }).join('');

    const prog = (t.progress || '0/3');
    const canConfirm = !!t.quorum_met && !!t.signatures_ok && t.result === 'pending';
    const decided = t.result && t.result !== 'pending';
    const decree = RS.shqDecree[cid];

    let reason = '';
    if(!canConfirm && !decided){
      const need = (t.quorum_need != null ? t.quorum_need : 3);
      reason = `의견 ${t.votes_cast || 0}/${need}·서명 ${t.signatures || 0}/${need}`;
    }

    const seatOpts = t.member_keys.map(k => {
      const m = seatMap[k];
      return `<option value="${esc(k)}" ${k === defSeat ? 'selected' : ''}>${esc(m ? SHQ_seatLabel(m) : k)}</option>`;
    }).join('');

    return `<section class="panel">
      <div class="panel-head"><h2>샤리아 심의 · 정족수 3인 (재심의 ${t.round || 1}차)</h2><span class="badge ${t.quorum_met ? 'strong' : 'attn'}">진행 ${esc(prog)}</span></div>
      <div class="tbl-wrap"><table>
        <thead><tr><th>좌석</th><th>의견</th><th class="num">서명</th></tr></thead>
        <tbody>${rows}</tbody>
      </table></div>
      <div class="fieldset"><legend>내 의견 제출</legend>
        <div class="field"><label>좌석</label><select id="shq-seat" class="in">${seatOpts}</select></div>
        <div class="field"><label>사유 (조건부·부적합 시 필수)</label><textarea id="shq-note" class="in" rows="2"></textarea><div class="inline-msg" id="shq-msg"></div></div>
        <div class="row-act">
          <button class="btn btn-sm btn-primary" onclick="App.shqVote('${cid}','approve')">✔ 적합</button>
          <button class="btn btn-sm" onclick="App.shqVote('${cid}','conditional')">△ 조건부</button>
          <button class="btn btn-sm" onclick="App.shqVote('${cid}','reject')">✖ 부적합</button>
        </div>
        <p class="muted" style="font-size:11px">서명은 아래 서명 패널에서 진행하세요.</p>
      </div>
      <div class="summary">
        <div><span class="k">결과</span><span class="v">${esc(SHQ_RESULT_KO[t.result] || '대기')}</span></div>
        <div><span class="k">서명</span><span class="v">${t.signatures_ok ? '✔ 완료' : (t.signatures || 0) + '/3'}</span></div>
      </div>
      <div class="row-act">
        <button class="btn btn-primary" ${canConfirm ? '' : 'disabled'} onclick="App.shqConfirm('${cid}')">위원장 확정 · D-19 발행</button>
        ${reason ? `<span class="inline-msg">${esc(reason)}</span>` : ''}
        ${decided && decree ? `<button class="btn btn-ghost" onclick="App.genPdf('${decree}','D-19_의결서.pdf')">D-19 PDF</button>` : ''}
      </div>
    </section>`;
  }

  const SHQ_origReview = VIEWS['sha-review'];
  VIEWS['sha-review'] = () => {
    const extra = SHQ_panel(S.selFatwa);
    const body = SHQ_origReview ? SHQ_origReview() : '';
    return extra + body;
  };

  App.shqVote = async function(cid, vote){
    const seatEl = $('#shq-seat');
    const member = seatEl ? seatEl.value : AUTH.username;
    const note = (($('#shq-note') || {}).value || '').trim();
    const msgEl = $('#shq-msg');
    if((vote === 'conditional' || vote === 'reject') && !note){ if(msgEl) msgEl.textContent = '사유를 입력해 주세요.'; return; }
    if(msgEl) msgEl.textContent = '';
    try{
      const r = await apiFetch('/cases/' + cid + '/fatwa/vote', { method:'POST', body: JSON.stringify({ member, vote, note }) });
      if(!r.ok){
        let code = '';
        try{ const j = await r.json(); code = (j && j.detail && j.detail.code) || ''; }catch(e){}
        if(r.status === 422){
          if(code === 'NOT_COMMITTEE_MEMBER') toast('위원회 구성원이 아닙니다.');
          else if(code === 'OPINION_REQUIRED') toast('의견을 선택해 주세요.');
          else if(code === 'REASON_REQUIRED') toast('사유를 입력해 주세요.');
          else toast('의견 제출에 실패했습니다.' + (code ? ' (' + code + ')' : ''));
        } else if(r.status === 403) toast('권한이 없습니다.');
        else toast('의견 제출에 실패했습니다.' + (code ? ' (' + code + ')' : ''));
        return;
      }
      const j = await r.json().catch(()=>({}));
      delete RS.shq[cid];
      delete RS.fatwa[cid];
      S.shqSeat = member;
      SHQ_ensure(cid);
      render();
      toast(`의견을 제출했습니다 (${(j.progress || '')})`);
    }catch(e){ toast('의견 제출에 실패했습니다.'); }
  };

  App.shqConfirm = async function(cid){
    try{
      const r = await apiFetch('/cases/' + cid + '/fatwa/confirm', { method:'POST', body: JSON.stringify({}) });
      if(!r.ok){
        let code = '';
        try{ const j = await r.json(); code = (j && j.detail && j.detail.code) || ''; }catch(e){}
        if(r.status === 409) toast('의견/서명이 아직 모이지 않았습니다.' + (code ? ' (' + code + ')' : ''));
        else if(r.status === 403) toast('위원장만 확정할 수 있습니다.');
        else toast('확정에 실패했습니다.' + (code ? ' (' + code + ')' : ''));
        return;
      }
      const j = await r.json();
      RS.shqDecree[cid] = j.gen_doc_id;
      delete RS.shq[cid];
      delete RS.fatwa[cid];
      try{ await loadSha(); }catch(e){}
      render();
      toast('결과를 확정했습니다 — 의결서 ' + (j.decision_no || ''));
    }catch(e){ toast('확정에 실패했습니다.'); }
  };

  const SHQ_origVote = App.shaVote;
  App.shaVote = function(cid, vote){
    const d = RS.shq[cid];
    if(d && d.tally && d.tally.member_keys){
      return App.shqVote(cid, vote === 'abstain' ? 'conditional' : vote);
    }
    if(typeof SHQ_origVote === 'function') return SHQ_origVote.apply(this, arguments);
  };

  async function SHQ_loadAgenda(){
    try{ const r = await apiFetch('/sharia/agenda'); RS.shqAgenda = r.ok ? (await r.json()) : { error:true, status:r.status }; }
    catch(e){ RS.shqAgenda = { error:true }; }
  }
  async function SHQ_loadHist(){
    try{ const r = await apiFetch('/sharia/history'); RS.shqHist = r.ok ? (await r.json()) : { error:true, status:r.status }; }
    catch(e){ RS.shqHist = { error:true }; }
  }

  RS._shqAgLoading = false;
  function SHQ_agendaPanel(){
    if(RS.shqAgenda === undefined){
      if(!RS._shqAgLoading){ RS._shqAgLoading = true; SHQ_loadAgenda().then(()=>{ RS._shqAgLoading = false; render(); }); }
      return '';
    }
    if(RS.shqAgenda && RS.shqAgenda.error) return '';
    const items = (RS.shqAgenda && RS.shqAgenda.items) || [];
    if(!items.length) return '<section class="panel"><div class="panel-head"><h2>샤리아 안건</h2></div><p class="empty">심의 대기 안건이 없습니다.</p></section>';
    return `<section class="panel"><div class="panel-head"><h2>샤리아 안건</h2></div><div class="tbl-wrap"><table>
      <thead><tr><th>업체</th><th>차수</th><th>진행</th><th>결과</th><th></th></tr></thead>
      <tbody>${items.map(it => `<tr><td>${esc(it.company_name)}</td><td>${esc(String(it.round || 1))}차</td><td>${esc(it.progress || '')}</td><td>${esc(SHQ_RESULT_KO[it.result] || '대기')}</td><td class="row-act"><button class="btn btn-sm" onclick="App.shaOpen('${it.case_id}')">열기</button></td></tr>`).join('')}</tbody>
    </table></div></section>`;
  }

  const SHQ_origHome = VIEWS['sha-home'];
  VIEWS['sha-home'] = () => {
    const body = SHQ_origHome ? SHQ_origHome() : '';
    return SHQ_agendaPanel() + body;
  };

  RS._shqHistLoading = false;
  function SHQ_histPanel(){
    if(RS.shqHist === undefined){
      if(!RS._shqHistLoading){ RS._shqHistLoading = true; SHQ_loadHist().then(()=>{ RS._shqHistLoading = false; render(); }); }
      return '';
    }
    if(RS.shqHist && RS.shqHist.error) return '';
    const items = (RS.shqHist && RS.shqHist.items) || [];
    if(!items.length) return '';
    return `<section class="panel"><div class="panel-head"><h2>심의 이력</h2></div><div class="tbl-wrap"><table>
      <thead><tr><th>의결서</th><th>업체</th><th>결과</th><th>일자</th></tr></thead>
      <tbody>${items.map(it => `<tr><td class="mono">${esc(it.decision_no || '')}</td><td>${esc(it.company_name)}</td><td>${esc(SHQ_RESULT_KO[it.decision] || it.decision || '')}</td><td class="mono">${esc((it.decided_at || '').slice(0,10))}</td></tr>`).join('')}</tbody>
    </table></div></section>`;
  }

  const SHQ_origHistory = VIEWS['sha-history'];
  VIEWS['sha-history'] = () => {
    const body = SHQ_origHistory ? SHQ_origHistory() : '';
    return SHQ_histPanel() + body;
  };

  const SHQ_origGo = App.go;
  App.go = function(viewId){
    RS.shqAgenda = undefined;
    RS.shqHist = undefined;
    if(typeof SHQ_origGo === 'function') return SHQ_origGo.apply(this, arguments);
  };
})();
