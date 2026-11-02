'use strict';

// Browser appearance only. Storage may be unavailable in private/embedded sessions.
(() => {
 const key='sailboat-theme', root=document.documentElement;
 const toggle=document.getElementById('themeToggle');
 const apply=value=>{
  const dark=value!=='light';root.dataset.theme=dark?'dark':'light';
  toggle.textContent=dark?'Dark':'Light';
  toggle.setAttribute('aria-pressed',String(dark));
  toggle.title=dark?'Switch to light mode':'Switch to dark mode';
  document.querySelector('meta[name="theme-color"]').content=dark?'#0d1b20':'#f3f5f2';
 };
 let saved;try{saved=localStorage.getItem(key);}catch(_){}
 apply(saved);
 toggle.addEventListener('click',()=>{
  const value=root.dataset.theme==='dark'?'light':'dark';apply(value);
  try{localStorage.setItem(key,value);}catch(_){}
 });
 window.addEventListener('storage',event=>{if(event.key===key||event.key===null)apply(event.newValue);});
})();

const el=id=>document.getElementById(id);
let csrf='',lastSeen=0,ready=false,loading=false,changing=false,generation=0,verified=false;
let loginTried=false,armReview=null;
function loginAttempt(value){
 try{if(value===undefined)return loginTried||!!sessionStorage.getItem('sailboat-login-attempt');
  if(value)sessionStorage.setItem('sailboat-login-attempt','1');else sessionStorage.removeItem('sailboat-login-attempt');
 }catch(_){} if(value!==undefined)loginTried=value;return loginTried;
}
const when=x=>typeof x==='number'?new Date(x*1000).toLocaleString('en-US',{timeZone:'America/New_York',month:'short',day:'numeric',hour:'numeric',minute:'2-digit'})+' ET':'—';
function assessmentProgress(s,now){
 const phase={FETCHING:'Reading account and market data',DISPATCHED:'Claude is analyzing',RESPONSE_READY:'Validating Claude’s response'}[s.active_phase]||'Assessment in progress';
 if(!Number.isFinite(s.active_started_at)||!Number.isFinite(now))return phase;
 const seconds=Math.max(0,Math.floor(now-s.active_started_at));
 return `${phase} · ${seconds}s elapsed${seconds>(s.active_budget_seconds||110)?' · overdue; awaiting recovery':''}`;
}
const text=(id,value)=>{el(id).textContent=value;};
const outcome=r=>(r.order_status?'Order: '+r.order_status:r.candidate_status?'Entry: '+r.candidate_status:r.status||'Outcome unavailable')+(r.assembly_status?' · Assembly: '+r.assembly_status:'');
const money=x=>x!==null&&x!==undefined&&Number.isFinite(Number(x))?Number(x).toLocaleString('en-US',{style:'currency',currency:'USD'}):'Unavailable';
function renderInterpretation(item,now){
 const host=el('interpretation');host.replaceChildren();const v=item?.interpretation;
 const add=(parent,tag,value,cls)=>{const node=document.createElement(tag);node.textContent=value;if(cls)node.className=cls;parent.append(node);return node;};
 if(!v)return;
 const analyst=item.source==='Scheduled assessment'?'Claude':'the agent';
 add(host,'p',`Sailboat’s reading of ${analyst}’s analysis`,'eyebrow');
 const names={PROCESSING_FAILED:'Processing failed',PENDING:'Processing analysis',PREVIEW_VALIDATED:'Analysis received',ACCEPTED:'Entry accepted · awaiting broker outcome',BLOCKED:'Entry blocked',STAY_OUT:'Stay out',INVALIDATED:'Entry invalidated · no longer eligible',EXPIRED:'Entry window expired',FILLED:'Broker-confirmed fill',WAITING:'Entry queued',RECONCILING:'Checking broker outcome',WORKING:'Order working',WAITING_ASSEMBLY:'Waiting for complete analysis'};
 add(host,'p',names[v.status]||v.status||'Waiting for interpretation','interpretation-status');
 if(v.reason)add(host,'p',v.reason,'interpretation-reason');
 const grid=add(host,'div','','candidate-grid');
 for(const c of v.candidates||[]){
  const card=add(grid,'article','','candidate-tile'+(c.selected?' selected':''));
  add(card,'p',c.selected?'Selected by Sailboat':c.probability!==null&&c.probability!==undefined?'Extracted candidate':'Referenced contract','candidate-label');
  const heading=add(card,'div','','candidate-heading');add(heading,'strong',c.right.toUpperCase());
  add(heading,'span',c.probability!==null&&c.probability!==undefined?`${c.probability}%`:'—','probability');
  add(card,'p',`${c.symbol} $${Number(c.strike).toLocaleString('en-US')} strike`,'candidate-contract');
  add(card,'p',`Expires ${c.expiry}`,'muted');
  add(card,'p',money(c.source_cost_per_contract),'candidate-cost');
  add(card,'p',`per contract · ${analyst}’s quoted ask`,'muted');
  add(card,'p',c.horizon_seconds?`${c.horizon_seconds/60}-minute profitable resale estimate`:'Resale probability unresolved','candidate-horizon');
  if(c.probability_error)add(card,'p',c.probability_error,'muted');
 }
 if(!(v.candidates||[]).length)add(host,'p',item.held_position_blocks?.length?'Managing the existing holding; see the current HOLD / SELL assessment.':'No unambiguous contract was extracted.','muted');
 else if(!v.selection_confirmed)add(host,'p','Candidate figures are extracted from the source. Sailboat has not confirmed an entry selection.','muted');
 if(v.quantity){
  add(host,'p',`Sailboat size: ${v.quantity} contract${v.quantity===1?'':'s'}${v.entry_limit?` · planned limit ${money(v.entry_limit)} · ${money(Number(v.entry_limit)*100*v.quantity)} total premium`:''}`,'entry-sizing');
 }
 if(v.broker_quote){const q=v.broker_quote,age=now-q.asof;
  add(host,'p',`${age>=0&&age<=30?'Last broker quote':'Historical broker quote'}: bid ${money(q.bid)} / ask ${money(q.ask)} · ${when(q.asof)}`,'muted');
 }
 add(host,'p','Estimates are unverified. Quoted premium excludes fees. An accepted entry is not a fill.','muted');
}
function unknown(note){ready=false;el('arm').disabled=true;text('state','STATE UNKNOWN');el('state').className='state unknown';text('statusNote',note);if(el('healthBanner')){el('healthBanner').hidden=false;text('healthBanner',note);}for(const id of ['pauseEntries','cancelOrders','openExit','requestAnalysis','openSettings'])if(el(id))el(id).disabled=true;}
async function call(path,body){
 const r=await fetch(path,{method:body?'POST':'GET',credentials:'same-origin',redirect:'error',cache:'no-store',signal:AbortSignal.timeout(45000),headers:body?{'Content-Type':'application/json','X-Sailboat-CSRF':csrf}:{},body:body?JSON.stringify(body):undefined});
 const value=await r.json();if(!r.ok){const e=new Error(value.error||'Request failed');e.status=r.status;throw e;}return value;
}
function renderResponses(items){
 const host=el('responseHistory');host.replaceChildren();
 const add=(parent,tag,value,cls)=>{const node=document.createElement(tag);node.textContent=value;if(cls)node.className=cls;parent.appendChild(node);return node;};
 if(!items.length){add(host,'p','No agent submissions recorded yet.','muted');return;}
 for(const item of items.slice(0,3)){
  const card=document.createElement('article');card.className='agent-response';host.appendChild(card);
  add(card,'h3',`${item.action?item.action.toUpperCase():'ACTION NOT RECORDED'} · ${outcome(item.receipt)}`);
  add(card,'p',`${item.source} · ${when(item.received_at)}`,'muted');
  for(const c of item.interpretation?.candidates||[]){
   add(card,'p',`${c.selected?'Selected: ':''}${c.right.toUpperCase()} ${c.probability??'—'}${c.probability!==null&&c.probability!==undefined?'%':''} · ${c.symbol} $${c.strike} · ${c.expiry} · ${money(c.source_cost_per_contract)}/contract at the source ask`,'history-candidate');
  }
  const full=document.createElement('details');card.appendChild(full);add(full,'summary','Read analysis and receipt');
  add(full,'p',item.reasoning||'No explanation was saved with this submission. Only its decision and receipt are available.','agent-reasoning');
  if(item.uncertainty)add(full,'p','Uncertainty: '+item.uncertainty,'agent-reasoning muted');
  if(item.missing_information?.length)add(full,'p','Missing information: '+item.missing_information.join('; '),'agent-reasoning muted');
  if(item.receipt.blocker)add(card,'p','Processing blocked: '+item.receipt.blocker,'agent-reasoning muted');
  if(item.receipt.reason)add(card,'p',item.receipt.reason,'agent-reasoning muted');
  if(item.research_evidence){const research=document.createElement('details');card.appendChild(research);add(research,'summary','Captured research (unverified source claims; no order authority)');add(research,'pre',JSON.stringify(item.research_evidence,null,2));}
  add(full,'p',item.request_id,'receipt-id');
  const details=document.createElement('details');full.appendChild(details);add(details,'summary','Receipt details');add(details,'pre',JSON.stringify(item.receipt,null,2));
 }
}
function render(v){
 verified=true;loginAttempt(false);csrf=v.csrf;lastSeen=Date.now();el('access').hidden=true;el('panel').hidden=false;el('refresh').hidden=false;
 const c=v.control,s=v.scheduler||{},risk=c.daily_risk||{},m=s.market_hours||{};
 const live=c.mode==='LIVE_ARMED';ready=c.mode==='DISARMED'&&c.live_ready===true&&v.durable===true&&!!v.owner_policy?.arming_window?.expires_at;
 text('state',v.recovery_required?'STORAGE BLOCKED':live?'LIVE ARMED':c.mode==='DISARMED'?'DISARMED':'STATE UNKNOWN');el('state').className='state '+(live?'armed':c.mode==='DISARMED'?'disarmed':'unknown');
 text('statusNote',v.recovery_required?'Storage recovery required. New submissions are blocked; existing broker orders may remain active.':live?'Real-money execution enabled.':c.mode==='DISARMED'?'Live order submission is blocked.':'Check controller status before any action.');
 text('account',c.account||'—');text('cadence',s.interval_seconds?`${s.provider} · every ${(s.effective_interval_seconds||s.interval_seconds)/60} min`:'Not configured');
 text('market',m.open?'Regular session open':m.reason==='MARKET_CLOSED'?'Market closed':'Unavailable');
 text('next',s.active_phase?assessmentProgress(s,v.observed_at):when(m.open?s.next_due:Math.max(m.next_open||0,s.next_due||0)||null));
 text('risk',risk.available?`$${risk.pnl_usd} / $${risk.max_loss_usd}`:'Unavailable');text('riskLock',risk.entry_locked?'Entries blocked'+(risk.available?'':' - risk data unavailable'):risk.available?'Not locked':'Unknown');
 text('plans',String(c.open_plans??'—'));text('updated',when(v.observed_at));text('blockers',(c.blockers||[]).join(' · '));text('hosting',c.hosting||s.hosting||'Host availability unknown.');
 const d=v.latest_decision;el('decisionEmpty').hidden=!!d;el('decisionContent').hidden=!d;
 if(d){
  text('decisionAction',d.action?d.action.toUpperCase():'ACTION NOT RECORDED');
  text('decisionOutcome',outcome(d.receipt));
  text('decisionWhen',`${d.source} · received ${when(d.received_at)}`);
  text('decisionId',d.request_id);text('decisionReceipt',JSON.stringify(d.receipt,null,2));
  text('latestAnalysis',d.reasoning||'No analysis text saved.');renderInterpretation(d,v.observed_at);
 }
 renderOverview(v);
 renderResponses(v.recent_decisions||[]);
 renderShadow(v.shadow_decisions||[]);
 renderConsole(v);
 el('arm').disabled=!ready||changing;el('disarm').disabled=v.recovery_required===true;
 el('arm').title=v.owner_policy?.arming_window?.expires_at?'Authorize the displayed regular session':v.owner_policy?.arming_window?.error||'Verified trading session unavailable';
}
async function refresh(){
 if(loading)return;loading=true;const g=generation;
 try{const v=await call('/owner/view');if(g===generation)render(v);}
 catch(e){if(g!==generation)return;unknown('Status could not be verified.');text('message',e.message);
  if(e.status===401){csrf='';text('message','');text('statusNote','Verifying your Hugging Face owner session. Your browser may require opening the controls directly.');el('access').hidden=false;el('refresh').hidden=true;el('disarm').disabled=true;
   if(!location.search.includes('locked')&&!loginAttempt()){
    loginAttempt(true);
    try{if(window.top===window.self)location.replace('/owner/login');else window.top.location.href=new URL('/owner/login',location.href).href;}
    catch(_){text('statusNote','Your browser requires one click to verify your Hugging Face identity.');}
   }
  }
 }finally{loading=false;}
}
el('refresh').onclick=()=>refresh();
el('arm').onclick=()=>{if(!ready||Date.now()-lastSeen>30000)return;armReview={revision:deskState.owner_policy.revision,...deskState.owner_policy.arming_window};el('confirmText').value='';el('confirmArm').disabled=true;el('confirm').showModal();el('confirmText').focus();};
el('confirmText').oninput=()=>{el('confirmArm').disabled=el('confirmText').value!=='ARM LIVE';};
el('cancelArm').onclick=()=>el('confirm').close();
async function control(command){
 if(command==='arm'&&(!ready||Date.now()-lastSeen>30000||el('confirmText').value!=='ARM LIVE')){el('confirm').close();return;}
 generation++;changing=true;ready=false;el('arm').disabled=true;el('confirm').close();text('message',command==='arm'?'Checking readiness…':'Disarm requested…');
 try{const v=await call('/owner/action',command==='arm'?{command:'arm_session',confirmation:'ARM LIVE',expected_revision:armReview.revision,session_starts_at:armReview.starts_at,session_expires_at:armReview.expires_at}:{command});text('message',v.mode==='LIVE_ARMED'?'LIVE ARMED confirmed.':v.mode==='DISARMED'?'DISARMED confirmed.':'Action accepted; refresh status to verify.');}
 catch(e){unknown('Action outcome is not confirmed.');text('message',e.message+' Refresh status before taking another action.');}
 finally{changing=false;if(!loading)await refresh();}
}
el('confirmArm').onclick=()=>control('arm');el('disarm').onclick=()=>control('disarm');
el('logout').onclick=async()=>{try{await call('/owner/logout',{});location.replace('/?locked=1');}catch(e){text('message',e.message);}};
setInterval(()=>{if(lastSeen&&Date.now()-lastSeen>30000)unknown('Last verified status is stale. Refresh to check the controller.');if(!document.hidden&&!changing)refresh();},15000);
refresh();

