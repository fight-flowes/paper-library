/* Display precomputed translations only. This page never submits model requests. */
document.addEventListener("DOMContentLoaded", () => {
  const controls = document.querySelector("[data-translation-url]");
  if (!controls) return;
  const toggle = controls.querySelector(".translation-toggle");
  const refresh = controls.querySelector(".translation-refresh");
  const status = controls.querySelector(".translation-status");
  const slots = new Map(Array.from(document.querySelectorAll("[data-translation-id]"))
    .map((element) => [element.dataset.translationId, element]));
  let enabled = false;
  let generation = 0;
  let controller = null;

  function preserveReadingPosition(update) {
    const body = document.querySelector('.paper-body');
    const blocks = body ? Array.from(body.querySelectorAll('p, h1, h2, h3, h4, h5, h6, li, table, pre'))
      .filter(element => !element.closest('.paragraph-translation, .section-reading-guide')) : [];
    const visible = blocks.filter(element => {
      const rect = element.getBoundingClientRect();
      return rect.height > 0 && rect.bottom > 100 && rect.top < innerHeight;
    });
    const anchor = visible[0];
    const top = anchor?.getBoundingClientRect().top;
    update();
    if (anchor) {
      const delta = anchor.getBoundingClientRect().top - top;
      if (Math.abs(delta) > 0.5) window.scrollBy({ top: delta, behavior: 'instant' });
    }
  }

  async function loadSaved() {
    const current = ++generation;
    if (controller) controller.abort();
    controller = new AbortController();
    controls.setAttribute("aria-busy", "true");
    status.hidden = true;
    status.textContent = "正在加载已保存的译文…";
    refresh.disabled = true;
    try {
      const response = await fetch(controls.dataset.translationUrl, {
        signal: controller.signal, credentials: "same-origin", cache: "no-store",
      });
      let manifest;
      try { manifest = await response.json(); }
      catch { throw new Error("译文暂时无法读取，请稍后刷新。"); }
      if (!response.ok) throw new Error(manifest.error || "译文暂时无法读取。");
      if (current !== generation) return;
      if (manifest.document_version !== controls.dataset.documentVersion) {
        throw new Error("论文正文已更新，请刷新页面。");
      }
      let completed = 0;
      preserveReadingPosition(() => {
        for (const segment of manifest.segments) {
          const element = slots.get(segment.id);
          if (!element) throw new Error("段落已变化，请刷新页面。");
          if (segment.html !== null) {
            // Server escapes generated text and restores only protected original tokens.
            element.innerHTML = segment.html;
            element.hidden = !enabled;
            completed += 1;
          } else {
            element.textContent = "";
            element.hidden = true;
          }
        }
        const total = manifest.segments.length;
        status.hidden = !total || completed === total;
        status.textContent = !total ? "没有需要翻译的英文段落。"
          : !completed ? "暂无已保存的中文译文。"
          : completed < total ? `已显示 ${completed} / ${total} 段译文，其余尚未生成。`
          : "";
      });
    } catch (error) {
      if (current === generation && error.name !== "AbortError") {
        status.hidden = false;
        status.textContent = error.message || "译文读取失败，请稍后刷新。";
      }
    } finally {
      if (current === generation) {
        controls.setAttribute("aria-busy", "false");
        refresh.disabled = false;
      }
    }
  }

  toggle.addEventListener("click", () => {
    preserveReadingPosition(() => {
    enabled = !enabled;
    try { localStorage.setItem('paper-reader-parallel', String(enabled)); } catch {}
    toggle.setAttribute("aria-pressed", String(enabled));
    const label = enabled ? "关闭中英对照" : "中英对照";
    toggle.setAttribute("aria-label", label);
    toggle.title = label;
    refresh.hidden = !enabled;
    if (enabled) {
      loadSaved();
    } else {
      generation += 1;
      if (controller) controller.abort();
      controls.setAttribute("aria-busy", "false");
      refresh.disabled = false;
      for (const element of slots.values()) element.hidden = true;
      status.hidden = true;
      status.textContent = "已显示原文。";
    }
    });
  });
  refresh.addEventListener("click", loadSaved);
  try { if (localStorage.getItem('paper-reader-parallel') === 'true') toggle.click(); } catch {}
});
