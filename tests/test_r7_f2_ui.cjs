/* Backend reasons rendered by the shipped v4 document panel; no frontend changes. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {spawnSync} = require('node:child_process');
const test = require('node:test');
const python = process.env.PYTHON || process.env.PY || '/Users/dany/IdeaProjects/llmdev/glhac-ai-v3/.venv/bin/python';
const result = spawnSync(python, ['-c', `
import json
from types import SimpleNamespace
from app import main as m
out = {}
for lang in ('ko', 'en', 'id'):
    reasons = []
    for expiry in ('2000-01-01', 'not-a-date'):
        doc = SimpleNamespace(fields={'expiry_date': expiry}, doc_type='halal_certificate',
            filename='Halal Certificate.pdf', text_full='HALAL CERTIFICATE', text_excerpt=None,
            document_id='ui-f2', content_b64='', confidence=.9)
        reasons += m._problem_document_reasons(doc)
    if 'EVIDENCE_SUPERSEDED' in m.PROBLEM_DOC_MSG:
        reasons.append({'code': 'EVIDENCE_SUPERSEDED', 'params': {'files': '<new certificate>.pdf'}})
    out[lang] = [{'code': r['code'], 'explanation': m._problem_document_explanation(r, lang)} for r in reasons]
print(json.dumps(out, ensure_ascii=False))
`], {cwd: path.join(__dirname, '..'), encoding: 'utf8', env: {...process.env, GLHAC_LLM_PROVIDER: 'none', GLHAC_ZIP_LOOKUP: '0'}});
assert.equal(result.status, 0, result.stderr);
const reasons = JSON.parse(result.stdout.trim());
const source = fs.readFileSync(path.join(__dirname, '../app/static/v4/js/problem-documents.js'), 'utf8');
const escape = v => String(v ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
for (const lang of ['ko', 'en', 'id']) {
  for (const code of ['CERT_EXPIRED', 'CERT_EXPIRY_UNKNOWN', 'EVIDENCE_SUPERSEDED']) {
    test(`${lang}: ${code} reaches the document panel`, () => {
      const reason = reasons[lang].find(r => r.code === code);
      assert.ok(reason, `${code}: missing backend guidance`);
      const I18N = {ko: {}, en: {}, id: {}};
      const context = vm.createContext({I18N, App: {}, AUTH: {role: 'auditor'}, S: {lang},
        RS: {problemDocs: {[`f2:${lang}`]: {count: 1, documents: [{filename: '<certificate>.pdf', uploaded_at: '2026-11-12', reasons: [reason]}]}}},
        esc: escape, jsq: escape, t: key => I18N[lang][key], render() {},
        apiFetch() { throw new Error('Unexpected fetch'); },
      });
      vm.runInContext(source, context);
      const html = vm.runInContext('PDN_panel("f2")', context);
      assert.ok(html.includes(escape(reason.explanation)));
      assert.ok(html.includes('&lt;certificate&gt;.pdf'));
      assert.ok(!html.includes('<new certificate>'));
      assert.match(html, /aria-live="polite"/);
    });
  }
}
