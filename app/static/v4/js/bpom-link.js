/* ===== BPOM demo link (separate system) ===== */
const BPOM_DEMO_URL = 'https://bpom-automation.ngrok-free.dev/';
const BPOM_I18N = {
  "bpom.demo_link": ["BPOM 인증자동화 (시연)", "BPOM automation (demo)", "Otomasi BPOM (demo)"],
  "bpom.demo_notice": ["별도 시연 시스템 · 합성 데이터 · GL-HAC 계정과 연동되지 않음", "Separate demo system · Synthetic data · Not linked to GL-HAC accounts", "Sistem demo terpisah · Data sintetis · Tidak terhubung dengan akun GL-HAC"]
};
for(const [key,values] of Object.entries(BPOM_I18N)){
  ['ko','en','id'].forEach((lang,i)=>{ I18N[lang][key]=values[i]; });
}

SHELL_EXT.sideFoot.push(() => {
  if(S.role !== 'adm') return '';
  return `<a class="link" style="font-size:12px" href="${BPOM_DEMO_URL}" target="_blank" rel="noopener noreferrer" title="${esc(t('bpom.demo_notice'))}">${esc(t('bpom.demo_link'))}</a>`;
});
