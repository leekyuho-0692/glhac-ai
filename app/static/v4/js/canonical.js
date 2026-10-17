/* ===== 정본 (D-17 / D-18) ===== */
RS.cn = RS.cn || {};

const CN_LANGS = ['en','id','ko'];
const CN_LBL = {en:'English (정본)', id:'Bahasa Indonesia', ko:'한국어'};

async function CN_load(cid){
  try{ const r = await apiFetch('/cases/'+cid+'/canonical'); RS.cn[cid] = r.ok ? (await r.json()) : {error:true, status:r.status}; }
  catch(e){ RS.cn[cid] = {error:true}; }
}

App.cnLang = function(lang){ S.cnLang = lang; render(); };
App.cnSelLang = function(lang){ S.cnLang = lang; render(); };

App.cnDl = async function(cid, fmt, lang){
  const d = RS.cn[cid] || {}; const v = (d.d17 && d.d17.version) || '';
  try{ const r = await apiFetch('/cases/'+cid+'/canonical/D-17.'+fmt+'?lang='+lang); if(!r.ok) return toast('파일을 불러올 수 없습니다.'+(r.status===403?' (권한이 없습니다)':''));
    const blob = await r.blob(); const a = document.createElement('a'); a.href = URL.createObjectURL(blob);
    a.download = 'D-17_v'+v+'_'+lang+'.'+fmt; document.body.appendChild(a); a.click();
    setTimeout(()=>{ URL.revokeObjectURL(a.href); a.remove(); }, 4000);
  }catch(e){ toast('파일을 불러올 수 없습니다.'); }
};

function CN_panel(cid){
  if(!cid) return '';
  const d = RS.cn[cid];
  if(!d){
    if(RS._cnLoading!==cid){ RS._cnLoading=cid; CN_load(cid).then(()=>{ RS._cnLoading=null; render(); }); }
    return '<section class="panel"><p class="empty">불러오는 중…</p></section>';
  }
  if(d.error) return `<section class="panel"><p class="empty">${d.status===403?'이 정본을 볼 권한이 없습니다.':'정본을 불러오지 못했습니다.'}</p></section>`;
  if(!d.d17) return '<section class="panel"><p class="empty">정본 세트는 샤리아 상정 시 생성됩니다.</p></section>';
  const lang = CN_LANGS.indexOf(S.cnLang)>=0 ? S.cnLang : (d.canonical_lang||'en');
  const d17 = d.d17; const notice = (d.notice||{})[lang] || '';
  const hp = ((d17.sha256||{})[lang]||'').slice(0,16);
  const ro = S.role==='cons';
  const tabs = `<div class="tabs">${CN_LANGS.map(k=>`<button class="${lang===k?'on':''}" onclick="App.cnLang('${k}')">${CN_LBL[k]}</button>`).join('')}</div>`;
  const dl = ro ? '<span class="muted">열람 전용</span>' : `<button class="btn btn-sm" onclick="App.cnDl('${cid}','docx','${lang}')">[DOCX]</button> <button class="btn btn-sm" onclick="App.cnDl('${cid}','pdf','${lang}')">[PDF]</button>`;
  const sig = [];
  if(d17.auditor_sign) sig.push(`<div><dt>오디터 서명</dt><dd class="mono">${esc(d17.auditor_sign)}</dd></div>`);
  if(d.client_signed) sig.push(`<div><dt>기업 확인 서명</dt><dd class="mono">${esc(d.client_sign_name||'서명됨')}</dd></div>`);
  if(!sig.length) sig.push('<div><dt>서명</dt><dd class="muted">미서명</dd></div>');
  const d18 = d.d18 ? `<div class="inline" style="margin-top:8px"><span class="muted">인증 문서 취합본 D-18 (v${d.d18.version})</span> <button class="btn btn-sm" onclick="App.genPdf('${d.d18.gen_doc_id}','D-18_취합본.pdf')">[PDF]</button></div>` : '';
  return `<section class="panel"><div class="panel-head"><h2>현장심사 보고서 D-17 · 영문 정본 (v${esc(String(d17.version||''))})</h2><span class="badge">${esc(lang)}</span></div>
    ${tabs}
    <div class="note" style="margin-top:10px">${esc(notice)}</div>
    <div class="inline" style="margin-top:10px"><span class="muted mono">${esc(hp)}</span> ${dl}</div>
    <dl class="dl" style="margin-top:12px">${sig.join('')}</dl>
    ${d18}
  </section>`;
}

