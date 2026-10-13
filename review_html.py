"""Offline searchable evidence tables; all source values remain inert text."""
import html
import json


def render(value,title):
    esc=lambda v:html.escape(str(v))
    text=lambda v:json.dumps(v,ensure_ascii=False,sort_keys=True) if isinstance(v,(dict,list)) else str(v)
    sections=[];summary=[]
    def visit(node,path='Overview'):
        if isinstance(node,dict):
            scalars=[{'property':k,'value':text(v)} for k,v in sorted(node.items()) if not isinstance(v,(dict,list))]
            if scalars:sections.append((path,scalars))
            for key,v in sorted(node.items()):
                if isinstance(v,(dict,list)):visit(v,path+' / '+key)
        elif isinstance(node,list):
            rows=[r if isinstance(r,dict) else {'value':r} for r in node]
            sections.append((path,rows))
    visit(value)
    for name,rows in sections:
        columns=sorted({k for row in rows for k in row})
        header=''.join('<th scope="col">'+esc(c.replace('_',' '))+'</th>' for c in columns)
        body=''.join('<tr>'+''.join('<td>'+esc(text(row.get(c,'')))+'</td>' for c in columns)+'</tr>' for row in rows)
        width=' style="min-width:'+str(8*len(columns))+'rem"' if len(columns)>3 else ''
        summary.append('<section><h2>'+esc(name)+'</h2><p>'+str(len(rows))+' records</p><div class="scroll"><table'+width+'><thead><tr>'+header+'</tr></thead><tbody>'+body+'</tbody></table></div></section>')
    return '''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>'''+esc(title)+'''</title>
<style>body{font:16px system-ui;max-width:1400px;margin:2rem auto;padding:0 1rem;background:#f4f6f8;color:#182633}h1{font-size:2rem}section{background:white;padding:1rem;margin:1rem 0;border:1px solid #c7d3dc;border-radius:8px}.scroll{overflow:auto}table{border-collapse:collapse;width:100%}th,td{text-align:left;padding:.65rem;border-bottom:1px solid #d9e0e5;vertical-align:top;overflow-wrap:anywhere;min-width:6rem;max-width:55rem}th{background:#eaf0f4}input{font:inherit;padding:.7rem;width:min(80%,40rem)}[hidden]{display:none!important}</style>
<h1>'''+esc(title)+'''</h1><p>Read-only evidence. Unknown is not complete. This report applies no configuration changes.</p><p><a href="report.json" download>Download complete JSON evidence</a></p><label for="search">Filter all records (application, path, status, dependency)</label><br><input id="search" type="search" placeholder="Search records"><p id="count" role="status"></p>
'''+''.join(summary)+'''
<script defer>
const search=document.getElementById('search'), count=document.getElementById('count');
function filter(){const term=search.value.toLocaleLowerCase();let shown=0,total=0;document.querySelectorAll('section').forEach(section=>{let visible=0;section.querySelectorAll('tbody tr').forEach(row=>{total++;row.hidden=!row.textContent.toLocaleLowerCase().includes(term);if(!row.hidden){shown++;visible++;}});section.hidden=visible===0&&term!=='';});count.textContent=shown+' of '+total+' records shown';}
search.addEventListener('input',filter);filter();
</script></html>'''
