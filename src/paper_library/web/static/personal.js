/* Personal data is independent of generated paper content. */
document.addEventListener('DOMContentLoaded', () => {
  const root=document.querySelector('[data-personal-url]');
  if(!root)return;
  const endpoint=root.dataset.personalUrl, token=root.dataset.personalToken, version=root.dataset.version;
  const article=document.querySelector('.paper-body');if(!article)return;
  const panel=document.getElementById('notes-panel'), list=document.getElementById('notes-list');
  const status=document.getElementById('notes-status'), editor=document.getElementById('note-text');
  const selected=document.getElementById('selected-quote'), resume=document.getElementById('resume-reading');
  const norm=text=>text.replace(/\s+/gu,' ').trim();
  let selection=null, data=null, timer=null, interactive=false, restoring=false;
  let revision=null, saving=false, pending=false, conflicted=false, retries=0;
  const blocks=()=>Array.from(article.querySelectorAll('[id]')).filter(e=>!e.closest('.paragraph-translation') && e.getClientRects().length && norm(e.textContent));
  async function api(path='',method='GET',body=null){
    const response=await fetch(endpoint+path,{method,headers:{'Content-Type':'application/json','X-Management-Token':token},body:body?JSON.stringify(body):null});
    let value;try{value=await response.json();}catch{throw Error('服务暂不可用，请重试');}if(!response.ok){const error=Error(value.error||'保存失败，请重试');error.conflict=value.conflict;throw error;}return value;
  }
  const notify=message=>{status.textContent=message;};
  function blockBounds(block){
    const box=block.getBoundingClientRect(), next=block.nextElementSibling;
    const bottom=next?.matches('.paragraph-translation:not([hidden])')?next.getBoundingClientRect().bottom:box.bottom;
    return {top:box.top,bottom,height:bottom-box.top};
  }
  function current(){
    const all=blocks();return all.find(e=>blockBounds(e).bottom>100) || all.at(-1);
  }
  function selectedText(){
    const sel=window.getSelection();if(!sel || sel.isCollapsed || !sel.rangeCount)return null;
    const range=sel.getRangeAt(0), node=range.startContainer.nodeType===1?range.startContainer:range.startContainer.parentElement;
    const block=node.closest('[id]');
    if(!block || !article.contains(block) || !block.contains(range.endContainer) || node.closest('.paragraph-translation'))return null;
    const quote=norm(sel.toString());if(!quote)return null;
    const before=document.createRange();before.selectNodeContents(block);before.setEnd(range.startContainer,range.startOffset);
    const after=document.createRange();after.selectNodeContents(block);after.setStart(range.endContainer,range.endOffset);
    // Keep literal boundary spaces after normalizing the entire block.
    const full=norm(block.textContent), index=norm(before.toString()).length;
    const start=full.indexOf(quote,index);
    if(start<0)return null;
    return {anchor:block.id,quote,prefix:Array.from(full.slice(0,start)).slice(-60).join(''),suffix:Array.from(full.slice(start+quote.length)).slice(0,60).join(''),document_version:version};
  }
  function openPanel(){selection=selectedText();selected.textContent=selection?selection.quote:'';panel.hidden=false;load();}
  document.getElementById('open-notes').addEventListener('click',openPanel);
  document.addEventListener('keydown',e=>{if(e.key==='Escape'&&!e.defaultPrevented)panel.hidden=true;});
  function jump(anchor,offset=0){
    const block=document.getElementById(anchor);if(!block)return;
    const folded=block.closest('details');if(folded)folded.open=true;
    restoring=true;const box=blockBounds(block);window.scrollTo({top:window.scrollY+box.top+offset*box.height-100,behavior:'instant'});
    setTimeout(()=>{restoring=false;},800);
  }
  function highlightRanges(){
    if(!window.CSS?.highlights || typeof Highlight==='undefined')return;
    const ranges=[];
    for(const note of data?.notes||[]){
      if(!note.resolved_anchor || note.kind==='bookmark')continue;
      const block=document.getElementById(note.resolved_anchor);if(!block)continue;
      const walker=document.createTreeWalker(block,NodeFilter.SHOW_TEXT), chars=[], positions=[];
      let node,space=false;
      while(node=walker.nextNode())for(let i=0;i<node.data.length;i++){
        const c=node.data[i];if(/\s/u.test(c)){if(chars.length && !space){chars.push(' ');positions.push([node,i]);}space=true;}
        else {chars.push(c);positions.push([node,i]);space=false;}
      }
      const text=chars.join('');let index=-1,start=0;
      while((start=text.indexOf(note.quote,start))>=0){
        if((!note.prefix||text.slice(0,start).endsWith(note.prefix))&&(!note.suffix||text.slice(start+note.quote.length).startsWith(note.suffix))){index=start;break;}start++;
      }
      if(index<0 || !positions[index+note.quote.length-1])continue;
      const range=document.createRange();range.setStart(...positions[index]);const [last,offset]=positions[index+note.quote.length-1];range.setEnd(last,offset+1);ranges.push(range);
    }
    CSS.highlights.set('reader-notes',new Highlight(...ranges));
  }
  function render(){
    list.replaceChildren();
    for(const note of data.notes){
      const item=document.createElement('article'), quote=document.createElement('button'), badge=document.createElement('small'), text=document.createElement('textarea');
      quote.type='button';quote.className='note-quote';quote.textContent=note.quote;quote.disabled=!note.resolved_anchor;
      quote.addEventListener('click',()=>jump(note.resolved_anchor));
      badge.textContent=note.status==='orphaned'?'原文已变化，位置待确认':note.status==='relocated'?'已按原文重新定位':({note:'笔记',bookmark:'位置标记',highlight:'高亮'}[note.kind]);
      text.value=note.body;text.setAttribute('aria-label','编辑笔记');text.rows=3;
      const save=document.createElement('button');save.textContent='保存修改';save.disabled=!token;
      save.addEventListener('click',async()=>{try{await api('/'+note.id,'PATCH',{body:text.value});notify('已保存');await load();}catch(e){notify(e.message);}});
      const remove=document.createElement('button');remove.textContent='删除';remove.disabled=!token;
      remove.addEventListener('click',async()=>{try{await api('/'+note.id,'DELETE');await load();}catch(e){notify(e.message);}});
      item.append(badge,quote,text,save,remove);list.append(item);
    }
    if(!data.notes.length)list.textContent='';
    highlightRanges();
  }
  async function load(){try{data=await api();if(revision===null)revision=data.progress?.revision||0;render();return data;}catch(e){notify(e.message);}}
  async function add(kind){
    let target=selection;
    if(!target){notify('请先选中同一段原文。');return;}
    try{await api('','POST',{...target,kind,body:editor.value});editor.value='';notify('已保存');await load();}catch(e){notify(e.message);}
  }
  for(const kind of ['note','highlight'])document.getElementById('add-'+kind).addEventListener('click',()=>add(kind));
  for(const name of ['wheel','touchstart','keydown'])window.addEventListener(name,()=>{interactive=true;},{passive:true});
  function progressPayload(){
    const block=current();if(!block)return null;const box=blockBounds(block);
    const all=blocks(), index=all.indexOf(block), weights=all.map(e=>norm(e.textContent).length);
    const offset=Math.max(0,Math.min(1,(100-box.top)/Math.max(1,box.height)));
    return {action:'progress',expected_revision:revision,anchor:block.id,quote:Array.from(norm(block.textContent)).slice(0,160).join(''),document_version:version,
      offset,percent:100*(weights.slice(0,index).reduce((a,b)=>a+b,0)+offset*weights[index])/Math.max(1,weights.reduce((a,b)=>a+b,0))};
  }
  async function saveProgress(){
    if(!token||!interactive||restoring||conflicted||revision===null)return;
    if(saving){pending=true;return;}
    const payload=progressPayload();if(!payload)return;
    saving=true;
    try{const result=await api('','POST',payload);revision=result.revision;retries=0;}
    catch(e){
      notify('阅读位置保存失败：'+e.message);
      if(e.conflict){
        conflicted=true;resume.hidden=false;resume.disabled=false;resume.textContent='恢复其他页面的最新位置';
        resume.onclick=async()=>{try{const value=await api();revision=value.progress?.revision||0;conflicted=false;
          if(value.progress?.resolved_anchor)jump(value.progress.resolved_anchor,value.progress.offset);
          resume.textContent='继续上次阅读';resume.onclick=()=>{if(value.progress?.resolved_anchor)jump(value.progress.resolved_anchor,value.progress.offset);};
        }catch(error){notify(error.message);}};
      }else if(retries<3){retries++;clearTimeout(timer);timer=setTimeout(saveProgress,1000*2**retries);}
    }finally{saving=false;if(pending){pending=false;clearTimeout(timer);timer=setTimeout(saveProgress,200);}}
  }
  window.addEventListener('scroll',()=>{clearTimeout(timer);timer=setTimeout(saveProgress,1000);},{passive:true});
  window.addEventListener('pagehide',()=>{
    clearTimeout(timer);if(!token||!interactive||restoring||saving||conflicted||revision===null)return;const payload=progressPayload();
    if(payload)fetch(endpoint,{method:'POST',keepalive:true,headers:{'Content-Type':'application/json','X-Management-Token':token},body:JSON.stringify(payload)}).catch(()=>{});
  });
  load().then(value=>{
    if(!value)return;
    const params=new URLSearchParams(location.search), note=value.notes.find(n=>n.id===params.get('note'));
    if(note){panel.hidden=false;if(note.resolved_anchor)jump(note.resolved_anchor);else notify('该标注已无法定位，内容仍保留。');}
    else if(value.progress){resume.hidden=false;resume.textContent=value.progress.resolved_anchor?'继续上次阅读':'上次位置已失效';resume.disabled=!value.progress.resolved_anchor;resume.onclick=()=>jump(value.progress.resolved_anchor,value.progress.offset);}
    if(params.get('resume')==='1' && value.progress?.resolved_anchor)jump(value.progress.resolved_anchor,value.progress.offset);
    if(params.get('lang')==='zh' && document.querySelector('.translation-toggle')?.getAttribute('aria-pressed')==='false')document.querySelector('.translation-toggle').click();
  });
});
