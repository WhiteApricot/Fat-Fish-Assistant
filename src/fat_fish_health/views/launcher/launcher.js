(async () => {
  const fragment = location.hash;
  history.replaceState(null, '', location.pathname);
  try {
    if (location.hostname !== '127.0.0.1' || !fragment.startsWith('#ffa-login=')) throw new Error();
    const state = JSON.parse(decodeURIComponent(fragment.slice(11)));
    if (!state.token || !state.user || state.expires * 1000 < Date.now() || state.expires * 1000 > Date.now() + 61000) throw new Error();
    const response = await fetch('/api/platform/stats', {headers: {Authorization: 'Bearer ' + state.token}, cache: 'no-store'});
    if (!response.ok || (await response.json()).status !== 'ok') throw new Error();
    localStorage.setItem('token', state.token);
    localStorage.setItem('user', state.user);
    const upgrade = !!state.password_upgrade_required, md5 = !!state.md5_pwd_hint;
    const change = !!state.change_pwd_hint || (md5 && !upgrade);
    for (const [key, value] of Object.entries({change_pwd_hint: change, md5_pwd_hint: md5 && !upgrade, password_upgrade_required: upgrade})) {
      if (value) localStorage.setItem(key, 'true'); else localStorage.removeItem(key);
    }
    location.replace(change || upgrade ? '/#/auth/setup' : '/#/dashboard/default');
  } catch (_) {
    document.getElementById('status').textContent = '自动登录链接已失效，请重新启动设置入口，或手动登录。';
    const link = document.createElement('a'); link.href = '/#/auth/login'; link.textContent = '手动登录 AstrBot'; document.body.appendChild(link);
  }
})();