let deskState=null,settingsRevision=0,dialogTask=null,alertSnapshot=null;
const dom=(parent,tag,value,cls)=>{const n=document.createElement(tag);if(value!==undefined)n.textContent=value;if(cls)n.className=cls;parent.append(n);return n;};
const button=(parent,label,fn,cls='text-button')=>{const b=dom(parent,'button',label,cls);b.type='button';b.onclick=fn;return b;};
const timeOnly=x=>Number.isFinite(x)?new Date(x*1000).toLocaleTimeString('en-US',{timeZone:'America/New_York',hour:'numeric',minute:'2-digit'})+' ET':'—';
const age=(x,now)=>!Number.isFinite(x)?'Not available':Math.max(0,now-x)<60?'Just now':`${Math.floor(Math.max(0,now-x)/60)} min ago`;
function freshControls(){if(!verified||!deskState?.durable||Date.now()-lastSeen>30000)throw new Error('Refresh to verify the controller before changing it.');}
async function ownerAction(body){freshControls();const result=await call('/owner/action',body);text('message',result.detail||result.effect||result.status||'Saved');await refresh();return result;}
function openAction(title,description,task,{inputLabel='',value='',confirmLabel='Confirm',loss=false}={}){
 text('actionTitle',title);el('actionBody').replaceChildren();dom(el('actionBody'),'p',description,'muted');text('actionError','');
 el('actionInputLabel').hidden=!inputLabel;text('actionInputText',inputLabel);el('actionInput').value=value;
 el('lossLabel').hidden=!loss;el('allowLoss').checked=false;text('confirmAction',confirmLabel);el('confirmAction').disabled=false;
 dialogTask=task;if(!el('actionDialog').open)el('actionDialog').showModal();
}
el('cancelAction').onclick=()=>el('actionDialog').close();
el('confirmAction').onclick=async()=>{el('confirmAction').disabled=true;try{await dialogTask(el('actionInput').value,el('allowLoss').checked);el('actionDialog').close();}catch(e){text('actionError',e.message);el('confirmAction').disabled=false;}};
function renderOverview(v){
 const o=v.overview||{},book=o.book,s=v.scheduler||{},now=v.observed_at,host=el('holdings');host.replaceChildren();
 const fresh=book&&now-book.observed_at<=30&&now>=book.observed_at,positions=book?.positions||[];
 text('activity',s.active_phase?assessmentProgress(s,now):o.orders?.length?'Orders are working. Broker confirmation determines fills.':v.control.mode==='DISARMED'?'Observing the account. Trading is disarmed.':'Waiting for the next assessment.');
 text('positionCount',`${positions.length} open`);
 if(!book||!positions.length){const empty=dom(host,'div','','empty-book');dom(empty,'div','◌','empty-mark');dom(empty,'h3',book?'Clear deck':'Reading your book');dom(empty,'p',book?'No open holdings in the last verified snapshot.':'Waiting for a verified broker snapshot.','muted');}
 for(const p of positions){
  const pid=p.position_id,manual=o.manual_holds?.[pid]?.active,savedTarget=v.owner_policy?.targets?.[pid],target=savedTarget&&JSON.stringify(savedTarget.position_epoch)===JSON.stringify(o.position_epochs?.[pid])?savedTarget:null,managed=o.management?.[pid],working=(o.orders||[]).find(r=>r.position_id===pid);
  const card=dom(host,'article','','position'),heading=dom(card,'div','','position-title');
  dom(heading,'strong',`${p.symbol} ${p.strike_price?'$'+p.strike_price+' ':''}${(p.option_type||p.kind||'option').toUpperCase()}`);dom(heading,'span',`${p.quantity} contract${p.quantity===1?'':'s'}`,'pill');
  dom(card,'p',p.expiration_date?`Expires ${p.expiration_date}`:'Broker-confirmed holding','muted');
  const stats=dom(card,'div','','position-stats');for(const [label,value] of [['Entry',money(Number(p.unit_cost)/(p.multiplier||100))],['Last bid',money(p.mark_bid)],['Target',target?money(target.limit_price):working?.side==='sell'?money(working.prices?.[0]):'—']]){const cell=dom(stats,'div');dom(cell,'span',label);dom(cell,'strong',value);}
  const ownership=dom(card,'div','','position-owner');dom(ownership,'span',manual?'You manage this position':'Sailboat manages this position');
  button(ownership,manual?'Hand back':'Take control',()=>openAction(manual?'Return management to Sailboat?':'Take manual control?',manual?'A fresh assessment is required before automatic management resumes.':'Automatic management pauses for this holding. Sailboat requests cancellation of its working orders and waits for broker confirmation.',async()=>ownerAction({command:manual?'resume_management':'take_control',position_id:pid}),{confirmLabel:manual?'Resume management':'Take control'}));
  const actions=dom(card,'div','','position-actions');
  if(p.kind==='option'){
   button(actions,'Set target',()=>openAction('Your exit target','Price per option share. This target takes priority over Claude while Sailboat manages this holding. Automatic targets must exceed purchase premium.',async value=>ownerAction({command:'set_target',position_id:pid,limit_price:value}),{inputLabel:'Limit price · $',value:target?.limit_price||'',confirmLabel:'Save target'}));
   if(target)button(actions,'Use Claude’s target',()=>openAction('Remove your target?','Return target selection to the existing Claude and fallback workflow.',async()=>ownerAction({command:'clear_target',position_id:pid}),{confirmLabel:'Remove target'}));
   button(actions,'Prepare close',()=>prepareClose(pid),'text-button danger-text');
  }
  dom(card,'p',manual?'Next: waiting for you. Automatic management stays paused.':working?`Next: ${working.status.toLowerCase().replaceAll('_',' ')} · ${working.filled} of ${working.quantity} filled.`:target?`Next: manage toward your ${money(target.limit_price)} limit when armed.`:managed?.source==='fallback'?'Next: fallback profit target; waiting for an executable bid.':'Next: a fresh assessment from Claude.','next-action');
  if(managed?.last_error)dom(card,'p',managed.last_error,'error');
 }
 if(book&&!fresh)dom(host,'p','Account snapshot is stale; prices are historical.','error');
 const orders=el('workingOrders');orders.replaceChildren();for(const r of o.orders||[]){const n=dom(orders,'div',`${r.side.toUpperCase()} ${r.quantity} ${r.symbol||''} · ${money(r.market_progress?.limit||r.prices[0])} limit · ${r.status} · ${r.filled} filled`,'order-row');if(r.last_error)dom(n,'p',r.last_error,'error');}
 const scores=(v.latest_decision?.held_position_blocks||[])[0]?.analyst_scores;text('preference',scores?.preference?`Claude’s preference: HOLD ${scores.preference.hold}% · SELL ${scores.preference.sell}%`:'');text('forecast',scores?.forecast?`Claude estimates ${scores.forecast.percent}% chance of a better bid within ${scores.forecast.minutes} minutes.`:'');
}
function renderConsole(v){
 deskState=v;const p=v.owner_policy,r=p?.values,s=v.scheduler||{},now=v.observed_at,book=v.overview?.book;
 const balance=v.overview?.account_summary,balanceFresh=balance&&now>=balance.observed_at&&now-balance.observed_at<=30;
 for(const[id,key]of [['accountValue','account_value'],['accountCash','cash'],['accountBuyingPower','buying_power']])text(id,money(balance?.[key]));text('balanceAsOf',balance?`${balanceFresh?'Broker snapshot':'Historical snapshot'} · ${when(balance.observed_at)}. Budget settings are separate.`:'Account balances unavailable; waiting for a verified broker snapshot.');text('settingsBuyingPower',balance?`Broker buying power: ${money(balance.buying_power)} · ${balanceFresh?'verified':'historical'} ${when(balance.observed_at)}. The amounts below are spending limits, not your balance.`:'Broker buying power unavailable. These amounts are spending limits, not your balance.');
 if(!r)return;
 const manageOnly=p.operating_mode==='manage_only',live=v.control.mode==='LIVE_ARMED',queued=live&&p.session?.starts_at>now,window=p.arming_window||{};
 text('arm',window.next_session?'Arm next session':'Arm for today');text('confirmArm',window.next_session?'Arm next session':'Arm for today');text('armTitle',window.next_session?'Arm Sailboat for the next session':'Arm Sailboat for today');
 text('state',v.recovery_required?'STORAGE BLOCKED':live?(queued?'ARMED · NEXT SESSION':manageOnly?'MANAGE ONLY':'LIVE ARMED'):v.control.mode==='DISARMED'?'DISARMED':'STATE UNKNOWN');
 text('statusNote',live?(queued?`Waiting until ${when(p.session.starts_at)}. No orders before that session.`:manageOnly?'New entries paused. Existing positions remain managed.':'Fresh decisions may place orders under your saved rules.'):'Observing only. No new live order submissions.');
 text('pauseEntries',manageOnly?'Allow entries':'Pause entries');
 text('healthClaude',s.active_phase?assessmentProgress(s,now):s.last_cycle?.status==='ANALYSIS_RECEIVED'?age(s.last_cycle.finished_at,now):s.last_cycle?.status?.replaceAll('_',' ')||'Not yet received');
 text('healthBroker',book&&now-book.observed_at<=30?'Verified '+age(book.observed_at,now):'Awaiting fresh snapshot');
 text('healthNext',s.active_phase?'In progress':s.market_hours?.open?timeOnly(s.next_due):'Next market session');
 text('healthSession',p.session&&live?timeOnly(p.session.expires_at):r.management_stop+' ET');
 text('sessionSummary',live&&p.session?'Armed until '+when(p.session.expires_at):'Owner console');
 text('scheduledCloseNote',r.scheduled_close_enabled?`Scheduled closing starts ${r.scheduled_close_time} ET, or earlier for broker/session deadlines. Includes loss-making exits; manual-control holdings are excluded.`:'');
 text('minimumBadge',`Minimum ${r.minimum_probability}%`);text('ruleProbability',`${r.minimum_probability}%`);text('ruleSize',r.sizing_mode==='analyst'?'Claude’s suggestion':r.sizing_mode==='contracts'?`${r.fixed_contracts} contract${r.fixed_contracts===1?'':'s'}`:money(r.trade_budget)+' budget');text('ruleCapital',money(r.capital_budget));text('ruleFallback',`+${r.fallback_profit_percent}% · ${r.fallback_misses} misses`);
 const candidates=v.latest_decision?.interpretation?.candidates||[],best=[...candidates].filter(x=>x.probability!=null&&Number.isFinite(Number(x.probability))).sort((a,b)=>b.probability-a.probability)[0];
 let comparison=el('currentThreshold');if(!comparison){comparison=dom(el('interpretation'),'p','','interpretation-reason');comparison.id='currentThreshold';}
 comparison.textContent=best?`Current minimum ${r.minimum_probability}% · Best source: ${best.right.toUpperCase()} ${best.probability}% · ${Number(best.probability)<r.minimum_probability?'Below threshold':candidates.filter(x=>Number(x.probability)===Number(best.probability)).length>1?'Tied — stay out':'Meets probability minimum'}. Fresh analysis and remaining checks still apply.`:'';
 const warnings=[...(v.control.blockers||[])];if(!v.durable)warnings.push('Storage is unavailable. State requires recovery.');if(p.closing_warning)warnings.push(p.closing_warning);for(const [pid,row] of Object.entries(p.scheduled_closes||{}))if(r.scheduled_close_enabled&&book?.positions?.some(x=>x.position_id===pid)&&row.last_error)warnings.push('Scheduled close: '+row.last_error);if(s.last_error)warnings.push('Assessment error: '+s.last_error);if(s.active_started_at&&now-s.active_started_at>(s.active_budget_seconds||110))warnings.push('Claude’s assessment is overdue.');
 el('healthBanner').hidden=!warnings.length;text('healthBanner',warnings.join(' · '));
 for(const id of ['pauseEntries','cancelOrders','openExit','requestAnalysis','openSettings'])el(id).disabled=!v.durable;
 text('armSummary',`${when(window.starts_at)} to ${when(window.expires_at)}. ${manageOnly?'Manage existing positions only':'Allow entries and position management'}. Entry window ${r.entry_start}–${r.entry_stop} ET; management ends ${r.management_stop} ET. Minimum entry probability ${r.minimum_probability}%. ${r.scheduled_close_enabled?`Scheduled closing at ${r.scheduled_close_time} ET (earlier for broker/session deadlines), including exits at a loss.`:'Scheduled closing is off.'}`);
 const activity=[...(p.activity||[]).map(x=>({at:x.at,title:x.kind.replaceAll('_',' ').toLowerCase(),detail:x.changes?Object.entries(x.changes).map(([k,v])=>`${k.replaceAll('_',' ')}: ${v.before} → ${v.after}`).join(' · '):x.mode||x.position_id||''})),...(v.overview?.order_history||[]).map(x=>({at:x.at,title:`${x.side.toUpperCase()} ${x.quantity} ${x.symbol} · ${x.status}`,detail:`${x.filled} broker-confirmed filled${x.error?' · '+x.error:''}`}))].sort((a,b)=>b.at-a.at).slice(0,35);
 text('activityCount',activity.length);const feed=el('activityFeed');feed.replaceChildren();if(!activity.length)dom(feed,'p','No recent activity.','muted');for(const item of activity){const row=dom(feed,'article','','activity-item');dom(row,'strong',item.title);dom(row,'p',when(item.at),'muted');if(item.detail)dom(row,'p',item.detail,'muted');}
 const signature=JSON.stringify([book?.positions?.map(x=>[x.position_id,x.quantity]),(v.overview?.orders||[]).map(x=>[x.plan_id,x.status,x.filled]),warnings]);
 try{if(alertSnapshot&&alertSnapshot!==signature&&typeof Notification!=='undefined'&&Notification.permission==='granted'&&sessionStorage.getItem('sailboat-alerts')==='on')new Notification('Sailboat update',{body:warnings.length?'Sailboat needs attention. Open the dashboard for details.':'Your holdings or order status changed. Open the dashboard to review.'});}catch{}alertSnapshot=signature;
}
function showSettings(){try{freshControls();const p=deskState.owner_policy;settingsRevision=p.revision;const form=el('settingsForm');setSettingsFields(form,p.values);
 const limits=deskState.control.limits||{};for(const [key,max]of Object.entries({fixed_contracts:limits.max_order_quantity,trade_budget:limits.max_order_notional,position_budget:limits.max_order_notional,capital_budget:limits.max_gross_notional}))if(max)form.elements.namedItem(key).max=max;
 text('settingsVersion',`Saved revision ${p.revision}${p.updated_at?' · '+when(p.updated_at):' · existing defaults'}`);text('settingsError','');const history=el('settingsHistory');history.replaceChildren();if(!p.history.length)dom(history,'p','Your saved changes will appear here.','muted');for(const item of p.history){const row=dom(history,'div','','settings-revision');dom(row,'span',`Revision ${item.revision} · ${when(item.replaced_at)}`);button(row,'Load values',()=>{setSettingsFields(form,{scheduled_close_enabled:false,scheduled_close_time:'15:45',...item.values});text('settingsVersion',`Loaded revision ${item.revision} as a draft. Save to apply.`);thresholdPreview();sizingFields();});}thresholdPreview();sizingFields();el('settingsDrawer').showModal();}catch(e){text('message',e.message);}}
