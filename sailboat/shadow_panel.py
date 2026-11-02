"""Owner-only read projection; no assessment or trading actions."""
import copy

def shadow_views(state,limit=3):
 rows=sorted(state.get('shadow_decisions',{}).values(),key=lambda r:r.get('created_at',0),reverse=True)[:limit]
 out=[]
 for r in rows:
  v={k:copy.deepcopy(r.get(k)) for k in ('message_id','created_at','status','policy','policy_authority','extraction','agent_decision_unchanged','inferences','resolved_conditions','global_blockers','candidates','proposal','controller_preview','research_sha256','input_hash','would_submit','broker_submission','replay_source')}
  research=r.get('research',{});account=research.get('account',{})
  v['research']={k:copy.deepcopy(research.get(k)) for k in ('origin','asof','errors','reads','underlying','underlying_after','bars','instruments','quotes')}
  v['research']['account']={k:copy.deepcopy(account.get(k)) for k in ('buying_power','observed_at')}
  out.append(v)
 return out

def shape_views(state):
 rows=sorted(state.get('shape_recent',{}).values(),key=lambda r:r.get('created_at',0),reverse=True)[:6]
 return copy.deepcopy(rows)
