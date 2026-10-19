/* Password recovery before login; consultant sharing uses the current session. */
(() => {
  const actions = typeof App !== 'undefined' ? App : {};
  window.GLHACMail = actions;
  const errorText = async r => {
    try { const d = (await r.json()).detail; return d.message || '입력값 또는 메일 설정을 확인해 주세요.'; }
    catch (_) { return '요청에 실패했습니다. 잠시 후 다시 시도해 주세요.'; }
  };
  function recovery(token) {
    const dialog = document.createElement('dialog');
    dialog.style.cssText = 'max-width:460px;width:90%;padding:28px;border:1px solid #ccc;border-radius:16px';
    dialog.innerHTML = `<form><h2>${token ? '새 비밀번호 설정' : '비밀번호 재설정'}</h2>
      ${token ? '<label>새 비밀번호<input class="in" name="password" type="password" required minlength="8" maxlength="128" autocomplete="new-password"></label><label>새 비밀번호 확인<input class="in" name="confirm" type="password" required minlength="8" maxlength="128" autocomplete="new-password"></label>' : '<p>가입한 이메일로 30분 동안 유효한 재설정 링크를 보냅니다.</p><label>가입 이메일<input class="in" name="email" type="email" required maxlength="254" autocomplete="email"></label>'}
      <p role="status" style="white-space:pre-wrap"></p><button class="btn btn-primary" type="submit">${token ? '비밀번호 변경' : '재설정 메일 받기'}</button>
      <button class="btn" type="button">닫기</button></form>`;
    document.body.appendChild(dialog);
    dialog.addEventListener('close', () => dialog.remove());
    dialog.querySelector('[type=button]').onclick = () => dialog.close();
    dialog.querySelector('form').onsubmit = async e => {
      e.preventDefault();
      const f=e.currentTarget, message=f.querySelector('[role=status]'), button=f.querySelector('[type=submit]');
      if(button.disabled) return;
      if(token && f.elements.password.value !== f.elements.confirm.value) { message.textContent='비밀번호가 일치하지 않습니다.'; return; }
      button.disabled=true; message.textContent='처리 중…';
      try {
        const body=token ? {token, new_password:f.elements.password.value} : {email:f.elements.email.value.trim()};
        const r=await fetch('/auth/password-reset/'+(token?'confirm':'request'), {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
        if(!r.ok) { message.textContent=await errorText(r); return; }
        message.textContent=token ? '비밀번호를 변경했습니다. 새 비밀번호로 다시 로그인해 주세요.' : (await r.json()).message;
        if(token) { authClear(); button.hidden=true; dialog.addEventListener('close',()=>location.assign('/ui/v4/'),{once:true}); }
      } catch(_) { message.textContent='연결에 실패했습니다. 다시 시도해 주세요.'; }
      finally { button.disabled=false; }
    };
    dialog.showModal();
  }
  actions.emailReset=()=>recovery(null);
  actions.sendQrMail=async e=>{
    e.preventDefault();
    const form=e.currentTarget, button=form.querySelector('[type=submit]'), msg=form.querySelector('[role=status]');
    if(button.disabled) return;
    button.disabled=true; msg.textContent='발송 중…';
    try {
      const options={method:'POST',body:JSON.stringify({email:form.querySelector('input').value.trim()})};
      const r=typeof apiFetch==='function' ? await apiFetch('/consultant/qr/email',options)
        : await fetch('/consultant/qr/email',{...options,headers:{'Content-Type':'application/json',Authorization:'Bearer '+token()}});
      msg.textContent=r.ok ? '고객에게 QR 이미지와 가입 링크 주소를 발송했습니다.' : await errorText(r);
    } catch(_) { msg.textContent='발송에 실패했습니다. 다시 시도해 주세요.'; }
    finally { button.disabled=false; }
  };
  const token=new URLSearchParams(location.hash.slice(1)).get('reset');
  if(token) { history.replaceState(null,'',location.pathname+location.search); recovery(token); }
})();
