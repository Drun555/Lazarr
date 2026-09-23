'use strict';
const $ = selector => document.querySelector(selector);
const config = JSON.parse($('#onboarding-config').textContent);
const names = ['Trawl', 'TMDB', 'Rutracker', 'Telegram'];
let current = Math.min(config.current || 0, 3);
const states = [...config.states];
let busy = false, timer, redirectTimer;

async function api(path, payload = {}) {
  const response = await fetch('/api/v1/onboarding/' + path, {
    method: 'POST', headers: {'Content-Type': 'application/json', 'X-CSRF-Token': $('meta[name="csrf-token"]').content},
    body: JSON.stringify(payload), signal: AbortSignal.timeout(120000),
  });
  if (response.status === 401) { location.assign('/login'); throw new Error('Войдите в аккаунт'); }
  const result = await response.json();
  if (!response.ok) throw new Error(Array.isArray(result.detail) ? result.detail.map(e => e.msg).join('; ') : result.detail || 'Не удалось выполнить запрос');
  return result;
}
function feedback(i, message = '', type = '') {
  const el = $('#screen-' + i + ' .feedback');
  if (el) { el.textContent = message; el.className = 'feedback ' + type; }
}
function render(focus = false) {
  document.querySelectorAll('.screen').forEach((el, i) => { el.hidden = current !== i; });
  $('.steps').hidden = current === 4;
  $('.steps').innerHTML = names.map((name, i) => `<button class="step ${i === current ? 'active' : ''} ${states[i] === 'success' ? 'done' : ''}" data-step="${i}" aria-label="Шаг ${i + 1}: ${name}" ${i === current ? 'aria-current="step"' : ''} ${busy ? 'disabled' : ''}><span></span></button>`).join('');
  document.querySelectorAll('[data-skip]').forEach((el, i) => {
    el.textContent = states[i] === 'success' ? (i === 3 ? 'Завершить' : 'Продолжить') : 'Позже';
    el.disabled = busy;
  });
  const button = $('[data-check="2"]');
  button.disabled = busy;
  button.textContent = busy && current === 2 ? 'Входим…' : states[2] === 'success' ? 'Продолжить' : 'Войти';
  document.querySelectorAll('input').forEach(el => { el.disabled = busy || (el.id === 'trawl-url' && states[0] === 'success'); });
  if (focus) $('#title-' + current).focus({preventScroll: true});
}
async function go(next, skip = false) {
  if (busy) return;
  clearTimeout(timer);
  const previous = current;
  busy = true; render();
  try {
    if (next === 4) {
      await api('complete');
      current = 4; busy = false; render(true);
      redirectTimer = setTimeout(() => location.assign('/'), 2000);
      return;
    }
    await api('progress', {current: next, skip: skip ? previous : null});
    if (skip && states[previous] !== 'success') states[previous] = 'skipped';
    current = next;
  } catch (error) { feedback(previous, error.message, 'error'); }
  finally { busy = false; render(true); }
}
function trawlStatus(status) {
  const badge = $('#trawl-badge');
  badge.innerHTML = status === 'loading' ? '<span class="spinner" aria-hidden="true"></span>' : status === 'success' ? '✓' : '!';
  badge.setAttribute('aria-label', status === 'loading' ? 'Проверяем Trawl' : status === 'success' ? 'Trawl доступен' : 'Trawl недоступен');
}
function validate(i) {
  if (i === 0) {
    try { const u = new URL($('#trawl-url').value.trim()); if (!['http:', 'https:'].includes(u.protocol) || u.username || u.password) throw Error(); }
    catch { return 'Введите адрес в формате http://host:8191.'; }
  }
  if (i === 1 && !/^[a-f0-9]{32}$/i.test($('#tmdb').value.trim())) return 'Введите API Key из 32 символов (0–9, a–f).';
  if (i === 2 && (!$('#rt-login').value.trim() || !$('#rt-password').value)) return 'Введите логин и пароль.';
  if (i === 3 && !/^\d+:[A-Za-z0-9_-]+$/.test($('#tg-token').value.trim())) return 'Проверьте формат токена: цифры, двоеточие и секретная часть.';
  return '';
}
async function check(i) {
  clearTimeout(timer);
  if (busy) return;
  if (states[i] === 'success' && i === 2) { await go(3); return; }
  const error = validate(i);
  if (error) { feedback(i, error, 'error'); if (i === 0) trawlStatus('error'); return; }
  const payload = [
    {url: $('#trawl-url').value.trim()},
    {api_key: $('#tmdb').value.trim()},
    {username: $('#rt-login').value.trim(), password: $('#rt-password').value},
    {token: $('#tg-token').value.trim()},
  ][i];
  busy = true; states[i] = 'loading';
  feedback(i, ['', 'Проверяем ключ…', 'Проверяем вход…', 'Проверяем токен…'][i], 'loading');
  if (i === 0) trawlStatus('loading');
  render();
  try {
    const result = await api(['trawl', 'tmdb', 'rutracker', 'telegram'][i], payload);
    if (i === 2 && result.status !== 'authenticated') {
      states[i] = 'pending';
      feedback(i, result.status === 'challenge' ? 'Трекер запросил дополнительную проверку. Повторите вход позже или пропустите этот шаг.' : result.message || 'Не удалось войти. Проверьте логин и пароль.', 'error');
      return;
    }
    states[i] = 'success';
    feedback(i, ['', 'Ключ принят', 'Вход выполнен', 'Токен добавлен'][i]);
    if (i === 0) { trawlStatus('success'); if (current === 0) current = 1; }
  } catch (error) {
    states[i] = 'error';
    feedback(i, error.name === 'TimeoutError' ? 'Проверка заняла слишком много времени. Попробуйте ещё раз.' : error.message, 'error');
    if (i === 0) trawlStatus('error');
  } finally { busy = false; render(i === 0); }
}

