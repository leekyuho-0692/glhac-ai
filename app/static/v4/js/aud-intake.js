/* Intake projection: one authorized request; all user data stays escaped. */
function INTAKE_label(key){
  const fields = [...(typeof ENTP_PROFILE_FIELDS==='undefined'?[]:ENTP_PROFILE_FIELDS), ...(typeof ENTP_EXT_FIELDS==='undefined'?[]:ENTP_EXT_FIELDS)];
  const known = fields.find(f=>f[0]===key);
  if(known) return t(known[1]);
  const translated=t('intake.field.'+key);
  return translated==='intake.field.'+key ? (RS.intake?.[S.intakeCase]?.labels?.[S.lang]?.[key]||String(key).replace(/_/g,' ').replace(/^./,c=>c.toUpperCase())) : translated;
}
const INTAKE_ENUM_FIELDS=new Set(['status','draft_state','scheme','pathway','logistics_scope','sector','sectors',
  'risk_category','source','mat_type','cert','screen_result','screen_status','screen_severity',
  'review_status','uploader_role','actor_role','submitted_role','verdict','channel','conflict_reason']);
function INTAKE_text(value, key){
  if(value===null || value===undefined || value==='') return t('intake.empty');
  if(typeof value==='boolean') return t(value?'intake.yes':'intake.no');
  if(!INTAKE_ENUM_FIELDS.has(key)) return String(value);
  const label=RS.intake?.[S.intakeCase]?.value_labels?.[S.lang]?.[value];
  if(label) return label;
  const code='intake.value.'+value, translated=t(code);
  if(translated!==code) return translated;
  const role='role.'+value;
  return t(role)!==role ? t(role) : String(value);
}
function INTAKE_value(value, key){
  if(Array.isArray(value)) return value.length ? '<ul>'+value.map(v=>'<li>'+INTAKE_value(v,key)+'</li>').join('')+'</ul>' : esc(t('intake.empty'));
  if(value && typeof value==='object') return INTAKE_fields(value);
  return esc(INTAKE_text(value,key));
}
function INTAKE_fields(value, skip=[]){
  const entries=Object.entries(value||{}).filter(([key])=>!key.startsWith('_')&&!skip.includes(key));
  return entries.length ? `<dl class="intake-grid">${entries.map(([key,v])=>`<div><dt>${esc(INTAKE_label(key))}</dt><dd>${INTAKE_value(v,key)}</dd></div>`).join('')}</dl>` : `<p class="muted">${t('intake.empty')}</p>`;
}
function INTAKE_source(source){
  const code=typeof source==='object' ? source?.source : source;
  const alias={ai:'llm',ai_unconfirmed:'llm',llm_confirmed:'llm',direct:'manual',user:'manual',user_input:'manual',structural_parser:'structural',filename_hint:'filename'};
  const kind=alias[code]||code;
  const key='intake.source.'+kind;
  return `<span class="badge intake-source" data-source="${esc(kind||'unknown')}">${esc(t(key)===key?t('intake.source.unknown'):t(key))}</span>`;
}
function INTAKE_conflicts(conflicts){
  if(!conflicts?.length) return '';
  return `<div class="intake-conflicts"><h3>${t('intake.conflicts')}</h3>${conflicts.map(c=>`<article class="intake-card"><b>${esc(INTAKE_label(c.field||'field'))}</b><dl class="intake-grid"><div><dt>${t('intake.current')}</dt><dd>${INTAKE_value(c.current,c.field)}</dd></div><div><dt>${t('intake.proposed')}</dt><dd>${INTAKE_value(c.proposed,c.field)}</dd></div></dl>${INTAKE_fields({filename:c.filename,reason:INTAKE_text(c.reason,'conflict_reason')})}${c.current_source?INTAKE_fields({source:c.current_source}):''}</article>`).join('')}</div>`;
}
function INTAKE_button(cid){
  return cid ? `<button class="btn btn-sm intake-open" onclick="event.stopPropagation();App.intakeOpen('${jsq(cid)}')">${t('intake.open')}</button>` : '';
}
function INTAKE_document(d, index){
  return `<details class="intake-card intake-document"><summary><b>${INTAKE_value(d.filename)}</b> <span class="badge">${INTAKE_value(d.review_status,'review_status')}</span></summary>
    ${INTAKE_fields(d,['document_id','filename','fields','sources','verify','truncated','truncations','problem_reasons','has_file','uploaded_by','material_id','product_id'])}
    ${d.has_file?`<button class="btn btn-sm" onclick="App.intakeOriginal(${index})">${t('intake.original')}</button>`:''}
    ${d.problem_reasons?.length?`<ul class="note attn">${d.problem_reasons.map(r=>`<li>${esc(r.explanations?.[S.lang]||r.code)}</li>`).join('')}</ul>`:''}
    ${d.truncated?`<p class="note attn">${t('intake.truncated')}</p>${INTAKE_value(d.truncations)}`:''}
    <h3>${t('intake.fields')}</h3><div class="tbl-wrap" tabindex="0"><table class="intake-extracted"><thead><tr><th>${t('intake.field.field')}</th><th>${t('intake.fields')}</th><th>${t('intake.source')}</th></tr></thead><tbody>
      ${Object.entries(d.fields||{}).map(([key,value])=>`<tr><th>${esc(INTAKE_label(key))}</th><td>${INTAKE_value(value,key)}</td><td>${INTAKE_source(d.sources?.[key])}</td></tr>`).join('')||`<tr><td colspan="3">${t('intake.empty')}</td></tr>`}
    </tbody></table></div><h3>${t('intake.verify')}</h3>${INTAKE_fields(d.verify)}</details>`;
}
function INTAKE_render(data){
  const co=data.company||{}, app=data.application||{}, summary=data.summary||{};
  const section=(id,key,body)=>`<section id="intake-${id}" class="panel"><h2>${t('intake.'+key)}</h2>${body}</section>`;
  const cards=(items,fn)=>items?.length?items.map(fn).join(''):`<p class="muted">${t('intake.empty')}</p>`;
  const summaryFields={company_name:co.company_name,status:app.status,draft_state:app.draft_state,submitted_at:app.submitted_at};
  return `<div class="intake-page"><header class="panel intake-summary"><h1>${t('nav.aud-intake')}</h1><h2>${INTAKE_value(co.company_name)}</h2>
    <div class="row-act intake-actions"><button class="btn" onclick="App.intakeBack()">${t('intake.back')}</button><button class="btn" onclick="App.intakeRefresh()">${t('intake.reload')}</button><button class="btn" onclick="window.print()">${t('intake.print')}</button><button class="btn btn-primary" onclick="App.ctlPreassessDocx('${jsq(data.case_id)}')">${t('intake.docx')}</button></div>
    ${INTAKE_fields(summaryFields)}<dl class="intake-grid">${['problem_document_count','pending_count','conflict_count'].map(k=>`<div><dt>${t('intake.'+(k==='problem_document_count'?'problem_count':k))}</dt><dd>${summary[k]||0}</dd></div>`).join('')}</dl></header>
    ${section('company','company',INTAKE_fields(co,['org_id']))}
    ${section('contacts','contacts',INTAKE_fields(data.contacts))}
    ${section('consent','consent',INTAKE_fields(data.consent))}
    ${section('facilities','facilities',cards(data.facilities,f=>`<article class="intake-card">${INTAKE_fields(f,['facility_id'])}</article>`))}
    ${section('products','products',cards(data.products,p=>`<article class="intake-card">${INTAKE_fields(p,['product_id'])}</article>`))}
    ${section('materials','materials',cards(data.materials,m=>`<article class="intake-card"><h3>${INTAKE_value(m.name)}</h3><p>${t('intake.stored_screen')} <span class="badge ${['BLOCK','NEEDS_EVIDENCE'].includes(m.screen_result)?'attn':'strong'}">${INTAKE_value(m.screen_result,'screen_result')}</span></p>${INTAKE_fields(m,['material_id','name','evidence'])}<h4>${t('intake.evidence')}</h4>${cards(m.evidence,e=>INTAKE_fields(e,['document_id']))}</article>`))}
    ${section('documents','documents',cards(data.documents,INTAKE_document))}
    ${section('pending','pending',cards(data.pending?.items,p=>`<article class="intake-card">${INTAKE_fields(p,['document_id'])}</article>`)+INTAKE_conflicts(data.pending?.conflicts))}
    ${section('history','history',data.history?.length?data.history.map(h=>`<article class="intake-card">${INTAKE_fields({actor_name:h.actor_name||t('intake.no_history'),actor_role:h.actor_role||t('intake.no_history'),at:h.at||t('intake.no_history'),action:h.action})}${(h.changes||[]).map(c=>`<b>${esc(INTAKE_label(c.field||'field'))}</b><dl class="intake-grid"><div><dt>${t('intake.before')}</dt><dd>${'before' in c?INTAKE_value(c.before,c.field):t('intake.no_history')}</dd></div><div><dt>${t('intake.after')}</dt><dd>${'after' in c?INTAKE_value(c.after,c.field):t('intake.no_history')}</dd></div></dl>`).join('')||`<p>${t('intake.no_history')}</p>`}${h.details?INTAKE_fields(h.details):''}${h.filename?INTAKE_fields({filename:h.filename}):''}</article>`).join(''):`<p>${t('intake.no_history')}</p>`)}
    ${section('formal','formal',INTAKE_fields(app.formal_application))}</div>`;
}
let INTAKE_generation=0;
const INTAKE_loading=new Set();
async function INTAKE_load(cid){
  const generation=INTAKE_generation;
  if(INTAKE_loading.has(cid)) return;
  INTAKE_loading.add(cid);
  try{
    const r=await apiFetch('/cases/'+encodeURIComponent(cid)+'/intake-view');
    const data=r.ok?await r.json():{error:true};
    if(generation===INTAKE_generation){ RS.intake=RS.intake||{}; RS.intake[cid]=data; }
  }catch(e){ if(generation===INTAKE_generation){ RS.intake=RS.intake||{}; RS.intake[cid]={error:true}; } }
  finally{ if(generation===INTAKE_generation) INTAKE_loading.delete(cid); }
}
VIEWS['aud-intake']=()=>{
  const cid=S.intakeCase, data=RS.intake?.[cid];
  if(!cid) return `<section class="panel"><p>${t('intake.empty')}</p></section>`;
  if(!data){ if(!INTAKE_loading.has(cid)) INTAKE_load(cid).then(()=>render()); return `<section class="panel"><p>${t('members.loading')}</p></section>`; }
  if(data.error) return `<section class="panel"><p role="alert">${t('intake.error')}</p><button class="btn" onclick="App.intakeRefresh()">${t('intake.reload')}</button></section>`;
  return INTAKE_render(data);
};
App.intakeOpen=function(cid){ S.intakeReturn=S.view; S.intakeCase=cid; RS.intake={}; App.go('aud-intake'); };
App.intakeBack=function(){ App.go(S.intakeReturn||'aud-home'); };
App.intakeRefresh=async function(){ if(!S.intakeCase)return; await INTAKE_load(S.intakeCase); render(); };
App.intakeOriginal=function(index){ const d=RS.intake?.[S.intakeCase]?.documents?.[index]; if(d?.has_file) return App.docDownload(d.document_id,d.filename); };
const INTAKE_logout=App.logout;
App.logout=function(){ INTAKE_generation++; INTAKE_loading.clear(); RS.intake={}; S.intakeCase=null; return INTAKE_logout.apply(this,arguments); };
for(const [view,key] of [['aud-detail','audCo'],['cons-status','selCompany'],['adm-flow','selFlow']]){
  const original=VIEWS[view];
  if(original) VIEWS[view]=()=>{ const base=original(); return INTAKE_button(S[key])+base; };
}
/* Reuse the existing case discussion API. No separate notes store. */
App.intakeMemo=function(cid){
  openModal(t('intake.memo'),`<p>${t('intake.memo_hint')}</p><textarea id="intake-memo" class="in" rows="4" aria-label="${t('intake.memo')}"></textarea>`, `<button class="btn" onclick="App.closeModal()">${t('intake.cancel')}</button><button class="btn btn-primary" onclick="App.intakeMemoSave('${jsq(cid)}')">${t('intake.send')}</button>`);
};
App.intakeMemoSave=async function(cid){
  const text=($('#intake-memo')?.value||'').trim(); if(!text) return;
  const r=await apiFetch('/cases/'+encodeURIComponent(cid)+'/discussions',{method:'POST',body:JSON.stringify({kind:'comment',target:null,text})}).catch(()=>null);
  if(!r?.ok) return toast(t('intake.error'));
  if(RS.cht) delete RS.cht[cid]; App.closeModal(); toast(t('intake.saved'));
};
