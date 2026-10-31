/* Reading preferences affect presentation only; no generated paper data is written. */
document.addEventListener('DOMContentLoaded', () => {
  const dialog = document.getElementById('reading-settings');
  const opener = document.getElementById('open-reading-settings');
  if (!dialog || !opener || !document.querySelector('.paper-body')) return;
  const key = 'paper-reader-type-v1';
  const size = document.getElementById('reading-font-size');
  const spacing = document.getElementById('reading-line-height');
  const allowedSizes = ['16', '18', '20', '22'], allowedSpacing = ['1.65', '1.8', '2'];
  function apply(values) {
    size.value = allowedSizes.includes(values.size) ? values.size : '18';
    spacing.value = allowedSpacing.includes(values.spacing) ? values.spacing : '1.8';
    document.documentElement.style.setProperty('--reader-font-size', size.value + 'px');
    document.documentElement.style.setProperty('--reader-line-height', spacing.value);
  }
  try { apply(JSON.parse(localStorage.getItem(key)) || {}); } catch { apply({}); }
  const save = () => {
    const body = document.querySelector('.paper-body');
    const anchor = [...body.children].find(el => el.getClientRects().length && el.getBoundingClientRect().bottom > 100);
    const top = anchor?.getBoundingClientRect().top;
    apply({size:size.value, spacing:spacing.value});
    if (anchor) window.scrollBy({top:anchor.getBoundingClientRect().top-top, behavior:'instant'});
    try { localStorage.setItem(key, JSON.stringify({size:size.value, spacing:spacing.value})); } catch {}
  };
  size.addEventListener('change', save);spacing.addEventListener('change', save);
  opener.addEventListener('click', () => {
    document.querySelector('.reading-more').open = false;dialog.showModal();
  });
  document.getElementById('reset-reading-settings').addEventListener('click', () => { size.value='18';spacing.value='1.8';save(); });
  dialog.addEventListener('close', () => document.querySelector('.reading-more > summary').focus({preventScroll:true}));
  dialog.addEventListener('click', event => { if (event.target === dialog) { const rect=dialog.getBoundingClientRect();if(event.clientX<rect.left||event.clientX>rect.right||event.clientY<rect.top||event.clientY>rect.bottom)dialog.close(); } });
});
