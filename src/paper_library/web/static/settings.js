(() => {
  'use strict';
  const forms = [...document.querySelectorAll('[data-settings-form]')];
  if (!forms.length) return;
  let busy = false;
  const dirty = new Set();
  const state = document.querySelector('[data-settings-state]');
  const updateState = () => {
    if (state.dataset.state === 'error') return;
    state.textContent = dirty.size ? '有修改待保存' : '当前配置已生效';
    state.dataset.state = dirty.size ? 'warning' : 'ok';
  };
  for (const form of forms) {
    const feedback = form.querySelector('.setting-feedback');
    const show = (message, state) => {
      feedback.textContent = message;
      feedback.dataset.state = state;
    };
    form.addEventListener('input', () => { dirty.add(form); updateState(); });
    async function send(test) {
      if (busy) return;
      form.querySelectorAll('input:invalid,select:invalid').forEach(input => {
        const details = input.closest('details');
        if (details) details.open = true;
      });
      if (!form.reportValidity()) return;
      busy = true;
      const data = new FormData(form);
      const buttons = forms.flatMap(item => [...item.querySelectorAll('button')]);
      const prior = buttons.map(button => button.disabled);
      buttons.forEach(button => { button.disabled = true; });
      form.setAttribute('aria-busy', 'true');
      show(test ? '正在检查连接…' : '正在验证并保存…', 'pending');
      try {
        const response = await fetch(test ? form.querySelector('[data-test-url]').dataset.testUrl : form.action,
          {method:'POST', body:data, headers:{Accept:'application/json'}, credentials:'same-origin'});
        const result = await response.json();
        if (!response.ok) throw new Error(result.error || '操作失败，请稍后重试');
        if (test) {
          const detail = result.gpu || result.model || '';
          show(`${result.message}${detail ? ' · ' + detail : ''}（${result.seconds} 秒）；当前输入尚未保存`, 'ok');
        } else {
          document.querySelectorAll('input[name="revision"]').forEach(input => { input.value = result.revision; });
          form.querySelectorAll('input[type="password"]').forEach(input => { input.value = ''; });
          form.querySelectorAll('input[name$="_clear"]').forEach(input => { input.checked = false; });
          dirty.delete(form);
          updateState();
          const group = data.get('group');
          for (const [name, value] of Object.entries(result.preferences[group])) {
            const input = form.elements.namedItem(name);
            if (input && input.type !== 'password' && input.type !== 'checkbox') input.value = String(value);
          }
          if (group === 'model') {
            const field = form.querySelector('[name="api_key"]').closest('.setting-field');
            field.querySelector('[data-setting-help]').textContent = result.preferences.model.api_key_configured ? '已配置，留空保留原密钥。' : '尚未配置；接口不要求认证时可留空。';
          }
          if (group === 'remote') {
            form.querySelector('[name="password"]').closest('.setting-field').querySelector('[data-setting-help]').textContent = result.preferences.remote.password_configured ? '已配置，留空保留原密码。' : '密码认证时填写；不会回显。';
          }
          show(result.message, 'ok');
        }
      } catch (error) {
        show(error instanceof SyntaxError ? '服务返回异常，请刷新页面后重试' : error.message || '连接中断，请刷新后确认是否已保存', 'error');
      } finally {
        buttons.forEach((button, index) => { button.disabled = prior[index]; });
        form.removeAttribute('aria-busy'); busy = false;
      }
    }
    form.addEventListener('submit', event => { event.preventDefault(); send(false); });
    form.querySelector('[data-test-url]')?.addEventListener('click', () => send(true));
  }
  addEventListener('beforeunload', event => {
    if (dirty.size || busy) { event.preventDefault(); event.returnValue = ''; }
  });
})();
