/* Translations for fixed messages identified in the v4 audit. */
const UIV4_AUDIT_I18N = {
  "audit.page": ["페이지", "Page", "Halaman"],
  "audit.processed": ["작성·보완 중 또는 접수 처리된 신청", "Draft, correction or processed applications", "Draf, pelengkapan, atau permohonan yang telah diproses"],
  "audit.no_companies": ["해당 업체가 없습니다.", "No matching companies.", "Tidak ada perusahaan yang sesuai."],
  "audit.sample_accounts": ["샘플 계정", "Demo accounts", "Akun demo"],
  "audit.excluded": ["제외", "Excluded", "Dikecualikan"],
  "audit.raw_material": ["원료 목록", "Raw material list", "Daftar bahan baku"],
  "audit.verified": ["처리 기록 {count}건 전부 현재 서명 키로 검증되었습니다.", "All {count} records were verified with the current signing key.", "Seluruh {count} catatan diverifikasi dengan kunci penandatanganan saat ini."],
  "audit.psre_note": ["벤더(PrivyID/VIDA/Peruri/Tilaka) 계약·자격증명(GLHAC_PSRE_PROVIDER/API_URL/TOKEN) 필요 — 미설정 시 내부 무결성 서명 폴백", "Vendor contract and credentials (GLHAC_PSRE_PROVIDER/API_URL/TOKEN) required. Internal integrity signatures are used when unconfigured.", "Kontrak dan kredensial vendor (GLHAC_PSRE_PROVIDER/API_URL/TOKEN) diperlukan. Tanda tangan integritas internal digunakan jika belum dikonfigurasi."],
  "audit.tsa_note": ["RFC3161 TSA 자격증명(GLHAC_TSA_URL, 선택 GLHAC_TSA_TOKEN) 필요 — 미설정 시 신뢰 타임스탬프 미연동(내부 시각만)", "RFC3161 TSA credentials required (GLHAC_TSA_URL, optional GLHAC_TSA_TOKEN). Without configuration, only internal time is recorded.", "Kredensial RFC3161 TSA diperlukan (GLHAC_TSA_URL, GLHAC_TSA_TOKEN opsional). Jika belum dikonfigurasi, hanya waktu internal yang dicatat."],
  "audit.pii_off": ["PII 암호화 미적용 — 프로덕션은 GLHAC_ENCRYPTION=1 + 전용 키(GLHAC_ENC_KEY) 권장", "PII encryption is off. GLHAC_ENCRYPTION=1 and a dedicated GLHAC_ENC_KEY are recommended for production.", "Enkripsi PII nonaktif. GLHAC_ENCRYPTION=1 dan GLHAC_ENC_KEY khusus disarankan untuk produksi."],
  "audit.headers": ["X-Frame-Options·X-Content-Type-Options·Referrer-Policy·CSP 전 응답 적용(HSTS는 HTTPS)", "X-Frame-Options, X-Content-Type-Options, Referrer-Policy and CSP apply to all responses; HSTS requires HTTPS.", "X-Frame-Options, X-Content-Type-Options, Referrer-Policy dan CSP diterapkan pada semua respons; HSTS memerlukan HTTPS."],
  "audit.psre_unset": ["미설정 — 벤더(PrivyID/VIDA/Peruri/Tilaka) 자격증명 확보 시 연동(미설정 시 내부 무결성 서명 폴백)", "Not configured. Integration requires vendor credentials; internal integrity signatures are currently used.", "Belum dikonfigurasi. Integrasi memerlukan kredensial vendor; tanda tangan integritas internal digunakan saat ini."],
  "audit.secret_default": ["기본 시크릿 사용 중 — 프로덕션 부팅은 차단됨(enforce_secret). GLHAC_SECRET 설정 필요", "Default secret in use. Production startup is blocked by enforce_secret; set GLHAC_SECRET.", "Rahasia bawaan digunakan. Startup produksi diblokir oleh enforce_secret; atur GLHAC_SECRET."],
  "audit.secret_custom": ["커스텀 GLHAC_SECRET 설정됨 — 프로덕션 부팅 허용", "Custom GLHAC_SECRET configured; production startup is allowed.", "GLHAC_SECRET khusus telah diatur; startup produksi diizinkan."],
  "audit.rate_limit": ["로그인 전용 제한 + write 엔드포인트 per-actor 분당 {count}회(인메모리·단일프로세스)", "Login limits plus {count} writes per actor per minute (in memory, single process).", "Batas login ditambah {count} penulisan per aktor per menit (dalam memori, satu proses)."],
  "audit.domain_reason": ["도메인사전 {code} · 증빙 {evidence}", "Domain dictionary {code} · Evidence {evidence}", "Kamus domain {code} · Bukti {evidence}"],
  "audit.doc_type_reason": ["문서유형 {type} (tier={tier})", "Document type {type} (tier={tier})", "Jenis dokumen {type} (tier={tier})"],
  "audit.legacy_key": ["이전 서명 체계", "Previous signing scheme", "Skema tanda tangan sebelumnya"],
  "audit.verified_prefix": ["현재 서명 키로", "With the current signing key", "Dengan kunci penandatanganan saat ini"],
  "audit.verified_suffix": ["검증 · 위·변조 흔적 없음.", "verified · No evidence of tampering.", "terverifikasi · Tidak ada tanda manipulasi."],
  "audit.existing_20": ["보안 응답 헤더", "Security response headers", "Header respons keamanan"],
  "audit.existing_21": ["레이트리밋", "Rate limit", "Batas laju"],
  "audit.existing_22": ["PSrE 공인 전자서명 연동", "PSrE accredited e-signature integration", "Integrasi tanda tangan elektronik terakreditasi PSrE"],
  "audit.existing_23": ["SIHALAL 제출 연동", "SIHALAL submission integration", "Integrasi pengiriman SIHALAL"],
  "audit.existing_24": ["TSA 신뢰 타임스탬프", "TSA trusted timestamp", "Timestamp terpercaya TSA"],
  "audit.existing_25": ["감사 체인", "Audit chain", "Rantai audit"],
  "audit.existing_26": ["백업·복구", "Backup & recovery", "Cadangan & pemulihan"],
  "audit.existing_27": ["LPH 인정", "LPH recognition", "Pengakuan LPH"],
  "audit.existing_28": ["앱 시크릿", "App secret", "Rahasia aplikasi"],
  "audit.log_summary": ["위조 {tampered}건 · 이전 서명 체계 {legacy}건({epoch} 이전 기록, 현재 키로 검증 불가·위조 아님) · 선행로그 삭제로 인한 단절 {gaps}건 (검사 {checked}행)", "Tampered: {tampered} · Legacy signatures: {legacy} (before {epoch}; cannot verify with the current key, not classified as tampering) · Deleted predecessors: {gaps} (checked {checked} rows)", "Dimanipulasi: {tampered} · Tanda tangan lama: {legacy} (sebelum {epoch}; tidak dapat diverifikasi dengan kunci saat ini, bukan indikasi manipulasi) · Pendahulu terhapus: {gaps} ({checked} baris diperiksa)"],
  "audit.first_break": ["첫 단절 위치: {id}", "First broken link: {id}", "Tautan terputus pertama: {id}"],
  "audit.tampered_rows": ["위조 의심 행: {ids}", "Suspected tampered rows: {ids}", "Baris yang diduga dimanipulasi: {ids}"],
  "audit.deleted_rows": ["선행로그 삭제 단절: {ids}", "Links broken by deleted predecessors: {ids}", "Tautan terputus karena pendahulu dihapus: {ids}"],
  "audit.subset": ["부분집합(케이스 범위) 검증 — 감사체인은 전역이므로 행 자기무결성만 대조한다. 선행 로그가 삭제된 단절은 deleted_predecessor로 별도 보고.", "Case subset verification checks each row's integrity; the audit chain is global. Deleted predecessors are reported separately.", "Verifikasi subset kasus memeriksa integritas setiap baris; rantai audit bersifat global. Pendahulu yang dihapus dilaporkan terpisah."],
  "audit.clean": ["무결", "Intact", "Utuh"],
  "audit.range_clean": ["검증 구간 무결", "Verified range intact", "Rentang terverifikasi utuh"],
  "audit.range_verified": ["현재 서명 키로 {count}건 검증 · 위·변조 흔적 없음.", "{count} records verified with the current key · No evidence of tampering.", "{count} catatan diverifikasi dengan kunci saat ini · Tidak ada tanda manipulasi."],
  "audit.legacy_range": ["이전 서명 체계 {count}건 ({date} 까지)", "Legacy signatures: {count} (through {date})", "Tanda tangan lama: {count} (hingga {date})"],
  "audit.legacy_reason": ["{date} 서명 키 변경 이전에 기록된 내역이라 현재 키로는 다시 검증할 수 없습니다. 위·변조로 판정하지 않으며, 기록 자체는 원본 그대로 보존돼 있습니다.", "These records predate the signing key change on {date} and cannot be verified with the current key. They are not classified as tampered, and the original records are preserved.", "Catatan ini dibuat sebelum perubahan kunci pada {date} dan tidak dapat diverifikasi dengan kunci saat ini. Catatan tidak dianggap dimanipulasi dan data asli tetap dipertahankan."],
  "audit.broken": ["무결성 깨짐", "Integrity broken", "Integritas rusak"],
  "audit.broken_reason": ["서명 키 변경({date}) 이후 기록 중 서명이 맞지 않는 항목이 있습니다 (기록 {id}…). 운영 책임자에게 즉시 보고하세요.", "A signature mismatch was found after the key change ({date}), at record {id}…. Report this to the operations manager immediately.", "Ketidakcocokan tanda tangan ditemukan setelah perubahan kunci ({date}), pada catatan {id}…. Segera laporkan kepada penanggung jawab operasional."],
  "audit.pii_on": ["PII 컬럼 AES-256-GCM 봉투암호화 적용(GLHAC_ENCRYPTION=1)", "PII columns use AES-256-GCM envelope encryption (GLHAC_ENCRYPTION=1).", "Kolom PII menggunakan enkripsi amplop AES-256-GCM (GLHAC_ENCRYPTION=1)."],
  "audit.pse_yes": ["전자시스템 사업자(PSE) 등록번호 보유", "Electronic system operator (PSE) registration number available.", "Nomor pendaftaran penyelenggara sistem elektronik (PSE) tersedia."],
  "audit.pse_no": ["PSE 등록번호(GLHAC_PSE_REG_NO) 미설정 — Kominfo PSE 등록 필요", "PSE number (GLHAC_PSE_REG_NO) not configured; Kominfo PSE registration is required.", "Nomor PSE (GLHAC_PSE_REG_NO) belum diatur; pendaftaran PSE Kominfo diperlukan."],
  "audit.sihalal_yes": ["SIHALAL 제출 자격증명 설정됨", "SIHALAL submission credentials configured.", "Kredensial pengajuan SIHALAL telah diatur."],
  "audit.sihalal_no": ["미설정 — LP3H 등록·자격증명(GLHAC_SIHALAL_API_URL/TOKEN) 확보 시 실제 제출", "Not configured. Live submission requires LP3H registration and GLHAC_SIHALAL_API_URL/TOKEN credentials.", "Belum dikonfigurasi. Pengajuan nyata memerlukan pendaftaran LP3H dan kredensial GLHAC_SIHALAL_API_URL/TOKEN."],
  "audit.tsa_yes": ["RFC3161 TSA 연동 설정됨", "RFC3161 TSA integration configured.", "Integrasi RFC3161 TSA telah diatur."],
  "audit.tsa_no": ["미설정 — GLHAC_TSA_URL 확보 시 연동(미설정 시 내부 시각만 사용)", "Not configured. Set GLHAC_TSA_URL to enable integration; only internal time is currently recorded.", "Belum dikonfigurasi. Atur GLHAC_TSA_URL untuk integrasi; hanya waktu internal dicatat saat ini."],
  "audit.backup": ["현재 수동 .bak 스냅샷만 — 자동 백업·복구 파이프라인 구성 필요", "Only manual .bak snapshots are available; automatic backup and recovery must be configured.", "Hanya snapshot .bak manual tersedia; pencadangan dan pemulihan otomatis perlu dikonfigurasi."],
  "audit.lph_yes": ["LPH 인정번호 보유(GLHAC_LPH_ACCRED)", "LPH accreditation number available (GLHAC_LPH_ACCRED).", "Nomor akreditasi LPH tersedia (GLHAC_LPH_ACCRED)."],
  "audit.lph_no": ["LPH 인정번호(GLHAC_LPH_ACCRED) 미설정 — BPJPH/KAN 인정 필요", "LPH accreditation number (GLHAC_LPH_ACCRED) not configured; BPJPH/KAN accreditation is required.", "Nomor akreditasi LPH (GLHAC_LPH_ACCRED) belum diatur; akreditasi BPJPH/KAN diperlukan."]
};
for(const [key,values] of Object.entries(UIV4_AUDIT_I18N)){
  ['ko','en','id'].forEach((lang,i)=>{ I18N[lang][key]=values[i]; });
  for(const [i,lang] of ['en','id'].entries()) I18N_DOM[lang][values[0]]=values[i+1];
}
function UIV4_systemText(value){
  const text=String(value||'');
  const key=Object.keys(UIV4_AUDIT_I18N).find(k=>UIV4_AUDIT_I18N[k][0]===text);
  if(key) return t(key);
  if(text===UIV4_I18N['docs.ai_extraction_note'][0]) return t('docs.ai_extraction_note');
  let m=text.match(/^도메인사전 (.*?)(?:·증빙 (.*))?$/);
  if(m) return tf('audit.domain_reason',{code:m[1],evidence:m[2]||'—'});
  m=text.match(/^문서유형 (.*) \(tier=(.*)\)$/);
  if(m) return tf('audit.doc_type_reason',{type:m[1],tier:m[2]});
  m=text.match(/^로그인 전용 제한 \+ write 엔드포인트 per-actor 분당 (\d+)회/);
  if(m) return tf('audit.rate_limit',{count:m[1]});
  return typeof I18D_trUI==='function'?I18D_trUI(text):text;
}
delete I18D_RE_CACHE.en; delete I18D_RE_CACHE.id;
