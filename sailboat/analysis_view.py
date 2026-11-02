"""Research evidence projection; never used to authorize execution."""
import copy

ANALYSIS_CONTEXT_VERSION='analysis_only_v2'
FEEDBACK_NOTE=('Recorded outcomes of earlier proposals, not market evidence or current permissions. '
               'An accepted proposal is not a fill. Do not interpret rejection as a market signal.')

def pick(value,keys):
 return {k:copy.deepcopy(value[k]) for k in keys if k in value}

def analysis_history(notes):
 result=[]
 for n in notes[-8:]:
  item=pick(n,('request_id','at','action','plan_id','decision_author','candidate_policy'))
  if n.get('analysis_context_version')==ANALYSIS_CONTEXT_VERSION:
   item.update(pick(n,('reasoning','uncertainty','missing_information','premium_thesis','position_blocks')))
  else:
   item['history_note']='Earlier rationale predates analysis/execution separation; original retained in owner journal.'
  result.append(item)
 return result

def plan_view(row):
 return pick(row,('plan','status','filled'))

def execution_feedback(context,notes):
 result=[]
 for n in notes[-8:]:
  receipt=n.get('result') or {}
  if receipt.get('status') in {'REJECTED','PREVIEW_BLOCKED'}:
   item=pick(n,('request_id','at','action','plan_id'))
   item.update(outcome='REJECTED',reason=receipt.get('blocker') or receipt.get('reason') or receipt.get('error'),
               scope='proposal validation' if receipt['status']=='PREVIEW_BLOCKED' else 'submission')
   result.append(item)
 for key,row in context.get('active_plans',{}).items():
  if row.get('stop_reason') or row.get('last_error') or row.get('status') in {'REJECTED','FILLED','CANCELLED','EXPIRED'}:
   item={'plan_id':key,'plan_version':row.get('plan',{}).get('plan_version'),
         'outcome':row.get('status'),'filled':row.get('filled',0)}
   item.update(pick(row,('stop_reason','last_error')))
   result.append(item)
 for pid,row in context.get('profit_management',{}).items():
  if row.get('last_error'):result.append({'position_id':pid,'outcome':'PROFIT_MANAGEMENT_BLOCKED','reason':row['last_error']})
 return {'interpretation':FEEDBACK_NOTE,'recorded_outcomes':result[-20:]}

def project_analysis(request,notes=()):
 """Allowlist evidence; preserve the stored request and opaque authorization hash."""
 result=pick(request,('request_id','context_hash','created_at','reply_before','new_plan_id',
                     'operator_request','market_data_tool','decision_schema','order_contract','decision_id'))
 original=request['context']
 context=pick(original,('observed_at','account_number','buying_power'))
 context['position_theses']={p['position_id']:copy.deepcopy(original['position_theses'][p['position_id']]) for p in original.get('positions',[]) if p['position_id'] in original.get('position_theses',{})}
 context['positions']=[pick(p,('id','position_id','kind','symbol','quantity','unit_cost','cost_basis',
  'multiplier','pending_sell','opened_at','expiration_date','mark_bid','market_value','unrealized_pnl',
  'strike_price','option_type','option_id')) for p in original.get('positions',[])]
 context['pending_orders']=[pick(o,('id','state','symbol','chain_symbol','quantity','processed_quantity',
  'cumulative_quantity','pending_quantity','canceled_quantity','price','stop_price','type','trigger',
  'time_in_force','market_hours','legs','created_at','updated_at')) for o in original.get('pending_orders',[])]
 context['active_plans']={k:plan_view(v) for k,v in original.get('active_plans',{}).items()}
 context['shared_plans']={k:pick(v,('id','name','version','status','source','updated_at','plan','terms','reasoning')) for k,v in original.get('shared_plans',{}).items()}
 context['prior_plan_versions']=[plan_view(v) for v in original.get('prior_plan_versions',[])]
 result.update(context=context,analysis_context_version=ANALYSIS_CONTEXT_VERSION,
  previous_assessments=analysis_history(notes),execution_feedback=execution_feedback(original,notes))
 return result
