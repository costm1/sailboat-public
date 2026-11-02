"""Minimal adapter to lm-format-enforcer's public TokenEnforcer API.
Uses the tokenizer decoding convention documented by lm-format-enforcer (MIT):
https://github.com/noamgat/lm-format-enforcer
Avoids its legacy Transformers import path, removed in Transformers 5.20.
"""
from lmformatenforcer import TokenEnforcer,TokenEnforcerTokenizerData
def tokenizer_data(tokenizer):
    anchor=tokenizer.encode("0",add_special_tokens=False)[-1]
    special=set(tokenizer.all_special_ids)
    tokens=[]
    for ident in range(len(tokenizer)):
        if ident in special:continue
        contextual=tokenizer.decode([anchor,ident],clean_up_tokenization_spaces=False)[1:]
        isolated=tokenizer.decode([ident],clean_up_tokenization_spaces=False)
        tokens.append((ident,contextual,len(contextual)>len(isolated)))
    return TokenEnforcerTokenizerData(tokens,
        lambda ids:tokenizer.decode(ids,clean_up_tokenization_spaces=False).rstrip("\ufffd"),
        tokenizer.eos_token_id,False,len(tokenizer))
def prefix_function(data,parser):
    engine=TokenEnforcer(data,parser)
    return lambda batch_id,ids:engine.get_allowed_tokens(ids.tolist()).allowed_tokens