App.cnPadClear = function(){ S.cnInk = false; const c = $('#cn-pad'); if(c && c.getContext){ c.getContext('2d').clearRect(0,0,c.width,c.height); } };
App.cnClientSign = function(cid){
  S.cnInk = false;
  openModal('기업 확인 서명', `<div class="field"><label>서명자 이름</label><input class="in" id="cn-signer"></div>
    <div class="field"><label>서명</label><canvas id="cn-pad" width="480" height="160" style="border:1px solid #ccc;border-radius:6px;touch-action:none"></canvas></div>
    <div class="inline"><button class="btn btn-sm" onclick="App.cnPadClear()">지우기</button></div>`,
    `<button class="btn" onclick="App.closeModal()">취소</button><button class="btn btn-primary" onclick="App.cnClientSignDo('${cid}')">서명 제출</button>`, true);
  setTimeout(()=>{ const c = $('#cn-pad'); if(!c || !c.getContext) return; const ctx = c.getContext('2d'); let down=false;
    const pos=(e)=>{ const r=c.getBoundingClientRect(); return [e.clientX-r.left, e.clientY-r.top]; };
    c.addEventListener('pointerdown', e=>{ down=true; S.cnInk=true; const [x,y]=pos(e); ctx.beginPath(); ctx.moveTo(x,y); });
    c.addEventListener('pointermove', e=>{ if(!down) return; const [x,y]=pos(e); ctx.lineTo(x,y); ctx.stroke(); });
    c.addEventListener('pointerup', ()=>{ down=false; });
    c.addEventListener('pointerleave', ()=>{ down=false; });
  },0);
};
App.cnClientSignDo = async function(cid){
  const name = (($('#cn-signer')||{}).value||'').trim();
  if(!name) return toast('서명자 이름을 입력해 주세요.');
  if(!S.cnInk) return toast('서명을 입력해 주세요.');
  const c = $('#cn-pad'); const image = c && c.toDataURL ? c.toDataURL('image/png') : '';
  try{ const r = await apiFetch('/cases/'+cid+'/audit-report/client-sign', {method:'POST', body:JSON.stringify({name, image})});
    if(!r.ok){ let code=''; try{ const j = await r.json(); code = j && j.detail && j.detail.code ? j.detail.code : ''; }catch(e){}
      if(r.status===409 && code==='REPORT_NOT_APPROVED') return toast('보고서 승인 후 서명할 수 있습니다.');
      return toast('서명 제출에 실패했습니다.'+(code?' ('+code+')':'')); }
    delete RS.cn[cid]; closeModal(); toast('서명이 등록되었습니다.'); await CN_load(cid); render();
  }catch(e){ toast('서명 제출에 실패했습니다.'); }
};

if(typeof VIEWS['aud-final']!=='undefined'){ const _f = VIEWS['aud-final']; VIEWS['aud-final'] = () => _f() + CN_panel(S.selFinal || S.selCase); }
if(typeof VIEWS['sha-review']!=='undefined'){ const _f2 = VIEWS['sha-review']; VIEWS['sha-review'] = () => _f2() + CN_panel(S.selFatwa); }
if(typeof VIEWS['ent-audit']!=='undefined'){ const _f3 = VIEWS['ent-audit']; VIEWS['ent-audit'] = () => { const cs = RS.cases||[]; const cid = cs[0] && cs[0].case_id; if(!cid) return _f3();
  const d = RS.cn[cid]; let blk = CN_panel(cid);
  if(d && !d.error && d.d17){ if(!d.client_signed){ if(d.d17.auditor_sign || d.d17.version){ blk += '<section class="panel"><div class="panel-head"><h2>기업 확인 서명</h2></div><button class="btn btn-primary" onclick="App.cnClientSign(\''+cid+'\')">확인 서명</button></section>'; } } else { blk += '<section class="panel"><p class="empty">확인 서명: '+esc(d.client_sign_name||'서명됨')+'</p></section>'; } }
  return _f3() + blk; };
}

if(typeof App.go==='function'){ const _go = App.go; App.go = function(v){ RS.cn = {}; return _go.apply(this, arguments); }; }
