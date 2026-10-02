/* ===== 운영 도구 (adm-ops) ===== */
const OPS_TABS = [];

if(!NAV.adm.includes('adm-ops')) NAV.adm.push('adm-ops');
Object.assign(I18N.ko, {'nav.adm-ops':'운영 도구'});
if(I18N.en) Object.assign(I18N.en, {'nav.adm-ops':'Operations'});
if(I18N.id) Object.assign(I18N.id, {'nav.adm-ops':'Alat Operasional'});

VIEWS['adm-ops'] = () => {
  if(AUTH.role !== 'admin' && AUTH.role !== 'operator')
    return `<section class="panel"><p class="empty">운영 도구는 관리자·운영관리자만 사용할 수 있습니다.</p></section>`;
  if(!OPS_TABS.length)
    return `<section class="panel"><p class="empty">등록된 운영 도구가 없습니다.</p></section>`;
  let cur = S.opsTab;
  if(!cur || !OPS_TABS.some(t => t.key === cur)) cur = OPS_TABS[0].key;
  S.opsTab = cur;
  const tabs = `<div class="tabs">${OPS_TABS.map(t => `<button class="${t.key === cur ? 'on' : ''}" onclick="App.opsTab('${t.key}')">${esc(t.label)}</button>`).join('')}</div>`;
  let body;
  try{
    const t = OPS_TABS.find(x => x.key === cur);
    body = t.render();
  }catch(e){
    body = `<section class="panel"><p class="empty">이 도구를 표시하지 못했습니다.</p></section>`;
  }
  return `${tabs}${body}`;
};

App.opsTab = function(k){ S.opsTab = k; render(); };
