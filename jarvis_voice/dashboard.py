"""本地可视化仪表盘 + 手动控制。

  - **只绑 127.0.0.1**（本项目被开放端口咬过，代码里 assert 强制）
  - SSE 推送；事件同时落 `~/.jarvis/events.jsonl`，可事后回放
  - 控制按钮：暂停/继续监听、立刻打断

设计取向（impeccable · Operate 模式）：工具该消失在任务里。
  · 单一家族、固定 rem 阶梯、收敛到**一个**强调色
  · 说话人靠**对齐 + 标签 + 极轻的表面色差**区分，不用两个饱和气泡
  · 图标是手绘 SVG（craft floor 明确禁止用 emoji 当图标系统）
  · 不用彩色 border-left（craft floor 明确禁止）
  · 主题化滚动条 / 选区 / 插入符 / 焦点环 / 表格数字
"""
import json
import queue

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel

from .events import BUS

app = FastAPI(title="jarvis-voice")
ORCH = None          # 由 bind() 注入


def bind(orch):
    global ORCH
    ORCH = orch


class Control(BaseModel):
    action: str                    # pause | resume | interrupt | mode | device
    value: str | int | None = None


def _out_devices():
    import sounddevice as sd
    return [{"index": i, "name": d["name"]}
            for i, d in enumerate(sd.query_devices()) if d["max_output_channels"] > 0]


@app.get("/api/devices")
def devices():
    try:
        return {"ok": True, "devices": _out_devices()}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/api/control")
def control(c: Control):
    if ORCH is None:
        return {"ok": False, "error": "未绑定 orchestrator"}
    if c.action == "pause":
        ORCH.pause()
    elif c.action == "resume":
        ORCH.resume()
    elif c.action == "interrupt":
        ORCH.interrupt_now()
    elif c.action == "mode":
        if not ORCH.set_mode(str(c.value)):
            return {"ok": False, "error": f"未知模式: {c.value}"}
    elif c.action == "device":
        v = c.value
        idx = None if v in (None, "", "default") else int(v)
        if not ORCH.set_output_device(idx):
            return {"ok": False, "error": "换设备失败（看终端日志）"}
    elif c.action == "reconnect":
        if not ORCH.reconnect_memory():
            return {"ok": False, "error": "第二大脑没连上——Obsidian 可能没开着"}
    elif c.action == "shutdown":
        # ⏻ 关闭整个应用（含麦克风与脑进程）。注意：响应发出后 ~0.4s 进程就退了，
        # 浏览器随后会显示"已断开"——那是**预期**，不是错误。
        ORCH.shutdown()
        return {"ok": True, "shutdown": True}
    else:
        return {"ok": False, "error": f"未知 action: {c.action}"}
    return {"ok": True, "paused": ORCH.paused,
            "mode": ORCH.cfg.audio_mode, "device": ORCH.player.device}


ICON = {
    # 16px / 1.6 描边，统一一套
    "tool": '<path d="M9.5 6.5a2.5 2.5 0 1 1 3.2 3.2l4.3 4.3-1.6 1.6-4.3-4.3A2.5 2.5 0 0 1 9.5 6.5Z"/>',
    "bolt": '<path d="M9 1.5 3.5 9h3.2l-.7 5.5L11.5 7H8.3l.7-5.5Z"/>',
    "stop": '<rect x="4" y="4" width="8" height="8" rx="1.5"/>',
    "pause": '<path d="M6 3.5v9M10 3.5v9"/>',
    "play": '<path d="M5.5 3.5v9l7-4.5-7-4.5Z"/>',
    "mic": '<rect x="6" y="2" width="4" height="7" rx="2"/><path d="M4 8a4 4 0 0 0 8 0M8 12v2"/>',
    "chat": '<path d="M2.5 4.5h11v7h-7l-4 3v-10Z"/>',
    "warn": '<path d="M8 2.5 14.5 13.5h-13L8 2.5Z"/><path d="M8 6.5v3M8 11.5h.01"/>',
    "link": '<path d="M6.5 9.5 9.5 6.5"/><path d="M7 4.5 8.8 2.7a2.6 2.6 0 0 1 3.7 3.7l-1.8 1.6"/>'
            '<path d="M9 11.5 7.2 13.3a2.6 2.6 0 0 1-3.7-3.7l1.8-1.6"/>',
    "power": '<path d="M8 2.5v5"/><path d="M4.9 4.3a4.6 4.6 0 1 0 6.2 0"/>',
}


