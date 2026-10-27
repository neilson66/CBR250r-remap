// The phone page served at http://192.168.4.1 (see HondaKLine.ino). Keep the decoding in sync with pc/honda_dlc.py
// (TABLE11, DTC_NAMES) and the trend with pc/trend.py.
#pragma once

static const char PAGE_HTML[] PROGMEM = R"rawpage(<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="mobile-web-app-capable" content="yes">
<meta name="theme-color" content="#111418">
<title>CBR250R DLC</title>
<style>
:root{--bg:#111418;--panel:#1b2026;--line:#2c333b;--text:#e8edf2;--dim:#8d99a6;--ok:#3fb950;--warn:#d29922;--bad:#f85149;--blue:#58a6ff}
*{box-sizing:border-box}
html,body{margin:0;background:var(--bg);color:var(--text);font:15px/1.35 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
body{padding:8px 8px calc(8px + env(safe-area-inset-bottom))}
header{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:8px}
.dot{width:12px;height:12px;border-radius:50%;background:var(--dim);flex:none}
#st{flex:1;min-width:150px;font-size:14px}
#sess{color:var(--dim);font-size:13px}
.btns{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:8px}
button{background:var(--panel);color:var(--text);border:1px solid var(--line);border-radius:8px;padding:9px 12px;font-size:15px}
button:active{background:var(--line)}
button.mark{background:#8b1d1d;border-color:#b62324;font-weight:700;flex:1 0 100%;padding:14px;font-size:18px}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:8px;margin-bottom:8px}
.panel h2{font-size:13px;font-weight:600;color:var(--dim);margin:0 0 6px;text-transform:uppercase;letter-spacing:.04em}
#cards{display:grid;grid-template-columns:repeat(2,1fr);gap:6px}
@media(min-width:700px){#cards{grid-template-columns:repeat(4,1fr)}}
@media(min-width:1100px){#cards{grid-template-columns:repeat(7,1fr)}}
.card{border:2px solid var(--line);border-radius:8px;padding:6px 8px;position:relative;user-select:none;-webkit-user-select:none}
.card.on{border-color:var(--c)}
.card .nm{font-size:12px;color:var(--dim);display:flex;align-items:center;gap:5px}
.card .sw{width:9px;height:9px;border-radius:2px;background:var(--c)}
.card .v{font:700 24px/1.1 ui-monospace,Consolas,monospace;font-variant-numeric:tabular-nums}
.card .u{font-size:12px;color:var(--dim);margin-left:3px}
.card .cv{font:12px ui-monospace,Consolas,monospace;color:var(--blue)}
.card .cv:empty{display:none}
.tctl{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-bottom:6px;font-size:14px}
select{background:var(--bg);color:var(--text);border:1px solid var(--line);border-radius:6px;padding:5px}
#trend{width:100%;height:42vh;min-height:220px;display:block;touch-action:pan-y;background:#0c0f12;border-radius:6px}
#raw{display:grid;grid-template-columns:repeat(10,1fr);gap:3px}
#raw div{font:13px ui-monospace,Consolas,monospace;text-align:center;background:var(--bg);border-radius:4px;padding:3px 0}
#raw div small{display:block;font-size:9px;color:var(--dim)}
#raw div.ch{background:#5a4400}
.mono{font:12px/1.5 ui-monospace,Consolas,monospace;white-space:pre-wrap;word-break:break-all}
#msgs{max-height:180px;overflow:auto}
.code{display:flex;gap:8px;padding:3px 0;border-bottom:1px solid var(--line)}
.code b{min-width:44px}
a{color:var(--blue)}
.dim{color:var(--dim)}
.hide{display:none}
</style></head><body>
<header><span class="dot" id="dot"></span><span id="st">connecting to the board…</span><span id="sess"></span></header>
<div class="btns">
 <button class="mark" id="bMark">MARK (it's running badly now)</button>
 <button id="bFaults">Fault codes</button><button id="bClear">Clear codes</button>
 <button id="bDump">Dump tables</button><button id="bSave">Save to phone</button><button id="bLogs">SD logs</button>
</div>

<div class="panel"><h2>Sensors — tap to add to the trend</h2><div id="cards"></div></div>

<div class="panel"><h2>Trend</h2>
 <div class="tctl">
  <label>Window <select id="win"></select></label>
  <label><input type="checkbox" id="pause"> Pause</label>
  <label><input type="checkbox" id="auto"> Auto-scale</label>
  <span class="dim" id="curt"></span>
 </div>
 <canvas id="trend"></canvas>
</div>

<div class="panel"><h2>Fault codes</h2><div id="faults" class="dim">not read yet</div></div>

<div class="panel"><h2>Raw bytes, table 0x11 (changed bytes highlighted)</h2><div id="raw"></div>
 <div id="extras" class="mono dim" style="margin-top:6px"></div></div>

<div class="panel hide" id="logsP"><h2>Logs on the SD card</h2><div id="logs"></div></div>

<div class="panel"><h2>Messages</h2><div id="msgs" class="mono"></div></div>

<script>
"use strict";
// Table 0x11 layout (same as TABLE11 in pc/honda_dlc.py): name, unit, offset, size, scale, add, decimals
const F=[["RPM","rpm",0,2,1,0,0],["Throttle sensor","V",2,1,5/256,0,2],["Throttle","%",3,1,1/1.6,0,1],
["Coolant sensor","V",4,1,5/256,0,2],["Coolant temp","°C",5,1,1,-40,0],["Intake air sensor","V",6,1,5/256,0,2],
["Intake air temp","°C",7,1,1,-40,0],["MAP sensor","V",8,1,5/256,0,2],["Manifold pressure","kPa",9,1,1,0,0],
["Battery","V",12,1,0.1,0,1],["Speed","km/h",13,1,1,0,0],["Injector time","ms",14,2,1/250,0,2],
["Ignition advance","°",16,1,0.5,-64,1]];
const RANGE={"RPM":[0,11000],"Throttle sensor":[0,5],"Throttle":[0,100],"Coolant sensor":[0,5],"Coolant temp":[-20,120],
"Intake air sensor":[0,5],"Intake air temp":[-20,60],"MAP sensor":[0,5],"Manifold pressure":[0,110],"Battery":[10,15.5],
"Speed":[0,160],"Injector time":[0,10],"Ignition advance":[-10,50]};
const COL=["#ff5c5c","#4da3ff","#4cc764","#ffa23a","#c38cff","#2fd0d6","#ff6fb1","#c9a27a","#9fb3c8","#e6e14a","#57c7ff","#ff8a5c","#8f7bff"];
const DTC={"1-1":"MAP sensor low voltage","1-2":"MAP sensor high voltage","7-1":"coolant temp sensor low voltage",
"7-2":"coolant temp sensor high voltage","8-1":"throttle sensor low voltage","8-2":"throttle sensor high voltage",
"9-1":"intake air temp sensor low voltage","9-2":"intake air temp sensor high voltage","12-1":"fuel injector circuit",
"21-1":"O2 sensor","29-1":"IACV circuit","33-2":"ECM EEPROM","54-1":"bank angle sensor low voltage",
"54-2":"bank angle sensor high voltage"};
const WIN=[["30 s",30],["1 min",60],["2 min",120],["5 min",300],["10 min",600],["30 min",1800],["All",0]];
const MAXH=36000;
const $=id=>document.getElementById(id);
const store={get(k,d){try{const v=localStorage.getItem(k);return v===null?d:JSON.parse(v)}catch(e){return d}},
 set(k,v){try{localStorage.setItem(k,JSON.stringify(v))}catch(e){}}};

function hex2b(h){return h?h.trim().split(/\s+/).map(x=>parseInt(x,16)):[]}
function dec(b){return F.map(f=>{if(f[2]+f[3]>b.length)return null;let r=0;for(let i=0;i<f[3];i++)r=r*256+b[f[2]+i];return r*f[4]+f[5]})}
function fmt(i,v){return v==null?"—":v.toFixed(F[i][6])}

// ---- history (this session, kept in the phone while the page is open)
let H={t:[],v:F.map(()=>[]),raw:[],mark:[]},seq=0,mid=0,sess=null,fseq=-1,prevRaw=[],paused=false,viewEnd=null,cursorX=null,cursorTimer=0;
function resetH(){H={t:[],v:F.map(()=>[]),raw:[],mark:[]};prevRaw=[];viewEnd=null}

// ---- sensor cards
const pens=store.get("pens",["RPM","Throttle","Coolant temp","Manifold pressure","Battery"]);
const cards=$("cards"),cardEls=[];
F.forEach((f,i)=>{const d=document.createElement("div");d.className="card";d.style.setProperty("--c",COL[i]);
 d.innerHTML=`<div class="nm"><span class="sw"></span>${f[0]}</div><div><span class="v">—</span><span class="u">${f[1]}</span></div><div class="cv"></div>`;
 if(pens.includes(f[0]))d.classList.add("on");
 d.onclick=()=>{d.classList.toggle("on");store.set("pens",F.filter((_,k)=>cardEls[k].el.classList.contains("on")).map(x=>x[0]));draw()};
 cards.appendChild(d);cardEls.push({el:d,v:d.querySelector(".v"),cv:d.querySelector(".cv")})});
const penOn=i=>cardEls[i].el.classList.contains("on");

// ---- raw grid
const rawEl=$("raw"),rawCells=[];
for(let i=0;i<20;i++){const d=document.createElement("div");d.innerHTML=`<small>${String(i).padStart(2,"0")}</small><span>--</span>`;rawEl.appendChild(d);rawCells.push(d)}
function showRaw(b){b.forEach((x,i)=>{if(i>=rawCells.length)return;rawCells[i].lastChild.textContent=x.toString(16).toUpperCase().padStart(2,"0");
 rawCells[i].classList.toggle("ch",prevRaw.length>i&&prevRaw[i]!==x)});prevRaw=b}
function showExtras(ex){let s="";for(const t in ex){const b=hex2b(ex[t]);if(!b.length)continue;s+=`table ${t}: ${ex[t]}`;
 if(t==="20"&&b[0]!==0xFF)s+=`   (byte 0 = ${(b[0]*5/256).toFixed(2)} V, O2 sensor?)`;
 if(t==="D0"&&b.length>12){const p=b[11]*256+b[12];if(p)s+=`   (crank ≈ ${Math.round(2460000/p)} rpm?)`}
 s+="\n"}$("extras").textContent=s}

// ---- trend
const win=$("win");WIN.forEach(([n],i)=>{const o=document.createElement("option");o.textContent=n;o.value=i;win.appendChild(o)});
win.value=store.get("win",1);win.onchange=()=>{store.set("win",+win.value);draw()};
$("pause").onchange=e=>{paused=e.target.checked;viewEnd=paused&&H.t.length?H.t[H.t.length-1]:null;draw()};
$("auto").onchange=draw;
const cv=$("trend"),ctx=cv.getContext("2d");
function bisect(a,x){let lo=0,hi=a.length;while(lo<hi){const m=(lo+hi)>>1;if(a[m]<x)lo=m+1;else hi=m}return lo}
let span=null;
function draw(){
 const dpr=window.devicePixelRatio||1,w=cv.clientWidth,h=cv.clientHeight;
 if(cv.width!==Math.round(w*dpr)||cv.height!==Math.round(h*dpr)){cv.width=Math.round(w*dpr);cv.height=Math.round(h*dpr)}
 ctx.setTransform(dpr,0,0,dpr,0,0);ctx.clearRect(0,0,w,h);
 const x0=44,x1=w-6,y0=8,y1=h-20;
 ctx.font="11px system-ui";
 if(!H.t.length){ctx.fillStyle="#8d99a6";ctx.textAlign="center";ctx.fillText("No data yet",w/2,h/2);return}
 const ws=WIN[+win.value][1],last=H.t[H.t.length-1],first=H.t[0];
 let t0,t1;
 if(!ws){t0=first;t1=Math.max(last,first+1)}
 else{let end=(paused&&viewEnd!=null)?viewEnd:last;if(last-first>ws)end=Math.min(Math.max(end,first+ws),last);else end=first+ws;t0=end-ws;t1=end}
 span=[t0,t1,x0,x1];
 const tx=t=>x0+(t-t0)*(x1-x0)/(t1-t0);
 const i0=Math.max(0,bisect(H.t,t0)-1),i1=Math.min(H.t.length,bisect(H.t,t1)+1);
 ctx.strokeStyle="#252c34";ctx.lineWidth=1;
 for(let k=0;k<=5;k++){const y=y0+k*(y1-y0)/5;ctx.beginPath();ctx.moveTo(x0,y);ctx.lineTo(x1,y);ctx.stroke()}
 const steps=[1,2,5,10,15,30,60,120,300,600,900,1800,3600];const st=steps.find(s=>(t1-t0)/s<=6)||7200;
 ctx.fillStyle="#8d99a6";ctx.textAlign="center";
 for(let t=Math.ceil(t0/st)*st;t<=t1;t+=st){const x=tx(t);ctx.beginPath();ctx.moveTo(x,y0);ctx.lineTo(x,y1);ctx.stroke();
  ctx.fillText(`${Math.floor(t/60)}:${String(Math.floor(t%60)).padStart(2,"0")}`,x,y1+14)}
 const sc={},stride=Math.max(1,Math.floor((i1-i0)/((x1-x0)*2)));let axis=-1;
 F.forEach((f,i)=>{if(!penOn(i))return;if(axis<0)axis=i;let [lo,hi]=RANGE[f[0]];const a=H.v[i];
  if($("auto").checked){let mn=Infinity,mx=-Infinity;for(let k=i0;k<i1;k++){const v=a[k];if(v!=null){if(v<mn)mn=v;if(v>mx)mx=v}}
   if(mn<=mx){const pad=(mx-mn)*0.1||Math.max(1,Math.abs(mx)*0.05);lo=mn-pad;hi=mx+pad}}
  sc[i]=[lo,hi];ctx.strokeStyle=COL[i];ctx.lineWidth=2;ctx.beginPath();let pen=false;
  for(let k=i0;k<i1;k+=stride){const v=a[k];if(v==null){pen=false;continue}
   const y=y1-(Math.min(Math.max(v,lo),hi)-lo)*(y1-y0)/(hi-lo);if(pen)ctx.lineTo(tx(H.t[k]),y);else{ctx.moveTo(tx(H.t[k]),y);pen=true}}
  ctx.stroke()});
 if(axis>=0){const [lo,hi]=sc[axis];ctx.fillStyle=COL[axis];ctx.textAlign="right";
  for(let k=0;k<=5;k++){const v=hi-k*(hi-lo)/5;ctx.fillText(Math.abs(hi-lo)>=50?v.toFixed(0):v.toPrecision(3),x0-4,y0+k*(y1-y0)/5+4)}}
 ctx.strokeStyle="#ff5c5c";ctx.fillStyle="#ff5c5c";ctx.setLineDash([4,3]);ctx.textAlign="left";
 for(let k=i0;k<i1;k++){if(!H.mark[k])continue;const x=tx(H.t[k]);ctx.beginPath();ctx.moveTo(x,y0);ctx.lineTo(x,y1);ctx.stroke();ctx.fillText(H.mark[k],x+3,y0+10)}
 ctx.setLineDash([]);
 let idx=-1;
 if(cursorX!=null&&cursorX>=x0&&cursorX<=x1){const tc=t0+(cursorX-x0)*(t1-t0)/(x1-x0);const j=bisect(H.t,tc);
  idx=(j>0&&(j>=H.t.length||Math.abs(H.t[j-1]-tc)<Math.abs(H.t[j]-tc)))?j-1:Math.min(j,H.t.length-1);
  const x=tx(H.t[idx]);ctx.strokeStyle="#e8edf2";ctx.beginPath();ctx.moveTo(x,y0);ctx.lineTo(x,y1);ctx.stroke();
  for(const i in sc){const v=H.v[i][idx];if(v==null)continue;const [lo,hi]=sc[i];const y=y1-(Math.min(Math.max(v,lo),hi)-lo)*(y1-y0)/(hi-lo);
   ctx.fillStyle=COL[i];ctx.beginPath();ctx.arc(x,y,3.5,0,7);ctx.fill()}
  const t=H.t[idx];$("curt").textContent=`cursor at ${Math.floor(t/60)}:${(t%60).toFixed(1).padStart(4,"0")}`;
  // read-out box inside the chart (the sensor cards may be scrolled out of view on a phone)
  const rows=Object.keys(sc).map(i=>[COL[i],`${F[i][0]} ${fmt(i,H.v[i][idx])} ${F[i][1]}`]);
  ctx.font="12px system-ui";const bw=Math.max(...rows.map(r=>ctx.measureText(r[1]).width))+16,bh=rows.length*16+6;
  const bx=x>x0+(x1-x0)/2?x0+4:x1-bw-4;ctx.fillStyle="rgba(12,15,18,.85)";ctx.fillRect(bx,y0+2,bw,bh);
  ctx.textAlign="left";rows.forEach((r,k)=>{ctx.fillStyle=r[0];ctx.fillText(r[1],bx+8,y0+18+k*16)})}
 else $("curt").textContent="";
 cardEls.forEach((c,i)=>{c.cv.textContent=idx>=0?"@ "+fmt(i,H.v[i][idx]):""});
}
function setCursor(e){const r=cv.getBoundingClientRect();cursorX=e.clientX-r.left;clearTimeout(cursorTimer);draw()}
cv.addEventListener("pointerdown",setCursor);cv.addEventListener("pointermove",e=>{if(e.pointerType==="mouse"||e.buttons)setCursor(e)});
cv.addEventListener("pointerleave",e=>{if(e.pointerType==="mouse"){cursorX=null;draw()}});
cv.addEventListener("pointerup",e=>{if(e.pointerType!=="mouse"){clearTimeout(cursorTimer);cursorTimer=setTimeout(()=>{cursorX=null;draw()},4000)}});
window.addEventListener("resize",draw);

// ---- data in
function addSample(s){const t=s[1]/1000,b=hex2b(s[2]),v=dec(b);H.t.push(t);v.forEach((x,i)=>H.v[i].push(x));H.raw.push(s[2]);H.mark.push(s[3]||"");
 if(H.t.length>MAXH){const c=H.t.length-MAXH;H.t.splice(0,c);H.v.forEach(a=>a.splice(0,c));H.raw.splice(0,c);H.mark.splice(0,c)}}
const KIND={ok:"var(--ok)",waking:"var(--warn)",busy:"var(--warn)",bridge:"var(--blue)",idle:"var(--dim)"};
let failures=0,dirty=false;
async function poll(){
 try{const r=await fetch(`/api/live?since=${seq}&m=${mid}`,{cache:"no-store"});const j=await r.json();failures=0;
  if(j.seq<seq){seq=0;mid=0}  // the board restarted
  if(j.sess!==sess){if(j.sess){resetH()}sess=j.sess}
  $("st").textContent=j.st;$("dot").style.background=KIND[j.kind]||"var(--dim)";
  $("sess").textContent=(j.sess?"session "+j.sess:"")+(j.sd?"":" · no SD card");
  if(j.s.length){j.s.forEach(addSample);seq=j.s[j.s.length-1][0];const b=hex2b(j.s[j.s.length-1][2]);
   const v=dec(b);cardEls.forEach((c,i)=>c.v.textContent=fmt(i,v[i]));showRaw(b);dirty=true}
  showExtras(j.ex);
  if(j.msgs.length){const m=$("msgs");j.msgs.forEach(x=>{m.textContent+=x[1]+"\n";mid=x[0]});
   const lines=m.textContent.split("\n");if(lines.length>200)m.textContent=lines.slice(-200).join("\n");m.scrollTop=m.scrollHeight}
  if(j.fseq!==fseq){fseq=j.fseq;loadFaults()}
  if(!j.time)setTime();
 }catch(e){if(++failures>3){$("st").textContent="no connection to the board — is the phone on the CBR250R-DLC WiFi?";$("dot").style.background="var(--bad)"}}
 setTimeout(poll,250)}
setInterval(()=>{if(dirty&&!paused){dirty=false;draw()}},200);

async function setTime(){try{await fetch(`/api/time?ms=${Date.now()}&tz=${new Date().getTimezoneOffset()}`,{method:"POST"})}catch(e){}}
async function loadFaults(){
 try{const j=await(await fetch("/api/faults",{cache:"no-store"})).json();let h="";
  for(const k of ["current","past"]){const rows=j[k]||[];const codes=[];
   rows.forEach(r=>{const b=hex2b(r[1]);for(let i=1;i+1<b.length;i+=2)if(b[i])codes.push(`${b[i]}-${b[i+1]}`)});
   h+=`<div style="margin:4px 0 2px;font-weight:600">${k==="current"?"Current":"Past (stored)"}</div>`;
   h+=codes.length?codes.map(c=>`<div class="code"><b>${c}</b><span>${DTC[c]||"see the workshop manual DTC index (5-14)"}</span></div>`).join(""):`<div class="dim">none</div>`}
  if(!(j.current||[]).length&&!(j.past||[]).length)h="<span class='dim'>not read yet</span>";
  $("faults").innerHTML=h}catch(e){}}

// ---- buttons
async function cmd(c){try{const j=await(await fetch(`/api/cmd?c=${c}`,{method:"POST"})).json();if(!j.ok)alert(j.why||"failed")}catch(e){alert("no connection to the board")}}
$("bMark").onclick=async()=>{try{navigator.vibrate&&navigator.vibrate(60)}catch(e){};try{await fetch("/api/mark",{method:"POST"})}catch(e){alert("mark not sent: no connection")}};
$("bFaults").onclick=()=>cmd("faults");
$("bClear").onclick=()=>{if(confirm("Erase the fault codes stored in the ECU?\n\nThe current codes are read and saved to the session log first. Engine off, key on is best. The codes are re-read afterwards."))cmd("clear")};
$("bDump").onclick=()=>{if(confirm("Read every table 00-FF? Live data pauses for about a minute."))cmd("dump")};
$("bSave").onclick=()=>{
 if(!H.t.length){alert("nothing recorded yet");return}
 let s="elapsed_s,mark,"+F.map(f=>`${f[0]} (${f[1]})`).join(",")+",raw_11\n";
 for(let k=0;k<H.t.length;k++)s+=H.t[k].toFixed(1)+","+H.mark[k]+","+F.map((_,i)=>H.v[i][k]==null?"":H.v[i][k].toFixed(F[i][6])).join(",")+","+H.raw[k]+"\n";
 const a=document.createElement("a");a.href=URL.createObjectURL(new Blob(["﻿"+s],{type:"text/csv"}));
 const d=new Date(),p=n=>String(n).padStart(2,"0");
 a.download=`cbr250r_${d.getFullYear()}${p(d.getMonth()+1)}${p(d.getDate())}_${p(d.getHours())}${p(d.getMinutes())}.csv`;
 document.body.appendChild(a);a.click();setTimeout(()=>{URL.revokeObjectURL(a.href);a.remove()},1000)};
$("bLogs").onclick=async()=>{const p=$("logsP");p.classList.toggle("hide");if(p.classList.contains("hide"))return;$("logs").textContent="loading…";
 try{const j=await(await fetch("/api/logs",{cache:"no-store"})).json();
  if(!j.length){$("logs").innerHTML="<span class='dim'>no sessions on the card</span>";return}
  j.sort((a,b)=>b.s.localeCompare(a.s));
  $("logs").innerHTML=j.map(d=>`<div class="code"><b>${d.s}</b><span>${d.f.map(f=>`<a href="/dl?f=/${d.s}/${f[0]}">${f[0]}</a> <span class="dim">${(f[1]/1024).toFixed(1)} kB</span>`).join(" · ")}</span></div>`).join("")}
 catch(e){$("logs").textContent="could not read the SD card"}};

setTime();poll();draw();
</script></body></html>)rawpage";
