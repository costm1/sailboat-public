"use strict";
function renderShadow(items){
 const host=document.getElementById('shadowHistory');if(!host)return;host.replaceChildren();
 const add=(p,tag,v)=>{const n=document.createElement(tag);n.textContent=v;p.appendChild(n);return n;};
 const detail=(p,label,v)=>{const d=add(p,'details','');add(d,'summary',label);add(d,'pre',typeof v==='string'?v:JSON.stringify(v,null,2));};
 if(!items.length){add(host,'p','Waiting for a chat message or scheduled assessment.');return;}
 for(const r of items){
  const c=add(host,'article','');c.className='agent-response';
  add(c,'h3',r.status+' · '+r.source);
  add(c,'p',(r.live_execution_enabled?'CANDIDATE ROUTE ENABLED · Owner controls apply · ':'SHADOW ONLY · No orders · ')+new Date(r.created_at*1000).toLocaleString());
  if(r.recovered)add(c,'p','Recovered prior agent message; not a new assessment.').className='muted';
  const s=r.sorting||{};add(c,'p',s.original_text||r.error||'Text unavailable').className='agent-reasoning';
  const evidence=s.research_evidence;
  if(evidence){
   add(c,'h4','Captured research — source claims');
   add(c,'p','Research retained independently of entry eligibility. Prices are not refreshed quotes.');
   if(evidence.preferences?.length)add(c,'p','Current source preference: '+[...new Set(evidence.preferences.map(v=>v.value))].join(', '));
   for(const v of evidence.contracts||[])add(c,'p',(v.symbol||'Underlying unresolved')+' '+v.strike+' '+v.right.toUpperCase()+' · '+(v.expiry||'Expiry unresolved')+' · reported bid/ask '+(v.reported_bid??'?')+'/'+(v.reported_ask??'?')+' · '+v.quote_freshness);
   for(const v of evidence.scenarios||[])add(c,'p','Conditional '+v.value.right.toUpperCase()+': '+(v.value.symbol||'?')+' '+(v.value.operator==='gt'?'above':'below')+' $'+v.value.threshold+' · not evaluated; no monitor installed'+(v.unresolved?.length?' · '+v.unresolved.join(' '):''));
   detail(c,'Captured observations, estimates, proposed parameters and source evidence',evidence);
  }
  for(const b of s.blocks||[]){if(b.kind==='observation')continue;add(c,'p',b.kind+' → '+b.capability+' · '+b.status);}
  if(s.missing_information?.length)add(c,'p','Missing pieces: '+s.missing_information.join(' ')).className='muted';
  if(r.research_note)add(c,'p',r.research_note).className='muted';
  if(s.preferred_candidate?.status==='SELECTED'){const p=s.preferred_candidate.terms;add(c,'p','Preferred candidate: '+p.symbol+' '+p.strike+' '+p.right.toUpperCase()+' · '+p.expiry+' · owner policy evaluates independently of PASS');}
  if(r.candidate_entry){const e=r.candidate_entry;add(c,'p','Candidate entry: '+e.status+(e.limit_price?' · '+e.quantity+' contract at limit $'+e.limit_price:''));if(e.source_opinion_overridden)add(c,'p','Source recommended waiting; candidate evaluated under owner policy. This is not an agent BUY recommendation.');detail(c,'Candidate and controller result',e);}
  detail(c,'Sorted blocks and exact source evidence',s.blocks||[]);
  detail(c,'Unresolved meaning and source decision',{warnings:s.warnings,missing:s.missing_information,agent_decision:s.agent_stated_decisions});
  detail(c,'Read-only facts and lookup results',r.research);
  detail(c,'Controller preview — never submission',r.controller_preview||'No complete supported instruction to preview.');
  detail(c,'Receipt',{id:r.message_id,version:r.version,input_hash:r.input_hash,broker_submission:r.broker_submission});
 }
}