document.addEventListener('click', event => {
  const step = event.target.closest('[data-step]');
  if (step) go(Number(step.dataset.step));
  if (event.target.closest('[data-skip]')) go(current + 1, true);
  if (event.target.closest('[data-check]')) check(2);
  const reveal = event.target.closest('[data-reveal]');
  if (reveal) {
    const input = $('#' + reveal.dataset.reveal);
    input.type = input.type === 'password' ? 'text' : 'password';
    reveal.textContent = input.type === 'password' ? 'Показать' : 'Скрыть';
    reveal.setAttribute('aria-label', reveal.textContent + ' ' + (input.labels[0]?.textContent || input.getAttribute('aria-label')));
  }
});
document.querySelectorAll('input').forEach(input => {
  const i = Number(input.closest('.screen').id.split('-')[1]);
  input.addEventListener('input', () => {
    clearTimeout(timer);
    states[i] = 'pending'; feedback(i); render();
    if (i !== 2 && input.value.trim()) timer = setTimeout(() => check(i), 650);
  });
  input.addEventListener('keydown', event => {
    if (event.key === 'Enter') { event.preventDefault(); check(i); }
  });
});
$('#download').onclick = () => {
  const canvas = document.createElement('canvas'); canvas.width = canvas.height = 1024;
  const ctx = canvas.getContext('2d'); ctx.fillStyle = '#000'; ctx.fillRect(0, 0, 1024, 1024);
  ctx.fillStyle = '#fff'; ctx.fillRect(308, 246, 147, 532); ctx.fillRect(308, 647, 410, 131);
  canvas.toBlob(blob => {
    if (!blob) return;
    const url = URL.createObjectURL(blob), a = document.createElement('a'); a.href = url; a.download = 'lazarr-bot-avatar.png'; a.click();
    setTimeout(() => URL.revokeObjectURL(url), 3000);
  }, 'image/png');
};
window.addEventListener('pagehide', () => { clearTimeout(timer); clearTimeout(redirectTimer); });
$('#trawl-url').value = config.trawlUrl;
render();
if (current === 0) check(0);
else if (states[0] === 'success') trawlStatus('success');