def ic(name: str, cls: str = "ic") -> str:
    return (f'<svg class="{cls}" viewBox="0 0 16 16" fill="none" stroke="currentColor" '
            f'stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">'
            f'{ICON.get(name, "")}</svg>')


PAGE = """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>jarvis-voice</title>
<style>
:root{
  color-scheme:dark;
  --bg:#0b0d11; --panel:#12151b; --sunk:#0e1116; --line:#1f242e; --line2:#2a3140;
  --fg:#e7eaf0; --dim:#8a93a6; --faint:#7b8496;
  --accent:#7aa2f7; --ok:#73d0a0; --warn:#e0a33c; --err:#e5645a;
  --u-surf:#151b26; --a-surf:#141f1b;
  --r:12px; --t:180ms;
}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--fg);
  font:14px/1.55 -apple-system,BlinkMacSystemFont,"PingFang SC","Segoe UI",sans-serif;
  font-variant-numeric:tabular-nums;-webkit-font-smoothing:antialiased}
::selection{background:color-mix(in srgb,var(--accent) 32%,transparent)}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px;border-radius:6px}
*::-webkit-scrollbar{width:10px;height:10px}
*::-webkit-scrollbar-track{background:var(--sunk)}
*::-webkit-scrollbar-thumb{background:var(--line2);border-radius:6px;
  border:2px solid var(--sunk)}
*::-webkit-scrollbar-thumb:hover{background:var(--faint)}

header{position:sticky;top:0;z-index:9;background:var(--panel);
  border-bottom:1px solid var(--line)}
.bar{max-width:960px;margin:0 auto;padding:10px 20px;display:flex;align-items:center;
  gap:14px;flex-wrap:wrap}
.brand{font-weight:650;letter-spacing:-.01em;font-size:15px}
.chip{display:inline-flex;align-items:center;gap:6px;padding:3px 10px;border-radius:999px;
  background:var(--sunk);border:1px solid var(--line);font-size:12.5px;color:var(--dim)}
.chip b{color:var(--fg);font-weight:600}
.warnchip{border-color:color-mix(in srgb,var(--warn) 45%,var(--line));
  background:color-mix(in srgb,var(--warn) 12%,var(--sunk));color:var(--warn)}
.dot{width:7px;height:7px;border-radius:50%;background:var(--faint);
  transition:background var(--t)}
[data-state=idle] .dot{background:var(--ok)}
[data-state=thinking] .dot{background:var(--warn)}
[data-state=speaking] .dot{background:var(--accent)}
[data-state=paused] .dot{background:var(--faint)}
.spacer{flex:1 1 auto}
.meter{width:64px;height:6px;border-radius:3px;background:var(--sunk);overflow:hidden;
  border:1px solid var(--line)}
.meter i{display:block;height:100%;width:100%;background:var(--ok);
  transform:scaleX(0);transform-origin:left;
  transition:transform 90ms linear,background 90ms linear}
.ctrls{display:flex;gap:8px}
select{font:inherit;color:var(--fg);background:var(--sunk);border:1px solid var(--line2);
  border-radius:9px;padding:5px 8px;cursor:pointer;max-width:190px}
select:hover{border-color:var(--faint)}
button{font:inherit;color:var(--fg);background:var(--sunk);border:1px solid var(--line2);
  border-radius:9px;padding:6px 12px;cursor:pointer;display:inline-flex;align-items:center;
  gap:6px;transition:background var(--t),border-color var(--t),opacity var(--t)}
button:hover{background:var(--panel);border-color:var(--faint)}
button:active{transform:translateY(.5px)}
button[disabled]{opacity:.42;cursor:not-allowed}
button.primary{border-color:color-mix(in srgb,var(--accent) 55%,var(--line2));
  background:color-mix(in srgb,var(--accent) 12%,var(--sunk))}
button.danger{border-color:color-mix(in srgb,var(--err) 42%,var(--line2));
  color:color-mix(in srgb,var(--err) 62%,var(--fg))}
button.danger:hover{background:color-mix(in srgb,var(--err) 13%,var(--sunk));border-color:var(--err)}
.ic{width:15px;height:15px;flex:none}

.tabs{max-width:960px;margin:0 auto;padding:12px 20px 0;display:flex;gap:4px}
.tab{padding:5px 11px;border-radius:8px;font-size:13px;color:var(--dim);cursor:pointer;
  background:none;border:1px solid transparent;font:inherit;
  transition:color var(--t),background var(--t)}
.tab:hover{color:var(--fg)}
.tab[aria-pressed=true]{color:var(--fg);background:var(--sunk);border-color:var(--line)}

main{max-width:960px;margin:0 auto;padding:14px 20px 40px}
.empty{color:var(--faint);text-align:center;padding:56px 20px;font-size:13.5px;
  border:1px dashed var(--line2);border-radius:var(--r);margin-top:10px}
.empty b{color:var(--dim);font-weight:600}

.ev{margin:10px 0;animation:in var(--t) cubic-bezier(.2,.8,.2,1)}
@keyframes in{from{opacity:0;transform:translateY(4px)}to{opacity:1;transform:none}}
@media (prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}

.msg{display:flex;flex-direction:column;max-width:80%}
.msg.me{align-self:flex-end;margin-left:auto;align-items:flex-end}
.msg.omen{align-items:flex-start}
.who{font-size:11px;letter-spacing:.06em;text-transform:uppercase;color:var(--faint);
  margin:0 8px 3px}
.bub{padding:9px 13px;border-radius:var(--r);border:1px solid var(--line);
  white-space:pre-wrap;word-break:break-word}
.me .bub{background:var(--u-surf)}
.omen .bub{background:var(--a-surf)}
.fact{font-size:11.5px;color:var(--faint);margin:3px 8px 0;display:flex;gap:10px;
  flex-wrap:wrap;align-items:center}
.lat{color:var(--faint)}
.lat.slow{color:var(--err)}
.raw{margin:4px 0 0;font-size:11.5px;color:var(--warn)}

.tool{margin:8px 0;border:1px solid var(--line);border-radius:var(--r);
  background:var(--sunk);overflow:hidden}
.tool summary{list-style:none;cursor:pointer;padding:8px 12px;display:flex;align-items:center;
  gap:9px;font-size:13px}
.tool summary::-webkit-details-marker{display:none}
.tool summary:hover{background:color-mix(in srgb,var(--fg) 4%,transparent)}
.tool .nm{color:var(--fg);font-weight:550}
.tool .args{color:var(--faint);font-size:12px;overflow:hidden;text-overflow:ellipsis;
  white-space:nowrap;flex:1}
.tool pre{margin:0;padding:10px 12px;border-top:1px solid var(--line);background:var(--bg);
  font:12px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace;color:var(--dim);
  overflow-x:auto;white-space:pre-wrap;word-break:break-word}

.note{display:flex;align-items:center;gap:8px;justify-content:center;color:var(--faint);
  font-size:12.5px;margin:9px 0}
.note.err{color:var(--err)}
.note.warn{color:var(--warn)}
.note.ok{color:var(--ok)}
.note .ic{width:13px;height:13px}

.stats{display:flex;gap:18px;flex-wrap:wrap;font-size:12.5px;color:var(--dim);
  padding:10px 20px;max-width:960px;margin:0 auto;border-top:1px solid var(--line)}
.stats b{color:var(--fg);font-weight:600}
.sr{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0)}
</style></head><body data-state="idle">

<header>
  <div class="bar">
    <span class="brand">jarvis-voice</span>
    <span class="chip" id="stateChip"><span class="dot"></span><b id="stateTxt">启动中</b></span>
    <span class="chip" id="modeChip">__MODE__</span>
    <span class="chip" title="麦克风电平" aria-hidden="true"><svg class="ic" viewBox="0 0 16 16" fill="none"
      stroke="currentColor" stroke-width="1.6" stroke-linecap="round">
      <rect x="6" y="2" width="4" height="7" rx="2"/><path d="M4 8a4 4 0 0 0 8 0M8 12v2"/>
      </svg><span class="meter"><i id="meter"></i></span></span>
    <span class="spacer"></span>
    <span class="chip" id="conn">连接中…</span>
    <span class="ctrls">
      <select id="devSel" title="输出设备（系统默认输出变了这里不会自动跟，需手动选）"></select>
      <button id="btnMode" title="切换耳机/免提">__MODEICON__<span>免提</span></button>
      <button id="btnVault" title="热重连第二大脑（Obsidian 得开着）">__VAULT__<span>第二大脑</span></button>
      <button id="btnPause" class="primary" title="暂停/继续监听">__PAUSE__<span>暂停监听</span></button>
      <button id="btnStop" title="立刻打断当前这句">__STOP__<span>打断</span></button>
      <button id="btnShutdown" class="danger" title="完全关闭：停麦克风 + 结束助手进程（含脑模型）">__POWER__<span>关闭</span></button>
    </span>
  </div>
</header>

<nav class="tabs" aria-label="筛选对话">
  <button class="tab" data-f="all" aria-pressed="true">全部</button>
  <button class="tab" data-f="talk" aria-pressed="false">只看对话</button>
  <button class="tab" data-f="tool" aria-pressed="false">只看工具</button>
</nav>

<main id="feed" role="log" aria-live="polite" aria-relevant="additions"
      aria-label="对话记录">
  <div class="empty" id="empty">
    <b>还没有内容。</b><br>戴上耳机，直接说话就行 —— 说完停顿一下它就会接话。
  </div>
</main>

<div class="stats" id="stats"></div>

<script>
const $=s=>document.querySelector(s);
const feed=$('#feed'), empty=$('#empty'), meter=$('#meter'), stats=$('#stats');
let paused=false, mode='headphones', turns=0, interrupts=0, cost=0;
let first=[], asr=[];
let filter='all';

function el(t,c,txt){const e=document.createElement(t);if(c)e.className=c;
  if(txt!=null)e.textContent=txt;return e;}
function ic(name){const s=document.createElementNS('http://www.w3.org/2000/svg','svg');
  s.setAttribute('viewBox','0 0 16 16');s.setAttribute('class','ic');
  s.setAttribute('fill','none');s.setAttribute('stroke','currentColor');
  s.setAttribute('stroke-width','1.6');s.setAttribute('stroke-linecap','round');
  s.setAttribute('stroke-linejoin','round');s.innerHTML=PATHS[name]||'';return s;}
const PATHS={
 tool:'<path d="M9.5 6.5a2.5 2.5 0 1 1 3.2 3.2l4.3 4.3-1.6 1.6-4.3-4.3A2.5 2.5 0 0 1 9.5 6.5Z"/>',
 bolt:'<path d="M9 1.5 3.5 9h3.2l-.7 5.5L11.5 7H8.3l.7-5.5Z"/>',
 stop:'<rect x="4" y="4" width="8" height="8" rx="1.5"/>',
 pause:'<path d="M6 3.5v9M10 3.5v9"/>',
 play:'<path d="M5.5 3.5v9l7-4.5-7-4.5Z"/>',
 chat:'<path d="M2.5 4.5h11v7h-7l-4 3v-10Z"/>',
 warn:'<path d="M8 2.5 14.5 13.5h-13L8 2.5Z"/><path d="M8 6.5v3M8 11.5h.01"/>',
 link:'<path d="M6.5 9.5 9.5 6.5"/><path d="M7 4.5 8.8 2.7a2.6 2.6 0 0 1 3.7 3.7l-1.8 1.6"/><path d="M9 11.5 7.2 13.3a2.6 2.6 0 0 1-3.7-3.7l1.8-1.6"/>',
 power:'<path d="M8 2.5v5"/><path d="M4.9 4.3a4.6 4.6 0 1 0 6.2 0"/>'};

function note(cls,icon,txt){
  const d=el('div','note '+(cls||'')); d.dataset.f='note';
  if(icon)d.appendChild(ic(icon)); d.appendChild(el('span',null,txt));
  add(d); return d;
}
function add(node){
  const kind=node.dataset.f||'talk';
  node.dataset.kind=kind;
  node.hidden = (filter==='all') ? false
              : (filter==='talk' ? kind==='tool' : kind!=='tool');
  feed.appendChild(node); empty.hidden=true; scroll();
}
function scroll(){window.scrollTo({top:document.body.scrollHeight,behavior:'instant'});}
function setFilter(f){filter=f;document.querySelectorAll('.tab').forEach(t=>
  t.setAttribute('aria-pressed', String(t.dataset.f===f)));
  feed.querySelectorAll('[data-kind]').forEach(n=>{
    const k=n.dataset.kind;
    n.hidden = (f==='all')?false:(f==='talk'?k==='tool':k!=='tool');});}

function updStats(){
  const pct=(a,p)=>{if(!a.length)return '—';const s=[...a].sort((x,y)=>x-y);
    return Math.round(s[Math.min(s.length-1,Math.floor(p*s.length))])+'ms';};
  stats.innerHTML='';
  const add=(l,v)=>{const d=el('span');d.append(l+' ');const b=el('b',null,v);
    d.appendChild(b);stats.appendChild(d);};
  add('轮次',turns); add('打断',interrupts);
  add('首句 p50',pct(first,.5)); add('首句 p90',pct(first,.9));
  add('ASR 中位',pct(asr,.5)); add('累计 $',cost.toFixed(4));
}

function on(ev){
 switch(ev.kind){
  case 'session_start':
    note('','chat','会话开始 · TTS '+ev.tts+' · VAD '+ev.vad);break;
  case 'state':
    if(!paused){document.body.dataset.state=ev.value;
      $('#stateTxt').textContent={idle:'在听',thinking:'思考中',speaking:'说话中'}[ev.value]||ev.value;}
    break;
  case 'level':{
    const p=Math.min(100,Math.round((ev.rms||0)*420));
    meter.style.transform='scaleX('+(p/100)+')';   // transform 不触发布局
    meter.style.background = p>72?'var(--warn)':'var(--ok)';
    break;}
  case 'user':{
    const w=el('div','ev');w.dataset.f='talk';
    const m=el('div','msg me');m.appendChild(el('div','who','你'));
    m.appendChild(el('div','bub',ev.text));w.appendChild(m);
    const f=el('div','fact');f.appendChild(el('span','lat','ASR '+ev.asr_ms+'ms'));
    if(ev.emotion&&ev.emotion!=='NEUTRAL')f.appendChild(el('span',null,ev.emotion));
    w.appendChild(f);add(w);
    turns++;asr.push(ev.asr_ms);updStats();break;}
  case 'tool':{
    const d=el('details','ev tool');d.dataset.f='tool';
    const s=el('summary');s.appendChild(ic('tool'));
    s.appendChild(el('span','nm',ev.name));
    const a=JSON.stringify(ev.input||{});
    s.appendChild(el('span','args',a.length>70?a.slice(0,70)+'…':a));
    d.appendChild(s);
    const p=el('pre',null,JSON.stringify(ev.input||{},null,2));d.appendChild(p);
    add(d);break;}
  case 'sentence':{
    const w=el('div','ev');w.dataset.f='talk';
    const m=el('div','msg omen');m.appendChild(el('div','who','omen'));
    m.appendChild(el('div','bub',ev.text));w.appendChild(m);
    const f=el('div','fact');
    if(ev.first_ms!=null){f.appendChild(el('span','lat'+(ev.first_ms>3000?' slow':''),
      '首句 '+ev.first_ms+'ms'));first.push(ev.first_ms);updStats();}
    if(ev.raw){const r=el('div','raw','原句含标记：'+ev.raw);w.appendChild(r);}
    if(f.childNodes.length)w.appendChild(f);
    add(w);break;}
  case 'done': if(ev.cost!=null){cost+=Number(ev.cost);updStats();} break;
  case 'mode':{
    mode=ev.value;
    const b=$('#btnMode');
    b.lastChild.textContent = mode==='headphones' ? '免提' : '耳机';
    b.title = mode==='headphones'
      ? '当前耳机模式（可插话打断）→ 点一下切免提' : '当前免提模式（不能插话打断）→ 点一下切耳机';
    document.getElementById('modeChip').textContent =
      mode==='headphones' ? '耳机 · 可插话打断' : '免提 · 不能插话打断';
    document.getElementById('modeChip').className =
      mode==='headphones' ? 'chip' : 'chip warnchip';
    note('warn','bolt','切到'+(mode==='headphones'?'耳机（可插话打断）':'免提（不能插话打断）'));
    break;}
  case 'device': note('','chat','输出设备 → '+(ev.name||ev.index)); break;
  case 'meta':{
    const lbl={clear:'清空上下文',pause:'暂停监听',resume:'继续监听',reconnect:'热重连第二大脑'}[ev.cmd]||ev.cmd;
    note('warn','bolt','元命令 · '+lbl+'「'+(ev.text||'')+'」');break;}
  case 'meta_result':{
    const m={clear:['上下文已清空','上下文清空失败'],
             reconnect:['第二大脑已连上','第二大脑没连上（Obsidian 没开着？）']};
    const okFail=m[ev.cmd]||['完成','失败'];
    note(ev.ok?'ok':'err', ev.ok?'chat':'warn', ev.ok?okFail[0]:okFail[1]);break;}
  case 'interrupt':
    interrupts++;updStats();
    note('warn','bolt','打断 · 已播 '+Number(ev.played_s||0).toFixed(2)+'s');break;
  case 'filler':
    note('','chat','填充音（等待 '+(ev.delay_ms||0)+'ms 未出声）');break;
  case 'backchannel': note('','chat','忽略应答词「'+ev.text+'」');break;
  case 'stop': note('warn','stop','停口令 · 就地闭嘴「'+ev.text+'」');break;
  case 'paused':
    paused=ev.value;
    document.body.dataset.state=paused?'paused':(document.body.dataset.state||'idle');
    $('#stateTxt').textContent=paused?'已暂停':{'idle':'在听','thinking':'思考中','speaking':'说话中'}[document.body.dataset.state]||'在听';
    $('#btnPause').lastChild.textContent=paused?'继续监听':'暂停监听';
    $('#btnPause').classList.toggle('primary',!paused);
    $('#btnStop').disabled=paused;
    note('',paused?'pause':'play',paused?'已暂停监听':'已恢复监听');break;
  case 'error': note('err','warn',String(ev.text));break;
  case 'shutdown':
    note('warn','power','正在关闭：停麦克风 + 结束脑进程…（浏览器稍后显示"已断开"属正常）');break;
 }
}

let ctlBusy=false;
async function ctl(action, value){
  if(ctlBusy) return;                       // 防重复点击（如连点"打断"发两次 POST）
  ctlBusy=true;
  const btns=[$('#btnPause'),$('#btnStop'),$('#btnMode'),$('#btnVault'),$('#btnShutdown')];
  btns.forEach(b=>{b.disabled=true;b.setAttribute('aria-busy','true');});
  const ctlr=new AbortController(); const to=setTimeout(()=>ctlr.abort(), 15000);
  try{
    const r=await fetch('/api/control',{method:'POST',
      headers:{'Content-Type':'application/json'},
      signal:ctlr.signal,
      body:JSON.stringify({action, value: value===undefined?null:value})});
    const j=await r.json(); if(!j.ok) note('err','warn',j.error||'控制失败');
  }catch(e){ note('err','warn','控制请求失败：'+e.message); }
  finally{
    clearTimeout(to);
    ctlBusy=false;
    // ⚠️ 必须把**开始禁用的每一个**都恢复。上一版只恢复了 btnStop，
    // 结果 btnPause 第一次点击后永久 disabled —— "暂停后无法继续监听"（真机踩过）。
    btns.forEach(b=>{b.removeAttribute('aria-busy');b.disabled=false;});
    $('#btnStop').disabled=paused;          // 暂停期间"打断"无意义
  }
}
$('#btnPause').onclick=()=>ctl(paused?'resume':'pause');
$('#btnStop').onclick=()=>ctl('interrupt');
$('#btnMode').onclick=()=>ctl('mode', mode==='headphones'?'speaker':'headphones');
$('#btnVault').onclick=()=>ctl('reconnect');
// ⚠️ 关闭**刻意不走 ctl()**：ctl 有 ctlBusy 门闩，若上一次控制请求卡住，关闭会被一起冻住
//    （真机踩过：点了没反应）。这里自带请求，且不 await 结果——进程马上就没，连接断是预期。
$('#btnShutdown').onclick=async ()=>{
  if(!confirm('关闭会停止麦克风并结束助手进程（含脑模型），确定吗？')) return;
  const b=$('#btnShutdown'); b.disabled=true; b.setAttribute('aria-busy','true');
  try{
    await fetch('/api/control',{method:'POST',
      headers:{'Content-Type':'application/json'}, body:JSON.stringify({action:'shutdown'})});
  }catch(e){ /* 进程正在退出，连接被切断属正常 */ }
  note('warn','power','已发送关闭指令，助手正在退出…');
};
$('#devSel').onchange=e=>ctl('device', e.target.value);

// 设备列表（系统默认输出改变后这里不会自动跟 —— 正是要手动选的原因）
fetch('/api/devices').then(r=>r.json()).then(j=>{
  if(!j.ok) return;
  const sel=$('#devSel'); sel.innerHTML='';
  const d0=document.createElement('option'); d0.value='default'; d0.textContent='系统默认输出';
  sel.appendChild(d0);
  j.devices.forEach(d=>{const o=document.createElement('option');
    o.value=String(d.index); o.textContent=d.name; sel.appendChild(o);});
});
document.querySelectorAll('.tab').forEach(t=>t.onclick=()=>setFilter(t.dataset.f));
document.body.dataset.state='idle';
$('#stateTxt').textContent='在听';
updStats();

const es=new EventSource('/events');
es.onopen=()=>{$('#conn').textContent='已连接';};
es.onerror=()=>{$('#conn').textContent='断开（自动重连）';};
es.onmessage=e=>{try{on(JSON.parse(e.data));}catch(_){}};
</script></body></html>"""