function sizingFields(){const mode=el('settingsForm').elements.namedItem('sizing_mode').value;el('fixedContractsField').hidden=mode!=='contracts';el('tradeBudgetField').hidden=mode!=='budget';}
function thresholdPreview(){const floor=Number(el('minimum_probability').value),candidates=deskState?.latest_decision?.interpretation?.candidates||[],best=[...candidates].filter(x=>x.probability!==null&&x.probability!==undefined&&Number.isFinite(Number(x.probability))).sort((a,b)=>b.probability-a.probability)[0];text('thresholdPreview',best?`Latest source: ${best.right.toUpperCase()} ${best.probability}%. ${best.probability<floor?'Below this minimum.':'Meets this minimum; other entry checks still apply.'} Source estimates stay unchanged.`:'Claude’s estimates stay unchanged. Fresh entries must meet this minimum.');}
el('openSettings').onclick=showSettings;el('editRules').onclick=showSettings;el('closeSettings').onclick=()=>el('settingsDrawer').close();el('minimum_probability').oninput=thresholdPreview;el('settingsForm').elements.namedItem('sizing_mode').onchange=sizingFields;
el('settingsForm').onsubmit=async event=>{event.preventDefault();el('saveSettings').disabled=true;try{freshControls();const values={};for(const[k,v]of new FormData(el('settingsForm')))if(k!=='scheduled_close_enabled')values[k]=['entry_start','entry_stop','management_stop','sizing_mode','scheduled_close_time'].includes(k)?v:Number(v);values.scheduled_close_enabled=el('settingsForm').elements.namedItem('scheduled_close_enabled').checked;const result=await ownerAction({command:'save_settings',values,expected_revision:settingsRevision});settingsRevision=result.revision;el('settingsDrawer').close();}catch(e){text('settingsError',e.message);}finally{el('saveSettings').disabled=false;}};
for(const tab of document.querySelectorAll('[data-tab]'))tab.onclick=()=>{for(const b of document.querySelectorAll('[data-tab]')){const active=b===tab;b.classList.toggle('active',active);b.setAttribute('aria-selected',String(active));el('tab-'+b.dataset.tab).hidden=!active;}};
el('pauseEntries').onclick=()=>{const resume=deskState?.owner_policy?.operating_mode==='manage_only';openAction(resume?'Allow new entries?':'Pause new entries?',resume?'Fresh assessments may open new positions when armed and inside your entry window.':'Existing holdings remain managed. Unfilled entry remainder is cancelled; holdings are not sold.',async()=>ownerAction({command:'operating_mode',mode:resume?'automatic':'manage_only'}),{confirmLabel:resume?'Allow entries':'Pause entries'});};
el('requestAnalysis').onclick=async()=>{try{await ownerAction({command:'request_assessment'});}catch(e){text('message',e.message);}};
el('cancelOrders').onclick=()=>openAction('Cancel working orders?','Requests cancellation of Sailboat’s working orders. New entries pause; affected exits stay under manual control. Positions are not liquidated.',async value=>{if(value!=='CANCEL ORDERS')throw new Error('Type CANCEL ORDERS to confirm.');return ownerAction({command:'cancel_orders',confirmation:value});},{inputLabel:'Type CANCEL ORDERS',confirmLabel:'Request cancellation'});
async function prepareClose(pid){try{freshControls();if(deskState.control.mode!=='LIVE_ARMED')throw new Error('Choose manage-only mode and Arm for today before preparing an exit. This keeps new entries paused.');text('message','Reading a fresh exit quote…');const q=await ownerAction({command:'preview_close',position_id:pid});
 openAction('Confirm position close',`Sell ${q.quantity} ${q.symbol} $${q.strike} ${q.right.toUpperCase()}, expiring ${q.expiry}, at a ${money(q.limit_price)} limit. Estimated proceeds ${money(q.estimated_premium)}; P&L ${money(q.estimated_pnl_before_fees)} before fees. Preview expires ${timeOnly(q.expires_at)}. Broker review and fills are still required.`,async(value,loss)=>{if(value!=='CLOSE POSITION')throw new Error('Type CLOSE POSITION to confirm.');return ownerAction({command:'confirm_close',token:q.token,confirmation:value,allow_loss:loss});},{inputLabel:'Type CLOSE POSITION',confirmLabel:'Submit limit exit',loss:q.loss});
 }catch(e){text('message',e.message);}}
