/* ===== 인증기업: 차량·세척 이력 (물류) ===== */
(function(){
  const ORG = () => (AUTH && AUTH.org_id) || '';
  let qcache = {};

  const VF = ['plate_no','vehicle_type','transport_type','capacity','reg_no',
    'previous_cargo','previous_cargo_halal','last_cleaned','sertu','note'];
  const CF = ['cleaned_at','previous_cargo','previous_cargo_halal','method',
    'sertu_steps','photo_doc_id','next_due','note'];

  const er = (r,d) => { const c=(d&&d.code)?' ('+d.code+')':''; return r.status===403
    ? '권한이 없습니다'+c : '요청에 실패했습니다'+c; };

  async function jfetch(path, opts){
    try{ const r = await apiFetch(path, opts);
      let d=null; try{ d=await r.json(); }catch(e){}
      return {ok:r.ok, status:r.status, d};
    }catch(e){ return {ok:false, status:0, d:null}; }
  }

  function ENTL_state(){
    RS.entl = RS.entl || {list:null, cleanings:{}, busy:{}};
    return RS.entl;
  }
  async function ENTL_load(){
    const st = ENTL_state(); const oid = ORG();
    if(!oid){ st.list = {error:true}; return; }
    const r = await jfetch('/orgs/'+encodeURIComponent(oid)+'/vehicles');
    st.list = r.ok ? r.d : {error:true, status:r.status};
  }
  async function ENTL_loadCleanings(vid){
    const st = ENTL_state();
    if(st.busy['c:'+vid]) return;
    st.busy['c:'+vid] = true;
    const r = await jfetch('/vehicles/'+encodeURIComponent(vid)+'/cleanings');
    st.cleanings[vid] = r.ok ? r.d : {error:true, status:r.status};
    delete st.busy['c:'+vid];
  }

  const B = (v)=> v===true?'<span class="badge strong">예</span>'
    : v===false?'<span class="badge attn">아니오</span>'
    : '<span class="muted">—</span>';
  const N = (v)=> v===null||v===undefined||v==='' ? '<span class="muted">—</span>' : esc(String(v));
  const opt = (v,cur)=> v===cur?' selected':'';

  const VTYPE = ['truck','van','container','tanker','reefer'];
  const TTRAN = ['ambient','chilled','frozen','insulated'];

  function ENTL_val(fields, prefix){
    const o={}; fields.forEach(k=>{ const e=$('#'+(prefix||'entl-v-')+k); if(e) o[k]=e.value; });
    return o;
  }
  function ENTL_collect(fields, prefix, optFields){
    const q = (prefix||'entl-v-');
    const o = {};
    (optFields||[]).forEach(k=>{ const e=$('#'+q+k); if(e) o[k]=e.value; });
    fields.forEach(k=>{ const e=$('#'+q+k); if(!e) return;
      if(e.type==='checkbox'){ o[k]=!!e.checked; }
      else if(e.type==='number'){ const s=e.value.trim(); o[k]= s===''?null:Number(s); }
      else { const s=e.value.trim(); o[k]= s===''?null:s; }
    });
    return o;
  }

  /* ---------- 뷰 ---------- */
  function ENTL_view(){
    const st = ENTL_state();
    let listHalf;
    if(!st.list){
      if(!st.busy.list){ st.busy.list=true; ENTL_load().then(()=>{ st.busy.list=false; render(); }); }
      listHalf = '<p class="empty">불러오는 중…</p>';
    } else if(st.list.error){
      listHalf = `<p class="empty">${st.list.status===403?'이 화면을 볼 권한이 없습니다.':'불러오지 못했습니다.'}</p>`;
    } else if(!st.list.length){
      listHalf = '<p class="empty">등록된 차량이 없습니다. 물류(운송·창고) 인증일 때만 필요합니다.</p>';
    } else {
      listHalf = '<div class="tbl-wrap"><table><thead><tr>'
        + '<th>번호판</th><th>종류</th><th>운송</th><th>적재량</th><th>등록번호</th>'
        + '<th>직전화물</th><th>직전 할랄</th><th>최근세척</th><th>Sertu</th>'
        + '<th>운송가능</th><th>세척수</th><th>다음예정</th><th></th>'
        + '</tr></thead><tbody>'
        + st.list.map(v=>{
          const on = S.entlVeh===v.vehicle_id;
          return `<tr><td class="mono">${esc(v.plate_no||'')}</td><td>${N(v.vehicle_type)}</td>`
            + `<td>${N(v.transport_type)}</td><td>${N(v.capacity)}</td><td class="mono">${N(v.reg_no)}</td>`
            + `<td>${N(v.previous_cargo)}</td><td>${B(v.previous_cargo_halal)}</td>`
            + `<td>${N(v.last_cleaned)}</td><td>${B(v.sertu)}</td>`
            + `<td>${B(v.halal_clear)}</td><td class="num">${N(v.cleaning_count)}</td><td>${N(v.next_due)}</td>`
            + `<td class="row-act">`
            + `<button class="btn btn-sm" onclick="App.entlToggle('${v.vehicle_id}')">${on?'닫기':'세척 이력'}</button>`
            + `<button class="btn btn-sm btn-ghost" onclick="App.entlEdit('${v.vehicle_id}')">수정</button>`
            + `<button class="btn btn-sm btn-ghost" onclick="App.entlDel('${v.vehicle_id}')">삭제</button>`
            + `</td></tr>`;
        }).join('') + '</tbody></table></div>';
    }

    let detail = '';
    if(S.entlVeh) detail = ENTL_cleanings(S.entlVeh);

    return `<section class="panel">
      <div class="panel-head"><h2>차량·세척 이력</h2>
        <button class="btn btn-sm btn-primary" onclick="App.entlAdd()">차량 추가</button></div>
      ${listHalf}${detail}</section>`;
  }

  function ENTL_cleanings(vid){
    const st = ENTL_state();
    const d = st.cleanings[vid];
    if(!d){
      ENTL_loadCleanings(vid).then(()=>render());
      return '<div class="fieldset" style="margin-top:14px"><legend>세척 이력</legend><p class="empty">불러오는 중…</p></div>';
    }
    if(d.error){ return `<div class="fieldset" style="margin-top:14px"><legend>세척 이력</legend>
      <p class="empty">${d.status===403?'이 화면을 볼 권한이 없습니다.':'불러오지 못했습니다.'}</p></div>`; }
    const items = d.items||[];
    const head = `<div class="panel-head" style="margin-top:14px"><h2>세척 이력</h2>
      <button class="btn btn-sm btn-primary" onclick="App.entlCleanAdd('${vid}')">세척 기록 추가</button></div>`;
    const halal = `<p class="muted">현재 운송 가능: ${B(d.halal_clear)}</p>`;
    if(!items.length) return `<div class="fieldset" style="margin-top:14px"><legend>세척 이력</legend>${halal}<p class="empty">세척 기록이 없습니다.</p>${head}</div>`;
    return `<div class="fieldset" style="margin-top:14px"><legend>세척 이력</legend>${halal}
      <div class="tbl-wrap"><table><thead><tr>
        <th>일시</th><th>직전화물</th><th>직전 할랄</th><th>방법</th><th>Sertu단계</th>
        <th>사진</th><th>서명</th><th>서명일</th><th>다음예정</th><th>비고</th><th></th>
      </tr></thead><tbody>
      ${items.map(c=>`<tr>
        <td>${N(c.cleaned_at)}</td><td>${N(c.previous_cargo)}</td><td>${B(c.previous_cargo_halal)}</td>
        <td>${esc(c.method||'')}</td><td class="num">${N(c.sertu_steps)}</td>
        <td>${c.photo_doc_id?`<button class="link" onclick="App.docDownload('${c.photo_doc_id}','photo')">보기</button>`:'<span class="muted">—</span>'}</td>
        <td>${B(c.penyelia_sign)}</td><td>${N(c.signed_at)}</td><td>${N(c.next_due)}</td><td>${N(c.note)}</td>
        <td class="row-act">
          ${c.penyelia_sign?'':`<button class="btn btn-sm" onclick="App.entlSign('${c.cleaning_id}')">서명</button>`}
          <button class="btn btn-sm btn-ghost" onclick="App.entlCleanDel('${c.cleaning_id}','${vid}')">삭제</button>
        </td></tr>`).join('')}
      </tbody></table></div></div>`;
  }

  /* ---------- 차량 폼 ---------- */
  function ENTL_vehModal(v){
    v = v || {};
    const sel = (arr,cur)=> arr.map(x=>`<option value="${x}"${opt(x,cur)}>${x}</option>`).join('');
    const f = `<div class="grid-2">
      <div class="field"><label>번호판 <span class="req">*</span></label><input class="in" id="entl-v-plate_no" value="${esc(v.plate_no||'')}"></div>
      <div class="field"><label>종류</label><select class="in" id="entl-v-vehicle_type">${sel(VTYPE, v.vehicle_type)}</select></div>
      <div class="field"><label>운송</label><select class="in" id="entl-v-transport_type">${sel(TTRAN, v.transport_type)}</select></div>
      <div class="field"><label>적재량</label><input class="in" id="entl-v-capacity" value="${esc(v.capacity||'')}"></div>
      <div class="field"><label>등록번호</label><input class="in" id="entl-v-reg_no" value="${esc(v.reg_no||'')}"></div>
      <div class="field"><label>직전화물</label><input class="in" id="entl-v-previous_cargo" value="${esc(v.previous_cargo||'')}"></div>
      <div class="field"><label>직전화물 할랄</label><select class="in" id="entl-v-previous_cargo_halal">
        <option value="">—</option><option value="true"${v.previous_cargo_halal===true?' selected':''}>예</option>
        <option value="false"${v.previous_cargo_halal===false?' selected':''}>아니오</option></select></div>
      <div class="field"><label>최근세척</label><input class="in" id="entl-v-last_cleaned" value="${esc(v.last_cleaned||'')}"></div>
      <div class="field"><label>Sertu</label><input type="checkbox" id="entl-v-sertu"${v.sertu?' checked':''}></div>
      <div class="field"><label>비고</label><input class="in" id="entl-v-note" value="${esc(v.note||'')}"></div>
    </div>`;
    const foot = `<button class="btn" onclick="App.closeModal()">취소</button>
      <button class="btn btn-primary" onclick="App.entlSave('${v.vehicle_id||''}')">저장</button>`;
    openModal(v.vehicle_id?'차량 수정':'차량 추가', f, foot, true);
  }

  /* ---------- 세척 폼 ---------- */
  function ENTL_cleanModal(vid){
    const f = `<div class="grid-2">
      <div class="field"><label>세척 일시</label><input class="in" id="entl-c-cleaned_at"></div>
      <div class="field"><label>직전화물</label><input class="in" id="entl-c-previous_cargo"></div>
      <div class="field"><label>직전화물 할랄</label><select class="in" id="entl-c-previous_cargo_halal">
        <option value="">—</option><option value="true">예</option><option value="false">아니오</option></select></div>
      <div class="field"><label>방법</label><select class="in" id="entl-c-method">
        <option value="normal">normal</option><option value="sertu">sertu</option></select></div>
      <div class="field"><label>Sertu 단계</label><input type="number" class="in" id="entl-c-sertu_steps"></div>
      <div class="field"><label>사진 문서 ID</label><input class="in" id="entl-c-photo_doc_id"></div>
      <div class="field"><label>다음 예정</label><input class="in" id="entl-c-next_due"></div>
      <div class="field"><label>비고</label><input class="in" id="entl-c-note"></div>
    </div>`;
    const foot = `<button class="btn" onclick="App.closeModal()">취소</button>
      <button class="btn btn-primary" onclick="App.entlCleanSave('${vid}')">저장</button>`;
    openModal('세척 기록 추가', f, foot, true);
  }

  /* ---------- 서명 캔버스 ---------- */
  function ENTL_signPad(){
    const c = $('#entl-pad'); if(!c) return;
    const ctx = c.getContext('2d');
    let drawing=false, has=false;
    const pos = (e)=>{ const r=c.getBoundingClientRect();
      return {x:(e.clientX-r.left)*c.width/r.width, y:(e.clientY-r.top)*c.height/r.height}; };
    c.__has = ()=>has; c.__data = ()=> has ? c.toDataURL('image/png') : null; c.__clear=()=>{ctx.clearRect(0,0,c.width,c.height);has=false;};
    ctx.lineWidth=2; ctx.lineCap='round'; ctx.strokeStyle='#111';
    c.addEventListener('pointerdown', e=>{ drawing=true; has=true; c.setPointerCapture(e.pointerId);
      const p=pos(e); ctx.beginPath(); ctx.moveTo(p.x,p.y); });
    c.addEventListener('pointermove', e=>{ if(!drawing) return; const p=pos(e); ctx.lineTo(p.x,p.y); ctx.stroke(); });
    c.addEventListener('pointerup', ()=>{ drawing=false; });
    c.addEventListener('pointercancel', ()=>{ drawing=false; });
  }

  App.entlSign = function(cid){
    const f = `<p class="muted">규정대로 세척되었음을 확정하는 서명입니다. 서명 이미지를 그려 주세요.</p>
      <canvas id="entl-pad" width="420" height="140" style="border:1px solid var(--line);border-radius:6px;touch-action:none;background:#fff;max-width:100%"></canvas>
      <p><button class="btn btn-sm btn-ghost" onclick="App.entlPadClear()">지우기</button></p>`;
    const foot = `<button class="btn" onclick="App.closeModal()">취소</button>
      <button class="btn btn-primary" onclick="App.entlSignSubmit('${cid}')">서명</button>`;
    openModal('세척 서명', f, foot, true);
    setTimeout(ENTL_signPad, 0);
  };
  App.entlPadClear = function(){ const c=$('#entl-pad'); if(c&&c.__clear) c.__clear(); };

  App.entlSignSubmit = async function(cid){
    const r = await jfetch('/cleanings/'+encodeURIComponent(cid)+'/sign', {method:'POST', body:JSON.stringify({})});
    if(!r.ok){ toast('서명에 실패했습니다.'+((r.d&&r.d.code)?' ('+r.d.code+')':'')); return; }
    App.closeModal();
    const st = ENTL_state();
    st.cleanings = {};        // 세척 캐시 무효화
    delete st.busy['c:'+ (S.entlVeh||'')];
    await ENTL_load();
    if(S.entlVeh) await ENTL_loadCleanings(S.entlVeh);
    render(); toast('서명했습니다.');
  };

  /* ---------- 액션 ---------- */
  App.entlAdd = ()=> ENTL_vehModal(null);
  App.entlEdit = function(vid){
    const st = ENTL_state(); const v = (st.list||[]).find(x=>x.vehicle_id===vid); if(v) ENTL_vehModal(v);
  };
  App.entlToggle = function(vid){
    S.entlVeh = (S.entlVeh===vid) ? null : vid;
    if(S.entlVeh && !ENTL_state().cleanings[S.entlVeh]) ENTL_loadCleanings(S.entlVeh).then(()=>render());
    render();
  };

  App.entlSave = async function(vid){
    const fields = VF;
    const body = ENTL_collect(fields, 'entl-v-');
    // checkbox type handled; select값 불리언 변환
    const h=$('#entl-v-previous_cargo_halal'); if(h) body.previous_cargo_halal = h.value===''?null:(h.value==='true');
    if(!body.plate_no){ toast('번호판을 입력해 주세요.'); return; }
    const path = vid ? '/vehicles/'+encodeURIComponent(vid) : '/orgs/'+encodeURIComponent(ORG())+'/vehicles';
    const method = vid ? 'PATCH' : 'POST';
    const r = await jfetch(path, {method, body:JSON.stringify(body)});
    if(!r.ok){ toast('저장에 실패했습니다.'+((r.d&&r.d.code)?' ('+r.d.code+')':'')); return; }
    App.closeModal();
    const st = ENTL_state(); st.list=null;
    await ENTL_load(); render(); toast('저장했습니다.');
  };

  App.entlDel = function(vid){
    const foot = `<button class="btn" onclick="App.closeModal()">취소</button>
      <button class="btn btn-primary" onclick="App.entlDelGo('${vid}')">삭제</button>`;
    openModal('차량 삭제', '<p>이 차량을 삭제할까요?</p>', foot, false);
  };
  App.entlDelGo = async function(vid){
    const r = await jfetch('/vehicles/'+encodeURIComponent(vid), {method:'DELETE'});
    if(!r.ok){ toast('삭제에 실패했습니다.'+((r.d&&r.d.code)?' ('+r.d.code+')':'')); return; }
    App.closeModal();
    if(S.entlVeh===vid) S.entlVeh=null;
    const st = ENTL_state(); st.list=null; delete st.cleanings[vid];
    await ENTL_load(); render(); toast('삭제했습니다.');
  };

  App.entlCleanAdd = (vid)=> ENTL_cleanModal(vid);
  App.entlCleanSave = async function(vid){
    const body = ENTL_collect(CF, 'entl-c-');
    const h=$('#entl-c-previous_cargo_halal'); if(h) body.previous_cargo_halal = h.value===''?null:(h.value==='true');
    const m=$('#entl-c-method'); if(m) body.method = m.value;
    const r = await jfetch('/vehicles/'+encodeURIComponent(vid)+'/cleanings', {method:'POST', body:JSON.stringify(body)});
    if(!r.ok){ toast('저장에 실패했습니다.'+((r.d&&r.d.code)?' ('+r.d.code+')':'')); return; }
    App.closeModal();
    const st = ENTL_state(); delete st.cleanings[vid]; st.list=null;
    await ENTL_load(); await ENTL_loadCleanings(vid); render(); toast('저장했습니다.');
  };

  App.entlCleanDel = async function(cid, vid){
    const r = await jfetch('/cleanings/'+encodeURIComponent(cid), {method:'DELETE'});
    if(!r.ok){ toast('삭제에 실패했습니다.'+((r.d&&r.d.code)?' ('+r.d.code+')':'')); return; }
    const st = ENTL_state(); delete st.cleanings[vid]; st.list=null;
    await ENTL_load(); await ENTL_loadCleanings(vid); render(); toast('삭제했습니다.');
  };

  /* ---------- 뷰 등록 ---------- */
  const prev = VIEWS['ent-apply'];
  VIEWS['ent-apply'] = () => prev() + ENTL_view();
})();