PAGE = (PAGE
        .replace("__PAUSE__", ic("pause"))
        .replace("__STOP__", ic("stop"))
        .replace("__VAULT__", ic("link"))
        .replace("__POWER__", ic("power"))
        .replace("__MODEICON__", ic("mic")))


@app.get("/", response_class=HTMLResponse)
def index():
    """⚠️ 模式标签由**服务端**注入，不走事件。

    真机踩过：原先靠 JS 在 `session_start` 事件里插入标注，结果受浏览器缓存与
    事件时序影响，用户看不到自己正处在哪个模式（免提/耳机行为差别很大，
    看不到就会以为坏了）。服务端本来就知道 —— 直接写进 HTML，零时序依赖。
    """
    cfg = getattr(ORCH, "cfg", None)
    if cfg is not None and not cfg.barge_in:
        label = "免提 · 不能插话打断"
        cls = "chip warnchip"
    else:
        label = "耳机 · 可插话打断"
        cls = "chip"
    page = PAGE.replace(
        '<span class="chip" id="modeChip">__MODE__</span>',
        f'<span class="{cls}" id="modeChip" title="音频模式">{label}</span>')
    return HTMLResponse(page, headers={"Cache-Control": "no-store"})


@app.get("/events")
def events():
    q = BUS.subscribe()

    def gen():
        try:
            while True:
                try:
                    ev = q.get(timeout=15)
                except queue.Empty:
                    yield ": ping\n\n"
                    continue
                yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
        finally:
            BUS.unsubscribe(q)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


def serve(host: str = "127.0.0.1", port: int = 8848):
    import uvicorn
    # ⚠️ 强制只绑本地：本项目被开放端口咬过（8006/22400 至今可达）
    assert host in ("127.0.0.1", "localhost"), "仪表盘只允许绑定本地回环"
    print(f"[仪表盘] http://{host}:{port}  （仅本机可访问）", flush=True)
    uvicorn.run(app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    serve()