el('openExit').onclick=()=>{const positions=deskState?.overview?.book?.positions?.filter(p=>p.kind==='option')||[];openAction('Close a position','Take manual control first if a working order needs cancellation. Choose a holding to prepare a fresh limit exit; no order is sent until you confirm.',async()=>{});el('confirmAction').hidden=true;for(const p of positions)button(el('actionBody'),`${p.quantity} ${p.symbol} · ${p.option_type||'option'} · ${p.expiration_date||''}`,()=>{el('actionDialog').close();el('confirmAction').hidden=false;prepareClose(p.position_id);},'secondary');if(!positions.length)dom(el('actionBody'),'p','No verified option holdings.','muted');};
el('actionDialog').addEventListener('close',()=>el('confirmAction').hidden=false);
el('enableAlerts').onclick=async()=>{if(typeof Notification==='undefined'){text('message','Browser notifications are unavailable here. Activity remains visible in this tab.');return;}try{const permission=await Notification.requestPermission();if(permission==='granted'){sessionStorage.setItem('sailboat-alerts','on');text('enableAlerts','Browser alerts enabled');}else text('message','Notifications were not enabled. Activity is still recorded here.');}catch(e){text('message',e.message);}};

function setSettingsFields(form,values){for(const[k,v]of Object.entries(values)){const field=form.elements.namedItem(k);if(!field)continue;if(field.type==='checkbox')field.checked=v===true;else field.value=v;}}
