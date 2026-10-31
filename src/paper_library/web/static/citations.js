(() => {
  const data = document.getElementById('citation-data');
  if (!data) return;
  const citations = JSON.parse(data.textContent);
  const status = document.getElementById('citation-status');
  const dialog = document.getElementById('citation-fallback');
  let timer;
  document.querySelectorAll('[data-citation-format]').forEach(button => {
    button.addEventListener('click', async () => {
      const text = citations[button.dataset.citationFormat];
      clearTimeout(timer);
      status.textContent = '';
      try {
        await navigator.clipboard.writeText(text);
        status.textContent = '已复制';
        timer = setTimeout(() => { status.textContent = ''; }, 2000);
      } catch (_) {
        const field = dialog.querySelector('textarea');
        field.value = text;
        dialog.showModal();
        field.focus();
        field.select();
      }
    });
  });
})();
