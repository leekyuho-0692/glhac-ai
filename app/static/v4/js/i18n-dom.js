/* ===== v4 본문 3개 국어(KO/EN/ID) 런타임 번역기 ===== */
const I18D_HANGUL = /[가-힣]/;
const I18D_RE_CACHE = {};
const I18D_UNITS = { en:{'개사':' companies','건':' items','명':' people','개':'','종':' types','회':' times','일':' days','차':' round','장':' pages','분':' min','시간':' hr','원':' KRW','점':' pts'}, id:{'개사':' perusahaan','건':' item','명':' orang','개':'','종':' jenis','회':' kali','일':' hari','차':' putaran','장':' halaman','분':' mnt','시간':' jam','원':' KRW','점':' poin'} };

function I18D_dict(){
  if(S.lang === 'ko') return null;
  if(typeof I18N_DOM === 'undefined' || !I18N_DOM) return null;
  return I18N_DOM[S.lang] || null;
}

function I18D_escRe(s){
  return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

function I18D_re(lang){
  if(I18D_RE_CACHE[lang]) return I18D_RE_CACHE[lang];
  if(typeof I18N_DOM === 'undefined' || !I18N_DOM) { I18D_RE_CACHE[lang] = null; return null; }
  const dict = I18N_DOM[lang];
  if(!dict) { I18D_RE_CACHE[lang] = null; return null; }
  const keys = Object.keys(dict).filter(k => k.length >= 2).sort((a, b) => b.length - a.length);
  if(!keys.length) { I18D_RE_CACHE[lang] = null; return null; }
  const re = new RegExp('(?<![가-힣])(?:' + keys.map(I18D_escRe).join('|') + ')(?![가-힣])', 'g');
  I18D_RE_CACHE[lang] = re;
  return re;
}

function I18D_tr(text){
  if(typeof text !== 'string' || !text) return text;
  if(!I18D_HANGUL.test(text)) return text;
  const dict = I18D_dict();
  if(!dict) return text;
  const m = text.match(/^(\s*)([\s\S]*?)(\s*)$/);
  if(!m) return text;
  const [, pre, body, post] = m;
  if(body && Object.prototype.hasOwnProperty.call(dict, body)){
    return pre + dict[body] + post;
  }
  const lang = S.lang;
  const unitMap = I18D_UNITS[lang];
  let out = text;
  if(unitMap){
    out = out.replace(/(\d[\d,.]*)\s*(개사|시간|건|명|개|종|회|일|차|장|분|원|점)(?![가-힣])/g, (mm, n, u) => n + (I18D_UNITS[lang][u] ?? u));
  }
  const re = I18D_re(S.lang);
  if(!re) return out;
  return out.replace(re, mm => (Object.prototype.hasOwnProperty.call(dict, mm) ? dict[mm] : mm));
}

function I18D_apply(root){
  if(!root || S.lang === 'ko') return;
  const dict = I18D_dict();
  if(!dict) return;
  try{
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, null, false);
    const textNodes = [];
    let n;
    while((n = walker.nextNode())) textNodes.push(n);
    textNodes.forEach(node => {
      const parent = node.parentNode;
      if(!parent) return;
      const tag = parent.nodeName;
      if(tag === 'SCRIPT' || tag === 'STYLE' || tag === 'TEXTAREA' || tag === 'NOSCRIPT') return;
      if(parent.nodeType === 1 && parent.closest && parent.closest('[data-noi18n]')) return;
      const v = node.nodeValue;
      if(!v || !I18D_HANGUL.test(v)) return;
      const next = I18D_tr(v);
      if(next !== v) node.nodeValue = next;
    });
  }catch(e){ /* noop */ }

  try{
    if(root.querySelectorAll){
      root.querySelectorAll('[placeholder],[title],[aria-label]').forEach(el => {
        if(el.closest && el.closest('[data-noi18n]')) return;
        ['placeholder','title','aria-label'].forEach(attr => {
          const v = el.getAttribute && el.getAttribute(attr);
          if(v && I18D_HANGUL.test(v)){
            const next = I18D_tr(v);
            if(next !== v) el.setAttribute(attr, next);
          }
        });
      });
    }
  }catch(e){ /* noop */ }
}

let I18D_observer = null;
let I18D_targets = [];
let I18D_started = false;

function I18D_flush(){
  RS._i18dPending = false;
  if(I18D_observer) I18D_observer.disconnect();
  I18D_targets.forEach(t => { try{ I18D_apply(t); }catch(e){} });
  if(I18D_observer) I18D_targets.forEach(t => I18D_observer.observe(t, {childList:true, subtree:true, characterData:true}));
}

function I18D_schedule(){
  if(S.lang === 'ko') return;
  if(RS._i18dPending) return;
  RS._i18dPending = true;
  if(typeof requestAnimationFrame === 'function') requestAnimationFrame(I18D_flush);
  else setTimeout(I18D_flush, 16);
}

function I18D_start(){
  if(I18D_started) return;
  const ids = ['app','modal-root','toast'];
  const els = ids.map(id => document.getElementById(id)).filter(Boolean);
  if(!els.length){ document.addEventListener('DOMContentLoaded', I18D_start); return; }
  I18D_started = true;
  I18D_targets = els;

  try{
    const saved = localStorage.getItem('glhac_v4_lang');
    if(saved === 'en' || saved === 'id' || saved === 'ko'){
      S.lang = saved;
      try{ render(); }catch(e){}
    }
  }catch(e){ /* noop */ }

  try{
    I18D_observer = new MutationObserver(() => { I18D_schedule(); });
    I18D_flush();
  }catch(e){ I18D_apply(document.body); }
}

(function(){
  try{
    const g = App.setLang;
    App.setLang = function(l){
      try{ localStorage.setItem('glhac_v4_lang', l); }catch(e){}
      S.lang = l;
      render();
    };
  }catch(e){ /* noop */ }
})();

I18D_start();
document.addEventListener('DOMContentLoaded', I18D_start);
