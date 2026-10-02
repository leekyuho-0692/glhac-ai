/* ===== 협의 스레드 (chat) ===== */
RS.cht = RS.cht || {};
function CHT_hash(cid){ return String(cid||'').slice(0,8); }

async function CHT_load(cid){
  try{
    const r = await apiFetch('/cases/'+encodeURIComponent(cid)+'/discussions');
    if(!r.ok){ RS.cht[cid] = {error:true, status:r.status}; return; }
    const j = await r.json();
    RS.cht[cid] = Array.isArray(j) ? j : [];
  }catch(e){ RS.cht[cid] = {error:true}; }
}

function CHT_panel(cid){
  if(!cid) return '';
  const idAttr = 'cht-msg-' + CHT_hash(cid);
  const data = RS.cht[cid];
  let inner;
  if(!data){
    if(RS._chtLoading !== cid){ RS._chtLoading = cid; CHT_load(cid).then(()=>{ RS._chtLoading=null; render(); }); }
    inner = '<p class="empty">불러오는 중…</p>';
  } else if(data.error){
    inner = `<p class="empty">${data.status===403 ? '이 화면을 볼 권한이 없습니다.' : '메시지를 불러오지 못했습니다.'}</p>`;
  } else if(!data.length){
    inner = '<p class="empty">아직 메시지가 없습니다.</p>';
  } else {
    inner = `<ul class="rows">${data.map(m=>{
      const isMe = AUTH && AUTH.username && m.author && m.author === AUTH.username;
      const roleKo = (ROLE_LABEL && ROLE_LABEL[m.author_role]) || m.author_role || '';
      const t = m.created_at ? String(m.created_at).replace('T',' ').slice(0,16) : '';
      const st = isMe ? ' style="text-align:right"' : '';
      const tag = m.kind && m.kind !== 'comment' ? `<span class="tag">${esc(m.kind)}</span> ` : '';
      const body = esc(m.text||'').replace(/\n/g,'<br>');
      return `<li class="row"${st}><div style="min-width:0"><div class="muted" style="font-size:11px">${tag}<span class="mono">${esc(m.author||'')}</span> · ${esc(roleKo)} · <span class="mono">${esc(t)}</span></div><div style="margin-top:4px;white-space:pre-wrap;word-break:break-word">${body}</div></div></li>`;
    }).join('')}</ul>`;
  }
  const foot = `<div class="inline" style="margin-top:10px">
      <textarea id="${idAttr}" class="in" rows="2" placeholder="${esc(t('chat.placeholder')||'메시지를 입력하세요…')}"></textarea>
      <button class="btn btn-primary" onclick="App.chtSend('${esc(cid)}')">보내기</button>
      <button class="btn btn-ghost" onclick="App.chtReload('${esc(cid)}')">새로고침</button>
    </div>`;
  return `<section class="panel"><div class="panel-head"><h2>협의</h2></div>${inner}${foot}</section>`;
}

App.chtReload = async function(cid){
  delete RS.cht[cid];
  await CHT_load(cid);
  render();
};

App.chtSend = async function(cid){
  const ta = $('#cht-msg-' + CHT_hash(cid));
  const text = ta ? (ta.value||'').trim() : '';
  if(!text) return;
  try{
    const r = await apiFetch('/cases/'+encodeURIComponent(cid)+'/discussions', {
      method:'POST', body: JSON.stringify({kind:'comment', target:null, text})
    });
    if(!r.ok){
      let extra = '';
      try{ const j = await r.json(); if(j && j.detail && j.detail.code) extra = ` (${j.detail.code})`; }catch(e){}
      if(r.status===403) return toast('권한이 없습니다.' + extra);
      return toast('메시지 전송에 실패했습니다.' + extra);
    }
    delete RS.cht[cid];
    await CHT_load(cid);
    render();
    toast('보냈습니다.');
  }catch(e){
    toast('메시지 전송에 실패했습니다.');
  }
};

/* ---- 붙이는 곳 ---- */
(() => {
  if(typeof I18N !== 'undefined' && I18N && I18N.ko){
    Object.assign(I18N.ko, {'chat.placeholder':'메시지를 입력하세요…'});
    if(I18N.en) Object.assign(I18N.en, {'chat.placeholder':'Type a message…'});
    if(I18N.id) Object.assign(I18N.id, {'chat.placeholder':'Tulis pesan…'});
  }

  // 기업
  if(typeof VIEWS !== 'undefined' && VIEWS['ent-consultant']){
    const f = VIEWS['ent-consultant'];
    VIEWS['ent-consultant'] = () => {
      let cid = null;
      if(RS.cases && RS.cases.length) cid = RS.cases[0].case_id;
      return f() + CHT_panel(cid);
    };
  }
  // 컨설턴트
  if(typeof VIEWS !== 'undefined' && VIEWS['cons-status']){
    const f = VIEWS['cons-status'];
    VIEWS['cons-status'] = () => {
      let cid = null;
      if(S.selCompany && RS.cases){
        const found = RS.cases.find(c=>c.company_name===S.selCompany);
        if(found) cid = found.case_id;
      }
      return f() + CHT_panel(cid);
    };
  }
  // 오디터
  if(typeof VIEWS !== 'undefined' && VIEWS['aud-detail']){
    const f = VIEWS['aud-detail'];
    VIEWS['aud-detail'] = () => f() + CHT_panel(S.audCo || null);
  }
})();
