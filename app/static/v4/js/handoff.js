/* ===== 홈페이지 로그인 인계 (handoff) ===== */
const HOF_RE = /handoff=([A-Za-z0-9_\-]+)/;

async function HOF_consume(){
  try{
    const m = (location.hash||'').match(HOF_RE);
    if(!m) return;
    const code = m[1];
    try{ history.replaceState(null,'',location.pathname+location.search); }
    catch(e){ location.hash=''; }
    let r;
    try{
      r = await fetch(API_BASE + '/auth/handoff/exchange', {
        method:'POST',
        headers:{'Content-Type':'application/json'},
        body: JSON.stringify({handoff: code})
      });
    }catch(e){
      toast('만료되었거나 이미 사용된 링크입니다. 다시 로그인해 주세요.');
      return;
    }
    if(!r.ok){
      toast(await apiErr(r, '만료되었거나 이미 사용된 링크입니다. 다시 로그인해 주세요.'));
      return;
    }
    let d;
    try{ d = await r.json(); }
    catch(e){
      toast('만료되었거나 이미 사용된 링크입니다. 다시 로그인해 주세요.');
      return;
    }
    const ui = (typeof uiRoleFromServer === 'function') ? uiRoleFromServer(d.role) : null;
    if(!ui){
      toast('지원하지 않는 역할입니다.');
      return;
    }
    AUTH.access = d.token;
    AUTH.refresh = d.refresh_token;
    AUTH.role = d.role;
    AUTH.uiRole = ui;
    AUTH.username = d.username;
    AUTH.org_id = d.org_id;
    if(typeof authSave === 'function') authSave();
    S.role = ui;
    S.userId = null;
    S.auth = 'login';
    try{
      const rk = Object.keys(ROLES).find(k => ROLES[k].code === ui);
      if(rk && ROLES[rk] && ROLES[rk].home) S.view = ROLES[rk].home;
    }catch(e){}
    try{ if(typeof loadRbac === 'function') await loadRbac(); }catch(e){}
    try{
      if(ui === 'pen'){ if(typeof loadPen === 'function') await loadPen(); }
      else if(ui === 'aud') await loadAud();
      else if(ui === 'ent') await loadEnt();
      else if(ui === 'cons') await loadCons();
      else if(ui === 'sha') await loadSha();
      else await loadAdm();
    }catch(e){}
    render();
    toast('로그인되었습니다. (' + d.username + ')');
  }catch(e){
    /* 조용히 무시 — 인계 실패로 앱 자체가 죽으면 안 된다 */
  }
}

HOF_consume();
