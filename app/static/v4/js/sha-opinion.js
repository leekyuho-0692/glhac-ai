/* ===== 현장심사 검토 의견 (샤리아) ===== */
const SHO_OPINIONS = [
  ["comply", "적합"],
  ["nonconformity", "부적합"],
];
const SHO_OPLABEL = { comply: "적합", nonconformity: "부적합" };

async function SHO_load(cid){
  RS.sho = RS.sho || {};
  try{
    const [rc, ro] = await Promise.all([
      apiFetch('/cases/'+cid+'/onsite-checklist'),
      apiFetch('/cases/'+cid+'/onsite/reviewer-opinions'),
    ]);
    const checklist = rc.ok ? await rc.json() : {error:true, status:rc.status};
    const opinions = ro.ok ? await ro.json() : {error:true, status:ro.status};
    RS.sho[cid] = {checklist, opinions};
  }catch(e){
    RS.sho[cid] = {error:true};
  }
}

function SHO_badge(op){
  if(op==='comply') return `<span class="badge strong">적합</span>`;
  if(op==='nonconformity') return `<span class="badge attn">부적합</span>`;
  return `<span class="badge">미입력</span>`;
}
function SHO_oResult(res){
  if(res==='comply') return `<span class="badge strong">적합</span>`;
  if(res==='nonconformity') return `<span class="badge attn">부적합</span>`;
  return `<span class="muted">미점검</span>`;
}

function SHO_detail(cid, d){
  const cl = d.checklist, op = d.opinions;
  const opErr = !op || op.error;
  const clErr = !cl || cl.error;
  if(clErr && opErr){
    const st = (cl && cl.status) || (op && op.status);
    return `<section class="panel"><p class="empty">${st===403?'이 화면을 볼 권한이 없습니다.':'검토 의견을 불러오지 못했습니다.'}</p></section>`;
  }
  const items = (cl && cl.items) || [];
  const opItems = (op && op.items) || {};
  const negative = (op && typeof op.negative === 'number') ? op.negative : Object.values(opItems).filter(v=>v && v.opinion==='nonconformity').length;
  const canEdit = (AUTH.role==='fatwa_liaison' || AUTH.role==='operator');

  const sum = `<section class="panel"><div class="panel-head"><h2>요약</h2></div>
    <div class="summary">
      <div><span class="k">점검 항목</span><span class="v mono">${items.length}</span></div>
      <div><span class="k">적합 판정</span><span class="v mono">${(cl && cl.comply) || 0}</span></div>
      <div><span class="k">부적합 판정</span><span class="v mono">${(cl && cl.nonconformity) || 0}</span></div>
      <div><span class="k">부적합 의견</span><span class="v mono">${negative}</span></div>
      <div><span class="k">작성 완료율</span><span class="v mono">${(cl && cl.completion) || 0}%</span></div>
    </div></section>`;

  if(!items.length){
    return sum + `<section class="panel"><p class="empty">현장심사 체크리스트가 아직 없습니다.</p></section>`;
  }

  const rows = items.map((it, idx)=>{
    const o = opItems[it.item_key] || {};
    const opinion = o.opinion;
    const who = (o.actor || o.role) ? `<span class="muted mono" style="font-size:11px">${esc(String(o.actor||''))} ${esc(ROLE_LABEL[o.role]||o.role||'')}</span>` : '';
    const input = canEdit
      ? `<div class="inline">
           <select class="in" id="sho-op-${idx}">${SHO_OPINIONS.map(([k,l])=>`<option value="${k}" ${opinion===k?'selected':''}>${esc(l)}</option>`).join('')}</select>
           <input class="in" id="sho-cm-${idx}" value="${esc(o.comment||'')}" placeholder="코멘트 (부적합은 5자 이상)">
           <button class="btn btn-sm btn-primary" onclick="App.shoSave('${cid}',${idx})">저장</button>
         </div>`
      : `<span class="muted">—</span>`;
    return `<tr>
      <td>${esc(it.label)}</td>
      <td>${SHO_oResult(it.result)}</td>
      <td>${SHO_badge(opinion)}${o.comment?`<div class="muted" style="font-size:12px">${esc(o.comment)}</div>`:''}${who}</td>
      <td>${input}</td>
    </tr>`;
  }).join('');

  return sum + `<section class="panel">
    <div class="panel-head"><h2>항목별 검토 의견</h2>${canEdit?'':`<span class="muted">읽기 전용</span>`}</div>
    <div class="tbl-wrap"><table>
      <thead><tr><th>점검 항목</th><th>오디터 판정</th><th>검토 의견</th><th>입력</th></tr></thead>
      <tbody>${rows}</tbody>
    </table></div>
  </section>`;
}

VIEWS['sha-review'] = () => {
  const list = (RS.cases||[]).filter(c=>!c.done);
  const sel = S.selFatwa;
  if(!sel) return '';
  RS.sho = RS.sho || {};
  const d = RS.sho[sel];
  if(!d){
    if(RS._shoLoading!==sel){
      RS._shoLoading = sel;
      SHO_load(sel).then(()=>{ RS._shoLoading=null; render(); });
    }
    return `<section class="panel"><p class="empty">불러오는 중…</p></section>`;
  }
  if(d.error){
    const st = d.checklist && d.checklist.status;
    return `<section class="panel"><p class="empty">${st===403?'이 화면을 볼 권한이 없습니다.':'검토 의견을 불러오지 못했습니다.'}</p></section>`;
  }
  return SHO_detail(sel, d);
};

App.shoSave = async function(cid, idx){
  const d = RS.sho && RS.sho[cid];
  const items = d && d.checklist && d.checklist.items;
  if(!items || !items[idx]) return toast('항목을 찾을 수 없습니다.');
  const item = items[idx];
  const opinion = (($('#sho-op-'+idx)||{}).value||'').trim();
  const comment = (($('#sho-cm-'+idx)||{}).value||'').trim();
  if(!opinion) return toast('판정을 선택해 주세요.');
  if(opinion==='nonconformity' && comment.length<5){
    toast('부적합 의견은 코멘트 5자 이상 필요합니다.');
    return;
  }
  try{
    const r = await apiFetch('/cases/'+cid+'/onsite/reviewer-opinion', {
      method:'POST', body: JSON.stringify({item_key:item.item_key, opinion, comment}),
    });
    if(!r.ok){
      if(r.status===403){ toast('권한이 없습니다.'); return; }
      let code = '';
      try{ const j = await r.json(); code = j && j.detail && j.detail.code ? ` (${j.detail.code})` : ''; }catch(e){}
      toast('검토 의견 저장에 실패했습니다.'+code);
      return;
    }
    await SHO_load(cid);
    render();
    toast('검토 의견을 저장했습니다.');
  }catch(e){
    toast('검토 의견 저장에 실패했습니다.');
  }
};
