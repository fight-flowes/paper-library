document.addEventListener('DOMContentLoaded', () => {
  const sidebar=document.querySelector('.reader-sidebar');
  if(!sidebar)return;
  const toggle=document.querySelector('.sidebar-toggle'), scrim=document.querySelector('.sidebar-scrim');
  const narrow=matchMedia('(max-width:900px)');
  const toc=document.getElementById('toc-panel'), notes=document.getElementById('notes-panel');
  const tocButton=document.getElementById('show-toc'), notesButton=document.getElementById('open-notes');
  const drawer=document.getElementById('library-drawer');
  const drawerScrim=document.querySelector('.drawer-scrim');
  const results=document.getElementById('drawer-results'), searchStatus=document.getElementById('drawer-status');
  let request=null, desktopOpen=true, drawerOpener=null;
  function closeDrawer(focus=false){
    request?.abort();request=null;drawer.hidden=true;drawerScrim.hidden=true;
    if(focus)(drawerOpener?.getClientRects().length?drawerOpener:toggle).focus({preventScroll:true});
  }
  async function loadDrawer(url){
    request?.abort();const active=new AbortController();request=active;
    searchStatus.textContent='加载中…';results.replaceChildren();results.setAttribute('aria-busy','true');
    try {
      const response=await fetch(url,{signal:active.signal,credentials:'same-origin'});
      if(!response.ok)throw Error('加载失败，请重试');
      const parsed=new DOMParser().parseFromString(await response.text(),'text/html');
      const content=parsed.querySelector('main.reader-shell');
      if(!content)throw Error('内容暂不可用，请重试');
      if(request!==active)return;
      content.querySelectorAll('script, h1, .search-form').forEach(el=>el.remove());
      content.querySelector('.workspace-nav')?.remove();
      results.replaceChildren(...[...content.children].map(el=>document.importNode(el,true)));
      searchStatus.textContent='';
    } catch(error){if(error.name!=='AbortError'&&request===active)searchStatus.textContent=error.message||'加载失败，请重试';}
    finally{if(request===active)results.removeAttribute('aria-busy');}
  }
  function openDrawer(opener){
    drawerOpener=opener;drawer.hidden=false;drawerScrim.hidden=false;
    if(narrow.matches)openSidebar(false);
    loadDrawer('/reading');document.getElementById('close-drawer').focus({preventScroll:true});
  }
  document.getElementById('close-drawer').addEventListener('click',()=>closeDrawer(true));
  drawerScrim.addEventListener('click',()=>closeDrawer(true));
  document.querySelectorAll('.open-reading-records').forEach(button=>button.addEventListener('click',()=>openDrawer(button)));
  results.addEventListener('click',event=>{if(event.target.closest('a[href*="/paper/"]'))closeDrawer();});
  function openSidebar(open) {
    if(open&&narrow.matches)closeDrawer();
    sidebar.hidden=!open;
    toggle.setAttribute('aria-expanded',String(open));
    toggle.setAttribute('aria-pressed',String(open));
    toggle.setAttribute('aria-label',open?'收起阅读栏':'展开阅读栏');
    document.body.classList.toggle('sidebar-collapsed',!open);
    scrim.hidden=!(open && narrow.matches);
    if(!narrow.matches)desktopOpen=open;
  }
  function syncPanel() {
    const showing=notes && !notes.hidden;
    toc.hidden=!!showing;
    tocButton.setAttribute('aria-pressed',String(!showing));
    notesButton?.setAttribute('aria-pressed',String(!!showing));
  }
  toggle.addEventListener('click',()=>{
    const anchor=[...document.querySelectorAll('.paper-body > [id]')].find(el=>el.getClientRects().length && el.getBoundingClientRect().bottom>100);
    const before=anchor?.getBoundingClientRect().top;
    openSidebar(sidebar.hidden);
    if(anchor&&!narrow.matches)window.scrollBy({top:anchor.getBoundingClientRect().top-before,behavior:'instant'});
    if(narrow.matches&&!sidebar.hidden)tocButton.focus();
  });
  scrim.addEventListener('click',()=>{openSidebar(false);toggle.focus();});
  tocButton.addEventListener('click',()=>{if(notes)notes.hidden=true;syncPanel();});
  if(notes)new MutationObserver(()=>{syncPanel();if(!notes.hidden)openSidebar(true);}).observe(notes,{attributes:true,attributeFilter:['hidden']});
  sidebar.addEventListener('click',event=>{if(narrow.matches && event.target.closest('.toc-link,.note-quote'))openSidebar(false);});
  document.addEventListener('keydown',event=>{
    if(event.key==='Escape'&&!drawer.hidden){event.preventDefault();closeDrawer(true);return;}
    if(event.key==='Tab'&&!drawer.hidden){
      const items=[...drawer.querySelectorAll('a[href],button,input')].filter(el=>el.getClientRects().length&&!el.disabled);
      if(event.shiftKey&&document.activeElement===items[0]){event.preventDefault();items.at(-1).focus();}
      else if(!event.shiftKey&&document.activeElement===items.at(-1)){event.preventDefault();items[0].focus();}
      return;
    }
    if(event.key==='Escape') { document.querySelector('.reading-more')?.removeAttribute('open'); if(narrow.matches&&!sidebar.hidden){openSidebar(false);toggle.focus();} }
    if(event.key==='Tab'&&narrow.matches&&!sidebar.hidden){
      const focusable=[toggle,...sidebar.querySelectorAll('a[href],button,textarea')].filter(el=>el.getClientRects().length&&!el.disabled);
      const first=focusable[0],last=focusable.at(-1);
      if(event.shiftKey&&document.activeElement===first){event.preventDefault();last.focus();}
      else if(!event.shiftKey&&document.activeElement===last){event.preventDefault();first.focus();}
    }
  });
  narrow.addEventListener('change',()=>openSidebar(narrow.matches?false:desktopOpen));
  openSidebar(!narrow.matches);syncPanel();
  document.addEventListener('click',event=>{const more=document.querySelector('.reading-more');if(more&&!more.contains(event.target))more.open=false;});
  // Collapse only explicitly classified publication metadata; never guess from paragraph length.
  const children=[...document.querySelector('.paper-body')?.children || []];
  const firstBody=children.findIndex(el=>el.matches('.structure-abstract,.structure-body'));
  const publications=children.slice(0,firstBody<0?0:firstBody).filter(el=>el.matches('.structure-publication'));
  if(publications.length>1){
    const details=document.createElement('details');details.className='publication-details';
    const summary=document.createElement('summary');summary.textContent='出版信息与相关链接';details.append(summary);
    publications[0].before(details);
    for(const block of publications){const next=block.nextElementSibling;details.append(block);if(next?.matches('.paragraph-translation'))details.append(next);}
    const revealHash=()=>{let id;try{id=decodeURIComponent(location.hash.slice(1));}catch{return;}const target=document.getElementById(id);if(target&&details.contains(target))details.open=true;};
    revealHash();window.addEventListener('hashchange',revealHash);
  }
});
