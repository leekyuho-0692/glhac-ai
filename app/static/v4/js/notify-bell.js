/* ===== 알림 벨 ===== */
const NTF_LANG = () => (S && S.lang) || 'ko';

async function NTF_loadCount(){
  try{
    const r = await apiFetch('/notifications/unread-count');
    RS.ntfUnread = r.ok ? ((await r.json()).count || 0) : 0;
  }catch(e){ RS.ntfUnread = 0; }
}

async function NTF_loadList(){
  try{
    const r = await apiFetch('/notifications?lang=' + encodeURIComponent(NTF_LANG()));
    if(!r.ok){ RS.ntfList = {error:true, status:r.status}; return; }
    RS.ntfList = await r.json();
  }catch(e){ RS.ntfList = {error:true}; }
}

const NTF_esc = (v) => esc(v);

const NTF_modalBody = () => {
  const rs = RS.ntfList;
  if(!rs || rs.error === true){
    const msg = (rs && rs.status === 403) ? '권한이 없습니다.' : '알림을 불러오지 못했습니다.';
    return `<p class="empty">${msg}</p>`;
  }
  if(!Array.isArray(rs) || !rs.length) return `<p class="empty">알림이 없습니다.</p>`;
  return `<div class="rows"><ul style="list-style:none;padding:0;margin:0">${rs.map((n,i)=>{
    const read = !!n.read;
    return `<li class="row" style="display:flex;flex-direction:column;gap:6px;align-items:stretch">
      <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
        ${read?'':'<span class="badge attn">새 알림</span>'}
        <span style="font-weight:600">${NTF_esc(n.title)}</span>
        <span class="muted mono" style="font-size:11px;margin-left:auto">${NTF_esc(n.created_at)}</span>
      </div>
      ${n.body?`<div class="muted" style="font-size:13px;white-space:pre-wrap">${NTF_esc(n.body)}</div>`:''}
      <div class="row-act" style="display:flex;gap:6px">
        ${read?'':`<button class="btn btn-sm" onclick="App.ntfRead('${i}')">읽음</button>`}
      </div>
    </li>`;
  }).join('')}</ul></div>`;
};

App.ntfRead = async function(idx){
  const list = RS.ntfList;
  if(!Array.isArray(list)) return;
  const n = list[Number(idx)];
  if(!n || !n.id) return;
  try{
    const r = await apiFetch('/notifications/' + encodeURIComponent(n.id) + '/read', {method:'POST'});
    if(!r.ok){
      let code = '';
      try{ const j = await r.json(); code = j && j.detail && j.detail.code ? (' (' + j.detail.code + ')') : ''; }catch(e){}
      toast('읽음 처리에 실패했습니다.' + code);
      return;
    }
    RS.ntfUnread = undefined;
    await NTF_loadList();
    App.ntfOpen();
    render();
  }catch(e){ toast('읽음 처리에 실패했습니다.'); }
};

App.ntfReadAll = async function(){
  try{
    const r = await apiFetch('/notifications/read-all', {method:'POST'});
    if(!r.ok){
      let code = '';
      try{ const j = await r.json(); code = j && j.detail && j.detail.code ? (' (' + j.detail.code + ')') : ''; }catch(e){}
      toast('모두 읽음 처리에 실패했습니다.' + code);
      return;
    }
    RS.ntfUnread = undefined;
    await NTF_loadList();
    App.ntfOpen();
    render();
    toast('모두 읽음 처리되었습니다.');
  }catch(e){ toast('모두 읽음 처리에 실패했습니다.'); }
};

App.ntfOpen = function(){
  RS.ntfList = undefined;
  openModal('알림', `<div id="ntf-modal-body"><p class="empty">불러오는 중…</p></div>`,
    `<button class="btn" onclick="App.ntfReadAll()">모두 읽음</button>
     <button class="btn btn-primary" onclick="App.closeModal()">닫기</button>`, true);
  NTF_loadList().then(()=>{
    const box = $('#ntf-modal-body');
    if(box) box.innerHTML = NTF_modalBody();
    render();
  });
};

SHELL_EXT.sideFoot.push(() => {
  const n = RS.ntfUnread;
  if(n === undefined){
    if(!RS._ntfLoading){
      RS._ntfLoading = true;
      NTF_loadCount().then(()=>{ RS._ntfLoading = false; render(); });
    }
  }
  const cnt = (typeof n === 'number' && n > 0) ? ` <span class="count">${n}</span>` : '';
  return `<button class="btn btn-block btn-ghost" onclick="App.ntfOpen()">🔔 알림${cnt}</button>`;
});

{ const g = App.go; App.go = async function(v){ RS.ntfUnread = undefined; return g.call(App, v); }; }
