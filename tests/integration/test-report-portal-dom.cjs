/* Static unit harness: executes the portal script against a minimal fake DOM.
 * No browser, rendering, network or UI automation. Pass synthetic HTML fixture.
 */
'use strict';
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const html=fs.readFileSync(process.argv[2],'utf8');
const data=JSON.parse(html.match(/<script id="data" type="application\/json">([\s\S]*?)<\/script>/)[1]);
const script=html.match(/<script>([\s\S]*?)<\/script>/)[1];
const elements=new Map(),downloads=[];
class Element {
 constructor(tag){this.tag=tag;this.children=[];this.attrs={};this._value='';this._text='';this.style={};}
 set id(value){this._id=value;elements.set(value,this)}get id(){return this._id}
 set value(value){this._value=value}get value(){return this._value}
 set textContent(value){this._text=String(value??'');this.children=[]}get textContent(){return this._text+this.children.map(c=>c.textContent||'').join('')}
 append(...items){for(const item of items){this.children.push(item);if(this.tag==='select'&&!this._value&&item.tag==='option')this._value=item.value}}
 replaceChildren(...items){this.children=[];this._text='';this.append(...items)}
 setAttribute(k,v){this.attrs[k]=String(v)}
 createTHead(){const e=new Element('thead');this.append(e);return e}createTBody(){const e=new Element('tbody');this.append(e);return e}
 insertRow(){const e=new Element('tr');this.append(e);return e}insertCell(){const e=new Element('td');this.append(e);return e}
 click(){if(this.tag==='a')downloads.push({href:this.href,name:this.download});if(this.onclick)this.onclick()}
}
for(const id of ['data','asof','zone','cards','sources','limits','aux','section','search','from','to','download','explanation','controls','chart','table','count']){const e=new Element(id==='section'?'select':'div');e.id=id;if(id==='data')e.textContent=JSON.stringify(data)}
const document={getElementById:id=>elements.get(id),createElement:tag=>new Element(tag),createElementNS:(_,tag)=>new Element(tag),createTextNode:text=>({textContent:String(text)})};
const sandbox={document,Intl,Date,Math,JSON,Map,Set,Number,String,Object,console,Blob:class {constructor(values){this.values=values}},URL:{createObjectURL:()=> 'blob:fixture',revokeObjectURL:()=>{}},setTimeout:fn=>fn()};
vm.createContext(sandbox);vm.runInContext(script,sandbox,{timeout:5000});
let checks=0;function run(code){vm.runInContext(code,sandbox,{timeout:5000});checks++}
for(const section of Object.keys(data.section_status).filter(k=>data.section_status[k]!=='disabled')){
 run(`$('section').value=${JSON.stringify(section)};draw()`);assert(elements.get('explanation').textContent.length>20);
 run(`$('search').value='';draw(false)`);run(`$('search').value='absent fixture unlikely';draw(false)`);assert(elements.get('count').textContent.includes('of 0 filtered'));
 run(`$('search').value='';draw(false)`);run(`$('download').onclick()`);
}
run("$('section').value='schedules';draw()");
for(const kind of [...new Set(['timeline','heatmap',...data.schedules.map(r=>r.report)])])run(`$('scheduleKind').value=${JSON.stringify(kind)};draw(false)`);
run("$('scheduleKind').value='timeline';draw(false)");assert(elements.get('chart').children.some(x=>x.tag==='svg'));
run("$('scheduleKind').value='heatmap';draw(false)");assert(elements.get('chart').children.some(x=>x.tag==='svg'));
run("$('section').value='history';draw()");assert(elements.get('chart').children.some(x=>x.tag==='svg'));assert(elements.get('series').children[0].textContent.includes('.'));
run("$('zone').value='Invalid/Zone';draw(false)");run("$('zone').value='America/New_York';$('from').value='2026-10-01';$('to').value='2026-10-31';draw(false)");
run("$('section').value='exposure';draw()");assert(elements.get('table').textContent.includes('unknown'));
assert(downloads.length>=Object.keys(data.section_status).length);assert(!html.includes('demo-secret-never-displayed'));
console.log(JSON.stringify({status:'PASS',checks,views:Object.keys(data.section_status).length,browser_rendering:false,network_used:false}));
