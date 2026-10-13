"""Display-only redaction; never mutate configuration or collector bytes."""
import os
import re

SECRET = re.compile(r'password|passwd|secret|token|api[_-]?key', re.I)
ASSIGN = re.compile(r'(?i)((?:password|passwd|secret|token|api[_-]?key)\s*[=:]\s*)([^\s;]+)')
FLAG = re.compile(r'(?i)^[-/]*(?:password|passwd|secret|token|api[_-]?key)$')
MASK = '***REDACTED***'


def redact(value):
    secrets = {v for k,v in os.environ.items() if SECRET.search(k) and v}
    def gather(item):
        if isinstance(item,dict):
            for key,child in item.items():
                if SECRET.search(str(key)) and isinstance(child,str) and child and not str(key).endswith('_env'):
                    secrets.add(child)
                if str(key).endswith('_env') and isinstance(child,str) and os.environ.get(child):
                    secrets.add(os.environ[child])
                gather(child)
        elif isinstance(item,list):
            for child in item:gather(child)
    gather(value)
    def text(item):
        for secret in sorted(secrets,key=lambda s:(-len(s),s)):
            if item==secret:return MASK
            if len(secret)>=4:item=item.replace(secret,MASK)
        item=ASSIGN.sub(lambda m:m[1]+MASK,item)
        return re.sub(r'(https?://)[^/@\s]+:[^/@\s]+@',r'\1'+MASK+'@',item)
    def walk(item):
        if isinstance(item,dict):
            return {k:MASK if SECRET.search(str(k)) else walk(v) for k,v in item.items()}
        if isinstance(item,list):
            result=[];sensitive=False
            for child in item:
                result.append(MASK if sensitive else walk(child))
                sensitive=isinstance(child,str) and bool(FLAG.fullmatch(child))
            return result
        return text(item) if isinstance(item,str) else item
    return walk(value)
