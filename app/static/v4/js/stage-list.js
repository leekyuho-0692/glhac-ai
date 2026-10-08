/* ===== 단계별 업체 목록 ===== */
const SL_SECTOR = {food:'식품', cosmetics:'화장품', household:'생활용품', warehouse:'창고', transport:'운송'};
const SL_OWNER = {client:'기업', consultant:'컨설턴트', auditor:'오디터', sharia:'샤리아', ops:'관리자'};
const SL_SCHEME = {product:'제품', logistics:'물류'};
const SL_PAGE = 50;

const SL_role = () => S.role==='adm' ? 'adm' : (S.role==='cons' ? 'cons' : 'aud');
const SL_countStep = (step) => (RS.cases||[]).filter(c=>c.step8===step).length;
const SL_countDone = () => (RS.cases||[]).filter(c=>c.done).length;
const SL_countHold = () => (RS.cases||[]).filter(c=>c.hold).length;
const SL_key = (step,q,sector) => (step===null?'all':String(step))+'|'+q+'|'+sector;

async function SL_load(step, q, sector){
  const key = SL_key(step, q, sector);
  try{
    const p = new URLSearchParams(); p.set('limit','500'); p.set('offset','0');
    if(step!==null) p.set('step8', String(step));
    if(sector) p.set('sector', sector);
    if(q) p.set('search', q);
    const r = await apiFetch('/cases?'+p.toString());
    RS.sl[key] = r.ok ? (await r.json()) : {error:true, status:r.status};
  }catch(e){ RS.sl[key] = {error:true}; }
}

function SL_filter(items){
  let arr = items.slice();
  if(S.slOwner) arr = arr.filter(c=>c.owner===S.slOwner);
  if(S.slHold) arr = arr.filter(c=>c.hold);
  if(S.slSort==='name') arr.sort((a,b)=>String(a.company_name||'').localeCompare(String(b.company_name||''),'ko'));
  else if(S.slSort==='status') arr.sort((a,b)=>String(a.status||'').localeCompare(String(b.status||'')));
  else arr.sort((a,b)=>String(b.last_event_at||'').localeCompare(String(a.last_event_at||'')));
  return arr;
}

function SL_detail(cur, arr){
  const step = arr.slice(S.slPage*SL_PAGE, S.slPage*SL_PAGE+SL_PAGE);
  const pages = Math.max(1, Math.ceil(arr.length/SL_PAGE));
  const head = `<div class="panel-head"><h2>단계별 업체 목록</h2><span class="count">${arr.length}건 · 페이지 ${S.slPage+1}/${pages}<button class="btn btn-sm" onclick="App.slPage(-1)">◀</button><button class="btn btn-sm" onclick="App.slPage(1)">▶</button></span></div>`;
  if(!arr.length) return `<section class="panel">${head}<p class="empty">대상 업체가 없습니다.</p></section>`;
  const rows = step.map(c=>`<tr style="cursor:pointer" onclick="App.slOpenCase('${c.case_id}')">
    <td>${esc(c.company_name)}</td>
    <td>${esc(SL_SCHEME[c.scheme]||c.scheme||'')}</td>
    <td>${esc(SL_SECTOR[c.sector]||c.sector||'')}</td>
    <td>${esc(c.step8_label||_s8label(c))} ${c.hold?'<span class="badge attn">보류</span>':''}</td>
    <td class="mono" style="font-size:11px">${esc(c.status||'')}</td>
    <td>${esc(c.main_auditor||'-')}${(c.co_auditors&&c.co_auditors.length)?' / '+esc(c.co_auditors.join(', ')):''}</td>
    <td>${esc(c.consultant||'-')}</td>
    <td>${esc(c.next_action||'-')}</td>
    <td class="muted mono" style="font-size:11px">${esc((c.last_event_at||'').slice(0,16).replace('T',' '))}</td>
    <td><button class="btn btn-sm" onclick="event.stopPropagation();App.slOpenCase('${c.case_id}')">열기</button></td>
  </tr>`).join('');
  return `<section class="panel">${head}<div class="tbl-wrap"><table><thead><tr>
    <th>업체</th><th>종류</th><th>분야</th><th>단계</th><th>상태</th><th>오디터</th><th>컨설턴트</th><th>다음 할 일</th><th>마지막 변경</th><th></th>
  </tr></thead><tbody>${rows}</tbody></table></div></section>`;
}

