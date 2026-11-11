/* ===== 단계별 업체 목록 ===== */
const SL_SECTOR={food:'sector.food',cosmetics:'sector.cosmetics',household:'sector.household',warehouse:'sector.warehouse',transport:'sector.transport'};
const SL_OWNER={client:'role.applicant',consultant:'role.consultant',auditor:'role.auditor',sharia:'role.fatwa_liaison',ops:'role.admin'};
const SL_SCHEME={product:'stage.product',logistics:'stage.logistics'};
const SL_PAGE = 50;
function SL_init(){
  RS.sl=RS.sl||{};
  if(!Number.isInteger(S.slPage)||S.slPage<0) S.slPage=0;
  if(!Number.isInteger(S.slStep)||S.slStep<0||S.slStep>7) S.slStep=null;
}

const SL_role = () => S.role==='adm' ? 'adm' : (S.role==='cons' ? 'cons' : 'aud');
const SL_countStep = (step) => (RS.cases||[]).filter(c=>c.step8===step).length;
const SL_countDone = () => (RS.cases||[]).filter(c=>c.done).length;
const SL_countHold = () => (RS.cases||[]).filter(c=>c.hold).length;
const SL_key = (step,q,sector) => (step==null?'all':String(step))+'|'+q+'|'+sector;

async function SL_load(step, q, sector){
  const key = SL_key(step, q, sector);
  try{
    const p = new URLSearchParams(); p.set('limit','500'); p.set('offset','0');
    if(step!=null) p.set('step8', String(step));
    if(sector) p.set('sector', sector);
    if(q) p.set('search', q);
    const r = await apiFetch('/cases?'+p.toString());
    RS.sl[key] = r.ok ? (await r.json()) : {error:true, status:r.status};
  }catch(e){ RS.sl[key] = {error:true}; }
}

function SL_filter(items){
  let arr = items.slice();
  if(S.slDone) arr=arr.filter(c=>c.done);
  if(S.slOwner) arr = arr.filter(c=>c.owner===S.slOwner);
  if(S.slHold) arr = arr.filter(c=>c.hold);
  if(S.slSort==='name') arr.sort((a,b)=>String(a.company_name||'').localeCompare(String(b.company_name||''),S.lang));
  else if(S.slSort==='status') arr.sort((a,b)=>String(a.status||'').localeCompare(String(b.status||'')));
  else arr.sort((a,b)=>String(b.last_event_at||'').localeCompare(String(a.last_event_at||'')));
  return arr;
}

function SL_detail(cur, arr){
  SL_init();
  const pages = Math.max(1, Math.ceil(arr.length/SL_PAGE));
  S.slPage=Math.min(S.slPage,pages-1);
  const step=arr.slice(S.slPage*SL_PAGE,(S.slPage+1)*SL_PAGE);
  const head = `<div class="panel-head sl-head"><h2>${t('nav.cons-stage')}</h2><div class="sl-pagination" aria-label="${t('stage.pagination')}"><span class="sl-count">${t('stage.page_count').replace('{count}',arr.length).replace('{page}',S.slPage+1).replace('{pages}',pages)}</span><div class="row-act"><button class="btn btn-sm" aria-label="${t('stage.previous')}" ${S.slPage===0?'disabled':''} onclick="App.slPage(-1)">◀</button><button class="btn btn-sm" aria-label="${t('stage.next')}" ${S.slPage>=pages-1?'disabled':''} onclick="App.slPage(1)">▶</button></div></div></div>`;
  if(!arr.length) return `<section class="panel">${head}<p class="empty">${t('upload.text_025')}</p></section>`;
  const rows = step.map(c=>`<tr style="cursor:pointer" onclick="App.slOpenCase('${c.case_id}')">
    <td>${esc(c.company_name)}</td>
    <td>${esc(t(SL_SCHEME[c.scheme]||c.scheme||''))}</td>
    <td>${esc(t(SL_SECTOR[c.sector]||c.sector||''))}</td>
    <td>${esc(Number.isInteger(c.step8)?t('stage.step'+c.step8):_s8label(c))} ${c.hold?`<span class="badge attn">${t('ui.text_049')}</span>`:''}</td>
    <td class="mono" style="font-size:11px">${esc(c.status||'')}</td>
    <td>${esc(c.main_auditor||'-')}${(c.co_auditors&&c.co_auditors.length)?' / '+esc(c.co_auditors.join(', ')):''}</td>
    <td>${esc(c.consultant||'-')}</td>
    <td>${esc(c.next_action||'-')}</td>
    <td class="muted mono" style="font-size:11px">${esc((c.last_event_at||'').slice(0,16).replace('T',' '))}</td>
    <td><button class="btn btn-sm" onclick="event.stopPropagation();App.slOpenCase('${c.case_id}')">${t('ui.text_050')}</button></td>
  </tr>`).join('');
  return `<section class="panel">${head}<div class="tbl-wrap"><table><thead><tr>
    <th>${t('ui.text_022')}</th><th>${t('ui.text_051')}</th><th>${t('ui.text_052')}</th><th>${t('ui.text_053')}</th><th>${t('upload.text_013')}</th><th>${t('ui.text_054')}</th><th>${t('ui.text_055')}</th><th>${t('ui.text_056')}</th><th>${t('ui.text_057')}</th><th></th>
  </tr></thead><tbody>${rows}</tbody></table></div></section>`;
}

