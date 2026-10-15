(()=>{'use strict';
const excluded=new Set(['search','discover','charts','stream','you','upload','settings','tracks','albums','reposts','likes','spotlight','sets','pages','stations']);
const processed=new WeakSet();
let scheduled=false;
function classify(href){try{const u=new URL(href,location.origin);if(!['soundcloud.com','www.soundcloud.com'].includes(u.hostname))return null;const bits=u.pathname.split('/').filter(Boolean);if(bits.length===3&&bits[1]==='sets')return {kind:'playlist',url:u.origin+u.pathname};if(bits.length===2&&!excluded.has(bits[0])&&!excluded.has(bits[1]))return {kind:'track',url:u.origin+u.pathname};return null}catch{return null}}
function eligible(a,meta){if(!meta||!a.textContent?.trim()||a.textContent.trim().length>150)return false;if(a.closest('header,nav,footer,.scweb-download,[role="navigation"]'))return false;
const klass=(a.className||'').toString();const parent=a.closest('.trackItem,.sound,.soundList__item,.userStreamItem,.searchItem,.playlistItem,.soundTitle,.playlist__title,.trackList__item,.soundList');
return Boolean(parent||/soundTitle__title|trackItem__trackTitle|playlist__title/.test(klass)||location.pathname===new URL(a.href).pathname)}
function add(a,meta){if(processed.has(a))return;processed.add(a);if(a.parentNode?.querySelector(':scope > .scweb-download'))return;
const b=document.createElement('button');b.type='button';b.className='scweb-download';b.textContent=meta.kind==='playlist'?'Download playlist':'Download';b.title=meta.kind==='playlist'?'Open the playlist track selector':'Queue this exact SoundCloud track';
b.addEventListener('click',(e)=>{e.preventDefault();e.stopImmediatePropagation();b.disabled=true;b.textContent='Queued…';chrome.runtime.sendMessage({type:meta.kind==='playlist'?'open_playlist':'enqueue',url:meta.url},res=>{if(chrome.runtime.lastError||!res?.ok){b.textContent='Set up extension';setTimeout(()=>{b.disabled=false;b.textContent=meta.kind==='playlist'?'Download playlist':'Download'},2500)}else{b.textContent=meta.kind==='playlist'?'Opening…':'Queued ✓';setTimeout(()=>{b.disabled=false;b.textContent=meta.kind==='playlist'?'Download playlist':'Download'},3500)}})} ,true);
a.insertAdjacentElement('afterend',b)}
function scan(){scheduled=false;const anchors=document.querySelectorAll('a[href*="soundcloud.com/"],a[href^="/"]');let count=0;for(const a of anchors){if(++count>1400)break;if(processed.has(a))continue;const meta=classify(a.href);if(eligible(a,meta))add(a,meta)} }
function defer(){if(scheduled)return;scheduled=true;setTimeout(scan,400)}
const observer=new MutationObserver(defer);observer.observe(document.documentElement,{childList:true,subtree:true});defer();
})();