function SL_view(){
  const key = SL_key(S.slStep, S.slQ||'', S.slSector||'');
  const c = RS.sl[key];
  const tabs = [['null','전체 '+((RS.cases||[]).length)], ...S8_LABELS.map((l,i)=>[String(i), pad(i+1)+' '+l+' '+SL_countStep(i)])];
  const curTab = S.slStep===null?'null':String(S.slStep);
  const tabHtml = `<div class="chipbar">${tabs.map(([k,l])=>`<button class="chip ${curTab===k?'on':''}" onclick="App.slStep(${k==='null'?'null':k})">${esc(l)}</button>`).join('')}<button class="chip ${S.slDone?'on':''}" onclick="App.slDone()">완료 ${SL_countDone()}</button></div>`;
  const filt = `<div class="inline">
    <input id="sl-q" class="in" placeholder="업체명 검색" value="${esc(S.slQ||'')}" onkeydown="if(event.key==='Enter')App.slQ(this.value)">
    <select id="sl-sector" class="in" onchange="App.slSector(this.value)"><option value="">분야 전체</option>${Object.keys(SL_SECTOR).map(k=>`<option value="${k}" ${S.slSector===k?'selected':''}>${esc(SL_SECTOR[k])}</option>`).join('')}</select>
    <select id="sl-owner" class="in" onchange="App.slOwner(this.value)"><option value="">담당 전체</option>${Object.keys(SL_OWNER).map(k=>`<option value="${k}" ${S.slOwner===k?'selected':''}>${esc(SL_OWNER[k])}</option>`).join('')}</select>
    <label class="inline"><input type="checkbox" ${S.slHold?'checked':''} onchange="App.slHold(this.checked)"> 보류만</label>
    <select id="sl-sort" class="in" onchange="App.slSort(this.value)"><option value="last" ${S.slSort==='last'?'selected':''}>최근 변경순</option><option value="name" ${S.slSort==='name'?'selected':''}>업체명</option><option value="status" ${S.slSort==='status'?'selected':''}>상태</option></select>
  </div>`;
  let body;
  if(!c){ if(RS._slLoading!==key){ RS._slLoading=key; SL_load(S.slStep, S.slQ||'', S.slSector||'').then(()=>{ RS._slLoading=null; render(); }); } body = '<section class="panel"><p class="empty">불러오는 중…</p></section>'; }
  else if(c.error){ body = `<section class="panel"><p class="empty">${c.status===403?'이 화면을 볼 권한이 없습니다.':'불러오지 못했습니다.'}</p></section>`; }
  else { body = SL_detail(key, SL_filter(c.items||[])); }
  return `<div style="display:flex;flex-direction:column;gap:22px">${tabHtml}${filt}${body}</div>`;
}

VIEWS['aud-stage'] = SL_view;
VIEWS['adm-stage'] = SL_view;
VIEWS['cons-stage'] = SL_view;

function SL_navInsert(arr, after, id){ const i = arr.indexOf(after); if(i>=0) arr.splice(i+1, 0, id); else arr.push(id); }
SL_navInsert(NAV.aud, 'aud-flow', 'aud-stage');
SL_navInsert(NAV.adm, 'adm-flow', 'adm-stage');
SL_navInsert(NAV.cons, 'cons-flow', 'cons-stage');
Object.assign(I18N.ko, {'nav.aud-stage':'단계별 업체 목록','nav.adm-stage':'단계별 업체 목록','nav.cons-stage':'단계별 업체 목록'});
if(I18N.en) Object.assign(I18N.en, {'nav.aud-stage':'Companies by Stage','nav.adm-stage':'Companies by Stage','nav.cons-stage':'Companies by Stage'});
if(I18N.id) Object.assign(I18N.id, {'nav.aud-stage':'Perusahaan per Tahap','nav.adm-stage':'Perusahaan per Tahap','nav.cons-stage':'Perusahaan per Tahap'});

['aud-flow','adm-flow','cons-flow'].forEach(v=>{ if(VIEWS[v]){ const f = VIEWS[v]; VIEWS[v] = () => `<div class="note">업체가 많으면 보드 대신 <button class="btn btn-sm" onclick="App.slOpen(null)">단계별 업체 목록</button> 에서 검색·필터로 보세요. ${S8_LABELS.map((l,i)=>`<button class="btn btn-sm" onclick="App.slOpen(${i})">${pad(i+1)} ${esc(l)} ${SL_countStep(i)}</button>`).join('')}</div>` + f(); } });

const _SL_go = App.go;
App.go = function(v){ RS.sl = {}; return _SL_go.apply(this, arguments); };

App.slStep = function(step){ S.slStep = step===null?null:Number(step); S.slDone=false; S.slPage=0; render(); };
App.slDone = function(){ S.slDone=!S.slDone; if(S.slDone){ S.slStep=null; } render(); };
App.slQ = function(v){ S.slQ = (v||'').trim(); S.slPage=0; render(); };
App.slSector = function(v){ S.slSector = v||''; S.slPage=0; render(); };
App.slOwner = function(v){ S.slOwner = v||''; S.slPage=0; render(); };
App.slHold = function(v){ S.slHold = !!v; S.slPage=0; render(); };
App.slSort = function(v){ S.slSort = v||'last'; S.slPage=0; render(); };
App.slPage = function(d){ S.slPage = Math.max(0, (S.slPage||0)+d); render(); };
App.slOpen = function(step){ S.slStep = step===null?null:Number(step); S.slDone=false; S.slPage=0; App.go(SL_role()+'-stage'); };
App.slOpenCase = function(id){ if(S.role==='aud' && App.audDetail) return App.audDetail(id); S.selCase = id; App.go(SL_role()==='adm'?'adm-flow':'cons-status'); };

NAVC['aud-stage'] = NAVC['adm-stage'] = NAVC['cons-stage'] = SL_countHold;

if(typeof I18N !== 'undefined'){
  if(I18N.en) Object.assign(I18N.en, {'단계별 업체 목록':'Companies by Stage','분야':'Sector','마지막 변경':'Last change','보류만':'On hold only','최근 변경순':'Recent first','업체명':'Company','담당':'In charge','전체':'All','완료':'Complete','열기':'Open','다음 할 일':'Next task','종류':'Type','단계':'Stage','상태':'Status','메인/서브 오디터':'Main/Co auditor','컨설턴트':'Consultant','제품':'Product','물류':'Logistics'});
  if(I18N.id) Object.assign(I18N.id, {'단계별 업체 목록':'Perusahaan per Tahap','분야':'Sektor','마지막 변경':'Perubahan terakhir','보류만':'Hanya ditahan','최근 변경순':'Terbaru dulu','업체명':'Perusahaan','담당':'Penanggung jawab','전체':'Semua','완료':'Selesai','열기':'Buka','다음 할 일':'Tugas berikutnya','종류':'Jenis','단계':'Tahap','상태':'Status','메인/서브 오디터':'Auditor utama/pendamping','컨설턴트':'Konsultan','제품':'Produk','물류':'Logistik'});
}