function SL_view(){
  SL_init();
  const key = SL_key(S.slStep, S.slQ||'', S.slSector||'');
  const c = RS.sl[key];
  const tabs = [['null',t('stage.all')+((RS.cases||[]).length)], ...S8_LABELS.map((l,i)=>[String(i), pad(i+1)+' '+t('stage.step'+i)+' '+SL_countStep(i)])];
  const curTab = S.slStep===null?'null':String(S.slStep);
  const tabHtml = `<div class="chipbar">${tabs.map(([k,l])=>`<button class="chip ${curTab===k?'on':''}" onclick="App.slStep(${k==='null'?'null':k})">${esc(l)}</button>`).join('')}<button class="chip ${S.slDone?'on':''}" onclick="App.slDone()">${t('stage.done')} ${SL_countDone()}</button></div>`;
  const filt = `<div class="inline">
    <input id="sl-q" class="in" placeholder="${t('ui.text_063')}" value="${esc(S.slQ||'')}" onkeydown="if(event.key==='Enter')App.slQ(this.value)">
    <select id="sl-sector" class="in" onchange="App.slSector(this.value)"><option value="">${t('ui.text_058')}</option>${Object.keys(SL_SECTOR).map(k=>`<option value="${k}" ${S.slSector===k?'selected':''}>${esc(t(SL_SECTOR[k]))}</option>`).join('')}</select>
    <select id="sl-owner" class="in" onchange="App.slOwner(this.value)"><option value="">${t('ui.text_059')}</option>${Object.keys(SL_OWNER).map(k=>`<option value="${k}" ${S.slOwner===k?'selected':''}>${esc(t(SL_OWNER[k]))}</option>`).join('')}</select>
    <label class="inline"><input type="checkbox" ${S.slHold?'checked':''} onchange="App.slHold(this.checked)"> ${t('ui.text_060')}</label>
    <select id="sl-sort" class="in" onchange="App.slSort(this.value)"><option value="last" ${S.slSort==='last'?'selected':''}>${t('ui.text_061')}</option><option value="name" ${S.slSort==='name'?'selected':''}>${t('ui.text_062')}</option><option value="status" ${S.slSort==='status'?'selected':''}>${t('upload.text_013')}</option></select>
  </div>`;
  let body;
  if(!c){ if(RS._slLoading!==key){ RS._slLoading=key; SL_load(S.slStep, S.slQ||'', S.slSector||'').then(()=>{ RS._slLoading=null; render(); }); } body = `<section class="panel"><p class="empty">${t('members.loading')}</p></section>`; }
  else if(c.error){ body = `<section class="panel"><p class="empty">${c.status===403?t('upload.text_038'):t('upload.text_024')}</p></section>`; }
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

['aud-flow','adm-flow','cons-flow'].forEach(v=>{ if(VIEWS[v]){ const f = VIEWS[v]; VIEWS[v] = () => `<div class="note">${t('ui.text_064')} <button class="btn btn-sm" onclick="App.slOpen(null)">${t('ui.text_065')}</button> ${t('stage.search_hint')} ${S8_LABELS.map((l,i)=>`<button class="btn btn-sm" onclick="App.slOpen(${i})">${pad(i+1)} ${t('stage.step'+i)} ${SL_countStep(i)}</button>`).join('')}</div>` + f(); } });

const _SL_go = App.go;
App.go = function(v){ RS.sl = {}; return _SL_go.apply(this, arguments); };

App.slStep = function(step){ S.slStep = step===null?null:Number(step); S.slDone=false; S.slPage=0; render(); };
App.slDone = function(){ S.slDone=!S.slDone; S.slPage=0; if(S.slDone){ S.slStep=null; } render(); };
App.slQ = function(v){ S.slQ = (v||'').trim(); S.slPage=0; render(); };
App.slSector = function(v){ S.slSector = v||''; S.slPage=0; render(); };
App.slOwner = function(v){ S.slOwner = v||''; S.slPage=0; render(); };
App.slHold = function(v){ S.slHold = !!v; S.slPage=0; render(); };
App.slSort = function(v){ S.slSort = v||'last'; S.slPage=0; render(); };
App.slPage = function(d){
  SL_init();
  const data=RS.sl[SL_key(S.slStep,S.slQ||'',S.slSector||'')];
  const last=Math.max(0,Math.ceil(SL_filter((data&&data.items)||[]).length/SL_PAGE)-1);
  S.slPage=Math.min(last,Math.max(0,S.slPage+(Number(d)||0))); render();
};
App.slOpen = function(step){ S.slStep = step===null?null:Number(step); S.slDone=false; S.slPage=0; App.go(SL_role()+'-stage'); };
App.slOpenCase = function(id){ S.selCompany=id; if(S.role==='aud' && App.audDetail) return App.audDetail(id); App.go(SL_role()==='adm'?'adm-flow':'cons-status'); };

NAVC['aud-stage'] = NAVC['adm-stage'] = NAVC['cons-stage'] = SL_countHold;

if(typeof I18N !== 'undefined'){
  if(I18N.en) Object.assign(I18N.en, {'단계별 업체 목록':'Companies by Stage','분야':'Sector','마지막 변경':'Last change','보류만':'On hold only','최근 변경순':'Recent first','업체명':'Company','담당':'In charge','전체':'All','완료':'Complete','열기':'Open','다음 할 일':'Next task','종류':'Type','단계':'Stage','상태':'Status','메인/서브 오디터':'Main/Co auditor','컨설턴트':'Consultant','제품':'Product','물류':'Logistics'});
  if(I18N.id) Object.assign(I18N.id, {'단계별 업체 목록':'Perusahaan per Tahap','분야':'Sektor','마지막 변경':'Perubahan terakhir','보류만':'Hanya ditahan','최근 변경순':'Terbaru dulu','업체명':'Perusahaan','담당':'Penanggung jawab','전체':'Semua','완료':'Selesai','열기':'Buka','다음 할 일':'Tugas berikutnya','종류':'Jenis','단계':'Tahap','상태':'Status','메인/서브 오디터':'Auditor utama/pendamping','컨설턴트':'Konsultan','제품':'Produk','물류':'Logistik'});
}
