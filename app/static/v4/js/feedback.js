/* ===== 개선 피드백 ===== */
(() => {
  const FDB_CATS = [['improvement','개선 제안'],['bug','오류 신고'],['question','질문'],['other','기타']];
  const FDB_ST = {open:'접수',reviewing:'검토 중',resolved:'해결',wontfix:'보류'};
  const FDB_ST_ORDER = ['open','reviewing','resolved','wontfix'];

  const FDB_isAdmin = () => AUTH && (AUTH.role === 'admin' || AUTH.role === 'operator');

  const FDB_catLabel = (k) => {
    const hit = FDB_CATS.find(x => x[0] === k);
    return hit ? hit[1] : (k || '');
  };

  const FDB_stBadge = (s) => {
    const label = FDB_ST[s] || s || '';
    const cls = s === 'resolved' ? 'badge strong' : (s === 'open' ? 'badge attn' : 'badge');
    return `<span class="${cls}">${esc(label)}</span>`;
  };

  const FDB_fmt = (v) => {
    if (!v && v !== 0) return '';
    const s = String(v).replace('T',' ');
    return s.length > 16 ? s.slice(0,16) : s;
  };

  async function FDB_loadList(){
    try{
      const r = await apiFetch('/feedback');
      RS.fdbList = r.ok ? (await r.json()) : {items:[], error:true, status:r.status};
    }catch(e){ RS.fdbList = {items:[], error:true}; }
    RS._fdbListLoading = false;
  }

  async function FDB_loadDetail(fid){
    try{
      const r = await apiFetch('/feedback/' + fid);
      RS.fdbDetail[fid] = r.ok ? (await r.json()) : {error:true, status:r.status};
    }catch(e){ RS.fdbDetail[fid] = {error:true}; }
  }

  RS.fdbDetail = RS.fdbDetail || {};
  S.fdbShow = S.fdbShow || null;
  S.fdbTab  = S.fdbTab  || 'new';

  function FDB_form(){
    const catOpts = FDB_CATS.map(([k,l]) =>
      `<option value="${k}">${esc(l)}</option>`).join('');
    const ctx = S.view ? `<input type="hidden" id="fdb-view" value="${esc(S.view)}">` : '';
    return `
    <div class="fieldset">
      <legend>피드백 작성</legend>
      ${ctx}
      <div class="field">
        <label>제목 <span class="req">*</span></label>
        <input class="in" id="fdb-title" maxlength="120" placeholder="한 줄로 요약해 주세요.">
      </div>
      <div class="grid-2">
        <div class="field">
          <label>분류</label>
          <select class="in" id="fdb-category">${catOpts}</select>
        </div>
        <div class="field">
          <label>현재 화면</label>
          <input class="in" value="${esc(S.view || '')}" disabled>
        </div>
      </div>
      <div class="field">
        <label>내용</label>
        <textarea class="in" id="fdb-body" rows="5" placeholder="어떤 점을 개선하면 좋을지 적어 주세요."></textarea>
      </div>
      <div class="inline">
        <button class="btn btn-primary" onclick="App.fdbSend()">보내기</button>
      </div>
      <div class="inline-msg" id="fdb-form-msg"></div>
    </div>`;
  }

  function FDB_list(){
    const box = RS.fdbList;
    if(!box){
      if(!RS._fdbListLoading){ RS._fdbListLoading = true; FDB_loadList().then(()=>render()); }
      return `<div class="fieldset"><legend>내가 보낸 피드백</legend><p class="empty">불러오는 중…</p></div>`;
    }
    if(box.error){
      return `<div class="fieldset"><legend>내가 보낸 피드백</legend><p class="empty">${box.status === 403 ? '이 목록을 볼 권한이 없습니다.' : '목록을 불러오지 못했습니다.'}</p></div>`;
    }
    const items = box.items || [];
    if(!items.length){
      return `<div class="fieldset"><legend>${box.is_admin ? '전체 피드백' : '내가 보낸 피드백'}</legend><p class="empty">아직 등록된 피드백이 없습니다.</p></div>`;
    }
    const rows = items.map((it, i) => {
      const on = S.fdbShow === it.feedback_id;
      const cmt = it.comment_count ? ` <span class="tag">댓글 ${it.comment_count}</span>` : '';
      return `<li class="row">
        <div style="flex:1;min-width:0">
          <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">
            <button class="link" style="font-weight:600;text-align:left" onclick="App.fdbShow(${i})">${esc(it.title || '(제목 없음)')}</button>
            ${FDB_stBadge(it.status)}${cmt}
          </div>
          <div class="muted" style="font-size:12px;margin-top:2px">
            ${esc(it.author || '')} · ${esc(FDB_catLabel(it.category))} · ${esc(FDB_fmt(it.created_at))}
          </div>
        </div>
        <div class="row-act">
          <button class="btn btn-sm btn-ghost" onclick="App.fdbShow(${i})">${on ? '접기' : '열기'}</button>
        </div>
      </li>${on ? `<li class="row" style="display:block">${FDB_detailHost(it.feedback_id)}</li>` : ''}`;
    }).join('');
    return `<div class="fieldset"><legend>${box.is_admin ? '전체 피드백' : '내가 보낸 피드백'} <span class="count">${items.length}</span></legend>
      <ul class="rows">${rows}</ul></div>`;
  }

  function FDB_detailHost(fid){
    const d = RS.fdbDetail[fid];
    if(!d){
      FDB_loadDetail(fid).then(()=>{ if($('#modal-root .dialog')) FDB_draw(); else render(); });
      return `<p class="empty">불러오는 중…</p>`;
    }
    if(d.error){
      return `<p class="empty">${d.status === 403 ? '이 피드백을 볼 권한이 없습니다.' : '상세를 불러오지 못했습니다.'}</p>`;
    }
    const cmts = (d.comments || []);
    const cmtHtml = cmts.length
      ? `<ul class="rows">${cmts.map(c =>
          `<li class="row" style="display:block">
            <div class="muted" style="font-size:12px">${esc(c.author || '')} · ${esc(ROLE_LABEL[c.author_role] || c.author_role || '')} · ${esc(FDB_fmt(c.created_at))}</div>
            <div style="white-space:pre-wrap;margin-top:4px">${esc(c.body || '')}</div>
          </li>`).join('')}</ul>`
      : `<p class="empty">아직 답변이 없습니다.</p>`;
    const adminBox = FDB_isAdmin()
      ? `<div class="fieldset" style="margin-top:10px">
          <legend>관리자 처리</legend>
          <div class="inline">
            <select class="in" id="fdb-st-${fid}">
              ${FDB_ST_ORDER.map(s => `<option value="${s}" ${s === d.status ? 'selected' : ''}>${esc(FDB_ST[s])}</option>`).join('')}
            </select>
            <button class="btn" onclick="App.fdbSetStatus('${fid}')">상태 변경</button>
          </div>
          <div class="field" style="margin-top:10px">
            <label>답변 작성</label>
            <textarea class="in" id="fdb-cmt" rows="3" placeholder="사용자에게 남길 답변"></textarea>
          </div>
          <div class="inline">
            <button class="btn btn-primary" onclick="App.fdbComment('${fid}')">답변 등록</button>
          </div>
          <div class="inline-msg" id="fdb-detail-msg"></div>
        </div>`
      : `<p class="note">답변 작성은 관리자·운영관리자만 가능합니다.</p>`;
    return `
    <div class="panel" style="margin-top:6px">
      <div class="panel-head"><h2>${esc(d.title || '')}</h2>${FDB_stBadge(d.status)}</div>
      <div class="summary">
        <div><span class="k">작성자</span><span class="v mono">${esc(d.author || '')}</span></div>
        <div><span class="k">분류</span><span class="v">${esc(FDB_catLabel(d.category))}</span></div>
        <div><span class="k">등록</span><span class="v mono">${esc(FDB_fmt(d.created_at))}</span></div>
        <div><span class="k">갱신</span><span class="v mono">${esc(FDB_fmt(d.updated_at))}</span></div>
      </div>
      <div class="fieldset" style="margin-top:10px">
        <legend>내용</legend>
        <div style="white-space:pre-wrap">${esc(d.body || '')}</div>
      </div>
      <div class="fieldset" style="margin-top:10px">
        <legend>답변 스레드 <span class="count">${cmts.length}</span></legend>
        ${cmtHtml}
      </div>
      <div class="fieldset" style="margin-top:10px">
        <legend>첨부 이미지 <span class="count">${(d.images || []).length}</span></legend>
        ${(d.images || []).length
          ? `<div class="inline">${(d.images || []).map((im, i) => `<button class="btn btn-sm btn-ghost" onclick="App.fdbImgView('${fid}', ${i})">${esc(im.filename || ('이미지 ' + (i + 1)))}</button>`).join('')}</div>`
          : `<p class="empty">첨부 이미지가 없습니다.</p>`}
        <div class="inline" style="margin-top:6px"><button class="btn btn-sm" onclick="App.fdbImgAdd('${fid}')">이미지 첨부</button></div>
      </div>
      ${adminBox}
    </div>`;
  }

  function FDB_modalBody(){
    const tabs = [['new','작성'],['list', FDB_isAdmin() ? '전체 보기' : '내 피드백']];
    const cur = (S.fdbTab === 'list') ? 'list' : 'new';
    const head = `<div class="tabs">${tabs.map(([k,l]) =>
      `<button class="${cur === k ? 'on' : ''}" onclick="App.fdbTab('${k}')">${esc(l)}</button>`).join('')}</div>`;
    const body = cur === 'new' ? FDB_form() : FDB_list();
    return head + body;
  }

  const FDB_draw = () => {
    openModal('개선 피드백', FDB_modalBody(),
      `<button class="btn" onclick="App.closeModal()">닫기</button>`, true);
  };

  SHELL_EXT.sideFoot.push(() =>
    `<button class="link" style="font-size:12px;text-align:left" onclick="App.fdbOpen()">개선 피드백 보내기</button>`);

  App.fdbOpen = function(){
    S.fdbTab = 'new';
    S.fdbShow = null;
    RS.fdbList = null;
    RS.fdbDetail = {};
    FDB_draw();
  };

  App.fdbTab = function(k){
    S.fdbTab = k;
    if(k === 'list' && !RS.fdbList){
      RS.fdbList = null; FDB_loadList().then(()=>{ if($('#modal-root .dialog')) FDB_draw(); else render(); });
    }
    if($('#modal-root .dialog')) FDB_draw(); else render();
  };

  App.fdbShow = function(i){
    const box = RS.fdbList;
    if(!box || !box.items || !box.items[i]) return;
    const fid = box.items[i].feedback_id;
    if(S.fdbShow === fid){ S.fdbShow = null; }
    else { S.fdbShow = fid; if(!RS.fdbDetail[fid]) FDB_loadDetail(fid).then(()=>{ if($('#modal-root .dialog')) FDB_draw(); else render(); }); }
    if($('#modal-root .dialog')) FDB_draw(); else render();
  };

  App.fdbSend = async function(){
    const title = (($('#fdb-title')||{}).value||'').trim();
    const body  = (($('#fdb-body')||{}).value||'').trim();
    const cat   = (($('#fdb-category')||{}).value||'improvement');
    const msgEl = $('#fdb-form-msg');
    if(!title){ if(msgEl) msgEl.textContent = '제목을 입력해 주세요.'; return; }
    const payload = {title, body, category: cat};
    try{
      const r = await apiFetch('/feedback', {method:'POST', body: JSON.stringify(payload)});
      if(!r.ok){
        let detail = '';
        try{ const j = await r.json(); detail = j && j.detail && j.detail.code ? ` (${j.detail.code})` : ''; }catch(e){}
        if(r.status === 403) return toast('권한이 없습니다.');
        return toast('피드백 등록에 실패했습니다.' + detail);
      }
      RS.fdbList = null;
      S.fdbTab = 'list';
      S.fdbShow = null;
      await FDB_loadList();
      toast('피드백을 보냈습니다.');
      if($('#modal-root .dialog')) FDB_draw(); else render();
    }catch(e){ toast('피드백 등록에 실패했습니다.'); }
  };

  App.fdbComment = async function(fid){
    const body = (($('#fdb-cmt')||{}).value||'').trim();
    const msgEl = $('#fdb-detail-msg');
    if(!body){ if(msgEl) msgEl.textContent = '답변 내용을 입력해 주세요.'; return; }
    try{
      const r = await apiFetch(`/feedback/${fid}/comments`, {method:'POST', body: JSON.stringify({body})});
      if(!r.ok){
        let detail = '';
        try{ const j = await r.json(); detail = j && j.detail && j.detail.code ? ` (${j.detail.code})` : ''; }catch(e){}
        if(r.status === 403) return toast('권한이 없습니다.');
        return toast('답변 등록에 실패했습니다.' + detail);
      }
      delete RS.fdbDetail[fid];
      RS.fdbList = null;
      await FDB_loadDetail(fid);
      await FDB_loadList();
      toast('답변을 등록했습니다.');
      if($('#modal-root .dialog')) FDB_draw(); else render();
    }catch(e){ toast('답변 등록에 실패했습니다.'); }
  };

  // 첨부 이미지 보기 — 인증 헤더가 필요해 blob 으로 받아 새 탭에 연다
  App.fdbImgView = async function(fid, i){
    const d = RS.fdbDetail[fid] || {};
    const im = (d.images || [])[i];
    if(!im) return;
    try{
      const r = await apiFetch(`/feedback/${fid}/image/${im.image_id}`);
      if(!r.ok) return toast(r.status === 403 ? '권한이 없습니다.' : '이미지를 불러오지 못했습니다.');
      const url = URL.createObjectURL(await r.blob());
      window.open(url, '_blank', 'noopener');
      setTimeout(() => URL.revokeObjectURL(url), 60000);
    }catch(e){ toast('이미지를 불러오지 못했습니다.'); }
  };

  App.fdbImgAdd = function(fid){
    const inp = document.createElement('input');
    inp.type = 'file'; inp.accept = 'image/*';
    inp.onchange = () => {
      const file = inp.files && inp.files[0];
      if(!file) return;
      const reader = new FileReader();
      reader.onload = async () => {
        try{
          const r = await apiFetch(`/feedback/${fid}/images`, {method:'POST', body: JSON.stringify({file_b64: reader.result, filename: file.name})});
          if(!r.ok){
            if(r.status === 403) return toast('권한이 없습니다.');
            if(r.status === 413) return toast('파일이 너무 큽니다.');
            let detail = '';
            try{ const j = await r.json(); detail = j && j.detail && j.detail.code ? ` (${j.detail.code})` : ''; }catch(e){}
            return toast('이미지 첨부에 실패했습니다.' + detail);
          }
          delete RS.fdbDetail[fid];
          await FDB_loadDetail(fid);
          toast('이미지를 첨부했습니다.');
          if($('#modal-root .dialog')) FDB_draw(); else render();
        }catch(e){ toast('이미지 첨부에 실패했습니다.'); }
      };
      reader.readAsDataURL(file);
    };
    inp.click();
  };

  App.fdbSetStatus = async function(fid){
    const sel = $('#fdb-st-' + fid);
    const status = sel ? sel.value : '';
    const msgEl = $('#fdb-detail-msg');
    if(!status){ if(msgEl) msgEl.textContent = '상태를 선택해 주세요.'; return; }
    try{
      const r = await apiFetch(`/feedback/${fid}`, {method:'PATCH', body: JSON.stringify({status})});
      if(!r.ok){
        let detail = '';
        try{ const j = await r.json(); detail = j && j.detail && j.detail.code ? ` (${j.detail.code})` : ''; }catch(e){}
        if(r.status === 403) return toast('권한이 없습니다.');
        return toast('상태 변경에 실패했습니다.' + detail);
      }
      delete RS.fdbDetail[fid];
      RS.fdbList = null;
      await FDB_loadDetail(fid);
      await FDB_loadList();
      toast('상태를 변경했습니다.');
      if($('#modal-root .dialog')) FDB_draw(); else render();
    }catch(e){ toast('상태 변경에 실패했습니다.'); }
  };
})();
