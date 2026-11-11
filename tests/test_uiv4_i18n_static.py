"""Reject hardcoded Korean in repaired views, including their dynamic JS modules.

Legacy files still use a shrinking debt baseline. Repaired renderers must use keys
even when a phrase appears in that baseline. Runtime tests additionally render
empty, loading and populated states without the asynchronous DOM translator.
"""
from collections import Counter
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
UI = ROOT / 'app/static/v4'
ALLOWLIST = Path(__file__).with_name('fixtures') / 'uiv4_hangul_allowlist.json'
DICTIONARIES = ('UIV4_I18N', 'ENTA_I18N', 'UIV4_AUDIT_I18N')
STRICT_REGIONS = {
    'js/ent-profile.js': [(None, None)],
    'index.html': [
        ("VIEWS['ent-apply'] =", 'App.entAddProduct ='),
        ("VIEWS['adm-assign'] =", 'App.selAssign ='),
    ],
}


def fragments(source):
    for name in DICTIONARIES:
        marker = 'const ' + name + ' = '
        if marker in source:
            a = source.index(marker) + len(marker)
            values, length = json.JSONDecoder().raw_decode(source[a:])
            assert all(isinstance(v, list) and len(v) == 3 for v in values.values()), name
            source = source[:a] + '{}' + source[a + length:]
    source = re.sub(r'/\*[\s\S]*?\*/|<!--[^>]*-->', '', source)
    source = re.sub(r'(?m)^\s*//.*$', '', source)
    return Counter(x.strip() for x in re.findall(r'[가-힣]+(?:[ \t·…—:()/0-9.,-]*[가-힣]+)*', source))


def ui_sources(root):
    sources = {}
    for file in sorted(root.rglob('*')):
        if file.suffix not in ('.html', '.js', '.css') or 'vendor' in file.parts or file.name == 'i18n-dict.js':
            continue
        sources[file.relative_to(root).as_posix()] = file.read_text()
    return sources


def violations(sources, allowed):
    failures = []
    for relative, source in sources.items():
        found = fragments(source)
        unexpected = found - Counter(allowed.get(relative, {}))
        failures.extend(f'{relative}: {text} (+{n})' for text, n in unexpected.items())
        for start, end in STRICT_REGIONS.get(relative, []):
            if start is None:
                section = source
            else:
                a = source.index(start)
                section = source[a:source.index(end, a)]
            failures.extend(f'{relative}: untranslated renderer: {text}' for text in fragments(section))
    return failures


def test_no_new_hardcoded_korean_outside_translation_dictionaries():
    allowed = json.loads(ALLOWLIST.read_text())['files']
    failures = violations(ui_sources(UI), allowed)
    assert not failures, '\n'.join(failures)


def test_legacy_allowlist_has_no_unused_entries_that_could_hide_reintroductions():
    sources = ui_sources(UI)
    allowed = json.loads(ALLOWLIST.read_text())['files']
    stale = {name: dict(Counter(values) - fragments(sources.get(name, '')))
             for name, values in allowed.items()}
    assert not {name: values for name, values in stale.items() if values}


def test_guard_rejects_a_new_ui_message_but_accepts_dictionary_entries():
    assert fragments("toast('새로 생긴 미번역 안내');") == {'새로 생긴 미번역 안내': 1}
    assert not fragments('const UIV4_I18N = {"key":["새 안내","New notice","Pemberitahuan baru"]};')


def test_guard_checks_dynamic_js_even_if_existing_words_were_allowlisted(tmp_path):
    js = tmp_path / 'js'
    js.mkdir()
    code = "const render = n => `<h2>기업 정보</h2><p>${n}명</p>`;"
    (js / 'ent-profile.js').write_text(code)
    sources = ui_sources(tmp_path)
    assert 'js/ent-profile.js' in sources
    allowed = {'js/ent-profile.js': dict(fragments(code))}
    assert violations(sources, allowed), 'A legacy allowance must not exempt a repaired renderer'
    assert violations({'js/new-panel.js': "el.textContent = '불러오는 중' + '…';"}, {})
    assert violations({'js/new-panel.js': 'el.innerHTML = `<p>${count}명</p>`;'}, {})
    assert not violations({'js/ent-profile.js': "el.innerHTML = `<p>${t('members.loading')}</p>`;"}, {})
