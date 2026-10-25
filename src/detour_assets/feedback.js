/* No network requests, cookies, names, analytics, or third-party scripts. */
'use strict';
(() => {
  const KEY = 'news-cast:detour:feedback:v1';
  const status = document.getElementById('storage-status');
  const summary = document.getElementById('feedback-summary');
  const forms = [...document.querySelectorAll('form[data-feedback]')];
  const validDate = x => /^\d{4}-\d{2}-\d{2}$/.test(x);
  let data = {};
  try {
    const saved = JSON.parse(localStorage.getItem(KEY) || '{}');
    if (!saved || typeof saved !== 'object' || Array.isArray(saved)) throw Error();
    for (const [date, row] of Object.entries(saved)) {
      if (validDate(date) && row && typeof row === 'object') {
        data[date] = {
          liked: ['yes', 'neutral', 'no'].includes(row.liked) ? row.liked : '',
          again: ['yes', 'neutral', 'no'].includes(row.again) ? row.again : '',
          remembered: typeof row.remembered === 'string' ? row.remembered.slice(0, 160) : '',
          updatedAt: typeof row.updatedAt === 'string' ? row.updatedAt : ''
        };
      }
    }
  } catch (_) { status.textContent = '保存済み回答を読めませんでした。端末への保存が許可されているか確認してください。'; }
  const updateSummary = () => {
    const rows = Object.values(data);
    const metric = key => `${rows.filter(r => r[key] === 'yes').length}/${rows.filter(r => r[key]).length}`;
    summary.textContent = `回答した回数 ${rows.filter(r => r.liked || r.again || r.remembered).length}回 · 気に入った ${metric('liked')} · また聴きたい ${metric('again')} · 思い出メモ ${rows.filter(r => r.remembered.trim()).length}件（分母は各項目の回答数）`;
  };
  const restore = form => {
    const row = data[form.dataset.feedback] || {};
    for (const name of ['liked', 'again', 'remembered']) form.elements.namedItem(name).value = row[name] || '';
  };
  for (const form of forms) {
    restore(form);
    form.addEventListener('submit', event => {
      event.preventDefault();
      const date = form.dataset.feedback;
      if (!validDate(date)) return;
      const row = Object.fromEntries(['liked', 'again', 'remembered'].map(name => [name, form.elements.namedItem(name).value]));
      row.remembered = row.remembered.slice(0, 160);
      row.updatedAt = new Date().toISOString();
      const next = { ...data, [date]: row };
      try {
        localStorage.setItem(KEY, JSON.stringify(next));
        data = next;
        form.querySelector('output').textContent = '保存しました。あとから変更できます。';
        updateSummary();
      } catch (_) { form.querySelector('output').textContent = '端末に保存できません。回答のダウンロードを使ってください。'; data = next; updateSummary(); }
    });
  }
  document.getElementById('export-feedback').addEventListener('click', () => {
    const blob = new Blob([JSON.stringify({ program: 'morning-detour', version: 1, answers: data }, null, 2)], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a'); a.href = url; a.download = 'morning-detour-feedback.json'; a.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  });
  document.getElementById('clear-feedback').addEventListener('click', () => {
    if (!window.confirm('この端末に保存した実験の回答を削除しますか？')) return;
    try {
      localStorage.removeItem(KEY); data = {}; forms.forEach(restore); updateSummary();
      forms.forEach(form => { form.querySelector('output').textContent = ''; });
      status.textContent = '回答を削除しました。';
    } catch (_) { status.textContent = '回答を削除できませんでした。ブラウザーの保存設定を確認してください。'; }
  });
  updateSummary();
})();
