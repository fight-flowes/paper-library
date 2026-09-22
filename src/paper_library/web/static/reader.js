document.addEventListener("DOMContentLoaded", () => {
  const guide = document.getElementById('reading-guide');
  document.querySelector('.guide-toolbar')?.addEventListener('click', () => {
    if (guide) guide.open = true;
  });
  if (location.hash === '#reading-guide' && guide) guide.open = true;
  guide?.querySelectorAll('[data-guide-section]').forEach(section => {
    const heading = document.getElementById(section.dataset.guideSection);
    if (!heading || !heading.closest('.paper-body')) return;
    const local = section.cloneNode(true);
    local.removeAttribute('data-guide-section');
    local.classList.add('section-reading-guide');
    if (heading.matches('.structure-title') || document.title.startsWith(heading.textContent.trim() + ' ·')) return;
    local.querySelector('summary').textContent = '本节导读';
    // Keep the heading's existing translation adjacent to its heading.
    const next = heading.nextElementSibling;
    (next?.matches('[data-translation-id]') ? next : heading).after(local);
  });
  const links = Array.from(
    document.querySelectorAll(".toc-link[data-heading-id]"),
  );

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
