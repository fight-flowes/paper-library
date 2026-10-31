document.addEventListener("DOMContentLoaded", () => {
  const guide = document.getElementById('reading-guide');
  document.querySelector('.guide-toolbar')?.addEventListener('click', () => {
    if (guide) guide.open = true;
  });
  if (location.hash === '#reading-guide' && guide) guide.open = true;
  const sectionCache = new Map();
  function bindSection(section, id) {
    const content = section.querySelector('[data-guide-content]');
    async function load() {
      if (content.dataset.loaded || content.getAttribute('aria-busy') === 'true') return;
      content.setAttribute('aria-busy', 'true');
      content.textContent = '正在加载章节导读…';
      try {
        if (!sectionCache.has(id)) {
          const url = new URL(guide.dataset.guideUrl, location.origin);
          url.searchParams.set('section', id);
          url.searchParams.set('version', guide.dataset.documentVersion);
          sectionCache.set(id, fetch(url, {credentials:'same-origin', cache:'no-store'}).then(async response => {
            const data = await response.json();
            if (!response.ok) throw Error(data.error || '章节导读暂不可用，请重试。');
            if (data.document_version !== guide.dataset.documentVersion) throw Error('正文已更新，请刷新页面。');
            return data.html;
          }).catch(error => { sectionCache.delete(id); throw error; }));
        }
        content.innerHTML = await sectionCache.get(id);
        content.dataset.loaded = 'true';
      } catch (error) {
        const message = document.createElement('p'), retry = document.createElement('button');
        message.setAttribute('role', 'status');message.textContent = error.message || '加载失败，请重试。';
        retry.type = 'button';retry.textContent = '重新加载';retry.addEventListener('click', load);
        content.replaceChildren(message, retry);
      } finally { content.removeAttribute('aria-busy'); }
    }
    section.addEventListener('toggle', () => { if (section.open) load(); });
  }
  guide?.querySelectorAll('[data-guide-section]').forEach(section => {
    const id = section.dataset.guideSection;
    bindSection(section, id);
    const heading = document.getElementById(section.dataset.guideSection);
    if (!heading || !heading.closest('.paper-body')) return;
    const local = section.cloneNode(true);
    local.removeAttribute('data-guide-section');
    local.classList.add('section-reading-guide');
    if (heading.matches('.structure-title') || document.title.startsWith(heading.textContent.trim() + ' ·')) return;
    local.querySelector('summary').textContent = '本节导读';
    bindSection(local, id);
    // Keep the heading's existing translation adjacent to its heading.
    const next = heading.nextElementSibling;
    (next?.matches('[data-translation-id]') ? next : heading).after(local);
  });
  const links = Array.from(
    document.querySelectorAll(".toc-link[data-heading-id]"),
  );

  const search = document.getElementById('toc-search');
  const expand = document.getElementById('toc-expand');
  const tree = document.querySelector('.paper-toc');
  let previousOpen = null;
  search?.addEventListener('input', () => {
    const query = search.value.trim().toLocaleLowerCase();
    if (query && !previousOpen) previousOpen = new Map([...tree.querySelectorAll('details')].map(el => [el, el.open]));
    const matches = new Set(links.filter(link => link.textContent.toLocaleLowerCase().includes(query)));
    tree.querySelectorAll('li').forEach(item => {
      item.hidden = !!query && ![...item.querySelectorAll('.toc-link')].some(link => matches.has(link));
    });
    tree.querySelectorAll('.toc-region').forEach(region => {
      region.hidden = !!query && ![...region.querySelectorAll('.toc-link')].some(link => matches.has(link));
    });
    if (query) {
      for (const link of matches) for (let parent = link.parentElement; parent && parent !== tree; parent = parent.parentElement) {
        if (parent.matches('details')) parent.open = true;
      }
    } else if (previousOpen) {
      for (const [element, open] of previousOpen) element.open = open;
      previousOpen = null;
    }
    document.getElementById('toc-empty').hidden = !query || matches.size > 0;
    expand.disabled = !!query;
  });
  expand?.addEventListener('click', () => {
    const open = expand.getAttribute('aria-expanded') !== 'true';
    tree.querySelectorAll('details').forEach(el => { el.open = open; });
    expand.setAttribute('aria-expanded', String(open));
    expand.textContent = open ? '收起全部' : '展开全部';
  });
  function revealHash() {
    let id;try { id = decodeURIComponent(location.hash.slice(1)); } catch { return; }
    const link = links.find(item => item.dataset.headingId === id);
    if (!link) return;
    if (search?.value) { search.value = '';search.dispatchEvent(new Event('input')); }
    for (let parent = link.parentElement; parent && parent !== tree; parent = parent.parentElement) {
      if (parent.matches('details')) parent.open = true;
    }
  }
  window.addEventListener('hashchange', revealHash);revealHash();

  if (links.length === 0 || !("IntersectionObserver" in window)) {
    return;
  }

  const linksByHeading = new Map();
  for (const link of links) {
    const headingId = link.dataset.headingId;
    const heading = headingId ? document.getElementById(headingId) : null;
    if (heading) {
      linksByHeading.set(heading, link);
    }
  }

  if (linksByHeading.size === 0) {
    return;
  }

  const setActive = (activeLink) => {
    for (const link of links) {
      const active = link === activeLink;
      link.classList.toggle("is-active", active);
      if (active) {
        link.setAttribute("aria-current", "location");
      } else {
        link.removeAttribute("aria-current");
      }
    }
  };

  setActive(linksByHeading.values().next().value);

  const observer = new IntersectionObserver(
    (entries) => {
      const visible = entries
        .filter((entry) => entry.isIntersecting)
        .sort((left, right) => left.boundingClientRect.top - right.boundingClientRect.top);
      if (visible.length > 0) {
        setActive(linksByHeading.get(visible[0].target));
      }
    },
    { rootMargin: "-15% 0px -70% 0px" },
  );

  for (const heading of linksByHeading.keys()) {
    observer.observe(heading);
  }
});
